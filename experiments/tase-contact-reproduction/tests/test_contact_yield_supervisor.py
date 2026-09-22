"""Fault-injected lifecycle checks. No network endpoints or robot execution."""
from types import SimpleNamespace as NS
import json
import pytest
from contact_yield_supervisor import (
    ResidentSupervisor,
    READABLE_RUNTIME_IDENTITY,
    VideoRecorder,
    _complete_path_home_recovered,
)
from contact_yield_live import ObservedTransport


def rig(*, body_error=False, stop_delay=.1, safety_fault_at=None):
    t=NS(now=0.,phase='stopped',play_at=None,stop_at=None)
    events=[]
    def phase():
        if t.stop_at is not None and t.now-t.stop_at>=stop_delay:return 'stopped'
        if t.play_at is not None and t.now-t.play_at>=.1:return 'playing'
        return 'stopped'
    class Observer:
        def start(self):events.append('observer.start')
        def latest(self,**kwargs):
            safety=3 if safety_fault_at is not None and t.now>=safety_fault_at else 1
            return dict(timestamp=10+t.now,received_monotonic_s=t.now,observed_at_s=100+t.now,
                runtime_state=2 if phase()=='playing' else 1,safety_mode=safety,robot_mode=7,
                actual_TCP_speed=[0.]*6,actual_qd=[0.]*6,payload=.413,
                payload_cog=[.0011,.0031,.0163],tcp_offset=[0,0,.0874,0,0,0],
                output_int_register_32=606006,output_int_register_33=READABLE_RUNTIME_IDENTITY[0],
                output_int_register_34=READABLE_RUNTIME_IDENTITY[1])
        def close(self):events.append('observer.close')
    class Video:
        def start(self):events.append('video.start')
        def check(self):pass
        def close(self):events.append('video.close')
    def dashboard():
        return {'is in remote control':'true','safetymode':'Safetymode: NORMAL',
                'robotmode':'Robotmode: RUNNING','get loaded program':'Loaded program: /programs/test.urp',
                'running':'Program running: '+str(phase()=='playing').lower(),
                'programState':phase().upper()+' test.urp'}
    def write(command):
        events.append(command)
        if command=='play':t.play_at=t.now
        if command=='stop':t.stop_at=t.now
        return NS(response='ack')
    def body(s):
        events.append('body')
        if body_error:raise RuntimeError('first body failure')
        return {'ok':True}
    supervisor=ResidentSupervisor(observer=Observer(),video=Video(),
        read_dashboard=dashboard,writer=NS(write=write),target='/programs/test.urp',
        clock=lambda:t.now,sleep=lambda dt:setattr(t,'now',t.now+dt))
    return supervisor,body,t,events


def test_ack_is_not_completion_and_observers_span_body_and_stop():
    s,body,t,events=rig()
    result=s.run(body)
    assert result['success']
    assert events[:3]==['video.start','observer.start','load /programs/test.urp']
    assert events.index('body')<events.index('stop')<events.index('observer.close')
    assert result['dashboard_stop']['sample']['received_monotonic_s']>=t.stop_at+.1
    assert t.stop_at-t.play_at>=1.1
    assert events.count('play')==events.count('stop')==1


def test_idle_resident_check_never_loads_plays_or_stops():
    s,body,t,events=rig()
    result=s.run(body, execute_program=False)
    assert result['success']
    assert events == ['video.start','observer.start','body','video.close','observer.close']
    assert not any(event == 'play' or event == 'stop' or event.startswith('load ') for event in events)


def test_returning_state_has_independent_joint_home_speed_guard():
    s, body, t, events = rig()
    latest = s.observer.latest

    def returning_too_fast(**kwargs):
        row = latest(**kwargs)
        row['output_int_register_26'] = 40
        row['actual_qd'][0] = 0.151
        return row

    s.observer.latest = returning_too_fast
    with pytest.raises(RuntimeError, match='joint Home return speed envelope violated'):
        s.check()


def test_first_body_failure_survives_stop_timeout():
    s,body,t,events=rig(body_error=True,stop_delay=10.)
    result=s.run(body)
    assert not result['success']
    assert result['error']=='RuntimeError: first body failure'
    assert 'STOPPED was not observed' in result['stop_error']
    assert events[-1]=='observer.close'


def test_body_partial_receipt_is_not_promoted_to_supervisor_success():
    s,body,t,events=rig()
    def partial(_supervisor):
        return {
            'error': 'LiveWriterError: r004 path has 21 of 550 bins',
            'evidence_eligible': False,
        }
    result=s.run(partial)
    assert not result['success']
    assert '21 of 550 bins' in result['error']
    assert events.count('stop') == 1


def test_safety_fault_is_recorded_and_stop_observation_keeps_actual_safety():
    s,body,t,events=rig(safety_fault_at=.3)
    result=s.run(body)
    assert not result['success'] and 'safety=3' in result['error']
    assert 'body' not in events
    assert result['dashboard_stop']['sample']['safety_mode']==3


def test_observer_start_failure_never_plays():
    s,body,t,events=rig()
    def fail():raise RuntimeError('observer failed')
    s.observer.start=fail
    result=s.run(body)
    assert not result['success'] and 'play' not in events


def test_old_runtime_identity_waits_for_new_program_then_times_out():
    s,body,t,events=rig()
    latest=s.observer.latest
    def old(**kwargs):
        row=latest(**kwargs);row['output_int_register_33']=18;return row
    s.observer.latest=old
    result=s.run(body)
    assert not result['success'] and 'body' not in events
    assert 'PLAYING was not observed' in result['error']
    assert 'stop' in events


def test_observer_abort_routes_to_writer_but_terminal_poll_remains_available():
    state=NS(stopping=False,now=1.)
    polls=[]
    def fail():raise RuntimeError('observer lost')
    transport=ObservedTransport(NS(poll_output=lambda **kw:polls.append('read')),fail,
        stopping=lambda:state.stopping,clock=lambda:state.now)
    with pytest.raises(RuntimeError,match='observer lost'):transport.poll_output()
    assert not polls
    state.stopping=True
    transport.poll_output()
    assert polls==['read']


def test_observer_loss_does_not_prevent_dashboard_stop_dispatch():
    s,body,t,events=rig()
    def body(s):
        def lost(**kwargs):raise RuntimeError('observer lost')
        s.observer.latest=lost
        raise RuntimeError('first body failure')
    result=s.run(body)
    assert events.count('stop')==1
    assert result['error']=='RuntimeError: first body failure'
    assert 'observer lost' in result['stop_error']


def test_complete_path_cleanup_failure_requires_verified_home_recovery(tmp_path):
    metrics = {
        'complete': True,
        'coverage_complete': True,
        'objective_eligible': True,
        'interrupted': False,
        'complete_bins': 550,
        'required_bins': 550,
        'path_duration_s': 60.0,
        'formal_metric_duration_s': 55.0,
        'timing_gate_passed': True,
        'timing_evidence': {'successful': True},
    }
    dispatch = {
        'command': 'pilot',
        'evidence_eligible': True,
        'live_path': {
            'kind': 'r013_compat_60',
            'protocol_id': 'figure8_window60_r013_compat_v1',
        },
        'evidence_metrics': metrics,
        'attempts': [{
            'evidence': {
                'complete_bins': 550,
                'return_gate_passed': True,
                'safety_gate_passed': True,
                'home_proof': {'stationary': True, 'fixed_home_route': True},
            },
        }],
    }
    (tmp_path / 'dispatch_receipt.json').write_text(json.dumps(dispatch))
    result = {
        'success': False,
        'error': 'YieldLiveError: attempt cleanup or physical stop confirmation failed',
        'autonomous_home_recovery': {'success': True, 'state': 'HOME_RECOVERED'},
    }
    assert _complete_path_home_recovered(tmp_path, result)

    result['autonomous_home_recovery'] = {'success': False, 'state': 'BLOCKED'}
    assert not _complete_path_home_recovered(tmp_path, result)


def test_preload_gate_failure_never_loads_or_plays():
    s,body,t,events=rig()
    def invalid(s):raise RuntimeError('Home differs')
    result=s.run(body,before_load=invalid)
    assert not result['success']
    assert 'play' not in events and not any(e.startswith('load ') for e in events)


def test_evidence_only_video_failure_does_not_become_motion_gate(tmp_path, monkeypatch):
    class DeadProcess:
        def poll(self):
            return 1

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(
        "contact_yield_supervisor.subprocess.Popen",
        lambda *args, **kwargs: DeadProcess(),
    )
    recorder = VideoRecorder("rtsp://127.0.0.1:8554/arm", tmp_path, policy="evidence-only")
    recorder.start()
    recorder.check()
    recorder.close()
    evidence = recorder.evidence()
    assert evidence["policy"] == "evidence-only"
    assert evidence["available"] is False
    assert evidence["required_for_motion_admission"] is False
