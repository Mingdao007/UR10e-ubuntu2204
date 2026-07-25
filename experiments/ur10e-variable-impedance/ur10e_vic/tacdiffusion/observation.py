"""Versioned frame/unit/timestamp-safe TacDiffusion observations."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable, Sequence


OBSERVATION_SCHEMA_VERSION = "ur10e_tacdiffusion_observation/v2"
OBSERVATION_SLICE_DIMENSION = 42
OBSERVATION_DIMENSION = 84
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

    def __post_init__(self) -> None:
        if self.sequence < 0 or not math.isfinite(self.timestamp_s) or self.timestamp_s < 0.0:
            raise ValueError("observation sequence/timestamp is invalid")
        if not math.isfinite(self.external_sample_timestamp_s) or self.external_sample_timestamp_s < 0.0:
            raise ValueError("external sample timestamp is invalid")
        if self.external_sample_timestamp_s > self.timestamp_s + 1e-12:
            raise ValueError("external observation must be causally available")
        if self.external_source_sequence < 0:
            raise ValueError("external source sequence must be non-negative")
        if self.timestamp_s - self.external_sample_timestamp_s > 2.0 / self.lineage.external_rate_hz + 1e-12:
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
        return self.timestamp_s - self.external_sample_timestamp_s


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
        if len(self.vector) != OBSERVATION_DIMENSION:
            raise AssertionError("TacDiffusion observation schema dimension mismatch")

    @property
    def schema_version(self) -> str:
        return OBSERVATION_SCHEMA_VERSION

    @property
    def vector(self) -> tuple[float, ...]:
        return self.current.vector + self.previous.vector

    @property
    def temporal_alignment(self) -> dict[str, object]:
        return {"previous_sequence": self.previous.sequence, "current_sequence": self.current.sequence, "previous_timestamp_s": self.previous.timestamp_s, "current_timestamp_s": self.current.timestamp_s}
