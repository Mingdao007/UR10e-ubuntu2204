import numpy as np
import pytest

from analyze_tase_normal_axis_effective_mass import (
    EffectiveMassError,
    normal_effective_mass_metrics,
)


def test_axis_mass_agrees_when_other_task_axes_do_not_couple():
    mass = np.diag([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    jacobian = np.diag([2.0, 3.0, 4.0, 5.0, 6.0, 7.0])

    result = normal_effective_mass_metrics(mass, jacobian)

    assert result["effective_mass_normal_free_other_task_axes_kg"] == pytest.approx(3.0 / 16.0)
    assert result["effective_mass_normal_with_tangent_and_orientation_locked_kg"] == pytest.approx(3.0 / 16.0)


def test_free_and_locked_task_assumptions_are_distinct_when_jacobian_couples_axes():
    mass = np.eye(6)
    jacobian = np.eye(6)
    jacobian[0, 2] = 0.5
    jacobian[2, 0] = 0.5

    result = normal_effective_mass_metrics(mass, jacobian)

    assert result["effective_mass_normal_free_other_task_axes_kg"] == pytest.approx(0.8)
    assert result["effective_mass_normal_with_tangent_and_orientation_locked_kg"] == pytest.approx(20.0 / 9.0)


def test_effective_mass_rejects_non_positive_or_singular_model_inputs():
    jacobian = np.eye(6)
    with pytest.raises(EffectiveMassError, match="positive definite"):
        normal_effective_mass_metrics(np.diag([1.0, 1.0, -1.0, 1.0, 1.0, 1.0]), jacobian)
    with pytest.raises(EffectiveMassError, match="rank deficient"):
        normal_effective_mass_metrics(np.eye(6), np.diag([1.0, 1.0, 1.0, 1.0, 1.0, 0.0]))
