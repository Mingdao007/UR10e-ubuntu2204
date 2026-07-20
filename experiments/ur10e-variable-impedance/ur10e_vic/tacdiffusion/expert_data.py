"""Typed, hash-bound lineage for UR10e TacDiffusion expert force labels."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from ..controller_contract import evaluate_controller_runtime_contract
from .contracts import CONTROL_RATE_HZ, PERMITTED_PROGRAM_CLAIM


EXPERT_MANIFEST_SCHEMA_VERSION = 2
EXPERT_CONTROLLER_PROFILE = "polyscope-5.26-direct-torque-v2-500hz"
EXPERT_FORCE_DEFINITION_ID = "task_zft_minus_impedance_term_v1"
LABEL_SEMANTICS = "pre_filter_tcp_f_ff_si"
REQUIRED_ARTIFACT_ROLES = frozenset(
    {
        "controller_contract",
        "sensor_calibration",
        "sensor_to_tcp_transform",
        "wrench_bias",
        "task_zft",
        "expert_force_definition",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _vector(values: Iterable[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain {length} finite values")
    return result


def _sha256(value: Any, name: str) -> str:
    text = str(value)
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _orthonormal_rotation(values: Sequence[float]) -> bool:
    rows = (values[0:3], values[3:6], values[6:9])
    for index, row in enumerate(rows):
        norm = sum(value * value for value in row)
        if abs(norm - 1.0) > 1e-6:
            return False
        for other in rows[index + 1 :]:
            if abs(sum(a * b for a, b in zip(row, other))) > 1e-6:
                return False
    determinant = (
        values[0] * (values[4] * values[8] - values[5] * values[7])
        - values[1] * (values[3] * values[8] - values[5] * values[6])
        + values[2] * (values[3] * values[7] - values[4] * values[6])
    )
    return abs(determinant - 1.0) <= 1e-6


@dataclass(frozen=True)
class SensorCalibrationArtifact:
    schema: str
    sensor_serial: str
    sensor_frame_id: str
    axis_scale: tuple[float, ...] | Sequence[float]
    axis_sign: tuple[float, ...] | Sequence[float]
    source_evidence_sha256: str
    wrench_units: str = "N,Nm"

    def __post_init__(self) -> None:
        if self.schema != "ur10e_sensor_calibration/v1":
            raise ValueError("sensor calibration schema mismatch")
        if not self.sensor_serial.strip() or not self.sensor_frame_id.strip():
            raise ValueError("sensor calibration identity is required")
        object.__setattr__(self, "axis_scale", _vector(self.axis_scale, 6, "axis_scale"))
        signs = _vector(self.axis_sign, 6, "axis_sign")
        if any(value not in (-1.0, 1.0) for value in signs):
            raise ValueError("axis_sign values must be -1 or +1")
        object.__setattr__(self, "axis_sign", signs)
        object.__setattr__(self, "source_evidence_sha256", _sha256(self.source_evidence_sha256, "source_evidence_sha256"))
        if self.wrench_units != "N,Nm":
            raise ValueError("calibrated wrench must use SI N,Nm")


@dataclass(frozen=True)
class SensorToTCPTransformArtifact:
    schema: str
    from_frame_id: str
    to_frame_id: str
    translation_m: tuple[float, ...] | Sequence[float]
    rotation_row_major: tuple[float, ...] | Sequence[float]
    source_evidence_sha256: str

    def __post_init__(self) -> None:
        if self.schema != "ur10e_sensor_to_tcp_transform/v1":
            raise ValueError("sensor-to-TCP transform schema mismatch")
        if not self.from_frame_id.strip() or not self.to_frame_id.strip():
            raise ValueError("transform frame ids are required")
        object.__setattr__(self, "translation_m", _vector(self.translation_m, 3, "translation_m"))
        rotation = _vector(self.rotation_row_major, 9, "rotation_row_major")
        if not _orthonormal_rotation(rotation):
            raise ValueError("rotation_row_major must be a right-handed orthonormal rotation")
        object.__setattr__(self, "rotation_row_major", rotation)
        object.__setattr__(self, "source_evidence_sha256", _sha256(self.source_evidence_sha256, "source_evidence_sha256"))


@dataclass(frozen=True)
class WrenchBiasArtifact:
    schema: str
    sensor_serial: str
    sensor_frame_id: str
    bias_wrench_sensor_si: tuple[float, ...] | Sequence[float]
    standard_deviation_si: tuple[float, ...] | Sequence[float]
    sample_count: int
    started_at_s: float
    ended_at_s: float
    source_trace_sha256: str
    sensor_calibration_sha256: str
    sensor_to_tcp_transform_sha256: str

    def __post_init__(self) -> None:
        if self.schema != "ur10e_wrench_bias/v1":
            raise ValueError("wrench bias schema mismatch")
        if not self.sensor_serial.strip() or not self.sensor_frame_id.strip():
            raise ValueError("wrench bias identity is required")
        object.__setattr__(self, "bias_wrench_sensor_si", _vector(self.bias_wrench_sensor_si, 6, "bias_wrench_sensor_si"))
        deviation = _vector(self.standard_deviation_si, 6, "standard_deviation_si")
        if any(value < 0.0 for value in deviation):
            raise ValueError("standard_deviation_si must be non-negative")
        object.__setattr__(self, "standard_deviation_si", deviation)
        if self.sample_count <= 0 or not (0.0 <= self.started_at_s < self.ended_at_s):
            raise ValueError("wrench bias requires a positive, ordered sample window")
        for name in ("source_trace_sha256", "sensor_calibration_sha256", "sensor_to_tcp_transform_sha256"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))


@dataclass(frozen=True)
class TaskZFTArtifact:
    schema: str
    episode_id: str
    canonical_frame_id: str
    timestamps_s: tuple[float, ...] | Sequence[float]
    wrench_tcp_si: tuple[tuple[float, ...], ...] | Sequence[Sequence[float]]
    source_definition_sha256: str
    sensor_calibration_sha256: str
    sensor_to_tcp_transform_sha256: str
    wrench_bias_sha256: str

    def __post_init__(self) -> None:
        if self.schema != "ur10e_task_zft/v1":
            raise ValueError("task-ZFT schema mismatch")
        if not self.episode_id.strip() or not self.canonical_frame_id.strip():
            raise ValueError("task-ZFT episode and frame are required")
        timestamps = tuple(float(value) for value in self.timestamps_s)
        if not timestamps or not all(math.isfinite(value) and value >= 0.0 for value in timestamps):
            raise ValueError("task-ZFT timestamps must be finite and non-negative")
        if any(current <= previous for previous, current in zip(timestamps, timestamps[1:])):
            raise ValueError("task-ZFT timestamps must be strictly increasing")
        wrench = tuple(_vector(value, 6, "wrench_tcp_si") for value in self.wrench_tcp_si)
        if len(wrench) != len(timestamps):
            raise ValueError("task-ZFT requires one wrench per timestamp")
        object.__setattr__(self, "timestamps_s", timestamps)
        object.__setattr__(self, "wrench_tcp_si", wrench)
        for name in ("source_definition_sha256", "sensor_calibration_sha256", "sensor_to_tcp_transform_sha256", "wrench_bias_sha256"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))


@dataclass(frozen=True)
class ExpertForceDefinitionArtifact:
    schema: str
    definition_id: str
    stiffness: tuple[float, ...] | Sequence[float]
    damping: tuple[float, ...] | Sequence[float]
    component_abs_max: tuple[float, ...] | Sequence[float]
    source_code_sha256: str
    canonical_frame_id: str
    label_semantics: str = LABEL_SEMANTICS

    def __post_init__(self) -> None:
        if self.schema != "ur10e_expert_force_definition/v1" or self.definition_id != EXPERT_FORCE_DEFINITION_ID:
            raise ValueError("expert force definition schema or id mismatch")
        for name in ("stiffness", "damping", "component_abs_max"):
            values = _vector(getattr(self, name), 6, name)
            if any(value <= 0.0 for value in values):
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, values)
        object.__setattr__(self, "source_code_sha256", _sha256(self.source_code_sha256, "source_code_sha256"))
        if not self.canonical_frame_id.strip() or self.label_semantics != LABEL_SEMANTICS:
            raise ValueError("expert F_ff frame or label semantics mismatch")


def deterministic_expert_f_ff(
    task_zft_wrench_tcp_si: Sequence[float],
    pose_error_tcp: Sequence[float],
    ee_twist_tcp: Sequence[float],
    definition: ExpertForceDefinitionArtifact,
) -> tuple[float, ...]:
    """Compute the pre-filter expert label: ZFT - K*error + D*twist."""

    target = _vector(task_zft_wrench_tcp_si, 6, "task_zft_wrench_tcp_si")
    error = _vector(pose_error_tcp, 6, "pose_error_tcp")
    twist = _vector(ee_twist_tcp, 6, "ee_twist_tcp")
    raw = tuple(
        target[i] - definition.stiffness[i] * error[i] + definition.damping[i] * twist[i]
        for i in range(6)
    )
    return tuple(
        max(-definition.component_abs_max[i], min(definition.component_abs_max[i], raw[i]))
        for i in range(6)
    )


@dataclass(frozen=True)
class ExpertTraceManifestV2:
    trace_id: str
    source_kind: str
    dataset_split: str
    controller_profile: str
    canonical_frame_id: str
    sample_count: int
    artifact_bindings: Mapping[str, Mapping[str, str]]
    software_sha256: str
    package_sha256: str
    trace_sha256: str
    claim_boundary: str
    schema_version: int = EXPERT_MANIFEST_SCHEMA_VERSION
    control_rate_hz: int = CONTROL_RATE_HZ
    permitted_program_claim: str = PERMITTED_PROGRAM_CLAIM
    label_semantics: str = LABEL_SEMANTICS
    vision_included: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != EXPERT_MANIFEST_SCHEMA_VERSION:
            raise ValueError("expert trace manifest requires schema_version=2")
        if not self.trace_id.strip() or not self.canonical_frame_id.strip():
            raise ValueError("trace id and canonical frame are required")
        if self.source_kind != "ur10e_expert_demonstration":
            raise ValueError("v2 expert manifest requires UR10e expert demonstrations")
        if self.dataset_split not in {"train", "validation", "test"}:
            raise ValueError("v2 expert manifest requires a formal episode split")
        if self.controller_profile != EXPERT_CONTROLLER_PROFILE:
            raise ValueError("v2 expert manifest requires the PolyScope 5.26 Direct Torque profile")
        if self.sample_count <= 0 or self.control_rate_hz != CONTROL_RATE_HZ:
            raise ValueError("expert trace sample count/rate is invalid")
        if self.claim_boundary != "ur10e_expert_force_labels" or self.label_semantics != LABEL_SEMANTICS:
            raise ValueError("expert trace label semantics or claim boundary mismatch")
        if self.permitted_program_claim != PERMITTED_PROGRAM_CLAIM or self.vision_included:
            raise ValueError("expert trace exceeds the force-only UR10e claim boundary")
        if set(self.artifact_bindings) != REQUIRED_ARTIFACT_ROLES:
            raise ValueError("expert trace artifact roles are incomplete")
        normalized: dict[str, dict[str, str]] = {}
        for role, binding in self.artifact_bindings.items():
            if not isinstance(binding, Mapping) or set(binding) != {"path", "sha256"}:
                raise ValueError(f"invalid artifact binding: {role}")
            relative = str(binding["path"])
            if Path(relative).is_absolute() or len(Path(relative).parts) != 1 or relative in {".", ".."}:
                raise ValueError("artifact binding paths must be portable sibling basenames")
            normalized[role] = {"path": relative, "sha256": _sha256(binding["sha256"], f"{role}.sha256")}
        object.__setattr__(self, "artifact_bindings", normalized)
        for name in ("software_sha256", "package_sha256", "trace_sha256"):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))

    @property
    def training_eligible(self) -> bool:
        """A manifest alone never establishes training eligibility."""

        return False

    @property
    def frame_calibration_sha256(self) -> str:
        return self.artifact_bindings["sensor_calibration"]["sha256"]

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "artifact_bindings": self.artifact_bindings,
            "canonical_frame_id": self.canonical_frame_id,
            "claim_boundary": self.claim_boundary,
            "control_rate_hz": self.control_rate_hz,
            "controller_profile": self.controller_profile,
            "dataset_split": self.dataset_split,
            "label_semantics": self.label_semantics,
            "package_sha256": self.package_sha256,
            "permitted_program_claim": self.permitted_program_claim,
            "sample_count": self.sample_count,
            "schema_version": self.schema_version,
            "software_sha256": self.software_sha256,
            "source_kind": self.source_kind,
            "trace_id": self.trace_id,
            "trace_sha256": self.trace_sha256,
            "vision_included": self.vision_included,
        }

    @property
    def fingerprint_sha256(self) -> str:
        return canonical_sha256(self.canonical_payload())


def _load_mapping(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"artifact must be a JSON object: {path.name}")
    return payload


def validate_episode_manifest_artifacts(
    manifest: ExpertTraceManifestV2, manifest_path: str | Path
) -> dict[str, Any]:
    """Re-read and validate every artifact; no manifest readiness flag is trusted."""

    root = Path(manifest_path).parent
    payloads: dict[str, dict[str, Any]] = {}
    for role, binding in manifest.artifact_bindings.items():
        path = root / binding["path"]
        if not path.is_file() or sha256_file(path) != binding["sha256"]:
            raise ValueError(f"expert artifact hash mismatch: {role}")
        payloads[role] = _load_mapping(path)

    decision = evaluate_controller_runtime_contract(payloads["controller_contract"])
    if not decision.controller_verified:
        raise ValueError("controller contract artifacts do not verify the controller")
    calibration = SensorCalibrationArtifact(**payloads["sensor_calibration"])
    transform = SensorToTCPTransformArtifact(**payloads["sensor_to_tcp_transform"])
    bias = WrenchBiasArtifact(**payloads["wrench_bias"])
    zft = TaskZFTArtifact(**payloads["task_zft"])
    definition = ExpertForceDefinitionArtifact(**payloads["expert_force_definition"])
    hashes = {role: binding["sha256"] for role, binding in manifest.artifact_bindings.items()}
    if calibration.sensor_frame_id != transform.from_frame_id or transform.to_frame_id != manifest.canonical_frame_id:
        raise ValueError("sensor calibration/transform frame chain mismatch")
    if bias.sensor_serial != calibration.sensor_serial or bias.sensor_frame_id != calibration.sensor_frame_id:
        raise ValueError("wrench bias sensor identity mismatch")
    if bias.sensor_calibration_sha256 != hashes["sensor_calibration"] or bias.sensor_to_tcp_transform_sha256 != hashes["sensor_to_tcp_transform"]:
        raise ValueError("wrench bias lineage hash mismatch")
    if zft.episode_id != manifest.trace_id or zft.canonical_frame_id != manifest.canonical_frame_id:
        raise ValueError("task-ZFT episode/frame mismatch")
    if (
        zft.source_definition_sha256 != hashes["expert_force_definition"]
        or
        zft.sensor_calibration_sha256 != hashes["sensor_calibration"]
        or zft.sensor_to_tcp_transform_sha256 != hashes["sensor_to_tcp_transform"]
        or zft.wrench_bias_sha256 != hashes["wrench_bias"]
    ):
        raise ValueError("task-ZFT definition or lineage hash mismatch")
    if definition.canonical_frame_id != manifest.canonical_frame_id:
        raise ValueError("expert force definition frame mismatch")
    return {
        "controller_decision": decision,
        "sensor_calibration": calibration,
        "sensor_to_tcp_transform": transform,
        "wrench_bias": bias,
        "task_zft": zft,
        "expert_force_definition": definition,
    }
