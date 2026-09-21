"""Pure recovery-policy contracts; no device I/O."""
import copy
import math

import numpy as np
import pytest
from contact_home_recovery_policy import (
    ReliefForceGuard,
    plan_home_recovery,
    plan_staged_home_recovery,
    validate_lift_sample,
)
from contact_yield_math import so3_exp, so3_log


HOME = np.array(
    [0.4620551816, 0.1778825964, 0.03408876139925415, 3.120752062, 0.0, 0.068626833]
)
IDENTITY = np.eye(3)
NO_LOAD = np.zeros(6)
QUIET_STD = np.array([0.02, 0.02, 0.02, 0.0, 0.0, 0.0])


def start_below(*, xy_m=(0.002444, 0.0), rise_m=0.010, rotvec=None):
    pose = HOME.copy()
    pose[0] += xy_m[0]
    pose[1] += xy_m[1]
    pose[2] -= rise_m
    if rotvec is not None:
        pose[3:] = so3_log(so3_exp(np.asarray(rotvec, dtype=float)) @ so3_exp(HOME[3:]))
    return pose


def wrench(force, torque=(0.0, 0.0, 0.0)):
    return np.array([*force, *torque], dtype=float)


def test_current_xy_drift_is_admissible_and_first_target_is_vertical():
    start = start_below()
    assert float(np.linalg.norm(start[:2] - HOME[:2])) == pytest.approx(0.002444)
    plan = plan_home_recovery(start, HOME)
    assert plan["needs_lift"] is True
    delta = plan["lift_pose"][:3] - start[:3]
    np.testing.assert_allclose(delta[:2], 0.0, atol=0, rtol=0)
    assert delta[2] == pytest.approx(0.010)
    np.testing.assert_allclose(plan["lift_pose"][3:], start[3:])
    np.testing.assert_allclose(plan["lift_pose"][2], HOME[2])
    np.testing.assert_allclose(plan["start_pose"], start)
    np.testing.assert_allclose(plan["home_pose"], HOME)


def test_sideways_or_rotation_while_below_home_is_rejected():
    start = start_below()
    plan = plan_home_recovery(start, HOME)
    still = np.zeros(6)
    validate_lift_sample(plan, start, still)
    sideways = start.copy()
    sideways[0] += 0.0006
    with pytest.raises(ValueError, match="lateral"):
        validate_lift_sample(plan, sideways, still)
    turned = start.copy()
    turned[3:] = so3_log(so3_exp(np.array([0.0, 0.004, 0.0])) @ so3_exp(start[3:]))
    with pytest.raises(ValueError, match="3mrad"):
        validate_lift_sample(plan, turned, still)
    down = start.copy()
    down[2] -= 0.0002
    with pytest.raises(ValueError, match="below start"):
        validate_lift_sample(plan, down, still)
    with pytest.raises(ValueError, match="downward"):
        validate_lift_sample(plan, start, np.array([0.0, 0.0, -0.0006, 0.0, 0.0, 0.0]))
    with pytest.raises(ValueError):
        validate_lift_sample(plan, [float("nan")] * 6, still)
    at_clearance = plan["lift_pose"].copy()
    at_clearance[:2] = HOME[:2]
    at_clearance[3:] = HOME[3:]
    with pytest.raises(ValueError, match="lateral"):
        validate_lift_sample(plan, at_clearance, still)
    validate_lift_sample(plan, start, np.array([0.0, 0.0, 0.006, 0.0, 0.0, 0.0]))
    with pytest.raises(ValueError, match="historical 40mm/s"):
        validate_lift_sample(plan, start, np.array([0.0, 0.0, 0.041, 0.0, 0.0, 0.0]))


def test_rise_above_15mm_is_invalid_geometry():
    with pytest.raises(ValueError, match="15mm"):
        plan_home_recovery(start_below(rise_m=0.015001), HOME)
    plan_home_recovery(start_below(rise_m=0.015), HOME)
    far = start_below(xy_m=(0.081, 0.0), rise_m=0.001)
    with pytest.raises(ValueError, match="80mm"):
        plan_home_recovery(far, HOME)
    rotated = start_below(rotvec=(0.011, 0.0, 0.0))
    with pytest.raises(ValueError, match="10mrad"):
        plan_home_recovery(rotated, HOME)
    wrong_home = HOME.copy()
    wrong_home[0] += 1e-6
    with pytest.raises(ValueError, match="original Home XYZ"):
        plan_home_recovery(start_below(), wrong_home)


def test_staged_route_turns_only_after_clearance_and_preserves_direct_gate():
    start = start_below(rise_m=0.012, rotvec=(0.012, 0.0, 0.0), xy_m=(0.00034, 0.0))
    with pytest.raises(ValueError, match="10mrad"):
        plan_home_recovery(start, HOME)
    plan = plan_staged_home_recovery(start, HOME)
    assert plan["staged_recovery"] is True
    assert plan["route"] == "staged_clearance_orientation"
    assert plan["needs_lift"] is True
    np.testing.assert_allclose(plan["lift_pose"][:2], start[:2])
    np.testing.assert_allclose(plan["lift_pose"][2], HOME[2])
    np.testing.assert_allclose(plan["lift_pose"][3:], start[3:])
    assert plan["staged_max_so3_angle_rad"] == pytest.approx(0.020)
    with pytest.raises(ValueError, match="3mm"):
        plan_staged_home_recovery(
            start_below(xy_m=(0.003001, 0.0), rotvec=(0.012, 0.0, 0.0)),
            HOME,
        )
    with pytest.raises(ValueError, match="20mrad"):
        plan_staged_home_recovery(
            start_below(rotvec=(0.020001, 0.0, 0.0)), HOME
        )


def test_overload_cannot_enter_if_wrong_direction():
    with pytest.raises(ValueError, match="into the workpiece"):
        ReliefForceGuard(wrench((0.0, 0.0, -25.43)), NO_LOAD, QUIET_STD, IDENTITY)
    with pytest.raises(ValueError, match="into the workpiece"):
        ReliefForceGuard(wrench((25.43, 0.0, 0.0)), NO_LOAD, QUIET_STD, IDENTITY)
    sideways = 25.43 * np.array((math.sin(math.pi / 4.0 + 0.05), 0.0, math.cos(math.pi / 4.0 + 0.05)))
    with pytest.raises(ValueError, match="into the workpiece"):
        ReliefForceGuard(wrench(sideways), NO_LOAD, QUIET_STD, IDENTITY)
    ReliefForceGuard(wrench((0.0, 0.0, 25.43)), NO_LOAD, QUIET_STD, IDENTITY)
    ReliefForceGuard(wrench((0.0, 0.0, 0.4)), NO_LOAD, QUIET_STD, IDENTITY)
    loaded = wrench((0.0, 0.0, 12.0))
    guard = ReliefForceGuard(loaded, NO_LOAD, QUIET_STD, IDENTITY)
    sample = loaded.copy()
    guard.update(sample, 1.0)
    np.testing.assert_array_equal(sample, loaded)


def test_current_25n_load_monotonically_reducing_is_accepted():
    initial = wrench((0.0, 0.0, 25.43))
    guard = ReliefForceGuard(initial, NO_LOAD, QUIET_STD, IDENTITY)
    forces = (25.2, 22.0, 18.0, 12.0, 6.0, 2.4, 0.8, 0.4)
    released = False
    for i, fz in enumerate(forces):
        result = guard.update(wrench((0.0, 0.0, fz)), 10.0 + 0.05 * i)
        assert result["corrected_force_norm"] == pytest.approx(abs(fz))
        assert result["raw_force_norm"] == pytest.approx(abs(fz))
        assert result["corrected_force_ceiling"] >= result["corrected_force_norm"]
        released = result["released"]
    assert released is False
    result = guard.update(wrench((0.0, 0.0, 0.3)), 10.0 + 0.05 * (len(forces) - 1) + 0.2)
    assert result["released"] is True


def test_noisy_single_minimum_does_not_collapse_the_ceiling():
    guard = ReliefForceGuard(wrench((0.0, 0.0, 25.43)), NO_LOAD, QUIET_STD, IDENTITY)
    guard.update(wrench((0.0, 0.0, 24.8)), 1.0)
    guard.update(wrench((0.0, 0.0, 5.0)), 1.05)
    guard.update(wrench((0.0, 0.0, 24.6)), 1.10)
    filled = guard.update(wrench((0.0, 0.0, 24.4)), 1.15)
    assert filled["corrected_force_ceiling"] == pytest.approx(24.6 + 0.5)
    later = guard.update(wrench((0.0, 0.0, 24.2)), 1.20)
    assert later["corrected_force_norm"] == pytest.approx(24.2)


def test_reload_timing_nonfinite_and_torque_are_rejected():
    guard = ReliefForceGuard(wrench((0.0, 0.0, 25.43)), NO_LOAD, QUIET_STD, IDENTITY)
    for i, fz in enumerate((24.0, 22.0, 20.0, 18.0)):
        guard.update(wrench((0.0, 0.0, fz)), 1.0 + 0.05 * (i + 1))
    with pytest.raises(ValueError, match="ceiling"):
        guard.update(wrench((0.0, 0.0, 24.0)), 1.30)
    with pytest.raises(ValueError, match="already failed"):
        guard.update(wrench((0.0, 0.0, 10.0)), 1.35)

    timing = ReliefForceGuard(wrench((0.0, 0.0, 8.0)), NO_LOAD, QUIET_STD, IDENTITY)
    timing.update(wrench((0.0, 0.0, 7.0)), 2.0)
    with pytest.raises(ValueError, match="strictly increasing"):
        timing.update(wrench((0.0, 0.0, 6.0)), 2.0)
    with pytest.raises(ValueError, match="already failed"):
        timing.update(wrench((0.0, 0.0, 5.0)), 2.1)

    with pytest.raises(ValueError):
        ReliefForceGuard(wrench((0.0, 0.0, float("nan"))), NO_LOAD, QUIET_STD, IDENTITY)
    finite = ReliefForceGuard(wrench((0.0, 0.0, 4.0)), NO_LOAD, QUIET_STD, IDENTITY)
    with pytest.raises(ValueError):
        finite.update(wrench((0.0, 0.0, float("inf"))), 3.0)
    stamped = ReliefForceGuard(wrench((0.0, 0.0, 4.0)), NO_LOAD, QUIET_STD, IDENTITY)
    with pytest.raises(ValueError):
        stamped.update(wrench((0.0, 0.0, 3.0)), float("nan"))

    with pytest.raises(ValueError, match="2Nm"):
        ReliefForceGuard(wrench((0.0, 0.0, 1.0), (0.0, 0.0, 2.01)), NO_LOAD, QUIET_STD, IDENTITY)
    torque = ReliefForceGuard(wrench((0.0, 0.0, 4.0)), NO_LOAD, QUIET_STD, IDENTITY)
    with pytest.raises(ValueError, match="2Nm"):
        torque.update(wrench((0.0, 0.0, 3.0), (0.0, 2.0, 0.0)), 4.0)

    noisy = np.array([0.5, 0.0, 0.0, 0.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="2N"):
        ReliefForceGuard(wrench((0.0, 0.0, 1.0)), NO_LOAD, noisy, IDENTITY)
    negative = np.array([0.01, -0.01, 0.0, 0.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="nonnegative"):
        ReliefForceGuard(wrench((0.0, 0.0, 1.0)), NO_LOAD, negative, IDENTITY)


def test_release_requires_continuous_0_2s_below_1n():
    guard = ReliefForceGuard(wrench((0.0, 0.0, 10.0)), NO_LOAD, QUIET_STD, IDENTITY)
    assert guard.update(wrench((0.0, 0.0, 0.4)), 5.0)["released"] is False
    assert guard.update(wrench((0.0, 0.0, 0.4)), 5.19)["released"] is False
    interrupted = guard.update(wrench((0.0, 0.0, 8.0)), 5.20)
    assert interrupted["released"] is False
    assert guard.update(wrench((0.0, 0.0, 0.3)), 5.21)["released"] is False
    assert guard.update(wrench((0.0, 0.0, 0.3)), 5.409)["released"] is False
    assert guard.update(wrench((0.0, 0.0, 0.3)), 5.41)["released"] is True
    latched = guard.update(wrench((0.0, 0.0, 0.2)), 5.50)
    assert latched["released"] is True


def test_snapshot_inputs_are_unchanged():
    start = start_below()
    home = HOME.copy()
    start_before = start.copy()
    home_before = home.copy()
    plan = plan_home_recovery(start, home)
    np.testing.assert_allclose(plan["start_pose"], start_before)
    start[0] += 0.01
    home[2] += 0.01
    plan["start_pose"][1] += 0.01
    plan["lift_pose"][2] += 0.01
    replay = plan_home_recovery(start_before, home_before)
    np.testing.assert_allclose(replay["start_pose"], start_before)
    np.testing.assert_allclose(replay["home_pose"], home_before)
    np.testing.assert_allclose(replay["lift_pose"][:2], start_before[:2])
    np.testing.assert_allclose(start[:2], start_before[:2] + np.array([0.01, 0.0]))

    raw = wrench((0.0, 0.0, 25.43))
    no_load = NO_LOAD.copy()
    std = QUIET_STD.copy()
    rotation = IDENTITY.copy()
    snapshots = tuple(copy.deepcopy(x) for x in (raw, no_load, std, rotation))
    guard = ReliefForceGuard(raw, no_load, std, rotation)
    np.testing.assert_array_equal(raw, snapshots[0])
    raw[2] = 0.0
    no_load[0] = 3.0
    std[0] = 9.0
    rotation[0, 0] = -1.0
    sample = wrench((0.0, 0.0, 20.0))
    sample_before = sample.copy()
    result = guard.update(sample, 8.0)
    sample[2] = 99.0
    result["corrected_force"][2] = 0.0
    assert result["raw_force_norm"] == pytest.approx(20.0)
    assert result["corrected_force_norm"] == pytest.approx(20.0)
    np.testing.assert_array_equal(snapshots[0], wrench((0.0, 0.0, 25.43)))
    np.testing.assert_array_equal(snapshots[1], NO_LOAD)
    np.testing.assert_array_equal(snapshots[2], QUIET_STD)
    np.testing.assert_array_equal(snapshots[3], IDENTITY)
    np.testing.assert_array_equal(sample_before, wrench((0.0, 0.0, 20.0)))


def test_release_is_revoked_if_load_returns_before_home():
    guard=ReliefForceGuard(wrench((0.,0.,10.)),NO_LOAD,QUIET_STD,IDENTITY)
    guard.update(wrench((0.,0.,.2)),1.)
    assert guard.update(wrench((0.,0.,.2)),1.21)['released']
    assert not guard.update(wrench((0.,0.,1.1)),1.22)['released']


def test_unloading_can_restore_raw_gravity_baseline_without_reload():
    baseline=wrench((0.,0.,-8.))
    guard=ReliefForceGuard(wrench((0.,0.,17.)),baseline,QUIET_STD,IDENTITY)
    for i,f in enumerate([15.,12.,9.,6.,3.,0.,-3.,-6.,-8.,-8.]):
        guard.update(wrench((0.,0.,f)),1.+i*.1)
    assert guard.update(wrench((0.,0.,-8.)),2.2)['released']


def test_live_pressed_pose_is_admissible():
    start = HOME.copy()
    start[:3] = HOME[:3] + np.array((0.0236851083635676, -0.04971000912059625, -0.01400000000000000))
    start[3:] = so3_log(
        so3_exp(np.array((0.0074394719065892425, 0.0, 0.0)))
        @ so3_exp(HOME[3:])
    )
    home = HOME.copy()
    plan = plan_home_recovery(start, home)
    assert plan["needs_lift"] is True
    np.testing.assert_allclose(plan["lift_pose"][:2], start[:2])
    np.testing.assert_allclose(plan["lift_pose"][2], home[2])
    validate_lift_sample(plan, start, np.zeros(6))
    rotation = so3_exp(start[3:])
    ReliefForceGuard(
        wrench((6.631, 2.982, -24.101)),
        wrench((7.214, 4.288, 2.095)),
        QUIET_STD,
        rotation,
    )
