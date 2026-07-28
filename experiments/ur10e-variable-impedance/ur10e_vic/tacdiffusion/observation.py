"""Versioned frame/unit/timestamp-safe TacDiffusion observations."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence


OBSERVATION_SCHEMA_VERSION = "ur10e_tacdiffusion_observation/v2"
OBSERVATION_V3_SCHEMA_VERSION = "ur10e_tacdiffusion_observation/v3"
OBSERVATION_SLICE_DIMENSION = 42
OBSERVATION_DIMENSION = 84
OBSERVATION_HOST_BATCH_MAX_AGE_S = 0.080
OBSERVATION_UNITS = {
    "wrench": "N,Nm",
    "pose_error": "m,rad",
    "twist": "m/s,rad/s",
    "acceleration": "m/s^2,rad/s^2",
}


def _vector(values: Iterable[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain {length} finite values")
    return result


@dataclass(frozen=True)
class ObservationLineage:
    sensor_frame_id: str
    canonical_frame_id: str
    calibration_sha256: str
    normalization_sha256: str
    external_rate_hz: int = 1000
    control_rate_hz: int = 500
    units: tuple[tuple[str, str], ...] = tuple(sorted(OBSERVATION_UNITS.items()))

    def __post_init__(self) -> None:
        if not self.sensor_frame_id.strip() or not self.canonical_frame_id.strip():
            raise ValueError("observation frame ids must be non-empty")
        for name in ("calibration_sha256", "normalization_sha256"):
            value = getattr(self, name)
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if dict(self.units) != OBSERVATION_UNITS:
            raise ValueError("observation units are incompatible with schema v2")
        if self.external_rate_hz != 1000 or self.control_rate_hz != 500:
            raise ValueError("observation rates are incompatible with schema v2")


@dataclass(frozen=True)
class ObservationSlice:
    sequence: int
    timestamp_s: float
    external_sample_timestamp_s: float
    external_wrench: tuple[float, ...] | Sequence[float]
    internal_wrench: tuple[float, ...] | Sequence[float]
    actual_ee_twist: tuple[float, ...] | Sequence[float]
    desired_pose: tuple[float, ...] | Sequence[float]
    desired_twist: tuple[float, ...] | Sequence[float]
    desired_acceleration: tuple[float, ...] | Sequence[float]
    tracking_error: tuple[float, ...] | Sequence[float]
    lineage: ObservationLineage
    external_source_sequence: int = 0
    external_device_time_s: float | None = None
    external_host_visible_time_s: float | None = None
    external_batch_id: int | None = None
    external_sample_index: int | None = None
    external_hold: bool = False
    external_held_ticks: int = 0
    device_age_samples: int = 0
    host_age_s: float | None = None
    internal_wrench_valid: bool = True
    external_lineage_valid: bool = True

    def __post_init__(self) -> None:
        if self.sequence < 0 or not math.isfinite(self.timestamp_s) or self.timestamp_s < 0.0:
            raise ValueError("observation sequence/timestamp is invalid")
        if not math.isfinite(self.external_sample_timestamp_s) or self.external_sample_timestamp_s < 0.0:
            raise ValueError("external sample timestamp is invalid")
        if self.external_source_sequence < 0:
            raise ValueError("external source sequence must be non-negative")
        v3 = self.external_host_visible_time_s is not None or any(
            value is not None
            for value in (self.external_device_time_s, self.external_batch_id, self.external_sample_index)
        )
        # v2 used one timestamp for both the source sample and causal
        # availability.  In v3 device time is an ordering clock only; the
        # host-visible clock below is the authority for causal availability.
        if not v3 and self.external_sample_timestamp_s > self.timestamp_s + 1e-12:
            raise ValueError("external observation must be causally available")
        device_time = (
            self.external_sample_timestamp_s
            if self.external_device_time_s is None
            else float(self.external_device_time_s)
        )
        if not math.isfinite(device_time) or device_time < 0.0:
            raise ValueError("external device time is invalid")
        object.__setattr__(self, "external_device_time_s", device_time)
        if v3:
            if self.external_host_visible_time_s is None:
                raise ValueError("observation v3 requires host-visible time")
            host_visible = float(self.external_host_visible_time_s)
            if not math.isfinite(host_visible) or host_visible < 0.0:
                raise ValueError("external host-visible time is invalid")
            if host_visible > self.timestamp_s + 1e-12:
                raise ValueError("external observation host-visible time is not causal")
            if self.external_batch_id is None or self.external_sample_index is None:
                raise ValueError("observation v3 requires batch and sample identity")
            if self.external_batch_id < 0 or self.external_sample_index < 0:
                raise ValueError("external batch/sample identity is invalid")
            if self.host_age_s is None:
                host_age = self.timestamp_s - host_visible
            else:
                host_age = float(self.host_age_s)
            if not math.isfinite(host_age) or host_age < 0.0:
                raise ValueError("external host age is invalid")
            if host_age > OBSERVATION_HOST_BATCH_MAX_AGE_S + 1e-12:
                raise ValueError("external observation host delivery is stale")
            if not isinstance(self.external_hold, bool) or self.external_held_ticks < 0:
                raise ValueError("external hold metadata is invalid")
            if self.external_hold and self.external_held_ticks <= 0:
                raise ValueError("held observation must report held ticks")
            if not self.external_hold and self.external_held_ticks != 0:
                raise ValueError("fresh observation cannot report held ticks")
            if self.device_age_samples < 0:
                raise ValueError("device age samples must be non-negative")
            object.__setattr__(self, "external_host_visible_time_s", host_visible)
            object.__setattr__(self, "host_age_s", host_age)
        else:
            if self.external_held_ticks != 0 or self.device_age_samples != 0:
                raise ValueError("legacy observation cannot carry v3 hold metadata")
        if self.timestamp_s - self.external_sample_timestamp_s > 2.0 / self.lineage.external_rate_hz + 1e-12 and not v3:
            raise ValueError("external observation sample is stale for the 1000 Hz source")
        for name in ("external_wrench", "internal_wrench", "actual_ee_twist", "desired_pose", "desired_twist", "desired_acceleration", "tracking_error"):
            object.__setattr__(self, name, _vector(getattr(self, name), 6, name))
        if not isinstance(self.lineage, ObservationLineage):
            raise ValueError("lineage must be ObservationLineage")

    @property
    def vector(self) -> tuple[float, ...]:
        return self.external_wrench + self.internal_wrench + self.actual_ee_twist + self.desired_pose + self.desired_twist + self.desired_acceleration + self.tracking_error

    @property
    def external_sample_age_s(self) -> float:
        # v3 device time is not in the control clock domain.  Expose the
        # causal host age instead of manufacturing a cross-clock subtraction.
        if self.observation_v3:
            if self.host_age_s is None:
                raise RuntimeError("v3 observation host age is unavailable")
            return self.host_age_s
        return self.timestamp_s - self.external_sample_timestamp_s

    @property
    def observation_v3(self) -> bool:
        return self.external_host_visible_time_s is not None

    @property
    def external_host_age_s(self) -> float | None:
        return self.host_age_s

    @classmethod
    def from_causal_alignment(
        cls,
        *,
        sequence: int,
        control_timestamp_s: float,
        alignment: object,
        internal_wrench: Sequence[float],
        actual_ee_twist: Sequence[float],
        desired_pose: Sequence[float],
        desired_twist: Sequence[float],
        desired_acceleration: Sequence[float],
        tracking_error: Sequence[float],
        lineage: ObservationLineage,
        internal_wrench_valid: bool = True,
        external_lineage_valid: bool = True,
    ) -> "ObservationSlice":
        """Build a v3 slice from the causal joiner's observable receipt.

        The joiner object is intentionally duck-typed so this module does not
        import ``signals.py`` into the control path.  Device time is copied for
        ordering only; host-visible time is the causal availability authority.
        """

        return cls(
            sequence=sequence,
            timestamp_s=control_timestamp_s,
            external_sample_timestamp_s=float(alignment.source_device_time_s),
            external_wrench=alignment.wrench_tcp_si,
            internal_wrench=internal_wrench,
            actual_ee_twist=actual_ee_twist,
            desired_pose=desired_pose,
            desired_twist=desired_twist,
            desired_acceleration=desired_acceleration,
            tracking_error=tracking_error,
            lineage=lineage,
            external_source_sequence=int(alignment.source_sequence),
            external_device_time_s=float(alignment.source_device_time_s),
            external_host_visible_time_s=float(alignment.source_host_visible_time_s),
            external_batch_id=int(alignment.source_batch_id),
            external_sample_index=int(alignment.source_sample_index),
            external_hold=bool(alignment.external_hold),
            external_held_ticks=int(alignment.external_held_ticks),
            device_age_samples=int(alignment.device_age_samples),
            host_age_s=float(alignment.host_age_s),
            internal_wrench_valid=internal_wrench_valid,
            external_lineage_valid=external_lineage_valid,
        )


@dataclass(frozen=True)
class TacDiffusionObservation:
    previous: ObservationSlice
    current: ObservationSlice

    def __post_init__(self) -> None:
        if self.current.sequence <= self.previous.sequence:
            raise ValueError("temporal observation sequence must increase")
        if self.current.timestamp_s <= self.previous.timestamp_s:
            raise ValueError("current observation timestamp must follow previous")
        if self.current.lineage != self.previous.lineage:
            raise ValueError("temporal observations have incompatible frame/unit lineage")
        if self.current.external_source_sequence < self.previous.external_source_sequence:
            raise ValueError("external source sequence moved backwards")
        if self.current.observation_v3 != self.previous.observation_v3:
            raise ValueError("temporal observations must use one observation schema")
        if self.current.observation_v3:
            if self.current.external_device_time_s < self.previous.external_device_time_s - 1e-12:
                raise ValueError("external device time moved backwards")
            if self.current.external_host_visible_time_s < self.previous.external_host_visible_time_s - 1e-12:
                raise ValueError("external host-visible time moved backwards")
            if self.current.external_sample_index < self.previous.external_sample_index:
                raise ValueError("external sample index moved backwards")
        if len(self.vector) != OBSERVATION_DIMENSION:
            raise AssertionError("TacDiffusion observation schema dimension mismatch")

    @property
    def schema_version(self) -> str:
        return OBSERVATION_V3_SCHEMA_VERSION if self.current.observation_v3 else OBSERVATION_SCHEMA_VERSION

    @property
    def vector(self) -> tuple[float, ...]:
        return self.current.vector + self.previous.vector

    @property
    def temporal_alignment(self) -> dict[str, object]:
        result: dict[str, object] = {
            "previous_sequence": self.previous.sequence,
            "current_sequence": self.current.sequence,
            "previous_timestamp_s": self.previous.timestamp_s,
            "current_timestamp_s": self.current.timestamp_s,
        }
        if self.current.observation_v3:
            result.update(
                {
                    "previous_external_device_time_s": self.previous.external_device_time_s,
                    "current_external_device_time_s": self.current.external_device_time_s,
                    "previous_external_host_visible_time_s": self.previous.external_host_visible_time_s,
                    "current_external_host_visible_time_s": self.current.external_host_visible_time_s,
                    "previous_external_batch_id": self.previous.external_batch_id,
                    "current_external_batch_id": self.current.external_batch_id,
                    "previous_external_hold": self.previous.external_hold,
                    "current_external_hold": self.current.external_hold,
                }
            )
        return result
