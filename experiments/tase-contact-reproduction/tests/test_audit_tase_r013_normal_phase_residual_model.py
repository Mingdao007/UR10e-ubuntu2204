from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from audit_tase_r013_normal_phase_residual_model import (  # noqa: E402
    PhaseResidualAuditError,
    _fit_zero_intercept,
    phase_demean,
)


def test_phase_demean_removes_shared_surface_profile_per_bin():
    shared = np.linspace(2.0, 4.0, 30)
    offsets = np.asarray((-0.2, -0.1, 0.1, 0.2))[:, None]
    result = phase_demean(shared[None, :] + offsets)
    assert result.mean(axis=0) == pytest.approx(np.zeros(30))
    assert result[0] == pytest.approx(-0.2)


def test_zero_intercept_fit_recovers_known_phase_residual_model():
    rng = np.random.default_rng(91)
    position = phase_demean(rng.normal(size=(5, 100)))
    velocity = phase_demean(rng.normal(size=(5, 100)))
    acceleration = phase_demean(rng.normal(size=(5, 100)))
    force = phase_demean(140.0 * position + 3.0 * velocity - 2.0 * acceleration)
    result = _fit_zero_intercept([position, velocity, acceleration], force)
    assert result["coefficients_physical_units"] == pytest.approx((140.0, 3.0, -2.0))
    assert result["r_squared_centered"] == pytest.approx(1.0)


def test_fit_rejects_features_without_between_attempt_variation():
    force = np.ones((3, 20))
    feature = np.ones((3, 20))
    with pytest.raises(PhaseResidualAuditError, match="no between-attempt variation"):
        _fit_zero_intercept([feature], force)
