"""Actual-dt timing and hash-bound Jacobian/qdot guard primitives for V4."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Sequence

from step5d_autotune_v4.contracts import V4Contract

if TYPE_CHECKING:
    from step5d_autotune_v4_r004.motion_profile import V4MotionProfile


class RuntimeGuardError(ValueError):
    """A V4 timing or kinematic safety invariant failed."""


def _validate_motion_profile(value: object) -> V4MotionProfile:
    from step5d_autotune_v4_r004.motion_profile import V4MotionProfile

    if not isinstance(value, V4MotionProfile):
        raise RuntimeGuardError("motion_profile must be a typed V4MotionProfile")
    return value


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return math.inf
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


@dataclass
class TimingGuard:
    timestamps_s: list[float] = field(default_factory=list)
    gaps_s: list[float] = field(default_factory=list)
    stopped: bool = False
    stop_reason: str = ""

    def observe(self, monotonic_timestamp_s: float) -> float | None:
        value = float(monotonic_timestamp_s)
        if not math.isfinite(value):
            self.stopped = True
            self.stop_reason = "nonfinite_monotonic_timestamp"
            return None
        if self.timestamps_s:
            dt = value - self.timestamps_s[-1]
            if dt <= 0.0:
                self.stopped = True
                self.stop_reason = "nonpositive_actual_dt"
                return dt
            if dt >= 0.08:
                self.stopped = True
                self.stop_reason = "actual_gap_at_least_80ms"
                self.gaps_s.append(dt)
                self.timestamps_s.append(value)
                return dt
            self.gaps_s.append(dt)
        self.timestamps_s.append(value)
        return None if len(self.timestamps_s) == 1 else self.gaps_s[-1]

    def acceptance(self) -> dict[str, float | bool | str]:
        duration = (
            self.timestamps_s[-1] - self.timestamps_s[0]
            if len(self.timestamps_s) >= 2
            else 0.0
        )
        rate = (len(self.timestamps_s) - 1) / duration if duration > 0.0 else 0.0
        p99 = _percentile(self.gaps_s, 0.99)
        maximum = max(self.gaps_s, default=math.inf)
        passed = (
            not self.stopped
            and rate >= 460.0
            and p99 <= 0.010
            and maximum < 0.020
        )
        return {
            "passed": passed,
            "rate_hz": rate,
            "p99_gap_s": p99,
            "max_gap_s": maximum,
            "stop_reason": self.stop_reason,
        }


@dataclass
class StartupHeartbeatGate:
    first_increment_s: float | None = None
    last_value: float | None = None
    increments: int = 0
    last_timestamp_s: float | None = None
    faulted: bool = False
    fault_reason: str = ""

    @property
    def stopped(self) -> bool:
        return self.faulted

    @property
    def stop_reason(self) -> str:
        return self.fault_reason

    def observe(self, value: float, now_s: float) -> bool:
        if self.faulted:
            return False
        heartbeat = float(value)
        now = float(now_s)
        if not math.isfinite(heartbeat) or not math.isfinite(now):
            self.faulted = True
            self.fault_reason = "nonfinite_startup_heartbeat"
            return False
        if self.last_timestamp_s is not None and now <= self.last_timestamp_s:
            self.faulted = True
            self.fault_reason = "nonpositive_startup_time"
            return False
        self.last_timestamp_s = now
        if self.last_value is None:
            self.last_value = heartbeat
            return False
        if heartbeat < self.last_value:
            self.faulted = True
            self.fault_reason = "startup_heartbeat_decreased"
            return False
        if heartbeat > self.last_value:
            if self.first_increment_s is None:
                self.first_increment_s = now
                self.increments = 1
            elif now - self.first_increment_s <= 0.25:
                self.increments += 1
            else:
                self.first_increment_s = now
                self.increments = 1
            self.last_value = heartbeat
        return self.increments >= 2 and self.first_increment_s is not None and now - self.first_increment_s <= 0.25


@dataclass(frozen=True)
class KinematicGateResult:
    allowed: bool
    qdot: tuple[float, float, float, float, float, float]
    twist: tuple[float, float, float, float, float, float]
    total_linear_m_s: float
    normal_m_s: float
    tangential_m_s: float
    angular_rad_s: float
    reason: str


def gate_qdot(
    contract: V4Contract,
    *,
    qdot: Sequence[float],
    jacobian_6x6: Sequence[Sequence[float]],
    normal_base: Sequence[float],
    observed_model_hashes: Mapping[str, str],
    motion_profile: V4MotionProfile | None = None,
) -> KinematicGateResult:
    if dict(observed_model_hashes) != dict(contract.model_hashes):
        raise RuntimeGuardError("robot-model/Jacobian hash binding differs")
    if len(qdot) != 6 or len(jacobian_6x6) != 6 or any(
        len(row) != 6 for row in jacobian_6x6
    ):
        raise RuntimeGuardError("qdot/Jacobian dimensions differ")
    q = tuple(float(value) for value in qdot)
    jacobian = tuple(
        tuple(float(value) for value in row) for row in jacobian_6x6
    )
    normal = tuple(float(value) for value in normal_base)
    if len(normal) != 3 or not all(
        math.isfinite(value)
        for value in (*q, *normal, *(value for row in jacobian for value in row))
    ):
        raise RuntimeGuardError("qdot/Jacobian/normal must be finite")
    normal_norm = math.sqrt(sum(value * value for value in normal))
    if not math.isclose(normal_norm, 1.0, rel_tol=0.0, abs_tol=1e-6):
        raise RuntimeGuardError("base-frame normal must be unit length")
    twist = tuple(
        sum(jacobian[row][column] * q[column] for column in range(6))
        for row in range(6)
    )
    linear = twist[:3]
    angular = twist[3:]
    total_linear = math.sqrt(sum(value * value for value in linear))
    normal_speed = abs(sum(linear[index] * normal[index] for index in range(3)))
    tangential_sq = max(0.0, total_linear * total_linear - normal_speed * normal_speed)
    tangential = math.sqrt(tangential_sq)
    angular_speed = math.sqrt(sum(value * value for value in angular))
    if motion_profile is None:
        qdot_cap = 0.15
        total_linear_cap = 0.0005
        normal_cap = 0.00035
        tangential_cap = 0.00035
        angular_cap = 0.05
    else:
        profile = _validate_motion_profile(motion_profile)
        qdot_cap = profile.qdot_cap_rad_s
        total_linear_cap = profile.total_linear_cap_m_s
        normal_cap = profile.normal_linear_cap_m_s
        tangential_cap = profile.tangential_cap_m_s
        angular_cap = profile.angular_cap_rad_s
    reason = ""
    if max(abs(value) for value in q) > qdot_cap:
        reason = "qdot_cap"
    elif total_linear > total_linear_cap:
        reason = "cartesian_total_cap"
    elif normal_speed > normal_cap:
        reason = "normal_component_cap"
    elif tangential > tangential_cap:
        reason = "tangential_component_cap"
    elif angular_speed > angular_cap:
        reason = "angular_cap"
    allowed = not reason
    return KinematicGateResult(
        allowed=allowed,
        qdot=q if allowed else (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        twist=twist,
        total_linear_m_s=total_linear,
        normal_m_s=normal_speed,
        tangential_m_s=tangential,
        angular_rad_s=angular_speed,
        reason=reason,
    )


def project_qdot_to_gate(
    contract: V4Contract,
    *,
    qdot: Sequence[float],
    jacobian_6x6: Sequence[Sequence[float]],
    normal_base: Sequence[float],
    observed_model_hashes: Mapping[str, str],
    motion_profile: V4MotionProfile | None = None,
) -> tuple[KinematicGateResult, float]:
    """Uniformly scale a finite qdot into the authoritative V4 envelope.

    This is the small typed equivalent of the mature V3 predicted-twist
    component clamp: direction is preserved, magnitude can only decrease, and
    the ordinary fail-closed gate remains the final authority.
    """

    original = tuple(float(value) for value in qdot)
    result = gate_qdot(
        contract,
        qdot=original,
        jacobian_6x6=jacobian_6x6,
        normal_base=normal_base,
        observed_model_hashes=observed_model_hashes,
        motion_profile=motion_profile,
    )
    if result.allowed:
        return result, 1.0
    if motion_profile is None:
        caps_and_values = (
            (0.15, max(abs(value) for value in original)),
            (0.0005, result.total_linear_m_s),
            (0.00035, result.normal_m_s),
            (0.00035, result.tangential_m_s),
            (0.05, result.angular_rad_s),
        )
    else:
        profile = _validate_motion_profile(motion_profile)
        caps_and_values = (
            (profile.qdot_cap_rad_s, max(abs(value) for value in original)),
            (profile.total_linear_cap_m_s, result.total_linear_m_s),
            (profile.normal_linear_cap_m_s, result.normal_m_s),
            (profile.tangential_cap_m_s, result.tangential_m_s),
            (profile.angular_cap_rad_s, result.angular_rad_s),
        )
    scale = min(
        1.0,
        *(0.999 * cap / value for cap, value in caps_and_values if value > cap),
    )
    if not 0.0 < scale < 1.0:
        return result, 1.0
    projected = gate_qdot(
        contract,
        qdot=tuple(value * scale for value in original),
        jacobian_6x6=jacobian_6x6,
        normal_base=normal_base,
        observed_model_hashes=observed_model_hashes,
        motion_profile=motion_profile,
    )
    if not projected.allowed:
        raise RuntimeGuardError(
            f"bounded qdot projection did not satisfy gate: {projected.reason}"
        )
    return projected, scale


def rescale_qdot_to_gate(
    contract: V4Contract,
    *,
    qdot: Sequence[float],
    previous_qdot: Sequence[float] | None,
    jacobian_6x6: Sequence[Sequence[float]],
    normal_base: Sequence[float],
    observed_model_hashes: Mapping[str, str],
    actual_dt_s: float,
    motion_profile: V4MotionProfile,
    dt_max_s: float = 0.02,
) -> tuple[KinematicGateResult, float]:
    """Apply V3 whole-delta slew semantics, then the typed V4 gate."""

    profile = _validate_motion_profile(motion_profile)
    from step5d_autotune_v4_r004.motion_profile import same_direction_qdot_rescale

    ramp = same_direction_qdot_rescale(
        qdot,
        previous_qdot,
        dt_s=actual_dt_s,
        max_slew_rad_s2=profile.host_slew_rad_s2,
        dt_max_s=dt_max_s,
    )
    result = gate_qdot(
        contract,
        qdot=ramp.qdot,
        jacobian_6x6=jacobian_6x6,
        normal_base=normal_base,
        observed_model_hashes=observed_model_hashes,
        motion_profile=profile,
    )
    return result, ramp.scale


__all__ = [
    "KinematicGateResult",
    "project_qdot_to_gate",
    "rescale_qdot_to_gate",
    "RuntimeGuardError",
    "StartupHeartbeatGate",
    "TimingGuard",
    "gate_qdot",
]
