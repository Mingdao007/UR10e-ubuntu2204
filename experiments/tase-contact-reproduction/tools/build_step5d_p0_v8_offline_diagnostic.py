#!/usr/bin/env python3
"""Build a fail-closed P0 v8 offline diagnostic without promoting live state."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Mapping, Sequence

from verify_step5d_sim_evidence import P0_REQUIRED_FAULTS


SCHEMA = "step5d_p0_v8_offline_simulation_diagnostic_v1"
STATE_SCHEMA = "step5d_p0_v8_offline_simulation_state_binding_v1"
EVIDENCE_SCHEMA_V1 = "ur10e_simulation_evidence_v1"
EVIDENCE_SCHEMA_V2 = "ur10e_simulation_evidence_v2"
EVIDENCE_SCHEMA_V3 = "ur10e_simulation_evidence_v3"
RUN_SCHEMA_V1 = "step5d_p0_v8_mujoco_run_v1"
RUN_SCHEMA_V2 = "step5d_p0_v8_mujoco_run_v2"
RUN_SCHEMA_V3 = "step5d_p0_v8_mujoco_run_v3"
TIMING_SCOPE_VERSION_V2 = "p0_v8_timing_lane_split_v2"
CONTROL_SCOPE_V2 = "simulator_state_ready_to_adapter_step_complete"
SIMULATOR_SCOPE_V2 = (
    "release_to_oracle_snapshot_to_adapter_step_to_command_apply_and_four_physics_substeps"
)
PREFAULT_STRATEGY_V2 = "numpy_fill_zero_before_gc_collect_and_measured_loop"
NUMERIC_THREAD_ENV_CONTRACT_V2 = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}
CURRENT_TIMING_SCOPE_STATUS = "current_control_hard_500hz_measurement_scope"
HISTORICAL_TIMING_SCOPE_STATUS = "historical_superseded_measurement_scope"
INVALID_TIMING_SCOPE_STATUS = "invalid_or_mixed_measurement_scope"
P0_PROFILE = "step5d_strict_rnn_no_contact_p0_v8"
V29_PROGRAM = "step5d_strict_rnn_ablation_v29"
V30_PROGRAM = "step5d_strict_rnn_ablation_v30"
PHASES_S = (2.0, 10.0, 60.0)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ZERO_COUNTERS = (
    "safe_hold_count",
    "stop_count",
    "missed_sequence_count",
    "nonfinite_output_count",
    "qdot_bound_violation_count",
    "unexpected_contact_count",
    "cage_collision_count",
)
FALSE_CLAIMS = {
    "p0_sim_physics_pass": False,
    "p0_v8_passed": False,
    "no_contact_p0_accepted": False,
    "v30_offline_ready": False,
    "package_accepted": False,
    "controller_readback_verified": False,
    "live_motion_authorized": False,
    "live_accepted": False,
    "reproduction_complete": False,
}


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _state_core() -> dict[str, object]:
    return {
        "workflow_state": "liveprep_blocked",
        "current_program": V29_PROGRAM,
        "v29_frozen_fallback": True,
        "v30_active": False,
        "evidence_frozen": False,
        "controller_readback_verified": False,
        "controller_canaries": {"completed": [], "p0_v8_passed": False},
        "claims": dict(FALSE_CLAIMS),
    }


def unbound_state_binding() -> dict[str, object]:
    return {
        "schema": STATE_SCHEMA,
        "status": "awaiting_final_canonical_artifact",
        "planned_summary_artifact": (
            "config/step5d_p0_v8_offline_simulation_diagnostic.json"
        ),
        "summary_artifact": None,
        "summary_sha256": None,
        **_state_core(),
        "blockers": [
            "final_canonical_simulator_artifact_not_bound",
            "controller_canaries_not_run",
            "controller_readback_missing",
            "evidence_not_frozen",
        ],
    }


def _timing_scope_classification(
    manifest: Mapping[str, object],
    phase_evidence: Sequence[Mapping[str, object]],
) -> tuple[str, list[str]]:
    run_schema = manifest.get("schema")
    evidence_schemas = tuple(row.get("schema") for row in phase_evidence)
    if run_schema == RUN_SCHEMA_V1 and evidence_schemas and all(
        schema == EVIDENCE_SCHEMA_V1 for schema in evidence_schemas
    ):
        return HISTORICAL_TIMING_SCOPE_STATUS, []
    current_schema_pair = (
        (RUN_SCHEMA_V2, EVIDENCE_SCHEMA_V2),
        (RUN_SCHEMA_V3, EVIDENCE_SCHEMA_V3),
    )
    if not evidence_schemas or not any(
        run_schema == expected_run
        and all(schema == expected_evidence for schema in evidence_schemas)
        for expected_run, expected_evidence in current_schema_pair
    ):
        return INVALID_TIMING_SCOPE_STATUS, ["timing_scope_schema_binding:invalid_or_mixed"]

    failures: list[str] = []
    expected_contract = {
        "version": TIMING_SCOPE_VERSION_V2,
        "control_hard_500hz": CONTROL_SCOPE_V2,
        "simulator_cycle_diagnostic": SIMULATOR_SCOPE_V2,
    }
    for index, evidence in enumerate(phase_evidence):
        source = evidence.get("source_binding")
        runtime = source.get("runtime_timing_environment") if isinstance(source, Mapping) else None
        prefault = runtime.get("trace_prefault") if isinstance(runtime, Mapping) else None
        timing_contract = (
            runtime.get("timing_scope_contract") if isinstance(runtime, Mapping) else None
        )
        thread_environment = (
            runtime.get("thread_environment") if isinstance(runtime, Mapping) else None
        )
        control_contract = evidence.get("control_contract")
        control_timing = evidence.get("control_hard_500hz")
        simulator_timing = evidence.get("simulator_cycle_diagnostic")
        if not isinstance(prefault, Mapping) or any(
            prefault.get(key) != expected
            for key, expected in {
                "required": True,
                "completed": True,
                "strategy": PREFAULT_STRATEGY_V2,
            }.items()
        ):
            failures.append(f"phases[{index}].trace_prefault_binding:invalid")
        if not isinstance(timing_contract, Mapping) or any(
            timing_contract.get(key) != expected
            for key, expected in expected_contract.items()
        ):
            failures.append(f"phases[{index}].timing_scope_contract:invalid")
        if thread_environment != NUMERIC_THREAD_ENV_CONTRACT_V2:
            failures.append(f"phases[{index}].numeric_thread_environment:invalid")
        if not isinstance(control_contract, Mapping) or (
            control_contract.get("timing_scope_version") != TIMING_SCOPE_VERSION_V2
            or control_contract.get("trace_buffers_prefaulted") is not True
        ):
            failures.append(f"phases[{index}].control_contract_timing_binding:invalid")
        if not isinstance(control_timing, Mapping) or (
            control_timing.get("scope") != CONTROL_SCOPE_V2
            or control_timing.get("prefault_required") is not True
            or control_timing.get("prefault_verified") is not True
        ):
            failures.append(f"phases[{index}].control_hard_500hz_binding:invalid")
        if not isinstance(simulator_timing, Mapping) or (
            simulator_timing.get("scope") != SIMULATOR_SCOPE_V2
            or simulator_timing.get("diagnostic_only") is not True
        ):
            failures.append(f"phases[{index}].simulator_cycle_binding:invalid")
    if run_schema == RUN_SCHEMA_V3:
        from verify_step5d_p0_v8_mujoco import expected_prewarm_contract

        prewarm = manifest.get("production_path_prewarm")
        expected_prewarm_binding = {
            "schema": "step5d_p0_v8_production_path_prewarm_v1",
            "source_composite_sha256": manifest.get("source_composite_sha256"),
            "execute_tick_count": 1_000,
            "pacing_hz": 500,
            "paced": True,
            "pass": True,
        }
        if not isinstance(prewarm, Mapping) or any(
            prewarm.get(field) != expected
            for field, expected in expected_prewarm_binding.items()
        ):
            failures.append("production_path_prewarm.binding:invalid")
        for index, evidence in enumerate(phase_evidence):
            source = evidence.get("source_binding")
            runtime = (
                source.get("runtime_timing_environment")
                if isinstance(source, Mapping)
                else None
            )
            control_contract = evidence.get("control_contract")
            if evidence.get("prewarm_binding") != prewarm:
                failures.append(f"phases[{index}].prewarm_binding:mismatch")
            if not isinstance(runtime, Mapping) or runtime.get(
                "production_path_prewarm_contract"
            ) != expected_prewarm_contract():
                failures.append(f"phases[{index}].prewarm_contract:invalid")
            if not isinstance(control_contract, Mapping) or any(
                control_contract.get(field) != expected
                for field, expected in {
                    "measured_samples_excluded": 0,
                    "prewarm_samples_in_control_trace": 0,
                    "measured_sequence_restarts_at_zero": True,
                }.items()
            ):
                failures.append(f"phases[{index}].measured_trace_boundary:invalid")
    gate = manifest.get("control_hard_500hz_gate")
    if not isinstance(gate, Mapping) or (
        gate.get("scope") != CONTROL_SCOPE_V2
        or gate.get("requires_prefault") is not True
    ):
        failures.append("control_hard_500hz_gate.binding:invalid")
    simulator_gate = manifest.get("simulator_cycle_diagnostic")
    if not isinstance(simulator_gate, Mapping) or (
        simulator_gate.get("scope") != SIMULATOR_SCOPE_V2
        or simulator_gate.get("diagnostic_only") is not True
    ):
        failures.append("simulator_cycle_diagnostic.binding:invalid")
    return (
        CURRENT_TIMING_SCOPE_STATUS if not failures else INVALID_TIMING_SCOPE_STATUS,
        failures,
    )


def _phase_summary(
    manifest_row: Mapping[str, object],
    evidence: Mapping[str, object],
) -> dict[str, object]:
    nominal = evidence.get("nominal")
    wall = evidence.get("wall_timing")
    control_hard = evidence.get("control_hard_500hz")
    simulator_cycle = evidence.get("simulator_cycle_diagnostic")
    source = evidence.get("source_binding")
    control_contract = evidence.get("control_contract")
    faults = evidence.get("faults")
    nominal = nominal if isinstance(nominal, Mapping) else {}
    wall = wall if isinstance(wall, Mapping) else {}
    control_hard = control_hard if isinstance(control_hard, Mapping) else {}
    simulator_cycle = simulator_cycle if isinstance(simulator_cycle, Mapping) else {}
    source = source if isinstance(source, Mapping) else {}
    control_contract = control_contract if isinstance(control_contract, Mapping) else {}
    runtime = source.get("runtime_timing_environment")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    fault_rows = (
        [row for row in faults if isinstance(row, Mapping)]
        if isinstance(faults, Sequence) and not isinstance(faults, (str, bytes))
        else []
    )
    fault_ids = {str(row.get("id") or "") for row in fault_rows}
    exact_zero = fault_ids == P0_REQUIRED_FAULTS and all(
        row.get("passed") is True
        and row.get("exact_zero_command") is True
        and row.get("command_qdot") == [0.0] * 6
        for row in fault_rows
    )
    return {
        "evidence_schema": evidence.get("schema"),
        "duration_s": manifest_row.get("duration_s"),
        "sequence_index": manifest_row.get("sequence_index"),
        "source_composite_sha256": source.get("composite_sha256"),
        "prewarm_binding": (
            dict(evidence["prewarm_binding"])
            if isinstance(evidence.get("prewarm_binding"), Mapping)
            else None
        ),
        "evidence": {
            "path": manifest_row.get("evidence_path"),
            "sha256": manifest_row.get("evidence_sha256"),
            "size_bytes": manifest_row.get("evidence_size_bytes"),
        },
        "structurally_valid": manifest_row.get("structurally_valid") is True,
        "validation_blockers": list(manifest_row.get("validation_blockers") or []),
        "control_path_diagnostic_pass": (
            nominal.get("control_path_diagnostic_pass") is True
        ),
        "control_counters": {
            key: nominal.get(key)
            for key in (
                "tick_count",
                "accepted_tick_count",
                *ZERO_COUNTERS,
                "max_qdot_abs_rad_s",
                "control_deadline_miss_count",
                "cycle_compute_deadline_miss_count",
                "absolute_deadline_miss_count",
            )
        },
        "timing_scope_binding": {
            "trace_prefault": (
                dict(runtime["trace_prefault"])
                if isinstance(runtime.get("trace_prefault"), Mapping)
                else None
            ),
            "timing_scope_contract": (
                dict(runtime["timing_scope_contract"])
                if isinstance(runtime.get("timing_scope_contract"), Mapping)
                else None
            ),
            "numeric_thread_environment": (
                dict(runtime["thread_environment"])
                if isinstance(runtime.get("thread_environment"), Mapping)
                else None
            ),
            "timing_scope_version": control_contract.get("timing_scope_version"),
            "trace_buffers_prefaulted": control_contract.get("trace_buffers_prefaulted"),
            "production_path_prewarm_contract": (
                dict(runtime["production_path_prewarm_contract"])
                if isinstance(
                    runtime.get("production_path_prewarm_contract"), Mapping
                )
                else None
            ),
            "measured_samples_excluded": control_contract.get(
                "measured_samples_excluded"
            ),
            "prewarm_samples_in_control_trace": control_contract.get(
                "prewarm_samples_in_control_trace"
            ),
            "measured_sequence_restarts_at_zero": control_contract.get(
                "measured_sequence_restarts_at_zero"
            ),
        },
        "control_hard_500hz": dict(control_hard),
        "simulator_cycle_diagnostic": dict(simulator_cycle),
        "wall_timing": dict(wall),
        "fault_injection": {
            "required_fault_ids": sorted(P0_REQUIRED_FAULTS),
            "observed_fault_ids": sorted(fault_ids),
            "fault_count": len(fault_rows),
            "all_required_faults_exact_zero": exact_zero,
        },
    }


def build_diagnostic(
    manifest: Mapping[str, object],
    phase_evidence: Sequence[Mapping[str, object]],
    *,
    run_manifest_binding: Mapping[str, object],
    source_host: str,
    model_manifest_binding: Mapping[str, object] | None = None,
) -> dict[str, object]:
    rows_value = manifest.get("phases")
    rows = (
        list(rows_value)
        if isinstance(rows_value, Sequence) and not isinstance(rows_value, (str, bytes))
        else []
    )
    if len(rows) != len(phase_evidence) or not all(
        isinstance(row, Mapping) for row in rows
    ):
        raise ValueError("run manifest and phase evidence lengths do not match")
    phases = [
        _phase_summary(row, evidence)
        for row, evidence in zip(rows, phase_evidence)
    ]
    first_source = phase_evidence[0].get("source_binding") if phase_evidence else {}
    first_engine = phase_evidence[0].get("engine") if phase_evidence else {}
    first_source = first_source if isinstance(first_source, Mapping) else {}
    first_engine = first_engine if isinstance(first_engine, Mapping) else {}
    timing_scope_status, timing_scope_failures = _timing_scope_classification(
        manifest, phase_evidence
    )
    historical_timing = manifest.get("timing_gate")
    historical_timing_pass = (
        isinstance(historical_timing, Mapping)
        and historical_timing.get("pass") is True
    )
    control_hard_gate = manifest.get("control_hard_500hz_gate")
    current_control_timing_pass = (
        timing_scope_status == CURRENT_TIMING_SCOPE_STATUS
        and isinstance(control_hard_gate, Mapping)
        and control_hard_gate.get("pass") is True
    )
    control_pass = bool(phases) and all(
        row["control_path_diagnostic_pass"] is True for row in phases
    )
    fault_pass = bool(phases) and all(
        row["fault_injection"]["all_required_faults_exact_zero"] is True
        for row in phases
    )

    blockers = {str(item) for item in (manifest.get("blockers") or [])}
    blockers.update(
        {
            "offline_simulation_cannot_promote_live_state",
            "controller_canaries_not_run",
            "controller_readback_missing",
            "evidence_not_frozen",
        }
    )
    blockers.update(timing_scope_failures)
    if timing_scope_status == HISTORICAL_TIMING_SCOPE_STATUS:
        blockers.add("current_control_timing_evidence_missing")
    elif not current_control_timing_pass:
        blockers.add("offline_control_timing_failed")
    if first_engine.get("physics_provenance") != "calibrated_physics":
        blockers.add("geometry_provisional_no_p0_physics_claim")
    if not control_pass:
        blockers.add("offline_control_path_diagnostic_failed")
    if not fault_pass:
        blockers.add("offline_fault_exact_zero_gate_failed")
    if model_manifest_binding is None:
        blockers.add("model_bundle_manifest_not_bound")

    payload: dict[str, object] = {
        "schema": SCHEMA,
        "generated_at": manifest.get("generated_at"),
        "profile": P0_PROFILE,
        "source_binding": {
            "host": source_host,
            "source_composite_sha256": manifest.get("source_composite_sha256"),
            "base_commit": first_source.get("base_commit"),
            "head_commit": first_source.get("head_commit"),
            "frame_lineage_sha256": first_source.get("frame_lineage_sha256"),
        },
        "model_binding": {
            "engine": first_engine.get("name"),
            "engine_version": first_engine.get("version"),
            "physics_provenance": first_engine.get("physics_provenance"),
            "model_sha256": first_source.get("model_sha256"),
            "model_manifest": (
                dict(model_manifest_binding)
                if model_manifest_binding is not None
                else None
            ),
        },
        "run_binding": dict(run_manifest_binding),
        "timing_evidence": {
            "timing_scope_status": timing_scope_status,
            "run_manifest_schema": manifest.get("schema"),
            "phase_evidence_schemas": [
                evidence.get("schema") for evidence in phase_evidence
            ],
            "production_path_prewarm": (
                dict(manifest["production_path_prewarm"])
                if isinstance(manifest.get("production_path_prewarm"), Mapping)
                else None
            ),
            "control_hard_500hz_gate": (
                dict(control_hard_gate)
                if isinstance(control_hard_gate, Mapping)
                else None
            ),
            "simulator_cycle_diagnostic": (
                dict(manifest["simulator_cycle_diagnostic"])
                if isinstance(manifest.get("simulator_cycle_diagnostic"), Mapping)
                else None
            ),
            "historical_combined_scope_timing_gate": (
                dict(historical_timing)
                if isinstance(historical_timing, Mapping)
                else None
            ),
        },
        "diagnostic": {
            "canonical_phase_sequence_complete": (
                manifest.get("canonical_phase_sequence_complete") is True
            ),
            "source_manifest_structurally_valid": True,
            "source_result": manifest.get("result"),
            "all_control_paths_diagnostic_pass": control_pass,
            "all_required_faults_exact_zero": fault_pass,
            "timing_scope_status": timing_scope_status,
            "offline_control_timing_pass": current_control_timing_pass,
            "historical_combined_scope_timing_pass": historical_timing_pass,
        },
        "phases": phases,
        "state_projection": {
            **{key: value for key, value in _state_core().items() if key != "claims"},
            "v29_current": True,
            "v30_program": V30_PROGRAM,
            "v30_state": "inactive_offline_candidate",
        },
        "claims": dict(FALSE_CLAIMS),
        "blockers": sorted(blockers),
    }
    payload["diagnostic_sha256"] = canonical_sha256(payload)
    return payload


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if value == parsed else None


def _finite(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _sha(value: object) -> bool:
    return SHA256_RE.fullmatch(str(value or "")) is not None


def _binding_blockers(
    value: object,
    label: str,
    *,
    required: bool = True,
) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, Mapping):
        return [f"{label}:invalid"]
    failures = []
    if not str(value.get("path") or ""):
        failures.append(f"{label}.path:invalid")
    if not _sha(value.get("sha256")):
        failures.append(f"{label}.sha256:invalid")
    if (_int(value.get("size_bytes")) or 0) <= 0:
        failures.append(f"{label}.size_bytes:invalid")
    return failures


def validate_diagnostic(payload: Mapping[str, object]) -> list[str]:
    """Verify the thin projection; the source run remains owned by its verifier."""

    failures: list[str] = []
    if payload.get("schema") != SCHEMA or payload.get("profile") != P0_PROFILE:
        failures.append("schema_or_profile:invalid")
    unhashed = {key: value for key, value in payload.items() if key != "diagnostic_sha256"}
    if payload.get("diagnostic_sha256") != canonical_sha256(unhashed):
        failures.append("diagnostic_sha256:mismatch")
    source = payload.get("source_binding")
    model = payload.get("model_binding")
    if not isinstance(source, Mapping):
        failures.append("source_binding:invalid")
        source = {}
    if not isinstance(model, Mapping):
        failures.append("model_binding:invalid")
        model = {}
    for field in ("source_composite_sha256", "frame_lineage_sha256"):
        if not _sha(source.get(field)):
            failures.append(f"source_binding.{field}:invalid")
    if not _sha(model.get("model_sha256")):
        failures.append("model_binding.model_sha256:invalid")
    failures.extend(_binding_blockers(payload.get("run_binding"), "run_binding"))
    failures.extend(
        _binding_blockers(
            model.get("model_manifest"),
            "model_binding.model_manifest",
            required=False,
        )
    )
    timing_evidence = payload.get("timing_evidence")
    if not isinstance(timing_evidence, Mapping):
        failures.append("timing_evidence:invalid")
        timing_evidence = {}
    timing_scope_status = timing_evidence.get("timing_scope_status")
    if timing_scope_status not in {
        CURRENT_TIMING_SCOPE_STATUS,
        HISTORICAL_TIMING_SCOPE_STATUS,
        INVALID_TIMING_SCOPE_STATUS,
    }:
        failures.append("timing_evidence.timing_scope_status:invalid")
    run_schema = timing_evidence.get("run_manifest_schema")
    evidence_schemas = timing_evidence.get("phase_evidence_schemas")
    if timing_scope_status == CURRENT_TIMING_SCOPE_STATUS:
        valid_current_schema = (
            run_schema == RUN_SCHEMA_V2
            and evidence_schemas == [EVIDENCE_SCHEMA_V2] * 3
        ) or (
            run_schema == RUN_SCHEMA_V3
            and evidence_schemas == [EVIDENCE_SCHEMA_V3] * 3
        )
        if not valid_current_schema:
            failures.append("timing_evidence.current_schema_binding:invalid")
    elif timing_scope_status == HISTORICAL_TIMING_SCOPE_STATUS:
        if run_schema != RUN_SCHEMA_V1 or evidence_schemas != [EVIDENCE_SCHEMA_V1] * 3:
            failures.append("timing_evidence.historical_schema_binding:invalid")
    else:
        failures.append("timing_evidence.current_scope_binding:failed")
    projected_prewarm = timing_evidence.get("production_path_prewarm")
    if run_schema == RUN_SCHEMA_V3:
        if not isinstance(projected_prewarm, Mapping):
            failures.append("timing_evidence.production_path_prewarm:missing")
            projected_prewarm = {}
        failures.extend(
            _binding_blockers(
                projected_prewarm,
                "timing_evidence.production_path_prewarm",
            )
        )
        for field, expected in {
            "schema": "step5d_p0_v8_production_path_prewarm_v1",
            "source_composite_sha256": source.get("source_composite_sha256"),
            "execute_tick_count": 1_000,
            "pacing_hz": 500,
            "paced": True,
            "pass": True,
        }.items():
            if projected_prewarm.get(field) != expected:
                failures.append(
                    f"timing_evidence.production_path_prewarm.{field}:invalid"
                )
    elif projected_prewarm is not None:
        failures.append("timing_evidence.production_path_prewarm:unexpected")

    phases_value = payload.get("phases")
    phases = (
        list(phases_value)
        if isinstance(phases_value, Sequence) and not isinstance(phases_value, (str, bytes))
        else []
    )
    durations = tuple(
        _finite(row.get("duration_s")) if isinstance(row, Mapping) else None
        for row in phases
    )
    if durations != PHASES_S:
        failures.append("phases:not_canonical_2_10_60")
    phase_control_results: list[dict[str, object]] = []
    phase_simulator_results: list[dict[str, object]] = []
    for index, row in enumerate(phases):
        if not isinstance(row, Mapping):
            failures.append(f"phases[{index}]:invalid")
            continue
        if row.get("sequence_index") != index:
            failures.append(f"phases[{index}].sequence_index:mismatch")
        expected_evidence_schema = (
            EVIDENCE_SCHEMA_V3
            if run_schema == RUN_SCHEMA_V3
            else EVIDENCE_SCHEMA_V2
            if timing_scope_status == CURRENT_TIMING_SCOPE_STATUS
            else EVIDENCE_SCHEMA_V1
        )
        if row.get("evidence_schema") != expected_evidence_schema:
            failures.append(f"phases[{index}].evidence_schema:mismatch")
        if run_schema == RUN_SCHEMA_V3 and row.get("prewarm_binding") != projected_prewarm:
            failures.append(f"phases[{index}].prewarm_binding:mismatch")
        if row.get("structurally_valid") is not True or row.get("validation_blockers") != []:
            failures.append(f"phases[{index}].structural_validation:failed")
        failures.extend(_binding_blockers(row.get("evidence"), f"phases[{index}].evidence"))
        counters = row.get("control_counters")
        faults = row.get("fault_injection")
        expected_ticks = int(PHASES_S[index] * 500)
        if not _sha(row.get("source_composite_sha256")) or (
            row.get("source_composite_sha256") != source.get("source_composite_sha256")
        ):
            failures.append(f"phases[{index}].source_composite_sha256:mismatch")
        if not isinstance(counters, Mapping):
            failures.append(f"phases[{index}].control_counters:invalid")
        else:
            if counters.get("tick_count") != expected_ticks or counters.get("accepted_tick_count") != expected_ticks:
                failures.append(f"phases[{index}].accepted_ticks:mismatch")
            if any(counters.get(field) != 0 for field in ZERO_COUNTERS):
                failures.append(f"phases[{index}].control_counter:nonzero")
            max_qdot = _finite(counters.get("max_qdot_abs_rad_s"))
            if max_qdot is None or max_qdot > 0.05 + 1e-12:
                failures.append(f"phases[{index}].max_qdot:invalid")
        if timing_scope_status == CURRENT_TIMING_SCOPE_STATUS:
            binding = row.get("timing_scope_binding")
            expected_prefault = {
                "required": True,
                "completed": True,
                "strategy": PREFAULT_STRATEGY_V2,
            }
            expected_contract = {
                "version": TIMING_SCOPE_VERSION_V2,
                "control_hard_500hz": CONTROL_SCOPE_V2,
                "simulator_cycle_diagnostic": SIMULATOR_SCOPE_V2,
            }
            if not isinstance(binding, Mapping) or (
                binding.get("trace_prefault") != expected_prefault
                or binding.get("timing_scope_contract") != expected_contract
                or binding.get("numeric_thread_environment")
                != NUMERIC_THREAD_ENV_CONTRACT_V2
                or binding.get("timing_scope_version") != TIMING_SCOPE_VERSION_V2
                or binding.get("trace_buffers_prefaulted") is not True
            ):
                failures.append(f"phases[{index}].timing_scope_binding:invalid")
            if run_schema == RUN_SCHEMA_V3:
                from verify_step5d_p0_v8_mujoco import expected_prewarm_contract

                if not isinstance(binding, Mapping) or (
                    binding.get("production_path_prewarm_contract")
                    != expected_prewarm_contract()
                    or binding.get("measured_samples_excluded") != 0
                    or binding.get("prewarm_samples_in_control_trace") != 0
                    or binding.get("measured_sequence_restarts_at_zero") is not True
                ):
                    failures.append(
                        f"phases[{index}].prewarm_trace_boundary:invalid"
                    )
            control_timing = row.get("control_hard_500hz")
            if not isinstance(control_timing, Mapping):
                failures.append(f"phases[{index}].control_hard_500hz:invalid")
            else:
                miss_count = _int(control_timing.get("deadline_miss_count"))
                values = tuple(
                    _finite(control_timing.get(field))
                    for field in ("p50_ms", "p95_ms", "p99_ms", "max_ms")
                )
                monotonic = (
                    all(value is not None and value >= 0.0 for value in values)
                    and values[0] <= values[1] <= values[2] <= values[3]
                )
                expected_pass = bool(
                    control_timing.get("paced") is True
                    and miss_count == 0
                    and monotonic
                    and values[2] <= 1.8
                    and values[3] < 2.0
                    and control_timing.get("prefault_required") is True
                    and control_timing.get("prefault_verified") is True
                )
                if (
                    control_timing.get("scope") != CONTROL_SCOPE_V2
                    or control_timing.get("samples") != expected_ticks
                    or control_timing.get("deadline_ms") != 2.0
                    or control_timing.get("p99_limit_ms") != 1.8
                    or miss_count is None
                    or miss_count < 0
                    or not monotonic
                    or control_timing.get("p99_within_limit") is not (
                        monotonic and values[2] <= 1.8
                    )
                    or control_timing.get("max_within_deadline") is not (
                        monotonic and values[3] < 2.0
                    )
                    or control_timing.get("pass") is not expected_pass
                    or not isinstance(counters, Mapping)
                    or counters.get("control_deadline_miss_count") != miss_count
                ):
                    failures.append(f"phases[{index}].control_hard_500hz:inconsistent")
                phase_control_results.append(
                    {
                        "duration_s": PHASES_S[index],
                        "deadline_miss_count": miss_count,
                        "pass": control_timing.get("pass"),
                    }
                )
            simulator_timing = row.get("simulator_cycle_diagnostic")
            if not isinstance(simulator_timing, Mapping):
                failures.append(f"phases[{index}].simulator_cycle_diagnostic:invalid")
            else:
                cycle_misses = _int(
                    simulator_timing.get("cycle_compute_deadline_miss_count")
                )
                absolute_misses = _int(
                    simulator_timing.get("absolute_deadline_miss_count")
                )
                distributions_valid = True
                for field in (
                    "oracle_snapshot_ms",
                    "command_apply_and_physics_ms",
                    "cycle_wall_ms",
                    "release_lateness_ms",
                    "absolute_finish_lateness_ms",
                ):
                    value = simulator_timing.get(field)
                    parsed = (
                        tuple(_finite(value.get(name)) for name in ("p50_ms", "p95_ms", "p99_ms", "max_ms"))
                        if isinstance(value, Mapping)
                        else ()
                    )
                    if (
                        len(parsed) != 4
                        or any(item is None or item < 0.0 for item in parsed)
                        or not parsed[0] <= parsed[1] <= parsed[2] <= parsed[3]
                    ):
                        distributions_valid = False
                if (
                    simulator_timing.get("scope") != SIMULATOR_SCOPE_V2
                    or simulator_timing.get("diagnostic_only") is not True
                    or simulator_timing.get("samples") != expected_ticks
                    or simulator_timing.get("physics_substeps_per_control_tick") != 4
                    or cycle_misses is None
                    or cycle_misses < 0
                    or absolute_misses is None
                    or absolute_misses < 0
                    or not isinstance(simulator_timing.get("meets_500hz_diagnostic"), bool)
                    or simulator_timing.get("meets_500hz_diagnostic")
                    is not (cycle_misses == 0 and absolute_misses == 0)
                    or not distributions_valid
                    or not isinstance(counters, Mapping)
                    or counters.get("cycle_compute_deadline_miss_count") != cycle_misses
                    or counters.get("absolute_deadline_miss_count") != absolute_misses
                ):
                    failures.append(f"phases[{index}].simulator_cycle_diagnostic:inconsistent")
                phase_simulator_results.append(
                    {
                        "duration_s": PHASES_S[index],
                        "cycle_compute_deadline_miss_count": cycle_misses,
                        "absolute_deadline_miss_count": absolute_misses,
                        "meets_500hz_diagnostic": simulator_timing.get(
                            "meets_500hz_diagnostic"
                        ),
                    }
                )
        else:
            timing = row.get("wall_timing")
            if not isinstance(timing, Mapping):
                failures.append(f"phases[{index}].wall_timing:invalid")
            else:
                compute_misses = _int(timing.get("compute_deadline_miss_count"))
                absolute_misses = _int(timing.get("absolute_deadline_miss_count"))
                p99_ms = _finite(timing.get("p99_ms"))
                max_ms = _finite(timing.get("max_ms"))
                expected_pass = (
                    timing.get("paced") is True
                    and compute_misses == 0
                    and absolute_misses == 0
                    and p99_ms is not None and p99_ms <= 1.8
                    and max_ms is not None and max_ms < 2.0
                )
                if (
                    timing.get("deadline_accounting")
                    != "compute_elapsed_and_absolute_release_deadline_v2"
                    or timing.get("samples") != expected_ticks
                    or timing.get("deadline_miss_count") != absolute_misses
                    or timing.get("pass") is not expected_pass
                ):
                    failures.append(f"phases[{index}].wall_timing:inconsistent")
        expected_faults = sorted(P0_REQUIRED_FAULTS)
        if not isinstance(faults, Mapping) or (
            faults.get("required_fault_ids") != expected_faults
            or faults.get("observed_fault_ids") != expected_faults
            or faults.get("fault_count") != len(expected_faults)
            or faults.get("all_required_faults_exact_zero") is not True
        ):
            failures.append(f"phases[{index}].fault_exact_zero:failed")
        if row.get("control_path_diagnostic_pass") is not True:
            failures.append(f"phases[{index}].control_path_diagnostic:failed")

    control_pass = len(phases) == 3 and all(
        isinstance(row, Mapping) and row.get("control_path_diagnostic_pass") is True
        for row in phases
    )
    fault_pass = len(phases) == 3 and all(
        isinstance(row, Mapping)
        and isinstance(row.get("fault_injection"), Mapping)
        and row["fault_injection"].get("all_required_faults_exact_zero") is True
        for row in phases
    )
    historical_final_timing_pass = (
        timing_scope_status == HISTORICAL_TIMING_SCOPE_STATUS
        and len(phases) == 3
        and isinstance(phases[2], Mapping)
        and isinstance(phases[2].get("wall_timing"), Mapping)
        and phases[2]["wall_timing"].get("pass") is True
    )
    current_control_timing_pass = False
    if timing_scope_status == CURRENT_TIMING_SCOPE_STATUS:
        final_control_pass = bool(
            len(phase_control_results) == 3
            and phase_control_results[2].get("pass") is True
        )
        expected_gate = {
            "scope": CONTROL_SCOPE_V2,
            "required_phase_duration_s": 60.0,
            "deadline_ms": 2.0,
            "p99_limit_ms": 1.8,
            "requires_zero_deadline_misses": True,
            "requires_prefault": True,
            "phase_results": phase_control_results,
            "complete_sequence_evaluated": len(phases) == 3,
            "pass": final_control_pass,
        }
        if timing_evidence.get("control_hard_500hz_gate") != expected_gate:
            failures.append("timing_evidence.control_hard_500hz_gate:projection_mismatch")
        expected_simulator = {
            "scope": SIMULATOR_SCOPE_V2,
            "diagnostic_only": True,
            "phase_results": phase_simulator_results,
        }
        if timing_evidence.get("simulator_cycle_diagnostic") != expected_simulator:
            failures.append("timing_evidence.simulator_cycle_diagnostic:projection_mismatch")
        current_control_timing_pass = final_control_pass
    else:
        historical_gate = timing_evidence.get("historical_combined_scope_timing_gate")
        if not isinstance(historical_gate, Mapping) or (
            historical_gate.get("pass") is not historical_final_timing_pass
        ):
            failures.append("timing_evidence.historical_timing_gate:projection_mismatch")
    if control_pass and fault_pass and (
        current_control_timing_pass or historical_final_timing_pass
    ):
        expected_result = "diagnostic_pass"
    elif control_pass and fault_pass:
        expected_result = (
            "control_diagnostic_pass_control_hard_500hz_blocked"
            if timing_scope_status == CURRENT_TIMING_SCOPE_STATUS
            else "control_diagnostic_pass_timing_blocked"
        )
    else:
        expected_result = "diagnostic_fail"
    expected_diagnostic = {
        "canonical_phase_sequence_complete": True,
        "source_manifest_structurally_valid": True,
        "source_result": expected_result,
        "all_control_paths_diagnostic_pass": control_pass,
        "all_required_faults_exact_zero": fault_pass,
        "timing_scope_status": timing_scope_status,
        "offline_control_timing_pass": current_control_timing_pass,
        "historical_combined_scope_timing_pass": historical_final_timing_pass,
    }
    if payload.get("diagnostic") != expected_diagnostic:
        failures.append("diagnostic:projection_mismatch")
    expected_state = {
        **{key: value for key, value in _state_core().items() if key != "claims"},
        "v29_current": True,
        "v30_program": V30_PROGRAM,
        "v30_state": "inactive_offline_candidate",
    }
    if payload.get("state_projection") != expected_state:
        failures.append("state_projection:non_promotion_boundary_mismatch")
    if payload.get("claims") != FALSE_CLAIMS:
        failures.append("claims:must_all_remain_false")
    blockers = payload.get("blockers")
    required_blockers = {
        "offline_simulation_cannot_promote_live_state",
        "controller_canaries_not_run",
        "controller_readback_missing",
        "evidence_not_frozen",
    }
    if timing_scope_status == HISTORICAL_TIMING_SCOPE_STATUS:
        required_blockers.add("current_control_timing_evidence_missing")
    elif not current_control_timing_pass:
        required_blockers.add("offline_control_timing_failed")
    if model.get("physics_provenance") != "calibrated_physics":
        required_blockers.add("geometry_provisional_no_p0_physics_claim")
    if not isinstance(blockers, list) or not required_blockers.issubset(set(blockers)):
        failures.append("blockers:required_fail_closed_blocker_missing")
    return sorted(set(failures))


def bound_state_binding(
    summary: Mapping[str, object],
    *,
    summary_artifact: str,
    summary_sha256: str,
) -> dict[str, object]:
    failures = validate_diagnostic(summary)
    if failures:
        raise ValueError("cannot bind invalid diagnostic: " + "; ".join(failures))
    if not summary_artifact or not _sha(summary_sha256):
        raise ValueError("summary artifact path and sha256 are required")
    diagnostic = summary.get("diagnostic") or {}
    timing_pass = diagnostic.get("offline_control_timing_pass") is True
    timing_scope_status = diagnostic.get("timing_scope_status")
    if timing_scope_status == HISTORICAL_TIMING_SCOPE_STATUS:
        status = "bound_timing_scope_superseded"
    else:
        status = "bound_diagnostic_complete" if timing_pass else "bound_timing_blocked"
    return {
        "schema": STATE_SCHEMA,
        "status": status,
        "planned_summary_artifact": (
            "config/step5d_p0_v8_offline_simulation_diagnostic.json"
        ),
        "summary_artifact": summary_artifact,
        "summary_sha256": summary_sha256,
        **_state_core(),
        "blockers": list(summary.get("blockers") or []),
    }


def validate_state_binding(
    binding: Mapping[str, object],
    *,
    summary: Mapping[str, object] | None = None,
) -> list[str]:
    failures: list[str] = []
    core = _state_core()
    if binding.get("schema") != STATE_SCHEMA:
        failures.append("state_binding.schema:invalid")
    for field, expected in core.items():
        if binding.get(field) != expected:
            failures.append(f"state_binding.{field}:non_promotion_mismatch")
    status = binding.get("status")
    if status == "awaiting_final_canonical_artifact":
        if binding.get("summary_artifact") is not None or binding.get("summary_sha256") is not None:
            failures.append("state_binding.awaiting:unexpected_artifact")
        if summary is not None:
            failures.append("state_binding.awaiting:summary_unexpected")
        if "final_canonical_simulator_artifact_not_bound" not in (binding.get("blockers") or []):
            failures.append("state_binding.awaiting:blocker_missing")
    elif status in {
        "bound_timing_scope_superseded",
        "bound_timing_blocked",
        "bound_diagnostic_complete",
    }:
        failures.extend(_binding_blockers({
            "path": binding.get("summary_artifact"),
            "sha256": binding.get("summary_sha256"),
            "size_bytes": 1,
        }, "state_binding.summary"))
        if summary is None:
            failures.append("state_binding.summary:missing")
        else:
            failures.extend(validate_diagnostic(summary))
            diagnostic = summary.get("diagnostic") or {}
            timing_pass = diagnostic.get("offline_control_timing_pass") is True
            expected = (
                "bound_timing_scope_superseded"
                if diagnostic.get("timing_scope_status")
                == HISTORICAL_TIMING_SCOPE_STATUS
                else "bound_diagnostic_complete"
                if timing_pass
                else "bound_timing_blocked"
            )
            if status != expected or binding.get("blockers") != summary.get("blockers"):
                failures.append("state_binding:summary_projection_mismatch")
    else:
        failures.append("state_binding.status:invalid")
    return sorted(set(failures))


def build_from_paths(
    run_manifest_path: Path,
    *,
    source_host: str,
    run_reference: str | None = None,
    model_manifest_path: Path | None = None,
    model_reference: str | None = None,
) -> dict[str, object]:
    from verify_step5d_p0_v8_mujoco import validate_run_manifest

    run_manifest_path = run_manifest_path.resolve()
    manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    failures = validate_run_manifest(manifest, root=run_manifest_path.parent, require_complete=True)
    if failures:
        raise ValueError("source run invalid: " + "; ".join(failures))
    phase_evidence = [
        json.loads((run_manifest_path.parent / str(row["evidence_path"])).read_text())
        for row in manifest["phases"]
    ]
    run_binding = {
        "path": run_reference or str(run_manifest_path),
        "sha256": file_sha256(run_manifest_path),
        "size_bytes": run_manifest_path.stat().st_size,
    }
    model_binding = None
    if model_manifest_path is not None:
        model_manifest_path = model_manifest_path.resolve()
        model_manifest = json.loads(model_manifest_path.read_text(encoding="utf-8"))
        no_contact = (model_manifest.get("outputs") or {}).get("no_contact_velocity") or {}
        if no_contact.get("sha256") != phase_evidence[0]["source_binding"]["model_sha256"]:
            raise ValueError("model bundle does not match run model sha256")
        model_binding = {
            "path": model_reference or str(model_manifest_path),
            "sha256": file_sha256(model_manifest_path),
            "size_bytes": model_manifest_path.stat().st_size,
        }
    summary = build_diagnostic(
        manifest,
        phase_evidence,
        run_manifest_binding=run_binding,
        source_host=source_host,
        model_manifest_binding=model_binding,
    )
    failures = validate_diagnostic(summary)
    if failures:
        raise ValueError("generated diagnostic invalid: " + "; ".join(failures))
    return summary


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-manifest", type=Path)
    parser.add_argument("--model-manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-host", default="local")
    parser.add_argument("--run-reference")
    parser.add_argument("--model-reference")
    parser.add_argument("--state-binding-output", type=Path)
    parser.add_argument("--summary-reference")
    parser.add_argument("--verify-summary", type=Path)
    args = parser.parse_args()
    if args.verify_summary is not None:
        payload = json.loads(args.verify_summary.read_text())
        failures = validate_diagnostic(payload)
        print(json.dumps({"valid": not failures, "blockers": failures}, indent=2, sort_keys=True))
        return 0 if not failures else 3
    if args.run_manifest is None or args.output is None:
        parser.error("--run-manifest and --output are required")
    summary = build_from_paths(
        args.run_manifest,
        source_host=args.source_host,
        run_reference=args.run_reference,
        model_manifest_path=args.model_manifest,
        model_reference=args.model_reference,
    )
    _write_json(args.output, summary)
    summary_sha = file_sha256(args.output)
    binding_output = None
    if args.state_binding_output is not None:
        binding = bound_state_binding(
            summary,
            summary_artifact=args.summary_reference or str(args.output),
            summary_sha256=summary_sha,
        )
        _write_json(args.state_binding_output, binding)
        binding_output = str(args.state_binding_output.resolve())
    print(json.dumps({
        "output": str(args.output.resolve()),
        "sha256": summary_sha,
        "diagnostic_sha256": summary["diagnostic_sha256"],
        "state_binding_output": binding_output,
        "status": "generated_offline_only_no_state_promotion",
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
