from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

import tools.step5d_autotune_v4_r013.lifecycle_trace as lifecycle_module
from tools.step5d_autotune_v4_r013.lifecycle_trace import (
    FLAG_TERMINAL,
    LifecycleTrace,
    load_lifecycle_artifact,
)


def _output(timestamp: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(
        timestamp=timestamp,
        observed_at_s=timestamp,
        tcp_pose_m_rad=(0.1, 0.2, 0.3, 0.0, 0.0, 0.0),
        tcp_speed_m_s_rad_s=(0.001, 0.0, 0.0, 0.0, 0.0, 0.0),
        qd_rad_s=(0.01, 0.0, 0.0, 0.0, 0.0, 0.0),
    )


def _sensor(force: float = 0.2) -> SimpleNamespace:
    return SimpleNamespace(
        normal_load_n=force,
        force_norm_n=force,
        filtered_normal_n=force,
        torque_norm_nm=0.01,
        wrench=(0.0, 0.0, -force, 0.0, 0.0, 0.0),
        sensor_fresh=True,
    )


def _complete(trace: LifecycleTrace, *, terminal: int = 78) -> dict:
    trace.begin_attempt(1, "exec-1", "BO_TRIAL", epoch=4, path_requested=True)
    for index, state in enumerate((11, 20, 21, 25, 25, 40)):
        trace.observe_tick(
            monotonic_s=index * 0.002,
            output=_output(index * 0.002),
            sensor=_sensor(),
            tp_state=state,
            command_mode=1,
            qdot=(0.001,) * 6,
            setpoint_n=0.3,
            packet_sequence=index,
            consumed_packet_sequence=index,
        )
    trace.observe_terminal(
        monotonic_s=0.012,
        output=_output(0.012),
        sensor=_sensor(),
        tp_state=terminal,
        packet_sequence=5,
        consumed_packet_sequence=5,
    )
    return trace.finalize_attempt()


def test_lifecycle_binary_round_trip_is_complete(tmp_path: Path) -> None:
    receipt = _complete(LifecycleTrace(tmp_path, chunk_records=2, path_min_duration_s=0.001))

    assert receipt["status"] == "complete"
    assert receipt["coverage_complete"] is True
    assert receipt["sample_count"] == 7
    assert receipt["missing_phases"] == []
    phase_counts = {
        row["name"]: row["sample_count"] for row in receipt["phase_sample_counts"]
    }
    assert phase_counts["BASELINE"] == 1
    assert phase_counts["PATH"] == 2
    metadata, rows = load_lifecycle_artifact(Path(receipt["artifact_path"]))
    assert metadata["schema"] == "step5d.autotune-v4/r013-force-lifecycle-v1"
    assert len(rows) == 7
    assert rows[-1]["tp_state"] == 78
    assert rows[-1]["flags"] & FLAG_TERMINAL
    assert rows[2]["qdot"] == (0.001,) * 6


def test_lifecycle_default_rejects_short_path_even_when_all_phases_exist(tmp_path: Path) -> None:
    receipt = _complete(LifecycleTrace(tmp_path, chunk_records=2))

    assert receipt["status"] == "incomplete"
    assert receipt["path_coverage_complete"] is False
    assert receipt["path_duration_s"] == 0.002
    assert any("PATH coverage is shorter" in error for error in receipt["errors"])


def test_path_end_request_excludes_stale_tp_state25_rows(tmp_path: Path) -> None:
    trace = LifecycleTrace(
        tmp_path, chunk_records=2, max_gap_s=1.0, path_min_duration_s=0.001
    )
    trace.begin_attempt(1, "exec-path-end", "BO_TRIAL", path_requested=True)
    for index, state in enumerate((11, 20, 21, 25, 25, 25, 25, 40)):
        trace.observe_tick(
            monotonic_s=index * 0.1,
            output=_output(index * 0.1),
            sensor=_sensor(),
            tp_state=state,
            command_mode=1,
            packet_sequence=index,
            consumed_packet_sequence=index,
            path_end_requested=index >= 6,
        )
    trace.observe_terminal(
        monotonic_s=0.9,
        output=_output(0.9),
        sensor=_sensor(),
        tp_state=78,
        packet_sequence=8,
        consumed_packet_sequence=8,
    )
    receipt = trace.finalize_attempt()

    assert receipt["status"] == "complete"
    assert receipt["path_end_requested"] is True
    assert receipt["path_sample_count"] == 3
    assert receipt["path_duration_s"] == pytest.approx(0.2)


def test_terminal_host_boundary_gap_is_diagnostic_when_rtde_and_packets_are_continuous(
    tmp_path: Path,
) -> None:
    trace = LifecycleTrace(tmp_path, max_gap_s=0.010)
    trace.begin_attempt(1, "exec-terminal-boundary", "BO_TRIAL", path_requested=False)
    for index, state in enumerate((11, 20, 21, 40)):
        trace.observe_tick(
            monotonic_s=index * 0.002,
            output=_output(index * 0.002),
            sensor=_sensor(),
            tp_state=state,
            packet_sequence=index,
        )
    trace.observe_terminal(
        monotonic_s=0.080,
        output=_output(0.008),
        sensor=_sensor(),
        tp_state=78,
        packet_sequence=3,
    )
    receipt = trace.finalize_attempt()

    assert receipt["status"] == "complete"
    assert receipt["max_gap_s"] > receipt["max_gap_limit_s"]
    assert receipt["max_nonterminal_gap_s"] <= receipt["max_gap_limit_s"]
    assert receipt["terminal_observation_gap_s"] > receipt["max_gap_limit_s"]
    assert abs(receipt["terminal_rtde_gap_s"] - 0.002) < 1e-9
    assert receipt["packet_sequence_gap_count"] == 0
    assert any("terminal host observation gap" in warning for warning in receipt["warnings"])


def test_cold_packet_sequence_gap_is_incomplete(tmp_path: Path) -> None:
    trace = LifecycleTrace(tmp_path, max_gap_s=0.010)
    trace.begin_attempt(1, "exec-packet-gap", "BO_TRIAL", path_requested=False)
    for index, (state, packet_sequence) in enumerate(
        ((11, 0), (20, 2), (21, 3), (40, 4))
    ):
        trace.observe_tick(
            monotonic_s=index * 0.002,
            output=_output(index * 0.002),
            sensor=_sensor(),
            tp_state=state,
            packet_sequence=packet_sequence,
        )
    trace.observe_terminal(
        monotonic_s=0.010,
        output=_output(0.008),
        sensor=_sensor(),
        tp_state=78,
        packet_sequence=4,
    )
    receipt = trace.finalize_attempt()

    assert receipt["status"] == "incomplete"
    assert receipt["coverage_complete"] is False
    assert receipt["packet_sequence_gap_count"] == 1
    assert any("packet sequence gaps" in error for error in receipt["errors"])


def test_lifecycle_missing_phase_and_gap_are_incomplete(tmp_path: Path) -> None:
    trace = LifecycleTrace(tmp_path, max_gap_s=0.010)
    trace.begin_attempt(1, "exec-missing", "BO_TRIAL", path_requested=True)
    trace.observe_tick(
        monotonic_s=0.0,
        output=_output(0.0),
        sensor=_sensor(),
        tp_state=11,
    )
    trace.observe_terminal(
        monotonic_s=0.025,
        output=_output(0.025),
        sensor=_sensor(),
        tp_state=78,
    )
    receipt = trace.finalize_attempt()

    assert receipt["status"] == "incomplete"
    assert receipt["coverage_complete"] is False
    assert {row["name"] for row in receipt["missing_phases"]} >= {
        "CONTACT_SEARCH",
        "BASELINE",
        "PATH",
        "RETURN",
    }
    assert receipt["max_gap_s"] > receipt["max_gap_limit_s"]


def test_stopped_attempt_is_preserved_but_not_admissible(tmp_path: Path) -> None:
    trace = LifecycleTrace(tmp_path)
    trace.begin_attempt(1, "exec-stop", "BO_TRIAL", path_requested=True)
    for index, state in enumerate((11, 20, 21, 25, 40)):
        trace.observe_tick(
            monotonic_s=index * 0.001,
            output=_output(index * 0.001),
            sensor=_sensor(),
            tp_state=state,
        )
    trace.observe_terminal(
        monotonic_s=0.006,
        output=_output(0.006),
        sensor=_sensor(),
        tp_state=90,
    )
    receipt = trace.finalize_attempt()

    assert receipt["status"] == "incomplete"
    assert receipt["terminal_state"] == 90
    assert receipt["home_verified"] is False
    assert "terminal HOME state 78 was not verified" in receipt["errors"]


def test_invalid_row_is_retained_and_marks_receipt_incomplete(tmp_path: Path) -> None:
    trace = LifecycleTrace(tmp_path)
    trace.begin_attempt(1, "exec-invalid", "BO_TRIAL", path_requested=False)
    trace.observe_tick(
        monotonic_s=0.0,
        output=None,
        sensor=None,
        tp_state=11,
    )
    receipt = trace.finalize_attempt()

    assert receipt["sample_count"] == 1
    assert receipt["invalid_rows"] == 1
    assert receipt["status"] == "incomplete"


def test_missing_force_row_is_explicitly_incomplete(tmp_path: Path) -> None:
    trace = LifecycleTrace(tmp_path)
    trace.begin_attempt(1, "exec-missing-force", "BO_TRIAL", path_requested=False)
    trace.observe_tick(
        monotonic_s=0.0,
        output=_output(0.0),
        sensor=_sensor(float("nan")),
        tp_state=21,
    )
    receipt = trace.finalize_attempt(terminal_state=78, home_verified=True)

    assert receipt["missing_force_rows"] == 1
    assert receipt["coverage_complete"] is False
    assert "lifecycle rows missing force scalars" in receipt["errors"]


def test_missing_pose_row_is_explicitly_incomplete(tmp_path: Path) -> None:
    trace = LifecycleTrace(tmp_path)
    trace.begin_attempt(1, "exec-missing-pose", "BO_TRIAL", path_requested=False)
    output = _output(0.0)
    output.tcp_pose_m_rad = (float("nan"),) * 6
    trace.observe_tick(
        monotonic_s=0.0,
        output=output,
        sensor=_sensor(),
        tp_state=21,
    )
    receipt = trace.finalize_attempt(terminal_state=78, home_verified=True)

    assert receipt["missing_pose_rows"] == 1
    assert receipt["coverage_complete"] is False
    assert "lifecycle rows missing pose or TCP speed" in receipt["errors"]


def test_queue_overflow_is_counted_and_never_evicted_silently(tmp_path: Path, monkeypatch) -> None:
    worker_started = threading.Event()
    release_worker = threading.Event()

    def blocked_worker(attempt) -> None:
        worker_started.set()
        release_worker.wait(timeout=2.0)
        while True:
            chunk = attempt.queue.get()
            attempt.queue.task_done()
            if chunk is None:
                return

    monkeypatch.setattr(lifecycle_module, "_write_chunks", blocked_worker)
    trace = LifecycleTrace(tmp_path, chunk_records=1, max_queue_chunks=1)
    trace.begin_attempt(1, "exec-overflow", "BO_TRIAL", path_requested=False)
    assert worker_started.wait(timeout=1.0)
    trace.observe_tick(monotonic_s=0.0, output=_output(0.0), sensor=_sensor(), tp_state=11)
    trace.observe_tick(monotonic_s=0.001, output=_output(0.001), sensor=_sensor(), tp_state=20)

    holder: list[dict] = []
    finalizer = threading.Thread(target=lambda: holder.append(trace.finalize_attempt()))
    finalizer.start()
    release_worker.set()
    finalizer.join(timeout=2.0)

    assert not finalizer.is_alive()
    assert holder[0]["dropped_chunks"] >= 1
    assert holder[0]["status"] == "incomplete"
