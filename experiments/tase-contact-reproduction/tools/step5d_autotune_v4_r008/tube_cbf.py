"""Offline analytic Tube CBF-QP filter for R008 PATH XY twists.

Pure function library: no OSQP, no live hooks, no robot I/O. Evaluates a
single linear CBF inequality on the PATH-frame XY plane and projects the
nominal Cartesian twist when needed:

    min ||v - v_nom||^2  s.t.  grad_h · v_xy + alpha * h >= 0

with ``h = -phi`` (positive inside the ellipse). Outside / degenerate cases
are fail-closed (``infeasible=True``); this module never invents a "safe"
escape velocity for the hard layer.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

PhiFn = Callable[[float, float], float]
GradFn = Callable[[float, float], tuple[float, float]]
RhoFn = Callable[[float, float], float]

DEFAULT_ALPHA_S = 2.0
DEFAULT_ENGAGE_RHO = 0.67
_GRAD_EPS = 1e-15


def _local_phi(u_m: float, v_m: float, a_m: float, b_m: float) -> float:
    """Minimal XY ellipse field (negative inside); matches tube.path_ellipse."""

    rho = math.hypot(u_m / a_m, v_m / b_m)
    return min(a_m, b_m) * (rho - 1.0)


def _local_grad_phi(u_m: float, v_m: float, a_m: float, b_m: float) -> tuple[float, float]:
    rho = math.hypot(u_m / a_m, v_m / b_m)
    if rho == 0.0:
        return (0.0, 0.0)
    scale = min(a_m, b_m) / rho
    return (scale * u_m / (a_m * a_m), scale * v_m / (b_m * b_m))


def _try_runtime_path_ellipse() -> tuple[Callable[..., float], Callable[..., tuple[float, float]]] | None:
    try:
        from ur10e_experiment_runtime.tube.path_ellipse import (  # type: ignore[import-not-found]
            grad_phi as runtime_grad_phi,
            phi as runtime_phi,
        )
    except Exception:
        return None
    return runtime_phi, runtime_grad_phi


_RUNTIME_PATH_ELLIPSE = _try_runtime_path_ellipse()


def _finite(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return 0.0 if result == 0.0 else result


def _finite_positive(value: Any, *, name: str) -> float:
    result = _finite(value, name=name)
    if result <= 0.0:
        raise ValueError(f"{name} must be a finite positive number")
    return result


def _as_xyz(v_nom_xyz: Sequence[float]) -> tuple[float, float, float]:
    if len(v_nom_xyz) < 3:
        raise ValueError("v_nom_xyz must provide at least three components")
    return (
        _finite(v_nom_xyz[0], name="vx"),
        _finite(v_nom_xyz[1], name="vy"),
        _finite(v_nom_xyz[2], name="vz"),
    )


def _as_uv(position_uv: Sequence[float]) -> tuple[float, float]:
    if len(position_uv) < 2:
        raise ValueError("position_uv must provide at least two components")
    return (
        _finite(position_uv[0], name="u"),
        _finite(position_uv[1], name="v"),
    )


@dataclass(frozen=True, slots=True)
class PathEllipse:
    """PATH-frame XY ellipse ``phi`` / ``grad_phi`` / ``rho`` wrapper.

    Prefers ``ur10e_experiment_runtime.tube.path_ellipse`` when importable;
    otherwise uses the local analytic field with the same convention.
    """

    a_m: float
    b_m: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "a_m", _finite_positive(self.a_m, name="a_m"))
        object.__setattr__(self, "b_m", _finite_positive(self.b_m, name="b_m"))

    @property
    def scale_m(self) -> float:
        return min(self.a_m, self.b_m)

    def rho(self, u_m: float, v_m: float) -> float:
        u = _finite(u_m, name="u_m")
        v = _finite(v_m, name="v_m")
        return math.hypot(u / self.a_m, v / self.b_m)

    def phi(self, u_m: float, v_m: float) -> float:
        if _RUNTIME_PATH_ELLIPSE is not None:
            runtime_phi, _ = _RUNTIME_PATH_ELLIPSE
            return float(runtime_phi(u_m, v_m, self.a_m, self.b_m))
        return _local_phi(u_m, v_m, self.a_m, self.b_m)

    def grad_phi(self, u_m: float, v_m: float) -> tuple[float, float]:
        if _RUNTIME_PATH_ELLIPSE is not None:
            _, runtime_grad = _RUNTIME_PATH_ELLIPSE
            gx, gy = runtime_grad(u_m, v_m, self.a_m, self.b_m)
            return (float(gx), float(gy))
        return _local_grad_phi(u_m, v_m, self.a_m, self.b_m)


@dataclass(frozen=True, slots=True)
class TubeCbfResult:
    """One offline CBF-QP evaluation."""

    v_xyz: tuple[float, float, float]
    fired: bool
    infeasible: bool
    h: float
    phi: float
    rho: float
    alpha: float
    engage_rho: float
    engaged: bool
    lambda_step: float
    grad_h_xy: tuple[float, float]
    constraint_nom: float

    @property
    def v_safe_xyz(self) -> tuple[float, float, float] | None:
        """Safe twist when feasible; ``None`` when fail-closed."""

        if self.infeasible:
            return None
        return self.v_xyz


@dataclass(frozen=True, slots=True)
class TubeCbfQp:
    """Single-constraint analytic CBF-QP on PATH XY twists.

    ``rho_fn`` is mandatory for the ``engage_rho`` deadband. Prefer
    ``from_ellipse`` / ``from_semi_axes`` (they supply ellipse.rho). External
    ``phi``/``grad`` sources must pass ``rho_fn`` via ``from_callables`` —
    omitting it raises rather than silently engaging everywhere inside.
    """

    phi_fn: PhiFn
    grad_fn: GradFn
    rho_fn: RhoFn | None = None

    @classmethod
    def from_ellipse(cls, ellipse: PathEllipse) -> TubeCbfQp:
        return cls(
            phi_fn=ellipse.phi,
            grad_fn=ellipse.grad_phi,
            rho_fn=ellipse.rho,
        )

    @classmethod
    def from_semi_axes(cls, a_m: float, b_m: float) -> TubeCbfQp:
        return cls.from_ellipse(PathEllipse(a_m=a_m, b_m=b_m))

    @classmethod
    def from_callables(
        cls,
        phi_fn: PhiFn,
        grad_fn: GradFn,
        *,
        rho_fn: RhoFn | None = None,
    ) -> TubeCbfQp:
        """Build from callables; ``rho_fn`` is required (engage deadband)."""

        if not callable(phi_fn) or not callable(grad_fn):
            raise ValueError("phi_fn and grad_fn must be callable")
        if rho_fn is None:
            raise ValueError(
                "rho_fn is required for engage_rho deadband; "
                "use PathEllipse/from_semi_axes or pass rho_fn"
            )
        if not callable(rho_fn):
            raise ValueError("rho_fn must be callable when provided")
        return cls(phi_fn=phi_fn, grad_fn=grad_fn, rho_fn=rho_fn)

    def filter_twist_xy(
        self,
        v_nom_xyz: Sequence[float],
        position_uv: Sequence[float],
        *,
        alpha: float = DEFAULT_ALPHA_S,
        engage_rho: float = DEFAULT_ENGAGE_RHO,
    ) -> TubeCbfResult:
        """Project XY twist under the CBF; leave ``vz`` unchanged.

        Deep inside (``rho < engage_rho``) returns the nominal twist unfired.
        Outside (``h < 0``) or a violated zero-gradient constraint marks
        ``infeasible=True`` and returns the nominal twist without inventing a
        recovery velocity (``v_safe_xyz`` is ``None``).

        Requires ``self.rho_fn``; missing rho fails closed with ``ValueError``
        instead of treating every in-tube sample as engaged.
        """

        vx, vy, vz = _as_xyz(v_nom_xyz)
        u, v = _as_uv(position_uv)
        alpha_s = _finite(alpha, name="alpha")
        if alpha_s < 0.0:
            raise ValueError("alpha must be >= 0")
        engage = _finite(engage_rho, name="engage_rho")
        if engage < 0.0:
            raise ValueError("engage_rho must be >= 0")
        if self.rho_fn is None:
            raise ValueError(
                "rho_fn is required for engage_rho deadband; "
                "use PathEllipse/from_semi_axes or pass rho_fn"
            )

        phi_val = _finite(self.phi_fn(u, v), name="phi")
        h = -phi_val
        gphi_u, gphi_v = self.grad_fn(u, v)
        gphi_u = _finite(gphi_u, name="grad_phi_u")
        gphi_v = _finite(gphi_v, name="grad_phi_v")
        grad_h = (-gphi_u, -gphi_v)

        rho = _finite(self.rho_fn(u, v), name="rho")
        engaged = bool(rho >= engage)
        constraint_nom = grad_h[0] * vx + grad_h[1] * vy + alpha_s * h

        def _result(
            *,
            v_xyz: tuple[float, float, float],
            fired: bool,
            infeasible: bool,
            lambda_step: float,
        ) -> TubeCbfResult:
            return TubeCbfResult(
                v_xyz=v_xyz,
                fired=fired,
                infeasible=infeasible,
                h=h,
                phi=phi_val,
                rho=rho,
                alpha=alpha_s,
                engage_rho=engage,
                engaged=engaged,
                lambda_step=lambda_step,
                grad_h_xy=grad_h,
                constraint_nom=constraint_nom,
            )

        # Outside the soft ellipse: fail-closed for the soft layer.
        if h < 0.0:
            return _result(
                v_xyz=(vx, vy, vz),
                fired=False,
                infeasible=True,
                lambda_step=0.0,
            )

        if not engaged:
            return _result(
                v_xyz=(vx, vy, vz),
                fired=False,
                infeasible=False,
                lambda_step=0.0,
            )

        a_norm_sq = grad_h[0] * grad_h[0] + grad_h[1] * grad_h[1]
        if a_norm_sq <= _GRAD_EPS:
            # Zero gradient: feasible iff alpha*h >= 0 (true inside/boundary).
            if constraint_nom >= -1e-15:
                return _result(
                    v_xyz=(vx, vy, vz),
                    fired=False,
                    infeasible=False,
                    lambda_step=0.0,
                )
            return _result(
                v_xyz=(vx, vy, vz),
                fired=False,
                infeasible=True,
                lambda_step=0.0,
            )

        if constraint_nom >= 0.0:
            return _result(
                v_xyz=(vx, vy, vz),
                fired=False,
                infeasible=False,
                lambda_step=0.0,
            )

        # Analytic projection onto half-space A·v >= b with b = -alpha*h.
        # v* = v_nom + λ A, λ = (b - A·v_nom) / ||A||^2 = -constraint_nom / ||A||^2
        # for the residual form constraint_nom = A·v_nom + alpha*h.
        lam = -constraint_nom / a_norm_sq
        return _result(
            v_xyz=(vx + lam * grad_h[0], vy + lam * grad_h[1], vz),
            fired=True,
            infeasible=False,
            lambda_step=lam,
        )


def filter_twist_xy(
    v_nom_xyz: Sequence[float],
    position_uv: Sequence[float],
    *,
    a_m: float | None = None,
    b_m: float | None = None,
    ellipse: PathEllipse | None = None,
    phi_fn: PhiFn | None = None,
    grad_fn: GradFn | None = None,
    rho_fn: RhoFn | None = None,
    filter: TubeCbfQp | None = None,
    alpha: float = DEFAULT_ALPHA_S,
    engage_rho: float = DEFAULT_ENGAGE_RHO,
) -> TubeCbfResult:
    """Module-level entry: build or reuse a ``TubeCbfQp`` then filter.

    When supplying external ``phi_fn``/``grad_fn``, ``rho_fn`` is mandatory
    so the engage deadband cannot be silently disabled.
    """

    qp = filter
    if qp is None:
        if phi_fn is not None or grad_fn is not None:
            if phi_fn is None or grad_fn is None:
                raise ValueError("phi_fn and grad_fn must be provided together")
            qp = TubeCbfQp.from_callables(phi_fn, grad_fn, rho_fn=rho_fn)
        elif ellipse is not None:
            qp = TubeCbfQp.from_ellipse(ellipse)
        elif a_m is not None and b_m is not None:
            qp = TubeCbfQp.from_semi_axes(a_m, b_m)
        else:
            raise ValueError("provide ellipse, (a_m, b_m), callables, or filter")
    return qp.filter_twist_xy(
        v_nom_xyz,
        position_uv,
        alpha=alpha,
        engage_rho=engage_rho,
    )


def microbench_filter_twist_xy(
    *,
    n: int = 1000,
    a_m: float = 0.030,
    b_m: float = 0.030,
    alpha: float = DEFAULT_ALPHA_S,
    engage_rho: float = DEFAULT_ENGAGE_RHO,
) -> Mapping[str, float]:
    """Wall-clock microbench for 100 Hz friendliness (no OSQP)."""

    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError("n must be a positive int")
    qp = TubeCbfQp.from_semi_axes(a_m, b_m)
    # Mix deep-inside and near-wall samples so the hot path is exercised.
    samples: list[tuple[tuple[float, float, float], tuple[float, float]]] = []
    for i in range(n):
        if i % 2 == 0:
            samples.append(((0.001, 0.0, 0.002), (0.0, 0.0)))
        else:
            samples.append(((0.05, 0.0, 0.002), (0.022, 0.0)))

    # Warm-up
    for v_nom, pos in samples[:8]:
        qp.filter_twist_xy(v_nom, pos, alpha=alpha, engage_rho=engage_rho)

    times: list[float] = []
    for v_nom, pos in samples:
        t0 = time.perf_counter()
        qp.filter_twist_xy(v_nom, pos, alpha=alpha, engage_rho=engage_rho)
        times.append(time.perf_counter() - t0)

    times.sort()
    p50 = times[int(0.50 * (n - 1))]
    p95 = times[int(0.95 * (n - 1))]
    p99 = times[int(0.99 * (n - 1))]
    return {
        "n": float(n),
        "mean_s": sum(times) / n,
        "p50_s": p50,
        "p95_s": p95,
        "p99_s": p99,
        "max_s": times[-1],
    }


__all__ = [
    "DEFAULT_ALPHA_S",
    "DEFAULT_ENGAGE_RHO",
    "PathEllipse",
    "TubeCbfQp",
    "TubeCbfResult",
    "filter_twist_xy",
    "microbench_filter_twist_xy",
]
