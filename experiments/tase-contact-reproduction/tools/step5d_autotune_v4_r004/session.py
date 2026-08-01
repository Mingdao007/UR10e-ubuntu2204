"""Resident Play/ARM/return protocol for the r004 TP loop."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import MAX_ATTEMPTS, R004Contract, R004ContractError
from .home import HomeReference, ReturnDecision
from .identity import ControllerReadbackReceipt, RuntimeIdentityEvidence, Script1StartReceipt, Script1UseLedger, SessionIdentityGate
from .wire import AttemptKind, SessionCommand, TPState


class SessionProtocolError(RuntimeError):
    """A command would violate the resident session contract."""


class SessionPhase(str, Enum):
    WAITING_FOR_PLAY = "waiting_for_play"
    READY_HOME_NEXT = "ready_home_next"
    RUNNING = "running"
    COMPLETE = "complete"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ArmRequest:
    command: SessionCommand
    command_sequence: int
    session_epoch: int
    logical_attempt_ordinal: int
    attempt_kind: AttemptKind
    candidate_token: int


@dataclass(frozen=True)
class SessionOutput:
    epoch: int
    ordinal: int
    state: TPState
    token: int
    reason: int
    consumed_sequence: int
    kind: AttemptKind
    return_guard: int


class ResidentSession:
    """One Play owns one captured Home and many rolling ARM attempts."""

    def __init__(self, contract: R004Contract) -> None:
        self.contract = contract
        self.identity = SessionIdentityGate(contract)
        self.script1_uses = Script1UseLedger()
        self.phase = SessionPhase.WAITING_FOR_PLAY
        self.epoch = 0
        self.session_id = ""
        self.home: HomeReference | None = None
        self.consumed_command_sequence = 0
        self.current_ordinal = 0
        self.current_kind = AttemptKind.QUALIFICATION
        self.current_token = 0
        self.last_reason = 0
        self.last_return_guard = 0

    @property
    def state(self) -> TPState:
        return {
            SessionPhase.WAITING_FOR_PLAY: TPState.WAITING_FOR_PLAY,
            SessionPhase.READY_HOME_NEXT: TPState.READY_HOME_NEXT,
            SessionPhase.RUNNING: TPState.PATH,
            SessionPhase.COMPLETE: TPState.COMPLETE,
            SessionPhase.STOPPED: TPState.STOPPED,
        }[self.phase]

    def play(
        self,
        *,
        controller_receipt: ControllerReadbackReceipt,
        script1_receipt: Script1StartReceipt,
        now_s: float,
        epoch: int,
        session_id: str,
        expected_triplet: dict[str, str],
        home: HomeReference,
    ) -> SessionOutput:
        if self.phase not in {SessionPhase.WAITING_FOR_PLAY, SessionPhase.STOPPED}:
            raise SessionProtocolError("Script 2 Play is only valid for a new resident session")
        if self.phase is SessionPhase.STOPPED and epoch <= self.epoch:
            raise SessionProtocolError("recovery requires a new session epoch")
        try:
            self.script1_uses.consume(script1_receipt, epoch)
            self.identity.begin_play(
                controller_receipt=controller_receipt,
                script1_receipt=script1_receipt,
                now_s=now_s,
                epoch=epoch,
                session_id=session_id,
                expected_triplet=expected_triplet,
            )
        except R004ContractError as exc:
            raise SessionProtocolError(str(exc)) from exc
        if len(home.pose) != 6 or any(not isinstance(value, float) for value in home.pose):
            raise SessionProtocolError("campaign Home pose must be captured once as six floats")
        self.phase = SessionPhase.READY_HOME_NEXT
        self.epoch = epoch
        self.session_id = session_id
        self.home = HomeReference(tuple(home.pose), None if home.q is None else tuple(home.q))
        self.consumed_command_sequence = 0
        self.current_ordinal = 0
        self.current_token = 0
        self.last_reason = 0
        self.last_return_guard = 0
        return self.output()

    def observe_runtime(self, evidence: RuntimeIdentityEvidence) -> bool:
        return self.identity.observe_runtime(evidence)

    def arm(self, request: ArmRequest, *, ledger_ready: bool) -> SessionOutput:
        if self.phase is not SessionPhase.READY_HOME_NEXT:
            raise SessionProtocolError("ARM is only accepted in READY_HOME_NEXT")
        if request.command is not SessionCommand.ARM:
            raise SessionProtocolError("arm request command is not ARM")
        if request.command_sequence <= self.consumed_command_sequence:
            raise SessionProtocolError("session command sequence is not strictly newer")
        if request.session_epoch != self.epoch:
            raise SessionProtocolError("ARM session epoch differs")
        if not 1 <= request.logical_attempt_ordinal <= MAX_ATTEMPTS:
            raise SessionProtocolError("ARM ordinal is outside the 16-attempt plan")
        if request.candidate_token <= 0:
            raise SessionProtocolError("ARM candidate token is invalid")
        if not ledger_ready:
            raise SessionProtocolError("next ARM is forbidden before durable ledger verification")
        self.consumed_command_sequence = request.command_sequence
        self.current_ordinal = request.logical_attempt_ordinal
        self.current_kind = request.attempt_kind
        self.current_token = request.candidate_token
        self.phase = SessionPhase.RUNNING
        return self.output(state=TPState.ARMING)

    def finish_attempt(self, decision: ReturnDecision) -> SessionOutput:
        if self.phase is not SessionPhase.RUNNING:
            raise SessionProtocolError("attempt completion is not in RUNNING")
        self.last_return_guard = int(decision.guard_mask)
        if not decision.passed:
            self.fault(decision.reason or "return_guard_failed", reason_code=17)
            return self.output(state=TPState.STOPPED)
        self.phase = SessionPhase.READY_HOME_NEXT
        self.last_reason = 0
        return self.output(state=TPState.READY_HOME_NEXT)

    def complete(self, command_sequence: int) -> SessionOutput:
        if self.phase is not SessionPhase.READY_HOME_NEXT:
            raise SessionProtocolError("COMPLETE is only accepted while resident and stationary")
        if command_sequence <= self.consumed_command_sequence:
            raise SessionProtocolError("COMPLETE sequence is not newer")
        self.consumed_command_sequence = command_sequence
        self.phase = SessionPhase.COMPLETE
        return self.output(state=TPState.COMPLETE)

    def stop(self, command_sequence: int, reason_code: int = 4) -> SessionOutput:
        if command_sequence <= self.consumed_command_sequence:
            raise SessionProtocolError("STOP sequence is not newer")
        self.consumed_command_sequence = command_sequence
        self.fault("session_stop", reason_code=reason_code)
        return self.output(state=TPState.STOPPED)

    def fault(self, reason: str, *, reason_code: int = 4) -> None:
        self.phase = SessionPhase.STOPPED
        self.last_reason = reason_code
        self.last_return_guard = 0
        self.identity.invalidate(reason)

    def output(self, *, state: TPState | None = None) -> SessionOutput:
        return SessionOutput(
            epoch=self.epoch,
            ordinal=self.current_ordinal,
            state=state or self.state,
            token=self.current_token,
            reason=self.last_reason,
            consumed_sequence=self.consumed_command_sequence,
            kind=self.current_kind,
            return_guard=self.last_return_guard,
        )


__all__ = [
    "ArmRequest",
    "ResidentSession",
    "SessionOutput",
    "SessionPhase",
    "SessionProtocolError",
]
