#!/usr/bin/env python3
"""Shared deterministic primitives for UR10e Review v2 artifacts."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


SCHEMA_POLICY = "ur10e_review_policy_v2"
SCHEMA_PACKET = "ur10e_review_packet_v2"
SCHEMA_MANIFEST = "ur10e_review_manifest_v2"
SCHEMA_INDEX = "ur10e_review_index_v2"
HEX_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
FULL_REVIEW_INDEX_ENTRY_FIELDS = (
    "bytes",
    "composite_fingerprint",
    "finding_counts",
    "invalidation_reason",
    "milestone",
    "path",
    "required_stack",
    "review_mode",
    "sha256",
    "workflow",
)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def relative_path(root: Path, candidate: str | Path) -> tuple[Path, str]:
    root = root.resolve()
    raw = Path(candidate)
    resolved = (root / raw).resolve() if not raw.is_absolute() else raw.resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes experiment root: {candidate}") from exc
    return resolved, relative.as_posix()


def bind_files(root: Path, paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    normalized: dict[str, Path] = {}
    for candidate in paths:
        resolved, relative = relative_path(root, candidate)
        normalized[relative] = resolved
    bindings: list[dict[str, Any]] = []
    for relative in sorted(normalized):
        path = normalized[relative]
        exists = path.is_file()
        bindings.append(
            {
                "path": relative,
                "exists": exists,
                "bytes": path.stat().st_size if exists else None,
                "sha256": file_sha256(path) if exists else None,
            }
        )
    return bindings


def normalize_paths(value: Any, root: Path) -> Any:
    """Normalize worktree-local absolute paths before hashing portable evidence."""

    if isinstance(value, dict):
        return {
            str(key): normalize_paths(item, root)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        return [normalize_paths(item, root) for item in value]
    if isinstance(value, str) and value.startswith("/"):
        path = Path(value)
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            return value
    return value


def policy_review_class(
    policy: dict[str, Any], workflow: str, milestone: str
) -> tuple[str, dict[str, Any]]:
    if policy.get("schema_version") != SCHEMA_POLICY:
        raise ValueError("unsupported review policy schema")
    mapping = (policy.get("workflow_milestone_map") or {}).get(workflow) or {}
    class_id = mapping.get(milestone)
    if not class_id:
        raise ValueError(f"unsupported workflow/milestone: {workflow}/{milestone}")
    review_class = (policy.get("review_classes") or {}).get(class_id)
    if not isinstance(review_class, dict):
        raise ValueError(f"review class missing from policy: {class_id}")
    return str(class_id), review_class


def requested_lane_contract(
    policy: dict[str, Any], lane_id: str, risk_flags: Iterable[str]
) -> dict[str, Any]:
    lane = (policy.get("lanes") or {}).get(lane_id)
    if not isinstance(lane, dict):
        raise ValueError(f"unknown review lane: {lane_id}")
    provider = str(lane.get("provider"))
    defaults = (policy.get("defaults") or {}).get(provider)
    if not isinstance(defaults, dict):
        raise ValueError(f"missing provider defaults: {provider}")
    escalation_reasons = sorted(
        set(str(flag) for flag in risk_flags)
        & set(str(flag) for flag in policy.get("max_escalation_flags", []))
    )
    effort = str(defaults.get("reasoning_effort"))
    if provider == "codex" and escalation_reasons:
        effort = "max"
    return {
        "lane": lane_id,
        "provider": provider,
        "requested_model": str(defaults.get("model")),
        "requested_effort": effort,
        "effort_escalation_reasons": (
            escalation_reasons if provider == "codex" else []
        ),
        "timeout_seconds": int(policy["execution"]["lane_timeout_seconds"]),
        "attempt_limit": int(policy["execution"]["lane_attempt_limit"]),
        "automatic_restart": bool(policy["execution"]["automatic_timeout_restart"]),
        "file_globs": sorted(set(str(value) for value in lane.get("file_globs", []))),
    }


def waiver_validation_issues(
    waiver: Any,
    *,
    composite_fingerprint: str,
    workflow: str,
    milestone: str,
    policy: dict[str, Any],
) -> list[str]:
    """Validate the exact one-fingerprint Fable5 waiver contract."""

    if not isinstance(waiver, dict):
        return ["fable5_waiver_missing"]
    issues: list[str] = []
    waiver_policy = policy["waiver"]
    if waiver.get("provider") != waiver_policy["allowed_provider"]:
        issues.append("waiver_provider_mismatch")
    if waiver.get("lane") != waiver_policy["allowed_lane"]:
        issues.append("waiver_lane_mismatch")
    if waiver.get("scope") != waiver_policy["scope"]:
        issues.append("waiver_scope_mismatch")
    if waiver.get("workflow") != workflow:
        issues.append("waiver_workflow_mismatch")
    if waiver.get("milestone") != milestone:
        issues.append("waiver_milestone_mismatch")
    if waiver.get("composite_fingerprint") != composite_fingerprint:
        issues.append("waiver_fingerprint_mismatch")
    if waiver.get("authorized_by") != "user" or waiver.get("explicit") is not True:
        issues.append("waiver_not_explicit_user_authorization")
    for field in ("waiver_id", "issued_at", "authorization_evidence", "reason"):
        if not waiver.get(field):
            issues.append(f"waiver_field_missing:{field}")
    return issues


def reviewer_lane_schema_issues(
    lane: Any, schema: dict[str, Any]
) -> list[str]:
    """Validate the project lane schema without an optional jsonschema dependency."""

    if not isinstance(lane, dict):
        return ["lane_schema_instance_not_object"]
    issues: list[str] = []
    properties = schema.get("properties") or {}
    for field in schema.get("required", []):
        if field not in lane:
            issues.append(f"lane_schema_required_missing:{field}")
    if schema.get("additionalProperties") is False:
        for field in sorted(set(lane) - set(properties)):
            issues.append(f"lane_schema_additional_property:{field}")

    def type_ok(value: Any, expected: Any) -> bool:
        names = expected if isinstance(expected, list) else [expected]
        checks = {
            "null": value is None,
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
            "boolean": isinstance(value, bool),
        }
        return any(checks.get(str(name), False) for name in names)

    for field, rule in properties.items():
        if field not in lane or not isinstance(rule, dict):
            continue
        value = lane[field]
        if "type" in rule and not type_ok(value, rule["type"]):
            issues.append(f"lane_schema_type:{field}")
            continue
        if "const" in rule and value != rule["const"]:
            issues.append(f"lane_schema_const:{field}")
        if "enum" in rule and value not in rule["enum"]:
            issues.append(f"lane_schema_enum:{field}")
        if isinstance(value, str):
            if len(value) < int(rule.get("minLength", 0)):
                issues.append(f"lane_schema_min_length:{field}")
            pattern = rule.get("pattern")
            if pattern and re.fullmatch(str(pattern), value) is None:
                issues.append(f"lane_schema_pattern:{field}")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in rule and value < rule["minimum"]:
                issues.append(f"lane_schema_minimum:{field}")
        if isinstance(value, list) and rule.get("uniqueItems") is True:
            rendered = [canonical_json_bytes(item) for item in value]
            if len(rendered) != len(set(rendered)):
                issues.append(f"lane_schema_unique_items:{field}")
        if isinstance(value, list) and isinstance(rule.get("items"), dict):
            item_rule = rule["items"]
            for index, item in enumerate(value):
                if "type" in item_rule and not type_ok(item, item_rule["type"]):
                    issues.append(f"lane_schema_item_type:{field}:{index}")
                    continue
                if isinstance(item, str) and len(item) < int(item_rule.get("minLength", 0)):
                    issues.append(f"lane_schema_item_min_length:{field}:{index}")
                if isinstance(item, dict):
                    item_properties = item_rule.get("properties") or {}
                    for required in item_rule.get("required", []):
                        if required not in item:
                            issues.append(
                                f"lane_schema_item_required_missing:{field}:{index}:{required}"
                            )
                    if item_rule.get("additionalProperties") is False:
                        for extra in sorted(set(item) - set(item_properties)):
                            issues.append(
                                f"lane_schema_item_additional_property:{field}:{index}:{extra}"
                            )
                    for item_field, item_field_rule in item_properties.items():
                        if item_field not in item:
                            continue
                        item_value = item[item_field]
                        if "type" in item_field_rule and not type_ok(
                            item_value, item_field_rule["type"]
                        ):
                            issues.append(
                                f"lane_schema_item_field_type:{field}:{index}:{item_field}"
                            )
                        if "enum" in item_field_rule and item_value not in item_field_rule["enum"]:
                            issues.append(
                                f"lane_schema_item_field_enum:{field}:{index}:{item_field}"
                            )

    status = lane.get("status")
    if status == "pass":
        if lane.get("verdict") != "pass":
            issues.append("lane_schema_pass_verdict")
        if not isinstance(lane.get("actual_model"), str) or not lane["actual_model"]:
            issues.append("lane_schema_pass_actual_model")
        if not isinstance(lane.get("actual_effort"), str) or not lane["actual_effort"]:
            issues.append("lane_schema_pass_actual_effort")
    if status == "unavailable_timeout":
        if lane.get("verdict") != "unavailable_timeout":
            issues.append("lane_schema_timeout_verdict")
        if lane.get("timeout_disposition") != "unavailable":
            issues.append("lane_schema_timeout_disposition")
    return issues


def path_matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def component_digest(payload: dict[str, Any]) -> str:
    return canonical_sha256(payload)


def validate_commit(value: Any) -> bool:
    return isinstance(value, str) and HEX_COMMIT_RE.fullmatch(value) is not None


def review_findings(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for lane_id, lane in sorted((manifest.get("lanes") or {}).items()):
        if not isinstance(lane, dict):
            continue
        for raw in lane.get("findings", []):
            if isinstance(raw, dict):
                findings.append({**raw, "lane": lane_id})
    return findings


def full_review_index_projection(index: dict[str, Any]) -> dict[str, Any]:
    """Project the index to the immutable full-review uniqueness surface.

    Targeted closers are hash-bound directly by their source/target manifests.
    They must not invalidate an unrelated workflow's frozen readiness artifact.
    """

    return {
        "blockers": index.get("blockers", []),
        "duplicate_full_review_fingerprints": index.get(
            "duplicate_full_review_fingerprints", {}
        ),
        "full_review_count_by_composite_fingerprint": index.get(
            "full_review_count_by_composite_fingerprint", {}
        ),
        "historical_artifacts": index.get("historical_artifacts", []),
        "policy_id": index.get("policy_id"),
        "schema_version": index.get("schema_version"),
        "v2_reviews": [
            {
                field: item.get(field)
                for field in FULL_REVIEW_INDEX_ENTRY_FIELDS
                if field in item
            }
            for item in index.get("v2_reviews", [])
            if isinstance(item, dict) and item.get("review_mode") == "full"
        ],
    }


def full_review_index_projection_sha256(index: dict[str, Any]) -> str:
    rendered = json.dumps(full_review_index_projection(index), indent=2, sort_keys=True)
    return hashlib.sha256((rendered + "\n").encode("utf-8")).hexdigest()
