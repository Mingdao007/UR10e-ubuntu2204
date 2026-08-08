"""Typed, EOAT-bound fixed Home profile for the isolated V4 r004 route.

The Cartesian Home is a configured source value, not a runtime capture.  A
successful pose check may establish the current joint-space IK branch; that
branch is then used as the per-attempt q reference.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Final, Sequence

from step5d_eoat_profiles import EOATProfile, load_new_eoat_profile

from .contracts import (
    HOME_ORIENTATION_TOLERANCE_RAD,
    HOME_POSITION_TOLERANCE_M,
    HOME_Q_TOLERANCE_RAD,
    SCRIPT1_TARGET_POSE,
)


HOME_PROFILE_ID: Final[str] = "step5d.autotune-v4/r004-fixed-home-v1"
FIXED_HOME_POSE: Final[tuple[float, float, float, float, float, float]] = (
    0.487834547,
    0.129337053,
    0.033000000,
    3.120752062,
    0.000000000,
    0.068626833,
)
if FIXED_HOME_POSE != SCRIPT1_TARGET_POSE:
    raise RuntimeError("fixed r004 Home differs from the configured Script 1 pose")

RETURN_RISE_ACCEL_M_S2: Final[float] = 0.060
RETURN_RISE_SPEED_M_S: Final[float] = 0.040
RETURN_TRANSFER_ACCEL_M_S2: Final[float] = 0.135
RETURN_TRANSFER_SPEED_M_S: Final[float] = 0.090


class HomeFailureReason(IntEnum):
    """Typed resident-TP reason codes owned by the fixed-Home gate."""

    ATTEMPT_ENTRY_NOT_CAPTURED_HOME = 69


ATTEMPT_ENTRY_NOT_CAPTURED_HOME: Final[int] = int(
    HomeFailureReason.ATTEMPT_ENTRY_NOT_CAPTURED_HOME
)

HomePose = tuple[float, float, float, float, float, float]
JointVector = tuple[float, float, float, float, float, float]


class HomeProfileError(ValueError):
    """The fixed Home or its EOAT binding is malformed."""


def _vector(value: Sequence[float], *, role: str) -> tuple[float, ...]:
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HomeProfileError(f"{role} must be numeric") from exc
    if len(result) != 6 or not all(math.isfinite(item) for item in result):
        raise HomeProfileError(f"{role} must contain six finite values")
    return result


def _sha256(value: object, *, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise HomeProfileError(f"{role} must be a lowercase SHA-256")
    return value


@dataclass(frozen=True)
class FixedHomeProfile:
    """Immutable fixed Cartesian Home bound to one EOAT profile digest."""

    eoat_profile_id: str
    eoat_profile_sha256: str
    home_profile_id: str = HOME_PROFILE_ID
    pose: HomePose = FIXED_HOME_POSE
    position_tolerance_m: float = HOME_POSITION_TOLERANCE_M
    orientation_tolerance_rad: float = HOME_ORIENTATION_TOLERANCE_RAD
    joint_tolerance_rad: float = HOME_Q_TOLERANCE_RAD

    def __post_init__(self) -> None:
        if self.home_profile_id != HOME_PROFILE_ID:
            raise HomeProfileError("fixed Home profile identity differs")
        if not isinstance(self.eoat_profile_id, str) or not self.eoat_profile_id:
            raise HomeProfileError("fixed Home EOAT profile id is missing")
        _sha256(self.eoat_profile_sha256, role="fixed Home EOAT profile")
        parsed_pose = _vector(self.pose, role="fixed Home pose")
        if parsed_pose != FIXED_HOME_POSE:
            raise HomeProfileError("fixed Home pose differs from configured Script 1 pose")
        object.__setattr__(self, "pose", parsed_pose)
        for role, value in (
            ("Home position tolerance", self.position_tolerance_m),
            ("Home orientation tolerance", self.orientation_tolerance_rad),
            ("Home joint tolerance", self.joint_tolerance_rad),
        ):
            if not math.isfinite(float(value)) or float(value) <= 0.0:
                raise HomeProfileError(f"{role} is not a positive finite bound")
        if self.position_tolerance_m != HOME_POSITION_TOLERANCE_M:
            raise HomeProfileError("fixed Home position tolerance differs")
        if self.orientation_tolerance_rad != HOME_ORIENTATION_TOLERANCE_RAD:
            raise HomeProfileError("fixed Home orientation tolerance differs")
        if self.joint_tolerance_rad != HOME_Q_TOLERANCE_RAD:
            raise HomeProfileError("fixed Home joint tolerance differs")

    @classmethod
    def from_eoat(cls, profile: EOATProfile) -> "FixedHomeProfile":
        return cls(
            eoat_profile_id=profile.profile_id,
            eoat_profile_sha256=profile.profile_sha256,
        )

    def assert_eoat(self, profile: EOATProfile) -> None:
        if (
            profile.profile_id != self.eoat_profile_id
            or profile.profile_sha256 != self.eoat_profile_sha256
        ):
            raise HomeProfileError("fixed Home EOAT binding differs")


def load_fixed_home_profile(
    eoat_profile: EOATProfile | None = None,
) -> FixedHomeProfile:
    """Load the new-EOAT binding used by the r004 renderer."""

    profile = load_new_eoat_profile() if eoat_profile is None else eoat_profile
    return FixedHomeProfile.from_eoat(profile)


@dataclass(frozen=True)
class HomeVerification:
    """Measured fixed-Home errors for one no-motion verification."""

    position_error_m: float
    orientation_error_rad: float
    q_error_rad: float | None
    profile_id: str = HOME_PROFILE_ID

    @property
    def pose_verified(self) -> bool:
        return self.position_error_m <= HOME_POSITION_TOLERANCE_M and self.orientation_error_rad <= HOME_ORIENTATION_TOLERANCE_RAD

    @property
    def q_verified(self) -> bool:
        return self.q_error_rad is None or self.q_error_rad <= HOME_Q_TOLERANCE_RAD

    @property
    def passed(self) -> bool:
        return self.pose_verified and self.q_verified


@dataclass(frozen=True)
class IKBranchLock:
    """Joint branch captured only after the fixed Cartesian pose passed."""

    q: JointVector
    profile_id: str = HOME_PROFILE_ID
    source: str = "post_pose_verification"

    def __post_init__(self) -> None:
        parsed = _vector(self.q, role="Home IK branch")
        object.__setattr__(self, "q", parsed)
        if self.profile_id != HOME_PROFILE_ID:
            raise HomeProfileError("Home IK branch profile identity differs")
        if self.source != "post_pose_verification":
            raise HomeProfileError("Home IK branch source is not pose-gated")


def verify_fixed_home(
    profile: FixedHomeProfile,
    actual_pose: Sequence[float],
    actual_q: Sequence[float],
    *,
    reference_q: Sequence[float] | None = None,
) -> HomeVerification:
    """Verify the fixed pose and, when present, the locked IK branch."""

    actual_pose_v = _vector(actual_pose, role="actual Home pose")
    actual_q_v = _vector(actual_q, role="actual Home q")
    expected_pose = profile.pose
    position_error = math.dist(actual_pose_v[:3], expected_pose[:3])
    orientation_error = math.dist(actual_pose_v[3:], expected_pose[3:])
    q_error: float | None = None
    if reference_q is not None:
        reference_q_v = _vector(reference_q, role="reference Home q")
        q_error = max(
            abs(actual - expected)
            for actual, expected in zip(actual_q_v, reference_q_v, strict=True)
        )
    return HomeVerification(
        position_error_m=position_error,
        orientation_error_rad=orientation_error,
        q_error_rad=q_error,
        profile_id=profile.home_profile_id,
    )


def lock_ik_branch(
    profile: FixedHomeProfile,
    actual_pose: Sequence[float],
    actual_q: Sequence[float],
) -> IKBranchLock:
    """Lock q only after the fixed Cartesian Home pose has passed."""

    verification = verify_fixed_home(profile, actual_pose, actual_q)
    if not verification.pose_verified:
        raise HomeProfileError(
            f"{HomeFailureReason.ATTEMPT_ENTRY_NOT_CAPTURED_HOME.name}: "
            "fixed Cartesian Home pose is not verified"
        )
    return IKBranchLock(tuple(float(value) for value in actual_q))


__all__ = [
    "ATTEMPT_ENTRY_NOT_CAPTURED_HOME",
    "FIXED_HOME_POSE",
    "HOME_PROFILE_ID",
    "HomeFailureReason",
    "HomePose",
    "HomeProfileError",
    "HomeVerification",
    "IKBranchLock",
    "JointVector",
    "RETURN_RISE_ACCEL_M_S2",
    "RETURN_RISE_SPEED_M_S",
    "RETURN_TRANSFER_ACCEL_M_S2",
    "RETURN_TRANSFER_SPEED_M_S",
    "FixedHomeProfile",
    "load_fixed_home_profile",
    "lock_ik_branch",
    "verify_fixed_home",
]
