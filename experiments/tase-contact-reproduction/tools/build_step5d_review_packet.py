#!/usr/bin/env python3
"""Build deterministic, lane-scoped UR10e Review v2 packets."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from step5d_review_v2 import (
    SCHEMA_INDEX,
    SCHEMA_PACKET,
    bind_files,
    canonical_sha256,
    component_digest,
    file_sha256,
    load_json,
    normalize_paths,
    path_matches_any,
    policy_review_class,
    requested_lane_contract,
    validate_commit,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "step5d_review_policy_v2.json"
DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: ?(.*))?$"
)


def _normalized_symbols(value: Any, root: Path) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("changed_symbols must be a list")
    normalized: dict[tuple[str, str], dict[str, str]] = {}
    for item in value:
        if isinstance(item, str):
            path, separator, symbol = item.partition("::")
            if not separator:
                raise ValueError("string changed_symbols must use path::symbol")
        elif isinstance(item, dict):
            path = str(item.get("path", ""))
            symbol = str(item.get("symbol", ""))
        else:
            raise ValueError("changed_symbols entries must be objects or path::symbol")
        if not path or not symbol:
            raise ValueError("changed_symbols require non-empty path and symbol")
        _, relative = _safe_relative(root, path)
        normalized[(relative, symbol)] = {"path": relative, "symbol": symbol}
    return [normalized[key] for key in sorted(normalized)]


def _safe_relative(root: Path, value: str) -> tuple[Path, str]:
    from step5d_review_v2 import relative_path

    return relative_path(root, value)


def _normalized_tests(value: Any, required_lanes: set[str], root: Path) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("tests must be a list")
    tests: dict[str, dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("test entries must be objects")
        test_id = str(raw.get("id", ""))
        if not test_id:
            raise ValueError("test id is required")
        if test_id in tests:
            raise ValueError(f"duplicate test id: {test_id}")
        path = str(raw.get("path", ""))
        relative = None
        if path:
            _, relative = _safe_relative(root, path)
        lanes = sorted(set(str(lane) for lane in raw.get("lanes", [])))
        unknown = set(lanes) - required_lanes
        if unknown:
            raise ValueError(f"test {test_id} names lanes not in this review: {sorted(unknown)}")
        tests[test_id] = {
            "id": test_id,
            "path": relative,
            "status": str(raw.get("status", "unknown")),
            "lanes": lanes,
            "command": str(raw.get("command", "")),
            "evidence": normalize_paths(raw.get("evidence"), root),
        }
    return [tests[key] for key in sorted(tests)]


def _state_resolver(
    spec: dict[str, Any], root: Path
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    path_value = spec.get("state_resolver_path")
    inline = spec.get("state_resolver")
    if path_value and inline is not None:
        raise ValueError("provide state_resolver_path or state_resolver, not both")
    if path_value:
        bindings = bind_files(root, [str(path_value)])
        binding = bindings[0]
        if binding["exists"]:
            path, _ = _safe_relative(root, str(path_value))
            snapshot = normalize_paths(load_json(path), root)
        else:
            snapshot = None
        return snapshot, {"kind": "file", **binding}
    if inline is not None:
        if not isinstance(inline, dict):
            raise ValueError("inline state_resolver must be an object")
        snapshot = normalize_paths(inline, root)
        return snapshot, {
            "kind": "inline",
            "sha256": canonical_sha256(snapshot),
        }
    return None, {"kind": "missing", "sha256": None}


def _bindings_for_component(
    spec: dict[str, Any], component: str, root: Path
) -> list[dict[str, Any]]:
    files = spec.get("files") or {}
    if not isinstance(files, dict):
        raise ValueError("files must be an object")
    values = files.get(component, [])
    if not isinstance(values, list):
        raise ValueError(f"files.{component} must be a list")
    return bind_files(root, [str(value) for value in values])


def _review_index(root: Path) -> tuple[dict[str, Any] | None, str]:
    value = root / "config" / "step5d_review_index_v2.json"
    path, relative = _safe_relative(root, str(value))
    if not path.is_file():
        return None, relative
    payload = load_json(path)
    if payload.get("schema_version") != SCHEMA_INDEX:
        raise ValueError("review index has unsupported schema")
    return payload, relative


def _changed_hunks(
    spec: dict[str, Any], root: Path
) -> tuple[dict[str, Any] | None, list[str]]:
    value = spec.get("changed_hunks")
    if value is None:
        return None, []
    if not isinstance(value, dict) or not value.get("patch_path"):
        return None, ["changed_hunks_patch_path_missing"]
    patch_path, relative = _safe_relative(root, str(value["patch_path"]))
    binding = bind_files(root, [relative])[0]
    blockers: list[str] = []
    if not binding["exists"]:
        return {"patch": binding, "hunks": []}, [f"changed_hunks_patch_missing:{relative}"]
    expected_sha256 = value.get("patch_sha256")
    if expected_sha256 is not None and expected_sha256 != binding["sha256"]:
        blockers.append("changed_hunks_patch_sha256_mismatch")

    current_path: str | None = None
    hunks: list[dict[str, Any]] = []
    for line in patch_path.read_text(encoding="utf-8").splitlines():
        diff_match = DIFF_HEADER_RE.match(line)
        if diff_match:
            old_path, new_path = diff_match.groups()
            current_path = new_path if new_path != "/dev/null" else old_path
            try:
                _, current_path = _safe_relative(root, current_path)
            except ValueError:
                blockers.append("changed_hunks_patch_path_escapes_root")
                current_path = None
            continue
        hunk_match = HUNK_HEADER_RE.match(line)
        if hunk_match and current_path:
            old_start, old_count, new_start, new_count, context = hunk_match.groups()
            hunks.append(
                {
                    "path": current_path,
                    "old_start": int(old_start),
                    "old_count": int(old_count or "1"),
                    "new_start": int(new_start),
                    "new_count": int(new_count or "1"),
                    "context": context or "",
                    "header": line,
                }
            )
    if not hunks:
        blockers.append("changed_hunks_patch_has_no_hunks")
    return {"patch": binding, "hunks": hunks}, blockers


def _evidence_role_path(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("path"):
        return str(value["path"])
    return None


def build_packet(
    spec: dict[str, Any],
    *,
    root: Path = ROOT,
    policy_path: Path = DEFAULT_POLICY,
) -> dict[str, Any]:
    root = root.resolve()
    policy_path = policy_path.resolve()
    policy = load_json(policy_path)
    workflow = str(spec.get("workflow", ""))
    milestone = str(spec.get("milestone", ""))
    review_class_id, review_class = policy_review_class(policy, workflow, milestone)
    required_lanes = [str(value) for value in review_class.get("lanes", [])]
    required_lane_set = set(required_lanes)
    risk_flags = sorted(set(str(value) for value in spec.get("risk_flags", [])))
    changed_symbols = _normalized_symbols(spec.get("changed_symbols"), root)
    tests = _normalized_tests(spec.get("tests"), required_lane_set, root)
    changed_hunks, changed_hunk_blockers = _changed_hunks(spec, root)
    state_snapshot, state_source = _state_resolver(spec, root)
    claims = normalize_paths(spec.get("claims"), root)
    if claims is None:
        claims = {}
    if not isinstance(claims, dict):
        raise ValueError("claims must be an object")
    package_identity = normalize_paths(spec.get("package_identity"), root)
    if package_identity is None:
        package_identity = {}
    if not isinstance(package_identity, dict):
        raise ValueError("package_identity must be an object")
    evidence_roles = normalize_paths(spec.get("evidence_roles"), root)
    if evidence_roles is None:
        evidence_roles = {}
    if not isinstance(evidence_roles, dict):
        raise ValueError("evidence_roles must be an object")

    bindings = {
        component: _bindings_for_component(spec, component, root)
        for component in ("code", "package", "evidence")
    }
    base_commit = spec.get("base_commit")
    head_commit = spec.get("head_commit")
    evidence_frozen = spec.get("evidence_frozen") is True

    code_payload = {
        "base_commit": base_commit,
        "head_commit": head_commit,
        "changed_symbols": changed_symbols,
        "changed_hunks": changed_hunks,
        "files": bindings["code"],
    }
    package_payload = {
        "files": bindings["package"],
        "identity": package_identity,
    }
    evidence_payload = {
        "files": bindings["evidence"],
        "state_resolver": state_snapshot,
        "state_resolver_source": state_source,
        "tests": tests,
        "claims": claims,
        "roles": evidence_roles,
        "evidence_frozen": evidence_frozen,
    }
    _, policy_relative = _safe_relative(root, str(policy_path))
    policy_payload = {
        "path": policy_relative,
        "file_sha256": file_sha256(policy_path),
        "canonical_sha256": canonical_sha256(policy),
        "policy_id": policy.get("policy_id"),
    }
    components = {
        "code": {"sha256": component_digest(code_payload), **code_payload},
        "package": {
            "sha256": component_digest(package_payload),
            **package_payload,
        },
        "evidence": {
            "sha256": component_digest(evidence_payload),
            **evidence_payload,
        },
        "policy": {
            "sha256": component_digest(policy_payload),
            **policy_payload,
        },
    }
    composite_payload = {
        "workflow": workflow,
        "milestone": milestone,
        "review_class": review_class_id,
        "component_sha256": {
            name: component["sha256"] for name, component in sorted(components.items())
        },
    }
    composite_sha256 = canonical_sha256(composite_payload)

    blockers: list[str] = []
    blockers.extend(changed_hunk_blockers)
    code_binding_paths = {item["path"] for item in bindings["code"]}
    for hunk_path in sorted(
        {str(item["path"]) for item in (changed_hunks or {}).get("hunks", [])}
        - code_binding_paths
    ):
        blockers.append(f"changed_hunk_path_not_bound_as_code:{hunk_path}")
    if not validate_commit(base_commit):
        blockers.append("base_commit_not_full_sha1")
    if not validate_commit(head_commit):
        blockers.append("head_commit_not_full_sha1")
    if state_snapshot is None:
        blockers.append("state_resolver_missing")
    for component, component_bindings in bindings.items():
        for binding in component_bindings:
            if not binding["exists"]:
                blockers.append(f"{component}_file_missing:{binding['path']}")
    required_claims = set(str(value) for value in policy.get("claim_boundaries", []))
    missing_claims = sorted(required_claims - set(claims))
    if review_class.get("full_review_required") and missing_claims:
        blockers.extend(f"claim_boundary_missing:{name}" for name in missing_claims)
    if claims.get("reproduction_complete") is True and claims.get("live_accepted") is not True:
        blockers.append("reproduction_complete_without_live_accepted")
    if claims.get("live_accepted") is True and claims.get("package_accepted") is not True:
        blockers.append("live_accepted_without_package_accepted")
    if milestone in {"pre_live", "baseline_re_review", "evidence_freeze"}:
        if claims.get("live_accepted") is True or claims.get("reproduction_complete") is True:
            blockers.append("pre_live_packet_crosses_claim_boundary")
    failed_tests = [test["id"] for test in tests if test["status"] != "pass"]
    blockers.extend(f"focused_test_not_pass:{test_id}" for test_id in failed_tests)
    unknown_risk_flags = sorted(
        set(risk_flags) - set(str(value) for value in policy.get("max_escalation_flags", []))
    )
    blockers.extend(f"unknown_risk_flag:{value}" for value in unknown_risk_flags)
    if review_class_id.startswith("ordinary_") and risk_flags:
        blockers.append("ordinary_0_plus_0_cannot_carry_review_risk_flags")
    code_change_declared = bool(bindings["code"] or changed_symbols)
    if (
        review_class.get("full_review_required") is True
        and base_commit != head_commit
        and code_change_declared
        and changed_hunks is None
    ):
        blockers.append("changed_hunks_patch_missing_for_code_change")
    if review_class.get("full_review_required") and evidence_frozen and not bindings["evidence"]:
        blockers.append("frozen_review_has_no_evidence_files")
    if review_class.get("requires_package_identity") is True and not package_identity:
        blockers.append("package_identity_missing")
    required_evidence_roles = set(
        str(value) for value in review_class.get("required_evidence_roles", [])
    )
    blockers.extend(
        f"evidence_role_missing:{role}"
        for role in sorted(required_evidence_roles - set(evidence_roles))
    )
    bound_evidence = {binding["path"]: binding for binding in bindings["evidence"]}
    for role, value in sorted(evidence_roles.items()):
        role_path = _evidence_role_path(value)
        if role_path not in bound_evidence:
            blockers.append(f"evidence_role_not_bound_to_evidence_file:{role}")
            continue
        if isinstance(value, dict) and value.get("sha256") not in {
            None,
            bound_evidence[role_path]["sha256"],
        }:
            blockers.append(f"evidence_role_sha256_mismatch:{role}")

    all_file_paths = sorted(
        {
            binding["path"]
            for component_bindings in bindings.values()
            for binding in component_bindings
        }
    )
    lane_packets: dict[str, Any] = {}
    lane_contracts: list[dict[str, Any]] = []
    maximum_tests = int(policy["execution"]["max_focused_tests_per_lane"])
    for lane_id in required_lanes:
        contract = requested_lane_contract(policy, lane_id, risk_flags)
        lane_contracts.append(contract)
        lane_tests = [test for test in tests if lane_id in test["lanes"]]
        if len(lane_tests) > maximum_tests:
            blockers.append(f"lane_focused_test_limit_exceeded:{lane_id}")
        allowed_files = [
            path for path in all_file_paths if path_matches_any(path, contract["file_globs"])
        ]
        allowed_symbols = [
            symbol
            for symbol in changed_symbols
            if path_matches_any(symbol["path"], contract["file_globs"])
        ]
        allowed_hunks = [
            hunk
            for hunk in (changed_hunks or {}).get("hunks", [])
            if path_matches_any(hunk["path"], contract["file_globs"])
        ]
        lane_packets[lane_id] = {
            "allowlisted_files": allowed_files,
            "changed_symbols": allowed_symbols,
            "changed_hunks": allowed_hunks,
            "focused_tests": lane_tests,
            "max_focused_tests": maximum_tests,
        }

    full_review_required = review_class.get("full_review_required") is True
    freeze_blockers = []
    if full_review_required and not evidence_frozen:
        freeze_blockers.append("evidence_not_frozen")
    prior_full_reviews = []
    review_index, review_index_relative = _review_index(root)
    if full_review_required and review_index is None:
        freeze_blockers.append("canonical_review_index_missing")
    if review_index:
        prior_full_reviews = [
            record
            for record in review_index.get("v2_reviews", [])
            if record.get("review_mode") == "full"
            and record.get("composite_fingerprint") == composite_sha256
        ]
        if prior_full_reviews:
            freeze_blockers.append("full_review_already_recorded_for_fingerprint")

    fable_required = any(contract["provider"] == "fable5" for contract in lane_contracts)
    preflight = normalize_paths(spec.get("fable5_preflight"), root)
    if not isinstance(preflight, dict):
        preflight = {"status": "not_checked"}
    preflight_status = str(preflight.get("status", "not_checked"))
    allowed_preflight_statuses = set(
        str(value) for value in policy.get("fable5_preflight_statuses", [])
    )
    preflight_valid = preflight_status in allowed_preflight_statuses
    preflight_complete = not fable_required or preflight_valid
    review_start_blockers = []
    if fable_required and not preflight_complete:
        review_start_blockers.append(
            "fable5_preflight_not_run"
            if preflight_status == "not_checked"
            else "fable5_preflight_status_invalid"
        )

    review_due = bool(
        full_review_required and evidence_frozen and not blockers and not freeze_blockers
    )
    invocation_plan: list[dict[str, Any]] = []
    for contract in lane_contracts:
        provider_available = True
        availability = "available"
        if contract["provider"] == "fable5":
            provider_available = preflight_status == "available"
            availability = preflight_status
        start_allowed = review_due and preflight_complete and provider_available
        invocation_plan.append(
            {
                **{key: value for key, value in contract.items() if key != "file_globs"},
                "availability": availability,
                "start_allowed": start_allowed,
            }
        )
    if not full_review_required:
        invocation_plan = []

    live_authorization_blockers = list(blockers) + list(freeze_blockers)
    if fable_required and preflight_status != "available":
        live_authorization_blockers.append(
            "fable5_review_requires_supplement_or_fingerprint_bound_user_waiver"
        )

    return {
        "schema_version": SCHEMA_PACKET,
        "policy_id": policy.get("policy_id"),
        "workflow": workflow,
        "milestone": milestone,
        "base_commit": base_commit,
        "head_commit": head_commit,
        "review_class": review_class_id,
        "required_stack": review_class.get("stack"),
        "full_review_required": full_review_required,
        "evidence_frozen": evidence_frozen,
        "fingerprints": {
            "code": components["code"]["sha256"],
            "package": components["package"]["sha256"],
            "evidence": components["evidence"]["sha256"],
            "policy": components["policy"]["sha256"],
            "composite": composite_sha256,
        },
        "components": components,
        "risk_flags": risk_flags,
        "state_resolver": state_snapshot,
        "changed_symbols": changed_symbols,
        "changed_hunks": changed_hunks,
        "tests": tests,
        "claims": claims,
        "package_identity": package_identity,
        "evidence_roles": evidence_roles,
        "lanes": lane_packets,
        "fable5_preflight": preflight,
        "review_due": review_due,
        "review_start_allowed": any(item["start_allowed"] for item in invocation_plan),
        "invocation_plan": invocation_plan,
        "prior_full_review_count_for_fingerprint": len(prior_full_reviews),
        "review_index_path": review_index_relative,
        "review_index_present": review_index is not None,
        "blockers": sorted(set(blockers)),
        "freeze_blockers": sorted(set(freeze_blockers)),
        "review_start_blockers": sorted(set(review_start_blockers)),
        "live_authorization_blockers": sorted(set(live_authorization_blockers)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--experiment-root", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    args = parser.parse_args()
    packet = build_packet(
        load_json(args.input), root=args.experiment_root, policy_path=args.policy
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(packet, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
