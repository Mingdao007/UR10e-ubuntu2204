"""Resident failure regressions. Every endpoint here is synthetic/offline."""
import json
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
            'lifecycle': {'path_complete': False, 'home_verified': False}}
    session.seal_attempt(item, service=False)
    assert item['lifecycle']['sealed']
    assert item['lifecycle']['path_complete'] is False
    assert Path(item['sealed_evidence']['segments']['raw_sensor']['path']).is_file()


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
