"""Exact 16-row r004 campaign, durable execution IDs, and promotion policy."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from statistics import median
from typing import Any, Callable, Mapping, Sequence

from .contracts import (
    D_ANCHOR,
    I_ON_ANCHOR,
    MAX_ATTEMPTS,
    P_ANCHOR,
    R004Contract,
    TARGET_FORCE_N,
    Candidate,
    assert_target,
    load_contract,
)
from .ledger import DurableCampaignLedger, LedgerError, sha256_mapping, verify_ledger_hash_chain
from .wire import AttemptKind


CAMPAIGN_SCHEMA = "step5d.autotune-v4/r004-campaign-v1"
CAMPAIGN_ID = "step5d_strict_rnn_autotune_v4_r004_campaign"
QUALIFICATION_COUNT = 3
BATCH_A_COUNT = 5
BATCH_B_COUNT = 5
RETEST_COUNT = 3


class CampaignError(RuntimeError):
    pass


@dataclass(frozen=True)
class Attempt:
    ordinal: int
    phase: str
    label: str
    kind: AttemptKind
    candidate: Candidate
    logical_attempt_id: str

    def __post_init__(self) -> None:
        if not 1 <= self.ordinal <= MAX_ATTEMPTS or not self.logical_attempt_id:
            raise CampaignError("attempt ordinal or identity is invalid")
        assert_target(self.candidate)


@dataclass(frozen=True)
class AttemptOutcome:
    status: str = "completed"
    completed: bool = True
    qualification_passed: bool | None = None
    safety_gate_passed: bool = True
    contact_gate_passed: bool = True
    return_gate_passed: bool = True
    complete_bins: int = 550
    effective_rate_hz: float = 100.0
    p99_packet_interval_s: float = 0.010
    max_packet_interval_s: float = 0.020
    mae_n: float | None = None
    objective: float | None = None
    reason: str = ""
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"completed", "interrupted", "failed"}:
            raise CampaignError("outcome status is invalid")
        if not isinstance(self.completed, bool):
            raise CampaignError("outcome completed must be bool")
        for role, value in (
            ("effective_rate_hz", self.effective_rate_hz),
            ("p99_packet_interval_s", self.p99_packet_interval_s),
            ("max_packet_interval_s", self.max_packet_interval_s),
        ):
            if not math.isfinite(float(value)):
                raise CampaignError(f"outcome {role} is nonfinite")
        if self.mae_n is not None and (not math.isfinite(float(self.mae_n)) or self.mae_n < 0.0):
            raise CampaignError("outcome MAE is invalid")
        if self.objective is not None and (not math.isfinite(float(self.objective)) or self.objective < 0.0):
            raise CampaignError("outcome objective is invalid")

    def completion_sha256(self, attempt: Attempt) -> str:
        return sha256_mapping(
            {
                "campaign_id": CAMPAIGN_ID,
                "ordinal": attempt.ordinal,
                "logical_attempt_id": attempt.logical_attempt_id,
                "candidate_uid": attempt.candidate.uid,
                "status": self.status,
                "completed": self.completed,
                "qualification_passed": self.qualification_passed,
                "metrics": dict(self.metrics),
            }
        )


def _candidate(anchor: Candidate, field: str, delta: float) -> Candidate:
    if field not in {"force_p_gain", "force_damping"}:
        raise CampaignError("r004 only permits bounded P/D campaign probes")
    return replace(anchor, **{field: getattr(anchor, field) * (2.0**delta)})


def build_campaign_plan(contract: R004Contract | None = None) -> tuple[Attempt, ...]:
    active = contract or load_contract()
    anchor = Candidate(**dict(active.raw["campaign"]["anchor"]))
    p_minus = _candidate(anchor, "force_p_gain", -0.25)
    p_plus = _candidate(anchor, "force_p_gain", 0.25)
    d_minus = _candidate(anchor, "force_damping", -0.25)
    d_plus = _candidate(anchor, "force_damping", 0.25)
    slots: list[tuple[str, str, AttemptKind, Candidate]] = []
    slots.extend(("QUALIFICATION", "anchor", AttemptKind.QUALIFICATION, anchor) for _ in range(3))
    slots.extend(
        ("BATCH_A", label, AttemptKind.BATCH_A, value)
        for label, value in (("anchor", anchor), ("P-", p_minus), ("anchor", anchor), ("P+", p_plus), ("anchor", anchor))
    )
    slots.extend(
        ("BATCH_B", label, AttemptKind.BATCH_B, value)
        for label, value in (("anchor", anchor), ("D-", d_minus), ("anchor", anchor), ("D+", d_plus), ("anchor", anchor))
    )
    slots.extend(("RETEST", "incumbent", AttemptKind.RETEST, anchor) for _ in range(3))
    result = tuple(
        Attempt(
            ordinal=index,
            phase=phase,
            label=label,
            kind=kind,
            candidate=candidate,
            logical_attempt_id=f"r004-{index:02d}-{phase.lower()}-{label.lower().replace('-', 'minus').replace('+', 'plus')}",
        )
        for index, (phase, label, kind, candidate) in enumerate(slots, start=1)
    )
    validate_campaign_plan(result)
    return result


def validate_campaign_plan(plan: Sequence[Attempt]) -> None:
    if len(plan) != MAX_ATTEMPTS:
        raise CampaignError("r004 plan must contain exactly 16 logical attempts")
    phases = ("QUALIFICATION",) * 3 + ("BATCH_A",) * 5 + ("BATCH_B",) * 5 + ("RETEST",) * 3
    labels = ("anchor",) * 3 + ("anchor", "P-", "anchor", "P+", "anchor") + ("anchor", "D-", "anchor", "D+", "anchor") + ("incumbent",) * 3
    if tuple(attempt.phase for attempt in plan) != phases or tuple(attempt.label for attempt in plan) != labels:
        raise CampaignError("r004 plan order differs")
    if tuple(attempt.ordinal for attempt in plan) != tuple(range(1, MAX_ATTEMPTS + 1)):
        raise CampaignError("r004 plan ordinals are not contiguous")
    if any(attempt.candidate.target_force_n != TARGET_FORCE_N for attempt in plan):
        raise CampaignError("r004 plan target force is mutable")


def _valid_attempt_evidence(row: Mapping[str, Any]) -> bool:
    try:
        effective_rate_hz = float(row.get("effective_rate_hz", 0.0))
        p99_packet_interval_s = float(row.get("p99_packet_interval_s", 1.0))
        max_packet_interval_s = float(row.get("max_packet_interval_s", 1.0))
    except (TypeError, ValueError, OverflowError):
        return False
    return bool(
        row.get("completed") is True
        and row.get("status", "completed") == "completed"
        and row.get("complete_bins") == 550
        and math.isfinite(effective_rate_hz)
        and math.isfinite(p99_packet_interval_s)
        and math.isfinite(max_packet_interval_s)
        and effective_rate_hz >= 75.0
        and p99_packet_interval_s <= 0.020
        and max_packet_interval_s < 0.080
        and row.get("safety_gate_passed") is True
        and row.get("contact_gate_passed") is True
        and row.get("return_gate_passed") is True
        and row.get("gp_eligible") is True
    )


def promotion_decision(rows: Sequence[Mapping[str, Any]], *, anchor_uid: str) -> dict[str, Any]:
    anchor_objectives = [
        float(row["objective"])
        for row in rows
        if row.get("candidate_uid") == anchor_uid and row.get("phase") in {"BATCH_A", "BATCH_B"} and row.get("gp_eligible") is True and isinstance(row.get("objective"), (int, float))
    ]
    candidate_uids = {
        str(row["candidate_uid"])
        for row in rows
        if row.get("phase") in {"BATCH_A", "BATCH_B"} and row.get("candidate_uid") != anchor_uid and row.get("gp_eligible") is True
    }
    retests = [row for row in rows if row.get("phase") == "RETEST"]
    passes = [
        row
        for row in retests
        if _valid_attempt_evidence(row) and isinstance(row.get("mae_n"), (int, float)) and float(row["mae_n"]) <= 0.30
    ]
    by_candidate: dict[str, list[float]] = {}
    for row in passes:
        if isinstance(row.get("objective"), (int, float)):
            by_candidate.setdefault(str(row["candidate_uid"]), []).append(float(row["objective"]))
    candidate_uid = min(by_candidate, key=lambda uid: (median(by_candidate[uid]), uid)) if by_candidate else anchor_uid
    candidate_objectives = by_candidate.get(candidate_uid, [])
    anchor_median = median(anchor_objectives) if anchor_objectives else None
    candidate_median = median(candidate_objectives) if candidate_objectives else None
    allowed = bool(
        candidate_uid != anchor_uid
        and candidate_uid in candidate_uids
        and len(retests) == RETEST_COUNT
        and len(passes) >= 2
        and anchor_median is not None
        and candidate_median is not None
        and candidate_median <= 0.95 * anchor_median
    )
    return {
        "promotion_allowed": allowed,
        "selected_candidate_uid": candidate_uid,
        "anchor_median_objective": anchor_median,
        "candidate_median_objective": candidate_median,
        "retest_pass_count": len(passes),
        "anchor_mutated": False,
        "disposition": "promote_candidate" if allowed else "complete_no_promotion_anchor_retained",
    }


OutcomeProvider = Callable[[Attempt], AttemptOutcome]


class CampaignRunner:
    """Offline runner whose next ARM is gated by the durable ledger."""

    def __init__(self, contract: R004Contract, ledger: DurableCampaignLedger) -> None:
        self.contract = contract
        self.ledger = ledger
        self.plan = build_campaign_plan(contract)

    def run(self, provider: OutcomeProvider | None = None, *, session_epoch: int = 1, controller_receipt_sha256: str = "1" * 64, script1_receipt_sha256: str = "2" * 64, baseline_ledger_sha256: str = "3" * 64) -> tuple[dict[str, Any], ...]:
        rows = list(self.ledger.rows)
        next_ordinal = rows[-1]["logical_attempt_ordinal"] + 1 if rows and rows[-1]["status"] == "completed" else (rows[-1]["logical_attempt_ordinal"] if rows else 1)
        for attempt in self.plan[next_ordinal - 1 :]:
            if rows and rows[-1].get("status") == "interrupted":
                raise CampaignError("resume must explicitly create a new epoch and Script 1 receipt")
            if not self.ledger.next_arm_allowed():
                raise CampaignError("next ARM forbidden until row durability proof completes")
            outcome = provider(attempt) if provider is not None else AttemptOutcome(
                qualification_passed=(True if attempt.phase == "QUALIFICATION" else None),
                mae_n=(0.2 if attempt.phase == "RETEST" else None),
                objective=(1.0 if attempt.phase != "QUALIFICATION" else None),
            )
            if not isinstance(outcome, AttemptOutcome):
                raise CampaignError("provider must return typed AttemptOutcome")
            completed_evidence = _valid_attempt_evidence(
                {
                    "completed": outcome.completed,
                    "complete_bins": outcome.complete_bins,
                    "effective_rate_hz": outcome.effective_rate_hz,
                    "p99_packet_interval_s": outcome.p99_packet_interval_s,
                    "max_packet_interval_s": outcome.max_packet_interval_s,
                    "safety_gate_passed": outcome.safety_gate_passed,
                    "contact_gate_passed": outcome.contact_gate_passed,
                    "return_gate_passed": outcome.return_gate_passed,
                    "gp_eligible": True,
                }
            )
            qualification_passed = outcome.qualification_passed
            if qualification_passed is None and attempt.phase == "QUALIFICATION":
                qualification_passed = outcome.completed and not outcome.reason
            row = {
                "record_type": "attempt",
                "logical_attempt_ordinal": attempt.ordinal,
                "logical_attempt_id": attempt.logical_attempt_id,
                "phase": attempt.phase,
                "label": attempt.label,
                "kind": attempt.kind.name,
                "session_epoch": session_epoch,
                "attempt_execution_id": f"r004-epoch{session_epoch}-ordinal{attempt.ordinal}",
                "controller_receipt_sha256": controller_receipt_sha256,
                "script1_receipt_sha256": script1_receipt_sha256,
                "input_baseline_ledger_sha256": baseline_ledger_sha256,
                "status": outcome.status,
                "completed": outcome.completed,
                "qualification_passed": qualification_passed,
                "candidate_uid": attempt.candidate.uid,
                "candidate": attempt.candidate.canonical,
                "target_force_n": TARGET_FORCE_N,
                "damping": D_ANCHOR,
                "complete_bins": outcome.complete_bins,
                "effective_rate_hz": outcome.effective_rate_hz,
                "p99_packet_interval_s": outcome.p99_packet_interval_s,
                "max_packet_interval_s": outcome.max_packet_interval_s,
                "safety_gate_passed": outcome.safety_gate_passed,
                "contact_gate_passed": outcome.contact_gate_passed,
                "return_gate_passed": outcome.return_gate_passed,
                "mae_n": outcome.mae_n,
                "objective": outcome.objective,
                "reason": outcome.reason,
                "gp_eligible": bool(attempt.phase != "QUALIFICATION" and completed_evidence and outcome.status == "completed" and not outcome.reason),
                "output_seal": {"completion_sha256": outcome.completion_sha256(attempt)},
                "durable_row": True,
                "durability": {"fsynced": True, "cold_read": True, "hash_verified": True},
            }
            receipt = self.ledger.append_attempt(row)
            if not receipt.ready_for_next_arm:
                raise CampaignError("durability receipt is not ready for next ARM")
            rows = list(self.ledger.rows)
            if outcome.status != "completed":
                break
        verify_ledger_hash_chain(self.ledger.path) if self.ledger.path.exists() else None
        return tuple(self.ledger.rows)


__all__ = [
    "Attempt",
    "AttemptOutcome",
    "CAMPAIGN_ID",
    "CAMPAIGN_SCHEMA",
    "CampaignError",
    "CampaignRunner",
    "build_campaign_plan",
    "promotion_decision",
    "validate_campaign_plan",
]
