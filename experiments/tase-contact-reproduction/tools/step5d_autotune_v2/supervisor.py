"""One-writer candidate supervisor with post-ACK analysis isolation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .reducer import LifecycleError, LifecycleEvent, LifecycleState
from .repository import Repository


class RuntimeFailure(RuntimeError):
    """Typed runtime failure which stops the service without repeating a tuple."""


class SafetyHalt(RuntimeFailure):
    """Bridge/TP evidence of a real safety terminal, never an infra ambiguity."""


@dataclass(frozen=True)
class ArtifactSeal:
    path: Path
    sha256: str


@dataclass(frozen=True)
class AnalysisArtifact:
    role: str
    path: Path
    sha256: str
    transfer_destination: str | None = None


@dataclass(frozen=True)
class AnalysisResult:
    metrics: Mapping[str, Any]
    diagnostic_eligible: bool
    objective_eligible: bool
    artifacts: tuple[AnalysisArtifact, ...] = ()
    warnings: tuple[str, ...] = ()


class RuntimePort(Protocol):
    """Narrow adapter implemented by the bridge/TP transport, never control math."""

    def publish_arm(
        self, *, trial_id: str, sequence: int, candidate: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def wait_tp_consumed(self, *, trial_id: str) -> Mapping[str, Any]: ...

    def wait_run_started(self, *, trial_id: str) -> Mapping[str, Any]: ...

    def wait_home_verified(self, *, trial_id: str) -> Mapping[str, Any]: ...

    def seal_raw(self, *, trial_id: str) -> ArtifactSeal: ...

    def publish_ack(
        self, *, trial_id: str, sequence: int, artifact: ArtifactSeal
    ) -> Mapping[str, Any]: ...

    def wait_ready_home(self, *, trial_id: str) -> Mapping[str, Any]: ...

    def analyze(self, *, trial_id: str, artifact: ArtifactSeal) -> AnalysisResult: ...


@dataclass(frozen=True)
class TrialOutcome:
    trial_id: str
    state: str
    physical_closed: bool
    analysis_complete: bool
    primary_blocker: str | None


class CampaignSupervisor:
    def __init__(
        self,
        repository: Repository,
        port: RuntimePort,
        *,
        outcome_callback: Callable[[TrialOutcome], None] | None = None,
    ) -> None:
        self.repository = repository
        self.port = port
        self.outcome_callback = outcome_callback

    def _emit(self, outcome: TrialOutcome) -> None:
        if self.outcome_callback is not None:
            self.outcome_callback(outcome)

    def run_candidate(self, *, candidate_id: str, deployment_id: str) -> TrialOutcome:
        candidate = self.repository.candidate(candidate_id)
        if candidate["status"] != "pending":
            raise RuntimeFailure("candidate is not pending; automatic repeat is forbidden")
        trial_id = self.repository.create_trial(
            candidate_id=candidate_id, deployment_id=deployment_id
        )
        return self.resume_trial(trial_id)

    def resume_trial(self, trial_id: str) -> TrialOutcome:
        """Roll one durable trial forward from any committed crash cut."""

        while True:
            detail = self.repository.trial_detail(trial_id)
            snapshot = detail["snapshot"]
            state = LifecycleState(snapshot["state"])
            candidate = self.repository.candidate(detail["candidate_id"])
            if state is LifecycleState.PENDING:
                self.repository.apply_lifecycle_event(trial_id, LifecycleEvent.PERSIST_ARM)
                continue
            if state is LifecycleState.ARM_PERSISTED:
                evidence = self.port.publish_arm(
                    trial_id=trial_id,
                    sequence=int(detail["arm_sequence"]),
                    candidate=candidate,
                )
                self.repository.apply_lifecycle_event(
                    trial_id, LifecycleEvent.PUBLISH_COMMAND, evidence
                )
                continue
            if state is LifecycleState.COMMAND_PUBLISHED:
                try:
                    evidence = self.port.wait_tp_consumed(trial_id=trial_id)
                except SafetyHalt as exc:
                    final = self.repository.apply_lifecycle_event(
                        trial_id,
                        LifecycleEvent.SAFETY_HALT,
                        {"reason": str(exc)},
                    )
                    return self._outcome(trial_id, final)
                except RuntimeFailure as exc:
                    return self._mark_uncertain(
                        trial_id, f"tp_consumption_ambiguous:{exc}"
                    )
                self.repository.apply_lifecycle_event(
                    trial_id, LifecycleEvent.OBSERVE_TP_CONSUMED, evidence
                )
                continue
            if state is LifecycleState.TP_CONSUMED:
                try:
                    evidence = self.port.wait_run_started(trial_id=trial_id)
                except SafetyHalt as exc:
                    final = self.repository.apply_lifecycle_event(
                        trial_id,
                        LifecycleEvent.SAFETY_HALT,
                        {"reason": str(exc)},
                    )
                    return self._outcome(trial_id, final)
                except RuntimeFailure as exc:
                    return self._mark_uncertain(
                        trial_id, f"tp_run_ambiguous:{exc}"
                    )
                self.repository.apply_lifecycle_event(
                    trial_id, LifecycleEvent.OBSERVE_RUN, evidence
                )
                continue
            if state is LifecycleState.RUNNING:
                try:
                    evidence = self.port.wait_home_verified(trial_id=trial_id)
                except SafetyHalt as exc:
                    final = self.repository.apply_lifecycle_event(
                        trial_id,
                        LifecycleEvent.SAFETY_HALT,
                        {"reason": str(exc)},
                    )
                    return self._outcome(trial_id, final)
                except RuntimeFailure as exc:
                    return self._mark_uncertain(
                        trial_id, f"run_closure_ambiguous:{exc}"
                    )
                self.repository.apply_lifecycle_event(
                    trial_id,
                    LifecycleEvent.VERIFY_HOME,
                    {**evidence, "safe_home_verified": True},
                )
                continue
            if state is LifecycleState.HOME_VERIFIED:
                try:
                    artifact = self.port.seal_raw(trial_id=trial_id)
                except SafetyHalt as exc:
                    final = self.repository.apply_lifecycle_event(
                        trial_id,
                        LifecycleEvent.SAFETY_HALT,
                        {"reason": str(exc)},
                    )
                    return self._outcome(trial_id, final)
                self.repository.add_artifact(
                    trial_id=trial_id,
                    role="raw_capture_seal",
                    path=artifact.path,
                    sha256=artifact.sha256,
                    immutable=True,
                )
                self.repository.apply_lifecycle_event(
                    trial_id,
                    LifecycleEvent.SEAL_RAW,
                    {"artifact_sha256": artifact.sha256, "path": str(artifact.path)},
                )
                continue
            artifact_row = self.repository.artifact_for_trial(trial_id)
            artifact = (
                ArtifactSeal(Path(artifact_row["path"]), artifact_row["sha256"])
                if artifact_row
                else None
            )
            if state is LifecycleState.RAW_SEALED:
                if artifact is None:
                    raise RuntimeFailure("raw-sealed state lacks its immutable artifact")
                self.repository.reserve_ack_sequence(trial_id)
                self.repository.apply_lifecycle_event(trial_id, LifecycleEvent.PERSIST_ACK)
                continue
            if state is LifecycleState.ACK_PERSISTED:
                if artifact is None or detail["ack_sequence"] is None:
                    raise RuntimeFailure("persisted ACK lacks sequence or artifact")
                evidence = self.port.publish_ack(
                    trial_id=trial_id,
                    sequence=int(detail["ack_sequence"]),
                    artifact=artifact,
                )
                self.repository.apply_lifecycle_event(
                    trial_id, LifecycleEvent.PUBLISH_ACK, evidence
                )
                continue
            if state is LifecycleState.ACK_PUBLISHED:
                try:
                    evidence = self.port.wait_ready_home(trial_id=trial_id)
                except SafetyHalt as exc:
                    final = self.repository.apply_lifecycle_event(
                        trial_id,
                        LifecycleEvent.SAFETY_HALT,
                        {"reason": str(exc)},
                    )
                    return self._outcome(trial_id, final)
                self.repository.apply_lifecycle_event(
                    trial_id, LifecycleEvent.OBSERVE_READY_HOME, evidence
                )
                continue
            if state is LifecycleState.READY_HOME_OBSERVED:
                self.repository.apply_lifecycle_event(
                    trial_id, LifecycleEvent.CLOSE_PHYSICAL
                )
                continue
            if state is LifecycleState.PHYSICAL_CLOSED:
                if artifact is None:
                    raise RuntimeFailure("physical closure lacks immutable raw artifact")
                if detail.get("analysis") is not None:
                    final = self.repository.apply_lifecycle_event(
                        trial_id,
                        LifecycleEvent.ANALYSIS_SUCCEEDED,
                        {
                            "eligible_objective": bool(
                                detail["analysis"]["objective_eligible"]
                            ),
                            "recovered": True,
                        },
                    )
                    return self._outcome(trial_id, final)
                try:
                    analysis = self.port.analyze(trial_id=trial_id, artifact=artifact)
                    for derived in analysis.artifacts:
                        artifact_id = self.repository.add_artifact(
                            trial_id=trial_id,
                            role=derived.role,
                            path=derived.path,
                            sha256=derived.sha256,
                            immutable=True,
                        )
                        if derived.transfer_destination is not None:
                            self.repository.queue_transfer(
                                artifact_id=artifact_id,
                                destination=derived.transfer_destination,
                            )
                    self.repository.record_analysis(
                        trial_id=trial_id,
                        metrics={
                            **analysis.metrics,
                            "_postprocess_warnings": list(analysis.warnings),
                        },
                        diagnostic_eligible=analysis.diagnostic_eligible,
                        objective_eligible=analysis.objective_eligible,
                    )
                    final = self.repository.apply_lifecycle_event(
                        trial_id,
                        LifecycleEvent.ANALYSIS_SUCCEEDED,
                        {"eligible_objective": analysis.objective_eligible},
                    )
                except Exception as exc:
                    final = self.repository.apply_lifecycle_event(
                        trial_id,
                        LifecycleEvent.ANALYSIS_FAILED,
                        {"reason": f"postprocess:{type(exc).__name__}:{exc}"},
                    )
                return self._outcome(trial_id, final)
            return TrialOutcome(
                trial_id=trial_id,
                state=state.value,
                physical_closed=bool(snapshot["physical_closed"]),
                analysis_complete=bool(snapshot["analysis_complete"]),
                primary_blocker=snapshot["primary_blocker"],
            )

    def run_pending(
        self, *, deployment_id: str, maximum: int | None = None
    ) -> tuple[TrialOutcome, ...]:
        outcomes: list[TrialOutcome] = []
        active = self.repository.active_trial_id()
        if active is not None:
            outcome = self.resume_trial(active)
            outcomes.append(outcome)
            self._emit(outcome)
            if outcome.state in {
                LifecycleState.FAULT.value,
                LifecycleState.UNCERTAIN_ATTEMPT.value,
                LifecycleState.ANALYSIS_FAILED.value,
            }:
                return tuple(outcomes)
        while maximum is None or len(outcomes) < maximum:
            if self.repository.get_metadata("stop_after_current") == "1" and outcomes:
                break
            candidate = self.repository.next_pending_candidate()
            if candidate is None:
                break
            outcome = self.run_candidate(
                candidate_id=candidate["candidate_id"], deployment_id=deployment_id
            )
            outcomes.append(outcome)
            self._emit(outcome)
            if outcome.state in {
                LifecycleState.FAULT.value,
                LifecycleState.UNCERTAIN_ATTEMPT.value,
                LifecycleState.ANALYSIS_FAILED.value,
            }:
                break
        return tuple(outcomes)

    @staticmethod
    def _outcome(trial_id: str, snapshot: Any) -> TrialOutcome:
        return TrialOutcome(
            trial_id=trial_id,
            state=snapshot.state.value,
            physical_closed=snapshot.physical_closed,
            analysis_complete=snapshot.analysis_complete,
            primary_blocker=snapshot.primary_blocker,
        )

    def _mark_uncertain(self, trial_id: str, reason: str) -> TrialOutcome:
        """Coalesce a concurrent bridge revocation with the waiter failure."""

        try:
            final = self.repository.apply_lifecycle_event(
                trial_id,
                LifecycleEvent.MARK_UNCERTAIN_ATTEMPT,
                {"reason": reason},
            )
            return self._outcome(trial_id, final)
        except LifecycleError:
            snapshot = self.repository.trial_detail(trial_id)["snapshot"]
            if snapshot["state"] != LifecycleState.UNCERTAIN_ATTEMPT.value:
                raise
            return TrialOutcome(
                trial_id=trial_id,
                state=snapshot["state"],
                physical_closed=bool(snapshot["physical_closed"]),
                analysis_complete=bool(snapshot["analysis_complete"]),
                primary_blocker=snapshot["primary_blocker"],
            )
