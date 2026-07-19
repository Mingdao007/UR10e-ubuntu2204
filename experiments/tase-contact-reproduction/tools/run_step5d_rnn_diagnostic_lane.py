#!/usr/bin/env python3
"""Run short Step5d RNN diagnostics in one persistent CuPy process."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import run_step5d_p0_v8_mujoco as p0
import sweep_step5d_p0_rnn_profiles as sweep
from ur10e_mujoco_adapter import MuJoCoVelocityPlant


def write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--startup-duration-s", type=float, required=True)
    parser.add_argument("--steady-duration-s", type=float, required=True)
    args = parser.parse_args()
    decision_digest = os.environ.get("UR10E_USER_DECISION_DIGEST", "")
    if len(decision_digest) != 64:
        raise RuntimeError("persistent RNN lane requires the shared user-decision digest")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    process_started = time.monotonic()
    plant = MuJoCoVelocityPlant(args.model_manifest.resolve())
    profile = sweep.RnnProfile(**sweep.BASELINE)
    solver = sweep.make_solver(profile)
    if getattr(solver, "_cp", None) is None:
        raise RuntimeError("persistent RNN diagnostic lane requires CuPy; CPU fallback is forbidden")
    initialized_s = time.monotonic() - process_started

    startup = p0.run_nominal_phase(
        plant=plant, solver=solver,
        spec=p0.PhaseSpec(duration_s=args.startup_duration_s, sequence_index=0),
        pace_wall_clock=False,
    )
    startup_summary = sweep.summarize(profile, startup, startup, paced=False)
    write(args.output_dir / "startup.json", startup_summary)

    steady = p0.run_nominal_phase(
        plant=plant, solver=solver,
        spec=p0.PhaseSpec(duration_s=args.steady_duration_s, sequence_index=1),
        pace_wall_clock=False,
    )
    steady_summary = sweep.summarize(profile, steady, steady, paced=False)
    write(args.output_dir / "steady.json", steady_summary)

    fault_rows = p0.run_fault_matrix(plant=plant, solver=solver)
    fault_payload = {
        "schema_version": "step5d_rnn_fault_stale_diagnostic_v1",
        "claim_class": "diagnostic_only",
        "rows": fault_rows,
    }
    write(args.output_dir / "fault_stale.json", fault_payload)
    timing = {
        "schema_version": "step5d_persistent_rnn_short_timing_v1",
        "claim_class": "diagnostic_only",
        "gpu_backend": "cupy",
        "cpu_fallback_allowed": False,
        "process_initialization_s": initialized_s,
        "startup": startup_summary["wall_timing"],
        "steady": steady_summary["wall_timing"],
    }
    write(args.output_dir / "timing.json", timing)
    aggregate = {
        "schema_version": "step5d_persistent_rnn_diagnostic_lane_v1",
        "claim_class": "diagnostic_only",
        "single_process": True,
        "single_cupy_solver_instance": True,
        "single_cupy_initialization_and_kernel_compile_phase": True,
        "solver_instance_reused_across_cases": True,
        "user_decision_digest": decision_digest,
        "sequence": ["startup", "steady", "fault_stale", "timing"],
        "feature_windows_s": {
            "startup": args.startup_duration_s,
            "steady": args.steady_duration_s,
        },
        "artifacts": ["startup.json", "steady.json", "fault_stale.json", "timing.json"],
    }
    write(args.output_dir / "aggregate.json", aggregate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
