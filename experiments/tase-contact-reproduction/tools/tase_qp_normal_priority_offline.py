"""Offline lexicographic normal-first TASE QP formulation.

Task priority is fixed as normal force velocity, posture/orientation, tangent
path. Joint and captured host-slew bounds remain hard. This module is offline
only: it has no registry entry, transport, or live package binding.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any

import numpy as np
import osqp
from scipy import sparse


METHOD_ID = "TASE_QP_NORMAL_PRIORITY_LEXICOGRAPHIC_OFFLINE_V1"
EPS_ABS = 1e-8
EPS_REL = 1e-8
MAX_ITER = 400
EQUALITY_TOLERANCE = 1e-6
BOUND_TOLERANCE = 1e-7
DEFAULT_OSQP_RHO = 0.1


class LexicographicTaseQpError(ValueError):
    """Malformed input, infeasible bound intersection, or rejected solve."""


@dataclass(frozen=True)
class LexicographicTaseQpResult:
    qdot_rad_s: tuple[float, float, float, float, float, float]
    requested_normal_m_s: float
    achieved_normal_m_s: float
    normal_residual_m_s: float
    orientation_residual_rad_s: tuple[float, float, float]
    tangential_residual_m_s: tuple[float, float]
    effective_lower_rad_s: tuple[float, float, float, float, float, float]
    effective_upper_rad_s: tuple[float, float, float, float, float, float]
    stage_iterations: tuple[int, int, int, int]
    stage_solve_time_s: tuple[float, float, float, float]
    stage_methods: tuple[str, str, str, str]
    max_stage_equality_residual: float
    max_bound_violation_rad_s: float
    offline_only: bool = True
    live_eligible: bool = False


def _vector(value: Any, size: int, label: str) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LexicographicTaseQpError(f"{label} must be a finite vector of length {size}") from exc
    if array.shape != (size,) or not np.isfinite(array).all():
        raise LexicographicTaseQpError(f"{label} must be a finite vector of length {size}")
    return array.copy()


def _matrix(value: Any, shape: tuple[int, int], label: str) -> np.ndarray:
    try:
        array = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise LexicographicTaseQpError(f"{label} must be a finite matrix with shape {shape}") from exc
    if array.shape != shape or not np.isfinite(array).all():
        raise LexicographicTaseQpError(f"{label} must be a finite matrix with shape {shape}")
    return array.copy()


def _solve_bounded_least_squares(
    *,
    stage_name: str,
    warm_start_qdot: np.ndarray | None,
    objective_matrix: np.ndarray,
    objective_target: np.ndarray,
    equality_matrix: np.ndarray,
    equality_target: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    osqp_rho: float,
) -> tuple[np.ndarray, int, float]:
    variables = lower.size
    if objective_matrix.ndim != 2 or objective_matrix.shape[1] != variables:
        raise LexicographicTaseQpError("objective matrix has an invalid shape")
    if objective_target.shape != (objective_matrix.shape[0],):
        raise LexicographicTaseQpError("objective target has an invalid shape")
    if equality_matrix.ndim != 2 or equality_matrix.shape[1] != variables:
        raise LexicographicTaseQpError("equality matrix has an invalid shape")
    if equality_target.shape != (equality_matrix.shape[0],):
        raise LexicographicTaseQpError("equality target has an invalid shape")
    if np.any(lower > upper):
        raise LexicographicTaseQpError("joint-velocity and slew bounds have empty intersection")

    p_dense = 2.0 * objective_matrix.T @ objective_matrix
    q = -2.0 * objective_matrix.T @ objective_target
    p = sparse.csc_matrix(0.5 * (p_dense + p_dense.T))
    identity = np.eye(variables)
    if equality_matrix.shape[0]:
        equality_row_norms = np.linalg.norm(equality_matrix, axis=1)
        if np.any(equality_row_norms <= 1e-15):
            raise LexicographicTaseQpError(f"stage={stage_name} equality matrix has a zero row")
        # Row scaling is algebraically neutral, but improves conditioning when
        # normal m/s and orientation rad/s equalities share one KKT system.
        scaled_equalities = equality_matrix / equality_row_norms[:, None]
        scaled_targets = equality_target / equality_row_norms
    else:
        scaled_equalities = equality_matrix
        scaled_targets = equality_target
    constraint_matrix = sparse.csc_matrix(np.vstack((scaled_equalities, identity)))
    low = np.concatenate((scaled_targets, lower))
    high = np.concatenate((scaled_targets, upper))
    solver = osqp.OSQP()
    solver.setup(
        P=p,
        q=q,
        A=constraint_matrix,
        l=low,
        u=high,
        verbose=False,
        eps_abs=EPS_ABS,
        eps_rel=EPS_REL,
        max_iter=MAX_ITER,
        check_termination=1,
        adaptive_rho_interval=25,
        rho=osqp_rho,
        polishing=False,
        warm_starting=True,
    )
    if warm_start_qdot is not None:
        warm_start = np.asarray(warm_start_qdot, dtype=float)
        if warm_start.shape != (variables,) or not np.isfinite(warm_start).all():
            raise LexicographicTaseQpError(f"stage={stage_name} warm start must be a finite qdot vector")
        solver.warm_start(x=warm_start)
    solved = solver.solve()
    status = str(solved.info.status).lower()
    if status != "solved" or solved.x is None:
        primal_residual = getattr(solved.info, "prim_res", None)
        dual_residual = getattr(solved.info, "dual_res", None)
        raise LexicographicTaseQpError(
            f"stage={stage_name} status={status} iterations={getattr(solved.info, 'iter', None)} "
            f"solve_time_s={getattr(solved.info, 'solve_time', None)} "
            f"primal_residual={primal_residual} dual_residual={dual_residual}"
        )
    qdot = np.asarray(solved.x, dtype=float)
    equality_residual = equality_matrix @ qdot - equality_target
    bound_violation = max(
        0.0,
        float(np.max(lower - qdot)),
        float(np.max(qdot - upper)),
    )
    if (
        not np.isfinite(qdot).all()
        or (equality_residual.size and float(np.max(np.abs(equality_residual))) > EQUALITY_TOLERANCE)
        or bound_violation > BOUND_TOLERANCE
    ):
        raise LexicographicTaseQpError("lexicographic QP stage failed post-solve validation")
    return qdot, int(solved.info.iter), float(solved.info.solve_time)


def _solve_bounded_two_dimensional_least_squares(
    *,
    objective_matrix: np.ndarray,
    objective_target: np.ndarray,
    constraint_matrix: np.ndarray,
    constraint_upper: np.ndarray,
) -> tuple[np.ndarray, int]:
    """Solve a strictly convex 2-D least-squares objective over linear bounds.

    The minimizer is either unconstrained, on one active edge, or at a vertex;
    enumerating those cases avoids adding a task weight or a new slack value.
    """
    matrix = _matrix(objective_matrix, (2, 2), "reduced_tangent_matrix")
    target = _vector(objective_target, 2, "reduced_tangent_target")
    constraints = np.asarray(constraint_matrix, dtype=float)
    limits = np.asarray(constraint_upper, dtype=float)
    if (
        constraints.ndim != 2 or constraints.shape[1] != 2
        or limits.shape != (constraints.shape[0],)
        or not np.isfinite(constraints).all() or not np.isfinite(limits).all()
    ):
        raise LexicographicTaseQpError("reduced tangent polytope is invalid")
    if np.linalg.matrix_rank(matrix) != 2:
        raise LexicographicTaseQpError("reduced tangent objective is rank deficient")
    tolerance = BOUND_TOLERANCE

    candidate_groups: list[np.ndarray] = []

    def feasible_rows(points: np.ndarray) -> np.ndarray:
        return (
            np.isfinite(points).all(axis=1)
            & np.all(points @ constraints.T <= limits[None, :] + tolerance, axis=1)
        )

    unconstrained = np.linalg.solve(matrix, target).reshape(1, 2)
    if feasible_rows(unconstrained)[0]:
        candidate_groups.append(unconstrained)
    origin = np.zeros((1, 2), dtype=float)
    if feasible_rows(origin)[0]:
        candidate_groups.append(origin)

    # Minimize along every active boundary line in one vectorized operation.
    norm_squared = np.einsum("ij,ij->i", constraints, constraints)
    valid_edges = norm_squared > 1e-24
    edge_normals = constraints[valid_edges]
    edge_limits = limits[valid_edges]
    edge_norm_squared = norm_squared[valid_edges]
    if edge_normals.size:
        points_on_lines = edge_normals * (edge_limits / edge_norm_squared)[:, None]
        tangents = np.column_stack((-edge_normals[:, 1], edge_normals[:, 0]))
        tangents /= np.sqrt(edge_norm_squared)[:, None]
        tangent_objectives = tangents @ matrix.T
        residuals_on_lines = points_on_lines @ matrix.T - target
        denominators = np.einsum("ij,ij->i", tangent_objectives, tangent_objectives)
        valid_objectives = denominators > 1e-24
        if np.any(valid_objectives):
            scalars = -np.einsum(
                "ij,ij->i",
                tangent_objectives[valid_objectives],
                residuals_on_lines[valid_objectives],
            ) / denominators[valid_objectives]
            edge_candidates = (
                points_on_lines[valid_objectives]
                + tangents[valid_objectives] * scalars[:, None]
            )
            edge_candidates = edge_candidates[feasible_rows(edge_candidates)]
            if edge_candidates.size:
                candidate_groups.append(edge_candidates)

    # Pairwise active-boundary intersections cover polygon vertices and
    # degenerate line/point feasible sets. Compute all pairs at once.
    first_indices, second_indices = np.triu_indices(constraints.shape[0], k=1)
    first_rows = constraints[first_indices]
    second_rows = constraints[second_indices]
    determinants = first_rows[:, 0] * second_rows[:, 1] - first_rows[:, 1] * second_rows[:, 0]
    nonsingular = np.abs(determinants) > 1e-12
    if np.any(nonsingular):
        first_rows = first_rows[nonsingular]
        second_rows = second_rows[nonsingular]
        first_limits = limits[first_indices[nonsingular]]
        second_limits = limits[second_indices[nonsingular]]
        determinants = determinants[nonsingular]
        vertices = np.column_stack((
            (first_limits * second_rows[:, 1] - first_rows[:, 1] * second_limits) / determinants,
            (first_rows[:, 0] * second_limits - first_limits * second_rows[:, 0]) / determinants,
        ))
        vertices = vertices[feasible_rows(vertices)]
        if vertices.size:
            candidate_groups.append(vertices)
    if not candidate_groups:
        raise LexicographicTaseQpError("reduced tangent polytope has no enumerable feasible minimizer")
    candidates = np.vstack(candidate_groups)
    residuals = candidates @ matrix.T - target
    objectives = np.einsum("ij,ij->i", residuals, residuals)
    best_index = int(np.argmin(objectives))
    return candidates[best_index], int(candidates.shape[0])


def solve_normal_priority_qp(
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
    osqp_rho: float = DEFAULT_OSQP_RHO,
) -> LexicographicTaseQpResult:
    """Solve strict priorities without task weights or selected slack values.

    Four bounded QPs lexicographically minimize: (1) normal velocity error,
    (2) orientation error while preserving the best normal output, (3) tangent
    error while preserving best normal/orientation outputs, then (4) distance
    to the previous qdot while preserving all achieved task outputs.
    """
    jacobian = _matrix(jacobian_6x6, (6, 6), "jacobian_6x6")
    desired = _vector(desired_twist_m_s_rad_s, 6, "desired_twist_m_s_rad_s")
    normal = _vector(reaction_normal_base, 3, "reaction_normal_base")
    tangent = _matrix(tangent_basis_base_3x2, (3, 2), "tangent_basis_base_3x2")
    previous = _vector(previous_qdot_rad_s, 6, "previous_qdot_rad_s")
    joint_lower = _vector(joint_velocity_lower_rad_s, 6, "joint_velocity_lower_rad_s")
    joint_upper = _vector(joint_velocity_upper_rad_s, 6, "joint_velocity_upper_rad_s")
    slew_lower = _vector(slew_velocity_lower_rad_s, 6, "slew_velocity_lower_rad_s")
    slew_upper = _vector(slew_velocity_upper_rad_s, 6, "slew_velocity_upper_rad_s")
    if not math.isfinite(float(osqp_rho)) or float(osqp_rho) <= 0.0:
        raise LexicographicTaseQpError("osqp_rho must be finite and positive")
    osqp_rho = float(osqp_rho)

    norm = float(np.linalg.norm(normal))
    if norm <= 1e-12:
        raise LexicographicTaseQpError("reaction_normal_base is degenerate")
    normal /= norm
    if (
        not np.allclose(tangent.T @ tangent, np.eye(2), rtol=0.0, atol=1e-8)
        or not np.allclose(tangent.T @ normal, np.zeros(2), rtol=0.0, atol=1e-8)
    ):
        raise LexicographicTaseQpError("tangent basis must be orthonormal and normal-orthogonal")
    lower = np.maximum(joint_lower, slew_lower)
    upper = np.minimum(joint_upper, slew_upper)
    if np.any(lower > upper):
        raise LexicographicTaseQpError("joint-velocity and slew bounds have empty intersection")

    approach = -normal
    normal_row = (approach @ jacobian[:3, :]).reshape(1, 6)
    normal_target = np.asarray((float(approach @ desired[:3]),))
    orientation_matrix = jacobian[3:, :]
    orientation_target = desired[3:]
    tangent_matrix = tangent.T @ jacobian[:3, :]
    tangent_target = tangent.T @ desired[:3]

    qdot_1, iter_1, time_1 = _solve_bounded_least_squares(
        stage_name="normal",
        warm_start_qdot=previous,
        objective_matrix=normal_row,
        objective_target=normal_target,
        equality_matrix=np.zeros((0, 6)),
        equality_target=np.zeros(0),
        lower=lower,
        upper=upper,
        osqp_rho=osqp_rho,
    )
    normal_achieved = normal_row @ qdot_1
    qdot_2, iter_2, time_2 = _solve_bounded_least_squares(
        stage_name="orientation",
        warm_start_qdot=qdot_1,
        objective_matrix=orientation_matrix,
        objective_target=orientation_target,
        equality_matrix=normal_row,
        equality_target=normal_achieved,
        lower=lower,
        upper=upper,
        osqp_rho=osqp_rho,
    )
    hard_matrix_2 = np.vstack((normal_row, orientation_matrix))
    hard_target_2 = hard_matrix_2 @ qdot_2
    tangent_started = time.perf_counter()
    left, singular_values, right = np.linalg.svd(hard_matrix_2, full_matrices=True)
    del left
    rank = int(np.count_nonzero(singular_values > max(hard_matrix_2.shape) * np.finfo(float).eps * singular_values[0]))
    nullspace = right[rank:, :].T
    stage_3_method = "osqp_bounded_least_squares_fallback"
    if rank == 4 and nullspace.shape == (6, 2):
        reduced_matrix = tangent_matrix @ nullspace
        reduced_target = tangent_target - tangent_matrix @ qdot_2
        inequality = np.vstack((nullspace, -nullspace))
        inequality_upper = np.concatenate((upper - qdot_2, qdot_2 - lower))
        try:
            reduced_solution, iter_3 = _solve_bounded_two_dimensional_least_squares(
                objective_matrix=reduced_matrix,
                objective_target=reduced_target,
                constraint_matrix=inequality,
                constraint_upper=inequality_upper,
            )
            qdot_3 = qdot_2 + nullspace @ reduced_solution
            stage_3_method = "two_dimensional_active_set"
        except LexicographicTaseQpError:
            qdot_3, iter_3, _stage_3_qp_time = _solve_bounded_least_squares(
                stage_name="tangent_fallback",
                warm_start_qdot=qdot_2,
                objective_matrix=tangent_matrix,
                objective_target=tangent_target,
                equality_matrix=hard_matrix_2,
                equality_target=hard_target_2,
                lower=lower,
                upper=upper,
                osqp_rho=osqp_rho,
            )
    else:
        qdot_3, iter_3, _stage_3_qp_time = _solve_bounded_least_squares(
            stage_name="tangent_fallback",
            warm_start_qdot=qdot_2,
            objective_matrix=tangent_matrix,
            objective_target=tangent_target,
            equality_matrix=hard_matrix_2,
            equality_target=hard_target_2,
                lower=lower,
                upper=upper,
                osqp_rho=osqp_rho,
        )
    time_3 = time.perf_counter() - tangent_started
    all_task_matrix = np.vstack((hard_matrix_2, tangent_matrix))
    all_task_target = all_task_matrix @ qdot_3
    task_rank = int(np.linalg.matrix_rank(all_task_matrix))
    if task_rank == 6:
        # The task coordinates span Cartesian linear/angular velocity; with
        # full-rank J the command is unique once all achieved tasks are fixed.
        qdot_4 = qdot_3.copy()
        iter_4 = 0
        time_4 = 0.0
        stage_4_method = "unique_task_realization_no_tiebreak_solve"
    else:
        qdot_4, iter_4, time_4 = _solve_bounded_least_squares(
            stage_name="joint_change_tiebreak",
            warm_start_qdot=qdot_3,
            objective_matrix=np.eye(6),
            objective_target=previous,
            equality_matrix=all_task_matrix,
            equality_target=all_task_target,
            lower=lower,
            upper=upper,
            osqp_rho=osqp_rho,
        )
        stage_4_method = "osqp_previous_qdot_tiebreak"

    achieved_normal = float(normal_row @ qdot_4)
    achieved_orientation = orientation_matrix @ qdot_4
    achieved_tangent = tangent_matrix @ qdot_4
    residuals = np.concatenate((
        normal_row @ qdot_4 - normal_achieved,
        orientation_matrix @ qdot_4 - orientation_matrix @ qdot_2,
        tangent_matrix @ qdot_4 - tangent_matrix @ qdot_3,
    ))
    bound_violation = max(
        0.0,
        float(np.max(lower - qdot_4)),
        float(np.max(qdot_4 - upper)),
    )
    max_equality = float(np.max(np.abs(residuals))) if residuals.size else 0.0
    if max_equality > EQUALITY_TOLERANCE or bound_violation > BOUND_TOLERANCE:
        raise LexicographicTaseQpError("final lexicographic solution failed validation")

    return LexicographicTaseQpResult(
        qdot_rad_s=tuple(float(value) for value in qdot_4),
        requested_normal_m_s=float(normal_target[0]),
        achieved_normal_m_s=achieved_normal,
        normal_residual_m_s=achieved_normal - float(normal_target[0]),
        orientation_residual_rad_s=tuple(float(value) for value in achieved_orientation - orientation_target),
        tangential_residual_m_s=tuple(float(value) for value in achieved_tangent - tangent_target),
        effective_lower_rad_s=tuple(float(value) for value in lower),
        effective_upper_rad_s=tuple(float(value) for value in upper),
        stage_iterations=(iter_1, iter_2, iter_3, iter_4),
        stage_solve_time_s=(time_1, time_2, time_3, time_4),
        stage_methods=("osqp_normal", "osqp_orientation", stage_3_method, stage_4_method),
        max_stage_equality_residual=max_equality,
        max_bound_violation_rad_s=bound_violation,
    )
