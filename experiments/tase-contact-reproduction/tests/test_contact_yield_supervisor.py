"""Fault-injected lifecycle checks. No network endpoints or robot execution."""
from types import SimpleNamespace as NS
import json
from pathlib import Path
import pytest
from contact_yield_supervisor import (
    ResidentSupervisor,
    ProcessObserver,
    READABLE_RUNTIME_IDENTITY,
    VideoRecorder,
    _verified_stopped_joint_home,
    validate_resident_candidate_directory,
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


def test_fresh_stopped_joint_home_proof_suppresses_second_recovery_motion():
    supervisor, _body, _time, _events = rig()
    supervisor.home_q = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6)
    supervisor.home_pose = (0.4, 0.2, 0.1, 0.0, 0.0, 0.0)
    sample = supervisor.observer.latest()
    sample.update(
        actual_q=list(supervisor.home_q),
        actual_TCP_pose=list(supervisor.home_pose),
        safety_mode=1,
        robot_mode=7,
        runtime_state=1,
        observed_at_s=100.5,
    )
    result = {
        'success': False,
        'program_stopped': True,
        'dashboard_stop': {
            'dashboard': {
                'is in remote control': 'true',
                'safetymode': 'Safetymode: NORMAL',
                'running': 'Program running: false',
                'programState': 'STOPPED test.urp',
            },
            'sample': sample,
        },
    }

    assert _verified_stopped_joint_home(result, supervisor) is sample


@pytest.mark.parametrize('change', [
    lambda result, sample: result.update(program_stopped=False),
    lambda result, sample: sample.update(safety_mode=3),
    lambda result, sample: sample.update(actual_q=[0.2] * 6),
    lambda result, sample: sample.update(actual_TCP_pose=[0.4, 0.2, 0.1, 0.0, 0.0, 0.006]),
    lambda result, sample: sample.update(actual_qd=[0.02] * 6),
])
def test_stopped_home_shortcut_rejects_unverified_state(change):
    supervisor, _body, _time, _events = rig()
    supervisor.home_q = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6)
    supervisor.home_pose = (0.4, 0.2, 0.1, 0.0, 0.0, 0.0)
    sample = supervisor.observer.latest()
    sample.update(actual_q=list(supervisor.home_q), actual_TCP_pose=list(supervisor.home_pose),
                  safety_mode=1, robot_mode=7, runtime_state=1, observed_at_s=100.5)
    result = {
        'success': False,
        'program_stopped': True,
        'dashboard_stop': {
            'dashboard': {
                'is in remote control': 'true',
                'safetymode': 'Safetymode: NORMAL',
                'running': 'Program running: false',
                'programState': 'STOPPED test.urp',
            },
            'sample': sample,
        },
    }
    change(result, sample)

    assert _verified_stopped_joint_home(result, supervisor) is None


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


def test_load_only_restoration_loads_but_never_plays_or_moves():
    s, _body, _t, events = rig()

    def restore(_supervisor):
        return {
            'success': True,
            'evidence_eligible': True,
            'program_loaded': True,
            'program_started': False,
            'motion_dispatched': False,
        }

    result = s.run(restore, execute_program=False, load_only=True)
    assert result['success'] is True
    assert result['program_stopped'] is True
    assert 'load /programs/test.urp' in events
    assert 'play' not in events and 'stop' not in events


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


def test_research_candidate_score_failure_does_not_fail_closed_session():
    s, _body, _t, events = rig()
    def research(_supervisor):
        return {
            'research_campaign': True,
            'campaign_execution_complete': True,
            'success': True,
            'evidence_eligible': False,
            'candidate_failures': 1,
        }
    result = s.run(research)
    assert result['success'] is True
    assert result['body']['candidate_failures'] == 1
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


def test_process_observer_refreshes_stale_cached_row_from_live_queue():
    import queue
    import threading
    import time

    observer = object.__new__(ProcessObserver)
    observer.samples = queue.Queue(maxsize=4)
    observer.errors = queue.Queue(maxsize=1)
    observer.process = NS(is_alive=lambda: True)
    observer.row = {'received_monotonic_s': time.monotonic() - .081, 'sequence': 1}
    observer.sample_count = 1
    observer.error = None

    def publish_fresh():
        observer.samples.put({
            'received_monotonic_s': time.monotonic(),
            'sequence': 2,
        })

    timer = threading.Timer(.004, publish_fresh)
    timer.start()
    try:
        row = observer.latest()
    finally:
        timer.join()

    assert row['sequence'] == 2
    assert 0 <= time.monotonic() - row['received_monotonic_s'] < .080


def test_process_observer_keeps_stale_gate_when_no_fresh_row_arrives():
    import queue
    import time

    observer = object.__new__(ProcessObserver)
    observer.samples = queue.Queue(maxsize=4)
    observer.errors = queue.Queue(maxsize=1)
    observer.process = NS(is_alive=lambda: True)
    observer.row = {'received_monotonic_s': time.monotonic() - .081, 'sequence': 1}
    observer.sample_count = 1
    observer.error = None

    with pytest.raises(RuntimeError, match=r'observer latest sample is stale: age_s=.*sample_count=1'):
        observer.latest()


def test_resident_candidate_directory_prevalidates_only_the_initial_file(tmp_path):
    root = Path(__file__).resolve().parents[1]
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()
    first_payload = json.loads(
        (root / "config/tase_figure8_integral_0p1_rate400.json").read_text()
    )
    first_payload["candidate_id"] = "tuner-1"
    first_payload["index"] = 0
    first_path = candidate_dir / "candidate-0001.json"
    first_path.write_text(json.dumps(first_payload), encoding="utf-8")

    single_directory, single_selected, single_binding = validate_resident_candidate_directory(
        candidate_dir, duration="r013_60_rate400", attempts=1
    )
    assert single_directory == candidate_dir.resolve()
    assert single_selected == first_path.resolve()
    assert single_binding["candidate_id"] == "tuner-1"

    directory, selected, binding = validate_resident_candidate_directory(
        candidate_dir, duration="r013_60_rate400", attempts=3
    )
    assert directory == candidate_dir.resolve()
    assert selected == first_path.resolve()
    assert binding["candidate_id"] == "tuner-1"

    (candidate_dir / "candidate-0002.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="only after the prior seal"):
        validate_resident_candidate_directory(
            candidate_dir, duration="r013_60_rate400", attempts=3
        )


def test_supervisor_rejects_static_and_adaptive_manifest_together(tmp_path):
    from contact_yield_supervisor import main

    with pytest.raises(SystemExit) as exc:
        main([
            "--action", "pilot",
            "--method", "TASE_RNN_MATURE",
            "--duration", "r013_60_rate400",
            "--run-dir", str(tmp_path / "run"),
            "--readback-dir", str(tmp_path / "readback"),
            "--control-cpu", "2",
            "--resident-attempts", "2",
            "--resident-parameter-manifest", str(tmp_path / "manifest.json"),
            "--resident-candidate-dir", str(tmp_path / "candidates"),
        ])
    assert exc.value.code == 2


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


def test_protocol_ack_never_substitutes_for_dashboard_stop():
    s, _, t, events = rig()
    result = s.run(lambda _: {'stop': {'stopped': True, 'tp_ack': True}})
    assert result['success']
    assert events.count('stop') == 1
    assert result['program_stopped'] is True
    assert result['dashboard_stop']['sample']['runtime_state'] == 1
    assert events.index('stop') < events.index('observer.close')


def test_protocol_ack_with_still_playing_dashboard_fails():
    s, _, t, events = rig(stop_delay=10.)
    result = s.run(lambda _: {'stop': {'stopped': True, 'tp_ack': True}})
    assert not result['success']
    assert not result['program_stopped']
    assert 'STOPPED was not observed' in result['stop_error']
    assert events.count('stop') == 1


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
