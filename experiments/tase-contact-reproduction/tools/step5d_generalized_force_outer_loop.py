#!/usr/bin/env python3
"""Offline, model-free generalized force outer loop.

The primitive keeps the original force-domain architecture: a measured force
error drives a bounded integral state and a scalar normal velocity command.
Optional nonlinear integral and filtered force-rate damping terms are explicit
configuration, so an offline study can compare them without touching the live
bridge or the existing paper-form outer loop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np


GENERALIZED_FORCE_SCHEMA = "step5d.offline/generalized-force-outer-loop-v1"
NORMAL_MODE_PROVIDED = "provided"
NORMAL_MODE_MEASURED_ONLY = "measured-only-v1"


class GeneralizedForceError(ValueError):
    """An offline generalized-force input or state violates its contract."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise GeneralizedForceError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GeneralizedForceError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise GeneralizedForceError(f"{name} must be finite")
    return number


def _vec3(value: Any, name: str) -> np.ndarray:
    try:
        vector = np.asarray(value, dtype=float)
    except (TypeError, ValueError) as exc:
        raise GeneralizedForceError(f"{name} must be a finite 3-vector") from exc
    if vector.shape != (3,) or not np.all(np.isfinite(vector)):
        raise GeneralizedForceError(f"{name} must be a finite 3-vector")
    return vector


def _tuple3(value: np.ndarray) -> tuple[float, float, float]:
    return tuple(float(item) for item in value.reshape(3))  # type: ignore[return-value]


def _unit(value: Any, fallback: Any, *, name: str, min_norm: float) -> tuple[np.ndarray, bool, float]:
    vector = _vec3(value, name)
    norm = float(np.linalg.norm(vector))
    if norm >= min_norm:
        return vector / norm, True, norm
    fallback_vector = _vec3(fallback, f"{name}_fallback")
    fallback_norm = float(np.linalg.norm(fallback_vector))
    if fallback_norm < min_norm:
        raise GeneralizedForceError(f"{name} and fallback have insufficient norm")
    return fallback_vector / fallback_norm, False, norm


def _clip(value: float, limit: float) -> float:
    return min(limit, max(-limit, value))


def _slerp_unit(source: np.ndarray, target: np.ndarray, fraction: float) -> np.ndarray:
    src = source / float(np.linalg.norm(source))
    dst = target / float(np.linalg.norm(target))
    fraction = min(1.0, max(0.0, float(fraction)))
    dot = float(np.clip(np.dot(src, dst), -1.0, 1.0))
    if dot < 0.0:
        dst = -dst
        dot = -dot
    angle = math.acos(dot)
    if angle < 1e-10:
        return dst
    sin_angle = math.sin(angle)
    result = (
        math.sin((1.0 - fraction) * angle) / sin_angle * src
        + math.sin(fraction * angle) / sin_angle * dst
    )
    return result / float(np.linalg.norm(result))


def nonlinear_integral_term(
    integral_error_n_s: float,
    *,
    linear_gain_m_s_per_n_s: float,
    quadratic_ratio: float,
    transition_n_s: float,
) -> float:
    """Return an odd, sector-bounded integral authority term.

    The second term behaves like ``z*abs(z)`` near zero and like a bounded
    additional linear gain for large ``abs(z)``.  Thus it is a safe analogue
    of an ``I`` second-order enhancement, rather than an even ``z**2`` term.
    """

    z = _finite(integral_error_n_s, "integral_error_n_s")
    ki = _finite(linear_gain_m_s_per_n_s, "linear_integral_gain")
    ratio = _finite(quadratic_ratio, "quadratic_ratio")
    transition = _finite(transition_n_s, "transition_n_s")
    if ki < 0.0 or ratio < 0.0 or transition <= 0.0:
        raise GeneralizedForceError("nonlinear integral parameters are invalid")
    return ki * z * (1.0 + ratio * abs(z) / (transition + abs(z)))


def force_rate_shape(
    rate_n_per_s: float,
    *,
    scale_n_per_s: float,
    exponent: float,
    deadband_n_per_s: float = 0.0,
) -> float:
    """Return a bounded odd monotone force-rate shape.

    ``deadband_n_per_s`` makes the term data-gated: rates within the measured
    noise/low-dynamics band produce exactly zero authority.  The resulting
    map remains odd and monotone, so it cannot inject energy in the rate
    channel when it is used with a non-negative gain.
    """

    rate = _finite(rate_n_per_s, "force_rate_n_per_s")
    scale = _finite(scale_n_per_s, "force_rate_scale_n_per_s")
    power = _finite(exponent, "force_rate_exponent")
    deadband = _finite(deadband_n_per_s, "force_rate_deadband_n_per_s")
    if scale <= 0.0 or power < 1.0 or deadband < 0.0:
        raise GeneralizedForceError("force-rate parameters are invalid")
    excess = max(0.0, abs(rate) - deadband)
    normalized = excess / scale
    if normalized == 0.0:
        return 0.0
    # The shape is dimensionless; ``scale`` only normalizes the measured rate
    # so the acceleration-authority gain has an unambiguous m/s^2 unit.
    return math.copysign(math.tanh(normalized**power), rate)


@dataclass(frozen=True)
class GeneralizedForceConfigV1:
    """Bounded parameters for one offline generalized-force transition."""

    force_target_n: float = 5.0
    force_p_gain_m_s2_per_n: float = 1.0 / 12.0
    force_i_gain_m_s2_per_n_s: float = 1.0 / 12.0
    force_damping_s_inv: float = 550.0 / 12.0
    nonlinear_i_ratio: float = 0.0
    nonlinear_i_transition_n_s: float = 0.5
    force_rate_gain_m_s2: float = 0.0
    force_rate_scale_n_per_s: float = 1.0
    force_rate_exponent: float = 1.0
    force_rate_deadband_n_per_s: float = 0.0
    force_filter_tau_s: float = 0.05
    integral_limit_n_s: float = 5.0
    normal_velocity_limit_m_s: float = 0.003
    position_gain_s_inv: float = 4.0
    normal_filter_tau_s: float = 0.05
    normal_rate_limit_rad_s: float = 2.0
    min_force_n: float = 1e-6
    min_tangent_step_m: float = 1e-8
    fallback_reaction_normal_base: tuple[float, float, float] = (0.0, 0.0, -1.0)
    normal_mode: str = NORMAL_MODE_PROVIDED
    schema: str = GENERALIZED_FORCE_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != GENERALIZED_FORCE_SCHEMA or self.version != 1:
            raise GeneralizedForceError("generalized-force schema/version differs")
        positive = {
            "force_target_n": self.force_target_n,
            "force_p_gain_m_s2_per_n": self.force_p_gain_m_s2_per_n,
            "force_i_gain_m_s2_per_n_s": self.force_i_gain_m_s2_per_n_s,
            "force_damping_s_inv": self.force_damping_s_inv,
            "nonlinear_i_transition_n_s": self.nonlinear_i_transition_n_s,
            "force_filter_tau_s": self.force_filter_tau_s,
            "integral_limit_n_s": self.integral_limit_n_s,
            "normal_velocity_limit_m_s": self.normal_velocity_limit_m_s,
            "normal_filter_tau_s": self.normal_filter_tau_s,
            "normal_rate_limit_rad_s": self.normal_rate_limit_rad_s,
            "min_force_n": self.min_force_n,
            "min_tangent_step_m": self.min_tangent_step_m,
        }
        for name, value in positive.items():
            if _finite(value, name) <= 0.0:
                raise GeneralizedForceError(f"{name} must be positive")
        if _finite(self.position_gain_s_inv, "position_gain_s_inv") < 0.0:
            raise GeneralizedForceError("position_gain_s_inv must be non-negative")
        nonnegative = {
            "nonlinear_i_ratio": self.nonlinear_i_ratio,
            "force_rate_gain_m_s2": self.force_rate_gain_m_s2,
            "force_rate_deadband_n_per_s": self.force_rate_deadband_n_per_s,
        }
        for name, value in nonnegative.items():
            if _finite(value, name) < 0.0:
                raise GeneralizedForceError(f"{name} must be non-negative")
        if _finite(self.force_rate_scale_n_per_s, "force_rate_scale_n_per_s") <= 0.0:
            raise GeneralizedForceError("force_rate_scale_n_per_s must be positive")
        if _finite(self.force_rate_exponent, "force_rate_exponent") < 1.0:
            raise GeneralizedForceError("force_rate_exponent must be >= 1")
        if self.normal_mode not in {NORMAL_MODE_PROVIDED, NORMAL_MODE_MEASURED_ONLY}:
            raise GeneralizedForceError("unsupported normal_mode")
        _unit(
            self.fallback_reaction_normal_base,
            self.fallback_reaction_normal_base,
            name="fallback_reaction_normal_base",
            min_norm=self.min_force_n,
        )


@dataclass(frozen=True)
class GeneralizedForceStateV1:
    integral_error_n_s: float = 0.0
    normal_velocity_m_s: float = 0.0
    filtered_force_n: float = 0.0
    force_filter_initialized: bool = False
    normal_estimate_base: tuple[float, float, float] = (0.0, 0.0, -1.0)
    tcp_position_prev_base: tuple[float, float, float] | None = None


@dataclass(frozen=True)
class GeneralizedForceInputsV1:
    tcp_position_base: tuple[float, float, float]
    force_base_n: tuple[float, float, float]
    desired_position_base: tuple[float, float, float]
    desired_velocity_base: tuple[float, float, float]
    dt_s: float
    reaction_normal_base: tuple[float, float, float]
    command_valid: bool = True


@dataclass(frozen=True)
class GeneralizedForceOutputV1:
    xdot_base_m_s: tuple[float, float, float]
    normal_velocity_m_s: float
    force_error_n: float
    next_state: GeneralizedForceStateV1
    command_valid: bool
    diagnostics: dict[str, Any]


def _normal_estimate(
    config: GeneralizedForceConfigV1,
    state: GeneralizedForceStateV1,
    position: np.ndarray,
    force: np.ndarray,
    provided_reaction: np.ndarray,
    dt_s: float,
) -> tuple[np.ndarray, bool, float, str]:
    provided, provided_valid, _ = _unit(
        provided_reaction,
        config.fallback_reaction_normal_base,
        name="reaction_normal_base",
        min_norm=config.min_force_n,
    )
    if config.normal_mode == NORMAL_MODE_PROVIDED:
        return provided, provided_valid, 0.0, "provided"

    previous, previous_valid, _ = _unit(
        state.normal_estimate_base,
        provided,
        name="normal_estimate_base",
        min_norm=config.min_force_n,
    )
    force_candidate, force_valid, force_norm = _unit(
        force,
        previous,
        name="force_base_n",
        min_norm=config.min_force_n,
    )
    if not force_valid or force_norm < config.min_force_n:
        return previous, previous_valid, 0.0, "hold_low_force"

    tangent = None
    if state.tcp_position_prev_base is not None:
        delta = position - _vec3(state.tcp_position_prev_base, "tcp_position_prev_base")
        tangent_delta = delta - float(np.dot(delta, previous)) * previous
        tangent_norm = float(np.linalg.norm(tangent_delta))
        if tangent_norm >= config.min_tangent_step_m:
            tangent = tangent_delta / tangent_norm
    if tangent is None:
        candidate = force_candidate
        source = "force_direction_fallback"
    else:
        candidate_raw = force_candidate - float(np.dot(force_candidate, tangent)) * tangent
        candidate, candidate_valid, _ = _unit(
            candidate_raw,
            previous,
            name="projected_force_normal",
            min_norm=config.min_force_n,
        )
        if not candidate_valid:
            return previous, previous_valid, 0.0, "hold_degenerate_projection"
        source = "tangent_projected_force"
    if float(np.dot(candidate, previous)) < 0.0:
        candidate = -candidate
    alpha = 1.0 - math.exp(-dt_s / config.normal_filter_tau_s)
    filtered = _slerp_unit(previous, candidate, alpha)
    angle = math.acos(float(np.clip(np.dot(previous, filtered), -1.0, 1.0)))
    max_angle = config.normal_rate_limit_rad_s * dt_s
    if angle > max_angle > 0.0:
        filtered = _slerp_unit(previous, filtered, max_angle / angle)
        return filtered, True, max_angle / max(dt_s, 1e-12), f"{source}_rate_limited"
    return filtered, True, angle / max(dt_s, 1e-12), source


def step_generalized_force_outer_loop(
    config: GeneralizedForceConfigV1,
    state: GeneralizedForceStateV1,
    inputs: GeneralizedForceInputsV1,
) -> GeneralizedForceOutputV1:
    """Advance one offline generalized-force transition."""

    position = _vec3(inputs.tcp_position_base, "tcp_position_base")
    force = _vec3(inputs.force_base_n, "force_base_n")
    desired_position = _vec3(inputs.desired_position_base, "desired_position_base")
    desired_velocity = _vec3(inputs.desired_velocity_base, "desired_velocity_base")
    dt_s = _finite(inputs.dt_s, "dt_s")
    if dt_s <= 0.0:
        raise GeneralizedForceError("dt_s must be positive")
    normal, normal_valid, normal_rate, normal_source = _normal_estimate(
        config,
        state,
        position,
        force,
        _vec3(inputs.reaction_normal_base, "reaction_normal_base"),
        dt_s,
    )
    next_position = _tuple3(position)
    if not bool(inputs.command_valid) or not normal_valid:
        return GeneralizedForceOutputV1(
            xdot_base_m_s=(0.0, 0.0, 0.0),
            normal_velocity_m_s=0.0,
            force_error_n=0.0,
            next_state=state,
            command_valid=False,
            diagnostics={
                "schema": GENERALIZED_FORCE_SCHEMA,
                "freeze_reason": "invalid_command_or_normal",
                "normal_source": normal_source,
                "normal_rate_rad_s": normal_rate,
            },
        )

    normal_load_n = float(np.dot(force, normal))
    error_n = config.force_target_n - normal_load_n
    old_integral = _clip(state.integral_error_n_s, config.integral_limit_n_s)
    trial_integral = _clip(old_integral + error_n * dt_s, config.integral_limit_n_s)

    if state.force_filter_initialized:
        alpha_force = 1.0 - math.exp(-dt_s / config.force_filter_tau_s)
        filtered_force = state.filtered_force_n + alpha_force * (normal_load_n - state.filtered_force_n)
        force_rate = (normal_load_n - filtered_force) / config.force_filter_tau_s
    else:
        filtered_force = normal_load_n
        force_rate = 0.0

    integral_term = nonlinear_integral_term(
        trial_integral,
        linear_gain_m_s_per_n_s=config.force_i_gain_m_s2_per_n_s,
        quadratic_ratio=config.nonlinear_i_ratio,
        transition_n_s=config.nonlinear_i_transition_n_s,
    )
    rate_term = force_rate_shape(
        force_rate,
        scale_n_per_s=config.force_rate_scale_n_per_s,
        exponent=config.force_rate_exponent,
        deadband_n_per_s=config.force_rate_deadband_n_per_s,
    )
    acceleration_like = (
        config.force_p_gain_m_s2_per_n * error_n
        + integral_term
        - config.force_damping_s_inv * state.normal_velocity_m_s
        - config.force_rate_gain_m_s2 * rate_term
    )
    raw_velocity = state.normal_velocity_m_s + acceleration_like * dt_s
    velocity = _clip(raw_velocity, config.normal_velocity_limit_m_s)
    pushes_same_direction = (
        abs(raw_velocity) > config.normal_velocity_limit_m_s
        and math.copysign(1.0, raw_velocity) == math.copysign(1.0, error_n)
    )
    conditional_frozen = bool(pushes_same_direction and abs(error_n) > 0.0)
    if conditional_frozen:
        trial_integral = old_integral
        integral_term = nonlinear_integral_term(
            trial_integral,
            linear_gain_m_s_per_n_s=config.force_i_gain_m_s2_per_n_s,
            quadratic_ratio=config.nonlinear_i_ratio,
            transition_n_s=config.nonlinear_i_transition_n_s,
        )
        acceleration_like = (
            config.force_p_gain_m_s2_per_n * error_n
            + integral_term
            - config.force_damping_s_inv * state.normal_velocity_m_s
            - config.force_rate_gain_m_s2 * rate_term
        )
        raw_velocity = state.normal_velocity_m_s + acceleration_like * dt_s
        velocity = _clip(raw_velocity, config.normal_velocity_limit_m_s)

    tangent_projector = np.eye(3) - np.outer(normal, normal)
    motion = tangent_projector @ (
        desired_velocity + config.position_gain_s_inv * (desired_position - position)
    )
    approach = -normal
    command = motion + approach * velocity
    next_state = GeneralizedForceStateV1(
        integral_error_n_s=float(trial_integral),
        normal_velocity_m_s=float(velocity),
        filtered_force_n=float(filtered_force),
        force_filter_initialized=True,
        normal_estimate_base=_tuple3(normal),
        tcp_position_prev_base=next_position,
    )
    return GeneralizedForceOutputV1(
        xdot_base_m_s=_tuple3(command),
        normal_velocity_m_s=float(velocity),
        force_error_n=float(error_n),
        next_state=next_state,
        command_valid=True,
        diagnostics={
            "schema": GENERALIZED_FORCE_SCHEMA,
            "normal_load_n": normal_load_n,
            "force_error_n": error_n,
            "integral_error_n_s": trial_integral,
            "integral_term": integral_term,
            "force_rate_n_per_s": force_rate,
            "force_rate_term": rate_term,
            "raw_normal_velocity_m_s": raw_velocity,
            "normal_velocity_m_s": velocity,
            "normal_rate_rad_s": normal_rate,
            "normal_source": normal_source,
            "conditional_integral_frozen": conditional_frozen,
            "velocity_saturated": not math.isclose(raw_velocity, velocity, rel_tol=0.0, abs_tol=1e-15),
            "normal_base": _tuple3(normal),
            "tangent_projector": tuple(tuple(float(v) for v in row) for row in tangent_projector),
            "command_base_m_s": _tuple3(command),
        },
    )


__all__ = [
    "GENERALIZED_FORCE_SCHEMA",
    "GeneralizedForceConfigV1",
    "GeneralizedForceError",
    "GeneralizedForceInputsV1",
    "GeneralizedForceOutputV1",
    "GeneralizedForceStateV1",
    "NORMAL_MODE_MEASURED_ONLY",
    "NORMAL_MODE_PROVIDED",
    "force_rate_shape",
    "nonlinear_integral_term",
    "step_generalized_force_outer_loop",
]
