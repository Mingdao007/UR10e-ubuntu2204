#!/usr/bin/env python3
"""Validate UR10e Review v2 packets, full reviews, and targeted closers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from step5d_review_v2 import (
    SCHEMA_INDEX,
    SCHEMA_MANIFEST,
    SCHEMA_PACKET,
    bind_files,
    canonical_sha256,
    component_digest,
    file_sha256,
    load_json,
    path_matches_any,
    policy_review_class,
    requested_lane_contract,
    review_findings,
    validate_commit,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "step5d_review_policy_v2.json"


def _component_payload(component: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in component.items() if key != "sha256"}


def _binding_issues(bindings: Any, root: Path, prefix: str) -> list[str]:
    if not isinstance(bindings, list):
        return [f"{prefix}_bindings_not_list"]
    issues: list[str] = []
    for binding in bindings:
        if not isinstance(binding, dict) or not binding.get("path"):
            issues.append(f"{prefix}_binding_invalid")
            continue
        try:
            current = bind_files(root, [str(binding["path"])])[0]
        except ValueError:
            issues.append(f"{prefix}_binding_path_escapes_root")
            continue
        for field in ("exists", "bytes", "sha256"):
            if binding.get(field) != current.get(field):
                issues.append(f"{prefix}_binding_changed:{binding['path']}:{field}")
    return issues


def validate_packet(
    packet: dict[str, Any], *, root: Path = ROOT, policy: dict[str, Any] | None = None
) -> dict[str, Any]:
    root = root.resolve()
    policy = policy or load_json(DEFAULT_POLICY)
    issues: list[str] = []
    if packet.get("schema_version") != SCHEMA_PACKET:
        issues.append("packet_schema_mismatch")
    if packet.get("policy_id") != policy.get("policy_id"):
        issues.append("packet_policy_id_mismatch")
    try:
        class_id, review_class = policy_review_class(
            policy, str(packet.get("workflow", "")), str(packet.get("milestone", ""))
        )
    except ValueError as exc:
        issues.append(f"packet_policy_route_invalid:{exc}")
        return {"ok": False, "issues": sorted(set(issues))}
    if packet.get("review_class") != class_id:
        issues.append("packet_review_class_mismatch")
    if packet.get("required_stack") != review_class.get("stack"):
        issues.append("packet_required_stack_mismatch")
    if packet.get("full_review_required") is not (
        review_class.get("full_review_required") is True
    ):
        issues.append("packet_full_review_requirement_mismatch")

    components = packet.get("components") or {}
    expected_component_names = {"code", "package", "evidence", "policy"}
    if set(components) != expected_component_names:
        issues.append("packet_component_set_mismatch")
    fingerprints = packet.get("fingerprints") or {}
    recomputed_sha: dict[str, str] = {}
    for name in sorted(expected_component_names):
        component = components.get(name)
        if not isinstance(component, dict):
            issues.append(f"packet_component_missing:{name}")
            continue
        payload = _component_payload(component)
        digest = component_digest(payload)
        recomputed_sha[name] = digest
        if component.get("sha256") != digest:
            issues.append(f"packet_component_digest_mismatch:{name}")
        if fingerprints.get(name) != digest:
            issues.append(f"packet_fingerprint_mismatch:{name}")
        if name in {"code", "package", "evidence"}:
            issues.extend(_binding_issues(component.get("files"), root, name))

    policy_component = components.get("policy") or {}
    policy_path = policy_component.get("path")
    if policy_path:
        try:
            binding = bind_files(root, [str(policy_path)])[0]
            if binding.get("sha256") != policy_component.get("file_sha256"):
                issues.append("packet_policy_file_changed")
        except ValueError:
            issues.append("packet_policy_path_escapes_root")
    else:
        issues.append("packet_policy_path_missing")
    if policy_component.get("canonical_sha256") != canonical_sha256(policy):
        issues.append("packet_policy_canonical_digest_mismatch")

    evidence_component = components.get("evidence") or {}
    code_component = components.get("code") or {}
    package_component = components.get("package") or {}
    for field in ("base_commit", "head_commit"):
        if packet.get(field) != code_component.get(field):
            issues.append(f"packet_top_level_code_binding_mismatch:{field}")
    if packet.get("changed_symbols") != code_component.get("changed_symbols"):
        issues.append("packet_top_level_changed_symbols_mismatch")
    if packet.get("package_identity") != package_component.get("identity"):
        issues.append("packet_top_level_package_identity_mismatch")
    if packet.get("evidence_roles") != evidence_component.get("roles"):
        issues.append("packet_top_level_evidence_roles_mismatch")
    for field in ("state_resolver", "tests", "claims"):
        if packet.get(field) != evidence_component.get(field):
            issues.append(f"packet_top_level_evidence_binding_mismatch:{field}")
    if review_class.get("requires_package_identity") is True and not package_component.get(
        "identity"
    ):
        issues.append("packet_required_package_identity_missing")
    required_roles = set(
        str(value) for value in review_class.get("required_evidence_roles", [])
    )
    actual_roles = set((evidence_component.get("roles") or {}).keys())
    issues.extend(
        f"packet_required_evidence_role_missing:{role}"
        for role in sorted(required_roles - actual_roles)
    )
    evidence_bindings = {
        binding.get("path"): binding
        for binding in evidence_component.get("files", [])
        if isinstance(binding, dict)
    }
    for role, value in sorted((evidence_component.get("roles") or {}).items()):
        role_path = value if isinstance(value, str) else (
            value.get("path") if isinstance(value, dict) else None
        )
        if role_path not in evidence_bindings:
            issues.append(f"packet_evidence_role_not_bound:{role}")
        elif isinstance(value, dict) and value.get("sha256") not in {
            None,
            evidence_bindings[role_path].get("sha256"),
        }:
            issues.append(f"packet_evidence_role_sha256_mismatch:{role}")
    resolver_source = evidence_component.get("state_resolver_source") or {}
    if resolver_source.get("kind") == "file":
        issues.extend(_binding_issues([resolver_source], root, "state_resolver"))

    composite_payload = {
        "workflow": packet.get("workflow"),
        "milestone": packet.get("milestone"),
        "review_class": packet.get("review_class"),
        "component_sha256": {
            name: recomputed_sha.get(name) for name in sorted(expected_component_names)
        },
    }
    composite = canonical_sha256(composite_payload)
    if fingerprints.get("composite") != composite:
        issues.append("packet_composite_fingerprint_mismatch")

    expected_lanes = [str(value) for value in review_class.get("lanes", [])]
    if set(packet.get("lanes") or {}) != set(expected_lanes):
        issues.append("packet_lane_set_mismatch")
    maximum_tests = int(policy["execution"]["max_focused_tests_per_lane"])
    all_paths = sorted(
        {
            binding.get("path")
            for name in ("code", "package", "evidence")
            for binding in (components.get(name) or {}).get("files", [])
            if isinstance(binding, dict) and binding.get("path")
        }
    )
    changed_symbols = code_component.get("changed_symbols") or []
    tests = evidence_component.get("tests") or []
    for lane_id, lane_packet in (packet.get("lanes") or {}).items():
        if len((lane_packet or {}).get("focused_tests", [])) > maximum_tests:
            issues.append(f"packet_lane_test_limit_exceeded:{lane_id}")
        contract = requested_lane_contract(
            policy, lane_id, packet.get("risk_flags") or []
        )
        expected_files = [
            path for path in all_paths if path_matches_any(path, contract["file_globs"])
        ]
        expected_symbols = [
            symbol
            for symbol in changed_symbols
            if path_matches_any(str(symbol.get("path")), contract["file_globs"])
        ]
        expected_tests = [test for test in tests if lane_id in test.get("lanes", [])]
        if (lane_packet or {}).get("allowlisted_files") != expected_files:
            issues.append(f"packet_lane_allowlist_mismatch:{lane_id}")
        if (lane_packet or {}).get("changed_symbols") != expected_symbols:
            issues.append(f"packet_lane_changed_symbols_mismatch:{lane_id}")
        if (lane_packet or {}).get("focused_tests") != expected_tests:
            issues.append(f"packet_lane_focused_tests_mismatch:{lane_id}")
    if not review_class.get("full_review_required"):
        if packet.get("invocation_plan"):
            issues.append("ordinary_0_plus_0_has_reviewer_invocation")
        if packet.get("review_due") is True:
            issues.append("ordinary_0_plus_0_marked_review_due")

    risk_flags = packet.get("risk_flags") or []
    expected_contracts = {
        lane_id: requested_lane_contract(policy, lane_id, risk_flags)
        for lane_id in expected_lanes
    }
    invocation_plan = packet.get("invocation_plan") or []
    if review_class.get("full_review_required") and {
        invocation.get("lane") for invocation in invocation_plan
    } != set(expected_lanes):
        issues.append("packet_invocation_lane_set_mismatch")
    preflight = packet.get("fable5_preflight") or {}
    preflight_status = str(preflight.get("status", "not_checked"))
    allowed_preflight = set(
        str(value) for value in policy.get("fable5_preflight_statuses", [])
    )
    fable_required = any(
        contract["provider"] == "fable5" for contract in expected_contracts.values()
    )
    preflight_complete = not fable_required or preflight_status in allowed_preflight
    for invocation in invocation_plan:
        lane_id = invocation.get("lane")
        expected = expected_contracts.get(lane_id)
        if not expected:
            issues.append(f"packet_invocation_unknown_lane:{lane_id}")
            continue
        for field in (
            "provider",
            "requested_model",
            "requested_effort",
            "timeout_seconds",
        ):
            if invocation.get(field) != expected.get(field):
                issues.append(f"packet_invocation_contract_mismatch:{lane_id}:{field}")
        expected_availability = (
            preflight_status if expected["provider"] == "fable5" else "available"
        )
        if invocation.get("availability") != expected_availability:
            issues.append(f"packet_invocation_availability_mismatch:{lane_id}")
        expected_start = bool(
            packet.get("review_due") is True
            and preflight_complete
            and (
                expected["provider"] != "fable5"
                or preflight_status == "available"
            )
        )
        if invocation.get("start_allowed") is not expected_start:
            issues.append(f"packet_invocation_start_gate_mismatch:{lane_id}")
    if packet.get("review_start_allowed") is not any(
        invocation.get("start_allowed") is True for invocation in invocation_plan
    ):
        issues.append("packet_review_start_summary_mismatch")
    if review_class.get("full_review_required") and packet.get("review_due") is True:
        if packet.get("evidence_frozen") is not True:
            issues.append("review_due_without_evidence_freeze")
        if packet.get("blockers") or packet.get("freeze_blockers"):
            issues.append("review_due_with_packet_blockers")
        if not validate_commit(code_component.get("base_commit")):
            issues.append("review_due_with_invalid_base_commit")
        if not validate_commit(code_component.get("head_commit")):
            issues.append("review_due_with_invalid_head_commit")
        if evidence_component.get("state_resolver") is None:
            issues.append("review_due_without_state_resolver")
        if not evidence_component.get("files"):
            issues.append("review_due_without_evidence_files")
        if any(
            binding.get("exists") is not True
            for name in ("code", "package", "evidence")
            for binding in (components.get(name) or {}).get("files", [])
            if isinstance(binding, dict)
        ):
            issues.append("review_due_with_missing_bound_file")
        if any(test.get("status") != "pass" for test in tests):
            issues.append("review_due_with_nonpassing_focused_test")
        required_claims = set(str(value) for value in policy.get("claim_boundaries", []))
        claims = evidence_component.get("claims") or {}
        if required_claims - set(claims):
            issues.append("review_due_with_missing_claim_boundary")
    return {
        "ok": not issues,
        "issues": sorted(set(issues)),
        "composite_fingerprint": composite,
    }


def _waiver_valid(
    waiver: Any, packet: dict[str, Any], policy: dict[str, Any]
) -> tuple[bool, list[str]]:
    if not isinstance(waiver, dict):
        return False, ["fable5_waiver_missing"]
    issues: list[str] = []
    waiver_policy = policy["waiver"]
    if waiver.get("provider") != waiver_policy["allowed_provider"]:
        issues.append("waiver_provider_mismatch")
    if waiver.get("lane") != waiver_policy["allowed_lane"]:
        issues.append("waiver_lane_mismatch")
    if waiver.get("composite_fingerprint") != packet["fingerprints"]["composite"]:
        issues.append("waiver_fingerprint_mismatch")
    if waiver.get("authorized_by") != "user" or waiver.get("explicit") is not True:
        issues.append("waiver_not_explicit_user_authorization")
    for field in ("waiver_id", "issued_at", "authorization_evidence", "reason"):
        if not waiver.get(field):
            issues.append(f"waiver_field_missing:{field}")
    return not issues, issues


def _lane_contract_issues(
    lane_id: str,
    lane: dict[str, Any],
    expected: dict[str, Any],
    *,
    policy: dict[str, Any],
) -> list[str]:
    issues: list[str] = []
    for field in ("requested_model", "requested_effort", "timeout_seconds"):
        if lane.get(field) != expected.get(field):
            issues.append(f"lane_contract_mismatch:{lane_id}:{field}")
    allowed_statuses = {"pass", "block", "unavailable", "unavailable_timeout", "timeout"}
    if lane.get("status") not in allowed_statuses:
        issues.append(f"lane_status_invalid:{lane_id}")
    if lane.get("status") == "pass":
        if lane.get("actual_model") != expected.get("requested_model"):
            issues.append(f"lane_actual_model_mismatch:{lane_id}")
        if lane.get("actual_effort") != expected.get("requested_effort"):
            issues.append(f"lane_actual_effort_mismatch:{lane_id}")
    elif "actual_model" not in lane or "actual_effort" not in lane:
        issues.append(f"lane_actual_runtime_fields_missing:{lane_id}")
    if lane.get("verdict") != lane.get("status"):
        issues.append(f"lane_verdict_status_mismatch:{lane_id}")
    timeout = int(policy["execution"]["lane_timeout_seconds"])
    elapsed = lane.get("elapsed_seconds")
    if not isinstance(elapsed, (int, float)) or elapsed < 0:
        issues.append(f"lane_elapsed_invalid:{lane_id}")
    elif float(elapsed) > timeout:
        issues.append(f"lane_timeout_exceeded:{lane_id}")
    if lane.get("status") == "timeout":
        issues.append(f"lane_timed_out:{lane_id}")
    return issues


def _finding_issues(findings: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    blocking: list[str] = []
    backlog: list[str] = []
    seen: set[str] = set()
    for finding in findings:
        finding_id = str(finding.get("id", ""))
        severity = str(finding.get("severity", ""))
        status = str(finding.get("status", "open"))
        if not finding_id:
            blocking.append("finding_id_missing")
            continue
        if finding_id in seen:
            blocking.append(f"finding_id_duplicate:{finding_id}")
        seen.add(finding_id)
        if severity not in {"P0", "P1", "P2"}:
            blocking.append(f"finding_severity_invalid:{finding_id}")
        elif severity in {"P0", "P1"} and status != "closed":
            blocking.append(f"blocking_finding_open:{finding_id}")
        elif severity == "P2":
            if status == "backlog" and finding.get("backlog_ref"):
                backlog.append(finding_id)
            elif status != "closed":
                blocking.append(f"p2_missing_backlog_or_closure:{finding_id}")
        if status == "closed" and not finding.get("resolution"):
            blocking.append(f"closed_finding_missing_resolution:{finding_id}")
    return blocking, backlog


def _prior_full_review_issues(
    index: dict[str, Any] | None,
    fingerprint: str,
    manifest_sha256: str | None,
) -> list[str]:
    if index is None:
        return []
    if index.get("schema_version") != SCHEMA_INDEX:
        return ["review_index_schema_mismatch"]
    duplicates = [
        record
        for record in index.get("v2_reviews", [])
        if record.get("review_mode") == "full"
        and record.get("composite_fingerprint") == fingerprint
        and record.get("sha256") != manifest_sha256
    ]
    return ["duplicate_full_review_for_composite_fingerprint"] if duplicates else []


def _source_packet_integrity_issues(packet: dict[str, Any]) -> list[str]:
    """Validate archived packet fingerprints without rereading changed files."""

    issues: list[str] = []
    components = packet.get("components") or {}
    fingerprints = packet.get("fingerprints") or {}
    names = ("code", "package", "evidence", "policy")
    recomputed: dict[str, str] = {}
    for name in names:
        component = components.get(name)
        if not isinstance(component, dict):
            issues.append(f"source_packet_component_missing:{name}")
            continue
        digest = component_digest(_component_payload(component))
        recomputed[name] = digest
        if component.get("sha256") != digest or fingerprints.get(name) != digest:
            issues.append(f"source_packet_component_digest_mismatch:{name}")
    composite = canonical_sha256(
        {
            "workflow": packet.get("workflow"),
            "milestone": packet.get("milestone"),
            "review_class": packet.get("review_class"),
            "component_sha256": {
                name: recomputed.get(name) for name in sorted(names)
            },
        }
    )
    if fingerprints.get("composite") != composite:
        issues.append("source_packet_composite_fingerprint_mismatch")
    return issues


def _changed_binding_paths(source: Any, target: Any) -> set[str]:
    def by_path(value: Any) -> dict[str, tuple[Any, Any, Any]]:
        if not isinstance(value, list):
            return {}
        return {
            str(item.get("path")): (
                item.get("exists"),
                item.get("bytes"),
                item.get("sha256"),
            )
            for item in value
            if isinstance(item, dict) and item.get("path")
        }

    source_map = by_path(source)
    target_map = by_path(target)
    return {
        path
        for path in set(source_map) | set(target_map)
        if source_map.get(path) != target_map.get(path)
    }


def _fingerprint_transition_issues(
    source: dict[str, Any],
    source_packet: dict[str, Any] | None,
    packet: dict[str, Any],
    manifest: dict[str, Any],
    target_lanes: set[str],
) -> list[str]:
    source_fingerprint = source.get("composite_fingerprint")
    target_fingerprint = packet.get("fingerprints", {}).get("composite")
    if source_fingerprint == target_fingerprint:
        return []
    issues: list[str] = []
    closer = manifest.get("targeted_closer") or {}
    transition = closer.get("fingerprint_transition") or {}
    if source_packet is None:
        return ["targeted_closer_source_packet_missing_for_fingerprint_transition"]
    issues.extend(_source_packet_integrity_issues(source_packet))
    if source_packet.get("fingerprints", {}).get("composite") != source_fingerprint:
        issues.append("targeted_closer_source_packet_manifest_fingerprint_mismatch")
    if transition.get("kind") != "targeted_finding_fix":
        issues.append("targeted_closer_fingerprint_transition_kind_invalid")
    if transition.get("source_composite_fingerprint") != source_fingerprint:
        issues.append("targeted_closer_transition_source_fingerprint_mismatch")
    if transition.get("target_composite_fingerprint") != target_fingerprint:
        issues.append("targeted_closer_transition_target_fingerprint_mismatch")
    if not manifest.get("invalidation_reason"):
        issues.append("targeted_closer_fingerprint_transition_missing_invalidation_reason")

    source_components = source_packet.get("components") or {}
    target_components = packet.get("components") or {}
    for component in ("policy", "package"):
        if source_packet.get("fingerprints", {}).get(component) != packet.get(
            "fingerprints", {}
        ).get(component):
            issues.append(f"targeted_closer_requires_full_review:{component}_changed")

    allowed_paths = {
        str(path)
        for lane_id in target_lanes
        for path in (packet.get("lanes", {}).get(lane_id) or {}).get(
            "allowlisted_files", []
        )
    }
    for component in ("code", "evidence"):
        source_component = source_components.get(component) or {}
        target_component = target_components.get(component) or {}
        for path in sorted(
            _changed_binding_paths(
                source_component.get("files"), target_component.get("files")
            )
        ):
            if path not in allowed_paths:
                issues.append(
                    f"targeted_closer_changed_file_outside_target_lane:{component}:{path}"
                )
    source_code = source_components.get("code") or {}
    target_code = target_components.get("code") or {}
    for symbol in target_code.get("changed_symbols", []):
        if isinstance(symbol, dict) and symbol.get("path") not in allowed_paths:
            issues.append(
                "targeted_closer_changed_symbol_outside_target_lane:"
                f"{symbol.get('path')}::{symbol.get('symbol')}"
            )
    source_evidence = source_components.get("evidence") or {}
    target_evidence = target_components.get("evidence") or {}
    for field in ("claims", "roles"):
        if source_evidence.get(field) != target_evidence.get(field):
            issues.append(f"targeted_closer_requires_full_review:evidence_{field}_changed")
    return issues


def _full_manifest_outcome(
    manifest: dict[str, Any],
    packet: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    expected_lane_ids = set(packet.get("lanes") or {})
    manifest_lanes = manifest.get("lanes") or {}
    if set(manifest_lanes) != expected_lane_ids:
        issues.append("manifest_lane_set_mismatch")
    waiver_ok, waiver_issues = _waiver_valid(manifest.get("waiver"), packet, policy)
    risk_flags = packet.get("risk_flags") or []
    for lane_id in sorted(expected_lane_ids):
        lane = manifest_lanes.get(lane_id)
        if not isinstance(lane, dict):
            continue
        expected = requested_lane_contract(policy, lane_id, risk_flags)
        issues.extend(_lane_contract_issues(lane_id, lane, expected, policy=policy))
        if lane.get("status") != "pass":
            if (
                expected["provider"] == "fable5"
                and str(lane.get("status", "")).startswith("unavailable")
                and waiver_ok
            ):
                continue
            issues.append(f"lane_not_pass:{lane_id}:{lane.get('status')}")
            if expected["provider"] == "fable5":
                issues.extend(waiver_issues)
    finding_issues, backlog = _finding_issues(review_findings(manifest))
    issues.extend(finding_issues)
    return issues, backlog


def _targeted_closer_outcome(
    manifest: dict[str, Any],
    source: dict[str, Any] | None,
    source_packet: dict[str, Any] | None,
    packet: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[list[str], list[str]]:
    if source is None:
        return ["targeted_closer_source_manifest_missing"], []
    issues: list[str] = []
    source_hash = canonical_sha256(source)
    closer = manifest.get("targeted_closer") or {}
    if closer.get("source_manifest_sha256") != source_hash:
        issues.append("targeted_closer_source_hash_mismatch")
    target_finding_ids = sorted(set(str(value) for value in closer.get("finding_ids", [])))
    supplemental_lanes = sorted(
        set(str(value) for value in closer.get("supplemental_lanes", []))
    )
    if not target_finding_ids and not supplemental_lanes:
        issues.append("targeted_closer_has_no_findings_or_supplemental_lanes")
    source_findings = {str(item.get("id")): item for item in review_findings(source)}
    for finding_id in target_finding_ids:
        finding = source_findings.get(finding_id)
        if not finding:
            issues.append(f"targeted_finding_not_in_source:{finding_id}")
        elif finding.get("severity") not in {"P0", "P1"}:
            issues.append(f"targeted_finding_not_blocking:{finding_id}")
    target_lanes = {
        str(source_findings[finding_id].get("lane"))
        for finding_id in target_finding_ids
        if finding_id in source_findings
    } | set(supplemental_lanes)
    manifest_lanes = manifest.get("lanes") or {}
    if set(manifest_lanes) != target_lanes:
        issues.append("targeted_closer_lane_scope_mismatch")
    issues.extend(
        _fingerprint_transition_issues(
            source, source_packet, packet, manifest, target_lanes
        )
    )
    risk_flags = packet.get("risk_flags") or []
    for lane_id, lane in manifest_lanes.items():
        if lane_id not in packet.get("lanes", {}):
            issues.append(f"targeted_closer_unknown_lane:{lane_id}")
            continue
        expected = requested_lane_contract(policy, lane_id, risk_flags)
        issues.extend(_lane_contract_issues(lane_id, lane, expected, policy=policy))
        if lane.get("status") != "pass":
            issues.append(f"targeted_closer_lane_not_pass:{lane_id}")

    closer_findings = {str(item.get("id")): item for item in review_findings(manifest)}
    for finding_id in target_finding_ids:
        closure = closer_findings.get(finding_id)
        if not closure or closure.get("status") != "closed" or not closure.get("resolution"):
            issues.append(f"targeted_finding_not_closed:{finding_id}")
    new_findings = [
        finding
        for finding_id, finding in closer_findings.items()
        if finding_id not in target_finding_ids
    ]
    new_issues, new_backlog = _finding_issues(new_findings)
    issues.extend(new_issues)

    unresolved_source = [
        finding
        for finding_id, finding in source_findings.items()
        if finding_id not in target_finding_ids
    ]
    source_issue_list, source_backlog = _finding_issues(unresolved_source)
    issues.extend(source_issue_list)

    source_lanes = source.get("lanes") or {}
    waiver_ok, waiver_issues = _waiver_valid(
        manifest.get("waiver") or source.get("waiver"), packet, policy
    )
    for lane_id, lane in source_lanes.items():
        if lane_id in target_lanes:
            continue
        if lane.get("status") == "pass":
            continue
        expected = requested_lane_contract(policy, lane_id, risk_flags)
        if (
            expected["provider"] == "fable5"
            and str(lane.get("status", "")).startswith("unavailable")
            and waiver_ok
        ):
            continue
        issues.append(f"source_lane_still_not_pass:{lane_id}")
        if expected["provider"] == "fable5":
            issues.extend(waiver_issues)
    return issues, sorted(set(source_backlog + new_backlog))


def validate_manifest(
    manifest: dict[str, Any],
    packet: dict[str, Any],
    *,
    root: Path = ROOT,
    policy: dict[str, Any] | None = None,
    review_index: dict[str, Any] | None = None,
    source_manifest: dict[str, Any] | None = None,
    source_packet: dict[str, Any] | None = None,
    manifest_sha256: str | None = None,
) -> dict[str, Any]:
    policy = policy or load_json(DEFAULT_POLICY)
    packet_validation = validate_packet(packet, root=root, policy=policy)
    issues = [f"packet_invalid:{value}" for value in packet_validation["issues"]]
    if manifest.get("schema_version") != SCHEMA_MANIFEST:
        issues.append("manifest_schema_mismatch")
    if "invalidation_reason" not in manifest:
        issues.append("manifest_invalidation_reason_field_missing")
    for field in ("policy_id", "workflow", "milestone", "required_stack"):
        expected = packet.get(field)
        if manifest.get(field) != expected:
            issues.append(f"manifest_packet_mismatch:{field}")
    fingerprint = packet.get("fingerprints", {}).get("composite")
    if manifest.get("composite_fingerprint") != fingerprint:
        issues.append("manifest_composite_fingerprint_mismatch")
    component_fingerprints = manifest.get("component_fingerprints")
    if component_fingerprints is not None and component_fingerprints != {
        name: packet.get("fingerprints", {}).get(name)
        for name in ("code", "evidence", "package", "policy")
    }:
        issues.append("manifest_component_fingerprints_mismatch")
    mode = manifest.get("review_mode")
    if mode not in {"full", "targeted_closer"}:
        issues.append("manifest_review_mode_invalid")
    if mode == "full" and packet.get("review_due") is not True:
        issues.append("full_review_not_due")
    if mode == "full":
        issues.extend(_prior_full_review_issues(review_index, str(fingerprint), manifest_sha256))
        outcome_issues, backlog = _full_manifest_outcome(manifest, packet, policy)
    else:
        outcome_issues, backlog = _targeted_closer_outcome(
            manifest, source_manifest, source_packet, packet, policy
        )
    issues.extend(outcome_issues)
    invalidation_reason = manifest.get("invalidation_reason")
    if "manifest_composite_fingerprint_mismatch" in issues and not invalidation_reason:
        issues.append("fingerprint_mismatch_without_invalidation_reason")
    accepted = not issues
    return {
        "schema_version": "ur10e_review_validation_v2",
        "accepted": accepted,
        "review_mode": mode,
        "composite_fingerprint": fingerprint,
        "packet_valid": packet_validation["ok"],
        "same_fingerprint_full_review_unique": (
            "duplicate_full_review_for_composite_fingerprint" not in issues
        ),
        "review_gate_satisfied_for_live_authorization": accepted,
        "live_motion_authorized": False,
        "blockers": sorted(set(issues)),
        "nonblocking_p2_backlog": sorted(set(backlog)),
        "invalidation_reason": invalidation_reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--experiment-root", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--review-index", type=Path)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--source-packet", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    policy = load_json(args.policy)
    manifest = load_json(args.manifest)
    result = validate_manifest(
        manifest,
        load_json(args.packet),
        root=args.experiment_root,
        policy=policy,
        review_index=load_json(args.review_index) if args.review_index else None,
        source_manifest=load_json(args.source_manifest) if args.source_manifest else None,
        source_packet=load_json(args.source_packet) if args.source_packet else None,
        manifest_sha256=file_sha256(args.manifest),
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if result["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
