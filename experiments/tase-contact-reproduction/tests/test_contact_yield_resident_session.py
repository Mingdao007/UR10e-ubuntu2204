"""Resident failure regressions. Every endpoint here is synthetic/offline."""
import json
from dataclasses import asdict
import multiprocessing as mp
import socket
import struct
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from contact_yield_resident_session import ResidentSession, ResidentSessionError
from step5d_autotune_v4_live_writer import WritableRTDEClient


def _peer(sock, done, results):
    sock.settimeout(.05)
    sent = 0
    error = None
    origin = time.monotonic()
    try:
        while not done.is_set():
            payload = b'\x01' + struct.pack('!73d', time.monotonic(), *([0.] * 72))
            sock.sendall(struct.pack('!HB', len(payload) + 3, ord('U')) + payload)
            sent += 1
            done.wait(max(0., origin + sent * .002 - time.monotonic()))
    except OSError as exc:
        error = type(exc).__name__
    finally:
        sock.close()
        results.put((sent, error))


def test_cpu_json_finalizer_keeps_independent_rtde_peer_serviced(tmp_path):
    ctx = mp.get_context('fork')
    peer, local = socket.socketpair()
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
    local.settimeout(.2)
    done, results = ctx.Event(), ctx.Queue()
    publisher = ctx.Process(target=_peer, args=(peer, done, results))
    publisher.start()
    peer.close()
    client = WritableRTDEClient('offline-no-device')
    client.sock = local
    owner = NS(stop=lambda reason: None)
    session = ResidentSession(mature=NS(writer=owner), runtime=None,
                              provider=None, prerequisites=None, run_dir=tmp_path)
    ticks = []
    def service():
        client.recv_latest_sample(1, ['DOUBLE'] * 73,
                                  ['timestamp'] + [f'v{i}' for i in range(72)])
        ticks.append(time.monotonic())
        time.sleep(.002)
    session._service_tick = service
    # Large Python/C JSON work, not a sleeping worker that releases the GIL.
    data = [{'index': i, 'wrench': [float(i)] * 40} for i in range(20000)]
    def cpu_work():
        until = time.monotonic() + .4
        length = 0
        while time.monotonic() < until:
            length = len(json.dumps(data, sort_keys=True))
        return length
    start = time.monotonic()
    try:
        assert session._run_terminal_finalize(cpu_work) > 1000000
        for _ in range(5):
            service()
    finally:
        done.set()
        publisher.join(timeout=2.)
        if publisher.is_alive():
            publisher.kill()
            publisher.join()
        local.close()
    sent, error = results.get(timeout=1.)
    assert error is None
    assert sent > 100 and len(ticks) > 100
    assert max(b - a for a, b in zip([start] + ticks, ticks)) < .080


def test_observer_failure_stops_before_evidence_cleanup(tmp_path):
    events = []
    owner = NS(stop=lambda reason: events.append(('stop', reason)))
    session = ResidentSession(mature=NS(writer=owner), runtime=None,
                              provider=None, prerequisites=None, run_dir=tmp_path)
    def fail():
        events.append(('observe', 'failed'))
        raise RuntimeError('observer exited first')
    session._service_tick = fail
    with pytest.raises(RuntimeError, match='observer exited first'):
        session._run_terminal_finalize(lambda: time.sleep(5.))
    assert events[:2] == [('observe', 'failed'), ('stop', 'resident_evidence_fault')]
    assert session.lifecycle_events[-1]['exitcode'] is not None


def test_service_rotation_retains_all_rows_over_old_4096_limit(tmp_path):
    rows = list(range(5000))
    owner = NS(_service_observations={'robot_frames': rows})
    session = ResidentSession(mature=NS(writer=owner), runtime=None,
                              provider=None, prerequisites=None, run_dir=tmp_path)
    frozen, _ = session._rotate_service_observations()
    assert frozen['robot_frames'] == rows
    assert owner._service_observations == {'robot_frames': []}


def test_stopped_session_seal_never_reopens_or_services_transport(tmp_path):
    owner = NS(raw_observations=[{'wrench': [0.] * 6}],
               _service_observations={'robot_frames': [{'state': 90}]})
    session = ResidentSession(mature=NS(writer=owner), runtime=None,
                              provider=NS(command_timeline=[]), prerequisites=None, run_dir=tmp_path)
    session.closed = True
    session._service_tick = lambda: pytest.fail('stopped transport was serviced')
    item = {'sequence': 1, 'partial': True,
            'timing': {'started_monotonic_s': time.monotonic() - 1.0},
            'lifecycle': {'path_complete': False, 'home_verified': False}}
    session.seal_attempt(item, service=False)
    assert item['lifecycle']['sealed']
    assert item['lifecycle']['path_complete'] is False
    assert Path(item['sealed_evidence']['segments']['raw_sensor']['path']).is_file()
    persisted = json.loads((tmp_path / 'attempts/0001/attempt-result.json').read_text())
    assert persisted['lifecycle']['sealed'] is True
    assert persisted['timing']['through_seal_s'] >= 1.0
    assert persisted['timing']['sealed_monotonic_s'] >= persisted['timing']['started_monotonic_s']


def _replay_capture_row(capture, *, sequence, reference_time_s=0.0):
    zero = (0.0,) * 6
    capture.begin_command()
    capture.stage_transition(
        1, 0, NS(lambda_state=[1.0] * 6, theta_dot_state=[2.0] * 6),
    )
    capture.stage_sample(
        host_monotonic_s=123.4, actual_dt_s=.002,
        desired_twist=(.1, .2, .3, .4, .5, .6),
        jacobian=[[float(row == col) for col in range(6)] for row in range(6)],
        solver_lower=(-.15,) * 6, solver_upper=(.15,) * 6,
        previous_qdot=zero, host_slew_scale=.5,
        host_slew_delta_limit=.03, packet_qdot=zero,
        solver_elapsed_s=.0001,
    )
    capture.set_provider_elapsed(.0002)
    capture.commit_published(
        packet_sequence=sequence, published_at_s=123.5,
        reference_phase='path', reference_time_s=reference_time_s,
        packet_qdot=(.01, .02, .03, .04, .05, .06),
    )


def test_resident_seal_serializes_and_reads_back_exact_tase_replay_join(tmp_path):
    from tase_contact_provider import _TaseReplayEvidenceBuffer

    capture = _TaseReplayEvidenceBuffer(capacity=4, path_duration_s=60.0)
    capture.reset_attempt(1)
    _replay_capture_row(capture, sequence=17, reference_time_s=5.0)
    writer = NS(
        raw_observations=[], robot_observations=[],
        admission_robot_observations=[], rejected_robot_observations=[],
        command_observations=[(123.5, {'sequence': 17, 'double_values': [0.0] * 13 +
                                       [.01, .02, .03, .04, .05, .06] + [0.0] * 5})],
        _service_observations={}, _host_formal_path_publish_count=1,
    )
    provider = NS(replay_evidence=capture, command_timeline=[])
    session = ResidentSession(
        mature=NS(writer=writer), runtime=None, provider=provider,
        prerequisites=None, run_dir=tmp_path,
    )
    session.closed = True
    item = {'sequence': 1, 'lifecycle': {'path_complete': True}}

    sealed = session.seal_attempt(item, service=False)
    attempt = tmp_path / 'attempts' / '0001'
    trace_rows = [json.loads(line) for line in
                  (attempt / 'command_timeline.jsonl').read_text().splitlines()]
    published_rows = [json.loads(line) for line in
                      (attempt / 'published_packets.jsonl').read_text().splitlines()]
    seal = json.loads((attempt / 'seal.json').read_text())
    result = json.loads((attempt / 'attempt-result.json').read_text())

    assert len(trace_rows) == 1
    trace = trace_rows[0]
    assert trace['packet_sequence'] == published_rows[0][1]['sequence'] == 17
    assert trace['reference_time_s'] == 5.0
    assert trace['host_monotonic_s'] == 123.4
    assert trace['jacobian_6x6'][0] == [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert trace['published_packet_qdot_rad_s'] == [.01, .02, .03, .04, .05, .06]
    assert trace['transition_events'][0]['event_type'] == 'path_origin'
    assert trace['transition_events'][0]['lambda_state'] == [1.0] * 6
    assert seal['replay_evidence']['complete'] is True
    assert seal['replay_evidence']['packet_sequence_join_passed'] is True
    assert result['lifecycle']['replay_evidence_complete'] is True
    assert sealed['sealed_evidence']['segments']['command_timeline']['count'] == 1


def test_resident_seal_marks_replay_capture_failed_on_sequence_join_miss(tmp_path):
    from tase_contact_provider import _TaseReplayEvidenceBuffer

    capture = _TaseReplayEvidenceBuffer(capacity=2, path_duration_s=60.0)
    capture.reset_attempt(1)
    _replay_capture_row(capture, sequence=18, reference_time_s=1.0)
    writer = NS(
        raw_observations=[], robot_observations=[],
        admission_robot_observations=[], rejected_robot_observations=[],
        command_observations=[(123.5, {'sequence': 17, 'double_values': [0.0] * 24})],
        _service_observations={}, _host_formal_path_publish_count=1,
    )
    provider = NS(replay_evidence=capture, command_timeline=[])
    session = ResidentSession(
        mature=NS(writer=writer), runtime=None, provider=provider,
        prerequisites=None, run_dir=tmp_path,
    )
    session.closed = True
    item = {'sequence': 1, 'lifecycle': {'path_complete': True}}

    sealed = session.seal_attempt(item, service=False)
    assert sealed['sealed_evidence']['replay_evidence']['complete'] is False
    assert sealed['lifecycle']['replay_evidence_complete'] is False
    assert 'published_packet_sequence_join_failed' in (
        sealed['sealed_evidence']['replay_evidence']['failure_reasons']
    )


def test_rate400_refresh_uses_original_300s_expiry_or_event_triggered(tmp_path):
    from figure8_resident_acceptance import _write_receipts
    from contact_yield_live_contract import load_identity_contract
    from contact_yield_live_writer import load_run_dir_receipts
    contract = load_identity_contract()
    _write_receipts(tmp_path, contract, 100.)
    prerequisites, _ = load_run_dir_receipts(
        tmp_path, contract=contract, route_id='r006-yield-live', attempt_id='old',
        now_s=100., admission_max_age_s=300.,
    )
    now = [399.]
    session = ResidentSession(mature=NS(writer=NS()), runtime=None, provider=None,
                              prerequisites=prerequisites, run_dir=tmp_path,
                              wall_clock=lambda: now[0])
    assert session._refresh_needed() is False
    for reason in ('home_reacquired', 'transport_reconnected', 'identity_changed',
                   'eoat_changed', 'fault_recovered'):
        session.require_refresh(reason)
        assert session._refresh_needed() is True
        session._refresh_reasons.clear()
    now[0] = 400.
    assert session._refresh_needed() is True
    with pytest.raises(ResidentSessionError, match='unknown'):
        session.require_refresh('arbitrary')


def test_reacquired_home_triggers_refresh_after_measured_settle(tmp_path, monkeypatch):
    import contact_yield_resident_session as resident
    now = [0.]
    writer = NS(_service_mode=False)
    session = ResidentSession(mature=NS(writer=writer), runtime=None, provider=None,
                              prerequisites=NS(admission_max_age_s=300.),
                              run_dir=tmp_path, mono_clock=lambda: now[0])
    session._home_settle_dirty = True
    def tick(*, settling=False):
        assert settling
        now[0] += .1
        return NS(timestamp=now[0]), None
    session._service_tick = tick
    monkeypatch.setattr(resident, '_home_proof', lambda *args, **kwargs: {'home_verified': True})
    session._wait_home_settle(NS(received_monotonic_s=0.))
    assert session._physical_home_arrival_mono_s == 0.
    assert session._stationary_home_verified_mono_s >= .5
    assert session._refresh_reasons == {'home_reacquired'}


def test_research_candidate_continues_after_only_rate_floor_failure():
    from contact_yield_live import resident_candidate_can_continue
    row = {
        'evidence_eligible': False,
        'evidence': {
            'safety_gate_passed': True,
            'contact_gate_passed': True,
            'return_gate_passed': True,
            'metrics': {
                'timing_gate_passed': False,
                'complete_bins': 550,
                'motion_gate_passed': True,
                'protocol_id': 'figure8_window60_r013_rate400_v1',
                'timing_evidence': {
                    'acceptance_protocol_id': 'figure8_window60_r013_rate400_v1',
                    'duration_s': 60.0,
                    'minimum_rate_hz': 400.0,
                    'layer_rates_hz': {
                        'writer_publishes': 369.4,
                        'rtde_frames': 369.4,
                        'kunwei_frames': 999.95,
                        'tp_consumed_packet_echoes': 369.4,
                    },
                    'max_fresh_gap_s': .0084,
                    'max_fresh_gap_limit_s': .02,
                    'runtime_stale_stop_s': .08,
                    'feedback_age_p99_s': .009,
                    'feedback_age_p99_max_s': .01,
                },
            },
        },
        'lifecycle': {'path_complete': True, 'home_verified': True,
                      'ready_for_next': True, 'sealed': True},
    }
    assert resident_candidate_can_continue(row, research_campaign=True)
    assert not resident_candidate_can_continue(row, research_campaign=False)

    unsafe_mutations = (
        lambda d: d['evidence'].__setitem__('safety_gate_passed', False),
        lambda d: d['evidence'].__setitem__('contact_gate_passed', False),
        lambda d: d['evidence'].__setitem__('return_gate_passed', False),
        lambda d: d['evidence']['metrics'].__setitem__('motion_gate_passed', False),
        lambda d: d['evidence']['metrics'].__setitem__('complete_bins', 549),
        lambda d: d['evidence']['metrics']['timing_evidence'].__setitem__(
            'max_fresh_gap_s', .02),
        lambda d: d['evidence']['metrics']['timing_evidence'].__setitem__(
            'feedback_age_p99_s', .010001),
        lambda d: d['evidence']['metrics']['timing_evidence']['layer_rates_hz'].update({
            'writer_publishes': 400.0,
            'rtde_frames': 400.0,
            'kunwei_frames': 400.0,
            'tp_consumed_packet_echoes': 400.0,
        }),
        lambda d: d['lifecycle'].__setitem__('path_complete', False),
        lambda d: d['lifecycle'].__setitem__('home_verified', False),
        lambda d: d['lifecycle'].__setitem__('ready_for_next', False),
        lambda d: d['lifecycle'].__setitem__('sealed', False),
    )
    for mutate in unsafe_mutations:
        changed = json.loads(json.dumps(row))
        mutate(changed)
        assert not resident_candidate_can_continue(changed, research_campaign=True)

    # An unrelated evidence failure cannot be treated as the approved rate-only
    # budget-consuming case.
    changed = json.loads(json.dumps(row))
    changed['evidence']['metrics']['timing_gate_passed'] = True
    assert not resident_candidate_can_continue(changed, research_campaign=True)


def test_real_refresh_callback_captures_ten_seconds_and_fetches_files(tmp_path, monkeypatch):
    from figure8_resident_acceptance import OfflineClock, OfflineResidentRTDE, _write_receipts
    from contact_yield_live_contract import load_identity_contract
    from contact_yield_live_writer import load_run_dir_receipts
    from contact_yield_resident_session import refresh_live_preparation
    import contact_recovery_readback

    contract = load_identity_contract()
    _write_receipts(tmp_path, contract, 100.)
    old, _ = load_run_dir_receipts(tmp_path, contract=contract,
                                   route_id='r006-yield-live', attempt_id='old', now_s=100.)
    clock = OfflineClock(wall_s=401.)
    rtde = OfflineResidentRTDE(contract, home_pose=contract.home_pose,
                              home_q=contract.home_q, clock=clock)
    rtde.open()
    fetched = []
    def fetch(path, *, basenames):
        fetched.append((str(path), tuple(basenames), clock.wall()))
        path.mkdir(parents=True)
        (path / 'readback-results.json').write_text(json.dumps(
            {'pass': True, 'observed_at_s': clock.wall(), 'scope': 'synthetic fetch boundary'}))
        return path
    monkeypatch.setattr(contact_recovery_readback, 'fetch_recovery_readback', fetch)
    writer = NS(_last_output=None, _service_mode=False)
    def service():
        clock.sleep(.002)
        out = rtde.poll_output()
        writer._last_output = out
        return out, NS(raw_wrench=(0., 0., 0., 0., 0., 0.), observed_at_s=clock.mono())
    session = NS(prerequisites=old, run_dir=tmp_path, refreshes=[], writer=writer,
                 mono_clock=clock.mono, wall_clock=clock.wall,
                 _service_tick=service,
                 _run_process_work=lambda *, reason, task: task(),
                 verify_ready_for_next=lambda **kw: None)
    result = refresh_live_preparation(session=session, home=None, now_s=clock.wall())
    new = result['prerequisites']
    assert len(fetched) == 1
    assert new.controller.observed_at_s == fetched[0][2]
    assert new.baseline_observed_at_s >= 410.9
    assert old.controller.observed_at_s == 100.
    assert new.session_epoch == old.session_epoch
    assert new.resident_session_id == old.resident_session_id
    assert result['baseline']['acquisition_duration_s'] >= 10.
    assert result['baseline']['initial_exclusion_s'] == .5
    assert result['baseline']['sample_count'] >= 4700
    capture = json.loads((tmp_path / 'session-refresh/0001/baseline-frames.json').read_text())
    assert capture[0]['poll_monotonic_s'] >= .5
    assert capture[-1]['poll_monotonic_s'] >= 10.
    assert all(row['robot']['integer_echoes']['26'] == 78 for row in capture)
    rtde.close()


def test_resident_seal_keeps_exact_rtde_json_bytes_without_deep_copy():
    from contact_yield_resident_session import _json_default
    from contact_yield_live_contract import load_identity_contract
    from figure8_resident_acceptance import OfflineClock, OfflineResidentRTDE
    contract = load_identity_contract()
    clock = OfflineClock()
    rtde = OfflineResidentRTDE(contract, home_pose=contract.home_pose,
                              home_q=contract.home_q, clock=clock)
    rtde.open()
    try:
        frame = rtde.poll_output()
        actual = json.dumps(frame, sort_keys=True, allow_nan=False,
                            default=_json_default)
        original = json.dumps(asdict(frame), sort_keys=True, allow_nan=False)
        assert actual == original
    finally:
        rtde.close()


def test_failed_seal_retains_rotated_service_until_recovery(tmp_path):
    source = [{'sequence': 7}, {'sequence': 8}]
    owner = NS(_service_observations={'published_packets': source})
    session = ResidentSession(mature=NS(writer=owner), runtime=None,
                              provider=NS(command_timeline=[]), prerequisites=None, run_dir=tmp_path)
    def failed_worker(**kwargs):
        raise OSError('injected disk/worker failure')
    session._run_process_work = failed_worker
    with pytest.raises(OSError, match='injected'):
        session.seal_attempt({'sequence': 1, 'lifecycle': {}})
    session.closed = True
    receipt = session.seal_service_tail()
    rows = [json.loads(line) for line in Path(receipt['service_segment']['path']).read_text().splitlines()]
    assert [row['row'] for row in rows] == source
    assert not session._pending_service_batches


def test_recovery_handoff_does_not_encode_large_trace():
    from contact_yield_live import recovery_handoff_receipt
    class MustNotEncode:
        pass
    full = {'attempt_id': 'failed-later-arm', 'armed': True,
            'stop': {'stopped': False, 'protective_stop': False},
            'error': 'first transport failure', 'evidence_seal_deferred': True,
            'session': {'transport_trace': MustNotEncode()},
            'attempts': MustNotEncode(), 'command_timeline': MustNotEncode()}
    handoff = recovery_handoff_receipt(full)
    assert len(json.dumps(handoff)) < 1000
    assert handoff['error'] == 'first transport failure'
    assert full['session']['transport_trace'].__class__ is MustNotEncode


@pytest.mark.parametrize('state,expected', [(40, 0), (78, 1)])
def test_return_neutralizes_arm_without_erasing_next_arm(state, expected, monkeypatch):
    from contact_yield_live_writer import NativeYieldLiveWriter
    from step5d_autotune_v4_r004_live_writer import LiveR004Writer
    from step5d_autotune_v4_r004.wire import CommandMode, SessionCommand
    captured = []
    def send(self, sensor, **kwargs):
        captured.append(int(self._session_command))
        return NS(sequence=1)
    monkeypatch.setattr(NativeYieldLiveWriter.__mro__[1], '_send_packet', send)
    writer = NativeYieldLiveWriter.__new__(NativeYieldLiveWriter)
    writer._last_output = NS(integer_echoes={26: state})
    writer._stopped = False
    writer._session_command = SessionCommand.ARM
    writer._mono_clock = lambda: 1.
    writer._record = lambda *args: None
    writer.command_observations = []
    writer._r013_path_early_end_controller = None
    writer._send_packet(object(), command_mode=CommandMode.HOLD)
    assert captured == [expected]


def test_real_path_collector_retired_while_new_service_frames_survive(tmp_path):
    from step5d_autotune_v4_r004.evidence import PathEvidenceCollector
    collector = PathEvidenceCollector()
    collector._path_samples = [object() for _ in range(3073)]
    collector._seen_path_identities = set(range(3073))
    collector._timing._layer_keys['rtde_frames'] = set(range(3073))
    owner = NS(_service_mode=False, raw_observations=list(range(3073)),
               _service_observations={'robot_frames': []})
    session = ResidentSession(mature=NS(writer=owner), runtime=None,
                              provider=None, prerequisites=None, run_dir=tmp_path)
    def tick():
        assert owner._service_mode
        owner._service_observations['robot_frames'].append('new')
    session._service_tick = tick
    session._run_process_work = lambda **kw: kw['task']()
    def finalize():
        return len(collector._path_samples)
    assert session._run_terminal_finalize(finalize) == 3073
    assert not collector._path_samples
    assert not collector._seen_path_identities
    assert not collector._timing._layer_keys['rtde_frames']
    session._clear_serviced(owner.raw_observations)
    assert not owner.raw_observations
    assert owner._service_observations['robot_frames']
    assert not owner._service_mode


def test_home_settle_restarts_dwell_after_actual_joint_motion(tmp_path, monkeypatch):
    import contact_yield_resident_session as module
    owner = NS(_service_mode=False)
    session = ResidentSession(mature=NS(writer=owner), runtime=None,
                              provider=None, prerequisites=None, run_dir=tmp_path)
    clock = [0.]
    session.mono_clock = lambda: clock[0]
    def service(*, settling):
        assert settling and owner._service_mode
        clock[0] += .01
        return NS(timestamp=clock[0], stationary=not .29 < clock[0] < .32), None
    session._service_tick = service
    session._home_settle_dirty = True
    monkeypatch.setattr(module, '_home_proof',
                        lambda w, o, fresh: {'home_verified': o.stationary})
    result = session._wait_home_settle(None)
    assert .81 <= result.timestamp <= .84
    assert not owner._service_mode
    assert not session._home_settle_dirty
    assert session.lifecycle_events[-1]['stationary_duration_s'] >= .5


def test_service_keeps_ready_home_transient_under_existing_return_guard(tmp_path):
    from step5d_autotune_v4_r004.wire import SessionCommand
    from step5d_autotune_v4_r004.wire import CommandMode
    output = NS(
        integer_echoes={26: 78}, stationary=False,
        qd_rad_s=(0., 0., 0., 0., -.0025, 0.),
        tcp_speed_m_s_rad_s=(.00035, -.00038, 0., -.0019, -.0017, 0.),
    )
    sent = []
    owner = NS(
        _last_poll_was_fresh=True,
        _poll_checked=lambda **kwargs: output,
        _read_sensor=lambda: object(),
        _send_packet=lambda sensor, **kwargs: sent.append(kwargs['command_mode']),
        _session_command=SessionCommand.ARM,
        fresh_frame_wait_policy=NS(wait_s=0.),
    )
    session = ResidentSession(mature=NS(writer=owner), runtime=None,
                              provider=None, prerequisites=None, run_dir=tmp_path)
    session.sleep = lambda _: None
    pair = session._service_tick()
    assert pair[0] is output
    assert sent == [CommandMode.HOLD]
    assert owner._session_command is SessionCommand.HOLD


@pytest.mark.parametrize('first_stationary,dirty', [(False, False), (True, True)])
def test_next_arm_waits_for_verified_home_after_ready_transient(
    tmp_path, monkeypatch, first_stationary, dirty,
):
    import contact_yield_resident_session as module
    moving = NS(stationary=False, integer_echoes={26: 78, 29: 2},
                timestamp=10., consumed_packet_sequence=100, runtime_state=2)
    stopped = NS(stationary=True, integer_echoes={26: 78, 29: 2},
                 timestamp=10.5, consumed_packet_sequence=101, runtime_state=2)
    checked = []
    owner = NS(
        _poll_checked=lambda **kwargs: stopped if first_stationary else moving,
        _last_poll_was_fresh=True,
        fresh_frame_wait_policy=NS(wait_s=0.),
        _assert_prearm_home_boundary=lambda output: checked.append(output),
    )
    session = ResidentSession(mature=NS(writer=owner), runtime=None,
                              provider=None, prerequisites=None, run_dir=tmp_path)
    session.prepared = True
    session._home_settle_dirty = dirty
    session.mono_clock = lambda: 1.
    session.wall_clock = lambda: 2.
    waits = []
    session._wait_home_settle = lambda output: (waits.append(output) or stopped)
    monkeypatch.setattr(module, '_home_proof',
                        lambda *args, **kwargs: {'home_verified': True})
    result = session.verify_ready_for_next(reason='test')
    assert result['home_verified'] is True
    assert waits == [stopped if first_stationary else moving]
    assert checked == [stopped]


def test_home_settle_timeout_never_manufactures_home(tmp_path, monkeypatch):
    import contact_yield_resident_session as module
    owner = NS(_service_mode=False)
    session = ResidentSession(mature=NS(writer=owner), runtime=None,
                              provider=None, prerequisites=None, run_dir=tmp_path)
    clock = [0.]
    session.mono_clock = lambda: clock[0]
    def service(*, settling):
        clock[0] += .1
        return NS(timestamp=clock[0]), None
    session._service_tick = service
    monkeypatch.setattr(module, '_home_proof', lambda *a, **k: {'home_verified': False})
    with pytest.raises(ResidentSessionError, match='stationary dwell timed out'):
        session._wait_home_settle(None)
    assert not owner._service_mode
