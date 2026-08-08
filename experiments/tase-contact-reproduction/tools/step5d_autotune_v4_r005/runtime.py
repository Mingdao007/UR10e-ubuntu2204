"""Long-lived offline host campaign loop for Autotune V4 r005."""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
import uuid
from typing import Any, Mapping, Protocol, Sequence

from .alignment import AlignedJointVelocity
from .contracts import Candidate, R005Contract, bootstrap_pd_candidates, validate_transition
from step5d_force_objective import ForceObjective, ForcePathSample
from .observations import ObservationLedger, ObservationRecord
from .optimizer import OptimizerError, OptimizerUnavailable, V4BoAdapter
from .queue import DispatchTicket, QueueError, QueuePort


MAE_THRESHOLD_N = 0.20
RETTEST_IMPROVEMENT = 0.95


class RuntimeErrorR005(RuntimeError):
    """A runtime phase, identity, safety, or evidence transition failed."""


class CampaignPhase(str, Enum):
    QUALIFICATION = "qualification"
    BOOTSTRAP = "bootstrap"
    BO = "bo"
    RETEST = "retest"
    COMPLETE = "complete"
    INCOMPLETE_STOPPED = "incomplete_stopped"


class AttemptOutcome(str, Enum):
    """The only four durable dispositions of a returned attempt."""

    OBJECTIVE = "OBJECTIVE"
    SAFE_NONTRAINABLE = "SAFE_NONTRAINABLE"
    CODE_OR_EVIDENCE_BUG = "CODE_OR_EVIDENCE_BUG"
    SAFETY_OR_RETURN_FAILURE = "SAFETY_OR_RETURN_FAILURE"


@dataclass(frozen=True)
class Attempt:
    epoch: int
    attempt_sequence: int
    candidate: Candidate
    kind: str
    dispatch_sequence: int
    request_uid: str
    execution_id: str = ""
    logical_request_uid: str | None = None

    @property
    def logical_identity(self) -> str:
        return self.logical_request_uid or self.request_uid


@dataclass(frozen=True)
class AttemptResult:
    """Raw returned evidence before it is sealed into the observation ledger."""

    epoch: int
    attempt_sequence: int
    kind: str
    candidate: Candidate
    safe_return: bool
    binding_ok: bool
    safety_gate: bool
    contact_gate: bool
    return_gate: bool
    motion_gate: bool
    timing_gate: bool
    identity_gate: bool
    qualification_passed: bool = False
    duration_s: float = 60.0
    force_objective: ForceObjective | None = None
    alignment_ok: bool = True
    metrics: Mapping[str, Any] = field(default_factory=dict)
    execution_id: str = ""
    joint_evidence: AlignedJointVelocity | None = None
    raw_path_samples: tuple[ForcePathSample, ...] = ()

    def __post_init__(self) -> None:
        for name in ("epoch", "attempt_sequence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RuntimeErrorR005(f"attempt result {name} is invalid")
        if not isinstance(self.kind, str) or not self.kind:
            raise RuntimeErrorR005("attempt result kind is invalid")
        if not isinstance(self.candidate, Candidate):
            raise RuntimeErrorR005("attempt result candidate is not typed")
        if not isinstance(self.execution_id, str):
            raise RuntimeErrorR005("attempt result execution_id is invalid")
        if self.joint_evidence is not None and not isinstance(
            self.joint_evidence, AlignedJointVelocity
        ):
            raise RuntimeErrorR005("attempt joint evidence is not typed")
        raw_path_samples = tuple(self.raw_path_samples)
        if any(not isinstance(sample, ForcePathSample) for sample in raw_path_samples):
            raise RuntimeErrorR005("attempt raw PATH evidence is not typed")
        for name in (
            "safe_return",
            "binding_ok",
            "safety_gate",
            "contact_gate",
            "return_gate",
            "motion_gate",
            "timing_gate",
            "identity_gate",
            "qualification_passed",
            "alignment_ok",
        ):
            if not isinstance(getattr(self, name), bool):
                raise RuntimeErrorR005(f"attempt result {name} is not bool")
        duration = float(self.duration_s)
        if not math.isfinite(duration) or duration < 0.0:
            raise RuntimeErrorR005("attempt duration is invalid")
        if self.force_objective is not None and not isinstance(self.force_objective, ForceObjective):
            raise RuntimeErrorR005("attempt force objective is not typed")
        object.__setattr__(self, "duration_s", duration)
        object.__setattr__(self, "metrics", dict(self.metrics))
        object.__setattr__(self, "raw_path_samples", raw_path_samples)

    @property
    def mae_n(self) -> float | None:
        return self.force_objective.mae_n if self.force_objective is not None else None

    @property
    def objective(self) -> float | None:
        return self.force_objective.objective if self.force_objective is not None else None

    @property
    def eligible(self) -> bool:
        """Pre-ledger eligibility shape; motion quality is diagnostic/final-only.

        A ForceObjective is advisory here, so production eligibility remains
        false until ObservationLedger verifies the immutable raw artifact.
        """

        return bool(
            self.safe_return
            and self.binding_ok
            and self.safety_gate
            and self.contact_gate
            and self.return_gate
            and self.timing_gate
            and self.identity_gate
            and self.alignment_ok
            and self.joint_evidence is not None
            and self.duration_s >= 60.0
            and self.force_objective is not None
            and self.force_objective.trainable
        )

    @property
    def outcome(self) -> AttemptOutcome:
        if (
            not self.binding_ok
            or not self.alignment_ok
            or (self.kind != "QUALIFICATION" and self.joint_evidence is None)
        ):
            return AttemptOutcome.CODE_OR_EVIDENCE_BUG
        if not self.safety_gate or not self.safe_return or not self.return_gate:
            return AttemptOutcome.SAFETY_OR_RETURN_FAILURE
        if self.eligible:
            return AttemptOutcome.OBJECTIVE
        return AttemptOutcome.SAFE_NONTRAINABLE

    def to_record(self, campaign_fingerprint: str) -> ObservationRecord:
        metrics = dict(self.metrics)
        metrics.update(
            {
                "alignment_ok": self.alignment_ok,
                "outcome": self.outcome.value,
                # The attempt identity is owned by HostLoop/Queue and cannot
                # be overridden by a runtime metrics mapping.
                "execution_id": self.execution_id,
            }
        )
        if self.joint_evidence is not None:
            metrics["joint_velocity_binding"] = self.joint_evidence.as_dict()
        return ObservationRecord(
            campaign_fingerprint=campaign_fingerprint,
            epoch=self.epoch,
            attempt_sequence=self.attempt_sequence,
            kind=self.kind,
            candidate=self.candidate,
            safe_return=self.safe_return,
            binding_ok=self.binding_ok,
            safety_gate=self.safety_gate,
            contact_gate=self.contact_gate,
            return_gate=self.return_gate,
            motion_gate=self.motion_gate,
            timing_gate=self.timing_gate,
            identity_gate=self.identity_gate,
            qualification_passed=self.qualification_passed,
            duration_s=self.duration_s,
            # A complete objective is still only advisory here.  The ledger
            # must compare it with a fresh rebuild from raw_path_samples and
            # is the only seam allowed to mark it optimizer-trainable.
            force_objective=(
                None if self.kind == "QUALIFICATION" else self.force_objective
            ),
            metrics=metrics,
            raw_path_samples=(
                () if self.kind == "QUALIFICATION" else self.raw_path_samples
            ),
        )


class RuntimePort(Protocol):
    def home(self) -> None: ...

    def dispatch(self, attempt: Attempt, ticket: DispatchTicket) -> None: ...

    def arm(self, attempt: Attempt) -> None: ...

    def run_60s(self, attempt: Attempt) -> AttemptResult: ...

    def safe_return(self, attempt: Attempt, result: AttemptResult) -> AttemptResult: ...

    def revoke_authority(self, reason: str) -> None: ...


class HostLoop:
    """One long-lived HOME -> dispatch -> ARM -> return -> seal/tell loop."""

    def __init__(
        self,
        *,
        contract: R005Contract,
        queue: QueuePort,
        ledger: ObservationLedger,
        optimizer: V4BoAdapter,
        runtime: RuntimePort,
    ) -> None:
        self.contract = contract
        self.queue = queue
        self.ledger = ledger
        self.optimizer = optimizer
        self.runtime = runtime
        self.events: list[str] = []
        self._phase_origin_s: float | None = None
        self._phase_last_s: float | None = None
        self.authority_revoked = False
        self.stop_reason: str | None = None
        self.phase = CampaignPhase.QUALIFICATION
        self.epoch = 0
        self.next_attempt_sequence = 1
        self.cursor = Candidate()
        self.qualification_passes = 0
        self.bootstrap_index = 0
        self.retest_candidate: Candidate | None = None
        self.retest_enqueued = 0
        self.retest_completed: list[ObservationRecord] = []
        self._resume_from_ledger()
        self._sync_qualification_state()

    def _sync_qualification_state(self) -> None:
        method = getattr(self.runtime, "sync_qualification_passes", None)
        if method is not None:
            method(self.qualification_passes)

    @property
    def terminal(self) -> bool:
        return self.phase in {
            CampaignPhase.COMPLETE,
            CampaignPhase.INCOMPLETE_STOPPED,
        }

    @property
    def status(self) -> str:
        return self.phase.value

    def _resume_from_ledger(self) -> None:
        # This is deliberately an r005-only cold read.  Passing an r004 file
        # fails in ObservationLedger before this method can import anything.
        self.ledger.fresh_process_verify()
        records = self.ledger.records
        queue_sequence = int(getattr(self.queue, "last_attempt_sequence", 0))
        if not records:
            # A crash can occur after durable ATTEMPT_EXECUTION and before the
            # first sealed observation.  The next execution must still advance
            # beyond that queue high-water mark while retaining the same
            # logical request identity after Home reconciliation.
            self.next_attempt_sequence = max(1, queue_sequence + 1)
            return
        self.epoch = max(record.epoch for record in records)
        self.next_attempt_sequence = max(
            max(record.attempt_sequence for record in records), queue_sequence
        ) + 1
        current = [record for record in records if record.epoch == self.epoch]
        self.qualification_passes = sum(
            1 for record in current if record.qualification_eligible
        )
        self.bootstrap_index = sum(
            1 for record in current if record.kind == "BOOTSTRAP_PD"
        )
        self.cursor = current[-1].candidate
        if self.qualification_passes < 3:
            self.phase = CampaignPhase.QUALIFICATION
        elif self.bootstrap_index < len(bootstrap_pd_candidates()):
            self.phase = CampaignPhase.BOOTSTRAP
        else:
            self.phase = CampaignPhase.BO
        self.events.append("RESUME_COLD_READ_VERIFIED")

    def start_epoch(self) -> int:
        if self.terminal:
            raise RuntimeErrorR005("cannot start an epoch after terminal state")
        if self.epoch == 0:
            self.epoch = 1
        else:
            self.epoch += 1
            self.qualification_passes = 0
            self.bootstrap_index = 0
            self.cursor = Candidate()
        self.phase = CampaignPhase.QUALIFICATION
        self.retest_candidate = None
        self.retest_enqueued = 0
        self.retest_completed.clear()
        self.events.append(f"EPOCH:{self.epoch}:FRESH_QUALIFICATIONS")
        binder = getattr(self.queue, "bind_home", None)
        if binder is not None:
            binder(campaign_epoch=self.epoch, last_trial_id=0, last_command_seq=0)
        return self.epoch

    def _stop(self, reason: str) -> None:
        if self.terminal:
            return
        self.stop_reason = reason
        self.phase = CampaignPhase.INCOMPLETE_STOPPED
        if not self.authority_revoked:
            self.authority_revoked = True
            self.events.append(f"AUTHORITY_REVOKED:{reason}")
            try:
                self.runtime.revoke_authority(reason)
            except Exception:
                # The original safety/return failure remains the authoritative
                # stop reason; a revoke reporting error cannot resume the loop.
                self.events.append("AUTHORITY_REVOKE_REPORT_FAILED")
        self.events.append(f"STOPPED:{reason}")

    def _begin_phase_clock(self) -> None:
        """Reset per-attempt wall-clock origin for timed host phase events."""

        now = time.perf_counter()
        self._phase_origin_s = now
        self._phase_last_s = now

    def _phase(self, name: str) -> None:
        """Append ``NAME:t+<elapsed>:dt=<delta>`` for offline/live residual splits."""

        now = time.perf_counter()
        if self._phase_origin_s is None:
            self._phase_origin_s = now
        if self._phase_last_s is None:
            self._phase_last_s = now
        elapsed = now - self._phase_origin_s
        delta = now - self._phase_last_s
        self._phase_last_s = now
        self.events.append(f"{name}:t+{elapsed:.3f}:dt={delta:.3f}")

    def _record_and_tell(self, result: AttemptResult) -> ObservationRecord:
        record = self.ledger.append(result.to_record(self.contract.campaign_fingerprint))
        self._phase("SEAL")
        if record.eligible:
            self.optimizer.tell(record)
            self._phase("TELL")
        return record

    def _queue_request(
        self,
        candidate: Candidate,
        *,
        kind: str,
        base: Candidate | None = None,
    ) -> None:
        transition_base = self.cursor if base is None else base
        try:
            validate_transition(transition_base, candidate)
        except Exception as exc:
            raise RuntimeErrorR005(
                "host refill attempted a transition outside the one-coordinate 0.25-octave contract"
            ) from exc
        self.queue.enqueue(candidate, kind=kind, epoch=self.epoch)
        self.events.append(f"ENQUEUE:{kind}")

    def _refill(self) -> None:
        if self.terminal:
            return
        if self.epoch <= 0:
            raise RuntimeErrorR005("refill requires a started epoch")
        if self.queue.inflight is not None:
            raise RuntimeErrorR005("refill cannot run with a physical inflight row")
        while len(self.queue.pending()) < 2:
            if self.phase == CampaignPhase.QUALIFICATION:
                # Qualification repeats are intentional anchor replicates.  A
                # Fill the bounded queue with fresh anchor identities; the
                # three-pass gate counts sealed successes, not enqueued rows.
                pending = self.queue.pending()
                queued = sum(item.kind == "QUALIFICATION" for item in pending)
                if self.qualification_passes + queued >= 3:
                    break
                base = pending[-1].candidate if pending else self.cursor
                self._queue_request(Candidate(), kind="QUALIFICATION", base=base)
                continue
            if self.phase == CampaignPhase.BOOTSTRAP:
                bootstrap = bootstrap_pd_candidates()
                pending = self.queue.pending()
                next_index = self.bootstrap_index + sum(
                    item.kind == "BOOTSTRAP_PD" for item in pending
                )
                if next_index >= len(bootstrap):
                    self.phase = CampaignPhase.BO
                    continue
                base = pending[-1].candidate if pending else self.cursor
                self._queue_request(bootstrap[next_index], kind="BOOTSTRAP_PD", base=base)
                continue
            if self.phase == CampaignPhase.RETEST:
                if self.retest_candidate is None:
                    raise RuntimeErrorR005("retest phase lacks incumbent")
                if self.retest_enqueued >= 3:
                    break
                pending = self.queue.pending()
                base = pending[-1].candidate if pending else self.cursor
                self._queue_request(self.retest_candidate, kind="RETEST", base=base)
                self.retest_enqueued += 1
                continue
            if self.phase == CampaignPhase.BO:
                try:
                    ask = self.optimizer.ask(
                        observations=self.ledger.records,
                        pending=tuple(item.candidate for item in self.queue.pending()),
                        incumbent=(
                            self.queue.pending()[-1].candidate
                            if self.queue.pending()
                            else self.cursor
                        ),
                    )
                except OptimizerUnavailable as exc:
                    self._stop(f"optimizer_unavailable:{exc}")
                    return
                except OptimizerError as exc:
                    self._stop(f"optimizer_binding_or_backend_error:{exc}")
                    return
                pending = self.queue.pending()
                base = pending[-1].candidate if pending else self.cursor
                self._queue_request(ask.candidate, kind="BO_TRIAL", base=base)
                continue
            raise RuntimeErrorR005(f"unknown host phase {self.phase}")
        self.events.append(f"REFILL:PENDING={len(self.queue.pending())}")

    def _validate_identity(self, attempt: Attempt, result: AttemptResult) -> None:
        if (
            result.epoch != attempt.epoch
            or result.attempt_sequence != attempt.attempt_sequence
            or result.kind != attempt.kind
            or result.candidate.candidate_uid != attempt.candidate.candidate_uid
            or result.execution_id != attempt.execution_id
        ):
            raise RuntimeErrorR005("code/evidence binding identity differs")
        if not result.binding_ok or not result.alignment_ok:
            raise RuntimeErrorR005(
                "code/evidence binding or qdot/actual_qd alignment failed"
            )
        if attempt.kind != "QUALIFICATION" and result.joint_evidence is None:
            raise RuntimeErrorR005(
                "PATH code/evidence binding lacks qdot/actual_qd alignment"
            )

    def _start_retests(self, candidate: Candidate) -> None:
        self.queue.cancel_pending(reason="eligible_mae_threshold_incumbent_retests")
        self.retest_candidate = candidate
        self.retest_enqueued = 0
        self.retest_completed.clear()
        self.phase = CampaignPhase.RETEST
        self.cursor = candidate
        self.events.append("RETEST_START:THREE_INCUMBENT_RETESTS")

    def _finish_retests_if_ready(self) -> None:
        if self.phase != CampaignPhase.RETEST or len(self.retest_completed) < 3:
            return
        rows = tuple(self.retest_completed)
        passing = tuple(
            row
            for row in rows
            if row.eligible and row.mae_n is not None and row.mae_n <= MAE_THRESHOLD_N
        )
        all_gates = all(
            row.eligible
            and row.safe_return
            and row.binding_ok
            and row.motion_gate
            and row.timing_gate
            and row.contact_gate
            and row.return_gate
            and row.identity_gate
            for row in rows
        )
        anchor_values = self.ledger.bootstrap_anchor_objectives()
        retest_values = tuple(float(row.objective) for row in rows if row.objective is not None)
        anchor_median = statistics.median(anchor_values) if anchor_values else None
        retest_median = statistics.median(retest_values) if len(retest_values) == 3 else None
        improved = (
            anchor_median is not None
            and retest_median is not None
            and retest_median <= RETTEST_IMPROVEMENT * anchor_median
        )
        if len(passing) >= 2 and all_gates and improved:
            self.queue.cancel_pending(reason="r005_completion")
            self.phase = CampaignPhase.COMPLETE
            self.events.append("COMPLETE:2_OF_3_AND_MEDIAN_5_PERCENT")
            return
        self.phase = CampaignPhase.BO
        self.cursor = rows[-1].candidate
        self.retest_candidate = None
        self.retest_completed.clear()
        self.retest_enqueued = 0
        self.events.append("RETEST_FAILED:RETURN_TO_BO")

    def _advance_after_record(self, record: ObservationRecord) -> None:
        self.cursor = record.candidate
        if record.kind == "QUALIFICATION":
            if record.qualification_eligible:
                self.qualification_passes += 1
            self._sync_qualification_state()
            if self.qualification_passes >= 3:
                self.phase = CampaignPhase.BOOTSTRAP
        elif record.kind == "BOOTSTRAP_PD":
            self.bootstrap_index += 1
            if self.bootstrap_index >= len(bootstrap_pd_candidates()):
                self.phase = CampaignPhase.BO
        elif record.kind == "RETEST":
            self.retest_completed.append(record)
            self._finish_retests_if_ready()
        elif record.kind == "BO_TRIAL":
            if record.eligible and record.mae_n is not None and record.mae_n <= MAE_THRESHOLD_N:
                self._start_retests(record.candidate)

    def run_one(self) -> AttemptResult | None:
        """Run one physical-shaped attempt; only offline runtimes may call this."""

        if self.terminal:
            return None
        if self.epoch == 0:
            self.start_epoch()
        try:
            self._begin_phase_clock()
            had_incomplete_inflight = self.queue.inflight is not None
            self.runtime.home()
            self._phase("HOME")
            if had_incomplete_inflight:
                reconcile = getattr(self.queue, "reconcile_inflight_after_home", None)
                if reconcile is None:
                    self._stop("resume_inflight_lacks_home_reconciliation")
                    return None
                reconciled = reconcile()
                if reconciled is None:
                    self._stop("resume_inflight_home_reconciliation_missing")
                    return None
                self.events.append(
                    f"RESUME_HOME_RECONCILED:{reconciled.logical_uid}"
                )
            self._refill()
            if self.terminal:
                return None
            ticket = self.queue.prepare_next()
            if ticket is None:
                self._stop("optimizer_exhausted_or_no_dispatchable_domain")
                return None
            attempt = Attempt(
                epoch=self.epoch,
                attempt_sequence=self.next_attempt_sequence,
                candidate=ticket.candidate,
                kind=ticket.kind,
                dispatch_sequence=ticket.dispatch_sequence,
                request_uid=ticket.request_uid,
                logical_request_uid=ticket.entry.logical_uid,
                execution_id=(
                    f"r005-e{self.epoch}-s{self.next_attempt_sequence}-"
                    f"{uuid.uuid4().hex}"
                ),
            )
            self.next_attempt_sequence += 1
            record_execution = getattr(self.queue, "record_execution", None)
            if record_execution is not None:
                record_execution(
                    ticket,
                    attempt_sequence=attempt.attempt_sequence,
                    execution_id=attempt.execution_id,
                )
            self.runtime.dispatch(attempt, ticket)
            self._phase("DISPATCH")
            self.runtime.arm(attempt)
            self._phase("ARM")
            result = self.runtime.run_60s(attempt)
            self._phase("RUN_60S")
            # A failed safety gate is not allowed to enter safe_return: the
            # authority revoke is the only next action.  Binding failures use
            # the same conservative path because their evidence is untrusted.
            self._validate_identity(attempt, result)
            if not result.safety_gate or not result.safe_return:
                self._stop("safety_or_return_failure_before_safe_return")
                return result
            result = self.runtime.safe_return(attempt, result)
            self._phase("SAFE_RETURN")
            self._validate_identity(attempt, result)
            if result.outcome is AttemptOutcome.CODE_OR_EVIDENCE_BUG:
                self._stop("code_or_evidence_binding_failure")
                return result
            if result.outcome is AttemptOutcome.SAFETY_OR_RETURN_FAILURE:
                self._stop("safety_or_safe_return_failure")
                return result
            record = self._record_and_tell(result)
            status = "SUCCEEDED" if record.eligible else "SAFE_NONTRAINABLE"
            self.queue.complete(ticket, status=status, detail="sealed_r005_observation")
            self._phase("QUEUE_COMPLETE")
            self._advance_after_record(record)
            self._refill()
            return result
        except (RuntimeErrorR005, QueueError, OptimizerError, OSError) as exc:
            self._stop(f"runtime_or_evidence_fault:{exc}")
            return None
        except Exception as exc:
            self._stop(f"unexpected_code_fault:{exc}")
            return None

    def run_until_terminal(self, *, diagnostic_limit: int | None = None) -> str:
        """Offline diagnostic runner; production live CLI has no ordinal stop."""

        steps = 0
        while not self.terminal:
            if diagnostic_limit is not None and steps >= diagnostic_limit:
                self._stop("offline_diagnostic_limit")
                break
            self.run_one()
            steps += 1
        return self.status


__all__ = [
    "Attempt",
    "AttemptOutcome",
    "AttemptResult",
    "CampaignPhase",
    "HostLoop",
    "MAE_THRESHOLD_N",
    "RETTEST_IMPROVEMENT",
    "RuntimeErrorR005",
    "RuntimePort",
]
