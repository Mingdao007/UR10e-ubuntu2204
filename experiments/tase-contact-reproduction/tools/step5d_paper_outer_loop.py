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
    control_reaction_normal_base: tuple[float, float, float] = (0.0, 0.0, -1.0)


@dataclass(frozen=True)
class Step5dOuterLoopOutput:
    xdot_p: tuple[float, float, float]
    xdot_o: tuple[float, float, float]
    xdot_c: tuple[float, float, float, float, float, float]
    cmd_valid: bool
    next_state: Step5dOuterLoopState
    diagnostics: dict[str, Any]


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
) -> Step5dOuterLoopOutput:
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
        return Step5dOuterLoopOutput(
            xdot_p=_tuple3(xdot_zero),
            xdot_o=_tuple3(xdot_zero),
            xdot_c=_tuple6(np.zeros(6, dtype=float)),
            cmd_valid=False,
            next_state=state,
            diagnostics={
                "cmd_valid": False,
                "freeze_reason": "invalid_command_or_control_normal",
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
    force_integral = clamp(
        state.force_integral_n_s + e_f * dt_s,
        -abs(float(config.force_integral_limit_n_s)),
        abs(float(config.force_integral_limit_n_s)),
    )
    xdot_p_prev = _finite_array(state.xdot_p_prev_m_s, (3,), "xdot_p_prev_m_s")
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
