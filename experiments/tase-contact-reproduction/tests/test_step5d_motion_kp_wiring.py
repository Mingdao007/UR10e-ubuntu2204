import math
from pathlib import Path
from types import SimpleNamespace

from step5d_autotune_contract import (
    ExecutionProfile,
    ForceCandidate,
    _trial_candidate_step,
)
from step5d_autotune_live_driver import BridgeMailboxRuntime
from step5d_autotune_optimizer import candidate_vector
from step5d_physics_soft_prior import PhysicsSoftPrior, physics_log_weight
from step5d_autotune_v3.optimizer_payloads import candidate_payload
from step5d_autotune_v3.runtime_profile import control_candidate_coordinates
from ur10e_experiment_runtime.candidate_identity import ControlCandidateUid


def test_motion_kp_is_candidate_identity_and_optimizer_input() -> None:
    baseline = ForceCandidate()
    paper_neighbor = ForceCandidate.from_log2(
        p=0.0,
        damping=0.0,
        motion=1.5,
    )
    assert baseline.payload().get("motion_kp") is None
    assert paper_neighbor.motion_kp == 1.5 * (2.0**1.5)
    assert len(candidate_vector(paper_neighbor)) == 7
    assert candidate_vector(paper_neighbor)[-1] == 1.5
    uid = ControlCandidateUid.from_overlay(
        {
            "force_p_gain": paper_neighbor.force_p_gain,
            "force_i_gain": paper_neighbor.force_i_gain,
            "force_damping": paper_neighbor.force_damping,
            "orientation_ko": paper_neighbor.orientation_ko,
            "normal_filter_tau_s": paper_neighbor.normal_filter_tau_s,
            "motion_kp": paper_neighbor.motion_kp,
        }
    )
    assert str(uid).startswith("control:v4:")


def test_motion_kp_is_one_independent_acceptance_coordinate() -> None:
    baseline = ForceCandidate()
    neighbor = ForceCandidate.from_log2(
        p=baseline.log2_p,
        damping=baseline.log2_damping,
        i=baseline.log2_i,
        filter_tau=baseline.log2_filter_tau,
        orientation=baseline.log2_orientation_ko,
        motion=baseline.log2_motion_kp + 0.25,
    )
    step = _trial_candidate_step(baseline, neighbor)
    assert step is not None and step[0] == "motion_kp"
    assert math.isclose(step[1], 0.25, abs_tol=1e-12)
    assert len(
        control_candidate_coordinates(
            {
                "force_p_gain": neighbor.force_p_gain,
                "force_i_gain": neighbor.force_i_gain,
                "force_damping": neighbor.force_damping,
                "orientation_ko": neighbor.orientation_ko,
                "normal_filter_tau_s": neighbor.normal_filter_tau_s,
                "motion_kp": neighbor.motion_kp,
            }
        )
    ) == 6


def test_control_identity_versions_and_candidate_payload_round_trip() -> None:
    legacy = {
        "force_p_gain": 0.001,
        "force_i_gain": 0.00001,
        "force_damping": 7.0,
        "orientation_ko": 0.4,
    }
    pre_motion = {**legacy, "normal_filter_tau_s": 0.7}
    current = {**pre_motion, "motion_kp": 3.0}
    assert str(ControlCandidateUid.from_overlay(legacy)).startswith("control:v2:")
    assert str(ControlCandidateUid.from_overlay(pre_motion)).startswith(
        "control:v3:"
    )
    assert str(ControlCandidateUid.from_overlay(current)).startswith("control:v4:")
    candidate = ForceCandidate(
        orientation_ko=0.2,
        normal_filter_tau_s=0.7,
        motion_kp=3.0,
    )
    assert ForceCandidate.from_payload(candidate_payload(candidate)) == candidate
    assert ForceCandidate.from_payload(
        {
            "target_force_n": 12.0,
            "force_p_gain": 0.001,
            "force_i_gain": 0.00001,
            "force_damping": 7.0,
        }
    ) == ForceCandidate()


def test_arm_boundary_wires_all_outer_loop_candidate_fields() -> None:
    candidate = ForceCandidate(
        orientation_ko=0.2,
        normal_filter_tau_s=0.7,
        motion_kp=3.0,
    )
    args = SimpleNamespace()
    binding = SimpleNamespace(
        candidate=candidate,
        profile=ExecutionProfile(
            "nf100000-slew250-a2000",
            100.0,
            host_qdot_slew_rad_s2=2.5,
            tp_speedj_accel_rad_s2=20.0,
            qdot_cap_rad_s=2.5,
            bridge_angular_limit_rad_s=0.25,
        ),
        batch_row_index=1,
        logical_batch_sequence=2,
        trial_overlay=None,
    )
    BridgeMailboxRuntime._apply_arm_runtime(args, binding)
    assert args.step5d_autotune_orientation_ko == 0.2
    assert args.bridge_normal_filter_tau_s == 0.7
    assert args.step5d_autotune_motion_kp == 3.0


def test_motion_paper_value_is_a_soft_preference_not_a_boundary() -> None:
    prior = PhysicsSoftPrior()
    baseline = ForceCandidate()
    near_paper = ForceCandidate.from_log2(p=0.0, damping=0.0, motion=1.5)
    assert physics_log_weight(near_paper, prior) > physics_log_weight(
        baseline, prior
    )
    assert physics_log_weight(baseline, prior) < 0.0


def test_bridge_consumes_runtime_motion_and_orientation_gains() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "tools/kunwei_rtde_bridge.py"
    ).read_text(encoding="utf-8")
    assert "float(args.step5d_autotune_motion_kp)" in source
    assert "float(args.step5d_autotune_orientation_ko)" in source
    assert '"_step5d_applied_motion_kp"' in source
    assert '"_step5d_applied_orientation_ko"' in source
