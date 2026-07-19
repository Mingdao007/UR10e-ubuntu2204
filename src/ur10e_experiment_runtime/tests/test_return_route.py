from __future__ import annotations

from ur10e_experiment_runtime.batch import BatchIdentity, BatchRow, ReturnReferenceKind
from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR
from ur10e_experiment_runtime.return_route import return_reference, return_route
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
    return BatchIdentity("a" * 64, "b" * 64, 2, tuple(rows))


def test_exact_batch_identity_selects_near_ready_then_campaign_home() -> None:
    identity = batch()
    near = (0.487834547, 0.129337053, 0.022863519, 3.120752062, 0.0, 0.068626833)
    home = (0.4, 0.1, 0.2, 3.14, 0.0, 0.0)
    row9 = return_reference(identity, 9, near_ready_pose=near, campaign_home_pose=home)
    row10 = return_reference(identity, 10, near_ready_pose=near, campaign_home_pose=home)
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
    assert route[0].target_xyz_m[2] == 0.033
    assert route[0].preserve_orientation
    assert route[1].target_xyz_m[2] == 0.033
    assert route[1].acceleration_m_s2 == 0.135
    assert route[1].velocity_m_s == 0.090
    assert route[2].target_xyz_m[2] == 0.022863519
    assert route[2].target_rotvec_rad == STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad
