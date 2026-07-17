from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_autotune_v3_g10 as g10  # noqa: E402


def passing_metrics() -> dict[str, int | float]:
    return {
        "replayed_rows": 5,
        "accepted_rows": 5,
        "active_bounds_rows": 0,
        "structural_failure_rows": 0,
        "rnn_residual_max": 1e-9,
        "rnn_oracle_qdot_delta_max_rad_s": 1e-9,
        "qdot_max_abs_rad_s": 0.5,
        "slew_violation_max_rad_s": 0.0,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("structural_failure_rows", 1),
        ("rnn_oracle_qdot_delta_max_rad_s", 1.1e-6),
        ("qdot_max_abs_rad_s", 0.500000001),
        ("slew_violation_max_rad_s", 1e-9),
        ("rnn_residual_max", math.nan),
    ],
)
def test_executable_replay_thresholds_fail_closed(field: str, value: int | float) -> None:
    metrics = passing_metrics()
    metrics[field] = value
    with pytest.raises(g10.GoldenReplayError, match="executable replay differs"):
        g10.verify_executable_metrics(metrics)


def test_executable_replay_acceptance_requires_all_rows() -> None:
    metrics = passing_metrics()
    metrics["accepted_rows"] = 4
    with pytest.raises(g10.GoldenReplayError, match="executable replay differs"):
        g10.verify_executable_metrics(metrics)


def test_raw_rnn_residual_is_reported_but_not_an_unplanned_threshold() -> None:
    metrics = passing_metrics()
    metrics["rnn_residual_max"] = 0.03
    g10.verify_executable_metrics(metrics)


def test_frozen_g10_spec_digest_is_exact() -> None:
    assert g10.sha256_file(g10.GOLDEN) == g10.GOLDEN_SHA256
