#!/usr/bin/env python3
"""Build the recomputable v35 source/package/read-back/timing freeze binding."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROGRAM = "step5d_strict_rnn_ablation_v35"
SOURCE_FILES = (
    "STEP5_FLOW.md",
    "tools/build_step5d_v35_freeze_binding.py",
    "tools/kunwei_rtde_bridge.py",
    "tools/step5d_runtime_interface.py",
    "tools/analyze_step5d_bridge_run.py",
    "tools/build_step5d_liveprep.py",
    "tools/verify_step5d_contact_v35.py",
    "tools/run_step5d_v34_live_path_timing.py",
    "tools/verify_step5d_current_binding.py",
    "tools/step5d_paper_outer_loop.py",
    "tools/step5d_control_contract.py",
    "tools/step5c_strict_rnn.py",
    "scripts/step5d-strict-rnn-contact-v35.sh",
    "scripts/bridge-line-operator.sh",
    "tests/test_step5d_v35_quota_safe_scheduler.py",
)
ARTIFACTS = (
    "config/step5d_v35_offline_validation.json",
    "config/step5d_v35_live_path_timing.json",
    "runs/derived/bridge_step5d_strict_rnn_ablation_v34_20260715_014943_20260715_014943/step5d-analysis/step5d_bridge_analysis.json",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_payload(root: Path = ROOT) -> dict[str, Any]:
    table = json.loads((root / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
    row = next(item for item in table["stages"] if item.get("id") == PROGRAM)
    package = row.get("package_delivery")
    if not isinstance(package, dict):
        raise RuntimeError("missing v35 package_delivery ledger")
    manifest_rel = package.get("controller_readback_manifest")
    if not isinstance(manifest_rel, str) or not manifest_rel:
        raise RuntimeError("missing v35 fresh read-back manifest")
    stage_contract = {
        key: row[key]
        for key in (
            "duration_s",
            "phase_law",
            "runtime_profile",
            "scheduler_lifecycle",
            "stage25_outer_profile",
            "feedback_policy",
            "wire_protocol",
            "guard",
            "bridge_runtime",
            "acceptance",
        )
    }
    package_binding = {
        "triplet_sha256": {
            ext: sha256(root / "programs" / "step5" / "step5d" / f"{PROGRAM}{ext}")
            for ext in (".script", ".txt", ".urp")
        },
        "controller_target": package.get("controller_target"),
        "controller_readback_manifest": manifest_rel,
        "controller_readback_manifest_sha256": sha256(root / manifest_rel),
    }
    payload: dict[str, Any] = {
        "schema": "step5d_v35_freeze_binding_v1",
        "program": PROGRAM,
        "source_sha256": {name: sha256(root / name) for name in SOURCE_FILES},
        "stage_contract": stage_contract,
        "stage_contract_sha256": canonical_sha256(stage_contract),
        "package_binding": package_binding,
        "artifact_sha256": {name: sha256(root / name) for name in ARTIFACTS},
        "claim_boundary": "package/read-back/offline freeze only; live authorization remains separate",
    }
    payload["composite_fingerprint"] = canonical_sha256(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_payload(args.root.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
