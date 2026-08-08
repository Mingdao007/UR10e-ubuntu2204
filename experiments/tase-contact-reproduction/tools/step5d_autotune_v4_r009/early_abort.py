"""Offline, shadow-only R009 early-abort primitives.

The module has no controller, runtime-control, GP-training, or raw-ledger
path.  Its metric is built from the canonical force-objective builder and
uses only sealed sufficient statistics when producing a causal snapshot;
bounded sidecar/audit file operations remain identity-bound and offline.
"""

from __future__ import annotations

import enum
import json
import math
import os
import re
from collections.abc import Sequence as ABCSequence, Set as ABCSet
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from step5d_force_objective import (
    BIN_WIDTH_S,
    FORMAL_END_S,
    FORMAL_START_S,
    ForceObjective,
    ForceObjectiveBuilder,
    ForceObjectiveError,
    ForcePathSample,
    REQUIRED_BINS,
    TARGET_FORCE_N,
)

from .identity import (
    DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG,
    R009_EARLY_ABORT_AUDIT_SCHEMA,
    R009EarlyAbortConfig,
    R009IdentityError,
    R009ReleaseIdentity,
    canonical_bytes,
    require_digest,
    sha256_bytes,
    validate_release_identity,
)


class R009EarlyAbortError(ValueError):
    """Base error for the pure R009 early-abort primitives."""


class R009MetricError(R009EarlyAbortError):
    """A sample or causal metric request is invalid."""


class R009WatermarkRegression(R009MetricError):
    """A causal watermark moved backwards."""


class R009ModeError(R009EarlyAbortError):
    """The requested early-abort mode is not a supported R009 mode."""


class R009ActiveModeRejected(R009ModeError):
    """R009 active/hard-stop mode is fail-closed and unavailable."""


class R009BindingError(R009EarlyAbortError):
    """A release, execution, dispatch, or payload binding is invalid."""


class R009ReleaseIdentityBindingError(R009BindingError):
    """A record's full release identity or digest is invalid."""


class R009ExecutionIdentityBindingError(R009BindingError):
    """An execution identity is missing or its digest is unstable/invalid."""


class R009DispatchIdentityBindingError(R009BindingError):
    """A dispatch identity is missing or has an invalid sequence."""


class R009PayloadBindingError(R009BindingError):
    """A payload is not canonical JSON or its digest does not bind it."""


class R009DispatchConflict(R009BindingError):
    """One dispatch identity was reused with a different payload/identity."""


class R009OutcomeCode(str, enum.Enum):
    """Stable codes for this pure slice's typed outcomes."""

    SAMPLE_ACCEPTED = "sample_accepted"
    SAMPLE_IDEMPOTENT_DUPLICATE = "sample_idempotent_duplicate"
    SAMPLE_REJECTED = "sample_rejected"
    WATERMARK_ACCEPTED = "watermark_accepted"
    WATERMARK_REGRESSION = "watermark_regression"
    MODE_SHADOW = "mode_shadow"
    MODE_OFF = "mode_off"
    ACTIVE_MODE_REJECTED = "active_mode_rejected"
    ADMITTED = "admitted"
    IDEMPOTENT_DUPLICATE = "idempotent_duplicate"
    RELEASE_IDENTITY_REJECTED = "release_identity_rejected"
    EXECUTION_IDENTITY_REJECTED = "execution_identity_rejected"
    DISPATCH_IDENTITY_REJECTED = "dispatch_identity_rejected"
    PAYLOAD_REJECTED = "payload_rejected"
    DISPATCH_IDENTITY_CONFLICT = "dispatch_identity_conflict"
    AUDIT_APPENDED = "audit_appended"
    AUDIT_WRITE_FAILED = "audit_write_failed"
    AUDIT_METADATA_REJECTED = "audit_metadata_rejected"
    SIDECAR_ADMITTED = "sidecar_admitted"
    SIDECAR_IDEMPOTENT_DUPLICATE = "sidecar_idempotent_duplicate"
    SIDECAR_DISPATCH_CONFLICT = "sidecar_dispatch_conflict"
    SIDECAR_MALFORMED_ROW = "sidecar_malformed_row"
    SIDECAR_SCHEMA_MISMATCH = "sidecar_schema_mismatch"
    SIDECAR_POLICY_MISMATCH = "sidecar_policy_mismatch"
    SIDECAR_RELEASE_IDENTITY_MISMATCH = "sidecar_release_identity_mismatch"
    SIDECAR_EXECUTION_IDENTITY_MISMATCH = "sidecar_execution_identity_mismatch"
    SIDECAR_DISPATCH_IDENTITY_MISMATCH = "sidecar_dispatch_identity_mismatch"
    SIDECAR_PAYLOAD_IDENTITY_MISMATCH = "sidecar_payload_identity_mismatch"
    SIDECAR_READ_FAILED = "sidecar_read_failed"
    SIDECAR_WRITE_FAILED = "sidecar_write_failed"


@dataclass(frozen=True)
class R009EarlyAbortOutcome:
    """Typed, non-persistent result for a pure R009 operation."""

    code: R009OutcomeCode
    accepted: bool
    detail: str = ""
    snapshot: "R009PartialMetricSnapshot | None" = None
    record: "R009EarlyAbortRecord | None" = None
    audit_event: "R009AuditEvent | None" = None

    @property
    def code_value(self) -> str:
        return self.code.value

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code.value,
            "accepted": self.accepted,
            "detail": self.detail,
        }
        if self.snapshot is not None:
            result["snapshot"] = self.snapshot.as_dict()
        if self.record is not None:
            result["record"] = self.record.as_dict()
        if self.audit_event is not None:
            result["audit_event"] = self.audit_event.as_dict()
        return result


@dataclass(frozen=True)
class R009PartialMetricSnapshot:
    """Causal formal-window statistic at one monotonic PATH watermark.

    ``partial_mae_n`` intentionally retains the formal objective denominator
    of 550 bins.  Only bins whose right boundary is at or before the supplied
    watermark contribute; the currently open bin and all future bins
    contribute no term.  Therefore a snapshot at full coverage is exactly the
    canonical formal MAE, while earlier snapshots remain causal lower-bound
    statistics rather than a sample-weighted approximation.
    """

    watermark_s: float
    closed_bin_count: int
    observed_closed_bins: int
    numerator_n: float
    denominator_bins: int
    partial_mae_n: float
    open_bin_index: int | None
    complete_bins: int
    formal_mae_n: float | None

    @property
    def mae_n(self) -> float:
        """Alias used by objective-facing callers."""

        return self.partial_mae_n

    def as_dict(self) -> dict[str, Any]:
        return {
            "watermark_s": self.watermark_s,
            "closed_bin_count": self.closed_bin_count,
            "observed_closed_bins": self.observed_closed_bins,
            "numerator_n": self.numerator_n,
            "denominator_bins": self.denominator_bins,
            "partial_mae_n": self.partial_mae_n,
            "open_bin_index": self.open_bin_index,
            "complete_bins": self.complete_bins,
            "formal_mae_n": self.formal_mae_n,
            "target_force_n": TARGET_FORCE_N,
            "bin_width_s": BIN_WIDTH_S,
            "formal_window_s": [FORMAL_START_S, FORMAL_END_S],
            "window_semantics": "[start,end)",
            "open_bin_policy": "exclude_until_bin_end",
        }


def _finite_watermark(value: Any) -> float:
    if isinstance(value, bool):
        raise R009MetricError("causal watermark must be numeric")
    try:
        watermark = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise R009MetricError("causal watermark must be numeric") from exc
    if not math.isfinite(watermark) or watermark < 0.0:
        raise R009MetricError("causal watermark must be finite and non-negative")
    return watermark


def _coerce_sample(sample: ForcePathSample | Mapping[str, Any]) -> ForcePathSample:
    if isinstance(sample, ForcePathSample):
        return sample
    if isinstance(sample, Mapping):
        try:
            return ForcePathSample.from_mapping(sample)
        except ForceObjectiveError as exc:
            raise R009MetricError(str(exc)) from exc
    raise R009MetricError("R009 causal metric accepts only ForcePathSample or its mapping")


class R009CausalFormalMetric:
    """Compose the canonical force builder into a causal partial metric."""

    def __init__(self, builder: ForceObjectiveBuilder | None = None) -> None:
        self.builder = ForceObjectiveBuilder() if builder is None else builder
        if not isinstance(self.builder, ForceObjectiveBuilder):
            raise R009MetricError("builder must be ForceObjectiveBuilder")
        self._last_watermark_s: float | None = None

    def observe(self, sample: ForcePathSample | Mapping[str, Any]) -> bool:
        """Add one canonical sample; return false for its canonical replay."""

        normalized = _coerce_sample(sample)
        if (
            self._last_watermark_s is not None
            and normalized.path_time_s < self._last_watermark_s
        ):
            raise R009MetricError(
                "sample PATH time precedes the sealed causal watermark"
            )
        try:
            return self.builder.add(normalized)
        except ForceObjectiveError as exc:
            raise R009MetricError(str(exc)) from exc

    add = observe

    def observe_outcome(
        self, sample: ForcePathSample | Mapping[str, Any]
    ) -> R009EarlyAbortOutcome:
        try:
            accepted = self.observe(sample)
        except R009MetricError as exc:
            return R009EarlyAbortOutcome(
                code=R009OutcomeCode.SAMPLE_REJECTED,
                accepted=False,
                detail=str(exc),
            )
        return R009EarlyAbortOutcome(
            code=(
                R009OutcomeCode.SAMPLE_ACCEPTED
                if accepted
                else R009OutcomeCode.SAMPLE_IDEMPOTENT_DUPLICATE
            ),
            accepted=True,
        )

    @staticmethod
    def _closed_bin_count(watermark_s: float) -> int:
        if watermark_s <= FORMAL_START_S:
            return 0
        if watermark_s >= FORMAL_END_S:
            return REQUIRED_BINS
        open_index = ForceObjectiveBuilder._bin(
            watermark_s, FORMAL_START_S, FORMAL_END_S
        )
        if open_index is None:
            raise R009MetricError("canonical formal watermark bin is undefined")
        return open_index

    def snapshot(self, watermark_s: float) -> R009PartialMetricSnapshot:
        """Return the formal-denominator metric up to a monotonic watermark."""

        watermark = _finite_watermark(watermark_s)
        if (
            self._last_watermark_s is not None
            and watermark < self._last_watermark_s
        ):
            raise R009WatermarkRegression(
                "causal watermark moved backwards"
            )
        self._last_watermark_s = watermark

        try:
            objective: ForceObjective = self.builder.finalize(
                provenance="r009_early_abort_shadow"
            )
        except ForceObjectiveError as exc:
            raise R009MetricError(str(exc)) from exc

        closed = self._closed_bin_count(watermark)
        numerator = math.fsum(
            abs(
                (objective.formal_bin_sum_n[index]
                 / objective.formal_bin_count[index])
                - TARGET_FORCE_N
            )
            for index in range(closed)
            if objective.formal_bin_count[index] > 0
        )
        observed = sum(
            1
            for index in range(closed)
            if objective.formal_bin_count[index] > 0
        )
        open_index = None
        if FORMAL_START_S <= watermark < FORMAL_END_S:
            open_index = ForceObjectiveBuilder._bin(
                watermark, FORMAL_START_S, FORMAL_END_S
            )
        return R009PartialMetricSnapshot(
            watermark_s=watermark,
            closed_bin_count=closed,
            observed_closed_bins=observed,
            numerator_n=numerator,
            denominator_bins=REQUIRED_BINS,
            partial_mae_n=numerator / REQUIRED_BINS,
            open_bin_index=open_index,
            # ``complete_bins`` is deliberately watermark-scoped.  The
            # builder may be prefilled with later evidence, but that evidence
            # must not appear in an early causal snapshot.
            complete_bins=observed,
            # A formal MAE is exposed only after the complete formal window
            # has closed and all 550 canonical bins are present.
            formal_mae_n=(
                objective.v2_mae_n
                if watermark >= FORMAL_END_S
                and objective.complete_bins == REQUIRED_BINS
                else None
            ),
        )


R009CausalFormalObjective = R009CausalFormalMetric


def _nonempty_binding_text(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise R009BindingError(f"{role} must be a non-empty string")
    return value


def _canonical_json_copy(value: Any, role: str) -> Any:
    """Detach finite JSON through the existing canonical identity primitive."""

    try:
        return json.loads(canonical_bytes(value).decode("utf-8"))
    except (R009IdentityError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise R009BindingError(f"{role} must be finite canonical JSON") from exc


def _freeze_json_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json_tree(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json_tree(item) for item in value)
    return value


def _thaw_json_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json_tree(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_tree(item) for item in value]
    return value


def canonical_payload_digest(payload: Any) -> str:
    """Digest one JSON payload with the repository's existing canonical bytes."""

    try:
        copied = _canonical_json_copy(payload, "payload")
        return sha256_bytes(canonical_bytes(copied))
    except (R009BindingError, R009IdentityError, TypeError, ValueError) as exc:
        raise R009PayloadBindingError(
            "payload must be finite canonical JSON"
        ) from exc


@dataclass(frozen=True)
class R009ExecutionIdentity:
    """Stable execution identity bound to a positive dispatch sequence."""

    execution_id: str
    dispatch_sequence: int
    identity: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        execution_id = _nonempty_binding_text(self.execution_id, "execution_id")
        if (
            isinstance(self.dispatch_sequence, bool)
            or not isinstance(self.dispatch_sequence, int)
            or self.dispatch_sequence <= 0
        ):
            raise R009ExecutionIdentityBindingError(
                "execution dispatch_sequence must be a positive int"
            )
        if not isinstance(self.identity, Mapping):
            raise R009ExecutionIdentityBindingError(
                "execution identity attributes must be a mapping"
            )
        try:
            attributes = _canonical_json_copy(
                self.identity, "execution identity attributes"
            )
        except R009BindingError as exc:
            raise R009ExecutionIdentityBindingError(str(exc)) from exc
        try:
            canonical_bytes(
                {
                    "execution_id": execution_id,
                    "dispatch_sequence": self.dispatch_sequence,
                    "identity": attributes,
                }
            )
        except (R009IdentityError, TypeError, ValueError) as exc:
            raise R009ExecutionIdentityBindingError(
                "execution identity attributes must be finite canonical JSON"
            ) from exc
        object.__setattr__(self, "execution_id", execution_id)
        object.__setattr__(self, "identity", _freeze_json_tree(attributes))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R009ExecutionIdentity":
        if not isinstance(value, Mapping):
            raise R009ExecutionIdentityBindingError(
                "execution identity must be a mapping"
            )
        if "execution_id" not in value or "dispatch_sequence" not in value:
            raise R009ExecutionIdentityBindingError(
                "execution identity fields are incomplete"
            )
        supplied_digest = value.get("execution_identity_sha256")
        if "identity" in value and "execution_identity" in value:
            if value["identity"] != value["execution_identity"]:
                raise R009ExecutionIdentityBindingError(
                    "execution identity aliases differ"
                )
        if "identity" in value:
            attributes = value["identity"]
        elif "execution_identity" in value:
            attributes = value["execution_identity"]
        else:
            reserved = {
                "execution_id",
                "dispatch_sequence",
                "execution_identity_sha256",
            }
            attributes = {key: item for key, item in value.items() if key not in reserved}
        identity = cls(
            execution_id=value["execution_id"],
            dispatch_sequence=value["dispatch_sequence"],
            identity=attributes,
        )
        if supplied_digest is not None:
            try:
                supplied = require_digest(
                    supplied_digest, "execution identity sha256"
                )
            except (R009IdentityError, TypeError, ValueError) as exc:
                raise R009ExecutionIdentityBindingError(
                    "execution identity sha256 is invalid"
                ) from exc
            if supplied != identity.execution_identity_sha256:
                raise R009ExecutionIdentityBindingError(
                    "execution identity sha256 differs"
                )
        return identity

    def identity_payload(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "dispatch_sequence": self.dispatch_sequence,
            "identity": _thaw_json_tree(self.identity),
        }

    @property
    def execution_identity_sha256(self) -> str:
        return sha256_bytes(canonical_bytes(self.identity_payload()))

    @property
    def digest(self) -> str:
        return self.execution_identity_sha256

    def as_dict(self) -> dict[str, Any]:
        return self.identity_payload()


@dataclass(frozen=True)
class R009DispatchIdentity:
    """Stable dispatch identity used for exactly-once in-memory admission."""

    dispatch_id: str
    dispatch_sequence: int
    identity: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        dispatch_id = _nonempty_binding_text(self.dispatch_id, "dispatch_id")
        if (
            isinstance(self.dispatch_sequence, bool)
            or not isinstance(self.dispatch_sequence, int)
            or self.dispatch_sequence <= 0
        ):
            raise R009DispatchIdentityBindingError(
                "dispatch_sequence must be a positive int"
            )
        if not isinstance(self.identity, Mapping):
            raise R009DispatchIdentityBindingError(
                "dispatch identity attributes must be a mapping"
            )
        try:
            attributes = _canonical_json_copy(
                self.identity, "dispatch identity attributes"
            )
        except R009BindingError as exc:
            raise R009DispatchIdentityBindingError(str(exc)) from exc
        try:
            canonical_bytes(
                {
                    "dispatch_id": dispatch_id,
                    "dispatch_sequence": self.dispatch_sequence,
                    "identity": attributes,
                }
            )
        except (R009IdentityError, TypeError, ValueError) as exc:
            raise R009DispatchIdentityBindingError(
                "dispatch identity attributes must be finite canonical JSON"
            ) from exc
        object.__setattr__(self, "dispatch_id", dispatch_id)
        object.__setattr__(self, "identity", _freeze_json_tree(attributes))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R009DispatchIdentity":
        if not isinstance(value, Mapping):
            raise R009DispatchIdentityBindingError(
                "dispatch identity must be a mapping"
            )
        if "dispatch_id" not in value or "dispatch_sequence" not in value:
            raise R009DispatchIdentityBindingError(
                "dispatch identity fields are incomplete"
            )
        supplied_digest = value.get("dispatch_identity_sha256")
        if "identity" in value and "dispatch_identity" in value:
            if value["identity"] != value["dispatch_identity"]:
                raise R009DispatchIdentityBindingError(
                    "dispatch identity aliases differ"
                )
        if "identity" in value:
            attributes = value["identity"]
        elif "dispatch_identity" in value:
            attributes = value["dispatch_identity"]
        else:
            reserved = {
                "dispatch_id",
                "dispatch_sequence",
                "dispatch_identity_sha256",
            }
            attributes = {key: item for key, item in value.items() if key not in reserved}
        identity = cls(
            dispatch_id=value["dispatch_id"],
            dispatch_sequence=value["dispatch_sequence"],
            identity=attributes,
        )
        if supplied_digest is not None:
            try:
                supplied = require_digest(
                    supplied_digest, "dispatch identity sha256"
                )
            except (R009IdentityError, TypeError, ValueError) as exc:
                raise R009DispatchIdentityBindingError(
                    "dispatch identity sha256 is invalid"
                ) from exc
            if supplied != identity.dispatch_identity_sha256:
                raise R009DispatchIdentityBindingError(
                    "dispatch identity sha256 differs"
                )
        return identity

    def identity_payload(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "dispatch_sequence": self.dispatch_sequence,
            "identity": _thaw_json_tree(self.identity),
        }

    @property
    def dispatch_identity_sha256(self) -> str:
        return sha256_bytes(canonical_bytes(self.identity_payload()))

    @property
    def digest(self) -> str:
        return self.dispatch_identity_sha256

    def as_dict(self) -> dict[str, Any]:
        return self.identity_payload()

    @property
    def dispatch_key(self) -> tuple[int, str]:
        """The dispatch slot, independent of mutable/extra dispatch attributes."""

        return (self.dispatch_sequence, self.dispatch_id)


@dataclass(frozen=True)
class R009EarlyAbortRecord:
    """Typed in-memory shadow record with all identity/digest bindings."""

    release_identity: R009ReleaseIdentity
    release_identity_sha256: str
    execution_identity: R009ExecutionIdentity
    execution_identity_sha256: str
    dispatch_identity: R009DispatchIdentity
    dispatch_identity_sha256: str
    payload: Any
    payload_sha256: str

    def __post_init__(self) -> None:
        try:
            payload_copy = _canonical_json_copy(self.payload, "record payload")
        except R009BindingError as exc:
            raise R009PayloadBindingError(str(exc)) from exc
        object.__setattr__(self, "payload", _freeze_json_tree(payload_copy))
        if not isinstance(self.release_identity, R009ReleaseIdentity):
            raise R009ReleaseIdentityBindingError(
                "release_identity must be the typed R009 release identity"
            )
        try:
            release_digest = require_digest(
                self.release_identity_sha256, "release identity sha256"
            )
        except (R009IdentityError, TypeError, ValueError) as exc:
            raise R009ReleaseIdentityBindingError(
                "release identity sha256 is invalid"
            ) from exc
        if release_digest != self.release_identity.release_identity_sha256:
            raise R009ReleaseIdentityBindingError(
                "release identity sha256 differs from full release identity"
            )
        if not isinstance(self.execution_identity, R009ExecutionIdentity):
            raise R009ExecutionIdentityBindingError(
                "execution_identity must be typed"
            )
        try:
            execution_digest = require_digest(
                self.execution_identity_sha256, "execution identity sha256"
            )
        except (R009IdentityError, TypeError, ValueError) as exc:
            raise R009ExecutionIdentityBindingError(
                "execution identity sha256 is invalid"
            ) from exc
        if execution_digest != self.execution_identity.execution_identity_sha256:
            raise R009ExecutionIdentityBindingError(
                "execution identity sha256 differs"
            )
        if not isinstance(self.dispatch_identity, R009DispatchIdentity):
            raise R009DispatchIdentityBindingError(
                "dispatch_identity must be typed"
            )
        try:
            dispatch_digest = require_digest(
                self.dispatch_identity_sha256, "dispatch identity sha256"
            )
        except (R009IdentityError, TypeError, ValueError) as exc:
            raise R009DispatchIdentityBindingError(
                "dispatch identity sha256 is invalid"
            ) from exc
        if dispatch_digest != self.dispatch_identity.dispatch_identity_sha256:
            raise R009DispatchIdentityBindingError(
                "dispatch identity sha256 differs"
            )
        if (
            self.execution_identity.dispatch_sequence
            != self.dispatch_identity.dispatch_sequence
        ):
            raise R009DispatchIdentityBindingError(
                "execution and dispatch sequences differ"
            )
        try:
            payload_digest = require_digest(self.payload_sha256, "payload sha256")
        except (R009IdentityError, TypeError, ValueError) as exc:
            raise R009PayloadBindingError("payload sha256 is invalid") from exc
        if payload_digest != canonical_payload_digest(self.payload):
            raise R009PayloadBindingError("payload sha256 differs")

    @classmethod
    def from_parts(
        cls,
        *,
        release_identity: R009ReleaseIdentity | Mapping[str, Any],
        release_identity_sha256: str | None = None,
        execution_identity: R009ExecutionIdentity | Mapping[str, Any],
        execution_identity_sha256: str | None = None,
        dispatch_identity: R009DispatchIdentity | Mapping[str, Any],
        dispatch_identity_sha256: str | None = None,
        payload: Any,
        payload_sha256: str | None = None,
    ) -> "R009EarlyAbortRecord":
        try:
            release = (
                release_identity
                if isinstance(release_identity, R009ReleaseIdentity)
                else validate_release_identity(release_identity)
            )
        except (R009IdentityError, TypeError, ValueError) as exc:
            raise R009ReleaseIdentityBindingError(
                "release identity is invalid"
            ) from exc
        execution = (
            execution_identity
            if isinstance(execution_identity, R009ExecutionIdentity)
            else R009ExecutionIdentity.from_mapping(execution_identity)
        )
        dispatch = (
            dispatch_identity
            if isinstance(dispatch_identity, R009DispatchIdentity)
            else R009DispatchIdentity.from_mapping(dispatch_identity)
        )
        return cls(
            release_identity=release,
            release_identity_sha256=(
                release.release_identity_sha256
                if release_identity_sha256 is None
                else release_identity_sha256
            ),
            execution_identity=execution,
            execution_identity_sha256=(
                execution.execution_identity_sha256
                if execution_identity_sha256 is None
                else execution_identity_sha256
            ),
            dispatch_identity=dispatch,
            dispatch_identity_sha256=(
                dispatch.dispatch_identity_sha256
                if dispatch_identity_sha256 is None
                else dispatch_identity_sha256
            ),
            payload=payload,
            payload_sha256=(
                canonical_payload_digest(payload)
                if payload_sha256 is None
                else payload_sha256
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R009EarlyAbortRecord":
        required = {
            "release_identity",
            "release_identity_sha256",
            "execution_identity",
            "execution_identity_sha256",
            "dispatch_identity",
            "dispatch_identity_sha256",
            "payload",
            "payload_sha256",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise R009BindingError("early-abort record fields differ")
        try:
            return cls.from_parts(
                release_identity=value["release_identity"],
                release_identity_sha256=value["release_identity_sha256"],
                execution_identity=value["execution_identity"],
                execution_identity_sha256=value["execution_identity_sha256"],
                dispatch_identity=value["dispatch_identity"],
                dispatch_identity_sha256=value["dispatch_identity_sha256"],
                payload=value["payload"],
                payload_sha256=value["payload_sha256"],
            )
        except R009BindingError:
            raise
        except (TypeError, ValueError) as exc:
            raise R009BindingError("early-abort record binding is invalid") from exc

    def as_dict(self) -> dict[str, Any]:
        return {
            "release_identity": self.release_identity.as_dict(),
            "release_identity_sha256": self.release_identity_sha256,
            "execution_identity": self.execution_identity.as_dict(),
            "execution_identity_sha256": self.execution_identity_sha256,
            "dispatch_identity": self.dispatch_identity.as_dict(),
            "dispatch_identity_sha256": self.dispatch_identity_sha256,
            "payload": _thaw_json_tree(self.payload),
            "payload_sha256": self.payload_sha256,
        }


class R009InMemorySidecar:
    """Pure in-memory exactly-once admission; no filesystem or control I/O."""

    def __init__(
        self,
        release_identity: R009ReleaseIdentity | Mapping[str, Any],
        *,
        release_identity_sha256: str | None = None,
    ) -> None:
        try:
            identity = (
                release_identity
                if isinstance(release_identity, R009ReleaseIdentity)
                else validate_release_identity(release_identity)
            )
        except (R009IdentityError, TypeError, ValueError) as exc:
            raise R009ReleaseIdentityBindingError(
                "expected release identity is invalid"
            ) from exc
        expected_digest = identity.release_identity_sha256
        if release_identity_sha256 is not None:
            try:
                supplied = require_digest(
                    release_identity_sha256, "expected release identity sha256"
                )
            except (R009IdentityError, TypeError, ValueError) as exc:
                raise R009ReleaseIdentityBindingError(
                    "expected release identity sha256 is invalid"
                ) from exc
            if supplied != expected_digest:
                raise R009ReleaseIdentityBindingError(
                    "expected release identity sha256 differs"
                )
        self.release_identity = identity
        self.release_identity_sha256 = expected_digest
        self._records: list[R009EarlyAbortRecord] = []

    @property
    def records(self) -> tuple[R009EarlyAbortRecord, ...]:
        return tuple(self._records)

    def admit(
        self, record: R009EarlyAbortRecord | Mapping[str, Any]
    ) -> R009EarlyAbortOutcome:
        try:
            normalized = (
                record
                if isinstance(record, R009EarlyAbortRecord)
                else R009EarlyAbortRecord.from_mapping(record)
            )
        except R009ReleaseIdentityBindingError as exc:
            return R009EarlyAbortOutcome(
                code=R009OutcomeCode.RELEASE_IDENTITY_REJECTED,
                accepted=False,
                detail=str(exc),
            )
        except R009ExecutionIdentityBindingError as exc:
            return R009EarlyAbortOutcome(
                code=R009OutcomeCode.EXECUTION_IDENTITY_REJECTED,
                accepted=False,
                detail=str(exc),
            )
        except R009DispatchIdentityBindingError as exc:
            return R009EarlyAbortOutcome(
                code=R009OutcomeCode.DISPATCH_IDENTITY_REJECTED,
                accepted=False,
                detail=str(exc),
            )
        except R009PayloadBindingError as exc:
            return R009EarlyAbortOutcome(
                code=R009OutcomeCode.PAYLOAD_REJECTED,
                accepted=False,
                detail=str(exc),
            )
        except R009BindingError as exc:
            return R009EarlyAbortOutcome(
                code=R009OutcomeCode.PAYLOAD_REJECTED,
                accepted=False,
                detail=str(exc),
            )

        dispatch_key = normalized.dispatch_identity.dispatch_key
        for prior in self._records:
            if prior.dispatch_identity.dispatch_key != dispatch_key:
                continue
            if (
                prior.release_identity_sha256
                == normalized.release_identity_sha256
                and prior.execution_identity_sha256
                == normalized.execution_identity_sha256
                and prior.dispatch_identity_sha256
                == normalized.dispatch_identity_sha256
                and
                prior.payload_sha256 == normalized.payload_sha256
            ):
                return R009EarlyAbortOutcome(
                    code=R009OutcomeCode.IDEMPOTENT_DUPLICATE,
                    accepted=True,
                    detail="exact dispatch/payload/identity duplicate",
                    record=prior,
                )
            return R009EarlyAbortOutcome(
                code=R009OutcomeCode.DISPATCH_IDENTITY_CONFLICT,
                accepted=False,
                detail="dispatch identity was reused with a different payload or identity",
                record=prior,
            )

        if (
            normalized.release_identity_sha256 != self.release_identity_sha256
            or normalized.release_identity != self.release_identity
        ):
            return R009EarlyAbortOutcome(
                code=R009OutcomeCode.RELEASE_IDENTITY_REJECTED,
                accepted=False,
                detail="record release identity differs from the admitted release",
            )

        self._records.append(normalized)
        return R009EarlyAbortOutcome(
            code=R009OutcomeCode.ADMITTED,
            accepted=True,
            record=normalized,
        )

    admit_record = admit


class R009SidecarError(R009EarlyAbortError):
    """Base error for the strict R009 shadow-sidecar boundary."""


class R009SidecarConfigError(R009SidecarError):
    """The sidecar path or identity-bound config is invalid."""


class R009SidecarRowError(R009SidecarError):
    """A sidecar row has a stable typed classification code."""

    def __init__(self, code: R009OutcomeCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def _coerce_early_abort_config(config: Any = None) -> R009EarlyAbortConfig:
    if isinstance(config, R009EarlyAbortConfig):
        return config
    if config is None:
        config = DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG["values"]["early_abort"]
    elif isinstance(config, Mapping):
        if "values" in config and isinstance(config.get("values"), Mapping):
            values = config["values"]
            if isinstance(values.get("early_abort"), Mapping):
                config = values["early_abort"]
        elif isinstance(config.get("early_abort"), Mapping):
            config = config["early_abort"]
    try:
        return R009EarlyAbortConfig.from_mapping(config)
    except (R009IdentityError, TypeError, ValueError) as exc:
        raise R009SidecarConfigError("R009 early-abort config is invalid") from exc


_SIDECAR_ROW_FIELDS = frozenset(
    {
        "schema",
        "version",
        "record_type",
        "channel",
        "shadow_only",
        "enters_gp_training",
        "enters_raw_ledger",
        "release_identity",
        "release_identity_sha256",
        "execution_identity",
        "execution_identity_sha256",
        "dispatch_identity",
        "dispatch_identity_sha256",
        "payload",
        "payload_sha256",
    }
)


def _sidecar_event(
    code: R009OutcomeCode,
    detail: str,
    *,
    config: R009EarlyAbortConfig,
    metadata: Any = None,
    accepted: bool = False,
    record: "R009EarlyAbortRecord | None" = None,
) -> R009EarlyAbortOutcome:
    event = R009AuditEvent(
        code=code,
        metadata={} if metadata is None else metadata,
        detail=detail,
        schema=config.audit_schema,
    )
    return R009EarlyAbortOutcome(
        code=code,
        accepted=accepted,
        detail=detail,
        record=record,
        audit_event=event,
    )


class R009AuditMetadataError(R009BindingError):
    """Audit metadata limits or canonical representation are invalid."""


@dataclass(frozen=True)
class R009AuditLimits:
    """Bounded audit metadata limits resolved from the R009 config."""

    max_depth: int = 4
    max_items: int = 64
    max_string_chars: int = 256
    max_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        for name in (
            "max_depth",
            "max_items",
            "max_string_chars",
            "max_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise R009AuditMetadataError(
                    f"audit limit {name} must be a positive int"
                )

    @classmethod
    def from_config(cls, config: Any = None) -> "R009AuditLimits":
        return cls(
            max_depth=_config_limit(
                config, "max_audit_metadata_depth", 4
            ),
            max_items=_config_limit(
                config, "max_audit_metadata_items", 64
            ),
            max_string_chars=_config_limit(
                config, "max_audit_metadata_string_chars", 256
            ),
            max_bytes=_config_limit(
                config,
                "max_audit_metadata_bytes",
                _config_limit(config, "max_audit_bytes", 64 * 1024),
            ),
        )


_MISSING = object()


def _config_limit(config: Any, name: str, default: int) -> int:
    """Read a limit from a typed config or its plain executable mapping."""

    if config is None:
        return default
    candidates: list[Any] = [config]
    if isinstance(config, Mapping):
        nested = config.get("early_abort")
        if isinstance(nested, Mapping):
            candidates.append(nested)
        values = config.get("values")
        if isinstance(values, Mapping) and isinstance(values.get("early_abort"), Mapping):
            candidates.append(values["early_abort"])
    else:
        raw = getattr(config, "raw", None)
        if isinstance(raw, Mapping):
            candidates.append(raw)
            nested = raw.get("early_abort")
            if isinstance(nested, Mapping):
                candidates.append(nested)
            values = raw.get("values")
            if isinstance(values, Mapping) and isinstance(values.get("early_abort"), Mapping):
                candidates.append(values["early_abort"])
    for candidate in candidates:
        if isinstance(candidate, Mapping) and name in candidate:
            return candidate[name]
        if not isinstance(candidate, Mapping):
            value = getattr(candidate, name, _MISSING)
            if value is not _MISSING:
                return value
    return default


def _qualified_type(value: Any) -> str:
    cls = value if isinstance(value, type) else type(value)
    module = getattr(cls, "__module__", "builtins")
    qualname = getattr(cls, "__qualname__", getattr(cls, "__name__", "object"))
    if not isinstance(module, str) or not module:
        module = "builtins"
    if not isinstance(qualname, str) or not qualname:
        qualname = "object"
    return f"{module}.{qualname}"


def _bounded_string(value: str, limits: R009AuditLimits) -> str | dict[str, Any]:
    if len(value) <= limits.max_string_chars:
        return value
    return {
        "__truncated__": "string",
        "value": value[: limits.max_string_chars],
    }


def _object_vars(value: Any) -> dict[str, Any]:
    try:
        raw = vars(value)
    except (TypeError, AttributeError):
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _magicmock_metadata(
    value: Any, limits: R009AuditLimits
) -> dict[str, Any] | None:
    raw = _object_vars(value)
    module = getattr(type(value), "__module__", "")
    type_name = getattr(type(value), "__name__", "")
    marker_keys = {"_mock_name", "_mock_children", "_mock_methods", "_spec_class"}
    if not (
        isinstance(module, str)
        and module.startswith("unittest.mock")
    ) and not marker_keys.intersection(raw):
        return None
    result: dict[str, Any] = {"__object_type__": _qualified_type(value)}
    mock_name = raw.get("_mock_name")
    if isinstance(mock_name, str) and mock_name:
        result["name"] = _bounded_string(mock_name, limits)
    spec_class = raw.get("_spec_class")
    if isinstance(spec_class, type):
        result["spec_type"] = _qualified_type(spec_class)
    elif spec_class is not None:
        result["spec_type"] = _qualified_type(spec_class)
    if not result.get("name") and isinstance(type_name, str) and type_name:
        result["class_name"] = type_name
    return result


@dataclass
class _AuditCanonicalState:
    limits: R009AuditLimits
    active_ids: set[int] = field(default_factory=set)
    items_used: int = 0

    def consume(self) -> bool:
        if self.items_used >= self.limits.max_items:
            return False
        self.items_used += 1
        return True


def _metadata_sort_bytes(value: Any, limits: R009AuditLimits) -> bytes:
    """Build a deterministic, cycle-safe key without consulting object repr."""

    active_ids: set[int] = set()

    def key(item: Any, depth: int) -> bytes:
        if item is None or isinstance(item, (bool, int, str)):
            return canonical_bytes({"type": _qualified_type(item), "value": item})
        if isinstance(item, float):
            value = item if math.isfinite(item) else (
                "nan" if math.isnan(item) else ("-inf" if item < 0 else "inf")
            )
            return canonical_bytes({"type": "float", "value": value})
        if depth > limits.max_depth:
            return canonical_bytes({"type": _qualified_type(item), "marker": "depth"})
        item_id = id(item)
        if item_id in active_ids:
            return canonical_bytes({"type": _qualified_type(item), "marker": "cycle"})
        active_ids.add(item_id)
        try:
            mock_metadata = _magicmock_metadata(item, limits)
            if mock_metadata is not None:
                return canonical_bytes(mock_metadata)
            if isinstance(item, (bytes, bytearray, memoryview)):
                raw = bytes(item)
                return canonical_bytes(
                    {"type": "bytes", "value": raw[: max(1, limits.max_bytes // 2)].hex()}
                )
            if isinstance(item, Path):
                return canonical_bytes({"type": "path", "value": item.as_posix()})
            if isinstance(item, enum.Enum):
                return canonical_bytes(
                    {
                        "type": _qualified_type(item),
                        "name": item.name,
                        "value": key(item.value, depth + 1).hex(),
                    }
                )
            if is_dataclass(item) and not isinstance(item, type):
                fields_key = [
                    [field_info.name, key(getattr(item, field_info.name), depth + 1).hex()]
                    for field_info in fields(item)
                ]
                return canonical_bytes(
                    {"type": _qualified_type(item), "fields": fields_key}
                )
            if isinstance(item, Mapping):
                pairs = sorted(
                    (
                        key(raw_key, depth + 1),
                        key(raw_value, depth + 1),
                    )
                    for raw_key, raw_value in item.items()
                )
                return canonical_bytes(
                    {
                        "type": "mapping",
                        "items": [[raw_key.hex(), raw_value.hex()] for raw_key, raw_value in pairs],
                    }
                )
            if isinstance(item, ABCSet):
                values = sorted(key(raw_item, depth + 1) for raw_item in item)
                return canonical_bytes(
                    {"type": "set", "items": [raw_item.hex() for raw_item in values]}
                )
            if isinstance(item, ABCSequence) and not isinstance(item, (str, bytes, bytearray)):
                return canonical_bytes(
                    {
                        "type": _qualified_type(item),
                        "items": [key(raw_item, depth + 1).hex() for raw_item in item],
                    }
                )
            attributes = _object_vars(item)
            if attributes:
                return canonical_bytes(
                    {
                        "type": _qualified_type(item),
                        "attributes": key(attributes, depth + 1).hex(),
                    }
                )
            return canonical_bytes({"type": _qualified_type(item)})
        finally:
            active_ids.discard(item_id)

    return key(value, 0)


def _cycle_marker(value: Any) -> dict[str, str]:
    return {"__cycle__": _qualified_type(value)}


def _items_marker(reason: str) -> dict[str, str]:
    return {"__truncated__": reason}


def _canonicalize_metadata(
    value: Any,
    depth: int,
    state: _AuditCanonicalState,
) -> Any:
    limits = state.limits
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"__float__": "nan" if math.isnan(value) else ("-inf" if value < 0 else "inf")}
    if isinstance(value, str):
        return _bounded_string(value, limits)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        byte_limit = max(1, limits.max_bytes // 2)
        clipped = raw[:byte_limit]
        return {
            "__bytes_hex__": clipped.hex(),
            "__truncated__": len(clipped) != len(raw),
        }
    if depth > limits.max_depth:
        return _items_marker("depth")

    mock_metadata = _magicmock_metadata(value, limits)
    if mock_metadata is not None:
        return mock_metadata
    if isinstance(value, Path):
        return {"__path__": _bounded_string(value.as_posix(), limits)}
    if isinstance(value, enum.Enum):
        return {
            "__enum__": _qualified_type(value),
            "name": _bounded_string(value.name, limits),
            "value": _canonicalize_metadata(value.value, depth + 1, state),
        }

    is_container = (
        isinstance(value, Mapping)
        or isinstance(value, (ABCSequence, ABCSet))
        or (is_dataclass(value) and not isinstance(value, type))
        or bool(_object_vars(value))
    )
    if not is_container:
        return {"__object_type__": _qualified_type(value)}
    value_id = id(value)
    if value_id in state.active_ids:
        return _cycle_marker(value)
    state.active_ids.add(value_id)
    try:
        if is_dataclass(value) and not isinstance(value, type):
            fields_payload: dict[str, Any] = {}
            for item in fields(value):
                if not state.consume():
                    return {
                        "__dataclass__": _qualified_type(value),
                        "fields": fields_payload,
                        "__truncated__": "items",
                    }
                try:
                    field_value = getattr(value, item.name)
                except Exception:
                    field_value = {"__unreadable__": _qualified_type(value)}
                fields_payload[item.name] = _canonicalize_metadata(
                    field_value, depth + 1, state
                )
            return {"__dataclass__": _qualified_type(value), "fields": fields_payload}

        if isinstance(value, Mapping):
            raw_items = list(value.items())
            ordered = sorted(
                raw_items,
                key=lambda item: (
                    _metadata_sort_bytes(item[0], limits),
                    _metadata_sort_bytes(item[1], limits),
                ),
            )
            pairs: list[tuple[Any, Any]] = []
            truncated = False
            for raw_key, raw_item in ordered:
                if not state.consume():
                    truncated = True
                    break
                pairs.append(
                    (
                        _canonicalize_metadata(raw_key, depth + 1, state),
                        _canonicalize_metadata(raw_item, depth + 1, state),
                    )
                )
            if not truncated and all(isinstance(key, str) for key, _ in pairs):
                keys = [key for key, _ in pairs]
                if len(keys) == len(set(keys)):
                    return {key: item for key, item in pairs}
            result: dict[str, Any] = {
                "__mapping__": [
                    {"key": key, "value": item} for key, item in pairs
                ]
            }
            if truncated:
                result["__truncated__"] = "items"
            return result

        if isinstance(value, ABCSet):
            raw_values = sorted(
                value, key=lambda item: _metadata_sort_bytes(item, limits)
            )
        else:
            raw_values = list(value)
        normalized_values: list[Any] = []
        truncated = False
        for raw_item in raw_values:
            if not state.consume():
                truncated = True
                break
            normalized_values.append(
                _canonicalize_metadata(raw_item, depth + 1, state)
            )
        result = normalized_values
        if truncated:
            return {"__sequence__": result, "__truncated__": "items"}
        if isinstance(value, ABCSet):
            return {"__set__": normalized_values}
        return result
    finally:
        state.active_ids.discard(value_id)


def _resolve_audit_limits(
    config: Any = None,
    *,
    max_depth: int | None = None,
    max_items: int | None = None,
    max_string_chars: int | None = None,
    max_bytes: int | None = None,
) -> R009AuditLimits:
    base = R009AuditLimits.from_config(config)
    return R009AuditLimits(
        max_depth=base.max_depth if max_depth is None else max_depth,
        max_items=base.max_items if max_items is None else max_items,
        max_string_chars=(
            base.max_string_chars
            if max_string_chars is None
            else max_string_chars
        ),
        max_bytes=base.max_bytes if max_bytes is None else max_bytes,
    )


def canonicalize_audit_metadata(
    value: Any,
    *,
    config: Any = None,
    max_depth: int | None = None,
    max_items: int | None = None,
    max_string_chars: int | None = None,
    max_bytes: int | None = None,
) -> Any:
    """Return bounded finite JSON metadata without using object ``repr``."""

    limits = _resolve_audit_limits(
        config,
        max_depth=max_depth,
        max_items=max_items,
        max_string_chars=max_string_chars,
        max_bytes=max_bytes,
    )
    state = _AuditCanonicalState(limits=limits)
    normalized = _canonicalize_metadata(value, 0, state)
    try:
        encoded = canonical_bytes(normalized)
    except (R009IdentityError, TypeError, ValueError) as exc:
        raise R009AuditMetadataError("audit metadata is not finite JSON") from exc
    if len(encoded) <= limits.max_bytes:
        return normalized
    marker = {"__truncated__": "bytes", "max_bytes": limits.max_bytes}
    if len(canonical_bytes(marker)) > limits.max_bytes:
        raise R009AuditMetadataError("audit metadata byte limit is too small")
    return marker


def canonical_audit_metadata_bytes(
    value: Any,
    *,
    config: Any = None,
    max_depth: int | None = None,
    max_items: int | None = None,
    max_string_chars: int | None = None,
    max_bytes: int | None = None,
) -> bytes:
    limits = _resolve_audit_limits(
        config,
        max_depth=max_depth,
        max_items=max_items,
        max_string_chars=max_string_chars,
        max_bytes=max_bytes,
    )
    normalized = canonicalize_audit_metadata(
        value,
        config=config,
        max_depth=limits.max_depth,
        max_items=limits.max_items,
        max_string_chars=limits.max_string_chars,
        max_bytes=limits.max_bytes,
    )
    encoded = canonical_bytes(normalized)
    if len(encoded) > limits.max_bytes:
        raise R009AuditMetadataError("canonical audit metadata exceeds byte limit")
    return encoded


_ADDRESS_PATTERN = re.compile(r"0x[0-9a-fA-F]+")


def _safe_detail(value: Any, *, limit: int = 256) -> str:
    if isinstance(value, str):
        text = value
    elif isinstance(value, BaseException):
        try:
            text = f"{_qualified_type(value)}: {str(value)}"
        except Exception:
            text = _qualified_type(value)
    else:
        text = _qualified_type(value)
    text = _ADDRESS_PATTERN.sub("<address>", text)
    return text[:limit]


@dataclass(frozen=True)
class R009AuditEvent:
    """Typed deterministic event suitable for the audit-only append helper."""

    code: str | R009OutcomeCode
    metadata: Any = field(default_factory=dict)
    detail: str = ""
    schema: str = R009_EARLY_ABORT_AUDIT_SCHEMA

    @property
    def code_value(self) -> str:
        value = self.code.value if isinstance(self.code, enum.Enum) else self.code
        if not isinstance(value, str) or not value:
            raise R009AuditMetadataError("audit event code must be non-empty text")
        return value

    def as_dict(self, *, config: Any = None) -> dict[str, Any]:
        if not isinstance(self.schema, str) or not self.schema:
            raise R009AuditMetadataError("audit event schema must be non-empty text")
        metadata = canonicalize_audit_metadata(self.metadata, config=config)
        return {
            "schema": self.schema,
            "record_type": "audit_event",
            "code": self.code_value,
            "detail": _safe_detail(self.detail),
            "metadata": metadata,
        }


def _audit_file_limit(config: Any, name: str, default: int) -> int:
    value = _config_limit(config, name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise R009AuditMetadataError(f"audit file limit {name} is invalid")
    return value


def _strict_audit_json_row(encoded: bytes) -> Any:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise R009AuditMetadataError("audit row repeats a JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                R009AuditMetadataError(
                    f"audit row contains non-finite JSON constant {token}"
                )
            ),
        )
    except (UnicodeError, json.JSONDecodeError, R009AuditMetadataError) as exc:
        raise R009AuditMetadataError("existing audit row is not strict JSON") from exc
    if not isinstance(value, Mapping):
        raise R009AuditMetadataError("existing audit row must be an object")
    return value


def _audit_failure(
    code: R009OutcomeCode,
    detail: Any,
    *,
    event: R009AuditEvent | None = None,
) -> R009EarlyAbortOutcome:
    safe_detail = _safe_detail(detail)
    if event is None:
        event = R009AuditEvent(
            code=code,
            metadata={"detail": safe_detail},
            detail=safe_detail,
        )
    return R009EarlyAbortOutcome(
        code=code,
        accepted=False,
        detail=safe_detail,
        audit_event=event,
    )


def append_audit_event(
    path: str | Path,
    event: R009AuditEvent | R009EarlyAbortOutcome | Mapping[str, Any] | str,
    *,
    config: Any = None,
    metadata: Any = None,
    detail: str = "",
) -> R009EarlyAbortOutcome:
    """Append one audit event without ever raising into a caller/control path."""

    try:
        if isinstance(event, R009AuditEvent):
            audit_event = event
        elif isinstance(event, R009EarlyAbortOutcome):
            audit_event = R009AuditEvent(
                code=event.code,
                metadata=event.as_dict(),
                detail=event.detail,
            )
        elif isinstance(event, Mapping):
            audit_event = R009AuditEvent(
                code=R009OutcomeCode.AUDIT_APPENDED,
                metadata=event,
                detail=detail,
            )
        elif isinstance(event, str):
            audit_event = R009AuditEvent(
                code=event,
                metadata={} if metadata is None else metadata,
                detail=detail,
            )
        else:
            return _audit_failure(
                R009OutcomeCode.AUDIT_METADATA_REJECTED,
                "audit event type is unsupported",
            )
        document = audit_event.as_dict(config=config)
        encoded = canonical_bytes(document) + b"\n"
        max_rows = _audit_file_limit(config, "max_audit_rows", 256)
        max_file_bytes = _audit_file_limit(config, "max_audit_bytes", 64 * 1024)
        audit_path = Path(path)
        if audit_path.is_symlink():
            return _audit_failure(
                R009OutcomeCode.AUDIT_WRITE_FAILED,
                "audit path is a symlink",
                event=audit_event,
            )
        existing = b""
        if audit_path.exists():
            if not audit_path.is_file():
                return _audit_failure(
                    R009OutcomeCode.AUDIT_WRITE_FAILED,
                    "audit path is not a regular file",
                    event=audit_event,
                )
            existing = audit_path.read_bytes()
            if len(existing) > max_file_bytes:
                return _audit_failure(
                    R009OutcomeCode.AUDIT_WRITE_FAILED,
                    "audit file exceeds configured byte limit",
                    event=audit_event,
                )
            rows = existing.splitlines()
            if any(not row.strip() for row in rows):
                return _audit_failure(
                    R009OutcomeCode.AUDIT_WRITE_FAILED,
                    "audit file contains a blank row",
                    event=audit_event,
                )
            for row in rows:
                _strict_audit_json_row(row)
            if len(rows) >= max_rows:
                return _audit_failure(
                    R009OutcomeCode.AUDIT_WRITE_FAILED,
                    "audit file exceeds configured row limit",
                    event=audit_event,
                )
            if existing and not existing.endswith(b"\n"):
                return _audit_failure(
                    R009OutcomeCode.AUDIT_WRITE_FAILED,
                    "audit file does not end with a newline",
                    event=audit_event,
                )
        if len(existing) + len(encoded) > max_file_bytes:
            return _audit_failure(
                R009OutcomeCode.AUDIT_WRITE_FAILED,
                "audit append exceeds configured byte limit",
                event=audit_event,
            )
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError as exc:
                return _audit_failure(
                    R009OutcomeCode.AUDIT_WRITE_FAILED,
                    f"audit fsync failed: {_safe_detail(exc)}",
                    event=audit_event,
                )
        return R009EarlyAbortOutcome(
            code=R009OutcomeCode.AUDIT_APPENDED,
            accepted=True,
            detail="audit event appended",
            audit_event=audit_event,
        )
    except Exception as exc:
        # This is intentionally the outermost boundary: audit failure is
        # observable but never becomes a resume/control exception.
        return _audit_failure(
            R009OutcomeCode.AUDIT_WRITE_FAILED,
            _safe_detail(exc),
        )


def build_shadow_sidecar_row(
    record: R009EarlyAbortRecord | Mapping[str, Any],
    *,
    config: Any = None,
) -> dict[str, Any]:
    """Build one exact R009 Channel-A shadow-only JSON row in memory."""

    sidecar_config = _coerce_early_abort_config(config)
    normalized = (
        record
        if isinstance(record, R009EarlyAbortRecord)
        else R009EarlyAbortRecord.from_mapping(record)
    )
    return {
        "schema": sidecar_config.sidecar_schema,
        "version": sidecar_config.version,
        "record_type": "shadow_early_abort",
        "channel": "A",
        "shadow_only": True,
        "enters_gp_training": False,
        "enters_raw_ledger": False,
        "release_identity": normalized.release_identity.as_dict(),
        "release_identity_sha256": normalized.release_identity_sha256,
        "execution_identity": normalized.execution_identity.as_dict(),
        "execution_identity_sha256": normalized.execution_identity_sha256,
        "dispatch_identity": normalized.dispatch_identity.as_dict(),
        "dispatch_identity_sha256": normalized.dispatch_identity_sha256,
        "payload": _thaw_json_tree(normalized.payload),
        "payload_sha256": normalized.payload_sha256,
    }


def _strict_sidecar_json_row(encoded: bytes) -> Mapping[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise R009SidecarRowError(
                    R009OutcomeCode.SIDECAR_MALFORMED_ROW,
                    "sidecar row repeats a JSON key",
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                R009SidecarRowError(
                    R009OutcomeCode.SIDECAR_MALFORMED_ROW,
                    f"sidecar row contains non-finite JSON constant {token}",
                )
            ),
        )
    except R009SidecarRowError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_MALFORMED_ROW,
            "sidecar row is not strict JSON",
        ) from exc
    if not isinstance(value, Mapping):
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_MALFORMED_ROW,
            "sidecar row must be a JSON object",
        )
    return value


def _record_from_shadow_sidecar_row(
    row: Mapping[str, Any],
    *,
    config: R009EarlyAbortConfig,
    expected_release_identity: R009ReleaseIdentity | None = None,
) -> R009EarlyAbortRecord:
    if set(row) != _SIDECAR_ROW_FIELDS:
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_SCHEMA_MISMATCH,
            "sidecar row fields differ",
        )
    if row["schema"] != config.sidecar_schema or row["version"] != config.version:
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_SCHEMA_MISMATCH,
            "sidecar row schema or version differs",
        )
    if row["record_type"] != "shadow_early_abort":
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_SCHEMA_MISMATCH,
            "sidecar row record type differs",
        )
    if (
        row["channel"] != "A"
        or row["shadow_only"] is not True
        or row["enters_gp_training"] is not False
        or row["enters_raw_ledger"] is not False
    ):
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_POLICY_MISMATCH,
            "sidecar row is not Channel-A shadow-only policy",
        )
    try:
        record = R009EarlyAbortRecord.from_mapping(
            {
                "release_identity": row["release_identity"],
                "release_identity_sha256": row["release_identity_sha256"],
                "execution_identity": row["execution_identity"],
                "execution_identity_sha256": row["execution_identity_sha256"],
                "dispatch_identity": row["dispatch_identity"],
                "dispatch_identity_sha256": row["dispatch_identity_sha256"],
                "payload": row["payload"],
                "payload_sha256": row["payload_sha256"],
            }
        )
    except R009ReleaseIdentityBindingError as exc:
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_RELEASE_IDENTITY_MISMATCH,
            str(exc),
        ) from exc
    except R009ExecutionIdentityBindingError as exc:
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_EXECUTION_IDENTITY_MISMATCH,
            str(exc),
        ) from exc
    except R009DispatchIdentityBindingError as exc:
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_DISPATCH_IDENTITY_MISMATCH,
            str(exc),
        ) from exc
    except R009PayloadBindingError as exc:
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_PAYLOAD_IDENTITY_MISMATCH,
            str(exc),
        ) from exc
    except R009BindingError as exc:
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_MALFORMED_ROW,
            str(exc),
        ) from exc

    if (
        expected_release_identity is not None
        and record.release_identity != expected_release_identity
    ):
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_RELEASE_IDENTITY_MISMATCH,
            "sidecar row release identity differs from expected release",
        )
    if canonical_bytes(row["execution_identity"]) != canonical_bytes(
        record.execution_identity.as_dict()
    ):
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_EXECUTION_IDENTITY_MISMATCH,
            "sidecar execution identity payload differs from its typed identity",
        )
    if canonical_bytes(row["dispatch_identity"]) != canonical_bytes(
        record.dispatch_identity.as_dict()
    ):
        raise R009SidecarRowError(
            R009OutcomeCode.SIDECAR_DISPATCH_IDENTITY_MISMATCH,
            "sidecar dispatch identity payload differs from its typed identity",
        )
    return record


def _same_dispatch_key(
    left: R009EarlyAbortRecord, right: R009EarlyAbortRecord
) -> bool:
    return left.dispatch_identity.dispatch_key == right.dispatch_identity.dispatch_key


def _all_record_digests_equal(
    left: R009EarlyAbortRecord, right: R009EarlyAbortRecord
) -> bool:
    return (
        left.release_identity_sha256 == right.release_identity_sha256
        and left.execution_identity_sha256 == right.execution_identity_sha256
        and left.dispatch_identity_sha256 == right.dispatch_identity_sha256
        and left.payload_sha256 == right.payload_sha256
    )


@dataclass(frozen=True)
class R009SidecarLoadResult:
    """Strict sidecar scan result; every scanned row has an outcome."""

    records: tuple[R009EarlyAbortRecord, ...]
    outcomes: tuple[R009EarlyAbortOutcome, ...]
    safe_to_append: bool

    @property
    def rows(self) -> tuple[R009EarlyAbortRecord, ...]:
        return self.records

    @property
    def audit_events(self) -> tuple[R009AuditEvent, ...]:
        return tuple(
            outcome.audit_event
            for outcome in self.outcomes
            if outcome.audit_event is not None
        )


class R009ShadowSidecarStore:
    """Strict JSONL row admission with no control, GP, or raw-ledger path."""

    def __init__(
        self,
        path: str | Path,
        release_identity: R009ReleaseIdentity | Mapping[str, Any],
        *,
        config: Any = None,
        release_identity_sha256: str | None = None,
    ) -> None:
        self.config = _coerce_early_abort_config(config)
        try:
            self.path = Path(path)
        except (TypeError, ValueError) as exc:
            raise R009SidecarConfigError("sidecar path is invalid") from exc
        if self.path.name != self.config.sidecar_filename:
            raise R009SidecarConfigError(
                "sidecar filename differs from identity-bound config"
            )
        try:
            identity = (
                release_identity
                if isinstance(release_identity, R009ReleaseIdentity)
                else validate_release_identity(release_identity)
            )
        except (R009IdentityError, TypeError, ValueError) as exc:
            raise R009SidecarConfigError("sidecar release identity is invalid") from exc
        if release_identity_sha256 is not None:
            try:
                supplied = require_digest(
                    release_identity_sha256, "sidecar release identity sha256"
                )
            except (R009IdentityError, TypeError, ValueError) as exc:
                raise R009SidecarConfigError(
                    "sidecar release identity sha256 is invalid"
                ) from exc
            if supplied != identity.release_identity_sha256:
                raise R009SidecarConfigError(
                    "sidecar release identity sha256 differs"
                )
        self.release_identity = identity
        self.release_identity_sha256 = identity.release_identity_sha256
        self._last_load = R009SidecarLoadResult((), (), True)

    @property
    def last_load(self) -> R009SidecarLoadResult:
        return self._last_load

    def _row_outcome(
        self,
        code: R009OutcomeCode,
        detail: str,
        *,
        line_number: int | None = None,
        accepted: bool = False,
        record: R009EarlyAbortRecord | None = None,
    ) -> R009EarlyAbortOutcome:
        metadata = {} if line_number is None else {"line_number": line_number}
        return _sidecar_event(
            code,
            detail,
            config=self.config,
            metadata=metadata,
            accepted=accepted,
            record=record,
        )

    def load(self) -> R009SidecarLoadResult:
        outcomes: list[R009EarlyAbortOutcome] = []
        records: list[R009EarlyAbortRecord] = []
        safe = True
        if not self.path.exists():
            result = R009SidecarLoadResult((), (), True)
            self._last_load = result
            return result
        if self.path.is_symlink() or not self.path.is_file():
            result = R009SidecarLoadResult(
                (),
                (
                    self._row_outcome(
                        R009OutcomeCode.SIDECAR_READ_FAILED,
                        "sidecar path is not a regular non-symlink file",
                    ),
                ),
                False,
            )
            self._last_load = result
            return result
        try:
            encoded = self.path.read_bytes()
        except Exception as exc:
            result = R009SidecarLoadResult(
                (),
                (
                    self._row_outcome(
                        R009OutcomeCode.SIDECAR_READ_FAILED,
                        _safe_detail(exc),
                    ),
                ),
                False,
            )
            self._last_load = result
            return result
        if len(encoded) > self.config.max_sidecar_bytes:
            result = R009SidecarLoadResult(
                (),
                (
                    self._row_outcome(
                        R009OutcomeCode.SIDECAR_READ_FAILED,
                        "sidecar exceeds configured max_sidecar_bytes",
                    ),
                ),
                False,
            )
            self._last_load = result
            return result
        if not encoded:
            result = R009SidecarLoadResult((), (), True)
            self._last_load = result
            return result
        if not encoded.endswith(b"\n"):
            result = R009SidecarLoadResult(
                (),
                (
                    self._row_outcome(
                        R009OutcomeCode.SIDECAR_MALFORMED_ROW,
                        "sidecar file is missing final newline",
                    ),
                ),
                False,
            )
            self._last_load = result
            return result
        raw_lines = encoded.split(b"\n")
        if raw_lines and raw_lines[-1] == b"":
            raw_lines.pop()
        for line_number, raw_line in enumerate(raw_lines, start=1):
            if not raw_line.strip():
                outcomes.append(
                    self._row_outcome(
                        R009OutcomeCode.SIDECAR_MALFORMED_ROW,
                        "sidecar row is blank",
                        line_number=line_number,
                    )
                )
                safe = False
                continue
            try:
                row = _strict_sidecar_json_row(raw_line)
                record = _record_from_shadow_sidecar_row(
                    row,
                    config=self.config,
                )
            except R009SidecarRowError as exc:
                outcomes.append(
                    self._row_outcome(
                        exc.code,
                        str(exc),
                        line_number=line_number,
                    )
                )
                safe = False
                continue
            prior = next(
                (item for item in records if _same_dispatch_key(item, record)),
                None,
            )
            if prior is not None:
                if _all_record_digests_equal(prior, record):
                    outcomes.append(
                        self._row_outcome(
                            R009OutcomeCode.SIDECAR_IDEMPOTENT_DUPLICATE,
                            "exact sidecar dispatch/payload/identity duplicate",
                            line_number=line_number,
                            accepted=True,
                            record=prior,
                        )
                    )
                else:
                    outcomes.append(
                        self._row_outcome(
                            R009OutcomeCode.SIDECAR_DISPATCH_CONFLICT,
                            "same dispatch key has changed payload or identity",
                            line_number=line_number,
                            record=prior,
                        )
                    )
                    safe = False
                continue
            if (
                record.release_identity != self.release_identity
                or record.release_identity_sha256 != self.release_identity_sha256
            ):
                outcomes.append(
                    self._row_outcome(
                        R009OutcomeCode.SIDECAR_RELEASE_IDENTITY_MISMATCH,
                        "sidecar row release identity or digest differs from expected release",
                        line_number=line_number,
                    )
                )
                safe = False
                continue
            records.append(record)
            outcomes.append(
                self._row_outcome(
                    R009OutcomeCode.SIDECAR_ADMITTED,
                    "sidecar row admitted",
                    line_number=line_number,
                    accepted=True,
                    record=record,
                )
            )
        result = R009SidecarLoadResult(tuple(records), tuple(outcomes), safe)
        self._last_load = result
        return result

    def append(
        self, record: R009EarlyAbortRecord | Mapping[str, Any]
    ) -> R009EarlyAbortOutcome:
        try:
            normalized = (
                record
                if isinstance(record, R009EarlyAbortRecord)
                else R009EarlyAbortRecord.from_mapping(record)
            )
        except R009ReleaseIdentityBindingError as exc:
            return self._row_outcome(
                R009OutcomeCode.SIDECAR_RELEASE_IDENTITY_MISMATCH,
                str(exc),
            )
        except R009ExecutionIdentityBindingError as exc:
            return self._row_outcome(
                R009OutcomeCode.SIDECAR_EXECUTION_IDENTITY_MISMATCH,
                str(exc),
            )
        except R009DispatchIdentityBindingError as exc:
            return self._row_outcome(
                R009OutcomeCode.SIDECAR_DISPATCH_IDENTITY_MISMATCH,
                str(exc),
            )
        except R009PayloadBindingError as exc:
            return self._row_outcome(
                R009OutcomeCode.SIDECAR_PAYLOAD_IDENTITY_MISMATCH,
                str(exc),
            )
        except R009BindingError as exc:
            return self._row_outcome(
                R009OutcomeCode.SIDECAR_MALFORMED_ROW,
                str(exc),
            )

        loaded = self.load()
        if not loaded.safe_to_append:
            failure = next(
                (item for item in loaded.outcomes if not item.accepted),
                self._row_outcome(
                    R009OutcomeCode.SIDECAR_READ_FAILED,
                    "sidecar is not safe for append",
                ),
            )
            return failure
        prior = next(
            (item for item in loaded.records if _same_dispatch_key(item, normalized)),
            None,
        )
        if prior is not None:
            if _all_record_digests_equal(prior, normalized):
                return self._row_outcome(
                    R009OutcomeCode.SIDECAR_IDEMPOTENT_DUPLICATE,
                    "exact sidecar dispatch/payload/identity duplicate",
                    accepted=True,
                    record=prior,
                )
            return self._row_outcome(
                R009OutcomeCode.SIDECAR_DISPATCH_CONFLICT,
                "same dispatch key has changed payload or identity",
                record=prior,
            )
        if (
            normalized.release_identity != self.release_identity
            or normalized.release_identity_sha256 != self.release_identity_sha256
        ):
            return self._row_outcome(
                R009OutcomeCode.SIDECAR_RELEASE_IDENTITY_MISMATCH,
                "record release identity differs from expected release",
            )
        try:
            row = build_shadow_sidecar_row(normalized, config=self.config)
            row_bytes = canonical_bytes(row) + b"\n"
            if self.path.is_symlink():
                raise OSError("sidecar path is a symlink")
            existing = self.path.read_bytes() if self.path.exists() else b""
            if self.path.exists() and not self.path.is_file():
                raise OSError("sidecar path is not a regular file")
            if existing and not existing.endswith(b"\n"):
                raise OSError("sidecar file does not end with a newline")
            if len(existing) + len(row_bytes) > self.config.max_sidecar_bytes:
                raise OSError("sidecar append exceeds max_sidecar_bytes")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as handle:
                handle.write(row_bytes)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception as exc:
            return self._row_outcome(
                R009OutcomeCode.SIDECAR_WRITE_FAILED,
                _safe_detail(exc),
                record=normalized,
            )
        return self._row_outcome(
            R009OutcomeCode.SIDECAR_ADMITTED,
            "new sidecar row appended",
            accepted=True,
            record=normalized,
        )


R009EarlyAbortSidecar = R009ShadowSidecarStore
R009SidecarStore = R009ShadowSidecarStore


def resolve_early_abort_mode(
    environ: Mapping[str, str] | None = None,
    *,
    mode_env: str = "R009_EARLY_ABORT_MODE",
) -> str:
    """Resolve only ``off`` or ``shadow``; reject active mode fail-closed."""

    raw = None if environ is None else environ.get(mode_env)
    mode = "shadow" if raw is None or not raw.strip() else raw.strip().lower()
    if mode in {"shadow", "shadow-only", "shadow_only"}:
        return "shadow"
    if mode in {"off", "disabled", "disable"}:
        return "off"
    if mode in {"active", "hard-stop", "hard_stop", "abort"}:
        raise R009ActiveModeRejected(
            "R009 active early-abort mode is rejected; only offline shadow mode is allowed"
        )
    raise R009ModeError("unknown R009 early-abort mode")


__all__ = [
    "R009ActiveModeRejected",
    "R009AuditEvent",
    "R009AuditLimits",
    "R009AuditMetadataError",
    "R009BindingError",
    "R009CausalFormalMetric",
    "R009CausalFormalObjective",
    "R009DispatchConflict",
    "R009DispatchIdentity",
    "R009DispatchIdentityBindingError",
    "R009EarlyAbortConfig",
    "R009EarlyAbortError",
    "R009EarlyAbortRecord",
    "R009EarlyAbortOutcome",
    "R009EarlyAbortSidecar",
    "R009ExecutionIdentity",
    "R009ExecutionIdentityBindingError",
    "R009MetricError",
    "R009ModeError",
    "R009OutcomeCode",
    "R009PayloadBindingError",
    "R009PartialMetricSnapshot",
    "R009ReleaseIdentityBindingError",
    "R009SidecarConfigError",
    "R009SidecarError",
    "R009SidecarLoadResult",
    "R009SidecarRowError",
    "R009SidecarStore",
    "R009ShadowSidecarStore",
    "R009WatermarkRegression",
    "R009InMemorySidecar",
    "append_audit_event",
    "build_shadow_sidecar_row",
    "canonical_audit_metadata_bytes",
    "canonical_payload_digest",
    "canonicalize_audit_metadata",
    "resolve_early_abort_mode",
]
