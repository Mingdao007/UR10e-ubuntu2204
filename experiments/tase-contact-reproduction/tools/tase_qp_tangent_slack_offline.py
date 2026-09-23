#!/usr/bin/env python3
"""Offline reference QP with hard normal/posture tasks and bounded tangent slack.

This is a formulation probe only. It has no transport, hardware interface, live
registry entry, default tangent-slack value, or live eligibility. The caller
must supply the tangent slack bound explicitly so no experiment value is
silently selected here.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np
import osqp
from scipy import sparse


class TaseTangentSlackQpError(ValueError):
    """Invalid offline hierarchical-QP input or rejected solve."""


@dataclass(frozen=True)
class TaseTangentSlackQpResult:
    qdot_rad_s: tuple[float, float, float, float, float, float]
    normal_residual_m_s: float
    orientation_residual_rad_s: tuple[float, float, float]
    tangential_slack_m_s: tuple[float, float]
    tangential_slack_limit_m_s: float
    effective_lower_rad_s: tuple[float, float, float, float, float, float]
    effective_upper_rad_s: tuple[float, float, float, float, float, float]
    solver_status: str
    solver_iterations: int
    solver_time_s: float
    objective: float
    offline_only: bool = True
    live_eligible: bool = False


def _vector(value: Any, size: int, name: str) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TaseTangentSlackQpError(f"{name} must be a finite vector of length {size}") from exc
    if array.shape != (size,) or not np.isfinite(array).all():
        raise TaseTangentSlackQpError(f"{name} must be a finite vector of length {size}")
    return array.copy()


def _matrix(value: Any, shape: tuple[int, int], name: str) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TaseTangentSlackQpError(f"{name} must be a finite matrix of shape {shape}") from exc
    if array.shape != shape or not np.isfinite(array).all():
        raise TaseTangentSlackQpError(f"{name} must be a finite matrix of shape {shape}")
    return array.copy()


def solve_tase_tangent_slack_qp(
    *,
    jacobian_6x6: Any,
    desired_twist_m_s_rad_s: Any,
    reaction_normal_base: Any,
    tangent_basis_base_3x2: Any,
    previous_qdot_rad_s: Any,
    joint_velocity_lower_rad_s: Any,
    joint_velocity_upper_rad_s: Any,
    slew_velocity_lower_rad_s: Any,
    slew_velocity_upper_rad_s: Any,
    tangent_slack_limit_m_s: float,
) -> TaseTangentSlackQpResult:
    """Keep normal velocity and posture exact; trade tangent error for smoothness.

    The bounded tangent slack uses an L-infinity limit in the supplied
    orthonormal tangent basis. Among commands satisfying the hard normal,
    orientation, joint-velocity, and slew constraints, the QP returns the
    command closest in joint-velocity space to ``previous_qdot_rad_s``.
    """

    J = _matrix(jacobian_6x6, (6, 6), "jacobian_6x6")
    desired = _vector(desired_twist_m_s_rad_s, 6, "desired_twist_m_s_rad_s")
    reaction = _vector(reaction_normal_base, 3, "reaction_normal_base")
    basis = _matrix(tangent_basis_base_3x2, (3, 2), "tangent_basis_base_3x2")
    previous = _vector(previous_qdot_rad_s, 6, "previous_qdot_rad_s")
    joint_lower = _vector(joint_velocity_lower_rad_s, 6, "joint_velocity_lower_rad_s")
    joint_upper = _vector(joint_velocity_upper_rad_s, 6, "joint_velocity_upper_rad_s")
    slew_lower = _vector(slew_velocity_lower_rad_s, 6, "slew_velocity_lower_rad_s")
    slew_upper = _vector(slew_velocity_upper_rad_s, 6, "slew_velocity_upper_rad_s")

    slack_limit = float(tangent_slack_limit_m_s)
    if not math.isfinite(slack_limit) or slack_limit < 0.0:
        raise TaseTangentSlackQpError("tangent_slack_limit_m_s must be finite and nonnegative")
    if np.any(joint_lower > joint_upper) or np.any(slew_lower > slew_upper):
        raise TaseTangentSlackQpError("velocity lower bound exceeds upper bound")

    reaction_norm = float(np.linalg.norm(reaction))
    if reaction_norm <= 1e-12:
        raise TaseTangentSlackQpError("reaction_normal_base must have nonzero norm")
    reaction /= reaction_norm
    if not np.allclose(basis.T @ basis, np.eye(2), atol=1e-8, rtol=0.0):
        raise TaseTangentSlackQpError("tangent basis columns must be orthonormal")
    if not np.allclose(basis.T @ reaction, np.zeros(2), atol=1e-8, rtol=0.0):
        raise TaseTangentSlackQpError("tangent basis must be orthogonal to reaction normal")

    lower = np.maximum(joint_lower, slew_lower)
    upper = np.minimum(joint_upper, slew_upper)
    if np.any(lower > upper):
        raise TaseTangentSlackQpError("joint-velocity and slew bounds have empty intersection")

    # The TASE approach axis is opposite the measured reaction normal.
    approach = -reaction
    normal_row = approach @ J[:3, :]
    orientation_rows = J[3:, :]
    tangent_rows = basis.T @ J[:3, :]
    hard_matrix = np.vstack((normal_row, orientation_rows))
    hard_target = np.concatenate(((approach @ desired[:3],), desired[3:]))
    tangent_target = basis.T @ desired[:3]

    # Variables are [qdot(6), tangent_slack(2)]. Task equalities are
    # [normal(1), orientation(3), tangent-plus-slack(2)]; remaining rows are
    # box bounds for qdot and the explicit tangent slack.
    task_matrix = np.zeros((6, 8), dtype=float)
    task_matrix[:4, :6] = hard_matrix
    task_matrix[4:, :6] = tangent_rows
    task_matrix[4:, 6:] = np.eye(2)
    task_target = np.concatenate((hard_target, tangent_target))
    bound_matrix = np.eye(8, dtype=float)
    constraint_matrix = sparse.csc_matrix(np.vstack((task_matrix, bound_matrix)))
    lower_all = np.concatenate((task_target, lower, (-slack_limit, -slack_limit)))
    upper_all = np.concatenate((task_target, upper, (slack_limit, slack_limit)))

    # Minimize ||qdot - previous_qdot||^2. The slack is bounded but unweighted;
    # no unreviewed task-priority weight is introduced.
    P = sparse.diags((2.0,) * 6 + (0.0, 0.0), format="csc")
    q = np.concatenate((-2.0 * previous, np.zeros(2)))
    solver = osqp.OSQP()
    solver.setup(
        P=P,
        q=q,
        A=constraint_matrix,
        l=lower_all,
        u=upper_all,
        verbose=False,
        eps_abs=1e-8,
        eps_rel=1e-8,
        max_iter=400,
        check_termination=1,
        adaptive_rho_interval=25,
        rho=0.1,
        polishing=False,
        warm_starting=True,
    )
    solved = solver.solve()
    status = str(solved.info.status).lower()
    if status != "solved" or solved.x is None:
        raise TaseTangentSlackQpError(f"hierarchical QP rejected: {status}")

    vector = np.asarray(solved.x, dtype=float)
    qdot = vector[:6]
    slack = vector[6:]
    hard_residual = hard_matrix @ qdot - hard_target
    tangent_residual = tangent_rows @ qdot - tangent_target
    bound_violation = float(max(0.0, np.max(lower - qdot), np.max(qdot - upper)))
    slack_violation = float(max(0.0, np.max(np.abs(slack)) - slack_limit))
    if (
        not np.isfinite(vector).all()
        or np.max(np.abs(hard_residual)) > 1e-6
        or np.max(np.abs(tangent_residual + slack)) > 1e-6
        or bound_violation > 1e-7
        or slack_violation > 1e-7
    ):
        raise TaseTangentSlackQpError("hierarchical QP post-solve validation failed")

    return TaseTangentSlackQpResult(
        qdot_rad_s=tuple(float(value) for value in qdot),
        normal_residual_m_s=float(hard_residual[0]),
        orientation_residual_rad_s=tuple(float(value) for value in hard_residual[1:]),
        tangential_slack_m_s=tuple(float(value) for value in slack),
        tangential_slack_limit_m_s=slack_limit,
        effective_lower_rad_s=tuple(float(value) for value in lower),
        effective_upper_rad_s=tuple(float(value) for value in upper),
        solver_status=status,
        solver_iterations=int(solved.info.iter),
        solver_time_s=float(solved.info.solve_time),
        objective=float(solved.info.obj_val),
    )
