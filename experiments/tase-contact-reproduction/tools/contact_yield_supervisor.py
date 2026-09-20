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
from pathlib import Path
import queue
import subprocess
import time
import uuid

from contact_yield_live_contract import (
    CONTACT_PROGRAM, HOME_PROGRAM, PACKAGE_DIR, READABLE_RUNTIME_IDENTITY,
    RUNTIME_PROTOCOL, load_identity_contract, software_identity_limbs,
)
from step5d_autotune_v4_r004.transport import OUTPUT_FIELDS, R004OutputSnapshot


MAX_AGE_S = .080
DASHBOARD_FIELDS = ['is in remote control', 'safetymode', 'robotmode',
                    'running', 'programState', 'get loaded program']


def _observe(host, path, samples, errors, ready, done):
    from step5d_autotune_v3.rtde_client import RTDEClient
    try:
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
    def __init__(self, host, path):
        ctx = mp.get_context('spawn')
        self.samples, self.errors = ctx.Queue(4), ctx.Queue(1)
        self.ready, self.done = ctx.Event(), ctx.Event()
        self.process = ctx.Process(target=_observe,
            args=(host, str(path), self.samples, self.errors, self.ready, self.done))
        self.row = None
        self.error = None

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
            try: self.row = self.samples.get_nowait()
            except queue.Empty: break
        try:
            error = self.errors.get_nowait()
            self.error = self.error or error
        except queue.Empty: pass

    def latest(self, *, allow_fault=False):
        self._drain()
        if self.error and not allow_fault: raise RuntimeError(self.error)
        if not self.process.is_alive(): raise RuntimeError('observer process exited')
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
    def __init__(self, url, directory):
        self.url, self.directory, self.process = url, Path(directory), None
        self.last_size = 0
        self.progress_at = None

    def start(self):
        self.log = (self.directory/'video-stderr.txt').open('x')
        self.path = self.directory/'video.mkv'
        self.process = subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error',
            '-rtsp_transport','tcp','-i',self.url,'-c','copy','-flush_packets','1',
            '-cluster_time_limit','500',str(self.path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=self.log)
        self.progress_at = time.monotonic()
        deadline = time.monotonic()+8.
        while time.monotonic() < deadline:
            self.check()
            if self.path.exists() and self.path.stat().st_size > 512: return
            time.sleep(.05)
        raise RuntimeError('video recording start barrier timed out')

    def check(self):
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError('video recording is not running')
        size = self.path.stat().st_size if self.path.exists() else 0
        now = time.monotonic()
        if size > self.last_size:
            self.last_size, self.progress_at = size, now
        elif self.last_size > 512 and now-self.progress_at > 2.:
            raise RuntimeError('video recording has stopped advancing')

    def close(self):
        if self.process is not None:
            self.process.terminate()
            try: self.process.wait(3.)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait()
        if hasattr(self, 'log'): self.log.close()


def stationary(row):
    return (math.hypot(*row['actual_TCP_speed'][:3]) <= .0005
            and math.hypot(*row['actual_TCP_speed'][3:]) <= .005
            and max(abs(v) for v in row['actual_qd']) <= .001)


class ResidentSupervisor:
    def __init__(self, *, observer, video, read_dashboard, writer, target,
                 clock=time.monotonic, sleep=time.sleep):
        self.observer, self.video = observer, video
        self.read_dashboard, self.writer, self.target = read_dashboard, writer, target
        self.clock, self.sleep = clock, sleep
        self.audit = {'success': False, 'events': [], 'motion_commands_from_supervisor': 0}

    def check(self, *, idle=False, identity=False):
        row = self.observer.latest()
        self.audit['last_sample'] = row
        self.video.check()
        if row['safety_mode'] != 1 or row['robot_mode'] != 7:
            raise RuntimeError(f"robot gate failed: safety={row['safety_mode']} robot={row['robot_mode']}")
        if idle and not stationary(row): raise RuntimeError('unexpected motion in resident idle')
        if (not math.isclose(row['payload'], .413, abs_tol=.0005)
            or any(abs(a-b)>.00005 for a,b in zip(row['payload_cog'],[.0011,.0031,.0163]))
            or any(abs(a-b)>.00005 for a,b in zip(row['tcp_offset'],[0,0,.0874,0,0,0]))):
            raise RuntimeError('observer EOAT binding differs')
        if identity and [row[f'output_int_register_{i}'] for i in (32,33,34)] != [RUNTIME_PROTOCOL,*READABLE_RUNTIME_IDENTITY]:
            raise RuntimeError('resident wire identity differs')
        return row

    def _wait(self, *, running, after, timeout=5., healthy=True, prior_timestamp=None):
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
                and (prior_timestamp is None or row['timestamp'] > prior_timestamp)):
                if running and [row[f'output_int_register_{i}'] for i in (32,33,34)] != [RUNTIME_PROTOCOL,*READABLE_RUNTIME_IDENTITY]:
                    self.sleep(.02)
                    continue
                return {'dashboard':dash,'sample':row}
            self.sleep(.02)
        raise RuntimeError(f'{desired} was not observed before timeout')

    def run(self, body, before_load=None):
        play_attempted = False
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
            if before_load is not None: before_load(self)
            at=self.clock(); self.writer.write('load '+self.target)
            self._wait(running=False,after=at)
            play_attempted=True
            at=self.clock(); self.writer.write('play')
            self._wait(running=True,after=at)
            until=self.clock()+1.
            while self.clock()<until:
                self.check(idle=True,identity=True); self.sleep(.002)
            self.audit['body']=body(self)
            self.audit['success']=True
        except BaseException as exc:
            self.audit['error']=f'{type(exc).__name__}: {exc}'
        finally:
            if play_attempted:
                try:
                    prior = self.audit.get('last_sample',{}).get('timestamp')
                    at=self.clock(); self.writer.write('stop')
                    self.audit['dashboard_stop']=self._wait(running=False,after=at,healthy=False,prior_timestamp=prior)
                except BaseException as exc:
                    self.audit['success']=False
                    self.audit['stop_error']=f'{type(exc).__name__}: {exc}'
            for label, resource in (('video',self.video),('observer',self.observer)):
                try: resource.close()
                except BaseException as exc:
                    self.audit['success']=False
                    self.audit.setdefault('close_errors',[]).append(f'{label}: {type(exc).__name__}: {exc}')
        return self.audit


def _prewarm(method):
    del method
    from contact_yield_live_writer import _prewarm_qp
    from contact_yield_protocol import QP_LIBRARY_PATH
    _prewarm_qp(QP_LIBRARY_PATH)


def _write_receipts(directory, row, proof, contract):
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
        script_sha256=contract.triplet['script'],session_epoch=1,resident_session_id=str(uuid.uuid4()),
        program_running=row['runtime_state']==2,uninterrupted=True,observed_at_s=row['observed_at_s'],
        provenance='continuous supervisor-rtde.jsonl from before Load/Play'))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--action',choices=['resident-check','qualify','pilot'],required=True)
    p.add_argument('--method',default='SFC'); p.add_argument('--duration',default='2')
    p.add_argument('--run-dir',type=Path,required=True); p.add_argument('--readback-dir',type=Path,required=True)
    p.add_argument('--controller-host',default='192.168.1.18'); p.add_argument('--kunwei-host',default='192.168.50.25')
    p.add_argument('--control-cpu',type=int,required=True)
    p.add_argument('--video-url',default='rtsp://127.0.0.1:8554/arm')
    a=p.parse_args(argv)
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
    observer=ProcessObserver(a.controller_host,a.run_dir/'supervisor-rtde.jsonl')
    video=VideoRecorder(a.video_url,a.run_dir)
    supervisor=ResidentSupervisor(observer=observer,video=video,
        read_dashboard=lambda:dashboard_exchange(a.controller_host,DASHBOARD_FIELDS),
        writer=RemoteDashboardWriter(a.controller_host,load_target=target),target=target)
    def body(s):
        if a.action=='resident-check':
            from contact_yield_transport import NativeYieldRTDETransport
            transport=NativeYieldRTDETransport(a.controller_host)
            frames=0
            try:
                transport.open()
                deadline=time.monotonic()+10.
                while time.monotonic()<deadline:
                    s.check(idle=True,identity=True)
                    if transport.poll_output(wait_s=.002) is not None: frames+=1
                    time.sleep(.002)
            finally: transport.close()
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
        if a.action=='pilot':cli+=['--duration',a.duration]
        return run_live(_parse_args(cli),observer_guard=lambda:s.check(identity=True))
    with WriterLock(INSTALLED_LOCK): result=supervisor.run(body,before_load=before_load)
    if a.action in ('qualify','pilot') and not result.get('success'):
        try:
            from run_contact_recovery import recover_failed_contact_run
            result['autonomous_home_recovery']=recover_failed_contact_run(a.run_dir,a.controller_host,a.video_url)
        except BaseException as exc:
            # Recovery setup must never disappear as an unclassified
            # exception.  A missing proof or unavailable owner is an explicit
            # BLOCKED outcome under the same policy; the attempt remains
            # failed and cannot be mistaken for a successful Home return.
            result['autonomous_home_recovery']={
                'success':False,
                'motion':False,
                'state':'BLOCKED',
                'phase':'recovery-dispatch',
                'error':f'{type(exc).__name__}: {exc}',
                'source_attempt':str(a.run_dir),
                'trial_stays_failed':True,
                'recovery_policy':'AUTO_HOME_UNLESS_SAFETY_PROOF_BLOCKS',
            }
    with (a.run_dir/'supervisor-result.json').open('x') as out: json.dump(result,out,indent=2)
    print(json.dumps(result,indent=2))
    return 0 if result['success'] else 1


if __name__=='__main__': raise SystemExit(main())
