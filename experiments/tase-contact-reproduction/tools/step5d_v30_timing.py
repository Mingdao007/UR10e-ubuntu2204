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
EXPECTED_THREAD_ENVIRONMENT = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}
SOLVER_BATCH_SIZE = 100
SOLVER_BATCH_REENTRY_SAMPLES = 99
SOLVER_BATCH_REENTRY_BOUNDARIES = list(range(100, 10_000, 100))
EXPECTED_SOLVER_MICROBENCHMARK_PACING = {
    "mode": "unmeasured_fixed_batch_yield_with_measured_reentry",
    "batch_size": SOLVER_BATCH_SIZE,
    "yield_s": 0.002,
    "yield_included_in_single_solve_latency": False,
    "steady_samples": 10_000,
    "steady_samples_per_batch": SOLVER_BATCH_SIZE,
    "measured_reentry_after_each_yield": True,
    "reentry_samples": SOLVER_BATCH_REENTRY_SAMPLES,
    "reentry_sample_boundaries": SOLVER_BATCH_REENTRY_BOUNDARIES,
    "reentry_included_in_steady_solver_summary": False,
    "all_reentry_samples_retained_raw": True,
    "reason": "avoid_linux_sched_fifo_runtime_throttling_during_10k_stress",
    "full_tick_loop_affected": False,
    "safe_hold_loop_affected": False,
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
    degraded_deadline_miss_ratio_max: float = 0.0002
    degraded_schedule_lateness_max_ms: float = 0.50
    degraded_max_consecutive_misses: int = 2


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
                "schema_version": "step5d_v30_remote_timing_raw_v2",
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
        "profile": "cupy/512/epsilon=0.010/r=0.8/qdot_cap=0.05",
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
        "inner_iterations": 512,
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
    if payload.get("schema_version") != "step5d_v30_remote_timing_raw_v2":
        blockers.append("remote_timing_schema_mismatch")
    if payload.get("profile") != expected_profile:
        blockers.append("remote_runtime_profile_mismatch")
    profile_selection = payload.get("profile_selection")
    if not isinstance(profile_selection, dict):
        profile_selection = {}
        blockers.append("remote_timing_profile_selection_missing")
    else:
        selection_without_sha = dict(profile_selection)
        selection_sha = selection_without_sha.pop("selection_sha256", None)
        computed_selection_sha = hashlib.sha256(
            json.dumps(
                selection_without_sha,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        effective_profile_sha = hashlib.sha256(
            json.dumps(
                payload.get("profile"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            profile_selection.get("schema_version")
            != "step5d_v30_timing_profile_selection_v1"
            or profile_selection.get("canonical_profile") != expected_profile
            or profile_selection.get("canonical_profile_sha256")
            != hashlib.sha256(
                json.dumps(
                    expected_profile,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            or profile_selection.get("requested_profile") != payload.get("profile")
            or profile_selection.get("requested_profile_sha256")
            != effective_profile_sha
            or profile_selection.get("effective_profile") != payload.get("profile")
            or profile_selection.get("effective_profile_sha256")
            != effective_profile_sha
            or payload.get("profile_sha256") != effective_profile_sha
            or selection_sha != computed_selection_sha
        ):
            blockers.append("remote_timing_profile_selection_binding_invalid")
        if (
            profile_selection.get("diagnostic_override_requested") is True
            or profile_selection.get("acceptance_profile_eligible") is not True
        ):
            blockers.append("remote_timing_diagnostic_profile_override")
    if payload.get("precompile_outside_control_loop") is not True:
        blockers.append("cupy_precompile_not_proven_outside_loop")
    if (
        payload.get("solver_microbenchmark_pacing")
        != EXPECTED_SOLVER_MICROBENCHMARK_PACING
    ):
        blockers.append("solver_microbenchmark_batch_reentry_pacing_unbound")
    if payload.get("cupy_host_staging_pinned") is not True:
        blockers.append("cupy_host_staging_not_pinned")
    if payload.get("cupy_dedicated_stream") is not True:
        blockers.append("cupy_dedicated_stream_not_proven")
    gpu_device = payload.get("gpu_device")
    if not (
        isinstance(gpu_device, dict)
        and isinstance(gpu_device.get("device_id"), int)
        and isinstance(gpu_device.get("name"), str)
        and bool(gpu_device.get("name"))
        and isinstance(gpu_device.get("compute_capability"), list)
        and len(gpu_device["compute_capability"]) == 2
        and all(isinstance(value, int) for value in gpu_device["compute_capability"])
        and isinstance(gpu_device.get("total_memory_bytes"), int)
        and gpu_device["total_memory_bytes"] > 0
    ):
        blockers.append("runtime_gpu_device_identity_unbound")
    nvidia_smi = payload.get("nvidia_smi")
    if not (
        isinstance(nvidia_smi, dict)
        and nvidia_smi.get("capture_scope")
        == "outside_measured_solver_and_500hz_loops"
        and all(
            isinstance(nvidia_smi.get(boundary), dict)
            and nvidia_smi[boundary].get("ok") is True
            and isinstance(nvidia_smi[boundary].get("values"), dict)
            and all(
                nvidia_smi[boundary]["values"].get(field) not in (None, "")
                for field in (
                    "driver_version",
                    "name",
                    "pci.bus_id",
                    "clocks.current.sm",
                    "clocks.current.memory",
                    "temperature.gpu",
                    "utilization.gpu",
                    "power.draw",
                    "persistence_mode",
                )
            )
            for boundary in ("start", "end")
        )
        and nvidia_smi["start"]["values"].get("driver_version")
        == nvidia_smi["end"]["values"].get("driver_version")
        and nvidia_smi["start"]["values"].get("name")
        == nvidia_smi["end"]["values"].get("name")
        == gpu_device.get("name")
        and nvidia_smi["start"]["values"].get("pci.bus_id")
        == nvidia_smi["end"]["values"].get("pci.bus_id")
    ):
        blockers.append("runtime_nvidia_smi_start_end_unbound")
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
        ("profile_selection", artifact_binding.get("profile_selection") or {}),
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

    reentry_raw_source = payload.get("solver_batch_reentry_ms")
    reentry_raw_valid = bool(
        isinstance(reentry_raw_source, list)
        and all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in reentry_raw_source
        )
    )
    if not reentry_raw_valid:
        blockers.append("solver_batch_reentry_raw_missing_or_invalid")
        reentry_raw: list[float] = []
    else:
        reentry_raw = [float(value) for value in reentry_raw_source]
    if len(reentry_raw) != SOLVER_BATCH_REENTRY_SAMPLES:
        blockers.append("solver_batch_reentry_sample_count_invalid")

    computed_reentry = _distribution(
        reentry_raw,
        hard_deadline_ms=thresholds.hard_deadline_ms,
    )
    reentry_source = payload.get("solver_batch_reentry")
    if not isinstance(reentry_source, dict):
        reentry_source = {}
        blockers.append("solver_batch_reentry_summary_missing")
    reentry = {
        "samples": int(reentry_source.get("samples", 0) or 0),
        "nonfinite_count": int(reentry_source.get("nonfinite_count", 0) or 0),
        "mean_ms": reentry_source.get("mean_ms"),
        "p95_ms": reentry_source.get("p95_ms"),
        "p99_ms": reentry_source.get("p99_ms"),
        "max_ms": reentry_source.get("max_ms"),
        "deadline_miss_count": int(
            reentry_source.get("compute_deadline_miss_count", 0) or 0
        ),
    }
    computed_reentry_summary = {
        "samples": computed_reentry["samples"],
        "nonfinite_count": computed_reentry["nonfinite_count"],
        "mean_ms": computed_reentry["mean_ms"],
        "p95_ms": computed_reentry["p95_ms"],
        "p99_ms": computed_reentry["p99_ms"],
        "max_ms": computed_reentry["max_ms"],
        "deadline_miss_count": computed_reentry["deadline_miss_count"],
    }

    def distribution_value_matches(left: Any, right: Any) -> bool:
        if left is None or right is None:
            return left is right
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return math.isclose(
                float(left),
                float(right),
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        return left == right

    if not all(
        distribution_value_matches(reentry.get(field), expected)
        for field, expected in computed_reentry_summary.items()
    ):
        blockers.append("solver_batch_reentry_summary_binding_mismatch")
    if computed_reentry["nonfinite_count"]:
        blockers.append("solver_batch_reentry_nonfinite_timing")

    reentry_miss_indices = [
        index
        for index, value in enumerate(reentry_raw)
        if math.isfinite(value) and value >= thresholds.hard_deadline_ms
    ]
    miss_diagnostics = payload.get("deadline_miss_diagnostics")
    miss_diagnostics = (
        miss_diagnostics if isinstance(miss_diagnostics, dict) else {}
    )
    reentry_miss_diagnostics = miss_diagnostics.get(
        "solver_batch_reentry_compute"
    )
    reentry_miss_diagnostics_valid = bool(
        isinstance(reentry_miss_diagnostics, dict)
        and reentry_miss_diagnostics.get("total") == len(reentry_miss_indices)
        and reentry_miss_diagnostics.get("retained_indices")
        == reentry_miss_indices
        and reentry_miss_diagnostics.get("capacity")
        == SOLVER_BATCH_REENTRY_SAMPLES
        and reentry_miss_diagnostics.get("overflowed") is False
    )
    if not reentry_miss_diagnostics_valid:
        blockers.append("solver_batch_reentry_miss_diagnostics_unbound")

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
    runtime_environment = payload.get("runtime_environment")
    if not isinstance(runtime_environment, dict):
        runtime_environment = {}
        blockers.append("runtime_timing_environment_missing")
    nice_value = runtime_environment.get("nice")
    scheduler_policy = runtime_environment.get("scheduler_policy")
    scheduler_priority = runtime_environment.get("scheduler_priority")
    production_fifo_priority_proven = bool(
        scheduler_policy == 1
        and scheduler_priority == 20
    )
    if not production_fifo_priority_proven:
        blockers.append("runtime_timing_process_priority_degraded")
    scheduler_limits = runtime_environment.get("scheduler_limits")
    rtprio_limits = (
        scheduler_limits.get("rtprio")
        if isinstance(scheduler_limits, dict)
        else None
    )
    if not (
        isinstance(rtprio_limits, list)
        and len(rtprio_limits) == 2
        and all(isinstance(value, int) for value in rtprio_limits)
        and min(rtprio_limits) >= 20
    ):
        blockers.append("runtime_realtime_limits_unbound")
    cuda_environment = runtime_environment.get("cuda")
    if not isinstance(cuda_environment, dict) or any(
        cuda_environment.get(name) in (None, "")
        for name in ("runtime_version", "driver_version", "nvrtc_version")
    ):
        blockers.append("runtime_cuda_versions_unbound")
    if runtime_environment.get("thread_environment") != EXPECTED_THREAD_ENVIRONMENT:
        blockers.append("runtime_timing_thread_environment_unbound")
    runtime_versions = runtime_environment.get("versions")
    if not isinstance(runtime_versions, dict) or any(
        not isinstance(runtime_versions.get(name), str)
        or runtime_versions.get(name) in {"", "unavailable"}
        for name in ("numpy", "cupy", "pinocchio")
    ):
        blockers.append("runtime_numeric_versions_unbound")
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
    full_control = payload.get("full_tick_control_diagnostics")
    if not isinstance(full_control, dict) or not (
        full_control.get("samples") == normalized["full_tick"]["samples"]
        and full_control.get("accepted_count") == normalized["full_tick"]["samples"]
        and full_control.get("execute_count") == normalized["full_tick"]["samples"]
        and full_control.get("safe_hold_count") == 0
        and full_control.get("execute_path_proven") is True
    ):
        blockers.append("full_tick_execute_path_evidence_incomplete")
    ramp_scale = (
        full_control.get("reference_ramp_scale", {})
        if isinstance(full_control, dict)
        else {}
    )
    ramp_error = (
        full_control.get("raw_to_governed_twist_error_norm", {})
        if isinstance(full_control, dict)
        else {}
    )
    ramp_active_count = (
        full_control.get("reference_ramp_active_count")
        if isinstance(full_control, dict)
        else None
    )
    if not (
        isinstance(ramp_active_count, int)
        and 0 < ramp_active_count <= normalized["full_tick"]["samples"]
        and isinstance(ramp_scale, dict)
        and isinstance(ramp_scale.get("min"), (int, float))
        and 0.0 < float(ramp_scale["min"]) <= 1.0
        and isinstance(ramp_scale.get("max"), (int, float))
        and 0.0 < float(ramp_scale["max"]) <= 1.0
        and isinstance(ramp_error, dict)
        and isinstance(ramp_error.get("max"), (int, float))
        and float(ramp_error["max"]) > 0.0
    ):
        blockers.append("full_tick_reference_ramp_evidence_incomplete")
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
    allowed_degraded_blockers = {
        "full_tick_deadline_miss",
        "full_tick_max_reaches_2ms_deadline",
        "full_tick_schedule_deadline_miss",
        "safe_hold_deadline_miss",
        "safe_hold_max_reaches_2ms_deadline",
        "safe_hold_schedule_deadline_miss",
    }
    degraded_unrelated_blockers = sorted(
        set(blockers) - allowed_degraded_blockers
    )
    degraded_full_budget = math.floor(
        thresholds.tick_samples_required
        * thresholds.degraded_deadline_miss_ratio_max
    )
    degraded_safe_budget = math.floor(
        thresholds.safe_hold_samples_required
        * thresholds.degraded_deadline_miss_ratio_max
    )
    full_lateness = payload.get("full_tick_schedule_max_lateness_ms")
    safe_lateness = payload.get("safe_hold_schedule_max_lateness_ms")
    expected_miss_totals = {
        "solver_compute": normalized["solver"]["deadline_miss_count"],
        "solver_batch_reentry_compute": reentry["deadline_miss_count"],
        "full_tick_compute": normalized["full_tick"]["deadline_miss_count"],
        "full_tick_schedule": schedule_misses,
        "safe_hold_compute": normalized["safe_hold"]["deadline_miss_count"],
        "safe_hold_schedule": safe_hold_schedule_misses,
    }
    miss_diagnostics_valid = bool(
        all(
            isinstance(miss_diagnostics.get(label), dict)
            and miss_diagnostics[label].get("total") == expected
            and miss_diagnostics[label].get("overflowed") is False
            for label, expected in expected_miss_totals.items()
        )
        and reentry_miss_diagnostics_valid
        and all(
            int((miss_diagnostics.get(label) or {}).get("max_consecutive", 0))
            <= thresholds.degraded_max_consecutive_misses
            for label in (
                "full_tick_compute",
                "full_tick_schedule",
                "safe_hold_compute",
                "safe_hold_schedule",
            )
        )
    )
    degraded_timing_candidate = bool(
        blockers
        and not degraded_unrelated_blockers
        and miss_diagnostics_valid
        and normalized["solver"]["deadline_miss_count"] == 0
        and normalized["full_tick"]["deadline_miss_count"]
        <= degraded_full_budget
        and normalized["safe_hold"]["deadline_miss_count"]
        <= degraded_safe_budget
        and schedule_misses <= degraded_full_budget
        and safe_hold_schedule_misses <= degraded_safe_budget
        and isinstance(full_lateness, (int, float))
        and float(full_lateness)
        <= thresholds.degraded_schedule_lateness_max_ms
        and isinstance(safe_lateness, (int, float))
        and float(safe_lateness)
        <= thresholds.degraded_schedule_lateness_max_ms
    )
    stale_hold_evidence = payload.get("controller_stale_hold_fault_evidence")
    stale_hold_proven = bool(
        isinstance(stale_hold_evidence, dict)
        and stale_hold_evidence.get("pass") is True
        and stale_hold_evidence.get("stale_tick_command")
        == "exact_zero_qdot_not_consumed"
    )
    degraded_fail_closed_pass = bool(
        degraded_timing_candidate and stale_hold_proven
    )
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
        "profile": payload.get("profile"),
        "expected_profile": expected_profile,
        "profile_sha256": payload.get("profile_sha256"),
        "profile_selection": profile_selection or {},
        "precompile_policy": "completed_before_control_loop",
        "cupy_precompile_ms": payload.get("cupy_precompile_ms"),
        "cupy_host_staging_pinned": payload.get("cupy_host_staging_pinned"),
        "cupy_dedicated_stream": payload.get("cupy_dedicated_stream"),
        "cupy_stream_priority": payload.get("cupy_stream_priority"),
        "cupy_stream_priority_capability": payload.get("cupy_stream_priority_capability"),
        "cupy_parallel_equivalence": parallel_equivalence,
        "model_prepare_ms": payload.get("model_prepare_ms"),
        "first_post_warm_ms": first_post_warm,
        "solver_microbenchmark_pacing": payload.get(
            "solver_microbenchmark_pacing"
        ),
        "solver": normalized["solver"],
        "solver_batch_reentry": reentry,
        "solver_batch_reentry_evidence": {
            "raw_samples_bound": bool(
                reentry_raw_valid
                and len(reentry_raw) == SOLVER_BATCH_REENTRY_SAMPLES
                and not computed_reentry["nonfinite_count"]
                and reentry_miss_diagnostics_valid
            ),
            "samples": len(reentry_raw),
            "steady_sample_boundaries": SOLVER_BATCH_REENTRY_BOUNDARIES,
            "deadline_miss_indices": reentry_miss_indices,
            "deadline_miss_count": len(reentry_miss_indices),
            "hard_solver_deadline_gate_applied": False,
            "steady_solver_hard_deadline_gate_applied": True,
            "full_tick_zero_miss_required_for_hard_acceptance": True,
            "acceptance_scope": (
                "diagnostic_only; every post-yield reentry is retained but the "
                "2 ms hard solver gate applies to the separate 10,000 steady "
                "samples"
            ),
        },
        "full_tick": normalized["full_tick"],
        "safe_hold": normalized["safe_hold"],
        "full_tick_schedule_deadline_miss_count": schedule_misses,
        "full_tick_schedule_max_lateness_ms": payload.get("full_tick_schedule_max_lateness_ms"),
        "safe_hold_schedule_deadline_miss_count": safe_hold_schedule_misses,
        "safe_hold_schedule_max_lateness_ms": payload.get(
            "safe_hold_schedule_max_lateness_ms"
        ),
        "pacing_provenance": payload.get("pacing_provenance"),
        "runtime_environment": runtime_environment,
        "gpu_device": gpu_device or {},
        "nvidia_smi": nvidia_smi or {},
        "runtime_scheduling_classification": (
            "production_sched_fifo_priority_20"
            if production_fifo_priority_proven
            else "degraded_or_unbound"
        ),
        "elapsed_full_tick_wall_s": elapsed,
        "elapsed_safe_hold_wall_s": safe_hold_elapsed,
        "full_tick_reason_counts": payload.get("full_tick_reason_counts", {}),
        "full_tick_control_diagnostics": full_control or {},
        "safe_hold_control_diagnostics": payload.get(
            "safe_hold_control_diagnostics", {}
        ),
        "thresholds": asdict(thresholds),
        "blockers": sorted(set(blockers)),
        "overall_pass": acceptance_eligible,
        "acceptance_eligible": acceptance_eligible,
        "classification": classification,
        "deadline_robustness": {
            "hard_realtime_pass": acceptance_eligible,
            "timing_degraded_candidate": degraded_timing_candidate,
            "degraded_fail_closed_pass": degraded_fail_closed_pass,
            "controller_stale_hold_proven": stale_hold_proven,
            "miss_index_diagnostics_valid": miss_diagnostics_valid,
            "deadline_miss_diagnostics": miss_diagnostics,
            "compute_miss_budget": degraded_full_budget,
            "safe_hold_miss_budget": degraded_safe_budget,
            "unrelated_blockers": degraded_unrelated_blockers,
            "claim_boundary": (
                "the 2 ms solver gate applies to 10,000 steady samples; all 99 "
                "post-yield reentries remain explicit diagnostics, and a hard "
                "500 Hz claim still requires zero full-tick deadline misses"
            ),
        },
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
