"""Shared allocation-bounded moving-sphere safety kernel."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import math
from typing import Sequence

from .identity import canonical_sha256
from .stage_adapters import ControllerProgress, ControllerProgressPhase


SPHERE_RADIUS_M = 0.015
PROGRESS_MAX_AGE_NS = 2_000_000


class SphereReason(IntEnum):
    SPHERE_INACTIVE = 0
    SPHERE_STAGE_UNKNOWN = 1
    SPHERE_ACTUAL_BREACH = 2
    SPHERE_PREDICTED_STOP_BREACH = 3
    SPHERE_INPUT_MISSING = 4
    SPHERE_INPUT_NONFINITE = 5
    SPHERE_REFERENCE_MISMATCH = 6
    SPHERE_PROGRESS_STALE = 7
    SPHERE_STOP_BOUND_UNCERTIFIED = 8
    SPHERE_OK = 9
    SPHERE_PROGRESS_NONSEQUENTIAL = 10


@dataclass(frozen=True)
class StoppingBoundArtifact:
    reaction_latency_s: float
    acceleration_growth_m_s2: float
    minimum_deceleration_m_s2: float
    center_speed_bound_m_s: float
    center_acceleration_bound_m_s2: float
    numeric_margin_m: float
    evidence_sha256: tuple[str, ...]
    validity_domain: str
    certified: bool

    def __post_init__(self) -> None:
        numbers = (
            self.reaction_latency_s,
            self.acceleration_growth_m_s2,
            self.minimum_deceleration_m_s2,
            self.center_speed_bound_m_s,
            self.center_acceleration_bound_m_s2,
            self.numeric_margin_m,
        )
        if not all(math.isfinite(value) for value in numbers):
            raise ValueError("stopping-bound values must be finite")
        if self.reaction_latency_s < 0 or self.acceleration_growth_m_s2 < 0:
            raise ValueError("latency and acceleration growth must be non-negative")
        if self.minimum_deceleration_m_s2 <= 0 or self.numeric_margin_m <= 0:
            raise ValueError("minimum deceleration and margin must be positive")
        if self.center_speed_bound_m_s < 0 or self.center_acceleration_bound_m_s2 < 0:
            raise ValueError("center bounds must be non-negative")
        if not self.evidence_sha256 or any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in self.evidence_sha256
        ):
            raise ValueError("stopping bound requires evidence digests")
        if not self.validity_domain:
            raise ValueError("stopping bound requires a validity domain")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(
            {
                "schema": "ur-exp/stopping-bound-v1",
                "frame": "base",
                "units": "metres_seconds",
                "equation": "full_interval_triangle_sweep_v1",
                "reaction_latency_s": self.reaction_latency_s,
                "acceleration_growth_m_s2": self.acceleration_growth_m_s2,
                "minimum_deceleration_m_s2": self.minimum_deceleration_m_s2,
                "center_speed_bound_m_s": self.center_speed_bound_m_s,
                "center_acceleration_bound_m_s2": self.center_acceleration_bound_m_s2,
                "numeric_margin_m": self.numeric_margin_m,
                "evidence_sha256": list(self.evidence_sha256),
                "validity_domain": self.validity_domain,
                "certified": self.certified,
            }
        )


@dataclass(slots=True)
class SphereTickResult:
    stop: bool = True
    reason: SphereReason = SphereReason.SPHERE_INPUT_MISSING
    actual_distance_m: float = math.nan
    predicted_radial_bound_m: float = math.nan


class MovingSphereKernel:
    """Writes into a caller-owned result so the 500 Hz path reuses storage."""

    __slots__ = (
        "reference_sha256",
        "stopping_bound",
        "result",
        "last_sample_sequence",
        "last_controller_timestamp_ns",
    )

    def __init__(
        self,
        *,
        reference_sha256: str,
        stopping_bound: StoppingBoundArtifact | None,
        result: SphereTickResult | None = None,
    ) -> None:
        if len(reference_sha256) != 64 or any(c not in "0123456789abcdef" for c in reference_sha256):
            raise ValueError("reference_sha256 must be a lowercase SHA256")
        self.reference_sha256 = reference_sha256
        self.stopping_bound = stopping_bound
        self.result = result if result is not None else SphereTickResult()
        self.last_sample_sequence = 0
        self.last_controller_timestamp_ns = 0

    def tick(
        self,
        *,
        progress: ControllerProgress | None,
        tcp_base: Sequence[float] | None,
        tcp_speed_m_s: float | None,
    ) -> SphereTickResult:
        out = self.result
        out.stop = True
        out.actual_distance_m = math.nan
        out.predicted_radial_bound_m = math.nan
        if progress is None:
            out.reason = SphereReason.SPHERE_INPUT_MISSING
            return out
        if progress.phase is ControllerProgressPhase.UNKNOWN:
            out.reason = SphereReason.SPHERE_STAGE_UNKNOWN
            return out
        if progress.phase is ControllerProgressPhase.INACTIVE:
            out.stop = False
            out.reason = SphereReason.SPHERE_INACTIVE
            return out
        if progress.phase is not ControllerProgressPhase.ACTIVE_STAGE25:
            out.reason = SphereReason.SPHERE_STAGE_UNKNOWN
            return out
        if tcp_base is None or tcp_speed_m_s is None:
            out.reason = SphereReason.SPHERE_INPUT_MISSING
            return out
        if len(tcp_base) < 3:
            out.reason = SphereReason.SPHERE_INPUT_MISSING
            return out
        if progress.reference_sha256 != self.reference_sha256:
            out.reason = SphereReason.SPHERE_REFERENCE_MISMATCH
            return out
        if progress.age_ns < 0 or progress.age_ns > PROGRESS_MAX_AGE_NS:
            out.reason = SphereReason.SPHERE_PROGRESS_STALE
            return out
        if (
            not progress.monotonic
            or progress.sample_sequence <= self.last_sample_sequence
            or progress.controller_timestamp_ns <= self.last_controller_timestamp_ns
        ):
            out.reason = SphereReason.SPHERE_PROGRESS_NONSEQUENTIAL
            return out
        self.last_sample_sequence = progress.sample_sequence
        self.last_controller_timestamp_ns = progress.controller_timestamp_ns
        tcp_x = float(tcp_base[0])
        tcp_y = float(tcp_base[1])
        tcp_z = float(tcp_base[2])
        speed = float(tcp_speed_m_s)
        if (
            not math.isfinite(tcp_x)
            or not math.isfinite(tcp_y)
            or not math.isfinite(tcp_z)
            or not math.isfinite(speed)
            or speed < 0.0
            or not math.isfinite(progress.progress_s)
            or not math.isfinite(progress.center_x_m)
            or not math.isfinite(progress.center_y_m)
            or not math.isfinite(progress.center_z_m)
        ):
            out.reason = SphereReason.SPHERE_INPUT_NONFINITE
            return out
        bound = self.stopping_bound
        if bound is None or not bound.certified:
            out.reason = SphereReason.SPHERE_STOP_BOUND_UNCERTIFIED
            return out
        dx = tcp_x - progress.center_x_m
        dy = tcp_y - progress.center_y_m
        dz = tcp_z - progress.center_z_m
        d0 = math.sqrt(dx * dx + dy * dy + dz * dz)
        out.actual_distance_m = d0
        if d0 > SPHERE_RADIUS_M:
            out.reason = SphereReason.SPHERE_ACTUAL_BREACH
            return out
        latency = bound.reaction_latency_s
        v0 = speed
        v_latency = v0 + bound.acceleration_growth_m_s2 * latency
        stop_time = v_latency / bound.minimum_deceleration_m_s2
        tcp_sweep = (
            v0 * latency
            + 0.5 * bound.acceleration_growth_m_s2 * latency * latency
            + v_latency * v_latency / (2.0 * bound.minimum_deceleration_m_s2)
        )
        interval = latency + stop_time
        center_sweep = (
            bound.center_speed_bound_m_s * interval
            + 0.5 * bound.center_acceleration_bound_m_s2 * interval * interval
        )
        out.predicted_radial_bound_m = d0 + tcp_sweep + center_sweep + bound.numeric_margin_m
        if out.predicted_radial_bound_m > SPHERE_RADIUS_M:
            out.reason = SphereReason.SPHERE_PREDICTED_STOP_BREACH
            return out
        out.stop = False
        out.reason = SphereReason.SPHERE_OK
        return out
