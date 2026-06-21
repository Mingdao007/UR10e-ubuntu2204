#!/usr/bin/env python3
"""Build a stage-scoped same-run dual-sensor observation manifest.

This is an offline report-level binder. It does not launch Gazebo, RViz, ROS,
or any live bench surface. The output is fail-closed: a manifest with missing
or cross-run surfaces is retained as evidence of a blocked observation, not as
same-run proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
RUNS = EXPERIMENT_ROOT / "runs"
GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"
DEFAULT_STAGE_ID = "step5b"
DEFAULT_FILENAME_TEMPLATE = "{stage_id}_same_run_stage_dual_sensor_observation_manifest.json"
SAME_RUN_STAGE_OBSERVATION_SCOPE = "same_run_stage_gazebo_row"

REQUIRED_SURFACES = (
    "stage_row_summary",
    "stage_contact_pair_log",
    "stage_contact_wrench_adapter",
    "stage_simulated_ft_manifest",
    "step_status_rnn_audit",
    "visual_evidence",
    "tcp_path_evidence",
)


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


def load_json_if_file(path: Path | str | None) -> dict[str, Any]:
    candidate = workspace_path(path)
    if candidate is None or not candidate.is_file():
        return {}
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"_load_error": f"{type(exc).__name__}: {exc}"}
    return payload if isinstance(payload, dict) else {"_load_error": "json_root:not_object"}


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


def _file_nonempty(path: Path | str | None) -> bool:
    candidate = workspace_path(path)
    return bool(candidate and candidate.is_file() and candidate.stat().st_size > 0)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def build_manifest(
    *,
    stage_id: str,
    observation_id: str,
    time_start: str,
    time_end: str,
    clock_source: str,
    surfaces: dict[str, Path | None],
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated = generated_at or _now_iso()
    target_run_id = run_id_for_path(surfaces.get("stage_row_summary")) or first_existing_run_id(surfaces)
    artifact_rows = [
        artifact_row(surface, surfaces.get(surface), target_run_id=target_run_id) for surface in REQUIRED_SURFACES
    ]
    missing_surfaces = [row["surface"] for row in artifact_rows if not row["exists"]]
    cross_run_surfaces = [
        row["surface"]
        for row in artifact_rows
        if row["exists"] and target_run_id and row["run_id"] and row["run_id"] != target_run_id
    ]
    validation_issues: list[str] = []
    if not observation_id.strip():
        validation_issues.append("observation_id:missing")
    if not time_start.strip():
        validation_issues.append("time_window.start:missing")
    if not time_end.strip():
        validation_issues.append("time_window.end:missing")
    if not clock_source.strip():
        validation_issues.append("time_window.clock_source:missing")
    if time_start.strip() and time_end.strip() and not time_window_order_valid(time_start.strip(), time_end.strip()):
        validation_issues.append("time_window.order:invalid")
    if missing_surfaces:
        validation_issues.append("required_stage_surfaces:missing:" + ",".join(sorted(missing_surfaces)))
    if cross_run_surfaces:
        validation_issues.append("required_stage_surfaces:cross_run:" + ",".join(sorted(cross_run_surfaces)))
    if not target_run_id:
        validation_issues.append("target_run_id:missing")

    content_validation = validate_surface_content(
        stage_id=stage_id,
        observation_id=observation_id.strip(),
        time_start=time_start,
        time_end=time_end,
        clock_source=clock_source,
        surfaces=surfaces,
    )
    validation_issues.extend(content_validation["validation_issues"])

    proven = not validation_issues
    return {
        "schema": "ur10e_stage_dual_sensor_observation_manifest_v1",
        "generated_at": generated,
        "goal_lineage": GOAL_LINEAGE,
        "mode": "offline_report_level_stage_dual_sensor_observation_binder",
        "stage_id": stage_id,
        "claim_tier": "visual_only",
        "observation_id": observation_id.strip() or None,
        "explicit": True,
        "same_run_concurrent_observation_explicit": True,
        "time_window": {
            "start": time_start.strip() or None,
            "end": time_end.strip() or None,
            "clock_source": clock_source.strip() or None,
        },
        "target_run_id": target_run_id,
        "surfaces": {surface: rel(surfaces.get(surface)) for surface in REQUIRED_SURFACES if surfaces.get(surface)},
        "artifact_rows": artifact_rows,
        "missing_surfaces": sorted(missing_surfaces),
        "cross_run_surfaces": sorted(cross_run_surfaces),
        "content_validation": content_validation,
        "same_run_stage_dual_sensor_observation_proven": proven,
        "required_observation_scope": SAME_RUN_STAGE_OBSERVATION_SCOPE,
        "validation_issues": validation_issues,
        "blockers": [] if proven else ["same_run_stage_dual_sensor_observation:not_proven"],
        "live_authorization": {
            "robot_motion_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "urscript_authorized": False,
            "zero_ftsensor_authorized": False,
            "payload_tcp_safety_writes_authorized": False,
            "real_bench_live_contact_authorized": False,
        },
        "forbidden_claim": (
            "real bench/live contact; per-stage physical Gazebo contact without stage-specific "
            "contact pair/log, total contact wrench, frame/normal evidence, and wrench/contact correlation"
        ),
    }


def validate_surface_content(
    *,
    stage_id: str,
    observation_id: str,
    time_start: str,
    time_end: str,
    clock_source: str,
    surfaces: dict[str, Path | None],
) -> dict[str, Any]:
    expected_time_window = {
        "start": time_start.strip(),
        "end": time_end.strip(),
        "clock_source": clock_source.strip(),
    }
    checks = {
        "stage_row_summary": stage_row_summary_issues(stage_id, load_json_if_file(surfaces.get("stage_row_summary"))),
        "stage_contact_pair_log": contact_pair_log_issues(
            load_json_if_file(surfaces.get("stage_contact_pair_log")),
            stage_id=stage_id,
            observation_id=observation_id,
            expected_time_window=expected_time_window,
        ),
        "stage_contact_wrench_adapter": contact_wrench_adapter_issues(
            load_json_if_file(surfaces.get("stage_contact_wrench_adapter")),
            stage_id=stage_id,
            observation_id=observation_id,
            expected_time_window=expected_time_window,
        ),
        "stage_simulated_ft_manifest": stage_simulated_ft_manifest_issues(
            stage_id,
            load_json_if_file(surfaces.get("stage_simulated_ft_manifest")),
        ),
        "step_status_rnn_audit": step_status_issues(stage_id, load_json_if_file(surfaces.get("step_status_rnn_audit"))),
        "visual_evidence": file_surface_issues("visual_evidence", surfaces.get("visual_evidence")),
        "tcp_path_evidence": file_surface_issues("tcp_path_evidence", surfaces.get("tcp_path_evidence")),
    }
    validation_issues = [issue for issues in checks.values() for issue in issues]
    return {
        "surface_content_proven": not validation_issues,
        "checks": {surface: {"ok": not issues, "issues": issues} for surface, issues in checks.items()},
        "validation_issues": validation_issues,
    }


def stage_row_summary_issues(stage_id: str, payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if payload.get("_load_error"):
        return [f"stage_row_summary.unreadable:{payload['_load_error']}"]
    if payload.get("schema") != "ur10e_gazebo_real_aligned_gui_matrix_row_v2":
        issues.append("stage_row_summary.schema:unsupported_or_missing")
    if str(payload.get("stage") or "") != stage_id:
        issues.append("stage_row_summary.stage:mismatch")
    if not (payload.get("visual_evidence_captured") or payload.get("scripted_camera_evidence_captured")):
        issues.append("stage_row_summary.visual_evidence:missing")
    if not (payload.get("trace_path") or payload.get("final_visual_pose_world")):
        issues.append("stage_row_summary.tcp_path_evidence:missing")
    model = payload.get("model_composition_audit") if isinstance(payload.get("model_composition_audit"), dict) else {}
    eoat_collision_count = _int(model.get("eoat_collision_count") or payload.get("eoat_collision_count"))
    if eoat_collision_count <= 0:
        issues.append("stage_row_summary.eoat_collision_count:zero")
    return issues


def contact_pair_log_issues(
    payload: dict[str, Any],
    *,
    stage_id: str,
    observation_id: str,
    expected_time_window: dict[str, str],
) -> list[str]:
    issues: list[str] = []
    if payload.get("_load_error"):
        return [f"stage_contact_pair_log.unreadable:{payload['_load_error']}"]
    if payload.get("schema") != "ur10e_gazebo_contact_pair_log_v1":
        issues.append("stage_contact_pair_log.schema:unsupported_or_missing")
    if payload.get("stage_id") != stage_id:
        issues.append("stage_contact_pair_log.stage_id:mismatch_or_missing")
    if payload.get("observation_id") != observation_id:
        issues.append("stage_contact_pair_log.observation_id:mismatch_or_missing")
    if payload.get("observation_scope") != SAME_RUN_STAGE_OBSERVATION_SCOPE:
        issues.append("stage_contact_pair_log.observation_scope:not_same_run_stage_gazebo_row")
    issues.extend(time_window_issues("stage_contact_pair_log", payload, expected_time_window=expected_time_window))
    if payload.get("parse_issues"):
        issues.append("stage_contact_pair_log.parse_issues:not_empty")
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    matching_rows = [row for row in rows if isinstance(row, dict) and _row_has_eoat_surface_contact_pair(row)]
    if not rows:
        issues.append("stage_contact_pair_log.rows:missing")
    if not matching_rows:
        issues.append("stage_contact_pair_log.eoat_surface_pair:missing")
        return issues
    first = matching_rows[0]
    if _float(first.get("stamp_s")) is None:
        issues.append("stage_contact_pair_log.matching_row.stamp_s:missing")
    if not first.get("position_m"):
        issues.append("stage_contact_pair_log.matching_row.position_m:missing")
    if not first.get("normal"):
        issues.append("stage_contact_pair_log.matching_row.normal:missing")
    if not first.get("normal_source"):
        issues.append("stage_contact_pair_log.matching_row.normal_source:missing")
    if _int(first.get("contact_count")) <= 0:
        issues.append("stage_contact_pair_log.matching_row.contact_count:zero")
    return issues


def contact_wrench_adapter_issues(
    payload: dict[str, Any],
    *,
    stage_id: str,
    observation_id: str,
    expected_time_window: dict[str, str],
) -> list[str]:
    issues: list[str] = []
    if payload.get("_load_error"):
        return [f"stage_contact_wrench_adapter.unreadable:{payload['_load_error']}"]
    if payload.get("schema") != "ur10e_gazebo_contact_wrench_adapter_report_v1":
        issues.append("stage_contact_wrench_adapter.schema:unsupported_or_missing")
    if payload.get("stage_id") != stage_id:
        issues.append("stage_contact_wrench_adapter.stage_id:mismatch_or_missing")
    if payload.get("observation_id") != observation_id:
        issues.append("stage_contact_wrench_adapter.observation_id:mismatch_or_missing")
    if payload.get("observation_scope") != SAME_RUN_STAGE_OBSERVATION_SCOPE:
        issues.append("stage_contact_wrench_adapter.observation_scope:not_same_run_stage_gazebo_row")
    issues.extend(time_window_issues("stage_contact_wrench_adapter", payload, expected_time_window=expected_time_window))
    if payload.get("claim_tier") != "physical Gazebo collision/contact physics":
        issues.append("stage_contact_wrench_adapter.claim_tier:not_physical_gazebo_contact")
    if payload.get("force_source") != "gazebo_contact":
        issues.append("stage_contact_wrench_adapter.force_source:not_gazebo_contact")
    if payload.get("trace_written") is not True:
        issues.append("stage_contact_wrench_adapter.trace_written:not_true")
    if payload.get("total_contact_wrench_proven") is not True:
        issues.append("stage_contact_wrench_adapter.total_contact_wrench_proven:not_true")
    if payload.get("wrench_aggregation_policy") != "total_contact_wrench":
        issues.append("stage_contact_wrench_adapter.wrench_aggregation_policy:not_total_contact_wrench")
    if _int(payload.get("verified_native_wrench_row_count")) <= 0:
        issues.append("stage_contact_wrench_adapter.verified_native_wrench_row_count:zero")
    if _int(payload.get("total_contact_wrench_row_count")) <= 0:
        issues.append("stage_contact_wrench_adapter.total_contact_wrench_row_count:zero")
    if payload.get("blockers"):
        issues.append("stage_contact_wrench_adapter.blockers:not_empty")
    trace = payload.get("wrench_trace") if isinstance(payload.get("wrench_trace"), dict) else {}
    rows = trace.get("rows") if isinstance(trace.get("rows"), list) else []
    if not rows:
        issues.append("stage_contact_wrench_adapter.wrench_trace.rows:missing")
        return issues
    valid_rows = [row for row in rows if isinstance(row, dict) and _valid_gazebo_contact_wrench_row(row)]
    if not valid_rows:
        issues.append("stage_contact_wrench_adapter.wrench_trace.valid_contact_row:missing")
    return issues


def time_window_issues(
    surface: str,
    payload: dict[str, Any],
    *,
    expected_time_window: dict[str, str],
) -> list[str]:
    window = payload.get("time_window") if isinstance(payload.get("time_window"), dict) else {}
    issues: list[str] = []
    if not window.get("start"):
        issues.append(f"{surface}.time_window.start:missing")
    elif window.get("start") != expected_time_window.get("start"):
        issues.append(f"{surface}.time_window.start:mismatch")
    if not window.get("end"):
        issues.append(f"{surface}.time_window.end:missing")
    elif window.get("end") != expected_time_window.get("end"):
        issues.append(f"{surface}.time_window.end:mismatch")
    if not window.get("clock_source"):
        issues.append(f"{surface}.time_window.clock_source:missing")
    elif window.get("clock_source") != expected_time_window.get("clock_source"):
        issues.append(f"{surface}.time_window.clock_source:mismatch")
    return issues


def time_window_order_valid(start: str, end: str) -> bool:
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        return False
    return start_dt < end_dt


def _valid_gazebo_contact_wrench_row(row: dict[str, Any]) -> bool:
    header = row.get("header") if isinstance(row.get("header"), dict) else {}
    return bool(
        _float(header.get("stamp_s")) is not None
        and header.get("frame_id")
        and row.get("source") == "gazebo_contact"
        and row.get("status") == "valid"
        and row.get("contact_state") == "contact"
        and row.get("baseline_policy")
        and (_float(row.get("normal_load_n")) or 0.0) > 0.0
    )


def stage_simulated_ft_manifest_issues(stage_id: str, payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if payload.get("_load_error"):
        return [f"stage_simulated_ft_manifest.unreadable:{payload['_load_error']}"]
    if payload.get("schema") != "ur10e_step_simulated_ft_evidence_pack_v1":
        issues.append("stage_simulated_ft_manifest.schema:unsupported_or_missing")
    if payload.get("claim_tier") != "simulated_ft":
        issues.append("stage_simulated_ft_manifest.claim_tier:not_simulated_ft")
    stages = payload.get("stages") if isinstance(payload.get("stages"), dict) else {}
    stage = stages.get(stage_id) if isinstance(stages.get(stage_id), dict) else {}
    if not stage:
        issues.append(f"stage_simulated_ft_manifest.stage:{stage_id}:missing")
        return issues
    if stage.get("claim_tier") != "simulated_ft":
        issues.append("stage_simulated_ft_manifest.stage.claim_tier:not_simulated_ft")
    if stage.get("valid") is not True:
        issues.append("stage_simulated_ft_manifest.stage.valid:not_true")
    fields = stage.get("evidence_fields_present") if isinstance(stage.get("evidence_fields_present"), dict) else {}
    required = ("stamp", "frame_id", "source", "status", "baseline", "log_evidence")
    missing = [field for field in required if fields.get(field) is not True]
    if missing:
        issues.append("stage_simulated_ft_manifest.stage.evidence_fields:missing:" + ",".join(missing))
    return issues


def step_status_issues(stage_id: str, payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if payload.get("_load_error"):
        return [f"step_status_rnn_audit.unreadable:{payload['_load_error']}"]
    if payload.get("schema") != "ur10e_step_status_rnn_audit_v1":
        issues.append("step_status_rnn_audit.schema:unsupported_or_missing")
    rows = payload.get("step_status_matrix") if isinstance(payload.get("step_status_matrix"), list) else []
    stage_row = next((row for row in rows if isinstance(row, dict) and row.get("stage_id") == stage_id), None)
    if not stage_row:
        issues.append(f"step_status_rnn_audit.stage:{stage_id}:missing")
    return issues


def file_surface_issues(surface: str, path: Path | None) -> list[str]:
    return [] if _file_nonempty(path) else [f"{surface}.file:missing_or_empty"]


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


def first_existing_run_id(surfaces: dict[str, Path | None]) -> str | None:
    for surface in REQUIRED_SURFACES:
        candidate = surfaces.get(surface)
        candidate_path = workspace_path(candidate)
        if candidate and candidate_path and candidate_path.is_file():
            run_id = run_id_for_path(candidate)
            if run_id:
                return run_id
    return None


def artifact_row(surface: str, path: Path | None, *, target_run_id: str | None) -> dict[str, Any]:
    candidate = workspace_path(path)
    exists = bool(candidate and candidate.is_file())
    run_id = run_id_for_path(path) if exists else None
    return {
        "surface": surface,
        "path": rel(path),
        "exists": exists,
        "run_id": run_id,
        "same_run_as_target": bool(run_id and target_run_id and run_id == target_run_id),
        "sha256": sha256_file(path) if exists else None,
    }


def write_manifest(
    output_dir: Path,
    *,
    stage_id: str,
    observation_id: str,
    time_start: str,
    time_end: str,
    clock_source: str,
    surfaces: dict[str, Path | None],
    generated_at: str | None = None,
    filename: str | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_filename = filename or DEFAULT_FILENAME_TEMPLATE.format(stage_id=stage_id)
    output_path = output_child(output_dir, output_filename)
    payload = build_manifest(
        stage_id=stage_id,
        observation_id=observation_id,
        time_start=time_start,
        time_end=time_end,
        clock_source=clock_source,
        surfaces=surfaces,
        generated_at=generated_at,
    )
    payload["artifact_path"] = str(output_path)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def output_child(output_dir: Path, filename: str) -> Path:
    child = Path(filename)
    if child.is_absolute() or len(child.parts) != 1:
        raise ValueError(f"output filename must be a simple filename: {filename}")
    return output_dir / child


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--stage-id", default=DEFAULT_STAGE_ID)
    parser.add_argument("--observation-id", required=True)
    parser.add_argument("--time-start", required=True)
    parser.add_argument("--time-end", required=True)
    parser.add_argument("--clock-source", required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--filename", default=None)
    parser.add_argument("--stage-row-summary", required=True, type=Path)
    parser.add_argument("--stage-contact-pair-log", required=True, type=Path)
    parser.add_argument("--stage-contact-wrench-adapter", required=True, type=Path)
    parser.add_argument("--stage-simulated-ft-manifest", required=True, type=Path)
    parser.add_argument("--step-status-audit", required=True, type=Path)
    parser.add_argument("--visual-evidence", required=True, type=Path)
    parser.add_argument("--tcp-path-evidence", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        write_manifest(
            args.output_dir,
            stage_id=args.stage_id,
            observation_id=args.observation_id,
            time_start=args.time_start,
            time_end=args.time_end,
            clock_source=args.clock_source,
            generated_at=args.generated_at,
            filename=args.filename,
            surfaces={
                "stage_row_summary": args.stage_row_summary,
                "stage_contact_pair_log": args.stage_contact_pair_log,
                "stage_contact_wrench_adapter": args.stage_contact_wrench_adapter,
                "stage_simulated_ft_manifest": args.stage_simulated_ft_manifest,
                "step_status_rnn_audit": args.step_status_audit,
                "visual_evidence": args.visual_evidence,
                "tcp_path_evidence": args.tcp_path_evidence,
            },
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
