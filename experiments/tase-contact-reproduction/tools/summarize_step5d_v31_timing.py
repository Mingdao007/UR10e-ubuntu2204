#!/usr/bin/env python3
"""Summarize exact-profile Step5d v31 formal timing without live I/O."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from build_step5d_v31_timing_bundle import v31_harness


ROOT = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_bindings_match(raw: dict) -> bool:
    paths = {
        "aggregator_sha256": ROOT / "tools/summarize_step5d_v31_timing.py",
        "bridge_sha256": ROOT / "tools/kunwei_rtde_bridge.py",
        "bundler_sha256": ROOT / "tools/build_step5d_v31_timing_bundle.py",
        "contact_semantics_sha256": ROOT / "tools/contact_semantics.py",
        "control_contract_sha256": ROOT / "tools/step5d_control_contract.py",
        "kinematics_sha256": ROOT / "tools/step5c_calibrated_kinematics_audit.py",
        "outer_loop_sha256": ROOT / "tools/step5d_paper_outer_loop.py",
        "readiness_builder_sha256": ROOT / "tools/build_step5d_v31_review_binding.py",
        "runtime_interface_sha256": ROOT / "tools/step5d_runtime_interface.py",
        "solver_sha256": ROOT / "tools/step5c_strict_rnn.py",
    }
    bound = raw.get("source_binding") or {}
    if any(bound.get(key) != sha(path) for key, path in paths.items()):
        return False
    return bound.get("harness_sha256") == hashlib.sha256(v31_harness().encode()).hexdigest()


def artifact_bindings_match(raw: dict) -> bool:
    for item in (raw.get("artifact_binding") or {}).values():
        try:
            path = Path(item["path"])
            if not path.is_file():
                return False
            if item.get("binding") == "v31_timing_contract_projection_v1":
                table = json.loads(path.read_text())
                stage = next(row for row in table["stages"] if row["id"] == "step5d_strict_rnn_ablation_v31")
                projection = {key: stage[key] for key in (
                    "runtime_profile", "runtime_scheduler", "guard", "bridge_runtime", "contact_policy"
                )}
                actual = hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            else:
                actual = sha(path)
            if actual != item["sha256"]:
                return False
        except (KeyError, TypeError):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.raw.read_text())
    profile = raw.get("v31_profile_contract") or {}
    full = raw.get("full_tick") or {}
    safe = raw.get("safe_hold") or {}
    solver = raw.get("solver") or {}
    stale = raw.get("controller_stale_hold_fault_evidence") or {}
    checks = {
        "source_bindings_match": source_bindings_match(raw),
        "artifact_bindings_match": artifact_bindings_match(raw),
        "exact_profile": profile.get("qdot_cap_rad_s") == 0.5
        and profile.get("normal_motion_policy") == "frame_contract_only"
        and profile.get("inner_iterations") == 512,
        "paced_500hz": raw.get("paced_500hz") is True,
        "full_tick_30000": int(full.get("samples", 0)) == 30000,
        "safe_hold_30000": int(safe.get("samples", 0)) == 30000,
        "elapsed_full_60s": 59.9 <= float(raw.get("elapsed_full_tick_wall_s", 0.0)) <= 60.2,
        "elapsed_safe_60s": 59.9 <= float(raw.get("elapsed_safe_hold_wall_s", 0.0)) <= 60.2,
        "finite": all(int(row.get("nonfinite_count", 1)) == 0 for row in (solver, full, safe)),
        "compute_deadline_policy": int(solver.get("compute_deadline_miss_count", 1)) == 0
        and int(full.get("compute_deadline_miss_count", 1)) / max(1, int(full.get("samples", 0))) <= 0.01
        and int(safe.get("compute_deadline_miss_count", 1)) / max(1, int(safe.get("samples", 0))) <= 0.01,
        "schedule_deadline_policy": int(raw.get("full_tick_schedule_deadline_miss_count", 1)) / max(1, int(full.get("samples", 0))) <= 0.01
        and int(raw.get("safe_hold_schedule_deadline_miss_count", 1)) / max(1, int(safe.get("samples", 0))) <= 0.01,
        "stale_hold_contract": stale.get("pass") is True
        and stale.get("late_candidate_policy") == "publish_when_guard_approved"
        and stale.get("heartbeat_stale_stop_s") == 1.0,
    }
    summary = {
        "schema_version": "step5d_v31_formal_timing_summary_v1",
        "raw_path": str(args.raw),
        "raw_sha256": sha(args.raw),
        "profile": profile,
        "solver": solver,
        "full_tick": full,
        "safe_hold": safe,
        "full_tick_schedule_deadline_miss_count": raw.get("full_tick_schedule_deadline_miss_count"),
        "safe_hold_schedule_deadline_miss_count": raw.get("safe_hold_schedule_deadline_miss_count"),
        "stale_hold": stale,
        "checks": checks,
        "overall_pass": all(checks.values()),
        "safety_boundary": raw.get("safety_boundary"),
    }
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["overall_pass"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
