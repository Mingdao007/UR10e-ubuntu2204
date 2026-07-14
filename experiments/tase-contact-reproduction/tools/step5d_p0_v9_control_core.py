#!/usr/bin/env python3
"""Pure tangential/free-space target builder for Step5d P0 v9."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from step5d_p0_v8_control_core import (
    P0_QDOT_CAP_RAD_S,
    _normalized3,
    _tuple6,
    scale_xdot_for_joint_feasibility,
)
from step5d_paper_outer_loop import rotvec_to_matrix


P0_V9_EFFECTIVE_KO = 0.01
P0_V9_PATH_AMPLITUDE_M = 0.001
P0_V9_PATH_PERIOD_S = 20.0
P0_V9_PATH_DURATION_S = 60.0
P0_V9_PATH_KP_S_INV = 1.0
P0_V9_TANGENTIAL_SPEED_CAP_M_S = 0.0005
P0_V9_NORMAL_TARGET_TOLERANCE_M_S = 1e-9
P0_V9_PREDICTED_NORMAL_TOLERANCE_M_S = 1e-5
P0_V9_ACTUAL_NORMAL_SPEED_STOP_M_S = 0.0002
P0_V9_ACTUAL_NORMAL_SPEED_DWELL_S = 0.004
P0_V9_NORMAL_DISPLACEMENT_STOP_M = 0.0005
P0_V9_ANGULAR_CAP_RAD_S = 0.015


@dataclass(frozen=True)
class P0V9Target:
    desired_twist: tuple[float, float, float, float, float, float]
    raw_outer_twist: tuple[float, float, float, float, float, float]
    limited_twist: tuple[float, float, float, float, float, float]
    posture_policy: dict[str, Any]
    path_diagnostics: dict[str, Any]
    feasibility_diagnostics: dict[str, Any]
    tangent_base: tuple[float, float, float]
    approach_normal_base: tuple[float, float, float]


def project_tangent_orthogonal_to_normal(
    safe_u_along_xy: Sequence[float],
    approach_normal_base: Sequence[float],
) -> np.ndarray:
    """Project the safe-frame along direction into the no-contact tangent plane."""

    along_xy = np.asarray(safe_u_along_xy, dtype=float)
    if along_xy.shape != (2,) or not np.all(np.isfinite(along_xy)):
        raise ValueError("P0 v9 safe-frame u_along_xy must be a finite 2-vector")
    normal = _normalized3(approach_normal_base)
    tangent = np.asarray((along_xy[0], along_xy[1], 0.0), dtype=float)
    tangent -= float(np.dot(tangent, normal)) * normal
    tangent_norm = float(np.linalg.norm(tangent))
    if tangent_norm < 1e-9:
        raise ValueError("P0 v9 tangent projection is degenerate")
    return tangent / tangent_norm


def one_sided_smooth_reference(path_time_s: float) -> tuple[float, float]:
    """Return 0..2 mm position and its smooth 20 s-period velocity."""

    time_s = min(max(float(path_time_s), 0.0), P0_V9_PATH_DURATION_S)
    omega = 2.0 * math.pi / P0_V9_PATH_PERIOD_S
    target_m = P0_V9_PATH_AMPLITUDE_M * (1.0 - math.cos(omega * time_s))
    target_velocity_m_s = P0_V9_PATH_AMPLITUDE_M * omega * math.sin(omega * time_s)
    return target_m, target_velocity_m_s


def _orientation_hold_velocity(
    current_rotvec: Sequence[float],
    anchor_rotvec: Sequence[float],
    *,
    gain: float = P0_V9_EFFECTIVE_KO,
) -> np.ndarray:
    current = rotvec_to_matrix(np.asarray(current_rotvec, dtype=float))
    anchor = rotvec_to_matrix(np.asarray(anchor_rotvec, dtype=float))
    error = anchor @ current.T
    vee = 0.5 * np.asarray(
        (error[2, 1] - error[1, 2], error[0, 2] - error[2, 0], error[1, 0] - error[0, 1]),
        dtype=float,
    )
    angular = float(gain) * vee
    norm = float(np.linalg.norm(angular))
    if norm > P0_V9_ANGULAR_CAP_RAD_S:
        angular *= P0_V9_ANGULAR_CAP_RAD_S / norm
    return angular


def build_p0_v9_target(
    *,
    tcp_pose_base: Sequence[float],
    anchor_tcp_pose_base: Sequence[float],
    safe_u_along_xy: Sequence[float],
    approach_normal_base: Sequence[float],
    path_time_s: float,
    normal_load_n: float,
    jacobian: Any,
    qdot_cap_rad_s: float = P0_QDOT_CAP_RAD_S,
) -> P0V9Target:
    """Build a tangential target with exact-zero commanded normal component."""

    pose = np.asarray(tcp_pose_base, dtype=float)
    anchor = np.asarray(anchor_tcp_pose_base, dtype=float)
    if pose.shape != (6,) or anchor.shape != (6,) or not np.all(np.isfinite(pose)) or not np.all(np.isfinite(anchor)):
        raise ValueError("P0 v9 target requires finite current and anchor TCP poses")
    approach = _normalized3(approach_normal_base)
    tangent = project_tangent_orthogonal_to_normal(safe_u_along_xy, approach)
    target_m, feedforward_m_s = one_sided_smooth_reference(path_time_s)
    actual_m = float(np.dot(pose[:3] - anchor[:3], tangent))
    normal_displacement_m = float(np.dot(pose[:3] - anchor[:3], approach))
    commanded_m_s = feedforward_m_s + P0_V9_PATH_KP_S_INV * (target_m - actual_m)
    commanded_m_s = min(max(commanded_m_s, -P0_V9_TANGENTIAL_SPEED_CAP_M_S), P0_V9_TANGENTIAL_SPEED_CAP_M_S)
    raw = np.zeros(6, dtype=float)
    raw[:3] = commanded_m_s * tangent
    raw[3:] = _orientation_hold_velocity(pose[3:], anchor[3:])
    desired_normal_m_s = float(np.dot(raw[:3], approach))
    if abs(desired_normal_m_s) > P0_V9_NORMAL_TARGET_TOLERANCE_M_S:
        raise ValueError("P0 v9 target lost exact-zero normal semantics")
    feasible, feasibility = scale_xdot_for_joint_feasibility(
        raw,
        jacobian,
        qdot_cap_rad_s=qdot_cap_rad_s,
        safety=0.9,
    )
    posture_policy = {
        "policy": "weak_posture_hold_v3",
        "active": True,
        "normal_load_n": max(0.0, float(normal_load_n)),
        "base_ko": 5.0,
        "effective_ko": P0_V9_EFFECTIVE_KO,
        "orientation_gain_scale": P0_V9_EFFECTIVE_KO / 5.0,
        "load_schedule": "disabled_constant_weak_hold",
    }
    return P0V9Target(
        desired_twist=_tuple6(feasible),
        raw_outer_twist=_tuple6(raw),
        limited_twist=_tuple6(raw),
        posture_policy=posture_policy,
        path_diagnostics={
            "policy": "tangential_one_sided_cosine_v1",
            "force_sign_convention": "step5_step6_positive_normal_load",
            "target_tangent_displacement_m": target_m,
            "actual_tangent_displacement_m": actual_m,
            "tangent_tracking_error_m": target_m - actual_m,
            "target_tangent_velocity_m_s": feedforward_m_s,
            "commanded_tangent_velocity_m_s": commanded_m_s,
            "target_normal_velocity_m_s": desired_normal_m_s,
            "anchor_normal_displacement_m": normal_displacement_m,
            "path_time_s": min(max(float(path_time_s), 0.0), P0_V9_PATH_DURATION_S),
        },
        feasibility_diagnostics=feasibility,
        tangent_base=tuple(float(value) for value in tangent),  # type: ignore[arg-type]
        approach_normal_base=tuple(float(value) for value in approach),  # type: ignore[arg-type]
    )
