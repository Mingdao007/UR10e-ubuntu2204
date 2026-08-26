from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_step5d_generalized_force_offline_study import (  # noqa: E402
    audit_trace,
    phase_leakage_reasons,
    sealed_style_metric,
)


def _row(attempt: int, index: int, *, leaked: bool = False) -> dict:
    return {
        "schema": "step5d.autotune-v4/r008-state25-path-trace-v1",
        "attempt_ordinal": attempt,
        "monotonic_s": attempt * 100.0 + index * 0.1,
        "filtered_normal_n": 5.0,
        "normal_load_n": 5.0,
        "force_error_n": 0.0,
        "tcp_pose_m_rad": [0.0, 0.0, index * 1e-5, 0.0, 0.0, 0.0],
        "integral_saturated": False,
        "runtime_strategy_enabled": leaked,
        "phase_target_correction_n": 0.0,
    }


def test_sealed_style_metric_has_550_gap_preserving_bins() -> None:
    rows = [_row(1, index) for index in range(610)]
    result = sealed_style_metric(rows)
    assert result["status"] == "sealed"
    assert result["complete_bin_count"] == 550
    assert result["formal_mae_n"] == 0.0
    assert result["authoritative"] is False


def test_audit_excludes_an_entire_phase_strategy_attempt(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    with trace.open("w", encoding="utf-8") as handle:
        for attempt in range(1, 11):
            for index in range(610):
                handle.write(json.dumps(_row(attempt, index, leaked=attempt == 5)) + "\n")
    result = audit_trace(trace)
    assert result["phase_leakage_audit"]["excluded_attempts"] == {"5": ["runtime_strategy_enabled"]}
    assert 5 not in result["split"]["eligible_train_attempts"]
    assert result["identification"]["status"] == "partial_stiffness_unidentified_from_trace"


def test_unknown_phase_fields_fail_closed() -> None:
    row = _row(1, 0)
    row["runtime_strategy_enabled"] = "unexpected-encoding"
    row["phase_target_correction_n"] = "not-a-number"
    assert phase_leakage_reasons(row) == (
        "runtime_strategy_enabled",
        "invalid_phase_target_correction_n",
    )
