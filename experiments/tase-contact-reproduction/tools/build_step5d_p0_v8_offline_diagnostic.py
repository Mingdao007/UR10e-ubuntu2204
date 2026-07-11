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


def _phase_summary(
    manifest_row: Mapping[str, object],
    evidence: Mapping[str, object],
) -> dict[str, object]:
    nominal = evidence.get("nominal")
    wall = evidence.get("wall_timing")
    faults = evidence.get("faults")
    nominal = nominal if isinstance(nominal, Mapping) else {}
    wall = wall if isinstance(wall, Mapping) else {}
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
        "duration_s": manifest_row.get("duration_s"),
        "sequence_index": manifest_row.get("sequence_index"),
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
            )
        },
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
    timing = manifest.get("timing_gate")
    timing_pass = isinstance(timing, Mapping) and timing.get("pass") is True
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
    if not timing_pass:
        blockers.add("offline_simulation_wall_timing_failed")
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
        "diagnostic": {
            "canonical_phase_sequence_complete": (
                manifest.get("canonical_phase_sequence_complete") is True
            ),
            "source_manifest_structurally_valid": True,
            "source_result": manifest.get("result"),
            "all_control_paths_diagnostic_pass": control_pass,
            "all_required_faults_exact_zero": fault_pass,
            "wall_timing_gate_pass": timing_pass,
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
    for index, row in enumerate(phases):
        if not isinstance(row, Mapping):
            failures.append(f"phases[{index}]:invalid")
            continue
        if row.get("sequence_index") != index:
            failures.append(f"phases[{index}].sequence_index:mismatch")
        if row.get("structurally_valid") is not True or row.get("validation_blockers") != []:
            failures.append(f"phases[{index}].structural_validation:failed")
        failures.extend(_binding_blockers(row.get("evidence"), f"phases[{index}].evidence"))
        counters = row.get("control_counters")
        timing = row.get("wall_timing")
        faults = row.get("fault_injection")
        expected_ticks = int(PHASES_S[index] * 500)
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
    final_timing_pass = (
        len(phases) == 3
        and isinstance(phases[2], Mapping)
        and isinstance(phases[2].get("wall_timing"), Mapping)
        and phases[2]["wall_timing"].get("pass") is True
    )
    expected_result = (
        "diagnostic_pass" if control_pass and fault_pass and final_timing_pass
        else "control_diagnostic_pass_timing_blocked" if control_pass and fault_pass
        else "diagnostic_fail"
    )
    expected_diagnostic = {
        "canonical_phase_sequence_complete": True,
        "source_manifest_structurally_valid": True,
        "source_result": expected_result,
        "all_control_paths_diagnostic_pass": control_pass,
        "all_required_faults_exact_zero": fault_pass,
        "wall_timing_gate_pass": final_timing_pass,
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
    if not final_timing_pass:
        required_blockers.add("offline_simulation_wall_timing_failed")
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
    timing_pass = (summary.get("diagnostic") or {}).get("wall_timing_gate_pass") is True
    return {
        "schema": STATE_SCHEMA,
        "status": "bound_diagnostic_complete" if timing_pass else "bound_timing_blocked",
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
    elif status in {"bound_timing_blocked", "bound_diagnostic_complete"}:
        failures.extend(_binding_blockers({
            "path": binding.get("summary_artifact"),
            "sha256": binding.get("summary_sha256"),
            "size_bytes": 1,
        }, "state_binding.summary"))
        if summary is None:
            failures.append("state_binding.summary:missing")
        else:
            failures.extend(validate_diagnostic(summary))
            timing_pass = (summary.get("diagnostic") or {}).get("wall_timing_gate_pass") is True
            expected = "bound_diagnostic_complete" if timing_pass else "bound_timing_blocked"
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
