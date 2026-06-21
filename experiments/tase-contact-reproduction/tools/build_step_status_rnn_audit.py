#!/usr/bin/env python3
"""Build a fail-closed Step5/6/7/8 and RNN status audit.

This report-level artifact is offline only. It labels evidence with the
current goal's claim tiers and keeps Gazebo contact physics blocked unless P2
has collision evidence, contact pair/log evidence, and wrench/contact
correlation.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
PACKAGE_ROOT = WORKSPACE / "src" / "ur10e_example_controllers"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from ur10e_example_controllers import canonical_wrench_contract as wrench_contract  # noqa: E402
from ur10e_example_controllers import step56_simulation_matrix as step56  # noqa: E402


RUNS = EXPERIMENT_ROOT / "runs"
GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"
P1_SIMULATED_FT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0818_p1_sim_ft_hard_floor"
    / "p1_simulated_ft_hard_floor_audit.json"
)
P2_CONTACT_PAIR_LOG = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0708_p2_gz_sim8_physical_contact_gate"
    / "p2_gazebo_contact_pair_log_verified.json"
)
P2_WRENCH_ADAPTER_REPORT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0708_p2_gz_sim8_physical_contact_gate"
    / "p2_gazebo_contact_wrench_adapter_report.json"
)
P2_CONTACT_CORRELATION_AUDIT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0708_p2_gz_sim8_physical_contact_gate"
    / "p2_contact_correlation_audit.json"
)
STEP5D_PAPER_TRUTH = EXPERIMENT_ROOT / "config" / "step5c_tase_paper_truth.json"
STEP5D_NUMERIC_SANITY = RUNS / "step5d_numeric_sanity_20260614_215555" / "step5d_numeric_sanity.json"
STAGE_SIM_FT_PACK_SCHEMA = "ur10e_step_simulated_ft_evidence_pack_v1"
STAGE_SIM_FT_LOG_SCHEMA = "ur10e_stage_canonical_simulated_ft_log_v1"
EPS = 1e-9

CLAIM_TIERS = [
    "visual_only",
    "virtual/software force-loop",
    "simulated_ft",
    "physical Gazebo collision/contact physics",
    "real bench/live contact",
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
            "If eoat_collision_count=0 or force_contact_physics_proven=false, physical Gazebo collision/contact physics is blocked/not proven and claims must downgrade.",
            "real bench/live contact is not authorized in this goal; no simulated_ft or Gazebo evidence may upgrade into real bench/live contact.",
        ],
    }


def p1_simulated_ft_summary(path: Path = P1_SIMULATED_FT) -> dict[str, Any]:
    payload = load_json(path)
    if payload.get("schema") == "ur10e_p1_simulated_ft_hard_floor_audit_v1":
        runtime_evidence = (
            payload.get("checks", {})
            .get("runtime_dry_run", {})
            .get("evidence", {})
        )
        fields = dict(runtime_evidence.get("evidence_fields_present", {}))
        per_stage_manifest = (
            payload.get("checks", {})
            .get("per_stage_pack", {})
            .get("evidence", {})
            .get("manifest_path")
        )
        return {
            "artifact": rel(path),
            "schema": payload.get("schema"),
            "claim_tier": payload.get("claim_tier", "visual_only"),
            "force_source": runtime_evidence.get("force_source"),
            "mode": payload.get("mode"),
            "hard_floor_ready": bool(payload.get("p1_simulated_ft_hard_floor_ready")),
            "observed_complete": False,
            "observed_counts": {
                "runtime_dry_run_sample_count": runtime_evidence.get("sample_count", 0),
                "passing_check_count": sum(
                    1 for check in payload.get("checks", {}).values() if check.get("pass")
                ),
            },
            "per_stage_simulated_ft_manifest": per_stage_manifest,
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

    fields = dict(payload.get("evidence_fields_present", {}))
    return {
        "artifact": rel(path),
        "schema": payload.get("schema"),
        "claim_tier": payload.get("claim_tier", "visual_only"),
        "force_source": payload.get("force_source"),
        "mode": payload.get("mode"),
        "hard_floor_ready": False,
        "observed_complete": bool(payload.get("observed_complete")),
        "observed_counts": payload.get("observed_counts", {}),
        "per_stage_simulated_ft_manifest": None,
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


def default_stage_sim_ft_manifest_from_p1(path: Path = P1_SIMULATED_FT) -> Path | None:
    payload = load_json(path)
    manifest_ref = (
        payload.get("checks", {})
        .get("per_stage_pack", {})
        .get("evidence", {})
        .get("manifest_path")
    )
    if not isinstance(manifest_ref, str) or not manifest_ref:
        return None
    manifest_path = Path(manifest_ref)
    if not manifest_path.is_absolute():
        manifest_path = WORKSPACE / manifest_path
    return manifest_path


def normalize_input_paths(inputs: Any) -> dict[str, Any]:
    if not isinstance(inputs, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key, value in inputs.items():
        if isinstance(value, str) and value:
            normalized[key] = rel(Path(value))
        else:
            normalized[key] = value
    return normalized


def p2_physical_gate_summary(path: Path = P2_CONTACT_CORRELATION_AUDIT) -> dict[str, Any]:
    payload = load_json(path)
    gate = dict(payload.get("physical_gazebo_contact_gate", {}))
    claim_boundary = payload.get("claim_boundary_gate") if isinstance(payload.get("claim_boundary_gate"), dict) else {}
    allowed_claim = str(payload.get("allowed_claim") or "")
    force_contact_proven = bool(gate.get("force_contact_physics_proven"))
    eoat_count = int(gate.get("eoat_collision_count") or 0)
    blocker_tokens: list[str] = []
    if eoat_count == 0:
        blocker_tokens.append("eoat_collision_count=0")
    if not force_contact_proven:
        blocker_tokens.append("force_contact_physics_proven=false")
    return {
        "artifact": rel(path),
        "claim_tier": "physical Gazebo collision/contact physics" if force_contact_proven else "visual_only",
        "target_claim_tier": "physical Gazebo collision/contact physics",
        "status": gate.get("status", "blocked_not_proven"),
        "eoat_collision_count": eoat_count,
        "contact_pair_log_evidence": bool(gate.get("contact_pair_log_evidence")),
        "adapter_verified_gazebo_contact_wrench": bool(gate.get("adapter_verified_gazebo_contact_wrench")),
        "wrench_contact_correlation": bool(gate.get("wrench_contact_correlation")),
        "force_contact_physics_proven": force_contact_proven,
        "scope": (
            "standalone_p2_witness_single_contact_point_wrench"
            if force_contact_proven and "single contact-point wrench" in allowed_claim
            else "stage_specific_or_unqualified"
        ),
        "stage_specific_contact_physics_proven": False,
        "total_contact_wrench_proven": bool(claim_boundary.get("total_contact_wrench_proven")),
        "same_run_concurrent_dual_sensor_observation": not bool(
            claim_boundary.get("surface_eoat_cross_check_may_be_cross_run_repeatability_not_concurrent_observation")
        )
        if force_contact_proven
        else False,
        "blocker_tokens": blocker_tokens,
        "known_blockers": payload.get("known_blockers", []),
        "inputs": normalize_input_paths(payload.get("inputs", {})),
        "allowed_claim": allowed_claim,
        "forbidden_claim": str(
            payload.get(
                "forbidden_claim",
                "real bench/live contact; physical Gazebo collision/contact physics unless all gate booleans are true",
            )
        ),
    }


def trace_has_simulated_ft_metadata(trace: dict[str, Any] | None) -> bool:
    if not trace:
        return False
    rows = trace.get("rows") or []
    if not rows:
        return False
    first = rows[0]
    header = first.get("header") if isinstance(first, dict) else None
    return bool(
        isinstance(header, dict)
        and "stamp_s" in header
        and header.get("frame_id")
        and first.get("source")
        and first.get("status")
        and first.get("baseline_policy")
        and trace.get("sample_count", len(rows)) > 0
    )


def stage_trace_evidence_fields(trace: dict[str, Any], *, log_path: Path | None = None) -> dict[str, bool]:
    rows = trace.get("rows") or []
    first = rows[0] if rows else {}
    header = first.get("header") if isinstance(first, dict) else {}
    return {
        "stamp": isinstance(header, dict) and "stamp_s" in header,
        "frame_id": isinstance(header, dict) and bool(header.get("frame_id")),
        "source": bool(first.get("source")),
        "status": bool(first.get("status")),
        "baseline": bool(first.get("baseline_policy")),
        "log_evidence": bool(log_path is None or log_path.is_file()) and len(rows) > 0,
    }


def stage_trace_contact_summary(trace: dict[str, Any]) -> dict[str, Any]:
    rows = trace.get("rows") or []
    contact_state_values = sorted({str(row.get("contact_state")) for row in rows})
    max_force_norm_n = float(trace.get("max_force_norm_n") or 0.0)
    max_normal_load_n = float(trace.get("max_normal_load_n") or 0.0)
    return {
        "contact_state_values": contact_state_values,
        "has_contact_state": "contact" in contact_state_values,
        "max_force_norm_n": max_force_norm_n,
        "max_normal_load_n": max_normal_load_n,
        "has_nonzero_load": max_force_norm_n > EPS and max_normal_load_n > EPS,
    }


def stage_trace_freshness_summary(trace: dict[str, Any]) -> dict[str, Any]:
    rows = trace.get("rows") or []
    stamps = [
        float(row.get("header", {}).get("stamp_s"))
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("header"), dict) and "stamp_s" in row["header"]
    ]
    stale_after_values = [
        float(row.get("stale_after_s"))
        for row in rows
        if isinstance(row, dict) and row.get("stale_after_s") is not None
    ]
    intervals = [b - a for a, b in zip(stamps, stamps[1:])]
    max_interval_s = max(intervals, default=0.0)
    min_stale_after_s = min(stale_after_values, default=0.0)
    return {
        "max_sample_interval_s": max_interval_s,
        "min_stale_after_s": min_stale_after_s,
        "freshness_ok": bool(stamps) and (not intervals or max_interval_s <= min_stale_after_s + EPS),
    }


def validate_stage_simulated_ft_log(log_path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "log_path": rel(log_path),
        "valid": False,
        "claim_tier": "visual_only",
        "sample_count": 0,
        "contact_semantics": {},
        "freshness": {},
        "evidence_fields_present": {
            "stamp": False,
            "frame_id": False,
            "source": False,
            "status": False,
            "baseline": False,
            "log_evidence": False,
        },
        "validation_issues": [],
    }
    if not log_path.is_file():
        result["validation_issues"] = ["log_path:missing"]
        return result

    try:
        payload = load_json(log_path)
    except (OSError, json.JSONDecodeError) as exc:
        result["validation_issues"] = [f"log_path:unreadable:{type(exc).__name__}"]
        return result

    issues: list[str] = []
    if payload.get("schema") != STAGE_SIM_FT_LOG_SCHEMA:
        issues.append("schema:not_stage_canonical_simulated_ft_log")
    trace = payload.get("trace")
    if not isinstance(trace, dict):
        issues.append("trace:missing")
        result["validation_issues"] = issues
        return result

    if trace.get("schema") != wrench_contract.TRACE_SCHEMA:
        issues.append("trace_schema:not_canonical_wrench_trace")
    if trace.get("force_source") != wrench_contract.SOURCE_SIMULATED_FT:
        issues.append("force_source:not_simulated_ft")
    if trace.get("claim_tier") != "simulated_ft":
        issues.append("claim_tier:not_simulated_ft")
    if trace.get("schema_issues"):
        issues.append("trace_schema_issues:not_empty")

    rows = trace.get("rows") or []
    fields = stage_trace_evidence_fields(trace, log_path=log_path)
    missing_fields = [field for field, present in fields.items() if not present]
    issues.extend(f"missing:{field}" for field in missing_fields)
    contact_semantics = stage_trace_contact_summary(trace)
    if not contact_semantics["has_contact_state"]:
        issues.append("contact_state:no_contact_only")
    if not contact_semantics["has_nonzero_load"]:
        issues.append("normal_load:not_positive")
    freshness = stage_trace_freshness_summary(trace)
    if not freshness["freshness_ok"]:
        issues.append(
            f"freshness:max_interval_{freshness['max_sample_interval_s']:.6f}_gt_stale_after_{freshness['min_stale_after_s']:.6f}"
        )

    for index, row in enumerate(rows):
        sample_issues = wrench_contract.validate_canonical_sample_row(row)
        if sample_issues:
            issues.append(f"row_{index}:" + ",".join(sample_issues))
        if row.get("source") != wrench_contract.SOURCE_SIMULATED_FT:
            issues.append(f"row_{index}:source:not_simulated_ft")
        if row.get("claim_tier") != "simulated_ft":
            issues.append(f"row_{index}:claim_tier:not_simulated_ft")

    result.update(
        {
            "valid": not issues,
            "claim_tier": "simulated_ft" if not issues else "visual_only",
            "sample_count": int(trace.get("sample_count") or len(rows)),
            "contact_semantics": contact_semantics,
            "freshness": freshness,
            "evidence_fields_present": fields,
            "validation_issues": issues,
        }
    )
    return result


def load_stage_simulated_ft_manifest(manifest_path: Path | None) -> dict[str, Any]:
    if manifest_path is None:
        return {
            "manifest_path": None,
            "status": "not_attached",
            "stages": {},
            "validation_issues": [],
        }
    if not manifest_path.is_file():
        return {
            "manifest_path": str(manifest_path),
            "status": "missing",
            "stages": {},
            "validation_issues": ["manifest_path:missing"],
        }

    payload = load_json(manifest_path)
    issues: list[str] = []
    if payload.get("schema") != STAGE_SIM_FT_PACK_SCHEMA:
        issues.append("schema:not_step_simulated_ft_evidence_pack")
    raw_stages = payload.get("stages")
    if not isinstance(raw_stages, dict):
        issues.append("stages:not_object")
        raw_stages = {}

    stages: dict[str, Any] = {}
    for stage_id, summary in raw_stages.items():
        if not isinstance(summary, dict):
            stages[stage_id] = {
                "valid": False,
                "claim_tier": "visual_only",
                "validation_issues": ["stage_summary:not_object"],
            }
            continue
        log_ref = summary.get("log_path")
        log_path = WORKSPACE / log_ref if isinstance(log_ref, str) and not Path(log_ref).is_absolute() else Path(str(log_ref))
        stage_validation = validate_stage_simulated_ft_log(log_path)
        stages[stage_id] = stage_validation
        if not stage_validation.get("valid"):
            for issue in stage_validation.get("validation_issues", []):
                issues.append(f"stage:{stage_id}:{issue}")

    return {
        "manifest_path": rel(manifest_path),
        "status": "valid" if not issues and all(stage.get("valid") for stage in stages.values()) else "invalid_or_partial",
        "stages": stages,
        "validation_issues": issues,
    }


def stage_status_row(
    stage_id: str,
    p2_gate: dict[str, Any],
    stage_sim_ft_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = step56.STAGE_REGISTRY[stage_id]
    artifact = step56.build_stage_artifact(stage_id)
    stage = artifact["stage"]
    trace = artifact.get("simulated_force_evidence")
    has_simulated_ft = trace_has_simulated_ft_metadata(trace)
    attached = stage_sim_ft_evidence or {}
    attached_present = bool(attached)
    attached_valid = bool(attached.get("valid")) and attached.get("claim_tier") == "simulated_ft"
    contact = bool(spec.contact)

    if has_simulated_ft and attached_valid:
        claim_tier = "simulated_ft"
        simulated_ft_status = "per_stage_canonical_log_evidence_attached"
        per_stage_log_evidence = True
        allowed_claim = (
            "simulated_ft per-stage canonical wrench log evidence only; not physical Gazebo collision/contact "
            "physics and not real bench/live contact"
        )
    elif has_simulated_ft and attached_present:
        claim_tier = "virtual/software force-loop"
        simulated_ft_status = "per_stage_canonical_log_invalid_or_baseline_only"
        per_stage_log_evidence = False
        allowed_claim = (
            "baseline-only or invalid per-stage canonical log evidence; not accepted as contact-stage simulated_ft"
        )
    elif has_simulated_ft:
        claim_tier = "virtual/software force-loop"
        simulated_ft_status = "not_per_stage_canonical_log_evidence"
        per_stage_log_evidence = False
        allowed_claim = (
            "virtual/software force-loop path status only; global P1 simulated_ft evidence is separate "
            "and per-stage canonical simulated FT logs are not attached"
        )
    else:
        claim_tier = "visual_only"
        simulated_ft_status = "not_applicable"
        per_stage_log_evidence = False
        allowed_claim = "visual_only/offline path or observer status only; no force/contact physics claim"

    if contact:
        gazebo_status = (
            "standalone_p2_witness_proven_not_stage_specific"
            if p2_gate["force_contact_physics_proven"]
            else p2_gate["status"]
        )
        blocker = (
            f"{spec.known_blocker}; force_contact_physics_proven=false; "
            "wrench/contact correlation missing or not proven"
            if not p2_gate["force_contact_physics_proven"]
            else (
                f"{spec.known_blocker}; per-stage Gazebo contact physics not proven; "
                "current physical Gazebo evidence is standalone P2 witness only"
            )
        )
    else:
        gazebo_status = "not_claimed"
        blocker = spec.known_blocker

    return {
        "stage_id": stage_id,
        "path_shape": stage.get("shape"),
        "source_spec": stage.get("source_stage_id"),
        "safe_frame": artifact.get("safe_frame", {}).get("source"),
        "contact_or_no_contact": "contact" if contact else "no_contact",
        "control_route": spec.runner_status,
        "inner_rnn_status": inner_rnn_status(stage_id),
        "outer_loop_status": outer_loop_status(stage_id),
        "simulated_ft_status": simulated_ft_status,
        "per_stage_simulated_ft_log_evidence": per_stage_log_evidence,
        "per_stage_simulated_ft_log_artifact": attached.get("log_path"),
        "per_stage_simulated_ft_sample_count": attached.get("sample_count", 0),
        "per_stage_simulated_ft_evidence_fields_present": attached.get(
            "evidence_fields_present",
            {
                "stamp": False,
                "frame_id": False,
                "source": False,
                "status": False,
                "baseline": False,
                "log_evidence": False,
            },
        ),
        "per_stage_simulated_ft_contact_semantics": attached.get("contact_semantics", {}),
        "per_stage_simulated_ft_freshness": attached.get("freshness", {}),
        "per_stage_simulated_ft_validation_issues": attached.get("validation_issues", []),
        "gazebo_contact_physics_status": gazebo_status,
        "evidence_artifact": "generated_from_step56_simulation_matrix",
        "current_blocker": blocker,
        "allowed_claim": allowed_claim,
        "forbidden_claim": (
            "physical Gazebo collision/contact physics unless EOAT collision evidence, contact pair/log evidence, "
            "and wrench/contact correlation are all present for this stage; real bench/live contact; "
            "live bridge/TP/URScript/motion"
        ),
        "claim_tier": claim_tier,
        "source_paths": artifact.get("source_paths", {}),
    }


def inner_rnn_status(stage_id: str) -> str:
    if stage_id == "step5d":
        return "blocked_pending_pdf_truth_extraction"
    if stage_id == "step5c":
        return "quarantined_offline_only"
    return "not_applicable"


def outer_loop_status(stage_id: str) -> str:
    if stage_id == "step5d":
        return "offline_paper_outer_loop_source_present_live_blocked"
    if stage_id == "step5c":
        return "not_current_route"
    return "not_applicable"


def rnn_interface_table() -> list[dict[str, Any]]:
    paper_truth = load_json(STEP5D_PAPER_TRUTH)
    numeric_sanity = load_json(STEP5D_NUMERIC_SANITY) if STEP5D_NUMERIC_SANITY.exists() else {}
    numeric_gate = "overall_pass=true" if numeric_sanity.get("overall_pass") else "not_current_or_missing"
    pending = pending_paper_truth_fields(paper_truth)
    return [
        {
            "interface": "path_provider",
            "source": rel(Path(step56.__file__)),
            "input": "Step5/6 stage tables and safe-frame configs",
            "output": "offline task-space reference rows",
            "status": "source_backed_offline_only",
            "claim_tier": "visual_only",
            "forbidden_claim": "no live motion, no force/contact physics, no real bench/live contact",
        },
        {
            "interface": "outer_loop",
            "source": rel(EXPERIMENT_ROOT / "tools" / "step5d_paper_outer_loop.py"),
            "input": "pose/speed/reference and software force input",
            "output": "xdot_c task velocity",
            "status": "offline_source_present_live_blocked",
            "claim_tier": "virtual/software force-loop",
            "forbidden_claim": "no physical Gazebo collision/contact physics; no real bench/live contact",
        },
        {
            "interface": "inner_strict_rnn_solver",
            "source": rel(EXPERIMENT_ROOT / "tools" / "step5c_strict_rnn.py"),
            "input": "J(q), xdot_c, omega bounds, dt, epsilon, r",
            "output": "qdot and solver diagnostics",
            "status": "blocked_pending_pdf_truth_extraction" if pending else "offline_solver_source_present",
            "claim_tier": "virtual/software force-loop",
            "evidence": {
                "paper_truth": rel(STEP5D_PAPER_TRUTH),
                "strict_rnn_enabled": bool(paper_truth.get("strict_rnn_enabled")),
                "pending_pdf_verify_count": len(pending),
                "pending_pdf_verify_fields": pending,
            },
            "forbidden_claim": "no live bridge; no controller upload; no robot motion; no real bench/live contact",
        },
        {
            "interface": "carrier_registers_ros2_runner",
            "source": rel(EXPERIMENT_ROOT / "tools" / "step5d_full_chain_sanity.py"),
            "input": "qdot solver output",
            "output": "register carriers 37..47 and full-chain residual audit",
            "status": f"offline_structural_sanity_{numeric_gate}",
            "claim_tier": "virtual/software force-loop",
            "evidence": {"numeric_sanity": rel(STEP5D_NUMERIC_SANITY)},
            "forbidden_claim": "no live bridge; no TP play; no controller upload; no real bench/live contact",
        },
    ]


def pending_paper_truth_fields(payload: dict[str, Any]) -> list[str]:
    pending = [str(field) for field in payload.get("pending_pdf_verify", [])]
    for section_name, section in payload.get("sections", {}).items():
        if not isinstance(section, dict):
            continue
        for field in section.get("pending_pdf_verify", []):
            pending.append(f"{section_name}.{field}")
    return sorted(pending)


def strict_rnn_final_acceptance_gate() -> dict[str, Any]:
    paper_truth = load_json(STEP5D_PAPER_TRUTH)
    numeric_sanity = load_json(STEP5D_NUMERIC_SANITY) if STEP5D_NUMERIC_SANITY.exists() else {}
    pending = pending_paper_truth_fields(paper_truth)
    strict_rnn_enabled = bool(paper_truth.get("strict_rnn_enabled"))
    numeric_sanity_pass = bool(numeric_sanity.get("overall_pass"))
    solver_source_present = (EXPERIMENT_ROOT / "tools" / "step5c_strict_rnn.py").exists()
    solver_tests_present = (EXPERIMENT_ROOT / "tests" / "test_step5d_strict_rnn_solver.py").exists()
    blockers: list[str] = []
    if not solver_source_present:
        blockers.append("solver_source:missing")
    if not solver_tests_present:
        blockers.append("solver_tests:missing")
    if not strict_rnn_enabled:
        blockers.append("paper_truth:strict_rnn_disabled")
    if pending:
        blockers.append("paper_truth:pending_pdf_verify")
    if not numeric_sanity_pass:
        blockers.append("numeric_sanity:not_passed_or_missing")
    allowed = not blockers
    status = "accepted_offline_solver_contract"
    if not allowed:
        status = (
            "blocked_pending_pdf_truth_extraction"
            if (not strict_rnn_enabled or pending)
            else "blocked_missing_required_evidence"
        )
    return {
        "gate": "strict_rnn_final_acceptance",
        "fail_closed": True,
        "strict_rnn_final_acceptance_allowed": allowed,
        "status": status,
        "claim_tier": "virtual/software force-loop",
        "target_claim_tier": "virtual/software force-loop",
        "blockers": blockers,
        "evidence": {
            "solver_source": rel(EXPERIMENT_ROOT / "tools" / "step5c_strict_rnn.py"),
            "solver_source_present": solver_source_present,
            "solver_tests": rel(EXPERIMENT_ROOT / "tests" / "test_step5d_strict_rnn_solver.py"),
            "solver_tests_present": solver_tests_present,
            "paper_truth": rel(STEP5D_PAPER_TRUTH),
            "strict_rnn_enabled": strict_rnn_enabled,
            "pending_pdf_verify_count": len(pending),
            "pending_pdf_verify_fields": pending,
            "numeric_sanity": rel(STEP5D_NUMERIC_SANITY),
            "numeric_sanity_overall_pass": numeric_sanity_pass,
            "numeric_sanity_force_input": numeric_sanity.get("assumptions", {}).get("force_input"),
            "numeric_sanity_contact_evidence": numeric_sanity.get("assumptions", {}).get("contact_evidence"),
        },
        "allowed_claim": (
            "offline strict RNN solver/source and numeric structural sanity only when paper-truth config is enabled "
            "and all PDF verification fields are closed"
        ),
        "forbidden_claim": (
            "simulated_ft; physical Gazebo collision/contact physics; real bench/live contact; live bridge/TP/URScript/motion"
        ),
    }


def current_goal_lineage_rows(p2_gate: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in wrench_contract.force_source_lineage_table():
        source_name = row["source_name"]
        current_tier = current_report_tier_for_source(source_name, p2_gate=p2_gate)
        rows.append(
            {
                "source_name": source_name,
                "canonical_allowed_claim_tier": row.get("allowed_claim_tier"),
                "current_report_claim_tier": current_tier,
                "target_claim_tier": target_report_tier_for_source(source_name),
                "input_topic_or_file": row.get("input_topic_or_file"),
                "output_topic": row.get("output_topic"),
                "frame_id": row.get("frame_id"),
                "zero_baseline_policy": row.get("zero_baseline_policy"),
                "current_goal_status": current_goal_status_for_source(source_name, p2_gate=p2_gate),
            }
        )
    return rows


def current_report_tier_for_source(source_name: str, *, p2_gate: dict[str, Any] | None = None) -> str:
    if source_name == wrench_contract.SOURCE_VIRTUAL_SOFTWARE:
        return "virtual/software force-loop"
    if source_name in {wrench_contract.SOURCE_SOFTWARE_REPLAY, wrench_contract.SOURCE_SIMULATED_FT}:
        return "simulated_ft"
    if source_name == wrench_contract.SOURCE_GAZEBO_CONTACT:
        if p2_gate and p2_gate.get("force_contact_physics_proven"):
            return "physical Gazebo collision/contact physics"
        return "visual_only"
    if source_name == wrench_contract.SOURCE_REAL_KUNWEI_READ_ONLY:
        return "visual_only"
    return "visual_only"


def target_report_tier_for_source(source_name: str) -> str:
    if source_name == wrench_contract.SOURCE_VIRTUAL_SOFTWARE:
        return "virtual/software force-loop"
    if source_name in {wrench_contract.SOURCE_SOFTWARE_REPLAY, wrench_contract.SOURCE_SIMULATED_FT}:
        return "simulated_ft"
    if source_name == wrench_contract.SOURCE_GAZEBO_CONTACT:
        return "physical Gazebo collision/contact physics"
    if source_name == wrench_contract.SOURCE_REAL_KUNWEI_READ_ONLY:
        return "not authorized in current goal"
    return "visual_only"


def current_goal_status_for_source(source_name: str, *, p2_gate: dict[str, Any] | None = None) -> str:
    if source_name == wrench_contract.SOURCE_GAZEBO_CONTACT:
        if p2_gate and p2_gate.get("force_contact_physics_proven"):
            return (
                "standalone P2 witness physical Gazebo contact physics is proven with one native contact-point "
                "wrench correlation; per-stage Gazebo contact physics and total contact wrench remain not proven"
            )
        return "requires EOAT collision evidence, contact pair/log evidence, and wrench/contact correlation; current P2 is blocked/not proven"
    if source_name == wrench_contract.SOURCE_REAL_KUNWEI_READ_ONLY:
        return "not authorized for real bench/live contact in this goal; retained logs do not authorize live contact"
    if source_name == wrench_contract.SOURCE_SIMULATED_FT:
        return "accepted only as simulated_ft when stamp/frame_id/source/status/baseline/log evidence exist"
    if source_name == wrench_contract.SOURCE_VIRTUAL_SOFTWARE:
        return "software-only force loop; not physical contact evidence"
    return "offline replay only; not live evidence"


def build_audit(
    *,
    generated_at: str | None = None,
    p1_path: Path = P1_SIMULATED_FT,
    p2_correlation_path: Path = P2_CONTACT_CORRELATION_AUDIT,
    stage_sim_ft_manifest_path: Path | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now().isoformat(timespec="seconds")
    p1 = p1_simulated_ft_summary(p1_path)
    p2 = p2_physical_gate_summary(p2_correlation_path)
    resolved_stage_sim_ft_manifest_path = stage_sim_ft_manifest_path or default_stage_sim_ft_manifest_from_p1(p1_path)
    stage_sim_ft_manifest = load_stage_simulated_ft_manifest(resolved_stage_sim_ft_manifest_path)
    stage_sim_ft_rows = stage_sim_ft_manifest["stages"]
    rows = [stage_status_row(stage_id, p2, stage_sim_ft_rows.get(stage_id)) for stage_id in step56.STAGE_REGISTRY]
    strict_gate = strict_rnn_final_acceptance_gate()
    p2_inputs = p2.get("inputs", {})
    return {
        "schema": "ur10e_step_status_rnn_audit_v1",
        "generated_at": generated,
        "goal_lineage": GOAL_LINEAGE,
        "mode": "offline_no_live_step_status_rnn_audit",
        "claim_boundary_gate": claim_boundary_gate(),
        "steering_note": {
            "recorded": True,
            "source": "2026-06-21 additive steering note",
            "acceptance_gate_changed": True,
            "effect": "All checkpoint/report/final claims must use the five explicit evidence tiers and downgrade missing or ambiguous evidence.",
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
        "source_artifacts": {
            "p1_simulated_ft": rel(p1_path),
            "p2_contact_pair_log": p2_inputs.get("contact_pair_path") or rel(P2_CONTACT_PAIR_LOG),
            "p2_wrench_adapter_report": p2_inputs.get("wrench_path") or rel(P2_WRENCH_ADAPTER_REPORT),
            "p2_contact_correlation_audit": rel(p2_correlation_path),
            "stage_simulated_ft_manifest": stage_sim_ft_manifest["manifest_path"],
            "step56_simulation_matrix": rel(Path(step56.__file__)),
            "step5d_paper_truth": rel(STEP5D_PAPER_TRUTH),
            "step5d_numeric_sanity": rel(STEP5D_NUMERIC_SANITY),
        },
        "p1_simulated_ft": p1,
        "p2_physical_gazebo_contact": p2,
        "stage_simulated_ft_evidence": {
            "claim_tier": "simulated_ft" if stage_sim_ft_manifest["status"] == "valid" else "visual_only",
            "manifest_path": stage_sim_ft_manifest["manifest_path"],
            "status": stage_sim_ft_manifest["status"],
            "validation_issues": stage_sim_ft_manifest["validation_issues"],
            "attached_stage_count": len(stage_sim_ft_rows),
            "valid_stage_count": sum(1 for stage in stage_sim_ft_rows.values() if stage.get("valid")),
            "forbidden_claim": "physical Gazebo collision/contact physics; real bench/live contact",
        },
        "step_status_matrix": rows,
        "rnn_interface_table": rnn_interface_table(),
        "strict_rnn_final_acceptance_gate": strict_gate,
        "force_source_lineage_current_goal": current_goal_lineage_rows(p2),
        "audit_coverage": {
            "stage_rows": len(rows),
            "rnn_interfaces": 4,
            "strict_rnn_final_acceptance_allowed": strict_gate["strict_rnn_final_acceptance_allowed"],
            "strict_rnn_final_acceptance_status": strict_gate["status"],
            "strict_rnn_claim_tier": strict_gate["claim_tier"],
            "p1_claim_tier": p1["claim_tier"],
            "p2_claim_tier": p2["claim_tier"],
            "p2_scope": p2["scope"],
            "stage_specific_contact_physics_proven": p2["stage_specific_contact_physics_proven"],
            "stage_simulated_ft_manifest_status": stage_sim_ft_manifest["status"],
            "per_stage_simulated_ft_attached_count": sum(
                1 for row in rows if row["per_stage_simulated_ft_log_evidence"]
            ),
            "full_acceptance_allowed": False,
            "full_acceptance_blocker": (
                "Full reproduction is not accepted: current physical Gazebo evidence is standalone P2 witness "
                "only; per-stage Gazebo contact physics, strict RNN final acceptance, integrated demo, and real "
                "bench/live contact remain unaccepted or unauthorized."
            ),
        },
    }


def write_audit(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    p1_path: Path = P1_SIMULATED_FT,
    p2_correlation_path: Path = P2_CONTACT_CORRELATION_AUDIT,
    stage_sim_ft_manifest_path: Path | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "step_status_rnn_audit.json"
    payload = build_audit(
        generated_at=generated_at,
        p1_path=p1_path,
        p2_correlation_path=p2_correlation_path,
        stage_sim_ft_manifest_path=stage_sim_ft_manifest_path,
    )
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--p1-path", type=Path, default=P1_SIMULATED_FT)
    parser.add_argument("--p2-correlation-path", type=Path, default=P2_CONTACT_CORRELATION_AUDIT)
    parser.add_argument("--stage-sim-ft-manifest", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = write_audit(
        args.output_dir,
        generated_at=args.generated_at,
        p1_path=args.p1_path,
        p2_correlation_path=args.p2_correlation_path,
        stage_sim_ft_manifest_path=args.stage_sim_ft_manifest,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
