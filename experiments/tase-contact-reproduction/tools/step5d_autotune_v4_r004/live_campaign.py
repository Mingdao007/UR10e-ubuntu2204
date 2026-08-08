"""Live r004 campaign handshake over the isolated writer boundary."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from .campaign import Attempt, CampaignError, build_campaign_plan
from .contracts import R004Contract
from .evidence import AttemptEvidence, QualificationEvidence
from .ledger import DurableCampaignLedger, LedgerError, sha256_mapping, verify_ledger_hash_chain
from .wire import AttemptKind


class LiveCampaignWriter(Protocol):
    @property
    def controller_receipt_sha256(self) -> str: ...
    @property
    def script1_receipt_sha256(self) -> str: ...
    @property
    def input_baseline_ledger_sha256(self) -> str: ...
    @property
    def session_epoch(self) -> int: ...
    def arm(self, *, ordinal: int, kind: AttemptKind, candidate_token: int, ledger: DurableCampaignLedger, resume: bool = False) -> Any: ...
    def execute_attempt(self, attempt: Attempt, *, timeout_s: float = 180.0) -> AttemptEvidence | QualificationEvidence: ...
    def bind_committed_qualifications(self, ledger: DurableCampaignLedger) -> int: ...
    def commit_qualification_success(self, *, ordinal: int, ledger: DurableCampaignLedger) -> None: ...


@dataclass(frozen=True)
class LiveCampaignResult:
    rows: tuple[dict[str, Any], ...]
    promotion: dict[str, Any]
    resumed: bool
    qualification_only: bool = False
    full_campaign_blocked: bool = True
    qualification_complete: bool = False


class LiveCampaignRunner:
    """The ledger is the only authority that permits the next ARM."""

    def __init__(self, contract: R004Contract, ledger: DurableCampaignLedger, writer: LiveCampaignWriter) -> None:
        self.contract = contract
        self.ledger = ledger
        self.writer = writer
        self.plan = build_campaign_plan(contract)

    @staticmethod
    def _token(attempt: Attempt) -> int:
        return max(1, int(attempt.candidate.uid[:8], 16) & 0x7FFFFFFF)

    def _row(
        self,
        attempt: Attempt,
        *,
        execution_id: str,
        evidence: AttemptEvidence | QualificationEvidence | None,
        status: str,
        completed: bool,
        reason: str = "",
    ) -> dict[str, Any]:
        if status not in {"completed", "interrupted", "failed"}:
            raise CampaignError("live row status is invalid")
        qualification_evidence = evidence if isinstance(evidence, QualificationEvidence) else None
        path_evidence = evidence if isinstance(evidence, AttemptEvidence) else None
        evidence_passed = bool(
            evidence is not None
            and (
                evidence.eligible
                if isinstance(evidence, (AttemptEvidence, QualificationEvidence))
                else False
            )
        )
        successful = bool(evidence_passed and status == "completed" and completed)
        completion = sha256_mapping(
            {
                "ordinal": attempt.ordinal,
                "execution_id": execution_id,
                "status": status,
                "completed": completed,
                "evidence_sha256": None if evidence is None else evidence.evidence_sha256,
            }
        )
        row: dict[str, Any] = {
            "record_type": "attempt",
            "logical_attempt_ordinal": attempt.ordinal,
            "logical_attempt_id": attempt.logical_attempt_id,
            "phase": attempt.phase,
            "label": attempt.label,
            "kind": attempt.kind.name,
            "session_epoch": self.writer.session_epoch,
            "attempt_execution_id": execution_id,
            "controller_receipt_sha256": self.writer.controller_receipt_sha256,
            "script1_receipt_sha256": self.writer.script1_receipt_sha256,
            "input_baseline_ledger_sha256": self.writer.input_baseline_ledger_sha256,
            "status": status,
            "completed": completed,
            "qualification_passed": successful if attempt.phase == "QUALIFICATION" else None,
            "candidate_uid": attempt.candidate.uid,
            "candidate": attempt.candidate.canonical,
            "target_force_n": 5.0,
            "damping": 28.0,
            "complete_bins": 0 if path_evidence is None else path_evidence.complete_bins,
            "effective_rate_hz": 0.0 if evidence is None else evidence.effective_rate_hz,
            "p99_packet_interval_s": 1.0 if evidence is None else evidence.p99_packet_interval_s,
            "max_packet_interval_s": 1.0 if evidence is None else evidence.max_packet_interval_s,
            "safety_gate_passed": False if evidence is None else evidence.safety_gate_passed,
            "contact_gate_passed": False if evidence is None else evidence.contact_gate_passed,
            "return_gate_passed": False if evidence is None else evidence.return_gate_passed,
            "mae_n": None if path_evidence is None else path_evidence.mae_n,
            "objective": None if path_evidence is None else path_evidence.objective,
            "motion_gate_passed": False if path_evidence is None else path_evidence.motion_gate_passed,
            "path_duration_s": None if path_evidence is None else path_evidence.path_duration_s,
            "path_phase": None if path_evidence is None else path_evidence.path_phase,
            "xy_error_p95_m": None if path_evidence is None else path_evidence.xy_error_p95_m,
            "xy_error_max_m": None if path_evidence is None else path_evidence.xy_error_max_m,
            "endpoint_error_max_m": None if path_evidence is None else path_evidence.endpoint_error_max_m,
            "qd_correlation": None if path_evidence is None else path_evidence.qd_correlation,
            "qd_lag_s": None if path_evidence is None else path_evidence.qd_lag_s,
            "timing_evidence": (
                path_evidence.timing_evidence.as_dict()
                if path_evidence is not None and path_evidence.timing_evidence is not None
                else (
                    qualification_evidence.timing_evidence.as_dict()
                    if qualification_evidence is not None and qualification_evidence.timing_evidence is not None
                    else None
                )
            ),
            "reason": reason,
            "gp_eligible": bool(
                successful
                and attempt.phase != "QUALIFICATION"
                and evidence is not None
                and evidence.safety_gate_passed
                and evidence.contact_gate_passed
                and evidence.return_gate_passed
            ),
            "path_evidence_sha256": None if path_evidence is None else path_evidence.evidence_sha256,
            "qualification_evidence_sha256": None if qualification_evidence is None else qualification_evidence.evidence_sha256,
            "home_proof": {} if evidence is None else dict(evidence.home_proof),
            "metrics": {} if evidence is None else dict(evidence.metrics),
            "output_seal": {"completion_sha256": completion},
        }
        if isinstance(evidence, (AttemptEvidence, QualificationEvidence)):
            row["metrics"]["timing_gate_passed"] = evidence.timing_gate_passed
        if path_evidence is not None:
            row["metrics"].update(
                {
                    "motion_gate_passed": path_evidence.motion_gate_passed,
                    "path_duration_s": path_evidence.path_duration_s,
                    "path_phase": path_evidence.path_phase,
                    "xy_error_p95_m": path_evidence.xy_error_p95_m,
                    "xy_error_max_m": path_evidence.xy_error_max_m,
                    "endpoint_error_max_m": path_evidence.endpoint_error_max_m,
                    "qd_correlation": path_evidence.qd_correlation,
                    "qd_lag_s": path_evidence.qd_lag_s,
                }
            )
        return row

    def _append(self, row: dict[str, Any]) -> None:
        receipt = self.ledger.append_attempt(row)
        if not receipt.ready_for_next_arm:
            raise CampaignError("durable row is not ready for next ARM")
        verify_ledger_hash_chain(self.ledger.path)

    def _run_ordinals(self, ordinals: list[int], *, first_execution_id: str | None = None, timeout_s: float = 180.0, resumed: bool = False) -> LiveCampaignResult:
        if any(not 1 <= ordinal <= 16 for ordinal in ordinals):
            raise CampaignError("r004 campaign ordinal is outside 1..16")
        for ordinal in ordinals:
            attempt = self.plan[ordinal - 1]
            committed_qualifications = self.writer.bind_committed_qualifications(self.ledger)
            if ordinal <= 3 and committed_qualifications != ordinal - 1:
                raise CampaignError("qualification ordinals must commit contiguously 1..3")
            if ordinal >= 4 and committed_qualifications != 3:
                raise CampaignError("PATH attempt requires three durable qualification successes")
            is_resume_arm = resumed and ordinal == ordinals[0]
            if not self.ledger.next_arm_allowed() and not is_resume_arm:
                raise CampaignError("next ARM forbidden until fsync/cold-read/hash verification")
            execution_id = first_execution_id if first_execution_id and ordinal == ordinals[0] else f"r004-e{self.writer.session_epoch}-o{ordinal}-{uuid.uuid4().hex}"
            durable_completion_appended = False
            try:
                self.writer.arm(
                    ordinal=ordinal,
                    kind=attempt.kind,
                    candidate_token=self._token(attempt),
                    ledger=self.ledger,
                    resume=is_resume_arm,
                )
                evidence = self.writer.execute_attempt(attempt, timeout_s=timeout_s)
                row = self._row(attempt, execution_id=execution_id, evidence=evidence, status="completed", completed=True)
                self._append(row)
                durable_completion_appended = True
                if attempt.phase == "QUALIFICATION":
                    self.writer.commit_qualification_success(ordinal=ordinal, ledger=self.ledger)
            except Exception as exc:
                # A completed row is immutable once fsynced and cold-read.
                # Never reuse its execution id for a second interrupted row;
                # preserve the original post-commit error for diagnosis.
                if durable_completion_appended:
                    raise CampaignError(
                        f"post-completion qualification commit failed: {exc}"
                    ) from exc
                interrupted = self._row(
                    attempt,
                    execution_id=execution_id,
                    evidence=None,
                    status="interrupted",
                    completed=False,
                    reason=f"interrupted:{type(exc).__name__}:{exc}",
                )
                self._append(interrupted)
                raise CampaignError(str(exc)) from exc
        rows = tuple(self.ledger.rows)
        completed = {
            int(row["logical_attempt_ordinal"])
            for row in rows
            if row.get("phase") == "QUALIFICATION"
            and row.get("status") == "completed"
            and row.get("qualification_passed") is True
        }
        return LiveCampaignResult(
            rows=rows,
            promotion={
                "promotion_allowed": False,
                "disposition": "campaign_complete" if len({int(row["logical_attempt_ordinal"]) for row in rows if row.get("status") == "completed"}) == 16 else "campaign_in_progress",
                "reason": "promotion remains fail-closed until all bounded campaign evidence is complete",
            },
            resumed=resumed,
            qualification_only=bool(ordinals and max(ordinals) <= 3),
            full_campaign_blocked=False,
            qualification_complete=completed == {1, 2, 3},
        )

    def run(self, *, timeout_s: float = 180.0, stop_after_ordinal: int = 16) -> LiveCampaignResult:
        if not 1 <= stop_after_ordinal <= 16:
            raise CampaignError("stop_after_ordinal must be within 1..16")
        rows = list(self.ledger.rows)
        if rows and rows[-1].get("status") == "interrupted":
            raise CampaignError("interrupted execution requires explicit new epoch/Script1 resume")
        next_ordinal = rows[-1]["logical_attempt_ordinal"] + 1 if rows else 1
        if next_ordinal > stop_after_ordinal:
            return self._run_ordinals([], timeout_s=timeout_s)
        return self._run_ordinals(
            list(range(next_ordinal, stop_after_ordinal + 1)),
            timeout_s=timeout_s,
        )

    def resume(
        self,
        *,
        new_epoch: int,
        new_script1_receipt_sha256: str,
        timeout_s: float = 180.0,
        stop_after_ordinal: int = 16,
    ) -> LiveCampaignResult:
        request = self.ledger.resume_after_interruption(
            new_epoch=new_epoch,
            script1_receipt_sha256=new_script1_receipt_sha256,
        )
        if self.writer.session_epoch != new_epoch or self.writer.script1_receipt_sha256 != new_script1_receipt_sha256:
            raise CampaignError("resume writer is not bound to the required new epoch/Script1 receipt")
        if not request.logical_attempt_ordinal <= stop_after_ordinal <= 16:
            raise CampaignError("resume stop_after_ordinal must include the interrupted ordinal and remain within 1..16")
        return self._run_ordinals(
            list(range(request.logical_attempt_ordinal, stop_after_ordinal + 1)),
            first_execution_id=request.new_attempt_execution_id,
            timeout_s=timeout_s,
            resumed=True,
        )


__all__ = ["LiveCampaignResult", "LiveCampaignRunner", "LiveCampaignWriter"]
