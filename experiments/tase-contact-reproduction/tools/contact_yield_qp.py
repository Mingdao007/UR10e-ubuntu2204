"""Equality native QP with an explicit measured-task scaling fallback.

Legacy priorities keep the normal component while scaling tangent/angular
progress. The live slew-constrained variant first seeks a continuous progress
scale; if full normal velocity is unreachable, it explicitly reports a reduced
normal-only task. Neither variant claims a contact-force bound.  Deadline and nonfinite failures are not infeasibility.
Failed attempts leave the warm start unchanged.  Command rates are not
measured progress.  This module does not claim a force-feasibility guarantee.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from contact_qp import NativeContactQp, QpError
from contact_yield_path_guard import YieldPathGuard
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


def continuous_feasible_scale(jac, normal_twist, progress_twist, lower, upper):
    """Exact scalar interval for a nonsingular six-axis equality task.

    Discrete scales can miss the entire slew-feasible interval. This computes
    q(s)=J^-1 normal + s J^-1 progress, then intersects all joint bounds.
    The native QP still independently solves and validates the chosen task.
    Singular geometry retains the existing native solver fallback.
    """
    try:
        if np.linalg.cond(jac) > 1e10:
            return None
        q = np.linalg.solve(jac, np.column_stack((normal_twist, progress_twist)))
    except np.linalg.LinAlgError:
        return None
    left, right = 0., 1.
    for base, direction, lo, hi in zip(q[:,0], q[:,1], lower, upper):
        if abs(direction) < 1e-14:
            if not lo <= base <= hi:
                return None
            continue
        ends = ((lo-base)/direction, (hi-base)/direction)
        left, right = max(left,min(ends)), min(right,max(ends))
        if left > right:
            return None
    return float(right)


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
        *, continuous_scaling: bool = False, path_guard_context=None,
    ) -> dict[str, Any]:
        jac = np.asarray(jacobian, dtype=float)
        twist = finite_vector6(twist_base, "twist_base")
        lo = finite_vector6(lower, "joint_velocity_lower")
        hi = finite_vector6(upper, "joint_velocity_upper")
        normal = normalize_unit(inward_normal_base, name="inward_normal_base")
        if jac.shape != (6, 6) or not np.all(np.isfinite(jac)):
            raise YieldQpFatal("jacobian must be a finite 6x6 matrix")
        guard = YieldPathGuard(path_guard_context) if path_guard_context is not None else None
        linear = twist[:3]
        angular = twist[3:]
        normal_linear = float(np.dot(linear, normal)) * normal
        tangent_linear = linear - normal_linear
        warm = self.qp.snapshot()
        last_infeasible: Exception | None = None
        candidates = [(1., scale) for scale in SCALE_SEQUENCE]
        if continuous_scaling:
            normal_twist = np.concatenate((normal_linear, np.zeros(3)))
            progress_twist = np.concatenate((tangent_linear, angular))
            candidate = continuous_feasible_scale(jac, normal_twist, progress_twist, lo, hi)
            if candidate is not None:
                candidates.insert(0, (1., candidate))
            else:
                # A frozen pre-PATH state can request a normal velocity which
                # cannot be reached from the last published command in one
                # tick. Prefer the largest feasible same-direction normal-only
                # task; expose that normal tracking was sacrificed to slew.
                normal_scale = continuous_feasible_scale(jac, np.zeros(6), normal_twist, lo, hi)
                if normal_scale is not None:
                    candidates.insert(0, (normal_scale, 0.))
        for normal_scale, scale in dict.fromkeys(candidates):
            scaled = np.concatenate((normal_scale * normal_linear + scale * tangent_linear, scale * angular))
            nominal_scaled = scaled.copy()
            if guard is not None:
                scaled = guard.project(scaled)
            try:
                solved = self.qp.solve(jac, scaled, lo, hi)
            except QpError as error:
                self.qp.restore(warm)
                kind = _kind(error)
                if kind != "infeasible":
                    raise YieldQpFatal(f"QP {kind} is not an infeasibility fallback: {error}") from error
                last_infeasible = error
                continue
            actual_twist = jac @ np.asarray(solved.qdot)
            if guard is not None:
                try:
                    guard.validate(actual_twist)
                except Exception:
                    self.qp.restore(warm)
                    raise
            guard_evidence = guard.evidence(nominal_scaled, scaled) if guard is not None else None
            normal_preserved = normal_scale == 1. and abs(float(np.dot(scaled[:3] - nominal_scaled[:3], normal))) < 1e-12
            planned = float(np.linalg.norm(tangent_linear))
            commanded = float(np.linalg.norm(scale * tangent_linear))
            return {
                "qdot_rad_s": solved.qdot,
                "applied_twist_base": tuple(float(value) for value in scaled),
                "requested_twist_base": tuple(float(value) for value in twist),
                "task_scale": float(scale),
                "qp_scaling_policy": "continuous_interval_v1" if continuous_scaling else "legacy_discrete_v1",
                "qp_intervention": scale < 1.0 or normal_scale < 1.0 or bool(guard_evidence and guard_evidence["intervention"]),
                "path_guard": guard_evidence,
                "planned_tangent_rate_m_s": planned,
                "commanded_tangent_rate_m_s": commanded,
                "normal_unloading_preserved": normal_preserved,
                "normal_task_scale": float(normal_scale),
                "feasibility_force_guarantee": False,
                "qp_equality_residual": solved.equality_residual,
                "qp_bound_violation": solved.bound_violation,
                "qp_solve_wall_s": solved.elapsed_s,
                "qp_iterations": solved.iterations,
            }
        raise YieldQpError(
            "native equality QP infeasible even after measured-task scaling "
            f"(all configured task candidates exhausted): {last_infeasible}"
        )
