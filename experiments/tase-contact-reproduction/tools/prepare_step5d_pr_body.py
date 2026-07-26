#!/usr/bin/env python3
"""Render and validate a PR body from a fixed, reviewable input document."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def render(payload: dict[str, object]) -> str:
    change_class = payload.get("change_class")
    if change_class not in {"behavior_preserving", "behavior_changing"}:
        raise ValueError("change_class must be behavior_preserving or behavior_changing")
    baseline = payload.get("frozen_baseline")
    if not isinstance(baseline, str):
        raise ValueError("frozen_baseline is required")
    deltas = payload.get("allowed_deltas")
    commands = payload.get("validation_commands")
    if not isinstance(deltas, list) or not all(isinstance(item, str) and item for item in deltas):
        raise ValueError("allowed_deltas must be a nonempty string list")
    if not isinstance(commands, list) or not all(isinstance(item, str) and item for item in commands):
        raise ValueError("validation_commands must be a nonempty string list")
    evidence = payload.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        raise ValueError("evidence is required")
    checks = {
        "behavior_preserving": "x" if change_class == "behavior_preserving" else " ",
        "behavior_changing": "x" if change_class == "behavior_changing" else " ",
    }
    return "\n".join(
        [
            "## Change class",
            "",
            "Select exactly one:",
            "",
            f"- [{checks['behavior_preserving']}] `behavior_preserving`",
            f"- [{checks['behavior_changing']}] `behavior_changing`",
            f"- Frozen baseline: `{baseline}`",
            "",
            "## Allowed deltas",
            "",
            *[f"- {item}" for item in deltas],
            "",
            "## Protected v1 closure",
            "",
            "- [x] The complete frozen v1 source/config/TP closure is zero-diff.",
            "- [x] No controller upload, load/Play, ARM, zero/tare, contact, or motion occurred.",
            "",
            "## Rollback",
            "",
            "```bash",
            "git revert HEAD",
            "```",
            "",
            "## Validation commands",
            "",
            "```bash",
            *commands,
            "```",
            "",
            "## Evidence and remaining gates",
            "",
            evidence,
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("PR body input must be a JSON object")
    body = render(payload)
    validator = args.root / "experiments/tase-contact-reproduction/tools/validate_step5d_autotune_v3_refactor.py"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md") as handle:
        handle.write(body)
        handle.flush()
        result = subprocess.run(
            [sys.executable, str(validator), "--root", str(args.root / "experiments/tase-contact-reproduction"), "--declaration-file", handle.name],
            cwd=args.root,
            check=False,
        )
    if result.returncode != 0:
        return result.returncode
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
