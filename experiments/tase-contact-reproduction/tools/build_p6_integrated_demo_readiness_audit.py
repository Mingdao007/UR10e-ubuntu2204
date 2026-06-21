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
import re
from typing import Any


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
DEFAULT_STEP_STATUS_AUDIT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0846_step_status_strict_rnn_gate"
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
SUBAGENT_RECORD_RE = re.compile(
    r"ur10e-gazebo-hour(?P<hour>\d+)-(?P<lens>visual-observer|geometry-frame|report-claim)-subagent-(?P<kind>prompt|result)-"
)


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


def _sorted_files(root: Path, pattern: str) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(root.glob(pattern), key=lambda path: path.name)


def _rel_handoff(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def timed_audit_coverage_summary(handoff_root: Path = HANDOFF_ROOT) -> dict[str, Any]:
    """Summarize timed Opus/subagent records without upgrading acceptance."""
    expected_lenses = {"visual-observer", "geometry-frame", "report-claim"}
    hours: dict[str, dict[str, Any]] = {}
    for path in _sorted_files(handoff_root, "ur10e-gazebo-hour*-subagent-*.md"):
        match = SUBAGENT_RECORD_RE.search(path.name)
        if not match:
            continue
        hour = match.group("hour")
        lens = match.group("lens")
        kind = match.group("kind")
        item = hours.setdefault(
            hour,
            {
                "hour": int(hour),
                "prompt_lenses": [],
                "result_lenses": [],
                "prompt_paths": {},
                "result_paths": {},
            },
        )
        if kind == "prompt":
            item["prompt_paths"][lens] = _rel_handoff(path)
            if lens not in item["prompt_lenses"]:
                item["prompt_lenses"].append(lens)
        else:
            item["result_paths"][lens] = _rel_handoff(path)
            if lens not in item["result_lenses"]:
                item["result_lenses"].append(lens)

    triplets: list[dict[str, Any]] = []
    for hour in sorted(hours, key=lambda value: int(value)):
        item = hours[hour]
        prompt_lenses = set(item["prompt_lenses"])
        result_lenses = set(item["result_lenses"])
        item["prompt_lenses"] = sorted(prompt_lenses)
        item["result_lenses"] = sorted(result_lenses)
        item["missing_prompt_lenses"] = sorted(expected_lenses - prompt_lenses)
        item["missing_result_lenses"] = sorted(expected_lenses - result_lenses)
        item["triplet_prompt_complete"] = not item["missing_prompt_lenses"]
        item["triplet_result_complete"] = not item["missing_result_lenses"]
        item["status"] = "complete" if item["triplet_result_complete"] else "incomplete"
        triplets.append(item)

    opus_exitcodes = _sorted_files(handoff_root, "ur10e-gazebo-hour*-opus-*response-*.exitcode.txt")
    latest_opus_exitcode = opus_exitcodes[-1] if opus_exitcodes else None
    latest_opus_record: dict[str, Any] = {
        "status": "missing",
        "exit_code": None,
        "exitcode_path": None,
        "stdout_path": None,
        "stderr_path": None,
    }
    if latest_opus_exitcode is not None:
        try:
            exit_code_text = latest_opus_exitcode.read_text(encoding="utf-8").strip()
        except OSError:
            exit_code_text = "unreadable"
        stem = latest_opus_exitcode.name.removesuffix(".exitcode.txt")
        stdout_path = latest_opus_exitcode.with_name(stem + ".stdout.txt")
        stderr_path = latest_opus_exitcode.with_name(stem + ".stderr.txt")
        latest_opus_record = {
            "status": "complete" if exit_code_text == "0" and stdout_path.is_file() else "failed_or_incomplete",
            "exit_code": exit_code_text,
            "exitcode_path": _rel_handoff(latest_opus_exitcode),
            "stdout_path": _rel_handoff(stdout_path) if stdout_path.is_file() else None,
            "stderr_path": _rel_handoff(stderr_path) if stderr_path.is_file() else None,
        }

    prompt_only_hours = [item["hour"] for item in triplets if item["triplet_prompt_complete"] and not item["triplet_result_complete"]]
    incomplete_hours = [item["hour"] for item in triplets if not item["triplet_result_complete"]]
    observed_hours = [item["hour"] for item in triplets]
    missing_sequence_hours: list[int] = []
    if observed_hours:
        observed_set = set(observed_hours)
        missing_sequence_hours = [
            hour
            for hour in range(min(observed_hours), max(observed_hours) + 1)
            if hour not in observed_set
        ]
    full_ready = bool(
        triplets
        and not incomplete_hours
        and not missing_sequence_hours
        and latest_opus_record["status"] == "complete"
    )
    unresolved: list[str] = []
    if latest_opus_record["status"] != "complete":
        unresolved.append("latest Opus advisory checkpoint missing or nonzero")
    if incomplete_hours:
        unresolved.append("hourly subagent triplet results incomplete")
    if missing_sequence_hours:
        unresolved.append("hourly subagent triplet sequence has gaps")
    unresolved.extend(
        [
            "P6 integrated demo run/report missing",
            "strict RNN final acceptance not proven",
            "standalone P2 witness is not per-stage contact physics",
        ]
    )
    return {
        "full_acceptance_timed_audit_ready": full_ready,
        "claim_tier": "visual_only",
        "handoff_root": _rel_handoff(handoff_root),
        "latest_opus_record": latest_opus_record,
        "hourly_subagent_triplets": triplets,
        "complete_subagent_triplet_count": sum(1 for item in triplets if item["triplet_result_complete"]),
        "incomplete_subagent_triplet_hours": incomplete_hours,
        "missing_subagent_triplet_sequence_hours": missing_sequence_hours,
        "prompt_only_subagent_triplet_hours": prompt_only_hours,
        "unresolved_p0_p1_findings": unresolved,
        "blocker": "Timed Opus and hourly subagent coverage must be verified before any full acceptance claim.",
    }


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
        "p2_claim_tier": p2.get("claim_tier") or coverage.get("p2_claim_tier"),
        "p2_scope": p2.get("scope") or coverage.get("p2_scope"),
        "standalone_p2_physical_witness": bool(
            p2.get("claim_tier") == "physical Gazebo collision/contact physics"
            and p2.get("force_contact_physics_proven")
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
) -> list[str]:
    blockers = [f"p6:{blocker}" for blocker in p6_blockers]
    if step["standalone_p2_physical_witness"] and not step["stage_specific_contact_physics_proven"]:
        blockers.append("per_stage_physical_gazebo_contact:not_proven")
    if not step["total_contact_wrench_proven"]:
        blockers.append("total_contact_wrench:not_proven")
    if not step["same_run_concurrent_dual_sensor_observation"]:
        blockers.append("same_run_dual_sensor_observation:not_proven")
    if not step["strict_rnn_final_acceptance"]:
        blockers.append("strict_rnn_final_acceptance:not_proven")
    blockers.append("same_run_integrated_binding:not_proven")
    if not timed_audit_coverage["full_acceptance_timed_audit_ready"]:
        blockers.append("timed_audit_coverage:not_verified")
    blockers.append("real_bench_live_contact:not_authorized")
    return blockers


def current_claim_tier_table(
    *,
    p3: dict[str, Any],
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
            "evidence_surface": "Per-stage canonical simulated FT logs",
            "current_status": "simulated wrench/FT topics, Gazebo FT plugin output, and synthetic force logs with stamp, frame_id, source, status, baseline, and log evidence",
            "claim_tier": "simulated_ft" if step["contact_stages_simulated_ft"] else "visual_only",
        },
        {
            "evidence_surface": "0708 standalone P2 Gazebo contact witness",
            "current_status": "EOAT collision evidence, contact pair/log evidence, and wrench/contact correlation all exist for the standalone P2 witness",
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


def build_audit(
    *,
    generated_at: str | None = None,
    p3_audit_path: Path = DEFAULT_P3_AUDIT,
    step_status_audit_path: Path = DEFAULT_STEP_STATUS_AUDIT,
    integrated_demo_manifest_path: Path | None = None,
    handoff_root: Path = HANDOFF_ROOT,
) -> dict[str, Any]:
    generated = generated_at or datetime.now().isoformat(timespec="seconds")
    p3_payload = load_json(p3_audit_path)
    step_payload = load_json(step_status_audit_path)
    p3 = p3_visual_rviz_summary(p3_payload, path=p3_audit_path)
    step = step_status_summary(step_payload, path=step_status_audit_path)
    demo_manifest = validate_demo_manifest(integrated_demo_manifest_path)
    timed_audit_coverage = timed_audit_coverage_summary(handoff_root)
    p6_blockers = build_blockers(p3=p3, step=step, demo_manifest=demo_manifest)
    final_blockers = full_goal_blockers(
        step=step,
        p6_blockers=p6_blockers,
        timed_audit_coverage=timed_audit_coverage,
    )
    source_artifacts = {
        "p3_visual_rviz_audit": rel(p3_audit_path),
        "step_status_rnn_audit": rel(step_status_audit_path),
        "integrated_demo_manifest": demo_manifest["manifest_path"],
        "tcp_distance_evidence": demo_manifest.get("tcp_distance_evidence", {}).get("path"),
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
        "same_run_binding": {
            "same_run_integrated_demo_proven": False,
            "visual_rviz_simulated_ft_same_run": False,
            "visual_rviz_physical_gazebo_contact_same_run": False,
            "step_rnn_physical_gazebo_contact_same_run": False,
            "binding_status": "cross_run_evidence_only",
            "cross_run_evidence": [
                "P3 visual/RViz audit is a retained visual_only evidence run.",
                "P1 per-stage simulated FT pack is a retained simulated_ft evidence pack.",
                "0708 P2 physical Gazebo contact is a standalone witness.",
                "Step/RNN v8 is a report-level matrix that binds evidence scopes but is not an integrated run.",
            ],
            "blocker": "No same-run P6 manifest binds visual, RViz, simulated FT, Step/RNN, and Gazebo contact physics evidence.",
        },
        "timed_audit_coverage": timed_audit_coverage,
        "p3_visual_rviz": p3,
        "step_status_rnn": step,
        "integrated_demo_manifest": demo_manifest,
        "stage_status_matrix": step["stage_status_matrix"],
        "current_claim_tier_table": current_claim_tier_table(p3=p3, step=step, demo_manifest=demo_manifest),
        "readiness_gates": {
            "p3_visual_rviz_ready": p3["p3_visual_rviz_ready"],
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
            "full_goal_acceptance_blockers": final_blockers,
            "forbidden_claim": "full UR10e reproduction acceptance; real bench/live contact; per-stage physical Gazebo contact unless proven by stage-specific evidence",
        },
    }


def write_audit(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    p3_audit_path: Path = DEFAULT_P3_AUDIT,
    step_status_audit_path: Path = DEFAULT_STEP_STATUS_AUDIT,
    integrated_demo_manifest_path: Path | None = None,
    handoff_root: Path = HANDOFF_ROOT,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "p6_integrated_demo_readiness_audit.json"
    payload = build_audit(
        generated_at=generated_at,
        p3_audit_path=p3_audit_path,
        step_status_audit_path=step_status_audit_path,
        integrated_demo_manifest_path=integrated_demo_manifest_path,
        handoff_root=handoff_root,
    )
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--p3-audit-path", type=Path, default=DEFAULT_P3_AUDIT)
    parser.add_argument("--step-status-audit-path", type=Path, default=DEFAULT_STEP_STATUS_AUDIT)
    parser.add_argument("--integrated-demo-manifest", type=Path, default=None)
    parser.add_argument("--handoff-root", type=Path, default=HANDOFF_ROOT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = write_audit(
        args.output_dir,
        generated_at=args.generated_at,
        p3_audit_path=args.p3_audit_path,
        step_status_audit_path=args.step_status_audit_path,
        integrated_demo_manifest_path=args.integrated_demo_manifest,
        handoff_root=args.handoff_root,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
