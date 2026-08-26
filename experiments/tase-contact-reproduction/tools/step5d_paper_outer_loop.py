#!/usr/bin/env python3
"""Offline Step5d paper-form outer loop.

This module converts the paper's force-motion and orientation-compliance
equations into an auditable task velocity xdot_c. It is intentionally offline:
it does not open the bridge, send URScript, or talk to the controller.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from contact_semantics import (
    approach_normal_from_reaction,
    desired_rotation_preserving_roll,
    force_error_n,
    force_motion_acceleration_base,
    orientation_axis_angle_error,
    signed_normal_load_n,
)


LEGACY_FORCE_INTEGRAL_POLICY = "legacy-clamp-v1"
CONDITIONAL_DOUBLE_CLAMP_POLICY = "conditional-double-clamp-v1"


def _finite_array(values: Any, shape: tuple[int, ...], name: str) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.shape != shape or not np.all(np.isfinite(array)):
        raise ValueError(f"{name} must be a finite array with shape {shape}")
    return array


def _finite_float(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _tuple3(values: np.ndarray) -> tuple[float, float, float]:
    return tuple(float(value) for value in values.reshape(3))  # type: ignore[return-value]


def _tuple4(values: np.ndarray) -> tuple[float, float, float, float]:
    return tuple(float(value) for value in values.reshape(4))  # type: ignore[return-value]


def _tuple6(values: np.ndarray) -> tuple[float, float, float, float, float, float]:
    return tuple(float(value) for value in values.reshape(6))  # type: ignore[return-value]


def _matrix_tuple(values: np.ndarray) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    return tuple(_tuple3(values[row, :]) for row in range(3))  # type: ignore[return-value]


@dataclass(frozen=True)
class Step5dOuterLoopConfig:
    kp: float = 4.0
    ko: float = 5.0
    orientation_gain_scale: float = 1.0
    kf: float = 1.0
    Md_scalar: float = 12.0
    Bd_scalar: float = 550.0
    force_target_n: float = 5.0
    force_integral_limit_n_s: float = 5.0
    force_integral_policy: str = LEGACY_FORCE_INTEGRAL_POLICY
    force_integral_authority_error_n: float = 0.5
    force_normal_velocity_limit_m_s: float | None = None
    min_force_norm_n: float = 1e-9
    control_reaction_normal_fallback_base: tuple[float, float, float] = (0.0, 0.0, -1.0)
    delay_T_s: float | None = None
    force_sign_convention: str = "step5_step6_positive_normal_load"


@dataclass(frozen=True)
class Step5dOuterLoopState:
    force_integral_n_s: float = 0.0
    xdot_p_prev_m_s: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class Step5dOuterLoopInputs:
    tcp_pose_base: tuple[float, float, float, float, float, float]
    tcp_speed_base: tuple[float, float, float, float, float, float]
    force_tcp_n: tuple[float, float, float]
    x_pd_base: tuple[float, float, float]
    xdot_pd_base: tuple[float, float, float]
    dt_s: float
    cmd_valid: bool = True
    integral_enabled: bool = True
    integral_reset_reason: str = ""
    control_reaction_normal_base: tuple[float, float, float] = (0.0, 0.0, -1.0)


@dataclass(frozen=True)
class Step5dOuterLoopOutput:
    xdot_p: tuple[float, float, float]
    xdot_o: tuple[float, float, float]
    xdot_c: tuple[float, float, float, float, float, float]
    cmd_valid: bool
    next_state: Step5dOuterLoopState
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class ConditionalAntiWindupResult:
    """One typed transition of ``conditional-double-clamp-v1``.

    ``force_p_gain``/``force_i_gain`` use the equivalent Step5b scalar
    convention.  The result therefore exposes ``i_term`` in the same
    acceleration-like units as ``P * error``.
    """

    schema: str
    integral_state_n_s: float
    effective_integral_limit_n_s: float
    i_term: float
    raw_normal_velocity_m_s: float
    applied_normal_velocity_m_s: float
    state_clamped: bool
    authority_clamped: bool
    conditional_frozen: bool
    velocity_saturated: bool
    reset_reason: str = ""


def conditional_double_clamp_step(
    *,
    force_error_n: float,
    integral_state_n_s: float,
    normal_velocity_m_s: float,
    dt_s: float,
    force_p_gain: float,
    force_i_gain: float,
    force_damping: float,
    normal_velocity_limit_m_s: float,
    state_limit_n_s: float = 1.0,
    authority_error_n: float = 0.5,
    integral_enabled: bool = True,
    reset_reason: str = "",
) -> ConditionalAntiWindupResult:
    """Apply R013 conditional integration and both integral clamps.

    A saturated trial freezes integration only when the current error pushes
    farther into the same saturation direction.  Opposite-sign error remains
    able to unwind the state.  This primitive performs no back-calculation.
    """

    values = {
        "force_error_n": force_error_n,
        "integral_state_n_s": integral_state_n_s,
        "normal_velocity_m_s": normal_velocity_m_s,
        "dt_s": dt_s,
        "force_p_gain": force_p_gain,
        "force_i_gain": force_i_gain,
        "force_damping": force_damping,
        "normal_velocity_limit_m_s": normal_velocity_limit_m_s,
        "state_limit_n_s": state_limit_n_s,
        "authority_error_n": authority_error_n,
    }
    parsed = {name: float(value) for name, value in values.items()}
    if not all(math.isfinite(value) for value in parsed.values()):
        raise ValueError("conditional-double-clamp-v1 inputs must be finite")
    if parsed["dt_s"] < 0.0:
        raise ValueError("conditional-double-clamp-v1 requires dt_s >= 0")
    if parsed["force_p_gain"] <= 0.0 or parsed["force_i_gain"] <= 0.0:
        raise ValueError("conditional-double-clamp-v1 requires P > 0 and I > 0")
    if parsed["force_damping"] < 0.0:
        raise ValueError("conditional-double-clamp-v1 requires damping >= 0")
    if parsed["normal_velocity_limit_m_s"] <= 0.0:
        raise ValueError("conditional-double-clamp-v1 requires a positive velocity limit")
    if parsed["state_limit_n_s"] <= 0.0 or parsed["authority_error_n"] <= 0.0:
        raise ValueError("conditional-double-clamp-v1 fixed limits must be positive")

    p_gain = parsed["force_p_gain"]
    i_gain = parsed["force_i_gain"]
    state_limit = parsed["state_limit_n_s"]
    authority_limit = parsed["authority_error_n"] * p_gain / i_gain
    effective_limit = min(state_limit, authority_limit)
    parsed_reset_reason = str(reset_reason).strip()
    if not integral_enabled and not parsed_reset_reason:
        raise ValueError("disabled conditional integration requires a reset reason")
    raw_old_state = 0.0 if not integral_enabled else parsed["integral_state_n_s"]
    state_limited_old = clamp(raw_old_state, -state_limit, state_limit)
    old_state = clamp(state_limited_old, -effective_limit, effective_limit)
    prior_state_clamped = not math.isclose(
        raw_old_state, state_limited_old, rel_tol=0.0, abs_tol=1e-15
    )
    prior_authority_clamped = not math.isclose(
        state_limited_old, old_state, rel_tol=0.0, abs_tol=1e-15
    )
    unbounded_trial = old_state + (
        parsed["force_error_n"] * parsed["dt_s"] if integral_enabled else 0.0
    )
    state_limited_trial = clamp(unbounded_trial, -state_limit, state_limit)
    trial_state = clamp(state_limited_trial, -effective_limit, effective_limit)
    state_clamped = prior_state_clamped or not math.isclose(
        unbounded_trial, state_limited_trial, rel_tol=0.0, abs_tol=1e-15
    )
    authority_clamped = prior_authority_clamped or not math.isclose(
        state_limited_trial, trial_state, rel_tol=0.0, abs_tol=1e-15
    )

    def raw_velocity(integral: float) -> float:
        acceleration = (
            p_gain * parsed["force_error_n"]
            + i_gain * integral
            - parsed["force_damping"] * parsed["normal_velocity_m_s"]
        )
        return parsed["normal_velocity_m_s"] + acceleration * parsed["dt_s"]

    raw_trial = raw_velocity(trial_state)
    velocity_limit = parsed["normal_velocity_limit_m_s"]
    pushes_upper = raw_trial > velocity_limit and parsed["force_error_n"] > 0.0
    pushes_lower = raw_trial < -velocity_limit and parsed["force_error_n"] < 0.0
    conditional_frozen = bool(integral_enabled and (pushes_upper or pushes_lower))
    next_state = old_state if conditional_frozen else trial_state
    raw_applied_state = raw_velocity(next_state)
    applied_velocity = clamp(raw_applied_state, -velocity_limit, velocity_limit)
    return ConditionalAntiWindupResult(
        schema=CONDITIONAL_DOUBLE_CLAMP_POLICY,
        integral_state_n_s=next_state,
        effective_integral_limit_n_s=effective_limit,
        i_term=i_gain * next_state,
        raw_normal_velocity_m_s=raw_applied_state,
        applied_normal_velocity_m_s=applied_velocity,
        state_clamped=state_clamped,
        authority_clamped=authority_clamped,
        conditional_frozen=conditional_frozen,
        velocity_saturated=not math.isclose(raw_applied_state, applied_velocity, rel_tol=0.0, abs_tol=1e-15),
        reset_reason=parsed_reset_reason if not integral_enabled else "",
    )


def clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


def normalize_vector(values: Any, fallback: Any, min_norm: float = 1e-9) -> tuple[np.ndarray, bool, float]:
    vector = _finite_array(values, (3,), "vector")
    fallback_vector = _finite_array(fallback, (3,), "fallback")
    norm = float(np.linalg.norm(vector))
    if norm < min_norm:
        fallback_norm = float(np.linalg.norm(fallback_vector))
        if fallback_norm < min_norm:
            raise ValueError("fallback vector norm is too small")
        return fallback_vector / fallback_norm, False, norm
    return vector / norm, True, norm


def rotvec_to_matrix(rotvec: Any) -> np.ndarray:
    rv = _finite_array(rotvec, (3,), "rotvec")
    theta = float(np.linalg.norm(rv))
    if theta < 1e-12:
        return np.eye(3)
    axis = rv / theta
    kx, ky, kz = axis
    K = np.array(
        [
            [0.0, -kz, ky],
            [kz, 0.0, -kx],
            [-ky, kx, 0.0],
        ],
        dtype=float,
    )
    return np.eye(3) + math.sin(theta) * K + (1.0 - math.cos(theta)) * (K @ K)


def rotation_matrix_to_quaternion(matrix: Any) -> np.ndarray:
    rotation = _finite_array(matrix, (3, 3), "rotation_matrix")
    trace = float(np.trace(rotation))
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        qw = 0.25 * scale
        qx = (rotation[2, 1] - rotation[1, 2]) / scale
        qy = (rotation[0, 2] - rotation[2, 0]) / scale
        qz = (rotation[1, 0] - rotation[0, 1]) / scale
    else:
        diag = np.diag(rotation)
        idx = int(np.argmax(diag))
        if idx == 0:
            scale = math.sqrt(max(0.0, 1.0 + rotation[0, 0] - rotation[1, 1] - rotation[2, 2])) * 2.0
            qw = (rotation[2, 1] - rotation[1, 2]) / scale
            qx = 0.25 * scale
            qy = (rotation[0, 1] + rotation[1, 0]) / scale
            qz = (rotation[0, 2] + rotation[2, 0]) / scale
        elif idx == 1:
            scale = math.sqrt(max(0.0, 1.0 + rotation[1, 1] - rotation[0, 0] - rotation[2, 2])) * 2.0
            qw = (rotation[0, 2] - rotation[2, 0]) / scale
            qx = (rotation[0, 1] + rotation[1, 0]) / scale
            qy = 0.25 * scale
            qz = (rotation[1, 2] + rotation[2, 1]) / scale
        else:
            scale = math.sqrt(max(0.0, 1.0 + rotation[2, 2] - rotation[0, 0] - rotation[1, 1])) * 2.0
            qw = (rotation[1, 0] - rotation[0, 1]) / scale
            qx = (rotation[0, 2] + rotation[2, 0]) / scale
            qy = (rotation[1, 2] + rotation[2, 1]) / scale
            qz = 0.25 * scale
    quaternion = np.array([qw, qx, qy, qz], dtype=float)
    return quaternion / float(np.linalg.norm(quaternion))


def quaternion_inverse(quaternion: Any) -> np.ndarray:
    q = _finite_array(quaternion, (4,), "quaternion")
    norm_sq = float(np.dot(q, q))
    if norm_sq < 1e-18:
        raise ValueError("quaternion norm is too small")
    return np.array([q[0], -q[1], -q[2], -q[3]], dtype=float) / norm_sq


def quaternion_multiply(left: Any, right: Any) -> np.ndarray:
    a = _finite_array(left, (4,), "left_quaternion")
    b = _finite_array(right, (4,), "right_quaternion")
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return np.array(
        [
            aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
        ],
        dtype=float,
    )


def quaternion_orientation_error(Q_d: Any, Q_cur: Any) -> tuple[np.ndarray, np.ndarray]:
    e_qua = quaternion_multiply(quaternion_inverse(Q_d), Q_cur)
    e_qua = e_qua / float(np.linalg.norm(e_qua))
    e_o = e_qua[1:4].copy()
    if e_qua[0] < 0.0:
        e_o = -e_o
    return e_qua, e_o


def compute_step5d_outer_loop(
    config: Step5dOuterLoopConfig,
    state: Step5dOuterLoopState,
    inputs: Step5dOuterLoopInputs,
    *,
    include_diagnostics: bool | str = True,
) -> Step5dOuterLoopOutput:
    """Compute one paper-form outer-loop update.

    ``include_diagnostics=False`` preserves the exact control calculation and
    state transition while omitting diagnostics.  ``"compact"`` retains only
    the fixed safety/log scalars used by the v30 bridge.  Full diagnostics
    remain the default for offline analysis and existing callers.
    """
    tcp_pose = _finite_array(inputs.tcp_pose_base, (6,), "tcp_pose_base")
    tcp_speed = _finite_array(inputs.tcp_speed_base, (6,), "tcp_speed_base")
    force_tcp = _finite_array(inputs.force_tcp_n, (3,), "force_tcp_n")
    x_pd = _finite_array(inputs.x_pd_base, (3,), "x_pd_base")
    xdot_pd = _finite_array(inputs.xdot_pd_base, (3,), "xdot_pd_base")
    dt_s = _finite_float(inputs.dt_s, "dt_s")
    if dt_s <= 0.0:
        raise ValueError("dt_s must be positive")
    Md = _finite_float(config.Md_scalar, "Md_scalar")
    if Md <= 0.0:
        raise ValueError("Md_scalar must be positive")
    Bd = _finite_float(config.Bd_scalar, "Bd_scalar")
    ko = _finite_float(config.ko, "ko")
    orientation_gain_scale = _finite_float(config.orientation_gain_scale, "orientation_gain_scale")
    if orientation_gain_scale < 0.0:
        raise ValueError("orientation_gain_scale must be non-negative")
    effective_ko = ko * orientation_gain_scale
    T_s = dt_s if config.delay_T_s is None else _finite_float(config.delay_T_s, "delay_T_s")
    if T_s < 0.0:
        raise ValueError("delay_T_s must be non-negative")

    x_p = tcp_pose[:3]
    R_cur = rotvec_to_matrix(tcp_pose[3:6])
    force_base = R_cur @ force_tcp
    force_norm_n = float(np.linalg.norm(force_base))
    control_reaction_normal_base, control_normal_valid, control_normal_input_norm = normalize_vector(
        inputs.control_reaction_normal_base,
        config.control_reaction_normal_fallback_base,
        config.min_force_norm_n,
    )
    approach_normal_base = approach_normal_from_reaction(control_reaction_normal_base)
    effective_cmd_valid = bool(inputs.cmd_valid and control_normal_valid)

    if not effective_cmd_valid:
        xdot_zero = np.zeros(3, dtype=float)
        next_state = (
            Step5dOuterLoopState()
            if config.force_integral_policy == CONDITIONAL_DOUBLE_CLAMP_POLICY
            else state
        )
        return Step5dOuterLoopOutput(
            xdot_p=_tuple3(xdot_zero),
            xdot_o=_tuple3(xdot_zero),
            xdot_c=_tuple6(np.zeros(6, dtype=float)),
            cmd_valid=False,
            next_state=next_state,
            diagnostics={
                "cmd_valid": False,
                "freeze_reason": "invalid_command_or_control_normal",
                "force_integral_policy": config.force_integral_policy,
                "integral_reset_reason": (
                    "invalid_command_or_control_normal"
                    if config.force_integral_policy == CONDITIONAL_DOUBLE_CLAMP_POLICY
                    else ""
                ),
                "control_normal_valid": control_normal_valid,
                "force_norm_n": force_norm_n,
                "control_normal_input_norm": control_normal_input_norm,
                "force_sign_convention": config.force_sign_convention,
            },
        )

    R_d = desired_rotation_preserving_roll(R_cur, approach_normal_base)
    Phi_E = np.diag([1.0, 1.0, 0.0])
    Phi_bar_E = np.eye(3) - Phi_E
    # Base-frame orthogonal projectors onto the desired task frame's tangent
    # plane (Phi_O) and normal line (Phi_bar_O). The former R_d.T @ Phi_E form
    # returned desired-frame vectors while the caller feeds xdot_c to a
    # base-frame Jacobian solver; with the tool z pointing down (R_d ~ 180 deg
    # flip) that inverted the force channel, commanding away from the surface
    # whenever the load was below target (Stage25.0 divergence, v11-v16).
    Phi_O = R_d @ Phi_E @ R_d.T
    Phi_bar_O = R_d @ Phi_bar_E @ R_d.T

    e_p = x_pd - x_p
    normal_load_n = signed_normal_load_n(force_base, control_reaction_normal_base)
    e_f = force_error_n(target_load_n=float(config.force_target_n), normal_load_n=normal_load_n)
    xdot_p_prev = _finite_array(state.xdot_p_prev_m_s, (3,), "xdot_p_prev_m_s")
    anti_windup: ConditionalAntiWindupResult | None = None
    if config.force_integral_policy == CONDITIONAL_DOUBLE_CLAMP_POLICY:
        velocity_limit = _finite_float(
            config.force_normal_velocity_limit_m_s,
            "force_normal_velocity_limit_m_s",
        )
        p_gain = 1.0 / Md
        i_gain = float(config.kf) / Md
        damping = Bd / Md
        normal_velocity = float(np.dot(xdot_p_prev, approach_normal_base))
        anti_windup = conditional_double_clamp_step(
            force_error_n=e_f,
            integral_state_n_s=state.force_integral_n_s,
            normal_velocity_m_s=normal_velocity,
            dt_s=dt_s,
            force_p_gain=p_gain,
            force_i_gain=i_gain,
            force_damping=damping,
            normal_velocity_limit_m_s=velocity_limit,
            state_limit_n_s=abs(float(config.force_integral_limit_n_s)),
            authority_error_n=float(config.force_integral_authority_error_n),
            integral_enabled=bool(inputs.integral_enabled),
            reset_reason=str(inputs.integral_reset_reason),
        )
        force_integral = anti_windup.integral_state_n_s
        xddot_p = force_motion_acceleration_base(
            force_error=e_f,
            force_integral=force_integral,
            kf=float(config.kf),
            Md=Md,
            Bd=Bd,
            xdot_p_prev_base=xdot_p_prev,
            reaction_normal=control_reaction_normal_base,
        )
        xdot_force_candidate = xdot_p_prev + xddot_p * T_s
        raw_normal_velocity = float(np.dot(xdot_force_candidate, approach_normal_base))
        xdot_force_candidate = xdot_force_candidate + (
            anti_windup.applied_normal_velocity_m_s - raw_normal_velocity
        ) * approach_normal_base
    elif config.force_integral_policy == LEGACY_FORCE_INTEGRAL_POLICY:
        integral_increment = (
            e_f * dt_s if bool(inputs.integral_enabled) else 0.0
        )
        force_integral = clamp(
            state.force_integral_n_s + integral_increment,
            -abs(float(config.force_integral_limit_n_s)),
            abs(float(config.force_integral_limit_n_s)),
        )
        xddot_p = force_motion_acceleration_base(
            force_error=e_f,
            force_integral=force_integral,
            kf=float(config.kf),
            Md=Md,
            Bd=Bd,
            xdot_p_prev_base=xdot_p_prev,
            reaction_normal=control_reaction_normal_base,
        )
        xdot_force_candidate = xdot_p_prev + xddot_p * T_s
    else:
        raise ValueError(f"unsupported force_integral_policy {config.force_integral_policy!r}")
    motion_component = Phi_O @ (xdot_pd + float(config.kp) * e_p)
    force_component = Phi_bar_O @ xdot_force_candidate
    xdot_p = motion_component + force_component

    Q_d = rotation_matrix_to_quaternion(R_d)
    Q_cur = rotation_matrix_to_quaternion(R_cur)
    e_qua, e_o = quaternion_orientation_error(Q_d, Q_cur)
    outer_orientation_angle_rad = orientation_axis_angle_error(R_cur, approach_normal_base)
    # e_o follows the paper's Q_d^-1 * Q_cur convention, which is a
    # desired-frame current-vs-desired error. Convert it to base frame and
    # negate it so the commanded angular velocity closes the approach-axis
    # error instead of amplifying it.
    xdot_o = -effective_ko * (R_d @ e_o)
    xdot_c = np.concatenate((xdot_p, xdot_o))
    next_state = Step5dOuterLoopState(
        force_integral_n_s=float(force_integral),
        xdot_p_prev_m_s=_tuple3(xdot_p),
    )
    if include_diagnostics is True:
        diagnostics = {
            "cmd_valid": True,
            "force_sign_convention": config.force_sign_convention,
            "control_normal_valid": control_normal_valid,
            "force_norm_n": force_norm_n,
            "control_normal_input_norm": control_normal_input_norm,
            "x_p": _tuple3(x_p),
            "x_pd": _tuple3(x_pd),
            "xdot_pd": _tuple3(xdot_pd),
            "e_p": _tuple3(e_p),
            "force_tcp": _tuple3(force_tcp),
            "force_base": _tuple3(force_base),
            "control_reaction_normal_base": _tuple3(control_reaction_normal_base),
            "approach_normal_base": _tuple3(approach_normal_base),
            "orientation_target_axis_base": _tuple3(approach_normal_base),
            "R_d_z_dot_R_cur_z": float(np.dot(R_d[:, 2], R_cur[:, 2])),
            "outer_orientation_angle_rad": outer_orientation_angle_rad,
            "R_d": _matrix_tuple(R_d),
            "Phi_E": _matrix_tuple(Phi_E),
            "Phi_bar_E": _matrix_tuple(Phi_bar_E),
            "Phi_O": _matrix_tuple(Phi_O),
            "Phi_bar_O": _matrix_tuple(Phi_bar_O),
            "normal_load_n": normal_load_n,
            "force_load_n": normal_load_n,
            "e_f": e_f,
            "force_integral_n_s": float(force_integral),
            "force_integral_policy": config.force_integral_policy,
            "integral_effective_limit_n_s": (
                anti_windup.effective_integral_limit_n_s
                if anti_windup is not None
                else abs(float(config.force_integral_limit_n_s))
            ),
            "integral_i_term": anti_windup.i_term if anti_windup is not None else float(config.kf) * force_integral / Md,
            "integral_raw_normal_velocity_m_s": anti_windup.raw_normal_velocity_m_s if anti_windup is not None else float(np.dot(xdot_force_candidate, approach_normal_base)),
            "integral_applied_normal_velocity_m_s": anti_windup.applied_normal_velocity_m_s if anti_windup is not None else float(np.dot(xdot_force_candidate, approach_normal_base)),
            "integral_state_clamped": anti_windup.state_clamped if anti_windup is not None else False,
            "integral_authority_clamped": anti_windup.authority_clamped if anti_windup is not None else False,
            "integral_conditional_frozen": anti_windup.conditional_frozen if anti_windup is not None else False,
            "integral_velocity_saturated": anti_windup.velocity_saturated if anti_windup is not None else False,
            "integral_reset_reason": anti_windup.reset_reason if anti_windup is not None else "",
            "xddot_p": _tuple3(xddot_p),
            "xdot_force_candidate": _tuple3(xdot_force_candidate),
            "motion_component": _tuple3(motion_component),
            "force_component": _tuple3(force_component),
            "Q_d": _tuple4(Q_d),
            "Q_cur": _tuple4(Q_cur),
            "e_qua": _tuple4(e_qua),
            "e_o": _tuple3(e_o),
            "ko": ko,
            "orientation_gain_scale": orientation_gain_scale,
            "effective_ko": effective_ko,
            "xdot_p": _tuple3(xdot_p),
            "xdot_o": _tuple3(xdot_o),
            "xdot_c": _tuple6(xdot_c),
            "dt_s": dt_s,
            "T_s": T_s,
            "tcp_speed_base": _tuple6(tcp_speed),
        }
    elif include_diagnostics == "compact":
        diagnostics = {
            "force_sign_convention": config.force_sign_convention,
            "normal_load_n": normal_load_n,
            "e_f": e_f,
            "outer_orientation_angle_rad": outer_orientation_angle_rad,
            "R_d_z_dot_R_cur_z": float(np.dot(R_d[:, 2], R_cur[:, 2])),
            "force_integral_n_s": float(force_integral),
            "force_integral_policy": config.force_integral_policy,
            "integral_effective_limit_n_s": (
                anti_windup.effective_integral_limit_n_s
                if anti_windup is not None
                else abs(float(config.force_integral_limit_n_s))
            ),
            "integral_i_term": anti_windup.i_term if anti_windup is not None else float(config.kf) * force_integral / Md,
            "integral_raw_normal_velocity_m_s": anti_windup.raw_normal_velocity_m_s if anti_windup is not None else float(np.dot(xdot_force_candidate, approach_normal_base)),
            "integral_applied_normal_velocity_m_s": anti_windup.applied_normal_velocity_m_s if anti_windup is not None else float(np.dot(xdot_force_candidate, approach_normal_base)),
            "integral_state_clamped": anti_windup.state_clamped if anti_windup is not None else False,
            "integral_authority_clamped": anti_windup.authority_clamped if anti_windup is not None else False,
            "integral_conditional_frozen": anti_windup.conditional_frozen if anti_windup is not None else False,
            "integral_velocity_saturated": anti_windup.velocity_saturated if anti_windup is not None else False,
            "integral_reset_reason": anti_windup.reset_reason if anti_windup is not None else "",
        }
    elif include_diagnostics is False:
        # Do not construct logging tuples/matrices in the 500 Hz hot path.
        # Safety metrics are recomputed from canonical numeric buffers by the
        # SafetyEnvelope, so an empty payload cannot weaken a guard.
        diagnostics = {}
    else:
        raise ValueError("include_diagnostics must be true, false, or 'compact'")
    return Step5dOuterLoopOutput(
        xdot_p=_tuple3(xdot_p),
        xdot_o=_tuple3(xdot_o),
        xdot_c=_tuple6(xdot_c),
        cmd_valid=True,
        next_state=next_state,
        diagnostics=diagnostics,
    )


def rnn_target_state_from_outer_loop(
    outer_output: Step5dOuterLoopOutput,
    *,
    J: Any,
    omega_minus: Any,
    omega_plus: Any,
    dt_s: float,
    epsilon: float | None = None,
    r: float | None = None,
) -> dict[str, Any]:
    return {
        "J": _finite_array(J, (6, 6), "J"),
        "xdot_c": np.asarray(outer_output.xdot_c, dtype=float),
        "omega_minus": _finite_array(omega_minus, (6,), "omega_minus"),
        "omega_plus": _finite_array(omega_plus, (6,), "omega_plus"),
        "dt": _finite_float(dt_s, "dt_s"),
        "epsilon": epsilon,
        "r": r,
        "cmd_valid": outer_output.cmd_valid,
    }
