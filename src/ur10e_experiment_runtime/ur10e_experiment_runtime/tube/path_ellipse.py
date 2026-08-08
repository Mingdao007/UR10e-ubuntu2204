"""PATH-frame XY ellipse field helpers.

Coordinates ``(u_m, v_m)`` are lateral offsets in the PATH/reference frame
XY plane.  Semi-axes ``(a_m, b_m)`` are strictly positive metres along ``u``
and ``v`` respectively.

The field matches the modular-tube ellipse scaling convention:

    rho = sqrt((u/a)^2 + (v/b)^2)
    phi = min(a, b) * (rho - 1)

so ``phi < 0`` inside, ``phi = 0`` on the boundary, ``phi > 0`` outside.
When ``a == b == r`` this collapses to the Euclidean circle SDF
``sqrt(u^2 + v^2) - r``.
"""

from __future__ import annotations

import math
from typing import Any


class PathEllipseError(ValueError):
    """Raised when PATH-frame ellipse inputs are invalid."""


def _finite_positive(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PathEllipseError(f"{name} must be a finite positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise PathEllipseError(f"{name} must be a finite positive number")
    return result


def _finite(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PathEllipseError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise PathEllipseError(f"{name} must be a finite number")
    return 0.0 if result == 0.0 else result


def phi(u_m: float, v_m: float, a_m: float, b_m: float) -> float:
    """Signed distance-like PATH-frame XY ellipse field (negative inside)."""

    u = _finite(u_m, name="u_m")
    v = _finite(v_m, name="v_m")
    a = _finite_positive(a_m, name="a_m")
    b = _finite_positive(b_m, name="b_m")
    rho = math.hypot(u / a, v / b)
    result = min(a, b) * (rho - 1.0)
    if not math.isfinite(result):  # pragma: no cover - constructor proof
        raise PathEllipseError("phi is non-finite")
    return result


def grad_phi(u_m: float, v_m: float, a_m: float, b_m: float) -> tuple[float, float]:
    """Analytic gradient of ``phi`` with respect to ``(u_m, v_m)``.

    Away from the origin the gradient points outward (increasing ``phi``).
    At the exact center the gradient is defined as ``(0.0, 0.0)``.
    """

    u = _finite(u_m, name="u_m")
    v = _finite(v_m, name="v_m")
    a = _finite_positive(a_m, name="a_m")
    b = _finite_positive(b_m, name="b_m")
    rho = math.hypot(u / a, v / b)
    if rho == 0.0:
        return (0.0, 0.0)
    scale = min(a, b) / rho
    return (scale * u / (a * a), scale * v / (b * b))


__all__ = [
    "PathEllipseError",
    "grad_phi",
    "phi",
]
