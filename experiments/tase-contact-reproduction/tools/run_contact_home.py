#!/usr/bin/env python3
"""Bounded Remote Home action through the existing Step5d Dashboard owner.

Default is read-only. --execute requires the UR live owner to have accepted
attendance/clearance and the freshly read-back package. No force task starts.
"""
from pathlib import Path
import argparse,datetime,json,threading,time,subprocess,os
import numpy as np
import pinocchio as pin
from step5d_remote_startup import RemoteDashboardWriter,_ExactLoadAdapter,dashboard_exchange,RTDEClient
from step5d_autotune_v4_r014.dispatcher import WriterLock
from step5c_calibrated_kinematics_audit import rotvec_to_matrix
from build_contact_home import BASENAME,recovery_geometry,withdrawal_geometry
from build_contact_benchmark_triplet import CONTROLLER_DIR
from contact_yield_supervisor import VideoRecorder
from contact_home_motion_profile import (
    HOME_ANGULAR_SPEED_GUARD_RAD_S,
    HOME_JOINT_SPEED_GUARD_RAD_S,
    HOME_TCP_SPEED_GUARD_M_S,
    HOME_TRANSFER_SPEED_M_S,
    HOME_VERTICAL_SPEED_M_S,
)

FIELDS=('timestamp','actual_TCP_pose','actual_TCP_speed','actual_q','actual_qd','tcp_offset','payload','payload_cog','safety_status_bits')
TARGET=f'{CONTROLLER_DIR}/{BASENAME}.urp'
INSTALLED_LOCK=Path('/home/andy/.codex-worktrees/step5d-r014-fixed-confidence-20260821/experiments/tase-contact-reproduction/runs/r014_autotuner/live-writer.lock')
# UR RTDE ``safety_status_bits`` reports NORMAL as 1, optionally combined with
# bit 11 (3PE input active) as 2049.  Protective Stop is bit 3 (4); the same
# explicitly known 3PE combination is 2052.  Recovery may admit only these
# two Protective Stop encodings for its pre-unlock observation.  Unknown,
# malformed, or mixed safety states remain rejected.
NORMAL_SAFETY_BITS = frozenset((1, 2049))
PROTECTIVE_STOP_SAFETY_BITS = frozenset((4, 2052))



def home_motion_timeout_s(home):
    """Budget the existing three-segment motion from distance and package speed."""
    start=np.asarray(home['rtde']['actual_TCP_pose'],dtype=float)
    target=np.asarray(home['home_pose'],dtype=float)
    if start.shape!=(6,) or target.shape!=(6,) or not np.isfinite(start).all() or not np.isfinite(target).all():
        raise ValueError('invalid Home geometry')
    if np.linalg.norm(start[:3]-target[:3])>.08:
        raise ValueError('Home transfer exceeds 80mm bound')
    clearance=max(start[2],target[2])
    vertical=(clearance-start[2])+(clearance-target[2])
    horizontal=float(np.linalg.norm(target[:2]-start[:2]))
    length_s=vertical/HOME_VERTICAL_SPEED_M_S+horizontal/HOME_TRANSFER_SPEED_M_S
    return max(20.,float(length_s)+10.)


def validate_robot_sample(sample, *, allow_protective=False):
    for key,n in [('actual_TCP_pose',6),('actual_TCP_speed',6),('actual_q',6),('actual_qd',6),('tcp_offset',6),('payload_cog',3)]:
        value=np.asarray(sample[key]);
        if value.shape!=(n,) or not np.isfinite(value).all():raise ValueError(f'invalid {key}')
    # UR RTDE bit 11 is informational 3PE input active.  NORMAL is admitted
    # only as 1/2049; recovery separately admits the explicit Protective Stop
    # encodings above and nothing else.
    bits = sample['safety_status_bits']
    if isinstance(bits, (bool, np.bool_)) or not isinstance(bits, (int, np.integer)):
        raise ValueError('invalid RTDE safety status bits')
    bits = int(bits)
    if bits not in NORMAL_SAFETY_BITS:
        if not (allow_protective and bits in PROTECTIVE_STOP_SAFETY_BITS):
            raise ValueError('RTDE safety is not NORMAL')
    if not np.isclose(sample['payload'],.413,atol=1e-6) or not np.allclose(sample['payload_cog'],[.0011,.0031,.0163],atol=1e-6) or not np.allclose(sample['tcp_offset'],[0,0,.0874,0,0,0],atol=1e-9):raise ValueError('active tool binding changed')
    if np.linalg.norm(sample['actual_TCP_speed'][:3])>HOME_TCP_SPEED_GUARD_M_S or max(abs(x) for x in sample['actual_qd'])>HOME_JOINT_SPEED_GUARD_RAD_S:raise ValueError('Home speed envelope violated')


def admit_sample(sample,home, *, initial):
    validate_robot_sample(sample)
    current=np.asarray(sample['actual_TCP_pose']);start=np.asarray(home['rtde']['actual_TCP_pose']);target=np.asarray(home['home_pose'])
    low=np.minimum(start[:3],target[:3])-.003;high=np.maximum(start[:3],target[:3])+.003
    if np.any(current[:3]<low) or np.any(current[:3]>high):raise ValueError('Home transfer envelope violated')
    withdrawal=withdrawal_geometry(home)
    if withdrawal is None:
        if current[2]<target[2]-.001:raise ValueError('TCP below Home floor')
    else:
        if current[2]<start[2]-.0001 or sample['actual_TCP_speed'][2]<-.0005:
            raise ValueError('withdrawal moved farther into the surface')
        if current[2]<target[2]-.0002 and np.linalg.norm(current[:2]-start[:2])>.0005:
            raise ValueError('withdrawal left the observed vertical approach')
    angle=np.linalg.norm(pin.log3(rotvec_to_matrix(current[3:])@rotvec_to_matrix(target[3:]).T))
    recovery=recovery_geometry(home)
    if recovery is None:
        if angle>.01:raise ValueError('unexpected attitude change')
    else:
        from contact_yield_math import so3_exp,so3_log
        start,target,turn=recovery
        rs=so3_exp(start[3:]);ra=so3_exp(current[3:])
        travelled=so3_log(ra@rs.T)
        fraction=float(np.clip(np.dot(travelled,turn)/max(np.dot(turn,turn),1e-16),0,1))
        closest=so3_exp(fraction*turn)@rs
        if np.linalg.norm(so3_log(ra@closest.T))>.003:raise ValueError('outside planned Home attitude corridor')
        if current[2]<max(start[2],target[2])-.0002 and np.linalg.norm(travelled)>.003:
            raise ValueError('Home rotation began before vertical clearance')
        if np.linalg.norm(sample['actual_TCP_speed'][3:])>HOME_ANGULAR_SPEED_GUARD_RAD_S or max(abs(x) for x in sample['actual_qd'])>HOME_JOINT_SPEED_GUARD_RAD_S:
            raise ValueError('bounded recovery angular/joint speed exceeded')
        if initial and (np.linalg.norm(current[:3]-start[:3])>.0005 or np.linalg.norm(travelled)>.003):
            raise ValueError('bounded recovery initial observation changed')
    if initial:
        if np.linalg.norm(current[:3]-start[:3])>.002 or np.linalg.norm(sample['actual_TCP_speed'])>.0005:raise ValueError('initial Home observation changed/not stationary')


class Observer:
    def __init__(self,host):self.host=host;self.rows=[];self.error=None;self.done=threading.Event();self.ready=threading.Event()
    def start(self):
        self.thread=threading.Thread(target=self._run,daemon=True);self.thread.start()
        if not self.ready.wait(5):raise ValueError('RTDE observer did not start')
        if self.error:raise ValueError(self.error)
    def _run(self):
        try:
            with RTDEClient(self.host,timeout=1.) as c:
                c.negotiate(version=2);recipe,types=c.setup_outputs(100.,FIELDS);c.start()
                while not self.done.is_set():
                    values=c.recv_recipe_sample(recipe,types);r=dict(zip(FIELDS,values));r['monotonic_s']=time.monotonic()
                    if len(values)!=len(FIELDS):raise ValueError('RTDE shape mismatch')
                    if self.rows and (r['timestamp']<=self.rows[-1]['timestamp'] or r['monotonic_s']-self.rows[-1]['monotonic_s']>.08):raise ValueError('RTDE stale/gap')
                    self.rows.append(r);self.ready.set()
        except Exception as e:self.error=f'{type(e).__name__}: {e}';self.ready.set()
    def latest(self):
        if self.error or not self.rows or time.monotonic()-self.rows[-1]['monotonic_s']>.08:raise ValueError(f'observer unavailable/stale: {self.error}')
        return self.rows[-1]
    def close(self):self.done.set();self.thread.join(timeout=2)


def run(args):
    home=json.loads(args.home_receipt.read_text());validation=json.loads(args.validation.read_text())
    if validation.get('pass') is not True or validation.get('state')!='controller read-back verified' or validation.get('basename')!=BASENAME:raise ValueError('Home package read-back validation required')
    for suffix in ('script','urp','txt'):
        local=args.package_dir/f'{BASENAME}.{suffix}';remote=args.readback_dir/f'{BASENAME}.{suffix}'
        if local.read_bytes()!=remote.read_bytes():raise ValueError('read-back bytes changed')
    observed=dashboard_exchange(args.host,['is in remote control','safetymode','running','programState','robotmode'])
    if observed.get('is in remote control')!='true' or observed.get('safetymode')!='Safetymode: NORMAL' or observed.get('running')!='Program running: false' or observed.get('robotmode')!='Robotmode: RUNNING':raise ValueError(f'Home Remote/stopped gate failed: {observed}')
    video_policy = getattr(args, 'video_policy', 'required')
    video_url = getattr(args, 'video_url', 'rtsp://127.0.0.1:8554/arm')
    if video_policy not in VideoRecorder.POLICIES:
        raise ValueError(f'unknown video policy: {video_policy}')
    if not args.execute:return {'action':'read_only_preflight','dashboard':observed,'motion':False,'video_policy':video_policy}
    args.output.mkdir(parents=True,exist_ok=False)
    result={'requested_action':'bounded contact withdrawal to Home' if home.get('bounded_withdrawal') else 'noncontact Home only','started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'load':None,'play':None,'success':False,'video_policy':video_policy}
    sensor=None;wrench_rows=[]
    def check_wrench():
        raw,received=sensor.poll();now=time.monotonic()
        if raw is None or received is None or not 0<=now-received<.08:raise ValueError('withdrawal Kunwei frame missing/stale')
        if np.linalg.norm(raw[:3])>=20 or np.linalg.norm(raw[3:])>=2:raise ValueError('withdrawal raw wrench guard exceeded')
        wrench_rows.append({'wrench_n_nm':raw,'received_monotonic_s':received,'checked_monotonic_s':now})
    obs=Observer(args.host);video=None;writer=RemoteDashboardWriter(args.host,load_target=TARGET)
    adapter=_ExactLoadAdapter(host=args.host,target=TARGET,program_id=BASENAME,dashboard_observer=dashboard_exchange,writer=writer,dashboard_port=29999,dashboard_timeout_s=2.,observe_timeout_s=5.,poll_interval_s=.05,monotonic=time.monotonic,sleeper=time.sleep)
    with WriterLock(INSTALLED_LOCK):
        try:
            obs.start()
            video = (
                VideoRecorder(video_url, args.output)
                if video_policy == 'required'
                else VideoRecorder(video_url, args.output, policy=video_policy)
            )
            video.start()
            barrier_deadline=time.monotonic()+8.
            while time.monotonic()<barrier_deadline:
                video.check()
                obs.latest()
                camera_ready = (
                    True
                    if video_policy == 'evidence-only'
                    else video.path.exists() and video.path.stat().st_size > 512
                )
                if len(obs.rows)>=10 and camera_ready:break
                time.sleep(.05)
            else:raise ValueError('RTDE/camera observer barrier did not complete')
            for row in obs.rows[-10:]:admit_sample(row,home,initial=True)
            if home.get('bounded_withdrawal'):
                from step5d_autotune_v4_r004.transport import LiveR004KunweiTransport
                sensor=LiveR004KunweiTransport('192.168.50.25',port=5152);sensor.open()
                until=time.monotonic()+1.
                while time.monotonic()<until:
                    raw,_=sensor.poll()
                    if raw is not None:break
                    obs.latest();time.sleep(.002)
                check_wrench()
            result['load']=adapter.load()
            admit_sample(obs.latest(),home,initial=True)
            result['play']=adapter.play()
            motion_timeout=home_motion_timeout_s(home)
            result['motion_timeout_s']=motion_timeout
            deadline=time.monotonic()+motion_timeout;stationary_since=None
            while time.monotonic()<deadline:
                row=obs.latest();admit_sample(row,home,initial=False)
                if sensor is not None:check_wrench()
                state=dashboard_exchange(args.host,['safetymode','running','programState','is in remote control','get loaded program'])
                if state['get loaded program']!=f'Loaded program: {TARGET}':raise ValueError('loaded Home program changed')
                video.check()
                if state['safetymode']!='Safetymode: NORMAL' or state['is in remote control']!='true':raise ValueError('Home live safety/mode changed')
                target=np.asarray(home['home_pose']);actual=np.asarray(row['actual_TCP_pose']);pe=np.linalg.norm(actual[:3]-target[:3]);ae=np.linalg.norm(pin.log3(rotvec_to_matrix(actual[3:])@rotvec_to_matrix(target[3:]).T))
                stopped=state['running']=='Program running: false' and state['programState'].startswith('STOPPED')
                if stopped and pe<.001 and ae<.005 and np.linalg.norm(row['actual_TCP_speed'])<.0005 and max(abs(x) for x in row['actual_qd'])<.001:
                    if stationary_since is None:stationary_since=time.monotonic()
                    if time.monotonic()-stationary_since>=.5:
                        result.update(success=True,final_position_error_m=float(pe),final_orientation_error_rad=float(ae),dashboard_after=state);break
                else:stationary_since=None
                time.sleep(.04)
            else:raise ValueError(f'Home did not finish within geometry-derived {motion_timeout:.3f}s')
        except BaseException as e:
            result['failure']=f'{type(e).__name__}: {e}'
            if adapter.play_issued:
                try:result['compensating_stop']=adapter.stop()
                except Exception as stop:result['stop_failure']=str(stop)
        finally:
            if adapter.play_issued:
                try:
                    before=time.monotonic();writer.write('stop')
                    until=before+3.
                    while time.monotonic()<until:
                        stopped=dashboard_exchange(args.host,['running','programState','safetymode'])
                        row=obs.latest()
                        if (stopped['running']=='Program running: false' and stopped['programState'].startswith('STOPPED')
                            and row['monotonic_s']>=before and np.linalg.norm(row['actual_TCP_speed'])<.0005
                            and max(map(abs,row['actual_qd']))<.001):
                            result['observed_stop']={'dashboard':stopped,'sample':row};break
                        time.sleep(.02)
                    else:raise ValueError('Home physical stop was not observed')
                except BaseException as exc:
                    result['success']=False;result['stop_confirmation_error']=f'{type(exc).__name__}: {exc}'
            if hasattr(obs,'thread'):obs.close()
            if sensor is not None:
                sensor.close()
                (args.output/'raw-wrench.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in wrench_rows))
            if video is not None:
                video.close()
                evidence = getattr(video, 'evidence', None)
                result['video_evidence'] = evidence() if callable(evidence) else None
            (args.output/'rtde.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in obs.rows))
            result['ended_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();(args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--host',default='192.168.1.18');p.add_argument('--home-receipt',type=Path,required=True);p.add_argument('--validation',type=Path,required=True);p.add_argument('--package-dir',type=Path,required=True);p.add_argument('--readback-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--video-url',default='rtsp://127.0.0.1:8554/arm');p.add_argument('--video-policy',choices=sorted(VideoRecorder.POLICIES),default='required');p.add_argument('--execute',action='store_true');a=p.parse_args()
    try:r=run(a)
    except Exception as exc:r={'success':False,'failure':f'{type(exc).__name__}: {exc}'}
    print(json.dumps(r,indent=2));raise SystemExit(0 if r.get('success') or r.get('motion') is False else 1)
