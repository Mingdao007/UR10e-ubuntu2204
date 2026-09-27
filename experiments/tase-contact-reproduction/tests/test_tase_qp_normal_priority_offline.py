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
from replay_tase_qp_normal_priority import _fixed_observation_actual_qd_proxy  # noqa: E402


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
    unscaled = solve_normal_priority_qp(**case, osqp_scaling=0)
    assert unscaled.qdot_rad_s == pytest.approx(result.qdot_rad_s, abs=1e-8)


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


def test_lexicographic_qp_rejects_negative_osqp_scaling():
    with pytest.raises(LexicographicTaseQpError, match="osqp_scaling"):
        solve_normal_priority_qp(**_case(), osqp_scaling=-1)


def test_actual_qd_proxy_uses_full_consumed_path_and_never_claims_live_gate():
    first = [float(value) for value in range(1, 7)]
    second = [-value for value in first]
    commands = {
        10: {"reference_phase": "path", "packet_sequence": 10,
             "published_packet_qdot_rad_s": first},
        11: {"reference_phase": "path", "packet_sequence": 11,
             "published_packet_qdot_rad_s": second},
    }
    frames = [
        {"integer_echoes": {"26": "25"}, "timestamp": 1.0,
         "consumed_packet_sequence": 10, "qd_rad_s": [0.9 * value for value in first]},
        {"integer_echoes": {"26": "25"}, "timestamp": 2.0,
         "consumed_packet_sequence": 11, "qd_rad_s": [0.9 * value for value in second]},
        {"integer_echoes": {"26": "25"}, "timestamp": 2.0,
         "consumed_packet_sequence": 11, "qd_rad_s": [0.9 * value for value in second]},
    ]
    result = _fixed_observation_actual_qd_proxy(
        frames=frames,
        commands_by_sequence=commands,
        candidate_qdot_by_sequence={10: second, 11: first},
    )

    assert result["candidate_matched_consumed_frames"] == 2
    assert result["source_join_counts"]["held_or_duplicate_rows_filtered"] == 1
    assert result["source_published_qdot_vs_historical_actual_qd_pearson"] == pytest.approx(1.0)
    assert result["candidate_qdot_vs_historical_actual_qd_pearson"] == pytest.approx(-1.0)
    assert [row["joint_1_based"] for row in result["per_joint"]] == [1, 2, 3, 4, 5, 6]
    assert result["per_joint"][0]["source_published_qdot_vs_historical_actual_qd_pearson"] == pytest.approx(1.0)
    assert result["per_joint"][0]["candidate_qdot_vs_historical_actual_qd_pearson"] == pytest.approx(-1.0)
    assert result["per_joint"][0]["source_published_qdot_vs_historical_actual_qd_mae_rad_s"] == pytest.approx(0.1)
    assert result["per_joint"][0]["candidate_qdot_vs_historical_actual_qd_mae_rad_s"] == pytest.approx(1.9)
    assert result["per_joint"][0]["candidate_vs_source_qdot_mae_rad_s"] == pytest.approx(2.0)
    assert result["not_candidate_actual_qd"] is True
    assert result["not_live_gate"] is True
    assert result["candidate_force_mae"] is None
