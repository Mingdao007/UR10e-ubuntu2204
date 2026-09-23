from __future__ import annotations

import numpy as np
import pytest

from audit_tase_qp_hard_task_feasibility import (
    FeasibilityAuditError,
    minimum_componentwise_tangent_slack,
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


def test_tangent_basis_must_be_orthonormal_and_normal_orthogonal():
    case = _case()
    case["tangent_basis_base_3x2"] = ((1.0, 0.0), (1.0, 0.0), (0.0, 1.0))
    with pytest.raises(FeasibilityAuditError, match="orthonormal"):
        minimum_componentwise_tangent_slack(**case)
