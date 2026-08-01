"""Return/Home proof primitive; faults never auto-home."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import (
    HOME_ORIENTATION_TOLERANCE_RAD,
    HOME_POSITION_TOLERANCE_M,
    HOME_Q_TOLERANCE_RAD,
    RETRACT_MIN_M,
    SCRIPT1_TARGET_POSE,
    TRANSFER_FLOOR_Z_M,
)
from .wire import ReturnGuard


ENTRY_LINEAR_SPEED_MAX_M_S = 0.010
RETURN_LINEAR_SPEED_MAX_M_S = 0.010
ENTRY_ANGULAR_SPEED_MAX_RAD_S = 0.100
RETURN_ANGULAR_SPEED_MAX_RAD_S = 0.100


@dataclass(frozen=True)
class HomeReference:
    pose: tuple[float, float, float, float, float, float] = SCRIPT1_TARGET_POSE
    q: tuple[float, float, float, float, float, float] | None = None


@dataclass(frozen=True)
class ReturnEvidence:
    stationary: bool
    retract_z_m: float
    transfer_floor_z_m: float
    entry_linear_speed_m_s: float
    return_linear_speed_m_s: float
    entry_angular_speed_rad_s: float
    return_angular_speed_rad_s: float
    descended_to_captured_home: bool
    home_pose_error_m: float
    home_orientation_error_rad: float
    home_q_error_rad: float
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
        evidence.transfer_floor_z_m,
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
    if finite and evidence.transfer_floor_z_m >= TRANSFER_FLOOR_Z_M:
        mask |= ReturnGuard.TRANSFER_FLOOR
    if finite and evidence.entry_linear_speed_m_s <= ENTRY_LINEAR_SPEED_MAX_M_S and evidence.entry_angular_speed_rad_s <= ENTRY_ANGULAR_SPEED_MAX_RAD_S:
        mask |= ReturnGuard.ENTRY_ENVELOPE
    if finite and evidence.return_linear_speed_m_s <= RETURN_LINEAR_SPEED_MAX_M_S and evidence.return_angular_speed_rad_s <= RETURN_ANGULAR_SPEED_MAX_RAD_S:
        mask |= ReturnGuard.RETURN_ENVELOPE
    if finite and evidence.descended_to_captured_home and evidence.home_pose_error_m <= HOME_POSITION_TOLERANCE_M and evidence.home_orientation_error_rad <= HOME_ORIENTATION_TOLERANCE_RAD:
        mask |= ReturnGuard.HOME_POSE
    if finite and evidence.home_q_error_rad <= HOME_Q_TOLERANCE_RAD:
        mask |= ReturnGuard.HOME_Q
    required = ReturnGuard.STATIONARY | ReturnGuard.RETRACT_5MM | ReturnGuard.TRANSFER_FLOOR | ReturnGuard.ENTRY_ENVELOPE | ReturnGuard.RETURN_ENVELOPE | ReturnGuard.HOME_POSE | ReturnGuard.HOME_Q
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
    "HomeReference",
    "ReturnDecision",
    "ReturnEvidence",
    "evaluate_return",
]
