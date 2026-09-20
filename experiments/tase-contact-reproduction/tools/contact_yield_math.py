"""SO(3) and contact-frame helpers for the offline yield-recovery route.

Rotvec subtraction is not an attitude error.  All orientation feedback uses
the principal logarithm on SO(3).  Law states stay in a fixed base frame;
these helpers never rotate a native law tensor.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from contact_semantics import (
    finite_matrix3,
    finite_vector3,
    minimal_rotation_between,
    normalize_unit,
    skew3,
)


_LOG_SMALL = 1e-12
_ORTHONORMAL_ATOL = 1e-8
_ROTATION_IDENTITY = np.eye(3)
# Preserve np.allclose's existing diagonal relative tolerance exactly. The
# finite 3x3 inputs need no generic broadcasting/NaN/Inf handling per tick.
_ROTATION_TOLERANCE = _ORTHONORMAL_ATOL + 1e-5 * _ROTATION_IDENTITY
_ROTATION_IDENTITY.flags.writeable = False
_ROTATION_TOLERANCE.flags.writeable = False


class YieldMathError(ValueError):
    pass


def finite_scalar(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise YieldMathError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise YieldMathError(f"{name} must be a finite number") from error
    if not math.isfinite(number):
        raise YieldMathError(f"{name} must be a finite number")
    return number


def require_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise YieldMathError(f"{name} must be a bool")
    return value


def require_unit_vector(values: Any, name: str, *, atol: float = 1e-8) -> np.ndarray:
    vector = finite_vector3(values, name)
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or abs(norm - 1.0) > atol:
        raise YieldMathError(f"{name} must already be a unit vector")
    return vector.copy()


def optional_finite_time(value: Any, name: str) -> float | None:
    if value is None:
        return None
    number = finite_scalar(value, name)
    if number < 0.0:
        raise YieldMathError(f"{name} must be nonnegative")
    return number


def require_rotation(matrix: Any, name: str = "rotation") -> np.ndarray:
    rotation = finite_matrix3(matrix, name)
    if not (np.abs(rotation.T @ rotation - _ROTATION_IDENTITY) <= _ROTATION_TOLERANCE).all():
        raise YieldMathError(f"{name} must be orthonormal")
    if float(np.linalg.det(rotation)) < 0.999:
        raise YieldMathError(f"{name} must be right-handed")
    return rotation


def projector_tangent(normal: Any) -> np.ndarray:
    unit = normalize_unit(normal, name="normal")
    return np.eye(3) - np.outer(unit, unit)


def so3_hat(vector: Any) -> np.ndarray:
    return skew3(vector)


def so3_vee(matrix: Any) -> np.ndarray:
    skew = finite_matrix3(matrix, "so3_vee")
    return np.array(
        [skew[2, 1], skew[0, 2], skew[1, 0]],
        dtype=float,
    )


def so3_exp(rotvec: Any) -> np.ndarray:
    vector = finite_vector3(rotvec, "rotvec")
    theta = float(np.linalg.norm(vector))
    if theta < _LOG_SMALL:
        return np.eye(3) + so3_hat(vector)
    axis = vector / theta
    hat = so3_hat(axis)
    return np.eye(3) + math.sin(theta) * hat + (1.0 - math.cos(theta)) * (hat @ hat)


def so3_log(rotation: Any) -> np.ndarray:
    matrix = require_rotation(rotation, "so3_log")
    cos_theta = float(np.clip(0.5 * (np.trace(matrix) - 1.0), -1.0, 1.0))
    theta = math.acos(cos_theta)
    if theta < _LOG_SMALL:
        return so3_vee(0.5 * (matrix - matrix.T))
    if math.pi - theta < 1e-8:
        # Near-π: extract axis from the symmetric part.
        diag = np.clip((np.diag(matrix) + 1.0) / 2.0, 0.0, 1.0)
        axis = np.sqrt(diag)
        if axis[0] >= axis[1] and axis[0] >= axis[2]:
            axis[1] = math.copysign(axis[1], matrix[0, 1])
            axis[2] = math.copysign(axis[2], matrix[0, 2])
        elif axis[1] >= axis[2]:
            axis[0] = math.copysign(axis[0], matrix[0, 1])
            axis[2] = math.copysign(axis[2], matrix[1, 2])
        else:
            axis[0] = math.copysign(axis[0], matrix[0, 2])
            axis[1] = math.copysign(axis[1], matrix[1, 2])
        norm = float(np.linalg.norm(axis))
        if norm < _LOG_SMALL:
            raise YieldMathError("so3_log failed near π")
        return (theta / norm) * axis
    return (theta / (2.0 * math.sin(theta))) * so3_vee(matrix - matrix.T)


def attitude_error_omega(
    current_rotation: Any,
    desired_rotation: Any,
    *,
    gain_s_inv: float,
    angular_cap_rad_s: float,
) -> np.ndarray:
    """Body-to-base angular velocity from SO(3) log, then capped.

    Equivalent to ``gain * log(R_des R_cur^T)`` in the base frame.  This is
    not ``rotvec(R_des) - rotvec(R_cur)``.
    """
    current = require_rotation(current_rotation, "current_rotation")
    desired = require_rotation(desired_rotation, "desired_rotation")
    gain = finite_scalar(gain_s_inv, "gain_s_inv")
    cap = finite_scalar(angular_cap_rad_s, "angular_cap_rad_s")
    if gain <= 0.0 or cap <= 0.0:
        raise YieldMathError("attitude gain and cap must be positive")
    omega = gain * so3_log(desired @ current.T)
    speed = float(np.linalg.norm(omega))
    if speed > cap:
        omega = omega * (cap / speed)
    return omega


def transported_roll_anchor(roll_anchor: Any, inward_normal: Any) -> np.ndarray:
    """Minimal transport of the stored roll anchor so +Z matches the estimated inward axis."""
    anchor = require_rotation(roll_anchor, "roll_anchor")
    return minimal_rotation_between(anchor[:, 2], inward_normal) @ anchor


def cap_vector(vector: Any, limit: float, name: str) -> tuple[np.ndarray, float]:
    value = finite_vector3(vector, name)
    bound = finite_scalar(limit, f"{name} limit")
    if bound <= 0.0:
        raise YieldMathError(f"{name} limit must be positive")
    speed = float(np.linalg.norm(value))
    if speed > bound:
        return value * (bound / speed), speed - bound
    return value.copy(), 0.0


def project_to_unit_sphere(vector: Any, name: str) -> np.ndarray:
    return normalize_unit(vector, name=name)


def tangent_project(vector: Any, normal: Any) -> np.ndarray:
    return projector_tangent(normal) @ finite_vector3(vector, "vector")
