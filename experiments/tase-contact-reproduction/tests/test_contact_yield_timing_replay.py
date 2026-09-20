"""Focused offline replay for the contact-yield hot path and dt contract."""

from types import SimpleNamespace

import pytest

from step5d_autotune_v4_r004 import baseline_runtime
from step5d_autotune_v4_r004.qualification import QualificationControlError
from step5d_autotune_v4_r004_live_writer import (
    HotPathTimingTrace,
    LiveR004Writer,
    LiveWriterError,
)
from step5d_autotune_v4_r004.wire import CommandMode
from contact_benchmark_provider import ContactReadinessObserver
from test_contact_qualification_provider import (
    _Provider,
    _control,
    _output,
    _sensor,
    _successful_baseline,
)
from test_step5d_autotune_v4_r004_live_boundary import _prerequisites


def test_first_sample_is_explicitly_nominal_and_next_tick_propagates_measured_dt():
    provider = _Provider()
    control = _control(provider, path_requested=False)
    control._last_monotonic_s = None
    control._origin_monotonic_s = None
    control._baseline_state = baseline_runtime.BaselineState(
        phase=baseline_runtime.BaselinePhase.SUCCESS
    )
    control._startup_ready_latched = False
    control._startup = SimpleNamespace(
        observe=lambda _heartbeat, _elapsed: False,
        stopped=False,
        stop_reason="",
    )

    first = control.step(
        output=_output(state=21),
        sensor=_sensor(),
        monotonic_s=0.002,
        command_sequence=1,
    )

    assert first.first_sample is True
    assert first.actual_dt_s is None
    assert provider.pause_calls[0]["actual_dt_s"] == pytest.approx(0.002)

    control._baseline_state = baseline_runtime.BaselineState(
        phase=baseline_runtime.BaselinePhase.RAMP
    )
    second = control.step(
        output=_output(state=21),
        sensor=_sensor(),
        monotonic_s=0.0135,
        command_sequence=2,
    )

    # The stale fixture packet timestamps are intentionally unchanged.  The
    # writer-owned monotonic sample clock is the only source of actual_dt_s.
    assert second.first_sample is False
    assert second.actual_dt_s == pytest.approx(0.0115)
    assert provider.late_cycle_calls[-1]["actual_dt_s"] == pytest.approx(0.0115)
    assert provider.late_cycle_calls[-1]["monotonic_s"] == pytest.approx(0.0135)


def test_r006_readiness_observer_does_not_reject_stationary_pre_path_late_cycle():
    """The native R006 seam uses the readiness observer as its path object.

    A delayed stationary frame before PATH is retained as evidence-only.  It
    must not reach the observer's native 4 ms admission, which would turn the
    already-reviewed bounded late-cycle policy back into a startup failure.
    """

    provider = _Provider()
    control = _control(provider, path_requested=False)
    control._path_controller = ContactReadinessObserver(0.02)
    control._baseline_state = baseline_runtime.BaselineState()
    control._last_monotonic_s = None
    control._origin_monotonic_s = None
    control._startup_ready_latched = False
    control._startup = SimpleNamespace(
        observe=lambda _heartbeat, _elapsed: False,
        stopped=False,
        stop_reason="",
    )

    first = control.step(
        output=_output(state=21),
        sensor=_sensor(),
        monotonic_s=0.002,
        command_sequence=1,
    )
    assert first.first_sample is True

    delayed = control.step(
        output=_output(state=21),
        sensor=_sensor(),
        monotonic_s=0.0136,
        command_sequence=2,
    )

    assert delayed.canonical_phase == "late_cycle"
    assert delayed.late_cycle is True
    assert delayed.actual_dt_s == pytest.approx(0.0116)
    assert provider.late_cycle_calls[-1]["actual_dt_s"] == pytest.approx(0.0116)
    assert control._path_controller.last_log.actual_dt_s == pytest.approx(0.002)


def test_failed_tick_rolls_back_provider_and_control_time_anchor(monkeypatch):
    monkeypatch.setattr(baseline_runtime, "step_baseline", _successful_baseline)

    class FailingProvider(_Provider):
        def __init__(self):
            super().__init__()
            self.state = 0

        def snapshot(self):
            return {"state": self.state, "last_result": self.last_result}

        def restore(self, state):
            self.state = state["state"]
            self.last_result = state["last_result"]

        def command(self, **kwargs):
            self.command_calls.append(kwargs)
            self.state += 1
            raise ValueError("injected provider failure")

    provider = FailingProvider()
    control = _control(provider)
    control._last_monotonic_s = 0.002
    before = provider.snapshot()

    with pytest.raises(QualificationControlError, match="injected provider failure"):
        control.step(
            output=_output(state=25),
            sensor=_sensor(),
            monotonic_s=0.004,
            command_sequence=2,
        )

    assert provider.snapshot() == before
    assert control._last_monotonic_s == pytest.approx(0.002)
    assert control._previous_qdot == (0.0,) * 6


def test_hot_path_trace_keeps_real_marks_and_does_not_fill_missing_dt():
    trace = HotPathTimingTrace(max_ticks=1)
    assert trace.begin_tick(sample_monotonic_s=73785.150631546, state=21, first_sample=True)
    trace.mark("provider_compute_enter")
    trace.mark("provider_compute_exit")
    trace.mark("pre_send_tracing_enter")
    trace.mark("transport_send_enter")
    trace.mark("transport_send_exit")
    trace.mark("post_send_tracing_enter")
    trace.mark("packet_evidence_enter")
    trace.mark("scheduler_enter")
    trace.finish(success=True)

    row = trace.rows[0]
    assert row["first_sample"] is True
    assert row["actual_dt_s"] is None
    marks = row["marks"]
    assert {
        "tick_start",
        "provider_compute_enter",
        "provider_compute_exit",
        "pre_send_tracing_enter",
        "transport_send_enter",
        "transport_send_exit",
        "post_send_tracing_enter",
        "packet_evidence_enter",
        "scheduler_enter",
        "tick_end",
    } <= set(marks)
    assert all(isinstance(mark["monotonic_ns"], int) for mark in marks.values())
    assert [mark["monotonic_ns"] for mark in marks.values()] == sorted(
        mark["monotonic_ns"] for mark in marks.values()
    )


def test_trace_rejects_duplicate_active_tick_without_changing_limits():
    trace = HotPathTimingTrace(max_ticks=1)
    trace.begin_tick(sample_monotonic_s=1.0, state=21, first_sample=False)
    with pytest.raises(LiveWriterError, match="active tick"):
        trace.begin_tick(sample_monotonic_s=1.002, state=21, first_sample=False)
    trace.finish(success=False, error=RuntimeError("replay stop"))
    assert trace.rows[0]["success"] is False
    assert trace.rows[0]["error_type"] == "RuntimeError"


def test_existing_writer_send_boundary_records_transport_and_history_marks(tmp_path):
    sent = []
    trace = HotPathTimingTrace(max_ticks=1)
    writer = LiveR004Writer(
        _prerequisites(),
        authority_root=tmp_path / "authority",
        route_id="r004-test-route",
        attempt_id="r004-yield-timing-replay",
        controller_transport=SimpleNamespace(
            send_packet=lambda *args: sent.append(args)
        ),
        kunwei_transport=object(),
        mono_clock=lambda: 0.002,
        wall_clock=lambda: 100.0,
        sleep=lambda _seconds: None,
        hot_path_timing=trace,
    )
    writer._hot_path_begin_tick(
        sample_monotonic_s=0.002, state=21, first_sample=True
    )
    writer._hot_path_mark("provider_compute_enter")
    writer._hot_path_mark("provider_compute_exit")
    writer._hot_path_mark("pre_send_enter")
    writer._send_packet(
        _sensor(),
        command_mode=CommandMode.BASELINE,
        proposed_qdot=(0.0,) * 6,
        internal_setpoint_n=1.0,
    )
    writer._hot_path_mark(
        "send_wrapper_return",
        published_monotonic_s=writer._last_writer_publish_mono_s,
    )
    writer._hot_path_mark("packet_evidence_enter")
    writer._hot_path_mark("packet_evidence_exit")
    writer._hot_path_mark("scheduler_enter")
    writer._hot_path_finish(success=True)

    assert sent
    marks = trace.rows[0]["marks"]
    assert {
        "wire_build_enter",
        "wire_build_exit",
        "transport_send_enter",
        "transport_send_exit",
        "publish_timestamp",
        "packet_history_enter",
        "packet_history_exit",
        "send_wrapper_return",
        "packet_evidence_enter",
        "packet_evidence_exit",
        "scheduler_enter",
    } <= set(marks)
    assert marks["publish_timestamp"]["published_monotonic_s"] == pytest.approx(0.002)


def test_native_offline_fixture_emits_all_hot_path_boundaries(
    tmp_path, monkeypatch
):
    from contact_yield_live_writer import NativeYieldLiveWriter
    from build_contact_qp import build
    from yield_full_writer_offline import exercise_full_writer

    lib = build(tmp_path / "qp")
    run = exercise_full_writer(
        tmp_path,
        monkeypatch,
        qp_library=lib,
        method="SFC",
        measure=True,
        writer_class=NativeYieldLiveWriter,
    )

    assert run.hot_path_timing
    first = run.hot_path_timing[0]
    assert first["first_sample"] is True
    assert first["actual_dt_s"] is None
    assert {
        "control_step_enter",
        "pre_send_tracing_enter",
        "transport_send_enter",
        "transport_send_exit",
        "post_send_tracing_enter",
        "packet_evidence_enter",
        "scheduler_enter",
    } <= set(first["marks"])
    assert run.provider.command_timeline
    timeline = run.provider.command_timeline
    assert timeline[0]["operation"] == "pause"
    assert timeline[0]["actual_dt_s"] == pytest.approx(0.002)
    assert any(row["operation"] == "command" for row in timeline)
    assert all(
        row["provider_exit_ns"] >= row["provider_enter_ns"]
        for row in timeline
    )
    assert "provider_enter_ns" in timeline[0]
