from __future__ import annotations

import numpy as np
import pytest

from audit_tase_qp_hard_task_feasibility import (
    FeasibilityAuditError,
    maximum_common_hard_task_scale,
    minimum_componentwise_tangent_slack,
    minimum_normal_orientation_priority_slack,
)


REACTION = (0.0, 0.0, 1.0)
TANGENT_BASIS = ((1.0, 0.0), (0.0, 1.0), (0.0, 0.0))


def _case():
    return {
        "jacobian_6x6": np.eye(6),
        "desired_twist_m_s_rad_s": (0.01, -0.01, 0.0, 0.0, 0.0, 0.0),
        "reaction_normal_base": REACTION,
        "tangent_basis_base_3x2": TANGENT_BASIS,
        "qdot_lower_rad_s": (-0.1,) * 6,
        "qdot_upper_rad_s": (0.1,) * 6,
    }


def test_strict_tangent_task_is_feasible_with_room_in_bounds():
    result = minimum_componentwise_tangent_slack(**_case())
    assert result["feasible"] is True
    assert result["minimum_componentwise_tangent_slack_m_s"] == pytest.approx(0.0, abs=1e-8)


def test_tangent_box_limit_is_measured_without_choosing_a_bound():
    case = _case()
    case["desired_twist_m_s_rad_s"] = (0.02, 0.0, 0.0, 0.0, 0.0, 0.0)
    case["qdot_lower_rad_s"] = (0.0, -0.1, -0.1, -0.1, -0.1, -0.1)
    case["qdot_upper_rad_s"] = (0.0, 0.1, 0.1, 0.1, 0.1, 0.1)
    result = minimum_componentwise_tangent_slack(**case)
    assert result["feasible"] is True
    assert result["minimum_componentwise_tangent_slack_m_s"] == pytest.approx(0.02, abs=1e-7)


def test_tangent_slack_cannot_fix_hard_orientation_infeasibility():
    case = _case()
    case["desired_twist_m_s_rad_s"] = (0.0, 0.0, 0.0, 0.1, 0.0, 0.0)
    case["qdot_lower_rad_s"] = (0.0,) * 6
    case["qdot_upper_rad_s"] = (0.0,) * 6
    result = minimum_componentwise_tangent_slack(**case)
    assert result["feasible"] is False
    assert "infeasible" in result["solver_status"].lower()


def test_slew_box_can_break_feasible_hard_normal_task():
    case = _case()
    case["desired_twist_m_s_rad_s"] = (0.0, 0.0, -0.05, 0.0, 0.0, 0.0)
    case["qdot_lower_rad_s"] = (-0.1,) * 6
    case["qdot_upper_rad_s"] = (0.1,) * 6
    assert minimum_componentwise_tangent_slack(**case)["feasible"] is True

    case["qdot_lower_rad_s"] = (0.0,) * 6
    case["qdot_upper_rad_s"] = (0.0,) * 6
    result = minimum_componentwise_tangent_slack(**case)
    assert result["feasible"] is False
    assert "infeasible" in result["solver_status"].lower()


def test_common_hard_task_scaling_finds_maximum_feasible_fraction():
    result = maximum_common_hard_task_scale(
        jacobian_6x6=np.eye(6),
        desired_twist_m_s_rad_s=(0.0, 0.0, -0.05, 0.1, 0.0, 0.0),
        reaction_normal_base=REACTION,
        qdot_lower_rad_s=(-1.0, -1.0, -0.02, -1.0, -1.0, -1.0),
        qdot_upper_rad_s=(1.0, 1.0, 0.02, 0.03, 1.0, 1.0),
    )
    assert result["feasible"] is True
    assert result["maximum_common_scale"] == pytest.approx(0.3, abs=1e-8)


def test_common_scaling_can_be_impossible_even_at_zero():
    result = maximum_common_hard_task_scale(
        jacobian_6x6=np.eye(6),
        desired_twist_m_s_rad_s=(0.0, 0.0, -0.05, 0.1, 0.0, 0.0),
        reaction_normal_base=REACTION,
        qdot_lower_rad_s=(-0.1, -0.1, 0.01, -0.1, -0.1, -0.1),
        qdot_upper_rad_s=(0.1, 0.1, 0.02, 0.1, 0.1, 0.1),
    )
    assert result["feasible"] is False
    assert result["zero_scale_feasible"] is False


def test_minimum_priority_slack_reports_units_and_both_task_orders():
    jacobian = np.eye(6)
    jacobian[3, :] = 0.0
    jacobian[3, 2] = 1.0
    result = minimum_normal_orientation_priority_slack(
        jacobian_6x6=jacobian,
        desired_twist_m_s_rad_s=(0.0, 0.0, -0.05, 0.02, 0.0, 0.0),
        reaction_normal_base=REACTION,
        qdot_lower_rad_s=(-1.0,) * 6,
        qdot_upper_rad_s=(1.0,) * 6,
    )
    assert result["feasible"] is True
    assert result["minimum_orientation_linf_slack_rad_s_with_normal_hard"] == pytest.approx(0.07, abs=1e-8)
    assert result["minimum_normal_abs_slack_m_s_with_orientation_hard"] == pytest.approx(0.07, abs=1e-8)


def test_tangent_basis_must_be_orthonormal_and_normal_orthogonal():
    case = _case()
    case["tangent_basis_base_3x2"] = ((1.0, 0.0), (1.0, 0.0), (0.0, 1.0))
    with pytest.raises(FeasibilityAuditError, match="orthonormal"):
        minimum_componentwise_tangent_slack(**case)
