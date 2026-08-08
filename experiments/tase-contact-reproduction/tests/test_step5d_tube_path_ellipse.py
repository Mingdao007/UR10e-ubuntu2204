"""Unit tests for PATH-frame XY ellipse helpers (offline, pure geometry)."""

from __future__ import annotations

import math

import pytest

from ur10e_experiment_runtime.tube.path_ellipse import grad_phi, phi


def test_a_equals_b_is_isotropic_circle_sdf() -> None:
    radius_m = 0.030
    points = (
        (0.010, 0.0),
        (0.0, 0.010),
        (0.010, 0.010),
        (-0.020, 0.015),
        (0.040, -0.010),
    )
    for u_m, v_m in points:
        expected = math.hypot(u_m, v_m) - radius_m
        assert phi(u_m, v_m, radius_m, radius_m) == pytest.approx(expected)
        # Rotating the query point must not change the field when a == b.
        assert phi(v_m, -u_m, radius_m, radius_m) == pytest.approx(expected)


def test_inside_negative_outside_positive() -> None:
    a_m = 0.040
    b_m = 0.020
    assert phi(0.0, 0.0, a_m, b_m) < 0.0
    assert phi(0.010, 0.005, a_m, b_m) < 0.0
    assert phi(a_m, 0.0, a_m, b_m) == pytest.approx(0.0)
    assert phi(0.0, b_m, a_m, b_m) == pytest.approx(0.0)
    assert phi(0.050, 0.0, a_m, b_m) > 0.0
    assert phi(0.0, 0.030, a_m, b_m) > 0.0
    assert phi(0.040, 0.020, a_m, b_m) > 0.0


def test_grad_phi_points_outward() -> None:
    a_m = 0.040
    b_m = 0.020
    samples = (
        (0.010, 0.0),
        (0.0, 0.010),
        (0.020, 0.010),
        (-0.015, 0.008),
        (0.050, -0.010),
    )
    for u_m, v_m in samples:
        gu, gv = grad_phi(u_m, v_m, a_m, b_m)
        # Directional derivative along the radius vector must be positive.
        assert gu * u_m + gv * v_m > 0.0
        eps = 1e-7
        numerical_u = (
            phi(u_m + eps, v_m, a_m, b_m) - phi(u_m - eps, v_m, a_m, b_m)
        ) / (2.0 * eps)
        numerical_v = (
            phi(u_m, v_m + eps, a_m, b_m) - phi(u_m, v_m - eps, a_m, b_m)
        ) / (2.0 * eps)
        assert gu == pytest.approx(numerical_u, abs=1e-6)
        assert gv == pytest.approx(numerical_v, abs=1e-6)
