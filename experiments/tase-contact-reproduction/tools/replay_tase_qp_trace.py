#!/usr/bin/env python3
"""Run an offline strict equality-QP comparison on a sealed TASE-RNN trace.

This freezes the measured RNN outer-loop inputs. It is not closed-loop QP
control and never opens a robot, sensor, RTDE, or video endpoint.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

import numpy as np

from audit_tase_qp_replay_inputs import inspect_attempt
from contact_qp import (
    NativeContactQp,
    QP_BOUND_VALIDATION_TOLERANCE,
    QP_EQUALITY_VALIDATION_TOLERANCE,
    QpError,
)


def _read_rows(path: Path):
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            yield json.loads(line)


def _apply_captured_slew(row: dict[str, Any], qdot: Any) -> tuple[list[float], float]:
    """Apply the same recorded scalar qdot-delta cap to an offline QP result."""

    requested = np.asarray(qdot, dtype=float)
    previous = np.asarray(row["previous_published_qdot_rad_s"], dtype=float)
    if requested.shape != (6,) or previous.shape != (6,) \
            or not np.isfinite(requested).all() or not np.isfinite(previous).all():
        raise ValueError("offline QP slew inputs must be finite six-vectors")
    limit_value = row.get("host_slew_delta_limit_rad_s")
    if limit_value is None:
        return requested.tolist(), 1.0
    limit = float(limit_value)
    if not math.isfinite(limit) or limit < 0.0:
        raise ValueError("captured host slew delta limit is invalid")
    delta = requested - previous
    magnitude = float(np.max(np.abs(delta)))
    scale = 1.0 if magnitude <= limit or magnitude == 0.0 else limit / magnitude
    return (previous + scale * delta).tolist(), float(scale)


def _solve_record(row: dict[str, Any], solver: Any) -> dict[str, Any]:
    jacobian = np.asarray(row["jacobian_6x6"], dtype=float)
    twist = np.asarray(row["requested_outer_twist_m_s_rad_s"], dtype=float)
    lower = np.asarray(row["solver_qdot_lower_rad_s"], dtype=float)
    upper = np.asarray(row["solver_qdot_upper_rad_s"], dtype=float)
    try:
        result = solver.solve(jacobian, twist, lower, upper)
    except QpError as exc:
        return {
            "packet_sequence": int(row["packet_sequence"]),
            "reference_time_s": float(row["reference_time_s"]),
            "status": "rejected",
            "error": str(exc),
            "qdot_rad_s": None,
        }
    qdot = np.asarray(result.qdot, dtype=float)
    applied, slew_scale = _apply_captured_slew(row, qdot)
    applied_array = np.asarray(applied, dtype=float)
    slew_lower = np.asarray(row["slew_adjusted_qdot_lower_rad_s"], dtype=float)
    slew_upper = np.asarray(row["slew_adjusted_qdot_upper_rad_s"], dtype=float)
    return {
        "packet_sequence": int(row["packet_sequence"]),
        "reference_time_s": float(row["reference_time_s"]),
        "status": "solved",
        "qdot_rad_s": list(result.qdot),
        "qdot_after_captured_slew_rad_s": applied,
        "captured_slew_scale": slew_scale,
        "iterations": int(result.iterations),
        "equality_residual_inf": float(result.equality_residual),
        "bound_violation": float(result.bound_violation),
        "task_residual_after_slew_inf": float(np.max(np.abs(jacobian @ applied_array - twist))),
        "captured_slew_bound_violation": float(max(
            0.0,
            np.max(slew_lower - applied_array),
            np.max(applied_array - slew_upper),
        )),
        "qdot_difference_from_published_rnn_inf": float(np.max(np.abs(
            applied_array - np.asarray(row["published_packet_qdot_rad_s"], dtype=float)
        ))),
        "primal_residual": float(result.primal_residual),
        "dual_residual": float(result.dual_residual),
        "elapsed_s": float(result.elapsed_s),
    }


def run_independent_rows(rows: list[dict[str, Any]], solver: Any) -> list[dict[str, Any]]:
    """Solve every frozen input from a zero QP warm state."""

    output = []
    for row in rows:
        solver.reset()
        output.append(_solve_record(row, solver))
    return output


def run_sequential_prefix(rows: list[dict[str, Any]], solver: Any) -> dict[str, Any]:
    """Replay the frozen inputs with one warm state, stopping at first rejection."""

    solver.reset()
    accepted = 0
    first_failure = None
    for row in rows:
        before = solver.snapshot()
        result = _solve_record(row, solver)
        if result["status"] != "solved":
            after = solver.snapshot()
            rollback_ok = after == before
            if not rollback_ok:
                solver.restore(before)
            first_failure = {
                "packet_sequence": result["packet_sequence"],
                "reference_time_s": result["reference_time_s"],
                "error": result["error"],
                "solver_state_unchanged_on_rejection": rollback_ok,
            }
            break
        accepted += 1
    return {
        "accepted_prefix_rows": accepted,
        "first_failure": first_failure,
        "complete_frozen_sequence": first_failure is None and accepted == len(rows),
        "closed_loop": False,
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    solved = [row for row in rows if row["status"] == "solved"]
    rejected = [row for row in rows if row["status"] != "solved"]
    timings = np.asarray([row["elapsed_s"] for row in solved], dtype=float)
    equality = [row["equality_residual_inf"] for row in solved]
    post_slew = [row["task_residual_after_slew_inf"] for row in solved]
    slew_violation = [row["captured_slew_bound_violation"] for row in solved]
    reasons = Counter(row["error"] for row in rejected)
    return {
        "input_rows": len(rows),
        "solved_rows": len(solved),
        "rejected_rows": len(rejected),
        "rejection_reasons": dict(reasons),
        "equality_residual_inf_max": max(equality, default=None),
        "task_residual_after_slew_inf_max": max(post_slew, default=None),
        "captured_slew_bound_violation_max": max(slew_violation, default=None),
        "qdot_difference_from_published_rnn_inf_median": (
            float(np.median([row["qdot_difference_from_published_rnn_inf"] for row in solved]))
            if solved else None
        ),
        "solver_time_ms": {
            "median": float(np.median(timings) * 1000.0) if len(timings) else None,
            "p95": float(np.percentile(timings, 95) * 1000.0) if len(timings) else None,
            "p99": float(np.percentile(timings, 99) * 1000.0) if len(timings) else None,
            "max": float(np.max(timings) * 1000.0) if len(timings) else None,
        },
    }


def replay_attempt(
    attempt_dir: Path,
    qp_library: Path,
    output_dir: Path,
    *,
    solver_factory: Callable[..., Any] = NativeContactQp,
) -> dict[str, Any]:
    audit = inspect_attempt(attempt_dir)
    if audit.get("replay_status") != "ready_for_offline_qp_solver_comparison":
        raise ValueError("sealed TASE-RNN trace is not complete for offline QP replay")
    rows = list(_read_rows(Path(attempt_dir) / "command_timeline.jsonl"))
    lib = Path(qp_library).resolve(strict=True)
    build_path = lib.parent / "build.json"
    build = json.loads(build_path.read_text(encoding="utf-8")) if build_path.exists() else None
    independent_solver = solver_factory(lib, deadline_s=None)
    sequential_solver = solver_factory(lib, deadline_s=None)
    independent = run_independent_rows(rows, independent_solver)
    sequential = run_sequential_prefix(rows, sequential_solver)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows_path = output_dir / "per-tick-qp-results.jsonl"
    with rows_path.open("w", encoding="utf-8") as target:
        for row in independent:
            target.write(json.dumps(row, allow_nan=False, separators=(",", ":")) + "\n")
    summary = {
        "schema": "tase.offline-qp-replay-result-v1",
        "offline_only": True,
        "live_eligible": False,
        "closed_loop": False,
        "claim_scope": "strict_equality_qp_on_frozen_tase_rnn_outer_inputs",
        "attempt_dir": str(Path(attempt_dir).resolve()),
        "input_audit": audit,
        "qp_library": {
            "path": str(lib),
            "sha256": hashlib.sha256(lib.read_bytes()).hexdigest(),
            "build": build,
        },
        "solver_profile": {
            "backend": "osqp-codegen-c",
            "deadline_s": None,
            "deadline_note": "disabled only for offline replay; this is not a live timing qualification",
            "post_solve_equality_validation_tolerance": QP_EQUALITY_VALIDATION_TOLERANCE,
            "post_solve_bound_validation_tolerance": QP_BOUND_VALIDATION_TOLERANCE,
            "bounds_source": "captured per-tick RNN solver lower/upper vectors",
            "slew_source": "captured host slew delta limit and previous published qdot",
        },
        "independent_per_tick": _summary(independent),
        "sequential_warm_state_prefix": sequential,
        "per_tick_results_path": str(rows_path),
        "interpretation": [
            "Per-tick solves reset QP warm state and evaluate frozen RNN outer inputs independently.",
            "The sequential prefix uses QP warm state but does not feed QP motion back into robot or outer-loop state.",
            "Post-slew task residual is reported separately because the shared host slew layer can reduce exact task realization.",
            "No force MAE, closed-loop path quality, live eligibility, or robot safety conclusion is computed.",
        ],
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--qp-library", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = replay_attempt(args.attempt_dir, args.qp_library, args.output_dir)
    print(json.dumps({
        "summary": str(args.output_dir / "summary.json"),
        "per_tick_results": str(args.output_dir / "per-tick-qp-results.jsonl"),
        "independent_per_tick": summary["independent_per_tick"],
        "sequential_warm_state_prefix": summary["sequential_warm_state_prefix"],
        "offline_only": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
