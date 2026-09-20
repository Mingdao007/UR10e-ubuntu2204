"""Recovery sequencing with fake endpoints; never moves hardware."""
import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
import run_contact_recovery as runner
from contact_yield_math import so3_exp

from contact_yield_task_frame import FIGURE8_CONTACT_HOME_XYZ_M
HOME=[*FIGURE8_CONTACT_HOME_XYZ_M,2.033134243,2.394988424,0.]

@pytest.mark.parametrize('failure',[None,'reload','no_release','stale','user_interrupt','post_clearance','home_failure'])
def test_only_released_stopped_lift_can_reach_home(tmp_path,monkeypatch,failure):
    clock=SimpleNamespace(t=100.,played=None)
    events=[]
    def sleep(dt):clock.t+=dt
    monkeypatch.setattr(runner,'time',SimpleNamespace(time=lambda:clock.t,monotonic=lambda:clock.t,sleep=sleep))
    monkeypatch.setattr(runner,'validate_recovery_packages',lambda *_:None)
    monkeypatch.setattr(
        runner,
        'check_dashboard',
        lambda *_, **__: (
            events.append('stopped_read')
            or {
                'safetymode':'Safetymode: NORMAL',
                'running':'Program running: false',
                'robotmode':'Robotmode: RUNNING',
                'is in remote control':'true',
            }
        ),
    )
    def terminal_read(*_):
        events.append('terminal_read')
        return {'safetymode':'Safetymode: NORMAL',
                'running':'Program running: true' if events.count('terminal_read') < 3 else 'Program running: false'}
    monkeypatch.setattr(runner,'dashboard_exchange',terminal_read)
    monkeypatch.setattr(runner,'load_identity_contract',lambda:SimpleNamespace(home_pose=HOME,eoat_sha256='tool'))
    monkeypatch.setattr(runner,'check_geometry',lambda *_:{'home_q':[0.]*6,'pass':True})
    class Lock:
        def __init__(self,*_):pass
        def __enter__(self):events.append('lock');return self
        def __exit__(self,*_):events.append('unlock')
    monkeypatch.setattr(runner,'WriterLock',Lock)
    start=np.array(HOME);start[0]-=.0024;start[2]-=.001
    def elapsed():return 0. if clock.played is None else clock.t-clock.played
    class Obs:
        def __init__(self,*_):self.rows=[];self.thread=True
        def start(self):pass
        def latest(self):
            pose=start.copy()
            if clock.played is not None:pose[2]=min(HOME[2],start[2]+elapsed()*.0005)
            row={'actual_TCP_pose':pose.tolist(),'actual_TCP_speed':[0.]*6,'actual_q':[0.]*6,'actual_qd':[0.]*6,'tcp_offset':[0.,0.,.0874,0.,0.,0.],'payload':.413,'payload_cog':[.0011,.0031,.0163],'safety_status_bits':1,'monotonic_s':clock.t}
            self.rows.append(row);return row
        def close(self):pass
    monkeypatch.setattr(runner,'Observer',Obs)
    rotation=so3_exp(HOME[3:])
    class Sensor:
        def __init__(self,*_,**__):pass
        def open(self):pass
        def poll(self):
            if failure=='user_interrupt' and elapsed()>.1:raise KeyboardInterrupt('operator stop')
            if failure=='post_clearance' and elapsed()>2.2:raise RuntimeError('post-clearance observation failed')
            load=25. if clock.played is None else max(0.,25.-elapsed()*50.)
            if failure=='reload' and elapsed()>.15:load=30.
            if failure=='no_release' and clock.played is not None:load=max(5.,load)
            at=clock.t-(.1 if failure=='stale' and elapsed()>.1 else 0.)
            return np.r_[rotation.T@np.array([0.,0.,load]),np.zeros(3)].tolist(),at
        def close(self):pass
    monkeypatch.setattr(runner,'LiveR004KunweiTransport',Sensor)
    class Video:
        def __init__(self,url,out):self.path=out/'video.mkv'
        def start(self):self.path.write_bytes(b'x'*600)
        def check(self):pass
        def close(self):pass
    monkeypatch.setattr(runner,'VideoRecorder',Video)
    monkeypatch.setattr(runner,'RemoteDashboardWriter',lambda *_,**__:object())
    class Adapter:
        play_issued=False
        def __init__(self,**_):pass
        def load(self):events.append('lift_load');return 'loaded'
        def play(self):events.append('lift_play');self.play_issued=True;clock.played=clock.t;return 'played'
        def stop(self):events.append('lift_stop');return 'stopped'
    monkeypatch.setattr(runner,'_ExactLoadAdapter',Adapter)
    def home(args):
        events.append('home')
        assert events[-2]=='unlock'
        return {'success':failure != 'home_failure', 'error':'Home read-back failed'}
    monkeypatch.setattr(runner,'run_home',home)
    source=tmp_path/'source';source.mkdir()
    (source/'software_baseline_receipt.json').write_text(json.dumps({'eoat_identity_sha256':'tool','mean_wrench_n_nm':[0.]*6,'std_wrench_n_nm':[.02]*6}))
    (source/'dispatch_receipt.json').write_text(json.dumps({'armed':True,'stop':{'protective_stop':False}}))
    args=SimpleNamespace(source_run=source,output=tmp_path/'recovery',readback_proof_dir=tmp_path,readback_dir=tmp_path,host='fake',video_url='fake',execute=True)
    result=runner.run(args)
    assert result['success'] is (failure in (None, 'post_clearance')),result
    assert ('home' in events) is (failure in (None, 'post_clearance', 'home_failure'))
    if failure and failure not in ('home_failure', 'post_clearance'):
        assert 'lift_stop' in events
        assert events.index('lift_stop')<events.index('unlock')
        assert result['home_required'] is True
        assert result['home_attempted'] is False
        assert result['home_blocked'] is True
        assert result['home_blocked_reason']
    if failure == 'home_failure':
        assert result['state']=='BLOCKED'
        assert 'Home read-back failed' in result['error']
    if failure == 'post_clearance':
        assert result['home_attempted'] is True
        assert result['home']['success'] is True
        assert result['state']=='HOME_RECOVERED'
    assert (args.output/'result.json').exists()


def test_recovery_preflight_failure_is_persisted_as_blocked(tmp_path, monkeypatch):
    packages=tmp_path/'packages'
    packages.mkdir()
    (packages/f'{runner.RELIEF_PROGRAM}.script').write_text('relief')
    source=tmp_path/'source'
    source.mkdir()
    monkeypatch.setattr(runner, 'PACKAGE_DIR', packages)
    def fail(*_args):
        raise ValueError('read-back proof expired')
    monkeypatch.setattr(runner, 'validate_recovery_packages', fail)

    result=runner.recover_failed_contact_run(source, 'fake', 'fake')

    assert result['success'] is False
    assert result['motion'] is False
    assert result['state']=='BLOCKED'
    assert result['recovery_policy']=='AUTO_HOME_WHEN_COMMANDABLE'
    assert result['home_required'] is True
    assert result['home_attempted'] is False
    record=Path(result['recovery_record'])
    assert record.exists()
    persisted=json.loads(record.read_text())
    assert persisted['state']=='BLOCKED'
    assert persisted['phase']=='recovery-preflight'


def test_recovery_cli_never_emits_unclassified_preflight_error(tmp_path, monkeypatch):
    source=tmp_path/'source'
    source.mkdir()
    output=tmp_path/'cli-recovery'
    def fail(_args):
        raise RuntimeError('dashboard unavailable')
    monkeypatch.setattr(runner, 'run', fail)

    assert runner.main(['--source-run', str(source), '--output', str(output)]) == 1

    persisted=json.loads((output/'result.json').read_text())
    assert persisted['state']=='BLOCKED'
    assert persisted['phase']=='cli-preflight'
    assert persisted['recovery_policy']=='AUTO_HOME_WHEN_COMMANDABLE'
    assert persisted['home_required'] is True
    assert persisted['home_attempted'] is False


def test_recovery_retries_after_prior_blocked_output(tmp_path, monkeypatch):
    packages=tmp_path/'packages'
    packages.mkdir()
    (packages/f'{runner.RELIEF_PROGRAM}.script').write_text('relief')
    source=tmp_path/'source'
    source.mkdir()
    prior=source.with_name(source.name+'-autonomous-home')
    prior.mkdir()
    (prior/'blocked-preflight.json').write_text(json.dumps({
        'success':False,'state':'BLOCKED','home_blocked':True,
    }))
    # The old readback directory is also present; it must not prevent a new
    # recovery from obtaining its own fresh proof.
    (prior.with_name(prior.name+'-readback')).mkdir()
    monkeypatch.setattr(runner, 'PACKAGE_DIR', packages)
    calls=[]
    def fake_run(args):
        calls.append(args)
        return {'success':True,'state':'HOME_RECOVERED','home_attempted':True}
    monkeypatch.setattr(runner, 'run', fake_run)

    result=runner.recover_failed_contact_run(source, 'fake', 'fake')

    assert result['success'] is True
    assert result['state']=='HOME_RECOVERED'
    assert result['recovery_output'] != str(prior)
    assert result['previous_recovery_output']==str(prior)
    assert calls and calls[0].output != prior
    assert prior.joinpath('blocked-preflight.json').exists()


def test_recovery_reuses_completed_home_receipt(tmp_path, monkeypatch):
    source=tmp_path/'source'
    source.mkdir()
    prior=source.with_name(source.name+'-autonomous-home')
    prior.mkdir()
    (prior/'result.json').write_text(json.dumps({
        'success':True,'state':'HOME_RECOVERED','home_attempted':True,
    }))
    monkeypatch.setattr(runner, 'run', lambda *_: pytest.fail('completed Home must be idempotent'))

    result=runner.recover_failed_contact_run(source, 'fake', 'fake')

    assert result['success'] is True
    assert result['state']=='HOME_RECOVERED'
    assert result['recovery_output']==str(prior)


def test_commandable_playing_race_is_stopped_before_home_preflight(monkeypatch):
    rows = [
        {
            'safetymode': 'Safetymode: NORMAL',
            'running': 'Program running: true',
            'robotmode': 'Robotmode: RUNNING',
            'is in remote control': 'true',
        },
        {
            'safetymode': 'Safetymode: NORMAL',
            'running': 'Program running: false',
            'robotmode': 'Robotmode: RUNNING',
            'is in remote control': 'true',
        },
    ]
    commands = []

    monkeypatch.setattr(runner, 'dashboard_exchange', lambda *_: rows.pop(0))

    class Writer:
        def __init__(self, *_, **__):
            pass

        def write(self, command):
            commands.append(command)
            return SimpleNamespace(command=command, response='Stopping program')

    monkeypatch.setattr(runner, 'RemoteDashboardWriter', Writer)
    result = runner.check_dashboard('fake', stop_if_running=True)
    assert commands == ['stop']
    assert result['running'] == 'Program running: false'
    assert result['recovery_stop_command']['command'] == 'stop'
