"""Small deterministic pose and Jacobian helpers used by offline tests."""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from .contracts import PoseSample


def quaternion_multiply(
    left: Sequence[float], right: Sequence[float]
) -> tuple[float, float, float, float]:
    lw, lx, ly, lz = left
    rw, rx, ry, rz = right
    return (
        lw * rw - lx * rx - ly * ry - lz * rz,
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
    )


def quaternion_error_rotvec(
    target_wxyz: Sequence[float], current_wxyz: Sequence[float]
) -> tuple[float, float, float]:
    current_inverse = (
        current_wxyz[0],
        -current_wxyz[1],
        -current_wxyz[2],
        -current_wxyz[3],
    )
    error = quaternion_multiply(target_wxyz, current_inverse)
    if error[0] < 0.0:
        error = tuple(-value for value in error)
    vector_norm = math.sqrt(sum(value * value for value in error[1:]))
    if vector_norm <= 1e-12:
        return (0.0, 0.0, 0.0)
    angle = 2.0 * math.atan2(vector_norm, max(error[0], 0.0))
    return tuple(angle * value / vector_norm for value in error[1:])


def pose_error(target: PoseSample, current: PoseSample) -> tuple[float, ...]:
    translation = tuple(
        target.position_m[index] - current.position_m[index] for index in range(3)
    )
    rotation = quaternion_error_rotvec(
        target.quaternion_wxyz, current.quaternion_wxyz
    )
    return translation + rotation


def damped_least_squares(
    jacobian: Sequence[Sequence[float]],
    desired_twist: Sequence[float],
    damping: float,
) -> tuple[float, ...]:
    if damping <= 0.0 or not math.isfinite(damping):
        raise ValueError("damping must be finite and positive")
    matrix = np.asarray(jacobian, dtype=float)
    twist = np.asarray(desired_twist, dtype=float)
    if matrix.shape != (6, 6) or twist.shape != (6,):
        raise ValueError("expected a 6x6 Jacobian and a six-axis twist")
    if not np.isfinite(matrix).all() or not np.isfinite(twist).all():
        raise ValueError("DLS inputs must be finite")
    regularized = matrix @ matrix.T + (damping * damping) * np.eye(6)
    result = matrix.T @ np.linalg.solve(regularized, twist)
    if not np.isfinite(result).all():
        raise ValueError("DLS produced non-finite output")
    return tuple(float(value) for value in result)
