#!/usr/bin/env python3
"""Start and own one isolated Step5d manual bridge in NO_ARM state."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
from typing import Any, Mapping

from step5d_autotune_v3.launcher import build_bridge_argv
from step5d_autotune_v3.runtime_profile import DEFAULT_OVERLAY
from step5d_autotune_v3.state import atomic_json
from step5d_manual_bridge import (
    CONTROL_PROFILE, DEFAULT_PREFLIGHT_MAX_AGE_S, PREFLIGHT_SCHEMA, PROGRAM,
    PROTOCOL, RELEASE_STAGE, ROOT, TICKET_SCHEMA, WIRE_PROTOCOL,
    ManualBridgeError, load_context, require_fresh_timestamp, sha256_path,
    strict_object, require_canonical_shell,
)
from step5d_manual_profile import DEFAULT_LAUNCH_PROFILE, load_manual_launch_profile
from ur10e_parallel import ResourceProfile, writer_lease
from step5d_autotune_v3.runtime_gate import process_starttime


WRAPPER = ROOT / "tools/run_step5d_manual_bridge.py"


def _argv_sha256(argv: list[str]) -> str:
    encoded = json.dumps(argv, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def _terminate(process: subprocess.Popen[Any] | None) -> int | None:
    if process is None:
        return None
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=8.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
    return process.returncode


def _parent_death_guard(
    expected_parent_pid: int,
    expected_parent_starttime: int,
) -> None:
    """Terminate the production bridge if its lease-owning parent disappears."""

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, int(signal.SIGTERM), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if (
        os.getppid() != expected_parent_pid
        or process_starttime(expected_parent_pid) != expected_parent_starttime
    ):
        os.kill(os.getpid(), signal.SIGTERM)


def _validate_preflight(path: Path, context: Mapping[str, Any]) -> dict[str, Any]:
    payload = strict_object(path, "manual live preflight")
    require_fresh_timestamp(
        payload.get("created_at"),
        role="manual live preflight",
        max_age_s=DEFAULT_PREFLIGHT_MAX_AGE_S,
    )
    required_predicates = {
        "safety_normal", "program_safe_for_bridge", "robot_stationary",
        "prealign_start_clearance", "no_existing_writer", "mailbox_initial_zero",
        "runtime_dependencies",
    }
    predicates = payload.get("predicates")
    if any(
        (
            payload.get("schema") != PREFLIGHT_SCHEMA,
            payload.get("ok") is not True,
            payload.get("fresh") is not True,
            payload.get("candidate_stage_id") != RELEASE_STAGE,
            payload.get("control_profile_id") != CONTROL_PROFILE,
            payload.get("tp_program_id") != PROGRAM,
            payload.get("manual_release_manifest_sha256") != context["manual_release_manifest_sha256"],
            not isinstance(predicates, Mapping),
            set(predicates or {}) != required_predicates,
            not all(isinstance(predicates[name], Mapping) and predicates[name].get("ok") is True for name in required_predicates) if isinstance(predicates, Mapping) else True,
        )
    ):
        raise ManualBridgeError("manual live preflight did not pass exactly")
    return payload


def run(args: argparse.Namespace) -> int:
    output_root = args.output_root.expanduser().absolute()
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    context = load_context(ROOT, args.bridge_start_context)
    preflight = _validate_preflight(args.preflight, context)
    runtime_root = output_root / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    bridge_run = runtime_root / "bridge"
    (bridge_run / "runtime").mkdir(parents=True, exist_ok=False, mode=0o700)
    launch = load_manual_launch_profile(args.launch_profile)
    bridge_argv = build_bridge_argv(
        runtime_root,
        launch_profile=launch,
        trial_overlay=DEFAULT_OVERLAY,
    )[2:]
    command = [sys.executable, str(WRAPPER), *bridge_argv]
    ticket = {
        "schema": TICKET_SCHEMA,
        "parent_pid": os.getpid(),
        "argv_sha256": _argv_sha256(bridge_argv),
        "launch_id": uuid.uuid4().hex,
        "scope": "manual_bridge_no_arm",
        "program": PROGRAM,
        "protocol": PROTOCOL,
        "wire_protocol": WIRE_PROTOCOL,
        "control_profile_id": CONTROL_PROFILE,
        "release_stage_id": RELEASE_STAGE,
        "manual_release_manifest_sha256": context["manual_release_manifest_sha256"],
        "launch_attempt_id": args.launch_attempt_id,
        "campaign_id": args.campaign_id,
        "bridge_start_context": {
            "path": str(args.bridge_start_context.expanduser().absolute()),
            "sha256": sha256_path(args.bridge_start_context),
        },
        "preflight": {
            "path": str(args.preflight.expanduser().absolute()),
            "sha256": sha256_path(args.preflight),
        },
    }
    ticket_path = runtime_root / "runtime_ticket.json"
    atomic_json(ticket_path, ticket)
    environment = {
        **os.environ,
        "STEP5D_MANUAL_BRIDGE_TICKET": str(ticket_path),
        "STEP5D_BRIDGE_LAUNCH_NONCE": ticket["launch_id"],
    }
    log_path = output_root / "bridge.log"
    process: subprocess.Popen[Any] | None = None
    stop_requested = False
    owner_pid = os.getpid()
    owner_starttime = process_starttime(owner_pid)

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_int = signal.signal(signal.SIGINT, request_stop)
    previous_term = signal.signal(signal.SIGTERM, request_stop)
    try:
        profile = ResourceProfile.from_env()
        with writer_lease(profile, "step5d-manual-no-arm-bridge", blocking=False):
            with log_path.open("wb") as bridge_log:
                process = subprocess.Popen(
                    command,
                    cwd=ROOT,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=bridge_log,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    preexec_fn=lambda expected_pid=owner_pid, expected_start=owner_starttime: _parent_death_guard(
                        expected_pid,
                        expected_start,
                    ),
                )
                ready_path = bridge_run / "bridge_ready.json"
                deadline = time.monotonic() + args.ready_timeout_s
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise ManualBridgeError(
                            f"manual bridge exited before readiness rc={process.returncode}"
                        )
                    if ready_path.is_file():
                        ready = strict_object(ready_path, "manual bridge readiness")
                        if any(
                            (
                                ready.get("ready_schema") != "step5d_bridge_ready_v2",
                                ready.get("ok") is not True,
                                ready.get("pid") != process.pid,
                                ready.get("launch_nonce") != ticket["launch_id"],
                                ready.get("bridge_profile") != CONTROL_PROFILE,
                                ready.get("rtde_connected") is not True,
                                ready.get("rtde_send_succeeded") is not True,
                                ready.get("sensor_stream_ready") is not True,
                            )
                        ):
                            raise ManualBridgeError("manual bridge readiness identity differs")
                        break
                    time.sleep(0.05)
                else:
                    raise ManualBridgeError("manual bridge readiness timeout")
                bridge_starttime = process_starttime(process.pid)
                if bridge_starttime <= 0 or owner_starttime <= 0:
                    raise ManualBridgeError(
                        "manual bridge process identity is unavailable"
                    )
                result = {
                    "schema": "step5d.manual-hold/bridge-launch-result-v1",
                    "ok": True,
                    "program": PROGRAM,
                    "protocol": PROTOCOL,
                    "wire_protocol": WIRE_PROTOCOL,
                    "scope": "manual_bridge_no_arm",
                    "pid": process.pid,
                    "pid_starttime_ticks": bridge_starttime,
                    "parent_pid": os.getpid(),
                    "parent_starttime_ticks": owner_starttime,
                    "launch_id": ticket["launch_id"],
                    "launch_attempt_id": args.launch_attempt_id,
                    "campaign_id": args.campaign_id,
                    "output_root": str(output_root),
                    "bridge_ready": str(ready_path),
                    "mailbox": str(runtime_root / "command.json"),
                    "manual_release_manifest_sha256": context["manual_release_manifest_sha256"],
                    "bridge_authorized": True,
                    "arm_authorized": False,
                    "motion_authorized": False,
                }
                atomic_json(output_root / "bridge_launch.json", result)
                print(json.dumps(result, sort_keys=True), flush=True)
                print("MANUAL_BRIDGE_READY_NO_ARM", flush=True)
                while process.poll() is None and not stop_requested:
                    time.sleep(0.2)
                if process.poll() is not None and process.returncode not in {0, 130}:
                    raise ManualBridgeError(f"manual bridge exited rc={process.returncode}")
    finally:
        bridge_rc = _terminate(process)
        atomic_json(output_root / "bridge_closure.json", {
            "schema": "step5d.manual-hold/bridge-closure-v1",
            "bridge_rc": bridge_rc,
            "arm_authorized": False,
            "motion_authorized": False,
        })
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--bridge-start-context", type=Path, required=True)
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--launch-profile", type=Path, default=DEFAULT_LAUNCH_PROFILE)
    parser.add_argument("--launch-attempt-id", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--canonical-owner-pid", type=int, required=True)
    parser.add_argument("--canonical-owner-starttime", type=int, required=True)
    parser.add_argument("--ready-timeout-s", type=float, default=20.0)
    args = parser.parse_args(argv)
    try:
        require_canonical_shell()
        _parent_death_guard(
            args.canonical_owner_pid,
            args.canonical_owner_starttime,
        )
        return run(args)
    except (OSError, TimeoutError, ValueError, ManualBridgeError) as exc:
        print(f"manual bridge start blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
