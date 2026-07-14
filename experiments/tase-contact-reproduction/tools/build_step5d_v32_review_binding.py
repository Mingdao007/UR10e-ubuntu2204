#!/usr/bin/env python3
"""Freeze the exact Step5d v32 package, core, transport, and operator inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PROFILE = "step5d_strict_rnn_ablation_v32"
PACKAGE_BASE = ROOT / "programs/step5/step5d" / PROFILE
CONTROL_CORE_PATHS = (
    "tools/contact_semantics.py",
    "tools/step5c_calibrated_kinematics_audit.py",
    "tools/step5c_strict_rnn.py",
    "tools/step5d_control_contract.py",
    "tools/step5d_paper_outer_loop.py",
)
TRANSPORT_OPERATOR_PATHS = (
    "scripts/bridge-line-operator.sh",
    "scripts/step5d-liveprep-operator.sh",
    "scripts/step5d-strict-rnn-contact-v32.sh",
    "scripts/step5d-workflow.sh",
    "tools/analyze_step5d_bridge_run.py",
    "tools/build_step5d_liveprep.py",
    "tools/build_step5d_v32_evidence.py",
    "tools/build_step5d_v32_review_binding.py",
    "tools/kunwei_rtde_bridge.py",
    "tools/preflight_readonly.py",
    "tools/step5d_runtime_interface.py",
    "tools/upload_ur_tp_package.py",
    "tools/verify_step5d_contact_v32.py",
    "tools/verify_step5d_current_binding.py",
    "tests/test_bridge_operator_startup_policy.py",
    "tests/test_preflight_readonly_dag.py",
    "tests/test_step5d_bridge_run_analysis.py",
    "tests/test_step5d_current_binding_gate.py",
    "tests/test_step5d_v30_profile.py",
    "tests/test_step5d_v32_stage_aware.py",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def compute_payloads(root: Path = ROOT) -> tuple[dict[str, str], dict[str, Any]]:
    table = json.loads((root / "config/step5_stage_table.json").read_text(encoding="utf-8"))
    stage = next(row for row in table["stages"] if row["id"] == PROFILE)
    delivery = stage["package_delivery"]
    readback = root / delivery["controller_readback_manifest"]
    package_base = root / "programs/step5/step5d" / PROFILE
    package = {suffix: sha(package_base.with_suffix(suffix)) for suffix in (".script", ".txt", ".urp")}
    control_core = {path: sha(root / path) for path in CONTROL_CORE_PATHS}
    transport_operator = {path: sha(root / path) for path in TRANSPORT_OPERATOR_PATHS}
    timing = stage["formal_timing"]
    inherited_timing = {
        "raw": {
            "path": timing["inherited_raw"],
            "sha256": sha(root / timing["inherited_raw"]),
        },
        "summary": {
            "path": timing["inherited_summary"],
            "sha256": sha(root / timing["inherited_summary"]),
        },
        "claim": "v31 CuPy512 control-core timing inherited; not exact full-source v32 timing",
    }
    transport_smoke = {
        "path": timing["transport_smoke"],
        "sha256": sha(root / timing["transport_smoke"]),
    }
    numeric = stage["numeric_sanity"]
    numeric_sanity = {
        "path": numeric["artifact"],
        "sha256": sha(root / numeric["artifact"]),
    }
    effective_operator_config = {
        "profile": PROFILE,
        "runtime_profile": stage["runtime_profile"],
        "runtime_scheduler": stage["runtime_scheduler"],
        "wire_protocol": stage["wire_protocol"],
        "guard": stage["guard"],
        "bridge_runtime": stage["bridge_runtime"],
        "operator_lifecycle": stage["operator_lifecycle"],
        "contact_policy": stage["contact_policy"],
        "acceptance": stage["acceptance"],
        "package_delivery": stage["package_delivery"],
    }
    evidence = {
        "schema_version": "step5d_v32_review_binding_evidence_v1",
        "profile": PROFILE,
        "package_triplet_sha256": package,
        "controller_readback": {
            "path": str(readback.relative_to(root)),
            "sha256": sha(readback),
        },
        "control_core_source_files_sha256": control_core,
        "transport_operator_source_files_sha256": transport_operator,
        "inherited_control_core_timing": inherited_timing,
        "transport_timing_smoke": transport_smoke,
        "numeric_sanity": numeric_sanity,
        "effective_operator_config": effective_operator_config,
        "claim_boundary": {
            "offline_tooling_accepted_only": True,
            "live_motion_authorized": False,
            "contact_accepted": False,
            "review_does_not_start_bridge_or_motion": True,
            "full_formal_timing_rerun": False,
        },
    }
    binding = {
        "package_triplet": canonical_sha(package),
        "controller_readback": evidence["controller_readback"]["sha256"],
        "timing_raw": inherited_timing["raw"]["sha256"],
        "timing_summary": canonical_sha({
            "inherited_summary": inherited_timing["summary"]["sha256"],
            "transport_timing_smoke": transport_smoke["sha256"],
            "numeric_sanity": numeric_sanity["sha256"],
        }),
        "source_fingerprint": canonical_sha({
            "control_core": control_core,
            "transport_operator": transport_operator,
        }),
        "effective_operator_config": canonical_sha(effective_operator_config),
    }
    return binding, evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    binding, evidence = compute_payloads(args.root.resolve())
    args.binding.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.binding.write_text(json.dumps(binding, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    args.evidence.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"binding": str(args.binding), "evidence": str(args.evidence)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
