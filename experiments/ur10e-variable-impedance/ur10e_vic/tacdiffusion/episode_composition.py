"""Canonical semantic composition for durable TacDiffusion episodes.

The accepted semantic owners (``DeterministicExpert``, trajectory/reference
objects, causal Kunwei alignment, and ``ExpertEpisodeBindings``) produce the
meaning of an episode.  ``EpisodeRecorder`` remains only the bounded storage
engine.  This module contains the small seams that let the v4 runner retain
diagnostic rows without turning controller diagnostics into expert labels.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from .contracts import (
    CONTROL_RATE_HZ,
    DYNAMICS_AUTHORITATIVE_TORQUE_SOURCE,
    DYNAMICS_RECEIPT_SCHEMA,
    DynamicsConformanceBinding,
    DynamicsReceipt,
    DynamicsSample,
    OFFLINE_FAKE_RTDE_FIXTURE_ID,
    RAW_WRENCH_RATE_HZ,
)
from .expert import DeterministicExpert, ExpertDecision, ExpertInput
from .expert_episode_artifact import ExpertEpisodeBindings
from .signals import (
    CausalWrenchAlignment,
    CanonicalWrenchSample,
    HOST_BATCH_WATCHDOG_S,
    causal_sync_wrench_1khz_to_control_500hz,
    reconstruct_internal_wrench_from_previous_command,
)


SEMANTIC_CONTEXT_SCHEMA = "ur10e_tacdiffusion_episode_semantic_context/v1"
CANONICAL_EXPERT_POLICY_ID = "deterministic_expert_v1"
CANONICAL_EXPERT_ACTION_SEMANTICS = "deterministic_expert_guarded_action_12d_v1"
DIAGNOSTIC_ACTION_SEMANTICS = "diagnostic_command_not_expert_shadow_only_v1"
DIAGNOSTIC_ACTION_SOURCE = "diagnostic_zero6_fixed_stiffness_shadow"
DETERMINISTIC_ACTION_SOURCE = "deterministic_expert"
SHADOW_TRANSITION_SCHEMA = "ur10e_tacdiffusion_shadow_transition/v1"
ACTION_LABEL_CONTEXT_SCHEMA = "ur10e_tacdiffusion_action_label_context/v1"
ACTION_LABEL_PROVIDER_SCHEMA = "ur10e_tacdiffusion_action_label_provider/v1"
REQUIRED_RUNTIME_HASH_IDENTITIES = (
    "receiver_source_sha256",
    "bundle_reference_sha256",
    "kunwei_calibration_sha256",
    "runtime_source_sha256",
)
_SHA256_LENGTH = 64


def _sha256_text(value: object) -> bool:
    text = str(value)
    return len(text) == _SHA256_LENGTH and all(
        character in "0123456789abcdef" for character in text
    )


def _finite_vector(values: Sequence[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain {length} finite values")
    return result


@dataclass(frozen=True)
class EpisodeSemanticContext:
    """One episode's semantic binding, independent of its storage engine.

    A diagnostic capture may intentionally have no complete
    ``ExpertEpisodeBindings`` object.  That absence is represented explicitly
    and is a training blocker; it is never repaired with placeholder hashes.
    """

    episode_id: str
    expert_policy_id: str
    action_label_semantics: str
    expert_label_status: str
    dataset_split: str
    capture_kind: str
    bindings: ExpertEpisodeBindings | None
    hash_identities: Mapping[str, str]
    shadow_only: bool = True

    def __post_init__(self) -> None:
        if not self.episode_id.strip() or not self.capture_kind.strip():
            raise ValueError("episode semantic identity is required")
        if self.dataset_split not in {"train", "validation", "test", "shadow"}:
            raise ValueError("episode semantic dataset split is invalid")
        if not self.expert_policy_id.strip() or not self.action_label_semantics.strip():
            raise ValueError("episode semantic policy and label semantics are required")
        if not self.expert_label_status.strip():
            raise ValueError("episode expert label status is required")
        if self.bindings is not None:
            if self.bindings.episode_id != self.episode_id:
                raise ValueError("episode semantic binding identity mismatch")
            if self.bindings.dataset_split != self.dataset_split:
                raise ValueError("episode semantic split mismatch")
            if self.bindings.capture_kind != self.capture_kind:
                raise ValueError("episode semantic capture kind mismatch")
        normalized = {str(key): str(value) for key, value in self.hash_identities.items()}
        if any(not key.strip() or not value.strip() for key, value in normalized.items()):
            raise ValueError("episode hash identities must be non-empty strings")
        object.__setattr__(self, "hash_identities", normalized)

    @classmethod
    def diagnostic(
        cls,
        *,
        episode_id: str,
        capture_kind: str,
        hash_identities: Mapping[str, str],
        dataset_split: str = "shadow",
    ) -> "EpisodeSemanticContext":
        """Bind a no-contact diagnostic without claiming an expert label."""

        return cls(
            episode_id=episode_id,
            expert_policy_id=CANONICAL_EXPERT_POLICY_ID,
            action_label_semantics=DIAGNOSTIC_ACTION_SEMANTICS,
            expert_label_status="unavailable_shadow_only",
            dataset_split=dataset_split,
            capture_kind=capture_kind,
            bindings=None,
            hash_identities=hash_identities,
            shadow_only=True,
        )

    @classmethod
    def from_training_bindings(
        cls,
        *,
        bindings: ExpertEpisodeBindings,
        hash_identities: Mapping[str, str],
    ) -> "EpisodeSemanticContext":
        """Construct a candidate context only with complete semantic owners.

        This does not issue the final eligibility verdict.  It only makes the
        required producer/binding seam available to ``EligibilityValidator``.
        """

        missing = [
            name
            for name in REQUIRED_RUNTIME_HASH_IDENTITIES
            if not _sha256_text(hash_identities.get(name, ""))
        ]
        if missing:
            raise ValueError(
                "training candidate is missing runtime hash bindings: "
                + ",".join(missing)
            )
        if bindings.dataset_split == "shadow":
            raise ValueError("shadow capture cannot construct a training candidate")
        if not bindings.training_eligible:
            raise ValueError("ExpertEpisodeBindings is not training eligible")
        return cls(
            episode_id=bindings.episode_id,
            expert_policy_id=CANONICAL_EXPERT_POLICY_ID,
            action_label_semantics=CANONICAL_EXPERT_ACTION_SEMANTICS,
            expert_label_status="available",
            dataset_split=bindings.dataset_split,
            capture_kind=bindings.capture_kind,
            bindings=bindings,
            hash_identities=hash_identities,
            shadow_only=False,
        )

    @property
    def runtime_hashes_complete(self) -> bool:
        return all(
            _sha256_text(self.hash_identities.get(name, ""))
            for name in REQUIRED_RUNTIME_HASH_IDENTITIES
        )

    @property
    def bindings_complete(self) -> bool:
        return bool(
            self.bindings is not None
            and self.bindings.training_eligible
            and self.runtime_hashes_complete
        )

    @property
    def expert_label_available(self) -> bool:
        return (
            self.expert_label_status == "available"
            and self.expert_policy_id == CANONICAL_EXPERT_POLICY_ID
            and self.action_label_semantics == CANONICAL_EXPERT_ACTION_SEMANTICS
        )

    @property
    def training_candidate(self) -> bool:
        return self.bindings_complete and self.expert_label_available and not self.shadow_only

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "expert_policy_id": self.expert_policy_id,
            "action_label_semantics": self.action_label_semantics,
            "expert_label_status": self.expert_label_status,
            "dataset_split": self.dataset_split,
            "capture_kind": self.capture_kind,
            "shadow_only": self.shadow_only,
            "bindings": None if self.bindings is None else asdict(self.bindings),
            "hash_identities": dict(self.hash_identities),
        }

    @property
    def fingerprint_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.canonical_payload(),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    def as_metadata(self) -> dict[str, Any]:
        return {
            "schema": SEMANTIC_CONTEXT_SCHEMA,
            "episode_id": self.episode_id,
            "expert_policy_id": self.expert_policy_id,
            "action_label_semantics": self.action_label_semantics,
            "expert_label_status": self.expert_label_status,
            "dataset_split": self.dataset_split,
            "capture_kind": self.capture_kind,
            "shadow_only": self.shadow_only,
            "bindings": None if self.bindings is None else asdict(self.bindings),
            "binding_status": "complete" if self.bindings_complete else "missing_required",
            "missing_runtime_hash_identities": [
                name
                for name in REQUIRED_RUNTIME_HASH_IDENTITIES
                if not _sha256_text(self.hash_identities.get(name, ""))
            ],
            "hash_identities": dict(self.hash_identities),
            "training_candidate": self.training_candidate,
            "semantic_context_fingerprint_sha256": self.fingerprint_sha256,
        }


@dataclass(frozen=True)
class ActionLabelContext:
    """One versioned, frame-bound call context for every action provider."""

    sequence: int
    timestamp_s: float
    frame_id: str
    applied_action_12d: tuple[float, ...] | Sequence[float] = (0.0,) * 12
    echoed_action_12d: tuple[float, ...] | Sequence[float] = (0.0,) * 12
    expert_input: ExpertInput | None = None
    previous_action_12d: tuple[float, ...] | Sequence[float] | None = None
    previous_sequence: int | None = None
    previous_timestamp_s: float | None = None
    dt_s: float = 1.0 / CONTROL_RATE_HZ
    state_id: str | None = None
    model_identity: str = CANONICAL_EXPERT_POLICY_ID
    tcp_identity: str = "tool0_tcp"
    calibration_identity: str = "offline_unbound_calibration"
    source_hashes: Mapping[str, str] = field(default_factory=dict)
    dynamics_receipt: DynamicsReceipt | None = None
    semantic_context_fingerprint_sha256: str | None = None
    schema_version: str = ACTION_LABEL_CONTEXT_SCHEMA

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or self.sequence < 0:
            raise ValueError("action context sequence is invalid")
        if not math.isfinite(self.timestamp_s) or self.timestamp_s < 0.0:
            raise ValueError("action context timestamp is invalid")
        if self.schema_version != ACTION_LABEL_CONTEXT_SCHEMA:
            raise ValueError("unsupported action-label context schema")
        for name in ("frame_id", "model_identity", "tcp_identity", "calibration_identity"):
            value = str(getattr(self, name))
            if not value.strip() or len(value) > 192 or any(char.isspace() for char in value):
                raise ValueError(f"action context {name} is invalid")
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "applied_action_12d",
            _finite_vector(self.applied_action_12d, 12, "applied_action_12d"),
        )
        object.__setattr__(
            self,
            "echoed_action_12d",
            _finite_vector(self.echoed_action_12d, 12, "echoed_action_12d"),
        )
        if self.previous_action_12d is not None:
            object.__setattr__(
                self,
                "previous_action_12d",
                _finite_vector(self.previous_action_12d, 12, "previous_action_12d"),
            )
        if not math.isfinite(self.dt_s) or not 0.0 < self.dt_s <= 0.100:
            raise ValueError("action context dt_s is outside the bounded contract")
        if self.previous_sequence is not None:
            if self.previous_sequence < 0 or self.sequence != self.previous_sequence + 1:
                raise ValueError("action context sequence is nonconsecutive")
        if self.previous_timestamp_s is not None:
            if (
                not math.isfinite(self.previous_timestamp_s)
                or self.previous_timestamp_s < 0.0
                or self.timestamp_s <= self.previous_timestamp_s
                or self.timestamp_s - self.previous_timestamp_s > 0.100 + 1e-12
            ):
                raise ValueError("action context timestamp is stale or non-monotonic")
        if self.state_id is not None and not str(self.state_id).strip():
            raise ValueError("action context state identity is invalid")
        if not isinstance(self.source_hashes, Mapping) or len(self.source_hashes) > 16:
            raise ValueError("action context source hashes are invalid")
        normalized_hashes: dict[str, str] = {}
        for key, value in self.source_hashes.items():
            key_text = str(key)
            value_text = str(value)
            if not key_text.strip() or len(key_text) > 192 or not _sha256_text(value_text):
                raise ValueError("action context source hashes are invalid")
            normalized_hashes[key_text] = value_text
        object.__setattr__(self, "source_hashes", MappingProxyType(dict(sorted(normalized_hashes.items()))))
        if self.semantic_context_fingerprint_sha256 is not None:
            if not _sha256_text(self.semantic_context_fingerprint_sha256):
                raise ValueError("action context semantic fingerprint is invalid")
        if self.dynamics_receipt is not None:
            if not isinstance(self.dynamics_receipt, DynamicsReceipt):
                raise ValueError("action context dynamics receipt has the wrong type")
            if (
                self.dynamics_receipt.sequence != self.sequence
                or not math.isclose(self.dynamics_receipt.timestamp_s, self.timestamp_s, abs_tol=1e-12, rel_tol=0.0)
                or self.dynamics_receipt.frame_id != self.frame_id
            ):
                raise ValueError("action context/dynamics receipt identity mismatch")

    @classmethod
    def diagnostic(
        cls,
        *,
        sequence: int,
        timestamp_s: float,
        frame_id: str,
        applied_action_12d: Sequence[float],
        echoed_action_12d: Sequence[float],
        **kwargs: object,
    ) -> "ActionLabelContext":
        return cls(
            sequence=sequence,
            timestamp_s=timestamp_s,
            frame_id=frame_id,
            applied_action_12d=applied_action_12d,
            echoed_action_12d=echoed_action_12d,
            **kwargs,
        )

    @classmethod
    def expert(
        cls,
        *,
        sequence: int,
        timestamp_s: float,
        frame_id: str,
        expert_input: ExpertInput,
        previous_action_12d: Sequence[float] | None = None,
        **kwargs: object,
    ) -> "ActionLabelContext":
        return cls(
            sequence=sequence,
            timestamp_s=timestamp_s,
            frame_id=frame_id,
            expert_input=expert_input,
            previous_action_12d=previous_action_12d,
            **kwargs,
        )

    def canonical_payload(self) -> dict[str, object]:
        expert_payload: dict[str, object] | None = None
        if self.expert_input is not None:
            expert_payload = {
                "normal_load_n": self.expert_input.normal_load_n,
                "target_load_n": self.expert_input.target_load_n,
                "pose_error": list(self.expert_input.pose_error),
                "twist": list(self.expert_input.twist),
                "path_progress": self.expert_input.path_progress,
                "tangential_speed_m_s": self.expert_input.tangential_speed_m_s,
                "fault": self.expert_input.fault,
                "desired_twist": list(self.expert_input.desired_twist),
                "desired_acceleration": list(self.expert_input.desired_acceleration),
            }
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "timestamp_s": self.timestamp_s,
            "frame_id": self.frame_id,
            "applied_action_12d": list(self.applied_action_12d),
            "echoed_action_12d": list(self.echoed_action_12d),
            "expert_input": expert_payload,
            "previous_action_12d": None if self.previous_action_12d is None else list(self.previous_action_12d),
            "previous_sequence": self.previous_sequence,
            "previous_timestamp_s": self.previous_timestamp_s,
            "dt_s": self.dt_s,
            "state_id": self.state_id,
            "model_identity": self.model_identity,
            "tcp_identity": self.tcp_identity,
            "calibration_identity": self.calibration_identity,
            "source_hashes": dict(self.source_hashes),
            "dynamics_receipt": None if self.dynamics_receipt is None else self.dynamics_receipt.as_json(),
            "semantic_context_fingerprint_sha256": self.semantic_context_fingerprint_sha256,
        }

    def as_json(self) -> dict[str, object]:
        payload = self.canonical_payload()
        payload["context_fingerprint_sha256"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        return payload


@runtime_checkable
class ActionLabelProvider(Protocol):
    """Common producer contract; adapters may exist only at legacy edges."""

    schema_version: str

    def produce(self, context: ActionLabelContext) -> "ActionLabel":
        ...


@dataclass(frozen=True)
class ActionLabel:
    """The semantic result of an action producer."""

    expert_action_12d: tuple[float, ...]
    available: bool
    source: str
    semantics: str
    policy_id: str
    shadow_only: bool = False
    sequence: int = 0
    timestamp_s: float = 0.0
    frame_id: str = "tool0_tcp"
    state_id: str | None = None
    context_fingerprint_sha256: str | None = None
    schema_version: str = ACTION_LABEL_PROVIDER_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "expert_action_12d",
            _finite_vector(self.expert_action_12d, 12, "expert_action_12d"),
        )
        if not self.source.strip() or not self.semantics.strip() or not self.policy_id.strip():
            raise ValueError("action label identity is required")
        if self.schema_version != ACTION_LABEL_PROVIDER_SCHEMA:
            raise ValueError("unsupported action-label provider schema")
        if self.sequence < 0 or not math.isfinite(self.timestamp_s) or self.timestamp_s < 0.0:
            raise ValueError("action label sequence/timestamp is invalid")
        if not self.frame_id.strip():
            raise ValueError("action label frame identity is required")
        if self.available and self.shadow_only:
            raise ValueError("available expert labels cannot be shadow-only")
        if self.context_fingerprint_sha256 is not None and not _sha256_text(self.context_fingerprint_sha256):
            raise ValueError("action label context fingerprint is invalid")

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "expert_action_12d": list(self.expert_action_12d),
            "available": self.available,
            "source": self.source,
            "semantics": self.semantics,
            "policy_id": self.policy_id,
            "shadow_only": self.shadow_only,
            "sequence": self.sequence,
            "timestamp_s": self.timestamp_s,
            "frame_id": self.frame_id,
            "state_id": self.state_id,
            "context_fingerprint_sha256": self.context_fingerprint_sha256,
        }


def _legacy_diagnostic_context(
    context: ActionLabelContext | Sequence[float],
    echoed_action_12d: Sequence[float] | None,
) -> ActionLabelContext:
    if isinstance(context, ActionLabelContext):
        if echoed_action_12d is not None:
            raise TypeError("ActionLabelContext call cannot carry a legacy echo argument")
        return context
    if echoed_action_12d is None:
        raise TypeError("legacy diagnostic adapter requires applied and echoed actions")
    return ActionLabelContext.diagnostic(
        sequence=0,
        timestamp_s=0.0,
        frame_id="tool0_tcp",
        applied_action_12d=context,
        echoed_action_12d=echoed_action_12d,
    )


class DiagnosticShadowActionProvider:
    """Record the diagnostic command while making expert labels unavailable."""

    schema_version = ACTION_LABEL_PROVIDER_SCHEMA
    policy_id = CANONICAL_EXPERT_POLICY_ID
    label_semantics = DIAGNOSTIC_ACTION_SEMANTICS
    source = DIAGNOSTIC_ACTION_SOURCE
    shadow_only = True

    def produce(
        self,
        context: ActionLabelContext | Sequence[float],
        legacy_echoed_action_12d: Sequence[float] | None = None,
    ) -> ActionLabel:
        resolved = _legacy_diagnostic_context(context, legacy_echoed_action_12d)
        if resolved.frame_id != "tool0_tcp":
            raise ValueError("diagnostic action context frame mismatch")
        if resolved.model_identity != self.policy_id:
            raise ValueError("diagnostic action context model identity mismatch")
        return ActionLabel(
            expert_action_12d=(0.0,) * 12,
            available=False,
            source=self.source,
            semantics=self.label_semantics,
            policy_id=self.policy_id,
            shadow_only=True,
            sequence=resolved.sequence,
            timestamp_s=resolved.timestamp_s,
            frame_id=resolved.frame_id,
            context_fingerprint_sha256=resolved.as_json()["context_fingerprint_sha256"],
        )


class DeterministicExpertActionProvider:
    """Offline/testable seam whose sole label producer is ``DeterministicExpert``."""

    schema_version = ACTION_LABEL_PROVIDER_SCHEMA
    policy_id = CANONICAL_EXPERT_POLICY_ID
    label_semantics = CANONICAL_EXPERT_ACTION_SEMANTICS
    source = DETERMINISTIC_ACTION_SOURCE
    shadow_only = False

    def __init__(self, expert: DeterministicExpert | None = None) -> None:
        self.expert = expert or DeterministicExpert()
        self._last_sequence: int | None = None
        self._last_timestamp_s: float | None = None
        self._last_action: TacDiffusionAction | None = None

    def reset(self) -> None:
        self.expert.reset()
        self._last_sequence = None
        self._last_timestamp_s = None
        self._last_action = None

    def produce(
        self,
        context: ActionLabelContext | ExpertInput,
        *,
        dt_s: float | None = None,
    ) -> ActionLabel:
        if isinstance(context, ExpertInput):
            # Narrow compatibility adapter for established offline callers.
            legacy_dt_s = 0.002 if dt_s is None else dt_s
            legacy_sequence = 0 if self._last_sequence is None else self._last_sequence + 1
            legacy_timestamp = 0.0 if self._last_timestamp_s is None else self._last_timestamp_s + legacy_dt_s
            resolved = ActionLabelContext.expert(
                sequence=legacy_sequence,
                timestamp_s=legacy_timestamp,
                frame_id=self.expert.profile.frame_id,
                expert_input=context,
                dt_s=legacy_dt_s,
            )
        elif isinstance(context, ActionLabelContext):
            resolved = context
            if dt_s is not None and not math.isclose(dt_s, resolved.dt_s, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError("legacy dt_s does not match ActionLabelContext.dt_s")
        else:
            raise TypeError("ActionLabelProvider.produce requires ActionLabelContext")
        if resolved.expert_input is None:
            raise ValueError("deterministic expert action context requires explicit expert state")
        if resolved.frame_id != self.expert.profile.frame_id:
            raise ValueError("action context frame mismatch")
        if resolved.model_identity != self.policy_id:
            raise ValueError("action context model identity mismatch")
        if self._last_sequence is not None:
            if resolved.sequence != self._last_sequence + 1:
                raise ValueError("action context sequence is nonconsecutive or stale")
            if self._last_timestamp_s is None or resolved.timestamp_s <= self._last_timestamp_s:
                raise ValueError("action context timestamp is stale or non-monotonic")
            if resolved.previous_action_12d is not None and self._last_action is not None and tuple(resolved.previous_action_12d) != self._last_action.vector12:
                raise ValueError("action context previous action mismatch")
        elif resolved.previous_action_12d is not None:
            self.expert._previous_action = TacDiffusionAction(
                resolved.previous_action_12d[:6],
                resolved.previous_action_12d[6:],
                resolved.frame_id,
            )
        decision: ExpertDecision = self.expert.step(resolved.expert_input, dt_s=resolved.dt_s)
        if resolved.state_id is not None and resolved.state_id != decision.state.value:
            raise ValueError("action label semantic state mismatch")
        self._last_sequence = resolved.sequence
        self._last_timestamp_s = resolved.timestamp_s
        self._last_action = decision.action
        return ActionLabel(
            expert_action_12d=decision.action.vector12,
            available=True,
            source=self.source,
            semantics=self.label_semantics,
            policy_id=self.policy_id,
            shadow_only=False,
            sequence=resolved.sequence,
            timestamp_s=resolved.timestamp_s,
            frame_id=resolved.frame_id,
            state_id=decision.state.value,
            context_fingerprint_sha256=resolved.as_json()["context_fingerprint_sha256"],
        )


@dataclass(frozen=True)
class ActiveTrainingWindow:
    """A declared contiguous window over retained rows.

    ``None`` bounds are a valid unresolved declaration.  They preserve all
    rows for diagnostics while making the training predicate fail closed.
    """

    start_sample_index: int | None
    end_sample_index: int | None
    warmup_sample_count: int = 0
    declaration: str = "receiver_torque_state_contiguous_window"
    error: str | None = None

    def __post_init__(self) -> None:
        if self.start_sample_index is not None and self.start_sample_index < 0:
            raise ValueError("active window start must be non-negative")
        if self.end_sample_index is not None and self.end_sample_index < 0:
            raise ValueError("active window end must be non-negative")
        if (
            self.start_sample_index is not None
            and self.end_sample_index is not None
            and self.end_sample_index < self.start_sample_index
        ):
            raise ValueError("active window bounds are inverted")
        if self.warmup_sample_count < 0:
            raise ValueError("active window warmup count must be non-negative")
        if not self.declaration.strip():
            raise ValueError("active window declaration is required")

    @property
    def declared(self) -> bool:
        return self.start_sample_index is not None and self.end_sample_index is not None

    @property
    def contiguous(self) -> bool:
        return self.declared and self.error is None

    @property
    def row_count(self) -> int:
        if not self.declared:
            return 0
        assert self.start_sample_index is not None
        assert self.end_sample_index is not None
        return self.end_sample_index - self.start_sample_index + 1

    def contains(self, sample_index: int) -> bool:
        return bool(
            self.declared
            and self.start_sample_index is not None
            and self.end_sample_index is not None
            and self.start_sample_index <= int(sample_index) <= self.end_sample_index
        )

    def select(self, rows: Sequence[object]) -> tuple[object, ...]:
        return tuple(
            row
            for row in rows
            if self.contains(int(_row_value(row, "sample_index", -1)))
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "declared": self.declared,
            "contiguous": self.contiguous,
            "start_sample_index": self.start_sample_index,
            "end_sample_index": self.end_sample_index,
            "row_count": self.row_count,
            "warmup_sample_count": self.warmup_sample_count,
            "declaration": self.declaration,
            "error": self.error,
        }


def _row_value(row: object, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def resolve_active_training_window(
    rows: Sequence[object],
    *,
    candidate_field: str = "candidate_window",
) -> ActiveTrainingWindow:
    """Resolve a window from flags without discarding any retained row."""

    candidates: list[int] = []
    for row in rows:
        try:
            sample_index = int(_row_value(row, "sample_index", -1))
        except (TypeError, ValueError):
            continue
        if bool(_row_value(row, candidate_field, False)):
            candidates.append(sample_index)
    if not candidates:
        return ActiveTrainingWindow(
            None,
            None,
            warmup_sample_count=len(rows),
            error="active_training_candidate_window_empty",
        )
    if any(right != left + 1 for left, right in zip(candidates, candidates[1:])):
        return ActiveTrainingWindow(
            None,
            None,
            warmup_sample_count=candidates[0],
            error="active_training_candidate_window_not_contiguous",
        )
    return ActiveTrainingWindow(
        candidates[0],
        candidates[-1],
        warmup_sample_count=candidates[0],
    )


@dataclass(frozen=True)
class InternalWrenchReceipt:
    wrench_tcp_si: tuple[float, ...]
    valid: bool
    reason: str | None = None
    residual_norm_nm: float | None = None
    dynamics_receipt: DynamicsReceipt | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "wrench_tcp_si",
            _finite_vector(self.wrench_tcp_si, 6, "internal_wrench_tcp_si"),
        )
        if self.valid and self.reason is not None:
            raise ValueError("valid internal wrench cannot have a failure reason")


class InternalWrenchReconstructionProvider:
    """Bounded seam for controller-equivalent previous-command reconstruction."""

    def __init__(
        self,
        input_provider: Callable[[Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
    ) -> None:
        self.input_provider = input_provider

    def reconstruct(
        self,
        row: Mapping[str, Any] | DynamicsSample,
        *,
        previous_sample: DynamicsSample | None = None,
        conformance_binding: DynamicsConformanceBinding | None = None,
    ) -> InternalWrenchReceipt | DynamicsReceipt:
        if isinstance(row, DynamicsSample):
            # Typed callers receive the durable receipt contract.  Production
            # remains invalid without a controller-conformance binding.
            return DynamicsReceipt.from_sample(
                row,
                previous_sample=previous_sample,
                source_kind="production",
                conformance_binding=conformance_binding,
            )
        if self.input_provider is None:
            return InternalWrenchReceipt(
                (0.0,) * 6,
                False,
                "controller_equivalent_dynamics_inputs_unavailable",
            )
        inputs = self.input_provider(row)
        if inputs is None:
            return InternalWrenchReceipt(
                (0.0,) * 6,
                False,
                "controller_equivalent_dynamics_inputs_unavailable",
            )
        try:
            estimate = reconstruct_internal_wrench_from_previous_command(**dict(inputs))
        except (TypeError, ValueError, KeyError) as exc:
            return InternalWrenchReceipt(
                (0.0,) * 6,
                False,
                f"internal_wrench_reconstruction_invalid:{type(exc).__name__}",
            )
        return InternalWrenchReceipt(
            estimate.wrench_tcp_si,
            True,
            residual_norm_nm=estimate.residual_norm_nm,
        )

    def produce(
        self,
        sample: DynamicsSample,
        *,
        previous_sample: DynamicsSample | None = None,
        conformance_binding: DynamicsConformanceBinding | None = None,
    ) -> DynamicsReceipt:
        result = self.reconstruct(
            sample,
            previous_sample=previous_sample,
            conformance_binding=conformance_binding,
        )
        assert isinstance(result, DynamicsReceipt)
        return result


class FakeRTDEDynamicsProvider:
    """Explicit offline-only fixture provider; never represents production conformance."""

    source_kind = "offline_fake_rtde"
    fixture_identity = OFFLINE_FAKE_RTDE_FIXTURE_ID

    def __init__(self, *, fixture_identity: str = OFFLINE_FAKE_RTDE_FIXTURE_ID) -> None:
        if fixture_identity != OFFLINE_FAKE_RTDE_FIXTURE_ID:
            raise ValueError("unknown FakeRTDE fixture identity")
        self.fixture_identity = fixture_identity

    def produce(
        self,
        sample: DynamicsSample,
        *,
        previous_sample: DynamicsSample | None = None,
    ) -> DynamicsReceipt:
        return DynamicsReceipt.from_sample(
            sample,
            previous_sample=previous_sample,
            source_kind=self.source_kind,
            fixture_identity=self.fixture_identity,
        )

    reconstruct = produce


SyntheticDynamicsReceiptProvider = FakeRTDEDynamicsProvider
FakeRTDEDynamicsReceiptProvider = FakeRTDEDynamicsProvider


class CausalKunweiAlignmentAdapter:
    """Bounded incremental adapter around the accepted causal join primitive.

    Only the previously selected sample/tick and the current sample/tick are
    needed to prove the next causal selection. Keeping the full episode and
    replaying the join on every call is O(N²), so this adapter deliberately
    keeps O(1) state on the recorder sidecar path.
    """

    def __init__(
        self,
        *,
        expected_frame_id: str,
        calibration_sha256: str,
        max_host_age_s: float = HOST_BATCH_WATCHDOG_S,
    ) -> None:
        if not expected_frame_id.strip():
            raise ValueError("Kunwei alignment frame id is required")
        if not math.isfinite(max_host_age_s) or max_host_age_s <= 0.0:
            raise ValueError("Kunwei alignment host age bound is invalid")
        self.expected_frame_id = expected_frame_id
        self.calibration_sha256 = calibration_sha256
        self.max_host_age_s = float(max_host_age_s)
        self._last_sample: CanonicalWrenchSample | None = None
        self._last_control_timestamp_s: float | None = None
        self._sample_count = 0
        self._held_ticks = 0
        self._last_sample_index: int | None = None
        self.last_error: str | None = None
        self._fault: str | None = None

    @property
    def sample_count(self) -> int:
        return self._sample_count

    @property
    def fault(self) -> str | None:
        """First alignment failure, latched for episode-level evidence."""

        return self._fault

    def align(
        self,
        *,
        control_timestamp_s: float,
        device_time_s: float | None,
        host_visible_time_s: float | None,
        batch_id: int | None,
        sample_index: int | None,
        wrench_tcp_si: Sequence[float],
        source_sequence: int | None = None,
    ) -> CausalWrenchAlignment | None:
        """Return the current causal selection, including explicit holds.

        A repeated TCP batch is represented by the same ``sample_index`` and
        is not assigned a fabricated device or arrival timestamp.  The
        accepted joiner computes the hold and both age values from the real
        clocks carried by the receipt.
        """

        control: float | None = None
        try:
            control = float(control_timestamp_s)
            if not math.isfinite(control) or control < 0.0:
                raise ValueError("control timestamp is invalid")
            if sample_index is None or batch_id is None or device_time_s is None or host_visible_time_s is None:
                raise ValueError("Kunwei source lineage is incomplete")
            sample_value = int(sample_index)
            batch_value = int(batch_id)
            if sample_value < 0 or batch_value < 0:
                raise ValueError("Kunwei source identity is invalid")
            device = float(device_time_s)
            host = float(host_visible_time_s)
            if self._last_sample_index is not None and sample_value < self._last_sample_index:
                raise ValueError("Kunwei sample index regressed")
            source_value = (
                sample_value if source_sequence is None else int(source_sequence)
            )
            wrench = _finite_vector(wrench_tcp_si, 6, "Kunwei wrench_tcp_si")
            new_sample = self._last_sample is None or sample_value > int(
                self._last_sample.sample_index
            )
            if self._last_sample is not None and not new_sample:
                previous = self._last_sample
                if (
                    batch_value != previous.batch_id
                    or source_value != previous.sequence
                    or not math.isclose(host, float(previous.host_visible_time_s), rel_tol=0.0, abs_tol=1e-12)
                    or not math.isclose(device, float(previous.device_time_s), rel_tol=0.0, abs_tol=1e-12)
                    or wrench != previous.wrench_tcp_si
                ):
                    raise ValueError("repeated Kunwei sample changed its lineage")
                sample = previous
            elif new_sample:
                sample = CanonicalWrenchSample(
                    sequence=source_value,
                    timestamp_s=device,
                    device_time_s=device,
                    host_visible_time_s=host,
                    batch_id=batch_value,
                    sample_index=sample_value,
                    wrench_tcp_si=wrench,
                    frame_id=self.expected_frame_id,
                    calibration_sha256=self.calibration_sha256,
                )
            else:
                raise ValueError("Kunwei sample identity is invalid")

            if self._last_sample is None:
                samples = (sample,)
                ticks = (control,)
            else:
                samples = (
                    (self._last_sample, sample)
                    if new_sample
                    else (self._last_sample,)
                )
                assert self._last_control_timestamp_s is not None
                ticks = (self._last_control_timestamp_s, control)
            alignment = causal_sync_wrench_1khz_to_control_500hz(
                samples,
                ticks,
                expected_frame_id=self.expected_frame_id,
                expected_calibration_sha256=self.calibration_sha256,
                max_host_age_s=self.max_host_age_s,
            )[-1]
            self._held_ticks = self._held_ticks + 1 if not new_sample else 0
            alignment = replace(
                alignment,
                external_hold=not new_sample,
                external_held_ticks=self._held_ticks,
                device_age_samples=(
                    self._held_ticks
                    * (RAW_WRENCH_RATE_HZ // CONTROL_RATE_HZ)
                ),
            )
        except (TypeError, ValueError, IndexError) as exc:
            self.last_error = f"causal_kunwei_alignment_invalid:{exc}"
            if self._fault is None:
                self._fault = self.last_error
            # A bad sensor lineage row does not erase the fact that a valid
            # 500 Hz control tick elapsed. Advancing only a valid next tick
            # lets later rows remain diagnostically aligned while the episode
            # fault above stays latched and prevents a false all-valid claim.
            if (
                control is not None
                and math.isfinite(control)
                and self._last_control_timestamp_s is not None
                and math.isclose(
                    control - self._last_control_timestamp_s,
                    1.0 / CONTROL_RATE_HZ,
                    rel_tol=0.05,
                    abs_tol=1e-9,
                )
            ):
                self._last_control_timestamp_s = control
            return None
        if new_sample:
            self._sample_count += 1
        self._last_sample = sample
        self._last_sample_index = sample_value
        self._last_control_timestamp_s = control
        self.last_error = None
        return alignment


def first_live_shadow_from_receipt(path: str | Path | None) -> bool:
    """Keep the first shadow forced until an explicit receipt closes it."""

    if path is None:
        return True
    receipt_path = Path(path)
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("shadow transition receipt is invalid") from exc
    if not isinstance(payload, Mapping) or payload.get("schema") != SHADOW_TRANSITION_SCHEMA:
        raise ValueError("shadow transition receipt schema mismatch")
    if payload.get("first_live_shadow_complete") is not True:
        raise ValueError("shadow transition receipt does not close first live shadow")
    if payload.get("training_transition_authorized") is not True:
        raise ValueError("shadow transition receipt lacks training transition authorization")
    unsigned = dict(payload)
    supplied = unsigned.pop("receipt_sha256", None)
    if not isinstance(supplied, str):
        raise ValueError("shadow transition receipt hash is required")
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if supplied != hashlib.sha256(canonical.encode("utf-8")).hexdigest():
        raise ValueError("shadow transition receipt hash mismatch")
    return False


__all__ = [
    "ActiveTrainingWindow",
    "ActionLabel",
    "ActionLabelContext",
    "ActionLabelProvider",
    "ACTION_LABEL_CONTEXT_SCHEMA",
    "ACTION_LABEL_PROVIDER_SCHEMA",
    "CANONICAL_EXPERT_ACTION_SEMANTICS",
    "CANONICAL_EXPERT_POLICY_ID",
    "CausalKunweiAlignmentAdapter",
    "DETERMINISTIC_ACTION_SOURCE",
    "DIAGNOSTIC_ACTION_SEMANTICS",
    "DIAGNOSTIC_ACTION_SOURCE",
    "DiagnosticShadowActionProvider",
    "DeterministicExpertActionProvider",
    "FakeRTDEDynamicsProvider",
    "FakeRTDEDynamicsReceiptProvider",
    "EpisodeSemanticContext",
    "InternalWrenchReceipt",
    "InternalWrenchReconstructionProvider",
    "SyntheticDynamicsReceiptProvider",
    "REQUIRED_RUNTIME_HASH_IDENTITIES",
    "SEMANTIC_CONTEXT_SCHEMA",
    "SHADOW_TRANSITION_SCHEMA",
    "first_live_shadow_from_receipt",
    "resolve_active_training_window",
]
