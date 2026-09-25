from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from analyze_tase_r31_cartesian_tracking import (  # noqa: E402
    AnalysisError,
    analyze_attempt,
    pair_metrics,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _attempt(tmp_path: Path) -> Path:
    attempt = tmp_path / "attempts" / "0001"
    attempt.mkdir(parents=True)
    identity = [[1.0 if i == j else 0.0 for j in range(6)] for i in range(6)]
    commands = [
        {
            "packet_sequence": 10,
            "reference_phase": "path",
            "reference_time_s": 4.999,
            "published_monotonic_s": 100.0,
            "published_packet_qdot_rad_s": [1, 2, 3, 4, 5, 6],
            "actual_qd_rad_s": [1, 2, 3, 4, 5, 6],
            "jacobian_6x6": identity,
        },
        {
            "packet_sequence": 11,
            "reference_phase": "path",
            "reference_time_s": 5.0,
            "published_monotonic_s": 101.0,
            "published_packet_qdot_rad_s": [2, 3, 4, 5, 6, 7],
            "actual_qd_rad_s": [2, 3, 4, 5, 6, 7],
            "jacobian_6x6": identity,
        },
    ]
    frames = [
        {
            "consumed_packet_sequence": 10,
            "received_monotonic_s": 100.002,
            "target_qd_rad_s": [1, 2, 3, 4, 5, 6],
            "qd_rad_s": [1, 2, 3, 4, 5, 6],
            "tcp_speed_m_s_rad_s": [1, 2, 3, 4, 5, 6],
        },
        {
            "consumed_packet_sequence": 10,
            "received_monotonic_s": 100.004,
            "target_qd_rad_s": [99] * 6,
            "qd_rad_s": [99] * 6,
            "tcp_speed_m_s_rad_s": [99] * 6,
        },
        {
            "consumed_packet_sequence": 11,
            "received_monotonic_s": 101.002,
            "target_qd_rad_s": [2, 3, 4, 5, 6, 7],
            "qd_rad_s": [2, 3, 4, 5, 6, 7],
            "tcp_speed_m_s_rad_s": [2, 3, 4, 5, 6, 7],
        },
    ]
    _write_jsonl(attempt / "command_timeline.jsonl", commands)
    _write_jsonl(attempt / "robot_frames.jsonl", frames)
    segments = {}
    for key in ("command_timeline", "robot_frames"):
        path = attempt / f"{key}.jsonl"
        segments[key] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (attempt / "seal.json").write_text(json.dumps({"segments": segments}), encoding="utf-8")
    (attempt / "attempt-result.json").write_text(json.dumps({
        "evidence_eligible": False,
        "parameter_binding": {"protocol_id": "figure8_window60_r013_rate460_v1"},
        "evidence": {"mae_n": 1.0, "qd_correlation": 0.8},
        "lifecycle": {"path_complete": True, "home_verified": True,
                      "ready_for_next": True, "sealed": True},
    }), encoding="utf-8")
    return attempt


def test_pair_metrics_reports_zero_error_and_unit_correlation() -> None:
    rows = [(0.0, 1.0), (1.0, 3.0), (2.0, 5.0)]
    result = pair_metrics(rows, rows)
    assert result["pearson_r_flat"] == pytest.approx(1.0)
    assert result["rmse_flat"] == pytest.approx(0.0)
    assert result["p95_vector_error_norm"] == pytest.approx(0.0)
    assert result["per_axis"][0]["expected_sample_std"] == pytest.approx(1.0)
    assert result["per_axis"][0]["observed_sample_std"] == pytest.approx(1.0)


def test_analysis_uses_first_exact_echo_and_separate_formal_window(tmp_path: Path) -> None:
    result = analyze_attempt("fixture", _attempt(tmp_path))
    assert result["join"]["matched_consumed_sequences"] == 2
    assert result["join"]["repeated_echo_frames_ignored"] == 1
    assert result["join"]["packet_offsets_applied"] == 0
    assert result["windows"]["path_0_60_s"]["samples"] == 2
    assert result["windows"]["formal_5_60_s"]["samples"] == 1
    cart = result["windows"]["path_0_60_s"]["cartesian_velocity"]
    assert cart["J_command_vs_RTDE_actual_TCP_speed"]["linear_3d"]["rmse_flat"] == pytest.approx(0.0)


def test_analysis_rejects_mutated_sealed_trace(tmp_path: Path) -> None:
    attempt = _attempt(tmp_path)
    with (attempt / "command_timeline.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{}\n")
    with pytest.raises(AnalysisError, match="sealed SHA mismatch"):
        analyze_attempt("fixture", attempt)
