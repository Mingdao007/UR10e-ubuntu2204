#!/usr/bin/env python3
"""Build the immutable v29 Review v2 state projection.

The projection compares only v29-owned state against the reviewed git commit,
so later v30 edits in the shared Step5 table cannot invalidate the frozen v29
baseline review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from step5d_review_v2 import canonical_sha256, file_sha256, load_json


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parents[1]
PROGRAM = "step5d_strict_rnn_ablation_v29"
REVIEWED_HEAD = "7e6e5278d8c7ce0a13ce5b444918d89eb3d23a58"
CURRENT_STAGE = "config/current_stage.json"
STAGE_TABLE = "config/step5_stage_table.json"
POLICY = "config/step5d_review_policy_v2.json"
CLOSER_PACKET = "config/reviews/v29_baseline_review_v2_closer_packet.json"
CLOSER_MANIFEST = "config/reviews/v29_baseline_review_v2_closer_manifest.json"
CLOSER_VALIDATION = "config/reviews/v29_baseline_review_v2_closer_validation.json"
DEFAULT_OUTPUT = ROOT / "config/reviews/v29_baseline_review_v2_state_projection.json"


def _repo_relative(relative: str) -> str:
    return (ROOT.relative_to(REPO_ROOT) / relative).as_posix()


def _git_bytes(commit: str, relative: str) -> bytes:
    return subprocess.check_output(
        ["git", "show", f"{commit}:{_repo_relative(relative)}"],
        cwd=REPO_ROOT,
    )


def _git_json(commit: str, relative: str) -> dict[str, Any]:
    value = json.loads(_git_bytes(commit, relative).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"reviewed source is not an object: {relative}")
    return value


def _stage_row(table: dict[str, Any]) -> dict[str, Any]:
    row = next(
        (item for item in table.get("stages", []) if item.get("id") == PROGRAM),
        None,
    )
    if not isinstance(row, dict):
        raise ValueError(f"missing stage row: {PROGRAM}")
    return row


def state_projection(
    current: dict[str, Any], table: dict[str, Any]
) -> dict[str, Any]:
    row = _stage_row(table)
    row_fields = (
        "id",
        "active",
        "blocked",
        "bridge",
        "contact",
        "complete",
        "local_delivery_evidence",
        "package_delivery",
        "review_v2",
        "runtime_profile",
        "strict_rnn",
        "contact_policy",
        "acceptance",
        "liveprep_status",
        "live_run_status",
        "reproduction_status",
        "success_condition",
    )
    return {
        "program": PROGRAM,
        "current_stage_v29_candidate": current.get("v29_contact_candidate"),
        "stage_table_v29": {field: row.get(field) for field in row_fields},
    }


def build(*, root: Path = ROOT) -> dict[str, Any]:
    reviewed_current_bytes = _git_bytes(REVIEWED_HEAD, CURRENT_STAGE)
    reviewed_table_bytes = _git_bytes(REVIEWED_HEAD, STAGE_TABLE)
    reviewed_policy_bytes = _git_bytes(REVIEWED_HEAD, POLICY)
    reviewed_projection = state_projection(
        json.loads(reviewed_current_bytes), json.loads(reviewed_table_bytes)
    )
    current_projection = state_projection(
        load_json(root / CURRENT_STAGE), load_json(root / STAGE_TABLE)
    )
    reviewed_sha = canonical_sha256(reviewed_projection)
    current_sha = canonical_sha256(current_projection)
    packet = load_json(root / CLOSER_PACKET)
    return {
        "schema_version": "ur10e_v29_review_state_projection_v1",
        "workflow": "v29",
        "milestone": "baseline_re_review",
        "program": PROGRAM,
        "reviewed_head_commit": REVIEWED_HEAD,
        "reviewed_composite_fingerprint": packet.get("fingerprints", {}).get(
            "composite"
        ),
        "reviewed_source_files": {
            CURRENT_STAGE: hashlib.sha256(reviewed_current_bytes).hexdigest(),
            STAGE_TABLE: hashlib.sha256(reviewed_table_bytes).hexdigest(),
            POLICY: hashlib.sha256(reviewed_policy_bytes).hexdigest(),
        },
        "reviewed_projection": reviewed_projection,
        "reviewed_projection_sha256": reviewed_sha,
        "current_projection_sha256": current_sha,
        "current_projection_matches_reviewed": current_sha == reviewed_sha,
        "review_artifacts": {
            CLOSER_PACKET: {
                "file_sha256": file_sha256(root / CLOSER_PACKET),
                "canonical_sha256": canonical_sha256(packet),
            },
            CLOSER_MANIFEST: {
                "file_sha256": file_sha256(root / CLOSER_MANIFEST)
            },
            CLOSER_VALIDATION: {
                "file_sha256": file_sha256(root / CLOSER_VALIDATION)
            },
        },
        "blockers": (
            [] if current_sha == reviewed_sha else ["v29_reviewed_state_projection_drift"]
        ),
    }


def validate_for_packet(
    packet: dict[str, Any], *, root: Path = ROOT
) -> tuple[bool, list[str], set[str]]:
    path = root / "config/reviews/v29_baseline_review_v2_state_projection.json"
    if not path.is_file():
        return False, ["v29_immutable_state_projection_missing"], set()
    tracked = load_json(path)
    rebuilt = build(root=root)
    issues: list[str] = []
    if tracked != rebuilt:
        issues.append("v29_immutable_state_projection_drift")
    if tracked.get("blockers"):
        issues.extend(str(value) for value in tracked["blockers"])
    if packet.get("workflow") != "v29" or packet.get("milestone") != "baseline_re_review":
        issues.append("v29_immutable_projection_packet_route_mismatch")
    if packet.get("head_commit") != tracked.get("reviewed_head_commit"):
        issues.append("v29_immutable_projection_head_mismatch")
    if packet.get("fingerprints", {}).get("composite") != tracked.get(
        "reviewed_composite_fingerprint"
    ):
        issues.append("v29_immutable_projection_fingerprint_mismatch")
    packet_binding = tracked.get("review_artifacts", {}).get(CLOSER_PACKET) or {}
    if canonical_sha256(packet) != packet_binding.get("canonical_sha256"):
        issues.append("v29_immutable_projection_packet_hash_mismatch")
    reviewed_files = tracked.get("reviewed_source_files") or {}
    packet_files = {
        item.get("path"): item.get("sha256")
        for component in (packet.get("components") or {}).values()
        for item in (component.get("files", []) if isinstance(component, dict) else [])
        if isinstance(item, dict)
    }
    for relative in (CURRENT_STAGE, STAGE_TABLE):
        if packet_files.get(relative) != reviewed_files.get(relative):
            issues.append(f"v29_immutable_projection_reviewed_file_mismatch:{relative}")
    policy_file_sha = (packet.get("components", {}).get("policy") or {}).get(
        "file_sha256"
    )
    if policy_file_sha != reviewed_files.get(POLICY):
        issues.append("v29_immutable_projection_reviewed_policy_mismatch")
    return not issues, sorted(set(issues)), {CURRENT_STAGE, STAGE_TABLE}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = build()
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"v29 review state projection drift: {args.output}")
            return 1
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(json.dumps({"ok": not payload["blockers"], "blockers": payload["blockers"]}))
    return 0 if not payload["blockers"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
