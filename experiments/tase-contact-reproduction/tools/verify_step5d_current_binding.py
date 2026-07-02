#!/usr/bin/env python3
"""Verify the selected Step5d live-prep package, read-back, and runtime binding."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from step5d_runtime_interface import resolve_runtime_interface
from verify_current_stage_readback import EXPERIMENT_ROOT, fail, load_json, verify


def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _local_triplet_paths(root: Path, current: dict[str, Any], program: str) -> list[Path]:
    stem_value = current.get("local_triplet")
    if stem_value:
        stem = root / str(stem_value)
    else:
        stem = root / "programs" / "step5" / program
    return [stem.with_suffix(ext) for ext in (".script", ".txt", ".urp")]


def verify_binding(root: Path, program: str | None = None, target_dir: str | None = None) -> dict[str, Any]:
    readback = verify(root, program, target_dir)
    current = load_json(root / "config" / "current_stage.json")
    selected = readback["program"]
    if not selected.startswith("step5d_strict_rnn_liveprep_"):
        fail(f"{selected} is not a Step5d live-prep package")

    interface = resolve_runtime_interface(program=selected, root=root)
    if current.get("controller_target") != interface.controller_target:
        fail(f"runtime interface target {interface.controller_target} differs from current_stage target {current.get('controller_target')}")
    if current.get("bridge_profile", {}).get("step4e_version") not in (None, selected):
        fail(f"bridge_profile.step4e_version is {current.get('bridge_profile', {}).get('step4e_version')}, expected {selected}")

    missing_triplet = [path for path in _local_triplet_paths(root, current, selected) if not path.exists()]
    if missing_triplet:
        fail("local triplet is incomplete: " + ", ".join(_relative(root, path) for path in missing_triplet))

    current_sha = current.get("sha256") or current.get("evidence", {}).get("sha256") or {}
    for ext in (".script", ".txt", ".urp"):
        if not current_sha.get(ext):
            fail(f"current_stage sha256 for {ext} is missing")

    return {
        "ok": True,
        "program": selected,
        "target_dir": readback["target_dir"],
        "manifest": readback["manifest"],
        "delivery_mode": readback["delivery_mode"],
        "runtime_interface": asdict(interface),
        "local_triplet": [_relative(root, path) for path in _local_triplet_paths(root, current, selected)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=EXPERIMENT_ROOT)
    parser.add_argument("--program", default=None)
    parser.add_argument("--target-dir", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = verify_binding(args.root, args.program, args.target_dir)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"[operator] Step5d current binding passed for {result['program']}: {result['manifest']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(24)
