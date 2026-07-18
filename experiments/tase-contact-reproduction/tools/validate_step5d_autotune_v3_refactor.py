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
RUNTIME_MODULE_LIMIT = 9
RUNTIME_LOC_LIMIT = 3600
RUNTIME_FILE_LOC_LIMIT = 650
REQUIRED_LANES = {"small", "medium", "large_ursim", "hil_no_motion"}
PARSER_CI_DEPENDENCY_STUBS = {
    "_ur_common", "capture_kunwei_kwr75_1khz", "numpy", "pandas",
    "pinocchio", "xacro", "yaml",
}
READINESS_TRANSITION_ORDER = [
    "offline_acceptance",
    "controller_readback",
    "hil_hold_only",
    "candidate_scoped_current_turn_authorization",
    "current_candidate_binding",
    "same_process_startup_gate",
    "live_execution",
]
READY_TO_EXECUTE_REQUIRES = [
    "offline_acceptance_pass",
    "controller_readback_verified",
    "hil_no_motion_pass",
    "candidate_scoped_current_turn_authorization",
    "current_candidate_binding",
    "live_runtime_promoted",
    "same_process_startup_gate_pass",
]
HIL_AUTHORIZATION_COMMAND = [
    "python3",
    "tools/verify_step5d_autotune_v3_hil_authorization.py",
    "--authorization",
    "<current-turn-authorization.json>",
    "--expected-thread-id",
    "<current-thread-id>",
    "--json",
]

# Keys are relative to the git root, not to this experiment root.
PROTECTED_V1_SHA256 = {
    "experiments/tase-contact-reproduction/tools/kunwei_rtde_bridge.py":
        "5f913259826dcaff0d54bcae43d6c30b0efffe63ad8b8565e43459fa954fe3db",
    "experiments/tase-contact-reproduction/tools/step5d_p0_v9_control_core.py":
        "e48051466beecc41045d35d4a4f39b5f61cce51c5fb314007a6b011de1083514",
    "experiments/tase-contact-reproduction/tools/step5d_p0_v9_bridge.py":
        "3b7446a43c77b95e9d2e597c9a3b8d660ec718d009faed5815405b1d3391b1a6",
    "experiments/tase-contact-reproduction/tools/step5d_autotune_contract.py":
        "e54eb807ad30df95e41ca108c647d010c360e344c7f6915f76666fb96879eee9",
    "experiments/tase-contact-reproduction/tools/step5d_autotune_coordinator.py":
        "1e0a0b1a6cc739ec259575c8e1e0f06c54bfc9dd77e504c78a6a6ad7f2b6f71a",
    "experiments/tase-contact-reproduction/tools/step5d_autotune_journal.py":
        "8e04cd0744b4df22e4fbfaaf9ee1587823b1f4ecacc317c0e0e7fdafaf7fb2b0",
    "experiments/tase-contact-reproduction/tools/step5d_autotune_store.py":
        "d483d5299f44e8c31be1f84d1e0fc03961dbe863cf2d70794108ac085f7eab4a",
    "experiments/tase-contact-reproduction/tools/step5d_autotune_live_driver.py":
        "5929c1ceb20a8541c28793f5fdf4c433146e7b35cecdc40ac6ab9177c0cd7fec",
    "experiments/tase-contact-reproduction/tools/step5d_runtime_interface.py":
        "56d388207542d5d5d93175f9d8922d3366f7fd368f0763a7304f2933844e7e2d",
    "experiments/tase-contact-reproduction/tools/step5d_paper_outer_loop.py":
        "f2134857e0b5d72618590404488cb4e15a9ac0d47e33c043a3210e50bbf9ca08",
    "experiments/tase-contact-reproduction/tools/step5d_control_contract.py":
        "eed2782da7d44d617758f4ca5f848fd082e40c7b85a9a5e3ffdfa7f5f7ffecb2",
    "experiments/tase-contact-reproduction/tools/contact_semantics.py":
        "39d85cf37e3d64b0eceed65a2899fd9ba33db5e96b1c0b8624e13959a820941e",
    "experiments/tase-contact-reproduction/tools/build_step5d_autotune_tp.py":
        "ef4531b743bd81465615ce260abd278d74eb41f03c7d97c4641e88ca26643a19",
    "experiments/tase-contact-reproduction/scripts/bridge-line-operator.sh":
        "2fdc3faa70c57614d5a731fbf1024d046d11da23f374711c9002a1b9d6be58ec",
    "experiments/tase-contact-reproduction/scripts/step5d-autotune-live.sh":
        "301cf94d9bed54518443075abada22b61981b4aa342f9c4aa458e5292b3b2b38",
    "experiments/tase-contact-reproduction/config/step5_safe_frame.json":
        "e0e4cda8c07d68161ff448223626b46d0eef903973d74e1af0483bf110aecfa4",
    "experiments/tase-contact-reproduction/config/step5d_autotune_campaign_v1.json":
        "90f1c92ba3497bfdcb08f9154ba007c98c25b8d31cf0df24716c4d69e7d44c43",
    "experiments/tase-contact-reproduction/config/step5d_autotune_i_scale_sanity_v1.json":
        "19b2a07cc43722cf486863150b65b9984160ccec1b326e762022135d20f6a225",
    "experiments/tase-contact-reproduction/config/current_stage.json":
        "8d6684717008a3d4bfbdd948a03083188f6456afb5313d58edbecfa6cb0e6132",
    "experiments/tase-contact-reproduction/programs/step5/step5d/step5d_strict_rnn_autotune_v1.script":
        "6af0254d27ab241c5cf6b952483c5c573255e3afee9daf95f55a62b2387dc29e",
    "experiments/tase-contact-reproduction/programs/step5/step5d/step5d_strict_rnn_autotune_v1.txt":
        "fc6473628ee391cc9297d71fc904da0486caaaa8b36e0989a46db8444b24aca3",
    "experiments/tase-contact-reproduction/programs/step5/step5d/step5d_strict_rnn_autotune_v1.urp":
        "0c6e21709887c6df2be00d02b868bf28bd9e72c92dd5edc5245d4e055b63b250",
}

APPROVED_ORCHESTRATION_VARIANTS = {
    "experiments/tase-contact-reproduction/tools/run_step5d_autotune_campaign.py": {
        "baseline_sha256": "f7485600db9571d882076f3ceeed8ee999bee998d35fdc8fe2386664bb55550c",
        "approved_sha256": "13f23b82ddbae4823efb8747c891963bf21c458244c5b36622570ec5ae9077cc",
        "change_class": "behavior_changing",
    },
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


def orchestration_variant_issues(
    repo: Path,
    *,
    baseline_ref: str = FROZEN_COMMIT,
    expected: Mapping[str, Mapping[str, str]] = APPROVED_ORCHESTRATION_VARIANTS,
) -> list[str]:
    """Allow only reviewed whole-file orchestration variants over the v1 blob."""

    issues: list[str] = []
    for relative, contract in sorted(expected.items()):
        path = repo / relative
        if not path.is_file() or path.is_symlink():
            issues.append(f"orchestration_variant_missing_or_symlink:{relative}")
            continue
        baseline = _git(repo, "show", f"{baseline_ref}:{relative}")
        if baseline.returncode != 0:
            issues.append(f"orchestration_variant_absent_from_baseline:{relative}")
            continue
        baseline_sha = _sha256(baseline.stdout)
        if baseline_sha != contract.get("baseline_sha256"):
            issues.append(f"orchestration_variant_baseline_hash_mismatch:{relative}")
        current_sha = _sha256(path.read_bytes())
        if current_sha != contract.get("approved_sha256"):
            issues.append(
                f"orchestration_variant_unapproved:{relative}:actual={current_sha}"
            )
        if contract.get("change_class") != "behavior_changing":
            issues.append(f"orchestration_variant_change_class_invalid:{relative}")
    return issues


def _physical_source_lines(path: Path) -> int:
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


def runtime_budget(root: Path) -> tuple[list[str], dict[str, Any]]:
    runtime = root / "tools" / "step5d_autotune_v3"
    all_entries = sorted(runtime.rglob("*")) if runtime.is_dir() else []
    modules = sorted(
        path for path in runtime.glob("*.py")
        if path.is_file() and not path.is_symlink()
    ) if runtime.is_dir() else []
    loc_by_module = {
        path.relative_to(root).as_posix(): _physical_source_lines(path)
        for path in modules
    }
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
    issues.extend(f"v3_runtime_unbudgeted_file:{path}" for path in unexpected_files)
    if len(modules) > RUNTIME_MODULE_LIMIT:
        issues.append(
            f"v3_runtime_module_budget_exceeded:{len(modules)}>{RUNTIME_MODULE_LIMIT}"
        )
    total_loc = sum(loc_by_module.values())
    if total_loc > RUNTIME_LOC_LIMIT:
        issues.append(f"v3_runtime_loc_budget_exceeded:{total_loc}>{RUNTIME_LOC_LIMIT}")
    for relative, count in loc_by_module.items():
        if count > RUNTIME_FILE_LOC_LIMIT:
            issues.append(
                f"v3_runtime_file_loc_budget_exceeded:{relative}:{count}>"
                f"{RUNTIME_FILE_LOC_LIMIT}"
            )
    nested_modules = sorted(
        path.relative_to(root).as_posix()
        for path in runtime.rglob("*.py")
        if path.is_file() and path.parent != runtime and "__pycache__" not in path.parts
    ) if runtime.is_dir() else []
    if nested_modules:
        issues.extend(f"v3_runtime_module_outside_flat_budget:{path}" for path in nested_modules)
    return issues, {
        "module_limit": RUNTIME_MODULE_LIMIT,
        "module_count": len(modules),
        "loc_limit": RUNTIME_LOC_LIMIT,
        "loc_count": total_loc,
        "file_loc_limit": RUNTIME_FILE_LOC_LIMIT,
        "loc_by_module": loc_by_module,
    }


def _commands_valid(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(command, list)
        and command
        and all(isinstance(token, str) and token for token in command)
        for command in value
    )


def matrix_issues(payload: Any) -> list[str]:
    issues: list[str] = []
    if not isinstance(payload, dict):
        return ["test_matrix_not_object"]
    if payload.get("schema_version") != "step5d.autotune-v3/test-matrix-v2":
        issues.append("test_matrix_schema_mismatch")
    if payload.get("evidence_manifest") != (
        "config/step5d/manifests/step5d_strict_rnn_autotune_v3/test_evidence.json"
    ):
        issues.append("test_matrix_evidence_manifest_mismatch")
    readiness = payload.get("operator_readiness_gate")
    if not isinstance(readiness, dict):
        issues.append("test_matrix_operator_readiness_gate_missing")
    else:
        if readiness.get("command") != [
            "python3",
            "tools/verify_step5d_autotune_v3_execution_readiness.py",
            "--json",
        ]:
            issues.append("test_matrix_operator_readiness_command_mismatch")
        if readiness.get("public_success_signal_policy") != (
            "next_legal_action_not_broad_pass"
        ):
            issues.append("test_matrix_operator_success_signal_policy_mismatch")
        if readiness.get("historical_authorization_reuse_allowed") is not False:
            issues.append("test_matrix_historical_authorization_reuse_not_forbidden")
        if readiness.get("transition_order") != READINESS_TRANSITION_ORDER:
            issues.append("test_matrix_readiness_transition_order_mismatch")
        if readiness.get("ready_to_execute_requires") != READY_TO_EXECUTE_REQUIRES:
            issues.append("test_matrix_ready_to_execute_requirements_mismatch")
    hil_authorization = payload.get("hil_authorization_gate")
    if not isinstance(hil_authorization, dict):
        issues.append("test_matrix_hil_authorization_gate_missing")
    else:
        expected_hil_authorization = {
            "command": HIL_AUTHORIZATION_COMMAND,
            "scope": "hil_full_bridge_hold",
            "candidate_stage_id": "step5d_strict_rnn_autotune_v3",
            "max_ttl_s": 1800,
            "current_fingerprint_binding_required": True,
            "current_turn_thread_binding_required": True,
            "serial": True,
            "hold_required": True,
            "live_writer_allowed": True,
            "operator_action_consumed": False,
        }
        if hil_authorization != expected_hil_authorization:
            issues.append("test_matrix_hil_authorization_contract_mismatch")
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
    small = lanes.get("small", {})
    if small.get("production_parser_test_double_allowed") is not False:
        issues.append("test_matrix_production_parser_double_not_forbidden")
    if set(small.get("dependency_double_scope", [])) != PARSER_CI_DEPENDENCY_STUBS:
        issues.append("test_matrix_parser_dependency_stub_scope_mismatch")
    for name in ("large_ursim", "hil_no_motion"):
        lane = lanes.get(name, {})
        expected = {
            "ci": False,
            "serial": True,
            "pinned": True,
            "test_doubles_allowed": False,
            "hold_required": True,
            "arm_allowed": False,
            "motion_allowed": False,
        }
        for field, required in expected.items():
            if lane.get(field) is not required:
                issues.append(f"test_matrix_realistic_lane_policy:{name}:{field}")
        forbidden_evidence = {
            "execution_status", "execution_observed_at", "immutable_result",
            "immutable_result_sha256", "raw_evidence", "raw_evidence_sha256",
            "cleanup_completed",
        }
        if forbidden_evidence & set(lane):
            issues.append(f"test_matrix_embeds_evidence:{name}")
    image = lanes.get("large_ursim", {}).get("container_image", "")
    match = re.fullmatch(r"[^\s@]+@sha256:([0-9a-f]{64})", image) if isinstance(image, str) else None
    if not match or set(match.group(1)) == {"0"}:
        issues.append("test_matrix_ursim_image_not_digest_pinned")
    large = lanes.get("large_ursim", {})
    restricted_network = {
        "network_allowed": True,
        "network_scope": "single_prestarted_container_on_docker_internal_network",
        "external_network_allowed": False,
        "robot_network_allowed": False,
        "host_port_publication_allowed": False,
        "container_lifecycle_mutation_allowed": False,
        "rtde_output_recipe_only": True,
        "rtde_input_recipe_allowed": False,
    }
    for field, required in restricted_network.items():
        if large.get(field) != required:
            issues.append(f"test_matrix_ursim_restricted_network_policy:{field}")
    if large.get("docker_actions_allowed") != [
        "version", "container_inspect", "image_inspect", "network_inspect"
    ]:
        issues.append("test_matrix_ursim_docker_action_allowlist_drift")
    if large.get("dashboard_commands_allowed") != [
        "PolyscopeVersion", "robotmode", "programState", "running", "safetymode"
    ]:
        issues.append("test_matrix_ursim_dashboard_allowlist_drift")
    if lanes.get("hil_no_motion", {}).get("controller_identity_pin") != (
        "fresh_read_only_snapshot_sha256_required"
    ):
        issues.append("test_matrix_hil_identity_pin_missing")
    hil = lanes.get("hil_no_motion", {})
    for field, required in {
        "network_allowed": True,
        "network_scope": "target_controller_and_kunwei_only_after_current_turn_authorization",
        "controller_access_allowed": True,
        "live_writer_allowed": True,
    }.items():
        if hil.get(field) != required:
            issues.append(f"test_matrix_hil_full_bridge_policy:{field}")
    ci = payload.get("ci")
    if not isinstance(ci, dict):
        issues.append("test_matrix_ci_contract_missing")
    else:
        if set(ci.get("lanes", [])) != {"small", "medium"}:
            issues.append("test_matrix_ci_lane_allowlist_mismatch")
        if set(ci.get("forbidden_lanes", [])) != {"large_ursim", "hil_no_motion"}:
            issues.append("test_matrix_ci_forbidden_lane_mismatch")
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


def load_matrix(path: Path) -> tuple[Any, list[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, [f"test_matrix_unreadable:{exc}"]
    issues = matrix_issues(payload)
    root = path.resolve().parent.parent
    evidence_relative = payload.get("evidence_manifest") if isinstance(payload, dict) else None
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
            if set(requirement.get("lanes", [])) != REQUIRED_LANES:
                issues.append(f"test_matrix_incident_lane_coverage_mismatch:{identifier}")
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
    lowered = value.lower()
    return (
        not value.strip()
        or "todo" in lowered
        or "replace" in lowered
        or "describe" in lowered
        or "<command" in lowered
        or "<commit" in lowered
        or value.strip() in {"...", "-"}
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
    runtime_findings, runtime = runtime_budget(root)
    matrix_payload, matrix_findings = load_matrix(matrix.resolve())
    governance_findings = content_governance_issues(
        root, matrix_payload if isinstance(matrix_payload, Mapping) else {}
    )
    issues = [
        *baseline_issues(repo),
        *protected_source_issues(repo),
        *orchestration_variant_issues(repo),
        *runtime_findings,
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
        "approved_orchestration_variant_count": len(APPROVED_ORCHESTRATION_VARIANTS),
        "runtime_budget": runtime,
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
