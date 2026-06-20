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
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0146"
    / "p1_auditor_installed_runtime_observed_ros2_simulated_ft.json"
)
P2_CONTACT_PAIR_LOG = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0320_p2_contact_pair_capture_strict_v2"
    / "p2_gazebo_contact_pair_log.json"
)
P2_WRENCH_ADAPTER_REPORT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0320_p2_gazebo_contact_wrench_adapter_strict_v2"
    / "p2_gazebo_contact_wrench_adapter_report.json"
)
P2_CONTACT_CORRELATION_AUDIT = (
    RUNS
    / "ur10e_gazebo_17h_sim_ft_rnn_20260621_0326_p2_gazebo_contact_wrench_adapter_strict_v2_correlation_audit"
    / "p2_contact_correlation_audit.json"
)
STEP5D_PAPER_TRUTH = EXPERIMENT_ROOT / "config" / "step5c_tase_paper_truth.json"
STEP5D_NUMERIC_SANITY = RUNS / "step5d_numeric_sanity_20260614_215555" / "step5d_numeric_sanity.json"

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


def p2_physical_gate_summary(path: Path = P2_CONTACT_CORRELATION_AUDIT) -> dict[str, Any]:
    payload = load_json(path)
    gate = dict(payload.get("physical_gazebo_contact_gate", {}))
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
        "blocker_tokens": blocker_tokens,
        "known_blockers": payload.get("known_blockers", []),
        "inputs": payload.get("inputs", {}),
        "forbidden_claim": "real bench/live contact; physical Gazebo collision/contact physics unless all gate booleans are true",
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


def stage_status_row(stage_id: str, p2_gate: dict[str, Any]) -> dict[str, Any]:
    spec = step56.STAGE_REGISTRY[stage_id]
    artifact = step56.build_stage_artifact(stage_id)
    stage = artifact["stage"]
    trace = artifact.get("simulated_force_evidence")
    has_simulated_ft = trace_has_simulated_ft_metadata(trace)
    contact = bool(spec.contact)

    if has_simulated_ft:
        claim_tier = "virtual/software force-loop"
        simulated_ft_status = "not_per_stage_canonical_log_evidence"
        allowed_claim = (
            "virtual/software force-loop path status only; global P1 simulated_ft evidence is separate "
            "and per-stage canonical simulated FT logs are not attached"
        )
    else:
        claim_tier = "visual_only"
        simulated_ft_status = "not_applicable"
        allowed_claim = "visual_only/offline path or observer status only; no force/contact physics claim"

    if contact:
        gazebo_status = p2_gate["status"]
        blocker = (
            f"{spec.known_blocker}; force_contact_physics_proven=false; "
            "wrench/contact correlation missing or not proven"
            if not p2_gate["force_contact_physics_proven"]
            else spec.known_blocker
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
        "per_stage_simulated_ft_log_evidence": False,
        "gazebo_contact_physics_status": gazebo_status,
        "evidence_artifact": "generated_from_step56_simulation_matrix",
        "current_blocker": blocker,
        "allowed_claim": allowed_claim,
        "forbidden_claim": (
            "physical Gazebo collision/contact physics unless EOAT collision evidence, contact pair/log evidence, "
            "and wrench/contact correlation are all present; real bench/live contact; live bridge/TP/URScript/motion"
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
    pending = paper_truth.get("pending_pdf_verify", [])
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


def current_goal_lineage_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in wrench_contract.force_source_lineage_table():
        source_name = row["source_name"]
        current_tier = current_report_tier_for_source(source_name)
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
                "current_goal_status": current_goal_status_for_source(source_name),
            }
        )
    return rows


def current_report_tier_for_source(source_name: str) -> str:
    if source_name == wrench_contract.SOURCE_VIRTUAL_SOFTWARE:
        return "virtual/software force-loop"
    if source_name in {wrench_contract.SOURCE_SOFTWARE_REPLAY, wrench_contract.SOURCE_SIMULATED_FT}:
        return "simulated_ft"
    if source_name == wrench_contract.SOURCE_GAZEBO_CONTACT:
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
        return "real bench/live contact"
    return "visual_only"


def current_goal_status_for_source(source_name: str) -> str:
    if source_name == wrench_contract.SOURCE_GAZEBO_CONTACT:
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
) -> dict[str, Any]:
    generated = generated_at or datetime.now().isoformat(timespec="seconds")
    p1 = p1_simulated_ft_summary(p1_path)
    p2 = p2_physical_gate_summary(p2_correlation_path)
    rows = [stage_status_row(stage_id, p2) for stage_id in step56.STAGE_REGISTRY]
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
            "p2_contact_pair_log": rel(P2_CONTACT_PAIR_LOG),
            "p2_wrench_adapter_report": rel(P2_WRENCH_ADAPTER_REPORT),
            "p2_contact_correlation_audit": rel(p2_correlation_path),
            "step56_simulation_matrix": rel(Path(step56.__file__)),
            "step5d_paper_truth": rel(STEP5D_PAPER_TRUTH),
            "step5d_numeric_sanity": rel(STEP5D_NUMERIC_SANITY),
        },
        "p1_simulated_ft": p1,
        "p2_physical_gazebo_contact": p2,
        "step_status_matrix": rows,
        "rnn_interface_table": rnn_interface_table(),
        "force_source_lineage_current_goal": current_goal_lineage_rows(),
        "audit_coverage": {
            "stage_rows": len(rows),
            "rnn_interfaces": 4,
            "p1_claim_tier": p1["claim_tier"],
            "p2_claim_tier": p2["claim_tier"],
            "full_acceptance_allowed": False,
            "full_acceptance_blocker": "P2 physical Gazebo collision/contact physics is blocked/not proven and real bench/live contact is not authorized.",
        },
    }


def write_audit(
    output_dir: Path,
    *,
    generated_at: str | None = None,
    p1_path: Path = P1_SIMULATED_FT,
    p2_correlation_path: Path = P2_CONTACT_CORRELATION_AUDIT,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "step_status_rnn_audit.json"
    payload = build_audit(generated_at=generated_at, p1_path=p1_path, p2_correlation_path=p2_correlation_path)
    payload["artifact_path"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--p1-path", type=Path, default=P1_SIMULATED_FT)
    parser.add_argument("--p2-correlation-path", type=Path, default=P2_CONTACT_CORRELATION_AUDIT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = write_audit(
        args.output_dir,
        generated_at=args.generated_at,
        p1_path=args.p1_path,
        p2_correlation_path=args.p2_correlation_path,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
