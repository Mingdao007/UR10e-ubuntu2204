"""Hash-bound, frame-explicit contracts for UR10e TacDiffusion preparation.

These contracts describe data and model output.  They never authorize a live
run or promote legacy velocity traces into force labels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence


CONDITION_DIMENSION = 36
CONTROL_RATE_HZ = 500
RAW_WRENCH_RATE_HZ = 1000
MODEL_RATE_CANDIDATES_HZ = (500, 200, 100, 50)
PERMITTED_PROGRAM_CLAIM = "UR10e 500 Hz force-domain diffusion adaptation"
REQUIRED_EXPERT_CONTROLLER_PROFILE = "polyscope-5.25.2-direct-torque-v2-500hz"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,192}$")

DYNAMICS_SAMPLE_SCHEMA = "ur10e_tacdiffusion_dynamics_sample/v1"
DYNAMICS_RECEIPT_SCHEMA = "ur10e_tacdiffusion_dynamics_receipt/v1"
DYNAMICS_CONFORMANCE_SCHEMA = "ur10e_tacdiffusion_dynamics_conformance/v1"
DYNAMICS_SAMPLE_SCHEMA_VERSION = DYNAMICS_SAMPLE_SCHEMA
DYNAMICS_RECEIPT_SCHEMA_VERSION = DYNAMICS_RECEIPT_SCHEMA
DYNAMICS_CONFORMANCE_CONTRACT = "ur10e_tacdiffusion_controller_conformance/v1"
OFFLINE_FAKE_RTDE_FIXTURE_ID = "offline_fake_rtde_synthetic_fixture_v1"
DYNAMICS_MAX_SEQUENCE = (1 << 63) - 1
DYNAMICS_MAX_TIME_S = 1.0e12
DYNAMICS_MAX_INTERVAL_S = 0.100
DYNAMICS_AUTHORITATIVE_TORQUE_SOURCE = "previous_commanded_no_gravity_torque"


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


def _identity(value: object, name: str) -> str:
    text = str(value)
    if not _IDENTITY_RE.fullmatch(text):
        raise ValueError(f"{name} must be a bounded non-empty identity")
    return text


def _source_hashes(values: Mapping[str, str], name: str = "source_hashes") -> Mapping[str, str]:
    if not isinstance(values, Mapping) or not 1 <= len(values) <= 16:
        raise ValueError(f"{name} must contain one to sixteen SHA-256 identities")
    normalized: dict[str, str] = {}
    for key, value in values.items():
        normalized[_identity(key, f"{name} key")] = _sha256(str(value), f"{name}[{key}]")
    return MappingProxyType(dict(sorted(normalized.items())))


def _bounded_vector(values: Iterable[float], length: int, name: str, limit: float) -> tuple[float, ...]:
    result = _vector(values, length, name)
    if any(abs(value) > limit for value in result):
        raise ValueError(f"{name} exceeds the bounded dynamics contract")
    return result


def _bounded_matrix(
    values: Sequence[Sequence[float]],
    rows: int,
    columns: int,
    name: str,
    limit: float,
) -> tuple[tuple[float, ...], ...]:
    try:
        result = tuple(tuple(float(value) for value in row) for row in values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite {rows}x{columns} matrix") from exc
    if len(result) != rows or any(len(row) != columns for row in result):
        raise ValueError(f"{name} must be a finite {rows}x{columns} matrix")
    if not all(math.isfinite(value) for row in result for value in row):
        raise ValueError(f"{name} must be a finite {rows}x{columns} matrix")
    if any(abs(value) > limit for row in result for value in row):
        raise ValueError(f"{name} exceeds the bounded dynamics contract")
    return result


def _canonical_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DynamicsSample:
    """One explicitly identified previous-tick dynamics input.

    The sample is deliberately independent of any controller or sensor API.
    ``previous_commanded_no_gravity_torque_nm`` is the only torque authority;
    ``actual_current_torque_nm_shadow`` is retained for cross-checking only.
    """

    sequence: int
    timestamp_s: float
    previous_q: tuple[float, ...] | Sequence[float]
    previous_qd: tuple[float, ...] | Sequence[float]
    previous_commanded_no_gravity_torque_nm: tuple[float, ...] | Sequence[float]
    calibrated_jacobian: tuple[tuple[float, ...], ...] | Sequence[Sequence[float]]
    coriolis_torque_nm: tuple[float, ...] | Sequence[float]
    joint_damping_torque_nm: tuple[float, ...] | Sequence[float]
    source_hashes: Mapping[str, str]
    model_identity: str
    tcp_identity: str
    calibration_identity: str
    frame_id: str = "tool0_tcp"
    canonical_tcp_frame_id: str = "tool0_tcp"
    jacobian_frame_id: str = "tool0_tcp"
    source_identity: str = "offline_source"
    actual_current_torque_nm_shadow: tuple[float, ...] | Sequence[float] | None = None
    schema_version: str = DYNAMICS_SAMPLE_SCHEMA

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not 0 <= self.sequence <= DYNAMICS_MAX_SEQUENCE:
            raise ValueError("dynamics sequence is outside the bounded contract")
        if (
            not math.isfinite(self.timestamp_s)
            or self.timestamp_s < 0.0
            or self.timestamp_s > DYNAMICS_MAX_TIME_S
        ):
            raise ValueError("dynamics timestamp is outside the bounded contract")
        if self.schema_version != DYNAMICS_SAMPLE_SCHEMA:
            raise ValueError("unsupported dynamics sample schema")
        for name in (
            "frame_id",
            "canonical_tcp_frame_id",
            "jacobian_frame_id",
            "source_identity",
            "model_identity",
            "tcp_identity",
            "calibration_identity",
        ):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        if self.frame_id != self.canonical_tcp_frame_id:
            raise ValueError("dynamics frame does not match canonical TCP frame")
        if self.jacobian_frame_id != self.canonical_tcp_frame_id:
            raise ValueError("Jacobian frame does not match canonical TCP frame")
        object.__setattr__(self, "source_hashes", _source_hashes(self.source_hashes))
        for name in (
            "previous_q",
            "previous_qd",
            "previous_commanded_no_gravity_torque_nm",
            "coriolis_torque_nm",
            "joint_damping_torque_nm",
        ):
            object.__setattr__(
                self,
                name,
                _bounded_vector(getattr(self, name), 6, name, 1.0e6),
            )
        object.__setattr__(
            self,
            "calibrated_jacobian",
            _bounded_matrix(self.calibrated_jacobian, 6, 6, "calibrated_jacobian", 1.0e6),
        )
        if self.actual_current_torque_nm_shadow is not None:
            object.__setattr__(
                self,
                "actual_current_torque_nm_shadow",
                _bounded_vector(
                    self.actual_current_torque_nm_shadow,
                    6,
                    "actual_current_torque_nm_shadow",
                    1.0e6,
                ),
            )

    @property
    def time_s(self) -> float:
        return self.timestamp_s

    @property
    def tcp_frame_id(self) -> str:
        return self.canonical_tcp_frame_id

    @property
    def previous_commanded_no_gravity_torque(self) -> tuple[float, ...]:
        return self.previous_commanded_no_gravity_torque_nm

    @property
    def coriolis_torque(self) -> tuple[float, ...]:
        return self.coriolis_torque_nm

    @property
    def joint_damping_torque(self) -> tuple[float, ...]:
        return self.joint_damping_torque_nm

    @property
    def actual_current_shadow(self) -> tuple[float, ...] | None:
        return self.actual_current_torque_nm_shadow

    def validate_temporal_successor(
        self,
        previous: "DynamicsSample | None" = None,
        *,
        expected_previous_sequence: int | None = None,
        expected_previous_timestamp_s: float | None = None,
        max_interval_s: float = DYNAMICS_MAX_INTERVAL_S,
        now_s: float | None = None,
        max_age_s: float = DYNAMICS_MAX_INTERVAL_S,
    ) -> None:
        if previous is not None:
            if not isinstance(previous, DynamicsSample):
                raise ValueError("previous dynamics sample has the wrong type")
            expected_previous_sequence = previous.sequence
            expected_previous_timestamp_s = previous.timestamp_s
        if expected_previous_sequence is not None:
            if isinstance(expected_previous_sequence, bool) or self.sequence != expected_previous_sequence + 1:
                raise ValueError("dynamics sequence is nonconsecutive or stale")
        if expected_previous_timestamp_s is not None:
            if not math.isfinite(expected_previous_timestamp_s) or self.timestamp_s <= expected_previous_timestamp_s:
                raise ValueError("dynamics time is non-monotonic or stale")
            if not math.isfinite(max_interval_s) or max_interval_s <= 0.0:
                raise ValueError("dynamics max interval is invalid")
            if self.timestamp_s - expected_previous_timestamp_s > max_interval_s + 1e-12:
                raise ValueError("dynamics time gap is stale")
        if now_s is not None:
            if not math.isfinite(now_s) or now_s < self.timestamp_s:
                raise ValueError("dynamics current time is invalid")
            if not math.isfinite(max_age_s) or max_age_s <= 0.0:
                raise ValueError("dynamics max age is invalid")
            if now_s - self.timestamp_s > max_age_s + 1e-12:
                raise ValueError("dynamics sample is stale")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "timestamp_s": self.timestamp_s,
            "frame_id": self.frame_id,
            "canonical_tcp_frame_id": self.canonical_tcp_frame_id,
            "jacobian_frame_id": self.jacobian_frame_id,
            "source_identity": self.source_identity,
            "source_hashes": dict(self.source_hashes),
            "model_identity": self.model_identity,
            "tcp_identity": self.tcp_identity,
            "calibration_identity": self.calibration_identity,
            "previous_q": list(self.previous_q),
            "previous_qd": list(self.previous_qd),
            "previous_commanded_no_gravity_torque_nm": list(
                self.previous_commanded_no_gravity_torque_nm
            ),
            "calibrated_jacobian": [list(row) for row in self.calibrated_jacobian],
            "coriolis_torque_nm": list(self.coriolis_torque_nm),
            "joint_damping_torque_nm": list(self.joint_damping_torque_nm),
            "actual_current_torque_nm_shadow": (
                None
                if self.actual_current_torque_nm_shadow is None
                else list(self.actual_current_torque_nm_shadow)
            ),
        }

    @property
    def fingerprint_sha256(self) -> str:
        return _canonical_hash(self.canonical_payload())

    def as_json(self) -> dict[str, object]:
        return self.canonical_payload() | {"sample_fingerprint_sha256": self.fingerprint_sha256}


@dataclass(frozen=True)
class DynamicsConformanceBinding:
    """Explicit controller-conformance identity required for production data."""

    binding_id: str
    controller_receipt_identity: str
    controller_receipt_sha256: str
    sample_fingerprint_sha256: str
    model_identity: str
    tcp_identity: str
    calibration_identity: str
    contract_id: str = DYNAMICS_CONFORMANCE_CONTRACT
    schema_version: str = DYNAMICS_CONFORMANCE_SCHEMA
    binding_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != DYNAMICS_CONFORMANCE_SCHEMA:
            raise ValueError("unsupported dynamics conformance schema")
        for name in (
            "binding_id",
            "controller_receipt_identity",
            "model_identity",
            "tcp_identity",
            "calibration_identity",
        ):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        if self.contract_id != DYNAMICS_CONFORMANCE_CONTRACT:
            raise ValueError("unsupported dynamics conformance contract")
        for name in (
            "controller_receipt_sha256",
            "sample_fingerprint_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        expected = _canonical_hash(self.canonical_payload())
        if self.binding_sha256 is not None and self.binding_sha256 != expected:
            raise ValueError("dynamics conformance binding hash mismatch")
        object.__setattr__(self, "binding_sha256", expected)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "binding_id": self.binding_id,
            "controller_receipt_identity": self.controller_receipt_identity,
            "controller_receipt_sha256": self.controller_receipt_sha256,
            "sample_fingerprint_sha256": self.sample_fingerprint_sha256,
            "model_identity": self.model_identity,
            "tcp_identity": self.tcp_identity,
            "calibration_identity": self.calibration_identity,
        }

    def accepted_for(self, sample: DynamicsSample) -> bool:
        return bool(
            self.contract_id == DYNAMICS_CONFORMANCE_CONTRACT
            and self.sample_fingerprint_sha256 == sample.fingerprint_sha256
            and self.model_identity == sample.model_identity
            and self.tcp_identity == sample.tcp_identity
            and self.calibration_identity == sample.calibration_identity
            and self.binding_sha256 == _canonical_hash(self.canonical_payload())
        )

    def as_json(self) -> dict[str, object]:
        return self.canonical_payload() | {"binding_sha256": self.binding_sha256}


@dataclass(frozen=True)
class DynamicsReceipt:
    """Hash-bound result of one controller-equivalent dynamics reconstruction."""

    sequence: int
    timestamp_s: float
    internal_wrench_tcp_si: tuple[float, ...] | Sequence[float]
    sample_fingerprint_sha256: str
    frame_id: str
    canonical_tcp_frame_id: str
    jacobian_frame_id: str
    source_identity: str
    source_hashes: Mapping[str, str]
    model_identity: str
    tcp_identity: str
    calibration_identity: str
    source_kind: str
    valid: bool
    reason: str | None = None
    actual_current_shadow_wrench_tcp_si: tuple[float, ...] | Sequence[float] | None = None
    actual_current_shadow_delta_norm: float | None = None
    authoritative_torque_source: str = DYNAMICS_AUTHORITATIVE_TORQUE_SOURCE
    conformance_binding: DynamicsConformanceBinding | None = None
    fixture_identity: str | None = None
    schema_version: str = DYNAMICS_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not 0 <= self.sequence <= DYNAMICS_MAX_SEQUENCE:
            raise ValueError("dynamics receipt sequence is invalid")
        if not math.isfinite(self.timestamp_s) or not 0.0 <= self.timestamp_s <= DYNAMICS_MAX_TIME_S:
            raise ValueError("dynamics receipt timestamp is invalid")
        if self.schema_version != DYNAMICS_RECEIPT_SCHEMA:
            raise ValueError("unsupported dynamics receipt schema")
        object.__setattr__(self, "sample_fingerprint_sha256", _sha256(self.sample_fingerprint_sha256, "sample_fingerprint_sha256"))
        for name in (
            "frame_id",
            "canonical_tcp_frame_id",
            "jacobian_frame_id",
            "source_identity",
            "model_identity",
            "tcp_identity",
            "calibration_identity",
            "source_kind",
        ):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        if self.frame_id != self.canonical_tcp_frame_id:
            raise ValueError("dynamics receipt frame does not match canonical TCP frame")
        if self.jacobian_frame_id != self.canonical_tcp_frame_id:
            raise ValueError("dynamics receipt Jacobian frame mismatch")
        object.__setattr__(self, "source_hashes", _source_hashes(self.source_hashes))
        object.__setattr__(self, "internal_wrench_tcp_si", _bounded_vector(self.internal_wrench_tcp_si, 6, "internal_wrench_tcp_si", 1.0e9))
        if self.actual_current_shadow_wrench_tcp_si is not None:
            object.__setattr__(self, "actual_current_shadow_wrench_tcp_si", _bounded_vector(self.actual_current_shadow_wrench_tcp_si, 6, "actual_current_shadow_wrench_tcp_si", 1.0e9))
        if self.actual_current_shadow_delta_norm is not None and (
            not math.isfinite(self.actual_current_shadow_delta_norm)
            or self.actual_current_shadow_delta_norm < 0.0
        ):
            raise ValueError("actual-current shadow delta is invalid")
        if self.authoritative_torque_source != DYNAMICS_AUTHORITATIVE_TORQUE_SOURCE:
            raise ValueError("dynamics receipt torque authority is invalid")
        if self.conformance_binding is not None and not isinstance(
            self.conformance_binding, DynamicsConformanceBinding
        ):
            raise ValueError("dynamics receipt conformance binding has the wrong type")
        if self.source_kind not in {"production", "offline_fake_rtde", "synthetic_fixture"}:
            raise ValueError("unsupported dynamics receipt source kind")
        if self.reason is not None and not _identity(self.reason, "dynamics receipt reason"):
            raise ValueError("dynamics receipt reason is invalid")
        if self.source_kind in {"offline_fake_rtde", "synthetic_fixture"}:
            if self.fixture_identity != OFFLINE_FAKE_RTDE_FIXTURE_ID:
                object.__setattr__(self, "valid", False)
                if self.reason is None:
                    object.__setattr__(self, "reason", "missing_or_unknown_offline_fixture_identity")
        if self.source_kind == "production":
            if self.conformance_binding is None and self.valid:
                object.__setattr__(self, "valid", False)
                if self.reason is None:
                    object.__setattr__(self, "reason", "missing_controller_conformance_binding")
        if self.valid and self.reason is not None:
            raise ValueError("valid dynamics receipt cannot carry a failure reason")
        if not isinstance(self.valid, bool):
            raise ValueError("dynamics receipt valid flag must be boolean")

    @property
    def internal_wrench(self) -> tuple[float, ...]:
        return self.internal_wrench_tcp_si

    @property
    def wrench_tcp_si(self) -> tuple[float, ...]:
        return self.internal_wrench_tcp_si

    @property
    def actual_current_shadow(self) -> tuple[float, ...] | None:
        return self.actual_current_shadow_wrench_tcp_si

    @classmethod
    def from_sample(
        cls,
        sample: DynamicsSample,
        *,
        previous_sample: DynamicsSample | None = None,
        expected_previous_sequence: int | None = None,
        expected_previous_timestamp_s: float | None = None,
        now_s: float | None = None,
        max_age_s: float = DYNAMICS_MAX_INTERVAL_S,
        source_kind: str = "production",
        fixture_identity: str | None = None,
        conformance_binding: DynamicsConformanceBinding | None = None,
    ) -> "DynamicsReceipt":
        if not isinstance(sample, DynamicsSample):
            raise TypeError("DynamicsReceipt requires a DynamicsSample")
        sample.validate_temporal_successor(
            previous_sample,
            expected_previous_sequence=expected_previous_sequence,
            expected_previous_timestamp_s=expected_previous_timestamp_s,
            now_s=now_s,
            max_age_s=max_age_s,
        )
        from .signals import reconstruct_internal_wrench_from_previous_command

        estimate = reconstruct_internal_wrench_from_previous_command(
            previous_applied_no_gravity_joint_torque_nm=sample.previous_commanded_no_gravity_torque_nm,
            previous_jacobian_tcp=sample.calibrated_jacobian,
            previous_coriolis_joint_torque_nm=sample.coriolis_torque_nm,
            previous_joint_damping_torque_nm=sample.joint_damping_torque_nm,
            jacobian_frame_id=sample.jacobian_frame_id,
            canonical_tcp_frame_id=sample.canonical_tcp_frame_id,
            actual_current_as_torque_nm_shadow=sample.actual_current_torque_nm_shadow,
        )
        source_valid = False
        reason: str | None = None
        if source_kind in {"offline_fake_rtde", "synthetic_fixture"}:
            source_valid = fixture_identity == OFFLINE_FAKE_RTDE_FIXTURE_ID
            if not source_valid:
                reason = "missing_or_unknown_offline_fixture_identity"
        elif source_kind == "production":
            source_valid = conformance_binding is not None and conformance_binding.accepted_for(sample)
            if not source_valid:
                reason = "missing_or_invalid_controller_conformance_binding"
        else:
            reason = "unsupported_dynamics_source_kind"
        return cls(
            sequence=sample.sequence,
            timestamp_s=sample.timestamp_s,
            internal_wrench_tcp_si=estimate.wrench_tcp_si,
            sample_fingerprint_sha256=sample.fingerprint_sha256,
            frame_id=sample.frame_id,
            canonical_tcp_frame_id=sample.canonical_tcp_frame_id,
            jacobian_frame_id=sample.jacobian_frame_id,
            source_identity=sample.source_identity,
            source_hashes=sample.source_hashes,
            model_identity=sample.model_identity,
            tcp_identity=sample.tcp_identity,
            calibration_identity=sample.calibration_identity,
            source_kind=source_kind,
            valid=source_valid,
            reason=reason,
            actual_current_shadow_wrench_tcp_si=estimate.actual_current_shadow_wrench_tcp_si,
            actual_current_shadow_delta_norm=estimate.actual_current_shadow_delta_norm,
            conformance_binding=conformance_binding,
            fixture_identity=fixture_identity,
        )

    @classmethod
    def invalid_for_sample(cls, sample: DynamicsSample, reason: str) -> "DynamicsReceipt":
        if not isinstance(sample, DynamicsSample):
            raise TypeError("DynamicsReceipt requires a DynamicsSample")
        return cls(
            sequence=sample.sequence,
            timestamp_s=sample.timestamp_s,
            internal_wrench_tcp_si=(0.0,) * 6,
            sample_fingerprint_sha256=sample.fingerprint_sha256,
            frame_id=sample.frame_id,
            canonical_tcp_frame_id=sample.canonical_tcp_frame_id,
            jacobian_frame_id=sample.jacobian_frame_id,
            source_identity=sample.source_identity,
            source_hashes=sample.source_hashes,
            model_identity=sample.model_identity,
            tcp_identity=sample.tcp_identity,
            calibration_identity=sample.calibration_identity,
            source_kind="production",
            valid=False,
            reason=reason,
        )

    def validate_against(
        self,
        sample: DynamicsSample,
        *,
        previous_sample: DynamicsSample | None = None,
        now_s: float | None = None,
        max_age_s: float = DYNAMICS_MAX_INTERVAL_S,
    ) -> None:
        if not isinstance(sample, DynamicsSample):
            raise TypeError("dynamics receipt/sample mismatch")
        sample.validate_temporal_successor(previous_sample, now_s=now_s, max_age_s=max_age_s)
        fields_to_match = (
            ("sequence", self.sequence, sample.sequence),
            ("timestamp_s", self.timestamp_s, sample.timestamp_s),
            ("frame_id", self.frame_id, sample.frame_id),
            ("canonical_tcp_frame_id", self.canonical_tcp_frame_id, sample.canonical_tcp_frame_id),
            ("jacobian_frame_id", self.jacobian_frame_id, sample.jacobian_frame_id),
            ("source_identity", self.source_identity, sample.source_identity),
            ("model_identity", self.model_identity, sample.model_identity),
            ("tcp_identity", self.tcp_identity, sample.tcp_identity),
            ("calibration_identity", self.calibration_identity, sample.calibration_identity),
        )
        for name, observed, expected in fields_to_match:
            if observed != expected:
                raise ValueError(f"dynamics receipt/sample mismatch: {name}")
        if dict(self.source_hashes) != dict(sample.source_hashes):
            raise ValueError("dynamics receipt/sample mismatch: source_hashes")
        if self.sample_fingerprint_sha256 != sample.fingerprint_sha256:
            raise ValueError("dynamics receipt/sample mismatch: sample_fingerprint_sha256")
        from .signals import reconstruct_internal_wrench_from_previous_command

        estimate = reconstruct_internal_wrench_from_previous_command(
            previous_applied_no_gravity_joint_torque_nm=sample.previous_commanded_no_gravity_torque_nm,
            previous_jacobian_tcp=sample.calibrated_jacobian,
            previous_coriolis_joint_torque_nm=sample.coriolis_torque_nm,
            previous_joint_damping_torque_nm=sample.joint_damping_torque_nm,
            jacobian_frame_id=sample.jacobian_frame_id,
            canonical_tcp_frame_id=sample.canonical_tcp_frame_id,
            actual_current_as_torque_nm_shadow=sample.actual_current_torque_nm_shadow,
        )
        if any(
            not math.isclose(observed, expected, rel_tol=0.0, abs_tol=1e-9)
            for observed, expected in zip(self.internal_wrench_tcp_si, estimate.wrench_tcp_si)
        ):
            raise ValueError("dynamics receipt/sample mismatch: reconstructed wrench")
        if self.valid and self.source_kind == "production":
            if self.conformance_binding is None or not self.conformance_binding.accepted_for(sample):
                raise ValueError("dynamics receipt conformance binding is invalid")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "timestamp_s": self.timestamp_s,
            "internal_wrench_tcp_si": list(self.internal_wrench_tcp_si),
            "sample_fingerprint_sha256": self.sample_fingerprint_sha256,
            "frame_id": self.frame_id,
            "canonical_tcp_frame_id": self.canonical_tcp_frame_id,
            "jacobian_frame_id": self.jacobian_frame_id,
            "source_identity": self.source_identity,
            "source_hashes": dict(self.source_hashes),
            "model_identity": self.model_identity,
            "tcp_identity": self.tcp_identity,
            "calibration_identity": self.calibration_identity,
            "source_kind": self.source_kind,
            "valid": self.valid,
            "reason": self.reason,
            "actual_current_shadow_wrench_tcp_si": (
                None
                if self.actual_current_shadow_wrench_tcp_si is None
                else list(self.actual_current_shadow_wrench_tcp_si)
            ),
            "actual_current_shadow_delta_norm": self.actual_current_shadow_delta_norm,
            "authoritative_torque_source": self.authoritative_torque_source,
            "conformance_binding": (
                None if self.conformance_binding is None else self.conformance_binding.as_json()
            ),
            "fixture_identity": self.fixture_identity,
        }

    @property
    def receipt_fingerprint_sha256(self) -> str:
        return _canonical_hash(self.canonical_payload())

    def as_json(self) -> dict[str, object]:
        return self.canonical_payload() | {"receipt_fingerprint_sha256": self.receipt_fingerprint_sha256}
