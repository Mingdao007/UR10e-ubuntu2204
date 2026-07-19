#!/usr/bin/env python3
"""Pure host/TP transaction state for re-entrant Step5d autotune trials."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, Mapping

from step5d_autotune_contract import (
    ClosureEvidence,
    SafeClosureEvidence,
    TrialDisposition,
    TrialSpec,
)


HOST_TO_TP_INTEGER_REGISTERS = {
    "campaign_epoch": 24,
    "trial_id": 25,
    "command": 26,
    "candidate_token": 27,
    "execution_profile_id": 28,
    "command_seq": 29,
}

TP_TO_HOST_INTEGER_REGISTERS = {
    "campaign_epoch_echo": 24,
    "trial_id_echo": 25,
    "state": 26,
    "candidate_token_echo": 27,
    "terminal_reason": 28,
    "execution_profile_id_echo": 29,
    "consumed_command_seq": 30,
}


class HostCommand(IntEnum):
    HOLD = 0
    ARM = 1
    ACK_BUNDLE = 2
    STOP = 3


class TpLoopState(IntEnum):
    READY_HOME = 10
    ARMED = 11
    RUN = 20
    TERMINAL = 30
    RETRACT = 40
    RETURN = 50
    HOME_VERIFY = 60
    WAIT_ACK = 70
    WAIT_INFRA_READY = 75
    READY_NEAR = 76
    READY_HOME_CLOSED = 77
    FAULT = 90


EXPECTED_SAFE_SEQUENCE = (
    TpLoopState.READY_HOME,
    TpLoopState.ARMED,
    TpLoopState.RUN,
    TpLoopState.TERMINAL,
    TpLoopState.RETRACT,
    TpLoopState.RETURN,
    TpLoopState.HOME_VERIFY,
    TpLoopState.WAIT_ACK,
)


@dataclass(frozen=True)
class HostPacket:
    campaign_epoch: int
    trial_id: int
    command: HostCommand
    candidate_token: int
    execution_profile_id: int
    command_seq: int


@dataclass(frozen=True)
class TpPacket:
    campaign_epoch_echo: int
    trial_id_echo: int
    state: TpLoopState
    candidate_token_echo: int
    terminal_reason: int
    execution_profile_id_echo: int
    consumed_command_seq: int


def classify_terminal_reason(
    reason: int,
    *,
    host_cause: str | None,
    safe_closure: bool,
    eligible_evidence: bool,
) -> TrialDisposition:
    """Classify without treating platform/code failures as parameter evidence."""

    if reason in {2, 3}:
        return TrialDisposition.MANUAL_RECOVERY
    if not safe_closure:
        return TrialDisposition.SAFETY_STOP
    if reason == 1:
        return TrialDisposition.OBJECTIVE if eligible_evidence else TrialDisposition.FAIL_CLOSED
    if reason in {5, 6, 7, 17}:
        return TrialDisposition.SAFETY_STOP
    if reason in {8, 10, 12, 14}:
        return TrialDisposition.WAIT_INFRA_READY
    if reason == 13:
        return TrialDisposition.CODE_CONTRACT_BUG
    if reason == 4:
        return {
            "parameter_event": TrialDisposition.PARAMETER_EVENT,
            "operator_stop": TrialDisposition.OPERATOR_STOP,
            "infra_stop": TrialDisposition.WAIT_INFRA_READY,
        }.get(host_cause or "", TrialDisposition.FAIL_CLOSED)
    return TrialDisposition.FAIL_CLOSED


def host_packet_for_trial(
    trial: TrialSpec,
    *,
    command: HostCommand,
    execution_profile_id: int,
) -> HostPacket:
    return HostPacket(
        campaign_epoch=trial.campaign.campaign_epoch,
        trial_id=trial.trial_id,
        command=command,
        candidate_token=trial.candidate_token,
        execution_profile_id=execution_profile_id,
        command_seq=trial.command_seq,
    )


def packet_matches(host: HostPacket, tp: TpPacket) -> bool:
    return (
        tp.campaign_epoch_echo == host.campaign_epoch
        and tp.trial_id_echo == host.trial_id
        and tp.candidate_token_echo == host.candidate_token
        and tp.execution_profile_id_echo == host.execution_profile_id
        and tp.consumed_command_seq == host.command_seq
    )


def verify_transcript(host: HostPacket, packets: Iterable[TpPacket]) -> tuple[bool, tuple[str, ...]]:
    rows = tuple(packets)
    failures: list[str] = []
    if not rows:
        return False, ("empty_transcript",)
    states = tuple(row.state for row in rows)
    cursor = 0
    for required in EXPECTED_SAFE_SEQUENCE:
        try:
            cursor = states.index(required, cursor) + 1
        except ValueError:
            failures.append(f"missing_state_{required.name.lower()}")
            break
    for row in rows:
        if row.state is TpLoopState.READY_HOME:
            continue
        if not packet_matches(host, row):
            failures.append(f"echo_mismatch_at_{row.state.name.lower()}")
            break
    if rows[-1].state is not TpLoopState.WAIT_ACK:
        failures.append("terminal_state_not_wait_ack")
    return not failures, tuple(failures)


class LoopCoordinator:
    """Pure single-writer gate for host orchestration tests and supervisor use."""

    def __init__(self) -> None:
        self.state = TpLoopState.READY_HOME
        self.active_trial_uid: str | None = None
        self.immutable_bundle_written = False
        self.safe_closure = False
        self.last_disposition: TrialDisposition | None = None
        self._post_ack_state = TpLoopState.READY_HOME

    def arm(self, trial: TrialSpec) -> None:
        if self.active_trial_uid is not None:
            raise RuntimeError("one live writer/trial is already active")
        if self.state is not TpLoopState.READY_HOME:
            raise RuntimeError("TP loop must complete ACK/infra transitions before re-arm")
        self.active_trial_uid = trial.trial_uid
        self.immutable_bundle_written = False
        self.safe_closure = False
        self.last_disposition = None
        self._post_ack_state = TpLoopState.READY_HOME
        self.state = TpLoopState.ARMED

    def observe_state(self, state: TpLoopState) -> None:
        if self.active_trial_uid is None and state not in {TpLoopState.READY_HOME, TpLoopState.WAIT_INFRA_READY}:
            raise RuntimeError("TP state advanced without an active trial")
        self.state = state

    def close_trial(
        self,
        *,
        reason: int,
        host_cause: str | None,
        evidence: ClosureEvidence,
        eligible_evidence: bool,
        immutable_bundle_written: bool,
    ) -> TrialDisposition:
        if self.active_trial_uid is None:
            raise RuntimeError("no active trial to close")
        self.safe_closure = evidence.returned_safe
        self.immutable_bundle_written = immutable_bundle_written
        self.last_disposition = classify_terminal_reason(
            reason,
            host_cause=host_cause,
            safe_closure=self.safe_closure,
            eligible_evidence=eligible_evidence,
        )
        if self.safe_closure:
            self._post_ack_state = (
                TpLoopState.WAIT_INFRA_READY
                if self.last_disposition is TrialDisposition.WAIT_INFRA_READY
                else TpLoopState.READY_HOME
            )
            self.state = TpLoopState.WAIT_ACK
        else:
            self._post_ack_state = TpLoopState.FAULT
            self.state = TpLoopState.FAULT
        return self.last_disposition

    def ack_bundle(self) -> None:
        if self.state is not TpLoopState.WAIT_ACK:
            raise RuntimeError("ACK is valid only in WAIT_ACK")
        if not self.safe_closure or not self.immutable_bundle_written:
            raise RuntimeError("cannot ACK before safe closure and immutable bundle")
        self.active_trial_uid = None
        self.state = self._post_ack_state

    def infra_ready(self) -> None:
        if self.state is not TpLoopState.WAIT_INFRA_READY:
            raise RuntimeError("not waiting for infrastructure")
        if not self.safe_closure or not self.immutable_bundle_written:
            raise RuntimeError("infra retry requires safe closure and immutable bundle")
        self.state = TpLoopState.READY_HOME


def integer_register_contract() -> Mapping[str, Mapping[str, int]]:
    return {"host_to_tp": HOST_TO_TP_INTEGER_REGISTERS, "tp_to_host": TP_TO_HOST_INTEGER_REGISTERS}
