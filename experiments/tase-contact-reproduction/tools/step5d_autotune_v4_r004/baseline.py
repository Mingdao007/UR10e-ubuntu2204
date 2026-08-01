"""Bounded baseline primitive with separate pre-latch and post-latch timers."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum

from .contracts import POST_LATCH_TIMEOUT_S, PRE_LATCH_TIMEOUT_S, TARGET_FORCE_N, Candidate, assert_target


class BaselinePhase(str, Enum):
    PRE_LATCH_WAIT = "pre_latch_wait"
    POST_LATCH_RAMP = "post_latch_ramp"
    ACQUIRE = "acquire"
    HOLD = "hold"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True)
class BaselineObservation:
    dt_s: float
    sticky_one_newton_latched: int
    baseline_consecutive_successes: int
    filtered_normal_n: float
    raw_normal_n: float
    force_norm_n: float
    torque_norm_nm: float
    sensor_fresh: bool
    stationary: bool
    internal_setpoint_n: float = 1.0


@dataclass(frozen=True)
class BaselineState:
    phase: BaselinePhase = BaselinePhase.PRE_LATCH_WAIT
    pre_latch_elapsed_s: float = 0.0
    post_latch_elapsed_s: float = 0.0
    readiness_dwell_s: float = 0.0
    hold_s: float = 0.0
    last_setpoint_n: float = 1.0
    last_baseline_consecutive_successes: int = 0
    stop_reason: str = ""


@dataclass(frozen=True)
class BaselineCommand:
    phase: BaselinePhase
    internal_setpoint_n: float
    approach_speed_m_s: float
    stop: bool
    retract_allowed: bool
    reason: str


def validate_sticky_latch(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        raise ValueError("sticky_one_newton_latched must be exactly integer 0 or 1")
    return value


def validate_baseline_counter(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 3:
        raise ValueError("baseline consecutive-success counter must be integer in [0, 3]")
    return value


def _finite_observation(observation: BaselineObservation) -> bool:
    values = (
        observation.dt_s,
        observation.filtered_normal_n,
        observation.raw_normal_n,
        observation.force_norm_n,
        observation.torque_norm_nm,
        observation.internal_setpoint_n,
    )
    return all(math.isfinite(float(value)) for value in values) and all(
        float(value) >= 0.0
        for value in (
            observation.force_norm_n,
            observation.torque_norm_nm,
            observation.internal_setpoint_n,
        )
    )


def _hard_reason(observation: BaselineObservation) -> str:
    if not _finite_observation(observation) or observation.dt_s <= 0.0 or observation.dt_s >= 0.080:
        return "malformed_baseline_progress_dt"
    if not isinstance(observation.sensor_fresh, bool) or not observation.sensor_fresh:
        return "sensor_stale"
    if abs(observation.raw_normal_n) >= 15.0:
        return "hard_abs_normal_15n"
    if observation.force_norm_n >= 20.0:
        return "hard_force_norm_20n"
    if observation.torque_norm_nm >= 1.0:
        return "hard_torque_norm_1nm"
    if not 1.0 <= observation.internal_setpoint_n <= TARGET_FORCE_N:
        return "malformed_baseline_progress_setpoint"
    return ""


def _readiness(observation: BaselineObservation) -> bool:
    return (
        observation.sensor_fresh
        and 4.0 <= observation.filtered_normal_n <= 6.0
        and 3.0 <= observation.raw_normal_n <= 7.0
        and observation.force_norm_n <= 7.0
        and observation.torque_norm_nm <= 0.30
    )


def _command(state: BaselineState, observation: BaselineObservation) -> BaselineCommand:
    setpoint = 1.0 if state.phase is BaselinePhase.PRE_LATCH_WAIT else min(
        TARGET_FORCE_N, 1.0 + 0.5 * min(max(state.post_latch_elapsed_s, 0.0), 8.0)
    )
    error = setpoint - observation.filtered_normal_n
    approach_speed = max(-0.0005, min(0.0005, 0.0003535533906 * error))
    if state.phase in {BaselinePhase.SUCCESS, BaselinePhase.FAILED}:
        approach_speed = 0.0
    return BaselineCommand(
        phase=state.phase,
        internal_setpoint_n=setpoint,
        approach_speed_m_s=approach_speed,
        stop=state.phase is BaselinePhase.FAILED,
        retract_allowed=(
            state.phase is BaselinePhase.SUCCESS
            and observation.sensor_fresh
            and observation.stationary
        ),
        reason=state.stop_reason,
    )


def _failed(state: BaselineState, reason: str, observation: BaselineObservation) -> tuple[BaselineState, BaselineCommand]:
    result = replace(state, phase=BaselinePhase.FAILED, stop_reason=reason)
    return result, _command(result, observation)


def step_baseline(
    candidate: Candidate, state: BaselineState, observation: BaselineObservation
) -> tuple[BaselineState, BaselineCommand]:
    """Advance one bounded actual-dt sample without robot-side effects."""

    assert_target(candidate)
    try:
        validate_sticky_latch(observation.sticky_one_newton_latched)
        validate_baseline_counter(observation.baseline_consecutive_successes)
    except ValueError as exc:
        return _failed(state, str(exc), observation)
    hard_reason = _hard_reason(observation)
    if hard_reason:
        return _failed(state, hard_reason, observation)
    if observation.baseline_consecutive_successes < state.last_baseline_consecutive_successes:
        return _failed(state, "malformed_baseline_progress_regression", observation)
    if state.phase in {BaselinePhase.SUCCESS, BaselinePhase.FAILED}:
        return state, _command(state, observation)
    dt = float(observation.dt_s)
    if state.phase is BaselinePhase.PRE_LATCH_WAIT:
        if observation.sticky_one_newton_latched == 0:
            pre = state.pre_latch_elapsed_s + dt
            if pre > PRE_LATCH_TIMEOUT_S:
                return _failed(
                    replace(state, pre_latch_elapsed_s=pre),
                    "pre_latch_timeout_20s",
                    observation,
                )
            next_state = replace(state, pre_latch_elapsed_s=pre, last_setpoint_n=1.0)
            return next_state, _command(next_state, observation)
        # The first valid sticky latch starts a distinct timer.  No pre-latch
        # elapsed time is carried into this post-latch budget.
        next_state = replace(
            state,
            phase=BaselinePhase.POST_LATCH_RAMP,
            post_latch_elapsed_s=0.0,
            readiness_dwell_s=0.0,
            hold_s=0.0,
            last_setpoint_n=1.0,
            stop_reason="",
        )
        return next_state, _command(next_state, observation)
    if observation.sticky_one_newton_latched == 0:
        return _failed(
            state,
            "sticky_latch_regression",
            observation,
        )
    post = state.post_latch_elapsed_s + dt
    if post > POST_LATCH_TIMEOUT_S:
        return _failed(
            replace(state, post_latch_elapsed_s=post),
            "post_latch_force_acquisition_timeout_20s",
            observation,
        )
    if observation.internal_setpoint_n < state.last_setpoint_n - 1e-9:
        return _failed(
            replace(state, post_latch_elapsed_s=post),
            "malformed_baseline_progress_regression",
            observation,
        )
    if observation.internal_setpoint_n - state.last_setpoint_n > 0.5 * dt + 0.010000001:
        return _failed(
            replace(state, post_latch_elapsed_s=post),
            "malformed_baseline_progress_rise",
            observation,
        )
    phase = state.phase
    readiness_dwell = state.readiness_dwell_s
    hold_s = state.hold_s
    if phase is BaselinePhase.POST_LATCH_RAMP and post >= 8.0:
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
        pre_latch_elapsed_s=state.pre_latch_elapsed_s,
        post_latch_elapsed_s=post,
            readiness_dwell_s=readiness_dwell,
            hold_s=hold_s,
            last_setpoint_n=observation.internal_setpoint_n,
            last_baseline_consecutive_successes=observation.baseline_consecutive_successes,
            stop_reason="",
        )
    return next_state, _command(next_state, observation)


def baseline_unlock_allowed(counter: int) -> bool:
    return validate_baseline_counter(counter) >= 3


__all__ = [
    "BaselineCommand",
    "BaselineObservation",
    "BaselinePhase",
    "BaselineState",
    "baseline_unlock_allowed",
    "step_baseline",
    "validate_baseline_counter",
    "validate_sticky_latch",
]
