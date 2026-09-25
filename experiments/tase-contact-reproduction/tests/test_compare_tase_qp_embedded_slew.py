import numpy as np
import pytest

from compare_tase_qp_embedded_slew import embedded_slew_bounds, confirm_linear_feasibility


def test_embedded_slew_intersects_recorded_qdot_box_per_joint():
    lower, upper = embedded_slew_bounds(
        [-0.05] * 6,
        [0.05] * 6,
        [0.02, -0.03, 0.0, 0.0, 0.0, 0.0],
        0.01,
    )
    np.testing.assert_allclose(lower, [0.01, -0.04, -0.01, -0.01, -0.01, -0.01])
    np.testing.assert_allclose(upper, [0.03, -0.02, 0.01, 0.01, 0.01, 0.01])


def test_embedded_slew_keeps_existing_joint_bounds():
    lower, upper = embedded_slew_bounds(
        [-0.005, -0.05, -0.05, -0.05, -0.05, -0.05],
        [0.005, 0.05, 0.05, 0.05, 0.05, 0.05],
        [0.0] * 6,
        0.02,
    )
    assert lower[0] == -0.005
    assert upper[0] == 0.005


def test_exact_task_and_embedded_slew_box_feasibility_is_checked_independently():
    feasible = confirm_linear_feasibility(np.eye(6), [0.1] * 6, [-0.2] * 6, [0.2] * 6)
    infeasible = confirm_linear_feasibility(np.eye(6), [0.3] * 6, [-0.2] * 6, [0.2] * 6)
    assert feasible["success"] is True
    assert infeasible["success"] is False
    assert infeasible["status"] == 2


@pytest.mark.parametrize("limit", [-1.0, float("nan"), float("inf")])
def test_embedded_slew_rejects_invalid_limit(limit):
    with pytest.raises(ValueError):
        embedded_slew_bounds([-1] * 6, [1] * 6, [0] * 6, limit)
