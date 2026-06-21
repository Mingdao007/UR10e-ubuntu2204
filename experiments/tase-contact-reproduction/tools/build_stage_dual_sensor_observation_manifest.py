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
    if missing_surfaces:
        validation_issues.append("required_stage_surfaces:missing:" + ",".join(sorted(missing_surfaces)))
    if cross_run_surfaces:
        validation_issues.append("required_stage_surfaces:cross_run:" + ",".join(sorted(cross_run_surfaces)))
    if not target_run_id:
        validation_issues.append("target_run_id:missing")

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
        "same_run_stage_dual_sensor_observation_proven": proven,
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
