"""Bounded, durable TacDiffusion episode recording primitives.

The producer-facing object in this module is deliberately small: it accepts a
validated-in-memory frame and performs one non-blocking bounded enqueue.  JSON
encoding and every filesystem operation belong to the background
``batch_fsync_10`` sealer.  Recorder health is a latched receipt consumed by a
task-level executor; it is never consulted by a 500 Hz controller loop.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, fields
import hashlib
import json
import math
from types import MappingProxyType
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from .contracts import (
    CONTROL_RATE_HZ,
    RAW_WRENCH_RATE_HZ,
    DynamicsReceipt,
    DynamicsSample,
    ForceAuthorityReceiptV1,
    FormalEpisodeManifestV1,
    ProductionDynamicsConformanceReceiptV1,
    validate_formal_force_source_payload,
)
from .episode_composition import ActionLabel, ActionLabelContext


EPISODE_ARTIFACT_SCHEMA_V2 = "ur10e_tacdiffusion_episode_artifact/v2"
EPISODE_FRAME_SCHEMA_V2 = "ur10e_tacdiffusion_episode_frame/v2"
EPISODE_ARTIFACT_SCHEMA = "ur10e_tacdiffusion_episode_artifact/v3"
EPISODE_FRAME_SCHEMA = "ur10e_tacdiffusion_episode_frame/v3"
EPISODE_ARTIFACT_SCHEMA_V3 = EPISODE_ARTIFACT_SCHEMA
EPISODE_FRAME_SCHEMA_V3 = EPISODE_FRAME_SCHEMA
EPISODE_ARTIFACT_SCHEMA_V4 = "ur10e_tacdiffusion_episode_artifact/v4"
EPISODE_FRAME_SCHEMA_V4 = "ur10e_tacdiffusion_episode_frame/v4"
EPISODE_TAIL_SCHEMA = "ur10e_tacdiffusion_episode_tail/v1"
EPISODE_TAIL_SCHEMA_V4 = "ur10e_tacdiffusion_episode_tail/v2"
DURABILITY_MODE = "batch_fsync_10"
RECORDER_HEALTH_SCHEMA = "ur10e_tacdiffusion_recorder_health/v1"
SPOOL_CAPACITY = 8192
BATCH_SIZE = 10
MAX_UNSEALED_TAIL = BATCH_SIZE - 1
OBSERVATION_DIMENSION = 84
ACTION_DIMENSION = 12
DEVICE_SAMPLES_PER_CONTROL_TICK = RAW_WRENCH_RATE_HZ // CONTROL_RATE_HZ


def _finite_vector(values: Sequence[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain {length} finite values")
    return result


def _optional_vector(
    values: Sequence[float] | None,
    length: int,
    name: str,
) -> tuple[float, ...] | None:
    return None if values is None else _finite_vector(values, length, name)


def _line(payload: Mapping[str, Any]) -> bytes:
    return _canonical_json_bytes(payload) + b"\n"


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    ).encode("utf-8")


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def _semantic_identity_status(metadata: Mapping[str, Any]) -> tuple[bool, str | None]:
    context = metadata.get("semantic_context")
    if not isinstance(context, Mapping):
        return False, None
    hashes = context.get("hash_identities")
    if not isinstance(hashes, Mapping) or not hashes:
        return False, None
    enabled = all(_is_sha256(value) for value in hashes.values())
    fingerprint = context.get("semantic_context_fingerprint_sha256")
    return enabled and _is_sha256(fingerprint), str(fingerprint) if _is_sha256(fingerprint) else None


_TYPED_RECEIPT_MAX_DEPTH = 8
_TYPED_RECEIPT_MAX_KEYS = 64
_TYPED_RECEIPT_MAX_ITEMS = 512
_TYPED_RECEIPT_MAX_STRING = 4096
_TYPED_RECEIPT_MAX_BYTES = 64 * 1024
V3_ROW_SEAL_FIELD = "row_sha256"


def _freeze_receipt_value(value: Any, *, depth: int = 0) -> Any:
    if depth > _TYPED_RECEIPT_MAX_DEPTH:
        raise ValueError("typed receipt payload is too deeply nested")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 10**18:
            raise ValueError("typed receipt integer is out of bounds")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("typed receipt payload contains a nonfinite value")
        return value
    if isinstance(value, str):
        if len(value) > _TYPED_RECEIPT_MAX_STRING:
            raise ValueError("typed receipt string is out of bounds")
        return value
    if isinstance(value, Mapping):
        if len(value) > _TYPED_RECEIPT_MAX_KEYS:
            raise ValueError("typed receipt mapping is out of bounds")
        frozen: dict[str, Any] = {}
        for key, nested in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("typed receipt keys must be nonempty strings")
            frozen[key] = _freeze_receipt_value(nested, depth=depth + 1)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        if len(value) > _TYPED_RECEIPT_MAX_ITEMS:
            raise ValueError("typed receipt sequence is out of bounds")
        return tuple(_freeze_receipt_value(item, depth=depth + 1) for item in value)
    raise ValueError("typed receipt payload must contain JSON values only")


def _plain_receipt_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_receipt_value(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_plain_receipt_value(item) for item in value]
    return value


@dataclass(frozen=True)
class TypedEpisodeReceipt:
    """Bounded immutable schema/payload receipt embedded in a v3 row."""

    schema: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.schema, str) or not self.schema.strip():
            raise ValueError("typed receipt schema is required")
        if len(self.schema) > _TYPED_RECEIPT_MAX_STRING:
            raise ValueError("typed receipt schema is out of bounds")
        if not isinstance(self.payload, Mapping):
            raise ValueError("typed receipt payload must be a mapping")
        frozen = _freeze_receipt_value(self.payload)
        if not isinstance(frozen, Mapping):
            raise ValueError("typed receipt payload must be a mapping")
        object.__setattr__(self, "payload", frozen)
        if len(self.canonical_bytes()) > _TYPED_RECEIPT_MAX_BYTES:
            raise ValueError("typed receipt serialization is out of bounds")

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "TypedEpisodeReceipt":
        if not isinstance(value, Mapping):
            raise ValueError("typed receipt must be an object")
        if "schema" not in value or "payload" not in value:
            raise ValueError("typed receipt schema/payload is incomplete")
        return cls(schema=value["schema"], payload=value["payload"])

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "payload": _plain_receipt_value(self.payload),
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.as_json())


@dataclass(frozen=True)
class ReferenceReceiptV1(TypedEpisodeReceipt):
    """Typed reference/derivative receipt required by formal V4 rows."""


@dataclass(frozen=True)
class ExpertActionReceiptV1(TypedEpisodeReceipt):
    """Typed expert-action ownership receipt required by formal V4 rows."""


@dataclass(frozen=True)
class TubeDecisionReceiptV1(TypedEpisodeReceipt):
    """Typed tube decision receipt required by formal V4 rows."""


# Explicit aliases keep the observation/reference role visible to callers
# while using one bounded typed contract for both optional receipt kinds.
EpisodeReceipt = TypedEpisodeReceipt
ObservationReceipt = TypedEpisodeReceipt
ReferenceReceipt = TypedEpisodeReceipt


def compute_v3_row_sha256(payload: Mapping[str, Any]) -> str:
    """Hash the canonical JSON bytes of a v3 row without its row seal field."""

    if not isinstance(payload, Mapping):
        raise ValueError("v3 row payload must be a mapping")
    unsigned = dict(payload)
    unsigned.pop(V3_ROW_SEAL_FIELD, None)
    return hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest()


def _validate_v3_optional_receipts(rows: Sequence[object]) -> None:
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        for field_name in ("observation_receipt", "reference_receipt"):
            value = row.get(field_name)
            if value is not None:
                try:
                    TypedEpisodeReceipt.from_json(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"episode v3 row {index} {field_name} is invalid"
                    ) from exc
        nested = row.get("typed_receipts")
        if nested is None:
            continue
        if not isinstance(nested, Mapping):
            raise ValueError(f"episode v3 row {index} typed receipts are invalid")
        for nested_name, field_name in (
            ("observation", "observation_receipt"),
            ("reference", "reference_receipt"),
        ):
            if nested_name not in nested:
                continue
            if field_name not in row or nested[nested_name] != row[field_name]:
                raise ValueError(
                    f"episode v3 row {index} {field_name} receipt mismatch"
                )


def _validate_v3_row_seals(
    rows: Sequence[object],
    *,
    require: bool,
) -> None:
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"episode v3 row {index} is not an object")
        if V3_ROW_SEAL_FIELD not in row:
            if require:
                raise ValueError(f"episode v3 row {index} row seal is missing")
            continue
        supplied = row[V3_ROW_SEAL_FIELD]
        if not _is_sha256(supplied):
            raise ValueError(f"episode v3 row {index} row seal is malformed")
        expected = compute_v3_row_sha256(row)
        if supplied != expected:
            raise ValueError(f"episode v3 row {index} row seal mismatch")


def _validate_v4_required_receipts(rows: Sequence[object]) -> None:
    """Validate the serialized shape of every formal V4 receipt set."""

    required = (
        "force_authority_receipt",
        "production_dynamics_receipt",
        "reference_receipt",
        "expert_action_receipt",
        "tube_decision_receipt",
        "formal_manifest",
    )
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or row.get("schema") != EPISODE_FRAME_SCHEMA_V4:
            raise ValueError(f"episode v4 row {index} schema mismatch")
        for name in required:
            value = row.get(name)
            if not isinstance(value, Mapping):
                raise ValueError(f"episode v4 row {index} {name} is missing")
        authority = row["force_authority_receipt"]
        if authority.get("schema_version") != "ur10e_tacdiffusion_force_authority_receipt/v1":
            raise ValueError(f"episode v4 row {index} force authority receipt schema mismatch")
        if authority.get("source_identity") != "kunwei_kwr75_tcp_raw_stream_v1":
            raise ValueError(f"episode v4 row {index} force authority identity mismatch")
        if authority.get("valid") is not True:
            raise ValueError(f"episode v4 row {index} force authority receipt is invalid")
        production = row["production_dynamics_receipt"]
        if production.get("schema_version") != "ur10e_tacdiffusion_production_dynamics_receipt/v1":
            raise ValueError(f"episode v4 row {index} production dynamics receipt schema mismatch")
        if production.get("source_kind") != "production" or production.get("previous_tick_only") is not True:
            raise ValueError(f"episode v4 row {index} production dynamics conformance is missing")
        if production.get("dynamics_receipt") != row.get("dynamics_receipt"):
            raise ValueError(f"episode v4 row {index} production/base dynamics receipt mismatch")
        manifest = row["formal_manifest"]
        if manifest.get("schema_version") != "ur10e_tacdiffusion_formal_episode_manifest/v2":
            raise ValueError(f"episode v4 row {index} formal manifest schema mismatch")
        expert = row["expert_action_receipt"]
        reference = row["reference_receipt"]
        tube = row["tube_decision_receipt"]
        for receipt_name, receipt in (
            ("expert action", expert),
            ("reference", reference),
            ("tube decision", tube),
        ):
            if not isinstance(receipt.get("payload"), Mapping):
                raise ValueError(f"episode v4 row {index} {receipt_name} payload is invalid")
        if expert.get("payload", {}).get("available") is not True or expert.get("payload", {}).get("shadow_only") is not False:
            raise ValueError(f"episode v4 row {index} expert action receipt is shadow-only")
        if reference.get("payload", {}).get("valid") is not True:
            raise ValueError(f"episode v4 row {index} reference receipt is invalid")
        if tube.get("payload", {}).get("accepted") is not True:
            raise ValueError(f"episode v4 row {index} tube decision is not accepted")
        if row.get("formal_receipts_valid") is not True:
            raise ValueError(f"episode v4 row {index} formal receipts are not valid")
        if row.get("row_valid") is not True:
            raise ValueError(f"episode v4 row {index} base row is invalid")
        nested_receipts = row.get("formal_receipts")
        if not isinstance(nested_receipts, Mapping):
            raise ValueError(f"episode v4 row {index} formal receipt index is missing")
        for nested_name, direct_name in (
            ("force_authority", "force_authority_receipt"),
            ("production_dynamics", "production_dynamics_receipt"),
            ("reference", "reference_receipt"),
            ("expert_action", "expert_action_receipt"),
            ("tube_decision", "tube_decision_receipt"),
        ):
            if nested_receipts.get(nested_name) != row.get(direct_name):
                raise ValueError(f"episode v4 row {index} formal receipt index mismatch: {nested_name}")
        try:
            observations = tuple(float(value) for value in row.get("observation_84d", ()))
            actions = tuple(float(value) for value in row.get("expert_action_12d", ()))
            if len(observations) != OBSERVATION_DIMENSION or not all(math.isfinite(value) for value in observations):
                raise ValueError(f"episode v4 row {index} observation dimension mismatch")
            if len(actions) != ACTION_DIMENSION or not all(math.isfinite(value) for value in actions):
                raise ValueError(f"episode v4 row {index} expert action dimension mismatch")
        except (TypeError, ValueError) as exc:
            if isinstance(exc, ValueError) and str(exc).startswith("episode v4 row"):
                raise
            raise ValueError(f"episode v4 row {index} numeric action/observation is invalid") from exc
        validate_formal_force_source_payload(row, path=f"episode_v4_row_{index}")


def _validate_v4_tail_lines(lines: Sequence[bytes], parsed: Sequence[object]) -> dict[str, Any]:
    if len(lines) < 2 or len(parsed) < 2:
        raise ValueError("episode v4 is missing its complete tail")
    tail = parsed[-1]
    if not isinstance(tail, Mapping) or tail.get("schema") != EPISODE_TAIL_SCHEMA_V4:
        raise ValueError("episode v4 tail is missing or incomplete")
    rows = parsed[1:-1]
    _validate_v4_required_receipts(rows)
    _validate_v3_row_seals(rows, require=True)
    row_lines = lines[1:-1]
    content_sha256 = hashlib.sha256(b"".join(row_lines)).hexdigest()
    if tail.get("content_sha256") != content_sha256:
        raise ValueError("episode v4 content identity mismatch")
    if tail.get("row_count") != len(rows):
        raise ValueError("episode v4 tail row count mismatch")
    expected_tail = dict(tail)
    supplied_tail_hash = expected_tail.pop("tail_sha256", None)
    if not _is_sha256(supplied_tail_hash) or supplied_tail_hash != hashlib.sha256(_line(expected_tail)).hexdigest():
        raise ValueError("episode v4 tail identity mismatch")
    last = rows[-1] if rows else None
    if tail.get("last_sample_index") != (None if last is None else last.get("sample_index")):
        raise ValueError("episode v4 tail sample identity mismatch")
    if tail.get("last_control_sequence") != (None if last is None else last.get("control_sequence")):
        raise ValueError("episode v4 tail sequence identity mismatch")
    if tail.get("complete_tail") is not True:
        raise ValueError("episode v4 tail is not complete")
    return {
        "rows": tuple(rows),
        "tail": dict(tail),
        "content_sha256": content_sha256,
        "tail_sha256": supplied_tail_hash,
        "header_sha256": hashlib.sha256(lines[0]).hexdigest(),
        "last_sample_index": None if last is None else last.get("sample_index"),
        "last_control_sequence": None if last is None else last.get("control_sequence"),
    }


@dataclass(frozen=True)
class EpisodeFrameV2:
    """One retained 500 Hz row, including invalid/torn-source receipts."""

    episode_id: str
    sample_index: int
    control_sequence: int
    control_time_s: float
    observation_84d: Sequence[float]
    expert_action_12d: Sequence[float]
    applied_action_12d: Sequence[float]
    echoed_action_12d: Sequence[float]
    action_generation: int
    action_age_ticks: int
    action_echo_coherent: bool
    external_device_time_s: float | None
    external_host_visible_time_s: float | None
    external_batch_id: int | None
    external_sample_index: int | None
    external_hold: bool
    external_held_ticks: int
    device_age_samples: int
    host_age_s: float | None
    internal_wrench_valid: bool
    external_lineage_valid: bool = True
    control_time_strict: bool = True
    source_row_torn: bool = False
    source_row_invalid: bool = False
    echoed_action_valid: bool = True
    recorder_valid: bool = True
    controller_time_s: float | None = None
    control_clock: str = "host_monotonic_elapsed"
    # Semantic fields are explicit so a diagnostic command cannot be inferred
    # as an expert label from the numeric action vector alone.  Defaults keep
    # the v2 reader/writer compatible with existing offline fixtures.
    expert_label_available: bool = True
    expert_action_source: str = "deterministic_expert"
    action_label_semantics: str = "deterministic_expert_guarded_action_12d_v1"
    diagnostic_command_12d: Sequence[float] | None = None
    controller_echo_12d: Sequence[float] | None = None
    observation_history_valid: bool = True
    reference_derivatives_valid: bool = True
    desired_pose_6d: Sequence[float] | None = None
    desired_twist_6d: Sequence[float] | None = None
    desired_acceleration_6d: Sequence[float] | None = None
    reference_sample_id: str | None = None
    candidate_window: bool = False
    capture_phase: str = "unknown"
    receiver_state: int | None = None

    def __post_init__(self) -> None:
        if not self.episode_id.strip() or self.sample_index < 0 or self.control_sequence < 0:
            raise ValueError("episode frame identity is invalid")
        if not math.isfinite(self.control_time_s) or self.control_time_s < 0.0:
            raise ValueError("episode frame control time is invalid")
        if self.controller_time_s is not None and (
            not math.isfinite(self.controller_time_s) or self.controller_time_s < 0.0
        ):
            raise ValueError("episode frame controller time is invalid")
        object.__setattr__(
            self,
            "observation_84d",
            _finite_vector(self.observation_84d, OBSERVATION_DIMENSION, "observation_84d"),
        )
        for name in ("expert_action_12d", "applied_action_12d", "echoed_action_12d"):
            object.__setattr__(
                self,
                name,
                _finite_vector(getattr(self, name), ACTION_DIMENSION, name),
            )
        if self.action_generation < 0 or self.action_age_ticks < 0:
            raise ValueError("episode action generation/age is invalid")
        if self.external_device_time_s is not None and (
            not math.isfinite(self.external_device_time_s)
            or self.external_device_time_s < 0.0
        ):
            raise ValueError("external device time is invalid")
        if self.external_host_visible_time_s is not None and (
            not math.isfinite(self.external_host_visible_time_s)
            or self.external_host_visible_time_s < 0.0
        ):
            raise ValueError("external host-visible time is invalid")
        for name in ("external_batch_id", "external_sample_index"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} is invalid")
        if self.external_held_ticks < 0 or self.device_age_samples < 0:
            raise ValueError("external hold/device age is invalid")
        if self.external_hold and self.external_held_ticks <= 0:
            raise ValueError("held episode frame must report held ticks")
        if not self.external_hold and self.external_held_ticks != 0:
            raise ValueError("fresh episode frame cannot report held ticks")
        if self.host_age_s is not None and (
            not math.isfinite(self.host_age_s) or self.host_age_s < 0.0
        ):
            raise ValueError("external host age is invalid")
        if not self.control_clock.strip():
            raise ValueError("control clock identity is required")
        if not isinstance(self.expert_label_available, bool):
            raise ValueError("expert_label_available must be boolean")
        if not self.expert_action_source.strip() or not self.action_label_semantics.strip():
            raise ValueError("expert action source and label semantics are required")
        object.__setattr__(
            self,
            "diagnostic_command_12d",
            _optional_vector(self.diagnostic_command_12d, ACTION_DIMENSION, "diagnostic_command_12d"),
        )
        object.__setattr__(
            self,
            "controller_echo_12d",
            _optional_vector(self.controller_echo_12d, ACTION_DIMENSION, "controller_echo_12d"),
        )
        for name in ("desired_pose_6d", "desired_twist_6d", "desired_acceleration_6d"):
            object.__setattr__(
                self,
                name,
                _optional_vector(getattr(self, name), 6, name),
            )
        if self.reference_sample_id is not None and not str(self.reference_sample_id).strip():
            raise ValueError("reference_sample_id must be non-empty when present")
        if not isinstance(self.observation_history_valid, bool) or not isinstance(self.reference_derivatives_valid, bool):
            raise ValueError("observation/reference validity flags must be boolean")
        if not isinstance(self.candidate_window, bool) or not self.capture_phase.strip():
            raise ValueError("capture window/phase metadata is invalid")
        if self.receiver_state is not None and int(self.receiver_state) < 0:
            raise ValueError("receiver state metadata is invalid")

    @property
    def row_valid(self) -> bool:
        return bool(
            self.recorder_valid
            and self.control_time_strict
            and self.external_lineage_valid
            and self.internal_wrench_valid
            and self.action_echo_coherent
            and self.echoed_action_valid
            and not self.source_row_torn
            and not self.source_row_invalid
        )

    @property
    def recorder_validity_flags(self) -> dict[str, bool]:
        return {
            "row_valid": self.row_valid,
            "recorder_valid": bool(self.recorder_valid),
            "control_time_strict": bool(self.control_time_strict),
            "external_lineage_valid": bool(self.external_lineage_valid),
            "internal_wrench_valid": bool(self.internal_wrench_valid),
            "action_echo_coherent": bool(self.action_echo_coherent),
            "echoed_action_valid": bool(self.echoed_action_valid),
            "source_row_torn": bool(self.source_row_torn),
            "source_row_invalid": bool(self.source_row_invalid),
        }

    def as_json(self) -> dict[str, Any]:
        payload = {
            field.name: getattr(self, field.name)
            for field in fields(EpisodeFrameV2)
        }
        payload["schema"] = EPISODE_FRAME_SCHEMA_V2
        payload["recorder_validity_flags"] = self.recorder_validity_flags
        payload["semantic_flags"] = {
            "expert_label_available": self.expert_label_available,
            "expert_action_source": self.expert_action_source,
            "action_label_semantics": self.action_label_semantics,
            "observation_history_valid": self.observation_history_valid,
            "reference_derivatives_valid": self.reference_derivatives_valid,
            "candidate_window": self.candidate_window,
            "capture_phase": self.capture_phase,
        }
        return payload


# The short alias makes the new-write contract discoverable without removing
# the v1 ``ExpertEpisodeFrame`` reader/writer.
EpisodeFrame = EpisodeFrameV2
_EPISODE_FRAME_FIELDS = tuple(field.name for field in fields(EpisodeFrameV2))


@dataclass(frozen=True)
class EpisodeFrameV3(EpisodeFrameV2):
    """Canonical row with typed dynamics/action receipts and identity binding."""

    dynamics_sample: DynamicsSample | None = None
    dynamics_receipt: DynamicsReceipt | None = None
    action_label_context: ActionLabelContext | None = None
    action_label: ActionLabel | None = None
    observation_receipt: TypedEpisodeReceipt | None = None
    reference_receipt: TypedEpisodeReceipt | None = None
    identity_enabled: bool = True
    semantic_context_fingerprint_sha256: str | None = None
    row_sha256: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.identity_enabled, bool):
            raise ValueError("v3 identity_enabled must be boolean")
        for name in ("observation_receipt", "reference_receipt"):
            receipt = getattr(self, name)
            if receipt is not None and not isinstance(receipt, TypedEpisodeReceipt):
                raise ValueError(f"v3 {name} must use a typed receipt")
        if self.row_sha256 is not None and not _is_sha256(self.row_sha256):
            raise ValueError("v3 row seal is malformed")
        if (self.dynamics_sample is None) != (self.dynamics_receipt is None):
            raise ValueError("v3 dynamics sample and receipt must be paired")
        if self.dynamics_sample is not None and self.dynamics_receipt is not None:
            if not isinstance(self.dynamics_sample, DynamicsSample) or not isinstance(self.dynamics_receipt, DynamicsReceipt):
                raise ValueError("v3 dynamics fields must use typed contracts")
            self.dynamics_receipt.validate_against(self.dynamics_sample)
        if (self.action_label_context is None) != (self.action_label is None):
            raise ValueError("v3 action context and label must be paired")
        if self.action_label_context is not None and self.action_label is not None:
            if not isinstance(self.action_label_context, ActionLabelContext) or not isinstance(self.action_label, ActionLabel):
                raise ValueError("v3 action fields must use typed contracts")
            if (
                self.action_label_context.sequence != self.control_sequence
                or not math.isclose(self.action_label_context.timestamp_s, self.control_time_s, rel_tol=0.0, abs_tol=1e-12)
                or self.action_label_context.frame_id != self.action_label.frame_id
                or self.action_label.sequence != self.action_label_context.sequence
                or not math.isclose(self.action_label.timestamp_s, self.action_label_context.timestamp_s, rel_tol=0.0, abs_tol=1e-12)
            ):
                raise ValueError("v3 action context/label identity mismatch")
            if self.action_label.available and tuple(self.expert_action_12d) != self.action_label.expert_action_12d:
                raise ValueError("v3 expert action does not match typed action label")
            if self.semantic_context_fingerprint_sha256 is not None and self.action_label_context.semantic_context_fingerprint_sha256 is not None:
                if self.action_label_context.semantic_context_fingerprint_sha256 != self.semantic_context_fingerprint_sha256:
                    raise ValueError("v3 semantic context fingerprint mismatch")
        if self.semantic_context_fingerprint_sha256 is not None:
            if len(self.semantic_context_fingerprint_sha256) != 64 or any(
                char not in "0123456789abcdef" for char in self.semantic_context_fingerprint_sha256
            ):
                raise ValueError("v3 semantic context fingerprint is invalid")

    @classmethod
    def from_v2(cls, frame: EpisodeFrameV2) -> "EpisodeFrameV3":
        if not isinstance(frame, EpisodeFrameV2):
            raise TypeError("v3 compatibility conversion requires EpisodeFrameV2")
        # V2 has already completed the expensive vector validation.  Copying
        # its frozen fields directly keeps the compatibility adapter out of
        # the producer's 500 Hz budget; v3 typed rows still validate normally
        # at construction time.
        accepted = object.__new__(cls)
        accepted.__dict__.update(frame.__dict__)
        accepted.__dict__.update(
            dynamics_sample=None,
            dynamics_receipt=None,
            action_label_context=None,
            action_label=None,
            identity_enabled=False,
            semantic_context_fingerprint_sha256=None,
        )
        return accepted

    @property
    def source_identity_enabled(self) -> bool:
        return self.identity_enabled

    @property
    def typed_receipts_valid(self) -> bool:
        return bool(
            self.identity_enabled
            and self.semantic_context_fingerprint_sha256 is not None
            and self.dynamics_sample is not None
            and self.dynamics_receipt is not None
            and self.dynamics_receipt.valid
            and self.action_label_context is not None
            and self.action_label is not None
            and self.action_label.available
            and not self.action_label.shadow_only
        )

    def _unsigned_json(self) -> dict[str, Any]:
        payload = super().as_json()
        payload["schema"] = EPISODE_FRAME_SCHEMA
        payload["dynamics_sample"] = None if self.dynamics_sample is None else self.dynamics_sample.as_json()
        payload["dynamics_receipt"] = None if self.dynamics_receipt is None else self.dynamics_receipt.as_json()
        payload["action_label_context"] = None if self.action_label_context is None else self.action_label_context.as_json()
        payload["action_label"] = None if self.action_label is None else self.action_label.as_json()
        payload["observation_receipt"] = (
            None if self.observation_receipt is None else self.observation_receipt.as_json()
        )
        payload["reference_receipt"] = (
            None if self.reference_receipt is None else self.reference_receipt.as_json()
        )
        payload["identity_enabled"] = self.identity_enabled
        payload["semantic_context_fingerprint_sha256"] = self.semantic_context_fingerprint_sha256
        payload["typed_receipts_valid"] = self.typed_receipts_valid
        payload["typed_receipts"] = {
            "dynamics": payload["dynamics_receipt"],
            "action_label": payload["action_label"],
            "observation": payload["observation_receipt"],
            "reference": payload["reference_receipt"],
        }
        return payload

    @property
    def row_valid(self) -> bool:
        return bool(super().row_valid and self.typed_receipts_valid)

    @property
    def row_seal_valid(self) -> bool:
        try:
            payload = self.as_json()
        except (TypeError, ValueError):
            return False
        return payload.get(V3_ROW_SEAL_FIELD) == compute_v3_row_sha256(payload)

    def as_json(self) -> dict[str, Any]:
        payload = self._unsigned_json()
        expected = compute_v3_row_sha256(payload)
        if self.row_sha256 is not None and self.row_sha256 != expected:
            raise ValueError("v3 row seal mismatch")
        payload[V3_ROW_SEAL_FIELD] = expected
        return payload


@dataclass(frozen=True)
class EpisodeFrameV4(EpisodeFrameV3):
    """Canonical formal row with all five required typed receipts.

    ``EpisodeFrameV2`` and ``EpisodeFrameV3`` remain valid compatibility
    readers, but neither can satisfy ``formal_eligible``.  A V4 row is only
    constructed with a Kunwei authority receipt, a production previous-tick
    dynamics conformance receipt, reference, expert-action, and tube-decision
    receipts plus one hash-bound formal manifest.
    """

    force_authority_receipt: ForceAuthorityReceiptV1 | None = None
    production_dynamics_receipt: ProductionDynamicsConformanceReceiptV1 | DynamicsReceipt | None = None
    expert_action_receipt: ExpertActionReceiptV1 | TypedEpisodeReceipt | None = None
    tube_decision_receipt: TubeDecisionReceiptV1 | TypedEpisodeReceipt | None = None
    formal_manifest: FormalEpisodeManifestV1 | None = None
    schema_version_v4: str = EPISODE_FRAME_SCHEMA_V4

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.schema_version_v4 != EPISODE_FRAME_SCHEMA_V4:
            raise ValueError("unsupported formal EpisodeFrameV4 schema")
        if not isinstance(self.force_authority_receipt, ForceAuthorityReceiptV1):
            raise ValueError("formal V4 row requires a typed Kunwei force authority receipt")
        if isinstance(self.production_dynamics_receipt, DynamicsReceipt):
            object.__setattr__(
                self,
                "production_dynamics_receipt",
                ProductionDynamicsConformanceReceiptV1(self.production_dynamics_receipt),
            )
        if not isinstance(self.production_dynamics_receipt, ProductionDynamicsConformanceReceiptV1):
            raise ValueError("formal V4 row requires a production dynamics conformance receipt")
        if not isinstance(self.expert_action_receipt, TypedEpisodeReceipt):
            raise ValueError("formal V4 row requires a typed expert action receipt")
        if not isinstance(self.tube_decision_receipt, TypedEpisodeReceipt):
            raise ValueError("formal V4 row requires a typed tube decision receipt")
        if not isinstance(self.reference_receipt, TypedEpisodeReceipt):
            raise ValueError("formal V4 row requires a typed reference receipt")
        if not isinstance(self.formal_manifest, FormalEpisodeManifestV1):
            raise ValueError("formal V4 row requires a typed formal episode manifest")
        production = self.production_dynamics_receipt
        assert isinstance(production, ProductionDynamicsConformanceReceiptV1)
        if (
            not isinstance(self.dynamics_receipt, DynamicsReceipt)
            or self.dynamics_receipt.receipt_fingerprint_sha256
            != production.dynamics_receipt.receipt_fingerprint_sha256
        ):
            raise ValueError("formal V4 production dynamics receipt is not bound to the base dynamics receipt")
        if production.sequence != self.control_sequence or not math.isclose(
            production.timestamp_s,
            self.control_time_s,
            rel_tol=0.0,
            abs_tol=1.0e-9,
        ):
            raise ValueError("formal V4 dynamics receipt does not match row tick")
        if self.force_authority_receipt.sequence != self.control_sequence:
            raise ValueError("formal V4 force authority receipt does not match row tick")
        if self.force_authority_receipt.frame_id != self.formal_manifest.force_authority.canonical_tcp_frame_id:
            raise ValueError("formal V4 force authority frame does not match manifest")
        if self.formal_manifest.force_authority.fingerprint_sha256 != self.force_authority_receipt.authority.fingerprint_sha256:
            raise ValueError("formal V4 force authority does not match manifest")
        if self.formal_manifest.contact_guard_profile.authority.fingerprint_sha256 != self.formal_manifest.force_authority.fingerprint_sha256:
            raise ValueError("formal V4 guard authority does not match manifest")
        reference_payload = dict(self.reference_receipt.payload)
        expert_payload = dict(self.expert_action_receipt.payload)
        tube_payload = dict(self.tube_decision_receipt.payload)
        if reference_payload.get("valid") is not True or reference_payload.get("shadow_only", False) is True:
            raise ValueError("formal V4 reference receipt is not valid")
        if expert_payload.get("available") is not True or expert_payload.get("shadow_only") is not False:
            raise ValueError("formal V4 expert action receipt is unavailable or shadow-only")
        if tube_payload.get("accepted") is not True or tube_payload.get("shadow_only", False) is True:
            raise ValueError("formal V4 tube decision receipt is not accepted")
        expert_action = expert_payload.get("expert_action_12d", expert_payload.get("action_12d"))
        if expert_action is not None and tuple(float(value) for value in expert_action) != tuple(self.expert_action_12d):
            raise ValueError("formal V4 expert action receipt does not match row action")
        if self.action_label is None or not self.action_label.available or self.action_label.shadow_only:
            raise ValueError("formal V4 row requires an available non-shadow typed action label")
        validate_formal_force_source_payload(
            {
                "force_authority_receipt": self.force_authority_receipt.as_json(),
                "production_dynamics_receipt": production.as_json(),
                "reference_receipt": self.reference_receipt.as_json(),
                "expert_action_receipt": self.expert_action_receipt.as_json(),
                "tube_decision_receipt": self.tube_decision_receipt.as_json(),
                "formal_manifest": self.formal_manifest.as_json(),
            },
            path="episode_frame_v4",
        )

    @property
    def formal_receipts_valid(self) -> bool:
        return bool(
            isinstance(self.force_authority_receipt, ForceAuthorityReceiptV1)
            and isinstance(self.production_dynamics_receipt, ProductionDynamicsConformanceReceiptV1)
            and isinstance(self.reference_receipt, TypedEpisodeReceipt)
            and isinstance(self.expert_action_receipt, TypedEpisodeReceipt)
            and isinstance(self.tube_decision_receipt, TypedEpisodeReceipt)
            and isinstance(self.formal_manifest, FormalEpisodeManifestV1)
            and self.production_dynamics_receipt.dynamics_receipt.valid
            and self.action_label is not None
            and self.action_label.available
            and not self.action_label.shadow_only
        )

    @property
    def formal_eligible(self) -> bool:
        return bool(super().row_valid and self.formal_receipts_valid and self.formal_manifest is not None)

    def _unsigned_json_v4(self) -> dict[str, Any]:
        payload = self._unsigned_json()
        payload["schema"] = EPISODE_FRAME_SCHEMA_V4
        payload["schema_version_v4"] = self.schema_version_v4
        production = self.production_dynamics_receipt
        assert isinstance(production, ProductionDynamicsConformanceReceiptV1)
        payload["force_authority_receipt"] = self.force_authority_receipt.as_json()
        payload["production_dynamics_receipt"] = production.as_json()
        payload["expert_action_receipt"] = self.expert_action_receipt.as_json()
        payload["tube_decision_receipt"] = self.tube_decision_receipt.as_json()
        payload["formal_manifest"] = self.formal_manifest.as_json()
        payload["formal_receipts_valid"] = self.formal_receipts_valid
        payload["row_valid"] = self.row_valid
        payload["formal_receipts"] = {
            "force_authority": payload["force_authority_receipt"],
            "production_dynamics": payload["production_dynamics_receipt"],
            "reference": payload["reference_receipt"],
            "expert_action": payload["expert_action_receipt"],
            "tube_decision": payload["tube_decision_receipt"],
        }
        return payload

    @property
    def row_valid(self) -> bool:
        return bool(super().row_valid and self.formal_receipts_valid)

    @property
    def row_seal_valid(self) -> bool:
        try:
            payload = self.as_json()
        except (TypeError, ValueError):
            return False
        return payload.get(V3_ROW_SEAL_FIELD) == compute_v3_row_sha256(payload)

    def as_json(self) -> dict[str, Any]:
        payload = self._unsigned_json_v4()
        expected = compute_v3_row_sha256(payload)
        if self.row_sha256 is not None and self.row_sha256 != expected:
            raise ValueError("formal V4 row seal mismatch")
        payload[V3_ROW_SEAL_FIELD] = expected
        return payload


# The canonical short name follows the current writer version.  Explicit
# ``EpisodeFrameV2`` remains available for compatibility adapters/readers.
EpisodeFrame = EpisodeFrameV3


def _with_recorder_metadata(
    frame: EpisodeFrameV2,
    *,
    sample_index: int,
    control_time_strict: bool,
    external_lineage_valid: bool,
    external_hold: bool,
    external_held_ticks: int,
    device_age_samples: int,
) -> EpisodeFrameV2:
    """Reuse already-validated vectors while replacing recorder-owned scalars.

    ``dataclasses.replace`` reruns the full 84D + 36D vector validation at
    500 Hz. The input frame is already frozen and validated; these six bounded
    scalar fields are the only recorder-owned values.
    """

    if sample_index < 0 or external_held_ticks < 0 or device_age_samples < 0:
        raise ValueError("recorder metadata is invalid")
    frame_type = type(frame)
    accepted = object.__new__(frame_type)
    replacements = {
        "sample_index": sample_index,
        "control_time_strict": bool(control_time_strict),
        "external_lineage_valid": bool(external_lineage_valid),
        "external_hold": bool(external_hold),
        "external_held_ticks": external_held_ticks,
        "device_age_samples": device_age_samples,
    }
    accepted.__dict__.update(frame.__dict__)
    accepted.__dict__.update(replacements)
    return accepted


@dataclass(frozen=True)
class RecorderHealth:
    capacity: int
    queue_depth: int
    enqueued_rows: int
    durable_rows: int
    rejected_rows: int
    dropped_rows: int
    overflowed: bool
    stalled: bool
    writer_error: str | None
    fault: str | None
    durability_mode: str
    unsealed_tail: int
    max_unsealed_tail: int
    sealed: bool
    manifest_written: bool
    tamper_free: bool

    @property
    def ok(self) -> bool:
        return not any(
            (
                self.overflowed,
                self.stalled,
                self.writer_error,
                self.fault,
                self.dropped_rows,
                self.rejected_rows,
            )
        )

    def as_json(self) -> dict[str, Any]:
        return asdict(self) | {"ok": self.ok}


class _FaultLatch:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reason: str | None = None

    def latch(self, reason: str) -> str:
        with self._lock:
            if self._reason is None:
                self._reason = str(reason)
            return self._reason

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason


class BoundedEpisodeSpool:
    """A non-overwriting, non-blocking producer queue."""

    def __init__(
        self,
        capacity: int = SPOOL_CAPACITY,
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if capacity <= 0:
            raise ValueError("spool capacity must be positive")
        self.capacity = int(capacity)
        self._queue: queue.Queue[EpisodeFrameV2] = queue.Queue(maxsize=self.capacity)
        self._fault = _FaultLatch()
        self._clock = clock
        self._enqueued = 0
        self._rejected = 0
        # Diagnostic latency history is bounded too; recorder correctness must
        # not quietly reintroduce an unbounded producer-side list.
        self._enqueue_latencies_us: deque[float] = deque(maxlen=self.capacity)

    def enqueue(self, frame: EpisodeFrameV2) -> bool:
        """Try once and return immediately; this method performs no FS/JSON IO."""

        started = self._clock()
        if self._fault.reason is not None:
            self._rejected += 1
            self._enqueue_latencies_us.append((self._clock() - started) * 1e6)
            return False
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            self._rejected += 1
            self._fault.latch("spool_overflow")
            self._enqueue_latencies_us.append((self._clock() - started) * 1e6)
            return False
        self._enqueued += 1
        self._enqueue_latencies_us.append((self._clock() - started) * 1e6)
        return True

    def try_enqueue(self, frame: EpisodeFrameV2) -> bool:
        return self.enqueue(frame)

    def put_nowait(self, frame: EpisodeFrameV2) -> bool:
        return self.enqueue(frame)

    def get(self, timeout_s: float) -> EpisodeFrameV2 | None:
        try:
            return self._queue.get(timeout=timeout_s)
        except queue.Empty:
            return None

    def task_done(self) -> None:
        self._queue.task_done()

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def unfinished_tasks(self) -> int:
        return self._queue.unfinished_tasks

    @property
    def fault(self) -> str | None:
        return self._fault.reason

    @property
    def enqueued_rows(self) -> int:
        return self._enqueued

    @property
    def rejected_rows(self) -> int:
        return self._rejected

    @property
    def enqueue_latency_us(self) -> tuple[float, ...]:
        return tuple(self._enqueue_latencies_us)


class RecorderError(RuntimeError):
    """Latched recorder fault surfaced at task cadence."""


class BatchFsync10Sealer:
    """Background JSONL sealer with one fsync per ten rows."""

    def __init__(
        self,
        spool: BoundedEpisodeSpool,
        artifact_path: str | Path,
        manifest_path: str | Path,
        *,
        episode_id: str,
        metadata: Mapping[str, Any] | None = None,
        batch_size: int = BATCH_SIZE,
        stall_timeout_s: float = 0.250,
        clock: Callable[[], float] = time.monotonic,
        formal_v4: bool = False,
    ) -> None:
        if batch_size != BATCH_SIZE:
            raise ValueError("the frozen sealer batch size is exactly ten")
        if stall_timeout_s <= 0.0 or not math.isfinite(stall_timeout_s):
            raise ValueError("stall timeout must be finite and positive")
        self.spool = spool
        self.artifact_path = Path(artifact_path)
        self.manifest_path = Path(manifest_path)
        self.episode_id = episode_id
        self.metadata = dict(metadata or {})
        self.formal_v4 = bool(formal_v4)
        self.identity_enabled, self.semantic_context_fingerprint_sha256 = _semantic_identity_status(self.metadata)
        self.batch_size = batch_size
        self.stall_timeout_s = float(stall_timeout_s)
        self._clock = clock
        self._fault = _FaultLatch()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle: Any = None
        self._state_lock = threading.Lock()
        self._last_progress = self._clock()
        self._work_pending_since: float | None = None
        self._durable_rows = 0
        self._max_unsealed_tail = 0
        self._inflight_rows = 0
        self._sealed = False
        self._manifest_written = False
        self._tamper_free = False

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("sealer already started")
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        if self.artifact_path.exists() or self.manifest_path.exists():
            raise FileExistsError("episode recorder artifact or manifest already exists")
        if self.formal_v4 and not self.identity_enabled:
            raise RecorderError("formal_v4_requires_complete_semantic_identity")
        try:
            with self._state_lock:
                self._last_progress = self._clock()
                self._work_pending_since = None
            self._handle = self.artifact_path.open("xb")
            artifact_schema = EPISODE_ARTIFACT_SCHEMA_V4 if self.formal_v4 else EPISODE_ARTIFACT_SCHEMA
            frame_schema = EPISODE_FRAME_SCHEMA_V4 if self.formal_v4 else EPISODE_FRAME_SCHEMA
            tail_schema = EPISODE_TAIL_SCHEMA_V4 if self.formal_v4 else EPISODE_TAIL_SCHEMA
            header = {
                "schema": artifact_schema,
                "format_version": 4 if self.formal_v4 else 3,
                "frame_schema": frame_schema,
                "tail_schema": tail_schema,
                "episode_id": self.episode_id,
                "durability_mode": DURABILITY_MODE,
                "spool_capacity": self.spool.capacity,
                "batch_size": self.batch_size,
                "max_unsealed_tail": MAX_UNSEALED_TAIL,
                "identity_enabled": self.identity_enabled,
                "semantic_context_fingerprint_sha256": self.semantic_context_fingerprint_sha256,
                "metadata": self.metadata,
                "formal_v4": self.formal_v4,
            }
            self._handle.write(_line(header))
            self._handle.flush()
            os.fsync(self._handle.fileno())
        except Exception as exc:
            self._fault.latch(f"writer_start:{type(exc).__name__}:{exc}")
            if self._handle is not None:
                self._handle.close()
            raise
        self._thread = threading.Thread(
            target=self._run,
            name="tacdiffusion-episode-batch-fsync-10",
            daemon=True,
        )
        self._thread.start()

    def _update_stall(self) -> None:
        now = self._clock()
        with self._state_lock:
            work_pending = self._inflight_rows > 0 or self.spool.depth > 0
            if not work_pending:
                self._work_pending_since = None
                return
            if self._work_pending_since is None:
                self._work_pending_since = now
            reference = max(self._last_progress, self._work_pending_since)
        if now - reference > self.stall_timeout_s:
            self._fault.latch("writer_stall")

    def _write_batch(self, batch: Sequence[EpisodeFrameV2]) -> None:
        if not batch:
            return
        if self.formal_v4:
            if not self.identity_enabled or self.semantic_context_fingerprint_sha256 is None:
                raise RecorderError("formal_v4_requires_complete_semantic_identity")
            if any(not isinstance(frame, EpisodeFrameV4) for frame in batch):
                raise RecorderError("formal_v4_requires_episode_frame_v4")
            canonical_batch = tuple(frame for frame in batch if isinstance(frame, EpisodeFrameV4))
            if any(
                frame.semantic_context_fingerprint_sha256 != self.semantic_context_fingerprint_sha256
                or not frame.formal_eligible
                for frame in canonical_batch
            ):
                raise RecorderError("formal_v4_row_receipts_or_identity_invalid")
            payload = b"".join(_line(frame.as_json()) for frame in canonical_batch)
            written = self._handle.write(payload)
            if written != len(payload):
                raise OSError("episode batch write was incomplete")
            self._handle.flush()
            os.fsync(self._handle.fileno())
            with self._state_lock:
                self._durable_rows += len(canonical_batch)
                self._last_progress = self._clock()
                self._work_pending_since = None
            return
        canonical_frames: list[EpisodeFrameV3] = []
        for frame in batch:
            canonical = frame if isinstance(frame, EpisodeFrameV3) else EpisodeFrameV3.from_v2(frame)
            if not self.identity_enabled or self.semantic_context_fingerprint_sha256 is None:
                bound = object.__new__(EpisodeFrameV3)
                for field in fields(EpisodeFrameV3):
                    object.__setattr__(bound, field.name, getattr(canonical, field.name))
                object.__setattr__(bound, "identity_enabled", False)
                object.__setattr__(bound, "semantic_context_fingerprint_sha256", None)
                canonical = bound
            elif canonical.semantic_context_fingerprint_sha256 is None:
                bound = object.__new__(EpisodeFrameV3)
                for field in fields(EpisodeFrameV3):
                    object.__setattr__(bound, field.name, getattr(canonical, field.name))
                object.__setattr__(bound, "semantic_context_fingerprint_sha256", self.semantic_context_fingerprint_sha256)
                canonical = bound
            elif canonical.semantic_context_fingerprint_sha256 != self.semantic_context_fingerprint_sha256:
                bound = object.__new__(EpisodeFrameV3)
                for field in fields(EpisodeFrameV3):
                    object.__setattr__(bound, field.name, getattr(canonical, field.name))
                object.__setattr__(bound, "identity_enabled", False)
                canonical = bound
            canonical_frames.append(canonical)
        canonical_batch = tuple(canonical_frames)
        payload = b"".join(_line(frame.as_json()) for frame in canonical_batch)
        written = self._handle.write(payload)
        if written != len(payload):
            raise OSError("episode batch write was incomplete")
        self._handle.flush()
        os.fsync(self._handle.fileno())
        with self._state_lock:
            self._durable_rows += len(batch)
            self._last_progress = self._clock()
            self._work_pending_since = None

    def _run(self) -> None:
        pending: list[EpisodeFrameV2] = []
        try:
            while True:
                frame = self.spool.get(0.010)
                if frame is None:
                    if self._stop_requested.is_set() and self.spool.depth == 0:
                        if pending:
                            self._write_batch(pending)
                            for _ in pending:
                                self.spool.task_done()
                            pending.clear()
                            with self._state_lock:
                                self._inflight_rows = 0
                        break
                    self._update_stall()
                    continue
                pending.append(frame)
                with self._state_lock:
                    self._inflight_rows = len(pending)
                    self._max_unsealed_tail = max(
                        self._max_unsealed_tail,
                        len(pending) % self.batch_size,
                    )
                self._update_stall()
                if len(pending) == self.batch_size:
                    self._write_batch(pending)
                    for _ in pending:
                        self.spool.task_done()
                    pending.clear()
                    with self._state_lock:
                        self._inflight_rows = 0
        except Exception as exc:
            self._fault.latch(f"writer_error:{type(exc).__name__}:{exc}")
            self._stop_requested.set()
        finally:
            if self._handle is not None and not self._handle.closed:
                try:
                    self._handle.close()
                except Exception as exc:
                    self._fault.latch(f"writer_close:{type(exc).__name__}:{exc}")

    def poll_fault(self) -> str | None:
        self._update_stall()
        return self.spool.fault or self._fault.reason

    @property
    def durable_rows(self) -> int:
        with self._state_lock:
            return self._durable_rows

    @property
    def unsealed_tail(self) -> int:
        with self._state_lock:
            # "Tail" is the incomplete durability quantum, not the full
            # undurable backlog. Complete seal separately requires
            # durable_rows == enqueued_rows and an empty spool.
            return (self.spool.depth + self._inflight_rows) % self.batch_size

    def close(self, timeout_s: float = 5.0) -> None:
        self._stop_requested.set()
        if self._thread is not None:
            deadline = self._clock() + timeout_s
            while self.spool.unfinished_tasks and self._clock() < deadline:
                if self._fault.reason is not None:
                    break
                time.sleep(0.001)
            self._thread.join(max(0.0, deadline - self._clock()))
            if self._thread.is_alive():
                self._fault.latch("writer_close_timeout")
        if (
            self._handle is not None
            and not self._handle.closed
            and (self._thread is None or not self._thread.is_alive())
        ):
            self._handle.close()

    def _append_v3_tail(self) -> dict[str, Any]:
        if self.formal_v4:
            return self._append_v4_tail()
        data = self.artifact_path.read_bytes()
        if not data or not data.endswith(b"\n"):
            raise RecorderError("episode_artifact_has_incomplete_tail")
        lines = data.splitlines(keepends=True)
        if not lines:
            raise RecorderError("episode_artifact_header_missing")
        try:
            header = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise RecorderError("episode_artifact_header_invalid") from exc
        if not isinstance(header, dict) or header.get("schema") != EPISODE_ARTIFACT_SCHEMA:
            raise RecorderError("episode_artifact_schema_mismatch")
        row_lines = lines[1:]
        if row_lines:
            try:
                last_payload = json.loads(row_lines[-1])
            except json.JSONDecodeError as exc:
                raise RecorderError("episode_artifact_tail_is_torn") from exc
            if isinstance(last_payload, dict) and last_payload.get("schema") == EPISODE_TAIL_SCHEMA:
                raise RecorderError("episode_artifact_tail_already_present")
        parsed_rows: list[dict[str, Any]] = []
        for index, raw in enumerate(row_lines):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RecorderError(f"episode_artifact_row_{index}_invalid") from exc
            if not isinstance(payload, dict) or payload.get("schema") != EPISODE_FRAME_SCHEMA:
                raise RecorderError(f"episode_artifact_row_{index}_schema_mismatch")
            parsed_rows.append(payload)
        try:
            _validate_v3_optional_receipts(parsed_rows)
            _validate_v3_row_seals(parsed_rows, require=True)
        except ValueError as exc:
            raise RecorderError(str(exc)) from exc
        if len(parsed_rows) != self.durable_rows:
            raise RecorderError("episode_artifact_durable_row_count_mismatch")
        content_bytes = b"".join(row_lines)
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        last = parsed_rows[-1] if parsed_rows else None
        tail_unsigned: dict[str, Any] = {
            "schema": EPISODE_TAIL_SCHEMA,
            "format_version": 3,
            "complete_tail": True,
            "row_count": len(parsed_rows),
            "last_sample_index": None if last is None else last.get("sample_index"),
            "last_control_sequence": None if last is None else last.get("control_sequence"),
            "content_sha256": content_sha256,
        }
        tail_sha256 = hashlib.sha256(_line(tail_unsigned)).hexdigest()
        tail = tail_unsigned | {"tail_sha256": tail_sha256}
        with self.artifact_path.open("ab") as handle:
            payload = _line(tail)
            if handle.write(payload) != len(payload):
                raise RecorderError("episode_artifact_tail_write_incomplete")
            handle.flush()
            os.fsync(handle.fileno())
        return {
            "content_sha256": content_sha256,
            "tail_sha256": tail_sha256,
            "row_count": len(parsed_rows),
            "last_sample_index": tail["last_sample_index"],
            "last_control_sequence": tail["last_control_sequence"],
            "header_sha256": hashlib.sha256(lines[0]).hexdigest(),
        }

    def _append_v4_tail(self) -> dict[str, Any]:
        data = self.artifact_path.read_bytes()
        if not data or not data.endswith(b"\n"):
            raise RecorderError("episode_artifact_has_incomplete_tail")
        lines = data.splitlines(keepends=True)
        if not lines:
            raise RecorderError("episode_artifact_header_missing")
        try:
            header = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise RecorderError("episode_artifact_header_invalid") from exc
        if not isinstance(header, Mapping) or header.get("schema") != EPISODE_ARTIFACT_SCHEMA_V4:
            raise RecorderError("episode_artifact_v4_schema_mismatch")
        row_lines = lines[1:]
        parsed_rows: list[dict[str, Any]] = []
        for index, raw in enumerate(row_lines):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RecorderError(f"episode_artifact_row_{index}_invalid") from exc
            if not isinstance(payload, dict) or payload.get("schema") != EPISODE_FRAME_SCHEMA_V4:
                raise RecorderError(f"episode_artifact_row_{index}_schema_mismatch")
            parsed_rows.append(payload)
        try:
            _validate_v4_required_receipts(parsed_rows)
            _validate_v3_row_seals(parsed_rows, require=True)
        except ValueError as exc:
            raise RecorderError(str(exc)) from exc
        if len(parsed_rows) != self.durable_rows:
            raise RecorderError("episode_artifact_durable_row_count_mismatch")
        content_bytes = b"".join(row_lines)
        content_sha256 = hashlib.sha256(content_bytes).hexdigest()
        last = parsed_rows[-1] if parsed_rows else None
        tail_unsigned: dict[str, Any] = {
            "schema": EPISODE_TAIL_SCHEMA_V4,
            "format_version": 4,
            "complete_tail": True,
            "row_count": len(parsed_rows),
            "last_sample_index": None if last is None else last.get("sample_index"),
            "last_control_sequence": None if last is None else last.get("control_sequence"),
            "content_sha256": content_sha256,
        }
        tail_sha256 = hashlib.sha256(_line(tail_unsigned)).hexdigest()
        tail = tail_unsigned | {"tail_sha256": tail_sha256}
        with self.artifact_path.open("ab") as handle:
            payload = _line(tail)
            if handle.write(payload) != len(payload):
                raise RecorderError("episode_artifact_tail_write_incomplete")
            handle.flush()
            os.fsync(handle.fileno())
        return {
            "content_sha256": content_sha256,
            "tail_sha256": tail_sha256,
            "row_count": len(parsed_rows),
            "last_sample_index": tail["last_sample_index"],
            "last_control_sequence": tail["last_control_sequence"],
            "header_sha256": hashlib.sha256(lines[0]).hexdigest(),
        }

    def seal(self) -> dict[str, Any]:
        self.close()
        fault = self.poll_fault()
        if fault is not None:
            raise RecorderError(fault)
        if self.spool.unfinished_tasks:
            raise RecorderError("writer_has_unsealed_rows")
        if not self.artifact_path.is_file():
            raise RecorderError("episode_artifact_missing")
        tail = self._append_v3_tail()
        artifact_hash = hashlib.sha256(self.artifact_path.read_bytes()).hexdigest()
        manifest: dict[str, Any] = {
            "schema": EPISODE_ARTIFACT_SCHEMA_V4 if self.formal_v4 else EPISODE_ARTIFACT_SCHEMA,
            "format_version": 4 if self.formal_v4 else 3,
            "episode_id": self.episode_id,
            "artifact": self.artifact_path.name,
            "artifact_sha256": artifact_hash,
            "row_count": self.durable_rows,
            "durability_mode": DURABILITY_MODE,
            "spool_capacity": self.spool.capacity,
            "batch_size": self.batch_size,
            "max_unsealed_tail": MAX_UNSEALED_TAIL,
            "frame_schema": EPISODE_FRAME_SCHEMA_V4 if self.formal_v4 else EPISODE_FRAME_SCHEMA,
            "tail_schema": EPISODE_TAIL_SCHEMA_V4 if self.formal_v4 else EPISODE_TAIL_SCHEMA,
            "header_sha256": tail["header_sha256"],
            "content_sha256": tail["content_sha256"],
            "tail_sha256": tail["tail_sha256"],
            "last_sample_index": tail["last_sample_index"],
            "last_control_sequence": tail["last_control_sequence"],
            "complete_tail": True,
            "identity_enabled": self.identity_enabled,
            "semantic_context_fingerprint_sha256": self.semantic_context_fingerprint_sha256,
            "metadata": self.metadata,
            "complete_seal": True,
            "tamper_free": True,
            "overflowed": False,
            "stalled": False,
            "dropped_rows": 0,
            "formal_v4": self.formal_v4,
        }
        _atomic_create_json(self.manifest_path, manifest)
        self._sealed = True
        self._manifest_written = True
        self._tamper_free = True
        return manifest

    def health(self, *, validate_tamper: bool = False) -> RecorderHealth:
        self._update_stall()
        fault = self.spool.fault or self._fault.reason
        tamper_free = self._tamper_free
        if self._sealed and validate_tamper:
            try:
                validate_sealed_episode_manifest(self.artifact_path, self.manifest_path)
            except (OSError, ValueError, json.JSONDecodeError):
                tamper_free = False
        return RecorderHealth(
            capacity=self.spool.capacity,
            queue_depth=self.spool.depth,
            enqueued_rows=self.spool.enqueued_rows,
            durable_rows=self.durable_rows,
            rejected_rows=self.spool.rejected_rows,
            dropped_rows=0,
            overflowed=self.spool.fault == "spool_overflow",
            stalled=self._fault.reason == "writer_stall",
            writer_error=(
                self._fault.reason
                if self._fault.reason is not None and self._fault.reason != "writer_stall"
                else None
            ),
            fault=fault,
            durability_mode=DURABILITY_MODE,
            unsealed_tail=self.unsealed_tail,
            max_unsealed_tail=max(self._max_unsealed_tail, self.unsealed_tail),
            sealed=self._sealed,
            manifest_written=self._manifest_written,
            tamper_free=tamper_free,
        )


def _atomic_create_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"receipt already exists: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(_line(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


class EpisodeRecorder:
    """Composition of the bounded producer spool and background sealer."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        episode_id: str,
        metadata: Mapping[str, Any] | None = None,
        semantic_context: Any | None = None,
        capacity: int = SPOOL_CAPACITY,
        stall_timeout_s: float = 0.250,
        formal_v4: bool = False,
        formal: bool | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.episode_id = episode_id
        if formal is not None:
            if formal_v4 and bool(formal) != formal_v4:
                raise ValueError("formal and formal_v4 recorder flags disagree")
            formal_v4 = bool(formal)
        self.formal_v4 = bool(formal_v4)
        normalized_metadata = dict(metadata or {})
        if semantic_context is not None:
            if not hasattr(semantic_context, "as_metadata"):
                raise TypeError("semantic_context must expose as_metadata()")
            normalized_metadata["semantic_context"] = semantic_context.as_metadata()
        self.spool = BoundedEpisodeSpool(capacity=capacity)
        artifact_stem = "episode_v4" if self.formal_v4 else "episode_v3"
        self.artifact_path = self.output_dir / f"{artifact_stem}.jsonl"
        self.manifest_path = self.output_dir / f"{artifact_stem}.manifest.json"
        self.health_path = self.output_dir / "recorder_health.json"
        self.sealer = BatchFsync10Sealer(
            self.spool,
            self.artifact_path,
            self.manifest_path,
            episode_id=episode_id,
            metadata=normalized_metadata,
            stall_timeout_s=stall_timeout_s,
            formal_v4=self.formal_v4,
        )
        self._producer_lock = threading.Lock()
        self._started = False
        self._accepting = True
        self._next_sample_index = 0
        self._last_control_time = -math.inf
        self._last_device_time = -math.inf
        self._last_host_time = -math.inf
        self._last_external_batch_id: int | None = None
        self._last_external_batch_host: float | None = None
        self._last_external_sample_index: int | None = None
        self._external_held_ticks = 0

    def start(self) -> None:
        if self.health_path.exists():
            raise FileExistsError("episode recorder health receipt already exists")
        if (
            (self.output_dir / "episode_v2.jsonl").exists()
            or (self.output_dir / "episode_v2.manifest.json").exists()
            or (self.output_dir / "episode_v3.jsonl").exists()
            or (self.output_dir / "episode_v3.manifest.json").exists()
            or (self.output_dir / "episode_v4.jsonl").exists()
            or (self.output_dir / "episode_v4.manifest.json").exists()
        ):
            raise FileExistsError("legacy episode artifact or manifest already exists")
        self.sealer.start()
        self._started = True

    def enqueue(self, frame: EpisodeFrameV2) -> bool:
        """Producer seam: bounded enqueue only; no JSON or filesystem calls."""

        if not self._started:
            raise RuntimeError("episode recorder is not started")
        if self.formal_v4:
            if not isinstance(frame, EpisodeFrameV4):
                raise TypeError("formal EpisodeRecorder requires EpisodeFrameV4")
        elif isinstance(frame, EpisodeFrameV4):
            raise TypeError("formal EpisodeFrameV4 requires a formal EpisodeRecorder")
        elif isinstance(frame, EpisodeFrameV2) and not isinstance(frame, EpisodeFrameV3):
            frame = EpisodeFrameV3.from_v2(frame)
        if not isinstance(frame, EpisodeFrameV3):
            raise TypeError("canonical EpisodeRecorder requires EpisodeFrameV3 or an EpisodeFrameV2 adapter")
        with self._producer_lock:
            if not self._accepting:
                return False
            control_strict = frame.control_time_strict and frame.control_time_s > self._last_control_time
            device_monotonic = (
                frame.external_device_time_s is None
                or frame.external_device_time_s >= self._last_device_time - 1e-12
            )
            host_monotonic = (
                frame.external_host_visible_time_s is None
                or frame.external_host_visible_time_s >= self._last_host_time - 1e-12
            )
            same_batch_arrival = True
            if (
                frame.external_batch_id is not None
                and frame.external_batch_id == self._last_external_batch_id
            ):
                same_batch_arrival = (
                    frame.external_host_visible_time_s is not None
                    and self._last_external_batch_host is not None
                    and math.isclose(
                        frame.external_host_visible_time_s,
                        self._last_external_batch_host,
                        rel_tol=0.0,
                        abs_tol=1e-12,
                    )
                )
            if (
                frame.external_sample_index is not None
                and frame.external_sample_index == self._last_external_sample_index
            ):
                self._external_held_ticks += 1
                external_hold = True
            elif frame.external_sample_index is not None:
                self._external_held_ticks = 0
                external_hold = False
            else:
                external_hold = False
            accepted = _with_recorder_metadata(
                frame,
                sample_index=self._next_sample_index,
                control_time_strict=control_strict,
                external_lineage_valid=(
                    frame.external_lineage_valid
                    and device_monotonic
                    and host_monotonic
                    and same_batch_arrival
                ),
                external_hold=external_hold,
                external_held_ticks=self._external_held_ticks,
                device_age_samples=(
                    self._external_held_ticks * DEVICE_SAMPLES_PER_CONTROL_TICK
                    if frame.external_sample_index is not None
                    else frame.device_age_samples
                ),
            )
            if not self.spool.enqueue(accepted):
                return False
            self._next_sample_index += 1
            self._last_control_time = frame.control_time_s
            if accepted.external_lineage_valid:
                if frame.external_device_time_s is not None:
                    self._last_device_time = frame.external_device_time_s
                if frame.external_host_visible_time_s is not None:
                    self._last_host_time = frame.external_host_visible_time_s
                if frame.external_batch_id is not None:
                    self._last_external_batch_id = frame.external_batch_id
                    self._last_external_batch_host = (
                        frame.external_host_visible_time_s
                    )
                if frame.external_sample_index is not None:
                    self._last_external_sample_index = frame.external_sample_index
            return True

    def poll_fault(self) -> str | None:
        return self.sealer.poll_fault()

    def require_healthy(self) -> None:
        fault = self.poll_fault()
        if fault is not None:
            raise RecorderError(fault)

    def health(self, *, validate_tamper: bool = False) -> RecorderHealth:
        return self.sealer.health(validate_tamper=validate_tamper)

    def close(self, *, seal: bool = True) -> dict[str, Any] | None:
        self._accepting = False
        if not self._started:
            return None
        if seal:
            try:
                return self.sealer.seal()
            finally:
                self._write_health_receipt()
        self.sealer.close()
        self._write_health_receipt()
        return None

    def _write_health_receipt(self) -> dict[str, Any]:
        health = self.health()
        payload: dict[str, Any] = {
            "schema": RECORDER_HEALTH_SCHEMA,
            "episode_id": self.episode_id,
            "health": health.as_json(),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        payload["health_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        _atomic_create_json(self.health_path, payload)
        return payload

    def __enter__(self) -> "EpisodeRecorder":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close(seal=True)


class FormalEpisodeRecorder(EpisodeRecorder):
    """Fail-closed V4 recorder convenience composition."""

    def __init__(self, output_dir: str | Path, *, episode_id: str, **kwargs: Any) -> None:
        super().__init__(output_dir, episode_id=episode_id, formal_v4=True, **kwargs)


def _load_jsonl(path: Path) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    data = path.read_bytes()
    if not data or not data.endswith(b"\n"):
        raise ValueError("episode artifact has an incomplete final line")
    lines = data.splitlines(keepends=True)
    try:
        parsed = [json.loads(row) for row in lines]
    except json.JSONDecodeError as exc:
        raise ValueError("episode artifact contains invalid JSON") from exc
    if not parsed or not isinstance(parsed[0], dict):
        raise ValueError("episode artifact header is invalid")
    header = parsed[0]
    if header.get("schema") == EPISODE_ARTIFACT_SCHEMA_V4:
        if (
            header.get("format_version") != 4
            or header.get("frame_schema") != EPISODE_FRAME_SCHEMA_V4
            or header.get("tail_schema") != EPISODE_TAIL_SCHEMA_V4
        ):
            raise ValueError("episode v4 header version/schema mismatch")
        integrity = _validate_v4_tail_lines(lines, parsed)
        return header, tuple(integrity["rows"])
    if header.get("schema") == EPISODE_ARTIFACT_SCHEMA:
        if (
            header.get("format_version") != 3
            or header.get("frame_schema") != EPISODE_FRAME_SCHEMA
            or header.get("tail_schema") != EPISODE_TAIL_SCHEMA
        ):
            raise ValueError("episode v3 header version/schema mismatch")
        integrity = _validate_v3_tail_lines(lines, parsed)
        return header, tuple(integrity["rows"])
    return header, tuple(row for row in parsed[1:] if isinstance(row, dict))


def _validate_v3_tail_lines(
    lines: Sequence[bytes],
    parsed: Sequence[object],
) -> dict[str, Any]:
    if len(lines) < 2 or len(parsed) < 2:
        raise ValueError("episode v3 is missing its complete tail")
    tail = parsed[-1]
    if not isinstance(tail, dict) or tail.get("schema") != EPISODE_TAIL_SCHEMA:
        raise ValueError("episode v3 tail is missing or incomplete")
    rows = parsed[1:-1]
    if any(not isinstance(row, dict) or row.get("schema") != EPISODE_FRAME_SCHEMA for row in rows):
        raise ValueError("episode v3 row schema mismatch")
    _validate_v3_optional_receipts(rows)
    # Older v3 artifacts predate the row seal and remain readable.  Any seal
    # that is present, however, is mandatory to validate before tail/content.
    _validate_v3_row_seals(rows, require=False)
    row_lines = lines[1:-1]
    content_sha256 = hashlib.sha256(b"".join(row_lines)).hexdigest()
    if tail.get("content_sha256") != content_sha256:
        raise ValueError("episode v3 content identity mismatch")
    if tail.get("row_count") != len(rows):
        raise ValueError("episode v3 tail row count mismatch")
    expected_tail = dict(tail)
    supplied_tail_hash = expected_tail.pop("tail_sha256", None)
    if not isinstance(supplied_tail_hash, str) or supplied_tail_hash != hashlib.sha256(_line(expected_tail)).hexdigest():
        raise ValueError("episode v3 tail identity mismatch")
    last = rows[-1] if rows else None
    if tail.get("last_sample_index") != (None if last is None else last.get("sample_index")):
        raise ValueError("episode v3 tail sample identity mismatch")
    if tail.get("last_control_sequence") != (None if last is None else last.get("control_sequence")):
        raise ValueError("episode v3 tail sequence identity mismatch")
    if tail.get("complete_tail") is not True:
        raise ValueError("episode v3 tail is not complete")
    return {
        "rows": tuple(rows),
        "tail": tail,
        "content_sha256": content_sha256,
        "tail_sha256": supplied_tail_hash,
        "header_sha256": hashlib.sha256(lines[0]).hexdigest(),
        "last_sample_index": tail.get("last_sample_index"),
        "last_control_sequence": tail.get("last_control_sequence"),
    }


def read_episode_artifact(path: str | Path) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Read v3 writes and expose v1/v2 rows without rewriting legacy artifacts."""

    header, rows = _load_jsonl(Path(path))
    schema = header.get("schema")
    if schema not in {
        EPISODE_ARTIFACT_SCHEMA,
        EPISODE_ARTIFACT_SCHEMA_V4,
        EPISODE_ARTIFACT_SCHEMA_V2,
        "ur10e_tacdiffusion_expert_episode/v1",
    }:
        raise ValueError("unsupported episode artifact schema")
    return header, rows


def read_recorder_health(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != RECORDER_HEALTH_SCHEMA:
        raise ValueError("recorder health receipt schema mismatch")
    if not isinstance(payload.get("health"), dict):
        raise ValueError("recorder health receipt payload is invalid")
    return payload


def validate_sealed_episode_manifest(
    artifact_path: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    artifact = Path(artifact_path)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("artifact") != artifact.name:
        raise ValueError("episode manifest artifact identity mismatch")
    if manifest.get("artifact_sha256") != hashlib.sha256(artifact.read_bytes()).hexdigest():
        raise ValueError("episode manifest artifact hash mismatch")
    # The v3 reader checks optional typed receipts and every present row seal
    # before accepting the row content/tail identities.
    header, rows = read_episode_artifact(artifact)
    if manifest.get("row_count") != len(rows):
        raise ValueError("episode manifest row count mismatch")
    if not manifest.get("complete_seal") or not manifest.get("tamper_free"):
        raise ValueError("episode manifest is not a complete seal")
    if header.get("schema") == EPISODE_ARTIFACT_SCHEMA:
        lines = artifact.read_bytes().splitlines(keepends=True)
        parsed = [json.loads(line) for line in lines]
        integrity = _validate_v3_tail_lines(lines, parsed)
        for name in (
            "header_sha256",
            "content_sha256",
            "tail_sha256",
            "last_sample_index",
            "last_control_sequence",
        ):
            if manifest.get(name) != integrity[name]:
                raise ValueError(f"episode v3 manifest {name} mismatch")
        if manifest.get("complete_tail") is not True or manifest.get("format_version") != 3:
            raise ValueError("episode v3 manifest tail is incomplete")
    elif header.get("schema") == EPISODE_ARTIFACT_SCHEMA_V4:
        lines = artifact.read_bytes().splitlines(keepends=True)
        parsed = [json.loads(line) for line in lines]
        integrity = _validate_v4_tail_lines(lines, parsed)
        for name in (
            "header_sha256",
            "content_sha256",
            "tail_sha256",
            "last_sample_index",
            "last_control_sequence",
        ):
            if manifest.get(name) != integrity[name]:
                raise ValueError(f"episode v4 manifest {name} mismatch")
        if (
            manifest.get("complete_tail") is not True
            or manifest.get("format_version") != 4
            or manifest.get("frame_schema") != EPISODE_FRAME_SCHEMA_V4
            or manifest.get("tail_schema") != EPISODE_TAIL_SCHEMA_V4
            or manifest.get("formal_v4") is not True
        ):
            raise ValueError("episode v4 manifest is incomplete")
    elif header.get("schema") not in {
        EPISODE_ARTIFACT_SCHEMA_V2,
        "ur10e_tacdiffusion_expert_episode/v1",
    }:
        raise ValueError("episode header schema mismatch")
    return manifest


def validate_formal_episode_artifact(
    artifact_path: str | Path,
    manifest_path: str | Path,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Accept only a complete, sealed V4 artifact with full receipt rows."""

    manifest = validate_sealed_episode_manifest(artifact_path, manifest_path)
    header, rows = read_episode_artifact(artifact_path)
    if header.get("schema") != EPISODE_ARTIFACT_SCHEMA_V4 or manifest.get("formal_v4") is not True:
        raise ValueError("formal artifact validator rejects legacy v2/v3 artifacts")
    _validate_v4_required_receipts(rows)
    return header, rows


__all__ = [
    "ACTION_DIMENSION",
    "BATCH_SIZE",
    "BoundedEpisodeSpool",
    "DURABILITY_MODE",
    "EPISODE_ARTIFACT_SCHEMA",
    "EPISODE_ARTIFACT_SCHEMA_V2",
    "EPISODE_ARTIFACT_SCHEMA_V3",
    "EPISODE_ARTIFACT_SCHEMA_V4",
    "EPISODE_FRAME_SCHEMA",
    "EPISODE_FRAME_SCHEMA_V2",
    "EPISODE_FRAME_SCHEMA_V3",
    "EPISODE_FRAME_SCHEMA_V4",
    "EPISODE_TAIL_SCHEMA",
    "EPISODE_TAIL_SCHEMA_V4",
    "EpisodeReceipt",
    "RECORDER_HEALTH_SCHEMA",
    "EpisodeFrame",
    "EpisodeFrameV2",
    "EpisodeFrameV3",
    "EpisodeFrameV4",
    "EpisodeRecorder",
    "FormalEpisodeRecorder",
    "ObservationReceipt",
    "ReferenceReceipt",
    "ReferenceReceiptV1",
    "ExpertActionReceiptV1",
    "TubeDecisionReceiptV1",
    "MAX_UNSEALED_TAIL",
    "OBSERVATION_DIMENSION",
    "RecorderError",
    "RecorderHealth",
    "SPOOL_CAPACITY",
    "BatchFsync10Sealer",
    "TypedEpisodeReceipt",
    "V3_ROW_SEAL_FIELD",
    "compute_v3_row_sha256",
    "read_episode_artifact",
    "read_recorder_health",
    "validate_sealed_episode_manifest",
    "validate_formal_episode_artifact",
]
