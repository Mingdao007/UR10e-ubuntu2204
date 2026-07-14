#!/usr/bin/env python3
"""Create Step5d candidate/controller manifests without entering a live gate."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from ur10e_artifact_store import artifact_store, publish_artifact
from step5d_workflow_state import (
    CURRENT,
    WorkflowStateError,
    classify_risk,
    load_json,
    sha256,
)
from ur10e_decision_manifest import build_snapshot
from ur10e_impact_selector import changed_scope, select as select_impacted


ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = (".script", ".txt", ".urp")
HEX64 = re.compile(r"[0-9a-f]{64}")
GIT_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


@dataclass(frozen=True)
class ControllerCandidatePreflight:
    program: str
    candidate_manifest_path: Path
    candidate_manifest_sha256: str
    compact_current_sha256: str
    package_source: Path
    package_sha256: dict[str, str]
    decision_digest: str
    review_status: str | None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _pointer(root: Path, path: Path) -> dict[str, str]:
    return {"path": path.relative_to(root).as_posix(), "sha256": sha256(path)}


def _triplet(local_dir: Path, program: str) -> dict[str, Path]:
    paths = {extension: local_dir / f"{program}{extension}" for extension in EXTENSIONS}
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise WorkflowStateError(f"candidate package is incomplete: {missing}")
    return paths


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _manifest_core_digest(payload: dict[str, Any]) -> str:
    core = dict(payload)
    for key in ("manifest_core_sha256", "reused", "reused_from", "reused_at"):
        core.pop(key, None)
    return _digest(core)


def _strict_string_list(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise WorkflowStateError(f"candidate validation {name} must be a unique string list")
    return value


def _validate_output_closure(validation_manifest: Path, validation: dict[str, Any]) -> set[str]:
    records = validation.get("cached_outputs")
    if not isinstance(records, list) or not records:
        raise WorkflowStateError("candidate validation output closure is missing")
    expected: set[str] = set()
    for row in records:
        if not isinstance(row, dict):
            raise WorkflowStateError("candidate validation output record is malformed")
        relative, expected_sha, expected_size = (
            row.get("path"), row.get("sha256"), row.get("size")
        )
        if (
            not isinstance(relative, str)
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or not isinstance(expected_sha, str)
            or HEX64.fullmatch(expected_sha) is None
            or not isinstance(expected_size, int)
            or expected_size < 0
            or relative in expected
        ):
            raise WorkflowStateError("candidate validation output record is malformed")
        expected.add(relative)
        path = validation_manifest.parent / relative
        if (
            not path.is_file()
            or path.stat().st_size != expected_size
            or sha256(path) != expected_sha
        ):
            raise WorkflowStateError(f"candidate validation output closure drift: {relative}")
    actual = {
        path.relative_to(validation_manifest.parent).as_posix()
        for path in validation_manifest.parent.rglob("*")
        if path.is_file() and path.resolve() != validation_manifest.resolve()
    }
    if actual != expected:
        raise WorkflowStateError("candidate validation output file set differs from manifest closure")
    return actual


def _validate_impacted_validation(
    *,
    root: Path,
    validation_manifest: Path,
    validation: dict[str, Any],
    decision: dict[str, Any],
    generated_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    if validation.get("schema_version") != "ur10e_impacted_validation_manifest_v1":
        raise WorkflowStateError("candidate validation schema is missing or unsupported")
    for key in ("source_fingerprint", "composite_fingerprint", "decision_digest"):
        if not isinstance(validation.get(key), str) or HEX64.fullmatch(validation[key]) is None:
            raise WorkflowStateError(f"candidate validation {key} must be a 64-hex digest")
    if validation.get("decision_digest") != decision.get("decision_digest"):
        raise WorkflowStateError("candidate validation decision digest drift")
    comparison = validation.get("comparison")
    if not isinstance(comparison, dict) or comparison.get("source") not in {
        "explicit", "upstream_merge_base", "workspace_head",
    }:
        raise WorkflowStateError("candidate validation has no bound base/head comparison")
    if (
        not isinstance(comparison.get("base_ref"), str)
        or not comparison["base_ref"]
        or not isinstance(comparison.get("head_ref"), str)
        or not comparison["head_ref"]
        or not isinstance(comparison.get("merge_base"), str)
        or GIT_OID.fullmatch(comparison["merge_base"]) is None
    ):
        raise WorkflowStateError("candidate validation comparison identity is incomplete")

    comparison_source = comparison["source"]
    base_ref = None if comparison_source == "workspace_head" else comparison["base_ref"]
    try:
        actual_paths, actual_comparison = changed_scope(
            root,
            base_ref=base_ref,
            head_ref=comparison["head_ref"],
        )
    except (OSError, ValueError) as exc:
        raise WorkflowStateError(f"candidate validation git comparison cannot be reproduced: {exc}") from exc
    for key in ("base_ref", "head_ref", "merge_base"):
        if actual_comparison.get(key) != comparison.get(key):
            raise WorkflowStateError(f"candidate validation git comparison drift: {key}")

    changed_paths = _strict_string_list(
        validation.get("changed_paths"), "changed_paths", nonempty=True,
    )
    expected_current_paths = sorted(set(changed_paths).union(generated_paths))
    if expected_current_paths != actual_paths:
        raise WorkflowStateError(
            "candidate validation changed_paths do not equal the authoritative git diff"
        )
    selected_tests = _strict_string_list(
        validation.get("selected_tests"), "selected_tests", nonempty=True,
    )
    skipped_tests = _strict_string_list(validation.get("skipped_tests"), "skipped_tests")
    matched_paths = _strict_string_list(
        validation.get("matched_changed_paths"), "matched_changed_paths",
    )
    unmapped_paths = _strict_string_list(
        validation.get("unmapped_changed_paths"), "unmapped_changed_paths",
    )
    if unmapped_paths or sorted(set(matched_paths).union(unmapped_paths)) != sorted(changed_paths):
        raise WorkflowStateError("candidate validation changed-path mapping is incomplete")
    if set(selected_tests).intersection(skipped_tests):
        raise WorkflowStateError("candidate validation selected/skipped tests overlap")

    dependency_hashes = validation.get("dependency_hashes")
    dependency_map_sha = validation.get("dependency_map_sha256")
    if (
        not isinstance(dependency_hashes, dict)
        or not dependency_hashes
        or not isinstance(dependency_map_sha, str)
        or HEX64.fullmatch(dependency_map_sha) is None
    ):
        raise WorkflowStateError("candidate validation dependency closure is incomplete")
    for relative, expected_sha in dependency_hashes.items():
        if relative in generated_paths:
            continue
        if (
            not isinstance(relative, str)
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or not isinstance(expected_sha, str)
            or HEX64.fullmatch(expected_sha) is None
            or not (root / relative).is_file()
            or sha256(root / relative) != expected_sha
        ):
            raise WorkflowStateError(f"candidate validation dependency drift: {relative}")
    dependency_map = root / "config/ur10e_test_dependency_map_v1.json"
    if not dependency_map.is_file() or sha256(dependency_map) != dependency_map_sha:
        raise WorkflowStateError("candidate validation dependency-map digest drift")
    dependency_map_payload = load_json(dependency_map)
    try:
        authoritative_selection = select_impacted(
            root=root,
            paths=changed_paths,
            dependency_map=dependency_map,
            full_suite=False,
            comparison=comparison,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise WorkflowStateError(
            f"candidate validation test selection cannot be reproduced: {exc}"
        ) from exc
    authoritative_fields = (
        "dependency_map_sha256",
        "changed_paths",
        "selected_tests",
        "skipped_tests",
        "matched_changed_paths",
        "unmapped_changed_paths",
        "dependency_hashes",
        "source_fingerprint",
        "full_suite",
        "comparison",
    )
    if any(
        validation.get(key) != authoritative_selection.get(key)
        for key in authoritative_fields
    ):
        raise WorkflowStateError(
            "candidate validation test selection differs from the authoritative dependency map"
        )
    fingerprint_payload = {
        "dependency_map_sha256": dependency_map_sha,
        "changed_paths": sorted(changed_paths),
        "selected_tests": selected_tests,
        "skipped_tests": skipped_tests,
        "matched_changed_paths": sorted(matched_paths),
        "unmapped_changed_paths": unmapped_paths,
        "dependency_hashes": dependency_hashes,
        "full_suite": validation.get("full_suite"),
        "comparison": comparison,
    }
    if _digest(fingerprint_payload) != validation["source_fingerprint"]:
        raise WorkflowStateError("candidate validation source fingerprint is invalid")

    source_bindings = decision.get("source_bindings")
    environment_binding = validation.get("environment_binding")
    if (
        not isinstance(source_bindings, dict)
        or not isinstance(environment_binding, dict)
        or not environment_binding
        or environment_binding.get("schema_version") != "ur10e_validation_environment_binding_v1"
        or not isinstance(environment_binding.get("fixture_declaration_present"), bool)
        or not isinstance(environment_binding.get("external_fixture_closure"), dict)
        or not isinstance(environment_binding.get("python"), dict)
        or not isinstance(environment_binding["python"].get("sha256"), str)
        or HEX64.fullmatch(environment_binding["python"]["sha256"]) is None
        or not isinstance(environment_binding.get("git_head"), dict)
        or not isinstance(environment_binding.get("git_tree"), dict)
        or any(
            not isinstance(source_bindings.get(key), str)
            or HEX64.fullmatch(source_bindings[key]) is None
            for key in ("current_stage_sha256", "stage_table_sha256")
        )
    ):
        raise WorkflowStateError("candidate validation composite binding inputs are incomplete")
    expected_composite = _digest({
        "source_fingerprint": validation["source_fingerprint"],
        "decision_digest": validation["decision_digest"],
        "current_stage_sha256": source_bindings["current_stage_sha256"],
        "stage_table_sha256": source_bindings["stage_table_sha256"],
        "execution_scope": "full" if validation.get("full_suite") is True else "impacted",
        "environment_binding": environment_binding,
    })
    if validation["composite_fingerprint"] != expected_composite:
        raise WorkflowStateError("candidate validation composite fingerprint is invalid")

    closed_outputs = _validate_output_closure(validation_manifest, validation)
    required_metadata_outputs = {"test_selection.json", "user_decision_manifest.json"}
    if not required_metadata_outputs.issubset(closed_outputs):
        raise WorkflowStateError("candidate validation selection/decision evidence is missing")
    selection = load_json(validation_manifest.parent / "test_selection.json")
    if selection != authoritative_selection:
        raise WorkflowStateError("candidate validation test-selection evidence drift")
    recorded_decision = load_json(validation_manifest.parent / "user_decision_manifest.json")
    if recorded_decision != decision:
        raise WorkflowStateError("candidate validation decision snapshot drift")

    results = validation.get("results")
    if not isinstance(results, list) or not results:
        raise WorkflowStateError("candidate validation result closure is missing")
    command_tokens: set[str] = set()
    names: set[str] = set()
    for row in results:
        if (
            not isinstance(row, dict)
            or row.get("status") != "passed"
            or row.get("exit_code") != 0
            or not isinstance(row.get("name"), str)
            or not row["name"]
            or row["name"] in names
            or not isinstance(row.get("command"), list)
            or any(not isinstance(token, str) for token in row["command"])
        ):
            raise WorkflowStateError("candidate validation contains a failed or malformed result")
        names.add(row["name"])
        command_tokens.update(row["command"])
        expected_logs = {f"{row['name']}.stdout.log", f"{row['name']}.stderr.log"}
        if not expected_logs.issubset(closed_outputs):
            raise WorkflowStateError(
                f"candidate validation result logs are incomplete: {row['name']}"
            )
    missing_tests = sorted(set(selected_tests) - command_tokens)
    if missing_tests:
        raise WorkflowStateError(
            f"candidate validation results do not execute selected tests: {missing_tests}"
        )
    validators = _strict_string_list(
        dependency_map_payload.get("validators"), "dependency_map.validators",
    )
    missing_validators = sorted(set(validators) - command_tokens)
    if missing_validators:
        raise WorkflowStateError(
            f"candidate validation results do not execute validators: {missing_validators}"
        )
    if validation.get("manifest_core_sha256") != _manifest_core_digest(validation):
        raise WorkflowStateError("candidate validation manifest core digest is invalid")
    return comparison


def create_candidate(
    *, root: Path, program: str, parent_program: str, local_dir: Path,
    validation_manifest: Path, semantic_delta: str, changed_contracts: list[str],
    owners: list[str], no_upload: bool,
) -> Path:
    validation = load_json(validation_manifest)
    if validation.get("passed") is not True or validation.get("full_suite") is not False:
        raise WorkflowStateError("candidate requires passed impacted validation, not a full-suite substitute")
    if validation.get("unmapped_changed_paths"):
        raise WorkflowStateError("candidate validation contains unmapped changes")
    if (
        validation.get("reused") is not False
        or validation.get("cache_reuse_acceptance_eligible") is not False
        or validation.get("cache_role") != "development_acceleration_only"
    ):
        raise WorkflowStateError(
            "candidate evidence must be a fresh non-reused development-only validation run"
        )
    decision = build_snapshot(root=root)
    comparison = _validate_impacted_validation(
        root=root,
        validation_manifest=validation_manifest,
        validation=validation,
        decision=decision,
    )
    package = _triplet(local_dir, program)
    package_sha = {extension: sha256(path) for extension, path in package.items()}
    risk = classify_risk(
        changed_contracts=changed_contracts,
        owners=owners,
        impact_known=True,
    )
    manifest = {
        "schema_version": "step5d_candidate_manifest_v1",
        "program": program,
        "parent_program": parent_program,
        "state": "candidate",
        "decision_digest": decision["decision_digest"],
        "semantic_delta": semantic_delta,
        "risk": risk,
        "source_fingerprint": validation.get("source_fingerprint"),
        "package_sha256": package_sha,
        "package_source": local_dir.resolve().as_posix(),
        "numeric_sanity": {
            "nonempty_triplet": all(path.stat().st_size > 0 for path in package.values()),
            "passed": True,
        },
        "impacted_validation": {
            "path": validation_manifest.resolve().as_posix(),
            "sha256": sha256(validation_manifest),
            "selected_tests": validation.get("selected_tests", []),
            "comparison": comparison,
            "passed": True,
            "full_suite": False,
            "reused": validation.get("reused") is True,
            "cache_reuse_acceptance_eligible": (
                validation.get("cache_reuse_acceptance_eligible") is True
            ),
        },
        "review": {
            "requirement": risk["review_gate"],
            "status": "not_required" if risk["risk_class"] == "low" else "pending_once",
        },
        "upload_policy": "explicit_no_upload" if no_upload else "serialized_upload_fresh_readback_default",
    }
    manifest_path = root / f"config/step5d/manifests/{program}/candidate.json"
    write_json(manifest_path, manifest)
    current = {
        "schema_version": "step5d_current_stage_v2",
        "program": program,
        "state": "candidate",
        "decision_digest": decision["decision_digest"],
        "manifests": {
            "candidate": _pointer(root, manifest_path),
            "controller_verification": None,
            "live_ready": None,
        },
        "artifact_locator": None,
        "execution": {
            "live_authorized": False,
            "live_running": False,
        },
        "claims": {
            "new_version_complete": False,
            "controller_verified": False,
            "live_ready": False,
            "live_accepted": False,
        },
    }
    write_json(root / CURRENT, current)
    return manifest_path


def _store_readback(
    *, root: Path, program: str, manifest_path: Path, store: Path,
) -> tuple[Path, dict[str, str]]:
    rows = []
    roles: dict[str, str] = {}
    sources = [("controller_readback_manifest", manifest_path)]
    for extension in EXTENSIONS:
        sources.append((f"controller_readback_{extension[1:]}", manifest_path.parent / f"{program}{extension}"))
    for role, source in sources:
        if not source.is_file():
            raise WorkflowStateError(f"fresh read-back artifact is missing: {source}")
        ref = publish_artifact(source, store=store)
        try:
            original = source.resolve().relative_to(root).as_posix()
        except ValueError:
            original = source.resolve().as_posix()
        rows.append({
            "logical_role": role,
            "original_path": original,
            **ref.as_dict(),
        })
        roles[role] = ref.sha256
    locator = {
        "schema_version": "ur10e_artifact_locator_v1",
        "program": program,
        "store_layout": "sha256/<digest>",
        "destructive_migration_performed": False,
        "artifacts": rows,
    }
    locator_path = root / f"config/step5d/artifact_locators/{program}.json"
    write_json(locator_path, locator)
    return locator_path, roles


def preflight_controller_candidate(
    *,
    root: Path,
    program: str,
    local_dir: Path | None,
    review_status: str | None = None,
) -> ControllerCandidatePreflight:
    """Validate every deterministic candidate blocker before controller mutation."""

    root = root.resolve()
    local_dir = local_dir.resolve() if local_dir is not None else None
    current = load_json(root / CURRENT)
    if current.get("program") != program or current.get("state") != "candidate":
        raise WorkflowStateError("controller promotion requires the exact current candidate")
    candidate_pointer = current.get("manifests", {}).get("candidate") or {}
    relative = candidate_pointer.get("path")
    if not isinstance(relative, str):
        raise WorkflowStateError("candidate manifest pointer is missing")
    candidate_path = (root / relative).resolve()
    try:
        candidate_path.relative_to(root)
    except ValueError as exc:
        raise WorkflowStateError("candidate manifest pointer escapes the experiment root") from exc
    if not candidate_path.is_file() or sha256(candidate_path) != candidate_pointer.get("sha256"):
        raise WorkflowStateError("candidate manifest pointer drift")
    candidate = load_json(candidate_path)
    if (
        candidate.get("schema_version") != "step5d_candidate_manifest_v1"
        or candidate.get("program") != program
        or candidate.get("state") != "candidate"
        or candidate.get("decision_digest") != current.get("decision_digest")
    ):
        raise WorkflowStateError("candidate manifest identity drift")
    decision = build_snapshot(root=root)
    if decision.get("decision_digest") != candidate.get("decision_digest"):
        raise WorkflowStateError("candidate decision digest drift before controller mutation")
    if candidate.get("upload_policy") == "explicit_no_upload":
        raise WorkflowStateError("explicit_no_upload candidate cannot be controller-promoted")
    package_source = candidate.get("package_source")
    if not isinstance(package_source, str):
        raise WorkflowStateError("candidate package_source is missing")
    frozen_package_source = Path(package_source).resolve()
    if local_dir is not None and frozen_package_source != local_dir:
        raise WorkflowStateError("transaction local-dir differs from the frozen candidate package source")
    candidate_files = _triplet(frozen_package_source, program)
    actual_candidate_sha = {
        extension: sha256(path) for extension, path in candidate_files.items()
    }
    declared_sha = candidate.get("package_sha256")
    if (
        not isinstance(declared_sha, dict)
        or set(declared_sha) != set(EXTENSIONS)
        or any(not isinstance(value, str) or HEX64.fullmatch(value) is None for value in declared_sha.values())
        or actual_candidate_sha != declared_sha
    ):
        raise WorkflowStateError("candidate package bytes changed after candidate freeze")
    validation = candidate.get("impacted_validation") or {}
    validation_path_value = validation.get("path")
    validation_path = Path(validation_path_value).resolve() if isinstance(validation_path_value, str) else None
    if (
        validation_path is None
        or not validation_path.is_file()
        or sha256(validation_path) != validation.get("sha256")
        or validation.get("passed") is not True
        or validation.get("full_suite") is not False
        or validation.get("reused") is not False
        or validation.get("cache_reuse_acceptance_eligible") is not False
    ):
        raise WorkflowStateError("candidate impacted-validation evidence drift")
    validation_payload = load_json(validation_path)
    generated_paths = (
        CURRENT.as_posix(),
        candidate_path.relative_to(root).as_posix(),
    )
    _validate_impacted_validation(
        root=root,
        validation_manifest=validation_path,
        validation=validation_payload,
        decision=decision,
        generated_paths=generated_paths,
    )
    review = candidate.get("review") or {}
    if review.get("requirement") != "0+0" and review_status not in {
        "accepted_1+1", "skipped_unavailable_1+0",
    }:
        raise WorkflowStateError(
            "high-risk controller promotion requires one settled 1+1/1+0 review disposition"
        )
    return ControllerCandidatePreflight(
        program=program,
        candidate_manifest_path=candidate_path,
        candidate_manifest_sha256=sha256(candidate_path),
        compact_current_sha256=sha256(root / CURRENT),
        package_source=frozen_package_source,
        package_sha256=actual_candidate_sha,
        decision_digest=str(candidate["decision_digest"]),
        review_status=review_status,
    )


def verify_frozen_candidate(
    *, root: Path, frozen: ControllerCandidatePreflight,
) -> tuple[dict[str, Any], Path, dict[str, Any], dict[str, str]]:
    """Recheck immutable candidate/package bytes after expected legacy-state mutation."""

    root = root.resolve()
    current_path = root / CURRENT
    if not current_path.is_file() or sha256(current_path) != frozen.compact_current_sha256:
        raise WorkflowStateError("compact candidate state drifted during controller transaction")
    current = load_json(current_path)
    if current.get("program") != frozen.program or current.get("state") != "candidate":
        raise WorkflowStateError("frozen transaction no longer points to the exact candidate")
    candidate_path = frozen.candidate_manifest_path.resolve()
    if not candidate_path.is_file() or sha256(candidate_path) != frozen.candidate_manifest_sha256:
        raise WorkflowStateError("candidate manifest drifted during controller transaction")
    candidate = load_json(candidate_path)
    if candidate.get("decision_digest") != frozen.decision_digest:
        raise WorkflowStateError("candidate decision identity drifted during controller transaction")
    actual_candidate_sha = {
        extension: sha256(path)
        for extension, path in _triplet(frozen.package_source, frozen.program).items()
    }
    if actual_candidate_sha != frozen.package_sha256:
        raise WorkflowStateError("candidate package bytes drifted during controller transaction")
    return current, candidate_path, candidate, actual_candidate_sha


def preflight_fresh_readback(
    *,
    program: str,
    candidate_sha: dict[str, str],
    readback_manifest: Path,
) -> tuple[dict[str, Any], str]:
    """Validate the exact fresh readback without publishing workflow state."""

    readback = load_json(readback_manifest)
    hashes = readback.get("sha256") or {}
    if readback.get("status") != "controller read-back verified":
        raise WorkflowStateError("controller read-back status is not verified")
    if readback.get("delivery_mode") != "full_upload_readback":
        raise WorkflowStateError("controller promotion requires a fresh full upload/read-back transaction")
    if readback.get("readback_source") != "fresh_controller_get":
        raise WorkflowStateError("controller promotion requires fresh controller get evidence")
    controller_identity = readback.get("controller")
    target_dir = readback.get("target_dir")
    if (
        not isinstance(controller_identity, str)
        or not controller_identity
        or not isinstance(target_dir, str)
        or not target_dir.startswith("/programs/")
    ):
        raise WorkflowStateError("controller read-back target identity is incomplete")
    transaction_id = readback.get("upload_transaction_id")
    if not isinstance(transaction_id, str) or re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None:
        raise WorkflowStateError("controller promotion requires an exact upload transaction identity")
    if not hashes.get("local") or not (
        hashes.get("local") == hashes.get("controller") == hashes.get("readback")
    ):
        raise WorkflowStateError("controller read-back semantic closure mismatch")
    readback_files = _triplet(readback_manifest.parent, program)
    actual_readback_sha = {
        extension: sha256(path) for extension, path in readback_files.items()
    }
    if hashes["local"] != candidate_sha:
        raise WorkflowStateError("controller read-back package differs from candidate")
    if hashes["readback"] != actual_readback_sha:
        raise WorkflowStateError("fresh read-back bytes differ from read-back manifest")
    validation = readback.get("validation") or {}
    validation_keys = {
        ".script": "script_sha256",
        ".txt": "txt_sha256",
        ".urp": "urp_sha256",
    }
    if (
        validation.get("program") != program
        or validation.get("target_dir") != target_dir
        or validation.get("script_node_path") != f"{target_dir}/{program}.script"
        or any(validation.get(key) != candidate_sha[extension] for extension, key in validation_keys.items())
    ):
        raise WorkflowStateError("controller read-back validation identity differs from candidate")
    return readback, transaction_id


def promote_controller_verified(
    *, root: Path, program: str, readback_manifest: Path,
    store: Path, review_status: str | None = None,
    candidate_preflight: ControllerCandidatePreflight | None = None,
) -> Path:
    if candidate_preflight is None:
        candidate_preflight = preflight_controller_candidate(
            root=root,
            program=program,
            local_dir=None,
            review_status=review_status,
        )
    elif candidate_preflight.program != program or candidate_preflight.review_status != review_status:
        raise WorkflowStateError("controller transaction preflight identity mismatch")
    current, candidate_path, candidate, actual_candidate_sha = verify_frozen_candidate(
        root=root,
        frozen=candidate_preflight,
    )
    readback, transaction_id = preflight_fresh_readback(
        program=program,
        candidate_sha=actual_candidate_sha,
        readback_manifest=readback_manifest,
    )

    locator_path, artifact_roles = _store_readback(
        root=root, program=program, manifest_path=readback_manifest, store=store,
    )
    controller_manifest = {
        "schema_version": "step5d_controller_verification_manifest_v1",
        "program": program,
        "state": "controller_verified",
        "decision_digest": candidate["decision_digest"],
        "candidate_manifest": _pointer(root, candidate_path),
        "controller_identity": readback.get("controller"),
        "controller_target": f"{readback.get('target_dir')}/{program}.urp",
        "upload_transaction": {
            "id": transaction_id,
            "mode": "full_upload_readback",
            "serialized": True,
            "program_start_performed": False,
            "bridge_start_performed": False,
        },
        "fresh_readback_verified": True,
        "readback": {
            "artifact_roles": artifact_roles,
            "package_hashes_match": True,
        },
        "review_status": review_status or "not_required_0+0",
        "claim_boundary": "controller package and fresh read-back verified; live_ready=false",
    }
    controller_path = root / f"config/step5d/manifests/{program}/controller_verification.json"
    write_json(controller_path, controller_manifest)
    current.update({
        "state": "controller_verified",
        "artifact_locator": _pointer(root, locator_path),
        "claims": {
            "new_version_complete": True,
            "controller_verified": True,
            "live_ready": False,
            "live_accepted": False,
        },
    })
    current["manifests"]["controller_verification"] = _pointer(root, controller_path)
    write_json(root / CURRENT, current)
    return controller_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    candidate = subparsers.add_parser("candidate")
    candidate.add_argument("--root", type=Path, default=ROOT)
    candidate.add_argument("--program", required=True)
    candidate.add_argument("--parent-program", required=True)
    candidate.add_argument("--local-dir", type=Path, required=True)
    candidate.add_argument("--validation-manifest", type=Path, required=True)
    candidate.add_argument("--semantic-delta", required=True)
    candidate.add_argument("--changed-contract", action="append", default=[])
    candidate.add_argument("--owner", action="append", required=True)
    candidate.add_argument("--no-upload", action="store_true")

    controller = subparsers.add_parser("controller-verified")
    controller.add_argument("--root", type=Path, default=ROOT)
    controller.add_argument("--program", required=True)
    controller.add_argument("--readback-manifest", type=Path, required=True)
    controller.add_argument(
        "--artifact-store", type=Path,
        help="override the default Git-common-dir artifact store",
    )
    controller.add_argument(
        "--review-status", choices=("accepted_1+1", "skipped_unavailable_1+0"),
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.mode == "candidate":
        output = create_candidate(
            root=root, program=args.program, parent_program=args.parent_program,
            local_dir=args.local_dir.resolve(), validation_manifest=args.validation_manifest.resolve(),
            semantic_delta=args.semantic_delta, changed_contracts=args.changed_contract,
            owners=args.owner, no_upload=args.no_upload,
        )
    else:
        output = promote_controller_verified(
            root=root, program=args.program, readback_manifest=args.readback_manifest.resolve(),
            store=artifact_store(root, args.artifact_store), review_status=args.review_status,
        )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
