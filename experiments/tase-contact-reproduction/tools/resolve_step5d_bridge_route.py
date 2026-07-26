#!/usr/bin/env python3
"""Resolve the canonical autotune bridge route from the current release."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from step5d_autotune_v3.governance import load_current_release_snapshot
from step5d_autotune_v3.state import atomic_json


ROOT = Path(__file__).resolve().parents[1]
ARCHIVED_MANUAL_PROGRAM = "step5d_strict_rnn_manual_tune_v2"
MANUAL_PATH = f"/programs/andyl/kunwei/step5/{ARCHIVED_MANUAL_PROGRAM}.urp"
ROUTES = frozenset({"autotune_v3", "BLOCKED"})


def _v3_program_paths(root: Path) -> tuple[str, frozenset[str]]:
    path = root / "config/step5d/v3_active_surface.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("V3 active surface is unavailable")
    payload = json.loads(path.read_text(encoding="utf-8"))
    pointer_path = root / "config/step5d/current.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    manifest_path = root / str(pointer.get("manifest_path") or "")
    manifest_bytes = manifest_path.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != pointer.get(
        "manifest_sha256"
    ):
        raise ValueError("V3 current release pointer identity differs")
    manifest = json.loads(manifest_bytes)
    active = (manifest.get("identity") or {}).get("program_id")
    recovery = payload.get("recovery_loaded_program_ids") if isinstance(payload, dict) else None
    if (
        not isinstance(active, str)
        or not active
        or not isinstance(recovery, list)
        or any(not isinstance(value, str) or not value for value in recovery)
    ):
        raise ValueError("V3 active/recovery program identity differs")
    recovery = [program for program in recovery if program != active]
    prefix = "/programs/andyl/kunwei/step5/"
    return (
        f"{prefix}{active}.urp",
        frozenset(f"{prefix}{program}.urp" for program in recovery),
    )


def resolve(*, root: Path, robot_host: str, timeout_s: float) -> dict[str, object]:
    del robot_host, timeout_s
    active_v3_path, recovery_v3_paths = _v3_program_paths(root)
    autotune_release = load_current_release_snapshot(root)
    if not autotune_release.valid:
        raise ValueError("current autotune release is invalid")
    return {
        "schema": "step5d.bridge-route/v2",
        "route": "autotune_v3",
        "reason_code": None,
        "loaded_program_response": None,
        "expected_manual_program": MANUAL_PATH,
        "expected_autotune_program": active_v3_path,
        "accepted_autotune_recovery_programs": sorted(recovery_v3_paths),
        "autotune_route_mode": "current_release",
        "program_state": None,
        "safety_mode": None,
        "manual_release_manifest_sha256": None,
        "autotune_release_manifest_sha256": autotune_release.manifest_sha256,
        "read_only": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--field", choices=("route", "release"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = resolve(
            root=args.root.resolve(strict=True),
            robot_host=args.robot_host,
            timeout_s=args.timeout_s,
        )
    except Exception as exc:
        print(f"bridge route unresolved: {type(exc).__name__}:{exc}", file=sys.stderr)
        return 2
    if args.output is not None:
        atomic_json(args.output, payload)
    if args.field == "route":
        print(payload["route"])
    elif args.field == "release":
        print(
            payload["manual_release_manifest_sha256"]
            or payload["autotune_release_manifest_sha256"]
            or ""
        )
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["route"] in ROUTES - {"BLOCKED"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
