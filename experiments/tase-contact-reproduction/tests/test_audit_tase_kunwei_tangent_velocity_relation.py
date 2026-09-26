from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from audit_tase_kunwei_tangent_velocity_relation import (  # noqa: E402
    AuditError,
    _leave_one_attempt_out_velocity_fit,
    _r_squared,
    _velocity_agreement,
    tangent_vectors_base,
)


def test_tangent_vectors_rotate_sensor_force_and_remove_normal_components():
    force, velocity = tangent_vectors_base(
        force_tcp_n=(1.0, 0.0, 8.0),
        tcp_pose_m_rad=(0.0, 0.0, 0.0, 0.0, 0.0, np.pi / 2.0),
        velocity_base_m_s=(0.0, -0.004, 0.5),
        normal_base=(0.0, 0.0, 1.0),
    )
    assert force == pytest.approx((0.0, 1.0, 0.0), abs=1e-12)
    assert velocity == pytest.approx((0.0, -0.004, 0.0), abs=1e-12)


def test_tangent_vectors_reject_degenerate_normal():
    with pytest.raises(AuditError, match="degenerate"):
        tangent_vectors_base(
            force_tcp_n=(1.0, 0.0, 0.0),
            tcp_pose_m_rad=(0.0,) * 6,
            velocity_base_m_s=(0.0, 1.0, 0.0),
            normal_base=(0.0, 0.0, 0.0),
        )


def test_velocity_regression_recovers_known_signed_damping_relation():
    velocity = np.linspace(-0.01, 0.01, 101)
    features = np.column_stack((velocity, velocity**2))
    response = np.column_stack((0.2 - 20.0 * velocity, -0.1 - 5.0 * velocity**2))
    coefficients, r_squared = _r_squared(features, response)
    assert coefficients[0] == pytest.approx((0.2, -0.1))
    assert coefficients[1:, 0] == pytest.approx((-20.0, 0.0))
    assert coefficients[1:, 1] == pytest.approx((0.0, -5.0))
    assert r_squared == pytest.approx((1.0, 1.0))


def test_velocity_agreement_checks_paired_direction_and_scale():
    result = _velocity_agreement(
        np.asarray(((1.0, 0.0), (0.0, 2.0))),
        np.asarray(((2.0, 0.0), (0.0, 1.0))),
    )
    assert result["valid_bins"] == 2
    assert result["positive_direction_fraction"] == pytest.approx(1.0)
    assert result["mean_cosine"] == pytest.approx(1.0)
    assert result["median_speed_ratio"] == pytest.approx(1.25)


def test_leave_one_attempt_out_model_preserves_repeatable_velocity_component():
    velocity = np.column_stack((np.linspace(-0.01, 0.01, 20), np.linspace(0.01, -0.01, 20)))
    force = np.column_stack((0.2 - 30.0 * velocity[:, 0], -0.1 - 15.0 * velocity[:, 1]))
    attempts = [
        {"force_bins_xy_n": force.tolist(), "actual_velocity_bins_xy_m_s": velocity.tolist()}
        for _ in range(3)
    ]
    folds = _leave_one_attempt_out_velocity_fit(attempts)
    assert len(folds) == 3
    assert all(row["held_out_bins"] == 20 for row in folds)
    assert all(row["velocity_model_rmse_n"] < row["constant_force_model_rmse_n"] for row in folds)
    assert all(row["velocity_model_reduction_vs_constant_fraction"] > 0.99 for row in folds)
