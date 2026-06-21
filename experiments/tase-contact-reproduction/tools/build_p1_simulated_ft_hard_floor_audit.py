#!/usr/bin/env python3
"""Build a fail-closed P1 simulated-FT hard-floor audit.

This audit aggregates the existing offline canonical-wrench, no-contact,
source-isolation, staleness, replay-parity, per-stage simulated-FT, and Step5d
force/frame semantic gates. It does not run Gazebo, start a bridge, or touch
real hardware.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
PACKAGE_ROOT = WORKSPACE / "src" / "ur10e_example_controllers"
TOOLS_ROOT = EXPERIMENT_ROOT / "tools"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLS_ROOT))

from ur10e_example_controllers import canonical_simulated_ft_runtime as runtime  # noqa: E402
from ur10e_example_controllers import canonical_wrench_contract as contract  # noqa: E402
from ur10e_example_controllers import step5b_contact_control_core as step5b_core  # noqa: E402
from ur10e_example_controllers import step5b_simulation_mvp as step5b_mvp  # noqa: E402
from ur10e_example_controllers import step56_simulation_matrix as step56  # noqa: E402

import build_step_simulated_ft_evidence_pack as sim_ft_pack  # noqa: E402
import ur_contact_semantic_gate as semantic_gate  # noqa: E402


SCHEMA = "ur10e_p1_simulated_ft_hard_floor_audit_v1"
GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"
RETAINED_KUNWEI_CSV = (
    EXPERIMENT_ROOT
    / "runs"
    / "bridge_step4e_line_outerloop_v9_autowatch_20260609_134652"
    / "kunwei_sensor_1khz.csv"
)
FORBIDDEN_SOURCE_TOKENS = (
    "gazebo_joint_state_fk_virtual_surface_model",
    "/gazebo/",
    "/world/",
)
FORBIDDEN_RUNTIME_TOKENS = (
    "gazebo_joint_state_fk_virtual_surface_model",
    "kunwei_rtde_bridge",
    "zero_ftsensor",
    "load_program",
    "play_program",
    "speedl(",
)


def rel(path: Path | str | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(WORKSPACE.resolve()))
    except (OSError, ValueError):
        return str(path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _check_result(*, passed: bool, claim_tier: str, evidence: dict[str, Any], blocker: str | None = None) -> dict[str, Any]:
    return {
        "pass": bool(passed),
        "claim_tier": claim_tier,
        "blocker": blocker if not passed else None,
        "evidence": evidence,
    }


def contract_schema_check() -> dict[str, Any]:
    spec = contract.canonical_contract_spec()
    required_topics = {
        "/joint_states",
        "/tf",
        "/tf_static",
        "/clock",
    }
    required_frames = set(contract.REQUIRED_FRAMES)
    present_topics = set(spec.get("required_input_topics", []))
    present_frames = set(spec.get("required_frames", []))
    passed = bool(
        required_topics.issubset(present_topics)
        and required_frames.issubset(present_frames)
        and set(spec.get("source_classes", [])) == set(contract.SOURCE_CLASSES)
        and spec.get("consumer_interface", {}).get("controller_private_gazebo_topic_dependency_allowed") is False
        and spec.get("force_frame_semantics", {}).get("orientation_target_axis") == "approach_normal"
    )
    return _check_result(
        passed=passed,
        claim_tier="simulated_ft",
        evidence={
            "contract_schema": spec.get("schema"),
            "canonical_wrench_topic": spec.get("canonical_wrench_topic"),
            "required_topics_present": sorted(required_topics & present_topics),
            "missing_required_topics": sorted(required_topics - present_topics),
            "missing_required_frames": sorted(required_frames - present_frames),
            "source_classes": spec.get("source_classes", []),
            "consumer_frame_id": spec.get("consumer_interface", {}).get("consumer_frame_id"),
            "wrench_frame_policy": spec.get("consumer_interface", {}).get("wrench_frame_policy"),
            "force_frame_semantics": spec.get("force_frame_semantics"),
        },
        blocker="canonical contract schema missing required topics/frames/source policy",
    )


def source_isolation_check() -> dict[str, Any]:
    core_path = Path(step5b_core.__file__).resolve()
    runtime_path = Path(runtime.__file__).resolve()
    core_source = core_path.read_text(encoding="utf-8")
    runtime_source = runtime_path.read_text(encoding="utf-8")
    core_hits = [token for token in FORBIDDEN_SOURCE_TOKENS if token in core_source]
    runtime_hits = [token for token in FORBIDDEN_RUNTIME_TOKENS if token in runtime_source]
    passed = not core_hits and not runtime_hits
    return _check_result(
        passed=passed,
        claim_tier="simulated_ft",
        evidence={
            "controller_source": rel(core_path),
            "runtime_source": rel(runtime_path),
            "controller_forbidden_hits": core_hits,
            "runtime_forbidden_hits": runtime_hits,
            "source_switching_policy": runtime.RuntimeConfig().source_switching_policy,
        },
        blocker="controller/runtime source contains private Gazebo, bridge, or live-motion dependency",
    )


def no_contact_static_check() -> dict[str, Any]:
    rows = [
        {"t_s": 0.0, "tcp_z_m": step5b_mvp.CONTACT_SURFACE_Z_M + 0.05},
        {"t_s": 0.1, "tcp_z_m": step5b_mvp.CONTACT_SURFACE_Z_M + 0.05},
    ]
    trace = contract.simulated_ft_trace_from_rows(
        rows,
        contact_surface_z_m=step5b_mvp.CONTACT_SURFACE_Z_M,
        nominal_contact_load_n=0.0,
    )
    contact_states = sorted({row.get("contact_state") for row in trace["rows"]})
    diagnostic_flags = sorted({flag for row in trace["rows"] for flag in row.get("diagnostic_flags", [])})
    passed = bool(
        trace["max_force_norm_n"] == 0.0
        and trace["max_normal_load_n"] == 0.0
        and contact_states == ["no_contact"]
        and "no_contact_static_baseline" in diagnostic_flags
        and not trace["schema_issues"]
    )
    return _check_result(
        passed=passed,
        claim_tier="simulated_ft",
        evidence={
            "trace_schema": trace["schema"],
            "sample_count": trace["sample_count"],
            "max_force_norm_n": trace["max_force_norm_n"],
            "max_normal_load_n": trace["max_normal_load_n"],
            "contact_state_values": contact_states,
            "diagnostic_flags": diagnostic_flags,
            "schema_issues": trace["schema_issues"],
        },
        blocker="no-contact static trace produced phantom load or invalid schema",
    )


def sign_frame_check() -> dict[str, Any]:
    artifact = step56.build_stage_artifact("step6b")
    trace = artifact["simulated_force_evidence"]
    first_rows = trace["rows"][:10]
    recomputed_load_errors = []
    for row in first_rows:
        force = [float(value) for value in row["force_n"]]
        reaction = [float(value) for value in row["reaction_normal"]]
        recomputed = max(0.0, sum(force[index] * reaction[index] for index in range(3)))
        recomputed_load_errors.append(abs(recomputed - float(row["normal_load_n"])))
    passed = bool(
        trace["force_source"] == contract.SOURCE_SIMULATED_FT
        and trace["reaction_normal"] == [0.0, 0.0, 1.0]
        and trace["approach_normal"] == [0.0, 0.0, -1.0]
        and trace["max_normal_load_n"] > 0.0
        and max(recomputed_load_errors, default=0.0) <= 1e-9
        and artifact["acceptance"]["canonical_wrench_schema_ok"]
    )
    return _check_result(
        passed=passed,
        claim_tier="simulated_ft",
        evidence={
            "stage_id": "step6b",
            "force_source": trace["force_source"],
            "reaction_normal": trace["reaction_normal"],
            "approach_normal": trace["approach_normal"],
            "normal_load_definition": trace["normal_load_definition"],
            "max_normal_load_n": trace["max_normal_load_n"],
            "max_recomputed_normal_load_error_n": max(recomputed_load_errors, default=0.0),
            "canonical_wrench_schema_ok": artifact["acceptance"]["canonical_wrench_schema_ok"],
        },
        blocker="reaction/approach normal semantics or normal-load sign check failed",
    )


def staleness_dropout_check() -> dict[str, Any]:
    fresh = contract.simulated_ft_sample(
        stamp_s=10.0,
        sequence=1,
        force_n=(0.0, 0.0, 5.0),
    )
    stale_status = contract.controller_status_from_canonical(fresh, now_s=10.5)
    stale = fresh.with_freshness(now_s=10.5)
    step5b_sample = contract.step5b_sample_from_canonical_wrench(
        stale,
        tcp_pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        robot_stage=25.0,
        dt_s=0.002,
    )
    passed = bool(
        stale_status["status"] == "hold"
        and stale_status["reason"] == "stale"
        and "stale_wrench" in stale_status["diagnostic_flags"]
        and step5b_sample.sensor_ok == 0.0
    )
    return _check_result(
        passed=passed,
        claim_tier="simulated_ft",
        evidence={
            "stale_status": stale_status,
            "step5b_sensor_ok": step5b_sample.sensor_ok,
        },
        blocker="stale canonical wrench did not force hold/sensor invalid state",
    )


def replay_parity_check(csv_path: Path = RETAINED_KUNWEI_CSV) -> dict[str, Any]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    canonical = contract.kunwei_csv_row_to_canonical(row)
    step5b_sample = contract.step5b_sample_from_canonical_wrench(
        canonical,
        tcp_pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        robot_stage=25.0,
        dt_s=0.002,
    )
    passed = bool(
        canonical.source == contract.SOURCE_REAL_KUNWEI_READ_ONLY
        and canonical.frame_id == "base"
        and step5b_sample.sensor_ok == 1.0
        and step5b_sample.tcp_wrench[2] == float(row["fz_n_zeroed"])
    )
    return _check_result(
        passed=passed,
        claim_tier="visual_only",
        evidence={
            "evidence_role": "supporting retained-log replay parity only; not a checkpoint/report claim tier upgrade",
            "retained_csv": rel(csv_path),
            "stamp_s": canonical.stamp_s,
            "canonical_source": canonical.source,
            "canonical_frame_id": canonical.frame_id,
            "status": canonical.status,
            "baseline_policy": canonical.baseline_policy,
            "log_evidence": csv_path.is_file(),
            "step5b_sensor_ok": step5b_sample.sensor_ok,
            "fz_zeroed_n": step5b_sample.tcp_wrench[2],
        },
        blocker="retained Kunwei replay could not drive the same canonical consumer path",
    )


def runtime_dry_run_check() -> dict[str, Any]:
    config = runtime.build_runtime_config({"max_samples": "4", "publish_hz": "20.0"})
    payload = runtime.build_dry_run_payload(config)
    first = payload["wrench_trace"]["rows"][0]
    fields_present = {
        "stamp": "stamp_s" in first["header"],
        "frame_id": bool(first["header"].get("frame_id")),
        "source": bool(first.get("source")),
        "status": bool(first.get("status")),
        "baseline": bool(first.get("baseline_policy")),
        "log_evidence": bool(payload["wrench_trace"]["rows"]),
    }
    passed = bool(
        payload["schema"] == "ur10e_canonical_simulated_ft_runtime_dry_run_v1"
        and payload["mode"] == "offline_no_motion"
        and payload["wrench_trace"]["force_source"] == contract.SOURCE_SIMULATED_FT
        and payload["wrench_trace"]["sample_count"] == 4
        and not payload["wrench_trace"]["schema_issues"]
        and all(fields_present.values())
        and payload["live_robot_command_authorized"] is False
        and payload["bridge_start_authorized"] is False
    )
    return _check_result(
        passed=passed,
        claim_tier="simulated_ft",
        evidence={
            "schema": payload["schema"],
            "mode": payload["mode"],
            "topics": payload["topics"],
            "force_source": payload["wrench_trace"]["force_source"],
            "sample_count": payload["wrench_trace"]["sample_count"],
            "schema_issues": payload["wrench_trace"]["schema_issues"],
            "evidence_fields_present": fields_present,
            "live_authorization": {
                "live_robot_command_authorized": payload["live_robot_command_authorized"],
                "bridge_start_authorized": payload["bridge_start_authorized"],
                "payload_tcp_safety_writes_authorized": payload["payload_tcp_safety_writes_authorized"],
            },
        },
        blocker="canonical simulated FT runtime dry-run did not expose required evidence fields/topics",
    )


def per_stage_pack_check(output_dir: Path, *, generated_at: str) -> dict[str, Any]:
    manifest_path = sim_ft_pack.write_pack(output_dir / "per_stage_simulated_ft_pack", generated_at=generated_at)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    passed = bool(
        manifest.get("schema") == sim_ft_pack.PACK_SCHEMA
        and manifest.get("all_contact_stages_valid") is True
        and manifest.get("valid_stage_count") == len(sim_ft_pack.CONTACT_STAGE_IDS)
        and manifest.get("claim_tier") == "simulated_ft"
    )
    return _check_result(
        passed=passed,
        claim_tier="simulated_ft" if passed else "visual_only",
        evidence={
            "manifest_path": rel(manifest_path),
            "schema": manifest.get("schema"),
            "claim_tier": manifest.get("claim_tier"),
            "contact_stage_ids": manifest.get("contact_stage_ids"),
            "valid_stage_count": manifest.get("valid_stage_count"),
            "all_contact_stages_valid": manifest.get("all_contact_stages_valid"),
        },
        blocker="per-stage canonical simulated FT pack is incomplete or invalid",
    )


def step5d_semantic_gate_check(output_dir: Path, *, max_rows: int | None = None) -> dict[str, Any]:
    gate_dir = output_dir / "step5d_contact_semantic_gate"
    payload = semantic_gate.run_gate(
        semantic_gate.DEFAULT_REPLAY_CSVS,
        output_dir=gate_dir,
        max_rows=max_rows,
    )
    summary_path = gate_dir / "ur_contact_semantic_gate_summary.json"
    passed = bool(payload["overall_pass"])
    return _check_result(
        passed=passed,
        claim_tier="simulated_ft",
        evidence={
            "summary_path": rel(summary_path),
            "overall_pass": payload["overall_pass"],
            "static_scan_pass": payload["static_scan"]["pass"],
            "replay_csv_count": len(payload["replay_summaries"]),
            "failure_contrast_passes": [
                {
                    "csv": rel(summary["csv"]),
                    "pass": summary["pass"],
                    "failure_contrast_required": summary["failure_contrast_required"],
                    "failure_contrast_pass": summary["failure_contrast_pass"],
                    "max_logged_step4e_orientation_error_rad": summary["max_logged_step4e_orientation_error_rad"],
                    "max_outer_xdot_norm": summary["max_outer_xdot_norm"],
                    "min_R_d_z_dot_R_cur_z": summary["min_R_d_z_dot_R_cur_z"],
                    "records_csv": rel(summary.get("records_csv")),
                }
                for summary in payload["replay_summaries"]
            ],
            "safety_boundary": payload["safety_boundary"],
        },
        blocker="Step5d offline force/frame semantic gate failed",
    )


def build_audit(output_dir: Path, *, generated_at: str | None = None, semantic_max_rows: int | None = None) -> dict[str, Any]:
    generated = generated_at or datetime.now().isoformat(timespec="seconds")
    checks = {
        "contract_schema": contract_schema_check(),
        "source_isolation": source_isolation_check(),
        "runtime_dry_run": runtime_dry_run_check(),
        "no_contact_static": no_contact_static_check(),
        "sign_frame": sign_frame_check(),
        "staleness_dropout": staleness_dropout_check(),
        "replay_parity": replay_parity_check(),
        "per_stage_pack": per_stage_pack_check(output_dir, generated_at=generated),
        "step5d_semantic_gate": step5d_semantic_gate_check(output_dir, max_rows=semantic_max_rows),
    }
    blockers = [name for name, check in checks.items() if not check["pass"]]
    all_pass = not blockers
    return {
        "schema": SCHEMA,
        "generated_at": generated,
        "goal_lineage": GOAL_LINEAGE,
        "mode": "offline_no_live_p1_simulated_ft_hard_floor_audit",
        "claim_tier": "simulated_ft" if all_pass else "visual_only",
        "p1_simulated_ft_hard_floor_ready": all_pass,
        "p1_blockers": blockers,
        "checks": checks,
        "claim_boundary": {
            "visual_only": "screenshots, EOAT visibility, TCP markers, model pose, observer views, or downgraded supporting evidence only",
            "virtual/software force-loop": "software force source such as gazebo_joint_state_fk_virtual_surface_model only",
            "simulated_ft": "canonical wrench/status/contact logs only when stamp, frame_id, source, status, baseline, and log evidence exist",
            "physical Gazebo collision/contact physics": "requires EOAT collision evidence, contact pair/log evidence, and wrench/contact correlation; not claimed by this P1 audit",
            "real bench/live contact": "not authorized in this goal",
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
        "forbidden_claim": "physical Gazebo collision/contact physics; real bench/live contact; live bridge/TP/URScript/motion",
    }


def write_audit(output_dir: Path, *, generated_at: str | None = None, semantic_max_rows: int | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = build_audit(output_dir, generated_at=generated_at, semantic_max_rows=semantic_max_rows)
    path = output_dir / "p1_simulated_ft_hard_floor_audit.json"
    payload["artifact_path"] = str(path)
    write_json(path, payload)
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--semantic-max-rows", type=int, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = write_audit(args.output_dir, generated_at=args.generated_at, semantic_max_rows=args.semantic_max_rows)
    payload = json.loads(path.read_text(encoding="utf-8"))
    print(path)
    return 0 if payload["p1_simulated_ft_hard_floor_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
