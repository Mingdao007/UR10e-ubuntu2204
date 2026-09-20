"""Sole-owner stopped -> monitored vertical relief -> clearance Home recovery.

Normal task admission is never relaxed. Recovery is a distinct, directional
operation; a failed trial remains failed. Protective/emergency stops, lost
observations, force increase, or failed clearance keep the robot stopped.
"""
import argparse,copy,datetime,json,time
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pinocchio as pin
from build_contact_recovery import RELIEF_PROGRAM
from contact_yield_live_contract import load_identity_contract,PACKAGE_DIR
from contact_yield_supervisor import VideoRecorder
from contact_yield_math import so3_exp,so3_log
from contact_home_recovery_policy import plan_home_recovery,ReliefForceGuard,validate_lift_sample
from run_contact_home import Observer,validate_robot_sample,run as run_home,INSTALLED_LOCK,BASENAME
from step5d_remote_startup import RemoteDashboardWriter,_ExactLoadAdapter,dashboard_exchange
from step5d_autotune_v4_r014.dispatcher import WriterLock
from step5d_autotune_v4_r004.transport import LiveR004KunweiTransport
from step5c_calibrated_kinematics_audit import build_calibrated_model,base_to_tool0
from step5d_autotune_v4_r004.calibrated_runtime import tcp_jacobian_base

DIRECTORY='/programs/andyl/kunwei/step5'


def check_dashboard(host):
    row=dashboard_exchange(host,['safetymode','running','robotmode','is in remote control'])
    if row!={'safetymode':'Safetymode: NORMAL','running':'Program running: false','robotmode':'Robotmode: RUNNING','is in remote control':'true'}:
        raise ValueError(f'recovery requires NORMAL, stopped, powered Remote robot: {row}')
    return row


def package_dir_from(args):
    path=getattr(args,'package_dir',None)
    return Path(path) if path is not None else PACKAGE_DIR


def validate_recovery_packages(run_dir,readback_dir,package_dir):
    proof=json.loads((run_dir/'readback-results.json').read_text())
    if proof.get('pass') is not True or not 0<=time.time()-proof['observed_at_s']<300:
        raise ValueError('fresh recovery read-back required')
    for name in (RELIEF_PROGRAM,BASENAME):
        valid=json.loads((run_dir/f'{name}-validation.json').read_text())
        if valid.get('pass') is not True or valid.get('state')!='controller read-back verified' or valid.get('basename')!=name:
            raise ValueError(f'{name} validation failed')
        for ext in ('script','txt','urp'):
            if (package_dir/f'{name}.{ext}').read_bytes()!=(readback_dir/name/f'{name}.{ext}').read_bytes():
                raise ValueError('recovery read-back bytes differ')
    if 'clearance entry rejected' not in (package_dir/f'{BASENAME}.script').read_text():
        raise ValueError('Home package is not clearance-entry version')


def check_geometry(sample,plan):
    """Reuse calibrated IK sampling from the already executed Home route."""
    model=build_calibrated_model();offset=pin.SE3(np.eye(3),np.array([0,0,.0874]))
    q=np.array(sample['actual_q']);initial=q.copy();start=np.asarray(plan['start_pose']);lift=np.asarray(plan['lift_pose']);home=np.asarray(plan['home_pose'])
    def err(q,pos,rot):
        actual=base_to_tool0(model,q)*offset
        return np.r_[pos-actual.translation,so3_log(rot@actual.rotation.T)]
    residual=err(q,start[:3],so3_exp(start[3:]))
    if np.linalg.norm(residual[:3])>.0005 or np.linalg.norm(residual[3:])>.003:
        raise ValueError('measured TCP and calibrated geometry disagree')
    max_residual=0.;max_qd=0.
    for a,b,speed in ((start,lift,.0005),(lift,home,.002)):
        ra=so3_exp(a[3:]);turn=so3_log(so3_exp(b[3:])@ra.T)
        duration=max(np.linalg.norm(b[:3]-a[:3])/speed,np.linalg.norm(turn)/.020,.1)
        for fraction in np.linspace(0.,1.,51):
            pos=a[:3]+fraction*(b[:3]-a[:3]);rot=so3_exp(fraction*turn)@ra
            for _ in range(15):
                e=err(q,pos,rot)
                if np.linalg.norm(e)<1e-9:break
                q+=np.linalg.solve(tcp_jacobian_base(model,q),e)
            max_residual=max(max_residual,float(np.linalg.norm(e)))
            qd=np.linalg.solve(tcp_jacobian_base(model,q),np.r_[(b[:3]-a[:3])/duration,turn/duration])
            max_qd=max(max_qd,float(np.max(np.abs(qd))))
            if not (np.all(q>model.model.lowerPositionLimit) and np.all(q<model.model.upperPositionLimit)):
                raise ValueError('recovery IK joint bounds exceeded')
    if max_residual>1e-7 or max_qd>.04:raise ValueError('recovery IK/speed check failed')
    return {'pass':True,'max_residual':max_residual,'max_joint_speed_rad_s':max_qd,'home_q':q.tolist(),'joint_delta':(q-initial).tolist(),'scope':'calibrated sampled geometry, not collision or force proof'}


def stationary(row):
    return np.linalg.norm(row['actual_TCP_speed'])<.0005 and max(map(abs,row['actual_qd']))<.001


def run(args):
    source=Path(args.source_run);out=Path(args.output)
    if out.exists():raise ValueError('recovery output already exists')
    packages=package_dir_from(args)
    if args.readback_proof_dir is None:
        from contact_recovery_readback import fetch_recovery_readback
        proof=fetch_recovery_readback(out.with_name(out.name+'-readback'),packages)
        args.readback_proof_dir=proof;args.readback_dir=proof/'readback'
    validate_recovery_packages(Path(args.readback_proof_dir),Path(args.readback_dir),packages)
    baseline=json.loads((source/'software_baseline_receipt.json').read_text())
    source_receipt=json.loads((source/'dispatch_receipt.json').read_text())
    if source_receipt.get('armed') is not True or source_receipt.get('stop',{}).get('protective_stop') is not False:
        raise ValueError('recovery requires a known non-protective stopped attempt')
    contract=load_identity_contract()
    if baseline.get('eoat_identity_sha256')!=contract.eoat_sha256:raise ValueError('baseline tool identity differs')
    check_dashboard(args.host)
    if not args.execute:return {'success':False,'motion':False,'state':'read-only preflight passed'}
    out.mkdir(parents=True)
    result={'success':False,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_attempt':str(source),'trial_stays_failed':True}
    obs=Observer(args.host);sensor=LiveR004KunweiTransport('192.168.50.25',port=5152);video=None;adapter=None;wrench_rows=[];last_sensor=None
    lease=WriterLock(INSTALLED_LOCK);lease_held=False
    try:
        lease.__enter__();lease_held=True
        obs.start();sensor.open();video=VideoRecorder(args.video_url,out);video.start()
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            video.check();row=obs.latest();validate_robot_sample(row);raw,at=sensor.poll()
            if len(obs.rows)>=10 and raw is not None and video.path.exists() and video.path.stat().st_size>512:break
            time.sleep(.01)
        else:raise ValueError('recovery observer/video barrier failed')
        if not stationary(row):raise ValueError('robot is not stationary before recovery')
        plan=plan_home_recovery(row['actual_TCP_pose'],contract.home_pose);result['plan']=plan
        result['geometry']=check_geometry(row,plan)
        guard=ReliefForceGuard(initial_raw_wrench=raw,no_load_wrench=baseline['mean_wrench_n_nm'],baseline_std_wrench=baseline['std_wrench_n_nm'],rotation=so3_exp(row['actual_TCP_pose'][3:]))
        def check():
            nonlocal last_sensor
            current=obs.latest();validate_robot_sample(current);video.check()
            raw,received=sensor.poll();now=time.monotonic()
            if raw is None or received is None or not 0<=now-received<.08:raise ValueError('recovery Kunwei observation stale')
            if last_sensor is None or received>last_sensor:
                force=guard.update(raw,now_s=received);last_sensor=received
                wrench_rows.append({'received_monotonic_s':received,'wrench_n_nm':raw,'force_check':force})
            else:force=wrench_rows[-1]['force_check'] if wrench_rows else None
            return current,force
        before,_=check()
        if np.linalg.norm(np.asarray(before['actual_TCP_pose'])[:3]-np.asarray(plan['start_pose'])[:3])>.0005 or not stationary(before):
            raise ValueError('start changed during geometry check')
        if plan['needs_lift']:
            target=f'{DIRECTORY}/{RELIEF_PROGRAM}.urp';writer=RemoteDashboardWriter(args.host,load_target=target)
            adapter=_ExactLoadAdapter(host=args.host,target=target,program_id=RELIEF_PROGRAM,dashboard_observer=dashboard_exchange,writer=writer,dashboard_port=29999,dashboard_timeout_s=2.,observe_timeout_s=5.,poll_interval_s=.05,monotonic=time.monotonic,sleeper=time.sleep)
            result['relief_load']=adapter.load();check();result['relief_play']=adapter.play()
            deadline=time.monotonic()+60.;stopped_since=None
            while time.monotonic()<deadline:
                row,force=check();validate_lift_sample(plan,row['actual_TCP_pose'],row['actual_TCP_speed'])
                if row['actual_TCP_pose'][2]>=plan['lift_pose'][2]-.0001 and stationary(row):
                    if stopped_since is None:stopped_since=time.monotonic()
                    if time.monotonic()-stopped_since>=.3:
                        check_dashboard(args.host)
                        if not force or not force['released']:raise ValueError('lift complete but force release not confirmed; no XY return')
                        result['relief_complete']=True;break
                else:stopped_since=None
                time.sleep(.01)
            else:raise ValueError('vertical relief timed out')
        else:
            until=time.monotonic()+.5
            while time.monotonic()<until:row,force=check();time.sleep(.01)
            if not force['released']:raise ValueError('clearance pose is still loaded; Home withheld')
        row,force=check();check_dashboard(args.host)
        home=json.loads((Path(__file__).resolve().parents[1]/'report/contact-six-qp-20260917/preserved-home.json').read_text())
        home.pop('bounded_recovery',None);home.pop('bounded_withdrawal',None)
        home.update(rtde=row,home_pose=list(contract.home_pose),home_q=result['geometry']['home_q'],clearance_entry=True)
        home_receipt=out/'clearance-home.json';home_receipt.write_text(json.dumps(home,indent=2)+'\n')
        # The lift is stopped and its lease is released before the existing
        # independent Home owner starts. Never overlap command writers.
        obs.close();sensor.close();video.close();video=None
        lease.__exit__(None,None,None);lease_held=False
        home_args=SimpleNamespace(host=args.host,home_receipt=home_receipt,validation=Path(args.readback_proof_dir)/f'{BASENAME}-validation.json',package_dir=packages,readback_dir=Path(args.readback_dir)/BASENAME,output=out/'home',execute=True)
        result['home']=run_home(home_args);result['success']=result['home']['success']
    except BaseException as exc:
        result['error']=f'{type(exc).__name__}: {exc}'
        if adapter is not None and adapter.play_issued:
            try:
                stopped_after=time.monotonic();result['stop']=adapter.stop()
                until=stopped_after+3.
                while time.monotonic()<until:
                    stopped=obs.latest()
                    if stopped['monotonic_s']>=stopped_after and stationary(stopped):
                        result['confirmed_stop']=stopped;break
                    time.sleep(.02)
                else:raise ValueError('recovery physical stop not confirmed')
            except BaseException as stop:result['stop_error']=str(stop)
        try:
            row=obs.latest();result['stopped_sample']=row
            if not stationary(row):result['stop_unconfirmed']=True
        except BaseException as stop:result['stop_observation_error']=str(stop)
    finally:
        if hasattr(obs,'thread'):obs.close()
        sensor.close()
        if video is not None:video.close()
        def encode(obj):
            if isinstance(obj,np.ndarray):return obj.tolist()
            if isinstance(obj,(np.floating,np.integer)):return obj.item()
            raise TypeError(type(obj))
        (out/'relief-rtde.jsonl').write_text(''.join(json.dumps(r,default=encode)+'\n' for r in obs.rows))
        (out/'relief-wrench.jsonl').write_text(''.join(json.dumps(r,default=encode)+'\n' for r in wrench_rows))
        if lease_held:lease.__exit__(None,None,None)
        result['ended_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        (out/'result.json').write_text(json.dumps(result,indent=2,default=encode)+'\n')
    return result


def recover_failed_contact_run(source_run, host, video_url):
    """Call after a confirmed non-protective contact stop. Packages must already be installed."""
    if not (PACKAGE_DIR/f'{RELIEF_PROGRAM}.script').exists():
        return {'success':False,'error':'relief package is not installed; autonomous Home withheld'}
    source=Path(source_run)
    return run(SimpleNamespace(source_run=source,output=source.with_name(source.name+'-autonomous-home'),readback_proof_dir=None,readback_dir=None,package_dir=PACKAGE_DIR,host=host,video_url=video_url,execute=True))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-run',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--readback-proof-dir',type=Path);p.add_argument('--readback-dir',type=Path);p.add_argument('--package-dir',type=Path);p.add_argument('--host',default='192.168.1.18');p.add_argument('--video-url',default='rtsp://127.0.0.1:8554/arm');p.add_argument('--execute',action='store_true');args=p.parse_args(argv)
    try:result=run(args)
    except Exception as exc:result={'success':False,'error':f'{type(exc).__name__}: {exc}'}
    print(json.dumps(result,indent=2));return 0 if result.get('success') or result.get('motion') is False else 1

if __name__=='__main__':raise SystemExit(main())
