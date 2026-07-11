#!/usr/bin/env python3
"""Verify a chained 2 -> 10 -> 60 s MuJoCo P0 v8 evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np

from step5d_control_contract import V30_DEFERRED_NUMERIC_FIELDS
from step5d_simulator_adapter import (
    P0_V8_CANARY_PHASES_S,
    P0_V8_CONTROL_HZ,
    P0_V8_DBIL_HZ,
    P0_V8_EFFECTIVE_KO,
    P0_V8_EPSILON,
    P0_V8_INNER_ITERATIONS,
    P0_V8_PHYSICS_HZ,
    P0_V8_QDOT_CAP_RAD_S,
    P0_V8_SIGR_EXPONENT_R,
    simulation_claim_boundary,
)
from verify_step5d_sim_evidence import (
    source_composite_sha256,
    validate_evidence as validate_v1_evidence,
)


RUN_SCHEMA_V1 = "step5d_p0_v8_mujoco_run_v1"
RUN_SCHEMA_V2 = "step5d_p0_v8_mujoco_run_v2"
RUN_SCHEMA_V3 = "step5d_p0_v8_mujoco_run_v3"
EVIDENCE_SCHEMA_V1 = "ur10e_simulation_evidence_v1"
EVIDENCE_SCHEMA_V2 = "ur10e_simulation_evidence_v2"
EVIDENCE_SCHEMA_V3 = "ur10e_simulation_evidence_v3"
TIMING_SCOPE_VERSION = "p0_v8_timing_lane_split_v2"
CONTROL_HARD_SCOPE = "simulator_state_ready_to_adapter_step_complete"
SIMULATOR_CYCLE_SCOPE = (
    "release_to_oracle_snapshot_to_adapter_step_to_command_apply_and_four_physics_substeps"
)
PREWARM_SCHEMA = "step5d_p0_v8_production_path_prewarm_v1"
PREWARM_EXECUTE_TICKS = 1_000
PREWARM_CONTROL_HZ = P0_V8_CONTROL_HZ
PREWARM_MODE = "source_bound_unmeasured_no_output_500hz"
PREWARM_PACING_STRATEGY = "previous_tick_start_plus_2ms_no_catch_up"
PREWARM_BURST_TOLERANCE_S = 0.00005
CONTROL_PATH = (
    "SimulatorState->Step5dObservation->StrictRnnControlPolicy->"
    "step5d_v30_contract_pipeline->SafetyEnvelope->RegisterCommand->"
    "SimulationCommand"
)
TRACE_PREFAULT_STRATEGY = (
    "numpy_fill_zero_before_gc_collect_and_measured_loop"
)
NUMERIC_THREAD_ENV_CONTRACT = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_prewarm_contract() -> dict[str, object]:
    return {
        "schema": PREWARM_SCHEMA,
        "mode": PREWARM_MODE,
        "execute_ticks": PREWARM_EXECUTE_TICKS,
        "control_hz": PREWARM_CONTROL_HZ,
        "paced": True,
        "pacing_strategy": PREWARM_PACING_STRATEGY,
        "burst_tolerance_s": PREWARM_BURST_TOLERANCE_S,
        "control_path": CONTROL_PATH,
        "complete_production_path": True,
        "safety_envelope_exercised": True,
        "dls_shadow_only": True,
        "register_command_generated": True,
        "command_sink_write_allowed": False,
        "timing_acceptance_eligible": False,
        "measured_samples_may_be_discarded": False,
    }


def expected_prewarm_profile() -> dict[str, object]:
    return {
        "id": "step5d_strict_rnn_no_contact_p0_v8",
        "backend": "cupy",
        "inner_iterations": P0_V8_INNER_ITERATIONS,
        "epsilon": P0_V8_EPSILON,
        "sigr_exponent_r": P0_V8_SIGR_EXPONENT_R,
        "qdot_cap_rad_s": P0_V8_QDOT_CAP_RAD_S,
        "effective_ko": P0_V8_EFFECTIVE_KO,
        "dls_runtime_fallback_allowed": False,
    }


def validate_prewarm_evidence(payload: Mapping[str, object]) -> list[str]:
    """Require a complete, unmeasured, no-output production-path prewarm."""

    blockers: list[str] = []
    expected_top_level = {
        "schema",
        "generated_at",
        "source_composite_sha256",
        "profile",
        "contract",
        "result",
        "reset",
        "claim_boundary",
    }
    if set(payload) != expected_top_level:
        blockers.append("top_level_fields:invalid")
    if payload.get("schema") != PREWARM_SCHEMA:
        blockers.append("schema:invalid")
    if not isinstance(payload.get("generated_at"), str) or not payload.get(
        "generated_at"
    ):
        blockers.append("generated_at:invalid")
    if SHA256_RE.fullmatch(str(payload.get("source_composite_sha256") or "")) is None:
        blockers.append("source_composite_sha256:invalid")
    if payload.get("profile") != expected_prewarm_profile():
        blockers.append("profile:invalid")
    if payload.get("contract") != expected_prewarm_contract():
        blockers.append("contract:invalid")
    result = payload.get("result")
    expected_result = {
        "execute_tick_count": PREWARM_EXECUTE_TICKS,
        "accepted_tick_count": PREWARM_EXECUTE_TICKS,
        "safe_hold_count": 0,
        "stop_count": 0,
        "nonfinite_output_count": 0,
        "qdot_bound_violation_count": 0,
        "dls_shadow_count": PREWARM_EXECUTE_TICKS,
        "dls_runtime_fallback_count": 0,
        "register_command_generation_count": PREWARM_EXECUTE_TICKS,
        "command_sink_write_count": 0,
        "first_sequence": 0,
        "last_sequence": PREWARM_EXECUTE_TICKS - 1,
        "release_wait_count": PREWARM_EXECUTE_TICKS - 1,
        "deferred_diagnostic_count": PREWARM_EXECUTE_TICKS,
        "pacing_hz": PREWARM_CONTROL_HZ,
        "paced": True,
        "unmeasured": True,
        "no_output": True,
        "timing_acceptance_eligible": False,
        "measured_sample_count": 0,
        "pass": True,
    }
    dynamic_timing_fields = {
        "first_release_elapsed_s",
        "last_release_elapsed_s",
        "elapsed_release_span_s",
        "min_inter_release_s",
        "max_inter_release_s",
        "burst_interval_count",
    }
    if not isinstance(result, Mapping):
        blockers.append("result:invalid")
    else:
        for field, expected in expected_result.items():
            observed = result.get(field)
            if isinstance(expected, bool):
                matches = observed is expected
            elif isinstance(expected, int):
                matches = (
                    isinstance(observed, int)
                    and not isinstance(observed, bool)
                    and observed == expected
                )
            else:
                matches = observed == expected
            if not matches:
                blockers.append(f"result.{field}:invalid")
        unexpected = set(result) - set(expected_result) - dynamic_timing_fields
        missing = dynamic_timing_fields - set(result)
        if unexpected:
            blockers.append("result:unexpected_fields")
        if missing:
            blockers.append("result:timing_fields_missing")
        timing: dict[str, float] = {}
        for field in dynamic_timing_fields - {"burst_interval_count"}:
            try:
                value = float(result[field])
            except (KeyError, TypeError, ValueError):
                blockers.append(f"result.{field}:invalid")
                continue
            if not math.isfinite(value) or value < 0.0:
                blockers.append(f"result.{field}:invalid")
            timing[field] = value
        burst_count = result.get("burst_interval_count")
        if (
            not isinstance(burst_count, int)
            or isinstance(burst_count, bool)
            or burst_count != 0
        ):
            blockers.append("result.burst_interval_count:invalid")
        if len(timing) == len(dynamic_timing_fields) - 1:
            first = timing["first_release_elapsed_s"]
            last = timing["last_release_elapsed_s"]
            span = timing["elapsed_release_span_s"]
            minimum = timing["min_inter_release_s"]
            maximum = timing["max_inter_release_s"]
            if last < first or not math.isclose(
                span,
                last - first,
                abs_tol=1e-12,
            ):
                blockers.append("result.release_span:invalid")
            minimum_allowed = (
                1.0 / PREWARM_CONTROL_HZ - PREWARM_BURST_TOLERANCE_S
            )
            if minimum < minimum_allowed or maximum < minimum:
                blockers.append("result.inter_release_pacing:invalid")
            if span < (PREWARM_EXECUTE_TICKS - 1) * minimum_allowed:
                blockers.append("result.release_span:too_short")
    expected_reset = {
        "simulator_state_reset_after_prewarm": True,
        "solver_state_reset_after_prewarm": True,
        "control_adapter_discarded_after_prewarm": True,
        "measured_phase_first_sequence": 0,
        "post_reset_unmeasured_execute_tick_count": 0,
        "next_action": "measured_canonical_2_10_60_sequence",
    }
    if payload.get("reset") != expected_reset:
        blockers.append("reset:invalid")
    expected_boundary = {
        "prewarm_is_not_measured_timing_evidence": True,
        "prewarm_is_not_p0_pass": True,
        "prewarm_is_not_live_acceptance": True,
    }
    if payload.get("claim_boundary") != expected_boundary:
        blockers.append("claim_boundary:invalid")
    return sorted(set(blockers))


def validate_prewarm_binding(
    binding: object,
    *,
    root: Path,
    source_composite_sha256_value: str,
) -> tuple[list[str], Mapping[str, object] | None]:
    blockers: list[str] = []
    if not isinstance(binding, Mapping):
        return ["production_path_prewarm:missing_or_not_object"], None
    expected_binding = {
        "schema": PREWARM_SCHEMA,
        "source_composite_sha256": source_composite_sha256_value,
        "execute_tick_count": PREWARM_EXECUTE_TICKS,
        "pacing_hz": PREWARM_CONTROL_HZ,
        "paced": True,
        "pass": True,
    }
    for field, expected in expected_binding.items():
        if binding.get(field) != expected:
            blockers.append(f"production_path_prewarm.{field}:invalid")
    expected_fields = {
        *expected_binding,
        "path",
        "sha256",
        "size_bytes",
    }
    if set(binding) != expected_fields:
        blockers.append("production_path_prewarm.fields:invalid")
    path = (root / str(binding.get("path") or "")).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        blockers.append("production_path_prewarm.path:escape")
        return sorted(set(blockers)), None
    if not path.is_file():
        blockers.append("production_path_prewarm.path:missing")
        return sorted(set(blockers)), None
    if path.stat().st_size != binding.get("size_bytes"):
        blockers.append("production_path_prewarm.size_bytes:mismatch")
    if sha256_path(path) != binding.get("sha256"):
        blockers.append("production_path_prewarm.sha256:mismatch")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        blockers.append(
            f"production_path_prewarm.unreadable:{type(exc).__name__}"
        )
        return sorted(set(blockers)), None
    blockers.extend(
        f"production_path_prewarm.{item}"
        for item in validate_prewarm_evidence(payload)
    )
    if payload.get("source_composite_sha256") != source_composite_sha256_value:
        blockers.append("production_path_prewarm.source_composite_sha256:mismatch")
    return sorted(set(blockers)), payload


def _trace_blockers_v1(
    evidence: Mapping[str, object],
    phase_dir: Path,
    *,
    require_timing_threshold: bool | None = None,
) -> list[str]:
    blockers: list[str] = []
    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, Sequence):
        return ["trace:artifact_array_missing"]
    rows = [
        row for row in artifacts
        if isinstance(row, Mapping) and row.get("role") == "control_trace_npz"
    ]
    if len(rows) != 1:
        return ["trace:exactly_one_control_trace_required"]
    path = phase_dir / str(rows[0].get("path") or "")
    if not path.is_file():
        return ["trace:missing"]
    nominal = evidence.get("nominal")
    if not isinstance(nominal, Mapping):
        return ["trace:nominal_missing"]
    tick_count = int(nominal.get("tick_count") or 0)
    try:
        with np.load(path, allow_pickle=False) as trace:
            required = {
                "sequence", "sim_time_s", "compute_ms", "qdot", "accepted",
                "release_lateness_ms", "absolute_finish_lateness_ms",
                "action", "reason", "deferred_numeric", "deferred_reason",
                "deferred_action", "command_jacobian", "desired_twist",
                "reaction_normal", "approach_normal", "wrench",
                "native_contact_count", "cage_collision_count",
                "tcp_inside_cage",
            }
            missing = required - set(trace.files)
            if missing:
                return [f"trace:missing_arrays:{','.join(sorted(missing))}"]
            sequence = np.asarray(trace["sequence"])
            sim_time = np.asarray(trace["sim_time_s"], dtype=float)
            compute = np.asarray(trace["compute_ms"], dtype=float)
            release_lateness = np.asarray(
                trace["release_lateness_ms"], dtype=float
            )
            absolute_finish_lateness = np.asarray(
                trace["absolute_finish_lateness_ms"], dtype=float
            )
            qdot = np.asarray(trace["qdot"], dtype=float)
            jacobian = np.asarray(trace["command_jacobian"], dtype=float)
            desired = np.asarray(trace["desired_twist"], dtype=float)
            reaction = np.asarray(trace["reaction_normal"], dtype=float)
            approach = np.asarray(trace["approach_normal"], dtype=float)
            wrench = np.asarray(trace["wrench"], dtype=float)
            native_contact = np.asarray(trace["native_contact_count"])
            cage_collision = np.asarray(trace["cage_collision_count"])
            inside_cage = np.asarray(trace["tcp_inside_cage"])
            accepted = np.asarray(trace["accepted"])
            action = np.asarray(trace["action"])
            reason = np.asarray(trace["reason"])
            deferred = np.asarray(trace["deferred_numeric"], dtype=float)
    except (OSError, ValueError, KeyError) as exc:
        return [f"trace:unreadable:{type(exc).__name__}"]
    if sequence.shape != (tick_count,) or not np.array_equal(sequence, np.arange(tick_count)):
        blockers.append("trace:sequence_not_contiguous")
    if sim_time.shape != (tick_count,) or not np.all(np.isfinite(sim_time)):
        blockers.append("trace:sim_time_invalid")
    elif tick_count > 1 and not np.allclose(np.diff(sim_time), 1.0 / P0_V8_CONTROL_HZ, atol=1e-12, rtol=0.0):
        blockers.append("trace:sim_time_not_exact_500hz")
    compute_valid = (
        compute.shape == (tick_count,)
        and np.all(np.isfinite(compute))
        and not np.any(compute < 0.0)
    )
    if not compute_valid:
        blockers.append("trace:compute_timing_invalid")
    else:
        release_valid = (
            release_lateness.shape == (tick_count,)
            and np.all(np.isfinite(release_lateness))
            and not np.any(release_lateness < 0.0)
        )
        absolute_finish_valid = (
            absolute_finish_lateness.shape == (tick_count,)
            and np.all(np.isfinite(absolute_finish_lateness))
            and not np.any(absolute_finish_lateness < 0.0)
        )
        if not release_valid:
            blockers.append("trace:release_lateness_invalid")
        if not absolute_finish_valid:
            blockers.append("trace:absolute_finish_lateness_invalid")
        wall = evidence.get("wall_timing")
        if not isinstance(wall, Mapping):
            blockers.append("trace:wall_timing_missing")
        elif release_valid and absolute_finish_valid:
            def distribution(values: np.ndarray) -> dict[str, float]:
                return {
                    "p50_ms": float(np.percentile(values, 50)),
                    "p95_ms": float(np.percentile(values, 95)),
                    "p99_ms": float(np.percentile(values, 99)),
                    "max_ms": float(np.max(values)),
                }

            actual = {
                "samples": tick_count,
                "p50_ms": float(np.percentile(compute, 50)),
                "p95_ms": float(np.percentile(compute, 95)),
                "p99_ms": float(np.percentile(compute, 99)),
                "max_ms": float(np.max(compute)),
                "compute_deadline_miss_count": int(
                    np.count_nonzero(compute >= 2.0)
                ),
                "absolute_deadline_miss_count": int(
                    np.count_nonzero(absolute_finish_lateness > 0.0)
                ),
                "release_lateness_ms": distribution(release_lateness),
                "absolute_finish_lateness_ms": distribution(
                    absolute_finish_lateness
                ),
            }
            actual["deadline_miss_count"] = actual[
                "absolute_deadline_miss_count"
            ]
            for field in ("p50_ms", "p95_ms", "p99_ms", "max_ms"):
                try:
                    declared = float(wall[field])
                except (KeyError, TypeError, ValueError):
                    blockers.append(f"trace:wall_timing.{field}:invalid")
                    continue
                if not math.isclose(declared, actual[field], abs_tol=1e-9):
                    blockers.append(f"trace:wall_timing.{field}:trace_mismatch")
            if wall.get("samples") != actual["samples"]:
                blockers.append("trace:wall_timing.samples:trace_mismatch")
            for field in (
                "deadline_miss_count",
                "compute_deadline_miss_count",
                "absolute_deadline_miss_count",
            ):
                if wall.get(field) != actual[field]:
                    blockers.append(
                        f"trace:wall_timing.{field}:trace_mismatch"
                    )
                if nominal.get(field) != actual[field]:
                    blockers.append(f"trace:{field}:nominal_mismatch")
            for field in (
                "release_lateness_ms",
                "absolute_finish_lateness_ms",
            ):
                declared_distribution = wall.get(field)
                if not isinstance(declared_distribution, Mapping):
                    blockers.append(f"trace:wall_timing.{field}:invalid")
                    continue
                for statistic, value in actual[field].items():
                    try:
                        declared = float(declared_distribution[statistic])
                    except (KeyError, TypeError, ValueError):
                        blockers.append(
                            f"trace:wall_timing.{field}.{statistic}:invalid"
                        )
                        continue
                    if not math.isclose(declared, value, abs_tol=1e-9):
                        blockers.append(
                            f"trace:wall_timing.{field}.{statistic}:trace_mismatch"
                        )
            threshold_claimed = (
                wall.get("pass") is True
                if require_timing_threshold is None
                else require_timing_threshold
            )
            if threshold_claimed and (
                actual["compute_deadline_miss_count"] != 0
                or actual["absolute_deadline_miss_count"] != 0
                or actual["p99_ms"] > 1.80
                or actual["max_ms"] >= 2.0
            ):
                blockers.append("trace:claimed_wall_timing_pass_but_threshold_failed")
    if qdot.shape != (tick_count, 6) or not np.all(np.isfinite(qdot)):
        blockers.append("trace:qdot_invalid")
    elif float(np.max(np.abs(qdot))) > P0_V8_QDOT_CAP_RAD_S + 1e-12:
        blockers.append("trace:qdot_over_cap")
    if accepted.shape != (tick_count,) or not np.all(accepted == 1):
        blockers.append("trace:not_all_accepted")
    if action.shape != (tick_count,) or not np.all(action == "execute"):
        blockers.append("trace:action_not_all_execute")
    if reason.shape != (tick_count,) or not np.all(reason == "ok"):
        blockers.append("trace:reason_not_all_ok")
    if deferred.shape[0] != tick_count or not np.all(np.isfinite(deferred[:, :12])):
        blockers.append("trace:deferred_diagnostics_invalid")
    if jacobian.shape != (tick_count, 6, 6) or not np.all(np.isfinite(jacobian)):
        blockers.append("trace:command_jacobian_invalid")
    if desired.shape != (tick_count, 6) or not np.all(np.isfinite(desired)):
        blockers.append("trace:desired_twist_invalid")
    if (
        reaction.shape != (tick_count, 3)
        or approach.shape != (tick_count, 3)
        or not np.all(np.isfinite(reaction))
        or not np.all(np.isfinite(approach))
        or not np.allclose(np.linalg.norm(reaction, axis=1), 1.0, atol=1e-9)
        or not np.allclose(reaction + approach, 0.0, atol=1e-9)
    ):
        blockers.append("trace:normal_contract_invalid")
    elif desired.shape == (tick_count, 6) and not np.all(
        np.sum(desired[:, :3] * approach, axis=1) > 0.0
    ):
        blockers.append("trace:desired_twist_not_pressing")
    if wrench.shape != (tick_count, 6) or not np.all(np.isfinite(wrench)):
        blockers.append("trace:wrench_invalid")
    elif (
        np.any(np.linalg.norm(wrench[:, :3], axis=1) > 5.0 + 1e-12)
        or np.any(np.linalg.norm(wrench[:, 3:], axis=1) > 3.0 + 1e-12)
    ):
        blockers.append("trace:wrench_guard_exceeded")
    if native_contact.shape != (tick_count,) or np.any(native_contact != 0):
        blockers.append("trace:unexpected_native_contact")
    if cage_collision.shape != (tick_count,) or np.any(cage_collision != 0):
        blockers.append("trace:cage_collision")
    if inside_cage.shape != (tick_count,) or not np.all(inside_cage == 1):
        blockers.append("trace:tcp_outside_cage")
    if (
        jacobian.shape == (tick_count, 6, 6)
        and qdot.shape == (tick_count, 6)
        and deferred.shape == (tick_count, len(V30_DEFERRED_NUMERIC_FIELDS))
    ):
        field = {name: index for index, name in enumerate(V30_DEFERRED_NUMERIC_FIELDS)}
        predicted = np.einsum("nij,nj->ni", jacobian, qdot)
        deferred_qdot = deferred[:, [field[f"qdot_{i}"] for i in range(6)]]
        deferred_predicted = deferred[
            :, [field[f"predicted_twist_{i}"] for i in range(6)]
        ]
        register_qdot = deferred[:, [field[f"register_{37 + i}"] for i in range(6)]]
        if not (
            np.array_equal(qdot, deferred_qdot)
            and np.array_equal(qdot, register_qdot)
            and np.allclose(predicted, deferred_predicted, atol=1e-12, rtol=0.0)
        ):
            blockers.append("trace:production_command_chain_mismatch")
    else:
        blockers.append("trace:production_command_chain_unverifiable")
    return blockers


def _timing_distribution(values: np.ndarray) -> dict[str, float]:
    return {
        "p50_ms": float(np.percentile(values, 50)),
        "p95_ms": float(np.percentile(values, 95)),
        "p99_ms": float(np.percentile(values, 99)),
        "max_ms": float(np.max(values)),
    }


def _compare_distribution(
    declared: object,
    actual: Mapping[str, float],
    field: str,
    blockers: list[str],
) -> None:
    if not isinstance(declared, Mapping):
        blockers.append(f"{field}:invalid")
        return
    for statistic, value in actual.items():
        try:
            observed = float(declared[statistic])
        except (KeyError, TypeError, ValueError):
            blockers.append(f"{field}.{statistic}:invalid")
            continue
        if not math.isfinite(observed) or not math.isclose(
            observed,
            value,
            abs_tol=1e-9,
        ):
            blockers.append(f"{field}.{statistic}:trace_mismatch")


def _trace_blockers_v2(
    evidence: Mapping[str, object],
    phase_dir: Path,
    *,
    require_timing_threshold: bool | None = None,
) -> list[str]:
    blockers: list[str] = []
    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, Sequence):
        return ["trace:artifact_array_missing"]
    rows = [
        row
        for row in artifacts
        if isinstance(row, Mapping) and row.get("role") == "control_trace_npz"
    ]
    if len(rows) != 1:
        return ["trace:exactly_one_control_trace_required"]
    path = phase_dir / str(rows[0].get("path") or "")
    if not path.is_file():
        return ["trace:missing"]
    nominal = evidence.get("nominal")
    if not isinstance(nominal, Mapping):
        return ["trace:nominal_missing"]
    tick_count = int(nominal.get("tick_count") or 0)
    timing_names = (
        "control_compute_ms",
        "oracle_snapshot_ms",
        "command_apply_and_physics_ms",
        "cycle_wall_ms",
        "release_lateness_ms",
        "absolute_finish_lateness_ms",
    )
    try:
        with np.load(path, allow_pickle=False) as trace:
            required = {
                "sequence",
                "sim_time_s",
                *timing_names,
                "qdot",
                "accepted",
                "action",
                "reason",
                "deferred_numeric",
                "deferred_reason",
                "deferred_action",
                "command_jacobian",
                "desired_twist",
                "reaction_normal",
                "approach_normal",
                "wrench",
                "native_contact_count",
                "cage_collision_count",
                "tcp_inside_cage",
            }
            missing = required - set(trace.files)
            if missing:
                return [f"trace:missing_arrays:{','.join(sorted(missing))}"]
            values = {
                name: np.asarray(trace[name], dtype=float)
                for name in timing_names
            }
            sequence = np.asarray(trace["sequence"])
            sim_time = np.asarray(trace["sim_time_s"], dtype=float)
            qdot = np.asarray(trace["qdot"], dtype=float)
            jacobian = np.asarray(trace["command_jacobian"], dtype=float)
            desired = np.asarray(trace["desired_twist"], dtype=float)
            reaction = np.asarray(trace["reaction_normal"], dtype=float)
            approach = np.asarray(trace["approach_normal"], dtype=float)
            wrench = np.asarray(trace["wrench"], dtype=float)
            native_contact = np.asarray(trace["native_contact_count"])
            cage_collision = np.asarray(trace["cage_collision_count"])
            inside_cage = np.asarray(trace["tcp_inside_cage"])
            accepted = np.asarray(trace["accepted"])
            action = np.asarray(trace["action"])
            reason = np.asarray(trace["reason"])
            deferred = np.asarray(trace["deferred_numeric"], dtype=float)
    except (OSError, ValueError, KeyError) as exc:
        return [f"trace:unreadable:{type(exc).__name__}"]

    if sequence.shape != (tick_count,) or not np.array_equal(
        sequence,
        np.arange(tick_count),
    ):
        blockers.append("trace:sequence_not_contiguous")
    if sim_time.shape != (tick_count,) or not np.all(np.isfinite(sim_time)):
        blockers.append("trace:sim_time_invalid")
    elif tick_count > 1 and not np.allclose(
        np.diff(sim_time),
        1.0 / P0_V8_CONTROL_HZ,
        atol=1e-12,
        rtol=0.0,
    ):
        blockers.append("trace:sim_time_not_exact_500hz")
    timing_valid = True
    for name, array in values.items():
        if (
            array.shape != (tick_count,)
            or not np.all(np.isfinite(array))
            or np.any(array < 0.0)
        ):
            blockers.append(f"trace:{name}:invalid")
            timing_valid = False
    if timing_valid:
        control = values["control_compute_ms"]
        oracle = values["oracle_snapshot_ms"]
        physics = values["command_apply_and_physics_ms"]
        cycle = values["cycle_wall_ms"]
        release = values["release_lateness_ms"]
        finish_late = values["absolute_finish_lateness_ms"]
        # These intervals are nested/sequential timestamps from one clock.
        # Reject relabeling or timestamp tamper while tolerating nanosecond-scale
        # perf_counter rounding after conversion to milliseconds.
        if np.any(cycle + 1e-6 < oracle + control + physics):
            blockers.append("trace:timing_intervals_not_nested")
        control_misses = int(np.count_nonzero(control >= 2.0))
        cycle_misses = int(np.count_nonzero(cycle >= 2.0))
        absolute_misses = int(np.count_nonzero(finish_late > 0.0))
        control_declared = evidence.get("control_hard_500hz")
        if not isinstance(control_declared, Mapping):
            blockers.append("trace:control_hard_500hz_missing")
        else:
            if control_declared.get("scope") != CONTROL_HARD_SCOPE:
                blockers.append("trace:control_hard_500hz.scope:invalid")
            actual_control = _timing_distribution(control)
            for field, value in actual_control.items():
                try:
                    declared = float(control_declared[field])
                except (KeyError, TypeError, ValueError):
                    blockers.append(f"trace:control_hard_500hz.{field}:invalid")
                    continue
                if not math.isfinite(declared) or not math.isclose(
                    declared,
                    value,
                    abs_tol=1e-9,
                ):
                    blockers.append(
                        f"trace:control_hard_500hz.{field}:trace_mismatch"
                    )
            if control_declared.get("samples") != tick_count:
                blockers.append("trace:control_hard_500hz.samples:trace_mismatch")
            if control_declared.get("deadline_miss_count") != control_misses:
                blockers.append(
                    "trace:control_hard_500hz.deadline_miss_count:trace_mismatch"
                )
            if nominal.get("control_deadline_miss_count") != control_misses:
                blockers.append("trace:control_deadline_miss_count:nominal_mismatch")
            threshold_claimed = (
                control_declared.get("pass") is True
                if require_timing_threshold is None
                else require_timing_threshold
            )
            if threshold_claimed and (
                control_misses != 0
                or actual_control["p99_ms"] > 1.80
                or actual_control["max_ms"] >= 2.0
            ):
                blockers.append(
                    "trace:claimed_control_hard_500hz_pass_but_threshold_failed"
                )
        cycle_declared = evidence.get("simulator_cycle_diagnostic")
        if not isinstance(cycle_declared, Mapping):
            blockers.append("trace:simulator_cycle_diagnostic_missing")
        else:
            if cycle_declared.get("scope") != SIMULATOR_CYCLE_SCOPE:
                blockers.append("trace:simulator_cycle_diagnostic.scope:invalid")
            for name in (
                "oracle_snapshot_ms",
                "command_apply_and_physics_ms",
                "cycle_wall_ms",
                "release_lateness_ms",
                "absolute_finish_lateness_ms",
            ):
                _compare_distribution(
                    cycle_declared.get(name),
                    _timing_distribution(values[name]),
                    f"trace:simulator_cycle_diagnostic.{name}",
                    blockers,
                )
            expected_counts = {
                "cycle_compute_deadline_miss_count": cycle_misses,
                "absolute_deadline_miss_count": absolute_misses,
            }
            for field, expected in expected_counts.items():
                if cycle_declared.get(field) != expected:
                    blockers.append(
                        f"trace:simulator_cycle_diagnostic.{field}:trace_mismatch"
                    )
                if nominal.get(field) != expected:
                    blockers.append(f"trace:{field}:nominal_mismatch")
            source = evidence.get("source_binding")
            runtime = (
                source.get("runtime_timing_environment")
                if isinstance(source, Mapping)
                else None
            )
            paced = isinstance(runtime, Mapping) and runtime.get("paced_wall_clock") is True
            if paced:
                expected_finish_late = np.maximum(0.0, release + cycle - 2.0)
                if not np.allclose(
                    finish_late,
                    expected_finish_late,
                    atol=1e-6,
                    rtol=0.0,
                ):
                    blockers.append("trace:absolute_finish_timestamp_relation_invalid")

    # Reuse the non-timing command-chain checks by presenting a v1-compatible
    # trace copy is deliberately avoided: v2 must never accept a legacy timing
    # array under a new label. Keep the shared invariants explicit here.
    if qdot.shape != (tick_count, 6) or not np.all(np.isfinite(qdot)):
        blockers.append("trace:qdot_invalid")
    elif float(np.max(np.abs(qdot))) > P0_V8_QDOT_CAP_RAD_S + 1e-12:
        blockers.append("trace:qdot_over_cap")
    if accepted.shape != (tick_count,) or not np.all(accepted == 1):
        blockers.append("trace:not_all_accepted")
    if action.shape != (tick_count,) or not np.all(action == "execute"):
        blockers.append("trace:action_not_all_execute")
    if reason.shape != (tick_count,) or not np.all(reason == "ok"):
        blockers.append("trace:reason_not_all_ok")
    if deferred.shape[0] != tick_count or not np.all(np.isfinite(deferred[:, :12])):
        blockers.append("trace:deferred_diagnostics_invalid")
    if jacobian.shape != (tick_count, 6, 6) or not np.all(np.isfinite(jacobian)):
        blockers.append("trace:command_jacobian_invalid")
    if desired.shape != (tick_count, 6) or not np.all(np.isfinite(desired)):
        blockers.append("trace:desired_twist_invalid")
    if (
        reaction.shape != (tick_count, 3)
        or approach.shape != (tick_count, 3)
        or not np.all(np.isfinite(reaction))
        or not np.all(np.isfinite(approach))
        or not np.allclose(np.linalg.norm(reaction, axis=1), 1.0, atol=1e-9)
        or not np.allclose(reaction + approach, 0.0, atol=1e-9)
    ):
        blockers.append("trace:normal_contract_invalid")
    elif desired.shape == (tick_count, 6) and not np.all(
        np.sum(desired[:, :3] * approach, axis=1) > 0.0
    ):
        blockers.append("trace:desired_twist_not_pressing")
    if wrench.shape != (tick_count, 6) or not np.all(np.isfinite(wrench)):
        blockers.append("trace:wrench_invalid")
    elif (
        np.any(np.linalg.norm(wrench[:, :3], axis=1) > 5.0 + 1e-12)
        or np.any(np.linalg.norm(wrench[:, 3:], axis=1) > 3.0 + 1e-12)
    ):
        blockers.append("trace:wrench_guard_exceeded")
    if native_contact.shape != (tick_count,) or np.any(native_contact != 0):
        blockers.append("trace:unexpected_native_contact")
    if cage_collision.shape != (tick_count,) or np.any(cage_collision != 0):
        blockers.append("trace:cage_collision")
    if inside_cage.shape != (tick_count,) or not np.all(inside_cage == 1):
        blockers.append("trace:tcp_outside_cage")
    if (
        jacobian.shape == (tick_count, 6, 6)
        and qdot.shape == (tick_count, 6)
        and deferred.shape == (tick_count, len(V30_DEFERRED_NUMERIC_FIELDS))
    ):
        field = {
            name: index for index, name in enumerate(V30_DEFERRED_NUMERIC_FIELDS)
        }
        predicted = np.einsum("nij,nj->ni", jacobian, qdot)
        deferred_qdot = deferred[:, [field[f"qdot_{i}"] for i in range(6)]]
        deferred_predicted = deferred[
            :, [field[f"predicted_twist_{i}"] for i in range(6)]
        ]
        register_qdot = deferred[
            :, [field[f"register_{37 + i}"] for i in range(6)]
        ]
        if not (
            np.array_equal(qdot, deferred_qdot)
            and np.array_equal(qdot, register_qdot)
            and np.allclose(predicted, deferred_predicted, atol=1e-12, rtol=0.0)
        ):
            blockers.append("trace:production_command_chain_mismatch")
    else:
        blockers.append("trace:production_command_chain_unverifiable")
    return blockers


def _trace_blockers(
    evidence: Mapping[str, object],
    phase_dir: Path,
    *,
    require_timing_threshold: bool | None = None,
) -> list[str]:
    if evidence.get("schema") == EVIDENCE_SCHEMA_V2:
        return _trace_blockers_v2(
            evidence,
            phase_dir,
            require_timing_threshold=require_timing_threshold,
        )
    return _trace_blockers_v1(
        evidence,
        phase_dir,
        require_timing_threshold=require_timing_threshold,
    )


def _validate_run_manifest_v1(
    payload: Mapping[str, object],
    *,
    root: Path,
    require_complete: bool = True,
) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema") != RUN_SCHEMA_V1:
        blockers.append("schema:invalid")
    fingerprint = str(payload.get("source_composite_sha256") or "")
    if SHA256_RE.fullmatch(fingerprint) is None:
        blockers.append("source_composite_sha256:invalid")
    phases = payload.get("phases")
    if not isinstance(phases, Sequence) or isinstance(phases, (str, bytes)):
        return sorted(set(blockers + ["phases:missing_or_not_array"]))
    expected = P0_V8_CANARY_PHASES_S if require_complete else P0_V8_CANARY_PHASES_S[: len(phases)]
    actual = tuple(
        float(row.get("duration_s", math.nan)) if isinstance(row, Mapping) else math.nan
        for row in phases
    )
    if actual != expected:
        blockers.append("phases:not_canonical_2_10_60_sequence")
    observed_timing: list[dict[str, object]] = []
    for index, row in enumerate(phases):
        if not isinstance(row, Mapping):
            blockers.append(f"phases[{index}]:not_object")
            continue
        if row.get("sequence_index") != index:
            blockers.append(f"phases[{index}].sequence_index:mismatch")
        if row.get("structurally_valid") is not True:
            blockers.append(f"phases[{index}].structurally_valid:false")
        if row.get("validation_blockers") != []:
            blockers.append(f"phases[{index}].validation_blockers:not_empty")
        relpath = str(row.get("evidence_path") or "")
        evidence_path = (root / relpath).resolve()
        try:
            evidence_path.relative_to(root.resolve())
        except ValueError:
            blockers.append(f"phases[{index}].evidence_path:escape")
            continue
        if not evidence_path.is_file():
            blockers.append(f"phases[{index}].evidence_path:missing")
            continue
        if evidence_path.stat().st_size != row.get("evidence_size_bytes"):
            blockers.append(f"phases[{index}].evidence_size:mismatch")
        if sha256_path(evidence_path) != row.get("evidence_sha256"):
            blockers.append(f"phases[{index}].evidence_sha256:mismatch")
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            blockers.append(f"phases[{index}].evidence:unreadable:{type(exc).__name__}")
            continue
        for item in validate_v1_evidence(evidence, artifact_root=evidence_path.parent):
            blockers.append(f"phases[{index}].{item}")
        source = evidence.get("source_binding")
        if not isinstance(source, Mapping) or source.get("composite_sha256") != fingerprint:
            blockers.append(f"phases[{index}].fingerprint:mismatch")
        phase = evidence.get("phase")
        if not isinstance(phase, Mapping) or phase.get("duration_s") != actual[index]:
            blockers.append(f"phases[{index}].duration:mismatch")
        nominal = evidence.get("nominal")
        if not isinstance(nominal, Mapping) or nominal.get("control_path_diagnostic_pass") is not True:
            blockers.append(f"phases[{index}].control_path_diagnostic_pass:false")
        else:
            clock = nominal.get("sim_clock")
            ticks = int(round(actual[index] * P0_V8_CONTROL_HZ))
            expected_clock = {
                "physics_tick_count": int(round(actual[index] * P0_V8_PHYSICS_HZ)),
                "control_tick_count": ticks,
                "dbil_tick_count": int(round(actual[index] * P0_V8_DBIL_HZ)),
                "first_sequence": 0,
                "last_sequence": ticks - 1,
            }
            if not isinstance(clock, Mapping):
                blockers.append(f"phases[{index}].sim_clock:missing")
            else:
                for field, value in expected_clock.items():
                    if clock.get(field) != value:
                        blockers.append(f"phases[{index}].sim_clock.{field}:mismatch")
                drift = clock.get("drift_s")
                if not isinstance(drift, (int, float)) or abs(float(drift)) > 1e-9:
                    blockers.append(f"phases[{index}].sim_clock.drift_s:nonzero")
        wall = evidence.get("wall_timing")
        wall_pass = wall.get("pass") if isinstance(wall, Mapping) else None
        wall_compute_misses = (
            wall.get("compute_deadline_miss_count")
            if isinstance(wall, Mapping)
            else None
        )
        wall_absolute_misses = (
            wall.get("absolute_deadline_miss_count")
            if isinstance(wall, Mapping)
            else None
        )
        if not isinstance(wall_pass, bool):
            blockers.append(f"phases[{index}].wall_timing.pass:not_boolean")
        if row.get("wall_timing_pass") is not wall_pass:
            blockers.append(f"phases[{index}].wall_timing_pass:mismatch")
        if row.get("compute_deadline_miss_count") != wall_compute_misses:
            blockers.append(
                f"phases[{index}].compute_deadline_miss_count:mismatch"
            )
        if row.get("absolute_deadline_miss_count") != wall_absolute_misses:
            blockers.append(
                f"phases[{index}].absolute_deadline_miss_count:mismatch"
            )
        observed_timing.append(
            {
                "duration_s": actual[index],
                "compute_deadline_miss_count": wall_compute_misses,
                "absolute_deadline_miss_count": wall_absolute_misses,
                "pass": wall_pass,
            }
        )
        faults = evidence.get("faults")
        if isinstance(faults, Sequence):
            for fault in faults:
                if not isinstance(fault, Mapping):
                    continue
                command_qdot = fault.get("command_qdot")
                if command_qdot != [0.0] * 6:
                    blockers.append(
                        f"phases[{index}].faults.{fault.get('id')}:command_not_exact_zero"
                    )
        blockers.extend(
            f"phases[{index}].{item}"
            for item in _trace_blockers(
                evidence,
                evidence_path.parent,
                require_timing_threshold=row.get("wall_timing_pass") is True,
            )
        )
    if require_complete and payload.get("canonical_phase_sequence_complete") is not True:
        blockers.append("canonical_phase_sequence_complete:false")
    if payload.get("claims") != {
        "p0_sim_physics_pass": False,
        "live_accepted": False,
        "reproduction_complete": False,
    }:
        blockers.append("claims:non_promotion_boundary_mismatch")
    if payload.get("claim_boundary") != simulation_claim_boundary():
        blockers.append("claim_boundary:non_promotion_boundary_mismatch")
    complete = len(phases) == len(P0_V8_CANARY_PHASES_S)
    final_timing = next(
        (row.get("pass") for row in observed_timing if row.get("duration_s") == 60.0),
        False,
    )
    expected_timing_pass = bool(complete and final_timing is True)
    timing_gate = payload.get("timing_gate")
    if not isinstance(timing_gate, Mapping):
        blockers.append("timing_gate:missing_or_not_object")
    else:
        expected_gate = {
            "scope": "separate_wall_timing_acceptance",
            "deadline_accounting": "compute_elapsed_and_absolute_release_deadline_v2",
            "required_phase_duration_s": 60.0,
            "deadline_ms": 2.0,
            "p99_limit_ms": 1.80,
            "requires_zero_compute_deadline_misses": True,
            "requires_zero_absolute_deadline_misses": True,
            "phase_results": observed_timing,
            "complete_sequence_evaluated": complete,
            "pass": expected_timing_pass,
        }
        if dict(timing_gate) != expected_gate:
            blockers.append("timing_gate:verdict_mismatch")
    control_pass = all(
        isinstance(row, Mapping)
        and row.get("control_path_diagnostic_pass") is True
        for row in phases
    )
    if control_pass and complete and expected_timing_pass:
        expected_result = "diagnostic_pass"
    elif control_pass and complete:
        expected_result = "control_diagnostic_pass_timing_blocked"
    elif control_pass:
        expected_result = "diagnostic_partial_pass"
    else:
        expected_result = "diagnostic_fail"
    if payload.get("result") != expected_result:
        blockers.append(f"result:expected_{expected_result}")
    declared_blockers = payload.get("blockers")
    if not isinstance(declared_blockers, list):
        blockers.append("blockers:missing_or_not_array")
    elif expected_result == "control_diagnostic_pass_timing_blocked":
        if "wall_timing_gate_failed_60s" not in declared_blockers:
            blockers.append("blockers:wall_timing_gate_failed_60s_missing")
    return sorted(set(blockers))


def _legacy_compatibility_view(payload: Mapping[str, object]) -> dict[str, object]:
    """Map v2 control-hard fields into the unchanged v1 common validator."""

    control = payload.get("control_hard_500hz")
    nominal = payload.get("nominal")
    if not isinstance(control, Mapping) or not isinstance(nominal, Mapping):
        return dict(payload)
    misses = control.get("deadline_miss_count")
    lateness = 0.0 if misses == 0 else 0.1
    legacy_nominal = dict(nominal)
    legacy_nominal.update(
        {
            "deadline_miss_count": misses,
            "compute_deadline_miss_count": misses,
            "absolute_deadline_miss_count": misses,
        }
    )
    legacy_wall = {
        "scope": "read_state_to_shared_control_to_four_physics_substeps",
        "deadline_accounting": "compute_elapsed_and_absolute_release_deadline_v2",
        "paced": control.get("paced"),
        "samples": control.get("samples"),
        "deadline_ms": control.get("deadline_ms"),
        "p99_limit_ms": control.get("p99_limit_ms"),
        **{
            name: control.get(name)
            for name in ("p50_ms", "p95_ms", "p99_ms", "max_ms")
        },
        "deadline_miss_count": misses,
        "compute_deadline_miss_count": misses,
        "absolute_deadline_miss_count": misses,
        "release_lateness_ms": {
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "max_ms": 0.0,
        },
        "absolute_finish_lateness_ms": {
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "max_ms": lateness,
        },
        "p99_within_limit": control.get("p99_within_limit"),
        "max_within_deadline": control.get("max_within_deadline"),
        "absolute_finish_within_deadline": misses == 0,
        "pass": control.get("pass"),
    }
    compatibility = dict(payload)
    compatibility["schema"] = EVIDENCE_SCHEMA_V1
    compatibility["nominal"] = legacy_nominal
    compatibility["wall_timing"] = legacy_wall
    compatibility.pop("control_hard_500hz", None)
    compatibility.pop("simulator_cycle_diagnostic", None)
    return compatibility


def validate_v2_evidence(
    payload: Mapping[str, object],
    *,
    artifact_root: Path | None = None,
) -> list[str]:
    blockers = list(
        validate_v1_evidence(
            _legacy_compatibility_view(payload),
            artifact_root=artifact_root,
        )
    )
    if payload.get("schema") != EVIDENCE_SCHEMA_V2:
        blockers.append("schema:invalid_v2")
    source = payload.get("source_binding")
    runtime = (
        source.get("runtime_timing_environment")
        if isinstance(source, Mapping)
        else None
    )
    if not isinstance(source, Mapping) or source.get(
        "composite_sha256"
    ) != source_composite_sha256(source):
        blockers.append("source_binding.composite_sha256:mismatch")
    if not isinstance(runtime, Mapping):
        blockers.append("source_binding.runtime_timing_environment:missing")
        runtime = {}
    scope = runtime.get("timing_scope_contract")
    expected_scope = {
        "version": TIMING_SCOPE_VERSION,
        "control_hard_500hz": CONTROL_HARD_SCOPE,
        "simulator_cycle_diagnostic": SIMULATOR_CYCLE_SCOPE,
    }
    if scope != expected_scope:
        blockers.append("source_binding.runtime_timing_environment.timing_scope_contract:invalid")
    prefault = runtime.get("trace_prefault")
    expected_prefault = {
        "required": True,
        "completed": True,
        "strategy": TRACE_PREFAULT_STRATEGY,
    }
    if prefault != expected_prefault:
        blockers.append("source_binding.runtime_timing_environment.trace_prefault:invalid")
    if runtime.get("thread_environment") != NUMERIC_THREAD_ENV_CONTRACT:
        blockers.append(
            "source_binding.runtime_timing_environment.thread_environment:invalid"
        )
    control_contract = payload.get("control_contract")
    if not isinstance(control_contract, Mapping):
        blockers.append("control_contract:missing")
        control_contract = {}
    if control_contract.get("timing_scope_version") != TIMING_SCOPE_VERSION:
        blockers.append("control_contract.timing_scope_version:invalid")
    if control_contract.get("trace_buffers_prefaulted") is not True:
        blockers.append("control_contract.trace_buffers_prefaulted:must_be_true")

    nominal = payload.get("nominal")
    tick_count = nominal.get("tick_count") if isinstance(nominal, Mapping) else None
    control = payload.get("control_hard_500hz")
    if not isinstance(control, Mapping):
        blockers.append("control_hard_500hz:missing")
    else:
        required = {
            "scope": CONTROL_HARD_SCOPE,
            "deadline_ms": 2.0,
            "p99_limit_ms": 1.8,
            "prefault_required": True,
            "prefault_verified": True,
        }
        for field, expected in required.items():
            if control.get(field) != expected:
                blockers.append(f"control_hard_500hz.{field}:invalid")
        if control.get("samples") != tick_count:
            blockers.append("control_hard_500hz.samples:mismatch")
        values: list[float] = []
        for field in ("p50_ms", "p95_ms", "p99_ms", "max_ms"):
            try:
                value = float(control[field])
            except (KeyError, TypeError, ValueError):
                blockers.append(f"control_hard_500hz.{field}:invalid")
                value = math.nan
            values.append(value)
        misses = control.get("deadline_miss_count")
        p50, p95, p99, maximum = values
        numeric_valid = all(math.isfinite(value) and value >= 0.0 for value in values)
        if not numeric_valid or not p50 <= p95 <= p99 <= maximum:
            blockers.append("control_hard_500hz.distribution:invalid")
        if not isinstance(misses, int) or isinstance(misses, bool) or misses < 0:
            blockers.append("control_hard_500hz.deadline_miss_count:invalid")
        else:
            if isinstance(nominal, Mapping) and nominal.get(
                "control_deadline_miss_count"
            ) != misses:
                blockers.append("control_hard_500hz.deadline_miss_count:nominal_mismatch")
            expected_p99 = numeric_valid and p99 <= 1.8
            expected_max = numeric_valid and maximum < 2.0
            expected_pass = (
                control.get("paced") is True
                and misses == 0
                and expected_p99
                and expected_max
                and control.get("prefault_verified") is True
            )
            if control.get("p99_within_limit") is not expected_p99:
                blockers.append("control_hard_500hz.p99_within_limit:mismatch")
            if control.get("max_within_deadline") is not expected_max:
                blockers.append("control_hard_500hz.max_within_deadline:mismatch")
            if control.get("pass") is not expected_pass:
                blockers.append("control_hard_500hz.pass:mismatch")
    cycle = payload.get("simulator_cycle_diagnostic")
    if not isinstance(cycle, Mapping):
        blockers.append("simulator_cycle_diagnostic:missing")
    else:
        if cycle.get("scope") != SIMULATOR_CYCLE_SCOPE:
            blockers.append("simulator_cycle_diagnostic.scope:invalid")
        if cycle.get("diagnostic_only") is not True:
            blockers.append("simulator_cycle_diagnostic.diagnostic_only:must_be_true")
        if cycle.get("samples") != tick_count:
            blockers.append("simulator_cycle_diagnostic.samples:mismatch")
        if cycle.get("physics_substeps_per_control_tick") != 4:
            blockers.append("simulator_cycle_diagnostic.physics_substeps_per_control_tick:invalid")
        for field in (
            "cycle_compute_deadline_miss_count",
            "absolute_deadline_miss_count",
        ):
            value = cycle.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                blockers.append(f"simulator_cycle_diagnostic.{field}:invalid")
            elif isinstance(nominal, Mapping) and nominal.get(field) != value:
                blockers.append(f"simulator_cycle_diagnostic.{field}:nominal_mismatch")
        expected_meets = (
            cycle.get("cycle_compute_deadline_miss_count") == 0
            and cycle.get("absolute_deadline_miss_count") == 0
        )
        if cycle.get("meets_500hz_diagnostic") is not expected_meets:
            blockers.append("simulator_cycle_diagnostic.meets_500hz_diagnostic:mismatch")
        for field in (
            "oracle_snapshot_ms",
            "command_apply_and_physics_ms",
            "cycle_wall_ms",
            "release_lateness_ms",
            "absolute_finish_lateness_ms",
        ):
            declared = cycle.get(field)
            if not isinstance(declared, Mapping):
                blockers.append(f"simulator_cycle_diagnostic.{field}:invalid")
                continue
            try:
                values = [
                    float(declared[name])
                    for name in ("p50_ms", "p95_ms", "p99_ms", "max_ms")
                ]
            except (KeyError, TypeError, ValueError):
                blockers.append(f"simulator_cycle_diagnostic.{field}:invalid")
                continue
            if not all(math.isfinite(value) and value >= 0.0 for value in values) or not (
                values[0] <= values[1] <= values[2] <= values[3]
            ):
                blockers.append(f"simulator_cycle_diagnostic.{field}:invalid")
    return sorted(set(blockers))


def validate_v3_evidence(
    payload: Mapping[str, object],
    *,
    artifact_root: Path | None = None,
) -> list[str]:
    compatibility = dict(payload)
    compatibility["schema"] = EVIDENCE_SCHEMA_V2
    blockers = list(
        validate_v2_evidence(
            compatibility,
            artifact_root=artifact_root,
        )
    )
    source = payload.get("source_binding")
    runtime = (
        source.get("runtime_timing_environment")
        if isinstance(source, Mapping)
        else None
    )
    contract = (
        runtime.get("production_path_prewarm_contract")
        if isinstance(runtime, Mapping)
        else None
    )
    if contract != expected_prewarm_contract():
        blockers.append(
            "source_binding.runtime_timing_environment."
            "production_path_prewarm_contract:invalid"
        )
    fingerprint = (
        str(source.get("composite_sha256") or "")
        if isinstance(source, Mapping)
        else ""
    )
    binding = payload.get("prewarm_binding")
    if not isinstance(binding, Mapping):
        blockers.append("prewarm_binding:missing_or_not_object")
    else:
        expected = {
            "schema": PREWARM_SCHEMA,
            "source_composite_sha256": fingerprint,
            "execute_tick_count": PREWARM_EXECUTE_TICKS,
            "pacing_hz": PREWARM_CONTROL_HZ,
            "paced": True,
            "pass": True,
        }
        for field, value in expected.items():
            if binding.get(field) != value:
                blockers.append(f"prewarm_binding.{field}:invalid")
        if SHA256_RE.fullmatch(str(binding.get("sha256") or "")) is None:
            blockers.append("prewarm_binding.sha256:invalid")
        if not str(binding.get("path") or ""):
            blockers.append("prewarm_binding.path:invalid")
        size = binding.get("size_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            blockers.append("prewarm_binding.size_bytes:invalid")
    control_contract = payload.get("control_contract")
    if not isinstance(control_contract, Mapping):
        blockers.append("control_contract:missing")
    else:
        expected_measured_boundary = {
            "measured_samples_excluded": 0,
            "prewarm_samples_in_control_trace": 0,
            "measured_sequence_restarts_at_zero": True,
        }
        for field, value in expected_measured_boundary.items():
            if control_contract.get(field) != value:
                blockers.append(f"control_contract.{field}:invalid")
    return sorted(set(blockers))


def validate_phase_evidence(
    payload: Mapping[str, object],
    *,
    artifact_root: Path | None = None,
) -> list[str]:
    if payload.get("schema") == EVIDENCE_SCHEMA_V3:
        return validate_v3_evidence(payload, artifact_root=artifact_root)
    if payload.get("schema") == EVIDENCE_SCHEMA_V2:
        return validate_v2_evidence(payload, artifact_root=artifact_root)
    return validate_v1_evidence(payload, artifact_root=artifact_root)


def _validate_run_manifest_v2(
    payload: Mapping[str, object],
    *,
    root: Path,
    require_complete: bool = True,
) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema") != RUN_SCHEMA_V2:
        blockers.append("schema:invalid")
    fingerprint = str(payload.get("source_composite_sha256") or "")
    if SHA256_RE.fullmatch(fingerprint) is None:
        blockers.append("source_composite_sha256:invalid")
    phases = payload.get("phases")
    if not isinstance(phases, Sequence) or isinstance(phases, (str, bytes)):
        return sorted(set(blockers + ["phases:missing_or_not_array"]))
    expected = (
        P0_V8_CANARY_PHASES_S
        if require_complete
        else P0_V8_CANARY_PHASES_S[: len(phases)]
    )
    actual = tuple(
        float(row.get("duration_s", math.nan))
        if isinstance(row, Mapping)
        else math.nan
        for row in phases
    )
    if actual != expected:
        blockers.append("phases:not_canonical_2_10_60_sequence")
    control_rows: list[dict[str, object]] = []
    cycle_rows: list[dict[str, object]] = []
    for index, row in enumerate(phases):
        if not isinstance(row, Mapping):
            blockers.append(f"phases[{index}]:not_object")
            continue
        if row.get("sequence_index") != index:
            blockers.append(f"phases[{index}].sequence_index:mismatch")
        if row.get("structurally_valid") is not True:
            blockers.append(f"phases[{index}].structurally_valid:false")
        if row.get("validation_blockers") != []:
            blockers.append(f"phases[{index}].validation_blockers:not_empty")
        evidence_path = (root / str(row.get("evidence_path") or "")).resolve()
        try:
            evidence_path.relative_to(root.resolve())
        except ValueError:
            blockers.append(f"phases[{index}].evidence_path:escape")
            continue
        if not evidence_path.is_file():
            blockers.append(f"phases[{index}].evidence_path:missing")
            continue
        if evidence_path.stat().st_size != row.get("evidence_size_bytes"):
            blockers.append(f"phases[{index}].evidence_size:mismatch")
        if sha256_path(evidence_path) != row.get("evidence_sha256"):
            blockers.append(f"phases[{index}].evidence_sha256:mismatch")
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            blockers.append(
                f"phases[{index}].evidence:unreadable:{type(exc).__name__}"
            )
            continue
        blockers.extend(
            f"phases[{index}].{item}"
            for item in validate_phase_evidence(
                evidence,
                artifact_root=evidence_path.parent,
            )
        )
        source = evidence.get("source_binding")
        if not isinstance(source, Mapping) or source.get(
            "composite_sha256"
        ) != fingerprint:
            blockers.append(f"phases[{index}].fingerprint:mismatch")
        phase = evidence.get("phase")
        if not isinstance(phase, Mapping) or phase.get("duration_s") != actual[index]:
            blockers.append(f"phases[{index}].duration:mismatch")
        nominal = evidence.get("nominal")
        if not isinstance(nominal, Mapping) or nominal.get(
            "control_path_diagnostic_pass"
        ) is not True:
            blockers.append(f"phases[{index}].control_path_diagnostic_pass:false")
        control = evidence.get("control_hard_500hz")
        cycle = evidence.get("simulator_cycle_diagnostic")
        control_pass = control.get("pass") if isinstance(control, Mapping) else None
        control_misses = (
            control.get("deadline_miss_count")
            if isinstance(control, Mapping)
            else None
        )
        cycle_misses = (
            cycle.get("cycle_compute_deadline_miss_count")
            if isinstance(cycle, Mapping)
            else None
        )
        absolute_misses = (
            cycle.get("absolute_deadline_miss_count")
            if isinstance(cycle, Mapping)
            else None
        )
        cycle_meets = (
            cycle.get("meets_500hz_diagnostic")
            if isinstance(cycle, Mapping)
            else None
        )
        expected_row = {
            "control_hard_500hz_pass": control_pass,
            "control_deadline_miss_count": control_misses,
            "simulator_cycle_meets_500hz_diagnostic": cycle_meets,
            "cycle_compute_deadline_miss_count": cycle_misses,
            "absolute_deadline_miss_count": absolute_misses,
        }
        for field, value in expected_row.items():
            if row.get(field) != value:
                blockers.append(f"phases[{index}].{field}:mismatch")
        control_rows.append(
            {
                "duration_s": actual[index],
                "deadline_miss_count": control_misses,
                "pass": control_pass,
            }
        )
        cycle_rows.append(
            {
                "duration_s": actual[index],
                "cycle_compute_deadline_miss_count": cycle_misses,
                "absolute_deadline_miss_count": absolute_misses,
                "meets_500hz_diagnostic": cycle_meets,
            }
        )
        blockers.extend(
            f"phases[{index}].{item}"
            for item in _trace_blockers_v2(
                evidence,
                evidence_path.parent,
                require_timing_threshold=control_pass is True,
            )
        )
    complete = len(phases) == len(P0_V8_CANARY_PHASES_S)
    if require_complete and payload.get("canonical_phase_sequence_complete") is not True:
        blockers.append("canonical_phase_sequence_complete:false")
    expected_control_pass = bool(
        complete
        and control_rows
        and control_rows[-1]["duration_s"] == 60.0
        and control_rows[-1]["pass"] is True
    )
    expected_gate = {
        "scope": CONTROL_HARD_SCOPE,
        "required_phase_duration_s": 60.0,
        "deadline_ms": 2.0,
        "p99_limit_ms": 1.8,
        "requires_zero_deadline_misses": True,
        "requires_prefault": True,
        "phase_results": control_rows,
        "complete_sequence_evaluated": complete,
        "pass": expected_control_pass,
    }
    if payload.get("control_hard_500hz_gate") != expected_gate:
        blockers.append("control_hard_500hz_gate:verdict_mismatch")
    expected_cycle = {
        "scope": SIMULATOR_CYCLE_SCOPE,
        "diagnostic_only": True,
        "phase_results": cycle_rows,
    }
    if payload.get("simulator_cycle_diagnostic") != expected_cycle:
        blockers.append("simulator_cycle_diagnostic:verdict_mismatch")
    control_path_pass = all(
        isinstance(row, Mapping)
        and row.get("control_path_diagnostic_pass") is True
        for row in phases
    )
    if control_path_pass and complete and expected_control_pass:
        expected_result = "diagnostic_pass"
    elif control_path_pass and complete:
        expected_result = "control_diagnostic_pass_control_hard_500hz_blocked"
    elif control_path_pass:
        expected_result = "diagnostic_partial_pass"
    else:
        expected_result = "diagnostic_fail"
    if payload.get("result") != expected_result:
        blockers.append(f"result:expected_{expected_result}")
    if payload.get("claims") != {
        "p0_sim_physics_pass": False,
        "live_accepted": False,
        "reproduction_complete": False,
    }:
        blockers.append("claims:non_promotion_boundary_mismatch")
    if payload.get("claim_boundary") != simulation_claim_boundary():
        blockers.append("claim_boundary:non_promotion_boundary_mismatch")
    declared = payload.get("blockers")
    if not isinstance(declared, list):
        blockers.append("blockers:missing_or_not_array")
    elif expected_result == "control_diagnostic_pass_control_hard_500hz_blocked" and (
        "control_hard_500hz_gate_failed_60s" not in declared
    ):
        blockers.append("blockers:control_hard_500hz_gate_failed_60s_missing")
    return sorted(set(blockers))


def _validate_run_manifest_v3(
    payload: Mapping[str, object],
    *,
    root: Path,
    require_complete: bool = True,
) -> list[str]:
    compatibility = dict(payload)
    compatibility["schema"] = RUN_SCHEMA_V2
    compatibility.pop("production_path_prewarm", None)
    blockers = list(
        _validate_run_manifest_v2(
            compatibility,
            root=root,
            require_complete=require_complete,
        )
    )
    fingerprint = str(payload.get("source_composite_sha256") or "")
    prewarm_binding = payload.get("production_path_prewarm")
    prewarm_blockers, _prewarm_payload = validate_prewarm_binding(
        prewarm_binding,
        root=root,
        source_composite_sha256_value=fingerprint,
    )
    blockers.extend(prewarm_blockers)
    phases = payload.get("phases")
    if isinstance(prewarm_binding, Mapping) and isinstance(phases, Sequence):
        for index, row in enumerate(phases):
            if not isinstance(row, Mapping):
                continue
            evidence_path = (root / str(row.get("evidence_path") or "")).resolve()
            if not evidence_path.is_file():
                continue
            try:
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if evidence.get("prewarm_binding") != prewarm_binding:
                blockers.append(f"phases[{index}].prewarm_binding:mismatch")
    return sorted(set(blockers))


def validate_run_manifest(
    payload: Mapping[str, object],
    *,
    root: Path,
    require_complete: bool = True,
) -> list[str]:
    if payload.get("schema") == RUN_SCHEMA_V3:
        return _validate_run_manifest_v3(
            payload,
            root=root,
            require_complete=require_complete,
        )
    if payload.get("schema") == RUN_SCHEMA_V2:
        return _validate_run_manifest_v2(
            payload,
            root=root,
            require_complete=require_complete,
        )
    return _validate_run_manifest_v1(
        payload,
        root=root,
        require_complete=require_complete,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    blockers = validate_run_manifest(
        payload,
        root=manifest_path.parent,
        require_complete=not args.allow_partial,
    )
    result = {
        "schema": (
            "step5d_p0_v8_mujoco_verification_v3"
            if payload.get("schema") == RUN_SCHEMA_V3
            else (
                "step5d_p0_v8_mujoco_verification_v2"
                if payload.get("schema") == RUN_SCHEMA_V2
                else "step5d_p0_v8_mujoco_verification_v1"
            )
        ),
        "manifest": str(manifest_path),
        "valid": not blockers,
        "blockers": blockers,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not blockers else 3


if __name__ == "__main__":
    raise SystemExit(main())
