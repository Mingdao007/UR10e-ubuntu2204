"""Sole-owner stopped -> monitored vertical relief -> clearance Home recovery.

Normal task admission is never relaxed. Recovery is a distinct, directional
operation; a failed trial remains failed. Every commandable fault enters this
same Home path, including faults raised while the relief program is running. A
protective stop is handled by one quiescence check and one allow-listed
Dashboard unlock, followed by a fresh Safety NORMAL check. The only terminal
alternative is ``BLOCKED`` when a Home command cannot safely be established or
the Home owner itself fails; there is no revoke-only terminal disposition.
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
RECOVERY_POLICY='AUTO_HOME_WHEN_COMMANDABLE'


def _protective_safety(row):
    return 'PROTECTIVE_STOP' in str(row.get('safetymode','')).upper()


def check_dashboard(host, *, allow_protective=False):
    """Read the recovery gate without hiding a Protective Stop.

    ``allow_protective`` is only for the pre-unlock observation. It does not
    authorize motion; callers must prove stationary/fresh RTDE and then
    re-read NORMAL after the single unlock command.
    """
    row=dashboard_exchange(host,['safetymode','running','robotmode','is in remote control'])
    common = {
        'robotmode':'Robotmode: RUNNING',
        'is in remote control':'true',
    }
    if any(row.get(key) != value for key,value in common.items()):
        raise ValueError(f'recovery requires a stopped, powered Remote robot: {row}')
    if row.get('safetymode') == 'Safetymode: NORMAL':
        if row.get('running') != 'Program running: false':
            raise ValueError(f'recovery requires NORMAL but stopped Dashboard: {row}')
        return row
    if allow_protective and _protective_safety(row):
        return row
    raise ValueError(f'recovery safety gate is not NORMAL: {row}')


def _dashboard_until_stopped(host, *, timeout_s=5.0):
    deadline=time.monotonic()+float(timeout_s)
    last=None
    while time.monotonic() < deadline:
        last=dashboard_exchange(host,['safetymode','running','robotmode','is in remote control'])
        if last.get('running') == 'Program running: false':
            return last
        time.sleep(.05)
    raise ValueError(f'recovery could not confirm Dashboard STOPPED: {last}')


def _unlock_protective_stop_once(host, *, target):
    """Unlock exactly once, then require Dashboard Safety NORMAL."""
    writer=RemoteDashboardWriter(host,load_target=target)
    outcome=writer.write('unlock protective stop')
    deadline=time.monotonic()+5.
    last=None
    while time.monotonic() < deadline:
        last=dashboard_exchange(host,['safetymode','running','robotmode','is in remote control'])
        if (last.get('safetymode') == 'Safetymode: NORMAL'
            and last.get('running') == 'Program running: false'
            and last.get('robotmode') == 'Robotmode: RUNNING'
            and last.get('is in remote control') == 'true'):
            return {'command':outcome.command,'response':outcome.response,'dashboard':last}
        time.sleep(.05)
    raise ValueError(f'protective stop did not return to NORMAL after one unlock: {last}')


def package_dir_from(args):
    path=getattr(args,'package_dir',None)
    return Path(path) if path is not None else PACKAGE_DIR


def _blocked_recovery_result(source, error, *, phase, output=None):
    """Return and, when possible, persist the only non-Home terminal result.

    Recovery setup can fail before :func:`run` has created its normal result
    directory (for example, a stale read-back proof or a missing package).
    Those failures must not escape as an unclassified exception that looks like
    the old revoke-only state. ``BLOCKED`` means that a Home attempt could not
    safely be dispatched or completed; it is an explicit safety result, not
    permission to leave the attempt silently stopped.
    """
    payload={
        'success':False,
        'motion':False,
        'state':'BLOCKED',
        'phase':str(phase),
        'error':f'{type(error).__name__}: {error}',
        'source_attempt':str(source),
        'trial_stays_failed':True,
        'recovery_policy':RECOVERY_POLICY,
        'home_required':True,
        'home_attempted':False,
        'home_blocked':True,
        'home_blocked_reason':f'{type(error).__name__}: {error}',
    }
    if output is not None:
        target=Path(output)
        try:
            if target.exists() and target.is_dir():
                record=target/'result.json'
                if record.exists():
                    record=target/'blocked-preflight.json'
            else:
                target.mkdir(parents=True,exist_ok=True)
                record=target/'result.json'
            record.write_text(json.dumps(payload,indent=2)+'\n')
            payload['recovery_record']=str(record)
        except BaseException as persist_error:
            payload['recovery_record_error']=f'{type(persist_error).__name__}: {persist_error}'
    return payload


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
    receipt_path=source/'dispatch_receipt.json'
    if receipt_path.exists():
        source_receipt=json.loads(receipt_path.read_text())
    else:
        # A qualification can fail before ARM or before the writer emits its
        # dispatch receipt. That is still a recovery event: the Dashboard and
        # fresh RTDE observation, not a missing host receipt, decide whether
        # the existing Home route can be run.
        source_receipt={'receipt_present':False,'armed':False,'stop':{}}
    source_protective=source_receipt.get('stop',{}).get('protective_stop') is True
    contract=load_identity_contract()
    if baseline.get('eoat_identity_sha256')!=contract.eoat_sha256:raise ValueError('baseline tool identity differs')
    dashboard_before=check_dashboard(args.host,allow_protective=True)
    if not args.execute:return {'success':False,'motion':False,'state':'read-only preflight passed','dashboard':dashboard_before,'source_protective_stop':source_protective}
    out.mkdir(parents=True)
    result={'success':False,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_attempt':str(source),'trial_stays_failed':True,'recovery_policy':RECOVERY_POLICY,'source_receipt_present':bool(source_receipt.get('receipt_present',True)),'source_armed':source_receipt.get('armed'),'source_protective_stop':source_protective,'dashboard_before':dashboard_before,'home_required':True,'home_attempted':False,'home_blocked':False}
    obs=Observer(args.host);sensor=LiveR004KunweiTransport('192.168.50.25',port=5152);video=None;adapter=None;wrench_rows=[];last_sensor=None;last_force=None;plan=None;geometry=None;home_invoked=False
    lease=WriterLock(INSTALLED_LOCK);lease_held=False
    protective_unlock_attempted=False

    def invoke_home(current, force):
        """Run the sole verified Home owner once a clearance proof exists.

        This helper is intentionally shared by the success path and the
        exception path. A relief fault may stop the helper program, but it
        must not silently end the recovery if the observed pose and force
        release already make the clearance Home commandable.
        """
        nonlocal lease_held, video, home_invoked
        if home_invoked:
            return result.get('home')
        if plan is None or geometry is None:
            raise ValueError('Home is not commandable: relief geometry proof is missing')
        if not stationary(current):
            raise ValueError('Home is not commandable: robot is not stationary')
        if float(current['actual_TCP_pose'][2]) < float(plan['lift_pose'][2]) - .0001:
            raise ValueError('Home is not commandable: vertical clearance is incomplete')
        if not force or force.get('released') is not True:
            raise ValueError('Home is not commandable: contact force is not released')
        check_dashboard(args.host)
        home=json.loads((Path(__file__).resolve().parents[1]/'report/contact-six-qp-20260917/preserved-home.json').read_text())
        home.pop('bounded_recovery',None);home.pop('bounded_withdrawal',None)
        home.update(rtde=current,home_pose=list(contract.home_pose),home_q=geometry['home_q'],clearance_entry=True)
        home_receipt=out/'clearance-home.json';home_receipt.write_text(json.dumps(home,indent=2)+'\n')
        # Never overlap the monitored relief writer and the independent Home
        # writer, even when this helper is entered from an exception path.
        obs.close();sensor.close()
        if video is not None:
            video.close();video=None
        if lease_held:
            lease.__exit__(None,None,None);lease_held=False
        home_invoked=True
        result['home_attempted']=True
        home_args=SimpleNamespace(host=args.host,home_receipt=home_receipt,validation=Path(args.readback_proof_dir)/f'{BASENAME}-validation.json',package_dir=packages,readback_dir=Path(args.readback_dir)/BASENAME,output=out/'home',execute=True)
        try:
            home_result=run_home(home_args)
        except BaseException as exc:
            home_result={'success':False,'error':f'{type(exc).__name__}: {exc}'}
        result['home']=home_result
        result['success']=home_result.get('success') is True
        result['home_blocked']=not result['success']
        if result['success']:
            result['state']='HOME_RECOVERED'
        else:
            result['state']='BLOCKED'
            result['error']=str(home_result.get('error','verified Home was not completed'))
            result['home_blocked_reason']=result['error']
        return home_result

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
        live_protective=_protective_safety(dashboard_before)
        if live_protective:
            # The source writer may still have a Dashboard PLAY state after a
            # protective stop. Stop it first, then use the already fresh,
            # stationary observer sample as the quiescence proof.
            stopper=RemoteDashboardWriter(args.host,load_target=f'{DIRECTORY}/{BASENAME}.urp')
            stopper.write('stop')
            dashboard_before=_dashboard_until_stopped(args.host)
            if not _protective_safety(dashboard_before):
                raise ValueError(f'protective-stop source changed unexpectedly: {dashboard_before}')
            quiescence=row
            result['protective_quiescence']={'stationary':stationary(quiescence),'fresh':True,'safety_mode':dashboard_before.get('safetymode'),'attempted_at':time.monotonic()}
            protective_unlock_attempted=True
            result['protective_unlock']=_unlock_protective_stop_once(args.host,target=f'{DIRECTORY}/{BASENAME}.urp')
            check_dashboard(args.host)
        else:
            check_dashboard(args.host)
        plan=plan_home_recovery(row['actual_TCP_pose'],contract.home_pose);result['plan']=plan
        geometry=check_geometry(row,plan);result['geometry']=geometry
        guard=ReliefForceGuard(initial_raw_wrench=raw,no_load_wrench=baseline['mean_wrench_n_nm'],baseline_std_wrench=baseline['std_wrench_n_nm'],rotation=so3_exp(row['actual_TCP_pose'][3:]))
        def check():
            nonlocal last_sensor,last_force
            current=obs.latest();validate_robot_sample(current);video.check()
            raw,received=sensor.poll();now=time.monotonic()
            if raw is None or received is None or not 0<=now-received<.08:raise ValueError('recovery Kunwei observation stale')
            if last_sensor is None or received>last_sensor:
                force=guard.update(raw,now_s=received);last_sensor=received;last_force=force
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
                        # TP can still be completing its terminal bookkeeping
                        # after physical motion has stopped. Keep observing force,
                        # pose and video while awaiting program termination.
                        terminal = dashboard_exchange(args.host, ['safetymode', 'running'])
                        if terminal.get('safetymode') != 'Safetymode: NORMAL':
                            raise ValueError(f'relief safety changed: {terminal}')
                        if terminal.get('running') == 'Program running: true':
                            time.sleep(.01)
                            continue
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
        row,force=check();invoke_home(row,force)
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
            # A relief fault is still routed to Home whenever the already
            # observed clearance and force release make that command safe.
            # If those facts are unavailable, BLOCKED records the exact
            # missing proof rather than pretending that a revoke is recovery.
            if not home_invoked:
                try:
                    invoke_home(row,last_force)
                except BaseException as home_exc:
                    result['home_blocked']=True
                    result['home_blocked_reason']=f'{type(home_exc).__name__}: {home_exc}'
        except BaseException as stop:
            result['stop_observation_error']=str(stop)
            result['home_blocked']=True
            result['home_blocked_reason']=f'{type(stop).__name__}: {stop}'
        if not result.get('success'):
            result['state']='BLOCKED'
            result['trial_stays_failed']=True
        if protective_unlock_attempted and 'protective_unlock' not in result:
            result['protective_unlock_error']='protective stop was observed but its one-shot unlock did not complete'
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
    """Route every failed contact attempt through the existing Home owner.

    Packages must already be installed. Protective stops are handled inside
    :func:`run` by the one-shot quiescence/unlock gate; a missing or pre-ARM
    dispatch receipt is also recoverable. A failure to establish the required
    physical proof is returned as ``success=False``/``BLOCKED`` and never
    silently relabeled as a successful trial.
    """
    source=Path(source_run)
    output=source.with_name(source.name+'-autonomous-home')
    try:
        if not (PACKAGE_DIR/f'{RELIEF_PROGRAM}.script').exists():
            return _blocked_recovery_result(
                source,
                ValueError('relief package is not installed; Home recovery cannot be proven'),
                phase='package-preflight',
                output=output,
            )
        return run(SimpleNamespace(source_run=source,output=output,readback_proof_dir=None,readback_dir=None,package_dir=PACKAGE_DIR,host=host,video_url=video_url,execute=True))
    except BaseException as exc:
        return _blocked_recovery_result(source,exc,phase='recovery-preflight',output=output)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-run',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--readback-proof-dir',type=Path);p.add_argument('--readback-dir',type=Path);p.add_argument('--package-dir',type=Path);p.add_argument('--host',default='192.168.1.18');p.add_argument('--video-url',default='rtsp://127.0.0.1:8554/arm');p.add_argument('--execute',action='store_true');args=p.parse_args(argv)
    try:result=run(args)
    except BaseException as exc:
        result=_blocked_recovery_result(
            args.source_run,
            exc,
            phase='cli-preflight',
            output=args.output,
        )
    print(json.dumps(result,indent=2));return 0 if result.get('success') or (result.get('motion') is False and result.get('state') != 'BLOCKED') else 1

if __name__=='__main__':raise SystemExit(main())
