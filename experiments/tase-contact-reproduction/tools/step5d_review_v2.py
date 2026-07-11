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
    escalation = bool(
        set(str(flag) for flag in risk_flags)
        & set(str(flag) for flag in policy.get("max_escalation_flags", []))
    )
    effort = str(defaults.get("reasoning_effort"))
    if provider == "codex" and escalation:
        effort = "max"
    return {
        "lane": lane_id,
        "provider": provider,
        "requested_model": str(defaults.get("model")),
        "requested_effort": effort,
        "timeout_seconds": int(policy["execution"]["lane_timeout_seconds"]),
        "file_globs": sorted(set(str(value) for value in lane.get("file_globs", []))),
    }


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
