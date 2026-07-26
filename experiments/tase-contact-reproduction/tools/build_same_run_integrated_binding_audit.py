#!/usr/bin/env python3
"""Build a fail-closed same-run integrated binding audit for P6.

This offline gate checks whether retained P6 evidence is actually from one
coherent run. It reads JSON manifests only; it does not launch Gazebo, RViz,
ROS, or any live bench surface.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
RUNS = EXPERIMENT_ROOT / "runs"
TOOLS = EXPERIMENT_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from build_dual_sensor_total_wrench_audit import (  # noqa: E402
    all_stage_simulated_ft_valid,
    dual_sensor_observation_summary,
    p2_physical_contact_proven,
    p2_step_summary,
    total_contact_wrench_proven,
)

GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"
DEFAULT_INTEGRATED_DEMO_MANIFEST = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0912_p6_pdf_truth_binding_bundle"
    / "p6_integrated_demo_manifest.json"
)

REQUIRED_SOURCE_ARTIFACTS = {
    "p6_manifest": "integrated_demo_manifest",
    "p3_visual_rviz_audit": "p3_visual_rviz_audit",
    "stage_simulated_ft_manifest": "stage_simulated_ft_manifest",
    "step_status_rnn_audit": "step_status_rnn_audit",
    "p2_contact_correlation_audit": "p2_contact_correlation_audit",
    "tcp_distance_evidence": "tcp_distance_evidence",
}
CONCURRENT_OBSERVATION_REQUIRED_SURFACES = sorted(
    surface for surface in REQUIRED_SOURCE_ARTIFACTS if surface != "p6_manifest"
)
EXPECTED_STAGE_IDS = ["step5a", "step5b", "step5c", "step5d", "step6a", "step6b", "step7", "step8"]
NO_LIVE_DEMO_CLAIM_TIERS = {
    "visual_only",
    "virtual/software force-loop",
    "simulated_ft",
    "physical Gazebo collision/contact physics",
}
REQUIRED_DEMO_FIELDS = (
    "platform_trajectory_evidence",
    "eoat_tooling_evidence",
    "contact_surface_evidence",
    "tcp_distance_evidence",
    "simulated_ft_artifacts",
    "step_rnn_pipeline_artifact",
    "gazebo_gui_evidence_paths",
    "rviz_evidence_paths",
)
REQUIRED_PLOTS = (
    "wrench_vs_time",
    "contact_state_vs_time",
    "tcp_distance_to_surface_vs_time",
    "force_threshold_crossing",
    "latency_staleness",
    "gravity_residual",
)
OPTIONAL_UNSUPPORTED_PLOTS = {"gravity_residual"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_json_safe(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def rel(path: Path | str | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(WORKSPACE.resolve()))
    except (OSError, ValueError):
        return str(path)


def workspace_path(path: str | None) -> Path | None:
    if not path:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return WORKSPACE / candidate


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_id_for_path(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        relative = path.resolve().relative_to(RUNS.resolve())
    except (OSError, ValueError):
        return None
    return relative.parts[0] if relative.parts else None


def classify_artifact(surface: str, path_value: str | None, *, target_run_id: str | None) -> dict[str, Any]:
    path = workspace_path(path_value)
    exists = bool(path and path.is_file())
    run_id = run_id_for_path(path) if exists else None
    return {
        "surface": surface,
        "path": rel(path_value),
        "exists": exists,
        "run_id": run_id,
        "same_run_as_manifest": bool(run_id and target_run_id and run_id == target_run_id),
        "sha256": sha256_file(path) if exists else None,
    }


def manifest_source_artifacts(manifest: dict[str, Any]) -> dict[str, Any]:
    source_artifacts = manifest.get("source_artifacts")
    return source_artifacts if isinstance(source_artifacts, dict) else {}


def validate_path_list(payload: dict[str, Any], field: str, issues: list[str]) -> None:
    value = payload.get(field)
    if not isinstance(value, list) or not value:
        issues.append(f"integrated_demo_manifest.{field}:missing_or_empty")
        return
    for index, item in enumerate(value):
        if not item:
            issues.append(f"integrated_demo_manifest.{field}[{index}]:empty")


def manifest_content_issues(manifest: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if manifest.get("fail_closed") is not True:
        issues.append("integrated_demo_manifest.fail_closed:not_true")
    claim_tier = manifest.get("claim_tier") or "visual_only"
    if claim_tier not in NO_LIVE_DEMO_CLAIM_TIERS:
        issues.append("integrated_demo_manifest.claim_tier:unsupported")
    for field in (
        "platform_trajectory_evidence",
        "eoat_tooling_evidence",
        "contact_surface_evidence",
        "tcp_distance_evidence",
        "step_rnn_pipeline_artifact",
    ):
        if not manifest.get(field):
            issues.append(f"integrated_demo_manifest.{field}:missing")
    for field in ("simulated_ft_artifacts", "gazebo_gui_evidence_paths", "rviz_evidence_paths"):
        validate_path_list(manifest, field, issues)
    plots = manifest.get("plots")
    if not isinstance(plots, dict):
        issues.append("integrated_demo_manifest.plots:missing")
        plots = {}
    for plot_name in REQUIRED_PLOTS:
        plot = plots.get(plot_name)
        if not isinstance(plot, dict):
            issues.append(f"integrated_demo_manifest.plots.{plot_name}:missing")
            continue
        if not plot.get("path"):
            issues.append(f"integrated_demo_manifest.plots.{plot_name}.path:missing")
        if not plot.get("unit_labels"):
            issues.append(f"integrated_demo_manifest.plots.{plot_name}.unit_labels:missing")
        if not plot.get("frame_label"):
            issues.append(f"integrated_demo_manifest.plots.{plot_name}.frame_label:missing")
        if not plot.get("claim_tier"):
            issues.append(f"integrated_demo_manifest.plots.{plot_name}.claim_tier:missing")
        elif plot.get("claim_tier") not in NO_LIVE_DEMO_CLAIM_TIERS:
            issues.append(f"integrated_demo_manifest.plots.{plot_name}.claim_tier:unsupported")
        if plot.get("supported") is not True and plot_name not in OPTIONAL_UNSUPPORTED_PLOTS:
            issues.append(f"integrated_demo_manifest.plots.{plot_name}:unsupported")
        if plot.get("supported") is not True and plot_name in OPTIONAL_UNSUPPORTED_PLOTS and not plot.get("unsupported_reason"):
            issues.append(f"integrated_demo_manifest.plots.{plot_name}.unsupported_reason:missing")
    return issues


def manifest_concurrent_observation(manifest: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    raw = manifest.get("concurrent_observation")
    observation = raw if isinstance(raw, dict) else {}
    time_window = observation.get("time_window") if isinstance(observation.get("time_window"), dict) else {}
    surfaces = observation.get("surfaces") if isinstance(observation.get("surfaces"), list) else []
    observed_surfaces = sorted({str(surface) for surface in surfaces if str(surface)})
    missing_surfaces = sorted(set(CONCURRENT_OBSERVATION_REQUIRED_SURFACES) - set(observed_surfaces))
    summary = {
        "observation_id": str(observation.get("observation_id") or "").strip() or None,
        "same_run_concurrent_observation_explicit": observation.get(
            "same_run_concurrent_observation_explicit"
        )
        is True,
        "time_window": {
            "start": time_window.get("start"),
            "end": time_window.get("end"),
            "clock_source": time_window.get("clock_source"),
        },
        "surfaces": observed_surfaces,
        "missing_surfaces": missing_surfaces,
    }
    issues: list[str] = []
    if not isinstance(raw, dict):
        issues.append("manifest.concurrent_observation:missing")
    if not summary["observation_id"]:
        issues.append("manifest.concurrent_observation.observation_id:missing")
    if not summary["same_run_concurrent_observation_explicit"]:
        issues.append("manifest.concurrent_observation.same_run_concurrent_observation_explicit:not_true")
    if not summary["time_window"]["start"]:
        issues.append("manifest.concurrent_observation.time_window.start:missing")
    if not summary["time_window"]["end"]:
        issues.append("manifest.concurrent_observation.time_window.end:missing")
    if not summary["time_window"]["clock_source"]:
        issues.append("manifest.concurrent_observation.time_window.clock_source:missing")
    if missing_surfaces:
        issues.append("manifest.concurrent_observation.surfaces:missing:" + ",".join(missing_surfaces))
    return summary, issues


def artifact_paths_by_surface(artifact_rows: list[dict[str, Any]]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for row in artifact_rows:
        surface = str(row.get("surface") or "")
        path = workspace_path(str(row.get("path") or "")) if row.get("path") else None
        if surface and path is not None and path.is_file():
            paths[surface] = path
    return paths


def source_artifact_hash_issues(
    manifest: dict[str, Any],
    artifact_rows: list[dict[str, Any]],
) -> list[str]:
    hash_map = manifest.get("source_artifact_sha256") if isinstance(manifest.get("source_artifact_sha256"), dict) else {}
    issues: list[str] = []
    rows_by_surface = {str(row.get("surface")): row for row in artifact_rows if isinstance(row, dict)}
    for surface, manifest_key in REQUIRED_SOURCE_ARTIFACTS.items():
        if surface == "p6_manifest":
            continue
        row = rows_by_surface.get(surface, {})
        expected_hash = hash_map.get(manifest_key)
        if not expected_hash:
            issues.append(f"source_artifact_sha256.{manifest_key}:missing")
            continue
        if row.get("sha256") != expected_hash:
            issues.append(f"source_artifact_sha256.{manifest_key}:mismatch")
    return issues


def schema_lineage_issues(payload: dict[str, Any], *, surface: str, expected_schema: str) -> list[str]:
    issues: list[str] = []
    if payload.get("schema") != expected_schema:
        issues.append(f"artifact_content.{surface}.schema:unsupported_or_missing")
    if payload.get("goal_lineage") != GOAL_LINEAGE:
        issues.append(f"artifact_content.{surface}.goal_lineage:mismatch_or_missing")
    return issues


def p3_visual_rviz_ready(payload: dict[str, Any]) -> bool:
    coverage = payload.get("audit_coverage") if isinstance(payload.get("audit_coverage"), dict) else {}
    return bool(
        int(coverage.get("gazebo_rows") or 0) > 0
        and int(coverage.get("gazebo_observer_visual_pass_count") or 0) > 0
        and coverage.get("rviz_all_required_items_evidenced") is True
        and coverage.get("rviz_rendered_screenshot_evidence_present") is True
    )


def stage_set_exact(step_status: dict[str, Any]) -> bool:
    rows = step_status.get("step_status_matrix") if isinstance(step_status.get("step_status_matrix"), list) else []
    stage_ids = [str(row.get("stage_id")) for row in rows if isinstance(row, dict)]
    return sorted(stage_ids) == sorted(EXPECTED_STAGE_IDS) and len(stage_ids) == len(set(stage_ids))


def tcp_distance_supported(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("schema") == "ur10e_p6_tcp_distance_evidence_audit_v1"
        and payload.get("tcp_distance_time_series_supported") is True
        and int(payload.get("planned_tcp_distance_row_count") or payload.get("row_count") or 0) > 0
    )


def source_observation_matches_manifest(
    source_observation: dict[str, Any],
    manifest_observation: dict[str, Any],
    artifact_paths: dict[str, Path],
) -> list[str]:
    issues: list[str] = []
    if source_observation.get("same_run_concurrent_dual_sensor_observation_proven") is not True:
        issues.append("artifact_content.same_run_concurrent_dual_sensor_observation:not_proven")
    if source_observation.get("observation_id") != manifest_observation.get("observation_id"):
        issues.append("artifact_content.same_run_concurrent_dual_sensor_observation.observation_id:manifest_mismatch")
    source_time = source_observation.get("time_window") if isinstance(source_observation.get("time_window"), dict) else {}
    manifest_time = manifest_observation.get("time_window") if isinstance(manifest_observation.get("time_window"), dict) else {}
    for field in ("start", "end", "clock_source"):
        if source_time.get(field) != manifest_time.get(field):
            issues.append(
                f"artifact_content.same_run_concurrent_dual_sensor_observation.time_window.{field}:manifest_mismatch"
            )
    source_surfaces = source_observation.get("surfaces") if isinstance(source_observation.get("surfaces"), dict) else {}
    manifest_surfaces = set(manifest_observation.get("surfaces") or [])
    for surface in ("stage_simulated_ft_manifest", "p2_contact_correlation_audit", "step_status_rnn_audit"):
        if surface not in manifest_surfaces or not source_surfaces.get(surface):
            issues.append(f"artifact_content.same_run_concurrent_dual_sensor_observation.surfaces.{surface}:missing")
            continue
        artifact_path = artifact_paths.get(surface)
        if artifact_path is not None and Path(str(source_surfaces.get(surface))).name != artifact_path.name:
            issues.append(
                f"artifact_content.same_run_concurrent_dual_sensor_observation.surfaces.{surface}:artifact_row_mismatch"
            )
    return issues


def source_content_validation_issues(
    artifact_rows: list[dict[str, Any]],
    manifest_observation: dict[str, Any],
) -> list[str]:
    paths = artifact_paths_by_surface(artifact_rows)
    p3 = load_json_safe(paths.get("p3_visual_rviz_audit"))
    stage_manifest = load_json_safe(paths.get("stage_simulated_ft_manifest"))
    step_status = load_json_safe(paths.get("step_status_rnn_audit"))
    p2_audit = load_json_safe(paths.get("p2_contact_correlation_audit"))
    tcp_distance = load_json_safe(paths.get("tcp_distance_evidence"))
    step_p2 = p2_step_summary(step_status)

    issues: list[str] = []
    issues.extend(
        schema_lineage_issues(
            p3,
            surface="p3_visual_rviz_audit",
            expected_schema="ur10e_p3_visual_rviz_evidence_audit_v1",
        )
    )
    if not p3_visual_rviz_ready(p3):
        issues.append("artifact_content.p3_visual_rviz_audit:not_ready")

    issues.extend(
        schema_lineage_issues(
            stage_manifest,
            surface="stage_simulated_ft_manifest",
            expected_schema="ur10e_step_simulated_ft_evidence_pack_v1",
        )
    )
    if not all_stage_simulated_ft_valid(stage_manifest):
        issues.append("artifact_content.stage_simulated_ft_manifest:not_all_valid")

    issues.extend(
        schema_lineage_issues(
            step_status,
            surface="step_status_rnn_audit",
            expected_schema="ur10e_step_status_rnn_audit_v1",
        )
    )
    if not stage_set_exact(step_status):
        issues.append("artifact_content.step_status_rnn_audit.stage_set:not_exact")

    issues.extend(
        schema_lineage_issues(
            p2_audit,
            surface="p2_contact_correlation_audit",
            expected_schema="ur10e_gazebo_p2_contact_correlation_audit_v1",
        )
    )
    if not p2_physical_contact_proven(p2_audit, step_p2):
        issues.append("artifact_content.p2_physical_gazebo_contact:not_proven")
    if not total_contact_wrench_proven(p2_audit, step_p2):
        issues.append("artifact_content.total_contact_wrench:not_proven")

    if not tcp_distance_supported(tcp_distance):
        issues.append("artifact_content.tcp_distance_evidence:not_supported")

    source_observation, source_observation_issues = dual_sensor_observation_summary(p2_audit, step_p2)
    issues.extend(f"artifact_content.{issue}" for issue in source_observation_issues)
    issues.extend(source_observation_matches_manifest(source_observation, manifest_observation, paths))
    return issues


def build_audit(
    *,
    generated_at: str | None = None,
    integrated_demo_manifest_path: Path | None = DEFAULT_INTEGRATED_DEMO_MANIFEST,
) -> dict[str, Any]:
    generated = generated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    manifest_path = integrated_demo_manifest_path
    manifest_exists = bool(manifest_path and manifest_path.is_file())
    validation_issues: list[str] = []
    manifest: dict[str, Any] = {}
    if manifest_exists:
        try:
            manifest = load_json(manifest_path)
        except (OSError, json.JSONDecodeError) as exc:
            validation_issues.append(f"integrated_demo_manifest:unreadable:{type(exc).__name__}")
            manifest = {}
    else:
        validation_issues.append("integrated_demo_manifest:missing")

    if manifest.get("schema") != "ur10e_p6_integrated_demo_manifest_v1":
        validation_issues.append("integrated_demo_manifest.schema:unsupported_or_missing")
    if manifest.get("goal_lineage") != GOAL_LINEAGE:
        validation_issues.append("integrated_demo_manifest.goal_lineage:mismatch_or_missing")
    validation_issues.extend(manifest_content_issues(manifest))

    target_run_id = run_id_for_path(manifest_path) if manifest_path and manifest_exists else None
    source_artifacts = manifest_source_artifacts(manifest)
    artifact_rows: list[dict[str, Any]] = [
        classify_artifact(
            "p6_manifest",
            str(manifest_path) if manifest_path is not None else None,
            target_run_id=target_run_id,
        )
    ]
    for surface, manifest_key in REQUIRED_SOURCE_ARTIFACTS.items():
        if surface == "p6_manifest":
            continue
        artifact_rows.append(
            classify_artifact(
                surface,
                source_artifacts.get(manifest_key),
                target_run_id=target_run_id,
            )
        )

    missing_surfaces = [row["surface"] for row in artifact_rows if not row["exists"]]
    cross_run_surfaces = [
        row["surface"]
        for row in artifact_rows
        if row["exists"] and row["surface"] != "p6_manifest" and not row["same_run_as_manifest"]
    ]
    manifest_binding = manifest.get("same_run_binding") if isinstance(manifest.get("same_run_binding"), dict) else {}
    manifest_claims_same_run = all(
        manifest_binding.get(field) is True
        for field in (
            "visual_rviz_simulated_ft_same_run",
            "visual_rviz_physical_gazebo_contact_same_run",
            "step_rnn_physical_gazebo_contact_same_run",
        )
    )
    concurrent_observation, concurrent_observation_issues = manifest_concurrent_observation(manifest)
    concurrent_observation_proven = not concurrent_observation_issues
    source_content_issues = source_content_validation_issues(artifact_rows, concurrent_observation)
    source_hash_issues = source_artifact_hash_issues(manifest, artifact_rows)
    if missing_surfaces:
        validation_issues.append("required_source_artifacts:missing:" + ",".join(sorted(missing_surfaces)))
    if cross_run_surfaces:
        validation_issues.append("required_source_artifacts:cross_run:" + ",".join(sorted(cross_run_surfaces)))
    if not manifest_claims_same_run:
        validation_issues.append("manifest.same_run_binding:not_all_true")
    validation_issues.extend(concurrent_observation_issues)
    validation_issues.extend(source_hash_issues)
    validation_issues.extend(source_content_issues)
    same_run_integrated_demo_proven = bool(
        manifest_exists
        and target_run_id
        and not validation_issues
        and not missing_surfaces
        and not cross_run_surfaces
        and manifest_claims_same_run
        and concurrent_observation_proven
    )

    return {
        "schema": "ur10e_same_run_integrated_binding_audit_v1",
        "generated_at": generated,
        "goal_lineage": GOAL_LINEAGE,
        "mode": "offline_report_level_same_run_integrated_binding",
        "claim_tier": "visual_only",
        "live_authorization": {
            "robot_motion_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "urscript_authorized": False,
            "zero_ftsensor_authorized": False,
            "payload_tcp_safety_writes_authorized": False,
            "real_bench_live_contact_authorized": False,
        },
        "integrated_demo_manifest": rel(manifest_path),
        "target_run_id": target_run_id,
        "artifact_rows": artifact_rows,
        "missing_surfaces": sorted(missing_surfaces),
        "cross_run_surfaces": sorted(cross_run_surfaces),
        "manifest_same_run_binding": {
            "binding_status": manifest_binding.get("binding_status"),
            "visual_rviz_simulated_ft_same_run": manifest_binding.get("visual_rviz_simulated_ft_same_run"),
            "visual_rviz_physical_gazebo_contact_same_run": manifest_binding.get("visual_rviz_physical_gazebo_contact_same_run"),
            "step_rnn_physical_gazebo_contact_same_run": manifest_binding.get("step_rnn_physical_gazebo_contact_same_run"),
        },
        "concurrent_observation": {
            **concurrent_observation,
            "required_surfaces": CONCURRENT_OBSERVATION_REQUIRED_SURFACES,
            "concurrent_observation_proven": concurrent_observation_proven,
        },
        "source_content_validation": {
            "source_content_proven": not source_content_issues and not source_hash_issues,
            "validation_issues": source_hash_issues + source_content_issues,
        },
        "same_run_integrated_demo_proven": same_run_integrated_demo_proven,
        "binding_status": "same_run_integrated_demo_proven"
        if same_run_integrated_demo_proven
        else (
            "same_run_paths_without_concurrent_observation"
            if not concurrent_observation_proven and not missing_surfaces and not cross_run_surfaces
            else "cross_run_evidence_only"
        ),
        "validation_issues": validation_issues,
        "blocker": None
        if same_run_integrated_demo_proven
        else "No same-run P6 manifest binds visual, RViz, simulated FT, Step/RNN, and Gazebo contact physics evidence.",
        "forbidden_claim": "full UR10e reproduction acceptance; real bench/live contact; physical Gazebo contact outside explicitly proven same-run evidence",
    }


def write_audit(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    integrated_demo_manifest_path: Path | None = DEFAULT_INTEGRATED_DEMO_MANIFEST,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "same_run_integrated_binding_audit.json"
    payload = build_audit(
        generated_at=generated_at,
        integrated_demo_manifest_path=integrated_demo_manifest_path,
    )
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--integrated-demo-manifest", type=Path, default=DEFAULT_INTEGRATED_DEMO_MANIFEST)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        write_audit(
            args.output_dir,
            generated_at=args.generated_at,
            integrated_demo_manifest_path=args.integrated_demo_manifest,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
