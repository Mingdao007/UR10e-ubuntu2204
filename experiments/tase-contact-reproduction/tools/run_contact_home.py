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
from build_contact_home import BASENAME
from build_contact_benchmark_triplet import CONTROLLER_DIR

FIELDS=('timestamp','actual_TCP_pose','actual_TCP_speed','actual_q','actual_qd','tcp_offset','payload','payload_cog','safety_status_bits')
TARGET=f'{CONTROLLER_DIR}/{BASENAME}.urp'
INSTALLED_LOCK=Path('/home/andy/.codex-worktrees/step5d-r014-fixed-confidence-20260821/experiments/tase-contact-reproduction/runs/r014_autotuner/live-writer.lock')


def admit_sample(sample,home, *, initial):
    for key,n in [('actual_TCP_pose',6),('actual_TCP_speed',6),('actual_q',6),('actual_qd',6),('tcp_offset',6),('payload_cog',3)]:
        value=np.asarray(sample[key]);
        if value.shape!=(n,) or not np.isfinite(value).all():raise ValueError(f'invalid {key}')
    # UR RTDE bit 11 is informational 3PE input active; bits 1-10 remain forbidden.
    # https://docs.universal-robots.com/tutorials/communication-protocol-tutorials/rtde-guide.html
    if sample['safety_status_bits'] not in (1, 2049):raise ValueError('RTDE safety is not NORMAL')
    if not np.isclose(sample['payload'],.413,atol=1e-6) or not np.allclose(sample['payload_cog'],[.0011,.0031,.0163],atol=1e-6) or not np.allclose(sample['tcp_offset'],[0,0,.0874,0,0,0],atol=1e-9):raise ValueError('active tool binding changed')
    current=np.asarray(sample['actual_TCP_pose']);start=np.asarray(home['rtde']['actual_TCP_pose']);target=np.asarray(home['home_pose'])
    low=np.minimum(start[:3],target[:3])-.003;high=np.maximum(start[:3],target[:3])+.003
    if np.any(current[:3]<low) or np.any(current[:3]>high):raise ValueError('Home transfer envelope violated')
    if current[2]<target[2]-.001:raise ValueError('TCP below Home floor')
    angle=np.linalg.norm(pin.log3(rotvec_to_matrix(current[3:])@rotvec_to_matrix(target[3:]).T))
    if angle>.01:raise ValueError('unexpected attitude change')
    if np.linalg.norm(sample['actual_TCP_speed'][:3])>.02 or max(abs(x) for x in sample['actual_qd'])>.06:raise ValueError('Home speed envelope violated')
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
                c.negotiate(version=2);recipe,types=c.setup_outputs(25.,FIELDS);c.start()
                while not self.done.is_set():
                    values=c.recv_recipe_sample(recipe,types);r=dict(zip(FIELDS,values));r['monotonic_s']=time.monotonic()
                    if len(values)!=len(FIELDS):raise ValueError('RTDE shape mismatch')
                    if self.rows and (r['timestamp']<=self.rows[-1]['timestamp'] or r['monotonic_s']-self.rows[-1]['monotonic_s']>.2):raise ValueError('RTDE stale/gap')
                    self.rows.append(r);self.ready.set()
        except Exception as e:self.error=f'{type(e).__name__}: {e}';self.ready.set()
    def latest(self):
        if self.error or not self.rows or time.monotonic()-self.rows[-1]['monotonic_s']>.2:raise ValueError(f'observer unavailable/stale: {self.error}')
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
    if not args.execute:return {'action':'read_only_preflight','dashboard':observed,'motion':False}
    args.output.mkdir(parents=True,exist_ok=False)
    result={'requested_action':'noncontact Home only','started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'load':None,'play':None,'success':False}
    obs=Observer(args.host);video=None;writer=RemoteDashboardWriter(args.host,load_target=TARGET)
    adapter=_ExactLoadAdapter(host=args.host,target=TARGET,program_id=BASENAME,dashboard_observer=dashboard_exchange,writer=writer,dashboard_port=29999,dashboard_timeout_s=2.,observe_timeout_s=5.,poll_interval_s=.05,monotonic=time.monotonic,sleeper=time.sleep)
    with WriterLock(INSTALLED_LOCK):
        try:
            video=subprocess.Popen(['ffmpeg','-hide_banner','-loglevel','error','-rtsp_transport','tcp','-stimeout','8000000','-i','rtsp://127.0.0.1:8554/arm','-c','copy','-flush_packets','1','-cluster_time_limit','500','-t','40',str(args.output/'home.mkv')],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            obs.start()
            barrier_deadline=time.monotonic()+8.
            while time.monotonic()<barrier_deadline:
                if video.poll() is not None:raise ValueError('camera capture failed before motion')
                obs.latest()
                camera_file=args.output/'home.mkv'
                if len(obs.rows)>=10 and camera_file.exists() and camera_file.stat().st_size>512:break
                time.sleep(.05)
            else:raise ValueError('RTDE/camera observer barrier did not complete')
            for row in obs.rows[-10:]:admit_sample(row,home,initial=True)
            result['load']=adapter.load()
            admit_sample(obs.latest(),home,initial=True)
            result['play']=adapter.play()
            deadline=time.monotonic()+20;stationary_since=None
            while time.monotonic()<deadline:
                row=obs.latest();admit_sample(row,home,initial=False)
                state=dashboard_exchange(args.host,['safetymode','running','programState','is in remote control','get loaded program'])
                if state['get loaded program']!=f'Loaded program: {TARGET}':raise ValueError('loaded Home program changed')
                if video.poll() is not None:raise ValueError('camera recording ended during Home')
                if state['safetymode']!='Safetymode: NORMAL' or state['is in remote control']!='true':raise ValueError('Home live safety/mode changed')
                target=np.asarray(home['home_pose']);actual=np.asarray(row['actual_TCP_pose']);pe=np.linalg.norm(actual[:3]-target[:3]);ae=np.linalg.norm(pin.log3(rotvec_to_matrix(actual[3:])@rotvec_to_matrix(target[3:]).T))
                stopped=state['running']=='Program running: false' and state['programState'].startswith('STOPPED')
                if stopped and pe<.001 and ae<.005 and np.linalg.norm(row['actual_TCP_speed'])<.0005 and max(abs(x) for x in row['actual_qd'])<.001:
                    if stationary_since is None:stationary_since=time.monotonic()
                    if time.monotonic()-stationary_since>=.5:
                        result.update(success=True,final_position_error_m=float(pe),final_orientation_error_rad=float(ae),dashboard_after=state);break
                else:stationary_since=None
                time.sleep(.04)
            else:raise ValueError('Home did not finish within 20s')
        except BaseException as e:
            result['failure']=f'{type(e).__name__}: {e}'
            if adapter.play_issued:
                try:result['compensating_stop']=adapter.stop()
                except Exception as stop:result['stop_failure']=str(stop)
        finally:
            if hasattr(obs,'thread'):obs.close()
            if video is not None:
                video.terminate()
                try:video.communicate(timeout=3)
                except subprocess.TimeoutExpired:video.kill();video.communicate()
            (args.output/'rtde.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in obs.rows))
            result['ended_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();(args.output/'result.json').write_text(json.dumps(result,indent=2)+'\n')
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--host',default='192.168.1.18');p.add_argument('--home-receipt',type=Path,required=True);p.add_argument('--validation',type=Path,required=True);p.add_argument('--package-dir',type=Path,required=True);p.add_argument('--readback-dir',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--execute',action='store_true');a=p.parse_args()
    try:r=run(a)
    except Exception as exc:r={'success':False,'failure':f'{type(exc).__name__}: {exc}'}
    print(json.dumps(r,indent=2));raise SystemExit(0 if r.get('success') or r.get('motion') is False else 1)
