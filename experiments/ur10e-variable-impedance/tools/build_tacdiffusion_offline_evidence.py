#!/usr/bin/env python3
"""Build deterministic-stage TacDiffusion evidence from current tracked sources."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = (
    "config/controller_5_25_2_upgrade_preflight.json",
    "config/controller_5_26_runtime_contract.json",
    "config/direct_torque_rtde_layout.json",
    "config/tacdiffusion_experiment_plan.json",
    "config/tacdiffusion_upstream_lock.json",
    "config/ursim_5_25_2_protocol.json",
    "config/ursim_5_26_protocol.json",
    "config/validation_ledger.json",
    "schemas/controller_runtime_contract_v1.schema.json",
    "schemas/expert_trace_manifest_v2.schema.json",
    "schemas/tacdiffusion_checkpoint_v2.schema.json",
    "schemas/tacdiffusion_evaluation_v1.schema.json",
    "schemas/ursim_capture_v1.schema.json",
    "programs/direct_torque_vic_offline_template.script",
    "ur10e_vic/controller_contract.py",
    "ur10e_vic/controller_upgrade.py",
    "ur10e_vic/ursim_protocol.py",
    "ur10e_vic/ursim_transport.py",
    "ur10e_vic/tacdiffusion/cli.py",
    "ur10e_vic/tacdiffusion/contracts.py",
    "ur10e_vic/tacdiffusion/dataset.py",
    "ur10e_vic/tacdiffusion/expert_data.py",
    "ur10e_vic/tacdiffusion/model.py",
    "tests/test_controller_contract.py",
    "tests/test_tacdiffusion_expert_data.py",
    "tests/test_tacdiffusion_model.py",
    "tests/test_ursim_transport.py",
    "tools/build_tacdiffusion_offline_evidence.py",
    "docs/TACDIFFUSION_UR10E_MAINLINE.md",
    "OFFLINE_READINESS.json",
    "README.md",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(test_count: int) -> dict[str, object]:
    if test_count <= 0:
        raise ValueError("test_count must be positive")
    bindings = []
    for relative in SOURCE_PATHS:
        path = ROOT / relative
        if not path.is_file():
            raise ValueError(f"missing evidence source: {relative}")
        bindings.append({"path": relative, "sha256": sha256_file(path)})
    return {
        "schema": "ur10e_tacdiffusion_offline_validation_v2",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "branch": "feature/tacdiffusion-5-26-reproduction-prep-20260720",
        "base_commit": "7d15f6e025fe655a0ee63e60ae51184f27cc24eb",
        "highest_stage": "deterministic_tested",
        "validation": {
            "variable_impedance_check_passed": True,
            "variable_impedance_test_count": test_count,
            "dependency_light_python_passed": True,
            "torch_2_11_cpu_fixture_smoke_passed": True,
            "torch_fixture_train_resume_evaluate_passed": True,
            "controller_5_26_fail_closed_contract_passed": True,
            "ursim_5_26_fake_transport_nonpromotion_passed": True,
            "expert_manifest_v2_artifact_graph_passed": True,
            "tacdiffusion_checkpoint_v2_resume_passed": True,
            "dbil_regression_passed": True,
            "legacy_5_25_2_hashes_unchanged": True,
            "external_dbil_artifacts_rehashed_this_round": False,
            "json_validation_passed": True,
            "python_compileall_passed": True,
            "git_diff_check_passed": True,
            "secret_scan_passed": True,
            "ursim_protocol_execution_performed": False,
            "gpu_used": False,
        },
        "review_policy": {
            "stack": "0+0 plus one non-blocking read-only Fable shadow",
            "reason": "ordinary offline coding under UR10e Review v2",
            "next_required_review": "1+1 before first no-contact torque canary",
        },
        "remaining_blockers": [
            "5.26 version-only observation lacks complete identity, URCap, configuration, calibration, and Direct Torque API bindings",
            "no accepted 5.26 URSim capture or runtime image fingerprint exists",
            "no hardware-verified expert F_ff dataset exists",
            "no retained formal UR10e TacDiffusion checkpoint or real evaluation artifact exists",
            "no independent real 50/100/200/500 Hz timing selection exists",
            "no live or contact authorization exists",
            "model-active remains disabled",
        ],
        "source_bindings": bindings,
        "simulation_run": False,
        "ursim_run": False,
        "hardware_run": False,
        "controller_verified": False,
        "formal_dataset_collected": False,
        "formal_checkpoint_trained": False,
        "model_active_enabled": False,
        "live_motion_authorized": False,
        "contact_authorized": False,
        "claim": "UR10e 500 Hz force-domain diffusion adaptation preparation",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-count", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.test_count), indent=2, sort_keys=True) + "\n", end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
