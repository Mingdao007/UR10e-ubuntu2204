#!/usr/bin/env python3
"""Atomic Codex-managed hybrid I-scale/log2 batches for Step5d autotune."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_contract import ForceCandidate, LOG2_LATTICE_OCTAVE


SCHEMA_VERSION = "step5d_autotune_codex_batch_plan_v1"
SCHEMA_VERSION_V2 = "step5d_autotune_codex_batch_plan_v2"
ENVELOPE_ID = "positive_i_multiplier_coarse_log2_fine_v1"
BATCH_SIZE = 5
V3_BATCH_SIZE = 10


@dataclass(frozen=True)
class CandidateBatchPlan:
    campaign_id: str
    revision: int
    closed: bool
    code_fix_replay_candidate_uid: str | None
    batch_size: int
    batches: tuple[tuple[ForceCandidate, ...], ...]
    payload: Mapping[str, Any]

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
    if set(payload) == fine_fields:
        return ForceCandidate.from_log2(
            p=_coordinate("log2_p", payload["log2_p"]),
            i=_coordinate("log2_i", payload["log2_i"]),
            damping=_coordinate("log2_damping", payload["log2_damping"]),
        )
    if set(payload) == scale_fields:
        multiplier = payload["i_multiplier"]
        if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
            raise ValueError("i_multiplier must be numeric")
        return ForceCandidate.from_i_multiplier(
            p=_coordinate("log2_p", payload["log2_p"]),
            i_multiplier=float(multiplier),
            damping=_coordinate("log2_damping", payload["log2_damping"]),
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
    if not isinstance(payload, Mapping) or not (
        set(payload) == required or set(payload) == required | optional
    ):
        raise ValueError("candidate plan schema is incomplete")
    if payload["schema_version"] not in {SCHEMA_VERSION, SCHEMA_VERSION_V2}:
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
    expected_batch_size = (
        BATCH_SIZE if payload["schema_version"] == SCHEMA_VERSION else V3_BATCH_SIZE
    )
    if payload["batch_size"] != expected_batch_size or type(payload["closed"]) is not bool:
        raise ValueError("candidate plan batch policy differs")
    if not isinstance(payload["batches"], list):
        raise ValueError("candidate plan batches must be a list")

    batches: list[tuple[ForceCandidate, ...]] = []
    seen: set[str] = set()
    for expected_id, row in enumerate(payload["batches"], start=1):
        if not isinstance(row, Mapping) or set(row) != {
            "batch_id",
            "source",
            "candidates",
        }:
            raise ValueError("candidate batch schema differs")
        if row["batch_id"] != expected_id:
            raise ValueError("candidate batch ids must be contiguous")
        if not isinstance(row["source"], str) or not row["source"].strip():
            raise ValueError("candidate batch source is invalid")
        candidate_count = (
            len(row["candidates"])
            if isinstance(row["candidates"], list)
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
        candidates = tuple(candidate_from_log2_payload(item) for item in row["candidates"])
        for candidate in candidates:
            if candidate.candidate_uid in seen:
                raise ValueError("candidate plan repeats an exact parameter set")
            seen.add(candidate.candidate_uid)
        batches.append(candidates)
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
    return CandidateBatchPlan(
        campaign_id=payload["campaign_id"],
        revision=payload["revision"],
        closed=payload["closed"],
        code_fix_replay_candidate_uid=replay_uid,
        batch_size=expected_batch_size,
        batches=tuple(batches),
        payload=payload,
    )


def assert_append_only(previous: CandidateBatchPlan, current: CandidateBatchPlan) -> None:
    if current.campaign_id != previous.campaign_id:
        raise ValueError("candidate plan campaign identity changed")
    if current.revision < previous.revision:
        raise ValueError("candidate plan revision regressed")
    if current.code_fix_replay_candidate_uid != previous.code_fix_replay_candidate_uid:
        raise ValueError("candidate plan code-fix replay identity changed")
    if current.batches[: previous.revision] != previous.batches:
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
