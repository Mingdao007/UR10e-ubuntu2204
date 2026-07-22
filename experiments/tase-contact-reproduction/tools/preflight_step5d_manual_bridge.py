#!/usr/bin/env python3
"""Fresh read-only preflight for the isolated manual-hold NO_ARM bridge."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping

import preflight_readonly as readonly
import preflight_step5d_autotune_v3 as r009_preflight
import run_step5d_autotune_v3_bridge as r009_bridge
from step5d_autotune_v3.launcher import build_bridge_argv
from step5d_autotune_v3.runtime_calibration import bootstrap_stable_cuda_runtime, dependency_observation
from step5d_autotune_v3.runtime_profile import DEFAULT_OVERLAY, load_launch_profile
from step5d_autotune_v3.state import atomic_json
from step5d_manual_bridge import (
    CONTROL_PROFILE, PREFLIGHT_SCHEMA, PROGRAM, RELEASE_STAGE, ROOT,
    ManualBridgeError, load_context, sha256_path,
)


def _program_safe(dashboard: Mapping[str, Any], rtde: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(dashboard.get("programState", dashboard.get("program_state", "")))
    state = raw.split(maxsplit=1)[0].upper() if raw else ""
    basenames = {
        Path(match).name.lower()
        for match in r009_preflight.TP_PROGRAM_PATTERN.findall(raw)
    }
    exact_program = basenames == {f"{PROGRAM}.urp".lower()}
    if state == "STOPPED":
        checks = {"exact_program": exact_program, "stopped": True}
        return {"ok": all(checks.values()), "mode": "loaded_stopped", "checks": checks, "raw": raw}
    identity_fields = [24, 25, 27, 28, 29, 30]
    checks = {
        "exact_program": exact_program,
        "playing": state == "PLAYING",
        "ready_home": rtde.get("output_int_register_26") == 10,
        "zero_identity": all(rtde.get(f"output_int_register_{index}") == 0 for index in identity_fields),
    }
    return {
        "ok": all(checks.values()),
        "mode": "playing_ready_home_zero_identity",
        "checks": checks,
        "raw": raw,
    }


def run_preflight(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    context = load_context(ROOT, args.bridge_start_context)
    launch = load_launch_profile(args.launch_profile)
    governed_argv = build_bridge_argv(
        args.mailbox.parent,
        launch_profile=launch,
        trial_overlay=DEFAULT_OVERLAY,
    )
    local = r009_preflight._parallel({
        "route_robot": lambda: readonly.run_command(["ip", "route", "get", args.robot_host]),
        "route_kunwei": lambda: readonly.run_command(["ip", "route", "get", args.sensor_ip]),
        "writer": readonly.no_existing_writer,
        "realtime": readonly.realtime_capability,
        "runtime_calibration": dependency_observation,
        "production_startup_prewarm": lambda: r009_bridge.check_v3_runtime_prewarm(governed_argv[2:]),
    })
    local_ok = all(r009_preflight._value(item).get("ok") is True for item in local.values())
    if not local_ok:
        remote: dict[str, Any] = {"skipped": "local_stage_failed"}
        dashboard: Mapping[str, Any] = {}
        rtde: Mapping[str, Any] = {}
    else:
        remote = r009_preflight._parallel({
            "dashboard": lambda: readonly.dashboard_exchange(
                args.robot_host,
                ["PolyscopeVersion", "is in remote control", "safetymode", "robotmode", "programState"],
                timeout=args.timeout_s,
            ),
            "rtde": lambda: readonly.read_rtde_once(
                args.robot_host,
                [*readonly.RTDE_FIELDS, "actual_qd", *[f"output_int_register_{index}" for index in range(24, 31)]],
                frequency_hz=10.0,
                timeout=args.timeout_s,
            ),
            "secondary_port": lambda: readonly.probe_port(args.robot_host, 30002, timeout=args.timeout_s),
            "kunwei": lambda: readonly.tcp_connect_only(args.sensor_ip, args.sensor_port, args.timeout_s),
        })
        dashboard = r009_preflight._value(remote["dashboard"])
        rtde = r009_preflight._value(remote["rtde"])
    controller_identity, controller_sha = r009_preflight._controller_identity(
        dashboard, rtde, robot_host=args.robot_host
    )
    predicates = {
        "safety_normal": r009_preflight._safety_normal(dashboard),
        "program_safe_for_bridge": _program_safe(dashboard, rtde),
        "robot_stationary": r009_preflight._stationary(rtde),
        "prealign_start_clearance": r009_preflight._prealign_start_clearance(rtde),
        "no_existing_writer": {
            "ok": r009_preflight._value(local.get("writer", {})).get("ok") is True,
            "observation": r009_preflight._value(local.get("writer", {})),
        },
        "mailbox_initial_zero": r009_preflight._mailbox_initial_zero(args.mailbox),
        "runtime_dependencies": {
            "ok": all(
                r009_preflight._value(local.get(name, {})).get("ok") is True
                for name in ("runtime_calibration", "production_startup_prewarm")
            ),
            "calibration": r009_preflight._value(local.get("runtime_calibration", {})),
            "production_startup_prewarm": r009_preflight._value(local.get("production_startup_prewarm", {})),
        },
    }
    transport_ok = (
        local_ok
        and "dashboard" in remote
        and bool(dashboard)
        and r009_preflight._value(remote["dashboard"]).get("ok", True) is True
        and "rtde" in remote
        and readonly.rtde_predicate(dict(rtde)).get("ok") is True
        and r009_preflight._connect_observation_ok(remote.get("secondary_port", {}))
        and r009_preflight._connect_observation_ok(remote.get("kunwei", {}))
    )
    payload = {
        "schema": PREFLIGHT_SCHEMA,
        "ok": transport_ok and all(item["ok"] is True for item in predicates.values()),
        "fresh": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": time.monotonic() - started,
        "candidate_stage_id": RELEASE_STAGE,
        "control_profile_id": CONTROL_PROFILE,
        "tp_program_id": PROGRAM,
        "manual_release_manifest_sha256": context["manual_release_manifest_sha256"],
        "bridge_start_context_sha256": sha256_path(args.bridge_start_context),
        "launch_profile_fingerprint": launch.fingerprint,
        "controller_identity": controller_identity,
        "controller_identity_sha256": controller_sha,
        "predicates": predicates,
        "observations": {"local": local, "remote": remote},
        "safety_boundary": [
            "Dashboard read-only commands only",
            "RTDE output recipe only",
            "Kunwei TCP connect-only; no stream, zero, or tare command",
            "no bridge, RTDE input, Load, Play, ARM, contact, or motion",
        ],
    }
    atomic_json(args.output, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument("--bridge-start-context", type=Path, required=True)
    parser.add_argument("--launch-profile", type=Path, default=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = run_preflight(args)
    except Exception as exc:
        payload = {"schema": PREFLIGHT_SCHEMA, "ok": False, "fresh": False, "blocker": str(exc)}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok") is True else 2


if __name__ == "__main__":
    bootstrap_stable_cuda_runtime()
    raise SystemExit(main())
