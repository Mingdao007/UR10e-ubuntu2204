from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from audit_tase_force_control_signal_selection import summarize_window  # noqa: E402


def test_control_max_fusion_and_metric_sign_disagreement_are_counted():
    rows = [
        {
            "reference_time_s": 0.01,
            "force_target_n": 5.0,
            "normal_force_filtered_n": 4.0,
            "normal_force_measured_n": 6.0,
            "normal_force_control_n": 6.0,
            "force_error_n": -1.0,
        },
        {
            "reference_time_s": 0.11,
            "force_target_n": 5.0,
            "normal_force_filtered_n": 6.0,
            "normal_force_measured_n": 4.0,
            "normal_force_control_n": 6.0,
            "force_error_n": -1.0,
        },
    ]

    result = summarize_window(rows, start_s=0.0, end_s=0.2)

    assert result["complete_bin_coverage"] is True
    assert result["signal_selection"]["measured_dominates_filtered_sample_fraction"] == pytest.approx(0.5)
    assert result["signal_selection"]["control_rule_violation_samples"] == 0
    assert result["signal_selection"]["force_error_vs_target_minus_control_max_abs_difference_n"] == pytest.approx(0.0)
    assert result["signal_selection"]["official_filtered_under_target_but_control_over_target_fraction"] == pytest.approx(0.5)
    assert result["signal_selection"]["filtered_and_control_error_sign_disagreement_fraction"] == pytest.approx(0.5)
    assert result["signals"]["filtered_normal_force_n"]["sample_mae_n"] == pytest.approx(1.0)
    assert result["signals"]["control_normal_force_n"]["sample_mae_n"] == pytest.approx(1.0)


def test_control_selection_audit_rejects_inconsistent_fusion():
    rows = [{
        "reference_time_s": 0.01,
        "force_target_n": 5.0,
        "normal_force_filtered_n": 4.0,
        "normal_force_measured_n": 6.0,
        "normal_force_control_n": 4.0,
        "force_error_n": 1.0,
    }]
    result = summarize_window(rows, start_s=0.0, end_s=0.1)
    assert result["signal_selection"]["control_rule_violation_samples"] == 1
