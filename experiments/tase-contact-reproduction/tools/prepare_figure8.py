"""Fresh stopped-Home sensor capture and installed-package read-back.

Never Load/Play, send motion, or hardware-tare. The supervisor owns motion.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time
import numpy as np
from contact_recovery_readback import fetch_recovery_readback
from contact_yield_live_contract import CONTACT_PROGRAM, HOME_PROGRAM, load_identity_contract
from contact_yield_math import so3_exp, so3_log
from contact_yield_method_registry import resolve_method
from contact_yield_supervisor import VideoRecorder
from run_contact_home import INSTALLED_LOCK
from run_contact_recovery import check_dashboard
from step5d_autotune_v4_r004.transport import LiveR004KunweiTransport
from step5d_autotune_v4_r012.register_transport import R012LiveRTDETransport
from step5d_autotune_v4_r014.dispatcher import WriterLock
from step5d_eoat_profiles import load_new_eoat_profile


def validate_sample(output, wrench, received, now, contract, profile):
    if output is None or not output.safety_normal or output.runtime_state != 1:
        raise ValueError('baseline needs a stopped NORMAL robot')
    for at in (output.received_monotonic_s, received):
        if at is None or not 0 <= now-at < .080:
            raise ValueError('baseline observation is stale')
    pose = np.asarray(output.tcp_pose_m_rad)
    if (np.linalg.norm(pose[:3]-contract.home_pose[:3]) > .0005
        or np.linalg.norm(so3_log(so3_exp(pose[3:]) @ so3_exp(contract.home_pose[3:]).T)) > .01):
        raise ValueError('not at the configured figure-eight clearance Home; run scripts/home.sh first')
    if (max(map(abs, output.qd_rad_s)) >= .001
        or max(map(abs, output.tcp_speed_m_s_rad_s)) >= .001):
        raise ValueError('baseline robot is moving')
    w = np.asarray(wrench, dtype=float)
    if w.shape != (6,) or not np.all(np.isfinite(w)) or np.linalg.norm(w[:3]) >= 20 or np.linalg.norm(w[3:]) >= 2:
        raise ValueError('baseline raw wrench invalid or excessive')
    if (abs(output.payload_kg-profile.payload_kg) > .0005
        or np.max(np.abs(np.asarray(output.payload_cog_m)-profile.cog_m)) > .00005
        or np.max(np.abs(np.asarray(output.tcp_offset_m_rad)-profile.controller_tcp_m_rad)) > .00005):
        raise ValueError('baseline EOAT identity differs')


def prepare(run_dir, method):
    resolve_method(method)  # Reject unsupported methods before any device access.
    out = Path(run_dir)
    contract = load_identity_contract()
    profile = load_new_eoat_profile()
    rows = []
    with WriterLock(INSTALLED_LOCK):
        check_dashboard('192.168.1.18')
        fetch_recovery_readback(out, basenames=(CONTACT_PROGRAM, HOME_PROGRAM))
        rtde = R012LiveRTDETransport('192.168.1.18')
        sensor = LiveR004KunweiTransport('192.168.50.25', port=5152)
        video_dir = out/'baseline-video'
        video_dir.mkdir()
        video = VideoRecorder('rtsp://127.0.0.1:8554/arm', video_dir)
        try:
            video.start(); rtde.open(); sensor.open()
            started = time.monotonic()
            last_sensor = None
            latest_output = None
            while time.monotonic()-started < 10.:
                fresh_output = rtde.poll_output(wait_s=.004)
                if fresh_output is not None:
                    latest_output = fresh_output
                output = latest_output  # Keep its real receive timestamp.
                wrench, received = sensor.poll()
                now = time.monotonic()
                video.check()
                # Startup only: no receipts or mean include the first 0.5 s.
                if now-started < .5:
                    continue
                validate_sample(output, wrench, received, now, contract, profile)
                if received == last_sensor:
                    continue
                last_sensor = received
                rows.append({'robot': asdict(output), 'wrench_n_nm': list(wrench),
                             'sensor_observed_monotonic_s': received, 'poll_monotonic_s': now})
            check_dashboard('192.168.1.18')
            if len(rows) < 100:
                raise ValueError('insufficient distinct sensor frames')
            capture = out/'baseline-frames.json'
            capture.write_text(json.dumps(rows)+'\n')
            values = np.asarray([r['wrench_n_nm'] for r in rows])
            receipt = {'schema': 'yield-software-baseline-v1',
                'observed_at_s': rows[-1]['robot']['observed_at_s'],
                'mean_wrench_n_nm': values.mean(axis=0).tolist(),
                'std_wrench_n_nm': values.std(axis=0).tolist(),
                'stationary': True, 'no_contact': True,
                'no_contact_basis': 'configured clearance Home, unchanged bench geometry; operator-attended task authorization; video retained',
                'eoat_identity_sha256': profile.profile_sha256,
                'capture_file': capture.name,
                'capture_sha256': hashlib.sha256(capture.read_bytes()).hexdigest(),
                'steady_samples': len(rows), 'claim_scope': 'software subtraction; no hardware tare'}
            (out/'software_baseline_receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
        finally:
            sensor.close(); rtde.close(); video.close()
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--method', default='TASE_RNN_MATURE')
    args = parser.parse_args()
    prepare(args.run_dir, args.method)

if __name__ == '__main__':
    main()
