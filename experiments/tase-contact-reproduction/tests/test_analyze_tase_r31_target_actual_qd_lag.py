from __future__ import annotations

import pytest

from analyze_tase_r31_target_actual_qd_lag import (
    QdLagAnalysisError,
    scan_target_actual_offsets,
)


def test_positive_offset_means_actual_qd_follows_target_qd():
    signal = [
        [0.13, -0.27, 0.41, -0.18, 0.32, -0.09],
        [-0.31, 0.17, -0.12, 0.44, -0.23, 0.08],
        [0.22, 0.39, -0.35, 0.11, -0.06, 0.47],
        [-0.48, -0.14, 0.29, -0.33, 0.19, 0.25],
        [0.36, -0.42, 0.07, 0.28, -0.45, -0.21],
        [-0.16, 0.48, 0.23, -0.04, 0.38, -0.37],
        [0.43, -0.05, -0.46, 0.21, 0.14, 0.31],
        [-0.26, 0.34, 0.15, -0.49, -0.11, 0.02],
    ]
    target = signal
    actual = [signal[0], *signal[:-1]]
    result = scan_target_actual_offsets(
        target, actual, [i * 0.0021 for i in range(len(signal))], max_offset_rows=2
    )
    assert result["same_row"]["flattened_pearson"] < 1.0
    assert result["best_flattened_scan"]["row_offset"] == 1
    assert result["best_flattened_scan"]["flattened_pearson"] == pytest.approx(1.0)
    assert result["best_flattened_scan"]["median_controller_time_offset_s"] == pytest.approx(0.0021)


def test_offset_scan_rejects_nonmonotonic_controller_time():
    rows = [[float(i + j) for j in range(6)] for i in range(3)]
    with pytest.raises(QdLagAnalysisError, match="strictly increasing"):
        scan_target_actual_offsets(rows, rows, [1.0, 1.002, 1.001])
