"""Native host lifecycle: RTDE servicing, distinct gates, bounded stop-observe."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from contact_yield_live_writer import NativeYieldLiveWriter, stop_and_confirm
from step5d_autotune_v4_r004.fake_rtde import FakeLiveKunweiTransport, FakeLiveRTDETransport
from step5d_autotune_v4_r004.transport import R004OutputSnapshot
from step5d_autotune_v4_r004.wire import SessionCommand
from step5d_autotune_v4_r004_live_writer import LIVE_ACK, LiveWriterError
from step5d_autotune_v4_r006.live_adapter import R006LiveWriter
from test_step5d_autotune_v4_r004_live_boundary import _prerequisites


class Clock:
    def __init__(self, value: float = 0.01) -> None:
        self.value = value

    def now(self) -> float:
        return self.value

    def wall(self) -> float:
        return 100.0

    def sleep(self, duration: float) -> None:
        self.value += duration


class ScriptedRTDE(FakeLiveRTDETransport):
    """Injected RTDE endpoint. Never opens a socket or Dashboard channel."""

    def __init__(self, contract, *, clock: Clock, events: list[str] | None = None) -> None:
        super().__init__(contract, events=events)
        self.clock = clock
        self.poll_count = 0
        self.safety_mode = "NORMAL"
        self.robot_mode = "RUNNING"
        self.runtime_state = "PLAYING"
        self.tcp_speed = [0.0] * 6
        self.qd = [0.0] * 6
        self.gap_after_first = False
        self._seen_fresh = False
        self.safety_at_poll: dict[int, object] = {}
        self.speed_at_poll: dict[int, list[float]] = {}
        self.send_error_on_stop = False
        self.ack_stop = True
        self.stop_send_while_open = None
        self.polls_at_stop_send = None
        self.polls_at_close = None
        self.polls_after_stop_before_close = 0
        self._stop_sent = False

    def poll_output(self, *, wait_s: float = 0.0):
        if not self.opened:
            raise RuntimeError("RTDE is not open")
        self.poll_count += 1
        if self.events is not None:
            self.events.append("rtde.poll")
        if self._stop_sent:
            self.polls_after_stop_before_close += 1
        if self.poll_count in self.safety_at_poll:
            self.safety_mode = self.safety_at_poll[self.poll_count]
        if self.poll_count in self.speed_at_poll:
            self.tcp_speed = list(self.speed_at_poll[self.poll_count])
        if self.gap_after_first and self._seen_fresh:
            return None
        payload = self._mapping()
        payload["safety_mode"] = self.safety_mode
        payload["robot_mode"] = self.robot_mode
        payload["runtime_state"] = self.runtime_state
        payload["actual_TCP_speed"] = list(self.tcp_speed)
        payload["actual_qd"] = list(self.qd)
        payload["timestamp"] = 1000.0 + self.poll_count
        snapshot = R004OutputSnapshot.from_mapping(
            float(self.clock.now()),
            payload,
            received_monotonic_s=float(self.clock.now()),
        )
        self._seen_fresh = True
        return snapshot

    def send_packet(self, double_values, integer_values) -> None:
        if not self.opened:
            raise RuntimeError("RTDE is not open")
        if integer_values[3] == int(SessionCommand.STOP):
            self.stop_send_while_open = True
            self.polls_at_stop_send = self.poll_count
            self._stop_sent = True
        if self.send_error_on_stop and integer_values[3] == int(SessionCommand.STOP):
            if self.events is not None:
                self.events.append("rtde.send_error")
            raise RuntimeError("injected STOP send failure")
        if not self.ack_stop:
            self.sent_packets.append((tuple(double_values), tuple(integer_values)))
            if self.events is not None:
                self.events.append("rtde.send")
            self._input_doubles = [float(value) for value in double_values]
            self._input_integers = [int(value) for value in integer_values]
            return
        super().send_packet(double_values, integer_values)

    def close(self) -> None:
        self.polls_at_close = self.poll_count
        super().close()


def _writer(tmp_path, clock, rtde, kunwei, **kwargs):
    return NativeYieldLiveWriter(
        _prerequisites(),
        authority_root=tmp_path / "authority",
        route_id="r004-test-route",
        attempt_id="r004-host-lifecycle",
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=clock.wall,
        mono_clock=clock.now,
        sleep=clock.sleep,
        **kwargs,
    )


def _stop_packets(rtde) -> list:
    return [packet for packet in rtde.sent_packets if packet[1][3] == int(SessionCommand.STOP)]


def test_warmup_safety_change_fails_distinctly_without_hold_or_arm(tmp_path):
    clock = Clock()
    events: list[str] = []
    contract = _prerequisites().contract
    rtde = ScriptedRTDE(contract, clock=clock, events=events)
    rtde.safety_at_poll = {3: "PROTECTIVE_STOP"}
    rtde.qd = [0.01, 0.0, 0.0, 0.0, 0.0, 0.0]
    kunwei = FakeLiveKunweiTransport(events=events, observed_clock=clock.now)
    writer = _writer(tmp_path, clock, rtde, kunwei)
    with pytest.raises(LiveWriterError, match="safety invalid") as raised:
        writer.open(live_ack=LIVE_ACK, now_s=100.0)
    assert "stationarity invalid" not in str(raised.value)
    assert "PROTECTIVE_STOP" in str(raised.value)
    assert "robot_mode='RUNNING'" in str(raised.value)
    assert writer._opened is False
    assert writer._first_output_error is not None
    assert writer.admission_robot_observations
    assert writer.rejected_robot_observations[0]["output"] is writer.admission_robot_observations[-1]
    assert writer.rejected_robot_observations[0]["safety_mode"] == "PROTECTIVE_STOP"
    assert not any(packet[1][3] == int(SessionCommand.HOLD) for packet in rtde.sent_packets)
    assert not any(packet[1][3] == int(SessionCommand.ARM) for packet in rtde.sent_packets)
    assert _stop_packets(rtde)


def test_rtde_gap_during_warmup_fails_without_refreshing_cached_timestamps(tmp_path):
    clock = Clock()
    contract = _prerequisites().contract
    rtde = ScriptedRTDE(contract, clock=clock)
    rtde.gap_after_first = True
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    writer = _writer(tmp_path, clock, rtde, kunwei)
    with pytest.raises(LiveWriterError, match="stale") as raised:
        writer.open(live_ack=LIVE_ACK, now_s=100.0)
    assert "without refreshing cached timestamps" in str(raised.value)
    assert writer._last_rtde_frame_sequence == 1001.0
    assert writer._last_rtde_frame_mono_s == pytest.approx(0.01)
    assert writer._opened is False


def test_partial_open_failure_observes_stop_before_closing(tmp_path):
    clock = Clock()
    events: list[str] = []
    contract = _prerequisites().contract
    rtde = ScriptedRTDE(contract, clock=clock, events=events)
    rtde.safety_at_poll = {1: "PROTECTIVE_STOP"}
    kunwei = FakeLiveKunweiTransport(events=events, observed_clock=clock.now)
    writer = _writer(tmp_path, clock, rtde, kunwei)
    with pytest.raises(LiveWriterError, match="safety invalid"):
        writer.open(live_ack=LIVE_ACK, now_s=100.0)
    assert writer._opened is False
    assert rtde.stop_send_while_open is True
    assert rtde.polls_at_stop_send is not None
    assert rtde.polls_at_close is not None
    assert rtde.polls_after_stop_before_close >= 1
    assert rtde.polls_at_close > rtde.polls_at_stop_send
    send_index = events.index("rtde.send")
    close_index = events.index("rtde.close")
    assert any(item == "rtde.poll" for item in events[send_index:close_index])
    cached = stop_and_confirm(writer, timeout_s=0.01)
    assert len(_stop_packets(rtde)) == 1
    assert cached is not None
    assert cached["stop_requested"] is True


def test_sensor_wait_uses_time_after_receiving_a_new_frame(tmp_path):
    clock=Clock(); rtde=ScriptedRTDE(_prerequisites().contract,clock=clock)
    poll=rtde.poll_output
    def delayed(**kwargs):
        clock.sleep(.001)
        return poll(**kwargs)
    rtde.poll_output=delayed
    writer=_writer(tmp_path,clock,rtde,FakeLiveKunweiTransport(observed_clock=clock.now))
    writer.open(live_ack=LIVE_ACK,now_s=100.)
    assert writer._opened
    writer.close()


@pytest.mark.parametrize('old_sequence,confirmed',[(False,True),(True,False)])
def test_stop_ack_matches_sequence_and_preserves_terminal_fault(tmp_path,old_sequence,confirmed):
    from dataclasses import replace
    clock=Clock();rtde=ScriptedRTDE(_prerequisites().contract,clock=clock)
    writer=_writer(tmp_path,clock,rtde,FakeLiveKunweiTransport(observed_clock=clock.now))
    writer.open(live_ack=LIVE_ACK,now_s=100.)
    poll=rtde.poll_output
    def terminal(**kwargs):
        frame=poll(**kwargs);echoes=dict(frame.integer_echoes)
        echoes[28]=43
        if old_sequence:echoes[29]-=1
        return replace(frame,integer_echoes=echoes)
    rtde.poll_output=terminal
    result=stop_and_confirm(writer,timeout_s=.02)
    assert result['stopped'] is confirmed
    assert result['stop_reason']==43
    writer.close()


def test_stop_send_error_is_not_reported_as_success(tmp_path):
    clock = Clock()
    contract = _prerequisites().contract
    rtde = ScriptedRTDE(contract, clock=clock)
    rtde.send_error_on_stop = True
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    writer = _writer(tmp_path, clock, rtde, kunwei)
    writer.open(live_ack=LIVE_ACK, now_s=100.0)
    receipt = stop_and_confirm(writer, timeout_s=0.02)
    assert receipt["stopped"] is False
    assert receipt["stop_send_ok"] is False
    assert receipt["stop_send_error"] is not None
    assert "injected STOP send failure" in receipt["stop_send_error"]
    assert receipt["reason"] == "stop_send_error"
    writer.close()


def test_unconfirmed_ack_is_not_a_tp_stop(tmp_path):
    clock = Clock()
    contract = _prerequisites().contract
    rtde = ScriptedRTDE(contract, clock=clock)
    rtde.ack_stop = False
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    writer = _writer(tmp_path, clock, rtde, kunwei)
    writer.open(live_ack=LIVE_ACK, now_s=100.0)
    receipt = stop_and_confirm(writer, timeout_s=0.02)
    assert receipt["stop_send_ok"] is True
    assert receipt["stopped"] is False
    assert receipt["tp_ack"] is False
    assert receipt["reason"] == "fresh_stationary_stop_confirmation_timeout"
    writer.close()


def test_duplicate_stop_and_close_send_one_stop_and_cache_receipt(tmp_path):
    clock = Clock()
    contract = _prerequisites().contract
    rtde = ScriptedRTDE(contract, clock=clock)
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    writer = _writer(tmp_path, clock, rtde, kunwei)
    writer.open(live_ack=LIVE_ACK, now_s=100.0)
    first = stop_and_confirm(writer, timeout_s=0.05)
    assert first["stopped"] is True
    assert first["tp_ack"] is True
    assert first["reason"] == 4
    writer.close()
    second = stop_and_confirm(writer, timeout_s=0.01)
    assert second == first
    assert len(_stop_packets(rtde)) == 1
    assert writer._stop_packet_provenance == "stop_only"


def test_protective_stop_stationary_is_not_tp_stop_ack(tmp_path):
    clock = Clock()
    contract = _prerequisites().contract
    rtde = ScriptedRTDE(contract, clock=clock)
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    writer = _writer(tmp_path, clock, rtde, kunwei)
    writer.open(live_ack=LIVE_ACK, now_s=100.0)
    rtde.safety_mode = "PROTECTIVE_STOP"
    receipt = stop_and_confirm(writer, timeout_s=0.05)
    assert receipt["stopped"] is False
    assert receipt["observed_stationary"] is True
    assert receipt["protective_stop"] is True
    assert receipt["tp_ack"] is False
    assert receipt["reason"] == "protective_stop_is_not_tp_stop_ack"
    writer.close()


def test_stop_only_packet_does_not_use_baseline_as_a_measurement(tmp_path):
    clock = Clock()
    contract = _prerequisites().contract
    rtde = ScriptedRTDE(contract, clock=clock)
    kunwei = FakeLiveKunweiTransport(
        wrench_n_nm=(0.0, 0.0, -5.0, 0.0, 0.0, 0.0),
        observed_clock=clock.now,
    )
    writer = _writer(tmp_path, clock, rtde, kunwei, software_baseline_n=(1.0, 0.0, 0.0, 0.0, 0.0, 0.0))
    writer.open(live_ack=LIVE_ACK, now_s=100.0)
    hold = rtde.sent_packets[0][0]
    assert hold[7:13] == pytest.approx((-1.0, 0.0, -5.0, 0.0, 0.0, 0.0))
    writer.close()
    stop = _stop_packets(rtde)[-1][0]
    assert stop[7:13] == (0.0,) * 6
    assert stop[3] == 0.0
    assert writer._stop_packet_provenance == "stop_only"


def test_stationarity_invalid_is_distinct_from_safety_invalid(tmp_path):
    clock = Clock()
    contract = _prerequisites().contract
    rtde = ScriptedRTDE(contract, clock=clock)
    rtde.speed_at_poll = {1: [0.01, 0.0, 0.0, 0.0, 0.0, 0.0]}
    rtde.qd = [0.02, 0.0, 0.0, 0.0, 0.0, 0.0]
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    writer = _writer(tmp_path, clock, rtde, kunwei)
    with pytest.raises(LiveWriterError, match="stationarity invalid") as raised:
        writer.open(live_ack=LIVE_ACK, now_s=100.0)
    detail = str(raised.value)
    assert "safety invalid" not in detail
    assert "safety_mode='NORMAL'" in detail
    assert "tcp_speed=" in detail
    assert "qd=" in detail
    assert writer.rejected_robot_observations[0]["error"].startswith("LiveWriterError: ")
    assert "stationarity invalid" in writer.rejected_robot_observations[0]["error"]


def test_first_rejected_frame_logs_remain_intact(monkeypatch):
    writer = object.__new__(NativeYieldLiveWriter)
    writer._opened = False
    writer._mono_clock = lambda: 12.0
    writer.admission_robot_observations = []
    writer.rejected_robot_observations = []
    writer._first_output_error = None
    output = SimpleNamespace(safety_mode=3, runtime_state=1)
    def reject(*args, **kwargs):
        raise RuntimeError("runtime Safety/stationary gate failed")
    monkeypatch.setattr(R006LiveWriter, "_validate_output", reject)
    with pytest.raises(RuntimeError, match="Safety"):
        writer._validate_output(output, require_stationary=True)
    assert writer.admission_robot_observations == [output]
    assert writer.rejected_robot_observations[0]["output"] is output
    assert writer.rejected_robot_observations[0]["host_monotonic_s"] == 12.0
    assert writer._first_output_error["output"] is output
