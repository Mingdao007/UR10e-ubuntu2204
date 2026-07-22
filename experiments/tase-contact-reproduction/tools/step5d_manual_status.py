#!/usr/bin/env python3
"""Publish and recompute the Manual V2 status used by canonical status --json."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

from step5d_autotune_v3.runtime_gate import loaded_program_matches
from step5d_autotune_v3.governance import read_proc_starttime_ticks
from step5d_autotune_v3.state import atomic_json
from step5d_manual_bridge import PROGRAM, ROOT
from step5d_manual_qualification import validate_result as validate_manual_qualification
from ur10e_parallel import ResourceProfile, writer_lease_owner


POINTER_SCHEMA = "step5d.manual-v2/active-run-pointer-v1"
STATUS_SCHEMA = "step5d.manual-v2/governed-status-v1"
QUALIFICATION_SCHEMA = "step5d.manual-v2/production-startup-qualification-v1"
EXPECTED_PROGRAM = f"/programs/andyl/kunwei/step5/{PROGRAM}.urp"
BRIDGE_HEARTBEAT_MAX_AGE_NS = 2_000_000_000


def _process_identity_current(pid: Any, starttime_ticks: Any) -> bool:
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid < 1
        or isinstance(starttime_ticks, bool)
        or not isinstance(starttime_ticks, int)
        or starttime_ticks < 1
    ):
        return False
    return read_proc_starttime_ticks(pid) == starttime_ticks


def _bridge_heartbeat(payload: dict[str, Any]) -> bool:
    output_root = Path(str(payload.get("output_root", "")))
    csv_path = output_root / "runtime/bridge/bridge_rtde_500hz.csv"
    ready_path = output_root / "runtime/bridge/bridge_ready.json"
    try:
        stat = csv_path.stat()
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        lease_owner = writer_lease_owner(ResourceProfile.from_env())
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if not (
        output_root.is_absolute()
        and not csv_path.is_symlink()
        and csv_path.is_file()
        and stat.st_size > 0
        and 0 <= time.time_ns() - stat.st_mtime_ns <= BRIDGE_HEARTBEAT_MAX_AGE_NS
        and _process_identity_current(
            payload.get("bridge_pid"), payload.get("bridge_starttime_ticks")
        )
        and _process_identity_current(
            payload.get("bridge_owner_pid"),
            payload.get("bridge_owner_starttime_ticks"),
        )
        and isinstance(payload.get("bridge_launch_id"), str)
        and len(payload["bridge_launch_id"]) == 32
        and isinstance(ready, dict)
        and ready.get("ok") is True
        and ready.get("pid") == payload.get("bridge_pid")
        and ready.get("launch_nonce") == payload.get("bridge_launch_id")
        and isinstance(lease_owner, dict)
        and lease_owner.get("pid") == payload.get("bridge_owner_pid")
        and lease_owner.get("starttime_ticks")
        == payload.get("bridge_owner_starttime_ticks")
        and lease_owner.get("task") == "step5d-manual-no-arm-bridge"
    ):
        return False
    try:
        encoded = csv_path.read_bytes()
        if not encoded.endswith(b"\n"):
            return False
        table = list(csv.reader(io.StringIO(encoded.decode("utf-8"), newline="")))
        if len(table) < 3:
            return False
        header = table[0]
        if len(header) != len(set(header)) or any(
            len(row) != len(header) for row in table[1:]
        ):
            return False
        required = {
            "write_index",
            "t_wall_ns",
            "heartbeat",
            "command",
            "step4e_controller_state",
            "ur_safety_mode",
        }
        if not required.issubset(header):
            return False
        indices = {name: header.index(name) for name in required}
        previous = {name: float(table[-2][indices[name]]) for name in required}
        current = {name: float(table[-1][indices[name]]) for name in required}
    except (OSError, TypeError, UnicodeError, ValueError):
        return False
    wall_age_ns = time.time_ns() - int(current["t_wall_ns"])
    return bool(
        all(math.isfinite(value) for value in (*previous.values(), *current.values()))
        and 0 <= wall_age_ns <= BRIDGE_HEARTBEAT_MAX_AGE_NS
        and current["write_index"] > previous["write_index"]
        and current["heartbeat"] > previous["heartbeat"]
        and int(current["ur_safety_mode"]) == 1
    )


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
    return read_run_status(status_path.parent)


def read_run_status(campaign_root: Path) -> dict[str, Any]:
    status_path = campaign_root / "manual_governed_status.json"
    if status_path.is_symlink() or not status_path.is_file():
        raise ValueError("Manual V2 machine status is unavailable")
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != STATUS_SCHEMA:
        raise ValueError("Manual V2 machine status schema differs")
    qualification = payload.get("offline_qualification")
    qualification_current = False
    if isinstance(qualification, dict) and set(qualification) == {"path", "sha256"}:
        qualification_path = Path(str(qualification["path"]))
        if (
            qualification_path.is_absolute()
            and not qualification_path.is_symlink()
            and qualification_path.is_file()
            and hashlib.sha256(qualification_path.read_bytes()).hexdigest()
            == qualification["sha256"]
        ):
            try:
                validate_manual_qualification(
                    ROOT,
                    qualification_path,
                    release_manifest_sha256=str(payload.get("release_sha", "")),
                )
            except Exception:
                qualification_current = False
            else:
                qualification_current = True
    payload["offline_proven"] = qualification_current
    controller = payload.get("controller_observation")
    controller_keys = set(controller) if isinstance(controller, dict) else set()
    required_controller_keys = {
        "observed_at_unix_ns",
        "loaded_program_response",
        "program_state",
        "program_state_normalized",
        "safety_mode",
        "safety_mode_normalized",
        "expected_loaded_program",
    }
    controller_preflight_valid = bool(
        isinstance(controller, dict)
        and controller_keys == required_controller_keys
        and isinstance(controller.get("observed_at_unix_ns"), int)
        and not isinstance(controller.get("observed_at_unix_ns"), bool)
        and controller.get("expected_loaded_program") == EXPECTED_PROGRAM
        and loaded_program_matches(
            str(controller.get("loaded_program_response", "")), EXPECTED_PROGRAM
        )
        and controller.get("program_state_normalized") == "STOPPED"
        and controller.get("safety_mode_normalized") == "NORMAL"
    )
    payload["controller_preflight_valid"] = controller_preflight_valid
    if payload.get("play_prompt_ready") is True and not controller_preflight_valid:
        payload["state"] = "BLOCKED"
        payload["blocker"] = "MANUAL_CONTROLLER_PREFLIGHT_INVALID"
        payload["play_prompt_ready"] = False
        payload["next_action"] = "restart through canonical bridge preflight"
    if not qualification_current:
        payload["state"] = "BLOCKED"
        payload["blocker"] = "MANUAL_PRODUCTION_QUALIFICATION_INVALID"
        payload["play_prompt_ready"] = False
        payload["next_action"] = "rerun canonical Manual production qualification"
    payload["bridge_heartbeat"] = _bridge_heartbeat(payload)
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
