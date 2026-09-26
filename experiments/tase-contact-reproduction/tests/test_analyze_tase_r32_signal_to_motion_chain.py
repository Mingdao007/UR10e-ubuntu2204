from __future__ import annotations

import numpy as np
import pytest

from analyze_tase_r32_signal_to_motion_chain import _lag_scan, _task_residual_components


def test_lag_scan_positive_lag_means_readback_after_command() -> None:
    rng = np.random.default_rng(712)
    command = rng.normal(size=256)
    lag = 4
    measured = np.zeros_like(command)
    measured[:lag] = rng.normal(size=lag)
    measured[lag:] = command[:-lag]
    host = np.arange(command.size, dtype=float) * 0.002
    published = host - 0.0004

    result = _lag_scan(command, measured, host, published, max_lag=12)

    assert result["best_positive_correlation_lag_samples"] == lag
    assert result["best_lag_correlation"] > 0.999999
    assert np.isclose(result["best_lag_median_publish_to_RTDE_sample_ms"], 8.4)


def test_lag_scan_rejects_trace_shorter_than_requested_scan() -> None:
    samples = np.arange(20, dtype=float)
    with pytest.raises(ValueError, match="not enough samples"):
        _lag_scan(samples, samples, samples, samples, max_lag=30)


def test_task_residual_is_split_into_approach_tangent_and_angular_parts() -> None:
    task = np.array([0.002, 0.001, -0.003, 0.01, 0.0, 0.0])
    achieved = np.array([0.0015, 0.001, -0.001, 0.009, 0.002, 0.0])
    approach = np.array([0.0, 0.0, -1.0])

    normal, tangent, angular = _task_residual_components(task, achieved, approach)

    assert np.isclose(normal, 0.002)
    assert np.isclose(tangent, 0.0005)
    assert np.isclose(angular, np.sqrt(0.001**2 + 0.002**2))
