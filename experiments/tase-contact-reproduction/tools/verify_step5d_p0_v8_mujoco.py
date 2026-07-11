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
    P0_V8_PHYSICS_HZ,
    P0_V8_QDOT_CAP_RAD_S,
    simulation_claim_boundary,
)
from verify_step5d_sim_evidence import validate_evidence


SCHEMA = "step5d_p0_v8_mujoco_run_v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trace_blockers(
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


def validate_run_manifest(
    payload: Mapping[str, object],
    *,
    root: Path,
    require_complete: bool = True,
) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema") != SCHEMA:
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
        for item in validate_evidence(evidence, artifact_root=evidence_path.parent):
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
        "schema": "step5d_p0_v8_mujoco_verification_v1",
        "manifest": str(manifest_path),
        "valid": not blockers,
        "blockers": blockers,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not blockers else 3


if __name__ == "__main__":
    raise SystemExit(main())
