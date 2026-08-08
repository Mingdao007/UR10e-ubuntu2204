from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from pathlib import Path

import pytest

from step5d_autotune_v4_r004.evidence import (
    EvidenceError,
    PathEvidenceCollector,
    PathSample,
    _json_safe,
)
from step5d_autotune_v4_r004.ledger import DurableCampaignLedger
from step5d_autotune_v4_r004.timing import (
    MAX_FRESH_GAP_S,
    RUNTIME_STALE_STOP_S,
    TimingEvidence,
    TimingEvidenceCollector,
)


def _timing(*, count: int = 460, gap: float = 0.019, age: float = 0.010) -> TimingEvidence:
    return TimingEvidence.from_counts(
        duration_s=1.0,
        writer_publishes=count,
        rtde_frames=count,
        kunwei_frames=count,
        tp_consumed_echoes=count,
        feedback_age_p99_s=age,
        max_fresh_gap_s=gap,
    )


def test_timing_layers_use_distinct_frames_and_460_hz_is_inclusive() -> None:
    collector = TimingEvidenceCollector()
    for index in range(460):
        timestamp = index / 460.0
        collector.observe_writer_publish(timestamp, sequence=index)
        collector.observe_rtde_frame(timestamp, sequence=index)
        collector.observe_kunwei_frame(timestamp, sequence=index)
        collector.observe_tp_consumed_echo(timestamp, sequence=index)
        collector.observe_feedback(timestamp, 0.010, sequence=index)
    evidence = collector.finalize(duration_s=1.0)
    assert evidence.successful
    assert evidence.successful_writer_publishes == 460
    assert evidence.distinct_rtde_frames == 460
    assert evidence.distinct_kunwei_frames == 460
    assert evidence.distinct_tp_consumed_packet_echoes == 460
    assert evidence.nominal_rate_hz == 500.0
    assert evidence.minimum_rate_hz == 460.0
    assert evidence.runtime_stale_stop_s == RUNTIME_STALE_STOP_S


def test_cached_or_duplicate_frames_do_not_count() -> None:
    collector = TimingEvidenceCollector()
    for index in range(460):
        timestamp = index / 460.0
        collector.observe_writer_publish(timestamp, sequence=index)
        collector.observe_rtde_frame(timestamp, sequence=index)
        collector.observe_kunwei_frame(timestamp, sequence=index)
        collector.observe_tp_consumed_echo(timestamp, sequence=index)
        collector.observe_feedback(timestamp, 0.001, sequence=index)
    # A cached callback with a new host timestamp is not a new frame.
    for layer in (
        collector.observe_writer_publish,
        collector.observe_rtde_frame,
        collector.observe_kunwei_frame,
        collector.observe_tp_consumed_echo,
    ):
        layer(1.1, sequence=459)
    collector.observe_feedback(1.1, 0.001, sequence=459)
    evidence = collector.finalize(duration_s=1.1)
    assert evidence.successful_writer_publishes == 460
    assert evidence.distinct_rtde_frames == 460
    assert evidence.distinct_kunwei_frames == 460
    assert evidence.distinct_tp_consumed_packet_echoes == 460
    assert not TimingEvidence.from_counts(
        duration_s=1.0,
        writer_publishes=459,
        rtde_frames=460,
        kunwei_frames=460,
        tp_consumed_echoes=460,
        feedback_age_p99_s=0.001,
        max_fresh_gap_s=0.001,
    ).successful


def test_kunwei_batched_cumulative_sequence_counts_physical_frames() -> None:
    collector = TimingEvidenceCollector()
    for index, cumulative in enumerate((8, 17, 25, 34)):
        timestamp = index * 0.018
        collector.observe_writer_publish(timestamp, sequence=index)
        collector.observe_rtde_frame(timestamp, sequence=index)
        collector.observe_kunwei_frame(timestamp, sequence=cumulative)
        collector.observe_tp_consumed_echo(timestamp, sequence=index)
        collector.observe_feedback(timestamp, 0.009, sequence=index)
    # First observation does not claim frames accumulated before this
    # collector; later deltas are directly evidenced by the transport counter.
    evidence = collector.finalize(duration_s=0.054)
    assert evidence.distinct_kunwei_frames == 27
    assert evidence.max_fresh_gap_s == pytest.approx(0.018)


def test_feedback_bounds_are_distinct_from_runtime_stale_stop() -> None:
    assert MAX_FRESH_GAP_S == 0.020
    assert RUNTIME_STALE_STOP_S == 0.080
    assert _timing(gap=0.019999).successful
    assert not _timing(gap=0.020).successful
    assert _timing(age=0.010).successful
    assert not _timing(age=0.010001).successful


def _force_only_collector() -> PathEvidenceCollector:
    collector = PathEvidenceCollector()
    for index in range(6001):
        collector.observe(
            PathSample(
                observed_at_s=index * 0.01,
                filtered_normal_n=5.0,
                force_norm_n=5.0,
                torque_norm_nm=0.1,
                sensor_fresh=True,
                state=25,
                safety_normal=True,
            )
        )
    return collector


def test_force_bins_alone_cannot_prove_path_motion() -> None:
    with pytest.raises(EvidenceError, match="force bins alone cannot prove motion"):
        _force_only_collector().finalize(return_gate_passed=True, home_proof={})


def test_path_requires_motion_metrics_and_timing_layers() -> None:
    collector = PathEvidenceCollector()
    collector.mark_path_start(observed_at_s=0.0, rtde_timestamp_s=0.0, tp_sequence=0)
    collector.observe(
        PathSample(
            observed_at_s=0.0,
            filtered_normal_n=5.0,
            force_norm_n=5.0,
            torque_norm_nm=0.1,
            sensor_fresh=True,
            state=20,
            safety_normal=True,
        )
    )
    for index in range(27600):
        path_time = index * (60.0 / 27600.0)
        signal = (
            math.sin(path_time),
            math.cos(path_time),
            math.sin(2.0 * path_time),
            math.cos(2.0 * path_time),
            math.sin(3.0 * path_time),
            math.cos(3.0 * path_time),
        )
        collector.observe(
            PathSample(
                observed_at_s=path_time,
                filtered_normal_n=5.0,
                force_norm_n=5.0,
                torque_norm_nm=0.1,
                sensor_fresh=True,
                state=25,
                safety_normal=True,
                desired_xy_m=(0.001 * math.sin(path_time), 0.001 * math.cos(path_time)),
                actual_xy_m=(0.001 * math.sin(path_time) + 0.0001, 0.001 * math.cos(path_time)),
                path_time_s=path_time,
                path_phase=6 if index == 27599 else min(6, int(path_time // 10.0)),
                desired_velocity_m_s=(0.001 * math.cos(path_time), -0.001 * math.sin(path_time)),
                actual_velocity_m_s=(0.001 * math.cos(path_time), -0.001 * math.sin(path_time)),
                qdot=signal,
                actual_qd=tuple(0.99 * value for value in signal),
                source_ages_s={"writer": 0.005, "rtde": 0.005, "kunwei": 0.005, "tp": 0.005},
                source_sequences={"writer": index, "rtde": path_time, "kunwei": index, "tp": index},
                qd_lag_s=0.010,
            )
        )
    evidence = collector.finalize(
        return_gate_passed=True,
        contact_gate_passed=True,
        home_proof={"stationary": True},
    )
    assert evidence.motion_gate_passed
    assert evidence.timing_gate_passed
    assert evidence.path_duration_s == pytest.approx(60.0)
    assert evidence.path_phase == 6
    assert evidence.xy_error_p95_m <= 0.0005
    assert evidence.xy_error_max_m <= 0.001
    assert evidence.endpoint_error_max_m <= 0.001
    assert evidence.qd_correlation >= 0.9
    assert evidence.qd_lag_s <= 0.020


def test_path_collector_non_path_state_is_not_accepted() -> None:
    collector = PathEvidenceCollector()
    assert collector.observe(
        PathSample(
            observed_at_s=0.0,
            filtered_normal_n=5.0,
            force_norm_n=5.0,
            torque_norm_nm=0.1,
            sensor_fresh=True,
            state=20,
            safety_normal=True,
        )
    ) is False
    assert collector.path_samples == ()


def _motion_sample(index: int, path_time_s: float, *, final: bool = False) -> PathSample:
    signal = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return PathSample(
        observed_at_s=path_time_s,
        filtered_normal_n=5.0,
        force_norm_n=5.0,
        torque_norm_nm=0.1,
        sensor_fresh=True,
        state=25,
        safety_normal=True,
        desired_xy_m=(0.0, 0.0),
        actual_xy_m=(0.0001, 0.0),
        path_time_s=path_time_s,
        path_phase=6 if final else min(6, int(path_time_s // 10.0)),
        desired_velocity_m_s=(0.0, 0.0),
        actual_velocity_m_s=(0.0, 0.0),
        qdot=signal,
        actual_qd=signal,
        source_ages_s={"writer": 0.005, "rtde": 0.005, "kunwei": 0.005, "tp": 0.005},
        source_sequences={"writer": index, "rtde": path_time_s, "kunwei": index, "tp": index + 1},
        qd_lag_s=0.010,
    )


def _coverage_collector(first_path_time_s: float, last_path_time_s: float) -> PathEvidenceCollector:
    sample_count = int((last_path_time_s - first_path_time_s) / 0.002) + 1
    collector = PathEvidenceCollector()
    collector.mark_path_start(observed_at_s=0.0, rtde_timestamp_s=0.0, tp_sequence=0)
    for index in range(sample_count):
        path_time_s = first_path_time_s + index * 0.002
        if index == sample_count - 1:
            path_time_s = last_path_time_s
        collector.observe(
            _motion_sample(index, path_time_s, final=index == sample_count - 1)
        )
    return collector


def _live_absolute_clock_coverage_collector(
    last_path_time_s: float,
    *,
    rtde_origin_s: float = 10.0,
    interval_s: float = 0.002,
) -> PathEvidenceCollector:
    """Model the live writer's absolute RTDE timestamp -> path-time order."""

    sample_count = int(round(last_path_time_s / interval_s)) + 1
    collector = PathEvidenceCollector(require_path_boundary=True)
    collector.mark_path_start(
        observed_at_s=100.0,
        rtde_timestamp_s=rtde_origin_s,
        tp_sequence=0,
    )
    for index in range(sample_count):
        offset_s = (
            last_path_time_s
            if index == sample_count - 1
            else index * interval_s
        )
        absolute_rtde_s = rtde_origin_s + offset_s
        path_time_s = absolute_rtde_s - rtde_origin_s
        sample = _motion_sample(
            index,
            path_time_s,
            final=index == sample_count - 1,
        )
        source_sequences = {
            "writer": index,
            "rtde": absolute_rtde_s,
            "kunwei": index,
            "tp": index,
        }
        collector.observe(
            replace(
                sample,
                observed_at_s=100.0 + path_time_s,
                source_sequences=source_sequences,
                source_sequence=source_sequences,
            )
        )
    return collector


def test_500hz_last_strict_sample_uses_one_validated_coverage_interval() -> None:
    evidence = _coverage_collector(0.002360, 59.998000).finalize(
        return_gate_passed=True,
        contact_gate_passed=True,
        home_proof={"stationary": True},
    )
    assert evidence.metrics["path_observed_span_s"] == pytest.approx(59.995640)
    assert evidence.path_duration_s == pytest.approx(60.0)
    assert evidence.metrics["path_duration_basis"] == (
        "tp_consumed_sequence+rtde_controller_timestamp"
    )
    assert evidence.metrics["path_coverage_interval_s"] == pytest.approx(0.002)
    assert evidence.metrics["path_cadence_hz"] == pytest.approx(500.0)


def test_500hz_truly_short_last_sample_remains_rejected_after_coverage() -> None:
    with pytest.raises(EvidenceError, match="below 60 s"):
        _coverage_collector(0.002360, 59.996000).finalize(
            return_gate_passed=True,
            contact_gate_passed=True,
            home_proof={"stationary": True},
        )


def test_live_absolute_endpoint_ulp_underflow_is_canonicalized_to_exact_60s() -> None:
    collector = _live_absolute_clock_coverage_collector(59.998000)
    evidence = collector.finalize(
        return_gate_passed=True,
        contact_gate_passed=True,
        home_proof={"stationary": True},
    )
    raw_duration = (
        evidence.metrics["path_physical_span_s"]
        + evidence.metrics["path_coverage_interval_s"]
    )
    assert raw_duration == math.nextafter(60.0, -math.inf)
    assert evidence.path_duration_s == 60.0
    assert evidence.metrics["path_duration_s"] == 60.0
    assert evidence.motion_gate_passed


def test_live_absolute_endpoint_one_frame_short_remains_fail_closed() -> None:
    with pytest.raises(EvidenceError, match="below 60 s"):
        _live_absolute_clock_coverage_collector(59.996000).finalize(
            return_gate_passed=True,
            contact_gate_passed=True,
            home_proof={"stationary": True},
        )


def test_live_large_absolute_origin_multi_ulp_endpoint_uses_source_bound() -> None:
    rtde_origin_s = 1_000_000_000.0
    validated_interval_s = 16_776 * math.ulp(rtde_origin_s)
    evidence = _live_absolute_clock_coverage_collector(
        59.998000,
        rtde_origin_s=rtde_origin_s,
        interval_s=validated_interval_s,
    ).finalize(
        return_gate_passed=True,
        contact_gate_passed=True,
        home_proof={"stationary": True},
    )
    raw_duration = (
        evidence.metrics["path_physical_span_s"]
        + evidence.metrics["path_coverage_interval_s"]
    )
    raw_deficit = 60.0 - raw_duration
    assert raw_deficit > math.ulp(60.0)
    assert raw_deficit < validated_interval_s
    assert evidence.metrics["path_duration_rounding_bound_s"] >= raw_deficit
    assert 2.0 * evidence.metrics["path_duration_rounding_bound_s"] < validated_interval_s
    assert evidence.path_duration_s == 60.0
    assert evidence.metrics["path_duration_s"] == 60.0
    assert evidence.motion_gate_passed


def test_live_large_absolute_origin_one_frame_short_remains_fail_closed() -> None:
    rtde_origin_s = 1_000_000_000.0
    validated_interval_s = 16_776 * math.ulp(rtde_origin_s)
    with pytest.raises(EvidenceError, match="below 60 s"):
        _live_absolute_clock_coverage_collector(
            59.996000,
            rtde_origin_s=rtde_origin_s,
            interval_s=validated_interval_s,
        ).finalize(
            return_gate_passed=True,
            contact_gate_passed=True,
            home_proof={"stationary": True},
        )


def test_common_tp_rtde_boundary_rejects_cached_join_without_counting_it() -> None:
    collector = PathEvidenceCollector()
    collector.mark_path_start(observed_at_s=0.0, rtde_timestamp_s=0.0, tp_sequence=0)
    sample = _motion_sample(1, 0.002)
    assert collector.observe(sample)
    assert not collector.observe(replace(sample, observed_at_s=0.004))
    assert len(collector.path_samples) == 1


def _row(ordinal: int = 1, *, status: str = "completed") -> dict[str, object]:
    return {
        "logical_attempt_ordinal": ordinal,
        "attempt_execution_id": f"r004-test-{ordinal}-{os.urandom(4).hex()}",
        "session_epoch": 1,
        "controller_receipt_sha256": "1" * 64,
        "script1_receipt_sha256": "2" * 64,
        "input_baseline_ledger_sha256": "3" * 64,
        "status": status,
        "completed": status == "completed",
        "gp_eligible": False,
        "output_seal": {"completion_sha256": "4" * 64},
    }


def test_ledger_uses_fsync_and_fresh_process_cold_read(tmp_path: Path) -> None:
    ledger = DurableCampaignLedger(tmp_path / "attempts.jsonl")
    receipt = ledger.append_attempt(_row())
    assert receipt.ready_for_next_arm
    assert receipt.verification_pid is not None and receipt.verification_pid != os.getpid()
    assert ledger.next_arm_allowed()
    assert ledger.rows[-1]["durability"] == {
        "fsynced": True,
        "cold_read": True,
        "hash_verified": True,
    }
    raw_records = [line for line in ledger.path.read_text(encoding="utf-8").splitlines() if line]
    assert len(raw_records) == 2
    assert "durable_row" not in __import__("json").loads(raw_records[0])


def test_supersession_creates_new_ledger_without_mutating_old(tmp_path: Path) -> None:
    old = DurableCampaignLedger(tmp_path / "old.jsonl")
    old.append_attempt(_row())
    before = old.path.read_bytes()
    new = old.supersede(tmp_path / "new.jsonl", reason="r004 evidence contract revision")
    assert old.path.read_bytes() == before
    assert new.rows == ()
    assert new.header is not None
    assert new.header["supersedes_output_sha256"] == old.last_output_sha256


def test_finalize_digest_allows_host_hard_tube_disabled_nan() -> None:
    """Regression: nested NaN in host_hard_tube must not raise at allow_nan=False digest."""
    material = {
        "samples": [
            {
                "host_hard_tube": {
                    "enabled": False,
                    "actual_distance_m": math.nan,
                    "remaining_margin_m": math.nan,
                }
            }
        ]
    }
    with pytest.raises(ValueError, match="Out of range float"):
        json.dumps(material, allow_nan=False)
    safe = _json_safe(material)
    encoded = json.dumps(safe, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert json.loads(encoded)["samples"][0]["host_hard_tube"]["actual_distance_m"] is None
