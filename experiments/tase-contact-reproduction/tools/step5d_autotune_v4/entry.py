"""Deterministic three-segment V4 entry planning primitive."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


ENTRY_X_M = 0.487834547
ENTRY_Y_M = 0.129337053
PRECONTACT_Z_M = 0.022863519
EXPECTED_CONTACT_Z_M_EVIDENCE_ONLY = 0.008044839
ROTATION_VECTOR_RAD = (3.120752062, 0.0, 0.068626833)
TRANSFER_Z_FLOOR_M = 0.062863519
ENTRY_STOPL_ACCELERATION_M_S2 = 0.25
SEARCH_STOPL_ACCELERATION_M_S2 = 0.01


class EntryPlanError(ValueError):
    """The V4 entry plan is invalid."""


@dataclass(frozen=True)
class EntrySegment:
    name: str
    target_pose: tuple[float, float, float, float, float, float]
    linear_speed_m_s: float
    angular_speed_cap_rad_s: float
    blend_radius_m: float = 0.0
    stationary_after: bool = True


def _pose(value: Sequence[float], role: str) -> tuple[float, ...]:
    if len(value) != 6:
        raise EntryPlanError(f"{role} must be a 6D pose")
    parsed = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in parsed):
        raise EntryPlanError(f"{role} must be finite")
    return parsed


def plan_entry(current_pose: Sequence[float]) -> tuple[EntrySegment, ...]:
    current = _pose(current_pose, "current pose")
    transfer_z = max(current[2], TRANSFER_Z_FLOOR_M)
    return (
        EntrySegment(
            "rise",
            (current[0], current[1], transfer_z, current[3], current[4], current[5]),
            linear_speed_m_s=0.04,
            angular_speed_cap_rad_s=0.0,
        ),
        EntrySegment(
            "xy_orientation_transfer",
            (ENTRY_X_M, ENTRY_Y_M, transfer_z, *ROTATION_VECTOR_RAD),
            linear_speed_m_s=0.01,
            angular_speed_cap_rad_s=0.10,
        ),
        EntrySegment(
            "descend",
            (ENTRY_X_M, ENTRY_Y_M, PRECONTACT_Z_M, *ROTATION_VECTOR_RAD),
            linear_speed_m_s=0.005,
            angular_speed_cap_rad_s=0.0,
        ),
    )


def validate_entry_plan(
    current_pose: Sequence[float], segments: Sequence[EntrySegment]
) -> None:
    expected = plan_entry(current_pose)
    if tuple(segments) != expected:
        raise EntryPlanError("entry segments differ from the deterministic plan")
    if any(segment.blend_radius_m != 0.0 for segment in segments):
        raise EntryPlanError("all entry segments require r=0")
    if any(not segment.stationary_after for segment in segments):
        raise EntryPlanError("stationary verification is required after each entry segment")
    if EXPECTED_CONTACT_Z_M_EVIDENCE_ONLY in {
        segment.target_pose[2] for segment in segments
    }:
        raise EntryPlanError("expected contact Z is evidence only and cannot be a target clamp")


__all__ = [
    "ENTRY_STOPL_ACCELERATION_M_S2",
    "EXPECTED_CONTACT_Z_M_EVIDENCE_ONLY",
    "EntryPlanError",
    "EntrySegment",
    "PRECONTACT_Z_M",
    "ROTATION_VECTOR_RAD",
    "SEARCH_STOPL_ACCELERATION_M_S2",
    "TRANSFER_Z_FLOOR_M",
    "plan_entry",
    "validate_entry_plan",
]
