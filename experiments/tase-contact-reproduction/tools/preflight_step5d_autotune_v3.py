#!/usr/bin/env python3
"""Fast, read-only, V3-specific preflight for the full-bridge HOLD HIL gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import preflight_readonly as base
import run_step5d_autotune_v3_bridge as bridge_wrapper
import verify_step5d_autotune_v3_hil_authorization as authorization_gate
from step5d_autotune_v3.launcher import build_bridge_argv
from step5d_autotune_v3.runtime_profile import DEFAULT_OVERLAY
from step5d_autotune_v3.runtime_profile import (
    CONTROL_PROFILE_ID,
    RELEASE_STAGE_ID,
    TP_PROGRAM_ID,
    load_launch_profile,
)
from step5d_autotune_v3.runtime_calibration import (
    bootstrap_stable_cuda_runtime,
    dependency_observation,
)
from step5d_autotune_v3.state import atomic_json


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "step5d.autotune-v3/hil-preflight-snapshot-v1"
TP_PROGRAM_PATTERN = re.compile(r"([^<>\s]+\.urp)(?=$|[>\s])", re.IGNORECASE)


class PreflightError(RuntimeError):
    pass


def _observed(function: Callable[[], Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        value = function()
    except Exception as exc:
        value = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    if not isinstance(value, dict):
        value = {"ok": False, "error": "observation was not an object"}
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": time.monotonic() - started,
        "value": value,
    }


def _parallel(checks: Mapping[str, Callable[[], Any]]) -> dict[str, dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=len(checks)) as executor:
        futures = {name: executor.submit(_observed, function) for name, function in checks.items()}
        return {name: futures[name].result() for name in checks}


def _value(observation: Mapping[str, Any]) -> Mapping[str, Any]:
    value = observation.get("value")
    return value if isinstance(value, Mapping) else {}


def _program_loaded_stopped(dashboard: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(dashboard.get("programState", dashboard.get("program_state", "")))
    state = raw.split(maxsplit=1)[0].upper() if raw else ""
    basenames = {Path(match).name.lower() for match in TP_PROGRAM_PATTERN.findall(raw)}
    expected = f"{TP_PROGRAM_ID}.urp".lower()
    checks = {"stopped": state == "STOPPED", "exact_program": basenames == {expected}}
    return {"ok": all(checks.values()), "checks": checks, "raw": raw}


def _stationary(rtde: Mapping[str, Any]) -> dict[str, Any]:
    speed = rtde.get("actual_TCP_speed")
    qd = rtde.get("actual_qd", [0.0] * 6)
    if not (
        isinstance(speed, list)
        and len(speed) == 6
        and isinstance(qd, list)
        and len(qd) == 6
    ):
        return {"ok": False, "error": "RTDE stationary vectors are incomplete"}
    try:
        linear = math.sqrt(sum(float(value) ** 2 for value in speed[:3]))
        joint = max(abs(float(value)) for value in qd)
    except (TypeError, ValueError):
        return {"ok": False, "error": "RTDE stationary vectors are nonnumeric"}
    return {
        "ok": math.isfinite(linear)
        and math.isfinite(joint)
        and linear <= 0.001
        and joint <= 0.005,
        "tcp_linear_speed_m_s": linear,
        "joint_speed_max_rad_s": joint,
        "limits": {"tcp_linear_speed_m_s": 0.001, "joint_speed_max_rad_s": 0.005},
    }


def _safety_normal(dashboard: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(dashboard.get("safetymode", dashboard.get("safety_mode", "")))
    value = raw.rsplit(":", 1)[-1].strip().upper()
    return {"ok": value == "NORMAL", "observed": value}


def _mailbox_hold_zero(path: Path) -> dict[str, Any]:
    absent = not path.exists() and not path.is_symlink()
    return {"ok": absent, "path": str(path), "policy": "absent_before_hold_bridge"}


def _connect_observation_ok(observation: Mapping[str, Any]) -> bool:
    value = _value(observation)
    return value.get("ok", True) is True and value.get("open") is True


def _controller_identity(
    dashboard: Mapping[str, Any], rtde: Mapping[str, Any], *, robot_host: str
) -> tuple[dict[str, Any], str]:
    material = {
        "schema": "step5d.autotune-v3/controller-identity-snapshot-v1",
        "robot_host": robot_host,
        "dashboard": {
            key: dashboard.get(key)
            for key in (
                "PolyscopeVersion",
                "robotmode",
                "safetymode",
                "programState",
                "is in remote control",
                "remote_control",
            )
            if key in dashboard
        },
        "rtde": {
            key: rtde.get(key)
            for key in ("robot_mode", "safety_mode", "runtime_state")
        },
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return material, hashlib.sha256(encoded).hexdigest()


def run_preflight(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    authorization = authorization_gate.verify_authorization(
        args.authorization,
        expected_thread_id=args.expected_thread_id,
        root=ROOT,
    )
    launch = load_launch_profile(args.launch_profile)
    governed_argv = build_bridge_argv(
        args.mailbox.parent,
        launch_profile=launch,
        trial_overlay=DEFAULT_OVERLAY,
    )
    local = _parallel(
        {
            "route_robot": lambda: base.run_command(["ip", "route", "get", args.robot_host]),
            "route_kunwei": lambda: base.run_command(["ip", "route", "get", args.sensor_ip]),
            "writer": base.no_existing_writer,
            "realtime": base.realtime_capability,
            "runtime_calibration": dependency_observation,
            "production_startup_prewarm": lambda: bridge_wrapper.check_v3_runtime_prewarm(
                governed_argv[2:]
            ),
        }
    )
    local_ok = all(_value(item).get("ok") is True for item in local.values())
    if not local_ok:
        remote: dict[str, Any] = {"skipped": "local_stage_failed"}
        dashboard: Mapping[str, Any] = {}
        rtde: Mapping[str, Any] = {}
    else:
        remote = _parallel(
            {
                "dashboard": lambda: base.dashboard_exchange(
                    args.robot_host,
                    [
                        "PolyscopeVersion",
                        "is in remote control",
                        "safetymode",
                        "robotmode",
                        "programState",
                    ],
                    timeout=args.timeout_s,
                ),
                "rtde": lambda: base.read_rtde_once(
                    args.robot_host,
                    [*base.RTDE_FIELDS, "actual_qd"],
                    frequency_hz=10.0,
                    timeout=args.timeout_s,
                ),
                "secondary_port": lambda: base.probe_port(
                    args.robot_host, 30002, timeout=args.timeout_s
                ),
                "kunwei": lambda: base.tcp_connect_only(
                    args.sensor_ip, args.sensor_port, args.timeout_s
                ),
            }
        )
        dashboard = _value(remote["dashboard"])
        rtde = _value(remote["rtde"])
    controller_identity, controller_sha = _controller_identity(
        dashboard, rtde, robot_host=args.robot_host
    )
    predicates = {
        "safety_normal": _safety_normal(dashboard),
        "program_loaded_stopped": _program_loaded_stopped(dashboard),
        "robot_stationary": _stationary(rtde),
        "no_existing_writer": {
            "ok": _value(local.get("writer", {})).get("ok") is True,
            "observation": _value(local.get("writer", {})),
        },
        "mailbox_hold_zero": _mailbox_hold_zero(args.mailbox),
        "runtime_dependencies": {
            "ok": all(
                _value(local.get(name, {})).get("ok") is True
                for name in ("runtime_calibration", "production_startup_prewarm")
            ),
            "calibration": _value(local.get("runtime_calibration", {})),
            "production_startup_prewarm": _value(
                local.get("production_startup_prewarm", {})
            ),
        },
    }
    transport_ok = (
        local_ok
        and "dashboard" in remote
        and bool(dashboard)
        and _value(remote["dashboard"]).get("ok", True) is True
        and "rtde" in remote
        and base.rtde_predicate(dict(rtde)).get("ok") is True
        and _connect_observation_ok(remote.get("secondary_port", {}))
        and _connect_observation_ok(remote.get("kunwei", {}))
    )
    elapsed = time.monotonic() - started
    payload = {
        "schema": SCHEMA,
        "ok": transport_ok and all(item["ok"] is True for item in predicates.values()),
        "fresh": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": elapsed,
        "healthy_target_s": 1.25,
        "candidate_stage_id": RELEASE_STAGE_ID,
        "control_profile_id": CONTROL_PROFILE_ID,
        "tp_program_id": TP_PROGRAM_ID,
        "authorization_id": authorization["authorization_id"],
        "identity": authorization["identity"],
        "launch_profile_fingerprint": launch.fingerprint,
        "controller_identity": controller_identity,
        "controller_identity_sha256": controller_sha,
        "predicates": predicates,
        "observations": {"local": local, "remote": remote},
        "probe_reuse": {
            "dashboard_port_29999": "dashboard observation",
            "rtde_port_30004": "RTDE observation",
            "secondary_port_30002": "one connect-only probe",
            "kunwei": "one connect-only probe",
            "ping": "omitted_from_fast_gate",
            "bench_network": "omitted_from_fast_gate",
        },
        "safety_boundary": [
            "Dashboard read-only commands only",
            "RTDE output recipe only",
            "Kunwei TCP connect-only; no stream/zero/tare command",
            "local runtime calibration artifact and installed model sources are hash-checked",
            "production startup prewarm runs locally before any device writer is started",
            "no bridge, RTDE input, Load, Play, ARM, contact, or motion",
        ],
    }
    atomic_json(args.output, payload)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--expected-thread-id", required=True)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument(
        "--launch-profile",
        type=Path,
        default=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = run_preflight(args)
    except Exception as exc:
        payload = {"schema": SCHEMA, "ok": False, "fresh": False, "blocker": str(exc)}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok") is True else 2


if __name__ == "__main__":
    bootstrap_stable_cuda_runtime()
    raise SystemExit(main())
