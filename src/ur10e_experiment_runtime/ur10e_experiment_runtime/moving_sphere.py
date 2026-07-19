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
STOPPING_BOUND_EVIDENCE_ROLES = (
    "reaction_latency_s",
    "acceleration_growth_m_s2",
    "minimum_deceleration_m_s2",
    "center_speed_bound_m_s",
    "center_acceleration_bound_m_s2",
    "numeric_margin_m",
)
STOPPING_BOUND_EVIDENCE_STATUSES = (
    "certified_for_domain",
    "diagnostic_only",
    "missing",
)


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
    SPHERE_STOP_BOUND_DOMAIN_MISMATCH = 11
    SPHERE_STOP_BOUND_LATENCY_INSUFFICIENT = 12


@dataclass(frozen=True)
class StoppingBoundEvidenceComponent:
    role: str
    status: str
    value: float | None
    units: str
    frame: str
    method: str
    evidence_sha256: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.role not in STOPPING_BOUND_EVIDENCE_ROLES:
            raise ValueError(f"unknown stopping-bound evidence role: {self.role}")
        if self.status not in STOPPING_BOUND_EVIDENCE_STATUSES:
            raise ValueError(f"unknown stopping-bound evidence status: {self.status}")
        if not self.units or not self.frame or not self.method:
            raise ValueError("stopping-bound evidence metadata must be non-empty")
        if self.status == "missing":
            if self.value is not None or self.evidence_sha256:
                raise ValueError("missing evidence cannot carry a value or digest")
            return
        if self.value is None or not math.isfinite(float(self.value)):
            raise ValueError("present stopping-bound evidence requires a finite value")
        if not self.evidence_sha256 or any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in self.evidence_sha256
        ):
            raise ValueError("present stopping-bound evidence requires SHA256 digests")

    def document(self) -> dict[str, object]:
        return {
            "role": self.role,
            "status": self.status,
            "value": self.value,
            "units": self.units,
            "frame": self.frame,
            "method": self.method,
            "evidence_sha256": list(self.evidence_sha256),
        }


@dataclass(frozen=True)
class StoppingBoundEvidenceManifest:
    components: tuple[StoppingBoundEvidenceComponent, ...]
    validity_domain: str
    source_binding_sha256: str | None
    stop_transport_sha256: str | None
    deployment_readback_sha256: str | None

    def __post_init__(self) -> None:
        if not self.validity_domain:
            raise ValueError("stopping-bound evidence requires a validity domain")
        roles = tuple(component.role for component in self.components)
        if roles != STOPPING_BOUND_EVIDENCE_ROLES:
            raise ValueError("stopping-bound evidence roles/order differ")
        for name in (
            "source_binding_sha256",
            "stop_transport_sha256",
            "deployment_readback_sha256",
        ):
            value = getattr(self, name)
            if value is not None and (
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} must be a lowercase SHA256 when present")

    @property
    def certified(self) -> bool:
        return bool(
            all(
                component.status == "certified_for_domain"
                for component in self.components
            )
            and self.source_binding_sha256 is not None
            and self.stop_transport_sha256 is not None
            and self.deployment_readback_sha256 is not None
        )

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(
            {
                "schema": "ur-exp/stopping-bound-evidence-manifest-v1",
                "components": [component.document() for component in self.components],
                "validity_domain": self.validity_domain,
                "source_binding_sha256": self.source_binding_sha256,
                "stop_transport_sha256": self.stop_transport_sha256,
                "deployment_readback_sha256": self.deployment_readback_sha256,
                "certified": self.certified,
            }
        )


@dataclass(frozen=True)
class StoppingBoundArtifact:
    reaction_latency_s: float
    acceleration_growth_m_s2: float
    minimum_deceleration_m_s2: float
    center_speed_bound_m_s: float
    center_acceleration_bound_m_s2: float
    numeric_margin_m: float
    evidence_manifest: StoppingBoundEvidenceManifest

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
        if not self.evidence_manifest.certified:
            raise ValueError("stopping-bound artifact requires complete domain evidence")
        values = dict(zip(STOPPING_BOUND_EVIDENCE_ROLES, numbers, strict=True))
        for component in self.evidence_manifest.components:
            if component.value is None or component.value != values[component.role]:
                raise ValueError(
                    "stopping-bound numeric values differ from their evidence manifest"
                )

    @property
    def validity_domain(self) -> str:
        return self.evidence_manifest.validity_domain

    @property
    def certified(self) -> bool:
        return self.evidence_manifest.certified

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
                "evidence_manifest_fingerprint": self.evidence_manifest.fingerprint,
                "validity_domain": self.validity_domain,
                "certified": self.certified,
            }
        )


def build_offline_fixture_stopping_bound(
    *,
    reaction_latency_s: float,
    acceleration_growth_m_s2: float,
    minimum_deceleration_m_s2: float,
    center_speed_bound_m_s: float,
    center_acceleration_bound_m_s2: float,
    numeric_margin_m: float,
    evidence_sha256: str,
    validity_domain: str,
) -> StoppingBoundArtifact:
    """Build a source-bound synthetic bound that can never match a live domain."""

    if "not_live" not in validity_domain:
        raise ValueError("offline fixture validity domain must explicitly contain not_live")
    values = (
        reaction_latency_s,
        acceleration_growth_m_s2,
        minimum_deceleration_m_s2,
        center_speed_bound_m_s,
        center_acceleration_bound_m_s2,
        numeric_margin_m,
    )
    units = ("s", "m/s^2", "m/s^2", "m/s", "m/s^2", "m")
    methods = (
        "synthetic_fixture_latency",
        "synthetic_fixture_acceleration_growth",
        "synthetic_fixture_minimum_deceleration",
        "analytic_cycloid_2Aomega",
        "analytic_cycloid_Aomega_squared",
        "synthetic_fixture_numeric_margin",
    )
    manifest = StoppingBoundEvidenceManifest(
        components=tuple(
            StoppingBoundEvidenceComponent(
                role=role,
                status="certified_for_domain",
                value=value,
                units=unit,
                frame="base",
                method=method,
                evidence_sha256=(evidence_sha256,),
            )
            for role, value, unit, method in zip(
                STOPPING_BOUND_EVIDENCE_ROLES,
                values,
                units,
                methods,
                strict=True,
            )
        ),
        validity_domain=validity_domain,
        source_binding_sha256=evidence_sha256,
        stop_transport_sha256=evidence_sha256,
        deployment_readback_sha256=evidence_sha256,
    )
    return StoppingBoundArtifact(
        reaction_latency_s=reaction_latency_s,
        acceleration_growth_m_s2=acceleration_growth_m_s2,
        minimum_deceleration_m_s2=minimum_deceleration_m_s2,
        center_speed_bound_m_s=center_speed_bound_m_s,
        center_acceleration_bound_m_s2=center_acceleration_bound_m_s2,
        numeric_margin_m=numeric_margin_m,
        evidence_manifest=manifest,
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
        "required_validity_domain",
        "minimum_reaction_latency_s",
        "result",
        "last_controller_tick_seq",
        "last_controller_timestamp_ns",
    )

    def __init__(
        self,
        *,
        reference_sha256: str,
        stopping_bound: StoppingBoundArtifact | None,
        required_validity_domain: str | None = None,
        minimum_reaction_latency_s: float = 0.0,
        result: SphereTickResult | None = None,
    ) -> None:
        if len(reference_sha256) != 64 or any(c not in "0123456789abcdef" for c in reference_sha256):
            raise ValueError("reference_sha256 must be a lowercase SHA256")
        self.reference_sha256 = reference_sha256
        self.stopping_bound = stopping_bound
        if required_validity_domain is not None and not required_validity_domain:
            raise ValueError("required_validity_domain must be non-empty when set")
        if (
            not math.isfinite(minimum_reaction_latency_s)
            or minimum_reaction_latency_s < 0.0
        ):
            raise ValueError("minimum_reaction_latency_s must be finite and non-negative")
        self.required_validity_domain = required_validity_domain
        self.minimum_reaction_latency_s = minimum_reaction_latency_s
        self.result = result if result is not None else SphereTickResult()
        self.last_controller_tick_seq = 0
        self.last_controller_timestamp_ns = 0

    def reset(self) -> None:
        """Forget controller-stream continuity at an explicit trial boundary."""

        self.last_controller_tick_seq = 0
        self.last_controller_timestamp_ns = 0
        self.result.stop = True
        self.result.reason = SphereReason.SPHERE_INPUT_MISSING
        self.result.actual_distance_m = math.nan
        self.result.predicted_radial_bound_m = math.nan

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
            or progress.controller_tick_seq <= 0
            or (
                self.last_controller_tick_seq != 0
                and progress.controller_tick_seq
                != self.last_controller_tick_seq + 1
            )
            or progress.controller_timestamp_ns <= self.last_controller_timestamp_ns
        ):
            out.reason = SphereReason.SPHERE_PROGRESS_NONSEQUENTIAL
            return out
        self.last_controller_tick_seq = progress.controller_tick_seq
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
        if (
            self.required_validity_domain is not None
            and bound.validity_domain != self.required_validity_domain
        ):
            out.reason = SphereReason.SPHERE_STOP_BOUND_DOMAIN_MISMATCH
            return out
        if bound.reaction_latency_s < self.minimum_reaction_latency_s:
            out.reason = SphereReason.SPHERE_STOP_BOUND_LATENCY_INSUFFICIENT
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
