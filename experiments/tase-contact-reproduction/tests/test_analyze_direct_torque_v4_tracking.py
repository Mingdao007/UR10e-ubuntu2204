from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from analyze_direct_torque_v4_tracking import analyze_rows  # noqa: E402


def _row(
    index: int,
    *,
    desired_x_m: float,
    actual_x_m: float,
    actual_y_m: float,
) -> dict[str, str]:
    row = {
        "receiver_state": "2",
        "controller_timestamp_s": f"{index * 0.002:.6f}",
    }
    for axis in range(3):
        row[f"command_desired_pose_{axis}"] = str(
            desired_x_m if axis == 0 else 0.0
        )
        row[f"actual_TCP_pose_{axis}"] = str(
            actual_x_m if axis == 0 else actual_y_m if axis == 1 else 0.0
        )
        row[f"actual_TCP_speed_{axis}"] = "0.0"
        row[f"applied_k_{axis}"] = "600.0"
    for axis in range(6):
        row[f"commanded_joint_torque_nm_{axis}"] = "0.01"
        row[f"actual_qd_{axis}"] = "0.0"
    return row


def test_directional_gain_recovers_commanded_response() -> None:
    rows = [
        _row(
            index,
            desired_x_m=(index / 19.0) ** 2 * 1e-4,
            actual_x_m=0.5 * (index / 19.0) ** 2 * 1e-4,
            actual_y_m=0.0,
        )
        for index in range(20)
    ]
    result = analyze_rows(rows)
    assert result["directional"]["through_origin_gain"] == pytest.approx(0.5)
    assert result["directional"]["intercept_fit_gain"] == pytest.approx(0.5)
    assert result["directional"]["command_actual_correlation"] == pytest.approx(1.0)
    assert result["directional"]["directional_tracking_supported"] is True
    assert result["coherent_response_fit"]["alpha"] == pytest.approx(0.5)


def test_norm_ratio_does_not_misclassify_orthogonal_drift_as_tracking() -> None:
    rows = [
        _row(
            index,
            desired_x_m=index * 1e-5,
            actual_x_m=0.0,
            actual_y_m=index * 1e-5,
        )
        for index in range(20)
    ]
    result = analyze_rows(rows)
    assert result["envelope"]["legacy_max_norm_response_ratio"] == pytest.approx(1.0)
    assert result["directional"]["through_origin_gain"] == pytest.approx(0.0)
    assert result["directional"]["command_actual_correlation"] == pytest.approx(0.0)
    assert result["directional"]["directional_tracking_supported"] is False
