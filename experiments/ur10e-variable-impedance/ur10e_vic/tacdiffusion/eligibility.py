"""The sole final TacDiffusion training-eligibility verdict writer."""

from __future__ import annotations

from dataclasses import dataclass, asdict, field
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    DYNAMICS_AUTHORITATIVE_TORQUE_SOURCE,
    FORMAL_EPISODE_MANIFEST_SCHEMA_V1,
    FORMAL_MODEL_RATE_CANDIDATES_HZ,
    FORMAL_OBSERVATION_DIMENSION,
    KUNWEI_ONLY_FORCE_SOURCE_ID,
    FormalEpisodeManifestV1,
    validate_formal_force_source_payload,
)
from .episode_composition import ActiveTrainingWindow, EpisodeSemanticContext
from .episode_recorder import (
    EPISODE_FRAME_SCHEMA_V4,
    compute_v3_row_sha256,
)


ELIGIBILITY_SCHEMA = "ur10e_tacdiffusion_training_eligibility/v2"
_MISSING = object()
HOST_BATCH_WATCHDOG_S = 0.080
MAX_UNSEALED_TAIL = 9
ACTION_DIMENSION = 12
FORMAL_ELIGIBILITY_SCHEMA = "ur10e_tacdiffusion_formal_training_eligibility/v1"


def _value(row: object, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _validity_flag(row: object, name: str, default: bool) -> bool:
    flags = _value(row, "recorder_validity_flags", {})
    if isinstance(flags, Mapping) and name in flags:
        return bool(flags[name])
    return bool(_value(row, name, default))


def _vector(row: object, name: str) -> tuple[float, ...] | None:
    values = _value(row, name)
    if values is None:
        return None
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError):
        return None
    if not result or not all(math.isfinite(value) for value in result):
        return None
    return result


def _is_v3_row(row: object) -> bool:
    schema = _value(row, "schema", "")
    return bool(schema == "ur10e_tacdiffusion_episode_frame/v3" or hasattr(row, "typed_receipts_valid"))


def _nested_value(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _v3_dynamics_receipt_valid(row: object) -> bool:
    receipt = _value(row, "dynamics_receipt")
    return bool(
        receipt is not None
        and _nested_value(receipt, "valid", False) is True
        and _nested_value(receipt, "authoritative_torque_source", "")
        == "previous_commanded_no_gravity_torque"
        and _nested_value(receipt, "schema_version", "")
        == "ur10e_tacdiffusion_dynamics_receipt/v1"
    )


def _v3_action_semantics_valid(
    row: object,
    semantic_context: EpisodeSemanticContext | None,
) -> bool:
    label = _value(row, "action_label")
    context = _value(row, "action_label_context")
    if label is None or context is None or semantic_context is None:
        return False
    return bool(
        _nested_value(label, "available", False) is True
        and _nested_value(label, "shadow_only", True) is False
        and _nested_value(label, "source", "") == "deterministic_expert"
        and _nested_value(label, "semantics", "") == semantic_context.action_label_semantics
        and _nested_value(label, "policy_id", "") == semantic_context.expert_policy_id
        and _nested_value(context, "semantic_context_fingerprint_sha256", None)
        == semantic_context.fingerprint_sha256
    )


@dataclass(frozen=True)
class EligibilityDecision:
    episode_id: str
    training_eligible: bool
    first_live_shadow: bool
    predicates: Mapping[str, bool]
    reasons: tuple[str, ...]
    live_ready: bool = False
    capture_integrity: bool = False
    data_quality: bool = False
    active_window: Mapping[str, Any] = field(default_factory=dict)
    semantic_context: Mapping[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": ELIGIBILITY_SCHEMA,
            "episode_id": self.episode_id,
            "training_eligible": self.training_eligible,
            "first_live_shadow": self.first_live_shadow,
            "predicates": dict(self.predicates),
            "reasons": list(self.reasons),
            "live_ready": self.live_ready,
            "capture_integrity": self.capture_integrity,
            "data_quality": self.data_quality,
            "active_window": dict(self.active_window),
            "semantic_context": dict(self.semantic_context),
        }


def _health_value(health: object, name: str, default: Any = None) -> Any:
    if isinstance(health, Mapping):
        return health.get(name, default)
    return getattr(health, name, default)


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class EligibilityValidator:
    """Compute and atomically publish the only final eligibility receipt.

    A frame may be retained with invalid/torn flags.  Such a row is evidence,
    not a reason to discard the row or to let another writer manufacture a
    training view.
    """

    def __init__(self, *, receipt_schema: str = ELIGIBILITY_SCHEMA) -> None:
        if receipt_schema != ELIGIBILITY_SCHEMA:
            raise ValueError("eligibility schema is frozen")
        self.receipt_schema = receipt_schema
        self._write_lock = threading.Lock()

    def evaluate(
        self,
        frames: Iterable[object],
        *,
        recorder_health: object,
        first_live_shadow: bool = True,
        episode_id: str | None = None,
        live_acceptance_evidence: bool = False,
        active_window: ActiveTrainingWindow | None = None,
        semantic_context: EpisodeSemanticContext | None = None,
    ) -> EligibilityDecision:
        rows = tuple(frames)
        resolved_episode_id = episode_id or str(
            _value(rows[0], "episode_id", "unknown") if rows else "unknown"
        )
        explicit_window = active_window is not None
        if active_window is None:
            # Preserve the accepted compatibility reader contract for callers
            # that predate the explicit active-window declaration.  New live
            # composition always passes a window, including an unresolved
            # empty declaration that fails closed.
            candidate_rows = rows
            active_window_payload: dict[str, Any] = {
                "declared": False,
                "compatibility_implicit": True,
            }
        else:
            candidate_rows = tuple(active_window.select(rows))
            active_window_payload = active_window.as_json()
        v3_rows = tuple(row for row in candidate_rows if _is_v3_row(row))
        has_v3_rows = bool(v3_rows)
        dynamics_receipts_valid = bool(
            not has_v3_rows or all(_v3_dynamics_receipt_valid(row) for row in v3_rows)
        )
        identity_enabled = bool(
            not has_v3_rows
            or all(bool(_value(row, "identity_enabled", False)) for row in v3_rows)
        )
        semantic_consistent = bool(
            not has_v3_rows
            or (
                semantic_context is not None
                and all(
                    _value(row, "semantic_context_fingerprint_sha256")
                    == semantic_context.fingerprint_sha256
                    for row in v3_rows
                )
            )
        )
        typed_action_labels_valid = bool(
            not has_v3_rows
            or all(_v3_action_semantics_valid(row, semantic_context) for row in v3_rows)
        )
        reasons: list[str] = []
        control_time_strict = bool(candidate_rows)
        previous_control = -math.inf
        previous_sample_index = -1
        previous_control_sequence = -1
        for row in candidate_rows:
            value = _value(row, "control_time_s")
            try:
                control = float(value)
                sample_index = int(_value(row, "sample_index"))
                control_sequence = int(_value(row, "control_sequence"))
            except (TypeError, ValueError):
                control_time_strict = False
                continue
            if (
                not math.isfinite(control)
                or control <= previous_control
                or sample_index <= previous_sample_index
                or control_sequence <= previous_control_sequence
                or not _validity_flag(row, "control_time_strict", True)
            ):
                control_time_strict = False
            previous_control = control
            previous_sample_index = sample_index
            previous_control_sequence = control_sequence
        exact_expert_applied = bool(candidate_rows) and all(
            (_vector(row, "expert_action_12d") is not None)
            and (_vector(row, "expert_action_12d") == _vector(row, "applied_action_12d"))
            for row in candidate_rows
        )
        coherent_echoes = bool(candidate_rows)
        for row in candidate_rows:
            echoed = _vector(row, "echoed_action_12d")
            if not (
                _validity_flag(row, "action_echo_coherent", False)
                and _validity_flag(row, "echoed_action_valid", False)
                and echoed is not None
                and len(echoed) == ACTION_DIMENSION
            ):
                coherent_echoes = False
        causal_sensor_lineage = bool(candidate_rows)
        previous_device = -math.inf
        previous_host = -math.inf
        previous_sample = -1
        previous_batch: int | None = None
        previous_batch_host: float | None = None
        for row in candidate_rows:
            control_time = _value(row, "control_time_s")
            device_time = _value(row, "external_device_time_s")
            host_time = _value(row, "external_host_visible_time_s")
            sample_index = _value(row, "external_sample_index")
            if not _validity_flag(row, "external_lineage_valid", False):
                causal_sensor_lineage = False
                continue
            if control_time is None or host_time is None or device_time is None:
                causal_sensor_lineage = False
                continue
            try:
                control = float(control_time)
                device = float(device_time)
                host = float(host_time)
                sample = int(sample_index)
            except (TypeError, ValueError):
                causal_sensor_lineage = False
                continue
            if (
                not all(math.isfinite(value) for value in (control, device, host))
                or host > control + 1e-12
                or control - host > HOST_BATCH_WATCHDOG_S + 1e-12
                or device < previous_device - 1e-12
                or host < previous_host - 1e-12
                or sample < previous_sample
                or (
                    previous_batch is not None
                    and _int_value(_value(row, "external_batch_id", -1), -1) == previous_batch
                    and (
                        previous_batch_host is None
                        or not math.isclose(
                            host,
                            previous_batch_host,
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        )
                    )
                )
                or (
                    bool(_value(row, "external_hold", False))
                    and _int_value(_value(row, "external_held_ticks", 0) or 0, 0) <= 0
                )
            ):
                causal_sensor_lineage = False
            previous_device, previous_host, previous_sample = device, host, sample
            previous_batch = _int_value(_value(row, "external_batch_id", -1), -1)
            previous_batch_host = host
        internal_wrench_valid = bool(candidate_rows) and all(
            _validity_flag(row, "internal_wrench_valid", False) for row in candidate_rows
        ) and dynamics_receipts_valid
        retained_rows_valid = bool(rows) and all(
            _validity_flag(row, "row_valid", False) for row in rows
        )
        candidate_window_rows_valid = bool(candidate_rows) and all(
            _validity_flag(row, "row_valid", False) for row in candidate_rows
        )
        no_overflow = (
            _health_value(recorder_health, "overflowed", _MISSING) is False
        )
        no_stall = _health_value(recorder_health, "stalled", _MISSING) is False
        dropped_rows = _health_value(
            recorder_health,
            "dropped_rows",
            _MISSING,
        )
        rejected_rows = _health_value(
            recorder_health,
            "rejected_rows",
            _MISSING,
        )
        no_drop = (
            dropped_rows is not _MISSING
            and rejected_rows is not _MISSING
            and _int_value(dropped_rows, -1) == 0
            and _int_value(rejected_rows, -1) == 0
        )
        enqueued_rows = _health_value(
            recorder_health,
            "enqueued_rows",
            _MISSING,
        )
        durable_rows = _health_value(
            recorder_health,
            "durable_rows",
            _MISSING,
        )
        queue_depth = _health_value(
            recorder_health,
            "queue_depth",
            _MISSING,
        )
        unsealed_tail = _health_value(
            recorder_health,
            "unsealed_tail",
            _MISSING,
        )
        max_unsealed_tail = _health_value(
            recorder_health,
            "max_unsealed_tail",
            _MISSING,
        )
        complete_seal = bool(
            _health_value(recorder_health, "sealed", _MISSING) is True
            and _health_value(recorder_health, "manifest_written", _MISSING)
            is True
            and _health_value(recorder_health, "durability_mode", "") == "batch_fsync_10"
            and enqueued_rows is not _MISSING
            and durable_rows is not _MISSING
            and _int_value(enqueued_rows, -1) >= 0
            and _int_value(durable_rows, -2) == _int_value(enqueued_rows, -1)
            and queue_depth is not _MISSING
            and _int_value(queue_depth, -1) == 0
            and unsealed_tail is not _MISSING
            and _int_value(unsealed_tail, -1) == 0
            and max_unsealed_tail is not _MISSING
            and 0 <= _int_value(max_unsealed_tail, -1) <= MAX_UNSEALED_TAIL
        )
        tamper_free = (
            _health_value(recorder_health, "tamper_free", _MISSING) is True
        )
        fault_value = _health_value(recorder_health, "fault", _MISSING)
        writer_error = _health_value(
            recorder_health,
            "writer_error",
            _MISSING,
        )
        no_writer_fault = (
            fault_value is None
            and writer_error is None
        )
        if semantic_context is None:
            semantic_bindings_complete = not has_v3_rows
            expert_policy_authoritative = not has_v3_rows
            expert_label_available = bool(candidate_rows) and all(
                bool(_value(row, "expert_label_available", True))
                for row in candidate_rows
            ) and typed_action_labels_valid
            reference_derivatives_valid = bool(candidate_rows) and all(
                bool(_value(row, "reference_derivatives_valid", True))
                for row in candidate_rows
            )
            semantic_payload: dict[str, Any] = {
                "compatibility_implicit": True,
            }
        else:
            semantic_bindings_complete = bool(semantic_context.bindings_complete)
            expert_policy_authoritative = bool(
                semantic_context.expert_policy_id == "deterministic_expert_v1"
                and semantic_context.action_label_semantics
                == "deterministic_expert_guarded_action_12d_v1"
            )
            expert_label_available = bool(
                semantic_context.expert_label_available
                and candidate_rows
                and all(bool(_value(row, "expert_label_available", False)) for row in candidate_rows)
                and all(
                    str(_value(row, "expert_action_source", "")) == "deterministic_expert"
                    for row in candidate_rows
                )
                and typed_action_labels_valid
            )
            reference_derivatives_valid = bool(candidate_rows) and all(
                bool(_value(row, "reference_derivatives_valid", False))
                for row in candidate_rows
            )
            semantic_payload = semantic_context.as_metadata()
        active_window_declared = (
            True
            if not explicit_window
            else bool(active_window and active_window.declared and active_window.contiguous)
        )
        active_window_non_empty = bool(candidate_rows)
        candidate_history_primed = bool(candidate_rows) and all(
            bool(_value(row, "observation_history_valid", True))
            for row in candidate_rows
        )
        predicates = {
            "non_empty": bool(candidate_rows),
            "strict_control_time": control_time_strict,
            "exact_expert_equals_applied": exact_expert_applied,
            "coherent_controller_echoes": coherent_echoes,
            "causal_valid_sensor_lineage": causal_sensor_lineage,
            "internal_wrench_valid": internal_wrench_valid,
            "dynamics_receipts_valid": dynamics_receipts_valid,
            "identity_enabled": identity_enabled,
            "semantic_consistent": semantic_consistent,
            "typed_action_labels_valid": typed_action_labels_valid,
            "retained_rows_valid": retained_rows_valid,
            "candidate_window_rows_valid": candidate_window_rows_valid,
            "active_window_declared": active_window_declared,
            "active_window_non_empty": active_window_non_empty,
            "candidate_history_primed": candidate_history_primed,
            "semantic_bindings_complete": semantic_bindings_complete,
            "expert_label_available": expert_label_available,
            "expert_policy_authoritative": expert_policy_authoritative,
            "reference_derivatives_valid": reference_derivatives_valid,
            "no_spool_overflow": no_overflow,
            "no_recorder_stall": no_stall,
            "no_drop_or_rejection": no_drop,
            "complete_seal": complete_seal,
            "tamper_free": tamper_free,
            "no_writer_fault": no_writer_fault,
            "first_live_shadow_clear": not first_live_shadow,
        }
        for name, passed in predicates.items():
            if not passed:
                reasons.append(name)
        if first_live_shadow:
            reasons.append("first_live_shadow_forced_ineligible")
        required_predicates = dict(predicates)
        if explicit_window:
            # Retained startup, warmup, terminal, and torn rows are capture
            # evidence.  Only the declared candidate window is part of the
            # training verdict.
            required_predicates.pop("retained_rows_valid", None)
        eligible = all(required_predicates.values())
        capture_integrity = bool(
            bool(rows)
            and no_overflow
            and no_stall
            and no_drop
            and complete_seal
            and tamper_free
            and no_writer_fault
        )
        data_quality = bool(
            candidate_window_rows_valid
            and control_time_strict
            and causal_sensor_lineage
            and internal_wrench_valid
            and dynamics_receipts_valid
            and identity_enabled
            and semantic_consistent
            and typed_action_labels_valid
            and coherent_echoes
            and candidate_history_primed
            and expert_label_available
            and reference_derivatives_valid
        )
        return EligibilityDecision(
            episode_id=resolved_episode_id,
            training_eligible=eligible,
            first_live_shadow=first_live_shadow,
            predicates=predicates,
            reasons=tuple(dict.fromkeys(reasons)),
            # Live readiness has independent route/controller/safety evidence;
            # this offline validator never creates that evidence.
            live_ready=bool(eligible and live_acceptance_evidence),
            capture_integrity=capture_integrity,
            data_quality=data_quality,
            active_window=active_window_payload,
            semantic_context=semantic_payload,
        )

    def write_receipt(
        self,
        path: str | Path,
        decision: EligibilityDecision,
        *,
        recorder_health: object | None = None,
    ) -> dict[str, Any]:
        if not isinstance(decision, EligibilityDecision):
            raise TypeError("EligibilityValidator is the sole receipt writer")
        payload = decision.as_json()
        if recorder_health is not None:
            health_json = (
                recorder_health.as_json()
                if hasattr(recorder_health, "as_json")
                else dict(recorder_health)
            )
            payload["recorder_health"] = health_json
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        payload["decision_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        destination = Path(path)
        with self._write_lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise FileExistsError(f"eligibility receipt already exists: {destination}")
            temporary = destination.with_name(
                f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                with temporary.open("x", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True, indent=2, allow_nan=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                directory_fd = os.open(destination.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)
        return payload

    def evaluate_and_write(
        self,
        path: str | Path,
        frames: Iterable[object],
        *,
        recorder_health: object,
        first_live_shadow: bool = True,
        episode_id: str | None = None,
        active_window: ActiveTrainingWindow | None = None,
        semantic_context: EpisodeSemanticContext | None = None,
    ) -> tuple[EligibilityDecision, dict[str, Any]]:
        decision = self.evaluate(
            frames,
            recorder_health=recorder_health,
            first_live_shadow=first_live_shadow,
            episode_id=episode_id,
            active_window=active_window,
            semantic_context=semantic_context,
        )
        return decision, self.write_receipt(path, decision, recorder_health=recorder_health)

    def evaluate_formal(
        self,
        frames: Iterable[object],
        *,
        recorder_health: object,
        formal_manifest: object | None = None,
        episode_id: str | None = None,
        first_live_shadow: bool = True,
    ) -> "FormalEligibilityDecision":
        """Route V4 verdicts to the sole formal validator."""

        return FormalEligibilityValidator().evaluate(
            frames,
            recorder_health=recorder_health,
            formal_manifest=formal_manifest,
            episode_id=episode_id,
            first_live_shadow=first_live_shadow,
        )


@dataclass(frozen=True)
class FormalEligibilityDecision:
    """The sole final V4 formal-data eligibility verdict."""

    episode_id: str
    formal_eligible: bool
    predicates: Mapping[str, bool]
    reasons: tuple[str, ...]
    row_count: int
    first_live_shadow: bool
    schema: str = FORMAL_ELIGIBILITY_SCHEMA

    @property
    def training_eligible(self) -> bool:
        return self.formal_eligible

    @property
    def active_enabled(self) -> bool:
        return False

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "episode_id": self.episode_id,
            "formal_eligible": self.formal_eligible,
            "training_eligible": self.training_eligible,
            "active_enabled": False,
            "shadow_only": True,
            "first_live_shadow": self.first_live_shadow,
            "row_count": self.row_count,
            "predicates": dict(self.predicates),
            "reasons": list(self.reasons),
        }


def _formal_row_payload(row: object) -> Mapping[str, Any] | None:
    if isinstance(row, Mapping):
        return row
    as_json = getattr(row, "as_json", None)
    if callable(as_json):
        payload = as_json()
        return payload if isinstance(payload, Mapping) else None
    return None


def _formal_manifest_payload(value: object) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    as_json = getattr(value, "as_json", None)
    if callable(as_json):
        payload = as_json()
        return payload if isinstance(payload, Mapping) else None
    return None


class FormalEligibilityValidator:
    """Fail-closed V4 validator and sole formal receipt writer.

    This validator intentionally does not delegate to the compatibility V2/V3
    validator.  A valid legacy row is useful evidence, but it cannot become a
    formal V4 row by re-labelling or re-signing it.
    """

    def __init__(self, *, receipt_schema: str = FORMAL_ELIGIBILITY_SCHEMA) -> None:
        if receipt_schema != FORMAL_ELIGIBILITY_SCHEMA:
            raise ValueError("formal eligibility schema is frozen")
        self.receipt_schema = receipt_schema
        self._write_lock = threading.Lock()

    @staticmethod
    def _manifest_ok(payload: Mapping[str, Any]) -> bool:
        try:
            manifest = FormalEpisodeManifestV1.from_json(payload)
            if manifest.force_authority.source_identity != KUNWEI_ONLY_FORCE_SOURCE_ID:
                return False
            if manifest.observation_dimension != FORMAL_OBSERVATION_DIMENSION:
                return False
            if tuple(manifest.model_rate_candidates_hz) != FORMAL_MODEL_RATE_CANDIDATES_HZ:
                return False
            if manifest.sampler_steps != 50 or manifest.control_rate_hz != 500:
                return False
            if manifest.model_active is not False or manifest.shadow_only is not True:
                return False
            if manifest.production_dynamics_required is not True:
                return False
            validate_formal_force_source_payload(payload, path="formal_manifest")
            return True
        except (TypeError, ValueError, KeyError):
            return False

    @staticmethod
    def _health_ok(health: object) -> bool:
        return bool(
            _health_value(health, "sealed", False) is True
            and _health_value(health, "manifest_written", False) is True
            and _health_value(health, "tamper_free", False) is True
            and _health_value(health, "fault", None) is None
            and _health_value(health, "writer_error", None) is None
            and _health_value(health, "overflowed", True) is False
            and _health_value(health, "stalled", True) is False
            and _int_value(_health_value(health, "queue_depth", -1), -1) == 0
            and _int_value(_health_value(health, "unsealed_tail", -1), -1) == 0
            and _int_value(_health_value(health, "enqueued_rows", -1), -1)
            == _int_value(_health_value(health, "durable_rows", -2), -2)
        )

    def evaluate(
        self,
        frames: Iterable[object],
        *,
        recorder_health: object,
        formal_manifest: object | None = None,
        episode_id: str | None = None,
        first_live_shadow: bool = True,
    ) -> FormalEligibilityDecision:
        rows = tuple(frames)
        payloads = tuple(_formal_row_payload(row) for row in rows)
        resolved_episode_id = episode_id or (
            str(payloads[0].get("episode_id", "unknown"))
            if payloads and payloads[0] is not None
            else "unknown"
        )
        manifest_payload = _formal_manifest_payload(formal_manifest)
        if manifest_payload is None and payloads and payloads[0] is not None:
            manifest_payload = payloads[0].get("formal_manifest")
        reasons: list[str] = []
        predicates: dict[str, bool] = {
            "non_empty": bool(rows),
            "all_rows_are_v4": bool(rows) and all(
                payload is not None and payload.get("schema") == EPISODE_FRAME_SCHEMA_V4
                for payload in payloads
            ),
            "no_legacy_v2_v3_rows": bool(rows) and all(
                payload is not None and payload.get("schema") not in {
                    "ur10e_tacdiffusion_episode_frame/v2",
                    "ur10e_tacdiffusion_episode_frame/v3",
                }
                for payload in payloads
            ),
            "formal_manifest_valid": manifest_payload is not None and self._manifest_ok(manifest_payload),
            "formal_seal_fields_present": bool(rows) and all(
                payload is not None and isinstance(payload.get("row_sha256"), str)
                and payload.get("row_sha256") == compute_v3_row_sha256(payload)
                for payload in payloads
            ),
            "full_receipts_present": bool(rows) and all(
                payload is not None
                and all(
                    isinstance(payload.get(name), Mapping)
                    for name in (
                        "force_authority_receipt",
                        "production_dynamics_receipt",
                        "reference_receipt",
                        "expert_action_receipt",
                        "tube_decision_receipt",
                    )
                )
                for payload in payloads
            ),
            "kunwei_only_force_authority": bool(rows),
            "production_previous_tick_dynamics": bool(rows),
            "reference_receipts_valid": bool(rows),
            "expert_action_receipts_valid": bool(rows),
            "tube_decisions_valid": bool(rows),
            "84d_observations": bool(rows),
            "strict_tick_identity": bool(rows),
            "row_validity": bool(rows),
            "recorder_health_complete": self._health_ok(recorder_health),
            "first_live_shadow_clear": not first_live_shadow,
            "model_inactive_shadow_only": True,
        }
        previous_sequence = -1
        previous_time = -math.inf
        for payload in payloads:
            if payload is None:
                continue
            try:
                validate_formal_force_source_payload(payload, path="formal_episode_row")
            except ValueError:
                predicates["kunwei_only_force_authority"] = False
            authority = payload.get("force_authority_receipt")
            if not isinstance(authority, Mapping) or authority.get("source_identity") != KUNWEI_ONLY_FORCE_SOURCE_ID or authority.get("valid") is not True:
                predicates["kunwei_only_force_authority"] = False
            production = payload.get("production_dynamics_receipt")
            dynamics = production.get("dynamics_receipt") if isinstance(production, Mapping) else None
            if not isinstance(production, Mapping) or production.get("source_kind") != "production" or production.get("previous_tick_only") is not True or not isinstance(dynamics, Mapping) or dynamics.get("valid") is not True or dynamics.get("authoritative_torque_source") != DYNAMICS_AUTHORITATIVE_TORQUE_SOURCE or not isinstance(dynamics.get("conformance_binding"), Mapping):
                predicates["production_previous_tick_dynamics"] = False
            reference = payload.get("reference_receipt")
            if not isinstance(reference, Mapping) or not isinstance(reference.get("payload"), Mapping) or reference["payload"].get("valid") is not True or reference["payload"].get("shadow_only", False) is True:
                predicates["reference_receipts_valid"] = False
            expert = payload.get("expert_action_receipt")
            if not isinstance(expert, Mapping) or not isinstance(expert.get("payload"), Mapping) or expert["payload"].get("available") is not True or expert["payload"].get("shadow_only") is not False:
                predicates["expert_action_receipts_valid"] = False
            tube = payload.get("tube_decision_receipt")
            if not isinstance(tube, Mapping) or not isinstance(tube.get("payload"), Mapping) or tube["payload"].get("accepted") is not True or tube["payload"].get("shadow_only", False) is True:
                predicates["tube_decisions_valid"] = False
            observation = payload.get("observation_84d")
            action = payload.get("expert_action_12d")
            applied = payload.get("applied_action_12d")
            try:
                if len(tuple(float(value) for value in observation)) != FORMAL_OBSERVATION_DIMENSION:
                    predicates["84d_observations"] = False
                if len(tuple(float(value) for value in action)) != ACTION_DIMENSION or tuple(float(value) for value in action) != tuple(float(value) for value in applied):
                    predicates["expert_action_receipts_valid"] = False
            except (TypeError, ValueError):
                predicates["84d_observations"] = False
                predicates["expert_action_receipts_valid"] = False
            if payload.get("formal_receipts_valid") is not True or payload.get("row_valid") is not True:
                predicates["row_validity"] = False
            source = str(payload.get("expert_action_source", ""))
            semantics = str(payload.get("action_label_semantics", ""))
            if "diagnostic" in source.lower() or "diagnostic" in semantics.lower() or payload.get("shadow_only") is True:
                predicates["expert_action_receipts_valid"] = False
            try:
                sequence = int(payload.get("control_sequence"))
                timestamp = float(payload.get("control_time_s"))
                if sequence <= previous_sequence or not math.isfinite(timestamp) or timestamp <= previous_time:
                    predicates["strict_tick_identity"] = False
                previous_sequence = sequence
                previous_time = timestamp
            except (TypeError, ValueError):
                predicates["strict_tick_identity"] = False
        if first_live_shadow:
            reasons.append("first_live_shadow_forced_ineligible")
        for name, passed in predicates.items():
            if not passed:
                reasons.append(name)
        eligible = bool(all(predicates.values()))
        return FormalEligibilityDecision(
            episode_id=resolved_episode_id,
            formal_eligible=eligible,
            predicates=predicates,
            reasons=tuple(dict.fromkeys(reasons)),
            row_count=len(rows),
            first_live_shadow=first_live_shadow,
        )

    def write_receipt(
        self,
        path: str | Path,
        decision: FormalEligibilityDecision,
        *,
        recorder_health: object | None = None,
    ) -> dict[str, Any]:
        if not isinstance(decision, FormalEligibilityDecision):
            raise TypeError("FormalEligibilityValidator is the sole formal receipt writer")
        payload = decision.as_json()
        if recorder_health is not None:
            payload["recorder_health"] = recorder_health.as_json() if hasattr(recorder_health, "as_json") else dict(recorder_health)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        payload["decision_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        destination = Path(path)
        with self._write_lock:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise FileExistsError(f"formal eligibility receipt already exists: {destination}")
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                with temporary.open("x", encoding="utf-8") as handle:
                    json.dump(payload, handle, sort_keys=True, indent=2, allow_nan=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return payload

    def evaluate_and_write(
        self,
        path: str | Path,
        frames: Iterable[object],
        *,
        recorder_health: object,
        formal_manifest: object | None = None,
        episode_id: str | None = None,
        first_live_shadow: bool = True,
    ) -> tuple[FormalEligibilityDecision, dict[str, Any]]:
        decision = self.evaluate(
            frames,
            recorder_health=recorder_health,
            formal_manifest=formal_manifest,
            episode_id=episode_id,
            first_live_shadow=first_live_shadow,
        )
        return decision, self.write_receipt(path, decision, recorder_health=recorder_health)


def read_eligibility_receipt(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != ELIGIBILITY_SCHEMA:
        raise ValueError("eligibility receipt schema mismatch")
    return payload


def read_formal_eligibility_receipt(path: str | Path) -> dict[str, Any]:
    """Read the sole V4 verdict receipt and verify its decision hash."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != FORMAL_ELIGIBILITY_SCHEMA:
        raise ValueError("formal eligibility receipt schema mismatch")
    supplied = payload.get("decision_sha256")
    if not isinstance(supplied, str) or len(supplied) != 64 or any(
        character not in "0123456789abcdef" for character in supplied
    ):
        raise ValueError("formal eligibility decision hash is malformed")
    unsigned = dict(payload)
    unsigned.pop("decision_sha256", None)
    expected = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    if supplied != expected:
        raise ValueError("formal eligibility decision hash mismatch")
    if payload.get("active_enabled") is not False or payload.get("shadow_only") is not True:
        raise ValueError("formal eligibility receipt must remain inactive and shadow-only")
    return payload


__all__ = [
    "ELIGIBILITY_SCHEMA",
    "FORMAL_ELIGIBILITY_SCHEMA",
    "EligibilityDecision",
    "EligibilityValidator",
    "FormalEligibilityDecision",
    "FormalEligibilityValidator",
    "read_formal_eligibility_receipt",
    "read_eligibility_receipt",
]
