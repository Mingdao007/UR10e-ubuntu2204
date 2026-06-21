#!/usr/bin/env python3
"""Build a fail-closed P6 integrated demo readiness audit.

This is an offline report-level gate. It does not run Gazebo, RViz, ROS, or
any live bench surface. It decides whether the existing evidence is sufficient
to claim readiness for the P6 integrated no-live-robot demo, and it keeps full
goal acceptance blocked when the evidence is missing, ambiguous, or scoped only
to a narrower witness.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from build_timed_audit_coverage_audit import (
    GOAL_START_AT as TIMED_AUDIT_GOAL_START_AT,
    expected_opus_checkpoint_hours as build_expected_opus_checkpoint_hours,
    expected_subagent_hours as build_expected_subagent_hours,
    timed_audit_coverage_summary as build_timed_audit_coverage_summary,
)
from build_same_run_integrated_binding_audit import (
    build_audit as build_same_run_integrated_binding_audit,
)
from build_dual_sensor_total_wrench_audit import (
    build_audit as build_dual_sensor_total_wrench_audit,
)


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
RUNS = EXPERIMENT_ROOT / "runs"
GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"
HANDOFF_ROOT = Path("/home/andy/codex_handoffs")

DEFAULT_P3_AUDIT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_055357_p3_merged_32row_visual_rviz_audit"
    / "p3_visual_rviz_evidence_audit.json"
)
DEFAULT_POST_GATE_VISUAL_FOUNDATION_ROW = (
    RUNS
    / "ur10e_gazebo_visual_mesh_foundation_20260621_115619_subtle_affordance_gui"
    / "matrix_gui_real_aligned"
    / "step5b"
    / "close_detail"
    / "row_summary.json"
)
DEFAULT_STEP_STATUS_AUDIT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0910_step_status_pdf_truth_binding"
    / "step_status_rnn_audit.json"
)

CLAIM_TIERS = [
    "visual_only",
    "virtual/software force-loop",
    "simulated_ft",
    "physical Gazebo collision/contact physics",
    "real bench/live contact",
]

CONTACT_STAGE_IDS = ["step5b", "step5d", "step6b", "step7", "step8"]
EXPECTED_STAGE_IDS = ["step5a", "step5b", "step5c", "step5d", "step6a", "step6b", "step7", "step8"]
REQUIRED_DEMO_FIELDS = [
    "platform_trajectory_evidence",
    "eoat_tooling_evidence",
    "contact_surface_evidence",
    "tcp_distance_evidence",
    "simulated_ft_artifacts",
    "step_rnn_pipeline_artifact",
    "gazebo_gui_evidence_paths",
    "rviz_evidence_paths",
]
REQUIRED_PLOTS = [
    "wrench_vs_time",
    "contact_state_vs_time",
    "tcp_distance_to_surface_vs_time",
    "force_threshold_crossing",
    "latency_staleness",
    "gravity_residual",
]
OPTIONAL_UNSUPPORTED_PLOTS = {"gravity_residual"}
MESH_VISUAL_SUFFIXES = {".dae", ".mesh", ".obj", ".stl", ".stp", ".step"}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path | str | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(WORKSPACE.resolve()))
    except (OSError, ValueError):
        return str(path)


def mesh_visual_uri_present(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    path_part = value.split("#", 1)[0].split("?", 1)[0]
    return Path(path_part).suffix.lower() in MESH_VISUAL_SUFFIXES


def workspace_path(path: str | None) -> Path | None:
    if not path:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return WORKSPACE / candidate


def sha256_file(path: str | None) -> str | None:
    candidate = workspace_path(path)
    if candidate is None or not candidate.is_file():
        return None
    digest = hashlib.sha256()
    with candidate.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def external_gate_validation_issues(payload: dict[str, Any], *, expected_schema: str) -> list[str]:
    issues: list[str] = []
    if payload.get("schema") != expected_schema:
        issues.append(f"schema:not_{expected_schema}")
    if payload.get("goal_lineage") != GOAL_LINEAGE:
        issues.append("goal_lineage:mismatch_or_missing")
    if not payload.get("generated_at"):
        issues.append("generated_at:missing")
    return issues


def invalid_timed_audit_coverage(payload: dict[str, Any], issues: list[str]) -> dict[str, Any]:
    coverage = payload.get("timed_audit_coverage")
    if not isinstance(coverage, dict):
        coverage = {}
    result = dict(coverage)
    result["full_acceptance_timed_audit_ready"] = False
    result["claim_tier"] = result.get("claim_tier", "visual_only")
    result["external_artifact_validation_issues"] = issues
    result["blocker"] = "External timed audit artifact failed schema/lineage validation."
    return result


def invalid_same_run_binding(payload: dict[str, Any], issues: list[str]) -> dict[str, Any]:
    result = dict(payload)
    result["same_run_integrated_demo_proven"] = False
    result["binding_status"] = "external_artifact_invalid"
    result["validation_issues"] = list(payload.get("validation_issues", [])) + issues
    result["blocker"] = "External same-run binding artifact failed schema/lineage validation."
    return result


def invalid_dual_sensor_total_wrench(payload: dict[str, Any], issues: list[str]) -> dict[str, Any]:
    result = dict(payload)
    total_invalid = dual_sensor_total_wrench_invalidated(payload, issues)
    result["total_contact_wrench_proven"] = bool(payload.get("total_contact_wrench_proven")) and not total_invalid
    result["same_run_dual_sensor_observation_proven"] = False
    result["validation_issues"] = list(payload.get("validation_issues", [])) + issues
    blockers = set(payload.get("blockers", []))
    if total_invalid:
        blockers.add("total_contact_wrench:not_proven")
    elif result["total_contact_wrench_proven"]:
        blockers.discard("total_contact_wrench:not_proven")
    blockers.add("same_run_dual_sensor_observation:not_proven")
    result["blockers"] = sorted(blockers)
    return result


def dual_sensor_total_wrench_invalidated(payload: dict[str, Any], issues: list[str]) -> bool:
    if payload.get("total_contact_wrench_proven") is not True:
        return True
    if "total_contact_wrench:not_proven" in list_value(payload, "blockers"):
        return True
    total_issue_tokens = (
        "schema:",
        "goal_lineage:",
        "generated_at:",
        "missing_surfaces",
        "cross_run_surfaces",
        "required_surfaces:",
        "artifact_rows:",
        ".sha256:",
        ".path:",
        "total_contact_wrench",
    )
    return any(issue.startswith(total_issue_tokens) or "total_contact_wrench" in issue for issue in issues)


def list_value(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    return value if isinstance(value, list) else []


def external_gate_internal_issues(payload: dict[str, Any], *, required_surfaces: set[str]) -> list[str]:
    issues: list[str] = []
    if list_value(payload, "missing_surfaces"):
        issues.append("missing_surfaces:not_empty")
    if list_value(payload, "cross_run_surfaces"):
        issues.append("cross_run_surfaces:not_empty")
    if list_value(payload, "validation_issues"):
        issues.append("validation_issues:not_empty")
    if list_value(payload, "blockers"):
        issues.append("blockers:not_empty")
    rows = payload.get("artifact_rows")
    if required_surfaces and not isinstance(rows, list):
        issues.append("artifact_rows:missing")
    elif required_surfaces:
        rows_by_surface = {
            str(row.get("surface")): row
            for row in rows
            if isinstance(row, dict)
        }
        observed = {
            surface
            for surface, row in rows_by_surface.items()
            if row.get("exists") is True
        }
        missing = sorted(required_surfaces - observed)
        if missing:
            issues.append("artifact_rows:missing_surfaces:" + ",".join(missing))
        for surface in sorted(required_surfaces & observed):
            row = rows_by_surface[surface]
            path_value = row.get("path")
            if not path_value:
                issues.append(f"artifact_rows.{surface}.path:missing")
                continue
            path = workspace_path(str(path_value))
            if path is None or not path.is_file():
                issues.append(f"artifact_rows.{surface}.path:missing_or_unreadable")
                continue
            expected_sha = row.get("sha256")
            if not expected_sha:
                issues.append(f"artifact_rows.{surface}.sha256:missing")
                continue
            actual_sha = sha256_file(str(path_value))
            if actual_sha != expected_sha:
                issues.append(f"artifact_rows.{surface}.sha256:mismatch")
    return issues


def external_same_run_concurrent_observation_issues(
    payload: dict[str, Any],
    *,
    required_surfaces: set[str],
) -> list[str]:
    issues: list[str] = []
    observation = payload.get("concurrent_observation")
    if not isinstance(observation, dict):
        return ["concurrent_observation:missing"]
    if observation.get("concurrent_observation_proven") is not True:
        issues.append("concurrent_observation.concurrent_observation_proven:not_true")
    if not observation.get("observation_id"):
        issues.append("concurrent_observation.observation_id:missing")
    if observation.get("same_run_concurrent_observation_explicit") is not True:
        issues.append("concurrent_observation.same_run_concurrent_observation_explicit:not_true")
    time_window = observation.get("time_window") if isinstance(observation.get("time_window"), dict) else {}
    if not time_window.get("start"):
        issues.append("concurrent_observation.time_window.start:missing")
    if not time_window.get("end"):
        issues.append("concurrent_observation.time_window.end:missing")
    if not time_window.get("clock_source"):
        issues.append("concurrent_observation.time_window.clock_source:missing")
    surfaces = observation.get("surfaces") if isinstance(observation.get("surfaces"), list) else []
    observed = {str(surface) for surface in surfaces if str(surface)}
    missing = sorted(required_surfaces - {"p6_manifest"} - observed)
    if missing:
        issues.append("concurrent_observation.surfaces:missing:" + ",".join(missing))
    return issues


def external_dual_sensor_concurrent_observation_issues(payload: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    container = payload.get("same_run_dual_sensor_observation")
    if not isinstance(container, dict):
        return ["same_run_dual_sensor_observation:missing"]
    observation = container.get("same_run_concurrent_dual_sensor_observation")
    if not isinstance(observation, dict):
        return ["same_run_concurrent_dual_sensor_observation:missing"]
    if observation.get("same_run_concurrent_dual_sensor_observation_proven") is not True:
        issues.append("same_run_concurrent_dual_sensor_observation.proven:not_true")
    if not observation.get("observation_id"):
        issues.append("same_run_concurrent_dual_sensor_observation.observation_id:missing")
    if observation.get("same_run_concurrent_dual_sensor_observation_explicit") is not True:
        issues.append("same_run_concurrent_dual_sensor_observation.explicit:not_true")
    time_window = observation.get("time_window") if isinstance(observation.get("time_window"), dict) else {}
    if not time_window.get("start"):
        issues.append("same_run_concurrent_dual_sensor_observation.time_window.start:missing")
    if not time_window.get("end"):
        issues.append("same_run_concurrent_dual_sensor_observation.time_window.end:missing")
    if not time_window.get("clock_source"):
        issues.append("same_run_concurrent_dual_sensor_observation.time_window.clock_source:missing")
    surfaces = observation.get("surfaces") if isinstance(observation.get("surfaces"), dict) else {}
    missing = sorted(
        surface
        for surface in (
            "stage_simulated_ft_manifest",
            "p2_contact_correlation_audit",
            "step_status_rnn_audit",
        )
        if not surfaces.get(surface)
    )
    if missing:
        issues.append("same_run_concurrent_dual_sensor_observation.surfaces:missing:" + ",".join(missing))
    return issues


def physical_gazebo_contact_claim_boundary(step_p2: dict[str, Any]) -> dict[str, Any]:
    eoat_collision_count = int(step_p2.get("eoat_collision_count") or 0)
    contact_pair_log_evidence = bool(step_p2.get("contact_pair_log_evidence"))
    adapter_verified_wrench = bool(step_p2.get("adapter_verified_gazebo_contact_wrench"))
    wrench_contact_correlation = bool(step_p2.get("wrench_contact_correlation"))
    force_contact_physics_proven = bool(step_p2.get("force_contact_physics_proven"))
    required_conditions = {
        "eoat_collision_count_gt_zero": eoat_collision_count > 0,
        "contact_pair_log_evidence": contact_pair_log_evidence,
        "adapter_verified_gazebo_contact_wrench": adapter_verified_wrench,
        "wrench_contact_correlation": wrench_contact_correlation,
        "force_contact_physics_proven": force_contact_physics_proven,
    }
    blockers = [
        name + ":not_proven"
        for name, proven in required_conditions.items()
        if not proven
    ]
    proven = not blockers
    return {
        "claim_tier": "physical Gazebo collision/contact physics" if proven else "visual_only",
        "proven": proven,
        "blocked_or_not_proven": not proven,
        "required_conditions": required_conditions,
        "eoat_collision_count": eoat_collision_count,
        "blockers": blockers,
        "downgrade_rule": "physical Gazebo collision/contact physics is blocked/not proven when EOAT collision evidence, contact pair/log evidence, wrench/contact correlation, or force_contact_physics_proven is missing.",
    }


def timed_audit_coverage_summary(
    handoff_root: Path = HANDOFF_ROOT,
    *,
    generated_at: str | None = None,
    goal_start_at: str | None = None,
) -> dict[str, Any]:
    """Summarize timed Opus/subagent records without upgrading acceptance."""
    return build_timed_audit_coverage_summary(
        handoff_root=handoff_root,
        generated_at=generated_at,
        goal_start_at=goal_start_at,
    )


def claim_boundary_gate() -> dict[str, Any]:
    return {
        "fail_closed": True,
        "tiers": CLAIM_TIERS,
        "rules": [
            "Gazebo/RViz screenshots, EOAT visibility, TCP marker, model pose, and observer-view evidence support only visual_only unless backed by stronger artifacts.",
            "gazebo_joint_state_fk_virtual_surface_model supports only virtual/software force-loop.",
            "simulated_ft requires stamp, frame_id, source, status, baseline, and log evidence.",
            "physical Gazebo collision/contact physics requires EOAT collision evidence, contact pair/log evidence, and wrench/contact correlation.",
            "If eoat_collision_count=0 or force_contact_physics_proven=false, physical Gazebo collision/contact physics is blocked/not proven and claims must downgrade.",
            "real bench/live contact is not authorized in this goal; no simulated_ft or Gazebo evidence may upgrade into real bench/live contact.",
        ],
    }


def p3_visual_rviz_summary(p3: dict[str, Any], *, path: Path) -> dict[str, Any]:
    coverage = p3.get("audit_coverage") if isinstance(p3.get("audit_coverage"), dict) else {}
    gazebo = p3.get("gazebo_observer_evidence") if isinstance(p3.get("gazebo_observer_evidence"), dict) else {}
    rviz = (
        p3.get("p3_requirement_status", {})
        .get("rviz_debug_evidence", {})
        if isinstance(p3.get("p3_requirement_status"), dict)
        else {}
    )
    gazebo_rows = int(coverage.get("gazebo_rows") or gazebo.get("row_count") or 0)
    visual_pass_count = int(coverage.get("gazebo_observer_visual_pass_count") or gazebo.get("observer_visual_pass_count") or 0)
    rviz_items = bool(coverage.get("rviz_all_required_items_evidenced") or rviz.get("all_required_items_evidenced"))
    rviz_rendered = bool(
        coverage.get("rviz_rendered_screenshot_evidence_present")
        or rviz.get("rendered_screenshot_evidence_present")
    )
    visual_pass = gazebo_rows > 0 and visual_pass_count == gazebo_rows
    lineage = p3.get("goal_lineage")
    return {
        "artifact": rel(path),
        "schema": p3.get("schema"),
        "goal_lineage": lineage,
        "goal_lineage_matches": lineage == GOAL_LINEAGE,
        "claim_tier": "visual_only",
        "gazebo_rows": gazebo_rows,
        "gazebo_observer_visual_pass_count": visual_pass_count,
        "gazebo_observer_visual_pass": visual_pass,
        "rviz_all_required_items_evidenced": rviz_items,
        "rviz_rendered_screenshot_evidence_present": rviz_rendered,
        "p3_visual_rviz_ready": bool(visual_pass and rviz_items and rviz_rendered),
        "forbidden_claim": "simulated_ft; physical Gazebo collision/contact physics; real bench/live contact",
    }


def post_gate_visual_foundation_summary(path: Path) -> dict[str, Any]:
    base = {
        "artifact": rel(path),
        "claim_tier": "visual_only",
        "schema": None,
        "post_gate_visual_foundation_ready": False,
        "support_scope": "single_step5b_close_detail_observer_row_only",
        "forbidden_claim": "simulated_ft; physical Gazebo collision/contact physics; real bench/live contact; full Step5/6/7/8 visual matrix",
    }
    if not path.is_file():
        return {
            **base,
            "status": "missing",
            "validation_issues": ["post_gate_visual_foundation_row:missing"],
        }
    try:
        row = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            **base,
            "status": "unreadable",
            "validation_issues": [f"post_gate_visual_foundation_row:unreadable:{type(exc).__name__}"],
        }

    git = row.get("git_provenance") if isinstance(row.get("git_provenance"), dict) else {}
    criteria = row.get("observer_visual_criteria") if isinstance(row.get("observer_visual_criteria"), dict) else {}
    eoat_mesh_uri = row.get("actual_eoat_mesh_visual_uri")
    contact_surface_mesh_uri = row.get("actual_contact_surface_mesh_uri")
    required = {
        "observer_visual_pass": row.get("observer_visual_pass") is True,
        "actual_eoat_mesh_visual_present": row.get("actual_eoat_mesh_visual_present") is True,
        "actual_eoat_mesh_visual_uri_mesh_like": mesh_visual_uri_present(eoat_mesh_uri),
        "actual_contact_surface_mesh_visual_present": row.get("actual_contact_surface_mesh_visual_present") is True,
        "actual_contact_surface_mesh_visual_uri_mesh_like": mesh_visual_uri_present(contact_surface_mesh_uri),
        "live_scene_actual_eoat_mesh_visuals_present": row.get("live_scene_actual_eoat_mesh_visuals_present") is True,
        "live_scene_actual_contact_surface_mesh_visuals_present": (
            row.get("live_scene_actual_contact_surface_mesh_visuals_present") is True
        ),
        "primitive_proxy_not_primary_visual": row.get("primitive_proxy_not_primary_visual") is True,
        "primitive_proxy_not_main_visual_cue": row.get("primitive_proxy_not_main_visual_cue") is True,
        "observer_level_demo_realism": row.get("observer_level_demo_realism") is True,
        "visual_evidence_captured": row.get("visual_evidence_captured") is True,
        "scripted_camera_evidence_captured": row.get("scripted_camera_evidence_captured") is True,
        "marker_style_auxiliary_only": row.get("marker_style") == "minimal_tcp_dot",
        "clean_git_provenance": git.get("dirty") is False,
        "force_contact_physics_not_inferred": row.get("force_contact_physics_proven") is False,
    }
    validation_issues = [f"{name}:not_true" for name, ok in required.items() if not ok]
    return {
        **base,
        "schema": row.get("schema"),
        "status": "ready" if not validation_issues else "not_ready",
        "post_gate_visual_foundation_ready": not validation_issues,
        "stage": row.get("stage"),
        "view": row.get("view"),
        "observer_visual_pass": row.get("observer_visual_pass"),
        "observer_visual_failure_reasons": row.get("observer_visual_failure_reasons", []),
        "observer_visual_gate_version": row.get("observer_visual_gate_version"),
        "observer_visual_reviewed_at": row.get("observer_visual_reviewed_at"),
        "scripted_camera_final_png": row.get("scripted_camera_final_png"),
        "scripted_camera_sha256": row.get("scripted_camera_sha256"),
        "video_path": row.get("video_path"),
        "marker_style": row.get("marker_style"),
        "live_scene_content_branch": row.get("live_scene_content_branch"),
        "live_scene_marker_visual_role": row.get("live_scene_marker_visual_role"),
        "live_scene_actual_eoat_mesh_visuals_present": row.get("live_scene_actual_eoat_mesh_visuals_present"),
        "live_scene_actual_contact_surface_mesh_visuals_present": row.get(
            "live_scene_actual_contact_surface_mesh_visuals_present"
        ),
        "actual_eoat_mesh_visual_uri": eoat_mesh_uri,
        "actual_contact_surface_mesh_visual_uri": contact_surface_mesh_uri,
        "primitive_proxy_not_main_visual_cue": row.get("primitive_proxy_not_main_visual_cue"),
        "observer_level_demo_realism": row.get("observer_level_demo_realism"),
        "force_loop_success": row.get("force_loop_success"),
        "force_contact_physics_proven": row.get("force_contact_physics_proven"),
        "git_provenance": git or None,
        "observer_visual_criteria": criteria or None,
        "validation_issues": validation_issues,
    }


def step_status_summary(step: dict[str, Any], *, path: Path) -> dict[str, Any]:
    coverage = step.get("audit_coverage") if isinstance(step.get("audit_coverage"), dict) else {}
    rows = step.get("step_status_matrix") if isinstance(step.get("step_status_matrix"), list) else []
    stage_ids = [str(row.get("stage_id")) for row in rows if isinstance(row, dict)]
    stage_id_counts = Counter(stage_ids)
    duplicate_stage_ids = sorted(stage_id for stage_id, count in stage_id_counts.items() if count > 1)
    missing_stage_ids = [stage_id for stage_id in EXPECTED_STAGE_IDS if stage_id not in stage_id_counts]
    extra_stage_ids = sorted(stage_id for stage_id in stage_id_counts if stage_id not in EXPECTED_STAGE_IDS)
    row_by_id = {str(row.get("stage_id")): row for row in rows if isinstance(row, dict)}
    contact_rows = [row_by_id.get(stage_id, {}) for stage_id in CONTACT_STAGE_IDS]
    contact_stages_simulated_ft = all(row.get("claim_tier") == "simulated_ft" for row in contact_rows)
    contact_stages_have_log_evidence = all(bool(row.get("per_stage_simulated_ft_log_evidence")) for row in contact_rows)
    p2 = step.get("p2_physical_gazebo_contact") if isinstance(step.get("p2_physical_gazebo_contact"), dict) else {}
    rnn = step.get("rnn_interface_table") if isinstance(step.get("rnn_interface_table"), list) else []
    rnn_by_name = {str(row.get("interface")): row for row in rnn if isinstance(row, dict)}
    inner = rnn_by_name.get("inner_strict_rnn_solver", {})
    inner_status = str(inner.get("status") or "missing")
    strict_gate = (
        step.get("strict_rnn_final_acceptance_gate")
        if isinstance(step.get("strict_rnn_final_acceptance_gate"), dict)
        else {}
    )
    if strict_gate:
        strict_rnn_final_acceptance = bool(strict_gate.get("strict_rnn_final_acceptance_allowed"))
    else:
        strict_rnn_final_acceptance = (
            inner_status not in {"blocked_pending_pdf_truth_extraction", "missing"}
            and not inner_status.startswith("blocked")
        )
    p2_claim_boundary = physical_gazebo_contact_claim_boundary(p2)
    reported_p2_claim_tier = p2.get("claim_tier") or coverage.get("p2_claim_tier")
    p2_claim_tier = (
        "physical Gazebo collision/contact physics"
        if reported_p2_claim_tier == "physical Gazebo collision/contact physics"
        and p2_claim_boundary["proven"]
        else "visual_only"
        if reported_p2_claim_tier == "physical Gazebo collision/contact physics"
        else reported_p2_claim_tier
    )
    lineage = step.get("goal_lineage")
    stage_status_matrix = [
        {
            "stage_id": str(row.get("stage_id")),
            "claim_tier": row.get("claim_tier"),
            "visual_status": "covered_by_p3_visual_only_audit",
            "simulated_ft_status": row.get("simulated_ft_status"),
            "gazebo_contact_physics_status": row.get("gazebo_contact_physics_status"),
            "inner_rnn_status": row.get("inner_rnn_status"),
            "outer_loop_status": row.get("outer_loop_status"),
            "allowed_claim": row.get("allowed_claim"),
            "forbidden_claim": row.get("forbidden_claim"),
            "current_blocker": row.get("current_blocker"),
        }
        for row in rows
        if isinstance(row, dict)
    ]
    return {
        "artifact": rel(path),
        "schema": step.get("schema"),
        "goal_lineage": lineage,
        "goal_lineage_matches": lineage == GOAL_LINEAGE,
        "stage_rows": int(coverage.get("stage_rows") or len(rows)),
        "stage_ids": stage_ids,
        "expected_stage_ids": EXPECTED_STAGE_IDS,
        "stage_set_exact": not missing_stage_ids and not extra_stage_ids and not duplicate_stage_ids,
        "missing_stage_ids": missing_stage_ids,
        "extra_stage_ids": extra_stage_ids,
        "duplicate_stage_ids": duplicate_stage_ids,
        "stage_simulated_ft_manifest_status": coverage.get("stage_simulated_ft_manifest_status"),
        "per_stage_simulated_ft_attached_count": int(coverage.get("per_stage_simulated_ft_attached_count") or 0),
        "contact_stages_simulated_ft": contact_stages_simulated_ft,
        "contact_stages_have_log_evidence": contact_stages_have_log_evidence,
        "contact_stage_status": {
            stage_id: {
                "claim_tier": row_by_id.get(stage_id, {}).get("claim_tier"),
                "simulated_ft_status": row_by_id.get(stage_id, {}).get("simulated_ft_status"),
                "gazebo_contact_physics_status": row_by_id.get(stage_id, {}).get("gazebo_contact_physics_status"),
                "current_blocker": row_by_id.get(stage_id, {}).get("current_blocker"),
            }
            for stage_id in CONTACT_STAGE_IDS
        },
        "stage_status_matrix": stage_status_matrix,
        "p2_claim_tier": p2_claim_tier,
        "p2_reported_claim_tier": reported_p2_claim_tier,
        "p2_physical_gazebo_contact_claim_boundary": p2_claim_boundary,
        "p2_scope": p2.get("scope") or coverage.get("p2_scope"),
        "standalone_p2_physical_witness": bool(
            reported_p2_claim_tier == "physical Gazebo collision/contact physics"
            and p2_claim_boundary["proven"]
        ),
        "stage_specific_contact_physics_proven": bool(
            p2.get("stage_specific_contact_physics_proven")
            or coverage.get("stage_specific_contact_physics_proven")
        ),
        "total_contact_wrench_proven": bool(p2.get("total_contact_wrench_proven")),
        "same_run_concurrent_dual_sensor_observation": bool(p2.get("same_run_concurrent_dual_sensor_observation")),
        "rnn_interfaces": len(rnn),
        "inner_strict_rnn_status": inner_status,
        "strict_rnn_final_acceptance": strict_rnn_final_acceptance,
        "strict_rnn_final_acceptance_gate": {
            "status": strict_gate.get("status", "missing") if strict_gate else "missing",
            "claim_tier": strict_gate.get("claim_tier") if strict_gate else None,
            "strict_rnn_final_acceptance_allowed": bool(
                strict_gate.get("strict_rnn_final_acceptance_allowed")
            )
            if strict_gate
            else strict_rnn_final_acceptance,
            "blockers": strict_gate.get("blockers", []) if strict_gate else [],
            "evidence": strict_gate.get("evidence", {}) if strict_gate else {},
            "forbidden_claim": strict_gate.get("forbidden_claim") if strict_gate else None,
        },
        "full_acceptance_allowed_by_step_audit": bool(coverage.get("full_acceptance_allowed")),
        "full_acceptance_blocker": coverage.get("full_acceptance_blocker"),
    }


def validate_path_list(payload: dict[str, Any], field: str, issues: list[str]) -> None:
    value = payload.get(field)
    if not isinstance(value, list) or not value:
        issues.append(f"{field}:missing_or_empty")
        return
    for index, item in enumerate(value):
        if not item:
            issues.append(f"{field}[{index}]:empty")


def validate_tcp_distance_evidence(payload: dict[str, Any], issues: list[str]) -> dict[str, Any]:
    path_value = payload.get("tcp_distance_evidence")
    result: dict[str, Any] = {
        "path": path_value,
        "status": "missing",
        "claim_tier": "visual_only",
        "tcp_distance_time_series_supported": False,
        "validation_issues": [],
    }
    if not path_value:
        issues.append("tcp_distance_evidence:missing")
        result["validation_issues"].append("tcp_distance_evidence:missing")
        return result

    path = workspace_path(str(path_value))
    if path is None or not path.is_file():
        issues.append("tcp_distance_evidence:missing_or_unreadable")
        result["status"] = "missing_or_unreadable"
        result["validation_issues"].append("tcp_distance_evidence:missing_or_unreadable")
        return result

    try:
        evidence = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        issues.append(f"tcp_distance_evidence:unreadable:{type(exc).__name__}")
        result["status"] = "unreadable"
        result["validation_issues"].append(f"tcp_distance_evidence:unreadable:{type(exc).__name__}")
        return result

    result.update(
        {
            "path": rel(path),
            "schema": evidence.get("schema"),
            "status": evidence.get("status"),
            "claim_tier": evidence.get("claim_tier") or "visual_only",
            "tcp_distance_time_series_supported": evidence.get("tcp_distance_time_series_supported") is True,
            "candidate_source_audit": evidence.get("candidate_source_audit", []),
        }
    )
    if evidence.get("schema") != "ur10e_p6_tcp_distance_evidence_audit_v1":
        issues.append("tcp_distance_evidence.schema:unsupported")
        result["validation_issues"].append("tcp_distance_evidence.schema:unsupported")
    if result["claim_tier"] not in CLAIM_TIERS:
        issues.append("tcp_distance_evidence.claim_tier:unsupported")
        result["validation_issues"].append("tcp_distance_evidence.claim_tier:unsupported")
    return result


def validate_demo_manifest(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "manifest_path": None,
            "status": "missing",
            "claim_tier": "visual_only",
            "valid": False,
            "required_fields": REQUIRED_DEMO_FIELDS,
            "required_plots": REQUIRED_PLOTS,
            "validation_issues": ["integrated_demo_manifest:missing"],
        }
    if not path.is_file():
        return {
            "manifest_path": rel(path),
            "status": "missing",
            "claim_tier": "visual_only",
            "valid": False,
            "required_fields": REQUIRED_DEMO_FIELDS,
            "required_plots": REQUIRED_PLOTS,
            "validation_issues": ["integrated_demo_manifest:missing"],
        }
    issues: list[str] = []
    try:
        payload = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "manifest_path": rel(path),
            "status": "unreadable",
            "claim_tier": "visual_only",
            "valid": False,
            "required_fields": REQUIRED_DEMO_FIELDS,
            "required_plots": REQUIRED_PLOTS,
            "validation_issues": [f"integrated_demo_manifest:unreadable:{type(exc).__name__}"],
        }

    if payload.get("schema") != "ur10e_p6_integrated_demo_manifest_v1":
        issues.append("schema:not_ur10e_p6_integrated_demo_manifest_v1")
    if payload.get("goal_lineage") != GOAL_LINEAGE:
        issues.append("goal_lineage:mismatch_or_missing")
    if payload.get("fail_closed") is not True:
        issues.append("fail_closed:not_true")
    manifest_claim_tier = payload.get("claim_tier") or "visual_only"
    if manifest_claim_tier not in CLAIM_TIERS:
        issues.append("claim_tier:unsupported")
        manifest_claim_tier = "visual_only"
    for field in (
        "platform_trajectory_evidence",
        "eoat_tooling_evidence",
        "contact_surface_evidence",
        "step_rnn_pipeline_artifact",
    ):
        if not payload.get(field):
            issues.append(f"{field}:missing")
    for field in ("simulated_ft_artifacts", "gazebo_gui_evidence_paths", "rviz_evidence_paths"):
        validate_path_list(payload, field, issues)
    tcp_distance_evidence = validate_tcp_distance_evidence(payload, issues)

    plots = payload.get("plots")
    if not isinstance(plots, dict):
        issues.append("plots:missing")
        plots = {}
    for plot_name in REQUIRED_PLOTS:
        plot = plots.get(plot_name)
        if not isinstance(plot, dict):
            issues.append(f"plots.{plot_name}:missing")
            continue
        if not plot.get("path"):
            issues.append(f"plots.{plot_name}.path:missing")
        if not plot.get("unit_labels"):
            issues.append(f"plots.{plot_name}.unit_labels:missing")
        if not plot.get("frame_label"):
            issues.append(f"plots.{plot_name}.frame_label:missing")
        if not plot.get("claim_tier"):
            issues.append(f"plots.{plot_name}.claim_tier:missing")
        elif plot.get("claim_tier") not in CLAIM_TIERS:
            issues.append(f"plots.{plot_name}.claim_tier:unsupported")
        if plot.get("supported") is not True and plot_name not in OPTIONAL_UNSUPPORTED_PLOTS:
            issues.append(f"plots.{plot_name}:unsupported")
        if plot.get("supported") is not True and plot_name in OPTIONAL_UNSUPPORTED_PLOTS and not plot.get("unsupported_reason"):
            issues.append(f"plots.{plot_name}.unsupported_reason:missing")
        if (
            plot_name == "tcp_distance_to_surface_vs_time"
            and plot.get("supported") is True
            and not tcp_distance_evidence["tcp_distance_time_series_supported"]
        ):
            issues.append("tcp_distance_evidence:not_supporting_supported_plot")

    return {
        "manifest_path": rel(path),
        "status": "valid" if not issues else "invalid",
        "claim_tier": str(manifest_claim_tier) if not issues else "visual_only",
        "valid": not issues,
        "required_fields": REQUIRED_DEMO_FIELDS,
        "required_plots": REQUIRED_PLOTS,
        "plot_status": {
            plot_name: (
                "supported"
                if isinstance(plots.get(plot_name), dict) and plots[plot_name].get("supported") is True
                else "unsupported"
                if isinstance(plots.get(plot_name), dict)
                else "missing"
            )
            for plot_name in REQUIRED_PLOTS
        },
        "tcp_distance_evidence": tcp_distance_evidence,
        "validation_issues": issues,
    }


def build_blockers(
    *,
    p3: dict[str, Any],
    post_gate_visual: dict[str, Any],
    step: dict[str, Any],
    demo_manifest: dict[str, Any],
) -> list[str]:
    blockers: list[str] = []
    if not p3["goal_lineage_matches"]:
        blockers.append("p3_goal_lineage:mismatch_or_missing")
    if not step["goal_lineage_matches"]:
        blockers.append("step_goal_lineage:mismatch_or_missing")
    if not p3["p3_visual_rviz_ready"]:
        blockers.append("p3_visual_rviz:not_ready")
    if not post_gate_visual["post_gate_visual_foundation_ready"]:
        blockers.append("post_gate_visual_foundation:not_ready")
    if not step["stage_set_exact"]:
        blockers.append("step_status_matrix:stage_set_not_exact")
    if not step["full_acceptance_allowed_by_step_audit"]:
        blockers.append("step_status_full_acceptance:not_allowed")
    if not step["strict_rnn_final_acceptance"]:
        blockers.append("strict_rnn_final_acceptance:not_proven")
    if step["stage_simulated_ft_manifest_status"] != "valid":
        blockers.append("stage_simulated_ft_manifest:not_valid")
    if not step["contact_stages_simulated_ft"] or not step["contact_stages_have_log_evidence"]:
        blockers.append("contact_stage_simulated_ft:not_all_verified")
    if not demo_manifest["valid"]:
        blockers.append("integrated_demo_manifest:not_valid")
    return blockers


def full_goal_blockers(
    *,
    step: dict[str, Any],
    p6_blockers: list[str],
    timed_audit_coverage: dict[str, Any],
    same_run_binding: dict[str, Any],
    dual_sensor_total_wrench: dict[str, Any],
    claim_boundary_validation_issues: list[str],
) -> list[str]:
    blockers = [f"p6:{blocker}" for blocker in p6_blockers]
    if not step["stage_specific_contact_physics_proven"]:
        blockers.append("per_stage_physical_gazebo_contact:not_proven")
    if claim_boundary_validation_issues:
        blockers.append("claim_boundary:not_verified")
    if not dual_sensor_total_wrench["total_contact_wrench_proven"]:
        blockers.append("total_contact_wrench:not_proven")
    if not dual_sensor_total_wrench["same_run_dual_sensor_observation_proven"]:
        blockers.append("same_run_dual_sensor_observation:not_proven")
    if not step["strict_rnn_final_acceptance"]:
        blockers.append("strict_rnn_final_acceptance:not_proven")
    if not same_run_binding["same_run_integrated_demo_proven"]:
        blockers.append("same_run_integrated_binding:not_proven")
    if not timed_audit_coverage["full_acceptance_timed_audit_ready"]:
        blockers.append("timed_audit_coverage:not_verified")
    blockers.append("real_bench_live_contact:not_authorized")
    return blockers


def current_claim_tier_table(
    *,
    p3: dict[str, Any],
    post_gate_visual: dict[str, Any],
    step: dict[str, Any],
    demo_manifest: dict[str, Any],
) -> list[dict[str, str]]:
    return [
        {
            "evidence_surface": "Gazebo/RViz observer evidence",
            "current_status": "Gazebo/RViz screenshots, EOAT visibility, TCP marker, model pose, and observer-view evidence only",
            "claim_tier": "visual_only",
        },
        {
            "evidence_surface": "Post-gate actual-mesh observer visual foundation",
            "current_status": (
                "single Step5b close-detail observer row with actual EOAT/contact surface meshes and low-dominance auxiliary markers"
                if post_gate_visual["post_gate_visual_foundation_ready"]
                else "missing or failed post-gate actual-mesh observer row"
            ),
            "claim_tier": "visual_only",
        },
        {
            "evidence_surface": "Per-stage canonical simulated FT logs",
            "current_status": "simulated wrench/FT topics, Gazebo FT plugin output, and synthetic force logs with stamp, frame_id, source, status, baseline, and log evidence",
            "claim_tier": "simulated_ft" if step["contact_stages_simulated_ft"] else "visual_only",
        },
        {
            "evidence_surface": "0708 standalone P2 Gazebo contact witness",
            "current_status": (
                "EOAT collision evidence, contact pair/log evidence, and adapter-verified total contact wrench/contact correlation exist for the standalone P2 witness"
                if step["standalone_p2_physical_witness"] and step["total_contact_wrench_proven"]
                else "EOAT collision evidence, contact pair/log evidence, and adapter-verified single contact-point wrench/contact correlation exist for the standalone P2 witness"
                if step["standalone_p2_physical_witness"]
                else "EOAT collision and contact-pair/log evidence exist, but native wrench/contact correlation is not proven for the standalone P2 witness"
            ),
            "claim_tier": "physical Gazebo collision/contact physics" if step["standalone_p2_physical_witness"] else "visual_only",
        },
        {
            "evidence_surface": "Step5b/Step5d/Step6b/Step7/Step8 per-stage Gazebo contact",
            "current_status": "per-stage evidence remains simulated_ft with stamp, frame_id, source, status, baseline, and log evidence; standalone P2 is not stage-specific",
            "claim_tier": "simulated_ft" if step["contact_stages_simulated_ft"] else "visual_only",
        },
        {
            "evidence_surface": "P6 integrated demo manifest",
            "current_status": demo_manifest["status"],
            "claim_tier": demo_manifest["claim_tier"] if demo_manifest["valid"] else "visual_only",
        },
        {
            "evidence_surface": "P6 TCP distance evidence",
            "current_status": demo_manifest.get("tcp_distance_evidence", {}).get("status", "missing"),
            "claim_tier": demo_manifest.get("tcp_distance_evidence", {}).get("claim_tier", "visual_only"),
        },
        {
            "evidence_surface": "Real bench/live contact",
            "current_status": "not authorized; no live robot action, bridge, TP Play, URScript, or device write occurred",
            "claim_tier": "visual_only",
        },
    ]


def claim_boundary_validation_issues(
    *,
    step: dict[str, Any],
    current_claim_tier_table: list[dict[str, str]],
) -> list[str]:
    issues: list[str] = []
    for index, row in enumerate(current_claim_tier_table):
        tier = row.get("claim_tier")
        if tier not in CLAIM_TIERS:
            issues.append(f"current_claim_tier_table[{index}].claim_tier:unsupported")
        if tier == "real bench/live contact":
            issues.append(f"current_claim_tier_table[{index}].claim_tier:real_bench_live_contact_not_authorized")
    p2_boundary = step.get("p2_physical_gazebo_contact_claim_boundary", {})
    if (
        step.get("p2_reported_claim_tier") == "physical Gazebo collision/contact physics"
        and not p2_boundary.get("proven")
    ):
        issues.append("p2_physical_gazebo_contact:reported_without_required_boundary_evidence")
    return issues


def build_audit(
    *,
    generated_at: str | None = None,
    p3_audit_path: Path = DEFAULT_P3_AUDIT,
    post_gate_visual_foundation_row_path: Path = DEFAULT_POST_GATE_VISUAL_FOUNDATION_ROW,
    step_status_audit_path: Path = DEFAULT_STEP_STATUS_AUDIT,
    integrated_demo_manifest_path: Path | None = None,
    handoff_root: Path = HANDOFF_ROOT,
    timed_audit_coverage_path: Path | None = None,
    same_run_binding_path: Path | None = None,
    dual_sensor_total_wrench_path: Path | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    p3_payload = load_json(p3_audit_path)
    step_payload = load_json(step_status_audit_path)
    p3 = p3_visual_rviz_summary(p3_payload, path=p3_audit_path)
    post_gate_visual = post_gate_visual_foundation_summary(post_gate_visual_foundation_row_path)
    step = step_status_summary(step_payload, path=step_status_audit_path)
    demo_manifest = validate_demo_manifest(integrated_demo_manifest_path)
    if timed_audit_coverage_path is not None:
        timed_audit_payload = load_json(timed_audit_coverage_path)
        timed_audit_issues = external_gate_validation_issues(
            timed_audit_payload,
            expected_schema="ur10e_timed_audit_coverage_audit_v1",
        )
        timed_coverage_candidate = (
            timed_audit_payload.get("timed_audit_coverage")
            if isinstance(timed_audit_payload.get("timed_audit_coverage"), dict)
            else {}
        )
        if not timed_coverage_candidate.get("expected_subagent_triplet_hours"):
            timed_audit_issues.append("timed_audit_coverage.expected_subagent_triplet_hours:missing")
        elif timed_coverage_candidate.get("expected_subagent_triplet_hours") != build_expected_subagent_hours(
            TIMED_AUDIT_GOAL_START_AT,
            generated,
        ):
            timed_audit_issues.append("timed_audit_coverage.expected_subagent_triplet_hours:stale_or_mismatch")
        if not timed_coverage_candidate.get("expected_opus_checkpoint_hours"):
            timed_audit_issues.append("timed_audit_coverage.expected_opus_checkpoint_hours:missing")
        elif timed_coverage_candidate.get("expected_opus_checkpoint_hours") != build_expected_opus_checkpoint_hours(
            TIMED_AUDIT_GOAL_START_AT,
            generated,
        ):
            timed_audit_issues.append("timed_audit_coverage.expected_opus_checkpoint_hours:stale_or_mismatch")
        if timed_audit_issues:
            timed_audit_coverage = invalid_timed_audit_coverage(timed_audit_payload, timed_audit_issues)
        else:
            timed_audit_coverage = timed_audit_payload["timed_audit_coverage"]
    else:
        timed_audit_coverage = timed_audit_coverage_summary(
            handoff_root,
            generated_at=generated,
            goal_start_at=TIMED_AUDIT_GOAL_START_AT,
        )
    if same_run_binding_path is not None:
        same_run_binding_payload = load_json(same_run_binding_path)
        same_run_issues = external_gate_validation_issues(
            same_run_binding_payload,
            expected_schema="ur10e_same_run_integrated_binding_audit_v1",
        )
        same_run_issues.extend(
            external_gate_internal_issues(
                same_run_binding_payload,
                required_surfaces={
                    "p6_manifest",
                    "p3_visual_rviz_audit",
                    "stage_simulated_ft_manifest",
                    "step_status_rnn_audit",
                    "p2_contact_correlation_audit",
                    "tcp_distance_evidence",
                },
            )
        )
        same_run_issues.extend(
            external_same_run_concurrent_observation_issues(
                same_run_binding_payload,
                required_surfaces={
                    "p6_manifest",
                    "p3_visual_rviz_audit",
                    "stage_simulated_ft_manifest",
                    "step_status_rnn_audit",
                    "p2_contact_correlation_audit",
                    "tcp_distance_evidence",
                },
            )
        )
        if same_run_issues:
            same_run_binding_payload = invalid_same_run_binding(same_run_binding_payload, same_run_issues)
    else:
        same_run_binding_payload = build_same_run_integrated_binding_audit(
            generated_at=generated,
            integrated_demo_manifest_path=integrated_demo_manifest_path,
        )
    same_run_binding = {
        "same_run_integrated_demo_proven": bool(
            same_run_binding_payload.get("same_run_integrated_demo_proven")
        ),
        "visual_rviz_simulated_ft_same_run": bool(
            same_run_binding_payload
            .get("manifest_same_run_binding", {})
            .get("visual_rviz_simulated_ft_same_run")
        ),
        "visual_rviz_physical_gazebo_contact_same_run": bool(
            same_run_binding_payload
            .get("manifest_same_run_binding", {})
            .get("visual_rviz_physical_gazebo_contact_same_run")
        ),
        "step_rnn_physical_gazebo_contact_same_run": bool(
            same_run_binding_payload
            .get("manifest_same_run_binding", {})
            .get("step_rnn_physical_gazebo_contact_same_run")
        ),
        "binding_status": same_run_binding_payload.get("binding_status", "cross_run_evidence_only"),
        "artifact": rel(same_run_binding_path),
        "target_run_id": same_run_binding_payload.get("target_run_id"),
        "missing_surfaces": same_run_binding_payload.get("missing_surfaces", []),
        "cross_run_surfaces": same_run_binding_payload.get("cross_run_surfaces", []),
        "validation_issues": same_run_binding_payload.get("validation_issues", []),
        "artifact_rows": same_run_binding_payload.get("artifact_rows", []),
        "concurrent_observation": same_run_binding_payload.get("concurrent_observation", {}),
        "blocker": same_run_binding_payload.get("blocker"),
        "cross_run_evidence": [
            "P3 visual/RViz audit is a retained visual_only evidence run.",
            "P1 per-stage simulated FT pack is a retained simulated_ft evidence pack.",
            "0708 P2 physical Gazebo contact is a standalone witness.",
            "Step/RNN status is a report-level matrix that binds evidence scopes but is not an integrated run.",
        ],
    }
    if dual_sensor_total_wrench_path is not None:
        dual_sensor_payload = load_json(dual_sensor_total_wrench_path)
        dual_sensor_issues = external_gate_validation_issues(
            dual_sensor_payload,
            expected_schema="ur10e_dual_sensor_total_wrench_audit_v1",
        )
        dual_sensor_issues.extend(
            external_gate_internal_issues(
                dual_sensor_payload,
                required_surfaces={
                    "stage_simulated_ft_manifest",
                    "p2_contact_correlation_audit",
                    "step_status_rnn_audit",
                },
            )
        )
        dual_sensor_issues.extend(external_dual_sensor_concurrent_observation_issues(dual_sensor_payload))
        if dual_sensor_issues:
            dual_sensor_payload = invalid_dual_sensor_total_wrench(dual_sensor_payload, dual_sensor_issues)
    else:
        dual_sensor_kwargs: dict[str, Any] = {
            "generated_at": generated,
            "step_status_audit_path": step_status_audit_path,
        }
        stage_simulated_ft_manifest_path = workspace_path(
            step_payload.get("source_artifacts", {}).get("stage_simulated_ft_manifest")
            if isinstance(step_payload.get("source_artifacts"), dict)
            else None
        )
        p2_contact_correlation_audit_path = workspace_path(
            step_payload.get("source_artifacts", {}).get("p2_contact_correlation_audit")
            if isinstance(step_payload.get("source_artifacts"), dict)
            else None
        )
        if stage_simulated_ft_manifest_path is not None:
            dual_sensor_kwargs["stage_simulated_ft_manifest_path"] = stage_simulated_ft_manifest_path
        if p2_contact_correlation_audit_path is not None:
            dual_sensor_kwargs["p2_contact_correlation_audit_path"] = p2_contact_correlation_audit_path
        dual_sensor_payload = build_dual_sensor_total_wrench_audit(
            **dual_sensor_kwargs,
        )
    dual_sensor_total_wrench = {
        "artifact": rel(dual_sensor_total_wrench_path),
        "total_contact_wrench_proven": bool(dual_sensor_payload.get("total_contact_wrench_proven")),
        "same_run_dual_sensor_observation_proven": bool(
            dual_sensor_payload.get("same_run_dual_sensor_observation_proven")
        ),
        "claim_tier": dual_sensor_payload.get("claim_tier", "visual_only"),
        "missing_surfaces": dual_sensor_payload.get("missing_surfaces", []),
        "cross_run_surfaces": dual_sensor_payload.get("cross_run_surfaces", []),
        "validation_issues": dual_sensor_payload.get("validation_issues", []),
        "blockers": dual_sensor_payload.get("blockers", []),
        "stage_simulated_ft": dual_sensor_payload.get("stage_simulated_ft", {}),
        "p2_physical_gazebo_contact": dual_sensor_payload.get("p2_physical_gazebo_contact", {}),
        "same_run_dual_sensor_observation": dual_sensor_payload.get("same_run_dual_sensor_observation", {}),
        "forbidden_claim": dual_sensor_payload.get("forbidden_claim"),
    }
    p6_blockers = build_blockers(
        p3=p3,
        post_gate_visual=post_gate_visual,
        step=step,
        demo_manifest=demo_manifest,
    )
    current_tier_table = current_claim_tier_table(
        p3=p3,
        post_gate_visual=post_gate_visual,
        step=step,
        demo_manifest=demo_manifest,
    )
    claim_boundary_issues = claim_boundary_validation_issues(
        step=step,
        current_claim_tier_table=current_tier_table,
    )
    final_blockers = full_goal_blockers(
        step=step,
        p6_blockers=p6_blockers,
        timed_audit_coverage=timed_audit_coverage,
        same_run_binding=same_run_binding,
        dual_sensor_total_wrench=dual_sensor_total_wrench,
        claim_boundary_validation_issues=claim_boundary_issues,
    )
    source_artifacts = {
        "p3_visual_rviz_audit": rel(p3_audit_path),
        "post_gate_visual_foundation_row": rel(post_gate_visual_foundation_row_path),
        "step_status_rnn_audit": rel(step_status_audit_path),
        "integrated_demo_manifest": demo_manifest["manifest_path"],
        "tcp_distance_evidence": demo_manifest.get("tcp_distance_evidence", {}).get("path"),
        "timed_audit_coverage": rel(timed_audit_coverage_path),
        "same_run_integrated_binding": rel(same_run_binding_path),
        "dual_sensor_total_wrench": rel(dual_sensor_total_wrench_path),
        "p1_simulated_ft_manifest": (
            step_payload.get("source_artifacts", {}).get("stage_simulated_ft_manifest")
            if isinstance(step_payload.get("source_artifacts"), dict)
            else None
        ),
        "p2_contact_correlation_audit": (
            step_payload.get("source_artifacts", {}).get("p2_contact_correlation_audit")
            if isinstance(step_payload.get("source_artifacts"), dict)
            else None
        ),
    }
    return {
        "schema": "ur10e_p6_integrated_demo_readiness_audit_v1",
        "generated_at": generated,
        "goal_lineage": GOAL_LINEAGE,
        "mode": "offline_no_live_p6_integrated_demo_readiness_audit",
        "claim_boundary_gate": claim_boundary_gate(),
        "steering_note": {
            "recorded": True,
            "source": "2026-06-21 additive steering note",
            "acceptance_gate_changed": True,
            "effect": "P6 and final claims must use explicit evidence tiers and downgrade missing or ambiguous evidence.",
        },
        "live_authorization": {
            "robot_motion_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "urscript_authorized": False,
            "zero_ftsensor_authorized": False,
            "payload_tcp_safety_writes_authorized": False,
            "real_bench_live_contact_authorized": False,
        },
        "source_artifacts": source_artifacts,
        "source_artifact_sha256": {
            key: sha256_file(value)
            for key, value in source_artifacts.items()
        },
        "same_run_binding": same_run_binding,
        "dual_sensor_total_wrench": dual_sensor_total_wrench,
        "timed_audit_coverage": timed_audit_coverage,
        "p3_visual_rviz": p3,
        "post_gate_visual_foundation": post_gate_visual,
        "step_status_rnn": step,
        "integrated_demo_manifest": demo_manifest,
        "stage_status_matrix": step["stage_status_matrix"],
        "current_claim_tier_table": current_tier_table,
        "claim_boundary_validation": {
            "fail_closed": True,
            "validation_issues": claim_boundary_issues,
            "claim_boundary_schema_valid": not claim_boundary_issues,
            "full_acceptance_claim_allowed": False,
            "acceptance_gate_changed_by_steering_note": True,
        },
        "readiness_gates": {
            "p3_visual_rviz_ready": p3["p3_visual_rviz_ready"],
            "post_gate_visual_foundation_ready": post_gate_visual["post_gate_visual_foundation_ready"],
            "stage_matrix_present": step["stage_rows"] >= 8,
            "contact_stage_simulated_ft_ready": bool(
                step["stage_simulated_ft_manifest_status"] == "valid"
                and step["contact_stages_simulated_ft"]
                and step["contact_stages_have_log_evidence"]
            ),
            "integrated_demo_manifest_valid": demo_manifest["valid"],
            "p6_integrated_demo_readiness_allowed": not p6_blockers,
            "p6_integrated_demo_blockers": p6_blockers,
        },
        "full_goal_acceptance_gate": {
            "full_goal_acceptance_allowed": False,
            "claim_boundary_schema_valid": not claim_boundary_issues,
            "claim_boundary_validation_issues": claim_boundary_issues,
            "full_goal_acceptance_blockers": final_blockers,
            "forbidden_claim": "full UR10e reproduction acceptance; real bench/live contact; per-stage physical Gazebo contact unless proven by stage-specific evidence",
        },
    }


def write_audit(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    p3_audit_path: Path = DEFAULT_P3_AUDIT,
    post_gate_visual_foundation_row_path: Path = DEFAULT_POST_GATE_VISUAL_FOUNDATION_ROW,
    step_status_audit_path: Path = DEFAULT_STEP_STATUS_AUDIT,
    integrated_demo_manifest_path: Path | None = None,
    handoff_root: Path = HANDOFF_ROOT,
    timed_audit_coverage_path: Path | None = None,
    same_run_binding_path: Path | None = None,
    dual_sensor_total_wrench_path: Path | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "p6_integrated_demo_readiness_audit.json"
    payload = build_audit(
        generated_at=generated_at,
        p3_audit_path=p3_audit_path,
        post_gate_visual_foundation_row_path=post_gate_visual_foundation_row_path,
        step_status_audit_path=step_status_audit_path,
        integrated_demo_manifest_path=integrated_demo_manifest_path,
        handoff_root=handoff_root,
        timed_audit_coverage_path=timed_audit_coverage_path,
        same_run_binding_path=same_run_binding_path,
        dual_sensor_total_wrench_path=dual_sensor_total_wrench_path,
    )
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--p3-audit-path", type=Path, default=DEFAULT_P3_AUDIT)
    parser.add_argument("--post-gate-visual-foundation-row", type=Path, default=DEFAULT_POST_GATE_VISUAL_FOUNDATION_ROW)
    parser.add_argument("--step-status-audit-path", type=Path, default=DEFAULT_STEP_STATUS_AUDIT)
    parser.add_argument("--integrated-demo-manifest", type=Path, default=None)
    parser.add_argument("--handoff-root", type=Path, default=HANDOFF_ROOT)
    parser.add_argument("--timed-audit-coverage", type=Path, default=None)
    parser.add_argument("--same-run-binding", type=Path, default=None)
    parser.add_argument("--dual-sensor-total-wrench", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = write_audit(
        args.output_dir,
        generated_at=args.generated_at,
        p3_audit_path=args.p3_audit_path,
        post_gate_visual_foundation_row_path=args.post_gate_visual_foundation_row,
        step_status_audit_path=args.step_status_audit_path,
        integrated_demo_manifest_path=args.integrated_demo_manifest,
        handoff_root=args.handoff_root,
        timed_audit_coverage_path=args.timed_audit_coverage,
        same_run_binding_path=args.same_run_binding,
        dual_sensor_total_wrench_path=args.dual_sensor_total_wrench,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
