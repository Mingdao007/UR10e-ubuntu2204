#!/usr/bin/env python3
"""Build a fail-closed per-stage dual-sensor contact audit.

This gate is stage-scoped. It intentionally does not reuse the standalone P2
contact witness to upgrade Step5b/5d/6b/7/8. It reads retained JSON artifacts
only; it does not launch Gazebo, RViz, ROS, or any live bench surface.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
RUNS = EXPERIMENT_ROOT / "runs"
GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"

DEFAULT_STAGE_ID = "step5b"
DEFAULT_STAGE_ROW_SUMMARY = (
    RUNS
    / "ur10e_gazebo_visual_mesh_foundation_clean_provenance_20260621_1606_isolated_display"
    / "matrix_gui_real_aligned"
    / DEFAULT_STAGE_ID
    / "close_detail"
    / "row_summary.json"
)
DEFAULT_STAGE_SIMULATED_FT_MANIFEST = (
    RUNS
    / "ur10e_gazebo_visual_mesh_foundation_post_steering_20260621_134252"
    / "readiness"
    / "p1"
    / "per_stage_simulated_ft_pack"
    / "step_simulated_ft_evidence_manifest.json"
)
DEFAULT_STEP_STATUS_AUDIT = (
    RUNS
    / "ur10e_gazebo_visual_mesh_foundation_post_steering_20260621_134252"
    / "readiness"
    / "step_status"
    / "step_status_rnn_audit.json"
)

REQUIRED_SIMULATED_FT_FIELDS = ("stamp", "frame_id", "source", "status", "baseline", "log_evidence")
REQUIRED_OBSERVATION_SURFACES = (
    "stage_row_summary",
    "stage_contact_pair_log",
    "stage_contact_wrench_adapter",
    "stage_simulated_ft_manifest",
    "step_status_rnn_audit",
    "visual_evidence",
    "tcp_path_evidence",
)


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def workspace_path(path: Path | str | None) -> Path | None:
    if path is None:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return WORKSPACE / candidate


def rel(path: Path | str | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(WORKSPACE.resolve()))
    except (OSError, ValueError):
        return str(path)


def run_id_for_path(path: Path | str | None) -> str | None:
    candidate = workspace_path(path)
    if candidate is None:
        return None
    try:
        relative = candidate.resolve().relative_to(RUNS.resolve())
    except (OSError, ValueError):
        return None
    return relative.parts[0] if relative.parts else None


def sha256_file(path: Path | str | None) -> str | None:
    candidate = workspace_path(path)
    if candidate is None or not candidate.is_file():
        return None
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_row(surface: str, path: Path | None) -> dict[str, Any]:
    exists = bool(path and path.is_file())
    return {
        "surface": surface,
        "path": rel(path),
        "exists": exists,
        "run_id": run_id_for_path(path),
        "sha256": sha256_file(path) if exists else None,
    }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def stage_simulated_ft_summary(stage_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    stages = manifest.get("stages") if isinstance(manifest.get("stages"), dict) else {}
    stage = stages.get(stage_id) if isinstance(stages.get(stage_id), dict) else {}
    fields = stage.get("evidence_fields_present") if isinstance(stage.get("evidence_fields_present"), dict) else {}
    field_coverage = {field: bool(fields.get(field)) for field in REQUIRED_SIMULATED_FT_FIELDS}
    valid = bool(
        manifest.get("schema") == "ur10e_step_simulated_ft_evidence_pack_v1"
        and manifest.get("claim_tier") == "simulated_ft"
        and stage.get("claim_tier") == "simulated_ft"
        and stage.get("valid") is True
        and all(field_coverage.values())
    )
    issues: list[str] = []
    if manifest.get("schema") != "ur10e_step_simulated_ft_evidence_pack_v1":
        issues.append("stage_simulated_ft_manifest.schema:unsupported_or_missing")
    if not stage:
        issues.append(f"stage_simulated_ft_manifest.stage:{stage_id}:missing")
    if stage.get("claim_tier") != "simulated_ft":
        issues.append("stage_simulated_ft.claim_tier:not_simulated_ft")
    if stage.get("valid") is not True:
        issues.append("stage_simulated_ft.valid:not_true")
    missing_fields = [field for field, present in field_coverage.items() if not present]
    if missing_fields:
        issues.append("stage_simulated_ft.evidence_fields:missing:" + ",".join(missing_fields))
    return {
        "stage_id": stage_id,
        "valid": valid,
        "claim_tier": "simulated_ft" if valid else "visual_only",
        "source": stage.get("source"),
        "frame_id": stage.get("frame_id"),
        "sample_count": stage.get("sample_count"),
        "first_stamp_s": stage.get("first_stamp_s"),
        "last_stamp_s": stage.get("last_stamp_s"),
        "log_path": stage.get("log_path"),
        "evidence_fields_present": field_coverage,
        "validation_issues": issues,
    }


def contact_pair_log_path(row_summary: dict[str, Any], explicit_path: Path | None) -> Path | None:
    if explicit_path is not None:
        return explicit_path
    value = row_summary.get("gazebo_contact_pair_log_path")
    return Path(value) if value else None


def stage_eoat_collision_count(row_summary: dict[str, Any]) -> int:
    model = row_summary.get("model_composition_audit")
    if isinstance(model, dict):
        return _int(model.get("eoat_collision_count"))
    return _int(row_summary.get("eoat_collision_count"))


def contact_pair_summary(payload: dict[str, Any], *, stage_id: str) -> dict[str, Any]:
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    parse_issues = payload.get("parse_issues") if isinstance(payload.get("parse_issues"), list) else []
    matching_rows = [row for row in rows if isinstance(row, dict) and _row_has_eoat_surface_contact_pair(row)]
    native_rows = [row for row in matching_rows if isinstance(row.get("native_gazebo_contact_wrench"), dict)]
    raw_wrench_rows = [row for row in matching_rows if _int(row.get("raw_gazebo_contact_wrench_count")) > 0]
    issues: list[str] = []
    if payload.get("schema") != "ur10e_gazebo_contact_pair_log_v1":
        issues.append("stage_contact_pair_log.schema:unsupported_or_missing")
    if parse_issues:
        issues.append("stage_contact_pair_log.parse_issues")
    if not rows:
        issues.append("stage_contact_pair_log:no_rows")
    if not matching_rows:
        issues.append("stage_contact_pair_log:no_stage_eoat_surface_pair")
    evidence = bool(rows and matching_rows and not parse_issues and payload.get("schema") == "ur10e_gazebo_contact_pair_log_v1")
    return {
        "stage_id": stage_id,
        "present": bool(payload),
        "schema": payload.get("schema"),
        "topic": payload.get("topic"),
        "claim_tier": payload.get("claim_tier", "visual_only"),
        "row_count": len(rows),
        "matching_row_count": len(matching_rows),
        "native_wrench_row_count": len(native_rows),
        "raw_wrench_row_count": len(raw_wrench_rows),
        "first_matching_row": matching_rows[0] if matching_rows else None,
        "evidence": evidence,
        "validation_issues": issues,
    }


def _row_has_eoat_surface_contact_pair(row: dict[str, Any]) -> bool:
    collision1 = str(row.get("collision1") or "")
    collision2 = str(row.get("collision2") or "")
    return (_is_eoat_collision(collision1) and _is_surface_collision(collision2)) or (
        _is_eoat_collision(collision2) and _is_surface_collision(collision1)
    )


def _is_eoat_collision(name: str) -> bool:
    return "eoat" in name and "collision" in name


def _is_surface_collision(name: str) -> bool:
    return "contact_surface" in name or "surface::collision" in name


def contact_wrench_adapter_summary(payload: dict[str, Any]) -> dict[str, Any]:
    trace = payload.get("wrench_trace") if isinstance(payload.get("wrench_trace"), dict) else {}
    rows = trace.get("rows") if isinstance(trace.get("rows"), list) else []
    present = bool(payload)
    proven = bool(
        present
        and payload.get("schema") == "ur10e_gazebo_contact_wrench_adapter_report_v1"
        and payload.get("claim_tier") == "physical Gazebo collision/contact physics"
        and payload.get("force_source") == "gazebo_contact"
        and payload.get("trace_written") is True
        and payload.get("total_contact_wrench_proven") is True
        and payload.get("wrench_aggregation_policy") == "total_contact_wrench"
        and _int(payload.get("verified_native_wrench_row_count")) > 0
        and _int(payload.get("total_contact_wrench_row_count")) > 0
        and not payload.get("blockers")
        and bool(rows)
    )
    issues: list[str] = []
    if not present:
        issues.append("stage_contact_wrench_adapter:missing")
    elif payload.get("schema") != "ur10e_gazebo_contact_wrench_adapter_report_v1":
        issues.append("stage_contact_wrench_adapter.schema:unsupported_or_missing")
    if present and payload.get("force_source") != "gazebo_contact":
        issues.append("stage_contact_wrench_adapter.force_source:not_gazebo_contact")
    if present and payload.get("total_contact_wrench_proven") is not True:
        issues.append("stage_total_contact_wrench:not_proven")
    if present and payload.get("wrench_aggregation_policy") != "total_contact_wrench":
        issues.append("stage_contact_wrench_adapter.aggregation:not_total_contact_wrench")
    if present and _int(payload.get("verified_native_wrench_row_count")) <= 0:
        issues.append("stage_contact_wrench_adapter.verified_native_wrench_row_count:zero")
    if present and not rows:
        issues.append("stage_contact_wrench_adapter.trace_rows:missing")
    if present and payload.get("blockers"):
        issues.append("stage_contact_wrench_adapter.blockers:not_empty")
    return {
        "present": present,
        "schema": payload.get("schema"),
        "claim_tier": payload.get("claim_tier"),
        "force_source": payload.get("force_source"),
        "trace_written": bool(payload.get("trace_written")),
        "native_wrench_row_count": _int(payload.get("native_wrench_row_count")),
        "verified_native_wrench_row_count": _int(payload.get("verified_native_wrench_row_count")),
        "total_contact_wrench_row_count": _int(payload.get("total_contact_wrench_row_count")),
        "total_contact_wrench_proven": proven,
        "wrench_aggregation_policy": payload.get("wrench_aggregation_policy"),
        "rows": rows,
        "validation_issues": issues,
    }


def correlate_wrench_to_contact(adapter: dict[str, Any], contact_pair: dict[str, Any], *, tolerance_s: float) -> dict[str, Any]:
    first = contact_pair.get("first_matching_row") if isinstance(contact_pair.get("first_matching_row"), dict) else {}
    contact_stamp = _float(first.get("stamp_s"))
    matches: list[dict[str, Any]] = []
    if contact_stamp is not None:
        for row in adapter.get("rows") or []:
            if not isinstance(row, dict):
                continue
            stamp = row_stamp(row)
            normal_load = _float(row.get("normal_load_n")) or 0.0
            if (
                stamp is not None
                and abs(stamp - contact_stamp) <= tolerance_s
                and row.get("source") == "gazebo_contact"
                and row.get("status") == "valid"
                and row.get("contact_state") == "contact"
                and normal_load > 0.0
            ):
                matches.append(
                    {
                        "wrench_stamp_s": stamp,
                        "contact_stamp_s": contact_stamp,
                        "time_delta_s": abs(stamp - contact_stamp),
                        "normal_load_n": normal_load,
                    }
                )
    if not adapter.get("total_contact_wrench_proven"):
        status = "blocked_total_contact_wrench_not_proven"
    elif not contact_pair.get("evidence"):
        status = "blocked_stage_contact_pair_log_missing_or_invalid"
    elif contact_stamp is None:
        status = "blocked_contact_stamp_missing"
    elif not matches:
        status = "blocked_no_timestamped_wrench_contact_overlap"
    else:
        status = "correlated"
    return {
        "evidence": bool(matches),
        "status": status,
        "matched_row_count": len(matches),
        "tolerance_s": tolerance_s,
        "first_match": matches[0] if matches else None,
    }


def row_stamp(row: dict[str, Any]) -> float | None:
    header = row.get("header") if isinstance(row.get("header"), dict) else {}
    if "stamp_s" in header:
        return _float(header.get("stamp_s"))
    if "t_s" in row:
        return _float(row.get("t_s"))
    return None


def observation_summary(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    time_window = payload.get("time_window") if isinstance(payload.get("time_window"), dict) else {}
    raw_surfaces = payload.get("surfaces")
    if isinstance(raw_surfaces, dict):
        surfaces = {surface: raw_surfaces.get(surface) for surface in REQUIRED_OBSERVATION_SURFACES if raw_surfaces.get(surface)}
    elif isinstance(raw_surfaces, list):
        values = {str(item) for item in raw_surfaces}
        surfaces = {surface: surface for surface in REQUIRED_OBSERVATION_SURFACES if surface in values}
    else:
        surfaces = {}
    missing_surfaces = [surface for surface in REQUIRED_OBSERVATION_SURFACES if surface not in surfaces]
    explicit = payload.get("explicit") is True or payload.get("same_run_concurrent_observation_explicit") is True
    summary = {
        "schema": payload.get("schema"),
        "observation_id": str(payload.get("observation_id") or "").strip() or None,
        "same_run_concurrent_observation_explicit": explicit,
        "time_window": {
            "start": time_window.get("start"),
            "end": time_window.get("end"),
            "clock_source": time_window.get("clock_source"),
        },
        "surfaces": surfaces,
        "missing_surfaces": missing_surfaces,
    }
    issues: list[str] = []
    if not payload:
        issues.append("same_run_stage_dual_sensor_observation:missing")
    if payload.get("schema") and payload.get("schema") != "ur10e_stage_dual_sensor_observation_manifest_v1":
        issues.append("same_run_stage_dual_sensor_observation.schema:unsupported")
    if payload.get("same_run_stage_dual_sensor_observation_proven") is False:
        issues.append("same_run_stage_dual_sensor_observation.manifest:not_proven")
    if not summary["observation_id"]:
        issues.append("same_run_stage_dual_sensor_observation.observation_id:missing")
    if not explicit:
        issues.append("same_run_stage_dual_sensor_observation.explicit:not_true")
    if not summary["time_window"]["start"]:
        issues.append("same_run_stage_dual_sensor_observation.time_window.start:missing")
    if not summary["time_window"]["end"]:
        issues.append("same_run_stage_dual_sensor_observation.time_window.end:missing")
    if not summary["time_window"]["clock_source"]:
        issues.append("same_run_stage_dual_sensor_observation.time_window.clock_source:missing")
    if missing_surfaces:
        issues.append("same_run_stage_dual_sensor_observation.surfaces:missing:" + ",".join(missing_surfaces))
    summary["same_run_stage_dual_sensor_observation_proven"] = not issues
    return summary, issues


def build_audit(
    *,
    stage_id: str = DEFAULT_STAGE_ID,
    generated_at: str | None = None,
    stage_row_summary_path: Path = DEFAULT_STAGE_ROW_SUMMARY,
    stage_simulated_ft_manifest_path: Path = DEFAULT_STAGE_SIMULATED_FT_MANIFEST,
    step_status_audit_path: Path = DEFAULT_STEP_STATUS_AUDIT,
    stage_contact_pair_log_path: Path | None = None,
    stage_contact_wrench_adapter_path: Path | None = None,
    same_run_observation_manifest_path: Path | None = None,
    correlation_tolerance_s: float = 0.02,
) -> dict[str, Any]:
    row_summary = load_json(stage_row_summary_path)
    stage_manifest = load_json(stage_simulated_ft_manifest_path)
    step_status = load_json(step_status_audit_path)
    contact_path = contact_pair_log_path(row_summary, stage_contact_pair_log_path)
    contact_payload = load_json(contact_path) if contact_path and contact_path.is_file() else {}
    adapter_payload = (
        load_json(stage_contact_wrench_adapter_path)
        if stage_contact_wrench_adapter_path and stage_contact_wrench_adapter_path.is_file()
        else {}
    )
    observation_payload = (
        load_json(same_run_observation_manifest_path)
        if same_run_observation_manifest_path and same_run_observation_manifest_path.is_file()
        else {}
    )

    rows = [
        artifact_row("stage_row_summary", stage_row_summary_path),
        artifact_row("stage_contact_pair_log", contact_path),
        artifact_row("stage_contact_wrench_adapter", stage_contact_wrench_adapter_path),
        artifact_row("stage_simulated_ft_manifest", stage_simulated_ft_manifest_path),
        artifact_row("step_status_rnn_audit", step_status_audit_path),
        artifact_row("same_run_observation_manifest", same_run_observation_manifest_path),
    ]
    missing_surfaces = [row["surface"] for row in rows if row["surface"] != "same_run_observation_manifest" and not row["exists"]]
    run_ids = {row["run_id"] for row in rows if row["run_id"] and row["surface"] != "same_run_observation_manifest"}
    cross_run_surfaces = [
        row["surface"]
        for row in rows
        if row["run_id"] and len(run_ids) > 1 and row["surface"] != "same_run_observation_manifest"
    ]

    sim_ft = stage_simulated_ft_summary(stage_id, stage_manifest)
    contact_pair = contact_pair_summary(contact_payload, stage_id=stage_id)
    adapter = contact_wrench_adapter_summary(adapter_payload)
    correlation = correlate_wrench_to_contact(adapter, contact_pair, tolerance_s=correlation_tolerance_s)
    observation, observation_issues = observation_summary(observation_payload)
    eoat_collision_count = stage_eoat_collision_count(row_summary)
    row_stage_matches = str(row_summary.get("stage") or stage_id) == stage_id
    visual_evidence = bool(row_summary.get("visual_evidence_captured") or row_summary.get("scripted_camera_evidence_captured"))
    tcp_path_evidence = bool(row_summary.get("trace_path") or row_summary.get("final_visual_pose_world"))
    stage_step_status = _stage_status(step_status, stage_id)
    per_stage_physical_contact = bool(
        row_stage_matches
        and eoat_collision_count > 0
        and contact_pair["evidence"]
        and adapter["total_contact_wrench_proven"]
        and correlation["evidence"]
    )
    same_run_dual_sensor = bool(
        per_stage_physical_contact
        and sim_ft["valid"]
        and observation["same_run_stage_dual_sensor_observation_proven"]
        and not missing_surfaces
        and not cross_run_surfaces
        and visual_evidence
        and tcp_path_evidence
    )

    validation_issues: list[str] = []
    validation_issues.extend(sim_ft["validation_issues"])
    validation_issues.extend(contact_pair["validation_issues"])
    validation_issues.extend(adapter["validation_issues"])
    validation_issues.extend(observation_issues)
    if missing_surfaces:
        validation_issues.append("required_stage_surfaces:missing:" + ",".join(sorted(missing_surfaces)))
    if cross_run_surfaces:
        validation_issues.append("required_stage_surfaces:cross_run:" + ",".join(sorted(cross_run_surfaces)))
    if not row_stage_matches:
        validation_issues.append("stage_row_summary.stage:mismatch")
    if eoat_collision_count <= 0:
        validation_issues.append("stage_eoat_collision_count:zero")
    if not visual_evidence:
        validation_issues.append("stage_visual_evidence:missing")
    if not tcp_path_evidence:
        validation_issues.append("stage_tcp_path_evidence:missing")

    blockers: list[str] = []
    if not sim_ft["valid"]:
        blockers.append("stage_simulated_ft:not_valid")
    if not per_stage_physical_contact:
        blockers.append("per_stage_physical_gazebo_contact:not_proven")
    if not adapter["total_contact_wrench_proven"]:
        blockers.append("stage_total_contact_wrench:not_proven")
    if not correlation["evidence"]:
        blockers.append("stage_wrench_contact_correlation:not_proven")
    if not same_run_dual_sensor:
        blockers.append("same_run_stage_dual_sensor_observation:not_proven")

    highest_tier = (
        "physical Gazebo collision/contact physics"
        if per_stage_physical_contact
        else "simulated_ft"
        if sim_ft["valid"]
        else "visual_only"
    )
    return {
        "schema": "ur10e_per_stage_dual_sensor_contact_audit_v1",
        "generated_at": generated_at or _now_iso(),
        "goal_lineage": GOAL_LINEAGE,
        "mode": "offline_report_level_per_stage_dual_sensor_contact_gate",
        "stage_id": stage_id,
        "claim_tier": highest_tier,
        "live_authorization": {
            "robot_motion_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "urscript_authorized": False,
            "zero_ftsensor_authorized": False,
            "payload_tcp_safety_writes_authorized": False,
            "real_bench_live_contact_authorized": False,
        },
        "artifact_rows": rows,
        "missing_surfaces": sorted(missing_surfaces),
        "cross_run_surfaces": sorted(cross_run_surfaces),
        "stage_row_summary": {
            "path": rel(stage_row_summary_path),
            "stage_matches": row_stage_matches,
            "observer_visual_pass": bool(row_summary.get("observer_visual_pass")),
            "visual_evidence_captured": visual_evidence,
            "tcp_path_evidence": tcp_path_evidence,
            "eoat_collision_count": eoat_collision_count,
            "force_contact_source": row_summary.get("force_contact_source"),
            "force_contact_physics_proven": bool(row_summary.get("force_contact_physics_proven")),
        },
        "stage_simulated_ft": sim_ft,
        "stage_step_status": {
            "present": bool(stage_step_status),
            "claim_tier": stage_step_status.get("claim_tier"),
            "gazebo_contact_physics_status": stage_step_status.get("gazebo_contact_physics_status"),
            "simulated_ft_status": stage_step_status.get("simulated_ft_status"),
            "current_blocker": stage_step_status.get("current_blocker"),
        },
        "stage_contact_pair_log": contact_pair,
        "stage_contact_wrench_adapter": {key: value for key, value in adapter.items() if key != "rows"},
        "stage_wrench_contact_correlation": correlation,
        "per_stage_physical_gazebo_contact": {
            "claim_tier": "physical Gazebo collision/contact physics" if per_stage_physical_contact else "visual_only",
            "per_stage_physical_gazebo_contact_proven": per_stage_physical_contact,
            "required_evidence": [
                "stage EOAT collision evidence",
                "stage contact pair/log evidence",
                "stage total contact wrench",
                "stage frame/normal evidence",
                "stage wrench/contact timing correlation",
            ],
        },
        "same_run_stage_dual_sensor_observation": {
            "same_run_stage_dual_sensor_observation_proven": same_run_dual_sensor,
            "observation": observation,
            "required_condition": (
                "physical Gazebo contact, simulated FT, Step/RNN status, visual/RViz, and TCP/path evidence "
                "must be bound by one observation_id, time_window, and clock_source from the same run."
            ),
        },
        "validation_issues": validation_issues,
        "blockers": blockers,
        "forbidden_claim": (
            "real bench/live contact; full reproduction acceptance; per-stage physical Gazebo contact unless "
            "stage-specific contact pair/log, total contact wrench, frame/normal evidence, and wrench/contact "
            "correlation are proven; same-run dual-sensor binding unless explicit observation_id/time_window/"
            "clock_source binds all required surfaces"
        ),
    }


def _stage_status(step_status: dict[str, Any], stage_id: str) -> dict[str, Any]:
    rows = step_status.get("step_status_matrix") if isinstance(step_status.get("step_status_matrix"), list) else []
    for row in rows:
        if isinstance(row, dict) and row.get("stage_id") == stage_id:
            return row
    return {}


def write_audit(
    output_dir: Path,
    *,
    stage_id: str = DEFAULT_STAGE_ID,
    generated_at: str | None = None,
    stage_row_summary_path: Path = DEFAULT_STAGE_ROW_SUMMARY,
    stage_simulated_ft_manifest_path: Path = DEFAULT_STAGE_SIMULATED_FT_MANIFEST,
    step_status_audit_path: Path = DEFAULT_STEP_STATUS_AUDIT,
    stage_contact_pair_log_path: Path | None = None,
    stage_contact_wrench_adapter_path: Path | None = None,
    same_run_observation_manifest_path: Path | None = None,
    correlation_tolerance_s: float = 0.02,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{stage_id}_per_stage_dual_sensor_contact_audit.json"
    payload = build_audit(
        stage_id=stage_id,
        generated_at=generated_at,
        stage_row_summary_path=stage_row_summary_path,
        stage_simulated_ft_manifest_path=stage_simulated_ft_manifest_path,
        step_status_audit_path=step_status_audit_path,
        stage_contact_pair_log_path=stage_contact_pair_log_path,
        stage_contact_wrench_adapter_path=stage_contact_wrench_adapter_path,
        same_run_observation_manifest_path=same_run_observation_manifest_path,
        correlation_tolerance_s=correlation_tolerance_s,
    )
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--stage-id", default=DEFAULT_STAGE_ID)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--stage-row-summary", type=Path, default=DEFAULT_STAGE_ROW_SUMMARY)
    parser.add_argument("--stage-simulated-ft-manifest", type=Path, default=DEFAULT_STAGE_SIMULATED_FT_MANIFEST)
    parser.add_argument("--step-status-audit", type=Path, default=DEFAULT_STEP_STATUS_AUDIT)
    parser.add_argument("--stage-contact-pair-log", type=Path, default=None)
    parser.add_argument("--stage-contact-wrench-adapter", type=Path, default=None)
    parser.add_argument("--same-run-observation-manifest", type=Path, default=None)
    parser.add_argument("--correlation-tolerance-s", type=float, default=0.02)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        write_audit(
            args.output_dir,
            stage_id=args.stage_id,
            generated_at=args.generated_at,
            stage_row_summary_path=args.stage_row_summary,
            stage_simulated_ft_manifest_path=args.stage_simulated_ft_manifest,
            step_status_audit_path=args.step_status_audit,
            stage_contact_pair_log_path=args.stage_contact_pair_log,
            stage_contact_wrench_adapter_path=args.stage_contact_wrench_adapter,
            same_run_observation_manifest_path=args.same_run_observation_manifest,
            correlation_tolerance_s=args.correlation_tolerance_s,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
