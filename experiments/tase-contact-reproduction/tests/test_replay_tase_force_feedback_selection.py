from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from replay_tase_force_feedback_selection import (  # noqa: E402
    ReplayError,
    _filter_recurrence_summary,
    _window_summary,
)


def test_outer_loop_command_summary_keeps_samples_bins_and_integral_separate() -> None:
    times = np.asarray([0.01, 0.05, 0.11, 0.17])
    baseline = np.zeros((4, 6))
    candidate = np.zeros((4, 6))
    baseline[:, 2] = [-0.001, -0.002, 0.001, 0.002]
    candidate[:, 2] = [-0.002, -0.003, -0.001, 0.001]
    baseline_integral = np.asarray([0.0, 0.1, 0.2, 0.3])
    candidate_integral = baseline_integral + 0.05

    result = _window_summary(
        times,
        baseline,
        candidate,
        baseline_integral,
        candidate_integral,
        start_s=0.0,
        end_s=0.2,
    )

    assert result["sample_count"] == 4
    assert result["nonempty_100ms_bins"] == 2
    assert result["complete_100ms_bin_coverage"] is True
    assert result["normal_base_z_command_delta_m_s"]["mean_candidate_minus_fused"] == pytest.approx(-0.00125)
    assert result["normal_base_z_command_delta_m_s"]["fraction_sign_reversal"] == pytest.approx(0.25)
    assert result["integral_state_delta_n_s"]["mean_absolute"] == pytest.approx(0.05)
    assert result["not_force_metric"] is True


def test_outer_loop_command_summary_rejects_empty_window() -> None:
    empty_twists = np.empty((0, 6))
    with pytest.raises(ReplayError, match="no samples"):
        _window_summary(
            [], empty_twists, empty_twists, np.asarray([]), np.asarray([]),
            start_s=0.0, end_s=0.1,
        )


def test_filter_recurrence_uses_measured_kunwei_and_actual_dt() -> None:
    tau_s = 0.35
    rows = [
        {"actual_dt_s": 0.002, "normal_force_measured_n": 0.2, "normal_force_filtered_n": 0.2},
        {"actual_dt_s": 0.003, "normal_force_measured_n": 1.0, "normal_force_filtered_n": 0.2},
        {"actual_dt_s": 0.004, "normal_force_measured_n": 2.0, "normal_force_filtered_n": 0.2},
    ]
    rows[1]["normal_force_filtered_n"] = 0.2 + -np.expm1(-0.003 / tau_s) * (1.0 - 0.2)
    rows[2]["normal_force_filtered_n"] = rows[1]["normal_force_filtered_n"] + -np.expm1(
        -0.004 / tau_s
    ) * (2.0 - rows[1]["normal_force_filtered_n"])

    result = _filter_recurrence_summary(rows, tau_s=tau_s)

    assert result["updates_compared"] == 2
    assert result["matches_one_pole_actual_dt_recurrence"] is True
    assert result["max_abs_recurrence_residual_n"] < 1e-12
