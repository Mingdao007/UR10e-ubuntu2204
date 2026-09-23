"""Offline collision intent never becomes a second hardware writer."""
import numpy as np
import pytest

from tase_collision_policy import OfflineDualSpaceCollisionPolicy


def _policy():
    # Values are deterministic offline test inputs, not selected live gains.
    return OfflineDualSpaceCollisionPolicy((0, 0, 1), virtual_inertia=1,
                                           joint_damping=5, joint_stiffness=10)


def _step(policy, **changes):
    args = dict(
        phase="PATH", dt_s=.002, normal=(0.0, 0.0, 1.0),
        tase_twist=(0.0, 0.0, .001, 0.0, 0.0, 0.0),
        sfc_twist=(.01, 0.0, 0.0, 0.0, 0.0, 0.0),
        jacobian=np.eye(6), q_rad=np.zeros(6),
        collision_site=None, collision_qualified=False,
        external_joint_torque_nm=None, normal_force_n=5.0,
        lateral_error_m=0.0, corridor_radius_m=.001,
        joint_observer_receipt={"admission_passed": True, "sample_fresh": True,
                                "calibration_sha256": "cal", "dynamics_model_sha256": "dyn",
                                "timestamps_sha256": "clock"},
    )
    args.update(changes)
    return policy.step(**args)


def test_link_collision_yields_tangent_but_preserves_normal_and_orientation_intents():
    policy = _policy()
    nominal = _step(policy)
    assert nominal.mode == "NOMINAL"
    assert nominal.cartesian_twist[:3] == pytest.approx((.01, 0, .001))
    impact = _step(policy, collision_site="link", collision_qualified=True,
                   external_joint_torque_nm=(1, 0, 1, 0, 0, 0))
    assert impact.mode == "LINK_YIELD"
    assert impact.tangent_frozen is True
    assert impact.cartesian_twist[:3] == pytest.approx((0, 0, .001))
    assert impact.joint_yield_preference_rad_s[0] > 0
    assert impact.joint_yield_preference_rad_s[2] == pytest.approx(0, abs=1e-12)
    assert impact.diagnostics["command_authority"] == "offline_intent_only"


def test_tool_collision_and_loaded_drift_request_governed_recovery_until_home_reset():
    policy = _policy()
    tool = _step(policy, collision_site="tool", collision_qualified=True)
    assert tool.mode == "TOOL_HOLD"
    assert tool.joint_yield_preference_rad_s is None
    assert tool.tangent_frozen
    out = _step(policy, collision_site=None, lateral_error_m=.002)
    assert out.recovery_requested is True
    assert out.cartesian_twist is None
    assert out.reason == "loaded_tool_outside_mark_corridor"
    assert _step(policy).recovery_requested is True
    with pytest.raises(ValueError, match="verified joint Home"):
        policy.reset_at_home(home_verified=False)
    policy.reset_at_home(home_verified=True)
    assert _step(policy).mode == "NOMINAL"


def test_unqualified_link_signal_missing_torque_or_stale_cycle_never_commands_yield():
    for changes, reason in (
        ({"collision_site": "link"}, "unqualified_collision_observation"),
        ({"collision_site": "link", "collision_qualified": True},
         "joint_torque_observation_missing"),
        ({"collision_site": "link", "collision_qualified": True,
          "external_joint_torque_nm": (1, 0, 0, 0, 0, 0),
          "joint_observer_receipt": None}, "joint_observer_not_qualified"),
        ({"collision_site": "link", "collision_qualified": True,
          "external_joint_torque_nm": (1, 0, 0, 0, 0, 0), "dt_s": .03},
         "stale_cycle_or_guard_trip"),
    ):
        policy = _policy()
        result = _step(policy, **changes)
        assert result.recovery_requested is True
        assert result.joint_yield_preference_rad_s is None
        assert result.reason == reason
