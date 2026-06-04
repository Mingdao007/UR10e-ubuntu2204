#!/usr/bin/env python3
"""Read-only preflight for the Kunwei closed-loop straight-line experiment."""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
UR_REALSETUP_SCRIPTS = Path("/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts")
sys.path.insert(0, str(UR_REALSETUP_SCRIPTS))

from _ur_common import dashboard_exchange, probe_port, read_rtde_once  # noqa: E402


RTDE_FIELDS = [
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_TCP_force",
    "actual_q",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    "speed_scaling",
    "payload",
    "payload_cog",
    "tcp_offset",
]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_output_dir() -> Path:
    return EXPERIMENT_ROOT / "runs" / f"preflight_{now_stamp()}"


def run_command(args: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=5.0, check=False)
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "args": args}
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "args": args,
    }


def tcp_connect_only(host: str, port: int, timeout_s: float) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {"host": host, "port": port, "open": False}
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_s)
        try:
            sock.connect((host, port))
            result["open"] = True
            result["connect_ms"] = (time.monotonic() - started) * 1000.0
        except OSError as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def vector_norm(values: list[float]) -> float:
    return sum(value * value for value in values) ** 0.5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--json-only", action="store_true")
    args = parser.parse_args(argv)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.output_dir / "preflight_readonly.json"

    dashboard: dict[str, str] | None
    rtde: dict[str, Any] | None
    errors: list[str] = []

    try:
        dashboard = dashboard_exchange(
            args.robot_host,
            ["is in remote control", "safetymode", "robotmode", "running", "programState"],
            timeout=args.timeout_s,
        )
    except Exception as exc:
        dashboard = None
        errors.append(f"dashboard: {type(exc).__name__}: {exc}")

    try:
        rtde = read_rtde_once(args.robot_host, RTDE_FIELDS, frequency_hz=10.0, timeout=args.timeout_s)
    except Exception as exc:
        rtde = None
        errors.append(f"rtde: {type(exc).__name__}: {exc}")

    robot_ports = {
        str(port): probe_port(args.robot_host, port, timeout=args.timeout_s)
        for port in [29999, 30002, 30004]
    }
    sensor_tcp = tcp_connect_only(args.sensor_ip, args.sensor_port, args.timeout_s)
    route_robot = run_command(["ip", "route", "get", args.robot_host])
    route_sensor = run_command(["ip", "route", "get", args.sensor_ip])

    payload: dict[str, Any] = {
        "ok": not errors,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "safety_boundary": [
            "read-only Dashboard commands",
            "read-only RTDE output recipe",
            "TCP connect-only probe to Kunwei endpoint",
            "no Kunwei 0x48/0x49/0x43 commands",
            "no URScript upload, no motion, no zero, no TCP/payload writes",
        ],
        "errors": errors,
        "dashboard": dashboard,
        "rtde": rtde,
        "derived": {},
        "ports": {
            "robot": robot_ports,
            "kunwei_tcp_connect_only": sensor_tcp,
        },
        "routes": {
            "robot": route_robot,
            "kunwei": route_sensor,
        },
    }
    if rtde:
        payload["derived"] = {
            "actual_tcp_speed_linear_norm_m_s": vector_norm(rtde["actual_TCP_speed"][:3]),
            "actual_tcp_force_norm_n": vector_norm(rtde["actual_TCP_force"][:3]),
            "actual_tcp_torque_norm_nm": vector_norm(rtde["actual_TCP_force"][3:]),
            "tcp_offset_z_mm": rtde["tcp_offset"][2] * 1000.0,
            "payload_kg": rtde["payload"],
        }

    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json_only:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"preflight_json={out_path}")
        print(f"ok={payload['ok']}")
        if errors:
            print("errors=" + "; ".join(errors))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
