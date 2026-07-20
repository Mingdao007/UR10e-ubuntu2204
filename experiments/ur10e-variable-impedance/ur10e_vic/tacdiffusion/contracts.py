"""Hash-bound, frame-explicit contracts for UR10e TacDiffusion preparation.

These contracts describe data and model output.  They never authorize a live
run or promote legacy velocity traces into force labels.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Iterable, Sequence


CONDITION_DIMENSION = 36
CONTROL_RATE_HZ = 500
RAW_WRENCH_RATE_HZ = 1000
MODEL_RATE_CANDIDATES_HZ = (500, 200, 100, 50)
PERMITTED_PROGRAM_CLAIM = "UR10e 500 Hz force-domain diffusion adaptation"
REQUIRED_EXPERT_CONTROLLER_PROFILE = "polyscope-5.25.2-direct-torque-v2-500hz"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _vector(values: Iterable[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length:
        raise ValueError(f"{name} must contain {length} values")
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must be finite")
    return result


def _sha256(value: str, name: str) -> str:
    text = str(value)
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


@dataclass(frozen=True)
class FrameCalibrationLineage:
    """Provenance for converting the Kunwei signal into the TCP frame."""

    external_sensor_frame_id: str
    canonical_tcp_frame_id: str
    twist_frame_id: str
    sensor_to_tcp_transform_sha256: str
    calibration_sha256: str
    zero_bias_sha256: str
    wrench_units: str = "N,Nm"
    twist_units: str = "m/s,rad/s"

    def __post_init__(self) -> None:
        for name in (
            "external_sensor_frame_id",
            "canonical_tcp_frame_id",
            "twist_frame_id",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be non-empty")
        if self.canonical_tcp_frame_id != self.twist_frame_id:
            raise ValueError("wrench and twist must use the same canonical TCP frame")
        for name in (
            "sensor_to_tcp_transform_sha256",
            "calibration_sha256",
            "zero_bias_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if self.wrench_units != "N,Nm":
            raise ValueError("wrench_units must be SI units N,Nm")
        if self.twist_units != "m/s,rad/s":
            raise ValueError("twist_units must be SI units m/s,rad/s")


@dataclass(frozen=True)
class ForceDiffusionObservation:
    """Current and previous 18D force observations in a common TCP frame.

    ``condition_36d`` is ordered as current external wrench, current internal
    wrench, current EE twist, then the same three previous-tick vectors.  No
    vision features are admitted by this schema.
    """

    sequence: int
    previous_timestamp_s: float
    current_timestamp_s: float
    previous_external_sample_timestamp_s: float
    current_external_sample_timestamp_s: float
    previous_external_wrench: tuple[float, ...] | Sequence[float]
    current_external_wrench: tuple[float, ...] | Sequence[float]
    previous_internal_wrench: tuple[float, ...] | Sequence[float]
    current_internal_wrench: tuple[float, ...] | Sequence[float]
    previous_ee_twist: tuple[float, ...] | Sequence[float]
    current_ee_twist: tuple[float, ...] | Sequence[float]
    lineage: FrameCalibrationLineage
    previous_external_source_sequence: int
    current_external_source_sequence: int
    raw_wrench_rate_hz: int = RAW_WRENCH_RATE_HZ
    control_rate_hz: int = CONTROL_RATE_HZ
    vision_included: bool = False

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("sequence must be at least one for a two-tick observation")
        timestamps = (
            self.previous_timestamp_s,
            self.current_timestamp_s,
            self.previous_external_sample_timestamp_s,
            self.current_external_sample_timestamp_s,
        )
        if not all(math.isfinite(value) and value >= 0.0 for value in timestamps):
            raise ValueError("observation timestamps must be finite and non-negative")
        if self.current_timestamp_s <= self.previous_timestamp_s:
            raise ValueError("current_timestamp_s must follow previous_timestamp_s")
        if self.previous_external_sample_timestamp_s > self.previous_timestamp_s:
            raise ValueError("previous external wrench must be causally available")
        if self.current_external_sample_timestamp_s > self.current_timestamp_s:
            raise ValueError("current external wrench must be causally available")
        if (
            self.previous_external_source_sequence < 0
            or self.current_external_source_sequence
            < self.previous_external_source_sequence
        ):
            raise ValueError("external source sequences must be monotonic")
        if self.raw_wrench_rate_hz != RAW_WRENCH_RATE_HZ:
            raise ValueError("raw Kunwei wrench rate must remain 1000 Hz")
        if self.control_rate_hz != CONTROL_RATE_HZ:
            raise ValueError("UR10e force-domain control rate must remain 500 Hz")
        if self.vision_included:
            raise ValueError("TacDiffusion adaptation is force-only; vision is not allowed")
        if not isinstance(self.lineage, FrameCalibrationLineage):
            raise ValueError("lineage must be a FrameCalibrationLineage")
        for name in (
            "previous_external_wrench",
            "current_external_wrench",
            "previous_internal_wrench",
            "current_internal_wrench",
            "previous_ee_twist",
            "current_ee_twist",
        ):
            object.__setattr__(self, name, _vector(getattr(self, name), 6, name))
        max_external_age_s = 2.0 / float(self.raw_wrench_rate_hz)
        if (
            self.previous_timestamp_s - self.previous_external_sample_timestamp_s
            > max_external_age_s + 1e-12
            or self.current_timestamp_s - self.current_external_sample_timestamp_s
            > max_external_age_s + 1e-12
        ):
            raise ValueError("causally synchronized external wrench is stale")
        if len(self.condition_36d) != CONDITION_DIMENSION:
            raise AssertionError("force diffusion condition contract is not 36D")

    @property
    def condition_36d(self) -> tuple[float, ...]:
        return (
            self.current_external_wrench
            + self.current_internal_wrench
            + self.current_ee_twist
            + self.previous_external_wrench
            + self.previous_internal_wrench
            + self.previous_ee_twist
        )


@dataclass(frozen=True)
class ForceDiffusionProposal:
    """A raw six-axis diffusion proposal; never a live authorization."""

    generated_at_s: float
    sequence: int
    raw_f_df: tuple[float, ...] | Sequence[float]
    model_sha256: str
    model_rate_hz: int
    age_s: float
    confidence: float
    valid: bool = True
    shadow_only: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.generated_at_s) or self.generated_at_s < 0.0:
            raise ValueError("generated_at_s must be finite and non-negative")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        object.__setattr__(self, "raw_f_df", _vector(self.raw_f_df, 6, "raw_f_df"))
        object.__setattr__(self, "model_sha256", _sha256(self.model_sha256, "model_sha256"))
        if self.model_rate_hz not in MODEL_RATE_CANDIDATES_HZ:
            raise ValueError("model_rate_hz must be 50, 100, 200, or 500")
        if not math.isfinite(self.age_s) or self.age_s < 0.0:
            raise ValueError("age_s must be finite and non-negative")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.valid and self.confidence <= 0.0:
            raise ValueError("valid proposals require confidence > 0")


@dataclass(frozen=True)
class ExpertTraceManifest:
    """A hash-bound trace identity with an explicit force-label boundary."""

    trace_id: str
    source_kind: str
    dataset_split: str
    controller_profile: str
    canonical_frame_id: str
    controller_verified: bool
    controller_readback_sha256: str
    sample_count: int
    has_expert_ff_labels: bool
    frame_calibration_sha256: str
    expert_policy_sha256: str
    software_sha256: str
    package_sha256: str
    trace_sha256: str
    claim_boundary: str
    control_rate_hz: int = CONTROL_RATE_HZ
    permitted_program_claim: str = PERMITTED_PROGRAM_CLAIM
    vision_included: bool = False

    def __post_init__(self) -> None:
        for name in ("trace_id", "controller_profile", "canonical_frame_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be non-empty")
        if self.source_kind not in {
            "ur10e_expert_demonstration",
            "legacy_v27_replay",
            "legacy_v29_replay",
            "synthetic_pipeline",
        }:
            raise ValueError("unsupported trace source_kind")
        if self.dataset_split not in {"train", "validation", "test", "pipeline_only"}:
            raise ValueError("unsupported dataset_split")
        if self.sample_count <= 0:
            raise ValueError("sample_count must be positive")
        if self.control_rate_hz != CONTROL_RATE_HZ:
            raise ValueError("expert trace control_rate_hz must be 500")
        if self.permitted_program_claim != PERMITTED_PROGRAM_CLAIM:
            raise ValueError("manifest must preserve the UR10e 500 Hz claim boundary")
        if self.vision_included:
            raise ValueError("the UR10e TacDiffusion mainline does not use vision")
        for name in (
            "controller_readback_sha256",
            "frame_calibration_sha256",
            "expert_policy_sha256",
            "software_sha256",
            "package_sha256",
            "trace_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if self.source_kind == "ur10e_expert_demonstration":
            if self.controller_profile != REQUIRED_EXPERT_CONTROLLER_PROFILE:
                raise ValueError(
                    "UR10e expert demonstrations require the verified 5.25.2 "
                    "Direct Torque V2 500 Hz controller profile"
                )
            if self.dataset_split == "pipeline_only" or not self.has_expert_ff_labels:
                raise ValueError("UR10e expert demonstrations require a formal split and F_ff labels")
            if self.claim_boundary != "ur10e_expert_force_labels":
                raise ValueError("expert demonstrations require the expert force-label boundary")
        else:
            if self.dataset_split != "pipeline_only" or self.has_expert_ff_labels:
                raise ValueError("legacy/synthetic traces are pipeline-only and cannot contain F_ff labels")
            if self.claim_boundary != "observation_pipeline_only":
                raise ValueError("legacy/synthetic traces require observation_pipeline_only")

    @property
    def training_eligible(self) -> bool:
        return (
            self.source_kind == "ur10e_expert_demonstration"
            and self.has_expert_ff_labels
            and self.controller_verified
            and self.dataset_split in {"train", "validation", "test"}
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "claim_boundary": self.claim_boundary,
            "control_rate_hz": self.control_rate_hz,
            "controller_profile": self.controller_profile,
            "canonical_frame_id": self.canonical_frame_id,
            "controller_verified": self.controller_verified,
            "controller_readback_sha256": self.controller_readback_sha256,
            "dataset_split": self.dataset_split,
            "expert_policy_sha256": self.expert_policy_sha256,
            "frame_calibration_sha256": self.frame_calibration_sha256,
            "has_expert_ff_labels": self.has_expert_ff_labels,
            "package_sha256": self.package_sha256,
            "permitted_program_claim": self.permitted_program_claim,
            "sample_count": self.sample_count,
            "software_sha256": self.software_sha256,
            "source_kind": self.source_kind,
            "trace_id": self.trace_id,
            "trace_sha256": self.trace_sha256,
            "vision_included": self.vision_included,
        }

    @property
    def fingerprint_sha256(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
