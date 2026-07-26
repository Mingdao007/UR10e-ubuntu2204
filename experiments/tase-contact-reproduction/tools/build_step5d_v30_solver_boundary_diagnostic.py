#!/usr/bin/env python3
"""Bind the RNN512 solver outliers to artificial 100-solve batch boundaries."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "config" / "step5d_v30_solver_batch_boundary_diagnostic.json"
INPUTS = (
    (
        "formal_attempt1",
        "config/step5d_v30_rnn512_d3089ac_formal_attempt1_raw.json",
        "b816b3c1d6b2dd5470d8864c6e3da988ea19e5bd788841b7aa8a356670528b2c",
    ),
    (
        "repeat1",
        "config/step5d_v30_rnn512_d3089ac_solver_repeat1_raw.json",
        "31bd1e26034341a57024ae231768b12bb04c78057970395c8ce083e3895d4159",
    ),
    (
        "repeat2",
        "config/step5d_v30_rnn512_d3089ac_solver_repeat2_raw.json",
        "8ae88e2b433d0d567f8ccdaae7cd71e0d9209ab302ad3dbca2355928cc637cbf",
    ),
    (
        "repeat3",
        "config/step5d_v30_rnn512_d3089ac_solver_repeat3_raw.json",
        "fd1266140b5ed5d7b5eae3a73914b0d1b64d6e6ceca935cc9d5edcb66dd58dfd",
    ),
)
ATTEMPT1_SUMMARY = (
    "config/step5d_v30_rnn512_d3089ac_formal_attempt1_summary.json",
    "ef7d1089ac16a5616a5c33db3a224ddc0ba41b4f63cb2fb273bee6fbda7826d1",
)
DEADLINE_MS = 2.0
BATCH_SIZE = 100


def sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def analyze_solver_samples(values: Sequence[float]) -> dict[str, Any]:
    if len(values) != 10_000:
        raise ValueError("solver diagnostic requires exactly 10,000 samples")
    normalized = [float(value) for value in values]
    if any(not math.isfinite(value) or value < 0.0 for value in normalized):
        raise ValueError("solver diagnostic contains invalid timing samples")
    outlier_indices = [
        index for index, value in enumerate(normalized) if value >= DEADLINE_MS
    ]
    boundary = [
        value for index, value in enumerate(normalized) if index % BATCH_SIZE == 0
    ]
    interior = [
        value for index, value in enumerate(normalized) if index % BATCH_SIZE != 0
    ]
    boundary_outliers = [
        index for index in outlier_indices if index % BATCH_SIZE == 0
    ]
    interior_outliers = [
        index for index in outlier_indices if index % BATCH_SIZE != 0
    ]
    return {
        "samples": len(normalized),
        "outlier_count": len(outlier_indices),
        "outlier_indices": outlier_indices,
        "batch_boundary_outlier_count": len(boundary_outliers),
        "batch_boundary_outlier_indices": boundary_outliers,
        "interior_outlier_count": len(interior_outliers),
        "interior_outlier_indices": interior_outliers,
        "batch_boundary_max_ms": max(boundary),
        "interior_max_ms": max(interior),
    }


def load_bound_input(label: str, relative: str, expected_sha: str) -> dict[str, Any]:
    path = ROOT / relative
    actual_sha = sha256_path(path)
    if actual_sha != expected_sha:
        raise ValueError(f"solver boundary input hash mismatch: {relative}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("profile") != {
        "backend": "cupy",
        "control_hz": 500.0,
        "epsilon": 0.01,
        "inner_iterations": 512,
        "qdot_cap_rad_s": 0.05,
        "sigr_exponent_r": 0.8,
    }:
        raise ValueError(f"solver boundary input profile mismatch: {relative}")
    runtime = payload.get("runtime_environment") or {}
    if (
        runtime.get("scheduler_policy_name") != "SCHED_FIFO"
        or runtime.get("scheduler_priority") != 20
    ):
        raise ValueError(f"solver boundary input scheduler mismatch: {relative}")
    analysis = analyze_solver_samples(payload.get("solver_ms") or [])
    return {
        "label": label,
        "path": relative,
        "sha256": actual_sha,
        "source_binding": payload.get("source_binding"),
        "artifact_binding": payload.get("artifact_binding"),
        "first_post_warm_ms": payload.get("first_post_warm_ms"),
        "solver_summary": payload.get("solver"),
        "full_tick_summary": payload.get("full_tick"),
        "full_tick_reason_counts": payload.get("full_tick_reason_counts"),
        **analysis,
    }


def build() -> dict[str, Any]:
    runs = [load_bound_input(*item) for item in INPUTS]
    source_bindings = {
        json.dumps(run["source_binding"], sort_keys=True, separators=(",", ":"))
        for run in runs
    }
    artifact_bindings = {
        json.dumps(run["artifact_binding"], sort_keys=True, separators=(",", ":"))
        for run in runs
    }
    if len(source_bindings) != 1 or len(artifact_bindings) != 1:
        raise ValueError("solver boundary runs do not share source/artifact bindings")
    total_outliers = sum(run["outlier_count"] for run in runs)
    boundary_outliers = sum(run["batch_boundary_outlier_count"] for run in runs)
    interior_outliers = sum(run["interior_outlier_count"] for run in runs)
    summary_path = ROOT / ATTEMPT1_SUMMARY[0]
    if sha256_path(summary_path) != ATTEMPT1_SUMMARY[1]:
        raise ValueError("formal attempt-1 summary hash mismatch")
    return {
        "schema_version": "step5d_v30_solver_batch_boundary_diagnostic_v1",
        "classification": "artificial_batch_boundary_tail_identified_not_acceptance",
        "profile": {
            "backend": "cupy",
            "inner_iterations": 512,
            "epsilon": 0.01,
            "sigr_exponent_r": 0.8,
            "qdot_cap_rad_s": 0.05,
            "control_hz": 500.0,
        },
        "scheduler": {"policy": "SCHED_FIFO", "priority": 20},
        "old_harness_batch_policy": {
            "batch_size": BATCH_SIZE,
            "unmeasured_yield_s": 0.002,
            "problem": "first solve at each artificial batch boundary was mixed into the 10k steady-solve distribution",
        },
        "runs": runs,
        "aggregate": {
            "solver_samples": sum(run["samples"] for run in runs),
            "deadline_outliers": total_outliers,
            "batch_boundary_outliers": boundary_outliers,
            "interior_outliers": interior_outliers,
            "all_outliers_at_batch_boundaries": bool(
                total_outliers > 0
                and boundary_outliers == total_outliers
                and interior_outliers == 0
            ),
            "max_interior_ms": max(run["interior_max_ms"] for run in runs),
            "max_batch_boundary_ms": max(
                run["batch_boundary_max_ms"] for run in runs
            ),
        },
        "formal_attempt1_summary": {
            "path": ATTEMPT1_SUMMARY[0],
            "sha256": ATTEMPT1_SUMMARY[1],
            "overall_pass": False,
            "full_tick_30000_execute": True,
            "full_tick_compute_deadline_misses": 0,
        },
        "required_followup": {
            "separate_reentry_samples_from_10k_steady_samples": True,
            "retain_every_reentry_outlier": True,
            "rerun_full_10k_plus_60s_plus_60s_formal": True,
            "status": "pending",
        },
        "claim_boundary": {
            "formal_timing_passed": False,
            "v30_offline_ready": False,
            "live_motion_authorized": False,
            "reproduction_complete": False,
        },
        "safety_boundary": [
            "no outlier discarded",
            "no deadline threshold changed",
            "full-tick and safe-hold loops unchanged",
            "offline evidence only",
            "no controller connection",
            "no robot motion",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.write_text(json.dumps(build(), indent=2, sort_keys=True) + "\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
