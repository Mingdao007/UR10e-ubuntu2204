#!/usr/bin/python3.10
"""Atomic Codex-managed hybrid I-scale/log2 batches for Step5d autotune."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_contract import ForceCandidate, LOG2_LATTICE_OCTAVE
from ur10e_experiment_runtime.candidate_identity import (
    ControlCandidateUid,
    OccurrenceUid,
    TransportCandidateUid,
)


SCHEMA_VERSION = "step5d_autotune_codex_batch_plan_v1"
SCHEMA_VERSION_V2 = "step5d_autotune_codex_batch_plan_v2"
SCHEMA_VERSION_R008 = "step5d_autotune_rolling_batch_plan_r008_v1"
SCHEMA_VERSION_ROLLING = "step5d_autotune_rolling_batch_plan_v1"
SCHEMA_VERSION_ROLLING_V2 = "step5d_autotune_rolling_batch_plan_v2"
ROLLING_LIFECYCLE_SCHEMAS = {
    SCHEMA_VERSION_ROLLING,
    SCHEMA_VERSION_ROLLING_V2,
}
ENVELOPE_ID = "positive_i_multiplier_coarse_log2_fine_v1"
BATCH_SIZE = 5
V3_BATCH_SIZE = 10
R008_BATCH_SIZE = 5


class PlanLifecycle(str, Enum):
    OPEN_READY = "OPEN_READY"
    OPEN_EMPTY = "OPEN_EMPTY"
    CLOSED_COMPLETE = "CLOSED_COMPLETE"


@dataclass(frozen=True)
class RuntimePlanRow:
    logical_batch_sequence: int
    row_index: int
    plan_revision: int
    occurrence_uid: OccurrenceUid
    transport_candidate_uid: TransportCandidateUid
    control_candidate_uid: ControlCandidateUid
    candidate: ForceCandidate
    overlay: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        for name, expected in (
            ("occurrence_uid", OccurrenceUid),
            ("transport_candidate_uid", TransportCandidateUid),
            ("control_candidate_uid", ControlCandidateUid),
        ):
            if type(getattr(self, name)) is not expected:
                raise TypeError(f"{name} uses the wrong UID namespace")
        if len(
            {
                str(self.occurrence_uid),
                str(self.transport_candidate_uid),
                str(self.control_candidate_uid),
            }
        ) != 3:
            raise ValueError("runtime plan row UID namespaces must be distinct")

    def with_overlay(self, overlay: Mapping[str, Any]) -> "RuntimePlanRow":
        if overlay.get("control_candidate_uid") != self.control_candidate_uid:
            raise ValueError("runtime plan row control UID differs from overlay")
        return RuntimePlanRow(
            self.logical_batch_sequence,
            self.row_index,
            self.plan_revision,
            self.occurrence_uid,
            self.transport_candidate_uid,
            self.control_candidate_uid,
            self.candidate,
            dict(overlay),
        )


@dataclass(frozen=True)
class CandidateOccurrence:
    candidate: ForceCandidate
    occurrence_uid: str
    transport_candidate_uid: str
    role: str
    replicate_ordinal: int
    control_candidate_uid: str

    def __post_init__(self) -> None:
        typed_expectations = {
            "occurrence_uid": OccurrenceUid,
            "transport_candidate_uid": TransportCandidateUid,
            "control_candidate_uid": ControlCandidateUid,
        }
        for name in typed_expectations:
            value = getattr(self, name)
            if (
                isinstance(
                    value,
                    (OccurrenceUid, TransportCandidateUid, ControlCandidateUid),
                )
                and type(value) is not typed_expectations[name]
            ):
                raise TypeError(f"{name} uses the wrong UID namespace")
            object.__setattr__(
                self,
                name,
                typed_expectations[name].parse(value, allow_legacy=True),
            )
        if not isinstance(self.role, str) or not self.role:
            raise ValueError("occurrence role must be non-empty")
        if (
            isinstance(self.replicate_ordinal, bool)
            or not isinstance(self.replicate_ordinal, int)
            or self.replicate_ordinal < 1
        ):
            raise ValueError("replicate_ordinal must be positive")
        if len(
            {
                str(self.occurrence_uid),
                str(self.transport_candidate_uid),
                str(self.control_candidate_uid),
            }
        ) != 3:
            raise ValueError("candidate occurrence UID namespaces must be distinct")


@dataclass(frozen=True)
class CandidateBatchPlan:
    campaign_id: str
    revision: int
    closed: bool
    code_fix_replay_candidate_uid: str | None
    batch_size: int
    batches: tuple[tuple[ForceCandidate, ...], ...]
    payload: Mapping[str, Any]
    occurrences: tuple[tuple[CandidateOccurrence, ...], ...] = ()
    batch_revisions: tuple[int, ...] = ()
    lifecycle: PlanLifecycle = PlanLifecycle.OPEN_EMPTY
    closure: Mapping[str, Any] | None = None

    @property
    def candidates(self) -> tuple[ForceCandidate, ...]:
        return tuple(candidate for batch in self.batches for candidate in batch)


def _coordinate(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric) or abs(numeric) > 1.0 + 1e-12:
        raise ValueError(f"{name} must remain inside [-1, 1] octave")
    if not math.isclose(
        numeric / LOG2_LATTICE_OCTAVE,
        round(numeric / LOG2_LATTICE_OCTAVE),
        abs_tol=1e-9,
    ):
        raise ValueError(f"{name} must use the 0.25-octave lattice")
    return numeric


def candidate_from_log2_payload(payload: Any) -> ForceCandidate:
    if not isinstance(payload, Mapping):
        raise ValueError("planned candidate must be an object")
    fine_fields = {"log2_p", "log2_i", "log2_damping"}
    scale_fields = {"log2_p", "i_multiplier", "log2_damping"}
    tau_field = {"log2_filter_tau"}
    if frozenset(payload) in {
        frozenset(fine_fields),
        frozenset(fine_fields | tau_field),
    }:
        return ForceCandidate.from_log2(
            p=_coordinate("log2_p", payload["log2_p"]),
            i=_coordinate("log2_i", payload["log2_i"]),
            damping=_coordinate("log2_damping", payload["log2_damping"]),
            filter_tau=_coordinate(
                "log2_filter_tau", payload.get("log2_filter_tau", 0.0)
            ),
        )
    if frozenset(payload) in {
        frozenset(scale_fields),
        frozenset(scale_fields | tau_field),
    }:
        multiplier = payload["i_multiplier"]
        if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
            raise ValueError("i_multiplier must be numeric")
        return ForceCandidate.from_i_multiplier(
            p=_coordinate("log2_p", payload["log2_p"]),
            i_multiplier=float(multiplier),
            damping=_coordinate("log2_damping", payload["log2_damping"]),
            filter_tau=_coordinate(
                "log2_filter_tau", payload.get("log2_filter_tau", 0.0)
            ),
        )
    raise ValueError(
        "planned candidate must contain log2 P/damping and log2_i or i_multiplier"
    )


def candidate_log2_payload(candidate: ForceCandidate) -> dict[str, float]:
    if candidate.i_mode != "positive":
        raise ValueError("Codex batch plans require positive log2 I")
    def lattice(value: float) -> float:
        return round(value / LOG2_LATTICE_OCTAVE) * LOG2_LATTICE_OCTAVE

    payload = {
        "log2_p": lattice(candidate.log2_p),
        "log2_damping": lattice(candidate.log2_damping),
        "log2_filter_tau": lattice(candidate.log2_filter_tau),
    }
    multiplier = candidate.approved_i_scale_multiplier
    if multiplier is None:
        payload["log2_i"] = lattice(candidate.log2_i)
    else:
        payload["i_multiplier"] = multiplier
    return payload


def load_plan(path: Path, *, campaign_id: str | None = None) -> CandidateBatchPlan:
    if path.is_symlink() or not path.is_file():
        raise ValueError("candidate plan must be a real regular file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "envelope_id",
        "campaign_id",
        "revision",
        "batch_size",
        "closed",
        "batches",
    }
    optional = {"code_fix_replay_candidate_uid"}
    rolling_lifecycle = {"lifecycle", "closure"}
    if not isinstance(payload, Mapping) or frozenset(payload) not in {
        frozenset(required),
        frozenset(required | optional),
        frozenset(required | rolling_lifecycle),
        frozenset(required | optional | rolling_lifecycle),
    }:
        raise ValueError("candidate plan schema is incomplete")
    if payload["schema_version"] not in {
        SCHEMA_VERSION,
        SCHEMA_VERSION_V2,
        SCHEMA_VERSION_R008,
        SCHEMA_VERSION_ROLLING,
        SCHEMA_VERSION_ROLLING_V2,
    }:
        raise ValueError("candidate plan schema version differs")
    if payload["envelope_id"] != ENVELOPE_ID:
        raise ValueError("candidate plan envelope differs")
    if not isinstance(payload["campaign_id"], str) or not payload["campaign_id"]:
        raise ValueError("candidate plan campaign_id is invalid")
    if campaign_id is not None and payload["campaign_id"] != campaign_id:
        raise ValueError("candidate plan campaign_id differs")
    if (
        isinstance(payload["revision"], bool)
        or not isinstance(payload["revision"], int)
        or payload["revision"] < 0
    ):
        raise ValueError("candidate plan revision is invalid")
    expected_batch_size = {
        SCHEMA_VERSION: BATCH_SIZE,
        SCHEMA_VERSION_V2: V3_BATCH_SIZE,
        SCHEMA_VERSION_R008: R008_BATCH_SIZE,
        SCHEMA_VERSION_ROLLING: R008_BATCH_SIZE,
        SCHEMA_VERSION_ROLLING_V2: R008_BATCH_SIZE,
    }[payload["schema_version"]]
    if payload["batch_size"] != expected_batch_size or type(payload["closed"]) is not bool:
        raise ValueError("candidate plan batch policy differs")
    if not isinstance(payload["batches"], list):
        raise ValueError("candidate plan batches must be a list")

    batches: list[tuple[ForceCandidate, ...]] = []
    occurrence_batches: list[tuple[CandidateOccurrence, ...]] = []
    seen: set[str] = set()
    seen_occurrences: set[str] = set()
    seen_transports: set[str] = set()
    for expected_id, row in enumerate(payload["batches"], start=1):
        rolling = payload["schema_version"] in {
            SCHEMA_VERSION_R008,
            *ROLLING_LIFECYCLE_SCHEMAS,
        }
        expected_row_fields = {
            "batch_id",
            "source",
            "occurrences" if rolling else "candidates",
        }
        if payload["schema_version"] in ROLLING_LIFECYCLE_SCHEMAS:
            expected_row_fields.add("plan_revision")
        if not isinstance(row, Mapping) or set(row) != expected_row_fields:
            raise ValueError("candidate batch schema differs")
        if row["batch_id"] != expected_id:
            raise ValueError("candidate batch ids must be contiguous")
        if payload["schema_version"] in ROLLING_LIFECYCLE_SCHEMAS and (
            isinstance(row["plan_revision"], bool)
            or not isinstance(row["plan_revision"], int)
            or row["plan_revision"] != expected_id
        ):
            raise ValueError("rolling batch plan revision differs from its append revision")
        if not isinstance(row["source"], str) or not row["source"].strip():
            raise ValueError("candidate batch source is invalid")
        raw_rows = row["occurrences" if rolling else "candidates"]
        candidate_count = (
            len(raw_rows)
            if isinstance(raw_rows, list)
            else 0
        )
        final_closed_partial = (
            payload["closed"]
            and expected_id == len(payload["batches"])
            and 1 <= candidate_count < expected_batch_size
        )
        if candidate_count != expected_batch_size and not final_closed_partial:
            raise ValueError(
                f"every open batch must contain exactly {expected_batch_size} points; "
                "only a closed final recovery batch may be partial"
            )
        if rolling:
            occurrences = []
            for raw in raw_rows:
                required_occurrence_fields = {
                    "candidate",
                    "occurrence_uid",
                    "transport_candidate_uid",
                    "role",
                    "replicate_ordinal",
                }
                if payload["schema_version"] in ROLLING_LIFECYCLE_SCHEMAS:
                    required_occurrence_fields.add("control_candidate_uid")
                if (
                    not isinstance(raw, Mapping)
                    or set(raw) != required_occurrence_fields
                ):
                    raise ValueError("rolling occurrence schema differs")
                candidate = candidate_from_log2_payload(raw["candidate"])
                occurrence = CandidateOccurrence(
                    candidate=candidate,
                    occurrence_uid=raw["occurrence_uid"],
                    transport_candidate_uid=raw["transport_candidate_uid"],
                    role=raw["role"],
                    replicate_ordinal=raw["replicate_ordinal"],
                    control_candidate_uid=raw.get(
                        "control_candidate_uid",
                        candidate.candidate_uid,
                    ),
                )
                if payload["schema_version"] == SCHEMA_VERSION_ROLLING_V2 and any(
                    uid.is_legacy
                    for uid in (
                        occurrence.occurrence_uid,
                        occurrence.transport_candidate_uid,
                        occurrence.control_candidate_uid,
                    )
                ):
                    raise ValueError("rolling-v2 requires domain-prefixed UID namespaces")
                if occurrence.occurrence_uid in seen_occurrences:
                    raise ValueError("rolling plan repeats an occurrence UID")
                if occurrence.transport_candidate_uid in seen_transports:
                    raise ValueError("rolling plan repeats a transport UID")
                seen_occurrences.add(occurrence.occurrence_uid)
                seen_transports.add(occurrence.transport_candidate_uid)
                occurrences.append(occurrence)
            occurrence_batch = tuple(occurrences)
            candidates = tuple(item.candidate for item in occurrence_batch)
        else:
            occurrence_batch = ()
            candidates = tuple(candidate_from_log2_payload(item) for item in raw_rows)
        for candidate in candidates:
            if not rolling and candidate.candidate_uid in seen:
                raise ValueError("candidate plan repeats an exact parameter set")
            seen.add(candidate.candidate_uid)
        batches.append(candidates)
        occurrence_batches.append(occurrence_batch)
    if payload["revision"] != len(batches):
        raise ValueError("candidate plan revision must equal the batch count")
    replay_uid = payload.get("code_fix_replay_candidate_uid")
    if replay_uid is not None:
        if (
            not isinstance(replay_uid, str)
            or len(replay_uid) != 64
            or any(char not in "0123456789abcdef" for char in replay_uid)
        ):
            raise ValueError("code-fix replay candidate uid is invalid")
        if sum(item.candidate_uid == replay_uid for item in (
            candidate for batch in batches for candidate in batch
        )) != 1:
            raise ValueError("code-fix replay candidate must appear exactly once")
    lifecycle = (
        PlanLifecycle(payload["lifecycle"])
        if payload["schema_version"] in ROLLING_LIFECYCLE_SCHEMAS
        else PlanLifecycle.CLOSED_COMPLETE
        if payload["closed"]
        else PlanLifecycle.OPEN_READY
        if batches
        else PlanLifecycle.OPEN_EMPTY
    )
    closure = payload.get("closure")
    if payload["schema_version"] in ROLLING_LIFECYCLE_SCHEMAS:
        if payload["closed"] is not (lifecycle is PlanLifecycle.CLOSED_COMPLETE):
            raise ValueError("rolling lifecycle and legacy closed projection differ")
        if lifecycle is PlanLifecycle.CLOSED_COMPLETE:
            if not isinstance(closure, Mapping) or set(closure) != {
                "reason",
                "evidence_sha256",
            }:
                raise ValueError("closed rolling plan lacks typed closure evidence")
            if not isinstance(closure["reason"], str) or not closure["reason"]:
                raise ValueError("closed rolling plan reason is missing")
            _sha256_digest(closure["evidence_sha256"], name="closure evidence")
        elif closure is not None:
            raise ValueError("open rolling plan cannot contain closure evidence")
        if not batches and lifecycle is not PlanLifecycle.OPEN_EMPTY:
            raise ValueError("empty rolling plan must remain OPEN_EMPTY")
    return CandidateBatchPlan(
        campaign_id=payload["campaign_id"],
        revision=payload["revision"],
        closed=payload["closed"],
        code_fix_replay_candidate_uid=replay_uid,
        batch_size=expected_batch_size,
        batches=tuple(batches),
        occurrences=tuple(occurrence_batches),
        batch_revisions=tuple(
            row.get("plan_revision", row["batch_id"])
            for row in payload["batches"]
        ),
        payload=payload,
        lifecycle=lifecycle,
        closure=closure,
    )


def assert_append_only(previous: CandidateBatchPlan, current: CandidateBatchPlan) -> None:
    if current.campaign_id != previous.campaign_id:
        raise ValueError("candidate plan campaign identity changed")
    if current.revision < previous.revision:
        raise ValueError("candidate plan revision regressed")
    if current.code_fix_replay_candidate_uid != previous.code_fix_replay_candidate_uid:
        raise ValueError("candidate plan code-fix replay identity changed")
    if current.payload["batches"][: previous.revision] != previous.payload["batches"]:
        raise ValueError("candidate plan rewrote a prior batch")
    if previous.closed and current != previous:
        raise ValueError("closed candidate plan cannot change")


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _replace_with_validated_plan(
    path: Path,
    payload: Mapping[str, Any],
    *,
    previous: CandidateBatchPlan,
) -> CandidateBatchPlan:
    """Validate replacement bytes before atomically publishing a plan revision."""

    candidate = path.with_name(f".{path.name}.{os.getpid()}.validated")
    if candidate.exists() or candidate.is_symlink():
        raise ValueError("candidate plan validation path already exists")
    try:
        _atomic_write(candidate, payload)
        updated = load_plan(candidate, campaign_id=previous.campaign_id)
        assert_append_only(previous, updated)
        if load_plan(path, campaign_id=previous.campaign_id).payload != previous.payload:
            raise ValueError("candidate plan changed during revision validation")
        os.replace(candidate, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return updated
    finally:
        candidate.unlink(missing_ok=True)


def _sha256_digest(value: Any, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA256")
    return value


def initialize_plan(
    path: Path,
    *,
    campaign_id: str,
    batch_size: int = BATCH_SIZE,
) -> CandidateBatchPlan:
    if path.exists() or path.is_symlink():
        raise ValueError("candidate plan already exists")
    if batch_size not in {BATCH_SIZE, V3_BATCH_SIZE}:
        raise ValueError("candidate plan batch size is unsupported")
    _atomic_write(
        path,
        {
            "schema_version": (
                SCHEMA_VERSION if batch_size == BATCH_SIZE else SCHEMA_VERSION_V2
            ),
            "envelope_id": ENVELOPE_ID,
            "campaign_id": campaign_id,
            "revision": 0,
            "batch_size": batch_size,
            "closed": False,
            "batches": [],
        },
    )
    return load_plan(path, campaign_id=campaign_id)


def initialize_r008_plan(path: Path, *, campaign_id: str) -> CandidateBatchPlan:
    if path.exists() or path.is_symlink():
        raise ValueError("candidate plan already exists")
    _atomic_write(
        path,
        {
            "schema_version": SCHEMA_VERSION_R008,
            "envelope_id": ENVELOPE_ID,
            "campaign_id": campaign_id,
            "revision": 0,
            "batch_size": R008_BATCH_SIZE,
            "closed": False,
            "batches": [],
        },
    )
    return load_plan(path, campaign_id=campaign_id)


def initialize_rolling_plan(path: Path, *, campaign_id: str) -> CandidateBatchPlan:
    if path.exists() or path.is_symlink():
        raise ValueError("candidate plan already exists")
    _atomic_write(
        path,
        {
            "schema_version": SCHEMA_VERSION_ROLLING_V2,
            "envelope_id": ENVELOPE_ID,
            "campaign_id": campaign_id,
            "revision": 0,
            "batch_size": R008_BATCH_SIZE,
            "closed": False,
            "lifecycle": PlanLifecycle.OPEN_EMPTY.value,
            "closure": None,
            "batches": [],
        },
    )
    return load_plan(path, campaign_id=campaign_id)


def append_r008_batch(
    path: Path,
    *,
    occurrences: Sequence[Any],
    source: str,
) -> CandidateBatchPlan:
    plan = load_plan(path)
    if plan.payload["schema_version"] not in {
        SCHEMA_VERSION_R008,
        *ROLLING_LIFECYCLE_SCHEMAS,
    }:
        raise ValueError("append_r008_batch requires the rolling schema")
    if plan.closed:
        raise ValueError("candidate plan is closed")
    if (
        plan.payload["schema_version"] in ROLLING_LIFECYCLE_SCHEMAS
        and plan.lifecycle is not PlanLifecycle.OPEN_EMPTY
    ):
        raise ValueError("rolling append requires the durable OPEN_EMPTY transition")
    if len(occurrences) != R008_BATCH_SIZE:
        raise ValueError("r008 append requires exactly 5 occurrences")
    expected_sequence = plan.revision + 1
    rows = []
    for expected_row, occurrence in enumerate(occurrences, start=1):
        if (
            getattr(occurrence, "logical_batch_sequence", None) != expected_sequence
            or getattr(occurrence, "row_index", None) != expected_row
            or getattr(occurrence, "plan_revision", None) != expected_sequence
        ):
            raise ValueError("r008 occurrence sequence/row/revision differs")
        rows.append(
            {
                "candidate": candidate_log2_payload(occurrence.candidate),
                "occurrence_uid": occurrence.occurrence_uid,
                "transport_candidate_uid": occurrence.transport_candidate_uid,
                **(
                    {"control_candidate_uid": occurrence.control_candidate_uid}
                    if plan.payload["schema_version"] in ROLLING_LIFECYCLE_SCHEMAS
                    else {}
                ),
                "role": occurrence.selection_role,
                "replicate_ordinal": occurrence.replicate_ordinal,
            }
        )
    payload = dict(plan.payload)
    batches = list(payload["batches"])
    batches.append(
        {
            "batch_id": expected_sequence,
            **(
                {"plan_revision": expected_sequence}
                if payload["schema_version"] in ROLLING_LIFECYCLE_SCHEMAS
                else {}
            ),
            "source": source,
            "occurrences": rows,
        }
    )
    payload["revision"] = expected_sequence
    payload["batches"] = batches
    if payload["schema_version"] in ROLLING_LIFECYCLE_SCHEMAS:
        payload["lifecycle"] = PlanLifecycle.OPEN_READY.value
    return _replace_with_validated_plan(path, payload, previous=plan)


def mark_rolling_plan_open_empty(path: Path) -> CandidateBatchPlan:
    """Persist that every occurrence in the current revision was consumed."""

    plan = load_plan(path)
    if plan.payload["schema_version"] not in ROLLING_LIFECYCLE_SCHEMAS:
        raise ValueError("OPEN_EMPTY transition requires the rolling-v1 schema")
    if plan.lifecycle is PlanLifecycle.CLOSED_COMPLETE:
        return plan
    if plan.lifecycle is PlanLifecycle.OPEN_EMPTY:
        return plan
    payload = dict(plan.payload)
    payload["lifecycle"] = PlanLifecycle.OPEN_EMPTY.value
    _atomic_write(path, payload)
    updated = load_plan(path, campaign_id=plan.campaign_id)
    assert_append_only(plan, updated)
    return updated


def close_rolling_plan(
    path: Path,
    *,
    reason: str,
    evidence_sha256: str,
) -> CandidateBatchPlan:
    plan = load_plan(path)
    if plan.payload["schema_version"] not in ROLLING_LIFECYCLE_SCHEMAS:
        raise ValueError("typed closure requires the rolling-v1 schema")
    if plan.closed:
        if plan.closure != {"reason": reason, "evidence_sha256": evidence_sha256}:
            raise ValueError("rolling plan already closed with different evidence")
        return plan
    if not isinstance(reason, str) or not reason:
        raise ValueError("rolling closure reason is required")
    _sha256_digest(evidence_sha256, name="closure evidence")
    payload = dict(plan.payload)
    payload["closed"] = True
    payload["lifecycle"] = PlanLifecycle.CLOSED_COMPLETE.value
    payload["closure"] = {"reason": reason, "evidence_sha256": evidence_sha256}
    _atomic_write(path, payload)
    updated = load_plan(path, campaign_id=plan.campaign_id)
    assert_append_only(plan, updated)
    return updated


def append_batch(
    path: Path,
    *,
    candidates: Sequence[ForceCandidate],
    source: str,
) -> CandidateBatchPlan:
    plan = load_plan(path)
    if plan.closed:
        raise ValueError("candidate plan is closed")
    if len(candidates) != plan.batch_size:
        raise ValueError(f"append requires exactly {plan.batch_size} candidates")
    payload = dict(plan.payload)
    batches = list(payload["batches"])
    batches.append(
        {
            "batch_id": plan.revision + 1,
            "source": source,
            "candidates": [candidate_log2_payload(item) for item in candidates],
        }
    )
    payload["revision"] = plan.revision + 1
    payload["batches"] = batches
    _atomic_write(path, payload)
    updated = load_plan(path, campaign_id=plan.campaign_id)
    assert_append_only(plan, updated)
    return updated
