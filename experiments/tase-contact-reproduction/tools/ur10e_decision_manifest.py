#!/usr/bin/env python3
"""Freeze and verify one shared user-decision barrier for offline UR10e lanes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DECISION_SOURCE = ROOT / "config/ur10e_user_decisions_v1.json"
CURRENT_STAGE = ROOT / "config/current_stage.json"
STAGE_TABLE = ROOT / "config/step5_stage_table.json"
P0_PROFILE = "step5d_strict_rnn_no_contact_p0_v8"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def p0_duration(current: dict[str, Any], table: dict[str, Any]) -> float:
    candidate = current.get("p0_v8_candidate") or {}
    policy = candidate.get("canary_policy") or {}
    rows = [row for row in table.get("stages", []) if row.get("id") == P0_PROFILE]
    if len(rows) != 1:
        raise ValueError("P0 v8 stage row must resolve exactly once")
    try:
        candidate_duration = float(policy["direct_duration_s"])
        stage_duration = float(rows[0]["duration_s"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("P0 v8 direct duration is missing from frozen stage config") from exc
    if candidate_duration <= 0 or candidate_duration != stage_duration:
        raise ValueError("P0 v8 current-stage and stage-table durations differ")
    if policy.get("mode") != "direct_single_duration" or "prerequisite_phases_s" in policy:
        raise ValueError("P0 v8 active policy is not a direct single-duration canary")
    return candidate_duration


def build_snapshot(*, root: Path = ROOT) -> dict[str, Any]:
    decision_path = root / "config/ur10e_user_decisions_v1.json"
    current_path = root / "config/current_stage.json"
    table_path = root / "config/step5_stage_table.json"
    decisions, current, table = load(decision_path), load(current_path), load(table_path)
    source_bindings = {
        "decision_source_sha256": sha256(decision_path),
        "current_stage_sha256": sha256(current_path),
        "stage_table_sha256": sha256(table_path),
    }
    resolved = {
        "current_stage_id": current.get("current_stage_id"),
        "workflow_state": current.get("workflow_state") or current.get("status"),
        "p0_v8_profile": P0_PROFILE,
        "p0_v8_direct_duration_s": p0_duration(current, table),
        "p0_v8_contact": False,
        "onrobot_mainline_enabled": False,
        "live_actions_authorized": False,
    }
    digest = canonical_digest({
        "decisions": decisions,
        "source_bindings": source_bindings,
        "resolved": resolved,
    })
    return {
        "schema_version": "ur10e_user_decision_manifest_v1",
        "decision_digest": digest,
        "decision_source_digest": canonical_digest(decisions),
        "decision_source": "config/ur10e_user_decisions_v1.json",
        "source_bindings": source_bindings,
        "resolved": resolved,
    }


def freeze(output: Path, *, root: Path = ROOT) -> dict[str, Any]:
    snapshot = build_snapshot(root=root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    return snapshot


def verify(path: Path, *, root: Path = ROOT) -> dict[str, Any]:
    frozen = load(path)
    current = build_snapshot(root=root)
    if frozen != current:
        raise ValueError("user-decision manifest is stale against decision/current-stage sources")
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "verify", "digest", "p0-duration"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.mode == "freeze":
        if args.output is None:
            parser.error("freeze requires --output")
        payload = freeze(args.output, root=args.root.resolve())
    elif args.mode == "verify":
        if args.manifest is None:
            parser.error("verify requires --manifest")
        payload = verify(args.manifest, root=args.root.resolve())
    else:
        payload = build_snapshot(root=args.root.resolve())
    if args.mode == "p0-duration":
        print(f'{float(payload["resolved"]["p0_v8_direct_duration_s"]):g}')
    else:
        print(payload["decision_digest"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
