#!/usr/bin/env python3
"""Pure production target builder for Step5d strict-RNN P0 v8.

This module contains no simulator, ROS, RTDE, controller, or file I/O.  Both
the bridge compatibility wrappers and simulator adapters use these functions
so frame limiting and the weak-posture target cannot drift into separate
implementations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from contact_semantics import twist_base_to_same_origin, twist_same_origin_to_base
from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopOutput,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
    rotvec_to_matrix,
)


P0_V8_EFFECTIVE_KO = 0.01
P0_BASE_KO = 5.0
P0_LOW_LOAD_N = 1.0
P0_HIGH_LOAD_N = 2.0
P0_V8_POLICY = "weak_posture_v2"
P0_PRESS_ONLY_SPEED_M_S = 0.00015
P0_XY_COMPONENT_LIMIT_M_S = 0.010
P0_Z_COMPONENT_LIMIT_M_S = 0.020
P0_ANGULAR_COMPONENT_LIMIT_RAD_S = 0.015
P0_QDOT_CAP_RAD_S = 0.05


@dataclass(frozen=True)
class P0V8Target:
    desired_twist: tuple[float, float, float, float, float, float]
    raw_outer_twist: tuple[float, float, float, float, float, float]
    limited_twist: tuple[float, float, float, float, float, float]
    posture_policy: dict[str, Any]
    outer_diagnostics: dict[str, Any]
    frame_diagnostics: dict[str, Any]
    feasibility_diagnostics: dict[str, Any]
    limiter_active: bool


def _tuple6(values: Any) -> tuple[float, float, float, float, float, float]:
    array = np.asarray(values, dtype=float)
    if array.shape != (6,) or not np.all(np.isfinite(array)):
        raise ValueError("P0 target value must be a finite 6-vector")
    return tuple(float(value) for value in array)  # type: ignore[return-value]


def _normalized3(values: Sequence[float]) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    if array.shape != (3,) or not np.all(np.isfinite(array)):
        raise ValueError("P0 normal must be a finite 3-vector")
    norm = float(np.linalg.norm(array))
    if norm < 1e-9:
        raise ValueError("P0 normal norm is too small")
    return array / norm


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(float(value), float(lower)), float(upper))


def smoothstep01(value: float) -> float:
    x = min(max(float(value), 0.0), 1.0)
    return x * x * (3.0 - 2.0 * x)


def low_force_posture_policy(
    *,
    normal_load_n: float,
    base_ko: float = P0_BASE_KO,
    low_load_n: float = P0_LOW_LOAD_N,
    high_load_n: float = P0_HIGH_LOAD_N,
    low_ko: float = P0_V8_EFFECTIVE_KO,
    policy: str = P0_V8_POLICY,
) -> dict[str, Any]:
    base = float(base_ko)
    load = float(normal_load_n)
    low_load = float(low_load_n)
    high_load = float(high_load_n)
    weak = float(low_ko)
    if not math.isfinite(base) or base <= 0.0:
        raise ValueError("P0 posture base_ko must be finite and positive")
    if not math.isfinite(load):
        load = 0.0
    if not math.isfinite(low_load) or not math.isfinite(high_load) or high_load <= low_load:
        raise ValueError("P0 posture load schedule must be finite and increasing")
    if not math.isfinite(weak) or weak < 0.0:
        raise ValueError("P0 low-force posture ko must be finite and non-negative")
    gamma = smoothstep01((max(0.0, load) - low_load) / (high_load - low_load))
    effective_ko = weak * (1.0 - gamma) + base * gamma
    return {
        "policy": str(policy),
        "active": bool(gamma < 1.0 - 1e-12),
        "normal_load_n": max(0.0, load),
        "load_low_n": low_load,
        "load_high_n": high_load,
        "gamma": gamma,
        "low_ko": weak,
        "base_ko": base,
        "effective_ko": effective_ko,
        "orientation_gain_scale": effective_ko / base,
    }


def press_only_outer_output(
    *,
    reaction_normal_b: Sequence[float],
    force_error_n: float,
    press_speed_m_s: float = P0_PRESS_ONLY_SPEED_M_S,
) -> Step5dOuterLoopOutput:
    reaction = _normalized3(reaction_normal_b)
    speed = float(press_speed_m_s)
    if not math.isfinite(speed) or speed <= 0.0:
        raise ValueError("P0 press-only speed must be finite and positive")
    xdot_c = np.zeros(6, dtype=float)
    xdot_c[:3] = -reaction * speed
    xdot = _tuple6(xdot_c)
    return Step5dOuterLoopOutput(
        xdot_p=xdot[:3],
        xdot_o=xdot[3:],
        xdot_c=xdot,
        cmd_valid=True,
        next_state=Step5dOuterLoopState(),
        diagnostics={
            "outer_orientation_angle_rad": 0.0,
            "e_f": float(force_error_n) if math.isfinite(float(force_error_n)) else 0.0,
            "R_d_z_dot_R_cur_z": 1.0,
            "force_sign_convention": "step5_step6_positive_normal_load",
            "no_contact_p0_target_policy": "press_only_v1",
            "no_contact_p0_press_speed_m_s": speed,
        },
    )


def limit_p0_xdot_components(
    xdot_c: Any,
    *,
    rotation_base_from_tcp: Any,
    max_xy_m_s: float = P0_XY_COMPONENT_LIMIT_M_S,
    max_z_m_s: float = P0_Z_COMPONENT_LIMIT_M_S,
    max_angular_rad_s: float = P0_ANGULAR_COMPONENT_LIMIT_RAD_S,
    return_diagnostics: bool = False,
) -> tuple[np.ndarray, bool] | tuple[np.ndarray, bool, dict[str, Any]]:
    xdot = np.asarray(xdot_c, dtype=float)
    if xdot.shape != (6,) or not np.all(np.isfinite(xdot)):
        raise ValueError("P0 xdot_c must be a finite 6-vector")
    caps = np.asarray(
        [max_xy_m_s, max_xy_m_s, max_z_m_s, max_angular_rad_s, max_angular_rad_s, max_angular_rad_s],
        dtype=float,
    )
    if np.any(~np.isfinite(caps)) or np.any(caps <= 0.0):
        raise ValueError("P0 component velocity caps must be finite and positive")
    rotation = np.asarray(rotation_base_from_tcp, dtype=float)
    if rotation.shape != (3, 3) or not np.all(np.isfinite(rotation)):
        raise ValueError("P0 rotation_base_from_tcp must be a finite 3x3 matrix")
    raw_tcp = twist_base_to_same_origin(xdot, rotation)
    limited_tcp = raw_tcp.copy()
    limited_tcp[0] = _clamp(limited_tcp[0], -max_xy_m_s, max_xy_m_s)
    limited_tcp[1] = _clamp(limited_tcp[1], -max_xy_m_s, max_xy_m_s)
    limited_tcp[2] = _clamp(limited_tcp[2], 0.0, max_z_m_s)
    for index in range(3, 6):
        limited_tcp[index] = _clamp(limited_tcp[index], -max_angular_rad_s, max_angular_rad_s)
    limited_base = twist_same_origin_to_base(limited_tcp, rotation)
    active = bool(np.any(np.abs(limited_base - xdot) > 1e-9))
    diagnostics = {
        "valid": True,
        "mode": "tcp_same_origin_v1",
        "reason": "ok",
        "raw_base": xdot,
        "raw_tcp": raw_tcp,
        "limited_tcp": limited_tcp,
        "limited_base": limited_base,
        "tcp_press_speed_m_s": float(limited_tcp[2]),
    }
    return (limited_base, active, diagnostics) if return_diagnostics else (limited_base, active)


def scale_xdot_for_joint_feasibility(
    xdot_c: Any,
    jacobian: Any,
    *,
    qdot_cap_rad_s: float = P0_QDOT_CAP_RAD_S,
    safety: float = 0.9,
) -> tuple[np.ndarray, dict[str, Any]]:
    xdot = np.asarray(xdot_c, dtype=float)
    J = np.asarray(jacobian, dtype=float)
    cap = float(qdot_cap_rad_s)
    safety_factor = float(safety)
    if xdot.shape != (6,) or J.shape != (6, 6) or not np.all(np.isfinite(xdot)) or not np.all(np.isfinite(J)):
        raise ValueError("Step5d feasibility scaling expects finite 6-vector xdot and 6x6 Jacobian")
    if not math.isfinite(cap) or cap <= 0.0:
        raise ValueError("Step5d qdot cap must be finite and positive")
    if not math.isfinite(safety_factor) or not 0.0 < safety_factor <= 1.0:
        raise ValueError("Step5d feasibility safety factor must be in (0, 1]")
    qdot_required = np.linalg.solve(J, xdot)
    if qdot_required.shape != (6,) or not np.all(np.isfinite(qdot_required)):
        raise ValueError("Step5d feasibility solve produced nonfinite qdot")
    required_inf = float(np.max(np.abs(qdot_required)))
    scale = 1.0 if required_inf <= 0.0 else min(1.0, safety_factor * cap / required_inf)
    scaled = xdot * scale
    return scaled, {
        "qdot_cap_rad_s": cap,
        "jinv_xdot_inf_rad_s": required_inf,
        "jinv_xdot_inf_over_qdot_cap": required_inf / cap,
        "jinv_xdot_solve_status": "ok",
        "xdot_feasibility_scale": scale,
        "xdot_norm_pre_feasibility_scale": float(np.linalg.norm(xdot)),
        "xdot_norm_post_feasibility_scale": float(np.linalg.norm(scaled)),
        "xdot_feasibility_scale_active": bool(scale < 1.0 - 1e-12),
    }


def build_p0_v8_target(
    *,
    tcp_pose_base: Sequence[float],
    tcp_speed_base: Sequence[float],
    force_tcp_n: Sequence[float],
    reaction_normal_b: Sequence[float],
    normal_load_n: float,
    force_error_n: float,
    jacobian: Any,
    dt_s: float = 0.002,
    qdot_cap_rad_s: float = P0_QDOT_CAP_RAD_S,
) -> P0V8Target:
    """Build the frozen weak-posture/press-only P0 v8 task target."""

    pose = np.asarray(tcp_pose_base, dtype=float)
    speed = np.asarray(tcp_speed_base, dtype=float)
    force = np.asarray(force_tcp_n, dtype=float)
    if pose.shape != (6,) or speed.shape != (6,) or force.shape != (3,):
        raise ValueError("P0 v8 target requires 6D pose/speed and 3D force")
    if not np.all(np.isfinite(pose)) or not np.all(np.isfinite(speed)) or not np.all(np.isfinite(force)):
        raise ValueError("P0 v8 target inputs must be finite")
    posture_policy = low_force_posture_policy(normal_load_n=normal_load_n)
    if normal_load_n <= P0_LOW_LOAD_N and not math.isclose(
        float(posture_policy["effective_ko"]), P0_V8_EFFECTIVE_KO, abs_tol=1e-12
    ):
        raise ValueError("P0 v8 low-load effective_ko drift")
    press = press_only_outer_output(
        reaction_normal_b=reaction_normal_b,
        force_error_n=force_error_n,
    )
    posture = compute_step5d_outer_loop(
        Step5dOuterLoopConfig(
            kp=0.0,
            ko=P0_BASE_KO,
            orientation_gain_scale=float(posture_policy["orientation_gain_scale"]),
            kf=0.0,
            Md_scalar=12.0,
            Bd_scalar=550.0,
            force_target_n=0.0,
            delay_T_s=float(dt_s),
            force_sign_convention="step5_step6_positive_normal_load",
        ),
        Step5dOuterLoopState(),
        Step5dOuterLoopInputs(
            tcp_pose_base=_tuple6(pose),
            tcp_speed_base=_tuple6(speed),
            force_tcp_n=tuple(float(value) for value in force),  # type: ignore[arg-type]
            control_reaction_normal_base=tuple(float(value) for value in _normalized3(reaction_normal_b)),  # type: ignore[arg-type]
            x_pd_base=tuple(float(value) for value in pose[:3]),  # type: ignore[arg-type]
            xdot_pd_base=(0.0, 0.0, 0.0),
            dt_s=float(dt_s),
            cmd_valid=True,
        ),
        include_diagnostics=True,
    )
    raw = np.asarray(posture.xdot_c, dtype=float)
    raw[:3] = np.asarray(press.xdot_c[:3], dtype=float)
    outer_diagnostics = dict(posture.diagnostics)
    outer_diagnostics.update(press.diagnostics)
    outer_diagnostics.update(
        {
            "effective_ko": float(posture_policy["effective_ko"]),
            "orientation_gain_scale": float(posture_policy["orientation_gain_scale"]),
            "no_contact_p0_target_policy": "press_plus_weak_posture_v2",
        }
    )
    limited, limiter_active, frame_diagnostics = limit_p0_xdot_components(
        raw,
        rotation_base_from_tcp=rotvec_to_matrix(pose[3:6]),
        return_diagnostics=True,
    )
    feasible, feasibility = scale_xdot_for_joint_feasibility(
        limited,
        jacobian,
        qdot_cap_rad_s=qdot_cap_rad_s,
        safety=0.9,
    )
    return P0V8Target(
        desired_twist=_tuple6(feasible),
        raw_outer_twist=_tuple6(raw),
        limited_twist=_tuple6(limited),
        posture_policy=posture_policy,
        outer_diagnostics=outer_diagnostics,
        frame_diagnostics=frame_diagnostics,
        feasibility_diagnostics=feasibility,
        limiter_active=limiter_active,
    )
