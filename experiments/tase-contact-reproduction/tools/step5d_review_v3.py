#!/usr/bin/env python3
"""Resolve the narrow UR10e Review v3 gate without authorizing live work."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config/step5d_review_policy_v3.json"
DEFAULT_INDEX = ROOT / "config/step5d_review_index_v3.json"
BLOCKING_SEVERITIES = {"P0", "P1"}


def load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def review_class(policy: dict[str, Any], workflow: str, milestone: str) -> tuple[str, dict[str, Any]]:
    try:
        class_name = policy["workflow_milestone_map"][workflow][milestone]
        return class_name, policy["review_classes"][class_name]
    except KeyError as exc:
        raise ValueError(f"unknown Review v3 route: {workflow}/{milestone}") from exc


def open_blocking_findings(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for lane in (manifest.get("lanes") or {}).values():
        if not isinstance(lane, dict):
            continue
        for finding in lane.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            if finding.get("severity") in BLOCKING_SEVERITIES and finding.get("status", "open") != "closed":
                findings.append(finding)
    return findings


def resolve(
    *,
    workflow: str,
    milestone: str,
    gate: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    index: dict[str, Any] | None = None,
) -> dict[str, Any]:
    policy = policy or load(DEFAULT_POLICY)
    index = index or load(DEFAULT_INDEX)
    gate = gate or {}
    class_name, selected = review_class(policy, workflow, milestone)
    required_stack = selected["stack"]
    result: dict[str, Any] = {
        "policy_id": policy["policy_id"],
        "workflow": workflow,
        "milestone": milestone,
        "review_class": class_name,
        "required_stack": required_stack,
        "effective_stack": required_stack,
        "review_invocation_required": selected.get("full_review_required") is True,
        "accepted": False,
        "degraded_review": False,
        "blockers": [],
    }
    if required_stack == "0+0":
        result["accepted"] = True
        result["status"] = "not_required"
        if selected.get("merge_into_next_contact_composite_fingerprint") is True:
            result["merge_into_next_contact_composite_fingerprint"] = True
        return result

    composite = gate.get("composite_fingerprint")
    result["composite_fingerprint"] = composite
    if gate.get("evidence_frozen") is not True:
        result["blockers"].append("deterministic_evidence_not_frozen")
    if not isinstance(composite, str) or len(composite) != 64:
        result["blockers"].append("composite_fingerprint_invalid")
    if manifest is None:
        result["blockers"].append("review_v3_manifest_missing")
        result["status"] = "not_due" if gate.get("evidence_frozen") is not True else "blocked"
        return result
    if manifest.get("schema_version") != "ur10e_review_manifest_v3":
        result["blockers"].append("review_v3_manifest_schema_invalid")
    if manifest.get("composite_fingerprint") != composite:
        result["blockers"].append("review_v3_composite_fingerprint_stale")
    if manifest.get("review_mode", "full") == "full":
        count = int(
            (index.get("full_review_count_by_composite_fingerprint") or {}).get(composite, 0)
            or 0
        )
        if count > 1:
            result["blockers"].append("duplicate_full_review_for_composite_fingerprint")

    lanes = manifest.get("lanes") or {}
    codex = lanes.get("control_timing_claim") or {}
    fable = lanes.get("physical_operator_safety") or {}
    if codex.get("provider") != "codex" or codex.get("status") != "pass":
        result["blockers"].append("codex_control_timing_claim_not_passed")
    fable_status = str(fable.get("status") or "missing")
    if fable_status == "pass" and fable.get("provider") == "fable5" and fable.get("exact_model_verified") is True:
        result["effective_stack"] = "1+1"
    elif fable_status in set(policy["execution"]["fable5_degraded_statuses"]):
        result["effective_stack"] = "1+0"
        result["degraded_review"] = True
        result["degraded_reason"] = fable_status
    else:
        result["blockers"].append("fable5_lane_neither_passed_nor_degradable")
    blocking = open_blocking_findings(manifest)
    if blocking:
        result["blockers"].append("open_p0_or_p1_finding")
    result["blocking_findings"] = blocking
    result["blockers"] = sorted(set(result["blockers"]))
    result["accepted"] = not result["blockers"] and result["effective_stack"] in {"1+1", "1+0"}
    result["status"] = "accepted" if result["accepted"] else "blocked"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow")
    parser.add_argument("milestone")
    parser.add_argument("--gate", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX)
    args = parser.parse_args()
    result = resolve(
        workflow=args.workflow,
        milestone=args.milestone,
        gate=load(args.gate) if args.gate else {},
        manifest=load(args.manifest) if args.manifest else None,
        policy=load(args.policy),
        index=load(args.index),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["accepted"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
