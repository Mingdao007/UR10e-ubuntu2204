#!/usr/bin/env python3
"""Invoke one exact-model read-only v31 Review v3 lane and normalize its evidence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["P0", "P1", "P2"]},
                    "status": {"type": "string", "enum": ["open", "closed"]},
                    "summary": {"type": "string"},
                    "evidence": {"type": "string"},
                    "remediation": {"type": "string"},
                },
                "required": ["id", "severity", "status", "summary", "evidence", "remediation"],
            },
        },
        "assessment": {"type": "string"},
    },
    "required": ["findings", "assessment"],
}


def parse_payload(text: str) -> dict[str, Any]:
    candidates = [text.strip(), *reversed([line.strip() for line in text.splitlines()])]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("structured_output"), dict):
            payload = payload["structured_output"]
        elif isinstance(payload, dict) and isinstance(payload.get("result"), str):
            try:
                payload = json.loads(payload["result"])
            except json.JSONDecodeError:
                pass
        if isinstance(payload, dict) and isinstance(payload.get("findings"), list):
            return payload
    raise ValueError("reviewer did not return the required structured payload")


def prompt(lane: str, binding: Path, evidence: Path, composite: str) -> str:
    focus = (
        "Audit strict-RNN control/timing/claim correctness, command consumption, layout 524, "
        "hard-versus-diagnostic guard implementation, and deterministic evidence bindings."
        if lane == "codex"
        else
        "Audit physical/operator safety and lifecycle semantics: entry/search/contact latch, "
        "gross stops, heartbeat/stale behavior, safe retract/home, and no auto-home from protective stop/E-stop."
    )
    return f"""You are the single read-only {lane} lane of the UR10e Step5d v31 Review v3 gate.
Composite fingerprint: {composite}
Binding: {binding}
Evidence inventory: {evidence}
Repository root: {ROOT}

{focus}

The user explicitly requires the permissive guard policy to remain in force until they request tightening.
Do not report diagnostic-only force windows, Cartesian/normal speed or displacement, DLS/residual/active-bound,
or ordinary normal-direction limits as missing hard guards. Hard structural checks and gross limits remain in scope.
This is offline review only: do not edit files, start a bridge, load/Play TP, or cause robot motion.
Review the bound package/read-back/source/config evidence. Report only concrete defects introduced by v31.
P0/P1 must have a unique stable ID and precise file/line or artifact evidence. Return the requested JSON only.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=("codex", "fable"))
    parser.add_argument("binding", type=Path)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    composite = os.environ.get("UR10E_REVIEW_COMPOSITE_FINGERPRINT", "")
    binding_sha = os.environ.get("UR10E_REVIEW_BINDING_SHA256", "")
    review_prompt = prompt(args.lane, args.binding.resolve(), args.evidence.resolve(), composite)

    with tempfile.TemporaryDirectory(prefix="step5d-v31-review-") as directory:
        directory_path = Path(directory)
        schema_path = directory_path / "schema.json"
        schema_path.write_text(json.dumps(SCHEMA), encoding="utf-8")
        if args.lane == "codex":
            last_message = directory_path / "last.json"
            command = [
                "codex", "exec", "--ephemeral", "--model", "gpt-5.6-sol",
                "--config", 'model_reasoning_effort="high"', "--sandbox", "read-only",
                "--cd", str(ROOT), "--output-schema", str(schema_path),
                "--output-last-message", str(last_message), review_prompt,
            ]
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
            raw = completed.stdout + completed.stderr
            structured_text = last_message.read_text() if last_message.is_file() else completed.stdout
            actual_model, actual_effort = "gpt-5.6-sol", "high"
        else:
            command = [
                "claude", "--print", "--model", "claude-fable-5", "--effort", "high",
                "--permission-mode", "plan", "--no-session-persistence",
                "--output-format", "json", "--json-schema", json.dumps(SCHEMA), review_prompt,
            ]
            completed = subprocess.run(command, text=True, capture_output=True, check=False)
            raw = completed.stdout + completed.stderr
            structured_text = completed.stdout
            actual_model, actual_effort = "claude-fable-5", "high"

    print(f"lane={args.lane} exact_model={actual_model} effort={actual_effort}")
    print(raw.rstrip())
    if completed.returncode != 0:
        return completed.returncode
    try:
        payload = parse_payload(structured_text)
    except ValueError as exc:
        print(str(exc))
        return 65
    metadata = {
        "actual_model": actual_model,
        "actual_effort": actual_effort,
        "reviewed_composite_fingerprint": composite,
        "reviewed_binding_sha256": binding_sha,
        "findings": payload["findings"],
        "assessment": payload.get("assessment", ""),
    }
    print(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
