"""Deterministic no-contact certification protocol for the V3 bridge/TP seam."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Any, Mapping

from ur10e_experiment_runtime.authorization import (
    CertificationMotionAuthorization,
    CertificationProcedureTicket,
    issue_certification_procedure_ticket,
)


COMMAND_DIRECT_STOP = 4
COMMAND_STALE_STOP = 5
COMMAND_RETURN_ROUTE = 6
EXECUTION_PROFILE_ID = 9001
SAMPLES_PER_STOP_PROCEDURE = 3
MOTION_STATE = 80
STOP_TRIGGERED_STATE = 81
STATIONARY_STATE = 82
RETURN_PREPARING_STATE = 83
RETURN_PREPARED_STATE = 84
SAFE_CLOSURE_STATE = 85
READY_HOME_STATE = 10
FAULT_STATE = 90
TRIGGER_SPEED_M_S = 0.005
MAX_MOTION_TICKS_WITHOUT_TRIGGER = 1000
STOP_LINEAR_SPEED_M_S = 0.010
STOP_LINEAR_ACCELERATION_M_S2 = 0.060
RETURN_LINEAR_SPEED_M_S = 0.090
RETURN_LINEAR_ACCELERATION_M_S2 = 0.135
RETURN_ANGULAR_SPEED_RAD_S = 0.050
RETURN_ANGULAR_ACCELERATION_RAD_S2 = 0.100

PROCEDURE_COMMANDS = {
    "direct_exact_stop": COMMAND_DIRECT_STOP,
    "stale_watchdog_exact_stop": COMMAND_STALE_STOP,
    "return_route": COMMAND_RETURN_ROUTE,
}


class CertificationProtocolError(RuntimeError):
    """Certification state, identity, or safety evidence failed closed."""


@dataclass(frozen=True)
class CertificationStep:
    procedure: str
    sample_index: int
    ticket: CertificationProcedureTicket
    trial_id: int
    candidate_token: int

    @property
    def command(self) -> int:
        return PROCEDURE_COMMANDS[self.procedure]


def _positive_hash_int(text: str, *, salt: str) -> int:
    digest = hashlib.sha256(f"{salt}:{text}".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 2_000_000_000 + 1


def certification_steps(
    authorization: CertificationMotionAuthorization,
) -> tuple[CertificationStep, ...]:
    rows: list[CertificationStep] = []
    for procedure, count in (
        ("direct_exact_stop", SAMPLES_PER_STOP_PROCEDURE),
        ("stale_watchdog_exact_stop", SAMPLES_PER_STOP_PROCEDURE),
        ("return_route", 1),
    ):
        for sample_index in range(1, count + 1):
            ticket = issue_certification_procedure_ticket(
                authorization, procedure
            )
            identity = (
                f"{authorization.authorization_ref_sha256}:"
                f"{procedure}:{sample_index}"
            )
            rows.append(
                CertificationStep(
                    procedure=procedure,
                    sample_index=sample_index,
                    ticket=ticket,
                    trial_id=_positive_hash_int(identity, salt="trial"),
                    candidate_token=_positive_hash_int(identity, salt="token"),
                )
            )
    return tuple(rows)


def _integer(output: Mapping[str, Any], index: int) -> int:
    name = f"output_int_register_{index}"
    value = output.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CertificationProtocolError(f"TP feedback lacks integer {name}")
    parsed = int(value)
    if float(value) != parsed:
        raise CertificationProtocolError(f"TP feedback {name} is not integral")
    return parsed


def _linear_speed(output: Mapping[str, Any]) -> float:
    value = output.get("actual_TCP_speed")
    if not isinstance(value, (list, tuple)) or len(value) != 6:
        raise CertificationProtocolError("TP feedback lacks actual_TCP_speed")
    try:
        xyz = tuple(float(item) for item in value[:3])
    except (TypeError, ValueError) as exc:
        raise CertificationProtocolError("TP speed is nonnumeric") from exc
    if any(item != item or abs(item) == float("inf") for item in xyz):
        raise CertificationProtocolError("TP speed is nonfinite")
    return sum(item * item for item in xyz) ** 0.5


class CertificationSession:
    """Advance the fixed seven-procedure plan from exact RTDE feedback only."""

    def __init__(self, authorization: CertificationMotionAuthorization) -> None:
        if not isinstance(authorization, CertificationMotionAuthorization):
            raise CertificationProtocolError(
                "CertificationMotionAuthorization is required"
            )
        self.authorization = authorization
        self.steps = certification_steps(authorization)
        self.step_index = 0
        self.command_seq = 0
        self.started = False
        self.awaiting_ready = False
        self.complete = False
        self.hold_heartbeat = False
        self.stop_request = False
        self.motion_ticks = 0
        self.trigger_controller_timestamp_s: float | None = None

    def _consume_current_ticket(self) -> None:
        step = self.current
        assert step is not None
        self.authorization.require_procedure(step.procedure)
        ticket = step.ticket
        if (
            ticket.authorization_ref_sha256
            != self.authorization.authorization_ref_sha256
            or ticket.procedure != step.procedure
            or ticket.plant_epoch != self.authorization.plant_epoch
            or ticket.deployment_readback_sha256
            != self.authorization.deployment_readback_sha256
        ):
            raise CertificationProtocolError(
                "certification procedure ticket binding differs"
            )
        required = (
            (
                STOP_LINEAR_SPEED_M_S,
                STOP_LINEAR_ACCELERATION_M_S2,
                RETURN_ANGULAR_ACCELERATION_RAD_S2,
            )
            if step.procedure != "return_route"
            else (
                RETURN_LINEAR_SPEED_M_S,
                RETURN_LINEAR_ACCELERATION_M_S2,
                RETURN_ANGULAR_ACCELERATION_RAD_S2,
            )
        )
        if (
            ticket.max_linear_speed_m_s < required[0]
            or ticket.max_linear_acceleration_m_s2 < required[1]
            or ticket.max_angular_speed_rad_s < RETURN_ANGULAR_SPEED_RAD_S
            or ticket.max_angular_acceleration_rad_s2 < required[2]
        ):
            raise CertificationProtocolError(
                "certification procedure ticket motion envelope is insufficient"
            )

    @property
    def current(self) -> CertificationStep | None:
        return None if self.complete else self.steps[self.step_index]

    @property
    def handshake(self) -> dict[str, int]:
        step = self.current
        if step is None:
            return {
                "campaign_epoch": 0,
                "trial_id": 0,
                "command": 0,
                "candidate_token": 0,
                "execution_profile_id": 0,
                "command_seq": self.command_seq,
                "batch_row_index": 0,
            }
        return {
            "campaign_epoch": self.authorization.plant_epoch,
            "trial_id": step.trial_id,
            "command": step.command,
            "candidate_token": step.candidate_token,
            "execution_profile_id": EXECUTION_PROFILE_ID,
            "command_seq": self.command_seq,
            "batch_row_index": step.sample_index,
        }

    def _identity_matches(self, output: Mapping[str, Any]) -> bool:
        step = self.current
        assert step is not None
        return (
            _integer(output, 24) == self.authorization.plant_epoch
            and _integer(output, 25) == step.trial_id
            and _integer(output, 27) == step.candidate_token
            and _integer(output, 29) == EXECUTION_PROFILE_ID
            and _integer(output, 30) == self.command_seq
            and _integer(output, 31) == step.sample_index
            and _integer(output, 32) == 3
        )

    def poll(self, output: Mapping[str, Any]) -> bool:
        if self.complete:
            return False
        if not isinstance(output, Mapping):
            raise CertificationProtocolError("RTDE feedback is unavailable")
        safety_mode = output.get("safety_mode")
        if safety_mode not in {1, "NORMAL"}:
            raise CertificationProtocolError("certification left NORMAL safety mode")
        state = _integer(output, 26)
        step = self.current
        assert step is not None
        if state == FAULT_STATE:
            if not self.started:
                return False
            if not self._identity_matches(output):
                raise CertificationProtocolError(
                    "TP certification fault identity differs"
                )
            raise CertificationProtocolError(
                f"TP certification fault reason={_integer(output, 28)}"
            )

        if not self.started:
            if state != READY_HOME_STATE or any(
                _integer(output, index) != 0 for index in (24, 25, 27, 29)
            ):
                return False
            self._consume_current_ticket()
            self.command_seq = _integer(output, 30) + 1
            self.started = True
            self.awaiting_ready = False
            self.motion_ticks = 0
            self.trigger_controller_timestamp_s = None
            return True

        if self.awaiting_ready:
            if state != READY_HOME_STATE:
                return False
            if _integer(output, 30) != self.command_seq:
                raise CertificationProtocolError(
                    "TP READY_HOME consumed sequence differs"
                )
            self.step_index += 1
            self.started = False
            self.awaiting_ready = False
            self.hold_heartbeat = False
            self.stop_request = False
            self.trigger_controller_timestamp_s = None
            if self.step_index == len(self.steps):
                self.complete = True
                return False
            return self.poll(output)

        if state == READY_HOME_STATE:
            return False
        if not self._identity_matches(output):
            raise CertificationProtocolError("TP certification identity differs")
        if state == MOTION_STATE:
            self.motion_ticks += 1
            if self.motion_ticks > MAX_MOTION_TICKS_WITHOUT_TRIGGER:
                raise CertificationProtocolError(
                    "certification motion did not reach its trigger"
                )
            if _linear_speed(output) >= TRIGGER_SPEED_M_S:
                if self.trigger_controller_timestamp_s is None:
                    try:
                        trigger_timestamp = float(output["timestamp"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise CertificationProtocolError(
                            "TP feedback lacks controller trigger timestamp"
                        ) from exc
                    if not math.isfinite(trigger_timestamp) or trigger_timestamp <= 0.0:
                        raise CertificationProtocolError(
                            "controller trigger timestamp is invalid"
                        )
                    self.trigger_controller_timestamp_s = trigger_timestamp
                if step.procedure == "direct_exact_stop":
                    self.stop_request = True
                elif step.procedure == "stale_watchdog_exact_stop":
                    self.hold_heartbeat = True
            return False
        if state in {STOP_TRIGGERED_STATE, STATIONARY_STATE}:
            self.stop_request = False
            self.hold_heartbeat = False
            return False
        if state in {
            RETURN_PREPARING_STATE,
            RETURN_PREPARED_STATE,
            SAFE_CLOSURE_STATE,
        }:
            if state == SAFE_CLOSURE_STATE:
                self.awaiting_ready = True
            return False
        raise CertificationProtocolError(
            f"unexpected TP certification state={state}"
        )


__all__ = [
    "COMMAND_DIRECT_STOP",
    "COMMAND_RETURN_ROUTE",
    "COMMAND_STALE_STOP",
    "CertificationProtocolError",
    "CertificationSession",
    "CertificationStep",
    "EXECUTION_PROFILE_ID",
    "PROCEDURE_COMMANDS",
    "SAMPLES_PER_STOP_PROCEDURE",
    "certification_steps",
]
