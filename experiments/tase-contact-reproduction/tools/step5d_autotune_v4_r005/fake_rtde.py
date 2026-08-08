"""FakeRTDE/TP resident model with no controller or network side effects."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable

from .alignment import (
    AlignedJointVelocity,
    JointVelocityPacket,
    align_qdot_actual_qd,
)
from .runtime import Attempt, AttemptResult
from .timing import AbsoluteDeadlinePacer, RateGates, TimingEvidence


class FakeRTDEError(RuntimeError):
    """The offline transport/TP model rejected an identity or phase."""


@dataclass
class TPResidentLoop:
    """Resident TP state machine accepting unbounded positive attempt IDs."""

    last_attempt_sequence: int = 0
    epoch: int | None = None
    state: str = "WAITING_ARM"
    arm_history: list[int] = field(default_factory=list)

    def accept_arm(self, *, epoch: int, attempt_sequence: int) -> None:
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
            raise FakeRTDEError("TP ARM epoch must be positive")
        if isinstance(attempt_sequence, bool) or not isinstance(attempt_sequence, int) or attempt_sequence <= 0:
            raise FakeRTDEError("TP ARM attempt sequence must be positive")
        if attempt_sequence <= self.last_attempt_sequence:
            raise FakeRTDEError("TP ARM attempt sequence must increase monotonically")
        if self.state != "WAITING_ARM":
            raise FakeRTDEError("TP resident loop is not waiting for ARM")
        if self.epoch is not None and epoch != self.epoch:
            raise FakeRTDEError("TP epoch differs within one resident Play")
        self.epoch = epoch
        self.last_attempt_sequence = attempt_sequence
        self.arm_history.append(attempt_sequence)
        self.state = "RUNNING"

    def safe_return(self) -> None:
        if self.state != "RUNNING":
            raise FakeRTDEError("TP safe return requires a running attempt")
        self.state = "WAITING_ARM"

    def revoke(self) -> None:
        self.state = "REVOKED"


@dataclass
class FakeRTDE:
    """Synthetic RTDE packet source; hardware_calls is a permanent zero gate."""

    tp: TPResidentLoop = field(default_factory=TPResidentLoop)
    hardware_calls: int = 0
    events: list[str] = field(default_factory=list)
    last_alignment: AlignedJointVelocity | None = None
    last_timing: TimingEvidence | None = None
    safe_return_calls: int = 0

    def home(self) -> None:
        if self.tp.state == "REVOKED":
            raise FakeRTDEError("TP authority is revoked")
        self.events.append("HOME")

    def dispatch(self, attempt: Attempt) -> None:
        if self.tp.state != "WAITING_ARM":
            raise FakeRTDEError("dispatch requires resident TP waiting at Home")
        self.events.append(f"DISPATCH:{attempt.dispatch_sequence}")

    def arm(self, attempt: Attempt) -> None:
        self.tp.accept_arm(
            epoch=attempt.epoch,
            attempt_sequence=attempt.attempt_sequence,
        )
        self.events.append(f"ARM:{attempt.attempt_sequence}")

    def run_synthetic_60s(self, attempt: Attempt) -> TimingEvidence:
        gates = RateGates()
        # Two points establish each independent gate's measured rate.  The
        # attempt duration remains 60 s in the result; no wall clock or robot
        # is used by this offline model.
        for name in ("writer", "rtde", "kunwei", "tp"):
            gates.record(name, 0.0)
            gates.record(name, 1.0 / 500.0)
        evidence = gates.evidence()
        self.last_timing = evidence
        if not evidence.passes:
            raise FakeRTDEError("synthetic independent 500 Hz gates did not pass")
        timestamp = float(attempt.attempt_sequence) * 0.002
        self.align(
            JointVelocityPacket(
                packet_sequence=attempt.attempt_sequence,
                rtde_sequence=attempt.dispatch_sequence,
                timestamp_s=timestamp,
                values=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            ),
            JointVelocityPacket(
                packet_sequence=attempt.attempt_sequence,
                rtde_sequence=attempt.dispatch_sequence,
                timestamp_s=timestamp + 0.0002,
                values=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            ),
        )
        return evidence

    def align(
        self,
        qdot: JointVelocityPacket,
        actual_qd: JointVelocityPacket,
    ) -> AlignedJointVelocity:
        aligned = align_qdot_actual_qd(qdot, actual_qd)
        self.last_alignment = aligned
        return aligned

    def safe_return(self) -> None:
        self.safe_return_calls += 1
        self.tp.safe_return()
        self.events.append("SAFE_RETURN")

    def revoke(self, reason: str) -> None:
        self.tp.revoke()
        self.events.append(f"REVOKE:{reason}")


class FakeAttemptRuntime:
    """RuntimePort implementation used only by offline integration tests."""

    def __init__(
        self,
        *,
        result_factory: Callable[[Attempt, TimingEvidence], AttemptResult],
        rtde: FakeRTDE | None = None,
    ) -> None:
        self.rtde = rtde or FakeRTDE()
        self.result_factory = result_factory
        self.events: list[str] = []
        self.authority_revoked = False
        self.revoke_reason: str | None = None
        self._timing: TimingEvidence | None = None
        self.attempts: list[Attempt] = []

    def home(self) -> None:
        self.rtde.home()
        self.events.append("HOME")

    def dispatch(self, attempt: Attempt, ticket: Any) -> None:
        del ticket
        self.rtde.dispatch(attempt)
        self.events.append("DISPATCH")

    def arm(self, attempt: Attempt) -> None:
        self.rtde.arm(attempt)
        self.events.append("ARM")

    def run_60s(self, attempt: Attempt) -> AttemptResult:
        self.attempts.append(attempt)
        self._timing = self.rtde.run_synthetic_60s(attempt)
        result = self.result_factory(attempt, self._timing)
        updates: dict[str, Any] = {}
        if result.joint_evidence is None:
            if self.rtde.last_alignment is None:
                raise FakeRTDEError("synthetic evidence did not bind joint velocities")
            updates["joint_evidence"] = self.rtde.last_alignment
        if result.execution_id != attempt.execution_id:
            updates["execution_id"] = attempt.execution_id
        if updates:
            result = replace(result, **updates)
        self.events.append("RUN_60S")
        return result

    def safe_return(self, attempt: Attempt, result: AttemptResult) -> AttemptResult:
        del attempt
        self.rtde.safe_return()
        self.events.append("SAFE_RETURN")
        return result

    def revoke_authority(self, reason: str) -> None:
        self.authority_revoked = True
        self.revoke_reason = reason
        self.rtde.revoke(reason)
        self.events.append("REVOKE")


__all__ = [
    "FakeAttemptRuntime",
    "FakeRTDE",
    "FakeRTDEError",
    "TPResidentLoop",
]
