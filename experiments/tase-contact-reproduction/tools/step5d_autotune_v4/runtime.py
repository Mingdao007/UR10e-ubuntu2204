"""Actual-dt timing and hash-bound Jacobian/qdot guard primitives for V4."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .contracts import V4Contract


class RuntimeGuardError(ValueError):
    """A V4 timing or kinematic safety invariant failed."""


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
            and rate >= 75.0
            and p99 <= 0.02
            and maximum < 0.08
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
    reason = ""
    if max(abs(value) for value in q) > 0.15:
        reason = "qdot_cap"
    elif total_linear > 0.0005:
        reason = "cartesian_total_cap"
    elif normal_speed > 0.00035:
        reason = "normal_component_cap"
    elif tangential > 0.00035:
        reason = "tangential_component_cap"
    elif angular_speed > 0.05:
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


__all__ = [
    "KinematicGateResult",
    "RuntimeGuardError",
    "StartupHeartbeatGate",
    "TimingGuard",
    "gate_qdot",
]
