#!/usr/bin/env python3
"""Validate normalized cross-step stage-table maintenance fields."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from build_step5d_v29_review_state_projection import build as build_v29_review_projection
from build_step5d_p0_v8_offline_diagnostic import (
    validate_diagnostic as validate_p0_offline_diagnostic,
    validate_state_binding as validate_p0_offline_state_binding,
)
from step5d_review_v2 import full_review_index_projection_sha256
from tase_protocol_table import resolve_experiment_profile


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_EXTENSIONS = (".script", ".txt", ".urp")
P0_V8_PROGRAM = "step5d_strict_rnn_no_contact_p0_v8"
V29_PROGRAM = "step5d_strict_rnn_ablation_v29"
V30_PROGRAM = "step5d_strict_rnn_ablation_v30"
REVIEW_POLICY_ID = "ur10e_review_policy_v2"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dotted_get(payload: dict[str, Any], dotted: str) -> Any:
    value: Any = payload
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(dotted)
        value = value[part]
    return value


def stage_by_id(table: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("id")): row for row in table.get("stages", [])}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_local_triplet(
    root: Path,
    *,
    label: str,
    delivery: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    triplet = delivery.get("local_triplet")
    hashes = delivery.get("sha256") or {}
    if not isinstance(triplet, str) or not triplet:
        return [f"{label} local_triplet is missing"]
    for ext in PACKAGE_EXTENSIONS:
        path = root / f"{triplet}{ext}"
        expected_hash = hashes.get(ext)
        if not path.is_file():
            failures.append(f"{label} local package file is missing: {path.relative_to(root)}")
        elif not is_sha256(expected_hash) or file_sha256(path) != expected_hash:
            failures.append(f"{label} local package sha mismatch for {ext}")
    return failures


def _validate_manifest_bound_delivery(
    root: Path,
    *,
    label: str,
    delivery: dict[str, Any],
) -> tuple[str, list[str]]:
    """Accept one of two complete states: local-only, or uploaded+read back.

    A partially populated controller claim is rejected.  The delivered state is
    bound to the tracked triplet through the read-back manifest and the copied
    read-back files; it does not imply current/live/bridge authorization.
    """

    failures: list[str] = []
    target = delivery.get("controller_target")
    uploaded = delivery.get("controller_uploaded")
    verified = delivery.get("controller_readback_verified")
    manifest_rel = delivery.get("controller_readback_manifest")
    offline = (
        target is None
        and uploaded is False
        and verified is False
        and manifest_rel in (None, "")
    )
    delivered = (
        isinstance(target, str)
        and bool(target)
        and uploaded is True
        and verified is True
        and isinstance(manifest_rel, str)
        and bool(manifest_rel)
    )
    if offline:
        return "offline", failures
    if not delivered:
        failures.append(
            f"{label} controller delivery must be entirely offline or a complete manifest-bound upload+readback"
        )
        return "invalid", failures

    program = delivery.get("program_basename")
    expected_target = (
        str(PurePosixPath(str(target)).parent / f"{program}.urp")
        if isinstance(program, str) and program
        else None
    )
    if target != expected_target:
        failures.append(f"{label} controller target does not match its program basename")
    manifest_path = root / str(manifest_rel)
    if not manifest_path.is_file():
        failures.append(f"{label} controller readback manifest is missing")
        return "delivered", failures
    try:
        manifest = load_json(manifest_path)
    except (OSError, json.JSONDecodeError):
        failures.append(f"{label} controller readback manifest is invalid JSON")
        return "delivered", failures

    target_dir = str(PurePosixPath(str(target)).parent)
    validation = manifest.get("validation") or {}
    if manifest.get("status") != "controller read-back verified":
        failures.append(f"{label} controller readback manifest status is not verified")
    if manifest.get("target_dir") != target_dir:
        failures.append(f"{label} controller readback manifest target_dir mismatch")
    if validation.get("program") != program or validation.get("target_dir") != target_dir:
        failures.append(f"{label} controller readback manifest program/target binding mismatch")
    target_resolution = manifest.get("target_resolution") or {}
    if target_resolution and target_resolution.get("controller_target") != target:
        failures.append(f"{label} controller readback manifest target-resolution mismatch")
    hashes = delivery.get("sha256") or {}
    manifest_hashes = manifest.get("sha256") or {}
    for channel in ("local", "controller", "readback"):
        if manifest_hashes.get(channel) != hashes:
            failures.append(f"{label} controller readback manifest {channel} sha mismatch")
    for ext in PACKAGE_EXTENSIONS:
        readback_file = manifest_path.parent / f"{program}{ext}"
        if not readback_file.is_file():
            failures.append(f"{label} controller readback file is missing for {ext}")
        elif file_sha256(readback_file) != hashes.get(ext):
            failures.append(f"{label} controller readback file sha mismatch for {ext}")
    return "delivered", failures


def _validate_review_v2_sources(
    root: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    failures: list[str] = []
    policy_path = root / "config" / "step5d_review_policy_v2.json"
    index_path = root / "config" / "step5d_review_index_v2.json"
    if not policy_path.is_file():
        failures.append("Review v2 policy file is missing")
        policy: dict[str, Any] = {}
    else:
        policy = load_json(policy_path)
        if (
            policy.get("schema_version") != REVIEW_POLICY_ID
            or policy.get("policy_id") != REVIEW_POLICY_ID
        ):
            failures.append("Review v2 policy schema/policy_id mismatch")
        codex_defaults = (policy.get("defaults") or {}).get("codex") or {}
        if (
            codex_defaults.get("model") != "gpt-5.6-sol"
            or codex_defaults.get("reasoning_effort") != "high"
        ):
            failures.append("Review v2 Codex default must be gpt-5.6-sol/high")
        expected_stacks = {
            "ordinary_development": "0+0",
            "ordinary_handoff": "0+0",
            "direction_change": "1+0",
            "p0_v8_pre_live": "1+1",
            "v29_baseline_re_review": "1+0",
            "v29_contact_pre_live": "2+1",
            "v30_contact_pre_live": "2+1",
            "p0_v8_post_run": "1+0",
            "contact_post_run": "1+1",
        }
        classes = policy.get("review_classes") or {}
        for class_id, stack in expected_stacks.items():
            if (classes.get(class_id) or {}).get("stack") != stack:
                failures.append(f"Review v2 stack mismatch: {class_id}")
        execution = policy.get("execution") or {}
        if (
            execution.get("evidence_freeze_required") is not True
            or execution.get("full_review_limit_per_composite_fingerprint") != 1
            or execution.get("ordinary_work_produces_reviewer_invocation") is not False
        ):
            failures.append("Review v2 evidence-freeze/single-review/ordinary-0+0 policy mismatch")
    if not index_path.is_file():
        failures.append("Review v2 index file is missing")
        index: dict[str, Any] = {}
    else:
        index = load_json(index_path)
        if (
            index.get("schema_version") != "ur10e_review_index_v2"
            or index.get("policy_id") != REVIEW_POLICY_ID
        ):
            failures.append("Review v2 index schema/policy_id mismatch")
        if index.get("blockers") or index.get("duplicate_full_review_fingerprints"):
            failures.append("Review v2 index contains duplicate/full-review blockers")
        historical = index.get("historical_artifacts") or []
        expected_history = {
            "config/step5d_v30_milestone_reviews.json",
            "config/step5d_v29_imported_evidence_manifest.json",
            "config/step5d_v29_remote_evidence_sha256.json",
        }
        actual_history = {
            str(item.get("path")) for item in historical if isinstance(item, dict)
        }
        if actual_history != expected_history:
            failures.append("Review v2 historical evidence set is incomplete or stale")
        for item in historical:
            if not isinstance(item, dict):
                failures.append("Review v2 historical evidence entry is invalid")
                continue
            path = root / str(item.get("path") or "")
            if (
                item.get("immutable") is not True
                or item.get("counts_as_review_v2") is not False
                or item.get("status") != "historical_superseded_by_review_policy_v2"
            ):
                failures.append(f"Review v2 historical evidence flags are invalid: {item.get('path')}")
            if not path.is_file():
                failures.append(f"Review v2 historical evidence file is missing: {item.get('path')}")
            elif (
                file_sha256(path) != item.get("sha256")
                or path.stat().st_size != item.get("bytes")
            ):
                failures.append(
                    f"Review v2 historical evidence hash/size mismatch: {item.get('path')}"
                )
        for item in index.get("v2_reviews") or []:
            if not isinstance(item, dict):
                failures.append("Review v2 manifest index entry is invalid")
                continue
            path = root / str(item.get("path") or "")
            if not path.is_file():
                failures.append(f"Review v2 manifest is missing: {item.get('path')}")
            elif (
                file_sha256(path) != item.get("sha256")
                or path.stat().st_size != item.get("bytes")
            ):
                failures.append(
                    f"Review v2 manifest hash/size mismatch: {item.get('path')}"
                )
    return policy, index, failures


def targets_bridge_startup_policy(ref: Any) -> bool:
    text = str(ref or "")
    return text == "bridge_startup_policy" or text.endswith("#bridge_startup_policy")


def triggerable_bridge_row_requires_startup_policy(row: dict[str, Any]) -> bool:
    operator = row.get("operator_lifecycle")
    return row.get("bridge") is True and isinstance(operator, dict) and bool(operator.get("mode"))


def derived_bridge_startup_policy_stage_ids(*tables: dict[str, Any]) -> list[str]:
    stage_ids: list[str] = []
    for table in tables:
        for row in table.get("stages", []):
            refs = row.get("policy_refs")
            if isinstance(refs, dict) and targets_bridge_startup_policy(refs.get("bridge_startup_policy")):
                stage_ids.append(str(row.get("id")))
    return stage_ids


def ref_exists(root: Path, table: dict[str, Any], ref: str) -> bool:
    if not ref:
        return False
    if "#" in ref:
        path_text, anchor = ref.split("#", 1)
        path = root / path_text
        if not path.is_file():
            return False
        if path.suffix == ".json":
            try:
                payload = load_json(path)
            except Exception:
                return False
            if anchor in payload:
                return True
            if "." in anchor:
                try:
                    dotted_get(payload, anchor)
                    return True
                except KeyError:
                    return False
            return True
        return True
    if ref in table:
        return True
    if "." in ref:
        try:
            dotted_get(table, ref)
            return True
        except KeyError:
            pass
    return (root / ref).is_file()


def validate(root: Path = EXPERIMENT_ROOT) -> list[str]:
    failures: list[str] = []
    step5 = load_json(root / "config" / "step5_stage_table.json")
    step6 = load_json(root / "config" / "step6_stage_table.json")
    current = load_json(root / "config" / "current_stage.json")
    protocol = load_json(root / "config" / "tase_protocol_table.json")
    step5_rows = stage_by_id(step5)
    step6_rows = stage_by_id(step6)
    _review_policy, review_index, review_source_failures = _validate_review_v2_sources(root)
    failures.extend(review_source_failures)
    current_stage_id = current.get("current_stage_id")
    current_program = current.get("program")
    current_target = current.get("controller_target")
    canonical_step5d = protocol.get("experiment_profiles", {}).get("Step5.step5d_rnn", {})
    if canonical_step5d.get("current_program") != current_program:
        failures.append("canonical Step5d current_program does not match current_stage.json")
    flow_path = root / "STEP5_FLOW.md"
    if not flow_path.is_file():
        failures.append("STEP5_FLOW.md is missing")
    else:
        flow_summary = "\n".join(flow_path.read_text(encoding="utf-8").splitlines()[:30])
        selected_match = re.search(r"currently selects\s+`([^`]+)`", flow_summary)
        if selected_match is None or selected_match.group(1) != str(current_stage_id):
            failures.append("STEP5_FLOW current summary does not match current_stage.json")

    profile_ref = step5.get("bridge_startup_policy", {}).get("startup_gate_profile_ref")
    if profile_ref != "startup_gate_profiles.prepared_fast_bridge_v1":
        failures.append("bridge_startup_policy.startup_gate_profile_ref must use prepared_fast_bridge_v1")
    try:
        profile = dotted_get(step5, str(profile_ref))
    except KeyError:
        failures.append(f"startup gate profile is missing: {profile_ref}")
        profile = {}
    for gate in profile.get("gates", []):
        if gate.get("liveness_required") is True and gate.get("cacheable") is True:
            failures.append(f"live gate must not be cacheable: {gate.get('id')}")

    applies_to = set(step5.get("bridge_startup_policy", {}).get("applies_to_stage_ids", []))
    derived_applies_to = set(derived_bridge_startup_policy_stage_ids(step5, step6))
    if applies_to != derived_applies_to:
        missing = sorted(derived_applies_to - applies_to)
        extra = sorted(applies_to - derived_applies_to)
        if missing:
            failures.append("bridge_startup_policy.applies_to_stage_ids missing derived refs: " + ", ".join(missing))
        if extra:
            failures.append("bridge_startup_policy.applies_to_stage_ids has stale refs: " + ", ".join(extra))

    for table_name, table, rows in (("step5", step5, step5_rows), ("step6", step6, step6_rows)):
        for row_id, row in rows.items():
            refs = row.get("policy_refs")
            if triggerable_bridge_row_requires_startup_policy(row) and not (
                isinstance(refs, dict) and targets_bridge_startup_policy(refs.get("bridge_startup_policy"))
            ):
                failures.append(f"{table_name}:{row_id} triggerable bridge row missing bridge_startup_policy ref")
            if not refs:
                continue
            for key, ref in refs.items():
                if not ref_exists(root, table, str(ref)):
                    failures.append(f"{table_name}:{row_id} policy_refs.{key} does not resolve: {ref}")

    current_row = step5_rows.get(str(current_stage_id))
    if current_row is None:
        failures.append(f"current stage row is missing from Step5 table: {current_stage_id}")
    else:
        binding = current_row.get("current_binding", {})
        delivery = current_row.get("package_delivery", {})
        if binding.get("is_current") is not True:
            failures.append(f"current row {current_stage_id} must have current_binding.is_current=true")
        if binding.get("stage_id") != current_stage_id:
            failures.append(f"current row binding stage_id mismatch: {binding.get('stage_id')} != {current_stage_id}")
        if binding.get("program") != current_program:
            failures.append(f"current row binding program mismatch: {binding.get('program')} != {current_program}")
        if binding.get("controller_target") != current_target:
            failures.append("current row binding controller_target does not match current_stage.json")
        if delivery.get("program_basename") != current_program:
            failures.append("current row package_delivery.program_basename does not match current_stage.json")
        if delivery.get("controller_target") != current_target:
            failures.append("current row package_delivery.controller_target does not match current_stage.json")
        if delivery.get("controller_readback_manifest") != current.get("controller_readback_manifest"):
            failures.append("current row controller readback manifest does not match current_stage.json")
        current_sha = current.get("sha256") or {}
        delivery_sha = delivery.get("sha256") or {}
        for ext in (".script", ".txt", ".urp"):
            if delivery_sha.get(ext) != current_sha.get(ext):
                failures.append(f"current row package sha mismatch for {ext}")
        current_mode = current_row.get("guard", {}).get("stage25_default_control_mode")
        if canonical_step5d.get("stage25_default_control_mode") != current_mode:
            failures.append("canonical Step5d control mode does not match current stage row")
        if str(current_stage_id) == "step5d_strict_rnn_ablation_v29":
            acceptance = current_row.get("acceptance") or {}
            local_delivery = current_row.get("local_delivery_evidence") or {}
            candidate = current.get("v29_contact_candidate") or {}
            if acceptance.get("controller_readback_verified") is not True:
                failures.append("current v29 acceptance must mark controller readback verified")
            if local_delivery.get("controller_readback_verified") is not True:
                failures.append("current v29 local delivery must mark controller readback verified")
            if candidate.get("controller_readback_verified") is not True:
                failures.append("current v29 candidate must mark controller readback verified")
            expected_profile = {
                "backend": "cupy",
                "inner_iterations": 1024,
                "epsilon": 0.01,
                "sigr_exponent_r": 0.8,
                "qdot_cap_rad_s": 0.05,
                "control_mode": "speedj_rnn_live",
                "joint_layout_code": 524.0,
            }
            if current_row.get("runtime_profile") != expected_profile:
                failures.append("current v29 runtime profile does not match the pinned live-prep profile")
            expected_states = {
                "liveprep_status": {"blocked", "awaiting_live_authorization"},
                "live_run_status": {"not_started", "running", "accepted"},
                "reproduction_status": {"incomplete", "complete"},
            }
            for field, allowed in expected_states.items():
                current_state = current.get(field)
                row_state = current_row.get(field)
                if not isinstance(current_state, dict) or current_state.get("state") not in allowed:
                    failures.append(f"current_stage.json {field}.state is missing or invalid")
                if not isinstance(row_state, dict) or row_state.get("state") != current_state.get("state"):
                    failures.append(f"current v29 {field}.state does not match current_stage.json")
            liveprep_state = current.get("liveprep_status", {}).get("state")
            if current_row.get("blocked") is not (liveprep_state == "blocked"):
                failures.append("current v29 blocked flag must match liveprep_status.state")
            current_liveprep = current.get("liveprep_status") or {}
            row_liveprep = current_row.get("liveprep_status") or {}
            if row_liveprep.get("readiness_artifact") != current_liveprep.get("readiness_artifact"):
                failures.append("current v29 readiness artifact pointer does not match current_stage.json")
            if liveprep_state == "awaiting_live_authorization":
                readiness_sha = current_liveprep.get("readiness_sha256")
                if (
                    not isinstance(readiness_sha, str)
                    or re.fullmatch(r"[0-9a-f]{64}", readiness_sha) is None
                    or row_liveprep.get("readiness_sha256") != readiness_sha
                ):
                    failures.append("current v29 awaiting state requires a matching readiness sha")

    v29_candidate = current.get("v29_contact_candidate") or {}
    v29_baseline_review = v29_candidate.get("review_v2_baseline_rereview") or {}
    v29_row = step5_rows.get(V29_PROGRAM) or {}
    v29_row_review = v29_row.get("review_v2") or {}
    if (
        v29_candidate.get("frozen_fallback") is not True
        or v29_baseline_review.get("required_stack") != "1+0"
    ):
        failures.append("v29 frozen fallback / Review v2 1+0 baseline binding is invalid")
    expected_v29_local = f"programs/step5/step5d/{V29_PROGRAM}"
    if (
        current.get("local_triplet") != expected_v29_local
        or v29_candidate.get("local_triplet") != expected_v29_local
    ):
        failures.append("v29 local triplet must use the canonical nested step5d path")
    expected_v29_triplet = f"{expected_v29_local}.{{script,txt,urp}}"
    for binding_name in ("local_delivery_evidence", "package_delivery"):
        binding = v29_row.get(binding_name) or {}
        if (
            binding.get("local_program_dir") != "programs/step5/step5d"
            or binding.get("local_triplet") != expected_v29_triplet
        ):
            failures.append(f"v29 {binding_name} local package path is inconsistent")
    expected_v29_review = {
        "policy_id": REVIEW_POLICY_ID,
        "milestone": "v29_baseline_re_review",
        "required_stack": "1+0",
        "status": "resolved_by_review_index",
        "evidence_frozen": True,
        "manifest_source": "config/step5d_review_index_v2.json",
        "source_review_manifest": "config/reviews/v29_baseline_review_v2_manifest.json",
    }
    for field, expected in expected_v29_review.items():
        if (
            v29_baseline_review.get(field) != expected
            or v29_row_review.get(field) != expected
        ):
            failures.append(f"v29 Review v2 externalized binding mismatch: {field}")
    source_review_path = expected_v29_review["source_review_manifest"]
    indexed_v29_source = next(
        (
            item
            for item in (review_index.get("v2_reviews") or [])
            if isinstance(item, dict) and item.get("path") == source_review_path
        ),
        None,
    )
    if (
        not indexed_v29_source
        or indexed_v29_source.get("workflow") != "v29"
        or indexed_v29_source.get("milestone") != "baseline_re_review"
        or indexed_v29_source.get("required_stack") != "1+0"
        or indexed_v29_source.get("review_mode") != "full"
    ):
        failures.append("v29 baseline source review is not bound in Review v2 index")
    indexed_v29_closer = next(
        (
            item
            for item in (review_index.get("v2_reviews") or [])
            if isinstance(item, dict)
            and item.get("path")
            == "config/reviews/v29_baseline_review_v2_closer_manifest.json"
        ),
        None,
    )
    if (
        not indexed_v29_closer
        or indexed_v29_closer.get("workflow") != "v29"
        or indexed_v29_closer.get("milestone") != "baseline_re_review"
        or indexed_v29_closer.get("required_stack") != "1+0"
        or indexed_v29_closer.get("review_mode") != "targeted_closer"
        or indexed_v29_closer.get("gate_status") != "pass"
        or indexed_v29_closer.get("blocking_open_finding_count") != 0
    ):
        failures.append("v29 baseline targeted closer is not accepted in Review v2 index")
    v29_closer_validation_path = (
        root / "config/reviews/v29_baseline_review_v2_closer_validation.json"
    )
    if not v29_closer_validation_path.is_file():
        failures.append("v29 baseline targeted closer validation is missing")
    else:
        v29_closer_validation = load_json(v29_closer_validation_path)
        if (
            v29_closer_validation.get("accepted") is not True
            or v29_closer_validation.get("blockers")
            or not indexed_v29_closer
            or v29_closer_validation.get("composite_fingerprint")
            != indexed_v29_closer.get("composite_fingerprint")
        ):
            failures.append("v29 baseline targeted closer validation/index mismatch")
    v29_projection_path = (
        root / "config/reviews/v29_baseline_review_v2_state_projection.json"
    )
    if not v29_projection_path.is_file():
        failures.append("v29 immutable Review v2 state projection is missing")
    else:
        tracked_v29_projection = load_json(v29_projection_path)
        rebuilt_v29_projection = build_v29_review_projection(root=root)
        if tracked_v29_projection != rebuilt_v29_projection:
            failures.append("v29 immutable Review v2 state projection drift")
        if rebuilt_v29_projection.get("blockers"):
            failures.append("v29 reviewed state no longer matches immutable projection")

    p0_capture = current.get("bridge_trigger", {}).get("no_contact_p0_capture", {})
    p0_profile = p0_capture.get("profile")
    if p0_profile:
        p0_row = step5_rows.get(str(p0_profile))
        if p0_row is None:
            failures.append(f"strict RNN no-contact P0 row is missing from Step5 table: {p0_profile}")
        else:
            p0_delivery = p0_row.get("package_delivery", {})
            capture_target = p0_capture.get("controller_target")
            capture_dir = str(PurePosixPath(str(capture_target)).parent) if capture_target else None
            if p0_delivery.get("program_basename") != p0_profile:
                failures.append("strict RNN no-contact P0 package_delivery.program_basename does not match current capture profile")
            if p0_delivery.get("controller_target") != capture_target:
                failures.append("strict RNN no-contact P0 package_delivery.controller_target does not match current capture pointer")
            if p0_delivery.get("controller_dir") != capture_dir:
                failures.append("strict RNN no-contact P0 package_delivery.controller_dir does not match current capture pointer")
            if p0_delivery.get("controller_readback_manifest") != p0_capture.get("controller_readback_manifest"):
                failures.append("strict RNN no-contact P0 package_delivery.controller_readback_manifest does not match current capture pointer")
            p0_capture_sha = p0_capture.get("sha256") or {}
            p0_delivery_sha = p0_delivery.get("sha256") or {}
            for ext in (".script", ".txt", ".urp"):
                if p0_delivery_sha.get(ext) != p0_capture_sha.get(ext):
                    failures.append(f"strict RNN no-contact P0 package sha mismatch for {ext}")

    p0_v8_row = step5_rows.get(P0_V8_PROGRAM)
    p0_v8_capture = current.get("bridge_trigger", {}).get("no_contact_p0_v8_capture") or {}
    p0_v8_candidate = current.get("p0_v8_candidate") or {}
    if p0_v8_row is None:
        failures.append("P0 v8 stage row is missing")
    else:
        p0_delivery = p0_v8_row.get("package_delivery") or {}
        p0_hashes = p0_delivery.get("sha256") or {}
        p0_fingerprint = p0_delivery.get("semantic_fingerprint")
        failures.extend(_validate_local_triplet(root, label="P0 v8", delivery=p0_delivery))
        p0_delivery_state, p0_delivery_failures = _validate_manifest_bound_delivery(
            root,
            label="P0 v8",
            delivery=p0_delivery,
        )
        failures.extend(p0_delivery_failures)
        expected_p0_runtime = {
            "backend": "cupy",
            "inner_iterations": 512,
            "epsilon": 0.01,
            "sigr_exponent_r": 0.8,
            "qdot_cap_rad_s": 0.05,
            "control_mode": "speedj_rnn_live",
            "joint_layout_code": 524.0,
            "effective_ko": 0.01,
            "normal_contract": "n_reaction = -n_approach",
            "solver_ok_status": 40.0,
            "dls_shadow_only": True,
            "dls_runtime_fallback_allowed": False,
        }
        runtime = p0_v8_row.get("runtime_profile") or {}
        for field, expected in expected_p0_runtime.items():
            if runtime.get(field) != expected:
                failures.append(f"P0 v8 runtime profile mismatch: {field}")
        if p0_v8_row.get("runtime_scheduler") != {
            "policy": "SCHED_FIFO",
            "priority": 20,
        }:
            failures.append("P0 v8 runtime scheduler must be SCHED_FIFO/20")
        p0_guard = p0_v8_row.get("guard") or {}
        if p0_guard.get("stage25_allowed_layout_tags") != [524.0]:
            failures.append("P0 v8 must allow only Stage25 layout 524")
        if (
            p0_guard.get("deadline_overrun_stale_tick_command")
            != "exact_zero_qdot_not_consumed"
            or p0_guard.get("deadline_overrun_next_fresh_tick_may_recover")
            is not True
            or p0_guard.get("heartbeat_stale_stop_s") != 0.006
            or p0_guard.get("hard_realtime_claim_requires_zero_deadline_miss")
            is not True
            or p0_guard.get("bounded_tail_miss_ratio_max") != 0.0002
            or p0_guard.get("bounded_tail_lateness_max_ms") != 0.5
            or p0_guard.get("bounded_tail_max_consecutive_misses") != 2
        ):
            failures.append("P0 v8 deadline-overrun fail-closed policy is invalid")
        if (
            p0_v8_row.get("contact") is not False
            or p0_guard.get("dls_runtime_fallback_allowed") is not False
            or (p0_v8_row.get("contact_policy") or {}).get("speedl_runtime_allowed") is not False
        ):
            failures.append("P0 v8 no-contact/layout524/DLS-shadow-only boundary is invalid")
        if not is_sha256(p0_fingerprint):
            failures.append("P0 v8 semantic fingerprint is missing or invalid")
        if (
            p0_v8_capture.get("profile") != P0_V8_PROGRAM
            or p0_v8_candidate.get("profile") != P0_V8_PROGRAM
        ):
            failures.append("P0 v8 current-stage capture/candidate profile mismatch")
        if p0_v8_candidate.get("package_sha256") != p0_hashes:
            failures.append("P0 v8 current-stage candidate package hashes do not match stage table")
        if p0_v8_capture.get("sha256") != p0_hashes:
            failures.append("P0 v8 current-stage capture package hashes do not match stage table")
        if p0_v8_candidate.get("semantic_fingerprint") != p0_fingerprint:
            failures.append("P0 v8 current-stage semantic fingerprint does not match stage table")
        for field in (
            "controller_target",
            "controller_uploaded",
            "controller_readback_verified",
            "controller_readback_manifest",
            "local_triplet",
        ):
            if p0_v8_capture.get(field) != p0_delivery.get(field):
                failures.append(f"P0 v8 current-stage capture delivery mismatch: {field}")
        if p0_delivery_state == "offline" and (
            p0_v8_row.get("active") is not False or p0_v8_row.get("bridge") is not False
        ):
            failures.append("offline P0 v8 candidate must remain inactive with bridge=false")

        marker_rel = p0_v8_capture.get("local_candidate_marker")
        marker_path = root / str(marker_rel or "")
        if not marker_path.is_file():
            failures.append("P0 v8 local-candidate marker is missing")
        else:
            marker = load_json(marker_path)
            if (
                marker.get("program") != P0_V8_PROGRAM
                or marker.get("sha256") != p0_hashes
                or marker.get("semantic_fingerprint") != p0_fingerprint
            ):
                failures.append("P0 v8 marker/package hash/fingerprint binding mismatch")

        stage_review = p0_v8_row.get("review_v2") or {}
        candidate_review = p0_v8_candidate.get("review_v2") or {}
        expected_review_fields = {
            "policy_id": REVIEW_POLICY_ID,
            "milestone": "p0_v8_pre_live",
            "required_stack": "1+1",
        }
        for field, expected in expected_review_fields.items():
            if stage_review.get(field) != expected or candidate_review.get(field) != expected:
                failures.append(f"P0 v8 Review v2 binding mismatch: {field}")
        if stage_review.get("status") != candidate_review.get("status"):
            failures.append("P0 v8 Review v2 status mismatch between stage and current candidate")
        if stage_review.get("composite_fingerprint") != candidate_review.get(
            "composite_fingerprint"
        ):
            failures.append("P0 v8 Review v2 composite fingerprint mismatch")
        if stage_review.get("evidence_frozen") is not (
            p0_v8_candidate.get("evidence_frozen") is True
        ):
            failures.append("P0 v8 evidence-freeze state mismatch")
        if stage_review.get("status") == "accepted":
            indexed = next(
                (
                    item
                    for item in (review_index.get("v2_reviews") or [])
                    if isinstance(item, dict) and item.get("path") == stage_review.get("manifest")
                ),
                None,
            )
            if (
                not indexed
                or indexed.get("required_stack") != "1+1"
                or indexed.get("composite_fingerprint")
                != stage_review.get("composite_fingerprint")
            ):
                failures.append("accepted P0 v8 Review v2 manifest is not current-fingerprint indexed")

        offline_pointer = p0_v8_candidate.get("offline_simulation_diagnostic") or {}
        stage_offline_pointer = p0_v8_row.get("offline_simulation_diagnostic") or {}
        if stage_offline_pointer != offline_pointer:
            failures.append(
                "P0 v8 offline-simulation diagnostic pointer differs between current state and stage table"
            )
        summary_rel = offline_pointer.get("summary_artifact")
        state_rel = offline_pointer.get("state_artifact")
        summary_path = root / str(summary_rel or "")
        state_path = root / str(state_rel or "")
        if not summary_path.is_file() or not state_path.is_file():
            failures.append("P0 v8 offline-simulation summary/state artifact is missing")
        else:
            summary = load_json(summary_path)
            offline_state = load_json(state_path)
            summary_failures = validate_p0_offline_diagnostic(summary)
            state_failures = validate_p0_offline_state_binding(
                offline_state,
                summary=summary,
            )
            failures.extend(
                f"P0 v8 offline diagnostic invalid: {item}"
                for item in summary_failures
            )
            failures.extend(
                f"P0 v8 offline state invalid: {item}"
                for item in state_failures
            )
            diagnostic = summary.get("diagnostic") or {}
            simulator_rows = (
                ((summary.get("timing_evidence") or {}).get(
                    "simulator_cycle_diagnostic"
                ) or {}).get("phase_results")
                or []
            )
            simulator_cycle_500hz_diagnostic_pass = bool(simulator_rows) and all(
                isinstance(row, dict)
                and row.get("meets_500hz_diagnostic") is True
                for row in simulator_rows
            )
            expected_pointer = {
                "status": offline_state.get("status"),
                "summary_artifact": summary_rel,
                "summary_sha256": file_sha256(summary_path),
                "state_artifact": state_rel,
                "state_sha256": file_sha256(state_path),
                "source_run_manifest_sha256": (
                    summary.get("run_binding") or {}
                ).get("sha256"),
                "control_path_diagnostic_pass": diagnostic.get(
                    "all_control_paths_diagnostic_pass"
                ),
                "all_required_faults_exact_zero": diagnostic.get(
                    "all_required_faults_exact_zero"
                ),
                "timing_scope_status": diagnostic.get(
                    "timing_scope_status"
                ),
                "offline_control_timing_pass": diagnostic.get(
                    "offline_control_timing_pass"
                ),
                "simulator_cycle_500hz_diagnostic_pass": (
                    simulator_cycle_500hz_diagnostic_pass
                ),
                "p0_sim_physics_pass": (summary.get("claims") or {}).get(
                    "p0_sim_physics_pass"
                ),
                "controller_canaries_completed": (
                    (summary.get("state_projection") or {}).get(
                        "controller_canaries"
                    )
                    or {}
                ).get("completed"),
                "claim_effect": "diagnostic_only_no_promotion",
            }
            if offline_pointer != expected_pointer:
                failures.append(
                    "P0 v8 offline-simulation pointer is stale or does not preserve the non-promotion boundary"
                )

        p0_passed = p0_v8_candidate.get("p0_v8_passed") is True
        if (p0_v8_row.get("acceptance") or {}).get("p0_v8_passed") is not p0_passed:
            failures.append("P0 v8 pass state mismatch between stage and current candidate")
        if p0_v8_capture.get("passed") is not p0_passed:
            failures.append("P0 v8 capture pass state mismatch")
        if p0_passed:
            final_canary = any(
                isinstance(item, dict)
                and isinstance(item.get("phase_s"), (int, float))
                and float(item["phase_s"]) == 60.0
                and item.get("canary_passed") is True
                and item.get("composite_fingerprint")
                == stage_review.get("composite_fingerprint")
                for item in (p0_v8_candidate.get("completed_canaries") or [])
            )
            if not p0_v8_candidate.get("passed_artifact") or not final_canary:
                failures.append("P0 v8 passed requires a final continuous 60 second artifact")
            if stage_review.get("status") != "accepted" or not is_sha256(
                stage_review.get("composite_fingerprint")
            ):
                failures.append("P0 v8 passed requires accepted fingerprint-bound Review v2 1+1")

    for table_name, rows in (("step5", step5_rows), ("step6", step6_rows)):
        for row_id, row in rows.items():
            binding = row.get("current_binding")
            if binding and binding.get("is_current") is True and row_id != current_stage_id:
                failures.append(f"{table_name}:{row_id} incorrectly claims global current binding")

    v30 = step5_rows.get(V30_PROGRAM)
    if v30 is None:
        failures.append("inactive v30 offline candidate row is missing")
    else:
        delivery = v30.get("package_delivery") or {}
        v30_is_current = current_stage_id == V30_PROGRAM or current_program == V30_PROGRAM
        if not v30_is_current:
            if v30.get("active") is not False or v30.get("bridge") is not False:
                failures.append("inactive v30 candidate must remain inactive with bridge=false")
            if v30.get("current_binding", {}).get("is_current") is not False:
                failures.append("inactive v30 candidate must not claim current binding")
        delivery_state, delivery_failures = _validate_manifest_bound_delivery(
            root,
            label="v30 inactive package",
            delivery=delivery,
        )
        failures.extend(delivery_failures)
        if delivery.get("delivery_preparation_allowed_before_p0_v8") is not True:
            failures.append("v30 package delivery preparation must remain allowed before P0 v8")
        canary = v30.get("canary_stop_register") or {}
        if not v30_is_current and (
            canary.get("enabled") is not False or canary.get("armed") is not False
        ):
            failures.append("v30 canary stop register must remain disabled and unarmed offline")
        expected_runtime = {
            "backend": "cupy",
            "inner_iterations": 512,
            "epsilon": 0.01,
            "sigr_exponent_r": 0.8,
            "qdot_cap_rad_s": 0.05,
            "control_mode": "speedj_rnn_live",
            "joint_layout_code": 524.0,
        }
        if v30.get("runtime_profile") != expected_runtime:
            failures.append("v30 runtime profile does not match the pinned strict-RNN profile")
        if v30.get("runtime_scheduler") != {
            "policy": "SCHED_FIFO",
            "priority": 20,
        }:
            failures.append("v30 runtime scheduler must be SCHED_FIFO/20")
        if v30.get("guard", {}).get("dls_runtime_fallback_allowed") is not False:
            failures.append("v30 must forbid DLS runtime fallback")
        v30_guard = v30.get("guard") or {}
        if (
            v30_guard.get("deadline_overrun_stale_tick_command")
            != "exact_zero_qdot_not_consumed"
            or v30_guard.get("deadline_overrun_next_fresh_tick_may_recover")
            is not True
            or v30_guard.get("heartbeat_stale_stop_s") != 0.006
            or v30_guard.get("hard_realtime_claim_requires_zero_deadline_miss")
            is not True
            or v30_guard.get("bounded_tail_miss_ratio_max") != 0.0002
            or v30_guard.get("bounded_tail_lateness_max_ms") != 0.5
            or v30_guard.get("bounded_tail_max_consecutive_misses") != 2
        ):
            failures.append("v30 deadline-overrun fail-closed policy is invalid")
        failures.extend(_validate_local_triplet(root, label="v30", delivery=delivery))

        p0_gate = v30.get("p0_v8_gate") or {}
        p0_passed = p0_v8_candidate.get("p0_v8_passed") is True
        if (
            p0_gate.get("profile") != P0_V8_PROGRAM
            or p0_gate.get("passed") is not p0_passed
            or p0_gate.get("passed_artifact") != p0_v8_candidate.get("passed_artifact")
            or p0_gate.get("package_upload_readback_may_precede_p0") is not True
            or any(
                p0_gate.get(field) is not True
                for field in (
                    "required_before_bridge_start",
                    "required_before_contact_run",
                    "required_before_current_promotion",
                )
            )
        ):
            failures.append("v30 P0 v8 gate is inconsistent with current_stage.json")
        promotion = v30.get("promotion_gate") or {}
        if not p0_passed and any(
            promotion.get(field) is not False
            for field in (
                "current_promotion_allowed",
                "bridge_start_allowed",
                "contact_run_allowed",
            )
        ):
            failures.append("v30 promotion/bridge/contact cannot open before P0 v8 passes")

        review = v30.get("review_v2") or {}
        if (
            review.get("policy_id") != REVIEW_POLICY_ID
            or review.get("milestone") != "v30_contact_pre_live"
            or review.get("required_stack") != "2+1"
            or review.get("legacy_status") != "historical_superseded_by_review_policy_v2"
        ):
            failures.append("v30 Review v2 policy/milestone/stack binding is invalid")
        historical_by_path = {
            str(item.get("path")): item
            for item in (review_index.get("historical_artifacts") or [])
            if isinstance(item, dict)
        }
        legacy_path = review.get("legacy_manifest")
        legacy = historical_by_path.get(str(legacy_path))
        legacy_file = root / str(legacy_path or "")
        if not legacy or legacy.get("sha256") != (
            file_sha256(legacy_file) if legacy_file.is_file() else None
        ):
            failures.append("v30 legacy review evidence is not hash-bound by Review v2 index")
        evidence = v30.get("local_analysis_evidence") or {}
        imported_manifest_path = root / str(evidence.get("imported_v29_manifest") or "")
        replay_path = root / str(evidence.get("v29_replay") or "")
        profile_selection_path = root / str(
            evidence.get("profile_selection") or ""
        )
        timing_raw_path = root / str(evidence.get("timing_raw") or "")
        current_source_solver_path = root / str(
            evidence.get("current_source_solver_10k_raw") or ""
        )
        timing_path = root / str(evidence.get("timing") or "")
        if not imported_manifest_path.is_file():
            failures.append("v30 imported v29 evidence manifest is missing")
        else:
            imported = load_json(imported_manifest_path)
            if imported.get("copy_complete") is not True or imported.get("blockers") != []:
                failures.append("v30 imported v29 evidence copy/hash binding is incomplete")
            if file_sha256(imported_manifest_path) != evidence.get("imported_v29_manifest_sha256"):
                failures.append("v30 imported v29 evidence manifest sha mismatch")
            for name, item in (imported.get("files") or {}).items():
                if item.get("source_local_sha256_match") is not True:
                    failures.append(f"v30 imported v29 source/local sha mismatch: {name}")
        if not replay_path.is_file():
            failures.append("v30 canonical v29 replay summary is missing")
        else:
            replay = load_json(replay_path)
            if (
                replay.get("acceptance_pass") is not True
                or float(replay.get("accepted_ratio", 0.0)) < 0.99
                or replay.get("normal_mismatch_rows") != 0
                or replay.get("nonfinite_rows") != 0
                or replay.get("qdot_over_bound_rows") != 0
                or replay.get("semantic_contract_invalid_rows") != 0
            ):
                failures.append("v30 canonical v29 replay acceptance is incomplete")
        if not profile_selection_path.is_file():
            failures.append("v30 strict-RNN profile selection evidence is missing")
        else:
            profile_selection = load_json(profile_selection_path)
            if (
                profile_selection.get("classification")
                != "diagnostic_selection_not_formal_timing"
                or profile_selection.get("selected_inner_iterations") != 512
                or (profile_selection.get("selected_profile") or {}).get(
                    "inner_iterations"
                )
                != 512
                or (profile_selection.get("claim_boundary") or {}).get(
                    "v30_offline_ready"
                )
                is not False
            ):
                failures.append("v30 strict-RNN profile selection contract is invalid")
            if file_sha256(profile_selection_path) != evidence.get(
                "profile_selection_sha256"
            ):
                failures.append("v30 strict-RNN profile selection sha mismatch")
            if evidence.get("profile_selection_status") not in {
                "canonical_rnn512_selected_formal_timing_passed",
                "canonical_rnn512_selected_formal_timing_rerun_pending_after_prefault",
            }:
                failures.append("v30 strict-RNN profile selection status is invalid")
        for label, path, expected_sha in (
            ("raw timing", timing_raw_path, evidence.get("timing_raw_sha256")),
            (
                "current-source solver 10k",
                current_source_solver_path,
                evidence.get("current_source_solver_10k_raw_sha256"),
            ),
            ("timing summary", timing_path, evidence.get("timing_sha256")),
        ):
            if not path.is_file():
                failures.append(f"v30 {label} evidence is missing")
            elif not isinstance(expected_sha, str) or file_sha256(path) != expected_sha:
                failures.append(f"v30 {label} evidence sha mismatch")
        if current_source_solver_path.is_file():
            current_source_solver = load_json(current_source_solver_path)
            solver_metrics = current_source_solver.get("solver") or {}
            if (
                int(solver_metrics.get("samples", 0) or 0) != 10_000
                or int(solver_metrics.get("nonfinite_count", 0) or 0) != 0
                or not isinstance(
                    current_source_solver.get("acceptance_eligible"), bool
                )
            ):
                failures.append(
                    "v30 historical current-source solver 10k evidence is malformed"
                )
        source_contract = evidence.get("timing_source_contract") or {}
        source_fields = (
            ("contact_semantics", "contact_semantics_sha256"),
            ("solver", "solver_sha256"),
            ("outer_loop", "outer_loop_sha256"),
            ("control_contract", "control_contract_sha256"),
            ("runtime_interface", "runtime_interface_sha256"),
            ("kinematics", "kinematics_sha256"),
            ("bridge", "bridge_sha256"),
            ("harness", "harness_sha256"),
            ("bundler", "bundler_sha256"),
            ("aggregator", "aggregator_sha256"),
            ("readiness_builder", "readiness_builder_sha256"),
        )
        for path_field, sha_field in source_fields:
            source_path = root / str(source_contract.get(path_field) or "")
            if not source_path.is_file():
                failures.append(f"v30 timing source is missing: {path_field}")
            elif file_sha256(source_path) != source_contract.get(sha_field):
                failures.append(f"v30 timing source sha mismatch: {path_field}")
        if timing_path.is_file() and timing_raw_path.is_file():
            timing = load_json(timing_path)
            offline_status = v30.get("offline_acceptance", {}).get("status")
            timing_pass = timing.get("overall_pass") is True
            if not timing_pass and offline_status != "v30_offline_blocked":
                failures.append("failing v30 timing must keep offline status v30_offline_blocked")
            if offline_status == "v30_offline_ready" and not timing_pass:
                failures.append("v30 offline-ready status requires passing timing")
            if not timing_pass and not timing.get("blockers"):
                failures.append("failing v30 timing must preserve explicit blockers")
            input_evidence = timing.get("input_evidence") or {}
            if input_evidence.get("sha256") != file_sha256(timing_raw_path):
                failures.append("v30 timing summary is not bound to tracked raw timing evidence")
            source_binding = timing.get("source_binding") or {}
            timing_is_current = (
                evidence.get("timing_raw_status")
                == "current_canonical_rnn512_hard_realtime_pass"
            )
            if timing_is_current and source_binding and timing_pass:
                for _, sha_field in source_fields:
                    if source_binding.get(sha_field) != source_contract.get(sha_field):
                        failures.append(f"v30 timing bundle source sha mismatch: {sha_field}")
            elif timing_is_current and timing_pass:
                failures.append("passing v30 timing requires an stdin source bundle binding")
            elif timing_is_current and not timing_pass:
                failures.append("current v30 timing status requires a passing timing summary")
            elif offline_status != "v30_offline_blocked":
                failures.append("superseded v30 timing must keep offline status blocked")
        for label, path_field, sha_field in (
            ("runtime-shaped smoke", "runtime_shaped_smoke", "runtime_shaped_smoke_sha256"),
            ("component diagnostic", "component_diagnostic", "component_diagnostic_sha256"),
            ("offline readiness", "offline_readiness", "offline_readiness_sha256"),
        ):
            evidence_path = root / str(evidence.get(path_field) or "")
            if not evidence_path.is_file():
                failures.append(f"v30 {label} evidence is missing")
            elif file_sha256(evidence_path) != evidence.get(sha_field):
                failures.append(f"v30 {label} evidence sha mismatch")
        readiness_path = root / str(evidence.get("offline_readiness") or "")
        if readiness_path.is_file():
            readiness = load_json(readiness_path)
            readiness_status = readiness.get("status")
            readiness_timing = readiness.get("timing") or {}
            readiness_acceptance_raw = (
                readiness_timing.get("acceptance_raw_evidence") or {}
            )
            readiness_current_source = (
                readiness_timing.get("current_source_evidence") or {}
            )
            timing_is_current = (
                evidence.get("timing_raw_status")
                == "current_canonical_rnn512_hard_realtime_pass"
            )
            if (
                readiness_acceptance_raw.get("path")
                != evidence.get("timing_raw")
                or readiness_acceptance_raw.get("sha256")
                != evidence.get("timing_raw_sha256")
            ):
                failures.append(
                    "v30 readiness selected timing artifact does not match the stage pointer"
                )
            if timing_is_current:
                if (
                    readiness_timing.get("overall_pass") is not True
                    or readiness_timing.get("hard_realtime_pass") is not True
                    or readiness_current_source.get("acceptance_eligible") is not True
                ):
                    failures.append(
                        "current v30 timing status requires readiness hard-realtime acceptance"
                    )
            elif any(
                value is True
                for value in (
                    readiness_timing.get("overall_pass"),
                    readiness_timing.get("hard_realtime_pass"),
                    readiness_current_source.get("acceptance_eligible"),
                )
            ):
                failures.append(
                    "superseded v30 timing must not remain acceptance-eligible in readiness"
                )
            if readiness_status not in {"v30_offline_ready", "v30_offline_blocked"}:
                failures.append("v30 offline readiness has an invalid status")
            if readiness_status != v30.get("offline_acceptance", {}).get("status"):
                failures.append("v30 stage/readiness offline status mismatch")
            if readiness_status == "v30_offline_blocked" and not readiness.get("blockers"):
                failures.append("blocked v30 readiness must preserve explicit blockers")
            authorization = readiness.get("authorization") or {}
            if any(authorization.get(field) is not False for field in (
                "live_motion_authorized",
                "controller_upload_authorized",
                "bridge_start_authorized",
                "tp_play_authorized",
            )):
                failures.append("v30 offline readiness must deny all live/controller authorization")
            claim = readiness.get("claim_boundary") or {}
            if any(claim.get(field) is not False for field in (
                "package_accepted",
                "live_accepted",
                "reproduction_complete",
            )):
                failures.append("v30 readiness crosses the offline claim boundary")
            readiness_package = readiness.get("package") or {}
            expected_readback = delivery_state == "delivered"
            if (
                readiness_package.get("controller_readback_verified")
                is not expected_readback
                or readiness_package.get("controller_readback_manifest")
                != delivery.get("controller_readback_manifest")
                or readiness_package.get("semantic_fingerprint")
                != delivery.get("semantic_fingerprint")
                or readiness_package.get("triplet_sha256") != delivery.get("sha256")
            ):
                failures.append("v30 stage/readiness package delivery binding mismatch")
            readiness_review = readiness.get("review_v2") or {}
            expected_review_sources = (
                (
                    "policy",
                    "config/step5d_review_policy_v2.json",
                    "policy_path",
                    "policy_sha256",
                ),
                (
                    "index",
                    "config/step5d_review_index_v2.json",
                    "index_path",
                    "index_sha256",
                ),
            )
            for label, relative, path_field, sha_field in expected_review_sources:
                source_path = root / relative
                expected_sha = (
                    full_review_index_projection_sha256(load_json(source_path))
                    if label == "index" and source_path.is_file()
                    else file_sha256(source_path) if source_path.is_file() else None
                )
                if (
                    readiness_review.get(path_field) != relative
                    or not source_path.is_file()
                    or readiness_review.get(sha_field) != expected_sha
                ):
                    failures.append(f"v30 readiness Review v2 {label} hash binding mismatch")
            history_roles = {
                str(item.get("role"))
                for item in (readiness.get("timing", {}).get("history") or [])
            }
            required_roles = {
                "six_lane_optimized_solver_10k_with_20_tick_smoke",
                "current_source_solver_10k_with_20_tick_runtime_smoke",
                "runtime_shaped_smoke_not_acceptance",
                "component_diagnostic_not_acceptance",
            }
            if not required_roles.issubset(history_roles):
                failures.append("v30 readiness timing history is incomplete")
            current_source_history = next(
                (
                    item
                    for item in (readiness.get("timing", {}).get("history") or [])
                    if item.get("role")
                    == "current_source_solver_10k_with_20_tick_runtime_smoke"
                ),
                None,
            )
            if current_source_history is not None:
                acceptance_evaluation = current_source_history.get(
                    "acceptance_evaluation"
                ) or {}
                if (
                    current_source_history.get("path")
                    != evidence.get("current_source_solver_10k_raw")
                    or current_source_history.get("sha256")
                    != evidence.get("current_source_solver_10k_raw_sha256")
                    or acceptance_evaluation.get(
                        "recomputed_from_single_hash_bound_raw_artifact"
                    )
                    is not True
                    or acceptance_evaluation.get("raw_sha256")
                    != evidence.get("current_source_solver_10k_raw_sha256")
                ):
                    failures.append(
                        "v30 readiness current-source solver 10k binding is inconsistent"
                    )

            review_accepted = (
                review.get("status") == "accepted"
                and review.get("evidence_frozen") is True
                and is_sha256(review.get("composite_fingerprint"))
                and readiness_review.get("accepted") is True
                and readiness_review.get("composite_fingerprint")
                == review.get("composite_fingerprint")
            )
            if review.get("status") == "accepted":
                manifest_rel = review.get("manifest")
                indexed = next(
                    (
                        item
                        for item in (review_index.get("v2_reviews") or [])
                        if isinstance(item, dict) and item.get("path") == manifest_rel
                    ),
                    None,
                )
                if (
                    not indexed
                    or indexed.get("composite_fingerprint")
                    != review.get("composite_fingerprint")
                    or indexed.get("required_stack") != "2+1"
                ):
                    failures.append("accepted v30 Review v2 manifest is not current-fingerprint indexed")

            promotion_claimed = promotion.get("current_promotion_allowed") is True
            if v30_is_current or promotion_claimed:
                if not (
                    p0_passed
                    and delivery_state == "delivered"
                    and readiness_status == "v30_offline_ready"
                    and review_accepted
                ):
                    failures.append(
                        "v30 current promotion requires P0 v8, manifest-bound "
                        "readback, ready timing/safe-hold, and accepted Review v2 2+1"
                    )
            if v30_is_current and promotion.get("current_promotion_allowed") is not True:
                failures.append("current v30 must have current_promotion_allowed=true")

    step6_v2 = step6_rows.get("step6_contact_eight_baseline_v2")
    if not step6_v2:
        failures.append("Step6b v2 row is missing")
    else:
        if step6_v2.get("current_binding", {}).get("is_current") is not False:
            failures.append("Step6b v2 must be retained evidence, not the global current pointer")
        if step6_v2.get("package_delivery", {}).get("controller_readback_status") != "verified_retained":
            failures.append("Step6b v2 package delivery must be marked verified_retained")

    try:
        step5_contact = resolve_experiment_profile("Step5.contact_cycloid", root)
        step5d = resolve_experiment_profile("Step5.step5d_rnn", root)
        step6a = resolve_experiment_profile("Step6.no_contact_eight", root)
        step6b_v1 = resolve_experiment_profile("Step6.contact_eight_v1", root)
        step6b_v2 = resolve_experiment_profile("Step6.contact_eight", root)
    except Exception as exc:
        failures.append(f"canonical protocol table cannot be resolved: {exc}")
        return failures

    step5b_row = step5_rows.get("step5_contact_cycloid_baseline_v1", {})
    current_step5d_id = (
        str(current_program or current_stage_id)
        if str(current_program or current_stage_id).startswith("step5d_strict_rnn_ablation_")
        else "step5d_strict_rnn_ablation_v27"
    )
    step5d_label = current_step5d_id.rsplit("_", 1)[-1]
    step5d_row = step5_rows.get(current_step5d_id, {})
    step6a_row = step6_rows.get("step6a_eight_no_contact_v1", {})
    step6b_v1_row = step6_rows.get("step6_contact_eight_baseline_v1", {})
    step6b_v2_row = step6_rows.get("step6_contact_eight_baseline_v2", {})

    canonical_refs = {
        "step5_contact_cycloid_baseline_v1": (step5b_row, "Step5.contact_cycloid"),
        current_step5d_id: (step5d_row, "Step5.step5d_rnn"),
        "step6a_eight_no_contact_v1": (step6a_row, "Step6.no_contact_eight"),
        "step6_contact_eight_baseline_v1": (step6b_v1_row, "Step6.contact_eight_v1"),
        "step6_contact_eight_baseline_v2": (step6b_v2_row, "Step6.contact_eight"),
    }
    for row_id, (row, expected_ref) in canonical_refs.items():
        if row.get("canonical_profile_ref") != expected_ref:
            failures.append(f"{row_id} canonical_profile_ref mismatch")

    if step5b_row.get("filter_policy", {}).get("alpha") != step5_contact["parameters"]["normal_filter_alpha"]:
        failures.append("Step5b normal filter alpha does not match canonical Step5 contact profile")
    if step5d_row.get("filter_policy", {}).get("alpha") != step5d["parameters"]["normal_filter_alpha"]:
        failures.append(f"Step5d {step5d_label} normal filter alpha does not match canonical profile")
    if step5d_row.get("guard", {}).get("post_far_search_rezero_s") != step5d["parameters"]["zero_hold_s"]:
        failures.append(f"Step5d {step5d_label} post-far-search rezero does not match canonical profile")
    if step5d_row.get("guard", {}).get("target_force_n") != step5d["parameters"]["target_force_n"]:
        failures.append(f"Step5d {step5d_label} target force does not match canonical profile")
    if step5d_row.get("guard", {}).get("speedl_linear_cap_m_s") != step5d["safety_limits"]["speedl_linear_cap_m_s"]:
        failures.append(f"Step5d {step5d_label} speedl linear cap does not match canonical profile")
    if step5d_row.get("guard", {}).get("speedl_angular_cap_rad_s") != step5d["safety_limits"]["speedl_angular_cap_rad_s"]:
        failures.append(f"Step5d {step5d_label} speedl angular cap does not match canonical profile")
    if step6a_row.get("duration_s") != step6a["parameters"]["trajectory_duration_s"]:
        failures.append("Step6a duration does not match canonical profile")
    if step6b_v1_row.get("filter_policy", {}).get("alpha") != step6b_v1["parameters"]["normal_filter_alpha"]:
        failures.append("Step6b v1 filter alpha does not match canonical profile")
    if step6b_v2_row.get("duration_s") != step6b_v2["parameters"]["trajectory_duration_s"]:
        failures.append("Step6b v2 duration does not match canonical profile")
    if step6b_v2_row.get("filter_policy", {}).get("alpha") != step6b_v2["parameters"]["normal_filter_alpha"]:
        failures.append("Step6b v2 filter alpha does not match canonical profile")
    if step6b_v2_row.get("bridge_limits", {}).get("total_linear_limit_m_s") != step6b_v2["safety_limits"]["total_linear_limit_m_s"]:
        failures.append("Step6b v2 total linear limit does not match canonical profile")
    if step6b_v2_row.get("bridge_limits", {}).get("angular_limit_rad_s") != step6b_v2["safety_limits"]["angular_limit_rad_s"]:
        failures.append("Step6b v2 angular limit does not match canonical profile")

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=EXPERIMENT_ROOT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    failures = validate(args.root)
    payload = {"ok": not failures, "failures": failures}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif failures:
        for failure in failures:
            print(f"FAIL: {failure}")
    else:
        print("cross-step parameter table validation passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
