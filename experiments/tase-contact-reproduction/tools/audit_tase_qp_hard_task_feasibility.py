#!/usr/bin/env python3
"""Offline feasibility and task-priority sensitivity audits for TASE-QP.

The audit uses sealed command traces to test prioritized tasks under captured
joint/slew bounds. It reports per-row slack lower bounds, not selected
controller tolerances, and never runs a live solver or hardware endpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import linprog

from audit_tase_qp_replay_inputs import PROTOCOL_ID, verify_seal


SCHEMA = "tase.qp-hard-task-feasibility-v1"
FORMAL_WINDOW_S = (5.0, 60.0)
EQUALITY_TOLERANCE = 1e-6
BOX_TOLERANCE = 1e-7


class FeasibilityAuditError(RuntimeError):
    pass


def _vector(value: Any, size: int, name: str) -> np.ndarray:
    try:
        result = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FeasibilityAuditError(f"{name} must be a finite vector of length {size}") from exc
    if result.shape != (size,) or not np.isfinite(result).all():
        raise FeasibilityAuditError(f"{name} must be a finite vector of length {size}")
    return result.copy()


def minimum_componentwise_tangent_slack(
    *,
    jacobian_6x6: Any,
    desired_twist_m_s_rad_s: Any,
    reaction_normal_base: Any,
    tangent_basis_base_3x2: Any,
    qdot_lower_rad_s: Any,
    qdot_upper_rad_s: Any,
) -> dict[str, Any]:
    """Find minimum L-infinity tangent residual with normal/orientation hard."""
    jacobian = np.asarray(jacobian_6x6, dtype=float)
    if jacobian.shape != (6, 6) or not np.isfinite(jacobian).all():
        raise FeasibilityAuditError("jacobian_6x6 must be a finite 6x6 matrix")
    desired = _vector(desired_twist_m_s_rad_s, 6, "desired_twist_m_s_rad_s")
    reaction = _vector(reaction_normal_base, 3, "reaction_normal_base")
    basis = np.asarray(tangent_basis_base_3x2, dtype=float)
    if basis.shape != (3, 2) or not np.isfinite(basis).all():
        raise FeasibilityAuditError("tangent_basis_base_3x2 must be a finite 3x2 matrix")
    lower = _vector(qdot_lower_rad_s, 6, "qdot_lower_rad_s")
    upper = _vector(qdot_upper_rad_s, 6, "qdot_upper_rad_s")
    reaction_norm = float(np.linalg.norm(reaction))
    if reaction_norm <= 1e-12:
        raise FeasibilityAuditError("reaction_normal_base must be nonzero")
    reaction /= reaction_norm
    if (not np.allclose(basis.T @ basis, np.eye(2), atol=1e-8, rtol=0.0)
            or not np.allclose(basis.T @ reaction, np.zeros(2), atol=1e-8, rtol=0.0)):
        raise FeasibilityAuditError("tangent basis must be orthonormal and perpendicular to the normal")
    if np.any(lower > upper):
        return {"feasible": False, "solver_status": "empty joint/slew bound intersection"}

    approach = -reaction
    hard_matrix = np.vstack((approach @ jacobian[:3, :], jacobian[3:, :]))
    hard_target = np.concatenate(((approach @ desired[:3],), desired[3:]))
    tangent_matrix = basis.T @ jacobian[:3, :]
    tangent_target = basis.T @ desired[:3]

    # Variables [qdot(6), slack]. The slack is optimized as an audit result,
    # not provided as a controller parameter. OSQP profile weights are absent.
    equality = np.zeros((4, 7), dtype=float)
    equality[:, :6] = hard_matrix
    inequality = np.zeros((4, 7), dtype=float)
    inequality[:2, :6] = tangent_matrix
    inequality[:2, 6] = -1.0
    inequality[2:, :6] = -tangent_matrix
    inequality[2:, 6] = -1.0
    rhs = np.concatenate((tangent_target, -tangent_target))
    solution = linprog(
        c=np.array((0.0,) * 6 + (1.0,)),
        A_ub=inequality,
        b_ub=rhs,
        A_eq=equality,
        b_eq=hard_target,
        bounds=list(zip(lower, upper, strict=True)) + [(0.0, None)],
        method="highs",
    )
    if not solution.success or solution.x is None:
        if solution.status == 2:
            return {"feasible": False, "solver_status": str(solution.message)}
        raise FeasibilityAuditError(f"linear feasibility audit failed: {solution.message}")

    qdot = np.asarray(solution.x[:6], dtype=float)
    slack = float(solution.x[6])
    hard_residual = hard_matrix @ qdot - hard_target
    tangent_residual = tangent_matrix @ qdot - tangent_target
    bound_violation = max(float(np.max(lower - qdot)), float(np.max(qdot - upper)), 0.0)
    if (not np.isfinite(qdot).all()
            or np.max(np.abs(hard_residual)) > EQUALITY_TOLERANCE
            or bound_violation > BOX_TOLERANCE
            or np.max(np.abs(tangent_residual)) > slack + BOX_TOLERANCE):
        raise FeasibilityAuditError("LP result failed frozen-profile post-solve validation")
    return {
        "feasible": True,
        "minimum_componentwise_tangent_slack_m_s": slack,
        "hard_residual_inf": float(np.max(np.abs(hard_residual))),
        "tangent_residual_components_m_s": [float(x) for x in tangent_residual],
        "box_violation_rad_s": bound_violation,
    }


def maximum_common_hard_task_scale(
    *,
    jacobian_6x6: Any,
    desired_twist_m_s_rad_s: Any,
    reaction_normal_base: Any,
    qdot_lower_rad_s: Any,
    qdot_upper_rad_s: Any,
) -> dict[str, Any]:
    """Find the largest common scale of hard normal/orientation references."""
    jacobian = np.asarray(jacobian_6x6, dtype=float)
    if jacobian.shape != (6, 6) or not np.isfinite(jacobian).all():
        raise FeasibilityAuditError("jacobian_6x6 must be a finite 6x6 matrix")
    desired = _vector(desired_twist_m_s_rad_s, 6, "desired_twist_m_s_rad_s")
    reaction = _vector(reaction_normal_base, 3, "reaction_normal_base")
    lower = _vector(qdot_lower_rad_s, 6, "qdot_lower_rad_s")
    upper = _vector(qdot_upper_rad_s, 6, "qdot_upper_rad_s")
    reaction_norm = float(np.linalg.norm(reaction))
    if reaction_norm <= 1e-12:
        raise FeasibilityAuditError("reaction_normal_base must be nonzero")
    reaction /= reaction_norm
    if np.any(lower > upper):
        return {"feasible": False, "zero_scale_feasible": False,
                "solver_status": "empty joint/slew bound intersection"}

    approach = -reaction
    hard_matrix = np.vstack((approach @ jacobian[:3, :], jacobian[3:, :]))
    hard_target = np.concatenate(((approach @ desired[:3],), desired[3:]))
    equality = np.zeros((4, 7), dtype=float)
    equality[:, :6] = hard_matrix
    equality[:, 6] = -hard_target
    solution = linprog(
        c=np.array((0.0,) * 6 + (-1.0,)),
        A_eq=equality,
        b_eq=np.zeros(4),
        bounds=list(zip(lower, upper, strict=True)) + [(0.0, 1.0)],
        method="highs",
    )
    if not solution.success or solution.x is None:
        if solution.status != 2:
            raise FeasibilityAuditError(f"common-scale audit failed: {solution.message}")
        zero_scale = linprog(
            c=np.zeros(6),
            A_eq=hard_matrix,
            b_eq=np.zeros(4),
            bounds=list(zip(lower, upper, strict=True)),
            method="highs",
        )
        if not zero_scale.success and zero_scale.status != 2:
            raise FeasibilityAuditError(f"zero-scale feasibility check failed: {zero_scale.message}")
        return {
            "feasible": False,
            "zero_scale_feasible": bool(zero_scale.success),
            "solver_status": str(solution.message),
        }

    qdot = np.asarray(solution.x[:6], dtype=float)
    scale = float(solution.x[6])
    residual = hard_matrix @ qdot - scale * hard_target
    bound_violation = max(float(np.max(lower - qdot)), float(np.max(qdot - upper)), 0.0)
    if (not np.isfinite(qdot).all()
            or np.max(np.abs(residual)) > EQUALITY_TOLERANCE
            or bound_violation > BOX_TOLERANCE):
        raise FeasibilityAuditError("common-scale LP failed frozen-profile post-solve validation")
    return {
        "feasible": True,
        "zero_scale_feasible": True,
        "maximum_common_scale": scale,
        "hard_residual_inf": float(np.max(np.abs(residual))),
        "box_violation_rad_s": bound_violation,
    }


def minimum_normal_orientation_priority_slack(
    *,
    jacobian_6x6: Any,
    desired_twist_m_s_rad_s: Any,
    reaction_normal_base: Any,
    qdot_lower_rad_s: Any,
    qdot_upper_rad_s: Any,
) -> dict[str, Any]:
    """Measure per-row slack needed when either normal or orientation stays hard."""
    jacobian = np.asarray(jacobian_6x6, dtype=float)
    if jacobian.shape != (6, 6) or not np.isfinite(jacobian).all():
        raise FeasibilityAuditError("jacobian_6x6 must be a finite 6x6 matrix")
    desired = _vector(desired_twist_m_s_rad_s, 6, "desired_twist_m_s_rad_s")
    reaction = _vector(reaction_normal_base, 3, "reaction_normal_base")
    lower = _vector(qdot_lower_rad_s, 6, "qdot_lower_rad_s")
    upper = _vector(qdot_upper_rad_s, 6, "qdot_upper_rad_s")
    reaction_norm = float(np.linalg.norm(reaction))
    if reaction_norm <= 1e-12:
        raise FeasibilityAuditError("reaction_normal_base must be nonzero")
    reaction /= reaction_norm
    if np.any(lower > upper):
        return {"feasible": False, "solver_status": "empty joint/slew bound intersection"}

    approach = -reaction
    normal_matrix = approach @ jacobian[:3, :]
    normal_target = float(approach @ desired[:3])
    orientation_matrix = jacobian[3:, :]
    orientation_target = desired[3:]
    qdot_bounds = list(zip(lower, upper, strict=True))

    # Keep normal velocity exact and minimize the componentwise orientation error.
    orientation_inequality = np.zeros((6, 7), dtype=float)
    orientation_inequality[:3, :6] = orientation_matrix
    orientation_inequality[:3, 6] = -1.0
    orientation_inequality[3:, :6] = -orientation_matrix
    orientation_inequality[3:, 6] = -1.0
    orientation_rhs = np.concatenate((orientation_target, -orientation_target))
    normal_equality = np.zeros((1, 7), dtype=float)
    normal_equality[0, :6] = normal_matrix
    normal_result = linprog(
        c=np.array((0.0,) * 6 + (1.0,)),
        A_ub=orientation_inequality,
        b_ub=orientation_rhs,
        A_eq=normal_equality,
        b_eq=np.array((normal_target,)),
        bounds=qdot_bounds + [(0.0, None)],
        method="highs",
    )
    if not normal_result.success or normal_result.x is None:
        if normal_result.status == 2:
            return {"feasible": False, "solver_status": str(normal_result.message)}
        raise FeasibilityAuditError(f"normal-priority slack audit failed: {normal_result.message}")

    # Keep all orientation rates exact and minimize the normal-velocity error.
    normal_inequality = np.zeros((2, 7), dtype=float)
    normal_inequality[0, :6] = normal_matrix
    normal_inequality[0, 6] = -1.0
    normal_inequality[1, :6] = -normal_matrix
    normal_inequality[1, 6] = -1.0
    orientation_equality = np.zeros((3, 7), dtype=float)
    orientation_equality[:, :6] = orientation_matrix
    orientation_result = linprog(
        c=np.array((0.0,) * 6 + (1.0,)),
        A_ub=normal_inequality,
        b_ub=np.array((normal_target, -normal_target)),
        A_eq=orientation_equality,
        b_eq=orientation_target,
        bounds=qdot_bounds + [(0.0, None)],
        method="highs",
    )
    if not orientation_result.success or orientation_result.x is None:
        if orientation_result.status == 2:
            return {"feasible": False, "solver_status": str(orientation_result.message)}
        raise FeasibilityAuditError(f"orientation-priority slack audit failed: {orientation_result.message}")

    return {
        "feasible": True,
        "minimum_orientation_linf_slack_rad_s_with_normal_hard": float(normal_result.x[6]),
        "minimum_normal_abs_slack_m_s_with_orientation_hard": float(orientation_result.x[6]),
    }


def _clusters(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    groups: list[list[dict[str, Any]]] = [[rows[0]]]
    for row in rows[1:]:
        if row["packet_sequence"] == groups[-1][-1]["packet_sequence"] + 1:
            groups[-1].append(row)
        else:
            groups.append([row])
    return [
        {
            "start_reference_time_s": group[0]["reference_time_s"],
            "end_reference_time_s": group[-1]["reference_time_s"],
            "packet_count": len(group),
            "host_slew_scale_min": min(item["host_slew_scale"] for item in group),
        }
        for group in groups
    ]


def analyze_attempt(
    attempt_dir: Path,
    *,
    reaction_normal_base: Any,
    tangent_basis_base_3x2: Any,
) -> dict[str, Any]:
    attempt_dir = Path(attempt_dir).resolve(strict=True)
    seal, result, verified = verify_seal(attempt_dir)
    binding = result.get("parameter_binding", {})
    if binding.get("protocol_id") != PROTOCOL_ID or float(binding.get("path_duration_s", -1.0)) != 60.0:
        raise FeasibilityAuditError("attempt protocol is not the sealed r013_60_rate400 60 s identity")
    command_path = Path(verified["command_timeline"]["path"])
    reaction = _vector(reaction_normal_base, 3, "reaction_normal_base")
    basis = np.asarray(tangent_basis_base_3x2, dtype=float)
    infeasible_rows: list[dict[str, Any]] = []
    feasible_slack: list[float] = []
    scale_values: list[float] = []
    task_priority_rows: list[dict[str, Any]] = []
    common_task_scales: list[float] = []
    common_scale_no_solution_rows = 0
    common_scale_zero_infeasible_rows = 0
    task_priority_unresolved_rows = 0
    orientation_priority_slacks: list[float] = []
    normal_priority_slacks: list[float] = []
    formal_rows = 0
    slew_box_only_infeasible_rows = 0
    joint_box_also_infeasible_rows = 0
    previous_sequence = -1
    with command_path.open("r", encoding="utf-8") as source:
        for line in source:
            row = json.loads(line)
            if row.get("reference_phase") != "path":
                continue
            reference_time = float(row.get("reference_time_s", math.nan))
            if not FORMAL_WINDOW_S[0] <= reference_time < FORMAL_WINDOW_S[1]:
                continue
            sequence = int(row.get("packet_sequence", -1))
            if sequence <= previous_sequence:
                raise FeasibilityAuditError("formal PATH packet sequence regressed")
            previous_sequence = sequence
            scale = float(row.get("host_slew_scale", math.nan))
            if not math.isfinite(scale) or not 0.0 <= scale <= 1.0 + 1e-12:
                raise FeasibilityAuditError("host_slew_scale is invalid")
            scale_values.append(scale)
            joint_lower = _vector(row.get("solver_qdot_lower_rad_s"), 6, "solver_qdot_lower_rad_s")
            joint_upper = _vector(row.get("solver_qdot_upper_rad_s"), 6, "solver_qdot_upper_rad_s")
            slew_lower = _vector(row.get("slew_adjusted_qdot_lower_rad_s"), 6, "slew_adjusted_qdot_lower_rad_s")
            slew_upper = _vector(row.get("slew_adjusted_qdot_upper_rad_s"), 6, "slew_adjusted_qdot_upper_rad_s")
            effective_lower = np.maximum(joint_lower, slew_lower)
            effective_upper = np.minimum(joint_upper, slew_upper)
            audit = minimum_componentwise_tangent_slack(
                jacobian_6x6=row.get("jacobian_6x6"),
                desired_twist_m_s_rad_s=row.get("requested_outer_twist_m_s_rad_s"),
                reaction_normal_base=reaction,
                tangent_basis_base_3x2=basis,
                qdot_lower_rad_s=effective_lower,
                qdot_upper_rad_s=effective_upper,
            )
            formal_rows += 1
            if audit["feasible"]:
                feasible_slack.append(audit["minimum_componentwise_tangent_slack_m_s"])
            else:
                joint_only_audit = minimum_componentwise_tangent_slack(
                    jacobian_6x6=row.get("jacobian_6x6"),
                    desired_twist_m_s_rad_s=row.get("requested_outer_twist_m_s_rad_s"),
                    reaction_normal_base=reaction,
                    tangent_basis_base_3x2=basis,
                    qdot_lower_rad_s=joint_lower,
                    qdot_upper_rad_s=joint_upper,
                )
                if joint_only_audit["feasible"]:
                    slew_box_only_infeasible_rows += 1
                    infeasibility_source = "introduced_by_slew_box"
                    common_scale_audit = maximum_common_hard_task_scale(
                        jacobian_6x6=row.get("jacobian_6x6"),
                        desired_twist_m_s_rad_s=row.get("requested_outer_twist_m_s_rad_s"),
                        reaction_normal_base=reaction,
                        qdot_lower_rad_s=effective_lower,
                        qdot_upper_rad_s=effective_upper,
                    )
                    if common_scale_audit["feasible"]:
                        common_task_scales.append(common_scale_audit["maximum_common_scale"])
                    else:
                        common_scale_no_solution_rows += 1
                        if not common_scale_audit["zero_scale_feasible"]:
                            common_scale_zero_infeasible_rows += 1
                    priority_audit = minimum_normal_orientation_priority_slack(
                        jacobian_6x6=row.get("jacobian_6x6"),
                        desired_twist_m_s_rad_s=row.get("requested_outer_twist_m_s_rad_s"),
                        reaction_normal_base=reaction,
                        qdot_lower_rad_s=effective_lower,
                        qdot_upper_rad_s=effective_upper,
                    )
                    if priority_audit["feasible"]:
                        orientation_slack = priority_audit[
                            "minimum_orientation_linf_slack_rad_s_with_normal_hard"
                        ]
                        normal_slack = priority_audit[
                            "minimum_normal_abs_slack_m_s_with_orientation_hard"
                        ]
                        orientation_priority_slacks.append(orientation_slack)
                        normal_priority_slacks.append(normal_slack)
                    else:
                        task_priority_unresolved_rows += 1
                        orientation_slack = None
                        normal_slack = None
                    task_priority_rows.append({
                        "packet_sequence": sequence,
                        "reference_time_s": reference_time,
                        "host_slew_scale": scale,
                        "maximum_common_hard_task_scale": (
                            common_scale_audit.get("maximum_common_scale")
                            if common_scale_audit["feasible"] else None
                        ),
                        "common_scale_feasible": common_scale_audit["feasible"],
                        "zero_scale_feasible": common_scale_audit["zero_scale_feasible"],
                        "minimum_orientation_linf_slack_rad_s_with_normal_hard": orientation_slack,
                        "minimum_normal_abs_slack_m_s_with_orientation_hard": normal_slack,
                    })
                else:
                    joint_box_also_infeasible_rows += 1
                    infeasibility_source = "infeasible_with_joint_box_alone"
                infeasible_rows.append({
                    "packet_sequence": sequence,
                    "reference_time_s": reference_time,
                    "host_slew_scale": scale,
                    "infeasibility_source": infeasibility_source,
                    "solver_status": audit["solver_status"],
                })

    if formal_rows == 0:
        raise FeasibilityAuditError("no formal PATH rows in the declared half-open window")
    scales = np.asarray(scale_values, dtype=float)
    slack = np.asarray(feasible_slack, dtype=float)
    quantiles = (0.50, 0.90, 0.95, 0.99, 1.0)
    return {
        "schema": SCHEMA,
        "status": "offline_feasibility_diagnostic_only",
        "live_eligible": False,
        "hardware_or_endpoint_action": False,
        "attempt_dir": str(attempt_dir),
        "protocol_id": binding["protocol_id"],
        "candidate_id": binding.get("candidate_id"),
        "formal_window_s": list(FORMAL_WINDOW_S),
        "normal_basis": {
            "reaction_normal_base": [float(x) for x in reaction],
            "tangent_basis_base_3x2": basis.tolist(),
            "source_note": "explicit existing TASE control basis; not terrain geometry",
        },
        "source_command_timeline": {
            "path": str(command_path),
            "sha256": verified["command_timeline"]["sha256"],
            "rows": verified["command_timeline"]["rows"],
        },
        "row_counts": {
            "formal_path_rows": formal_rows,
            "hard_normal_orientation_feasible_with_captured_joint_and_slew_bounds": len(feasible_slack),
            "infeasible_even_with_unbounded_tangent_slack": len(infeasible_rows),
            "infeasible_only_after_intersecting_slew_box": slew_box_only_infeasible_rows,
            "infeasible_with_joint_box_alone": joint_box_also_infeasible_rows,
            "host_slew_scaled_rows": int(np.sum(scales < 1.0 - 1e-9)),
            "infeasible_rows_also_slew_scaled": sum(row["host_slew_scale"] < 1.0 - 1e-9 for row in infeasible_rows),
        },
        "hard_task_infeasible_sequence_clusters": _clusters(infeasible_rows),
        "first_hard_task_infeasible": infeasible_rows[0] if infeasible_rows else None,
        "task_priority_sensitivity_offline_only": {
            "status": "diagnostic_only_no_priority_or_tolerance_selected",
            "scope_rows": len(task_priority_rows),
            "task_individual_priority_feasibility_rows": (
                len(task_priority_rows) - task_priority_unresolved_rows
            ),
            "common_scale": {
                "max_scale_feasible_rows": len(common_task_scales),
                "no_common_scale_solution_rows": common_scale_no_solution_rows,
                "no_common_scale_solution_even_at_zero_rows": common_scale_zero_infeasible_rows,
                "quantile_levels": [0.0, 0.01, 0.05, 0.5, 0.95, 1.0],
                "maximum_scale_alpha_quantiles": (
                    np.quantile(common_task_scales, [0.0, 0.01, 0.05, 0.5, 0.95, 1.0]).tolist()
                    if common_task_scales else []
                ),
            },
            "minimum_single_task_priority_slack": {
                "preserve_normal_hard_orientation_linf_slack_rad_s_quantiles_min_p01_p05_p50_p95_max": (
                    np.quantile(orientation_priority_slacks, [0.0, 0.01, 0.05, 0.5, 0.95, 1.0]).tolist()
                    if orientation_priority_slacks else []
                ),
                "preserve_orientation_hard_normal_abs_slack_m_s_quantiles_min_p01_p05_p50_p95_max": (
                    np.quantile(normal_priority_slacks, [0.0, 0.01, 0.05, 0.5, 0.95, 1.0]).tolist()
                    if normal_priority_slacks else []
                ),
            },
            "per_row": task_priority_rows,
            "interpretation": "Common scaling can still be infeasible if the captured slew box cannot realize a zero hard task. The priority slacks are independent per-row lower bounds, not temporally coherent commands, chosen tolerances, or evidence of closed-loop force/path performance.",
        },
        "host_slew_scale_on_infeasible_quantiles_min_median_p95_max": (
            np.quantile([row["host_slew_scale"] for row in infeasible_rows], [0.0, 0.5, 0.95, 1.0]).tolist()
            if infeasible_rows else []
        ),
        "minimum_slack_for_otherwise_feasible_rows_m_s": {
            "quantile_levels": list(quantiles),
            "componentwise_linf_values": np.quantile(slack, quantiles).tolist() if slack.size else [],
            "interpretation": "Per-row minimum tangent slack for feasibility only; not a selected controller bound and not a smoothing/trajectory acceptance result.",
        },
        "numerical_method": {
            "solver": "scipy.optimize.linprog(method=highs)",
            "objective": "minimize nonnegative componentwise tangent slack",
            "hard_equalities": "normal velocity and three orientation rates",
            "hard_bounds": "intersection of captured joint-qdot box and captured host-slew box",
            "post_solve_equality_tolerance": EQUALITY_TOLERANCE,
            "post_solve_box_tolerance": BOX_TOLERANCE,
            "seal_verified": True,
            "seal_sha256": hashlib.sha256((attempt_dir / "seal.json").read_bytes()).hexdigest(),
        },
        "interpretation": "Tangent slack cannot resolve samples infeasible in the hard normal/orientation tasks under the unchanged joint/slew bounds. The separate joint-only replay attributes each such row either to the slew-box intersection or to infeasibility already present under joint bounds. The additional task-priority sensitivity is offline-only, has no selected priority or tolerance, and does not authorize live use.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--reaction-normal-base", type=float, nargs=3, required=True)
    parser.add_argument("--tangent-basis-base", type=float, nargs=6, required=True,
                        help="row-major 3x2 orthonormal basis; two tangent axes are explicit")
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args()
    basis = np.asarray(args.tangent_basis_base, dtype=float).reshape((3, 2))
    report = analyze_attempt(
        args.attempt_dir,
        reaction_normal_base=args.reaction_normal_base,
        tangent_basis_base_3x2=basis,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "json": str(args.json_out),
        "row_counts": report["row_counts"],
        "first_hard_task_infeasible": report["first_hard_task_infeasible"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
