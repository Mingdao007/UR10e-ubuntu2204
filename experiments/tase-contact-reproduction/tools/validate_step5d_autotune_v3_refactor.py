#!/usr/bin/env python3
"""Fail-closed repository and PR gate for the Step5d Autotune v3 refactor."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "config" / "step5d_autotune_v3_test_matrix.json"
DEFAULT_ACTIVE_SURFACE = ROOT / "config" / "step5d" / "v3_active_surface.json"
DEFAULT_EVIDENCE = (
    ROOT
    / "config/step5d/manifests/step5d_strict_rnn_autotune_v3/test_evidence.json"
)
DEFAULT_GOVERNANCE = (
    ROOT
    / "config/step5d/manifests/step5d_autotune_v3_implementation_20260718/governance.json"
)
FROZEN_COMMIT = "6f9ef0912842ac003545eb1906b38d13c7552218"
FROZEN_TAG = "archive/step5d-autotune-v1-20260715"
REQUIRED_LANES = {"small", "medium"}
PORTABLE_CI_DEPENDENCY_DOUBLES: frozenset[str] = frozenset()
STEP5D_V3_TEST_MARKERS = (
    "step5d_autotune_v3",
    "step5d.autotune-v3",
    "step5d-autotune-v3",
    "step5d_strict_rnn_autotune_v3",
    "STEP5D_V3",
)
AUTHORITATIVE_ACTIVE_TESTS = {
    "tests/test_step5d_manual_bridge.py",
    "tests/test_step5d_manual_campaign_plan.py",
    "tests/test_step5d_manual_governed_route.py",
    "tests/test_step5d_manual_queue.py",
    "tests/test_step5d_manual_release.py",
    "tests/test_step5d_manual_tp_v1.py",
    "tests/test_step5d_autotune_live_driver.py",
    "tests/test_step5d_autotune_production_second_lap.py",
    "tests/test_step5d_autotune_runtime.py",
    "tests/test_step5d_autotune_runtime_lifecycle.py",
    "tests/test_step5d_autotune_store.py",
    "tests/test_step5d_autotune_v3_batch_producer.py",
    "tests/test_step5d_autotune_v3_campaign_prepare.py",
    "tests/test_step5d_autotune_v3_contract.py",
    "tests/test_step5d_autotune_v3_dashboard.py",
    "tests/test_step5d_autotune_v3_governance.py",
    "tests/test_step5d_autotune_v3_identity_layers.py",
    "tests/test_step5d_autotune_v3_installed_runtime.py",
    "tests/test_step5d_autotune_v3_live_startup.py",
    "tests/test_step5d_optimizer_protocol.py",
    "tests/test_step5d_autotune_v3_qualification.py",
    "tests/test_step5d_autotune_v3_qualification_endpoints.py",
    "tests/test_step5d_autotune_v3_qualification_production.py",
    "tests/test_step5d_autotune_v3_refactor_gate.py",
    "tests/test_step5d_autotune_v3_test_matrix_runner.py",
    "tests/test_step5d_autotune_v3_tp_delivery_transaction.py",
    "tests/test_step5d_autotune_v3_trial_overlay_mailbox.py",
    "tests/test_step5d_autotune_v3_bridge_wrapper.py",
    "tests/test_step5d_bridge_status.py",
    "tests/test_step5d_bridge_authority.py",
    "tests/test_step5d_r008_rolling_policy.py",
    "tests/test_step5d_r009_release_core.py",
    "tests/test_step5d_runtime_environment.py",
    "tests/test_step5d_runtime_gate.py",
    "tests/test_step5d_runtime_identity.py",
    "tests/test_step5d_runtime_observation.py",
    "tests/test_step5d_production_csv.py",
    "tests/test_step5d_historical_incident_contracts.py",
    "tests/test_step5d_no_contact_p0.py",
    "tests/test_step5d_v3_active_surface_architecture.py",
    "tests/test_step5d_v3_immutable_payload_routing.py",
    "tests/test_step5d_v3_oci_contract.py",
    "tests/test_step5d_v3_runtime_installation.py",
    "tests/test_step5d_v3_source_closure.py",
}
AUTHORITATIVE_OBSOLETE_TESTS = {
    "tests/test_cross_step_parameter_table.py",
    "tests/test_step5d_autotune_v3_arming.py",
    "tests/test_step5d_autotune_v3_bridge_start_context_builder.py",
    "tests/test_step5d_autotune_v3_certification.py",
    "tests/test_step5d_autotune_v3_certification_entrypoints.py",
    "tests/test_step5d_autotune_v3_certification_extractor.py",
    "tests/test_step5d_autotune_v3_execution_readiness.py",
    "tests/test_step5d_autotune_v3_pre_live_rebuild.py",
    "tests/test_step5d_autotune_v3_release_readiness.py",
    "tests/test_step5d_autotune_v3_return_route_evidence.py",
    "tests/test_step5d_autotune_v3_return_route_promotion.py",
    "tests/test_step5d_autotune_v3_stopping_bound_evidence.py",
    "tests/test_step5d_autotune_v3_stopping_bound_promotion.py",
    "tests/test_step5d_autotune_v3_ursim_hold.py",
    "tests/test_step5d_current_binding_gate.py",
    "tests/test_step5d_r006_production_chain.py",
    "tests/test_step5d_r008_production_chain.py",
    "tests/test_step5d_review_policy_v3.py",
    "tests/test_step5d_v31_permissive_contact.py",
    "tests/test_step5d_v3_r004_release_candidate.py",
    "tests/test_step5d_v3_timing_equivalence.py",
    "tests/test_ur_experiment_stage5d_adapter_parity.py",
}
AUTHORITATIVE_ACCEPTANCE_PATH = [
    "clean_environment_canonical_launch",
    "release_manifest_v3_verified",
    "tp_runtime_identity_verified",
    "simulated_play_observed",
    "post_play_identity_rechecked",
    "command_bound_first_arm_grant",
    "first_arm_acknowledged",
    "one_trial_completed",
    "command_bound_next_arm_grant",
    "next_arm_acknowledged",
    "bridge_process_still_alive_at_campaign_outcome",
    "campaign_terminal_attested_before_bridge_cleanup",
]
REQUIRED_ACTIVE_RUNTIME_MODULES = {
    "tools/step5d_autotune_v3/delivery_observation.py",
    "tools/step5d_autotune_v3/preflight_support.py",
    "tools/step5d_autotune_v3/rtde_client.py",
    "tools/step5d_autotune_v3/runtime_environment.py",
    "tools/step5d_autotune_v3/source_closure.py",
}

# Keys are relative to the git root, not to this experiment root.
PROTECTED_V1_SHA256 = {
    "experiments/tase-contact-reproduction/tools/step5d_p0_v9_control_core.py":
        "e48051466beecc41045d35d4a4f39b5f61cce51c5fb314007a6b011de1083514",
    "experiments/tase-contact-reproduction/tools/step5d_p0_v9_bridge.py":
        "3b7446a43c77b95e9d2e597c9a3b8d660ec718d009faed5815405b1d3391b1a6",
    "experiments/tase-contact-reproduction/tools/step5d_paper_outer_loop.py":
        "f2134857e0b5d72618590404488cb4e15a9ac0d47e33c043a3210e50bbf9ca08",
    "experiments/tase-contact-reproduction/tools/step5d_control_contract.py":
        "eed2782da7d44d617758f4ca5f848fd082e40c7b85a9a5e3ffdfa7f5f7ffecb2",
    "experiments/tase-contact-reproduction/tools/contact_semantics.py":
        "39d85cf37e3d64b0eceed65a2899fd9ba33db5e96b1c0b8624e13959a820941e",
    "experiments/tase-contact-reproduction/tools/build_step5d_autotune_tp.py":
        "ef4531b743bd81465615ce260abd278d74eb41f03c7d97c4641e88ca26643a19",
    "experiments/tase-contact-reproduction/config/step5_safe_frame.json":
        "e0e4cda8c07d68161ff448223626b46d0eef903973d74e1af0483bf110aecfa4",
    "experiments/tase-contact-reproduction/config/step5d_autotune_campaign_v1.json":
        "90f1c92ba3497bfdcb08f9154ba007c98c25b8d31cf0df24716c4d69e7d44c43",
    "experiments/tase-contact-reproduction/config/step5d_autotune_i_scale_sanity_v1.json":
        "19b2a07cc43722cf486863150b65b9984160ccec1b326e762022135d20f6a225",
    "experiments/tase-contact-reproduction/programs/step5/step5d/step5d_strict_rnn_autotune_v1.script":
        "6af0254d27ab241c5cf6b952483c5c573255e3afee9daf95f55a62b2387dc29e",
    "experiments/tase-contact-reproduction/programs/step5/step5d/step5d_strict_rnn_autotune_v1.txt":
        "fc6473628ee391cc9297d71fc904da0486caaaa8b36e0989a46db8444b24aca3",
    "experiments/tase-contact-reproduction/programs/step5/step5d/step5d_strict_rnn_autotune_v1.urp":
        "0c6e21709887c6df2be00d02b868bf28bd9e72c92dd5edc5245d4e055b63b250",
}

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
    )


def git_root(root: Path) -> Path:
    result = _git(root, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise ValueError(result.stderr.decode(errors="replace").strip() or "not a git worktree")
    return Path(result.stdout.decode().strip()).resolve()


def _resolve_ref(repo: Path, ref: str) -> str | None:
    result = _git(repo, "rev-parse", f"{ref}^{{commit}}")
    return result.stdout.decode().strip() if result.returncode == 0 else None


def baseline_issues(repo: Path) -> list[str]:
    issues: list[str] = []
    if _resolve_ref(repo, FROZEN_COMMIT) != FROZEN_COMMIT:
        issues.append("frozen_commit_unresolvable")
    if _resolve_ref(repo, FROZEN_TAG) != FROZEN_COMMIT:
        issues.append("frozen_tag_target_mismatch")
    return issues


def protected_source_issues(
    repo: Path,
    *,
    baseline_ref: str = FROZEN_COMMIT,
    expected: Mapping[str, str] = PROTECTED_V1_SHA256,
) -> list[str]:
    """Compare the current worktree, embedded digest, and immutable git blob."""

    issues: list[str] = []
    for relative, expected_sha in sorted(expected.items()):
        current_path = repo / relative
        if not current_path.is_file() or current_path.is_symlink():
            issues.append(f"protected_source_missing_or_symlink:{relative}")
            continue
        current = current_path.read_bytes()
        current_sha = _sha256(current)
        if current_sha != expected_sha:
            issues.append(
                f"protected_source_hash_mismatch:{relative}:"
                f"expected={expected_sha}:actual={current_sha}"
            )
        baseline = _git(repo, "show", f"{baseline_ref}:{relative}")
        if baseline.returncode != 0:
            issues.append(f"protected_source_absent_from_baseline:{relative}")
            continue
        baseline_sha = _sha256(baseline.stdout)
        if baseline_sha != expected_sha:
            issues.append(
                f"protected_source_embedded_hash_not_baseline:{relative}:"
                f"expected={expected_sha}:baseline={baseline_sha}"
            )
        if current != baseline.stdout:
            issues.append(f"protected_source_not_zero_diff:{relative}")
    return issues


def runtime_surface_issues(root: Path) -> tuple[list[str], dict[str, Any]]:
    runtime = root / "tools" / "step5d_autotune_v3"
    if not runtime.is_dir() or runtime.is_symlink():
        return ["v3_runtime_directory_missing_or_symlink"], {"python_modules": []}

    all_entries = sorted(runtime.rglob("*"))
    issues: list[str] = []
    symlinks = [path.relative_to(root).as_posix() for path in all_entries if path.is_symlink()]
    issues.extend(f"v3_runtime_symlink_forbidden:{path}" for path in symlinks)
    unexpected_files = [
        path.relative_to(root).as_posix()
        for path in all_entries
        if path.is_file()
        and not path.is_symlink()
        and path.suffix != ".py"
        and "__pycache__" not in path.parts
    ]
    issues.extend(f"v3_runtime_non_python_file_forbidden:{path}" for path in unexpected_files)
    nested_modules = sorted(
        path.relative_to(root).as_posix()
        for path in runtime.rglob("*.py")
        if path.is_file() and path.parent != runtime and "__pycache__" not in path.parts
    )
    issues.extend(f"v3_runtime_nested_module_forbidden:{path}" for path in nested_modules)
    modules = sorted(
        path.relative_to(root).as_posix()
        for path in runtime.glob("*.py")
        if path.is_file() and not path.is_symlink()
    )
    return issues, {"python_modules": modules}


def active_surface_issues(
    root: Path,
    payload: Any,
    *,
    runtime_modules: set[str],
) -> list[str]:
    """Require every v3 runtime module to be explicitly active or historical."""

    if not isinstance(payload, dict):
        return ["active_surface_not_object"]
    issues: list[str] = []
    if payload.get("schema") != "step5d.autotune-v3/active-surface-v3":
        issues.append("active_surface_schema_mismatch")
    release_truth = payload.get("release_truth")
    if not isinstance(release_truth, dict):
        issues.append("active_surface_release_truth_missing")
    elif release_truth.get("manifest_schema") != (
        "step5d.autotune-v3/release-manifest-v3"
    ):
        issues.append("active_surface_release_manifest_schema_mismatch")

    path_sets: dict[str, set[str]] = {}
    for field in ("active_orchestration_paths", "historical_only"):
        values = payload.get(field)
        if not isinstance(values, list) or any(
            not isinstance(path, str) or not path for path in values
        ):
            issues.append(f"active_surface_paths_invalid:{field}")
            continue
        if len(values) != len(set(values)):
            issues.append(f"active_surface_paths_duplicated:{field}")
        path_sets[field] = set(values)
    if set(path_sets) != {"active_orchestration_paths", "historical_only"}:
        return issues

    active = path_sets["active_orchestration_paths"]
    historical = path_sets["historical_only"]
    for path in sorted(active & historical):
        issues.append(f"active_surface_path_overlap:{path}")
    for relative in sorted(active):
        target = root / relative
        if target.is_symlink() or not target.is_file():
            issues.append(f"active_surface_active_path_unavailable:{relative}")

    runtime_prefix = "tools/step5d_autotune_v3/"
    declared_runtime = {
        path
        for path in active | historical
        if path.startswith(runtime_prefix) and path.endswith(".py")
    }
    for path in sorted(runtime_modules - declared_runtime):
        issues.append(f"active_surface_runtime_module_unclassified:{path}")
    for path in sorted(REQUIRED_ACTIVE_RUNTIME_MODULES - active):
        issues.append(f"active_surface_required_runtime_not_active:{path}")
    return issues


def _commands_valid(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(command, list)
        and command
        and all(isinstance(token, str) and token for token in command)
        for command in value
    )


def step5d_v3_test_paths(root: Path) -> set[str]:
    """Discover repository tests that explicitly bind the Step5d v3 surface."""

    tests = root / "tests"
    if not tests.is_dir() or tests.is_symlink():
        return set()
    discovered: set[str] = set()
    for path in tests.glob("test_*.py"):
        if not path.is_file():
            continue
        filename_bound = re.search(
            r"test_step5d_(?:autotune_)?v3(?:_|\.py$)",
            path.name,
        ) is not None
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            source = ""
        if filename_bound or any(marker in source for marker in STEP5D_V3_TEST_MARKERS):
            discovered.add(path.relative_to(root).as_posix())
    return discovered


def _commanded_test_paths(payload: Mapping[str, Any]) -> set[str]:
    lanes = payload.get("lanes", {})
    commanded = {
        token
        for lane in lanes.values()
        if isinstance(lane, Mapping)
        for command in lane.get("commands", [])
        if isinstance(command, list)
        for token in command
        if isinstance(token, str) and token.startswith("tests/")
    }
    installed_runtime = payload.get("local_installed_runtime_gate", {})
    if isinstance(installed_runtime, Mapping):
        command = installed_runtime.get("command", [])
        commanded.update(
            token
            for token in command
            if isinstance(token, str) and token.startswith("tests/")
        )
    return commanded


def matrix_issues(payload: Any, *, root: Path = ROOT) -> list[str]:
    issues: list[str] = []
    classified: dict[str, set[str]] | None = None
    if not isinstance(payload, dict):
        return ["test_matrix_not_object"]
    if payload.get("schema_version") != "step5d.autotune-v3/test-matrix-v3":
        issues.append("test_matrix_schema_mismatch")
    if payload.get("claim_boundary") != "deterministic_live_entry_prerequisites":
        issues.append("test_matrix_claim_boundary_mismatch")
    if payload.get("historical_evidence_manifest") != (
        "config/step5d/manifests/step5d_strict_rnn_autotune_v3/test_evidence.json"
    ):
        issues.append("test_matrix_historical_evidence_manifest_mismatch")
    if "operator_readiness_gate" in payload:
        issues.append("test_matrix_obsolete_operator_readiness_gate_present")
    bridge_gate = payload.get("authoritative_bridge_gate")
    if not isinstance(bridge_gate, dict):
        issues.append("test_matrix_authoritative_bridge_gate_missing")
    else:
        expected_fields = {
            "current_tp_program_id": "step5d_strict_rnn_autotune_v3_r010",
            "canonical_launcher": "scripts/step5d-autotune-v3.sh bridge",
            "status_reanchor": "scripts/step5d-autotune-v3.sh status --json",
            "production_path_requirement": (
                "canonical shell -> real supervisor -> internal bridge worker -> "
                "real mailbox lifecycle"
            ),
            "unclassified_failure_policy": "block",
        }
        for field, expected in expected_fields.items():
            if bridge_gate.get(field) != expected:
                issues.append(f"test_matrix_authoritative_bridge_field_mismatch:{field}")
        if bridge_gate.get("acceptance_path") != AUTHORITATIVE_ACCEPTANCE_PATH:
            issues.append("test_matrix_authoritative_acceptance_path_mismatch")
        classifications = bridge_gate.get("classified_test_files")
        if not isinstance(classifications, dict) or set(classifications) != {
            "active", "obsolete", "unrelated",
        }:
            issues.append("test_matrix_classification_contract_mismatch")
        else:
            candidate: dict[str, set[str]] = {}
            for name in ("active", "obsolete", "unrelated"):
                values = classifications.get(name)
                if not isinstance(values, list) or any(
                    not isinstance(path, str)
                    or not path.startswith("tests/")
                    or not path.endswith(".py")
                    for path in values
                ):
                    issues.append(f"test_matrix_classification_paths_invalid:{name}")
                    continue
                if len(values) != len(set(values)):
                    issues.append(f"test_matrix_classification_duplicates:{name}")
                candidate[name] = set(values)
            if set(candidate) == {"active", "obsolete", "unrelated"}:
                classified = candidate
                if classified["active"] != AUTHORITATIVE_ACTIVE_TESTS:
                    issues.append("test_matrix_authoritative_active_set_mismatch")
                if classified["obsolete"] != AUTHORITATIVE_OBSOLETE_TESTS:
                    issues.append("test_matrix_authoritative_obsolete_set_mismatch")
                names = ("active", "obsolete", "unrelated")
                for index, left in enumerate(names):
                    for right in names[index + 1:]:
                        for path in sorted(classified[left] & classified[right]):
                            issues.append(
                                f"test_matrix_classification_overlap:{left}:{right}:{path}"
                            )
                all_classified = set().union(*classified.values())
                for path in sorted(step5d_v3_test_paths(root) - all_classified):
                    issues.append(f"test_matrix_repository_v3_test_unclassified:{path}")
                for path in sorted(all_classified):
                    target = root / path
                    if target.is_symlink() or not target.is_file():
                        issues.append(f"test_matrix_classified_test_unavailable:{path}")
    if "hil_launch_permit_gate" in payload:
        issues.append("test_matrix_obsolete_hil_launch_permit_present")
    installed_runtime = payload.get("local_installed_runtime_gate")
    expected_installed_runtime = {
        "command": [
            "@control-runtime-python", "-m", "pytest", "-q",
            "tests/test_step5d_autotune_v3_contract.py",
            "tests/test_step5d_autotune_runtime.py",
            "tests/test_step5d_autotune_live_driver.py",
            "tests/test_step5d_autotune_v3_bridge_wrapper.py",
            "tests/test_step5d_autotune_v3_qualification_production.py",
            "tests/test_step5d_autotune_v3_installed_runtime.py",
            "tests/test_step5d_manual_bridge.py",
            "tests/test_step5d_no_contact_p0.py",
        ],
        "activation": "explicit_local_authoritative_after_hermetic_small_medium",
        "ci": False,
        "serial": True,
        "test_doubles_allowed": False,
        "network_allowed": False,
        "controller_access_allowed": False,
        "live_writer_allowed": False,
        "arm_allowed": False,
        "motion_allowed": False,
        "evidence_output": "parallel runner log and manifest outside rule JSON",
    }
    if installed_runtime != expected_installed_runtime:
        issues.append("test_matrix_local_installed_runtime_gate_mismatch")
    lanes = payload.get("lanes")
    if not isinstance(lanes, dict):
        return issues + ["test_matrix_lanes_not_object"]
    if set(lanes) != REQUIRED_LANES:
        issues.append(
            "test_matrix_lane_set_mismatch:"
            f"expected={sorted(REQUIRED_LANES)}:actual={sorted(lanes)}"
        )
    for name in sorted(REQUIRED_LANES & set(lanes)):
        lane = lanes[name]
        if not isinstance(lane, dict):
            issues.append(f"test_matrix_lane_not_object:{name}")
            continue
        if not _commands_valid(lane.get("commands")):
            issues.append(f"test_matrix_commands_invalid:{name}")
        for field in (
            "ci", "serial", "pinned", "test_doubles_allowed", "network_allowed",
            "controller_access_allowed", "live_writer_allowed", "hold_required",
            "arm_allowed", "motion_allowed",
        ):
            if not isinstance(lane.get(field), bool):
                issues.append(f"test_matrix_boolean_missing:{name}:{field}")
    for name in ("small", "medium"):
        lane = lanes.get(name, {})
        for field in ("network_allowed", "controller_access_allowed", "live_writer_allowed", "motion_allowed"):
            if lane.get(field) is not False:
                issues.append(f"test_matrix_ci_lane_not_hermetic:{name}:{field}")
        if lane.get("ci") is not True:
            issues.append(f"test_matrix_ci_lane_disabled:{name}")
        if not lane.get("commands"):
            issues.append(f"test_matrix_ci_lane_commands_empty:{name}")
        if any(
            token == "tests/test_step5d_autotune_v3_installed_runtime.py"
            for command in lane.get("commands", [])
            for token in command
        ):
            issues.append(f"test_matrix_installed_runtime_leaked_into_ci:{name}")
    if classified is not None:
        commanded = _commanded_test_paths(payload)
        for path in sorted(classified["active"] - commanded):
            issues.append(f"test_matrix_active_test_not_commanded:{path}")
        for path in sorted(classified["obsolete"] & commanded):
            issues.append(f"test_matrix_obsolete_test_commanded:{path}")
        for path in sorted(classified["unrelated"] & commanded):
            issues.append(f"test_matrix_unrelated_test_commanded:{path}")
        for path in sorted(commanded - set().union(*classified.values())):
            issues.append(f"test_matrix_commanded_test_unclassified:{path}")
    small = lanes.get("small", {})
    if small.get("production_parser_test_double_allowed") is not False:
        issues.append("test_matrix_production_parser_double_not_forbidden")
    if set(small.get("dependency_double_scope", [])) != PORTABLE_CI_DEPENDENCY_DOUBLES:
        issues.append("test_matrix_parser_dependency_stub_scope_mismatch")
    ci = payload.get("ci")
    if not isinstance(ci, dict):
        issues.append("test_matrix_ci_contract_missing")
    else:
        if set(ci.get("lanes", [])) != {"small", "medium"}:
            issues.append("test_matrix_ci_lane_allowlist_mismatch")
        for field in ("network_allowed", "controller_access_allowed", "live_writer_allowed", "motion_allowed"):
            if ci.get(field) is not False:
                issues.append(f"test_matrix_ci_boundary_not_false:{field}")
    return issues


def evidence_issues(payload: Any, *, root: Path) -> list[str]:
    if not isinstance(payload, dict):
        return ["test_evidence_not_object"]
    issues: list[str] = []
    if payload.get("schema") != "step5d.autotune-v3/test-evidence-v1":
        issues.append("test_evidence_schema_mismatch")
    if payload.get("test_matrix") != "config/step5d_autotune_v3_test_matrix.json":
        issues.append("test_evidence_matrix_binding_mismatch")
    lanes = payload.get("lanes")
    if not isinstance(lanes, dict) or set(lanes) != {"large_ursim", "hil_no_motion"}:
        return issues + ["test_evidence_lane_set_mismatch"]
    for name, lane in lanes.items():
        if not isinstance(lane, dict):
            issues.append(f"test_evidence_lane_not_object:{name}")
            continue
        status = lane.get("status")
        if status == "pass":
            if name != "large_ursim":
                issues.append(f"test_evidence_false_pass:{name}")
                continue
            required_paths = {
                "result": "config/step5/step5d_autotune_v3_ursim_hold_result.json",
                "raw_evidence": "config/step5/step5d_autotune_v3_ursim_hold_raw.json",
            }
            if lane.get("cleanup_completed") is not True:
                issues.append(f"test_evidence_pass_field:{name}:cleanup_completed")
            if not isinstance(lane.get("observed_at"), str) or not lane["observed_at"].strip():
                issues.append(f"test_evidence_pass_field:{name}:observed_at")
            for path_field, required in required_paths.items():
                sha_field = f"{path_field}_sha256"
                relative = lane.get(path_field)
                expected_sha = lane.get(sha_field)
                if relative != required:
                    issues.append(f"test_evidence_pass_field:{name}:{path_field}")
                    continue
                if not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None or set(expected_sha) == {"0"}:
                    issues.append(f"test_evidence_pass_field:{name}:{sha_field}")
                    continue
                target = root / relative
                if target.is_symlink() or not target.is_file():
                    issues.append(f"test_evidence_unavailable:{name}:{path_field}")
                elif _sha256(target.read_bytes()) != expected_sha:
                    issues.append(f"test_evidence_digest:{name}:{path_field}")
        elif status == "failed":
            relative = lane.get("result")
            expected_sha = lane.get("result_sha256")
            if (
                name != "hil_no_motion"
                or lane.get("cleanup_completed") is not True
                or not isinstance(lane.get("observed_at"), str)
                or not lane["observed_at"].strip()
            ):
                issues.append(f"test_evidence_failed_field:{name}")
            expected_path = (
                "config/step5d/manifests/step5d_strict_rnn_autotune_v3/"
                "hil_hold_failure_20260719.json"
            )
            if relative != expected_path:
                issues.append(f"test_evidence_failed_field:{name}:result")
            elif (
                not isinstance(expected_sha, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None
                or set(expected_sha) == {"0"}
            ):
                issues.append(f"test_evidence_failed_field:{name}:result_sha256")
            else:
                target = root / relative
                if target.is_symlink() or not target.is_file():
                    issues.append(f"test_evidence_unavailable:{name}:result")
                elif _sha256(target.read_bytes()) != expected_sha:
                    issues.append(f"test_evidence_digest:{name}:result")
        elif status not in {"not_run", "blocked_not_authorized"}:
            issues.append(f"test_evidence_status_invalid:{name}")
    return issues


def content_governance_issues(root: Path, matrix: Mapping[str, Any]) -> list[str]:
    governance_path = (
        root
        / "config/step5d/manifests/step5d_autotune_v3_implementation_20260718/governance.json"
    )
    try:
        governance = json.loads(governance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"content_governance_unreadable:{exc}"]
    issues: list[str] = []
    if governance.get("schema") != "step5d.autotune-v3/content-governance-v1":
        issues.append("content_governance_schema_mismatch")
    if governance.get("selected_policy") != "B1_broad_cleanup_staged":
        issues.append("content_governance_selected_policy_mismatch")
    budgets = governance.get("file_budgets") or {}
    json_paths = [
        root / "config/step5d_autotune_v3_test_matrix.json",
        root / "config/step5/step5d_autotune_v3_launch_profile.json",
        root / "config/step5d/manifests/step5d_strict_rnn_autotune_v3/test_evidence.json",
        governance_path,
    ]
    max_bytes = budgets.get("v3_hand_authored_json_max_bytes")
    max_lines = budgets.get("v3_hand_authored_json_max_lines")
    for path in json_paths:
        if not isinstance(max_bytes, int) or not isinstance(max_lines, int):
            issues.append("content_governance_json_budget_invalid")
            break
        if path.is_symlink() or not path.is_file():
            issues.append(f"content_governance_json_missing:{path.relative_to(root)}")
            continue
        encoded = path.read_bytes()
        if len(encoded) > max_bytes:
            issues.append(f"content_governance_json_bytes_exceeded:{path.relative_to(root)}")
        if len(encoded.splitlines()) > max_lines:
            issues.append(f"content_governance_json_lines_exceeded:{path.relative_to(root)}")
    lanes = matrix.get("lanes") or {}
    test_paths: dict[str, str] = {}
    for lane_name in ("small", "medium"):
        for command in (lanes.get(lane_name) or {}).get("commands", []):
            for token in command:
                if isinstance(token, str) and token.startswith("tests/"):
                    prior = test_paths.setdefault(token, lane_name)
                    if prior != lane_name:
                        issues.append(f"test_matrix_duplicate_test_path:{token}:{prior}:{lane_name}")
    exception = governance.get("temporary_exception") or {}
    if exception.get("path") != "STEP5_FLOW.md" or not exception.get("reason"):
        issues.append("content_governance_markdown_exception_invalid")
    return issues


def load_matrix(path: Path, *, root: Path | None = None) -> tuple[Any, list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [f"test_matrix_unreadable:{exc}"]
    root = (root or path.resolve().parent.parent).resolve()
    issues = matrix_issues(payload, root=root)
    evidence_relative = (
        payload.get("historical_evidence_manifest")
        if isinstance(payload, dict)
        else None
    )
    evidence_path = root / evidence_relative if isinstance(evidence_relative, str) else None
    if evidence_path is None or evidence_path.is_symlink() or not evidence_path.is_file():
        issues.append("test_evidence_manifest_unavailable")
    else:
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            issues.append(f"test_evidence_unreadable:{exc}")
        else:
            issues.extend(evidence_issues(evidence, root=root))
    requirements = payload.get("requirements") if isinstance(payload, dict) else None
    if not isinstance(requirements, list) or not requirements:
        issues.append("test_matrix_incident_requirements_missing")
    else:
        for requirement in requirements:
            if not isinstance(requirement, dict) or not isinstance(requirement.get("id"), str):
                issues.append("test_matrix_incident_requirement_invalid")
                continue
            identifier = requirement["id"]
            fixture = requirement.get("incident_fixture")
            if not isinstance(fixture, str) or not fixture:
                issues.append(f"test_matrix_incident_fixture_missing:{identifier}")
            else:
                fixture_path = root / fixture
                if not fixture_path.is_file() or fixture_path.is_symlink():
                    issues.append(f"test_matrix_incident_fixture_unavailable:{identifier}:{fixture}")
    return payload, issues


def _sections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", text))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1).strip().lower()] = text[match.end():end].strip()
    return sections


def _meaningful_text(section: str) -> str:
    section = re.sub(r"<!--.*?-->", "", section, flags=re.DOTALL)
    return "\n".join(
        line for line in section.splitlines()
        if line.strip() and not line.strip().startswith("```")
    ).strip()


def _placeholder(value: str) -> bool:
    stripped = value.strip()
    lowered = stripped.lower()
    plain = lowered.lstrip("-* ").strip()
    return (
        not stripped
        or plain in {"...", "-", "todo", "tbd", "describe", "replace"}
        or plain.startswith(("todo:", "tbd:", "describe this", "replace this", "replace with"))
        or re.search(r"<(?:command|commit)(?:\b|>)", lowered) is not None
    )


def _code_commands(section: str) -> list[str]:
    commands: list[str] = []
    for block in re.findall(r"```(?:bash|sh|shell|console)?\s*\n(.*?)```", section, re.DOTALL | re.IGNORECASE):
        for line in block.splitlines():
            command = line.strip()
            if command.startswith("$"):
                command = command[1:].strip()
            if command and not command.startswith("#") and not _placeholder(command):
                commands.append(command)
    return commands


def declaration_issues(text: str) -> list[str]:
    issues: list[str] = []
    checked = re.findall(
        r"(?mi)^\s*-\s*\[[xX]\]\s*`?(behavior_preserving|behavior_changing)`?\s*$",
        text,
    )
    if len(checked) != 1:
        issues.append(f"declaration_change_class_count:{len(checked)}")
    baseline = re.search(
        r"(?mi)^\s*-\s*Frozen baseline:\s*`?([0-9a-f]{40})`?\s*$",
        text,
    )
    if baseline is None:
        issues.append("declaration_frozen_baseline_missing")
    elif baseline.group(1) != FROZEN_COMMIT:
        issues.append("declaration_frozen_baseline_mismatch")
    sections = _sections(text)
    allowed = _meaningful_text(sections.get("allowed deltas", ""))
    if _placeholder(allowed):
        issues.append("declaration_allowed_deltas_missing")
    rollback = _code_commands(sections.get("rollback", ""))
    if not rollback:
        issues.append("declaration_rollback_command_missing")
    tests = _code_commands(sections.get("validation commands", ""))
    if not tests:
        issues.append("declaration_test_command_missing")
    return issues


def pr_template_structure_issues(repo: Path) -> list[str]:
    path = repo / ".github" / "pull_request_template.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"pr_template_unreadable:{exc}"]
    issues: list[str] = []
    for token in (
        "behavior_preserving", "behavior_changing", FROZEN_COMMIT,
        "## Allowed deltas", "## Rollback", "## Validation commands",
    ):
        if token not in text:
            issues.append(f"pr_template_required_token_missing:{token}")
    return issues


def validate_repository(root: Path, matrix: Path = DEFAULT_MATRIX) -> dict[str, Any]:
    root = root.resolve()
    repo = git_root(root)
    runtime_findings, runtime = runtime_surface_issues(root)
    active_surface_path = root / "config" / "step5d" / "v3_active_surface.json"
    try:
        active_surface = json.loads(active_surface_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        active_surface = None
        active_surface_findings = [f"active_surface_unreadable:{exc}"]
    else:
        active_surface_findings = active_surface_issues(
            root,
            active_surface,
            runtime_modules=set(runtime["python_modules"]),
        )
    matrix_payload, matrix_findings = load_matrix(matrix.resolve(), root=root)
    governance_findings = content_governance_issues(
        root, matrix_payload if isinstance(matrix_payload, Mapping) else {}
    )
    issues = [
        *baseline_issues(repo),
        *protected_source_issues(repo),
        *runtime_findings,
        *active_surface_findings,
        *matrix_findings,
        *governance_findings,
        *pr_template_structure_issues(repo),
    ]
    return {
        "schema": "step5d.autotune-v3/refactor-gate-report-v1",
        "ok": not issues,
        "mode": "repository",
        "root": str(root),
        "git_root": str(repo),
        "frozen_commit": FROZEN_COMMIT,
        "protected_source_count": len(PROTECTED_V1_SHA256),
        "runtime_surface": runtime,
        "active_surface_schema": (
            active_surface.get("schema") if isinstance(active_surface, dict) else None
        ),
        "test_matrix_schema": (
            matrix_payload.get("schema_version") if isinstance(matrix_payload, dict) else None
        ),
        "issues": sorted(set(issues)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--declaration-file", type=Path)
    source.add_argument("--declaration-env", default=None)
    parser.add_argument(
        "--repository-only",
        action="store_true",
        help="check repository invariants only; never counts as PR declaration acceptance",
    )
    args = parser.parse_args()
    try:
        report = validate_repository(args.root, args.matrix)
    except (OSError, UnicodeError, ValueError) as exc:
        report = {
            "schema": "step5d.autotune-v3/refactor-gate-report-v1",
            "ok": False,
            "mode": "repository",
            "issues": [f"repository_gate_error:{exc}"],
        }
    if not args.repository_only:
        declaration: str | None = None
        if args.declaration_file is not None:
            try:
                declaration = args.declaration_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                report.setdefault("issues", []).append(f"declaration_unreadable:{exc}")
        else:
            env_name = args.declaration_env or "STEP5D_V3_PR_BODY"
            declaration = os.environ.get(env_name)
            if declaration is None:
                report.setdefault("issues", []).append(f"declaration_environment_missing:{env_name}")
        if declaration is not None:
            report.setdefault("issues", []).extend(declaration_issues(declaration))
        report["mode"] = "pull_request"
        report["issues"] = sorted(set(report.get("issues", [])))
        report["ok"] = not report["issues"]
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
