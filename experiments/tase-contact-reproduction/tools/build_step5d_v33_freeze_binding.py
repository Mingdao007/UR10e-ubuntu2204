#!/usr/bin/env python3
"""Build the recomputable v33 source/package/read-back/timing freeze binding."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROFILES = (
    "step5d_strict_rnn_ablation_v33c20",
    "step5d_strict_rnn_ablation_v33",
)
SOURCE_FILES = (
    "tools/build_step5d_v33_freeze_binding.py",
    "tools/kunwei_rtde_bridge.py",
    "tools/step5d_runtime_interface.py",
    "tools/analyze_step5d_bridge_run.py",
    "tools/build_step5d_liveprep.py",
    "tools/verify_step5d_contact_v33.py",
    "tools/validate_step5d_v33.py",
    "tools/run_step5d_v33_live_path_timing.py",
    "tools/verify_step5d_current_binding.py",
    "tools/step5d_paper_outer_loop.py",
    "tools/step5d_control_contract.py",
    "tools/step5c_strict_rnn.py",
    "scripts/step5d-strict-rnn-contact-v33.sh",
    "scripts/bridge-line-operator.sh",
)
ARTIFACTS = (
    "config/step5d_v33_offline_validation.json",
    "config/step5d_v33_live_path_timing.json",
    "config/step5d_v32_failure_evidence.json",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_payload(root: Path = ROOT) -> dict[str, Any]:
    current = json.loads((root / "config" / "current_stage.json").read_text(encoding="utf-8"))
    table = json.loads((root / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
    rows = {row.get("id"): row for row in table.get("stages", [])}
    stage_contract = {
        profile: {
            key: rows[profile][key]
            for key in (
                "duration_s",
                "phase_law",
                "runtime_profile",
                "stage25_outer_profile",
                "feedback_policy",
                "wire_protocol",
                "guard",
                "bridge_runtime",
                "acceptance",
            )
        }
        for profile in PROFILES
    }
    package_binding: dict[str, Any] = {}
    for profile, candidate_key in zip(PROFILES, ("v33c20_candidate", "v33_candidate")):
        candidate = current.get(candidate_key)
        if not isinstance(candidate, dict) or not isinstance(candidate.get("package"), dict):
            raise RuntimeError(f"missing {candidate_key}.package ledger")
        package = candidate["package"]
        manifest_rel = package.get("controller_readback_manifest")
        if not isinstance(manifest_rel, str) or not manifest_rel:
            raise RuntimeError(f"missing fresh read-back manifest for {profile}")
        package_binding[profile] = {
            "triplet_sha256": {
                ext: sha256(root / "programs" / "step5" / "step5d" / f"{profile}{ext}")
                for ext in (".script", ".txt", ".urp")
            },
            "controller_target": package.get("controller_target"),
            "controller_readback_manifest": manifest_rel,
            "controller_readback_manifest_sha256": sha256(root / manifest_rel),
        }
    payload: dict[str, Any] = {
        "schema": "step5d_v33_freeze_binding_v1",
        "profiles": list(PROFILES),
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
