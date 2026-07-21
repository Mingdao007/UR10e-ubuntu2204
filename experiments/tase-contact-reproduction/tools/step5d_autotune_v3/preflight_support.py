"""Repository-owned, read-only host probes for the active V3 preflight."""

from __future__ import annotations

import math
import os
import socket
import subprocess
import time
from typing import Any, Sequence


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


def run_command(args: Sequence[str], *, timeout_s: float = 5.0) -> dict[str, Any]:
    command = list(args)
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "args": command,
        }
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "args": command,
    }


def no_existing_writer() -> dict[str, Any]:
    probe = run_command(
        [
            "pgrep",
            "-af",
            "kunwei_rtde_bridge.py|step5d_p0_v8_bridge.py|step5d_p0_v9_bridge.py",
        ]
    )
    matches = [line for line in probe.get("stdout", "").splitlines() if line]
    returncode = probe.get("returncode")
    return {
        "ok": returncode == 1 and not matches and not probe.get("stderr"),
        "active_writers": matches,
        "probe": probe,
    }


def realtime_capability() -> dict[str, Any]:
    chrt = run_command(["chrt", "--max"])
    affinity = run_command(["taskset", "-pc", str(os.getpid())])
    return {
        "ok": chrt.get("ok") is True and affinity.get("ok") is True,
        "chrt": chrt,
        "affinity": affinity,
    }


def probe_port(host: str, port: int, timeout: float = 1.5) -> dict[str, Any]:
    result: dict[str, Any] = {"port": port, "open": False}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((host, port))
            result["open"] = True
            try:
                banner = sock.recv(128)
            except socket.timeout:
                banner = b""
    except OSError as exc:
        result["open"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    if banner:
        try:
            text = banner.decode("ascii")
        except UnicodeDecodeError:
            result["banner_hex"] = banner.hex()
        else:
            if text.isprintable():
                result["banner"] = text.strip()
            else:
                result["banner_hex"] = banner.hex()
    return result


def tcp_connect_only(host: str, port: int, timeout_s: float) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {"host": host, "port": port, "open": False}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout_s)
            sock.connect((host, port))
    except OSError as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    result["open"] = True
    result["connect_ms"] = (time.monotonic() - started) * 1000.0
    return result


def rtde_predicate(result: Any) -> dict[str, Any]:
    lengths = {
        "actual_TCP_pose": 6,
        "actual_TCP_speed": 6,
        "actual_TCP_force": 6,
        "actual_q": 6,
        "payload_cog": 3,
        "tcp_offset": 6,
    }
    if not isinstance(result, dict):
        return {"ok": False, "checks": {"object": False}}
    checks: dict[str, bool] = {}
    for field in RTDE_FIELDS:
        value = result.get(field)
        if field in lengths:
            checks[field] = (
                isinstance(value, list)
                and len(value) == lengths[field]
                and all(
                    isinstance(item, (int, float))
                    and not isinstance(item, bool)
                    and math.isfinite(float(item))
                    for item in value
                )
            )
        else:
            checks[field] = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
    return {"ok": all(checks.values()), "checks": checks}


__all__ = [
    "RTDE_FIELDS",
    "no_existing_writer",
    "probe_port",
    "realtime_capability",
    "rtde_predicate",
    "run_command",
    "tcp_connect_only",
]
