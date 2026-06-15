#!/usr/bin/env python3
"""Canonical contact force/frame semantics for UR tabletop control."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


DEFAULT_MIN_NORM = 1e-9


def finite_vector3(values: Any, name: str) -> np.ndarray:
    vector = np.asarray(values, dtype=float)
    if vector.shape != (3,) or not np.all(np.isfinite(vector)):
        raise ValueError(f"{name} must be a finite length-3 vector")
    return vector


def finite_matrix3(values: Any, name: str) -> np.ndarray:
    matrix = np.asarray(values, dtype=float)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise ValueError(f"{name} must be a finite 3x3 matrix")
    return matrix


def normalize_unit(values: Any, *, name: str, min_norm: float = DEFAULT_MIN_NORM) -> np.ndarray:
    vector = finite_vector3(values, name)
    norm = float(np.linalg.norm(vector))
    if norm < float(min_norm):
        raise ValueError(f"{name} norm is too small")
    return vector / norm


def skew3(values: Any) -> np.ndarray:
    x, y, z = finite_vector3(values, "skew_vector")
    return np.array(
        [
            [0.0, -z, y],
            [z, 0.0, -x],
            [-y, x, 0.0],
        ],
        dtype=float,
    )


def approach_normal_from_reaction(reaction_normal: Any) -> np.ndarray:
    return -normalize_unit(reaction_normal, name="reaction_normal")


def signed_normal_load_n(force_base: Any, reaction_normal: Any) -> float:
    force = finite_vector3(force_base, "force_base")
    reaction = normalize_unit(reaction_normal, name="reaction_normal")
    return float(np.dot(force, reaction))


def force_error_n(*, target_load_n: float, normal_load_n: float) -> float:
    target = float(target_load_n)
    load = float(normal_load_n)
    if not math.isfinite(target) or not math.isfinite(load):
        raise ValueError("target_load_n and normal_load_n must be finite")
    return target - load


def force_motion_acceleration_base(
    *,
    force_error: float,
    force_integral: float,
    kf: float,
    Md: float,
    Bd: float,
    xdot_p_prev_base: Any,
    reaction_normal: Any,
) -> np.ndarray:
    """Return the signed force-motion acceleration in base coordinates.

    Positive force error means the load is too low, so the command must move
    along the approach direction (-reaction). Negative force error unloads along
    the reaction direction.
    """

    err = float(force_error)
    integ = float(force_integral)
    gain = float(kf)
    mass = float(Md)
    damping = float(Bd)
    if not all(math.isfinite(v) for v in [err, integ, gain, mass, damping]) or mass <= 0.0:
        raise ValueError("force-motion parameters must be finite and Md must be positive")
    reaction = normalize_unit(reaction_normal, name="reaction_normal")
    previous = finite_vector3(xdot_p_prev_base, "xdot_p_prev_base")
    return -((err + gain * integ) / mass) * reaction - (damping / mass) * previous


def minimal_rotation_between(source_axis: Any, target_axis: Any) -> np.ndarray:
    source = normalize_unit(source_axis, name="source_axis")
    target = normalize_unit(target_axis, name="target_axis")
    dot_value = float(np.clip(np.dot(source, target), -1.0, 1.0))
    if dot_value > 1.0 - 1e-12:
        return np.eye(3)
    if dot_value < -1.0 + 1e-12:
        reference = np.array([1.0, 0.0, 0.0], dtype=float)
        if abs(float(np.dot(reference, source))) > 0.9:
            reference = np.array([0.0, 1.0, 0.0], dtype=float)
        axis = reference - float(np.dot(reference, source)) * source
        axis = axis / float(np.linalg.norm(axis))
        K = skew3(axis)
        return np.eye(3) + 2.0 * (K @ K)
    axis = np.cross(source, target)
    K = skew3(axis)
    return np.eye(3) + K + K @ K * (1.0 / (1.0 + dot_value))


def desired_rotation_preserving_roll(current_rotation_base: Any, orientation_target_axis_base: Any) -> np.ndarray:
    current = finite_matrix3(current_rotation_base, "current_rotation_base")
    target_axis = normalize_unit(orientation_target_axis_base, name="orientation_target_axis_base")
    current_tcp_z = current[:, 2]
    align = minimal_rotation_between(current_tcp_z, target_axis)
    return align @ current


def orientation_axis_angle_error(current_rotation_base: Any, orientation_target_axis_base: Any) -> float:
    current = finite_matrix3(current_rotation_base, "current_rotation_base")
    target_axis = normalize_unit(orientation_target_axis_base, name="orientation_target_axis_base")
    current_tcp_z = current[:, 2]
    cross = np.cross(current_tcp_z, target_axis)
    return float(math.atan2(float(np.linalg.norm(cross)), float(np.clip(np.dot(current_tcp_z, target_axis), -1.0, 1.0))))


def semantic_boundary_is_consistent(
    *,
    contact_orientation_error_rad: float,
    outer_orientation_error_rad: float,
    tolerance_rad: float,
) -> bool:
    contact_error = float(contact_orientation_error_rad)
    outer_error = float(outer_orientation_error_rad)
    tolerance = float(tolerance_rad)
    if not all(math.isfinite(v) for v in [contact_error, outer_error, tolerance]) or tolerance < 0.0:
        return False
    return abs(contact_error - outer_error) <= tolerance
