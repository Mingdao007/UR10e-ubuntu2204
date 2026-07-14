#!/usr/bin/env python3
"""Resolve the narrow UR10e Review v3 gate without authorizing live work."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config/step5d_review_policy_v3.json"
DEFAULT_INDEX = ROOT / "config/step5d_review_index_v3.json"
BLOCKING_SEVERITIES = {"P0", "P1"}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
LANE_REQUIRED = {
    "requested_model", "actual_model", "effort", "runtime_evidence_sha256",
    "started_at", "ended_at", "status", "findings", "requested_effort",
    "actual_effort",
}


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


def canonical_composite(binding: dict[str, Any]) -> str:
    required = {"package_triplet", "controller_readback", "timing_raw", "timing_summary",
                "source_fingerprint", "effective_operator_config"}
    if set(binding) != required or not all(SHA256_RE.fullmatch(str(value) or "") for value in binding.values()):
        raise ValueError("composite binding must contain exactly six SHA-256 fields")
    encoded = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def lane_contract_valid(lane: Any) -> bool:
    return bool(isinstance(lane, dict) and LANE_REQUIRED.issubset(lane)
                and SHA256_RE.fullmatch(str(lane.get("runtime_evidence_sha256") or ""))
                and isinstance(lane.get("findings"), list)
                and lane.get("requested_model") and lane.get("actual_model")
                and lane.get("effort") and lane.get("started_at") and lane.get("ended_at"))


def exact_lane_valid(lane: dict[str, Any], provider: str, model: str) -> bool:
    return bool(lane.get("provider") == provider
                and lane.get("requested_model") == model
                and lane.get("actual_model") == model
                and lane.get("requested_effort") == "xhigh"
                and lane.get("actual_effort") == "xhigh"
                and lane.get("effort") == lane.get("actual_effort")
                and lane.get("exact_model_verified") is True)


def deterministic_closure_valid(
    closure: Any, *, gate: dict[str, Any], reviewed_composite: str,
    repaired_composite: str, finding_ids: set[str],
) -> bool:
    validation = closure.get("owner_validation") if isinstance(closure, dict) else None
    return bool(
        isinstance(closure, dict)
        and closure.get("no_reviewer_invoked") is True
        and closure.get("parent_review_manifest_sha256") == gate.get("manifest_sha256")
        and closure.get("reviewed_composite_fingerprint") == reviewed_composite
        and closure.get("repaired_composite_fingerprint") == repaired_composite
        and set(closure.get("finding_ids") or []) == finding_ids
        and isinstance(validation, dict)
        and validation.get("status") == "pass"
        and validation.get("path")
        and SHA256_RE.fullmatch(str(validation.get("sha256") or "")) is not None
        and closure.get("decision_digest") == gate.get("decision_digest")
        and SHA256_RE.fullmatch(str(closure.get("decision_digest") or "")) is not None
    )


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
    if not isinstance(composite, str) or SHA256_RE.fullmatch(composite) is None:
        result["blockers"].append("composite_fingerprint_invalid")
    if manifest is None:
        result["blockers"].append("review_v3_manifest_missing")
        result["status"] = "not_due" if gate.get("evidence_frozen") is not True else "blocked"
        return result
    if manifest.get("schema_version") != "ur10e_review_manifest_v3":
        result["blockers"].append("review_v3_manifest_schema_invalid")
    reviewed_composite = str(manifest.get("composite_fingerprint") or "")
    binding = manifest.get("composite_binding")
    try:
        recomputed = canonical_composite(binding) if isinstance(binding, dict) else None
    except ValueError:
        recomputed = None
    if recomputed != reviewed_composite:
        result["blockers"].append("review_v3_composite_not_recomputed_from_binding")
    review_mode = manifest.get("review_mode", "full")
    if review_mode != "full":
        result["blockers"].append("review_mode_must_be_single_full_review")
    count = int(
        (index.get("full_review_count_by_composite_fingerprint") or {}).get(reviewed_composite, 0)
        or 0
    )
    if count != 1:
        result["blockers"].append("full_review_count_must_equal_one")
    matching = [row for row in (index.get("review_records") or [])
                if isinstance(row, dict) and row.get("composite_fingerprint") == reviewed_composite
                and row.get("review_mode") == "full"]
    if len(matching) != 1:
        result["blockers"].append("full_review_index_record_must_equal_one")

    lanes = manifest.get("lanes") or {}
    codex = lanes.get("control_timing_claim") or {}
    fable = lanes.get("physical_operator_safety") or {}
    for lane_name, lane in lanes.items():
        degraded_fable = (lane_name == "physical_operator_safety"
                          and lane.get("status") in set(policy["execution"]["fable5_degraded_statuses"]))
        if (isinstance(lane, dict)
                and not degraded_fable
                and (lane.get("reviewed_composite_fingerprint") != reviewed_composite
                     or lane.get("reviewed_binding_sha256") != manifest.get("binding_document_sha256"))):
            result["blockers"].append(f"{lane_name}_review_input_not_bound")
    if not lane_contract_valid(codex):
        result["blockers"].append("codex_lane_runtime_contract_invalid")
    if not lane_contract_valid(fable):
        result["blockers"].append("fable_lane_runtime_contract_invalid")
    if review_mode == "full" and (not exact_lane_valid(codex, "codex", "gpt-5.6-sol")
                                  or codex.get("status") != "pass"):
        result["blockers"].append("codex_control_timing_claim_not_passed")
    fable_status = str(fable.get("status") or "missing")
    if fable_status == "pass" and exact_lane_valid(fable, "fable5", "claude-fable-5"):
        result["effective_stack"] = "1+1"
    elif fable_status in set(policy["execution"]["fable5_degraded_statuses"]):
        transcript = fable.get("degraded_transcript")
        if (isinstance(transcript, dict)
                and SHA256_RE.fullmatch(str(transcript.get("sha256") or ""))
                and transcript.get("path") and transcript.get("status") == fable_status):
            result["effective_stack"] = "1+0"
            result["degraded_review"] = True
            result["degraded_reason"] = fable_status
        else:
            result["blockers"].append("fable_degraded_transcript_missing_or_invalid")
    else:
        result["blockers"].append("fable5_lane_neither_passed_nor_degradable")
    blocking = open_blocking_findings(manifest)
    finding_ids = {str(row.get("id") or "") for row in blocking}
    closure = gate.get("deterministic_finding_closure")
    closure_valid = bool(blocking) and deterministic_closure_valid(
        closure, gate=gate, reviewed_composite=reviewed_composite,
        repaired_composite=str(composite), finding_ids=finding_ids,
    )
    if reviewed_composite != composite and not closure_valid:
        result["blockers"].append("review_v3_composite_fingerprint_stale")
    if blocking and not closure_valid:
        result["blockers"].append("open_p0_or_p1_finding")
    result["blocking_findings"] = [] if closure_valid else blocking
    result["deterministic_finding_closure_accepted"] = closure_valid
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
