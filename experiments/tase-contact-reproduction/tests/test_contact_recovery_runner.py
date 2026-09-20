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

@pytest.mark.parametrize('failure',[None,'reload','no_release','stale','user_interrupt'])
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
        return {'success':True}
    monkeypatch.setattr(runner,'run_home',home)
    source=tmp_path/'source';source.mkdir()
    (source/'software_baseline_receipt.json').write_text(json.dumps({'eoat_identity_sha256':'tool','mean_wrench_n_nm':[0.]*6,'std_wrench_n_nm':[.02]*6}))
    (source/'dispatch_receipt.json').write_text(json.dumps({'armed':True,'stop':{'protective_stop':False}}))
    args=SimpleNamespace(source_run=source,output=tmp_path/'recovery',readback_proof_dir=tmp_path,readback_dir=tmp_path,host='fake',video_url='fake',execute=True)
    result=runner.run(args)
    assert result['success'] is (failure is None),result
    assert ('home' in events) is (failure is None)
    if failure:
        assert 'lift_stop' in events
        assert events.index('lift_stop')<events.index('unlock')
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
    assert result['recovery_policy']=='AUTO_HOME_UNLESS_SAFETY_PROOF_BLOCKS'
    record=Path(result['recovery_record'])
    assert record.exists()
    persisted=json.loads(record.read_text())
    assert persisted['state']=='BLOCKED'
    assert persisted['phase']=='recovery-preflight'
