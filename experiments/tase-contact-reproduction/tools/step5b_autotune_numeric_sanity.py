#!/usr/bin/env python3
"""Deterministic numeric sanity gate for the Step5b autotune experiment."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from step5_table import step5_path_reference
from step5b_autotune_contract import (
    CONTROLLER_PROGRAM,
    EXPERIMENT_ROOT,
    TARGET_CONTEXTS_N,
    Candidate,
    bridge_command,
    one_step_neighbors,
)


DEFAULT_OUTPUT = EXPERIMENT_ROOT / "config" / "step5b_autotune_numeric_sanity.json"


def run_sanity() -> dict:
    times = np.linspace(0.0, 60.0, 30001)
    references = [
        step5_path_reference("step5_contact_cycloid_baseline_v1", (0.0, 0.0), float(t))
        for t in times
    ]
    xy = np.array([[float(value) for value in item["desired_xy"]] for item in references])
    velocity = np.array([[float(value) for value in item["desired_velocity_xy"]] for item in references])
    baseline = Candidate(target_force_n=12.0)
    command = bridge_command(baseline, Path("/tmp/step5b_autotune_sanity"), python_executable="python3")
    neighbors = one_step_neighbors(baseline, tier2_unlocked=False)
    checks = {
        "duration_is_60s": math.isclose(times[-1], 60.0),
        "path_span_x_le_100mm": float(np.ptp(xy[:, 0])) <= 0.100,
        "path_span_y_le_100mm": float(np.ptp(xy[:, 1])) <= 0.100,
        "reference_speed_le_3mm_s": float(np.max(np.linalg.norm(velocity, axis=1))) <= 0.0030001,
        "fixed_total_linear_cap_4mm_s": command[command.index("--step4e-total-linear-limit-m-s") + 1] == "0.004",
        "fixed_normal_cap_10mm_s": command[command.index("--step4e-normal-velocity-limit-m-s") + 1] == "0.01",
        "fixed_angular_cap_0p15rad_s": command[command.index("--step4e-angular-limit-rad-s") + 1] == "0.150",
        "fixed_force_guards": all(token in command for token in ("50", "60", "3.0")),
        "single_12n_target_context": TARGET_CONTEXTS_N == (12.0,),
        "tier1_trust_region_is_local": len(neighbors) == 5,
    }
    return {
        "schema_version": "step5b_autotune_numeric_sanity_v3",
        "pass": all(checks.values()),
        "checks": checks,
        "metrics": {
            "path_span_x_m": float(np.ptp(xy[:, 0])),
            "path_span_y_m": float(np.ptp(xy[:, 1])),
            "max_reference_speed_m_s": float(np.max(np.linalg.norm(velocity, axis=1))),
            "duration_s": float(times[-1]),
            "tier1_neighbors_including_incumbent": len(neighbors),
        },
        "controller_target": CONTROLLER_PROGRAM,
        "guards": {"raw_normal_n": 50.0, "force_norm_n": 60.0, "torque_norm_nm": 3.0},
        "attitude_guard": {"angular_command_cap_rad_s": 0.15, "home_orientation_tolerance_rad": 0.05},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_sanity()
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["pass"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
