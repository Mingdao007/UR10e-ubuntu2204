#!/usr/bin/env python3
"""Publish and recompute the Manual V2 status used by canonical status --json."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from step5d_autotune_v3.runtime_gate import loaded_program_matches
from step5d_autotune_v3.state import atomic_json
from step5d_manual_bridge import PROGRAM, ROOT
from step5d_manual_authorization import load_capability_authorization
from step5d_manual_qualification import validate_result as validate_manual_qualification


POINTER_SCHEMA = "step5d.manual-v2/active-run-pointer-v1"
STATUS_SCHEMA = "step5d.manual-v2/governed-status-v1"
QUALIFICATION_SCHEMA = "step5d.manual-v2/production-startup-qualification-v1"
CAPABILITIES = ("bridge", "play", "arm", "motion", "zero", "tare")
EXPECTED_PROGRAM = f"/programs/andyl/kunwei/step5/{PROGRAM}.urp"
CONTROLLER_IDENTITY_MAX_AGE_NS = 2_000_000_000
BRIDGE_HEARTBEAT_MAX_AGE_NS = 2_000_000_000


def _pid_alive(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return False
    try:
        os.kill(value, 0)
    except OSError:
        return False
    return True


def _bridge_heartbeat(payload: dict[str, Any]) -> bool:
    output_root = Path(str(payload.get("output_root", "")))
    csv_path = output_root / "runtime/bridge/bridge_rtde_500hz.csv"
    try:
        stat = csv_path.stat()
    except OSError:
        return False
    return bool(
        output_root.is_absolute()
        and not csv_path.is_symlink()
        and csv_path.is_file()
        and stat.st_size > 0
        and 0 <= time.time_ns() - stat.st_mtime_ns <= BRIDGE_HEARTBEAT_MAX_AGE_NS
        and _pid_alive(payload.get("bridge_pid"))
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
    capabilities = payload.get("capabilities")
    authorization = payload.get("authorization")
    authorization_current = False
    if isinstance(authorization, dict) and set(authorization) == {"path", "sha256"}:
        authorization_path = Path(str(authorization["path"]))
        if (
            authorization_path.is_absolute()
            and not authorization_path.is_symlink()
            and authorization_path.is_file()
            and hashlib.sha256(authorization_path.read_bytes()).hexdigest()
            == authorization["sha256"]
        ):
            try:
                observed_authorization = load_capability_authorization(
                    authorization_path,
                    attempt_id=str(payload.get("launch_attempt_id", "")),
                    campaign_id=str(payload.get("campaign_id", "")),
                    release_manifest_sha256=str(payload.get("release_sha", "")),
                )
            except Exception:
                authorization_current = False
            else:
                authorization_current = observed_authorization.get("capabilities") == capabilities
    play_scope = bool(
        authorization_current
        and isinstance(capabilities, dict)
        and set(capabilities) == set(CAPABILITIES)
        and all(isinstance(capabilities[name], bool) for name in CAPABILITIES)
        and all(capabilities[name] for name in ("bridge", "play", "arm", "motion"))
        and not capabilities["zero"]
        and not capabilities["tare"]
    )
    if payload.get("play_prompt_ready") is True and not play_scope:
        payload["state"] = "BRIDGE_ALIVE_NO_ARM"
        payload["blocker"] = "AUTHORIZATION_SCOPE_INSUFFICIENT"
        payload["play_prompt_ready"] = False
        payload["next_action"] = "await explicit Play/ARM/motion authorization or stop"
    controller = payload.get("controller_observation")
    observed_at = controller.get("observed_at_unix_ns") if isinstance(controller, dict) else None
    controller_identity_fresh = bool(
        isinstance(controller, dict)
        and set(controller)
        == {
            "observed_at_unix_ns",
            "loaded_program_response",
            "program_state",
            "safety_mode",
            "expected_loaded_program",
        }
        and isinstance(observed_at, int)
        and not isinstance(observed_at, bool)
        and 0 <= time.time_ns() - observed_at <= CONTROLLER_IDENTITY_MAX_AGE_NS
        and controller.get("expected_loaded_program") == EXPECTED_PROGRAM
        and loaded_program_matches(
            str(controller.get("loaded_program_response", "")), EXPECTED_PROGRAM
        )
    )
    payload["controller_identity_fresh"] = controller_identity_fresh
    if payload.get("play_prompt_ready") is True and not controller_identity_fresh:
        payload["state"] = "BRIDGE_ALIVE_NO_ARM"
        payload["blocker"] = "MANUAL_CONTROLLER_IDENTITY_STALE"
        payload["play_prompt_ready"] = False
        payload["next_action"] = "refresh exact Manual loaded-program identity or stop"
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
