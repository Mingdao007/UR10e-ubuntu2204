"""Pure 1-to-5 N ramp and 10 s hold state machine for V4 qualification."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum

from .contracts import TARGET_FORCE_N, V4Candidate, assert_runtime_target


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


def _readiness(observation: BaselineObservation) -> bool:
    return (
        observation.sensor_fresh
        and 4.0 <= observation.filtered_normal_n <= 6.0
        and 3.0 <= observation.raw_normal_n <= 7.0
        and observation.force_norm_n <= 7.0
        and observation.torque_norm_nm <= 0.30
    )


def _hard_reason(observation: BaselineObservation) -> str:
    if not _finite_observation(observation) or observation.dt_s <= 0.0:
        return "nonfinite_or_nonpositive_dt"
    if not observation.sensor_fresh:
        return "sensor_stale"
    if abs(observation.raw_normal_n) >= 15.0:
        return "hard_abs_normal_15n"
    if observation.force_norm_n >= 20.0:
        return "hard_force_norm_20n"
    if observation.torque_norm_nm >= 1.0:
        return "hard_torque_norm_1nm"
    return ""


def _setpoint(after_latch_s: float) -> float:
    return min(TARGET_FORCE_N, 1.0 + 0.5 * min(max(after_latch_s, 0.0), 8.0))


def step_baseline(
    candidate: V4Candidate,
    state: BaselineState,
    observation: BaselineObservation,
) -> tuple[BaselineState, BaselineCommand]:
    """Advance one actual-dt sample; no wall-clock or robot side effects."""
    assert_runtime_target(candidate, TARGET_FORCE_N)
    if state.phase in {BaselinePhase.SUCCESS, BaselinePhase.FAILED}:
        return state, _command(candidate, state, observation)
    hard_reason = _hard_reason(observation)
    if hard_reason:
        failed = replace(state, phase=BaselinePhase.FAILED, stop_reason=hard_reason)
        return failed, _command(candidate, failed, observation)
    dt = observation.dt_s
    if state.phase is BaselinePhase.WAIT_ONE_NEWTON:
        if not observation.one_newton_latched:
            return state, _command(candidate, state, observation)
        state = replace(state, phase=BaselinePhase.RAMP, after_latch_s=0.0)
    after_latch = state.after_latch_s + dt
    if after_latch > 20.0:
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
        readiness_dwell = readiness_dwell + dt if _readiness(observation) else 0.0
        if readiness_dwell >= 0.25 - 1e-12:
            phase = BaselinePhase.HOLD
            hold_s = 0.0
    elif phase is BaselinePhase.HOLD:
        if _readiness(observation):
            hold_s += dt
        else:
            phase = BaselinePhase.ACQUIRE
            readiness_dwell = 0.0
            hold_s = 0.0
        if hold_s >= 10.0 - 1e-9:
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
    "BaselineObservation",
    "BaselinePhase",
    "BaselineState",
    "baseline_unlock_allowed",
    "step_baseline",
]
