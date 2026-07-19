from __future__ import annotations

import math

import pytest

from ur10e_experiment_runtime.batch import BatchIdentity, BatchRow, ReturnReferenceKind
from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR
from ur10e_experiment_runtime.return_route import (
    CampaignHomeReference,
    NearReadyReference,
    RETURN_ANGULAR_ACCELERATION_LIMIT_RAD_S2,
    RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2,
    RETURN_ANGULAR_SPEED_LIMIT_RAD_S,
    RETURN_ANGULAR_SPEED_GUARD_RAD_S,
    RETURN_ANGULAR_STOP_DECELERATION_RAD_S2,
    RETURN_CONTROLLER_MAX_SAMPLE_GAP_S,
    RETURN_CONTROLLER_PERIOD_S,
    RETURN_ORIENTATION_ADMISSION_LIMIT_RAD,
    return_orientation_distance_rad,
    return_reference,
    return_route,
)
from ur10e_experiment_runtime.stage_adapters import control_candidate_uid


def batch() -> BatchIdentity:
    rows = []
    for index in range(1, 11):
        candidate = {
            "force_p_gain": 0.001 + index * 1e-6,
            "force_i_gain": 0.00001,
            "force_damping": 7.0,
            "orientation_ko": 0.4,
        }
        overlay = {
            **candidate,
            "control_candidate_uid": control_candidate_uid(candidate),
            "execution_profile_id": "111",
            "step5d_preload_filtered_min_n": 5.0,
            "step5d_preload_filtered_max_n": 22.0,
            "step5d_preload_raw_min_n": 3.0,
            "step5d_preload_raw_max_n": 25.0,
            "step5d_preload_force_norm_max_n": 100.0,
            "step5d_preload_hold_s": 0.0,
            "step5d_preload_timeout_s": 60.0,
        }
        rows.append(BatchRow(index, candidate, overlay))
    return BatchIdentity(
        campaign_uid="campaign-uid",
        experiment_fingerprint="a" * 64,
        launch_fingerprint="b" * 64,
        adapter_fingerprint="c" * 64,
        physical_prior_fingerprint="d" * 64,
        safety_envelope_fingerprint="e" * 64,
        return_policy_fingerprint="f" * 64,
        controller_readback_fingerprint="1" * 64,
        authorization_ref_sha256="2" * 64,
        plant_epoch=2,
        rows=tuple(rows),
    )


def test_exact_batch_identity_selects_near_ready_then_campaign_home() -> None:
    identity = batch()
    near = (0.487834547, 0.129337053, 0.022863519, 3.120752062, 0.0, 0.068626833)
    home = (0.4, 0.1, 0.2, 3.14, 0.0, 0.0)
    row9 = return_reference(identity, 9, near_ready_pose=near, campaign_home_pose=home)
    row10 = return_reference(identity, 10, near_ready_pose=near, campaign_home_pose=home)
    assert isinstance(row9, NearReadyReference)
    assert isinstance(row10, CampaignHomeReference)
    assert row9.kind is ReturnReferenceKind.NEAR_READY
    assert row9.pose_xyz_m == near[:3]
    assert row10.kind is ReturnReferenceKind.CAMPAIGN_HOME
    assert row10.pose_xyz_m == home[:3]
    assert row9.row_uid != row10.row_uid


def test_fixed_route_has_safe_z_and_prior_orientation() -> None:
    identity = batch()
    near = (*STEP5D_V3_PHYSICAL_PRIOR.precontact_xyz_m, *STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad)
    reference = return_reference(identity, 1, near_ready_pose=near, campaign_home_pose=near)
    route = return_route(current_pose=(0.5, 0.2, 0.01, 3.0, 0.0, 0.0), reference=reference, prior=STEP5D_V3_PHYSICAL_PRIOR)
    assert len(route.segments) == 3
    assert route.segments[0].target_xyz_m[2] == 0.033
    assert route.segments[0].preserve_orientation
    assert route.segments[1].target_xyz_m[2] == 0.033
    assert route.segments[1].acceleration_m_s2 == 0.135
    assert route.segments[1].velocity_m_s == 0.090
    assert set(route.segments[1].document()) == {
        "name",
        "target_xyz_m",
        "target_rotvec_rad",
        "acceleration_m_s2",
        "velocity_m_s",
        "angular_speed_limit_rad_s",
        "angular_acceleration_limit_rad_s2",
        "angular_speed_guard_rad_s",
        "angular_acceleration_guard_rad_s2",
        "angular_stop_deceleration_rad_s2",
        "preserve_orientation",
    }
    assert all(
        segment.angular_speed_limit_rad_s == RETURN_ANGULAR_SPEED_LIMIT_RAD_S
        for segment in route.segments
    )
    assert all(
        segment.angular_acceleration_limit_rad_s2
        == RETURN_ANGULAR_ACCELERATION_LIMIT_RAD_S2
        for segment in route.segments
    )
    assert all(
        segment.angular_stop_deceleration_rad_s2
        == RETURN_ANGULAR_STOP_DECELERATION_RAD_S2
        for segment in route.segments
    )
    assert all(
        segment.angular_speed_guard_rad_s == RETURN_ANGULAR_SPEED_GUARD_RAD_S
        for segment in route.segments
    )
    assert all(
        segment.angular_acceleration_guard_rad_s2
        == RETURN_ANGULAR_ACCELERATION_GUARD_RAD_S2
        for segment in route.segments
    )
    assert RETURN_ORIENTATION_ADMISSION_LIMIT_RAD > 0.0
    assert RETURN_CONTROLLER_PERIOD_S == 0.002
    assert RETURN_CONTROLLER_MAX_SAMPLE_GAP_S == 0.004
    assert route.segments[2].target_xyz_m[2] == 0.022863519
    assert route.segments[2].target_rotvec_rad == STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad
    assert len(route.route_fingerprint) == 64


def test_campaign_home_is_direct_three_segment_route_without_precontact_excursion() -> None:
    identity = batch()
    near = (*STEP5D_V3_PHYSICAL_PRIOR.precontact_xyz_m, *STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad)
    home = (0.4, 0.1, 0.2, 3.14, 0.0, 0.0)
    reference = return_reference(
        identity,
        10,
        near_ready_pose=near,
        campaign_home_pose=home,
    )
    route = return_route(
        current_pose=(0.5, 0.2, 0.01, 3.0, 0.0, 0.0),
        reference=reference,
        prior=STEP5D_V3_PHYSICAL_PRIOR,
    )
    assert len(route.segments) == 3
    assert route.segments[1].target_xyz_m == (home[0], home[1], 0.033)
    assert route.segments[2].target_xyz_m == home[:3]
    assert all(
        segment.target_xyz_m[:2]
        != STEP5D_V3_PHYSICAL_PRIOR.precontact_xyz_m[:2]
        for segment in route.segments[1:]
    )


def test_return_orientation_admission_uses_shortest_so3_distance_near_pi() -> None:
    epsilon = 0.01
    distance = return_orientation_distance_rad(
        (math.pi - epsilon, 0.0, 0.0),
        (-math.pi + epsilon, 0.0, 0.0),
    )
    assert distance == pytest.approx(2.0 * epsilon, abs=1e-12)


def test_dynamic_campaign_home_outside_orientation_domain_is_rejected() -> None:
    identity = batch()
    near = (*STEP5D_V3_PHYSICAL_PRIOR.precontact_xyz_m, *STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad)
    home = (0.4, 0.1, 0.2, 0.0, 0.0, 0.0)
    reference = return_reference(
        identity,
        10,
        near_ready_pose=near,
        campaign_home_pose=home,
    )
    with pytest.raises(ValueError, match="orientation admission domain"):
        return_route(
            current_pose=(0.5, 0.2, 0.01, math.pi, 0.0, 0.0),
            reference=reference,
            prior=STEP5D_V3_PHYSICAL_PRIOR,
        )
