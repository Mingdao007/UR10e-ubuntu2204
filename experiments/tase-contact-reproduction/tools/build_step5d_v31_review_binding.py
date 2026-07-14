#!/usr/bin/env python3
"""Freeze the exact Step5d v31 pre-live Review v3 composite inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "step5d_strict_rnn_ablation_v31"
PACKAGE_BASE = ROOT / "programs/step5/step5d" / PROFILE
TIMING_RAW = ROOT / "config/step5d_v31_formal_timing_raw.json"
TIMING_SUMMARY = ROOT / "config/step5d_v31_formal_timing_summary.json"
SOURCE_PATHS = (
    "config/step5d_review_policy_v31.json",
    "config/tase_protocol_table.json",
    "scripts/bridge-line-operator.sh",
    "scripts/step5d-liveprep-operator.sh",
    "tools/build_step5d_liveprep.py",
    "tools/build_step5d_v31_review_binding.py",
    "tools/build_step5d_v31_timing_bundle.py",
    "tools/contact_semantics.py",
    "tools/kunwei_rtde_bridge.py",
    "tools/step5c_calibrated_kinematics_audit.py",
    "tools/step5c_strict_rnn.py",
    "tools/step5d_control_contract.py",
    "tools/step5d_paper_outer_loop.py",
    "tools/step5d_review_v3.py",
    "tools/step5d_runtime_interface.py",
    "tools/summarize_step5d_v31_timing.py",
    "tools/tase_protocol_table.py",
    "tools/upload_ur_tp_package.py",
    "tools/verify_current_stage_readback.py",
    "tools/verify_step5d_current_binding.py",
    "tools/verify_step5d_contact_v31.py",
    "scripts/step5d-strict-rnn-contact-v31.sh",
    "tests/test_step5d_v31_permissive_contact.py",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def compute_payloads() -> tuple[dict[str, str], dict[str, Any]]:
    """Return the canonical binding and its fully expanded evidence inventory."""
    stage_table = json.loads((ROOT / "config/step5_stage_table.json").read_text())
    stage = next(row for row in stage_table["stages"] if row["id"] == PROFILE)
    readback = ROOT / stage["package_delivery"]["controller_readback_manifest"]
    package = {suffix: sha(PACKAGE_BASE.with_suffix(suffix)) for suffix in (".script", ".txt", ".urp")}
    source_files = {path: sha(ROOT / path) for path in SOURCE_PATHS}
    resolved_operator_config = {
        "bridge_profile": PROFILE,
        "stage25_control_mode": "speedj_rnn_live",
        "backend": "cupy",
        "inner_iterations": 512,
        "epsilon": 0.01,
        "sigr_exponent_r": 0.8,
        "qdot_cap_rad_s": 0.5,
        "qdot_slew_rad_s2": 0.05,
        "sensor_stale_s": 2.0,
        "heartbeat_stale_stop_s": 1.0,
        "baseline_s": 1.0,
        "rezero_s": 1.0,
        "normal_follow_mode": "filtered_live",
        "normal_min_force_n": 2.0,
        "target_force_n": 12.0,
        "raw_normal_guard_n": 60.0,
        "force_norm_guard_n": 100.0,
        "torque_norm_guard_nm": 3.0,
        "runtime_limit_s": 75.0,
        "normal_motion_policy": "frame_contract_only",
        "layout_code": 524.0,
    }
    effective_operator_config = {
        "profile": PROFILE,
        "guard_policy": stage["contact_policy"]["guard_policy"],
        "guard": stage["guard"],
        "runtime_profile": stage["runtime_profile"],
        "bridge_runtime": stage["bridge_runtime"],
        "operator_lifecycle": stage["operator_lifecycle"],
        "contact_policy": stage["contact_policy"],
        "acceptance": stage["acceptance"],
        "package_delivery": stage["package_delivery"],
        "resolved_operator_config": resolved_operator_config,
    }
    evidence = {
        "schema_version": "step5d_v31_review_binding_evidence_v1",
        "profile": PROFILE,
        "package_triplet_sha256": package,
        "controller_readback": {"path": str(readback.relative_to(ROOT)), "sha256": sha(readback)},
        "timing_raw": {"path": str(TIMING_RAW.relative_to(ROOT)), "sha256": sha(TIMING_RAW)},
        "timing_summary": {"path": str(TIMING_SUMMARY.relative_to(ROOT)), "sha256": sha(TIMING_SUMMARY)},
        "source_files_sha256": source_files,
        "effective_operator_config": effective_operator_config,
        "resolved_operator_config": resolved_operator_config,
        "claim_boundary": {
            "live_motion_authorized": False,
            "contact_accepted": False,
            "review_does_not_start_bridge_or_motion": True,
        },
    }
    binding = {
        "package_triplet": canonical_sha(package),
        "controller_readback": evidence["controller_readback"]["sha256"],
        "timing_raw": evidence["timing_raw"]["sha256"],
        "timing_summary": evidence["timing_summary"]["sha256"],
        "source_fingerprint": canonical_sha(source_files),
        "effective_operator_config": canonical_sha(effective_operator_config),
    }
    return binding, evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    binding, evidence = compute_payloads()
    args.binding.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.binding.write_text(json.dumps(binding, indent=2, sort_keys=True) + "\n")
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"binding": str(args.binding), "evidence": str(args.evidence)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
