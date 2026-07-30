"""Composition parent for injected V4 policies and fixed invariants."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .baseline import (
    BaselineCommand,
    BaselineObservation,
    BaselinePhase,
    BaselineState,
)
from .baseline_ledger import (
    BASELINE_SUCCESS_STAGE,
    BaselineQualificationLedger,
    BaselineSuccessReceipt,
)
from .contracts import TARGET_FORCE_N, V4Candidate, V4Contract, assert_runtime_target
from .policies import V4InvariantEnvelope, V4PolicyBundle
from .runtime import RuntimeGuardError


NON_FATAL_NO_MOTION_REASONS = {"baseline_qualification_pending"}


def _validate_baseline_command(
    candidate: V4Candidate,
    state: BaselineState,
    command: BaselineCommand,
) -> None:
    """Keep an injected baseline policy inside the fixed baseline envelope."""
    if not isinstance(command, BaselineCommand):
        raise RuntimeGuardError("baseline provider returned an untyped command")
    if (
        len(command.xy_velocity_m_s) != 2
        or len(command.angular_velocity_rad_s) != 3
        or not all(
            isinstance(value, bool)
            for value in (command.stop, command.retract_allowed, command.auto_home)
        )
    ):
        raise RuntimeGuardError("baseline provider command schema differs")
    numeric = (
        command.internal_setpoint_n,
        command.candidate_target_force_n,
        command.approach_speed_m_s,
        *command.xy_velocity_m_s,
        *command.angular_velocity_rad_s,
    )
    if not all(math.isfinite(float(value)) for value in numeric):
        raise RuntimeGuardError("baseline provider returned nonfinite command")
    if command.phase is not state.phase:
        raise RuntimeGuardError("baseline provider command phase differs")
    if not math.isclose(
        command.candidate_target_force_n, candidate.target_force_n,
        rel_tol=0.0, abs_tol=1e-12,
    ):
        raise RuntimeGuardError("baseline provider target differs")
    if not 1.0 <= command.internal_setpoint_n <= TARGET_FORCE_N:
        raise RuntimeGuardError("baseline provider setpoint is outside 1-to-5 N")
    if abs(command.approach_speed_m_s) > 0.0005 + 1e-12:
        raise RuntimeGuardError("baseline provider normal speed exceeds cap")
    if any(abs(value) > 1e-12 for value in command.xy_velocity_m_s):
        raise RuntimeGuardError("baseline provider returned tangential motion")
    if any(abs(value) > 1e-12 for value in command.angular_velocity_rad_s):
        raise RuntimeGuardError("baseline provider returned angular motion")
    if command.auto_home:
        raise RuntimeGuardError("baseline provider requested automatic Home")
    if command.retract_allowed and state.phase is not BaselinePhase.SUCCESS:
        raise RuntimeGuardError("baseline provider requested retract before success")


@dataclass(frozen=True)
class RuntimeObservation:
    monotonic_s: float
    heartbeat: float
    one_newton_latched: bool
    filtered_normal_n: float
    raw_normal_n: float
    force_norm_n: float
    torque_norm_nm: float
    sensor_fresh: bool
    stationary: bool
    safety_failure: bool = False
    structural_failure: bool = False


@dataclass(frozen=True)
class RuntimeDecision:
    phase: str
    qdot: tuple[float, float, float, float, float, float]
    internal_setpoint_n: float
    candidate_target_force_n: float
    stop: bool
    freeze_bo: bool
    retract_allowed: bool
    full_path_allowed: bool
    reason: str


@dataclass
class V4ControlPrimitive:
    contract: V4Contract
    candidate: V4Candidate
    policies: V4PolicyBundle = field(default_factory=V4PolicyBundle.defaults)
    invariant_envelope: V4InvariantEnvelope = field(
        default_factory=V4InvariantEnvelope
    )
    baseline_ledger: BaselineQualificationLedger | None = None
    attempt_id: str = "offline-attempt"
    baseline_state: BaselineState = field(default_factory=BaselineState)
    startup_ready: bool = False
    frozen: bool = False
    freeze_reason: str = ""
    _baseline_receipt_recorded: bool = False

    def __post_init__(self) -> None:
        self.invariant_envelope.enforce_candidate(self.candidate)
        self.policies.__post_init__()
        self.policies.assert_bound(self.contract)
        if self.baseline_ledger is None:
            self.baseline_ledger = BaselineQualificationLedger(self.contract)

    @property
    def successful_baseline_holds(self) -> int:
        assert self.baseline_ledger is not None
        return self.baseline_ledger.snapshot().consecutive_successes

    def _freeze(self, reason: str, *, terminal_stage: int = 0) -> None:
        self.frozen = True
        self.freeze_reason = reason
        assert self.baseline_ledger is not None
        self.baseline_ledger.record_failure(
            attempt_id=self.attempt_id,
            terminal_stage=terminal_stage,
            reason=reason,
            safety_or_structural=True,
        )

    def step(
        self,
        observation: RuntimeObservation,
        *,
        proposed_qdot: Sequence[float],
        jacobian_6x6: Sequence[Sequence[float]],
        normal_base: Sequence[float],
        observed_model_hashes: Mapping[str, str],
    ) -> RuntimeDecision:
        assert_runtime_target(self.candidate, TARGET_FORCE_N)
        self.invariant_envelope.enforce_candidate(self.candidate)
        try:
            dt = self.policies.timing.observe(observation.monotonic_s)
            startup_ready = self.policies.timing.observe_startup(
                observation.heartbeat, observation.monotonic_s
            )
            timing_stopped = self.policies.timing.stopped
            timing_stop_reason = self.policies.timing.stop_reason
        except Exception as exc:
            self._freeze(f"timing_provider:{type(exc).__name__}")
            return self._stop(self.freeze_reason, freeze=True)
        if not isinstance(startup_ready, bool) or not isinstance(
            timing_stopped, bool
        ) or not isinstance(timing_stop_reason, str):
            self._freeze("timing_provider:gate_state_untyped")
            return self._stop(self.freeze_reason, freeze=True)
        if dt is not None and (
            isinstance(dt, bool)
            or not isinstance(dt, (int, float))
            or not math.isfinite(float(dt))
            or float(dt) <= 0.0
            or float(dt) >= 0.08
        ):
            self._freeze("timing_provider:dt_outside_contract")
            return self._stop(self.freeze_reason, freeze=True)
        self.startup_ready = startup_ready
        if observation.safety_failure or observation.structural_failure:
            self._freeze(
                (
                "safety_failure"
                if observation.safety_failure
                else "structural_failure"
                )
            )
        if timing_stopped:
            self._freeze(timing_stop_reason or "timing_provider_stopped")
        if self.frozen:
            return self._stop(self.freeze_reason, freeze=True)
        for role in ("entry", "force_search", "candidate_provider"):
            if role in self.policies.no_motion_roles:
                return self._stop(f"{role}_provider_unavailable", freeze=False)
        if not self.startup_ready:
            return self._stop("startup_two_increments_pending", freeze=False)
        if dt is None:
            return self._stop("first_actual_dt_pending", freeze=False)
        baseline_observation = BaselineObservation(
            dt_s=dt,
            one_newton_latched=observation.one_newton_latched,
            filtered_normal_n=observation.filtered_normal_n,
            raw_normal_n=observation.raw_normal_n,
            force_norm_n=observation.force_norm_n,
            torque_norm_nm=observation.torque_norm_nm,
            sensor_fresh=observation.sensor_fresh,
            stationary=observation.stationary,
        )
        try:
            self.baseline_state, baseline_command = self.policies.baseline.step(
                self.candidate, self.baseline_state, baseline_observation
            )
        except Exception as exc:
            self._freeze(
                f"baseline_provider:{type(exc).__name__}", terminal_stage=21
            )
            return self._stop(self.freeze_reason, freeze=True)
        try:
            _validate_baseline_command(
                self.candidate, self.baseline_state, baseline_command
            )
        except Exception as exc:
            self._freeze(f"baseline_command:{exc}", terminal_stage=21)
            return self._stop(self.freeze_reason, freeze=True)
        if self.baseline_state.phase is BaselinePhase.FAILED:
            self.frozen = True
            self.freeze_reason = self.baseline_state.stop_reason
            assert self.baseline_ledger is not None
            self.baseline_ledger.record_failure(
                attempt_id=self.attempt_id,
                terminal_stage=21,
                reason=self.freeze_reason,
                safety_or_structural=False,
            )
            return self._stop(self.freeze_reason, freeze=True)
        if (
            self.baseline_state.phase is BaselinePhase.SUCCESS
            and not self._baseline_receipt_recorded
        ):
            completion_sha = hashlib.sha256(
                (
                    f"{self.attempt_id}:{self.contract.campaign_fingerprint}:"
                    f"{self.candidate.candidate_uid}:{BASELINE_SUCCESS_STAGE}"
                ).encode("utf-8")
            ).hexdigest()
            assert self.baseline_ledger is not None
            self.baseline_ledger.record_success(
                BaselineSuccessReceipt(
                    attempt_id=self.attempt_id,
                    terminal_stage=BASELINE_SUCCESS_STAGE,
                    campaign_fingerprint=self.contract.campaign_fingerprint,
                    eoat_sha256=self.contract.eoat_sha256,
                    target_force_n=TARGET_FORCE_N,
                    sensor_authority="kunwei_only",
                    completion_sha256=completion_sha,
                )
            )
            self._baseline_receipt_recorded = True
        if baseline_command.stop:
            return self._stop(
                baseline_command.reason or "baseline_policy_stop", freeze=False
            )
        try:
            gate = self.policies.qdot_gate.gate(
                self.contract,
                qdot=proposed_qdot,
                jacobian_6x6=jacobian_6x6,
                normal_base=normal_base,
                observed_model_hashes=observed_model_hashes,
            )
            gate = self.invariant_envelope.enforce_gate(gate)
        except Exception as exc:
            self._freeze(f"kinematic_gate:{type(exc).__name__}:{exc}")
            return self._stop(self.freeze_reason, freeze=True)
        qdot = gate.qdot
        reason = gate.reason
        baseline_active = self.baseline_state.phase not in {
            BaselinePhase.SUCCESS,
            BaselinePhase.FAILED,
        }
        if baseline_active and (
            gate.tangential_m_s > 1e-9 or gate.angular_rad_s > 1e-9
        ):
            qdot = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            reason = "baseline_forbids_xy_or_angular_motion"
        assert self.baseline_ledger is not None
        qualification = self.baseline_ledger.snapshot()
        full_path_allowed = (
            self.baseline_state.phase is BaselinePhase.SUCCESS
            and qualification.full_path_allowed
        )
        if self.baseline_state.phase is BaselinePhase.SUCCESS and not full_path_allowed:
            # A single successful hold can only retract or remain in a zero-
            # qdot hold.  It must never become an implicit PATH admission.
            qdot = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            reason = "baseline_qualification_pending"
        return RuntimeDecision(
            phase=self.baseline_state.phase.value,
            qdot=qdot,
            internal_setpoint_n=baseline_command.internal_setpoint_n,
            candidate_target_force_n=baseline_command.candidate_target_force_n,
            stop=(
                not gate.allowed
                or (bool(reason) and reason not in NON_FATAL_NO_MOTION_REASONS)
            ),
            freeze_bo=False,
            retract_allowed=baseline_command.retract_allowed,
            full_path_allowed=full_path_allowed,
            reason=reason,
        )

    def _stop(self, reason: str, *, freeze: bool) -> RuntimeDecision:
        return RuntimeDecision(
            phase="frozen" if freeze else "startup",
            qdot=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            internal_setpoint_n=1.0,
            candidate_target_force_n=TARGET_FORCE_N,
            stop=True,
            freeze_bo=freeze,
            retract_allowed=False,
            full_path_allowed=False,
            reason=reason,
        )


__all__ = [
    "RuntimeDecision",
    "RuntimeObservation",
    "V4ControlPrimitive",
]
