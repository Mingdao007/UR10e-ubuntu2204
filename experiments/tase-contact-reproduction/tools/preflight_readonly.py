#!/usr/bin/env python3
"""Read-only preflight for the Kunwei closed-loop straight-line experiment."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
BENCH_GATE = Path(
    "/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts/"
    "check_ubuntu_network.py"
)
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


def no_existing_writer() -> dict[str, Any]:
    result = run_command(
        ["pgrep", "-af", "kunwei_rtde_bridge.py|step5d_p0_v8_bridge.py"]
    )
    matches = [line for line in result.get("stdout", "").splitlines() if line]
    return {"ok": not matches, "active_writers": matches}


def realtime_capability() -> dict[str, Any]:
    chrt = run_command(["chrt", "--max"])
    affinity = run_command(["taskset", "-pc", str(os.getpid())])
    return {"ok": chrt.get("ok") is True and affinity.get("ok") is True,
            "chrt": chrt, "affinity": affinity}


def _open(result: Any) -> bool:
    return isinstance(result, dict) and result.get("ok") is True and result.get("open", True) is True


def dashboard_predicate(result: Any) -> dict[str, Any]:
    result = result if isinstance(result, dict) else {}
    def value(*keys: str) -> str:
        raw = next((result[key] for key in keys if key in result), "")
        text = str(raw).strip()
        return text.split(":", 1)[-1].strip().upper()
    robot_mode = value("robotmode", "robot_mode")
    program_state = value("programState", "program_state")
    safety_mode = value("safetymode", "safety_mode")
    remote = result.get("remote_control") is True or value("is in remote control") == "TRUE"
    program_token = program_state.split(maxsplit=1)[0] if program_state else ""
    checks = {
        "remote_control": remote,
        "safety_normal": safety_mode == "NORMAL",
        "robot_mode": robot_mode in {"RUNNING", "IDLE", "POWER_ON"},
        "program_state": program_token in {"STOPPED", "PLAYING", "PAUSED", "RUNNING"},
    }
    return {"ok": all(checks.values()), "checks": checks}


def rtde_predicate(result: Any) -> dict[str, Any]:
    lengths = {"actual_TCP_pose": 6, "actual_TCP_speed": 6, "actual_TCP_force": 6,
               "actual_q": 6, "payload_cog": 3, "tcp_offset": 6}
    checks: dict[str, bool] = {}
    if not isinstance(result, dict):
        return {"ok": False, "checks": {"object": False}}
    for field in RTDE_FIELDS:
        value = result.get(field)
        if field in lengths:
            checks[field] = isinstance(value, list) and len(value) == lengths[field] and all(
                isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item))
                for item in value
            )
        else:
            checks[field] = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
    return {"ok": all(checks.values()), "checks": checks}


def safe_call(label: str, function: Any) -> tuple[str, Any]:
    try:
        return label, function()
    except Exception as exc:
        return label, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


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

    started = time.monotonic()
    created_at = datetime.now().isoformat(timespec="milliseconds")
    local_checks = {
        "route_robot": lambda: run_command(["ip", "route", "get", args.robot_host]),
        "route_kunwei": lambda: run_command(["ip", "route", "get", args.sensor_ip]),
        "writer_process": no_existing_writer,
        "realtime": realtime_capability,
    }
    with ThreadPoolExecutor(max_workers=len(local_checks)) as executor:
        local = dict(
            future.result()
            for future in [
                executor.submit(safe_call, label, function)
                for label, function in local_checks.items()
            ]
        )
    local_ok = all(
        bool(local[name].get("ok"))
        for name in ("route_robot", "route_kunwei", "writer_process", "realtime")
    )

    remote: dict[str, Any] = {}
    if local_ok:
        remote_checks = {
            "dashboard": lambda: dashboard_exchange(
                args.robot_host,
                [
                    "is in remote control",
                    "safetymode",
                    "robotmode",
                    "running",
                    "programState",
                ],
                timeout=args.timeout_s,
            ),
            "rtde": lambda: read_rtde_once(
                args.robot_host,
                RTDE_FIELDS,
                frequency_hz=10.0,
                timeout=args.timeout_s,
            ),
            "robot_ports": lambda: {
                str(port): probe_port(args.robot_host, port, timeout=args.timeout_s)
                for port in (29999, 30002, 30004)
            },
            "kunwei_tcp_connect_only": lambda: tcp_connect_only(
                args.sensor_ip, args.sensor_port, args.timeout_s
            ),
            "controller_binding": lambda: run_command(
                [
                    sys.executable,
                    str(EXPERIMENT_ROOT / "tools/verify_step5d_current_binding.py"),
                    "--root",
                    str(EXPERIMENT_ROOT),
                    "--json",
                ]
            ),
            "bench_network": lambda: run_command(
                [sys.executable, str(BENCH_GATE), "--include-kunwei", "--json-only"]
            ),
        }
        with ThreadPoolExecutor(max_workers=len(remote_checks)) as executor:
            remote = dict(
                future.result()
                for future in [
                    executor.submit(safe_call, label, function)
                    for label, function in remote_checks.items()
                ]
            )
    else:
        remote = {"skipped": "local_stage_failed"}

    dashboard = remote.get("dashboard") if local_ok else None
    rtde = remote.get("rtde") if local_ok else None
    bench_network = remote.get("bench_network") if local_ok else None
    if isinstance(bench_network, dict) and bench_network.get("ok"):
        try:
            bench_network["payload"] = json.loads(bench_network.get("stdout") or "{}")
        except json.JSONDecodeError:
            bench_network["ok"] = False
            bench_network["error"] = "bench network output was not JSON"
    predicates = {
        "realtime": {"ok": local.get("realtime", {}).get("ok") is True},
        "dashboard": dashboard_predicate(dashboard),
        "rtde": rtde_predicate(rtde),
        "robot_ports": {"ok": local_ok and all(_open(value) for value in (remote.get("robot_ports") or {}).values())},
        "kunwei": {"ok": _open(remote.get("kunwei_tcp_connect_only"))},
    }
    remote_ok = local_ok and all(
        bool(remote[name].get("ok", True))
        for name in remote
        if name not in {"dashboard", "rtde", "robot_ports"}
    )
    if local_ok:
        remote_ok = remote_ok and all(item["ok"] for item in predicates.values())
    errors = [
        f"local:{name}" for name, result in local.items() if not result.get("ok")
    ]
    if local_ok:
        errors.extend(
            f"remote:{name}"
            for name, result in remote.items()
            if isinstance(result, dict) and result.get("ok") is False
        )
        errors.extend(f"predicate:{name}" for name, result in predicates.items() if not result["ok"])

    payload: dict[str, Any] = {
        "ok": local_ok and remote_ok and not errors,
        "schema_version": "ur10e_readonly_preflight_snapshot_v2",
        "contract_id": "ur10e_concurrency_contract_v1",
        "created_at": created_at,
        "completed_at": datetime.now().isoformat(timespec="milliseconds"),
        "elapsed_s": time.monotonic() - started,
        "safety_boundary": [
            "read-only Dashboard commands",
            "read-only RTDE output recipe",
            "TCP connect-only probe to Kunwei endpoint",
            "no Kunwei 0x48/0x49/0x43 commands",
            "no URScript upload, no motion, no zero, no TCP/payload writes",
        ],
        "errors": errors,
        "predicates": predicates,
        "stages": {"local": local, "remote": remote},
        "dashboard": dashboard,
        "rtde": rtde,
        "derived": {},
        "ports": {
            "robot": remote.get("robot_ports", {}),
            "kunwei_tcp_connect_only": remote.get("kunwei_tcp_connect_only", {}),
        },
        "routes": {
            "robot": local["route_robot"],
            "kunwei": local["route_kunwei"],
        },
    }
    required_derived_fields = {
        "actual_TCP_speed",
        "actual_TCP_force",
        "tcp_offset",
        "payload",
    }
    if isinstance(rtde, dict) and required_derived_fields.issubset(rtde):
        payload["derived"] = {
            "actual_tcp_speed_linear_norm_m_s": vector_norm(rtde["actual_TCP_speed"][:3]),
            "actual_tcp_force_norm_n": vector_norm(rtde["actual_TCP_force"][:3]),
            "actual_tcp_torque_norm_nm": vector_norm(rtde["actual_TCP_force"][3:]),
            "tcp_offset_z_mm": rtde["tcp_offset"][2] * 1000.0,
            "payload_kg": rtde["payload"],
        }
    elif local_ok:
        payload["ok"] = False
        payload["errors"].append("remote:rtde_required_fields_missing")

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
