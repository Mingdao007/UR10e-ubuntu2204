from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from replay_tase_force_feedback_selection import (  # noqa: E402
    LOWER_ALLOWED_FILTER_TAU_S,
    ReplayError,
    _filter_measured_normal,
    _filter_recurrence_summary,
    _qdot_window_summary,
    _window_summary,
)
from step5d_autotune_v4_r005.contracts import DOMAIN_EXPONENTS, TAU_ANCHOR  # noqa: E402


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


def test_counterfactual_filter_uses_actual_dt_and_explicit_initial_state() -> None:
    rows = [
        {"actual_dt_s": 0.002, "normal_force_measured_n": 1.0},
        {"actual_dt_s": 0.004, "normal_force_measured_n": 3.0},
    ]
    tau_s = 0.2
    result = _filter_measured_normal(rows, tau_s=tau_s, initial_state_n=2.0)
    expected = 2.0 + -np.expm1(-0.004 / tau_s) * (3.0 - 2.0)

    assert result.tolist() == pytest.approx([2.0, expected])


def test_lower_tau_candidate_is_existing_contract_lattice_boundary() -> None:
    expected = TAU_ANCHOR * 2.0 ** min(DOMAIN_EXPONENTS["tau"])
    assert LOWER_ALLOWED_FILTER_TAU_S == pytest.approx(expected)


def test_rnn_qdot_window_summary_reports_limits_separately() -> None:
    rows = [{"reference_time_s": 0.01}, {"reference_time_s": 0.11}]
    baseline = {
        "final_qdots": np.zeros((2, 6)),
        "pre_gate_tasks": np.zeros((2, 6)),
        "tasks": np.zeros((2, 6)),
        "host_slew_scales": np.ones(2),
        "final_gate_scales": np.ones(2),
        "post_gate_slew_violations": np.zeros(2, dtype=bool),
        "post_gate_task_cap_violations": np.zeros(2, dtype=bool),
        "bound_violations": np.zeros(2, dtype=bool),
        "task_cap_violations": np.zeros(2, dtype=bool),
        "task_cap_rows": {
            "normal": np.zeros(2, dtype=bool),
            "tangential": np.zeros(2, dtype=bool),
            "total_linear": np.zeros(2, dtype=bool),
            "angular": np.zeros(2, dtype=bool),
        },
    }
    candidate = {
        "final_qdots": np.asarray([[0.001, 0, 0, 0, 0, 0], [0.002, 0, 0, 0, 0, 0]]),
        "pre_gate_tasks": np.asarray([[0.002, 0, 0, 0, 0, 0], [0.003, 0, 0, 0, 0, 0]]),
        "tasks": np.asarray([[0.001, 0, 0, 0, 0, 0], [0.002, 0, 0, 0, 0, 0]]),
        "host_slew_scales": np.asarray([0.5, 1.0]),
        "final_gate_scales": np.asarray([0.5, 1.0]),
        "post_gate_slew_violations": np.asarray([False, False]),
        "post_gate_task_cap_violations": np.asarray([False, False]),
        "bound_violations": np.asarray([False, True]),
        "task_cap_violations": np.asarray([True, False]),
        "task_cap_rows": {
            "normal": np.asarray([True, False]),
            "tangential": np.asarray([False, False]),
            "total_linear": np.asarray([False, False]),
            "angular": np.asarray([False, False]),
        },
    }

    result = _qdot_window_summary(
        rows, baseline, candidate, start_s=0.0, end_s=0.2,
    )

    assert result["sample_count"] == 2
    assert result["candidate_joint_bound_violation_rows"] == 1
    assert result["candidate_task_cap_pre_gate_rows"] == 1
    assert result["candidate_task_cap_pre_gate_violations_by_axis"]["normal"] == 1
    assert result["candidate_post_gate_task_cap_violation_rows"] == 0
    assert result["candidate_host_slew_limited_rows"] == 1
    assert result["candidate_final_gate_projection_rows"] == 1
    assert result["candidate_post_gate_slew_violation_rows"] == 0
    assert result["candidate_minus_fused_qdot_delta_rad_s"]["max_vector_norm"] == pytest.approx(0.002)
