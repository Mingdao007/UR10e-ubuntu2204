"""Fixed-Home proof and return primitive; faults never auto-home."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import (
    HOME_ORIENTATION_TOLERANCE_RAD,
    HOME_POSITION_TOLERANCE_M,
    HOME_Q_TOLERANCE_RAD,
    RETRACT_MIN_M,
)
from .home_profile import (
    ATTEMPT_ENTRY_NOT_CAPTURED_HOME,
    FIXED_HOME_POSE,
    FixedHomeProfile,
    HomeFailureReason,
    HomeVerification,
    IKBranchLock,
    verify_fixed_home,
)
from .wire import ReturnGuard


ENTRY_LINEAR_SPEED_MAX_M_S = 0.010
RETURN_LINEAR_SPEED_MAX_M_S = 0.010
ENTRY_ANGULAR_SPEED_MAX_RAD_S = 0.100
RETURN_ANGULAR_SPEED_MAX_RAD_S = 0.100


@dataclass(frozen=True)
class HomeReference:
    """Compatibility wrapper whose pose is always the configured fixed Home."""

    pose: tuple[float, float, float, float, float, float] = FIXED_HOME_POSE
    q: tuple[float, float, float, float, float, float] | None = None

    def __post_init__(self) -> None:
        pose = tuple(float(value) for value in self.pose)
        if len(pose) != 6 or pose != FIXED_HOME_POSE:
            raise ValueError("r004 HomeReference must use the fixed configured Home pose")
        object.__setattr__(self, "pose", pose)
        if self.q is not None:
            q = tuple(float(value) for value in self.q)
            if len(q) != 6 or not all(math.isfinite(value) for value in q):
                raise ValueError("r004 HomeReference q must contain six finite values")
            object.__setattr__(self, "q", q)


@dataclass(frozen=True)
class HomeEntryDecision:
    """No-motion ARM entry decision and optional post-pose IK branch lock."""

    passed: bool
    reason: HomeFailureReason | None
    verification: HomeVerification
    branch: IKBranchLock | None


def evaluate_entry_home(
    profile: FixedHomeProfile,
    actual_pose: tuple[float, float, float, float, float, float],
    actual_q: tuple[float, float, float, float, float, float],
    *,
    locked_branch: IKBranchLock | None = None,
) -> HomeEntryDecision:
    """Verify fixed Home before ARM and lock q only after pose verification."""

    verification = verify_fixed_home(
        profile,
        actual_pose,
        actual_q,
        reference_q=None if locked_branch is None else locked_branch.q,
    )
    if not verification.passed:
        return HomeEntryDecision(
            passed=False,
            reason=HomeFailureReason.ATTEMPT_ENTRY_NOT_CAPTURED_HOME,
            verification=verification,
            branch=locked_branch,
        )
    branch = locked_branch or IKBranchLock(tuple(float(value) for value in actual_q))
    return HomeEntryDecision(
        passed=True,
        reason=None,
        verification=verification,
        branch=branch,
    )


@dataclass(frozen=True)
class ReturnEvidence:
    stationary: bool
    retract_z_m: float
    entry_linear_speed_m_s: float
    return_linear_speed_m_s: float
    entry_angular_speed_rad_s: float
    return_angular_speed_rad_s: float
    descended_to_captured_home: bool
    home_pose_error_m: float
    home_orientation_error_rad: float
    home_q_error_rad: float
    # Kept as a read-back compatibility field; r004 no longer gates on it.
    transfer_floor_z_m: float = 0.0
    safety_gate_passed: bool = True
    contact_gate_passed: bool = True
    fault_reason: str = ""


@dataclass(frozen=True)
class ReturnDecision:
    passed: bool
    guard_mask: ReturnGuard
    reason: str
    stop_required: bool
    auto_home: bool


def evaluate_return(evidence: ReturnEvidence) -> ReturnDecision:
    values = (
        evidence.retract_z_m,
        evidence.entry_linear_speed_m_s,
        evidence.return_linear_speed_m_s,
        evidence.entry_angular_speed_rad_s,
        evidence.return_angular_speed_rad_s,
        evidence.home_pose_error_m,
        evidence.home_orientation_error_rad,
        evidence.home_q_error_rad,
    )
    finite = all(math.isfinite(float(value)) for value in values)
    mask = ReturnGuard.NONE
    if evidence.stationary:
        mask |= ReturnGuard.STATIONARY
    if finite and evidence.retract_z_m >= RETRACT_MIN_M:
        mask |= ReturnGuard.RETRACT_5MM
    if finite and evidence.entry_linear_speed_m_s <= ENTRY_LINEAR_SPEED_MAX_M_S and evidence.entry_angular_speed_rad_s <= ENTRY_ANGULAR_SPEED_MAX_RAD_S:
        mask |= ReturnGuard.ENTRY_ENVELOPE
    if finite and evidence.return_linear_speed_m_s <= RETURN_LINEAR_SPEED_MAX_M_S and evidence.return_angular_speed_rad_s <= RETURN_ANGULAR_SPEED_MAX_RAD_S:
        mask |= ReturnGuard.RETURN_ENVELOPE
    if finite and evidence.descended_to_captured_home and evidence.home_pose_error_m <= HOME_POSITION_TOLERANCE_M and evidence.home_orientation_error_rad <= HOME_ORIENTATION_TOLERANCE_RAD:
        mask |= ReturnGuard.HOME_POSE
    if finite and evidence.home_q_error_rad <= HOME_Q_TOLERANCE_RAD:
        mask |= ReturnGuard.HOME_Q
    required = ReturnGuard.STATIONARY | ReturnGuard.RETRACT_5MM | ReturnGuard.ENTRY_ENVELOPE | ReturnGuard.RETURN_ENVELOPE | ReturnGuard.HOME_POSE | ReturnGuard.HOME_Q
    passed = finite and evidence.safety_gate_passed and evidence.contact_gate_passed and not evidence.fault_reason and mask == required
    reason = "return_home_verified" if passed else (evidence.fault_reason or "return_home_guard_failed")
    return ReturnDecision(
        passed=passed,
        guard_mask=mask,
        reason=reason,
        stop_required=not passed,
        auto_home=False,
    )


__all__ = [
    "ENTRY_ANGULAR_SPEED_MAX_RAD_S",
    "ENTRY_LINEAR_SPEED_MAX_M_S",
    "HOME_ORIENTATION_TOLERANCE_RAD",
    "HOME_POSITION_TOLERANCE_M",
    "HOME_Q_TOLERANCE_RAD",
    "ATTEMPT_ENTRY_NOT_CAPTURED_HOME",
    "HomeEntryDecision",
    "HomeFailureReason",
    "HomeReference",
    "ReturnDecision",
    "ReturnEvidence",
    "evaluate_return",
]
