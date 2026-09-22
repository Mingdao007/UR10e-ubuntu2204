"""Sole-owner stopped -> monitored vertical relief -> clearance Home recovery.

Normal task admission is never relaxed. Recovery is a distinct, directional
operation; a failed trial remains failed. Every commandable fault enters this
same Home path, including faults raised while the relief program is running. A
protective stop is handled by one quiescence check and one allow-listed
Dashboard unlock, followed by a fresh Safety NORMAL check. The only terminal
alternative is ``BLOCKED`` when a Home command cannot safely be established or
the Home owner itself fails; there is no revoke-only terminal disposition.
"""
import argparse,copy,datetime,json,subprocess,time
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pinocchio as pin
from build_contact_recovery import RELIEF_PROGRAM
from contact_home_motion_profile import (
    HOME_ANGULAR_SPEED_GUARD_RAD_S,
    HOME_JOINT_SPEED_GUARD_RAD_S,
    HOME_TRANSFER_SPEED_M_S,
    HOME_VERTICAL_SPEED_M_S,
)
from contact_yield_live_contract import load_identity_contract,PACKAGE_DIR
from contact_yield_supervisor import VideoRecorder
from contact_yield_math import so3_exp,so3_log
from contact_home_recovery_policy import (
    plan_home_recovery,
    plan_staged_home_recovery,
    ReliefForceGuard,
    validate_lift_sample,
)
from run_contact_home import Observer,validate_robot_sample,run as run_home,INSTALLED_LOCK,BASENAME
from step5d_remote_startup import RemoteDashboardWriter,_ExactLoadAdapter,dashboard_exchange
from step5d_autotune_v4_r014.dispatcher import WriterLock
from step5d_autotune_v4_r004.transport import LiveR004KunweiTransport
from step5c_calibrated_kinematics_audit import build_calibrated_model,base_to_tool0
from step5d_autotune_v4_r004.calibrated_runtime import tcp_jacobian_base

DIRECTORY='/programs/andyl/kunwei/step5'
RECOVERY_POLICY='AUTO_HOME_WHEN_COMMANDABLE'
VIDEO_POLICIES = frozenset(('required', 'evidence-only'))


def _video_evidence(video):
    recorder = getattr(video, 'evidence', None)
    return recorder() if callable(recorder) else None
# Use the historical 40 mm/s vertical command, with a small observer margin
# for the controller's first RTDE sample. This is still a vertical-only
# relief and does not relax force, lateral, attitude, freshness, or safety
# checks.
RECOVERY_LIFT_SPEED_LIMIT_M_S = HOME_VERTICAL_SPEED_M_S + 0.010
# A UR controller can report one short downward TCP-speed sample while a
# vertical relief program is entering RUNNING.  Treat that sample as a
# commandable startup race: stop, wait for a fresh stationary observation, and
# retry the exact installed relief triplet a bounded number of times.  This is
# deliberately narrow; force, lateral, attitude, stale-sensor, and safety
# violations remain terminal recovery faults.
RELIEF_STARTUP_RETRY_LIMIT = 3
RELIEF_STARTUP_SETTLE_S = 0.20
RELIEF_STARTUP_RETRYABLE_ERROR = 'lift downward velocity'


def _protective_safety(row):
    return 'PROTECTIVE_STOP' in str(row.get('safetymode','')).upper()


def check_dashboard(host, *, allow_protective=False, stop_if_running=False):
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
        if row.get('running') == 'Program running: false':
            return row
        if stop_if_running and row.get('running') == 'Program running: true':
            # A failed writer can leave the Dashboard PLAYING for a short
            # interval after its RTDE stop acknowledgement.  This is still a
            # commandable recovery state: issue the idempotent STOP, wait for
            # the controller's STOPPED echo, then let the normal observer/
            # clearance gates decide whether Home is safe.  Do not classify
            # this transient race as a terminal Home block.
            stopper = RemoteDashboardWriter(
                host, load_target=f'{DIRECTORY}/{BASENAME}.urp'
            )
            stop_outcome = stopper.write('stop')
            stopped = _dashboard_until_stopped(host)
            if stopped.get('safetymode') != 'Safetymode: NORMAL':
                raise ValueError(f'recovery safety changed after STOP: {stopped}')
            stopped['recovery_stop_command'] = {
                'command': stop_outcome.command,
                'response': stop_outcome.response,
            }
            return stopped
        raise ValueError(f'recovery requires NORMAL but stopped Dashboard: {row}')
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


def _blocked_recovery_result(source, error, *, phase, output=None, previous_output=None):
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
        'recovery_owner_invoked':True,
        'home_commandability_checked':False,
        'home_attempted':False,
        'home_commandable':False,
        'home_motion_dispatched':False,
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
    if previous_output is not None:
        payload['previous_recovery_output']=str(previous_output)
    return payload


def _emergency_home_when_commandable(source, output, host, packages, *, reason,
                                     previous_output=None, video_url=None,
                                     video_policy='required'):
    """Attempt the installed Home owner even when relief setup failed.

    A recovery-package/read-back failure is a failure of the preferred
    vertical-relief route; it is not, by itself, a reason to leave a
    commandable robot stopped.  This fallback deliberately does *not* bypass
    the Home owner's geometry, fresh RTDE, safety, video, or package checks.
    It first obtains a fresh stationary sample and admits only the existing
    clearance-entry Home corridor.  If the sample is below that corridor, or
    the controller/package cannot be proved, the result is an explicit
    communication/safety/geometry BLOCKED receipt.
    """
    source = Path(source)
    output = Path(output)
    payload = {
        'success': False,
        'motion': False,
        'state': 'BLOCKED',
        'source_attempt': str(source),
        'trial_stays_failed': True,
        'recovery_policy': RECOVERY_POLICY,
        'home_required': True,
        'recovery_owner_invoked': True,
        'home_commandability_checked': False,
        'home_attempted': False,
        'home_commandable': False,
        'home_motion_dispatched': False,
        'home_blocked': True,
        'preflight_error': str(reason),
    }
    if previous_output is not None:
        payload['previous_recovery_output'] = str(previous_output)
    if not host:
        payload['home_blocked_reason'] = 'controller host is unavailable for monitored Home recovery'
        return payload

    obs = None
    sensor = None
    lease = None
    lease_held = False
    video = None
    try:
        packages = Path(packages)
        if not (packages / f'{BASENAME}.script').exists():
            raise ValueError('Home package is not installed')
        from contact_recovery_readback import fetch_recovery_readback
        contract = load_identity_contract()
        # The read-back is intentionally Home-only here.  A missing relief
        # package must not prevent the independent Home owner from being
        # attempted when its own installed triplet is valid.
        proof = fetch_recovery_readback(
            output.with_name(output.name + '-fallback-readback'),
            packages,
            basenames=(BASENAME,),
        )
        lease = WriterLock(INSTALLED_LOCK)
        lease.__enter__()
        lease_held = True
        dashboard = check_dashboard(host, allow_protective=True, stop_if_running=True)
        obs = Observer(host)
        obs.start()
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            row = obs.latest()
            if stationary(row):
                if _protective_safety(dashboard):
                    validate_robot_sample(row, allow_protective=True)
                break
            time.sleep(.02)
        else:
            raise ValueError('fresh stationary RTDE sample unavailable for Home fallback')
        # Protective Stop may be commandable after a fresh stationary sample,
        # but it must be unlocked exactly once and re-verified before Home.
        if _protective_safety(dashboard):
            if not video_url:
                raise ValueError('Protective Stop Home fallback requires a video URL')
            video_dir = output.with_name(output.name + '-video-probe')
            video_dir.mkdir(parents=True, exist_ok=False)
            video = VideoRecorder(video_url, video_dir, policy='required')
            video.start()
            video.check()
            healthy = getattr(video, 'healthy', None)
            if callable(healthy) and not healthy():
                raise ValueError('Protective Stop Home fallback lacks fresh video quiescence evidence')
            if dashboard.get('running') == 'Program running: true':
                dashboard = _dashboard_until_stopped(host)
            video.check()
            healthy = getattr(video, 'healthy', None)
            if callable(healthy) and not healthy():
                raise ValueError('Protective Stop Home fallback video became stale before unlock')
            row = obs.latest()
            validate_robot_sample(row, allow_protective=True)
            if not stationary(row):
                raise ValueError('Protective Stop Home fallback RTDE sample is not stationary before unlock')
            sensor = LiveR004KunweiTransport('192.168.50.25', port=5152)
            sensor.open()
            force_deadline = time.monotonic() + 1.0
            while time.monotonic() < force_deadline:
                raw, received = sensor.poll()
                now = time.monotonic()
                if raw is None or received is None or not 0 <= now - received < .08:
                    time.sleep(.01)
                    continue
                wrench = np.asarray(raw, dtype=float)
                if wrench.shape != (6,) or not np.isfinite(wrench).all():
                    raise ValueError('Protective Stop Home fallback force sample is invalid')
                if np.linalg.norm(wrench[:3]) >= 20 or np.linalg.norm(wrench[3:]) >= 2:
                    raise ValueError('Protective Stop Home fallback force guard exceeded')
                break
            else:
                raise ValueError('Protective Stop Home fallback force sample is stale')
            # Force initialization can take up to a second.  Do not unlock
            # using the RTDE/video evidence captured before that wait: refresh
            # every quiescence predicate, then perform one final Dashboard
            # check immediately before the one-shot unlock.
            video.check()
            healthy = getattr(video, 'healthy', None)
            if callable(healthy) and not healthy():
                raise ValueError('Protective Stop Home fallback video became stale before unlock')
            row = obs.latest()
            validate_robot_sample(row, allow_protective=True)
            if not stationary(row):
                raise ValueError('Protective Stop Home fallback RTDE sample is not stationary before unlock')
            raw, received = sensor.poll()
            now = time.monotonic()
            if raw is None or received is None or not 0 <= now - received < .08:
                raise ValueError('Protective Stop Home fallback force sample is stale before unlock')
            wrench = np.asarray(raw, dtype=float)
            if wrench.shape != (6,) or not np.isfinite(wrench).all():
                raise ValueError('Protective Stop Home fallback force sample is invalid before unlock')
            if np.linalg.norm(wrench[:3]) >= 20 or np.linalg.norm(wrench[3:]) >= 2:
                raise ValueError('Protective Stop Home fallback force guard exceeded before unlock')
            dashboard = check_dashboard(host, allow_protective=True)
            if not _protective_safety(dashboard):
                raise ValueError(f'Protective Stop disappeared before unlock: {dashboard}')
            if dashboard.get('running') != 'Program running: false':
                raise ValueError(
                    f'Protective Stop Home fallback Dashboard is not stopped before unlock: {dashboard}'
                )
            # The Dashboard exchange is a blocking network read.  Refresh the
            # independent evidence after it returns so a slow response cannot
            # turn an old stationary/force/video sample into an unlock proof.
            video.check()
            healthy = getattr(video, 'healthy', None)
            if callable(healthy) and not healthy():
                raise ValueError('Protective Stop Home fallback video became stale before unlock')
            row = obs.latest()
            validate_robot_sample(row, allow_protective=True)
            if not stationary(row):
                raise ValueError('Protective Stop Home fallback RTDE sample is not stationary before unlock')
            raw, received = sensor.poll()
            now = time.monotonic()
            if raw is None or received is None or not 0 <= now - received < .08:
                raise ValueError('Protective Stop Home fallback force sample is stale before unlock')
            wrench = np.asarray(raw, dtype=float)
            if wrench.shape != (6,) or not np.isfinite(wrench).all():
                raise ValueError('Protective Stop Home fallback force sample is invalid before unlock')
            if np.linalg.norm(wrench[:3]) >= 20 or np.linalg.norm(wrench[3:]) >= 2:
                raise ValueError('Protective Stop Home fallback force guard exceeded before unlock')
            _unlock_protective_stop_once(
                host, target=f'{DIRECTORY}/{BASENAME}.urp'
            )
            dashboard = check_dashboard(host)
        else:
            check_dashboard(host)

        pose = np.asarray(row['actual_TCP_pose'], dtype=float)
        target = np.asarray(contract.home_pose, dtype=float)
        # Match the clearance-entry guard generated in build_contact_home;
        # do not convert an unknown low/contact pose into a lateral Home move.
        from contact_yield_math import so3_exp, so3_log
        payload['home_commandability_checked'] = True
        if pose[2] < target[2] - .001:
            raise ValueError('current TCP is below the clearance-entry Home floor')
        if np.linalg.norm(pose[:3] - target[:3]) > .080:
            raise ValueError('Home fallback transfer exceeds 80mm bound')
        if np.linalg.norm(so3_log(so3_exp(pose[3:]) @ so3_exp(target[3:]).T)) > .010:
            raise ValueError('Home fallback attitude is outside the clearance corridor')

        preserved = json.loads(
            (Path(__file__).resolve().parents[1]
             / 'report/contact-six-qp-20260917/preserved-home.json').read_text()
        )
        preserved.pop('bounded_recovery', None)
        preserved.pop('bounded_withdrawal', None)
        preserved.update(
            rtde=row,
            home_pose=list(contract.home_pose),
            clearance_entry=True,
            fallback_reason=str(reason),
        )
        home_receipt = output.with_name(output.name + '-fallback-home-receipt.json')
        home_receipt.write_text(json.dumps(preserved, indent=2) + '\n')
        if obs is not None:
            obs.close()
            obs = None

        output.mkdir(parents=True, exist_ok=False)
        home_args = SimpleNamespace(
            host=host,
            home_receipt=home_receipt,
            validation=Path(proof) / f'{BASENAME}-validation.json',
            package_dir=packages,
            readback_dir=Path(proof) / 'readback' / BASENAME,
            output=output / 'fallback-home',
            video_url=video_url or 'rtsp://127.0.0.1:8554/arm',
            video_policy=video_policy,
            execute=True,
        )
        payload['home_attempted'] = True
        if sensor is not None:
            sensor.close()
            sensor = None
        if lease_held:
            lease.__exit__(None, None, None)
            lease_held = False
        home_result = run_home(home_args)
        payload['home'] = home_result
        payload['motion'] = bool(
            home_result.get('success') is True or home_result.get('play') is not None
        )
        payload['success'] = home_result.get('success') is True
        payload['home_blocked'] = not payload['success']
        if payload['success']:
            payload['state'] = 'HOME_RECOVERED'
        else:
            payload['home_blocked_reason'] = str(
                home_result.get('failure') or home_result.get('error') or 'Home fallback failed'
            )
    except BaseException as exc:
        payload['home_blocked_reason'] = f'{type(exc).__name__}: {exc}'
    finally:
        if obs is not None:
            try:
                obs.close()
            except BaseException:
                pass
        if sensor is not None:
            try:
                sensor.close()
            except BaseException:
                pass
        if video is not None:
            try:
                video.close()
                evidence = getattr(video, 'evidence', None)
                payload['video_evidence'] = evidence() if callable(evidence) else None
            except BaseException as exc:
                payload['video_evidence_error'] = f'{type(exc).__name__}: {exc}'
        if lease_held and lease is not None:
            try:
                lease.__exit__(None, None, None)
            except BaseException:
                pass
    try:
        output.mkdir(parents=True, exist_ok=True)
        record = output / 'result.json'
        record.write_text(json.dumps(payload, indent=2, default=str) + '\n')
        payload['recovery_record'] = str(record)
    except BaseException as exc:
        payload['recovery_record_error'] = f'{type(exc).__name__}: {exc}'
    return payload


def _read_recovery_result(path):
    """Read a prior recovery receipt without treating it as live authority."""
    for name in ('result.json', 'blocked-preflight.json'):
        record=Path(path)/name
        if not record.exists():
            continue
        try:
            value=json.loads(record.read_text())
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _select_recovery_output(source):
    """Choose a non-colliding receipt directory for an automatic Home retry.

    A previous preflight or failed Home attempt is evidence to preserve, not a
    reason to suppress the next commandable Home.  A completed recovery is
    idempotent; an incomplete/blocked one gets a fresh sibling directory so
    its readback and result files cannot collide with the new attempt.
    """
    base=Path(source).with_name(Path(source).name+'-autonomous-home')
    if not base.exists():
        return base, None, None
    prior=_read_recovery_result(base)
    if prior and prior.get('success') is True and prior.get('state')=='HOME_RECOVERED':
        return base, None, prior
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    candidate=base.with_name(base.name+'-retry-'+stamp)
    suffix=0
    while candidate.exists():
        suffix+=1
        candidate=base.with_name(base.name+f'-retry-{stamp}-{suffix}')
    return candidate, base, None


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
    for a,b,speed in ((start,lift,HOME_VERTICAL_SPEED_M_S),(lift,home,HOME_TRANSFER_SPEED_M_S)):
        ra=so3_exp(a[3:]);turn=so3_log(so3_exp(b[3:])@ra.T)
        duration=max(np.linalg.norm(b[:3]-a[:3])/speed,np.linalg.norm(turn)/HOME_ANGULAR_SPEED_GUARD_RAD_S,.1)
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
    if max_residual>1e-7 or max_qd>HOME_JOINT_SPEED_GUARD_RAD_S:raise ValueError('recovery IK/speed check failed')
    return {'pass':True,'max_residual':max_residual,'max_joint_speed_rad_s':max_qd,'home_q':q.tolist(),'joint_delta':(q-initial).tolist(),'scope':'calibrated sampled geometry, not collision or force proof'}


def _plan_recovery_with_staged_fallback(start_pose, home_pose):
    """Use direct Home first, then the approved staged clearance route.

    Only the known direct SO(3) corridor rejection may enter the staged
    fallback.  Position, Home identity, rise, and numeric failures remain
    hard failures and are never hidden by a broader exception catch.
    """
    try:
        plan = plan_home_recovery(start_pose, home_pose)
        plan['route'] = 'direct'
        plan['direct_home_rejected'] = False
        return plan
    except ValueError as direct_error:
        if 'Home SO3 angle exceeds 10mrad' not in str(direct_error):
            raise
        plan = plan_staged_home_recovery(start_pose, home_pose)
        plan['direct_home_rejected'] = True
        plan['direct_home_rejection'] = str(direct_error)
        return plan


def stationary(row):
    return np.linalg.norm(row['actual_TCP_speed'])<.0005 and max(map(abs,row['actual_qd']))<.001


def _wait_relief_stop_and_stationary(adapter, observer, host, *, timeout_s=5.0):
    """Stop one relief attempt and prove a stable commandable retry point."""
    stop_response = adapter.stop()
    stopped_dashboard = _dashboard_until_stopped(host, timeout_s=timeout_s)
    deadline = time.monotonic() + timeout_s
    stable_since = None
    last = None
    while time.monotonic() < deadline:
        last = observer.latest()
        validate_robot_sample(last)
        if stationary(last):
            if stable_since is None:
                stable_since = time.monotonic()
            if time.monotonic() - stable_since >= RELIEF_STARTUP_SETTLE_S:
                dashboard = check_dashboard(host)
                return {
                    'stop_response': stop_response,
                    'dashboard': dashboard,
                    'stopped_dashboard': stopped_dashboard,
                    'stationary_sample': last,
                }
        else:
            stable_since = None
        time.sleep(.02)
    raise ValueError(
        f'relief retry could not confirm stationary RTDE after STOP: {last}'
    )


def _prepare_staged_home_package(output, current, geometry, contract, packages):
    """Build and install the staged Home triplet after vertical relief.

    The normal Home triplet intentionally keeps the direct 10 mrad corridor.
    A staged recovery needs a fresh start pose and the separate 20 mrad
    clearance corridor, so it must never reuse that direct package by name.
    This operation runs while the recovery writer lease is still held and the
    robot is stationary; it only uploads/read-backs a TP package and does not
    Load or Play it.
    """
    from build_contact_home import build as build_contact_home
    from contact_recovery_readback import fetch_recovery_readback

    root = Path(output) / 'staged-home-package'
    package_dir = root / 'package'
    receipt_path = root / 'home-receipt.json'
    package_dir.mkdir(parents=True, exist_ok=False)
    preserved = json.loads(
        (Path(__file__).resolve().parents[1]
         / 'report/contact-six-qp-20260917/preserved-home.json').read_text()
    )
    preserved.pop('bounded_recovery', None)
    preserved.pop('bounded_withdrawal', None)
    preserved.update(
        rtde=current,
        home_pose=list(contract.home_pose),
        home_q=geometry['home_q'],
        clearance_entry=True,
        bounded_recovery=True,
        bounded_withdrawal=False,
        recovery_route='staged_clearance_orientation',
        direct_home_rejected=True,
        direct_home_rejection='Home SO3 angle exceeds 10mrad',
        recovery_source_attempt=str(output.parent),
        actual_motion_performed=False,
    )
    receipt_path.write_text(json.dumps(preserved, indent=2) + '\n')
    build_contact_home(receipt_path, package_dir)

    deploy_readback = root / 'deploy-readback'
    deploy_readback.mkdir()
    owner = Path('/home/andy/codex-private-skills-shared-main/skills')
    deploy_cmd = [
        '/usr/bin/python3',
        str(owner / 'ur10e-controller-access/scripts/ur10e_controller_ssh.py'),
        'deploy-readback-triplet',
        '--local-directory', str(package_dir),
        '--basename', BASENAME,
        '--controller-directory', DIRECTORY,
        '--readback-directory', str(deploy_readback),
        '--confirm-deploy',
    ]
    completed = subprocess.run(
        deploy_cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    (root / 'deploy-readback.json').write_text(
        json.dumps({'command': deploy_cmd, 'stdout': completed.stdout, 'stderr': completed.stderr}, indent=2)
        + '\n'
    )
    proof = fetch_recovery_readback(
        root / 'proof', package_dir, basenames=(BASENAME,)
    )
    return {
        'package_dir': package_dir,
        'readback_dir': Path(proof) / 'readback' / BASENAME,
        'validation': Path(proof) / f'{BASENAME}-validation.json',
        'receipt': receipt_path,
        'proof': Path(proof),
    }


def _retryable_relief_startup_fault(error, row, plan):
    """Recognize only the known one-frame downward startup transient."""
    if str(error) != RELIEF_STARTUP_RETRYABLE_ERROR:
        return False
    return float(row['actual_TCP_pose'][2]) < float(plan['lift_pose'][2]) - .0001


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
    # The source writer normally leaves a stopped Dashboard, but the UR
    # controller can acknowledge the host stop before its PLAYING state has
    # propagated.  Clear that commandable race here instead of turning it
    # into ``BLOCKED`` and making the next retry responsible for Home.
    dashboard_before=check_dashboard(
        args.host, allow_protective=True, stop_if_running=True
    )
    if not args.execute:return {'success':False,'motion':False,'state':'read-only preflight passed','dashboard':dashboard_before,'source_protective_stop':source_protective}
    out.mkdir(parents=True)
    result={'success':False,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'source_attempt':str(source),'trial_stays_failed':True,'recovery_policy':RECOVERY_POLICY,'source_receipt_present':bool(source_receipt.get('receipt_present',True)),'source_armed':source_receipt.get('armed'),'source_protective_stop':source_protective,'dashboard_before':dashboard_before,'home_required':True,'recovery_owner_invoked':True,'home_commandability_checked':False,'home_attempted':False,'home_commandable':False,'home_motion_dispatched':False,'home_blocked':False}
    video_policy = getattr(args, 'video_policy', 'required')
    if video_policy not in VIDEO_POLICIES:
        raise ValueError(f'unknown video policy: {video_policy}')
    obs=Observer(args.host);sensor=LiveR004KunweiTransport('192.168.50.25',port=5152);video=None;adapter=None;wrench_rows=[];last_sensor=None;last_force=None;plan=None;geometry=None;geometry_scope=None;home_invoked=False
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
        result['home_commandability_checked'] = True
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
        result['home_commandable'] = True
        if geometry_scope == 'vertical_relief_only':
            # The initial pose can be too far laterally from Home for one
            # calibrated Home package, even though the pure vertical relief
            # is valid.  Release the relief owner first, then reuse the
            # bounded segmented Home owner from the fresh clearance pose.
            obs.close(); sensor.close()
            if video is not None:
                video.close()
                result['video_evidence'] = _video_evidence(video)
                video = None
            if lease_held:
                lease.__exit__(None, None, None); lease_held = False
            from run_segmented_home_recovery import run as run_segmented_home
            segmented_output = out / 'segmented-home'
            segmented = run_segmented_home(SimpleNamespace(
                output=segmented_output,
                host=args.host,
                video_url=args.video_url,
                video_policy=video_policy,
                execute=True,
            ))
            result['segmented_home_recovery'] = segmented
            result['home'] = segmented
            result['home_attempted'] = True
            result['home_motion_dispatched'] = bool(segmented.get('segments'))
            if segmented.get('success') is not True:
                result['success'] = False
                result['state'] = 'BLOCKED'
                result['home_blocked'] = True
                result['home_blocked_reason'] = str(
                    segmented.get('failure') or 'segmented Home recovery failed'
                )
                return segmented
            # Segmented recovery binds a fresh package to each start pose.
            # Restore the canonical Figure-eight Home triplet before the next
            # experiment so package admission cannot see a dynamic recovery
            # SHA. This is delivery/read-back only; it does not Load or Play.
            restore_dir = out / 'canonical-home-restore'
            restore_dir.mkdir(parents=False, exist_ok=False)
            owner = Path('/home/andy/codex-private-skills-shared-main/skills')
            restore_cmd = [
                '/usr/bin/python3',
                str(owner / 'ur10e-controller-access/scripts/ur10e_controller_ssh.py'),
                'deploy-readback-triplet',
                '--local-directory', str(PACKAGE_DIR),
                '--basename', BASENAME,
                '--controller-directory', DIRECTORY,
                '--readback-directory', str(restore_dir),
                '--confirm-deploy',
            ]
            restored = subprocess.run(
                restore_cmd, check=True, capture_output=True, text=True, timeout=60
            )
            result['canonical_home_restore'] = {
                'command': restore_cmd,
                'stdout': restored.stdout,
                'stderr': restored.stderr,
                'readback_dir': str(restore_dir),
            }
            result['success'] = True
            result['state'] = 'HOME_RECOVERED'
            result['home_blocked'] = False
            return segmented
        home=json.loads((Path(__file__).resolve().parents[1]/'report/contact-six-qp-20260917/preserved-home.json').read_text())
        home.pop('bounded_recovery',None);home.pop('bounded_withdrawal',None)
        home.update(
            rtde=current,
            home_pose=list(contract.home_pose),
            home_q=geometry['home_q'],
            clearance_entry=True,
            bounded_recovery=bool(plan.get('staged_recovery', False)),
            recovery_route=plan.get('route', 'direct'),
            direct_home_rejected=bool(plan.get('direct_home_rejected', False)),
            direct_home_rejection=plan.get('direct_home_rejection'),
        )
        home_receipt=out/'clearance-home.json';home_receipt.write_text(json.dumps(home,indent=2)+'\n')
        home_package_dir=packages
        home_readback_dir=Path(args.readback_dir)/BASENAME
        home_validation=Path(args.readback_proof_dir)/f'{BASENAME}-validation.json'
        if plan.get('staged_recovery'):
            # The installed direct package has a deliberate 10 mrad guard.
            # Bind and install a fresh staged triplet only after relief has
            # produced a stationary clearance pose, then use its own proof.
            staged_package=_prepare_staged_home_package(
                out, current, geometry, contract, packages
            )
            home_package_dir=staged_package['package_dir']
            home_readback_dir=staged_package['readback_dir']
            home_validation=staged_package['validation']
            result['staged_home_package']={
                'package_dir':str(staged_package['package_dir']),
                'readback_dir':str(staged_package['readback_dir']),
                'validation':str(staged_package['validation']),
                'receipt':str(staged_package['receipt']),
            }
        # Never overlap the monitored relief writer and the independent Home
        # writer, even when this helper is entered from an exception path.
        obs.close();sensor.close()
        if video is not None:
            video.close()
            result['video_evidence'] = _video_evidence(video)
            video=None
        if lease_held:
            lease.__exit__(None,None,None);lease_held=False
        home_invoked=True
        result['home_attempted']=True
        result['home_motion_dispatched']=True
        home_args=SimpleNamespace(host=args.host,home_receipt=home_receipt,validation=home_validation,package_dir=home_package_dir,readback_dir=home_readback_dir,output=out/'home',video_url=args.video_url,video_policy=video_policy,execute=True)
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
        live_protective = _protective_safety(dashboard_before)
        obs.start();sensor.open()
        video = (
            VideoRecorder(args.video_url, out)
            if video_policy == 'required'
            else VideoRecorder(args.video_url, out, policy=video_policy)
        )
        video.start()
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            video.check();row=obs.latest();validate_robot_sample(row, allow_protective=live_protective);raw,at=sensor.poll()
            video_ready = (
                True
                if video_policy == 'evidence-only'
                else video.path.exists() and video.path.stat().st_size > 512
            )
            if len(obs.rows)>=10 and raw is not None and video_ready:break
            time.sleep(.01)
        else:raise ValueError('recovery observer/sensor barrier failed')
        if not stationary(row):raise ValueError('robot is not stationary before recovery')
        if live_protective:
            healthy = getattr(video, 'healthy', None)
            video_healthy = healthy() if callable(healthy) else getattr(video, 'available', True)
            if not video_healthy:
                raise ValueError('Protective Stop recovery requires fresh video quiescence evidence')
            # The source writer may still have a Dashboard PLAY state after a
            # protective stop. Stop it first, then use the already fresh,
            # stationary observer sample as the quiescence proof.
            stopper=RemoteDashboardWriter(args.host,load_target=f'{DIRECTORY}/{BASENAME}.urp')
            stopper.write('stop')
            dashboard_before=_dashboard_until_stopped(args.host)
            if not _protective_safety(dashboard_before):
                raise ValueError(f'protective-stop source changed unexpectedly: {dashboard_before}')
            video.check()
            healthy = getattr(video, 'healthy', None)
            if callable(healthy) and not healthy():
                raise ValueError('Protective Stop video became stale before unlock')
            row = obs.latest()
            validate_robot_sample(row, allow_protective=True)
            if not stationary(row):
                raise ValueError('Protective Stop RTDE sample is no longer stationary before unlock')
            raw, received = sensor.poll()
            now = time.monotonic()
            if raw is None or received is None or not 0 <= now - received < .08:
                raise ValueError('Protective Stop force sample is stale before unlock')
            wrench = np.asarray(raw, dtype=float)
            if wrench.shape != (6,) or not np.isfinite(wrench).all():
                raise ValueError('Protective Stop force sample is invalid before unlock')
            if np.linalg.norm(wrench[:3]) >= 20 or np.linalg.norm(wrench[3:]) >= 2:
                raise ValueError('Protective Stop force guard exceeded before unlock')
            # The force poll above is intentionally followed by a fresh
            # Dashboard read and a second RTDE/video sample.  This keeps the
            # one-shot unlock adjacent to all three independent quiescence
            # proofs, even when sensor setup or Dashboard STOP propagation is
            # slow.
            dashboard_before = check_dashboard(
                args.host, allow_protective=True
            )
            if not _protective_safety(dashboard_before):
                raise ValueError(
                    f'protective-stop source changed before unlock: {dashboard_before}'
                )
            if dashboard_before.get('running') != 'Program running: false':
                raise ValueError(
                    f'protective-stop Dashboard is not stopped before unlock: {dashboard_before}'
                )
            # Recompute freshness after the final Dashboard exchange.  The
            # controller may take long enough to answer that the evidence
            # sampled immediately before the exchange is no longer valid.
            video.check()
            healthy = getattr(video, 'healthy', None)
            if callable(healthy) and not healthy():
                raise ValueError('Protective Stop video became stale before unlock')
            row = obs.latest()
            validate_robot_sample(row, allow_protective=True)
            if not stationary(row):
                raise ValueError('Protective Stop RTDE sample is no longer stationary before unlock')
            raw, received = sensor.poll()
            now = time.monotonic()
            if raw is None or received is None or not 0 <= now - received < .08:
                raise ValueError('Protective Stop force sample is stale before unlock')
            wrench = np.asarray(raw, dtype=float)
            if wrench.shape != (6,) or not np.isfinite(wrench).all():
                raise ValueError('Protective Stop force sample is invalid before unlock')
            if np.linalg.norm(wrench[:3]) >= 20 or np.linalg.norm(wrench[3:]) >= 2:
                raise ValueError('Protective Stop force guard exceeded before unlock')
            quiescence=row
            result['protective_quiescence']={'stationary':stationary(quiescence),'fresh':True,'safety_mode':dashboard_before.get('safetymode'),'attempted_at':time.monotonic()}
            protective_unlock_attempted=True
            result['protective_unlock']=_unlock_protective_stop_once(args.host,target=f'{DIRECTORY}/{BASENAME}.urp')
            check_dashboard(args.host)
        else:
            check_dashboard(args.host)
        result['home_commandability_checked'] = True
        plan=_plan_recovery_with_staged_fallback(row['actual_TCP_pose'],contract.home_pose)
        result['plan']=plan
        result['recovery_route']=plan.get('route')
        result['direct_home_rejected']=bool(plan.get('direct_home_rejected', False))
        if plan.get('direct_home_rejection'):
            result['direct_home_rejection']=plan['direct_home_rejection']
        try:
            geometry=check_geometry(row,plan);result['geometry']=geometry
        except ValueError as geometry_error:
            # A low-Z pose can satisfy the vertical relief geometry while the
            # full lateral Home transfer exceeds the calibrated joint-speed
            # envelope. Keep that proof separate and defer the lateral move to
            # the bounded segmented Home owner after force release.
            if not plan.get('needs_lift') or 'recovery IK/speed check failed' not in str(geometry_error):
                raise
            relief_plan=dict(plan)
            relief_plan['home_pose']=list(plan['lift_pose'])
            geometry=check_geometry(row,relief_plan)
            geometry_scope='vertical_relief_only'
            result['geometry']=geometry
            result['geometry_scope']=geometry_scope
            result['deferred_full_home_geometry_error']=str(geometry_error)
        result['lift_guard']={
            'speed_limit_m_s': RECOVERY_LIFT_SPEED_LIMIT_M_S,
            'pure_policy_speed_limit_m_s': HOME_VERTICAL_SPEED_M_S,
            'reason': 'historical 40mm/s vertical command plus 10mm/s RTDE startup margin; other guards unchanged',
        }
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
            result['relief_attempts']=[]
            relief_deadline=time.monotonic()+60.
            relief_attempt=0
            while True:
                relief_attempt += 1
                attempt_record={'attempt':relief_attempt}
                should_retry=False
                try:
                    attempt_record['load']=adapter.load()
                    result.setdefault('relief_load', attempt_record['load'])
                    check()
                    attempt_record['play']=adapter.play()
                    result.setdefault('relief_play', attempt_record['play'])
                    stopped_since=None
                    while time.monotonic()<relief_deadline:
                        row,force=check()
                        try:
                            validate_lift_sample(
                                plan, row['actual_TCP_pose'], row['actual_TCP_speed'],
                                speed_limit_m_s=RECOVERY_LIFT_SPEED_LIMIT_M_S,
                            )
                        except ValueError as lift_error:
                            if (
                                relief_attempt < RELIEF_STARTUP_RETRY_LIMIT
                                and time.monotonic() < relief_deadline
                                and _retryable_relief_startup_fault(lift_error, row, plan)
                            ):
                                attempt_record['retry_reason']=str(lift_error)
                                attempt_record['retryable']=True
                                attempt_record['retry']=_wait_relief_stop_and_stationary(
                                    adapter, obs, args.host
                                )
                                result['relief_retry_count']=relief_attempt
                                should_retry=True
                                break
                            raise
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
                finally:
                    result['relief_attempts'].append(attempt_record)
                if result.get('relief_complete') or not should_retry:
                    break
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
        if video is not None:
            video.close()
            result['video_evidence'] = _video_evidence(video)
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


def recover_failed_contact_run(source_run, host, video_url, *, video_policy='required'):
    """Route every failed contact attempt through the existing Home owner.

    Packages must already be installed. Protective stops are handled inside
    :func:`run` by the one-shot quiescence/unlock gate; a missing or pre-ARM
    dispatch receipt is also recoverable. A failure to establish the required
    physical proof is returned as ``success=False``/``BLOCKED`` and never
    silently relabeled as a successful trial. The first action after the
    writer has stopped is a direct Home-owner probe. Only a fresh geometry
    proof that the robot is below the clearance floor or outside the direct
    attitude corridor may fall through to the monitored staged-relief route;
    readback/replay diagnostics stay after that recovery decision.
    """
    source=Path(source_run)
    output,previous_output,prior_success=_select_recovery_output(source)
    if prior_success is not None:
        # Repeated fault handling must not replay an already verified Home.
        # Returning the sealed receipt is safe and keeps the failed trial
        # failed while making the recovery operation idempotent.
        prior_success=dict(prior_success)
        prior_success.setdefault('recovery_output',str(output))
        return prior_success
    try:
        # Home has priority over post-fault analysis. This probe is deliberately
        # Home-only: it does not open Kunwei, start video, or run relief
        # diagnostics. When the robot is already in the clearance corridor it
        # can dispatch the sole Home owner immediately after the writer releases
        # the lock.
        if (PACKAGE_DIR/f'{BASENAME}.script').exists():
            home_first_output=output.with_name(output.name+'-home-first')
            home_first=_emergency_home_when_commandable(
                source,
                home_first_output,
                host,
                PACKAGE_DIR,
                reason=ValueError('home-first recovery priority'),
                previous_output=previous_output,
                video_url=video_url,
                video_policy=video_policy,
            )
            if home_first.get('success') is True:
                home_first['recovery_phase']='HOME_FIRST_DIRECT'
                home_first['diagnostics_deferred']=True
                home_first['recovery_output']=str(home_first_output)
                return home_first
            # A low/contact pose or a direct attitude mismatch is the only
            # expected reason to continue into staged relief. Any command,
            # readback, or safety failure remains terminal; repeating it through
            # a second owner would only delay the Home decision.
            reason_text=str(home_first.get('home_blocked_reason',''))
            staged_geometry_failure=(
                'below the clearance-entry Home floor' in reason_text
                or 'outside the clearance corridor' in reason_text
            )
            if not staged_geometry_failure:
                home_first['recovery_phase']='HOME_FIRST_BLOCKED'
                home_first['diagnostics_deferred']=True
                home_first['recovery_output']=str(home_first_output)
                return home_first
        if not (PACKAGE_DIR/f'{RELIEF_PROGRAM}.script').exists():
            return _emergency_home_when_commandable(
                source,
                output,
                host,
                PACKAGE_DIR,
                reason=ValueError('relief package is not installed; Home recovery cannot be proven'),
                previous_output=previous_output,
                video_url=video_url,
                video_policy=video_policy,
            )
        result=run(SimpleNamespace(
            source_run=source,
            output=output,
            readback_proof_dir=None,
            readback_dir=None,
            package_dir=PACKAGE_DIR,
            host=host,
            video_url=video_url,
            video_policy=video_policy,
            execute=True,
        ))
        if 'home_first' in locals():
            result['home_first_probe']=home_first
            result['recovery_phase']='STAGED_RELIEF_THEN_HOME'
            result['diagnostics_deferred']=True
        if previous_output is not None:
            result['previous_recovery_output']=str(previous_output)
            result['recovery_output']=str(output)
            try:
                (output/'result.json').write_text(json.dumps(result,indent=2,default=str)+'\n')
            except BaseException:
                pass
        return result
    except BaseException as exc:
        return _emergency_home_when_commandable(
            source,
            output,
            host,
            PACKAGE_DIR,
            reason=exc,
            previous_output=previous_output,
            video_url=video_url,
            video_policy=video_policy,
        )


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source-run',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--readback-proof-dir',type=Path);p.add_argument('--readback-dir',type=Path);p.add_argument('--package-dir',type=Path);p.add_argument('--host',default='192.168.1.18');p.add_argument('--video-url',default='rtsp://127.0.0.1:8554/arm');p.add_argument('--video-policy',choices=sorted(VIDEO_POLICIES),default='required');p.add_argument('--execute',action='store_true');args=p.parse_args(argv)
    try:result=run(args)
    except BaseException as exc:
        result = _emergency_home_when_commandable(
            args.source_run,
            args.output,
            args.host,
            args.package_dir or PACKAGE_DIR,
            reason=exc,
            video_url=args.video_url,
            video_policy=args.video_policy,
        )
    print(json.dumps(result,indent=2));return 0 if result.get('success') or (result.get('motion') is False and result.get('state') != 'BLOCKED') else 1

if __name__=='__main__':raise SystemExit(main())
