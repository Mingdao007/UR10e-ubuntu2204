#!/usr/bin/env python3
"""Offline timing aggregation for the v30 strict-RNN readiness gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


SOURCE_BINDING_FILES = {
    "contact_semantics_sha256": "tools/contact_semantics.py",
    "solver_sha256": "tools/step5c_strict_rnn.py",
    "outer_loop_sha256": "tools/step5d_paper_outer_loop.py",
    "control_contract_sha256": "tools/step5d_control_contract.py",
    "runtime_interface_sha256": "tools/step5d_runtime_interface.py",
    "kinematics_sha256": "tools/step5c_calibrated_kinematics_audit.py",
    "bridge_sha256": "tools/kunwei_rtde_bridge.py",
    "harness_sha256": "tools/run_step5d_v30_remote_timing.py",
    "bundler_sha256": "tools/build_step5d_v30_remote_timing_bundle.py",
    "aggregator_sha256": "tools/step5d_v30_timing.py",
    "readiness_builder_sha256": "tools/build_step5d_v30_offline_readiness.py",
}

EXPECTED_PACING_PROVENANCE = {
    "clock": "time.perf_counter",
    "control_hz": 500.0,
    "period_s": 0.002,
    "full_tick_release_policy": "absolute",
    "safe_hold_release_policy": "independent_absolute",
}


@dataclass(frozen=True)
class TimingThresholds:
    first_post_warm_max_ms: float = 1.75
    solver_p99_max_ms: float = 1.50
    full_tick_p99_max_ms: float = 1.80
    safe_hold_p99_max_ms: float = 1.80
    hard_deadline_ms: float = 2.00
    solver_samples_required: int = 10_000
    tick_samples_required: int = 30_000
    safe_hold_samples_required: int = 30_000


def _distribution(values: Sequence[float], *, hard_deadline_ms: float) -> dict[str, Any]:
    raw = np.asarray(values, dtype=float)
    finite_mask = np.isfinite(raw)
    finite = raw[finite_mask]
    result: dict[str, Any] = {
        "samples": int(raw.size),
        "finite_samples": int(finite.size),
        "nonfinite_count": int(raw.size - finite.size),
        "deadline_miss_count": int(np.count_nonzero(finite >= hard_deadline_ms)),
        "mean_ms": None,
        "p95_ms": None,
        "p99_ms": None,
        "max_ms": None,
    }
    if finite.size:
        result.update(
            {
                "mean_ms": float(np.mean(finite)),
                "p95_ms": float(np.percentile(finite, 95)),
                "p99_ms": float(np.percentile(finite, 99)),
                "max_ms": float(np.max(finite)),
            }
        )
    return result


def summarize_timing(
    *,
    solver_ms: Sequence[float],
    tick_ms: Sequence[float],
    safe_hold_ms: Sequence[float],
    first_post_warm_ms: float,
    thresholds: TimingThresholds = TimingThresholds(),
    full_tick_schedule_deadline_misses: int = 0,
    acceptance_provenance: Mapping[str, Any] | None = None,
    expected_source_binding: Mapping[str, str] | None = None,
    expected_replay_sha256: str | None = None,
    expected_paper_truth_sha256: str | None = None,
) -> dict[str, Any]:
    """Summarize raw arrays, fail-closed unless full provenance accompanies them.

    Arrays alone are useful synthetic diagnostics but cannot prove wall-clock
    pacing, independent safe-hold scheduling, or source/artifact identity.
    """

    solver = _distribution(solver_ms, hard_deadline_ms=thresholds.hard_deadline_ms)
    tick = _distribution(tick_ms, hard_deadline_ms=thresholds.hard_deadline_ms)
    safe_hold = _distribution(safe_hold_ms, hard_deadline_ms=thresholds.hard_deadline_ms)
    verified_binding_expectations = bool(
        expected_source_binding is not None
        and expected_replay_sha256 is not None
        and expected_paper_truth_sha256 is not None
    )
    if acceptance_provenance is not None and verified_binding_expectations:
        compact = dict(acceptance_provenance)

        def remote_distribution(result: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "samples": result["samples"],
                "nonfinite_count": result["nonfinite_count"],
                "mean_ms": result["mean_ms"],
                "p95_ms": result["p95_ms"],
                "p99_ms": result["p99_ms"],
                "max_ms": result["max_ms"],
                "compute_deadline_miss_count": result["deadline_miss_count"],
            }

        compact.update(
            {
                "schema_version": "step5d_v30_remote_timing_raw_v1",
                "first_post_warm_ms": float(first_post_warm_ms),
                "solver": remote_distribution(solver),
                "full_tick": remote_distribution(tick),
                "safe_hold": remote_distribution(safe_hold),
            }
        )
        return summarize_preaggregated(
            compact,
            thresholds=thresholds,
            expected_source_binding=expected_source_binding,
            expected_replay_sha256=expected_replay_sha256,
            expected_paper_truth_sha256=expected_paper_truth_sha256,
        )

    blockers: list[str] = []
    if expected_source_binding is None:
        blockers.append("remote_timing_expected_source_binding_missing")
    if expected_replay_sha256 is None:
        blockers.append("remote_timing_expected_replay_sha256_missing")
    if expected_paper_truth_sha256 is None:
        blockers.append("remote_timing_expected_paper_truth_sha256_missing")

    if not math.isfinite(float(first_post_warm_ms)) or first_post_warm_ms > thresholds.first_post_warm_max_ms:
        blockers.append("first_post_warm_exceeds_1p75_ms")
    for label, result, required in (
        ("solver", solver, thresholds.solver_samples_required),
        ("full_tick", tick, thresholds.tick_samples_required),
        ("safe_hold", safe_hold, thresholds.safe_hold_samples_required),
    ):
        if result["samples"] < required:
            blockers.append(f"{label}_insufficient_samples")
        if result["nonfinite_count"]:
            blockers.append(f"{label}_nonfinite_timing")
        if result["deadline_miss_count"]:
            blockers.append(f"{label}_deadline_miss")

    if solver["p99_ms"] is None or solver["p99_ms"] > thresholds.solver_p99_max_ms:
        blockers.append("solver_p99_exceeds_1p50_ms")
    if tick["p99_ms"] is None or tick["p99_ms"] > thresholds.full_tick_p99_max_ms:
        blockers.append("full_tick_p99_exceeds_1p80_ms")
    if safe_hold["p99_ms"] is None or safe_hold["p99_ms"] > thresholds.safe_hold_p99_max_ms:
        blockers.append("safe_hold_p99_exceeds_1p80_ms")
    if int(full_tick_schedule_deadline_misses) > 0:
        blockers.append("full_tick_schedule_deadline_miss")
    blockers.append(
        "raw_array_timing_missing_acceptance_provenance"
        if acceptance_provenance is None
        else "raw_array_timing_unverified_acceptance_bindings"
    )

    return {
        "schema_version": "step5d_v30_timing_v1",
        "profile": "cupy/32/epsilon=0.010/r=0.8/qdot_cap=0.05",
        "precompile_policy": "must_complete_before_control_loop",
        "thresholds": asdict(thresholds),
        "first_post_warm_ms": float(first_post_warm_ms),
        "solver": solver,
        "full_tick": tick,
        "full_tick_schedule_deadline_miss_count": int(full_tick_schedule_deadline_misses),
        "safe_hold": safe_hold,
        "blockers": sorted(set(blockers)),
        "overall_pass": False,
        "acceptance_eligible": False,
        "classification": "diagnostic_only_not_acceptance",
        "safety_boundary": [
            "offline timing evidence only",
            "no bridge start",
            "no controller write",
            "no motion authorization",
        ],
    }


def summarize_preaggregated(
    payload: dict[str, Any],
    *,
    thresholds: TimingThresholds = TimingThresholds(),
    expected_source_binding: Mapping[str, str] | None = None,
    expected_replay_sha256: str | None = None,
    expected_paper_truth_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate the compact stdout payload from the read-only Ubuntu harness."""

    expected_profile = {
        "backend": "cupy",
        "inner_iterations": 32,
        "epsilon": 0.010,
        "sigr_exponent_r": 0.8,
        "qdot_cap_rad_s": 0.05,
        "control_hz": 500.0,
    }
    blockers: list[str] = []
    if expected_source_binding is None:
        blockers.append("remote_timing_expected_source_binding_missing")
    if expected_replay_sha256 is None:
        blockers.append("remote_timing_expected_replay_sha256_missing")
    if expected_paper_truth_sha256 is None:
        blockers.append("remote_timing_expected_paper_truth_sha256_missing")
    if payload.get("schema_version") != "step5d_v30_remote_timing_raw_v1":
        blockers.append("remote_timing_schema_mismatch")
    if payload.get("profile") != expected_profile:
        blockers.append("remote_runtime_profile_mismatch")
    if payload.get("precompile_outside_control_loop") is not True:
        blockers.append("cupy_precompile_not_proven_outside_loop")
    if payload.get("cupy_host_staging_pinned") is not True:
        blockers.append("cupy_host_staging_not_pinned")
    if payload.get("cupy_dedicated_stream") is not True:
        blockers.append("cupy_dedicated_stream_not_proven")
    parallel_equivalence = payload.get("cupy_parallel_equivalence")
    if (
        not isinstance(parallel_equivalence, dict)
        or parallel_equivalence.get("bitwise_equal") is not True
        or int(parallel_equivalence.get("samples", 0) or 0) < 100
        or parallel_equivalence.get("parallel_block_threads") != 6
        or float(parallel_equivalence.get("max_abs_difference", math.inf)) != 0.0
    ):
        blockers.append("cupy_parallel_serial_equivalence_not_proven")
    source_binding = payload.get("source_binding")
    if not isinstance(source_binding, dict) or source_binding.get("delivery") != "stdin_bundle":
        source_binding = source_binding if isinstance(source_binding, dict) else {}
        blockers.append("remote_timing_source_bundle_unbound")
    for field in SOURCE_BINDING_FILES:
        value = source_binding.get(field)
        if not isinstance(value, str) or len(value) != 64:
            blockers.append(f"remote_timing_{field}_missing")
        if expected_source_binding is not None and value != expected_source_binding.get(field):
            blockers.append(f"remote_timing_{field}_does_not_match_local_source")
    artifact_binding = payload.get("artifact_binding")
    if not isinstance(artifact_binding, dict):
        artifact_binding = {}
        blockers.append("remote_timing_artifact_binding_missing")
    replay_binding = artifact_binding.get("replay_csv") or {}
    paper_binding = artifact_binding.get("paper_truth") or {}
    for label, binding in (
        ("replay_csv", replay_binding),
        ("paper_truth", paper_binding),
        ("stage_table", artifact_binding.get("stage_table") or {}),
        ("calibration_yaml", artifact_binding.get("calibration_yaml") or {}),
        ("ur_xacro", artifact_binding.get("ur_xacro") or {}),
    ):
        artifact_sha = binding.get("sha256") if isinstance(binding, dict) else None
        if (
            not isinstance(binding, dict)
            or not isinstance(artifact_sha, str)
            or len(artifact_sha) != 64
        ):
            blockers.append(f"remote_timing_{label}_artifact_unbound")
    if expected_replay_sha256 is not None and replay_binding.get("sha256") != expected_replay_sha256:
        blockers.append("remote_timing_replay_csv_sha_mismatch")
    if expected_paper_truth_sha256 is not None and paper_binding.get("sha256") != expected_paper_truth_sha256:
        blockers.append("remote_timing_paper_truth_sha_mismatch")

    normalized: dict[str, dict[str, Any]] = {}
    for label, required, p99_limit in (
        ("solver", thresholds.solver_samples_required, thresholds.solver_p99_max_ms),
        ("full_tick", thresholds.tick_samples_required, thresholds.full_tick_p99_max_ms),
        ("safe_hold", thresholds.safe_hold_samples_required, thresholds.safe_hold_p99_max_ms),
    ):
        source = payload.get(label)
        if not isinstance(source, dict):
            source = {}
            blockers.append(f"{label}_summary_missing")
        result = {
            "samples": int(source.get("samples", 0) or 0),
            "nonfinite_count": int(source.get("nonfinite_count", 0) or 0),
            "mean_ms": source.get("mean_ms"),
            "p95_ms": source.get("p95_ms"),
            "p99_ms": source.get("p99_ms"),
            "max_ms": source.get("max_ms"),
            "deadline_miss_count": int(source.get("compute_deadline_miss_count", 0) or 0),
        }
        normalized[label] = result
        if result["samples"] < required:
            blockers.append(f"{label}_insufficient_samples")
        elif result["samples"] > required:
            blockers.append(f"{label}_unexpected_sample_count")
        if result["nonfinite_count"]:
            blockers.append(f"{label}_nonfinite_timing")
        if result["deadline_miss_count"]:
            blockers.append(f"{label}_deadline_miss")
        p99 = result["p99_ms"]
        if not isinstance(p99, (int, float)) or not math.isfinite(float(p99)) or float(p99) > p99_limit:
            blockers.append(f"{label}_p99_exceeds_limit")
        maximum = result["max_ms"]
        if not isinstance(maximum, (int, float)) or not math.isfinite(float(maximum)) or float(maximum) >= thresholds.hard_deadline_ms:
            blockers.append(f"{label}_max_reaches_2ms_deadline")

    first_post_warm = payload.get("first_post_warm_ms")
    if (
        not isinstance(first_post_warm, (int, float))
        or not math.isfinite(float(first_post_warm))
        or float(first_post_warm) > thresholds.first_post_warm_max_ms
    ):
        blockers.append("first_post_warm_exceeds_1p75_ms")
    if "full_tick_schedule_deadline_miss_count" not in payload:
        blockers.append("full_tick_schedule_deadline_miss_count_missing")
    schedule_misses = int(payload.get("full_tick_schedule_deadline_miss_count", 0) or 0)
    if schedule_misses:
        blockers.append("full_tick_schedule_deadline_miss")
    if "safe_hold_schedule_deadline_miss_count" not in payload:
        blockers.append("safe_hold_schedule_deadline_miss_count_missing")
    safe_hold_schedule_misses = int(
        payload.get("safe_hold_schedule_deadline_miss_count", 0) or 0
    )
    if safe_hold_schedule_misses:
        blockers.append("safe_hold_schedule_deadline_miss")
    for label in ("full_tick", "safe_hold"):
        maximum_lateness = payload.get(f"{label}_schedule_max_lateness_ms")
        if (
            not isinstance(maximum_lateness, (int, float))
            or not math.isfinite(float(maximum_lateness))
            or float(maximum_lateness) < 0.0
        ):
            blockers.append(f"{label}_schedule_max_lateness_missing_or_invalid")
    if payload.get("paced_500hz") is not True:
        blockers.append("full_tick_not_paced_500hz")
    if payload.get("pacing_provenance") != EXPECTED_PACING_PROVENANCE:
        blockers.append("independent_absolute_500hz_pacing_provenance_missing")
    if payload.get("runtime_path_source") != "kunwei_rtde_bridge.step5d_v30_contract_pipeline":
        blockers.append("v30_runtime_contract_path_not_proven")
    for label, expected_count in (
        ("full_tick", normalized["full_tick"]["samples"]),
        ("safe_hold", normalized["safe_hold"]["samples"]),
    ):
        deferred = payload.get(f"{label}_deferred_diagnostics") or {}
        if (
            deferred.get("count") != expected_count
            or deferred.get("overflowed") is not False
        ):
            blockers.append(f"{label}_deferred_diagnostics_incomplete")
    safe_reasons = payload.get("safe_hold_reason_counts") or {}
    if sum(int(value) for value in safe_reasons.values()) != normalized["safe_hold"]["samples"]:
        blockers.append("safe_hold_runtime_rejection_path_incomplete")
    full_reasons = payload.get("full_tick_reason_counts") or {}
    if int(full_reasons.get("ok", 0) or 0) != normalized["full_tick"]["samples"]:
        blockers.append("full_tick_runtime_acceptance_path_incomplete")
    elapsed = payload.get("elapsed_full_tick_wall_s")
    if not isinstance(elapsed, (int, float)) or not 59.5 <= float(elapsed) <= 75.0:
        blockers.append("full_tick_wall_duration_not_60s")
    safe_hold_elapsed = payload.get("elapsed_safe_hold_wall_s")
    if (
        not isinstance(safe_hold_elapsed, (int, float))
        or not 59.5 <= float(safe_hold_elapsed) <= 75.0
    ):
        blockers.append("safe_hold_wall_duration_not_60s")

    hard_solver_failure = bool(
        normalized["solver"]["deadline_miss_count"]
        or (
            isinstance(normalized["solver"]["max_ms"], (int, float))
            and float(normalized["solver"]["max_ms"]) >= thresholds.hard_deadline_ms
        )
    )
    acceptance_eligible = not blockers
    classification = (
        "failed_hard_solver_deadline"
        if hard_solver_failure
        else (
            "acceptance_eligible"
            if acceptance_eligible
            else "diagnostic_only_not_acceptance"
        )
    )

    return {
        "schema_version": "step5d_v30_timing_v1",
        "source_schema_version": payload.get("schema_version"),
        "source_binding": source_binding,
        "artifact_binding": artifact_binding,
        "profile": expected_profile,
        "precompile_policy": "completed_before_control_loop",
        "cupy_precompile_ms": payload.get("cupy_precompile_ms"),
        "cupy_host_staging_pinned": payload.get("cupy_host_staging_pinned"),
        "cupy_dedicated_stream": payload.get("cupy_dedicated_stream"),
        "cupy_stream_priority": payload.get("cupy_stream_priority"),
        "cupy_stream_priority_capability": payload.get("cupy_stream_priority_capability"),
        "cupy_parallel_equivalence": parallel_equivalence,
        "model_prepare_ms": payload.get("model_prepare_ms"),
        "first_post_warm_ms": first_post_warm,
        "solver": normalized["solver"],
        "full_tick": normalized["full_tick"],
        "safe_hold": normalized["safe_hold"],
        "full_tick_schedule_deadline_miss_count": schedule_misses,
        "full_tick_schedule_max_lateness_ms": payload.get("full_tick_schedule_max_lateness_ms"),
        "safe_hold_schedule_deadline_miss_count": safe_hold_schedule_misses,
        "safe_hold_schedule_max_lateness_ms": payload.get(
            "safe_hold_schedule_max_lateness_ms"
        ),
        "pacing_provenance": payload.get("pacing_provenance"),
        "elapsed_full_tick_wall_s": elapsed,
        "elapsed_safe_hold_wall_s": safe_hold_elapsed,
        "full_tick_reason_counts": payload.get("full_tick_reason_counts", {}),
        "thresholds": asdict(thresholds),
        "blockers": sorted(set(blockers)),
        "overall_pass": acceptance_eligible,
        "acceptance_eligible": acceptance_eligible,
        "classification": classification,
        "safety_boundary": payload.get("safety_boundary", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_json", type=Path, help="JSON with solver_ms/tick_ms/safe_hold_ms/first_post_warm_ms")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.input_json.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[1]
    expected_source_binding = {
        field: hashlib.sha256((root / relative).read_bytes()).hexdigest()
        for field, relative in SOURCE_BINDING_FILES.items()
    }
    evidence = json.loads(
        (root / "config" / "step5d_v29_remote_evidence_sha256.json").read_text(
            encoding="utf-8"
        )
    )
    expected_replay_sha256 = evidence["sha256"]["bridge_rtde_500hz.csv"]
    expected_paper_truth_sha256 = hashlib.sha256(
        (root / "config" / "step5d_liveprep_solver_gate.json").read_bytes()
    ).hexdigest()
    if any(key in payload for key in ("solver_ms", "tick_ms", "safe_hold_ms")):
        provenance_keys = set(payload) - {
            "solver_ms",
            "tick_ms",
            "safe_hold_ms",
            "first_post_warm_ms",
        }
        result = summarize_timing(
            solver_ms=payload.get("solver_ms", []),
            tick_ms=payload.get("tick_ms", []),
            safe_hold_ms=payload.get("safe_hold_ms", []),
            first_post_warm_ms=float(payload.get("first_post_warm_ms", math.nan)),
            full_tick_schedule_deadline_misses=int(payload.get("full_tick_schedule_deadline_miss_count", 0)),
            acceptance_provenance=(payload if provenance_keys else None),
            expected_source_binding=expected_source_binding,
            expected_replay_sha256=expected_replay_sha256,
            expected_paper_truth_sha256=expected_paper_truth_sha256,
        )
    else:
        result = summarize_preaggregated(
            payload,
            expected_source_binding=expected_source_binding,
            expected_replay_sha256=expected_replay_sha256,
            expected_paper_truth_sha256=expected_paper_truth_sha256,
        )
    result["input_evidence"] = {
        "path": str(args.input_json),
        "sha256": hashlib.sha256(args.input_json.read_bytes()).hexdigest(),
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["overall_pass"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
