from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from tase_qp_normal_priority_offline import (  # noqa: E402
    LexicographicTaseQpError,
    solve_normal_priority_qp,
)


def _case():
    return {
        "jacobian_6x6": np.eye(6),
        "desired_twist_m_s_rad_s": (0.01, -0.02, -0.03, 0.01, 0.02, -0.01),
        "reaction_normal_base": (0.0, 0.0, 1.0),
        "tangent_basis_base_3x2": ((1.0, 0.0), (0.0, 1.0), (0.0, 0.0)),
        "previous_qdot_rad_s": (0.0,) * 6,
        "joint_velocity_lower_rad_s": (-0.1,) * 6,
        "joint_velocity_upper_rad_s": (0.1,) * 6,
        "slew_velocity_lower_rad_s": (-0.1,) * 6,
        "slew_velocity_upper_rad_s": (0.1,) * 6,
    }


def test_unconstrained_lexicographic_qp_matches_requested_twist():
    case = _case()
    result = solve_normal_priority_qp(**case)
    assert result.qdot_rad_s == pytest.approx(case["desired_twist_m_s_rad_s"], abs=1e-6)
    assert result.normal_residual_m_s == pytest.approx(0.0, abs=1e-6)
    assert result.orientation_residual_rad_s == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)
    assert result.tangential_residual_m_s == pytest.approx((0.0, 0.0), abs=1e-6)
    assert result.offline_only is True
    assert result.live_eligible is False
    alternate_rho = solve_normal_priority_qp(**case, osqp_rho=0.01)
    assert alternate_rho.qdot_rad_s == pytest.approx(result.qdot_rad_s, abs=1e-8)


def test_normal_priority_preserves_force_axis_before_orientation_and_tangent():
    case = _case()
    case["desired_twist_m_s_rad_s"] = (0.5, 0.0, -0.4, 0.3, 0.0, 0.0)
    case["joint_velocity_lower_rad_s"] = (-0.05, -0.1, -0.02, -0.03, -0.1, -0.1)
    case["joint_velocity_upper_rad_s"] = (0.05, 0.1, 0.02, 0.03, 0.1, 0.1)
    case["slew_velocity_lower_rad_s"] = (-0.1,) * 6
    case["slew_velocity_upper_rad_s"] = (0.1,) * 6
    result = solve_normal_priority_qp(**case)
    assert result.achieved_normal_m_s == pytest.approx(0.02, abs=1e-6)
    assert result.normal_residual_m_s == pytest.approx(-0.38, abs=1e-6)
    assert result.qdot_rad_s[3] == pytest.approx(0.03, abs=1e-6)
    assert result.orientation_residual_rad_s[0] == pytest.approx(-0.27, abs=1e-6)
    assert result.qdot_rad_s[0] == pytest.approx(0.05, abs=1e-6)
    assert result.tangential_residual_m_s[0] == pytest.approx(-0.45, abs=1e-6)


def test_lexicographic_qp_rejects_empty_joint_and_slew_intersection():
    case = _case()
    case["joint_velocity_lower_rad_s"] = (0.05,) * 6
    case["joint_velocity_upper_rad_s"] = (0.1,) * 6
    case["slew_velocity_lower_rad_s"] = (-0.1,) * 6
    case["slew_velocity_upper_rad_s"] = (0.04,) * 6
    with pytest.raises(LexicographicTaseQpError, match="empty intersection"):
        solve_normal_priority_qp(**case)


def test_lexicographic_qp_rejects_nonpositive_rho():
    with pytest.raises(LexicographicTaseQpError, match="osqp_rho"):
        solve_normal_priority_qp(**_case(), osqp_rho=0.0)
