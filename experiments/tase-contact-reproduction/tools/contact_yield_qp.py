"""Equality native QP with an explicit measured-task scaling fallback.

Priorities: keep the normal unloading component, then scale tangent and
angular progress.  Deadline and nonfinite failures are not infeasibility.
Failed attempts leave the warm start unchanged.  Command rates are not
measured progress.  This module does not claim a force-feasibility guarantee.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from contact_qp import NativeContactQp, QpError
from contact_semantics import finite_vector6, normalize_unit


SCALE_SEQUENCE = (1.0, 0.5, 0.25, 0.125, 0.0625, 0.0)


class YieldQpError(RuntimeError):
    pass


class YieldQpFatal(YieldQpError):
    """Deadline, nonfinite, or invalid input.  Not an infeasibility fallback."""


def _kind(error: QpError) -> str:
    message = str(error).lower()
    if "deadline" in message:
        return "deadline"
    if "nonfinite" in message or "must be finite" in message:
        return "nonfinite"
    if "inverted" in message:
        return "invalid"
    return "infeasible" if message == "osqp did not solve accurately: status=3" else "solver_failure"


class YieldQp:
    def __init__(self, library: Path | str, *, deadline_s: float | None = None) -> None:
        path = Path(library).resolve()
        self.library = path
        self.library_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        self.qp = NativeContactQp(path, deadline_s=deadline_s)

    def snapshot(self) -> dict[str, Any]:
        return self.qp.snapshot()

    def restore(self, state: Mapping[str, Any]) -> None:
        self.qp.restore(state)

    def reset(self) -> None:
        self.qp.reset()

    def compose(
        self,
        jacobian: Any,
        twist_base: Any,
        lower: Any,
        upper: Any,
        inward_normal_base: Any,
    ) -> dict[str, Any]:
        jac = np.asarray(jacobian, dtype=float)
        twist = finite_vector6(twist_base, "twist_base")
        lo = finite_vector6(lower, "joint_velocity_lower")
        hi = finite_vector6(upper, "joint_velocity_upper")
        normal = normalize_unit(inward_normal_base, name="inward_normal_base")
        if jac.shape != (6, 6) or not np.all(np.isfinite(jac)):
            raise YieldQpFatal("jacobian must be a finite 6x6 matrix")
        linear = twist[:3]
        angular = twist[3:]
        normal_linear = float(np.dot(linear, normal)) * normal
        tangent_linear = linear - normal_linear
        warm = self.qp.snapshot()
        last_infeasible: Exception | None = None
        for scale in SCALE_SEQUENCE:
            scaled = np.concatenate((normal_linear + scale * tangent_linear, scale * angular))
            try:
                solved = self.qp.solve(jac, scaled, lo, hi)
            except QpError as error:
                self.qp.restore(warm)
                kind = _kind(error)
                if kind != "infeasible":
                    raise YieldQpFatal(f"QP {kind} is not an infeasibility fallback: {error}") from error
                last_infeasible = error
                continue
            planned = float(np.linalg.norm(tangent_linear))
            commanded = float(np.linalg.norm(scale * tangent_linear))
            return {
                "qdot_rad_s": solved.qdot,
                "applied_twist_base": tuple(float(value) for value in scaled),
                "requested_twist_base": tuple(float(value) for value in twist),
                "task_scale": float(scale),
                "qp_intervention": scale < 1.0,
                "planned_tangent_rate_m_s": planned,
                "commanded_tangent_rate_m_s": commanded,
                "normal_unloading_preserved": True,
                "feasibility_force_guarantee": False,
                "qp_equality_residual": solved.equality_residual,
                "qp_bound_violation": solved.bound_violation,
                "qp_solve_wall_s": solved.elapsed_s,
                "qp_iterations": solved.iterations,
            }
        raise YieldQpError(
            "native equality QP infeasible even after measured-task scaling "
            f"(normal preserved, tangent scaled to 0): {last_infeasible}"
        )
