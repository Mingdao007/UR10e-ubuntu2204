from pathlib import Path
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from tase_qp_tangent_slack_offline import (
    TaseTangentSlackQpError,
    solve_tase_tangent_slack_qp,
)


@pytest.fixture
def qp_case():
    return {
        "jacobian_6x6": np.eye(6),
        "desired_twist_m_s_rad_s": (0.10, -0.04, -0.02, 0.003, 0.004, -0.006),
        "reaction_normal_base": (0.0, 0.0, 1.0),
        "tangent_basis_base_3x2": ((1.0, 0.0), (0.0, 1.0), (0.0, 0.0)),
        "previous_qdot_rad_s": (0.10, -0.04, -0.02, 0.003, 0.004, -0.006),
        "joint_velocity_lower_rad_s": (-0.2,) * 6,
        "joint_velocity_upper_rad_s": (0.2,) * 6,
        "slew_velocity_lower_rad_s": (-0.2,) * 6,
        "slew_velocity_upper_rad_s": (0.2,) * 6,
    }


def test_slack_bound_is_required_and_cannot_be_negative_or_nonfinite(qp_case):
    with pytest.raises(TypeError):
        solve_tase_tangent_slack_qp(**qp_case)
    for value in (-1e-6, float("inf"), float("nan")):
        with pytest.raises(TaseTangentSlackQpError, match="slack_limit"):
            solve_tase_tangent_slack_qp(**qp_case, tangent_slack_limit_m_s=value)


def test_zero_slack_recovers_strict_six_dimensional_solution(qp_case):
    result = solve_tase_tangent_slack_qp(**qp_case, tangent_slack_limit_m_s=0.0)

    np.testing.assert_allclose(result.qdot_rad_s, qp_case["desired_twist_m_s_rad_s"], atol=1e-7)
    assert result.normal_residual_m_s == pytest.approx(0.0, abs=1e-7)
    np.testing.assert_allclose(result.orientation_residual_rad_s, 0.0, atol=1e-7)
    np.testing.assert_allclose(result.tangential_slack_m_s, 0.0, atol=1e-7)
    assert result.offline_only is True
    assert result.live_eligible is False


def test_within_slack_previous_command_is_selected_for_smoothness(qp_case):
    qp_case["previous_qdot_rad_s"] = (0.09, -0.03, -0.02, 0.003, 0.004, -0.006)
    result = solve_tase_tangent_slack_qp(**qp_case, tangent_slack_limit_m_s=0.02)

    np.testing.assert_allclose(result.qdot_rad_s, qp_case["previous_qdot_rad_s"], atol=2e-7)
    assert max(abs(value) for value in result.tangential_slack_m_s) <= 0.02 + 1e-7


def test_tight_slack_changes_only_tangent_components(qp_case):
    qp_case["previous_qdot_rad_s"] = (0.09, -0.03, -0.02, -0.03, 0.02, -0.01)
    result = solve_tase_tangent_slack_qp(**qp_case, tangent_slack_limit_m_s=0.005)
    qdot = np.asarray(result.qdot_rad_s)
    desired = np.asarray(qp_case["desired_twist_m_s_rad_s"])

    np.testing.assert_allclose(qdot[2:], desired[2:], atol=1e-7)
    assert np.max(np.abs(qdot[:2] - desired[:2])) <= 0.005 + 1e-7
    assert np.max(np.abs(result.tangential_slack_m_s)) <= 0.005 + 1e-7


def test_hard_velocity_bounds_are_intersected_with_slew_bounds(qp_case):
    qp_case["previous_qdot_rad_s"] = (0.0,) * 6
    qp_case["joint_velocity_lower_rad_s"] = (-0.15,) * 6
    qp_case["joint_velocity_upper_rad_s"] = (0.15,) * 6
    qp_case["slew_velocity_lower_rad_s"] = (-0.08,) * 6
    qp_case["slew_velocity_upper_rad_s"] = (0.08,) * 6
    result = solve_tase_tangent_slack_qp(**qp_case, tangent_slack_limit_m_s=0.05)

    np.testing.assert_allclose(result.effective_lower_rad_s, (-0.08,) * 6)
    np.testing.assert_allclose(result.effective_upper_rad_s, (0.08,) * 6)
    assert max(abs(value) for value in result.qdot_rad_s) <= 0.08 + 1e-7


def test_infeasible_hard_normal_task_is_rejected(qp_case):
    qp_case["desired_twist_m_s_rad_s"] = (0.0, 0.0, -0.03, 0.0, 0.0, 0.0)
    qp_case["joint_velocity_lower_rad_s"] = (-0.02,) * 6
    qp_case["joint_velocity_upper_rad_s"] = (0.02,) * 6
    qp_case["slew_velocity_lower_rad_s"] = (-0.02,) * 6
    qp_case["slew_velocity_upper_rad_s"] = (0.02,) * 6

    with pytest.raises(TaseTangentSlackQpError, match="rejected"):
        solve_tase_tangent_slack_qp(**qp_case, tangent_slack_limit_m_s=0.1)
