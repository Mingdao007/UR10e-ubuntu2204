#!/usr/bin/env python3
"""Build v34 offline numeric, acceleration, scheduler, and package evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import build_step5d_liveprep as builder
import validate_step5d_v33 as v33
import verify_step5d_contact_v34 as verifier
from step5d_runtime_interface import STEP5D_ABLATION_V34_STAGE_ID, build_stage_env


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_V33C20 = (
    ROOT
    / "runs"
    / "bridge_step5d_strict_rnn_ablation_v33c20_20260715_002234"
    / "bridge_rtde_500hz.csv"
)


def acceleration_binding(root: Path) -> dict[str, Any]:
    spec = builder.ABLATION_SPECS[STEP5D_ABLATION_V34_STAGE_ID]
    env = build_stage_env(STEP5D_ABLATION_V34_STAGE_ID, root)
    script = (
        root / "programs" / "step5" / "step5d" / f"{STEP5D_ABLATION_V34_STAGE_ID}.script"
    ).read_text(encoding="utf-8")
    host = builder.host_qdot_slew_rad_s2(spec)
    tp = builder.joint_accel_rad_s2(spec)
    dt_s = 0.002
    worst_tick_delta = max(abs(value) for value in (host * dt_s, -host * dt_s))
    passed = (
        host == 0.1
        and tp == 0.1
        and float(env["STEP5D_QDOT_SLEW_RAD_S2"]) == 0.1
        and "local joint_accel_rad_s2 = 0.100" in script
        and worst_tick_delta <= 0.1 * dt_s + 1e-15
    )
    return {
        "host_qdot_command_slew_rad_s2": host,
        "tp_speedj_acceleration_rad_s2": tp,
        "control_dt_s": dt_s,
        "max_per_tick_qdot_delta_rad_s": worst_tick_delta,
        "pass": passed,
    }


def scheduler_static_contract(root: Path) -> dict[str, Any]:
    table = json.loads((root / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
    row = next(item for item in table["stages"] if item.get("id") == STEP5D_ABLATION_V34_STAGE_ID)
    lifecycle = row["scheduler_lifecycle"]
    bridge_source = (root / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")
    launcher = (root / "scripts" / "bridge-line-operator.sh").read_text(encoding="utf-8")
    passed = (
        lifecycle["launch_policy"] == "SCHED_OTHER/0"
        and lifecycle["control_thread_policy"] == "SCHED_FIFO/20"
        and lifecycle["helper_thread_policy"] == "SCHED_OTHER/0"
        and lifecycle["kernel_sched_rt_runtime_us_write_allowed"] is False
        and "promote_v34_control_thread_scheduler(args.bridge_profile)" in bridge_source
        and '|| [[ "${BRIDGE_PROFILE}" == "step5d_strict_rnn_ablation_v34" ]]' in launcher
        and '|| "${BRIDGE_PROFILE}" == "step5d_strict_rnn_ablation_v34" \\' not in launcher.split("requires_step5d_realtime_launcher()", 1)[1].split("}", 1)[0]
    )
    return {"contract": lifecycle, "pass": passed}


def residual_logging_contract(root: Path) -> dict[str, Any]:
    source = (root / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")
    analyzer = (root / "tools" / "analyze_step5d_bridge_run.py").read_text(encoding="utf-8")
    checks = {
        "raw_rnn_field": '"_step5d_raw_rnn_residual_norm"' in source,
        "post_slew_field": '"_step5d_post_slew_residual_norm"' in source,
        "legacy_field_retained": '"_step5d_constraint_residual_norm"' in source,
        "analyzer_uses_raw": 'finite_values(accepted_rnn_rows, "_step5d_raw_rnn_residual_norm")' in analyzer,
        "analyzer_uses_post_slew": 'finite_values(accepted_rnn_rows, "_step5d_post_slew_residual_norm")' in analyzer,
        "legacy_residual_remains_raw": 'values["_step5d_constraint_residual_norm"] = float(\n                    candidate_v30.residual_norm' not in source,
    }
    return {"checks": checks, "pass": all(checks.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step5b-csv", type=Path, default=v33.DEFAULT_STEP5B)
    parser.add_argument("--rnn-csv", type=Path, default=DEFAULT_V33C20)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    alignment = v33.rnn_oracle_alignment(args.rnn_csv)
    alignment["pass"] = (
        alignment.get("rows", 0) > 1000
        and alignment.get("delta_norm_max") is not None
        and float(alignment["delta_norm_max"]) <= 1e-6
    )
    alignment["v34_acceptance_limit_rad_s"] = 1e-6
    checks = {
        "mathematical_equivalence": v33.formula_equivalence(),
        "rtde_backlog": v33.backlog_simulation(),
        "step5b_continuous_replay": v33.replay_step5b(args.step5b_csv),
        "rnn_oracle_alignment": alignment,
        "matched_acceleration": acceleration_binding(ROOT),
        "scheduler_static_contract": scheduler_static_contract(ROOT),
        "residual_logging_contract": residual_logging_contract(ROOT),
        "tp_runtime_contract": verifier.verify(ROOT),
    }
    payload = {
        "schema": "step5d_v34_offline_validation_v1",
        "profile": STEP5D_ABLATION_V34_STAGE_ID,
        "checks": checks,
        "overall_pass": all(check["pass"] is True if "pass" in check else check["ok"] is True for check in checks.values()),
        "claim_boundary": "offline numeric/static/replay evidence only; no live authorization",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
