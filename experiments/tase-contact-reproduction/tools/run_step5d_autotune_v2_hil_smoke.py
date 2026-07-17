#!/usr/bin/env python3
"""Run a serialized HOLD-only TP Play smoke on the deployed UR10e controller."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from step5d_autotune_v2.bridge import LiveWriterLock
from step5d_autotune_v2.dashboard import DashboardClient
from step5d_autotune_v2.startup_smoke import RtdeHoldSession, verify_play_startup


PROGRAM = "step5d_strict_rnn_autotune_v2.urp"


def program_is_stopped(response: str) -> bool:
    return response == "STOPPED" or response.startswith("STOPPED ")


def wait_program_stopped(dashboard: DashboardClient, timeout_s: float = 5.0) -> str:
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        last = dashboard.command(
            "programState", ("PLAYING", "PAUSED", "STOPPED")
        ).matched_line
        if program_is_stopped(last):
            return last
        time.sleep(0.1)
    raise RuntimeError(f"TP watchdog did not leave the program STOPPED: {last}")


def _write(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run(host: str, output: Path, *, timeout_s: float) -> dict[str, Any]:
    lock = LiveWriterLock()
    dashboard = DashboardClient(host, connect_timeout_s=2.0, response_timeout_s=5.0)
    try:
        lock.acquire()
        snapshot = dashboard.snapshot()
        version = dashboard.command("PolyscopeVersion", ("URSoftware",)).matched_line
        if "5.26." not in version:
            raise RuntimeError("target controller software is not the required 5.26 release")
        if not program_is_stopped(snapshot["programState"]):
            raise RuntimeError("target program must be STOPPED before the no-motion smoke")
        if not snapshot["get loaded program"].endswith("/" + PROGRAM):
            raise RuntimeError("the frozen Step5d v2 TP package is not loaded")
        with RtdeHoldSession(host, timeout_s=5.0) as session:
            result = verify_play_startup(
                session,
                trigger_play=lambda: print(
                    "HIL_READY: 请在 TP 上按 Play；此 gate 只发送 heartbeat + HOLD，不发送 ARM。",
                    flush=True,
                ),
                timeout_s=timeout_s,
                allow_latched_ready_baseline=True,
            )
        stopped_after_smoke = wait_program_stopped(dashboard)
        payload = {
            "schema": "step5d.autotune.hil-startup-smoke/v1",
            "ok": True,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "controller": host,
            "software_version": version,
            "loaded_program": snapshot["get loaded program"],
            "motion_allowed": False,
            "host_command": "HOLD",
            "operator_play_required": True,
            "phases": list(result.phases),
            "startup_duration_s": result.startup_duration_s,
            "first_active_state": result.first_active_state,
            "consumed_command_seq": result.consumed_command_seq,
            "samples": result.samples,
            "stopped_after_smoke": stopped_after_smoke,
        }
    except (Exception, KeyboardInterrupt) as exc:
        try:
            stopped_after_failure = wait_program_stopped(dashboard)
        except Exception as stop_exc:
            stopped_after_failure = f"unconfirmed: {type(stop_exc).__name__}: {stop_exc}"
        payload = {
            "schema": "step5d.autotune.hil-startup-smoke/v1",
            "ok": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "controller": host,
            "motion_allowed": False,
            "error": f"{type(exc).__name__}: {exc}",
            "stopped_after_failure": stopped_after_failure,
        }
    finally:
        lock.release()
    _write(output, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.1.18")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=60.0)
    args = parser.parse_args()
    payload = run(args.host, args.output, timeout_s=args.timeout_s)
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
