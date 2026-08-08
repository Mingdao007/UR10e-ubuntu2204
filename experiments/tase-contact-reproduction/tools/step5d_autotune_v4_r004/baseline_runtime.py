"""Pure 1-to-5 N ramp and 10 s hold state machine for V4 qualification."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum

from step5d_autotune_v4.contracts import TARGET_FORCE_N, V4Candidate, assert_runtime_target


class BaselinePhase(str, Enum):
    WAIT_ONE_NEWTON = "wait_one_newton"
    RAMP = "ramp"
    ACQUIRE = "acquire"
    HOLD = "hold"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class BaselineObservation:
    dt_s: float
    one_newton_latched: bool
    filtered_normal_n: float
    raw_normal_n: float
    force_norm_n: float
    torque_norm_nm: float
    sensor_fresh: bool
    stationary: bool


@dataclass(frozen=True)
class BaselineReadinessGate:
    filtered_min_n: float = 4.0
    filtered_max_n: float = 6.0
    raw_min_n: float = 3.0
    raw_max_n: float = 7.0
    force_norm_max_n: float = 7.0
    torque_norm_max_nm: float = 0.30

    def __post_init__(self) -> None:
        values = (
            self.filtered_min_n,
            self.filtered_max_n,
            self.raw_min_n,
            self.raw_max_n,
            self.force_norm_max_n,
            self.torque_norm_max_nm,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("baseline readiness gate must be finite")
        if not 0.0 <= self.filtered_min_n <= self.filtered_max_n < 15.0:
            raise ValueError("baseline filtered readiness range is invalid")
        if not 0.0 <= self.raw_min_n <= self.raw_max_n < 15.0:
            raise ValueError("baseline raw readiness range is invalid")
        if not 0.0 < self.force_norm_max_n < 20.0:
            raise ValueError("baseline readiness force norm must remain below hard stop")
        if not 0.0 < self.torque_norm_max_nm < 1.0:
            raise ValueError("baseline readiness torque norm must remain below hard stop")


@dataclass(frozen=True)
class BaselineHardLimits:
    max_abs_normal_n: float = 15.0
    max_force_norm_n: float = 20.0
    max_torque_norm_nm: float = 1.0

    def __post_init__(self) -> None:
        values = (
            self.max_abs_normal_n,
            self.max_force_norm_n,
            self.max_torque_norm_nm,
        )
        if not all(math.isfinite(value) and value > 0.0 for value in values):
            raise ValueError("baseline hard limits must be finite and positive")


@dataclass(frozen=True)
class BaselineState:
    phase: BaselinePhase = BaselinePhase.WAIT_ONE_NEWTON
    after_latch_s: float = 0.0
    readiness_dwell_s: float = 0.0
    hold_s: float = 0.0
    stop_reason: str = ""


@dataclass(frozen=True)
class BaselineCommand:
    phase: BaselinePhase
    internal_setpoint_n: float
    candidate_target_force_n: float
    approach_speed_m_s: float
    xy_velocity_m_s: tuple[float, float]
    angular_velocity_rad_s: tuple[float, float, float]
    stop: bool
    retract_allowed: bool
    auto_home: bool
    reason: str


def _finite_observation(observation: BaselineObservation) -> bool:
    values = (
        observation.dt_s,
        observation.filtered_normal_n,
        observation.raw_normal_n,
        observation.force_norm_n,
        observation.torque_norm_nm,
    )
    return all(math.isfinite(value) for value in values) and all(
        value >= 0.0
        for value in (
            observation.force_norm_n,
            observation.torque_norm_nm,
        )
    )


def _readiness(observation: BaselineObservation, gate: BaselineReadinessGate) -> bool:
    return (
        observation.sensor_fresh
        and gate.filtered_min_n <= observation.filtered_normal_n <= gate.filtered_max_n
        and gate.raw_min_n <= observation.raw_normal_n <= gate.raw_max_n
        and observation.force_norm_n <= gate.force_norm_max_n
        and observation.torque_norm_nm <= gate.torque_norm_max_nm
    )


def _hard_reason(observation: BaselineObservation, limits: BaselineHardLimits) -> str:
    if not _finite_observation(observation) or observation.dt_s <= 0.0:
        return "nonfinite_or_nonpositive_dt"
    if not observation.sensor_fresh:
        return "sensor_stale"
    if abs(observation.raw_normal_n) >= limits.max_abs_normal_n:
        return f"hard_abs_normal_{limits.max_abs_normal_n:g}n"
    if observation.force_norm_n >= limits.max_force_norm_n:
        return f"hard_force_norm_{limits.max_force_norm_n:g}n"
    if observation.torque_norm_nm >= limits.max_torque_norm_nm:
        return f"hard_torque_norm_{limits.max_torque_norm_nm:g}nm"
    return ""


def _setpoint(after_latch_s: float) -> float:
    return min(TARGET_FORCE_N, 1.0 + 0.5 * min(max(after_latch_s, 0.0), 8.0))


def step_baseline(
    candidate: V4Candidate,
    state: BaselineState,
    observation: BaselineObservation,
    *,
    required_hold_s: float = 10.0,
    readiness_gate: BaselineReadinessGate | None = None,
    hard_limits: BaselineHardLimits | None = None,
) -> tuple[BaselineState, BaselineCommand]:
    """Advance one actual-dt sample; no wall-clock or robot side effects."""
    assert_runtime_target(candidate, TARGET_FORCE_N)
    if not math.isfinite(required_hold_s) or not 0.1 <= required_hold_s <= 10.0:
        raise ValueError("required baseline hold must be within [0.1,10] seconds")
    gate = readiness_gate if readiness_gate is not None else BaselineReadinessGate()
    if not isinstance(gate, BaselineReadinessGate):
        raise TypeError("readiness_gate must be BaselineReadinessGate")
    limits = hard_limits if hard_limits is not None else BaselineHardLimits()
    if not isinstance(limits, BaselineHardLimits):
        raise TypeError("hard_limits must be BaselineHardLimits")
    if state.phase in {BaselinePhase.SUCCESS, BaselinePhase.FAILED}:
        return state, _command(candidate, state, observation)
    hard_reason = _hard_reason(observation, limits)
    if hard_reason:
        failed = replace(state, phase=BaselinePhase.FAILED, stop_reason=hard_reason)
        return failed, _command(candidate, failed, observation)
    dt = observation.dt_s
    if state.phase is BaselinePhase.WAIT_ONE_NEWTON:
        if not observation.one_newton_latched:
            return state, _command(candidate, state, observation)
        state = replace(state, phase=BaselinePhase.RAMP, after_latch_s=0.0)
    after_latch = state.after_latch_s + dt
    if after_latch > 30.0:
        failed = replace(
            state,
            phase=BaselinePhase.FAILED,
            after_latch_s=after_latch,
            stop_reason="five_newton_acquisition_timeout",
        )
        return failed, _command(candidate, failed, observation)
    phase = state.phase
    readiness_dwell = state.readiness_dwell_s
    hold_s = state.hold_s
    if phase is BaselinePhase.RAMP and after_latch >= 8.0:
        phase = BaselinePhase.ACQUIRE
    if phase is BaselinePhase.ACQUIRE:
        if _readiness(observation, gate):
            phase = BaselinePhase.HOLD
            readiness_dwell += dt
            hold_s = dt
        else:
            readiness_dwell = 0.0
            hold_s = 0.0
    elif phase is BaselinePhase.HOLD:
        if _readiness(observation, gate):
            readiness_dwell += dt
            hold_s += dt
        else:
            phase = BaselinePhase.ACQUIRE
            readiness_dwell = 0.0
            hold_s = 0.0
        if hold_s >= required_hold_s - 1e-9:
            phase = BaselinePhase.SUCCESS
    next_state = BaselineState(
        phase=phase,
        after_latch_s=after_latch,
        readiness_dwell_s=readiness_dwell,
        hold_s=hold_s,
        stop_reason="",
    )
    return next_state, _command(candidate, next_state, observation)


def _command(
    candidate: V4Candidate,
    state: BaselineState,
    observation: BaselineObservation,
) -> BaselineCommand:
    setpoint = (
        1.0
        if state.phase is BaselinePhase.WAIT_ONE_NEWTON
        else _setpoint(state.after_latch_s)
    )
    error = setpoint - observation.filtered_normal_n
    approach_speed = max(
        -0.0005, min(0.0005, candidate.force_p_gain * error)
    )
    # SUCCESS is a typed transition into RETRACT or PATH selection, not a
    # provider stop request.  A true stop request must remain stop-dominant.
    stop = state.phase is BaselinePhase.FAILED
    if state.phase in {BaselinePhase.SUCCESS, BaselinePhase.FAILED}:
        approach_speed = 0.0
    retract_allowed = (
        state.phase is BaselinePhase.SUCCESS
        and observation.sensor_fresh
        and observation.stationary
    )
    return BaselineCommand(
        phase=state.phase,
        internal_setpoint_n=setpoint,
        candidate_target_force_n=candidate.target_force_n,
        approach_speed_m_s=approach_speed,
        xy_velocity_m_s=(0.0, 0.0),
        angular_velocity_rad_s=(0.0, 0.0, 0.0),
        stop=stop,
        retract_allowed=retract_allowed,
        auto_home=False,
        reason=state.stop_reason,
    )


def baseline_unlock_allowed(completions: list[BaselineState]) -> bool:
    return (
        len(completions) >= 3
        and all(state.phase is BaselinePhase.SUCCESS for state in completions[-3:])
    )


__all__ = [
    "BaselineCommand",
    "BaselineHardLimits",
    "BaselineObservation",
    "BaselineReadinessGate",
    "BaselinePhase",
    "BaselineState",
    "baseline_unlock_allowed",
    "step_baseline",
]
