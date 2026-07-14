#!/usr/bin/env python3
"""Canonical free-space Step5 target builder for Step5d P0 v9."""

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
P0_V9_PATH_AMPLITUDE_M = 0.015
P0_V9_PATH_THETA_RATE_RAD_S = 0.1
P0_V9_PATH_DURATION_S = 60.0
P0_V9_PATH_Z_LIFT_M = 0.020
P0_V9_PATH_KP_S_INV = 1.0


@dataclass(frozen=True)
class P0V9Target:
    desired_twist: tuple[float, float, float, float, float, float]
    raw_outer_twist: tuple[float, float, float, float, float, float]
    limited_twist: tuple[float, float, float, float, float, float]
    posture_policy: dict[str, Any]
    path_diagnostics: dict[str, Any]
    feasibility_diagnostics: dict[str, Any]
    tangent_base: tuple[float, float, float]
    lateral_base: tuple[float, float, float]
    approach_normal_base: tuple[float, float, float]


def _base_xy_unit(direction_xy: Sequence[float], *, name: str) -> np.ndarray:
    direction = np.asarray(direction_xy, dtype=float)
    if direction.shape != (2,) or not np.all(np.isfinite(direction)):
        raise ValueError(f"P0 v9 {name} must be a finite 2-vector")
    norm = float(np.linalg.norm(direction))
    if norm < 1e-9:
        raise ValueError(f"P0 v9 {name} is degenerate")
    return np.asarray((direction[0] / norm, direction[1] / norm, 0.0), dtype=float)


def canonical_cycloid_lift_reference(
    path_time_s: float,
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return canonical Step5 along/lateral/Z offsets and velocities.

    The XY path uses A=15 mm and theta=0..6 over 60 s.  Z is a monotonic
    quintic smoothstep from the Stage25 anchor to +20 mm, with zero endpoint
    velocity.
    """

    time_s = min(max(float(path_time_s), 0.0), P0_V9_PATH_DURATION_S)
    theta = P0_V9_PATH_THETA_RATE_RAD_S * time_s
    along_m = P0_V9_PATH_AMPLITUDE_M * (theta - math.sin(theta))
    lateral_m = P0_V9_PATH_AMPLITUDE_M * (1.0 - math.cos(theta))
    along_velocity_m_s = (
        P0_V9_PATH_AMPLITUDE_M
        * P0_V9_PATH_THETA_RATE_RAD_S
        * (1.0 - math.cos(theta))
    )
    lateral_velocity_m_s = (
        P0_V9_PATH_AMPLITUDE_M
        * P0_V9_PATH_THETA_RATE_RAD_S
        * math.sin(theta)
    )
    phase = time_s / P0_V9_PATH_DURATION_S
    smoothstep = 10.0 * phase**3 - 15.0 * phase**4 + 6.0 * phase**5
    smoothstep_rate = (
        30.0 * phase**2 - 60.0 * phase**3 + 30.0 * phase**4
    ) / P0_V9_PATH_DURATION_S
    return (
        (along_m, lateral_m, P0_V9_PATH_Z_LIFT_M * smoothstep),
        (
            along_velocity_m_s,
            lateral_velocity_m_s,
            P0_V9_PATH_Z_LIFT_M * smoothstep_rate,
        ),
    )


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
    return angular


def build_p0_v9_target(
    *,
    tcp_pose_base: Sequence[float],
    anchor_tcp_pose_base: Sequence[float],
    safe_u_along_xy: Sequence[float],
    safe_p_lateral_xy: Sequence[float],
    approach_normal_base: Sequence[float],
    path_time_s: float,
    normal_load_n: float,
    jacobian: Any,
    qdot_cap_rad_s: float = P0_QDOT_CAP_RAD_S,
) -> P0V9Target:
    """Build the guard-v2 canonical XYZ target without Cartesian speed guards."""

    pose = np.asarray(tcp_pose_base, dtype=float)
    anchor = np.asarray(anchor_tcp_pose_base, dtype=float)
    if pose.shape != (6,) or anchor.shape != (6,) or not np.all(np.isfinite(pose)) or not np.all(np.isfinite(anchor)):
        raise ValueError("P0 v9 target requires finite current and anchor TCP poses")
    approach = _normalized3(approach_normal_base)
    along = _base_xy_unit(safe_u_along_xy, name="safe-frame u_along_xy")
    lateral = _base_xy_unit(safe_p_lateral_xy, name="safe-frame p_lateral_xy")
    if abs(float(np.dot(along, lateral))) > 1e-6:
        raise ValueError("P0 v9 safe-frame along/lateral directions must be orthogonal")
    target_local, feedforward_local = canonical_cycloid_lift_reference(path_time_s)
    basis = np.column_stack((along, lateral, np.asarray((0.0, 0.0, 1.0))))
    actual_base = pose[:3] - anchor[:3]
    actual_local = basis.T @ actual_base
    target_base = basis @ np.asarray(target_local, dtype=float)
    feedforward_base = basis @ np.asarray(feedforward_local, dtype=float)
    tracking_error_base = target_base - actual_base
    raw = np.zeros(6, dtype=float)
    raw[:3] = feedforward_base + P0_V9_PATH_KP_S_INV * tracking_error_base
    raw[3:] = _orientation_hold_velocity(pose[3:], anchor[3:])
    desired_normal_m_s = float(np.dot(raw[:3], approach))
    normal_displacement_m = float(np.dot(actual_base, approach))
    feasible, feasibility = scale_xdot_for_joint_feasibility(
        raw,
        jacobian,
        qdot_cap_rad_s=qdot_cap_rad_s,
        safety=1.0,
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
            "policy": "canonical_step5_cycloid_plus_base_z_lift_v1",
            "force_sign_convention": "step5_step6_positive_normal_load",
            "target_along_displacement_m": target_local[0],
            "actual_along_displacement_m": float(actual_local[0]),
            "target_lateral_displacement_m": target_local[1],
            "actual_lateral_displacement_m": float(actual_local[1]),
            "target_z_displacement_m": target_local[2],
            "actual_z_displacement_m": float(actual_local[2]),
            "xyz_tracking_error_norm_m": float(np.linalg.norm(tracking_error_base)),
            "target_along_velocity_m_s": feedforward_local[0],
            "target_lateral_velocity_m_s": feedforward_local[1],
            "target_z_velocity_m_s": feedforward_local[2],
            "target_tangent_displacement_m": target_local[0],
            "actual_tangent_displacement_m": float(actual_local[0]),
            "tangent_tracking_error_m": target_local[0] - float(actual_local[0]),
            "target_tangent_velocity_m_s": feedforward_local[0],
            "commanded_tangent_velocity_m_s": float(np.dot(raw[:3], along)),
            "target_normal_velocity_m_s": desired_normal_m_s,
            "anchor_normal_displacement_m": normal_displacement_m,
            "path_time_s": min(max(float(path_time_s), 0.0), P0_V9_PATH_DURATION_S),
        },
        feasibility_diagnostics=feasibility,
        tangent_base=tuple(float(value) for value in along),  # type: ignore[arg-type]
        lateral_base=tuple(float(value) for value in lateral),  # type: ignore[arg-type]
        approach_normal_base=tuple(float(value) for value in approach),  # type: ignore[arg-type]
    )
