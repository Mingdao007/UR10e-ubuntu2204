#!/usr/bin/env python3
"""Verify the offline cross-engine falsification artifact fail-closed."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping


TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import build_ur10e_cross_engine_falsification as builder  # noqa: E402


REPORT_SCHEMA = "ur10e_cross_engine_falsification_verifier_v1"
FALSE_CLAIMS = (
    "cross_engine_contact_equivalence",
    "cross_engine_timing_equivalence",
    "cross_engine_physics_equivalence",
    "domain_randomization_executed",
    "domain_randomization_robustness_pass",
    "p0_sim_physics_pass",
    "contact_sim_pass",
    "v30_offline_ready",
)
FALSE_BOUNDARY_FIELDS = (
    "v30_active",
    "live_motion_authorized",
    "controller_upload_authorized",
    "bridge_start_authorized",
    "tp_play_authorized",
    "zero_ftsensor_authorized",
    "package_accepted",
    "live_accepted",
    "reproduction_complete",
)


def _load(path: Path) -> dict[str, Any]:
    return builder.load_object(path)


def _binding_path(binding: Mapping[str, Any], *, evidence_dir: Path) -> Path:
    locator = str(binding.get("path") or "")
    return builder.resolve_binding_path(locator, base_dir=evidence_dir)


def _verify_binding(
    binding: Any,
    *,
    evidence_dir: Path,
    label: str,
) -> tuple[Path | None, list[str]]:
    if not isinstance(binding, Mapping):
        return None, [f"{label}:binding_invalid"]
    path = _binding_path(binding, evidence_dir=evidence_dir)
    issues: list[str] = []
    if not path.is_file():
        return path, [f"{label}:file_missing"]
    if builder.sha256_file(path) != binding.get("sha256"):
        issues.append(f"{label}:hash_mismatch")
    if path.stat().st_size != binding.get("size_bytes"):
        issues.append(f"{label}:size_mismatch")
    return path, issues


def verify(evidence_path: Path) -> dict[str, Any]:
    issues: list[str] = []
    evidence = _load(evidence_path)
    if evidence.get("schema") != builder.EVIDENCE_SCHEMA:
        issues.append("evidence_schema_invalid")
    inputs = evidence.get("inputs")
    if not isinstance(inputs, Mapping):
        inputs = {}
        issues.append("inputs_invalid")
    paths: dict[str, Path] = {}
    for key in (
        "mujoco_model_manifest",
        "gazebo_static_contract",
        "randomization_config",
        "scenario_manifest",
    ):
        path, binding_issues = _verify_binding(
            inputs.get(key),
            evidence_dir=evidence_path.parent,
            label=key,
        )
        issues.extend(binding_issues)
        if path is not None:
            paths[key] = path
    scenario: dict[str, Any] = {}
    config: dict[str, Any] = {}
    if "scenario_manifest" in paths and paths["scenario_manifest"].is_file():
        scenario = _load(paths["scenario_manifest"])
    if "randomization_config" in paths and paths["randomization_config"].is_file():
        config = _load(paths["randomization_config"])
    if scenario.get("schema") != builder.SCENARIO_SCHEMA:
        issues.append("scenario_schema_invalid")
    scenario_without_fingerprint = dict(scenario)
    scenario_fingerprint = scenario_without_fingerprint.pop("manifest_fingerprint_sha256", None)
    if builder.sha256_value(scenario_without_fingerprint) != scenario_fingerprint:
        issues.append("scenario_fingerprint_mismatch")
    evidence_without_fingerprint = dict(evidence)
    evidence_fingerprint = evidence_without_fingerprint.pop("evidence_fingerprint_sha256", None)
    if builder.sha256_value(evidence_without_fingerprint) != evidence_fingerprint:
        issues.append("evidence_fingerprint_mismatch")

    needed = {
        "mujoco_model_manifest",
        "gazebo_static_contract",
        "randomization_config",
        "scenario_manifest",
    }
    if needed.issubset(paths) and all(paths[key].is_file() for key in needed):
        expected_scenario = builder.build_scenario_manifest(
            config,
            config_binding=builder.source_binding(
                paths["randomization_config"],
                role="uncalibrated_falsification_parameter_ranges",
            ),
            builder_binding=builder.source_binding(
                Path(builder.__file__).resolve(),
                role="deterministic_scenario_builder",
            ),
        )
        if expected_scenario != scenario:
            issues.append("scenario_deterministic_rebuild_mismatch")
        expected_evidence = builder.build_evidence_payload(
            mujoco_manifest_path=paths["mujoco_model_manifest"],
            gazebo_contract_path=paths["gazebo_static_contract"],
            config_path=paths["randomization_config"],
            scenario_path=paths["scenario_manifest"],
            scenario_manifest=scenario,
        )
        if expected_evidence != evidence:
            issues.append("evidence_deterministic_rebuild_mismatch")

    claims = evidence.get("claims")
    if not isinstance(claims, Mapping):
        issues.append("claims_invalid")
    else:
        for key in FALSE_CLAIMS:
            if claims.get(key) is not False:
                issues.append(f"claim_not_false:{key}")
    boundary = evidence.get("claim_boundary")
    if not isinstance(boundary, Mapping):
        issues.append("claim_boundary_invalid")
    else:
        if boundary.get("workflow_state") != "liveprep_blocked":
            issues.append("workflow_state_not_liveprep_blocked")
        if boundary.get("current_program") != "step5d_strict_rnn_ablation_v29":
            issues.append("current_program_not_v29")
        if boundary.get("offline_plan_cannot_promote_runtime_claim") is not True:
            issues.append("offline_plan_promotion_guard_missing")
        for key in FALSE_BOUNDARY_FIELDS:
            if boundary.get(key) is not False:
                issues.append(f"claim_boundary_not_false:{key}")
    comparison = evidence.get("engine_comparison")
    if not isinstance(comparison, Mapping):
        issues.append("engine_comparison_invalid")
    else:
        for key in (
            "mujoco_runtime_evidence_present",
            "gazebo_runtime_evidence_present",
            "eoat_dynamics_equivalence_proven",
            "native_contact_same_run_equivalence_proven",
            "contact_equivalence_pass",
            "timing_equivalence_pass",
            "physics_equivalence_pass",
        ):
            if comparison.get(key) is not False:
                issues.append(f"runtime_equivalence_not_false:{key}")
        if comparison.get("numeric_tcp_equivalence_proven") is not True:
            issues.append("static_numeric_tcp_equivalence_not_proven")
        if comparison.get("static_numeric_tcp_contract_match") is not True:
            issues.append("static_numeric_tcp_contract_mismatch")
    blockers = evidence.get("blockers")
    if not isinstance(blockers, list):
        issues.append("blockers_invalid")
        blockers = []
    for blocker in builder.RUNTIME_BLOCKERS:
        if blocker not in blockers:
            issues.append(f"required_blocker_missing:{blocker}")
    domain = evidence.get("domain_randomization")
    if not isinstance(domain, Mapping):
        issues.append("domain_randomization_invalid")
    else:
        if domain.get("scenario_executed_count") != 0:
            issues.append("scenario_executed_count_not_zero")
        if domain.get("runtime_metrics_present") is not False:
            issues.append("domain_runtime_metrics_present")
        if domain.get("robustness_pass") is not False:
            issues.append("domain_robustness_pass_not_false")
    report = {
        "schema": REPORT_SCHEMA,
        "evidence_path": str(evidence_path),
        "evidence_sha256": builder.sha256_file(evidence_path),
        "structurally_valid": not issues,
        "promotion_ready": False,
        "physics_or_runtime_claim_allowed": False,
        "issues": list(dict.fromkeys(issues)),
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = verify(args.evidence.resolve())
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["structurally_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
