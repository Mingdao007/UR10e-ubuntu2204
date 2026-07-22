#!/usr/bin/env python3
"""Resolve the canonical bridge route from one fresh read-only Dashboard GET."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from step5d_autotune_v3.dashboard import dashboard_exchange
from step5d_autotune_v3.runtime_gate import loaded_program_matches
from step5d_manual_bridge import PROGRAM, ROOT
from promote_step5d_manual_release import load_manual_release


MANUAL_PATH = f"/programs/andyl/kunwei/step5/{PROGRAM}.urp"


def resolve(*, root: Path, robot_host: str, timeout_s: float) -> dict[str, object]:
    dashboard = dashboard_exchange(
        robot_host,
        ["programState", "safetymode", "get loaded program"],
        timeout=timeout_s,
    )
    loaded = dashboard["get loaded program"]
    manual = loaded_program_matches(loaded, MANUAL_PATH)
    release = load_manual_release(root) if manual else None
    return {
        "schema": "step5d.bridge-route/v1",
        "route": "manual_v2" if manual else "autotune_v3",
        "loaded_program_response": loaded,
        "expected_manual_program": MANUAL_PATH,
        "program_state": dashboard["programState"],
        "safety_mode": dashboard["safetymode"],
        "manual_release_manifest_sha256": (
            None if release is None else release["manifest_sha256"]
        ),
        "read_only": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--field", choices=("route", "release"))
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
    if args.field == "route":
        print(payload["route"])
    elif args.field == "release":
        print(payload["manual_release_manifest_sha256"] or "")
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
