#!/usr/bin/env python3
"""Dependency-light verifier for UR10e simulation evidence and claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_simulator_adapter import (
    P0_V8_CANARY_PHASES_S,
    P0_V8_CONTROL_HZ,
    P0_V8_QDOT_CAP_RAD_S,
    SIMULATION_CLAIMS,
    assert_p0_v8_profile,
)


SCHEMA = "ur10e_simulation_evidence_v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
P0_REQUIRED_FAULTS = {
    "direction_unload",
    "wrong_frame",
    "normal_mismatch",
    "sequence_duplicate",
    "sequence_gap",
    "sequence_reordered",
    "watchdog_stale",
    "missing_observation",
    "missing_policy_output",
    "nan_state",
    "qdot_rail",
    "control_jitter",
    "packet_drop",
    "unexpected_contact",
    "cage_collision",
    "tcp_outside_cage",
    "force_guard",
    "torque_guard",
}
ZERO_COUNT_FIELDS = (
    "safe_hold_count",
    "stop_count",
    "missed_sequence_count",
    "nonfinite_output_count",
    "qdot_bound_violation_count",
    "unexpected_contact_count",
    "cage_collision_count",
    "exact_zero_rejection_count",
)


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_composite_sha256(source_binding: Mapping[str, object]) -> str:
    return canonical_sha256(
        {
            key: value
            for key, value in source_binding.items()
            if key != "composite_sha256"
        }
    )


def _number(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if value == parsed else None


def _mapping(payload: Mapping[str, object], field: str, blockers: list[str]) -> Mapping[str, object]:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        blockers.append(f"{field}:missing_or_not_object")
        return {}
    return value


def validate_evidence(
    payload: Mapping[str, object],
    *,
    artifact_root: Path | None = None,
) -> list[str]:
    blockers: list[str] = []
    if payload.get("schema") != SCHEMA:
        blockers.append("schema:invalid")

    profile = _mapping(payload, "profile", blockers)
    if profile.get("id") != "step5d_strict_rnn_no_contact_p0_v8":
        blockers.append("profile.id:invalid")
    try:
        assert_p0_v8_profile(profile)
    except ValueError as exc:
        blockers.append(f"profile:{exc}")

    engine = _mapping(payload, "engine", blockers)
    engine_name = str(engine.get("name") or "")
    provenance = str(engine.get("physics_provenance") or "")
    if engine.get("command_jacobian_source") != "calibrated_pinocchio":
        blockers.append("engine.command_jacobian_source:not_calibrated_pinocchio")
    if engine.get("engine_oracle_is_command_source") is not False:
        blockers.append("engine.engine_oracle_is_command_source:must_be_false")

    source = _mapping(payload, "source_binding", blockers)
    for field in ("model_sha256", "frame_lineage_sha256", "composite_sha256"):
        if SHA256_RE.fullmatch(str(source.get(field) or "")) is None:
            blockers.append(f"source_binding.{field}:invalid")
    if source and source.get("composite_sha256") != source_composite_sha256(source):
        blockers.append("source_binding.composite_sha256:mismatch")
    if not source.get("calibration_hash"):
        blockers.append("source_binding.calibration_hash:missing")

    schedule = _mapping(payload, "schedule", blockers)
    expected_schedule = {
        "physics_hz": 2_000,
        "control_hz": 500,
        "dbil_hz": 200,
        "integer_schedule": True,
        "sim_tick_miss_count": 0,
    }
    for field, expected in expected_schedule.items():
        if schedule.get(field) != expected:
            blockers.append(f"schedule.{field}:expected_{expected}")

    phase = _mapping(payload, "phase", blockers)
    duration = _number(phase.get("duration_s"))
    sequence_index = _integer(phase.get("sequence_index"))
    if duration not in P0_V8_CANARY_PHASES_S:
        blockers.append("phase.duration_s:not_2_10_60")
    else:
        expected_index = P0_V8_CANARY_PHASES_S.index(duration)
        if sequence_index != expected_index:
            blockers.append("phase.sequence_index:mismatch")
        expected_same = expected_index > 0
        if phase.get("same_fingerprint_as_previous") is not expected_same:
            blockers.append("phase.same_fingerprint_as_previous:mismatch")

    nominal = _mapping(payload, "nominal", blockers)
    tick_count = _integer(nominal.get("tick_count"))
    accepted_count = _integer(nominal.get("accepted_tick_count"))
    if duration is not None:
        expected_ticks = int(round(duration * P0_V8_CONTROL_HZ))
        if tick_count != expected_ticks:
            blockers.append(f"nominal.tick_count:expected_{expected_ticks}")
    if accepted_count != tick_count:
        blockers.append("nominal.accepted_tick_count:not_all_ticks")
    for field in ZERO_COUNT_FIELDS:
        if _integer(nominal.get(field)) != 0:
            blockers.append(f"nominal.{field}:must_be_zero")
    deadline_miss_count = _integer(nominal.get("deadline_miss_count"))
    if deadline_miss_count is None or deadline_miss_count < 0:
        blockers.append("nominal.deadline_miss_count:must_be_nonnegative_integer")
    max_qdot = _number(nominal.get("max_qdot_abs_rad_s"))
    if max_qdot is None or max_qdot > P0_V8_QDOT_CAP_RAD_S + 1e-12:
        blockers.append("nominal.max_qdot_abs_rad_s:invalid_or_over_cap")

    wall_value = payload.get("wall_timing")
    wall_required = (
        engine_name == "mujoco"
        and engine.get("lane") == "p0_v8_no_contact_air_motion"
    )
    if wall_value is None:
        if wall_required:
            blockers.append("wall_timing:missing")
    elif not isinstance(wall_value, Mapping):
        blockers.append("wall_timing:not_object")
    else:
        wall = wall_value
        if wall.get("scope") != "read_state_to_shared_control_to_four_physics_substeps":
            blockers.append("wall_timing.scope:invalid")
        if not isinstance(wall.get("paced"), bool):
            blockers.append("wall_timing.paced:not_boolean")
        if _integer(wall.get("samples")) != tick_count:
            blockers.append("wall_timing.samples:mismatch")
        deadline_ms = _number(wall.get("deadline_ms"))
        p99_limit_ms = _number(wall.get("p99_limit_ms"))
        if deadline_ms != 2.0:
            blockers.append("wall_timing.deadline_ms:expected_2")
        if p99_limit_ms != 1.80:
            blockers.append("wall_timing.p99_limit_ms:expected_1p80")
        timing_values = [
            _number(wall.get(field))
            for field in ("p50_ms", "p95_ms", "p99_ms", "max_ms")
        ]
        if any(value is None or value < 0.0 for value in timing_values):
            blockers.append("wall_timing.distribution:invalid")
        else:
            p50_ms, p95_ms, p99_ms, max_ms = timing_values
            assert None not in (p50_ms, p95_ms, p99_ms, max_ms)
            if not p50_ms <= p95_ms <= p99_ms <= max_ms:
                blockers.append("wall_timing.distribution:not_monotonic")
            expected_p99 = p99_ms <= 1.80
            expected_max = max_ms < 2.0
            if wall.get("p99_within_limit") is not expected_p99:
                blockers.append("wall_timing.p99_within_limit:mismatch")
            if wall.get("max_within_deadline") is not expected_max:
                blockers.append("wall_timing.max_within_deadline:mismatch")
            wall_misses = _integer(wall.get("deadline_miss_count"))
            if wall_misses is None or wall_misses < 0:
                blockers.append("wall_timing.deadline_miss_count:invalid")
            else:
                if wall_misses != deadline_miss_count:
                    blockers.append("wall_timing.deadline_miss_count:nominal_mismatch")
                if (wall_misses == 0) is not expected_max:
                    blockers.append("wall_timing.deadline_miss_count:max_inconsistent")
                expected_pass = (
                    wall.get("paced") is True
                    and wall_misses == 0
                    and expected_p99
                    and expected_max
                )
                if wall.get("pass") is not expected_pass:
                    blockers.append("wall_timing.pass:mismatch")

    if wall_required:
        runtime = source.get("runtime_timing_environment")
        if not isinstance(runtime, Mapping):
            blockers.append("source_binding.runtime_timing_environment:missing")
        else:
            if isinstance(wall_value, Mapping) and (
                runtime.get("paced_wall_clock") is not wall_value.get("paced")
            ):
                blockers.append(
                    "source_binding.runtime_timing_environment.paced_wall_clock:mismatch"
                )
            for field in ("process_affinity", "process_scheduler"):
                value = runtime.get(field)
                if not isinstance(value, Mapping) or not isinstance(
                    value.get("available"), bool
                ):
                    blockers.append(
                        f"source_binding.runtime_timing_environment.{field}:invalid"
                    )
            thread_environment = runtime.get("thread_environment")
            thread_names = (
                "OPENBLAS_NUM_THREADS",
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            )
            if not isinstance(thread_environment, Mapping) or any(
                name not in thread_environment
                or not isinstance(thread_environment.get(name), (str, type(None)))
                for name in thread_names
            ):
                blockers.append(
                    "source_binding.runtime_timing_environment.thread_environment:invalid"
                )
            versions = runtime.get("versions")
            if not isinstance(versions, Mapping) or any(
                not str(versions.get(name) or "")
                for name in ("python", "python_implementation", "numpy", "cupy", "mujoco")
            ):
                blockers.append(
                    "source_binding.runtime_timing_environment.versions:invalid"
                )
            capabilities = runtime.get("capabilities")
            if not isinstance(capabilities, Mapping) or any(
                capabilities.get(name) is not True
                for name in (
                    "busy_poll_completion",
                    "pinned_host_staging",
                    "dedicated_nonblocking_stream",
                )
            ):
                blockers.append(
                    "source_binding.runtime_timing_environment.capabilities:invalid"
                )

    faults = payload.get("faults")
    seen_faults: set[str] = set()
    if not isinstance(faults, Sequence) or isinstance(faults, (str, bytes)):
        blockers.append("faults:missing_or_not_array")
    else:
        for index, row in enumerate(faults):
            if not isinstance(row, Mapping):
                blockers.append(f"faults[{index}]:not_object")
                continue
            fault_id = str(row.get("id") or "")
            if not fault_id:
                blockers.append(f"faults[{index}].id:missing")
                continue
            if fault_id in seen_faults:
                blockers.append(f"faults:{fault_id}:duplicate")
            seen_faults.add(fault_id)
            if row.get("passed") is not True:
                blockers.append(f"faults:{fault_id}:not_passed")
            if row.get("exact_zero_command") is not True:
                blockers.append(f"faults:{fault_id}:not_exact_zero")
            if not str(row.get("observed_reason") or ""):
                blockers.append(f"faults:{fault_id}:reason_missing")
        for missing in sorted(P0_REQUIRED_FAULTS - seen_faults):
            blockers.append(f"faults:{missing}:missing")

    control = _mapping(payload, "control_contract", blockers)
    expected_path = (
        "SimulatorState->Step5dObservation->StrictRnnControlPolicy->"
        "step5d_v30_contract_pipeline->SafetyEnvelope->RegisterCommand->SimulationCommand"
    )
    if control.get("path") != expected_path:
        blockers.append("control_contract.path:invalid")
    for field in ("dls_shadow_only", "exact_zero_rejection", "same_production_code"):
        if control.get(field) is not True:
            blockers.append(f"control_contract.{field}:must_be_true")

    boundary = _mapping(payload, "claim_boundary", blockers)
    expected_boundary = {
        "workflow_state": "liveprep_blocked",
        "current_program": "step5d_strict_rnn_ablation_v29",
        "v30_active": False,
        "live_motion_authorized": False,
        "package_accepted": False,
        "live_accepted": False,
        "reproduction_complete": False,
        "sim_pass_cannot_promote_live_state": True,
    }
    for field, expected in expected_boundary.items():
        if boundary.get(field) != expected:
            blockers.append(f"claim_boundary.{field}:expected_{expected}")

    claims = _mapping(payload, "claims", blockers)
    for claim in SIMULATION_CLAIMS:
        if not isinstance(claims.get(claim), bool):
            blockers.append(f"claims.{claim}:not_boolean")
    p0_claim = claims.get("p0_sim_physics_pass") is True
    if p0_claim:
        if duration != 60.0:
            blockers.append("claims.p0_sim_physics_pass:requires_60s")
        if engine_name not in {"mujoco", "gazebo_fortress", "gazebo_harmonic"}:
            blockers.append("claims.p0_sim_physics_pass:wrong_engine")
        if provenance != "calibrated_physics":
            blockers.append("claims.p0_sim_physics_pass:requires_calibrated_physics")
    if claims.get("p0_ursim_protocol_pass") is True and engine_name != "ursim_5_11":
        blockers.append("claims.p0_ursim_protocol_pass:wrong_engine")
    if claims.get("direct_torque_ursim_software_pass") is True and engine_name != "ursim_5_23":
        blockers.append("claims.direct_torque_ursim_software_pass:wrong_engine")
    if claims.get("contact_sim_pass") is True:
        blockers.append("claims.contact_sim_pass:forbidden_in_no_contact_p0_artifact")
    if claims.get("v30_offline_ready") is True:
        blockers.append("claims.v30_offline_ready:requires_separate_readiness_aggregator")

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
        blockers.append("artifacts:missing_or_not_array")
    else:
        for index, artifact in enumerate(artifacts):
            if not isinstance(artifact, Mapping):
                blockers.append(f"artifacts[{index}]:not_object")
                continue
            relpath = str(artifact.get("path") or "")
            expected_hash = str(artifact.get("sha256") or "")
            expected_size = _integer(artifact.get("size_bytes"))
            if not relpath or SHA256_RE.fullmatch(expected_hash) is None or expected_size is None:
                blockers.append(f"artifacts[{index}]:binding_invalid")
                continue
            if artifact_root is not None:
                path = (artifact_root / relpath).resolve()
                try:
                    path.relative_to(artifact_root.resolve())
                except ValueError:
                    blockers.append(f"artifacts[{index}]:path_escape")
                    continue
                if not path.is_file():
                    blockers.append(f"artifacts[{index}]:missing")
                    continue
                if path.stat().st_size != expected_size:
                    blockers.append(f"artifacts[{index}]:size_mismatch")
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                    blockers.append(f"artifacts[{index}]:sha256_mismatch")

    declared = payload.get("blockers")
    if not isinstance(declared, list) or any(not isinstance(item, str) for item in declared):
        blockers.append("blockers:missing_or_not_string_array")
    return sorted(set(blockers))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.evidence.read_text(encoding="utf-8"))
    blockers = validate_evidence(payload, artifact_root=args.artifact_root)
    result = {
        "schema": "ur10e_simulation_evidence_verification_v1",
        "evidence": str(args.evidence),
        "valid": not blockers,
        "blockers": blockers,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not blockers else 3


if __name__ == "__main__":
    raise SystemExit(main())
