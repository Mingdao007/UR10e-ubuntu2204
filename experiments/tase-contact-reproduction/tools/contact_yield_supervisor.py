"""Continuous, output-only observation around the sole native live writer.

Load/Play/Stop remain main-owner operations. The observer never owns input
registers or sends a robot command. No endpoint is opened at import time.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import queue
import subprocess
import time
import uuid

from contact_yield_live_contract import (
    CONTACT_PROGRAM, HOME_PROGRAM, PACKAGE_DIR, READABLE_RUNTIME_IDENTITY,
    RUNTIME_PROTOCOL, load_identity_contract, software_identity_limbs,
)
from contact_home_motion_profile import (
    HOME_ANGULAR_SPEED_GUARD_RAD_S,
    HOME_JOINT_SPEED_GUARD_RAD_S,
    HOME_TCP_SPEED_GUARD_M_S,
)
from step5d_autotune_v4_r004.transport import OUTPUT_FIELDS, R004OutputSnapshot
from tase_r013_timing_ledger import ledger_from_receipts


MAX_AGE_S = .080
DASHBOARD_FIELDS = ['is in remote control', 'safetymode', 'robotmode',
                    'running', 'programState', 'get loaded program']


def _observe(host, path, samples, errors, ready, done, excluded_cpu=None):
    from step5d_autotune_v3.rtde_client import RTDEClient
    try:
        if excluded_cpu is not None:
            available = set(os.sched_getaffinity(0))
            if excluded_cpu in available and len(available) > 1:
                available.remove(excluded_cpu)
                os.sched_setaffinity(0, available)
        with Path(path).open('x', buffering=1) as log, RTDEClient(host, timeout=.2) as client:
            client.negotiate()
            recipe, types = client.setup_outputs(100., OUTPUT_FIELDS)
            client.start()
            previous = None
            safety_fault_sent = False
            while not done.is_set():
                values = client.recv_recipe_sample(recipe, types)
                row = dict(zip(OUTPUT_FIELDS, values, strict=True))
                row['received_monotonic_s'] = time.monotonic()
                row['observed_at_s'] = time.time()
                log.write(json.dumps(row, allow_nan=False) + '\n')
                if previous is not None:
                    if row['timestamp'] <= previous['timestamp']:
                        raise RuntimeError('observer controller timestamp did not advance')
                    if (row['received_monotonic_s'] - previous['received_monotonic_s'] >= MAX_AGE_S
                        or row['timestamp'] - previous['timestamp'] >= MAX_AGE_S):
                        raise RuntimeError('observer RTDE gap exceeded 80 ms')
                previous = row
                try: samples.put_nowait(row)
                except queue.Full:
                    try: samples.get_nowait()
                    except queue.Empty: pass
                    try: samples.put_nowait(row)
                    except queue.Full: pass
                ready.set()
                if row['safety_mode'] != 1 and not safety_fault_sent:
                    errors.put_nowait(f"observer safety changed: {row['safety_mode']}")
                    safety_fault_sent = True
    except BaseException as exc:
        try: errors.put_nowait(f'{type(exc).__name__}: {exc}')
        except queue.Full: pass
        ready.set()


class ProcessObserver:
    """Dedicated process avoids sharing the 500 Hz writer's Python GIL."""
    def __init__(self, host, path, *, excluded_cpu=None):
        ctx = mp.get_context('spawn')
        self.samples, self.errors = ctx.Queue(4), ctx.Queue(1)
        self.ready, self.done = ctx.Event(), ctx.Event()
        self.process = ctx.Process(target=_observe,
            args=(host, str(path), self.samples, self.errors, self.ready, self.done,
                  None if excluded_cpu is None else int(excluded_cpu)))
        self.row = None
        self.error = None
        self.sample_count = 0

    def start(self):
        self.process.start()
        if not self.ready.wait(5.): raise RuntimeError('observer start barrier timed out')
        deadline = time.monotonic() + .080
        while self.row is None:
            self._drain()
            if self.error: raise RuntimeError(self.error)
            if time.monotonic() > deadline: raise RuntimeError('observer first sample missing')
            if self.row is None: time.sleep(.002)
        self.latest()

    def _drain(self):
        while True:
            try:
                self.row = self.samples.get_nowait()
                self.sample_count += 1
            except queue.Empty: break
        try:
            error = self.errors.get_nowait()
            self.error = self.error or error
        except queue.Empty: pass

    def latest(self, *, allow_fault=False):
        self._drain()
        if self.error and not allow_fault: raise RuntimeError(self.error)
        # After a normal TP STOP the RTDE child may close before Dashboard's
        # STOPPED acknowledgement is observed.  Stop-only verification may
        # use the final fresh sample; live/healthy reads still require the
        # observer process to remain alive.
        if not self.process.is_alive() and not allow_fault:
            raise RuntimeError('observer process exited')
        if self.row is None or not 0 <= time.monotonic()-self.row['received_monotonic_s'] < MAX_AGE_S:
            raise RuntimeError('observer latest sample is stale')
        return self.row

    def close(self):
        self.done.set()
        if self.process.pid is not None:
            self.process.join(1.)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(1.)
        for channel in (self.samples, self.errors):
            channel.cancel_join_thread()
            channel.close()


class VideoRecorder:
    """Best-effort video capture with an explicit evidence policy.

    ``required`` preserves the historical admission behavior.  In
    ``evidence-only`` mode the recorder makes a short best-effort attempt and
    records the result, but an absent publisher never becomes a motion gate.
    The controller/RTDE/sensor and recovery gates remain independent.
    """

    POLICIES = frozenset(("required", "evidence-only"))

    def __init__(self, url, directory, *, policy="required"):
        if policy not in self.POLICIES:
            raise ValueError(f"unknown video policy: {policy}")
        self.url, self.directory, self.process = url, Path(directory), None
        self.policy = policy
        self.attempted = False
        self.available = False
        self.error = None
        self.closed = False
        self.path = self.directory / "video.mkv"
        self.last_size = 0
        self.progress_at = None

    def start(self):
        self.log = (self.directory/'video-stderr.txt').open('x')
        self.attempted = True
        self.process = subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error',
            '-rtsp_transport','tcp','-i',self.url,'-c','copy','-flush_packets','1',
            '-cluster_time_limit','500',str(self.path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.log)
        self.progress_at = time.monotonic()
        # A missing publisher is an evidence gap in evidence-only mode.  Keep
        # the probe short so it cannot serialize every candidate behind an
        # unavailable camera; required mode retains the original 8 s barrier.
        deadline = time.monotonic() + (8. if self.policy == "required" else .5)
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                reason = "video recorder exited before first frame"
                if self.policy == "required":
                    raise RuntimeError(reason)
                self.error = reason
                self.process = None
                return
            size = self.path.stat().st_size if self.path.exists() else 0
            if size > 512:
                self.available = True
                self.last_size = size
                return
            time.sleep(.05)
        if self.policy == "required":
            raise RuntimeError('video recording start barrier timed out')
        self.error = "video stream was not available at recorder start"
        self._stop_process()

    def check(self):
        if self.process is None:
            if self.policy == "required":
                raise RuntimeError('video recording is not running')
            return
        if self.process.poll() is not None:
            if self.policy == "required":
                raise RuntimeError('video recording is not running')
            if self.error is None:
                self.error = "video recorder exited during run"
            self.process = None
            return
        size = self.path.stat().st_size if self.path.exists() else 0
        now = time.monotonic()
        if size > self.last_size:
            self.last_size, self.progress_at = size, now
            self.available = True
        elif self.last_size > 512 and now-self.progress_at > 2.:
            if self.policy == "required":
                raise RuntimeError('video recording has stopped advancing')
            self.error = "video recording stopped advancing"
            self._stop_process()

    def _stop_process(self):
        if self.process is None:
            return
        self.process.terminate()
        try: self.process.wait(3.)
        except subprocess.TimeoutExpired:
            self.process.kill(); self.process.wait()
        self.process = None

    def close(self):
        self._stop_process()
        if hasattr(self, 'log'): self.log.close()
        self.closed = True

    def evidence(self):
        return {
            'policy': self.policy,
            'attempted': self.attempted,
            'available': self.available,
            'path': str(self.path) if self.available else None,
            'error': self.error,
            'required_for_motion_admission': self.policy == 'required',
        }

    def healthy(self):
        """Return true only while a live capture is currently advancing."""
        if not self.available or self.process is None:
            return False
        if self.process.poll() is not None:
            return False
        if self.last_size <= 512 or self.progress_at is None:
            return False
        return time.monotonic() - self.progress_at <= 2.0


def stationary(row):
    return (math.hypot(*row['actual_TCP_speed'][:3]) <= .0005
            and math.hypot(*row['actual_TCP_speed'][3:]) <= .005
            and max(abs(v) for v in row['actual_qd']) <= .001)


class ResidentSupervisor:
    def __init__(self, *, observer, video, read_dashboard, writer, target,
                 home_pose=None, home_q=None, clock=time.monotonic, sleep=time.sleep):
        self.observer, self.video = observer, video
        self.read_dashboard, self.writer, self.target = read_dashboard, writer, target
        self.home_pose = None if home_pose is None else tuple(float(value) for value in home_pose)
        self.home_q = None if home_q is None else tuple(float(value) for value in home_q)
        self.clock, self.sleep = clock, sleep
        self.audit = {
            'success': False,
            'events': [],
            'lifecycle_events': [],
            'motion_commands_from_supervisor': 0,
            'program_stopped': False,
        }

    def _mark_lifecycle(self, stage: str, event: str = 'start') -> None:
        """Record only supervisor-owned transitions in its monotonic domain."""
        self.audit['lifecycle_events'].append({
            'stage': str(stage),
            'event': str(event),
            'timestamp_s': float(self.clock()),
        })

    def check(self, *, idle=False, identity=False):
        row = self.observer.latest()
        self.audit['last_sample'] = row
        self.video.check()
        if row['safety_mode'] != 1 or row['robot_mode'] != 7:
            raise RuntimeError(f"robot gate failed: safety={row['safety_mode']} robot={row['robot_mode']}")
        if idle and not stationary(row): raise RuntimeError('unexpected motion in resident idle')
        # The TP publishes RETURNING before any Home motion.  Keep its joint
        # and Cartesian return envelope independent from PATH admission: a
        # return sample that exceeds the ordinary Home guards stops the live
        # owner and enters the same automatic Home recovery path.
        if row.get('output_int_register_26') == 40:
            if max(abs(float(value)) for value in row['actual_qd']) > HOME_JOINT_SPEED_GUARD_RAD_S:
                raise RuntimeError('joint Home return speed envelope violated')
            if math.hypot(*row['actual_TCP_speed'][:3]) > HOME_TCP_SPEED_GUARD_M_S:
                raise RuntimeError('joint Home return Cartesian speed envelope violated')
            if math.hypot(*row['actual_TCP_speed'][3:]) > HOME_ANGULAR_SPEED_GUARD_RAD_S:
                raise RuntimeError('joint Home return angular speed envelope violated')
        if (not math.isclose(row['payload'], .413, abs_tol=.0005)
            or any(abs(a-b)>.00005 for a,b in zip(row['payload_cog'],[.0011,.0031,.0163]))
            or any(abs(a-b)>.00005 for a,b in zip(row['tcp_offset'],[0,0,.0874,0,0,0]))):
            raise RuntimeError('observer EOAT binding differs')
        if identity and [row[f'output_int_register_{i}'] for i in (32,33,34)] != [RUNTIME_PROTOCOL,*READABLE_RUNTIME_IDENTITY]:
            raise RuntimeError('resident wire identity differs')
        return row

    def _at_home(self, row):
        if self.home_pose is None or self.home_q is None:
            return True
        if max(abs(a - b) for a, b in zip(row['actual_q'], self.home_q, strict=True)) > .02:
            return False
        if math.dist(row['actual_TCP_pose'][:3], self.home_pose[:3]) > .0005:
            return False
        try:
            import numpy as np
            from contact_yield_math import so3_exp, so3_log
            orientation_error = float(np.linalg.norm(
                so3_log(so3_exp(row['actual_TCP_pose'][3:]) @ so3_exp(self.home_pose[3:]).T)
            ))
        except Exception:
            orientation_error = math.dist(row['actual_TCP_pose'][3:], self.home_pose[3:])
        return orientation_error <= .01

    def _wait(self, *, running, after, timeout=5., healthy=True, prior_timestamp=None,
              require_home=False):
        deadline = self.clock()+timeout
        while self.clock() < deadline:
            # Stop verification preserves raw safety mode rather than requiring NORMAL.
            row = self.check(idle=True) if healthy else self.observer.latest(allow_fault=True)
            dash = self.read_dashboard()
            self.audit['events'].append({'monotonic_s':self.clock(),'dashboard':dash})
            if healthy and (dash['is in remote control']!='true'
                            or dash['safetymode']!='Safetymode: NORMAL'):
                raise RuntimeError(f'Dashboard safety/Remote changed: {dash}')
            if dash['get loaded program'] != 'Loaded program: '+self.target:
                raise RuntimeError('loaded program differs')
            desired = 'PLAYING' if running else 'STOPPED'
            if (dash['running']==f"Program running: {'true' if running else 'false'}"
                and dash['programState'].startswith(desired)
                and row['runtime_state']==(2 if running else 1)
                and row['received_monotonic_s'] >= after and stationary(row)
                and (not require_home or self._at_home(row))
                and (prior_timestamp is None or row['timestamp'] > prior_timestamp)):
                if running and [row[f'output_int_register_{i}'] for i in (32,33,34)] != [RUNTIME_PROTOCOL,*READABLE_RUNTIME_IDENTITY]:
                    self.sleep(.02)
                    continue
                return {'dashboard':dash,'sample':row}
            self.sleep(.02)
        raise RuntimeError(f'{desired} was not observed before timeout')

    def stop_program_and_verify(self, *, reason='operator_stop', protocol_stop=None,
                                home_proof=None):
        """Issue the real Dashboard STOP and verify a fresh stopped Home image."""

        del protocol_stop, home_proof
        prior = self.audit.get('last_sample', {}).get('timestamp')
        requested_at = self.clock()
        self._mark_lifecycle('PROGRAM_STOP', 'requested')
        self.writer.write('stop')
        observed = self._wait(
            running=False,
            after=requested_at,
            healthy=False,
            prior_timestamp=prior,
            require_home=True,
        )
        sample = observed['sample']
        if sample.get('runtime_state') != 1 or not stationary(sample) or not self._at_home(sample):
            raise RuntimeError('fresh Dashboard STOPPED image is not stationary approved Home')
        self.audit['dashboard_stop'] = observed
        self.audit['program_stopped'] = True
        self._mark_lifecycle('PROGRAM_STOP', 'verified')
        return {
            'program_stopped': True,
            'stopped': True,
            'dashboard': observed['dashboard'],
            'sample': sample,
            'reason': str(reason),
            'requested_monotonic_s': requested_at,
        }

    def run(self, body, before_load=None, *, execute_program=True):
        play_attempted = False
        body_result = None
        try:
            # Establish the video barrier before starting the bounded RTDE
            # observer queue.  Starting the observer first lets ffmpeg's
            # startup delay fill the four-row queue; the child then drops
            # samples while the parent is not draining it and the first
            # resident check can report a false stale-observation failure.
            self.video.start(); self.observer.start()
            self.check(idle=True)
            initial=self.read_dashboard()
            if (initial['is in remote control']!='true' or initial['safetymode']!='Safetymode: NORMAL'
                or initial['robotmode']!='Robotmode: RUNNING' or initial['running']!='Program running: false'
                or not initial['programState'].startswith('STOPPED')):
                raise RuntimeError(f'initial Remote/stopped gate failed: {initial}')
            if execute_program:
                if before_load is not None: before_load(self)
                at=self.clock(); self.writer.write('load '+self.target)
                self._wait(running=False,after=at)
                play_attempted=True
                at=self.clock(); self.writer.write('play')
                self._wait(running=True,after=at)
                until=self.clock()+1.
                while self.clock()<until:
                    self.check(idle=True,identity=True); self.sleep(.002)
            body_result = body(self)
            self.audit['body'] = body_result
            if isinstance(body_result, dict):
                for event in body_result.get('lifecycle_events', ()):
                    if isinstance(event, dict):
                        self.audit['lifecycle_events'].append(dict(event))
            if not any(
                isinstance(row, dict) and row.get('stage') == 'HOME_CHECK'
                for row in self.audit['lifecycle_events']
            ):
                self._mark_lifecycle('HOME_CHECK', 'verified')
            body_failed = (
                isinstance(body_result, dict)
                and (
                    bool(body_result.get('error'))
                    or (
                        body_result.get('success') is not True
                        if body_result.get('research_campaign') is True
                        else body_result.get('evidence_eligible') is False
                    )
                )
            )
            if body_failed:
                self.audit['success'] = False
                self.audit['error'] = str(
                    body_result.get('error') or 'body evidence was not eligible'
                )
            else:
                self.audit['success'] = True
        except BaseException as exc:
            self.audit['error']=f'{type(exc).__name__}: {exc}'
        finally:
            if play_attempted:
                try:
                    prior = self.audit.get('last_sample',{}).get('timestamp')
                    if not any(
                        isinstance(row, dict) and row.get('stage') == 'STOP'
                        for row in self.audit['lifecycle_events']
                    ):
                        self._mark_lifecycle('STOP', 'requested')
                    if not self.audit.get('program_stopped'):
                        at=self.clock(); self.writer.write('stop')
                        self.audit['dashboard_stop']=self._wait(
                            running=False,
                            after=at,
                            healthy=False,
                            prior_timestamp=prior,
                            require_home=True,
                        )
                        self.audit['program_stopped'] = True
                except BaseException as exc:
                    self.audit['success']=False
                    self.audit['stop_error']=f'{type(exc).__name__}: {exc}'
            for label, resource in (('video',self.video),('observer',self.observer)):
                try: resource.close()
                except BaseException as exc:
                    self.audit['success']=False
                    self.audit.setdefault('close_errors',[]).append(f'{label}: {type(exc).__name__}: {exc}')
            evidence = getattr(self.video, 'evidence', None)
            self.audit['video_evidence'] = evidence() if callable(evidence) else None
        return self.audit


def _prewarm(method):
    del method
    from contact_yield_live_writer import _prewarm_qp
    from contact_yield_protocol import QP_LIBRARY_PATH
    _prewarm_qp(QP_LIBRARY_PATH)


def _write_receipts(directory, row, proof, contract, *, session_epoch=1,
                    resident_session_id=None):
    hi,lo=software_identity_limbs(contract)
    def write(name, body):
        body['receipt_sha256']=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with (directory/name).open('x') as stream: json.dump(body,stream,indent=2)
    common={'runtime_protocol':RUNTIME_PROTOCOL,'runtime_digest_hi':hi,'runtime_digest_lo':lo}
    write('controller_receipt.json',dict(common,program=contract.program,
        controller_target=contract.raw['script2']['controller_target'],
        **{f'{k}_sha256':v for k,v in contract.triplet.items()},observed_at_s=proof['observed_at_s'],
        eoat_identity_sha256=contract.eoat_sha256,stationary=stationary(row),safety_mode='NORMAL',
        route_id='r006-yield-live',readback={'payload_kg':row['payload'],'payload_cog_m':row['payload_cog'],
        'tcp_offset_m_rad':row['tcp_offset'],'actual_TCP_speed':row['actual_TCP_speed']},
        provenance={'state':'supervisor-rtde.jsonl','triplet':'readback-results.json',
                    'wire_identity':[RUNTIME_PROTOCOL,*READABLE_RUNTIME_IDENTITY]}))
    write('home_start_receipt.json',{'schema':'yield-live-entry/home-start-receipt-v1',
        'script_sha256':contract.script1_sha256['script'],'observed_at_s':row['observed_at_s'],
        'final_pose':row['actual_TCP_pose'],'final_q':row['actual_q'],'stationary':stationary(row),
        'safety_mode':'NORMAL','eoat_identity_sha256':contract.eoat_sha256,
        'provenance':'supervisor-rtde.jsonl; actual observed pose, not desired Home'})
    write('runtime_evidence.json',dict(common,program=contract.program,
        script_sha256=contract.triplet['script'],session_epoch=session_epoch,
        resident_session_id=resident_session_id or str(uuid.uuid4()),
        program_running=row['runtime_state']==2,uninterrupted=True,observed_at_s=row['observed_at_s'],
        provenance='continuous supervisor-rtde.jsonl from before Load/Play'))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--action',choices=['resident-check','qualify','pilot'],required=True)
    p.add_argument('--method',default='SFC'); p.add_argument('--duration',default='2')
    p.add_argument('--run-dir',type=Path,required=True); p.add_argument('--readback-dir',type=Path,required=True)
    p.add_argument('--parameter-file',type=Path)
    p.add_argument('--resident-parameter-manifest',type=Path)
    p.add_argument('--resident-attempts',type=int,default=1)
    p.add_argument('--controller-host',default='192.168.1.18'); p.add_argument('--kunwei-host',default='192.168.50.25')
    p.add_argument('--control-cpu',type=int,required=True)
    p.add_argument('--video-url',default='rtsp://127.0.0.1:8554/arm')
    p.add_argument('--video-policy',choices=sorted(VideoRecorder.POLICIES),default='required')
    a=p.parse_args(argv)
    if a.resident_attempts < 1 or (a.action != 'pilot' and a.resident_attempts != 1):
        p.error('resident attempts require a positive pilot count')
    parameter_files = None
    if a.resident_parameter_manifest is not None:
        if a.action != 'pilot' or a.method != 'TASE_RNN_MATURE' or a.duration not in {'r013_60', 'r013_60_rate400'}:
            p.error('resident parameter manifest requires a 60 s TASE_RNN_MATURE pilot')
        manifest_path = a.resident_parameter_manifest.expanduser().resolve()
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if not isinstance(manifest, dict) or manifest.get('schema') != 'tase.resident-parameter-manifest-v1':
            p.error('resident parameter manifest schema differs')
        names = manifest.get('parameter_files')
        if not isinstance(names, list) or len(names) != a.resident_attempts or not all(
            isinstance(name, str) and name for name in names
        ):
            p.error('resident parameter manifest must name each attempt')
        parameter_files = [
            (manifest_path.parent / name).resolve() for name in names
        ]
        if a.parameter_file is not None and a.parameter_file.expanduser().resolve() != parameter_files[0]:
            p.error('initial parameter file differs from resident manifest')
        from tase_contact_provider import load_tase_outer_config
        for parameter_file in parameter_files:
            _, binding = load_tase_outer_config(parameter_file)
            selected_protocol = (
                'figure8_window60_r013_rate400_v1' if a.duration == 'r013_60_rate400'
                else 'figure8_window60_r013_compat_v1'
            )
            if (binding.get('protocol_id') != selected_protocol
                or binding.get('duration_token') != a.duration):
                p.error(f'resident candidate protocol differs: {parameter_file}')
        a.parameter_file = parameter_files[0]
    # Resolve once at the process boundary so every receipt, observer and
    # recovery owner shares the same directory even when the caller starts
    # from the worktree root or the experiment root.
    a.run_dir = a.run_dir.expanduser().resolve()
    a.readback_dir = a.readback_dir.expanduser().resolve()
    from contact_yield_method_registry import load_live_entry_config,resolve_method
    if not load_live_entry_config()['user_standing_live_authority']:
        raise RuntimeError('live authority is revoked; no endpoint opened')
    resolve_method(a.method)
    from contact_yield_live_path import parse_live_duration
    if a.action=='pilot': parse_live_duration(a.duration)
    if (a.run_dir/'supervisor-result.json').exists(): raise RuntimeError('run already completed')
    contract=load_identity_contract()
    proof=json.loads((a.run_dir/'readback-results.json').read_text())
    if proof.get('pass') is not True or not 0<=time.time()-proof['observed_at_s']<300:
        raise RuntimeError('fresh verified read-back required')
    for base in (CONTACT_PROGRAM,HOME_PROGRAM):
        for ext in ('script','txt','urp'):
            if (a.readback_dir/base/f'{base}.{ext}').read_bytes()!=(PACKAGE_DIR/f'{base}.{ext}').read_bytes():
                raise RuntimeError('read-back triplet bytes differ')
    _prewarm(a.method)
    from contact_yield_live import _parse_args,run_live
    from contact_yield_live_writer import load_software_baseline
    from contact_yield_live_contract import contact_home_binding
    def before_load(s):
        if not 0 <= time.time()-proof['observed_at_s'] < 300:
            raise RuntimeError('controller read-back expired before Load')
        if a.action != 'resident-check':
            load_software_baseline(a.run_dir,contract,now_s=time.time())
            row=s.check(idle=True)
            contact_home_binding(contract=contract,final_pose=row['actual_TCP_pose'],
                final_q=row['actual_q'],observed_at_s=row['observed_at_s'],
                receipt_sha256=hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest())
    from step5d_remote_startup import RemoteDashboardWriter,dashboard_exchange
    from step5d_autotune_v4_r014.dispatcher import WriterLock
    from run_contact_home import INSTALLED_LOCK
    target=contract.raw['script2']['controller_target']
    observer=ProcessObserver(
        a.controller_host,
        a.run_dir/'supervisor-rtde.jsonl',
        excluded_cpu=a.control_cpu,
    )
    video=VideoRecorder(a.video_url,a.run_dir,policy=a.video_policy)
    supervisor=ResidentSupervisor(observer=observer,video=video,
        read_dashboard=lambda:dashboard_exchange(a.controller_host,DASHBOARD_FIELDS),
        writer=RemoteDashboardWriter(a.controller_host,load_target=target),target=target,
        home_pose=contract.home_pose, home_q=contract.home_q)
    deferred_seals = []
    from contact_yield_resident_session import refresh_live_preparation
    def body(s):
        if a.action=='resident-check':
            # The supervisor's ProcessObserver already owns the sole RTDE
            # output recipe. Opening a second recipe here can starve the
            # observer queue on a UR controller and create a false stale
            # failure. Reuse that observer and count the frames it drains.
            before_frames=observer.sample_count
            deadline=time.monotonic()+10.
            while time.monotonic()<deadline:
                s.check(idle=True,identity=True)
                time.sleep(.002)
            frames=observer.sample_count-before_frames
            if frames < 100: raise RuntimeError('native resident check has insufficient RTDE frames')
            return {'idle_seconds':10.,'arm_dispatched':False,'input_packets_sent':0,
                    'native_recipe_frames':frames,'native_recipe_excludes_onrobot_input24':True,
                    'requested_input_recipe':list(__import__('contact_yield_transport').NATIVE_INPUT_FIELDS),
                    'wire_identity':[RUNTIME_PROTOCOL,*READABLE_RUNTIME_IDENTITY],
                    'readback_triplet':dict(contract.triplet)}
        _write_receipts(a.run_dir,s.check(idle=True,identity=True),proof,contract)
        cli=[a.action,'--method',a.method,'--run-dir',str(a.run_dir),
             '--controller-host',a.controller_host,'--kunwei-host',a.kunwei_host,
             '--control-cpu',str(a.control_cpu),'--attempt-id','r006-supervised-'+a.action]
        if a.parameter_file is not None:
            cli += ['--parameter-file', str(a.parameter_file)]
        if a.action=='pilot':cli+=['--duration',a.duration]
        return run_live(
            _parse_args(cli),
            observer_guard=lambda:s.check(identity=True),
            # This function is called while the supervisor owns the global
            # writer lock.  Defer monitored Home until the context below has
            # released it; otherwise recovery collides with its own lock.
            defer_recovery=True,
            dashboard_stop_and_verify=supervisor.stop_program_and_verify,
            deferred_seals=deferred_seals,
            attempt_count=a.resident_attempts,
            parameter_files=parameter_files,
            research_campaign=parameter_files is not None,
            refresh_readback=refresh_live_preparation,
        )
    with WriterLock(INSTALLED_LOCK):
        result=supervisor.run(
            body,
            before_load=before_load if a.action != 'resident-check' else None,
            execute_program=a.action != 'resident-check',
        )
    result['video_policy'] = a.video_policy
    if a.action in ('qualify','pilot') and not result.get('success'):
        try:
            from run_contact_recovery import recover_failed_contact_run
            result['autonomous_home_recovery']=recover_failed_contact_run(
                a.run_dir, a.controller_host, a.video_url,
                video_policy=a.video_policy,
            )
        except BaseException as exc:
            # A recovery-owner exception is itself a commandable recovery
            # event.  Give the Home module one last direct, monitored attempt
            # before recording BLOCKED; only its communication/safety/
            # geometry denial may leave the robot without a verified Home.
            try:
                from run_contact_recovery import _emergency_home_when_commandable
                import inspect
                fallback_kwargs = {
                    'video_url': a.video_url,
                    'video_policy': a.video_policy,
                }
                if 'video_url' not in inspect.signature(_emergency_home_when_commandable).parameters:
                    fallback_kwargs = {}
                result['autonomous_home_recovery'] = _emergency_home_when_commandable(
                    a.run_dir,
                    a.run_dir.with_name(a.run_dir.name + '-autonomous-home-fallback'),
                    a.controller_host,
                    PACKAGE_DIR,
                    reason=exc,
                    **fallback_kwargs,
                )
            except BaseException as fallback_exc:
                result['autonomous_home_recovery']={
                    'success':False,
                    'motion':False,
                    'state':'BLOCKED',
                    'phase':'recovery-dispatch',
                    'error':f'{type(fallback_exc).__name__}: {fallback_exc}',
                    'source_attempt':str(a.run_dir),
                    'trial_stays_failed':True,
                    'recovery_policy':'AUTO_HOME_WHEN_COMMANDABLE',
                    'home_required':True,
                    'recovery_owner_invoked':True,
                    'home_commandability_checked':False,
                    'home_attempted':False,
                    'home_commandable':False,
                    'home_motion_dispatched':False,
                    'home_blocked':True,
                    'home_blocked_reason':f'{type(fallback_exc).__name__}: {fallback_exc}',
                }
    # Fault recovery owns the robot before any potentially large serialization.
    for seal in deferred_seals:
        try:
            seal()
        except Exception as exc:
            result['success'] = False
            result.setdefault('seal_errors', []).append(f'{type(exc).__name__}: {exc}')
    dispatch_path = a.run_dir / 'dispatch_receipt.json'
    dispatch_receipt = {}
    if dispatch_path.is_file() and not dispatch_path.is_symlink():
        try:
            loaded = json.loads(dispatch_path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                dispatch_receipt = loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            dispatch_receipt = {}
    if dispatch_receipt:
        dispatch_receipt['video_policy'] = a.video_policy
        dispatch_receipt['video_evidence'] = result.get('video_evidence')
        try:
            temporary = dispatch_path.with_suffix('.video.tmp')
            temporary.write_text(json.dumps(dispatch_receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            temporary.replace(dispatch_path)
        except Exception as exc:
            result['video_evidence_persist_error'] = f'{type(exc).__name__}: {exc}'
    try:
        timing_ledger = ledger_from_receipts(
            str(dispatch_receipt.get('attempt_id') or f'r006-supervised-{a.action}'),
            dispatch_receipt=dispatch_receipt,
            supervisor_result=result,
        ).as_dict()
        result['timing_ledger'] = timing_ledger
        if dispatch_receipt:
            dispatch_receipt['timing_ledger'] = timing_ledger
            temporary = dispatch_path.with_suffix('.timing.tmp')
            temporary.write_text(json.dumps(dispatch_receipt, indent=2, sort_keys=True) + '\n', encoding='utf-8')
            temporary.replace(dispatch_path)
    except Exception as exc:
        # Timing reduction is diagnostic.  A malformed or late auxiliary
        # event must never prevent the supervisor from sealing the original
        # stop/recovery result.
        result['timing_ledger_error'] = f'{type(exc).__name__}: {exc}'
    with (a.run_dir/'supervisor-result.json').open('x') as out: json.dump(result,out,indent=2)
    print(json.dumps(result,indent=2))
    return 0 if result['success'] else 1


if __name__=='__main__': raise SystemExit(main())
