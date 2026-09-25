#!/usr/bin/env python3
"""Offline fixed-input comparison: place the captured slew box inside QP bounds."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Sequence

import numpy as np
from scipy.optimize import linprog

from contact_qp import NativeContactQp, QpError
from replay_tase_qp_attempt_fixed_inputs import _finite_vector, _load_inputs


def embedded_slew_bounds(
    joint_lower: Sequence[float],
    joint_upper: Sequence[float],
    previous_qdot: Sequence[float],
    delta_limit: float,
) -> tuple[np.ndarray, np.ndarray]:
    lower = _finite_vector(joint_lower, 6, "joint lower bounds")
    upper = _finite_vector(joint_upper, 6, "joint upper bounds")
    previous = _finite_vector(previous_qdot, 6, "previous qdot")
    limit = float(delta_limit)
    if not math.isfinite(limit) or limit < 0.0:
        raise ValueError("slew delta limit must be finite and nonnegative")
    return np.maximum(lower, previous - limit), np.minimum(upper, previous + limit)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def _status_code(message: str) -> int | None:
    match = re.search(r"status=(-?\d+)", message)
    return int(match.group(1)) if match else None


def confirm_linear_feasibility(
    jacobian: Sequence[Sequence[float]],
    twist: Sequence[float],
    lower: Sequence[float],
    upper: Sequence[float],
) -> dict[str, Any]:
    """Independently check exact J qdot=twist feasibility under the supplied box."""
    matrix = np.asarray(jacobian, dtype=np.float64)
    target = _finite_vector(twist, 6, "task twist")
    lo = _finite_vector(lower, 6, "effective lower bounds")
    hi = _finite_vector(upper, 6, "effective upper bounds")
    if matrix.shape != (6, 6) or not np.isfinite(matrix).all():
        raise ValueError("Jacobian must be a finite 6x6 matrix")
    if np.any(lo > hi):
        return {"success": False, "status": 2, "message": "empty bound intersection"}
    result = linprog(
        np.zeros(6), A_eq=matrix, b_eq=target,
        bounds=list(zip(lo.tolist(), hi.tolist())), method="highs",
    )
    return {"success": bool(result.success), "status": int(result.status), "message": str(result.message)}


def compare(attempt_dir: Path, library: Path, output_dir: Path) -> dict[str, Any]:
    metadata, rows = _load_inputs(attempt_dir, library)
    solver = NativeContactQp(Path(library).resolve(strict=True), deadline_s=None)
    solver.reset()
    previous = _finite_vector(rows[0]["previous_published_qdot_rad_s"], 6, "initial previous qdot")
    results: list[dict[str, Any]] = []
    first_failure: dict[str, Any] | None = None

    for row in rows:
        seq = int(row["packet_sequence"])
        dt_limit = row.get("host_slew_delta_limit_rad_s")
        if dt_limit is None:
            first_failure = {
                "packet_sequence": seq,
                "reference_time_s": float(row["reference_time_s"]),
                "kind": "missing_recorded_slew_envelope",
                "message": "sealed row has no host_slew_delta_limit_rad_s",
            }
            break
        lower, upper = embedded_slew_bounds(
            row["solver_qdot_lower_rad_s"], row["solver_qdot_upper_rad_s"], previous, dt_limit
        )
        if np.any(lower > upper):
            first_failure = {
                "packet_sequence": seq,
                "reference_time_s": float(row["reference_time_s"]),
                "kind": "empty_joint_slew_intersection",
                "empty_joint_indices": np.flatnonzero(lower > upper).astype(int).tolist(),
                "max_bound_inversion_rad_s": float(np.max(lower - upper)),
                "message": "joint-velocity and per-joint slew boxes do not intersect",
            }
            break

        before = solver.snapshot()
        jacobian = np.asarray(row["jacobian_6x6"], dtype=np.float64)
        twist = _finite_vector(row["requested_outer_twist_m_s_rad_s"], 6, "outer twist")
        try:
            solved = solver.solve(jacobian, twist, lower, upper)
        except QpError as exc:
            solver.restore(before)
            message = str(exc)
            code = _status_code(message)
            first_failure = {
                "packet_sequence": seq,
                "reference_time_s": float(row["reference_time_s"]),
                "kind": "qp_rejected_with_embedded_slew_bounds",
                "solver_status_code": code,
                "solver_status_interpretation": (
                    "primal_infeasible" if code == 3 else
                    "primal_infeasible_inaccurate" if code == 4 else
                    "other_solver_or_validation_rejection"
                ),
                "message": message,
                "solver_state_restored": solver.snapshot() == before,
                "independent_linear_feasibility_check": confirm_linear_feasibility(
                    jacobian, twist, lower, upper
                ),
            }
            break

        qdot = np.asarray(solved.qdot, dtype=np.float64)
        original = _finite_vector(row["published_packet_qdot_rad_s"], 6, "recorded published qdot")
        delta = qdot - previous
        allowed = float(dt_limit)
        results.append({
            "packet_sequence": seq,
            "reference_time_s": float(row["reference_time_s"]),
            "qdot_rad_s": qdot.tolist(),
            "qdot_inf_error_vs_recorded_publish_rad_s": float(np.max(np.abs(qdot - original))),
            "joint_slew_delta_inf_rad_s": float(np.max(np.abs(delta))),
            "recorded_delta_limit_rad_s": allowed,
            "slew_bound_utilization": 0.0 if allowed == 0.0 else float(np.max(np.abs(delta)) / allowed),
            "task_residual_inf_m_s": float(np.max(np.abs(jacobian @ qdot - twist))),
            "solver_equality_residual_inf_m_s": float(solved.equality_residual),
            "solver_elapsed_s": float(solved.elapsed_s),
        })
        previous = qdot

    qdot_errors = [item["qdot_inf_error_vs_recorded_publish_rad_s"] for item in results]
    residuals = [item["task_residual_inf_m_s"] for item in results]
    solve_times = [item["solver_elapsed_s"] for item in results]
    summary = {
        "schema": "tase.qp_embedded_slew_fixed_input_comparison/v1",
        "claim_class": "sequential_counterfactual_with_frozen_jacobian_twist_and_timing_not_closed_loop",
        "attempt_dir": metadata["attempt_dir"],
        "protocol_id": metadata["attempt_result"].get("parameter_binding", {}).get("protocol_id"),
        "method": "TASE_QP",
        "sealed_source_sha256": metadata["source_sha256"],
        "solver_profile": metadata["dispatch_solver_profile"],
        "solver_library_sha256": metadata["qp_library_sha256"],
        "slew_policy": "per-joint box intersection of existing joint-velocity bounds with previous candidate qdot +/- captured host slew delta; exact J*qdot=twist retained",
        "input_rows": len(rows),
        "solved_prefix_rows": len(results),
        "complete_counterfactual": first_failure is None and len(results) == len(rows),
        "first_failure": first_failure,
        "qdot_inf_error_vs_recorded_publish_rad_s": {
            "median": statistics.median(qdot_errors) if qdot_errors else None,
            "p95": _percentile(qdot_errors, 0.95),
            "max": max(qdot_errors) if qdot_errors else None,
        },
        "task_residual_inf_m_s": {
            "median": statistics.median(residuals) if residuals else None,
            "p95": _percentile(residuals, 0.95),
            "max": max(residuals) if residuals else None,
        },
        "solver_elapsed_s_offline_only": {
            "median": statistics.median(solve_times) if solve_times else None,
            "p95": _percentile(solve_times, 0.95),
            "max": max(solve_times) if solve_times else None,
        },
        "interpretation": [
            "This tests whether the exact equality task can be solved when the already-recorded per-tick slew envelope is included in the QP box bounds.",
            "It holds recorded Jacobian, requested twist and timing fixed, while recursively using this candidate's previous qdot; robot state and force feedback are not recomputed.",
            "An infeasible prefix is an offline diagnostic only; it is not a live attempt and does not change the failed attempt or its denominator.",
            "Offline solve times are not live deadline evidence.",
        ],
        "per_tick_results": None,
    }
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    rows_path = output_dir / "per-tick-results.jsonl"
    tmp = rows_path.with_name(rows_path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        for item in results:
            stream.write(json.dumps(item, sort_keys=True, allow_nan=False) + "\n")
    tmp.replace(rows_path)
    summary["per_tick_results"] = str(rows_path)
    summary_path = output_dir / "summary.json"
    temp_summary = summary_path.with_name(summary_path.name + ".tmp")
    temp_summary.write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    temp_summary.replace(summary_path)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--qp-library", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = compare(args.attempt_dir, args.qp_library, args.output_dir)
    print(json.dumps({
        "summary": str(args.output_dir.expanduser().resolve() / "summary.json"),
        "input_rows": result["input_rows"],
        "solved_prefix_rows": result["solved_prefix_rows"],
        "complete_counterfactual": result["complete_counterfactual"],
        "first_failure": result["first_failure"],
        "offline_only": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
