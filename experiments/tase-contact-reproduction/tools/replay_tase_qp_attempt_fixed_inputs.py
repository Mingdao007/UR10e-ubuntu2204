#!/usr/bin/env python3
"""Replay one sealed live TASE-QP attempt on its exact frozen inputs offline."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
from typing import Any, Sequence

import numpy as np

from analyze_tase_r31_cartesian_tracking import verify_sealed_sources
from contact_qp import NativeContactQp


class ReplayError(ValueError):
    """The live receipt or frozen QP inputs are not eligible for this replay."""


def _finite_vector(value: Any, size: int, label: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (size,) or not np.isfinite(array).all():
        raise ReplayError(f"{label} must be a finite {size}-vector")
    return array


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def apply_captured_slew(
    qdot: Sequence[float], previous_qdot: Sequence[float], delta_limit_rad_s: float | None
) -> tuple[np.ndarray, float]:
    requested = _finite_vector(qdot, 6, "solver qdot")
    previous = _finite_vector(previous_qdot, 6, "previous published qdot")
    if delta_limit_rad_s is None:
        return requested.copy(), 1.0
    limit = float(delta_limit_rad_s)
    if not math.isfinite(limit) or limit < 0.0:
        raise ReplayError("captured host slew limit is invalid")
    delta = requested - previous
    magnitude = float(np.max(np.abs(delta)))
    scale = 1.0 if magnitude == 0.0 or magnitude <= limit else limit / magnitude
    return previous + scale * delta, scale


def _load_inputs(attempt_dir: Path, library: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    attempt_dir = attempt_dir.expanduser().resolve(strict=True)
    if attempt_dir.name != "0001" or attempt_dir.parent.name != "attempts":
        raise ReplayError("select exactly one sealed attempt directory")
    hashes = verify_sealed_sources(attempt_dir)
    attempt = json.loads((attempt_dir / "attempt-result.json").read_text(encoding="utf-8"))
    binding = attempt.get("parameter_binding", {})
    state = attempt.get("state", {})
    last_result = state.get("last_result", {}) if isinstance(state, dict) else {}
    if last_result.get("implementation") != "standalone_tase_outer_loop_osqp_qp":
        raise ReplayError("selected attempt is not the live standalone TASE-QP method")
    if binding.get("protocol_id") != "figure8_window60_r013_rate460_v1":
        raise ReplayError("selected attempt is not the frozen R31 rate460 protocol")
    if attempt.get("evidence_eligible") is not False:
        raise ReplayError("replay is restricted to a failed QP diagnostic attempt")

    run_dir = attempt_dir.parent.parent
    dispatch = json.loads((run_dir / "dispatch_receipt.json").read_text(encoding="utf-8"))
    profile = dispatch.get("controller_identity", {}).get("solver_profile", {})
    library = library.expanduser().resolve(strict=True)
    library_sha = hashlib.sha256(library.read_bytes()).hexdigest()
    if profile.get("backend") != "osqp-codegen-c":
        raise ReplayError("dispatch receipt is not bound to the OSQP codegen-C backend")
    if Path(profile.get("library", "")).resolve() != library:
        raise ReplayError("selected QP library path differs from the dispatch receipt")
    if profile.get("library_sha256") != library_sha:
        raise ReplayError("selected QP library SHA differs from the dispatch receipt")
    if profile.get("qdot_limit_rad_s") != 0.05 or profile.get("deadline_s") != 0.001:
        raise ReplayError("dispatch receipt does not contain the frozen matched-A solver profile")

    rows: list[dict[str, Any]] = []
    previous_sequence = -1
    previous_reference = -math.inf
    with (attempt_dir / "command_timeline.jsonl").open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("reference_phase") != "path":
                continue
            sequence = int(row.get("packet_sequence", -1))
            reference = float(row.get("reference_time_s", math.nan))
            if sequence <= previous_sequence or reference < previous_reference \
                    or not (0.0 <= reference < 60.0):
                raise ReplayError(f"PATH sequence/time regressed at line {line_number}")
            for key, width in (
                ("jacobian_6x6", None),
                ("requested_outer_twist_m_s_rad_s", 6),
                ("solver_qdot_lower_rad_s", 6),
                ("solver_qdot_upper_rad_s", 6),
                ("previous_published_qdot_rad_s", 6),
                ("published_packet_qdot_rad_s", 6),
            ):
                if key == "jacobian_6x6":
                    matrix = np.asarray(row.get(key), dtype=np.float64)
                    if matrix.shape != (6, 6) or not np.isfinite(matrix).all():
                        raise ReplayError(f"invalid {key} at line {line_number}")
                else:
                    _finite_vector(row.get(key), width, key)
            rows.append(row)
            previous_sequence, previous_reference = sequence, reference
    if len(rows) < 1000:
        raise ReplayError("sealed QP PATH trace is unexpectedly short")
    return {
        "attempt_dir": str(attempt_dir),
        "attempt_result": attempt,
        "source_sha256": hashes,
        "dispatch_solver_profile": profile,
        "qp_library_sha256": library_sha,
        "rows": rows,
    }, rows


def replay(attempt_dir: Path, library: Path, output_dir: Path) -> dict[str, Any]:
    metadata, rows = _load_inputs(attempt_dir, library)
    solver = NativeContactQp(Path(library).resolve(strict=True), deadline_s=None)
    solver.reset()
    results: list[dict[str, Any]] = []
    failure: dict[str, Any] | None = None
    for row in rows:
        before = solver.snapshot()
        sequence = int(row["packet_sequence"])
        jacobian = np.asarray(row["jacobian_6x6"], dtype=np.float64)
        twist = _finite_vector(row["requested_outer_twist_m_s_rad_s"], 6, "outer twist")
        lower = _finite_vector(row["solver_qdot_lower_rad_s"], 6, "solver lower")
        upper = _finite_vector(row["solver_qdot_upper_rad_s"], 6, "solver upper")
        try:
            solved = solver.solve(jacobian, twist, lower, upper)
            post_slew, replay_scale = apply_captured_slew(
                solved.qdot,
                row["previous_published_qdot_rad_s"],
                row.get("host_slew_delta_limit_rad_s"),
            )
        except Exception as exc:
            solver.restore(before)
            failure = {
                "packet_sequence": sequence,
                "reference_time_s": row["reference_time_s"],
                "error": f"{type(exc).__name__}: {exc}",
                "solver_state_restored": solver.snapshot() == before,
            }
            break
        published = _finite_vector(row["published_packet_qdot_rad_s"], 6, "published qdot")
        applied_twist = jacobian @ post_slew
        results.append({
            "packet_sequence": sequence,
            "reference_time_s": float(row["reference_time_s"]),
            "solver_qdot_rad_s": list(solved.qdot),
            "replayed_after_captured_slew_qdot_rad_s": post_slew.tolist(),
            "replayed_slew_scale": replay_scale,
            "recorded_slew_scale": float(row.get("host_slew_scale", 1.0)),
            "published_qdot_rad_s": published.tolist(),
            "qdot_inf_error_vs_published": float(np.max(np.abs(post_slew - published))),
            "solver_equality_residual_inf": float(solved.equality_residual),
            "post_slew_task_residual_inf": float(np.max(np.abs(applied_twist - twist))),
            "recorded_host_slew_limited": row.get("host_slew_limited") is True,
        })

    diffs = [row["qdot_inf_error_vs_published"] for row in results]
    slew_diffs = [abs(row["replayed_slew_scale"] - row["recorded_slew_scale"]) for row in results]
    residuals = [row["post_slew_task_residual_inf"] for row in results]
    flagged = [row for row in results if row["recorded_host_slew_limited"]]
    recorded_slew_count = sum(row.get("host_slew_limited") is True for row in rows)
    replay_slew_count = sum(row["replayed_slew_scale"] < 1.0 - 1e-12 for row in results)
    summary = {
        "schema": "tase.live_qp_fixed_input_replay/v1",
        "claim_class": "offline_frozen_input_solver_and_post_slew_replay_not_closed_loop",
        "attempt_dir": metadata["attempt_dir"],
        "protocol_id": metadata["attempt_result"].get("parameter_binding", {}).get("protocol_id"),
        "method": "TASE_QP",
        "sealed_source_sha256": metadata["source_sha256"],
        "solver_profile": metadata["dispatch_solver_profile"],
        "solver_library_sha256": metadata["qp_library_sha256"],
        "offline_deadline_s": None,
        "closed_loop": False,
        "attempt_evidence_eligible": False,
        "official_qd_gate_value": metadata["attempt_result"].get("evidence", {}).get("qd_correlation"),
        "input_rows": len(rows),
        "replayed_rows": len(results),
        "first_failure": failure,
        "complete_sequential_replay": failure is None and len(results) == len(rows),
        "replay_slew_limited_rows": replay_slew_count,
        "recorded_slew_limited_rows": recorded_slew_count,
        "max_replay_vs_recorded_slew_scale_error": max(slew_diffs, default=None),
        "qdot_inf_error_vs_published": {
            "median": statistics.median(diffs) if diffs else None,
            "p95": _percentile(diffs, 0.95),
            "max": max(diffs) if diffs else None,
        },
        "solver_equality_residual_inf_max": max(
            (row["solver_equality_residual_inf"] for row in results), default=None
        ),
        "post_slew_task_residual_inf": {
            "all_max": max(residuals, default=None),
            "slew_limited_count": len(flagged),
            "slew_limited_p95": _percentile(
                [row["post_slew_task_residual_inf"] for row in flagged], 0.95
            ),
            "slew_limited_max": max(
                (row["post_slew_task_residual_inf"] for row in flagged), default=None
            ),
        },
        "interpretation": [
            "Replays the recorded QP Jacobian, twist, solver bounds, and post-solve slew bound using the exact library SHA in the live dispatch receipt.",
            "Replaying the host slew after the QP solve is a frozen-input command reconstruction; it is not a counterfactual closed-loop path or force score.",
            "The recorded actual_qd gate failure remains unchanged; no result here promotes the attempt or changes its denominator.",
        ],
        "per_tick_results": None,
    }
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    rows_path = output_dir / "per-tick-results.jsonl"
    temporary_rows = rows_path.with_name(rows_path.name + ".tmp")
    with temporary_rows.open("w", encoding="utf-8") as stream:
        for row in results:
            stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
    os.replace(temporary_rows, rows_path)
    summary["per_tick_results"] = str(rows_path)
    summary_path = output_dir / "summary.json"
    temporary_summary = summary_path.with_name(summary_path.name + ".tmp")
    temporary_summary.write_text(
        json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_summary, summary_path)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, required=True)
    parser.add_argument("--qp-library", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = replay(args.attempt_dir, args.qp_library, args.output_dir)
    print(json.dumps({
        "summary": str(args.output_dir.expanduser().resolve() / "summary.json"),
        "input_rows": result["input_rows"],
        "replayed_rows": result["replayed_rows"],
        "complete_sequential_replay": result["complete_sequential_replay"],
        "replay_slew_limited_rows": result["replay_slew_limited_rows"],
        "qdot_inf_error_vs_published": result["qdot_inf_error_vs_published"],
        "offline_only": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
