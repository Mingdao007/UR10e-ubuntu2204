"""Focused offline checks for the r004 fixed-Home rendered route."""

from __future__ import annotations

from pathlib import Path

import pytest

from step5d_autotune_v4_r004.home import ReturnEvidence, evaluate_return
from step5d_autotune_v4_r004.home_profile import (
    ATTEMPT_ENTRY_NOT_CAPTURED_HOME,
    FIXED_HOME_POSE,
    HomeFailureReason,
    HomeProfileError,
    load_fixed_home_profile,
    lock_ik_branch,
    verify_fixed_home,
)
from step5d_autotune_v4_r004.tp import render_script


ROOT = Path(__file__).resolve().parents[1]
SCRIPT1 = ROOT / "programs/step5/step5d/step5d_autotune_start_hover_r001.script"


def test_fixed_home_is_eoat_bound_and_not_runtime_captured() -> None:
    profile = load_fixed_home_profile()
    assert profile.pose == FIXED_HOME_POSE
    assert profile.eoat_profile_id == "new-3d-printed-eoat-v4"
    assert len(profile.eoat_profile_sha256) == 64

    rendered = render_script()
    pose_text = "p[0.487834547, 0.129337053, 0.033000000, 3.120752062, 0.000000000, 0.068626833]"
    assert f"# FIXED_HOME_POSE: {pose_text}" in rendered
    assert f"local fixed_home_pose = {pose_text}" in rendered
    assert "local campaign_home_pose = get_actual_tcp_pose()" not in rendered
    assert "local campaign_home_q = get_actual_joint_positions()" not in rendered
    assert "# FIXED_HOME_PROFILE: step5d.autotune-v4/r004-fixed-home-v1" in rendered


def test_every_arm_gates_fixed_home_before_any_attempt_motion() -> None:
    rendered = render_script()
    main = rendered[rendered.index("def step5d_strict_rnn_autotune_v4_r004():") :]
    entry = main.index("codex_r004_entry_home_verified(")
    attempt = main.index("codex_r004_execute_attempt(")
    entry_branch = main[entry:attempt]

    assert "reason = 69" in entry_branch
    assert "stopl(0.250000000)" in entry_branch
    assert "movel(" not in entry_branch
    assert "locked_home_q = arm_home_q" in entry_branch
    assert entry < attempt
    assert "position_error <= 0.001000000" in rendered
    assert "rotation_error <= 0.010000000" in rendered
    assert "codex_r004_abs(actual[index] - expected[index]) > 0.020000000" in rendered
    assert f"# HOME_ENTRY_FAILURE: {ATTEMPT_ENTRY_NOT_CAPTURED_HOME} ATTEMPT_ENTRY_NOT_CAPTURED_HOME" in rendered
    assert HomeFailureReason.ATTEMPT_ENTRY_NOT_CAPTURED_HOME == 69


def test_verified_home_enters_v4_search_directly_and_preserves_bounds() -> None:
    rendered = render_script()
    attempt_start = rendered.index("def codex_r004_execute_attempt(")
    search_start = rendered.index("local contact_start = get_actual_tcp_pose()", attempt_start)
    before_search = rendered[attempt_start:search_start]

    assert "movel(" not in before_search
    assert "speedl([0.0, 0.0, -0.000200000" in rendered
    assert "0.005000000" in rendered
    assert "travel >= 0.025000000" in rendered
    assert "contact_elapsed_s >= 90.000000000" in rendered
    assert "transfer_floor" not in rendered
    assert "0.062863519" not in rendered


def test_return_is_v3_sequential_route_without_legacy_floor() -> None:
    rendered = render_script()
    return_branch = rendered[
        rendered.index("def codex_r004_return_home") : rendered.index(
            "def codex_r004_execute_attempt"
        )
    ]
    rise = return_branch.index("movel(rise_pose, a=0.060, v=0.040, r=0.0)")
    transfer = return_branch.index("movel(transfer_pose, a=0.135, v=0.090, r=0.0)")
    descent = return_branch.index("movel(home_pose, a=0.060, v=0.040, r=0.0)")

    assert rise < transfer < descent
    assert "speedl([0.0, 0.0, 0.000500000" not in return_branch
    assert "transfer_floor" not in return_branch
    assert "floor_pose" not in return_branch
    assert "codex_r004_home_close(home_pose, home_q)" in return_branch


def test_script1_remains_arbitrary_start_to_the_same_fixed_home() -> None:
    script1 = SCRIPT1.read_text(encoding="utf-8")
    assert "local current_pose = get_actual_tcp_pose()" in script1
    assert "local target_pose = p[0.487834547, 0.129337053, 0.033000000, 3.120752062, 0.000000000, 0.068626833]" in script1
    assert "movel(rise_pose, a=0.060, v=0.040, r=0.0)" in script1
    assert "movel(transfer_pose, a=0.135, v=0.090, r=0.0)" in script1
    assert "movel(descent_pose, a=0.060, v=0.040, r=0.0)" in script1


def test_host_fixed_home_verification_locks_q_only_after_pose_passes() -> None:
    profile = load_fixed_home_profile()
    q = (0.1, -0.2, 0.3, -0.4, 0.5, -0.6)
    passed = verify_fixed_home(profile, profile.pose, q, reference_q=q)
    assert passed.passed
    assert passed.position_error_m == 0.0
    assert passed.orientation_error_rad == 0.0
    assert passed.q_error_rad == 0.0
    assert lock_ik_branch(profile, profile.pose, q).q == q

    bad_pose = (*profile.pose[:2], profile.pose[2] + 0.0011, *profile.pose[3:])
    rejected = verify_fixed_home(profile, bad_pose, q)
    assert not rejected.pose_verified
    with pytest.raises(HomeProfileError, match="ATTEMPT_ENTRY_NOT_CAPTURED_HOME"):
        lock_ik_branch(profile, bad_pose, q)


def test_return_gate_no_longer_requires_transfer_floor_bit() -> None:
    decision = evaluate_return(
        ReturnEvidence(
            stationary=True,
            retract_z_m=0.005,
            entry_linear_speed_m_s=0.010,
            return_linear_speed_m_s=0.010,
            entry_angular_speed_rad_s=0.010,
            return_angular_speed_rad_s=0.010,
            descended_to_captured_home=True,
            home_pose_error_m=0.001,
            home_orientation_error_rad=0.010,
            home_q_error_rad=0.020,
        )
    )
    assert decision.passed
    # Bit 4 was the legacy transfer-floor guard; r004 must never set it.
    assert not (int(decision.guard_mask) & 4)
    assert decision.auto_home is False
