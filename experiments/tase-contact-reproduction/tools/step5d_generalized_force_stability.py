#!/usr/bin/env python3
"""Offline stability certificates for the generalized force outer loop.

The certificate is deliberately separate from the controller transition.  It
checks the interval-vertex linear parameter-varying representation induced by
the sector-bounded integral map and reports infeasibility instead of turning a
failed search into a stability claim.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Sequence

import numpy as np

from step5d_generalized_force_outer_loop import GENERALIZED_FORCE_SCHEMA


STABILITY_SCHEMA = "step5d.offline/generalized-force-stability-v1"


class StabilityCertificateError(ValueError):
    """A stability certificate input is invalid."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise StabilityCertificateError(f"{name} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StabilityCertificateError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise StabilityCertificateError(f"{name} must be finite")
    return result


def force_state_matrix(
    *,
    effective_stiffness_n_per_m: float,
    integral_slope_m_s2_per_n_s: float,
    force_p_gain_m_s2_per_n: float,
    force_damping_s_inv: float,
) -> np.ndarray:
    """Return A for x=[force_error, normal_velocity, integral_error]."""

    ke = _finite(effective_stiffness_n_per_m, "effective_stiffness_n_per_m")
    mu = _finite(integral_slope_m_s2_per_n_s, "integral_slope_m_s2_per_n_s")
    kp = _finite(force_p_gain_m_s2_per_n, "force_p_gain_m_s2_per_n")
    damping = _finite(force_damping_s_inv, "force_damping_s_inv")
    if min(ke, mu, kp, damping) <= 0.0:
        raise StabilityCertificateError("force state parameters must be positive")
    return np.asarray(
        [[0.0, -ke, 0.0], [kp, -damping, mu], [1.0, 0.0, 0.0]],
        dtype=float,
    )


def interval_vertices(
    *,
    stiffness_bounds_n_per_m: tuple[float, float],
    integral_slope_bounds_m_s2_per_n_s: tuple[float, float],
    force_p_gain_m_s2_per_n: float,
    force_damping_s_inv: float,
) -> tuple[np.ndarray, ...]:
    """Enumerate the four endpoint matrices of the affine uncertainty box."""

    ke_low, ke_high = (_finite(item, "stiffness bound") for item in stiffness_bounds_n_per_m)
    mu_low, mu_high = (_finite(item, "integral slope bound") for item in integral_slope_bounds_m_s2_per_n_s)
    if ke_low <= 0.0 or ke_high < ke_low or mu_low <= 0.0 or mu_high < mu_low:
        raise StabilityCertificateError("uncertainty bounds are invalid")
    return tuple(
        force_state_matrix(
            effective_stiffness_n_per_m=ke,
            integral_slope_m_s2_per_n_s=mu,
            force_p_gain_m_s2_per_n=force_p_gain_m_s2_per_n,
            force_damping_s_inv=force_damping_s_inv,
        )
        for ke in (ke_low, ke_high)
        for mu in (mu_low, mu_high)
    )


def _symmetric_matrix(value: Any, name: str) -> np.ndarray:
    matrix = np.asarray(value, dtype=float)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise StabilityCertificateError(f"{name} must be a finite 3x3 matrix")
    if not np.allclose(matrix, matrix.T, atol=1e-10, rtol=0.0):
        raise StabilityCertificateError(f"{name} must be symmetric")
    return (matrix + matrix.T) / 2.0


@dataclass(frozen=True)
class LyapunovCertificateV1:
    schema: str
    controller_schema: str
    feasible: bool
    continuous_margin: float
    discrete_margin: float | None
    p_eigen_min: float
    q_eigen_min: float
    sampled_period_bound_s: float | None
    matrix_p: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
    vertex_count: int
    solver: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "controller_schema": self.controller_schema,
            "feasible": self.feasible,
            "continuous_margin": self.continuous_margin,
            "discrete_margin": self.discrete_margin,
            "p_eigen_min": self.p_eigen_min,
            "q_eigen_min": self.q_eigen_min,
            "sampled_period_bound_s": self.sampled_period_bound_s,
            "matrix_p": [list(row) for row in self.matrix_p],
            "vertex_count": self.vertex_count,
            "solver": self.solver,
            "message": self.message,
        }


def check_common_lyapunov(
    matrix_p: Any,
    vertices: Iterable[np.ndarray],
    *,
    sample_period_s: float | None = None,
    min_margin: float = 1e-9,
) -> LyapunovCertificateV1:
    """Check continuous and optional Euler-discrete Lyapunov inequalities."""

    P = _symmetric_matrix(matrix_p, "matrix_p")
    matrices = tuple(np.asarray(A, dtype=float) for A in vertices)
    if not matrices or any(A.shape != (3, 3) or not np.all(np.isfinite(A)) for A in matrices):
        raise StabilityCertificateError("vertices must be non-empty finite 3x3 matrices")
    if sample_period_s is not None and _finite(sample_period_s, "sample_period_s") <= 0.0:
        raise StabilityCertificateError("sample_period_s must be positive")
    eig_p = np.linalg.eigvalsh(P)
    q_mins = [float(np.min(np.linalg.eigvalsh(-(A.T @ P + P @ A)))) for A in matrices]
    p_min = float(np.min(eig_p))
    q_min = min(q_mins)
    continuous_margin = min(p_min, q_min)
    discrete_margin: float | None = None
    if sample_period_s is not None:
        Ts = float(sample_period_s)
        discrete_mins = []
        for A in matrices:
            Ad = np.eye(3) + Ts * A
            discrete_mins.append(float(np.min(np.linalg.eigvalsh(P - Ad.T @ P @ Ad))) )
        discrete_margin = min(discrete_mins)
    feasible = continuous_margin > min_margin and (
        discrete_margin is None or discrete_margin > min_margin
    )
    return LyapunovCertificateV1(
        schema=STABILITY_SCHEMA,
        controller_schema=GENERALIZED_FORCE_SCHEMA,
        feasible=feasible,
        continuous_margin=continuous_margin,
        discrete_margin=discrete_margin,
        p_eigen_min=p_min,
        q_eigen_min=q_min,
        sampled_period_bound_s=None,
        matrix_p=tuple(tuple(float(item) for item in row) for row in P),  # type: ignore[arg-type]
        vertex_count=len(matrices),
        solver="direct_check",
        message="feasible" if feasible else "common Lyapunov inequality is infeasible for supplied P",
    )


def sampled_period_bound(
    matrix_p: Any,
    vertices: Sequence[np.ndarray],
    *,
    min_margin: float = 1e-12,
) -> float:
    """Return a conservative Euler period bound from the continuous certificate."""

    P = _symmetric_matrix(matrix_p, "matrix_p")
    q_min = min(
        float(np.min(np.linalg.eigvalsh(-(A.T @ P + P @ A)))) for A in vertices
    )
    c_max = max(float(np.linalg.norm(A.T @ P @ A, 2)) for A in vertices)
    if q_min <= min_margin or c_max <= min_margin:
        raise StabilityCertificateError("continuous certificate has no positive sampling bound")
    return q_min / c_max


def uub_radius(
    matrix_p: Any,
    q_matrix: Any,
    *,
    disturbance_gain: float,
    disturbance_bound: float,
) -> float:
    """Return the standard quadratic ISS ultimate-radius estimate."""

    P = _symmetric_matrix(matrix_p, "matrix_p")
    Q = _symmetric_matrix(q_matrix, "q_matrix")
    gain = _finite(disturbance_gain, "disturbance_gain")
    bound = _finite(disturbance_bound, "disturbance_bound")
    if gain < 0.0 or bound < 0.0:
        raise StabilityCertificateError("disturbance gain/bound must be non-negative")
    p_min = float(np.min(np.linalg.eigvalsh(P)))
    p_max = float(np.max(np.linalg.eigvalsh(P)))
    q_min = float(np.min(np.linalg.eigvalsh(Q)))
    if min(p_min, q_min) <= 0.0:
        raise StabilityCertificateError("P and Q must be positive definite")
    return 2.0 * gain / q_min * math.sqrt(p_max / p_min) * bound


def search_common_lyapunov(
    vertices: Sequence[np.ndarray],
    *,
    seed: int = 17,
    maxiter: int = 160,
    population_size: int = 12,
) -> LyapunovCertificateV1:
    """Search for a trace-normalized common P using SciPy only.

    This is an offline convenience search, not a substitute for an SDP solver.
    The returned certificate is accepted only after ``check_common_lyapunov``
    recomputes all eigenvalue margins from the returned matrix.
    """

    matrices = tuple(np.asarray(A, dtype=float) for A in vertices)
    try:
        from scipy.optimize import differential_evolution
    except ImportError:
        return LyapunovCertificateV1(
            schema=STABILITY_SCHEMA,
            controller_schema=GENERALIZED_FORCE_SCHEMA,
            feasible=False,
            continuous_margin=float("-inf"),
            discrete_margin=None,
            p_eigen_min=float("nan"),
            q_eigen_min=float("nan"),
            sampled_period_bound_s=None,
            matrix_p=((float("nan"),) * 3,) * 3,
            vertex_count=len(matrices),
            solver="unavailable",
            message="scipy.optimize is unavailable",
        )

    def make_p(parameters: np.ndarray) -> np.ndarray:
        L = np.asarray(
            [
                [math.exp(parameters[0]), 0.0, 0.0],
                [parameters[1], math.exp(parameters[2]), 0.0],
                [parameters[3], parameters[4], math.exp(parameters[5])],
            ],
            dtype=float,
        )
        P = L @ L.T
        return P / float(np.trace(P))

    def objective(parameters: np.ndarray) -> float:
        P = make_p(parameters)
        margins = [float(np.min(np.linalg.eigvalsh(P)))]
        margins.extend(
            float(np.min(np.linalg.eigvalsh(-(A.T @ P + P @ A)))) for A in matrices
        )
        return -min(margins)

    result = differential_evolution(
        objective,
        [(-6.0, 6.0), (-20.0, 20.0), (-6.0, 6.0), (-20.0, 20.0), (-20.0, 20.0), (-6.0, 6.0)],
        seed=seed,
        maxiter=maxiter,
        popsize=population_size,
        polish=True,
        workers=1,
        updating="immediate",
    )
    P = make_p(np.asarray(result.x, dtype=float))
    checked = check_common_lyapunov(P, matrices)
    return LyapunovCertificateV1(
        schema=checked.schema,
        controller_schema=checked.controller_schema,
        feasible=checked.feasible,
        continuous_margin=checked.continuous_margin,
        discrete_margin=checked.discrete_margin,
        p_eigen_min=checked.p_eigen_min,
        q_eigen_min=checked.q_eigen_min,
        sampled_period_bound_s=sampled_period_bound(P, matrices)
        if checked.feasible and checked.q_eigen_min > 1e-12
        else None,
        matrix_p=checked.matrix_p,
        vertex_count=checked.vertex_count,
        solver="scipy_differential_evolution",
        message=str(result.message),
    )


__all__ = [
    "LyapunovCertificateV1",
    "STABILITY_SCHEMA",
    "StabilityCertificateError",
    "check_common_lyapunov",
    "force_state_matrix",
    "interval_vertices",
    "sampled_period_bound",
    "search_common_lyapunov",
    "uub_radius",
]
