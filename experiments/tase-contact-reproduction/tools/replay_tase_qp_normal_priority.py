#!/usr/bin/env python3
"""Offline replay of the lexicographic normal-priority TASE-QP candidate.

The replay uses source outer twists, Jacobians, joint limits, and per-tick
slew deltas from sealed rate460 traces. It evolves its own qdot from the PATH
origin for slew bounds, but source force/pose observations remain fixed, so
this is not a closed-loop force score or live qualification.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

import numpy as np

from audit_tase_qp_hard_task_feasibility import validate_attempt_protocol
from audit_tase_qp_replay_inputs import verify_seal
from tase_qp_normal_priority_offline import (
    DEFAULT_OSQP_RHO,
    DEFAULT_OSQP_SCALING,
    METHOD_ID,
    solve_normal_priority_qp,
)


SCHEMA = "tase.qp-normal-priority-fixed-input-replay-v1"
PATH_START_S = 0.0
FORMAL_START_S = 5.0
FORMAL_END_S = 60.0
FORMAL_BIN_COUNT = 550
PATH_BIN_COUNT = 600
TASE_NORMAL_BASE = np.asarray((0.0, 0.0, 1.0), dtype=float)


class ReplayError(RuntimeError):
    """The sealed source or candidate replay failed its declared evidence contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summary(values: list[float]) -> dict[str, float]:
    array = np.asarray(values, dtype=float)
    if array.size == 0 or not np.isfinite(array).all():
        raise ReplayError("candidate summary has no finite samples")
    q = np.quantile(array, (0.5, 0.95, 0.99, 1.0))
    return {"median": float(q[0]), "p95": float(q[1]), "p99": float(q[2]), "max": float(q[3])}


def _tangent_basis(normal: np.ndarray) -> np.ndarray:
    expected = np.asarray((0.0, 0.0, 1.0))
    if not np.allclose(normal, expected, rtol=0.0, atol=1e-8):
        raise ReplayError("this audit only supports the captured fixed base-Z TASE normal")
    return np.asarray(((1.0, 0.0), (0.0, 1.0), (0.0, 0.0)), dtype=float)


def replay_attempt(
    attempt_dir: Path,
    *,
    osqp_rho: float = DEFAULT_OSQP_RHO,
    osqp_scaling: int = DEFAULT_OSQP_SCALING,
    desired_twist_trace: Path | None = None,
) -> dict[str, Any]:
    attempt_dir = Path(attempt_dir).resolve(strict=True)
    _seal, result, verified = verify_seal(attempt_dir)
    binding = result.get("parameter_binding", {})
    protocol_id = validate_attempt_protocol(binding)
    if protocol_id != "figure8_window60_r013_rate460_v1":
        raise ReplayError("normal-priority candidate replay requires the rate460 source protocol")
    lifecycle = result.get("lifecycle") or {}
    if lifecycle.get("path_complete") is not True or lifecycle.get("home_verified") is not True:
        raise ReplayError("source attempt must have a complete PATH and verified Home")
    command_path = Path(verified["command_timeline"]["path"])
    source_timeline_hash = str(verified["command_timeline"]["sha256"])
    candidate_twist_rows: list[dict[str, Any]] | None = None
    candidate_twist_trace_path: Path | None = None
    candidate_twist_trace_hash: str | None = None
    if desired_twist_trace is not None:
        candidate_twist_trace_path = Path(desired_twist_trace).resolve(strict=True)
        candidate_twist_trace_hash = _sha256(candidate_twist_trace_path)
        opener = gzip.open if candidate_twist_trace_path.suffix == ".gz" else open
        with opener(candidate_twist_trace_path, "rt", encoding="utf-8") as stream:
            candidate_twist_rows = [
                json.loads(line) for line in stream if line.strip()
            ]
        if not candidate_twist_rows:
            raise ReplayError("candidate desired-twist trace is empty")
    path_row_index = 0
    previous_sequence = -1
    previous_candidate_qdot: np.ndarray | None = None
    path_bins: set[int] = set()
    bins: set[int] = set()
    source_qdot: list[np.ndarray] = []
    candidate_qdot: list[np.ndarray] = []
    normal_residuals: list[float] = []
    orientation_residual_norms: list[float] = []
    tangent_residual_norms: list[float] = []
    max_stage_eq: list[float] = []
    bound_violations: list[float] = []
    solve_total_s: list[float] = []
    solve_stage_s: list[list[float]] = [[], [], [], []]
    iterations: list[list[int]] = [[], [], [], []]
    path_solve_total_s: list[float] = []
    path_solve_stage_s: list[list[float]] = [[], [], [], []]
    path_iterations: list[list[int]] = [[], [], [], []]
    stage_methods: tuple[str, ...] | None = None
    row_failures: list[dict[str, Any]] = []
    path_rows = 0
    path_first_time_s: float | None = None
    path_last_time_s: float | None = None
    solver_path_rows_completed = 0
    formal_rows = 0
    with command_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReplayError(f"invalid JSON at {command_path}:{line_number}") from exc
            if row.get("reference_phase") != "path":
                continue
            time_s = float(row.get("reference_time_s", math.nan))
            if not math.isfinite(time_s) or not PATH_START_S <= time_s < FORMAL_END_S:
                raise ReplayError(f"PATH row has invalid/out-of-protocol time {time_s}")
            if path_first_time_s is None:
                path_first_time_s = time_s
                if abs(time_s - PATH_START_S) > 1e-8:
                    raise ReplayError("source PATH does not begin at the PATH origin")
            path_last_time_s = time_s
            candidate_twist_row = None
            if candidate_twist_rows is not None:
                if path_row_index >= len(candidate_twist_rows):
                    raise ReplayError("candidate desired-twist trace ended before source PATH")
                candidate_twist_row = candidate_twist_rows[path_row_index]
                candidate_time = float(candidate_twist_row.get("reference_time_s", math.nan))
                if not math.isfinite(candidate_time) or abs(candidate_time - time_s) > 1e-9:
                    raise ReplayError(
                        f"candidate desired-twist trace time mismatch at PATH row {path_row_index}"
                )
                path_row_index += 1
            sequence = int(row.get("packet_sequence", -1))
            if sequence <= previous_sequence:
                raise ReplayError("PATH packet sequence regressed")
            previous_sequence = sequence
            path_rows += 1
            path_bins.add(int(math.floor((time_s - PATH_START_S) / 0.1 + 1e-9)))
            is_formal = FORMAL_START_S <= time_s < FORMAL_END_S
            if is_formal:
                bins.add(int(math.floor((time_s - FORMAL_START_S) / 0.1 + 1e-9)))
                formal_rows += 1
            try:
                jacobian = row["jacobian_6x6"]
                desired = (
                    row["requested_outer_twist_m_s_rad_s"]
                    if candidate_twist_row is None
                    else candidate_twist_row["candidate_twist_m_s_rad_s"]
                )
                lower_joint = np.asarray(row["solver_qdot_lower_rad_s"], dtype=float)
                upper_joint = np.asarray(row["solver_qdot_upper_rad_s"], dtype=float)
                dt_s = float(row["actual_dt_s"])
                slew_delta = float(row["host_slew_delta_limit_rad_s"])
                published = np.asarray(row["published_packet_qdot_rad_s"], dtype=float)
                if previous_candidate_qdot is None:
                    previous_candidate_qdot = np.asarray(row["previous_published_qdot_rad_s"], dtype=float)
                replay_inputs = row.get("sfc_replay_inputs") or {}
                captured_normal = replay_inputs.get("control_reaction_normal_base")
                normal = (
                    TASE_NORMAL_BASE.copy()
                    if captured_normal is None
                    else np.asarray(captured_normal, dtype=float)
                )
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ReplayError(f"formal row {sequence} lacks solver inputs") from exc
            if (
                lower_joint.shape != (6,) or upper_joint.shape != (6,)
                or published.shape != (6,) or previous_candidate_qdot.shape != (6,)
                or normal.shape != (3,) or not np.isfinite(np.concatenate((lower_joint, upper_joint, published, previous_candidate_qdot, normal))).all()
                or not math.isfinite(dt_s) or dt_s <= 0.0
                or not math.isfinite(slew_delta) or slew_delta < 0.0
            ):
                raise ReplayError(f"PATH row {sequence} contains invalid solver values")
            tangent = _tangent_basis(normal)
            slew_lower = previous_candidate_qdot - slew_delta
            slew_upper = previous_candidate_qdot + slew_delta
            try:
                solved = solve_normal_priority_qp(
                    jacobian_6x6=jacobian,
                    desired_twist_m_s_rad_s=desired,
                    reaction_normal_base=normal,
                    tangent_basis_base_3x2=tangent,
                    previous_qdot_rad_s=previous_candidate_qdot,
                    joint_velocity_lower_rad_s=lower_joint,
                    joint_velocity_upper_rad_s=upper_joint,
                    slew_velocity_lower_rad_s=slew_lower,
                    slew_velocity_upper_rad_s=slew_upper,
                    osqp_rho=osqp_rho,
                    osqp_scaling=osqp_scaling,
                )
            except Exception as exc:
                row_failures.append({
                    "packet_sequence": sequence,
                    "reference_time_s": time_s,
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                break
            qdot = np.asarray(solved.qdot_rad_s, dtype=float)
            previous_candidate_qdot = qdot
            if stage_methods is None:
                stage_methods = tuple(solved.stage_methods)
            elif tuple(solved.stage_methods) != stage_methods:
                raise ReplayError("candidate stage algorithm changed during one attempt")
            solver_path_rows_completed += 1
            stage_times = list(solved.stage_solve_time_s)
            stage_iterations = list(solved.stage_iterations)
            for index in range(4):
                path_solve_stage_s[index].append(stage_times[index])
                path_iterations[index].append(stage_iterations[index])
            path_solve_total_s.append(sum(stage_times))
            if is_formal:
                source_qdot.append(published)
                candidate_qdot.append(qdot)
                normal_residuals.append(float(solved.normal_residual_m_s))
                orientation_residual_norms.append(float(np.linalg.norm(solved.orientation_residual_rad_s)))
                tangent_residual_norms.append(float(np.linalg.norm(solved.tangential_residual_m_s)))
                max_stage_eq.append(float(solved.max_stage_equality_residual))
                bound_violations.append(float(solved.max_bound_violation_rad_s))
                for index in range(4):
                    solve_stage_s[index].append(stage_times[index])
                    iterations[index].append(stage_iterations[index])
                solve_total_s.append(sum(stage_times))
    if (
        candidate_twist_rows is not None
        and not row_failures
        and path_row_index != len(candidate_twist_rows)
    ):
        raise ReplayError(
            f"candidate desired-twist trace has {len(candidate_twist_rows) - path_row_index} extra PATH rows"
        )
    if path_rows == 0:
        raise ReplayError("source has no PATH rows")
    if formal_rows == 0:
        raise ReplayError("source has no formal PATH rows")
    if not row_failures and len(path_bins) != PATH_BIN_COUNT:
        raise ReplayError(f"source PATH bin coverage is {len(path_bins)}/{PATH_BIN_COUNT}")
    if not row_failures and len(bins) != FORMAL_BIN_COUNT:
        raise ReplayError(f"source formal bin coverage is {len(bins)}/{FORMAL_BIN_COUNT}")
    if candidate_qdot:
        source_matrix = np.asarray(source_qdot)
        candidate_matrix = np.asarray(candidate_qdot)
        delta = candidate_matrix - source_matrix
        qdot_change_norm = np.linalg.norm(delta, axis=1)
    else:
        qdot_change_norm = np.asarray([], dtype=float)
    return {
        "schema": SCHEMA,
        "status": "offline_fixed_input_command_replay_only",
        "method_id": METHOD_ID,
        "live_eligible": False,
        "hardware_or_endpoint_action": False,
        "source_attempt_dir": str(attempt_dir),
        "source_candidate_id": binding.get("candidate_id"),
        "source_protocol_id": protocol_id,
        "normal_basis_source": "fixed approved TASE base-Z reaction axis; no surface geometry",
        "source_evidence_eligible": result.get("evidence_eligible"),
        "source_path_complete": lifecycle.get("path_complete"),
        "source_home_verified": lifecycle.get("home_verified"),
        "source_official_mae_n_diagnostic_only": (result.get("evidence") or {}).get("mae_n"),
        "source_command_timeline": {"path": str(command_path), "sha256": source_timeline_hash},
        "candidate_desired_twist_trace": (
            None if candidate_twist_trace_path is None else {
                "path": str(candidate_twist_trace_path),
                "sha256": candidate_twist_trace_hash,
                "rows_consumed": path_row_index,
                "composition": "frozen SFC+TASE candidate twist; same source attempt and timestamps",
            }
        ),
        "formal_window_s": [FORMAL_START_S, FORMAL_END_S],
        "formal_rows": formal_rows,
        "formal_bins": len(bins),
        "solver_rows_completed": len(candidate_qdot),
        "path_window_s": [PATH_START_S, FORMAL_END_S],
        "path_rows": path_rows,
        "path_bins": len(path_bins),
        "path_first_time_s": path_first_time_s,
        "path_last_time_s": path_last_time_s,
        "solver_path_rows_completed": solver_path_rows_completed,
        "candidate_solver_path_complete": bool(
            not row_failures
            and solver_path_rows_completed == path_rows
            and len(path_bins) == PATH_BIN_COUNT
        ),
        "solver_state_started_at_path_origin": bool(
            path_first_time_s is not None
            and abs(path_first_time_s - PATH_START_S) <= 1e-8
            and solver_path_rows_completed > 0
        ),
        "candidate_solver_failures": row_failures,
        "candidate_force_mae_recomputed": False,
        "candidate_is_not_closed_loop": True,
        "metrics": {
            "normal_task_residual_m_s": _summary(normal_residuals) if normal_residuals else None,
            "orientation_task_residual_norm_rad_s": _summary(orientation_residual_norms) if orientation_residual_norms else None,
            "tangent_task_residual_norm_m_s": _summary(tangent_residual_norms) if tangent_residual_norms else None,
            "maximum_locked_task_equality_residual": _summary(max_stage_eq) if max_stage_eq else None,
            "maximum_box_violation_rad_s": _summary(bound_violations) if bound_violations else None,
            "qdot_delta_vs_source_command_norm_rad_s": _summary(qdot_change_norm.tolist()) if qdot_change_norm.size else None,
            "four_qp_stage_total_s": _summary(solve_total_s) if solve_total_s else None,
            "stage_solve_time_s": [_summary(values) if values else None for values in solve_stage_s],
            "stage_iteration_count": [_summary([float(value) for value in values]) if values else None for values in iterations],
            "full_path_four_qp_stage_total_s": _summary(path_solve_total_s) if path_solve_total_s else None,
            "full_path_stage_solve_time_s": [_summary(values) if values else None for values in path_solve_stage_s],
            "full_path_stage_iteration_count": [_summary([float(value) for value in values]) if values else None for values in path_iterations],
        },
        "numerical_profile": {
            "osqp_rho": float(osqp_rho),
            "osqp_scaling": osqp_scaling,
            "eps_abs": 1e-8,
            "eps_rel": 1e-8,
            "max_iter_per_stage": 400,
            "stage_count": 4,
            "stage_methods": list(stage_methods or ()),
            "new_physical_slack_or_weight": None,
            "bounds": "captured joint qdot bounds intersected with per-tick dynamic slew interval rebuilt from candidate's previous qdot and recorded delta",
            "force_source_used_for_scoring": None,
        },
        "interpretation_limit": "The candidate qdot sequence is replayed against frozen source observations, Jacobians, joint limits, and recorded slew deltas. When an explicit desired-twist trace is supplied it is consumed row-by-row after timestamp checks. Force observations are not fed back through the new controller; the source MAE is not a candidate score and no real candidate actual_qd exists.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--osqp-rho", type=float, default=DEFAULT_OSQP_RHO)
    parser.add_argument("--osqp-scaling", type=int, default=DEFAULT_OSQP_SCALING)
    parser.add_argument("--desired-twist-trace", type=Path)
    args = parser.parse_args()
    result = replay_attempt(
        args.attempt_dir,
        osqp_rho=args.osqp_rho,
        osqp_scaling=args.osqp_scaling,
        desired_twist_trace=args.desired_twist_trace,
    )
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "json": str(args.json_out.resolve()),
        "status": result["status"],
        "source_candidate_id": result["source_candidate_id"],
        "formal_rows": result["formal_rows"],
        "solver_rows_completed": result["solver_rows_completed"],
        "path_rows": result["path_rows"],
        "path_bins": result["path_bins"],
        "solver_path_rows_completed": result["solver_path_rows_completed"],
        "candidate_solver_path_complete": result["candidate_solver_path_complete"],
        "candidate_solver_failures": len(result["candidate_solver_failures"]),
        "metrics": result["metrics"],
    }, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
