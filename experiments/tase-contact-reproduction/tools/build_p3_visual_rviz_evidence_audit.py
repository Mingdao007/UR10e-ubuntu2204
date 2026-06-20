#!/usr/bin/env python3
"""Build a P3 Gazebo observer and RViz evidence boundary audit.

This tool is offline only. It summarizes existing Gazebo observer evidence and
keeps it at visual_only, while explicitly marking RViz debug evidence as
missing until current RViz artifacts exist.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
RUNS = EXPERIMENT_ROOT / "runs"
GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"

DEFAULT_GAZEBO_OBSERVER_RUN = RUNS / "ur10e_gazebo_close_camera_full_matrix_20260620_223446"
DEFAULT_OBSERVER_SUMMARY = DEFAULT_GAZEBO_OBSERVER_RUN / "full_matrix_summary_stdout.after-observer-review.json"
DEFAULT_OBSERVER_MANIFEST = DEFAULT_GAZEBO_OBSERVER_RUN / "observer_review_manifest.json"
DEFAULT_P2_CORRELATION_AUDIT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0326_p2_gazebo_contact_wrench_adapter_strict_v2_correlation_audit"
    / "p2_contact_correlation_audit.json"
)
DEFAULT_P1_SIMULATED_FT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0146"
    / "p1_auditor_installed_runtime_observed_ros2_simulated_ft.json"
)
RVIZ_MANIFEST_SCHEMA = "ur10e_p3_rviz_debug_evidence_pack_v1"

CLAIM_TIERS = [
    "visual_only",
    "virtual/software force-loop",
    "simulated_ft",
    "physical Gazebo collision/contact physics",
    "real bench/live contact",
]

RVIZ_REQUIRED_ITEMS = [
    "TF tree",
    "robot model",
    "EOAT/tool frames",
    "TCP/contact_tip/contact_surface frames",
    "wrench/contact vectors or markers",
    "trajectory/path markers",
    "frame and claim-tier labels",
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(WORKSPACE.resolve()))
    except ValueError:
        return str(path)


def claim_boundary_gate() -> dict[str, Any]:
    return {
        "fail_closed": True,
        "tiers": CLAIM_TIERS,
        "rules": [
            "Gazebo/RViz screenshots, EOAT visibility, TCP marker, model pose, and observer-view evidence support only visual_only unless backed by stronger artifacts.",
            "gazebo_joint_state_fk_virtual_surface_model supports only virtual/software force-loop.",
            "simulated_ft requires stamp, frame_id, source, status, baseline, and log evidence.",
            "physical Gazebo collision/contact physics requires EOAT collision evidence, contact pair/log evidence, and wrench/contact correlation.",
            "If eoat_collision_count=0 or force_contact_physics_proven=false, physical Gazebo collision/contact physics is blocked/not proven and must downgrade to visual_only.",
            "real bench/live contact is not authorized in this goal; no simulated_ft or Gazebo evidence may upgrade into real bench/live contact.",
        ],
    }


def summarize_gazebo_observer(
    *,
    summary_path: Path = DEFAULT_OBSERVER_SUMMARY,
    manifest_path: Path = DEFAULT_OBSERVER_MANIFEST,
) -> dict[str, Any]:
    summary = load_json(summary_path)
    manifest = load_json(manifest_path)
    rows = list(summary.get("rows", []))
    stages = sorted({str(row.get("stage")) for row in rows if row.get("stage")})
    views = sorted({str(row.get("view")) for row in rows if row.get("view")})
    force_sources = sorted({str(row.get("force_contact_source")) for row in rows if row.get("force_contact_source")})
    force_physics_values = {bool(row.get("force_contact_physics_proven")) for row in rows}
    force_contact_physics_proven = bool(rows) and force_physics_values == {True}
    blocker_tokens = [] if force_contact_physics_proven else ["force_contact_physics_proven=false"]

    row_count = int(summary.get("row_count") or len(rows))
    observer_visual_pass_count = int(summary.get("observer_visual_pass_count") or 0)
    observer_visual_fail_count = int(summary.get("observer_visual_fail_count") or 0)
    reviewed_row_count = int(manifest.get("reviewed_row_count") or 0)
    review_status_counts = Counter(str(row.get("observer_visual_review_status")) for row in rows)
    criteria_counts = {
        key: Counter(str(row.get(key)) for row in rows)
        for key in (
            "observer_review_present",
            "observer_visual_pass",
            "eoat_tooling_visible",
            "tcp_marker_visible",
            "surface_path_visible",
            "robot_tool_surface_relation_visible",
            "clean_scene_capture",
            "obstructive_ui_panels_absent",
        )
    }
    return {
        "claim_tier": "visual_only",
        "source_boundary": "visual observer evidence only; not Gazebo contact physics",
        "run_dir": rel(Path(summary.get("run_dir") or DEFAULT_GAZEBO_OBSERVER_RUN)),
        "summary_path": rel(summary_path),
        "observer_manifest_path": rel(manifest_path),
        "contact_sheet_path": manifest.get("contact_sheet"),
        "schema": summary.get("schema"),
        "row_count": row_count,
        "reviewed_row_count": reviewed_row_count,
        "observer_visual_pass_count": observer_visual_pass_count,
        "observer_visual_fail_count": observer_visual_fail_count,
        "stages": stages,
        "views": views,
        "expected_rows_present": bool(summary.get("all_expected_rows_present")),
        "all_rows_observer_visual_pass": bool(summary.get("all_rows_observer_visual_pass")),
        "all_rows_gui_evidence_captured": bool(summary.get("all_rows_gui_evidence_captured")),
        "review_status_counts": dict(review_status_counts),
        "criteria_counts": {key: dict(value) for key, value in criteria_counts.items()},
        "force_contact_source": force_sources[0] if len(force_sources) == 1 else "mixed_or_missing",
        "force_contact_physics_proven": force_contact_physics_proven,
        "blocker_tokens": blocker_tokens,
        "forbidden_claim": "physical Gazebo collision/contact physics; simulated_ft; real bench/live contact",
    }


def find_rviz_artifacts(search_root: Path = WORKSPACE) -> dict[str, Any]:
    configs = sorted(search_root.rglob("*.rviz"))
    screenshots = sorted(
        path
        for pattern in ("*rviz*.png", "*rviz*.jpg", "*rviz*.jpeg")
        for path in search_root.rglob(pattern)
    )
    manifests = sorted(
        path
        for pattern in ("*rviz*.json", "*rviz*.md")
        for path in search_root.rglob(pattern)
        if path.name != "p3_visual_rviz_evidence_audit.json"
    )
    valid_manifests = [(path, payload) for path in manifests if (payload := load_rviz_manifest(path)) is not None]
    if valid_manifests:
        manifest_path, manifest_payload = valid_manifests[-1]
        evidenced_items = {
            item: bool((manifest_payload.get("evidenced_items", {}).get(item) or {}).get("evidenced"))
            for item in RVIZ_REQUIRED_ITEMS
        }
        all_required_items_evidenced = all(evidenced_items.values())
        rendered = bool((manifest_payload.get("rendered_screenshot_evidence") or {}).get("present"))
        rviz_config_path = manifest_path.parent / str(manifest_payload.get("rviz_config_path", ""))
        config_paths = sorted({*configs, rviz_config_path} if rviz_config_path.is_file() else set(configs))
        return {
            "claim_tier": "visual_only",
            "status": (
                "rviz_config_manifest_evidence_present_not_rendered"
                if all_required_items_evidenced and not rendered
                else "blocked_incomplete_rviz_evidence"
            ),
            "all_required_items_evidenced": all_required_items_evidenced,
            "rendered_screenshot_evidence_present": rendered,
            "full_rviz_render_acceptance_allowed": bool(rendered and all_required_items_evidenced),
            "required_items": RVIZ_REQUIRED_ITEMS,
            "evidenced_items": evidenced_items,
            "config_paths": [rel(path) for path in config_paths],
            "screenshot_paths": [rel(path) for path in screenshots],
            "manifest_paths": [rel(path) for path, _payload in valid_manifests],
            "valid_manifest_schema": RVIZ_MANIFEST_SCHEMA,
            "evidence_mode": manifest_payload.get("evidence_mode"),
            "source_manifest": rel(manifest_path),
            "forbidden_claim": "physical Gazebo collision/contact physics; simulated_ft; real bench/live contact",
        }
    has_shallow_artifacts = bool(configs or screenshots or manifests)
    evidenced_items = {item: False for item in RVIZ_REQUIRED_ITEMS}
    return {
        "claim_tier": "visual_only",
        "status": "blocked_incomplete_rviz_evidence" if has_shallow_artifacts else "blocked_missing_current_rviz_evidence",
        "all_required_items_evidenced": False,
        "rendered_screenshot_evidence_present": False,
        "full_rviz_render_acceptance_allowed": False,
        "required_items": RVIZ_REQUIRED_ITEMS,
        "evidenced_items": evidenced_items,
        "config_paths": [rel(path) for path in configs],
        "screenshot_paths": [rel(path) for path in screenshots],
        "manifest_paths": [rel(path) for path in manifests],
        "forbidden_claim": "RViz debug acceptance; physical Gazebo collision/contact physics; real bench/live contact",
    }


def load_rviz_manifest(path: Path) -> dict[str, Any] | None:
    try:
        payload = load_json(path)
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema") != RVIZ_MANIFEST_SCHEMA:
        return None
    return payload


def p2_physical_status(path: Path = DEFAULT_P2_CORRELATION_AUDIT) -> dict[str, Any]:
    payload = load_json(path)
    gate = dict(payload.get("physical_gazebo_contact_gate", {}))
    force_contact_proven = bool(gate.get("force_contact_physics_proven"))
    return {
        "artifact": rel(path),
        "claim_tier": "physical Gazebo collision/contact physics" if force_contact_proven else "visual_only",
        "status": gate.get("status", "blocked_not_proven"),
        "eoat_collision_count": int(gate.get("eoat_collision_count") or 0),
        "contact_pair_log_evidence": bool(gate.get("contact_pair_log_evidence")),
        "adapter_verified_gazebo_contact_wrench": bool(gate.get("adapter_verified_gazebo_contact_wrench")),
        "wrench_contact_correlation": bool(gate.get("wrench_contact_correlation")),
        "force_contact_physics_proven": force_contact_proven,
        "known_blockers": payload.get("known_blockers", []),
        "blocker_tokens": [] if force_contact_proven else ["force_contact_physics_proven=false"],
    }


def p1_simulated_ft_reference(path: Path = DEFAULT_P1_SIMULATED_FT) -> dict[str, Any]:
    payload = load_json(path)
    fields = dict(payload.get("evidence_fields_present", {}))
    return {
        "artifact": rel(path),
        "claim_tier": payload.get("claim_tier", "visual_only"),
        "force_source": payload.get("force_source"),
        "mode": payload.get("mode"),
        "observed_complete": bool(payload.get("observed_complete")),
        "observed_counts": payload.get("observed_counts", {}),
        "evidence_fields_present": {
            "stamp": bool(fields.get("stamp")),
            "frame_id": bool(fields.get("frame_id")),
            "source": bool(fields.get("source")),
            "status": bool(fields.get("status")),
            "baseline": bool(fields.get("baseline")),
            "log_evidence": bool(fields.get("log_evidence")),
        },
        "forbidden_claim": "physical Gazebo collision/contact physics; real bench/live contact",
    }


def p3_requirement_status(gazebo: dict[str, Any], rviz: dict[str, Any]) -> dict[str, Any]:
    return {
        "gazebo_gui_observer": {
            "status": "visual_observer_pass_with_claim_boundary"
            if gazebo["observer_visual_pass_count"] == gazebo["row_count"] and gazebo["row_count"] > 0
            else "blocked_missing_or_failed_observer_rows",
            "claim_tier": "visual_only",
            "covered_view_roles": gazebo["views"],
            "explicit_side_view_status": "present" if "side_view" in gazebo["views"] else "missing_explicit_side_view_label",
            "oblique_or_context_view_status": "present" if "context_overview" in gazebo["views"] else "missing",
            "interaction_view_status": "present" if "interaction_view" in gazebo["views"] else "missing",
            "close_up_contact_view_status": "present" if "close_detail" in gazebo["views"] else "missing",
            "physical_contact_claim_allowed": False,
        },
        "rviz_debug_evidence": rviz,
    }


def current_claim_tier_table(
    gazebo: dict[str, Any],
    rviz: dict[str, Any],
    p2: dict[str, Any],
    p1: dict[str, Any],
) -> list[dict[str, str]]:
    return [
        {
            "evidence_surface": "Gazebo observer matrix",
            "current_status": f"{gazebo['observer_visual_pass_count']}/{gazebo['row_count']} observer rows pass; screenshots/observer review only",
            "claim_tier": "visual_only",
        },
        {
            "evidence_surface": "RViz debug evidence",
            "current_status": (
                "config+manifest evidence present; rendered RViz screenshot not observed"
                if rviz["all_required_items_evidenced"]
                else "missing current RViz evidence artifact"
            ),
            "claim_tier": "visual_only",
        },
        {
            "evidence_surface": "Gazebo virtual force source",
            "current_status": "`gazebo_joint_state_fk_virtual_surface_model` is software-only force evidence",
            "claim_tier": "virtual/software force-loop",
        },
        {
            "evidence_surface": "P2 physical Gazebo contact",
            "current_status": "`force_contact_physics_proven=false`; blocked/not proven",
            "claim_tier": "visual_only",
        },
        {
            "evidence_surface": "P1 simulated FT",
            "current_status": "separate P1 artifact has stamp, frame_id, source, status, baseline, and log evidence",
            "claim_tier": "simulated_ft",
            "evidence_artifact": p1["artifact"],
            "required_evidence_fields": ["stamp", "frame_id", "source", "status", "baseline", "log_evidence"],
        },
        {
            "evidence_surface": "Real bench/live contact",
            "current_status": "not authorized; no live bench evidence may be claimed",
            "claim_tier": "visual_only",
        },
    ]


def build_audit(
    *,
    generated_at: str | None = None,
    observer_summary_path: Path = DEFAULT_OBSERVER_SUMMARY,
    observer_manifest_path: Path = DEFAULT_OBSERVER_MANIFEST,
    p2_correlation_path: Path = DEFAULT_P2_CORRELATION_AUDIT,
    p1_simulated_ft_path: Path = DEFAULT_P1_SIMULATED_FT,
    rviz_search_root: Path = WORKSPACE,
) -> dict[str, Any]:
    gazebo = summarize_gazebo_observer(summary_path=observer_summary_path, manifest_path=observer_manifest_path)
    rviz = find_rviz_artifacts(rviz_search_root)
    p2 = p2_physical_status(p2_correlation_path)
    p1 = p1_simulated_ft_reference(p1_simulated_ft_path)
    return {
        "schema": "ur10e_p3_visual_rviz_evidence_audit_v1",
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "goal_lineage": GOAL_LINEAGE,
        "mode": "offline_no_live_visual_rviz_evidence_audit",
        "claim_boundary_gate": claim_boundary_gate(),
        "live_authorization": {
            "robot_motion_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "urscript_authorized": False,
            "zero_ftsensor_authorized": False,
            "payload_tcp_safety_writes_authorized": False,
            "real_bench_live_contact_authorized": False,
        },
        "source_artifacts": {
            "gazebo_observer_summary": rel(observer_summary_path),
            "gazebo_observer_manifest": rel(observer_manifest_path),
            "p2_contact_correlation_audit": rel(p2_correlation_path),
            "p1_simulated_ft": rel(p1_simulated_ft_path),
            "rviz_search_root": rel(rviz_search_root),
        },
        "gazebo_observer_evidence": gazebo,
        "p1_simulated_ft_reference": p1,
        "p2_physical_gazebo_contact": p2,
        "p3_requirement_status": p3_requirement_status(gazebo, rviz),
        "current_claim_tier_table": current_claim_tier_table(gazebo, rviz, p2, p1),
        "audit_coverage": {
            "gazebo_rows": gazebo["row_count"],
            "gazebo_observer_visual_pass_count": gazebo["observer_visual_pass_count"],
            "rviz_all_required_items_evidenced": rviz["all_required_items_evidenced"],
            "rviz_rendered_screenshot_evidence_present": rviz["rendered_screenshot_evidence_present"],
            "physical_contact_claim_allowed": False,
            "full_p3_acceptance_allowed": False,
            "full_p3_acceptance_blocker": "RViz rendered screenshot evidence is missing and physical Gazebo contact physics remains blocked/not proven.",
        },
    }


def write_audit(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    observer_summary_path: Path = DEFAULT_OBSERVER_SUMMARY,
    observer_manifest_path: Path = DEFAULT_OBSERVER_MANIFEST,
    p2_correlation_path: Path = DEFAULT_P2_CORRELATION_AUDIT,
    p1_simulated_ft_path: Path = DEFAULT_P1_SIMULATED_FT,
    rviz_search_root: Path = WORKSPACE,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "p3_visual_rviz_evidence_audit.json"
    payload = build_audit(
        generated_at=generated_at,
        observer_summary_path=observer_summary_path,
        observer_manifest_path=observer_manifest_path,
        p2_correlation_path=p2_correlation_path,
        p1_simulated_ft_path=p1_simulated_ft_path,
        rviz_search_root=rviz_search_root,
    )
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--observer-summary-path", type=Path, default=DEFAULT_OBSERVER_SUMMARY)
    parser.add_argument("--observer-manifest-path", type=Path, default=DEFAULT_OBSERVER_MANIFEST)
    parser.add_argument("--p2-correlation-path", type=Path, default=DEFAULT_P2_CORRELATION_AUDIT)
    parser.add_argument("--p1-simulated-ft-path", type=Path, default=DEFAULT_P1_SIMULATED_FT)
    parser.add_argument("--rviz-search-root", type=Path, default=WORKSPACE)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = write_audit(
        args.output_dir,
        generated_at=args.generated_at,
        observer_summary_path=args.observer_summary_path,
        observer_manifest_path=args.observer_manifest_path,
        p2_correlation_path=args.p2_correlation_path,
        p1_simulated_ft_path=args.p1_simulated_ft_path,
        rviz_search_root=args.rviz_search_root,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
