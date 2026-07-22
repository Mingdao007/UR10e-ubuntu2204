#!/usr/bin/env python3
"""Publish and recompute the Manual V2 status used by canonical status --json."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

from step5d_autotune_v3.state import atomic_json


POINTER_SCHEMA = "step5d.manual-v2/active-run-pointer-v1"
STATUS_SCHEMA = "step5d.manual-v2/governed-status-v1"


def _pid_alive(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return False
    try:
        os.kill(value, 0)
    except OSError:
        return False
    return True


def activate(
    campaign_root: Path,
    output_root: Path,
    *,
    pointer_root: Path | None = None,
) -> dict[str, Any]:
    payload = {
        "schema": POINTER_SCHEMA,
        "campaign_root": str(campaign_root.resolve()),
        "output_root": str(output_root.resolve()),
        "status_path": str((campaign_root / "manual_governed_status.json").resolve()),
        "activated_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json((pointer_root or campaign_root) / "manual_active_run.json", payload)
    return payload


def read_status(campaign_root: Path) -> dict[str, Any]:
    pointer_path = campaign_root / "manual_active_run.json"
    if pointer_path.is_symlink() or not pointer_path.is_file():
        raise FileNotFoundError("no active Manual V2 run")
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    if not isinstance(pointer, dict) or pointer.get("schema") != POINTER_SCHEMA:
        raise ValueError("Manual V2 active-run pointer differs")
    status_path = Path(str(pointer.get("status_path", "")))
    if not status_path.is_absolute() or status_path.is_symlink() or not status_path.is_file():
        raise ValueError("Manual V2 machine status is unavailable")
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != STATUS_SCHEMA:
        raise ValueError("Manual V2 machine status schema differs")
    payload["bridge_heartbeat"] = _pid_alive(payload.get("bridge_pid"))
    if not payload["bridge_heartbeat"] and payload.get("state") not in {"COMPLETE", "BLOCKED"}:
        payload["state"] = "BLOCKED"
        payload["blocker"] = "BRIDGE_HEARTBEAT_LOST"
        payload["play_prompt_ready"] = False
        payload["next_action"] = "restart through canonical bridge"
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--campaign-root", type=Path, required=True)
    activate_parser.add_argument("--output-root", type=Path, required=True)
    activate_parser.add_argument("--pointer-root", type=Path)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--campaign-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = (
            activate(
                args.campaign_root,
                args.output_root,
                pointer_root=args.pointer_root,
            )
            if args.command == "activate"
            else read_status(args.campaign_root)
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"manual status unavailable: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
