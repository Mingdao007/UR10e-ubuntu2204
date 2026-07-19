"""Deterministic Failure-to-Guard contracts for offline experiment governance.

This module deliberately has no robot, ROS, bridge, optimizer, or process side
effects.  It turns observed failures and proposed changes into small immutable
decisions that callers can persist in their own evidence stores.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from .identity import canonical_json_bytes


class FailureToGuardError(ValueError):
    """Raised when governance input is ambiguous or malformed."""


class ParameterImpact(str, Enum):
    """Effect of a change on already collected parameter evidence."""

    NONE = "none"
    REPLAY_REQUIRED = "replay_required"
    RETUNE_REQUIRED = "retune_required"


class TrialOutcomeClass(str, Enum):
    """Canonical outcome classes accepted by the optimizer eligibility gate."""

    VALID_PARAMETER_OBSERVATION = "valid_parameter_observation"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    STARTUP_FAILURE = "startup_failure"
    MODEL_MISMATCH = "model_mismatch"
    SAFETY_STOP = "safety_stop"
    OBSERVER_GAP = "observer_gap"
    OPERATOR_STOP = "operator_stop"
    CODE_CONTRACT_FAILURE = "code_contract_failure"


class MetricRole(str, Enum):
    TRAINABLE_OBJECTIVE = "trainable_objective"
    DIAGNOSTIC_ONLY = "diagnostic_only"
    UNAVAILABLE = "unavailable"


class OracleStatus(str, Enum):
    CREDIBLE = "credible"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class ObserverStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    UNAVAILABLE = "unavailable"


_IMPACT_ORDER = {
    ParameterImpact.NONE: 0,
    ParameterImpact.REPLAY_REQUIRED: 1,
    ParameterImpact.RETUNE_REQUIRED: 2,
}
_PLAN_ONLY_LOCKS = frozenset({"live_writer", "formal_timing"})

_ISO_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+(?:Z|[+-]\d{2}:?\d{2})?\b",
    re.IGNORECASE,
)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_DIGEST_RE = re.compile(r"\b[0-9a-f]{32,128}\b", re.IGNORECASE)
_PID_RE = re.compile(
    r"\b(pid|process|attempt|round|sequence|seq)\s*[:=#-]?\s*\d+\b",
    re.IGNORECASE,
)
_TMP_PATH_RE = re.compile(r"/(?:tmp|var/tmp)/[^\s,;]+", re.IGNORECASE)
_HOME_PREFIX_RE = re.compile(r"/home/[^/\s]+/", re.IGNORECASE)
_RUN_INSTANCE_RE = re.compile(r"\b(runs?/)[^/\s]+/", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(payload)


class _StrictLineJSONError(ValueError):
    """Internal signal for ambiguous JSONL records."""


def _reject_duplicate_json_keys(
    pairs: Iterable[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictLineJSONError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> None:
    raise _StrictLineJSONError(f"non-finite number {value!r}")


def _parse_finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _StrictLineJSONError(f"non-finite number {value!r}")
    return parsed


def _strict_ledger_record(raw_line: bytes, line_number: int) -> dict[str, Any]:
    try:
        text = raw_line.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise FailureToGuardError(
            f"invalid failure ledger JSON at line {line_number}: input is not UTF-8"
        ) from exc
    try:
        entry = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
            parse_float=_parse_finite_json_float,
        )
    except (_StrictLineJSONError, json.JSONDecodeError) as exc:
        raise FailureToGuardError(
            f"invalid failure ledger JSON at line {line_number}: {exc}"
        ) from exc
    if not isinstance(entry, dict):
        raise FailureToGuardError(
            f"failure ledger line {line_number} must be a JSON object"
        )
    return entry


def _normalize_label(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def sanitize_failure_text(value: object) -> str:
    """Remove volatile identifiers while preserving root-cause semantics."""

    text = unicodedata.normalize("NFKC", str(value)).strip().lower()
    text = _ISO_TIMESTAMP_RE.sub("<timestamp>", text)
    text = _UUID_RE.sub("<uuid>", text)
    text = _DIGEST_RE.sub("<digest>", text)
    text = _PID_RE.sub(lambda match: f"{match.group(1).lower()}=<n>", text)
    text = _TMP_PATH_RE.sub("<tmp>", text)
    text = _HOME_PREFIX_RE.sub("<home>/", text)
    text = _RUN_INSTANCE_RE.sub(r"\1<run>/", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _first_value(
    event: Mapping[str, Any], nested: Mapping[str, Any], keys: Sequence[str]
) -> object | None:
    for source in (event, nested):
        for key in keys:
            value = source.get(key)
            if value not in (None, "", [], {}):
                return value
    return None


def _string_list(value: object | None) -> tuple[str, ...]:
    if value is None:
        return ()
    values = value if isinstance(value, (list, tuple, set, frozenset)) else (value,)
    result = {_normalize_label(item) for item in values if str(item).strip()}
    result.discard("")
    return tuple(sorted(result))


def canonical_failure_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return the stable semantic subset used to identify a failure class.

    Run IDs, timestamps, hashes, hosts, PIDs, and volatile run-directory names
    are intentionally excluded.  Stage and lane are also excluded so the same
    missing guard escaping at another stage is recognized as a recurrence.
    """

    if not isinstance(event, Mapping):
        raise FailureToGuardError("failure event must be a mapping")
    failure = event.get("failure", {})
    if not isinstance(failure, Mapping):
        raise FailureToGuardError("failure field must be a mapping")

    outcome = _first_value(
        event,
        failure,
        ("outcome_class", "failure_class", "category", "kind"),
    )
    component = _first_value(
        event, failure, ("component", "subsystem", "owner", "process")
    )
    invariants = _string_list(
        _first_value(event, failure, ("invariant_ids", "invariant_id"))
    )
    root_cause = _first_value(
        event, failure, ("root_cause", "canonical_cause", "reason")
    )
    symptom = _first_value(event, failure, ("blocker", "symptom", "message", "error"))
    missing_resource = _first_value(
        event, failure, ("missing_resource", "missing_path", "missing_artifact")
    )

    if not any((outcome, component, invariants, root_cause, symptom, missing_resource)):
        raise FailureToGuardError("failure event has no stable semantic fields")

    payload: dict[str, Any] = {"signature_schema": "failure_to_guard.signature/v1"}
    if outcome is not None:
        payload["outcome_class"] = _normalize_label(outcome)
    if component is not None:
        payload["component"] = _normalize_label(component)
    if invariants:
        payload["invariant_ids"] = list(invariants)
    if root_cause is not None:
        payload["root_cause"] = sanitize_failure_text(root_cause)
    if symptom is not None:
        payload["symptom"] = sanitize_failure_text(symptom)
    if missing_resource is not None:
        resource_text = sanitize_failure_text(missing_resource).replace("\\", "/")
        payload["missing_resource"] = PurePosixPath(resource_text).name
    return payload


def canonical_failure_signature(event: Mapping[str, Any]) -> str:
    """Return a SHA256 signature for the stable failure semantics."""

    return hashlib.sha256(_canonical_json_bytes(canonical_failure_payload(event))).hexdigest()


@dataclass(frozen=True)
class OutcomeGate:
    outcome_class: TrialOutcomeClass
    metric_role: MetricRole
    oracle_status: OracleStatus
    observer_status: ObserverStatus
    objective: float | None
    optimizer_eligible: bool
    rejection_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "failure_to_guard.outcome_gate/v1",
            "outcome_class": self.outcome_class.value,
            "metric_role": self.metric_role.value,
            "oracle_status": self.oracle_status.value,
            "observer_status": self.observer_status.value,
            "objective": self.objective,
            "optimizer_eligible": self.optimizer_eligible,
            "rejection_reasons": list(self.rejection_reasons),
        }


def gate_optimizer_observation(
    outcome_class: str | TrialOutcomeClass,
    objective: float | int | None,
    *,
    metric_role: str | MetricRole = MetricRole.UNAVAILABLE,
    oracle_status: str | OracleStatus = OracleStatus.UNAVAILABLE,
    observer_status: str | ObserverStatus = ObserverStatus.UNAVAILABLE,
    fingerprint_verified: bool = False,
    exact_ack_consumed: bool = False,
    post_ack_closure_verified: bool = False,
    publication_unique: bool = False,
) -> OutcomeGate:
    """Fail closed before an observation reaches a store or optimizer."""

    try:
        outcome = TrialOutcomeClass(outcome_class)
    except ValueError as exc:
        raise FailureToGuardError(f"unknown trial outcome class: {outcome_class!r}") from exc
    try:
        role = MetricRole(metric_role)
        oracle = OracleStatus(oracle_status)
        observer = ObserverStatus(observer_status)
    except ValueError as exc:
        raise FailureToGuardError(f"unknown optimizer gate enum: {exc}") from exc

    reasons: list[str] = []
    numeric_objective: float | None = None
    if outcome is not TrialOutcomeClass.VALID_PARAMETER_OBSERVATION:
        reasons.append(f"outcome:{outcome.value}")
    if role is not MetricRole.TRAINABLE_OBJECTIVE:
        reasons.append(f"metric_role:{role.value}")
    if (
        isinstance(objective, bool)
        or objective is None
        or not isinstance(objective, (int, float))
    ):
        reasons.append("objective_missing_or_non_numeric")
    else:
        numeric_objective = float(objective)
        if not math.isfinite(numeric_objective):
            reasons.append("objective_non_finite")
            numeric_objective = None
    if fingerprint_verified is not True:
        reasons.append("fingerprint_unverified")
    if oracle is not OracleStatus.CREDIBLE:
        reasons.append(f"oracle_status:{oracle.value}")
    if observer is not ObserverStatus.COMPLETE:
        reasons.append(f"observer_status:{observer.value}")
    if exact_ack_consumed is not True:
        reasons.append("exact_ack_unverified")
    if post_ack_closure_verified is not True:
        reasons.append("post_ack_closure_unverified")
    if publication_unique is not True:
        reasons.append("publication_not_unique")

    eligible = not reasons
    return OutcomeGate(
        outcome_class=outcome,
        metric_role=role,
        oracle_status=oracle,
        observer_status=observer,
        objective=numeric_objective if eligible else None,
        optimizer_eligible=eligible,
        rejection_reasons=tuple(reasons),
    )


classify_trial_outcome = gate_optimizer_observation


class FailureToGuardOutcomeClassifier:
    """Allowlist-ready adapter implementing the runtime OutcomeClassifier protocol."""

    component_id = "outcome_classifier_v1"
    version = "1"

    def classify(self, evidence: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(evidence, Mapping):
            raise FailureToGuardError("trial evidence must be a mapping")
        checks = evidence.get("checks", {})
        if not isinstance(checks, Mapping):
            raise FailureToGuardError("trial evidence checks must be a mapping")

        def verified(name: str) -> bool:
            value = evidence.get(name, checks.get(name))
            return value is True

        return gate_optimizer_observation(
            evidence.get("outcome_class", ""),
            evidence.get("objective"),
            metric_role=evidence.get("metric_role", "unavailable"),
            oracle_status=evidence.get("oracle_status", "unavailable"),
            observer_status=evidence.get("observer_status", "unavailable"),
            fingerprint_verified=verified("fingerprint_verified"),
            exact_ack_consumed=verified("exact_ack_consumed"),
            post_ack_closure_verified=verified("post_ack_closure_verified"),
            publication_unique=verified("publication_unique"),
        ).to_dict()


def load_failure_ledger(path: Path) -> tuple[dict[str, Any], ...]:
    """Stream strict UTF-8 JSONL and reject ambiguous or non-object records."""

    entries: list[dict[str, Any]] = []
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise FailureToGuardError(
                    "failure ledger must be a no-follow regular file"
                )
            with os.fdopen(fd, "rb", closefd=False) as stream:
                for line_number, raw_line in enumerate(stream, start=1):
                    if not raw_line.strip():
                        continue
                    entries.append(_strict_ledger_record(raw_line, line_number))
        finally:
            os.close(fd)
    except FailureToGuardError:
        raise
    except OSError as exc:
        raise FailureToGuardError(
            f"failure ledger must be a no-follow regular file: {exc}"
        ) from exc
    return tuple(entries)


def _entry_signature(entry: Mapping[str, Any]) -> str | None:
    if entry.get("counts_for_recurrence") is False:
        return None
    for key in ("canonical_signature", "failure_signature"):
        value = entry.get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    nested = entry.get("failure_event")
    if isinstance(nested, Mapping):
        return canonical_failure_signature(nested)
    return None


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FailureToGuardError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True)
class DebtDecision:
    required: bool
    canonical_signature: str
    recurrence_count: int
    reasons: tuple[str, ...]
    execution_mode: str
    blocking_resource_locks: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "failure_to_guard.debt_lane_request/v1",
            "required": self.required,
            "canonical_signature": self.canonical_signature,
            "recurrence_count": self.recurrence_count,
            "reasons": list(self.reasons),
            "execution_mode": self.execution_mode,
            "blocking_resource_locks": list(self.blocking_resource_locks),
        }


def assess_debt_lane(
    failure_event: Mapping[str, Any],
    *,
    prior_ledger_entries: Iterable[Mapping[str, Any]] = (),
    escaped_existing_tests: bool = False,
    patch_test_evidence_loops: int = 0,
    sampling_rounds: int = 0,
    resource_locks: Iterable[str] = (),
) -> DebtDecision:
    """Evaluate deterministic debt-lane triggers without starting an agent."""

    loops = _nonnegative_int(patch_test_evidence_loops, "patch_test_evidence_loops")
    rounds = _nonnegative_int(sampling_rounds, "sampling_rounds")
    signature = canonical_failure_signature(failure_event)
    prior_matches = sum(
        1 for entry in prior_ledger_entries if _entry_signature(entry) == signature
    )
    recurrence_count = prior_matches + 1
    lane = _normalize_label(
        failure_event.get("lane")
        or failure_event.get("source_lane")
        or failure_event.get("execution_lane")
        or ""
    )

    reasons: list[str] = []
    if escaped_existing_tests and lane in {"hil", "live", "live_autotune"}:
        reasons.append("hil_live_escape")
    if recurrence_count >= 2:
        reasons.append("canonical_failure_recurrence")
    if loops >= 4:
        reasons.append("patch_test_evidence_loop")
    if rounds > 40:
        reasons.append("sampling_round_budget")

    locks = tuple(sorted({_normalize_label(lock) for lock in resource_locks if lock}))
    blocking = tuple(lock for lock in locks if lock in _PLAN_ONLY_LOCKS)
    required = bool(reasons)
    if not required:
        execution_mode = "not_requested"
    elif blocking:
        execution_mode = "plan_only"
    else:
        execution_mode = "implementation_allowed"
    return DebtDecision(
        required=required,
        canonical_signature=signature,
        recurrence_count=recurrence_count,
        reasons=tuple(reasons),
        execution_mode=execution_mode,
        blocking_resource_locks=blocking,
    )


build_debt_lane_request = assess_debt_lane


def _normalize_touched_path(path: object) -> str:
    text = str(path).replace("\\", "/").strip()
    candidate = PurePosixPath(text)
    if not text or candidate.is_absolute() or ".." in candidate.parts:
        raise FailureToGuardError(f"touched path must be repository-relative: {path!r}")
    return candidate.as_posix().lstrip("./")


def _matches(path: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/").lstrip("./")
    candidates = {normalized}
    while "**/" in normalized:
        normalized = normalized.replace("**/", "", 1)
        candidates.add(normalized)
    return any(fnmatch.fnmatchcase(path, candidate) for candidate in candidates)


def _parse_impact(value: object) -> ParameterImpact:
    try:
        return ParameterImpact(str(value))
    except ValueError as exc:
        raise FailureToGuardError(f"unknown parameter impact: {value!r}") from exc


def parameter_impact_for_paths(
    touched_paths: Iterable[object], invariant_registry: Mapping[str, Any]
) -> ParameterImpact:
    """Return the strongest parameter impact selected by mapped invariants."""

    paths = tuple(sorted({_normalize_touched_path(path) for path in touched_paths}))
    invariants = invariant_registry.get("invariants")
    if not isinstance(invariants, list):
        raise FailureToGuardError("invariant registry must contain an invariants list")
    selected = ParameterImpact.NONE
    for invariant in invariants:
        if not isinstance(invariant, Mapping):
            raise FailureToGuardError("each invariant registry entry must be an object")
        patterns = invariant.get("touched_path_patterns", invariant.get("path_patterns", []))
        if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
            raise FailureToGuardError("invariant path patterns must be a list of strings")
        if any(_matches(path, pattern) for path in paths for pattern in patterns):
            impact = _parse_impact(invariant.get("parameter_impact", "none"))
            if _IMPACT_ORDER[impact] > _IMPACT_ORDER[selected]:
                selected = impact
    return selected


@dataclass(frozen=True)
class ChangeContract:
    touched_paths: tuple[str, ...]
    invariant_ids: tuple[str, ...]
    related_failure_signatures: tuple[str, ...]
    required_test_lanes: tuple[str, ...]
    expected_failure_modes: tuple[str, ...]
    parameter_impact: ParameterImpact
    unmapped_touched_paths: tuple[str, ...]
    contract_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "failure_to_guard.change_contract/v1",
            "touched_paths": list(self.touched_paths),
            "invariant_ids": list(self.invariant_ids),
            "related_failure_signatures": list(self.related_failure_signatures),
            "required_test_lanes": list(self.required_test_lanes),
            "expected_failure_modes": list(self.expected_failure_modes),
            "parameter_impact": self.parameter_impact.value,
            "unmapped_touched_paths": list(self.unmapped_touched_paths),
            "contract_fingerprint": self.contract_fingerprint,
        }


def build_change_contract(
    touched_paths: Iterable[object],
    invariant_registry: Mapping[str, Any],
    coverage_map: Mapping[str, Any],
    *,
    failure_ledger_entries: Iterable[Mapping[str, Any]] = (),
    fail_on_unmapped: bool = True,
) -> ChangeContract:
    """Build a deterministic feedforward contract for a proposed code change."""

    paths = tuple(sorted({_normalize_touched_path(path) for path in touched_paths}))
    if not paths:
        raise FailureToGuardError("change contract requires at least one touched path")
    invariants = invariant_registry.get("invariants")
    if not isinstance(invariants, list):
        raise FailureToGuardError("invariant registry must contain an invariants list")

    selected: list[Mapping[str, Any]] = []
    mapped_paths: set[str] = set()
    for invariant in invariants:
        if not isinstance(invariant, Mapping) or not isinstance(invariant.get("id"), str):
            raise FailureToGuardError("each invariant must have a string id")
        patterns = invariant.get("touched_path_patterns", invariant.get("path_patterns", []))
        if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
            raise FailureToGuardError("invariant path patterns must be a list of strings")
        matches = {
            path for path in paths if any(_matches(path, pattern) for pattern in patterns)
        }
        if matches:
            selected.append(invariant)
            mapped_paths.update(matches)

    unmapped = tuple(path for path in paths if path not in mapped_paths)
    if fail_on_unmapped and unmapped:
        raise FailureToGuardError(
            "unmapped touched paths: " + ", ".join(unmapped)
        )

    invariant_ids = tuple(sorted(str(item["id"]) for item in selected))
    selected_id_set = set(invariant_ids)
    normalized_selected_id_set = {_normalize_label(item) for item in invariant_ids}
    required_lanes: set[str] = set()
    failure_modes: set[str] = set()
    impact = ParameterImpact.NONE
    for invariant in selected:
        required_lanes.update(_string_list(invariant.get("required_lanes")))
        failure_modes.update(_string_list(invariant.get("expected_failure_modes")))
        item_impact = _parse_impact(invariant.get("parameter_impact", "none"))
        if _IMPACT_ORDER[item_impact] > _IMPACT_ORDER[impact]:
            impact = item_impact

    coverage_entries = coverage_map.get("invariants", [])
    if not isinstance(coverage_entries, list):
        raise FailureToGuardError("coverage map invariants must be a list")
    for entry in coverage_entries:
        if not isinstance(entry, Mapping) or entry.get("invariant_id") not in selected_id_set:
            continue
        lanes = entry.get("lanes", {})
        if not isinstance(lanes, Mapping):
            raise FailureToGuardError("coverage lanes must be an object")
        for lane, coverage in lanes.items():
            if isinstance(coverage, Mapping) and coverage.get("required") is True:
                required_lanes.add(_normalize_label(lane))

    related: set[str] = set()
    for ledger_entry in failure_ledger_entries:
        ledger_invariants = set(_string_list(ledger_entry.get("invariant_ids")))
        if ledger_invariants & normalized_selected_id_set:
            signature = _entry_signature(ledger_entry)
            if signature:
                related.add(signature)

    unsigned = {
        "schema": "failure_to_guard.change_contract/v1",
        "touched_paths": list(paths),
        "invariant_ids": list(invariant_ids),
        "related_failure_signatures": sorted(related),
        "required_test_lanes": sorted(required_lanes),
        "expected_failure_modes": sorted(failure_modes),
        "parameter_impact": impact.value,
        "unmapped_touched_paths": list(unmapped),
    }
    fingerprint = hashlib.sha256(_canonical_json_bytes(unsigned)).hexdigest()
    return ChangeContract(
        touched_paths=paths,
        invariant_ids=invariant_ids,
        related_failure_signatures=tuple(sorted(related)),
        required_test_lanes=tuple(sorted(required_lanes)),
        expected_failure_modes=tuple(sorted(failure_modes)),
        parameter_impact=impact,
        unmapped_touched_paths=unmapped,
        contract_fingerprint=fingerprint,
    )
