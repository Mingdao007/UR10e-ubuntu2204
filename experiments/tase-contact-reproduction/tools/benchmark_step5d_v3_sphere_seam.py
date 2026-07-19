#!/usr/bin/env python3
"""Paced source-exact timing probe for the Step5d V3 sphere bridge seam."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
import statistics
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SOURCE = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
if str(RUNTIME_SOURCE) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SOURCE))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import kunwei_rtde_bridge as bridge  # noqa: E402
from ur10e_experiment_runtime.moving_sphere import (  # noqa: E402
    MovingSphereKernel,
    SphereReason,
    StoppingBoundArtifact,
)
from ur10e_experiment_runtime.physical_prior import (  # noqa: E402
    STEP5D_V3_PHYSICAL_PRIOR,
)
from ur10e_experiment_runtime.stage_adapters import (  # noqa: E402
    Stage25ControllerProgressAdapter,
    frozen_step5d_path_reference,
)


CONTROL_HZ = 500.0
PERIOD_NS = 2_000_000
SOURCE_FILES = (
    ROOT.parents[1]
    / "src/ur10e_experiment_runtime/ur10e_experiment_runtime/moving_sphere.py",
    ROOT.parents[1]
    / "src/ur10e_experiment_runtime/ur10e_experiment_runtime/stage_adapters.py",
    ROOT / "tools/kunwei_rtde_bridge.py",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _fixture_bound() -> StoppingBoundArtifact:
    return StoppingBoundArtifact(
        reaction_latency_s=0.002,
        acceleration_growth_m_s2=0.1,
        minimum_deceleration_m_s2=2.0,
        center_speed_bound_m_s=0.002,
        center_acceleration_bound_m_s2=0.001,
        numeric_margin_m=0.0001,
        evidence_sha256=("b" * 64,),
        validity_domain="offline_timing_fixture_only_not_live_certification",
        certified=True,
    )


def run(*, samples: int, paced: bool) -> dict[str, Any]:
    if samples < 1:
        raise ValueError("samples must be positive")
    adapter = Stage25ControllerProgressAdapter(
        physical_prior_sha256=STEP5D_V3_PHYSICAL_PRIOR.fingerprint
    )
    kernel = MovingSphereKernel(
        reference_sha256=adapter.reference_sha256,
        stopping_bound=_fixture_bound(),
    )
    args = SimpleNamespace(
        step5d_controller_progress_adapter=adapter,
        step5d_moving_sphere_kernel=kernel,
        step5d_moving_sphere_progress_age_ns=0,
    )
    inputs = []
    for index in range(samples):
        progress_s = min(index / CONTROL_HZ, 60.0)
        reference = frozen_step5d_path_reference((0.0, 0.0), progress_s)
        desired = reference["desired_xy"]
        inputs.append((progress_s, (desired[0], desired[1], 0.008, 0.0, 0.0, 0.0)))
    values = bridge.bridge_zero_values()
    values.update(
        {
            "stop_request": 0.0,
            "_step5d_moving_sphere_reason": SphereReason.SPHERE_OK.name,
            "_step5d_moving_sphere_actual_distance_m": 0.0,
            "_step5d_moving_sphere_predicted_bound_m": 0.0,
            "_step5d_contact_safety_reason": "",
        }
    )
    latest_output = {"output_double_register_31": 0.0, "timestamp": 1.0}
    compute_ms: list[float] = []
    release_lateness_ms: list[float] = []
    absolute_deadline_misses = 0
    compute_deadline_misses = 0
    stopped = 0
    start_ns = time.perf_counter_ns()
    for index, (progress_s, pose) in enumerate(inputs):
        release_ns = start_ns + index * PERIOD_NS
        deadline_ns = release_ns + PERIOD_NS
        if paced:
            remaining_ns = release_ns - time.perf_counter_ns()
            if remaining_ns > 0:
                time.sleep(remaining_ns / 1_000_000_000.0)
        actual_release_ns = time.perf_counter_ns()
        latest_output["output_double_register_31"] = progress_s
        latest_output["timestamp"] = 1.0 + index / CONTROL_HZ
        tick_start_ns = time.perf_counter_ns()
        bridge.apply_step5d_moving_sphere_guard(
            values=values,
            args=args,
            latest_output=latest_output,
            robot_stage=25.0,
            pose=pose,
            tcp_speed_m_s=0.0,
        )
        finish_ns = time.perf_counter_ns()
        elapsed_ms = (finish_ns - tick_start_ns) / 1_000_000.0
        compute_ms.append(elapsed_ms)
        release_lateness_ms.append(max(0, actual_release_ns - release_ns) / 1_000_000.0)
        compute_deadline_misses += int(elapsed_ms > 2.0)
        absolute_deadline_misses += int(paced and finish_ns > deadline_ns)
        stopped += int(values["stop_request"] != 0.0)
    elapsed_s = (time.perf_counter_ns() - start_ns) / 1_000_000_000.0
    compute = {
        "samples": samples,
        "mean_ms": statistics.fmean(compute_ms),
        "p95_ms": percentile(compute_ms, 0.95),
        "p99_ms": percentile(compute_ms, 0.99),
        "max_ms": max(compute_ms),
        "deadline_miss_count": compute_deadline_misses,
    }
    schedule = {
        "paced": paced,
        "elapsed_s": elapsed_s,
        "release_lateness_p99_ms": percentile(release_lateness_ms, 0.99),
        "release_lateness_max_ms": max(release_lateness_ms),
        "absolute_deadline_miss_count": absolute_deadline_misses,
    }
    passed = bool(
        stopped == 0
        and compute_deadline_misses == 0
        and (not paced or absolute_deadline_misses == 0)
    )
    return {
        "schema": "step5d.autotune-v3/source-exact-sphere-seam-timing-v1",
        "control_hz": CONTROL_HZ,
        "period_ms": 2.0,
        "source_sha256": {
            str(path.relative_to(ROOT.parents[1])): sha256(path)
            for path in SOURCE_FILES
        },
        "runtime": {
            "python": sys.version.split()[0],
            "scheduler_policy": os.sched_getscheduler(0),
            "scheduler_priority": os.sched_getparam(0).sched_priority,
            "cpu_affinity": sorted(os.sched_getaffinity(0)),
        },
        "compute": compute,
        "schedule": schedule,
        "sphere_stop_count": stopped,
        "pass": passed,
        "claim_boundary": (
            "source-exact adapter+kernel+bridge-seam timing with an offline-only "
            "fixture bound; not a certified stopping bound, full control-loop timing, "
            "controller connection, bridge start, or motion authorization"
        ),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--samples", type=int, default=30_000)
    result.add_argument("--unpaced", action="store_true")
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    report = run(samples=args.samples, paced=not args.unpaced)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    else:
        print(
            f"sphere_seam_timing_pass={str(report['pass']).lower()} "
            f"samples={report['compute']['samples']} "
            f"p99_ms={report['compute']['p99_ms']:.6f} "
            f"compute_misses={report['compute']['deadline_miss_count']} "
            f"absolute_misses={report['schedule']['absolute_deadline_miss_count']}"
        )
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
