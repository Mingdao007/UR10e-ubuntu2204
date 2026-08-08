"""r004-owned invariant envelope for the typed V3-effective motion profile."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from step5d_autotune_v4.contracts import TARGET_FORCE_N, V4Candidate, assert_runtime_target

from .runtime import KinematicGateResult, RuntimeGuardError

if TYPE_CHECKING:
    from .motion_profile import V4MotionProfile


@dataclass(frozen=True)
class V4InvariantEnvelope:
    """Final typed r004 PATH envelope; no optimizer can replace it."""

    target_force_n: float = TARGET_FORCE_N
    cartesian_total_cap_m_s: float = 0.0005
    normal_cap_m_s: float = 0.00035
    tangential_cap_m_s: float = 0.00035
    angular_cap_rad_s: float = 0.05
    qdot_cap_rad_s: float = 0.15
    motion_profile: V4MotionProfile | None = None

    def __post_init__(self) -> None:
        if self.motion_profile is not None:
            from .motion_profile import V4MotionProfile

            if not isinstance(self.motion_profile, V4MotionProfile):
                raise RuntimeGuardError("motion_profile must be a typed V4MotionProfile")
            object.__setattr__(
                self, "cartesian_total_cap_m_s", self.motion_profile.total_linear_cap_m_s
            )
            object.__setattr__(
                self, "normal_cap_m_s", self.motion_profile.normal_linear_cap_m_s
            )
            object.__setattr__(
                self, "tangential_cap_m_s", self.motion_profile.tangential_cap_m_s
            )
            object.__setattr__(
                self, "angular_cap_rad_s", self.motion_profile.angular_cap_rad_s
            )
            object.__setattr__(self, "qdot_cap_rad_s", self.motion_profile.qdot_cap_rad_s)

    def enforce_candidate(self, candidate: V4Candidate) -> None:
        assert_runtime_target(candidate, self.target_force_n)

    def enforce_gate(self, result: KinematicGateResult) -> KinematicGateResult:
        if not isinstance(result, KinematicGateResult):
            raise RuntimeGuardError("provider returned an untyped kinematic result")
        if (
            not isinstance(result.allowed, bool)
            or not isinstance(result.reason, str)
            or len(result.qdot) != 6
            or len(result.twist) != 6
        ):
            raise RuntimeGuardError("provider returned a non-6D kinematic result")
        try:
            values = tuple(
                float(value)
                for value in (
                    *result.qdot,
                    *result.twist,
                    result.total_linear_m_s,
                    result.normal_m_s,
                    result.tangential_m_s,
                    result.angular_rad_s,
                )
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeGuardError("provider returned a nonnumeric kinematic output") from exc
        if not all(math.isfinite(value) for value in values):
            raise RuntimeGuardError("provider returned nonfinite kinematic output")
        qdot = tuple(values[:6])
        twist = tuple(values[6:12])
        total_linear, normal, tangential, angular = values[12:]
        if any(value < 0.0 for value in (total_linear, normal, tangential, angular)):
            raise RuntimeGuardError("provider returned a negative speed norm")
        if any(
            (
                max(abs(value) for value in qdot) > self.qdot_cap_rad_s,
                total_linear > self.cartesian_total_cap_m_s,
                normal > self.normal_cap_m_s,
                tangential > self.tangential_cap_m_s,
                angular > self.angular_cap_rad_s,
            )
        ):
            raise RuntimeGuardError("provider output exceeds invariant envelope")
        if not result.allowed and any(abs(value) > 0.0 for value in qdot):
            raise RuntimeGuardError("blocked provider output must be zero qdot")
        return KinematicGateResult(
            allowed=result.allowed,
            qdot=qdot,
            twist=twist,
            total_linear_m_s=total_linear,
            normal_m_s=normal,
            tangential_m_s=tangential,
            angular_rad_s=angular,
            reason=result.reason,
        )


__all__ = ["V4InvariantEnvelope"]
