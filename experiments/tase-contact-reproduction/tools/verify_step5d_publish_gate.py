#!/usr/bin/env python3
"""Offline publish gate for Step5d TP package/current consistency."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from verify_step5d_current_binding import verify_binding


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = (".script", ".txt", ".urp")


def fail(message: str) -> None:
    raise RuntimeError(f"refusing Step5d publish: {message}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"missing JSON file: {path}")
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON file {path}: {exc}")


def git_staged_paths(root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        fail(f"git diff --cached failed: {completed.stderr.strip()}")
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def verify_no_staged_local_candidates(root: Path) -> None:
    for path in git_staged_paths(root):
        if "runs/local_tp_packages/" in path or path.endswith(".local_tp_candidate.json"):
            fail(f"staged local-only TP candidate path is not publishable: {path}")


def verify_root_triplets_match_current(root: Path, current_program: str) -> None:
    root_dir = root / "programs" / "step5"
    programs: dict[str, set[str]] = {}
    for path in root_dir.glob("step5d_strict_rnn_liveprep_v*.*"):
        if path.suffix in EXTENSIONS:
            programs.setdefault(path.stem, set()).add(path.suffix)
    for program, exts in sorted(programs.items()):
        if program != current_program:
            fail(
                f"root Step5d triplet {program} is not current; archive retained versions under programs/step5/step5d"
            )
        missing = set(EXTENSIONS) - exts
        if missing:
            fail(f"root Step5d triplet {program} is incomplete: {sorted(missing)}")


def verify_stage_table_current(root: Path, current_program: str) -> None:
    current = load_json(root / "config" / "current_stage.json")
    table_rel = current.get("stage_table_path") or "config/step5_stage_table.json"
    table = load_json(root / str(table_rel))
    active = [row.get("id") for row in table.get("stages", []) if row.get("stage") == "Step5d" and row.get("active") is True]
    if active != [current_program]:
        fail(f"active Step5d stage rows are {active}, expected only {current_program}")
    row = next((item for item in table.get("stages", []) if item.get("id") == current_program), None)
    if row is None:
        fail(f"current stage row is missing from {table_rel}: {current_program}")
    if row.get("complete") is True:
        fail(f"current stage row {current_program} is marked complete before successful reproduction")
    delivery = row.get("local_delivery_evidence", {})
    if delivery.get("controller_readback_verified") is not True:
        fail(f"current stage row {current_program} lacks controller_readback_verified=true")
    if delivery.get("archived_to_step5d_dir") is True:
        fail(f"current stage row {current_program} is marked archived")


def verify_operator_text(root: Path, current_program: str) -> None:
    bridge = (root / "scripts" / "bridge-line-operator.sh").read_text(encoding="utf-8")
    base = (root / "scripts" / "step4e-line-v1-operator.sh").read_text(encoding="utf-8")
    forbidden = [
        'BRIDGE_PROFILE="${BRIDGE_PROFILE:-step5d_strict_rnn_liveprep_v21}"',
        'STEP4E_VERSION="${STEP4E_VERSION:-step5d_strict_rnn_liveprep_v20}"',
    ]
    for needle in forbidden:
        if needle in bridge or needle in base:
            fail(f"operator contains stale fallback: {needle}")
    if "refusing Step5d alias: current_stage does not name" not in bridge:
        fail("bridge-line-operator no longer refuses missing Step5d current_stage aliases")
    if "refusing Step5d alias: current_stage does not name" not in base:
        fail("step4e-line-v1-operator no longer refuses missing Step5d current_stage aliases")
    if current_program not in load_json(root / "config" / "current_stage.json").get("controller_target", ""):
        fail("current_stage controller target does not contain the current program")


def verify(root: Path, *, staged: bool = False) -> dict[str, Any]:
    if staged:
        verify_no_staged_local_candidates(root)
    binding = verify_binding(root)
    current_program = binding["program"]
    verify_root_triplets_match_current(root, current_program)
    verify_stage_table_current(root, current_program)
    verify_operator_text(root, current_program)
    return {
        "ok": True,
        "program": current_program,
        "manifest": binding["manifest"],
        "staged_checked": staged,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=EXPERIMENT_ROOT)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = verify(args.root.resolve(), staged=args.staged)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"[publish-gate] passed for {result['program']}: {result['manifest']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(24)
