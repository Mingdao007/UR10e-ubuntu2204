#!/usr/bin/env python3
"""Build a fail-closed dual-sensor and total-wrench audit.

This offline gate checks two full-acceptance blockers:

* whether a total Gazebo contact wrench is proven rather than a single native
  contact-point sample;
* whether simulated_ft and physical Gazebo contact evidence were observed in
  one concurrent same-run evidence surface.

It reads retained JSON artifacts only and does not launch Gazebo, RViz, ROS, or
any live bench surface.
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

DEFAULT_STAGE_SIMULATED_FT_MANIFEST = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0818_p1_sim_ft_hard_floor"
    / "per_stage_simulated_ft_pack"
    / "step_simulated_ft_evidence_manifest.json"
)
DEFAULT_P2_CONTACT_CORRELATION_AUDIT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0708_p2_gz_sim8_physical_contact_gate"
    / "p2_contact_correlation_audit.json"
)
DEFAULT_STEP_STATUS_AUDIT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0910_step_status_pdf_truth_binding"
    / "step_status_rnn_audit.json"
)


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: Path | str | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(WORKSPACE.resolve()))
    except (OSError, ValueError):
        return str(path)


def workspace_path(path: Path | str | None) -> Path | None:
    if path is None:
        return None
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate
    return WORKSPACE / candidate


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


def all_stage_simulated_ft_valid(stage_manifest: dict[str, Any]) -> bool:
    stages = stage_manifest.get("stages")
    if not isinstance(stages, dict) or not stages:
        return False
    return bool(
        stage_manifest.get("all_contact_stages_valid")
        and int(stage_manifest.get("valid_stage_count") or 0) == int(stage_manifest.get("stage_count") or 0)
        and all(stage.get("claim_tier") == "simulated_ft" and stage.get("valid") is True for stage in stages.values())
    )


def simulated_ft_field_coverage(stage_manifest: dict[str, Any]) -> dict[str, bool]:
    stages = stage_manifest.get("stages")
    if not isinstance(stages, dict) or not stages:
        return {field: False for field in ("stamp", "frame_id", "source", "status", "baseline", "log_evidence")}
    required = ("stamp", "frame_id", "source", "status", "baseline", "log_evidence")
    return {
        field: all(
            bool(stage.get("evidence_fields_present", {}).get(field))
            for stage in stages.values()
            if isinstance(stage, dict)
        )
        for field in required
    }


def p2_step_summary(step_status: dict[str, Any]) -> dict[str, Any]:
    p2 = step_status.get("p2_physical_gazebo_contact")
    return p2 if isinstance(p2, dict) else {}


def p2_physical_contact_proven(p2_audit: dict[str, Any], step_p2: dict[str, Any]) -> bool:
    gate = p2_audit.get("physical_gazebo_contact_gate")
    gate_proven = isinstance(gate, dict) and bool(gate.get("force_contact_physics_proven"))
    step_proven = bool(step_p2.get("force_contact_physics_proven"))
    return bool(gate_proven or step_proven)


def total_contact_wrench_proven(p2_audit: dict[str, Any], step_p2: dict[str, Any]) -> bool:
    gate = p2_audit.get("claim_boundary_gate")
    wrench = p2_audit.get("wrench_evidence") if isinstance(p2_audit.get("wrench_evidence"), dict) else {}
    policy = wrench.get("wrench_aggregation_policy") or step_p2.get("wrench_aggregation_policy")
    rows = wrench.get("rows") if isinstance(wrench.get("rows"), list) else []
    p2_total = (
        isinstance(gate, dict)
        and bool(gate.get("total_contact_wrench_proven"))
        and bool(wrench.get("total_contact_wrench_proven"))
        and policy == "total_contact_wrench"
        and bool(rows)
    )
    return bool(p2_total)


def dual_sensor_observation_summary(p2_audit: dict[str, Any], step_p2: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    raw_candidates = [
        p2_audit.get("same_run_concurrent_dual_sensor_observation"),
        step_p2.get("same_run_concurrent_dual_sensor_observation"),
    ]
    observation = next((candidate for candidate in raw_candidates if isinstance(candidate, dict)), {})
    legacy_true_flag = any(candidate is True for candidate in raw_candidates)
    time_window = observation.get("time_window") if isinstance(observation.get("time_window"), dict) else {}
    required_surface_fields = (
        "stage_simulated_ft_manifest",
        "p2_contact_correlation_audit",
        "step_status_rnn_audit",
    )
    surfaces = observation.get("surfaces") if isinstance(observation.get("surfaces"), dict) else {}
    missing_surface_bindings = sorted(
        field for field in required_surface_fields if not surfaces.get(field)
    )
    summary = {
        "observation_id": str(observation.get("observation_id") or "").strip() or None,
        "same_run_concurrent_dual_sensor_observation_explicit": observation.get("explicit") is True,
        "time_window": {
            "start": time_window.get("start"),
            "end": time_window.get("end"),
            "clock_source": time_window.get("clock_source"),
        },
        "surfaces": {
            field: surfaces.get(field)
            for field in required_surface_fields
            if surfaces.get(field)
        },
        "missing_surface_bindings": missing_surface_bindings,
        "legacy_true_flag_without_detail": legacy_true_flag and not observation,
    }
    issues: list[str] = []
    if not isinstance(observation, dict) or not observation:
        issues.append("same_run_concurrent_dual_sensor_observation:detail_missing")
    if summary["legacy_true_flag_without_detail"]:
        issues.append("same_run_concurrent_dual_sensor_observation:legacy_true_flag_only")
    if not summary["observation_id"]:
        issues.append("same_run_concurrent_dual_sensor_observation.observation_id:missing")
    if not summary["same_run_concurrent_dual_sensor_observation_explicit"]:
        issues.append("same_run_concurrent_dual_sensor_observation.explicit:not_true")
    if not summary["time_window"]["start"]:
        issues.append("same_run_concurrent_dual_sensor_observation.time_window.start:missing")
    if not summary["time_window"]["end"]:
        issues.append("same_run_concurrent_dual_sensor_observation.time_window.end:missing")
    if not summary["time_window"]["clock_source"]:
        issues.append("same_run_concurrent_dual_sensor_observation.time_window.clock_source:missing")
    if missing_surface_bindings:
        issues.append(
            "same_run_concurrent_dual_sensor_observation.surfaces:missing:"
            + ",".join(missing_surface_bindings)
        )
    summary["same_run_concurrent_dual_sensor_observation_proven"] = not issues
    return summary, issues


def build_audit(
    *,
    generated_at: str | None = None,
    stage_simulated_ft_manifest_path: Path = DEFAULT_STAGE_SIMULATED_FT_MANIFEST,
    p2_contact_correlation_audit_path: Path = DEFAULT_P2_CONTACT_CORRELATION_AUDIT,
    step_status_audit_path: Path = DEFAULT_STEP_STATUS_AUDIT,
) -> dict[str, Any]:
    generated = generated_at or datetime.now().astimezone().isoformat(timespec="seconds")
    stage_manifest = load_json(stage_simulated_ft_manifest_path)
    p2_audit = load_json(p2_contact_correlation_audit_path)
    step_status = load_json(step_status_audit_path)
    step_p2 = p2_step_summary(step_status)

    rows = [
        artifact_row("stage_simulated_ft_manifest", stage_simulated_ft_manifest_path),
        artifact_row("p2_contact_correlation_audit", p2_contact_correlation_audit_path),
        artifact_row("step_status_rnn_audit", step_status_audit_path),
    ]
    missing_surfaces = [row["surface"] for row in rows if not row["exists"]]
    run_ids = {row["run_id"] for row in rows if row["run_id"]}
    cross_run_surfaces = [row["surface"] for row in rows if row["run_id"] and len(run_ids) > 1]
    stage_valid = all_stage_simulated_ft_valid(stage_manifest)
    p2_proven = p2_physical_contact_proven(p2_audit, step_p2)
    raw_total_wrench = total_contact_wrench_proven(p2_audit, step_p2)
    dual_sensor_observation, dual_sensor_observation_issues = dual_sensor_observation_summary(p2_audit, step_p2)
    explicit_same_run_dual = bool(
        dual_sensor_observation["same_run_concurrent_dual_sensor_observation_proven"]
    )
    total_wrench = bool(raw_total_wrench and not missing_surfaces and not cross_run_surfaces)
    dual_sensor = bool(
        stage_valid
        and p2_proven
        and total_wrench
        and explicit_same_run_dual
        and not missing_surfaces
        and not cross_run_surfaces
    )

    blockers: list[str] = []
    validation_issues: list[str] = []
    if missing_surfaces:
        validation_issues.append("required_surfaces:missing:" + ",".join(sorted(missing_surfaces)))
    if cross_run_surfaces:
        validation_issues.append("required_surfaces:cross_run:" + ",".join(sorted(cross_run_surfaces)))
    if not stage_valid:
        validation_issues.append("stage_simulated_ft_manifest:not_all_valid")
    if not p2_proven:
        validation_issues.append("p2_physical_gazebo_contact:not_proven")
    if not raw_total_wrench:
        validation_issues.append("total_contact_wrench_evidence:not_proven")
    if not total_wrench:
        blockers.append("total_contact_wrench:not_proven")
    if not dual_sensor:
        blockers.append("same_run_dual_sensor_observation:not_proven")
    if not explicit_same_run_dual:
        validation_issues.append("same_run_concurrent_dual_sensor_observation:not_explicitly_proven")
    validation_issues.extend(dual_sensor_observation_issues)

    return {
        "schema": "ur10e_dual_sensor_total_wrench_audit_v1",
        "generated_at": generated,
        "goal_lineage": GOAL_LINEAGE,
        "mode": "offline_report_level_dual_sensor_total_wrench_gate",
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
        "artifact_rows": rows,
        "missing_surfaces": sorted(missing_surfaces),
        "cross_run_surfaces": sorted(cross_run_surfaces),
        "validation_issues": validation_issues,
        "blockers": blockers,
        "stage_simulated_ft": {
            "claim_tier": stage_manifest.get("claim_tier"),
            "all_contact_stages_valid": bool(stage_manifest.get("all_contact_stages_valid")),
            "stage_count": stage_manifest.get("stage_count"),
            "valid_stage_count": stage_manifest.get("valid_stage_count"),
            "required_evidence_fields_present_for_all_stages": simulated_ft_field_coverage(stage_manifest),
        },
        "p2_physical_gazebo_contact": {
            "claim_tier": step_p2.get("claim_tier") or p2_audit.get("claim_tier"),
            "force_contact_physics_proven": p2_proven,
            "scope": step_p2.get("scope"),
            "single_contact_point_wrench_correlation": bool(p2_proven),
            "total_contact_wrench_proven": total_wrench,
            "wrench_aggregation_policy": (
                p2_audit.get("wrench_evidence", {}).get("wrench_aggregation_policy")
                or step_p2.get("wrench_aggregation_policy")
                or "single_native_contact_point_wrench_sample_no_total_contact_wrench_claim"
            ),
        },
        "same_run_dual_sensor_observation": {
            "same_run_dual_sensor_observation_proven": dual_sensor,
            "same_run_concurrent_dual_sensor_observation_explicit": explicit_same_run_dual,
            "same_run_concurrent_dual_sensor_observation": dual_sensor_observation,
            "required_surfaces": [
                "stage_simulated_ft_manifest",
                "p2_contact_correlation_audit",
                "step_status_rnn_audit",
            ],
            "required_condition": "simulated_ft and physical Gazebo contact evidence must be same-run concurrent observations, not cross-run retained witnesses.",
        },
        "total_contact_wrench_proven": total_wrench,
        "same_run_dual_sensor_observation_proven": dual_sensor,
        "forbidden_claim": "total Gazebo contact wrench; same-run dual-sensor observation; real bench/live contact",
    }


def write_audit(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    stage_simulated_ft_manifest_path: Path = DEFAULT_STAGE_SIMULATED_FT_MANIFEST,
    p2_contact_correlation_audit_path: Path = DEFAULT_P2_CONTACT_CORRELATION_AUDIT,
    step_status_audit_path: Path = DEFAULT_STEP_STATUS_AUDIT,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "dual_sensor_total_wrench_audit.json"
    payload = build_audit(
        generated_at=generated_at,
        stage_simulated_ft_manifest_path=stage_simulated_ft_manifest_path,
        p2_contact_correlation_audit_path=p2_contact_correlation_audit_path,
        step_status_audit_path=step_status_audit_path,
    )
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--stage-simulated-ft-manifest", type=Path, default=DEFAULT_STAGE_SIMULATED_FT_MANIFEST)
    parser.add_argument("--p2-contact-correlation-audit", type=Path, default=DEFAULT_P2_CONTACT_CORRELATION_AUDIT)
    parser.add_argument("--step-status-audit", type=Path, default=DEFAULT_STEP_STATUS_AUDIT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        write_audit(
            args.output_dir,
            generated_at=args.generated_at,
            stage_simulated_ft_manifest_path=args.stage_simulated_ft_manifest,
            p2_contact_correlation_audit_path=args.p2_contact_correlation_audit,
            step_status_audit_path=args.step_status_audit,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
