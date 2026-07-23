"""Transport-neutral strict-RNN command seam for Remote Control."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from step5d_control_contract import (
    ZERO6,
    ControlCandidate,
    SafetyDecision,
    SafetyEnvelope,
    Step5dObservation,
    StrictRnnControlPolicy,
    Vector6,
    _normal_motion_constraints_are_hard,
    apply_direction_preserving_slew,
    build_slew_compatible_reference,
)


REMOTE_GUARD_DT_MAX_S = 0.010

REMOTE_NUMERIC_FIELDS = (
    "sequence",
    "timestamp_s",
    "accepted",
    "stop_requested",
    "command_valid",
    "path_time_s",
    "force_error_n",
    "orientation_error_rad",
    "residual_norm",
    "active_bounds_count",
    "desired_approach_m_s",
    "predicted_approach_m_s",
    "reference_ramp_scale",
    "reference_ramp_active",
    *(f"raw_desired_twist_{index}" for index in range(6)),
    *(f"governed_desired_twist_{index}" for index in range(6)),
    *(f"reference_prior_qdot_{index}" for index in range(6)),
    *(f"candidate_qdot_{index}" for index in range(6)),
    *(f"predicted_twist_{index}" for index in range(6)),
    *(f"command_qdot_{index}" for index in range(6)),
)


@dataclass(frozen=True)
class RemoteJointCommand:
    sequence: int
    qdot: Vector6
    valid: bool
    stop_requested: bool
    decision_reason: str


@dataclass(frozen=True)
class RemoteControlStepResult:
    raw_candidate: ControlCandidate
    candidate: ControlCandidate
    decision: SafetyDecision
    command: RemoteJointCommand


class RemoteDeferredDiagnostics:
    """Prefaulted evidence buffer containing only RNN, safety, and command data."""

    def __init__(self, capacity: int) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("Remote diagnostics capacity must be a positive integer")
        self.capacity = capacity
        self.field_names = REMOTE_NUMERIC_FIELDS
        self.numeric = np.empty((capacity, len(self.field_names)), dtype=np.float64)
        self.numeric.fill(0.0)
        self.prefaulted = True
        self.reasons: list[str | None] = [None] * capacity
        self.actions: list[str | None] = [None] * capacity
        self.count = 0
        self.overflowed = False
        self._field = {name: index for index, name in enumerate(self.field_names)}
        self._raw_desired = self._slice("raw_desired_twist")
        self._governed_desired = self._slice("governed_desired_twist")
        self._reference_prior = self._slice("reference_prior_qdot")
        self._candidate_qdot = self._slice("candidate_qdot")
        self._predicted_twist = self._slice("predicted_twist")
        self._command_qdot = self._slice("command_qdot")

    def _slice(self, prefix: str) -> slice:
        return slice(self._field[f"{prefix}_0"], self._field[f"{prefix}_5"] + 1)

    @staticmethod
    def _scalar(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return math.nan

    @staticmethod
    def _vector(values: Any, length: int) -> np.ndarray:
        try:
            result = np.asarray(values, dtype=float)
        except (TypeError, ValueError, OverflowError):
            return np.full(length, math.nan, dtype=float)
        if result.shape != (length,):
            return np.full(length, math.nan, dtype=float)
        return result

    def reset_for_trial(self) -> None:
        self.count = 0
        self.overflowed = False

    def record(
        self,
        observation: Step5dObservation,
        candidate: ControlCandidate,
        decision: SafetyDecision,
        command: RemoteJointCommand,
    ) -> bool:
        if self.count >= self.capacity:
            self.overflowed = True
            return False
        if not decision.accepted and command.qdot != ZERO6:
            raise ValueError("rejected Remote candidate must record exact zero")
        row = self.numeric[self.count]
        field = self._field
        row[field["sequence"]] = self._scalar(observation.sequence)
        row[field["timestamp_s"]] = self._scalar(observation.timestamp_s)
        row[field["accepted"]] = 1.0 if decision.accepted else 0.0
        row[field["stop_requested"]] = 1.0 if command.stop_requested else 0.0
        row[field["command_valid"]] = 1.0 if command.valid else 0.0
        row[field["path_time_s"]] = self._scalar(observation.path_time_s)
        row[field["force_error_n"]] = self._scalar(observation.force_error_n)
        row[field["orientation_error_rad"]] = self._scalar(
            observation.orientation_error_rad
        )
        row[field["residual_norm"]] = self._scalar(candidate.residual_norm)
        row[field["active_bounds_count"]] = self._scalar(
            candidate.active_bounds_count
        )
        row[field["desired_approach_m_s"]] = float(
            decision.metrics.get("desired_approach_m_s", math.nan)
        )
        row[field["predicted_approach_m_s"]] = float(
            decision.metrics.get("predicted_approach_m_s", math.nan)
        )
        row[field["reference_ramp_scale"]] = self._scalar(
            observation.reference_ramp_scale
        )
        row[field["reference_ramp_active"]] = (
            1.0 if observation.reference_ramp_active else 0.0
        )
        row[self._raw_desired] = self._vector(
            observation.raw_desired_twist
            if observation.raw_desired_twist is not None
            else observation.desired_twist,
            6,
        )
        row[self._governed_desired] = self._vector(observation.desired_twist, 6)
        row[self._reference_prior] = self._vector(
            observation.reference_prior_qdot, 6
        )
        row[self._candidate_qdot] = self._vector(candidate.qdot, 6)
        row[self._predicted_twist] = self._vector(candidate.predicted_twist, 6)
        row[self._command_qdot] = self._vector(command.qdot, 6)
        self.reasons[self.count] = decision.reason
        self.actions[self.count] = decision.action
        self.count += 1
        return True


def _remote_command(
    observation: Step5dObservation, decision: SafetyDecision
) -> RemoteJointCommand:
    sequence = observation.sequence if type(observation.sequence) is int else 0
    if sequence < 0:
        sequence = 0
    return RemoteJointCommand(
        sequence=sequence,
        qdot=decision.qdot if decision.accepted else ZERO6,
        valid=decision.accepted or decision.action == "safe_hold",
        stop_requested=decision.action == "stop",
        decision_reason=decision.reason,
    )


def _failure_result(
    observation: Step5dObservation,
    *,
    reason: str,
    diagnostics: RemoteDeferredDiagnostics,
    solver_status: str = "invalid",
) -> RemoteControlStepResult:
    candidate = ControlCandidate(
        qdot=ZERO6,
        predicted_twist=ZERO6,
        residual_norm=math.inf,
        active_bounds_count=0,
        frame_id=observation.command_frame or "invalid",
        solver_status=solver_status,
        diagnostics={"contract_failure": reason},
    )
    decision = SafetyDecision(
        accepted=False,
        action="stop",
        reason=reason,
        qdot=ZERO6,
        metrics={},
    )
    command = _remote_command(observation, decision)
    if not diagnostics.record(observation, candidate, decision, command):
        raise RuntimeError("Remote diagnostics capacity exhausted")
    return RemoteControlStepResult(
        raw_candidate=candidate,
        candidate=candidate,
        decision=decision,
        command=command,
    )


def _candidate_pipeline(
    observation: Step5dObservation,
    raw_candidate: ControlCandidate,
    *,
    previous_qdot: Vector6 | None,
    safety_envelope: SafetyEnvelope,
    diagnostics: RemoteDeferredDiagnostics,
    max_slew_rad_s2: float,
    dt_max_s: float,
) -> RemoteControlStepResult:
    try:
        candidate = apply_direction_preserving_slew(
            observation,
            raw_candidate,
            previous_qdot=previous_qdot or ZERO6,
            dt_s=float(observation.dt_s),
            max_slew_rad_s2=float(max_slew_rad_s2),
            dt_max_s=float(dt_max_s),
            copy_diagnostics=False,
        )
        decision = safety_envelope.evaluate(observation, candidate)
    except (
        ValueError,
        RuntimeError,
        np.linalg.LinAlgError,
        FloatingPointError,
        OverflowError,
        AttributeError,
        TypeError,
    ) as exc:
        return _failure_result(
            observation,
            reason=f"remote_contract_structural_failure:{type(exc).__name__}",
            diagnostics=diagnostics,
            solver_status=str(getattr(raw_candidate, "solver_status", "invalid")),
        )
    command = _remote_command(observation, decision)
    if not diagnostics.record(observation, candidate, decision, command):
        raise RuntimeError("Remote diagnostics capacity exhausted")
    return RemoteControlStepResult(
        raw_candidate=raw_candidate,
        candidate=candidate,
        decision=decision,
        command=command,
    )


def remote_rnn_control_step(
    observation: Step5dObservation,
    policy: StrictRnnControlPolicy,
    *,
    previous_qdot: Vector6 | None,
    safety_envelope: SafetyEnvelope,
    diagnostics: RemoteDeferredDiagnostics,
    max_slew_rad_s2: float,
    dt_max_s: float = REMOTE_GUARD_DT_MAX_S,
) -> RemoteControlStepResult:
    """Produce one command with strict RNN as the sole nonzero candidate owner."""

    if not isinstance(policy, StrictRnnControlPolicy):
        return _failure_result(
            observation,
            reason="remote_policy_type_invalid",
            diagnostics=diagnostics,
        )
    governed = observation
    try:
        if _normal_motion_constraints_are_hard(observation.normal_motion_policy):
            governed = build_slew_compatible_reference(
                observation,
                previous_qdot=previous_qdot,
                max_slew_rad_s2=max_slew_rad_s2,
                dt_max_s=dt_max_s,
            )
        raw_candidate = policy.compute(governed)
        if not isinstance(raw_candidate, ControlCandidate):
            raise TypeError("strict RNN policy must return ControlCandidate")
    except Exception as exc:
        return _failure_result(
            governed,
            reason=f"strict_rnn_policy_failure:{type(exc).__name__}",
            diagnostics=diagnostics,
        )
    return _candidate_pipeline(
        governed,
        raw_candidate,
        previous_qdot=previous_qdot,
        safety_envelope=safety_envelope,
        diagnostics=diagnostics,
        max_slew_rad_s2=max_slew_rad_s2,
        dt_max_s=dt_max_s,
    )
