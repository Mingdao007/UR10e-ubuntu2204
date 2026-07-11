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


def _trace_blockers(evidence: Mapping[str, object], phase_dir: Path) -> list[str]:
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
    if compute.shape != (tick_count,) or not np.all(np.isfinite(compute)) or np.any(compute < 0.0):
        blockers.append("trace:compute_timing_invalid")
    elif int(np.count_nonzero(compute >= 2.0)) != int(nominal.get("deadline_miss_count") or 0):
        blockers.append("trace:deadline_count_mismatch")
    elif float(np.percentile(compute, 99)) > 1.80:
        blockers.append("trace:compute_p99_over_1p80ms")
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
    for index, row in enumerate(phases):
        if not isinstance(row, Mapping):
            blockers.append(f"phases[{index}]:not_object")
            continue
        if row.get("sequence_index") != index:
            blockers.append(f"phases[{index}].sequence_index:mismatch")
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
            for item in _trace_blockers(evidence, evidence_path.parent)
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
    expected_result = (
        "diagnostic_pass"
        if len(phases) == len(P0_V8_CANARY_PHASES_S)
        else "diagnostic_partial_pass"
    )
    if payload.get("result") != expected_result:
        blockers.append(f"result:expected_{expected_result}")
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
