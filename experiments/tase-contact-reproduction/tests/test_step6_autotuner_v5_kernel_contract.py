from __future__ import annotations

import pytest


def test_v5_design_bounds_are_fixed_six_dimensional_and_not_minmax() -> None:
    from step6_figure8_autotune_v1.optimizer_worker import (
        _feature_bounds_for_block,
        _feature_bounds_receipt,
    )

    bounds = _feature_bounds_for_block("controller_path")
    assert len(bounds) == 6
    assert all(lower < upper for lower, upper in bounds)
    receipt = _feature_bounds_receipt("controller_path")
    assert receipt["dimensions"] == 6
    assert receipt["normalization"] == "fixed_design_bounds_not_observation_minmax"
    assert len(receipt["feature_bounds_log2"]) == 6

    correction = _feature_bounds_receipt("correction")
    assert correction["normalization"] == "fixed_correction_design_bounds_not_observation_minmax"
    assert correction["feature_bounds_log2"] == [[-0.5, 0.5]] * 6


def test_kernel_challenger_requires_strict_nlpd_and_nonworse_safety_metrics() -> None:
    from step6_figure8_autotune_v1.v5_kernel_selection import select_kernel

    incumbent = {
        "nlpd": 1.0,
        "coverage": 0.90,
        "low_tail_recall": 0.80,
        "false_optimism": 0.10,
        "replay_regret": 0.20,
    }
    challengers = {
        "rbf_ard": {**incumbent, "nlpd": 0.9},
        "matern32_ard": {**incumbent, "nlpd": 0.8, "coverage": 0.89},
    }
    receipt = select_kernel(incumbent_metrics=incumbent, challenger_metrics=challengers)
    assert receipt.selected_kernel == "rbf_ard"
    assert receipt.decision == "freeze_challenger"
    assert receipt.calibration_observation_count == 24


def test_kernel_challenger_is_rejected_when_nlpd_does_not_improve() -> None:
    from step6_figure8_autotune_v1.v5_kernel_selection import select_kernel

    metrics = {
        "nlpd": 1.0,
        "coverage": 0.90,
        "low_tail_recall": 0.80,
        "false_optimism": 0.10,
        "replay_regret": 0.20,
    }
    receipt = select_kernel(
        incumbent_metrics=metrics,
        challenger_metrics={"rbf_ard": metrics, "matern32_ard": metrics},
    )
    assert receipt.selected_kernel == "matern52_ard"
    assert receipt.decision == "retain_incumbent"


def test_kernel_selection_rejects_wrong_calibration_size() -> None:
    from step6_figure8_autotune_v1.v5_kernel_selection import select_kernel

    metrics = {
        "nlpd": 1.0,
        "coverage": 0.90,
        "low_tail_recall": 0.80,
        "false_optimism": 0.10,
        "replay_regret": 0.20,
    }
    with pytest.raises(ValueError, match="exactly 24"):
        select_kernel(
            incumbent_metrics=metrics,
            challenger_metrics={"rbf_ard": metrics, "matern32_ard": metrics},
            calibration_observation_count=23,
        )
