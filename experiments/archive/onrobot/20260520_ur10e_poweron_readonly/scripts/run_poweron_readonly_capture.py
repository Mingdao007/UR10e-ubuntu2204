#!/usr/bin/env python3
"""Run the first UR10e + OnRobot read-only power-on checks.

This orchestrator intentionally performs no robot motion, no freedrive, no
zero_ftsensor(), no payload/TCP writes, and no OnRobot bias/config calls.
It only runs existing read-only helpers and, when explicitly confirmed, a
static no-contact 60 s force capture from UR RTDE and the OnRobot web stream.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


EXP_ROOT = Path("/home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260520_ur10e_poweron_readonly")
UR_SKILL_ROOT = Path("/home/andy/codex-private-skills/skills/ur10e-realsetup")
ONROBOT_ROOT = Path("/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def run_json(name: str, cmd: list[str], cwd: Path, out_dir: Path) -> dict[str, Any]:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    (out_dir / f"{name}.stdout").write_text(result.stdout, encoding="utf-8")
    (out_dir / f"{name}.stderr").write_text(result.stderr, encoding="utf-8")
    payload: dict[str, Any]
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {
            "ok": False,
            "error": "stdout was not JSON",
            "returncode": result.returncode,
            "stdout_path": str(out_dir / f"{name}.stdout"),
            "stderr_path": str(out_dir / f"{name}.stderr"),
        }
    payload["command"] = cmd
    payload["returncode"] = result.returncode
    (out_dir / f"{name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def onrobot_version(host: str, timeout_s: float) -> dict[str, Any]:
    url = f"http://{host.rstrip('/')}/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            body = response.read().decode("utf-8", errors="replace")
        return {"ok": True, "url": url, "response": body}
    except Exception as exc:
        return {"ok": False, "url": url, "error": f"{type(exc).__name__}: {exc}"}


def response_contains(dashboard: dict[str, Any], command: str, token: str) -> bool:
    response = dashboard.get("responses", {}).get(command, "")
    return token.lower() in str(response).lower()


def safety_is_normal(dashboard: dict[str, Any]) -> bool:
    text = str(dashboard.get("responses", {}).get("safetymode", ""))
    lowered = text.lower()
    return "normal" in lowered and "protective" not in lowered and "fault" not in lowered


def write_markdown_report(run_dir: Path, summary: dict[str, Any]) -> None:
    dashboard = summary.get("dashboard_state", {})
    payload_tcp = summary.get("payload_tcp_state", {})
    onrobot = summary.get("onrobot_version", {})
    capture = summary.get("simultaneous_capture", {})
    report = f"""# UR10e + OnRobot Read-Only Power-On Check

- Run directory: `{run_dir}`
- Generated: `{datetime.now().isoformat(timespec="seconds")}`
- UR10e power state: inferred from network/Dashboard reachability in this run
- Robot motion: none commanded by this script
- UR writes: none; payload/TCP were only read
- UR zero_ftsensor(): not called
- OnRobot writes: none; logger reads `/version` and Socket.IO polling only
- Same static posture capture: `{capture.get("attempted", False)}`

## Operator Physical Notes

Fill these in immediately after the run:

- Stack-up: UR10e flange -> OnRobot HEX-E -> adapter -> custom EOAT -> KSM-8N
- Tool/environment contact during capture:
- Cable slack at J4/J5/J6:
- Connector strain or cable rubbing:
- Any installation disturbance:

## Dashboard

```json
{json.dumps(dashboard.get("responses", dashboard), indent=2, ensure_ascii=False)}
```

## Payload/TCP Readback

```json
{json.dumps(payload_tcp, indent=2, ensure_ascii=False)}
```

## OnRobot Version Check

```json
{json.dumps(onrobot, indent=2, ensure_ascii=False)}
```

## Capture

```json
{json.dumps(capture, indent=2, ensure_ascii=False)}
```
"""
    (run_dir / "report.md").write_text(report, encoding="utf-8")


def run_simultaneous_capture(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    ur_dir = run_dir / "ur_actual_tcp_force_60s"
    onrobot_dir = run_dir / "onrobot_wrench_60s"
    ur_dir.mkdir(parents=True, exist_ok=True)
    onrobot_dir.mkdir(parents=True, exist_ok=True)

    seconds_text = str(args.seconds)
    ur_cmd = [
        sys.executable,
        "scripts/sample_tcp_force.py",
        "--seconds",
        seconds_text,
        "--hz",
        str(args.ur_hz),
        "--output-dir",
        str(ur_dir),
        "--prefix",
        "S00_ur_actual_tcp_force_static",
        "--plot",
        "all",
        "--json-only",
    ]
    onrobot_cmd = [
        sys.executable,
        "tools/onrobot_socketio_logger.py",
        "--host",
        args.onrobot_host,
        "--duration-s",
        seconds_text,
        "--out-dir",
        str(onrobot_dir),
        "--status-every-s",
        str(max(10.0, min(30.0, args.seconds / 2.0))),
        "--baseline-s",
        str(min(10.0, args.seconds)),
        "--bin-s",
        str(min(10.0, args.seconds)),
    ]

    started = datetime.now().isoformat(timespec="milliseconds")
    with (run_dir / "ur_sample.stdout").open("w", encoding="utf-8") as ur_out, (
        run_dir / "ur_sample.stderr"
    ).open("w", encoding="utf-8") as ur_err, (run_dir / "onrobot_logger.stdout").open(
        "w", encoding="utf-8"
    ) as on_out, (
        run_dir / "onrobot_logger.stderr"
    ).open(
        "w", encoding="utf-8"
    ) as on_err:
        ur_proc = subprocess.Popen(ur_cmd, cwd=UR_SKILL_ROOT, stdout=ur_out, stderr=ur_err, text=True)
        on_proc = subprocess.Popen(onrobot_cmd, cwd=ONROBOT_ROOT, stdout=on_out, stderr=on_err, text=True)
        ur_code = ur_proc.wait()
        on_code = on_proc.wait()

    finished = datetime.now().isoformat(timespec="milliseconds")
    return {
        "attempted": True,
        "started": started,
        "finished": finished,
        "seconds": args.seconds,
        "ur_command": ur_cmd,
        "onrobot_command": onrobot_cmd,
        "ur_returncode": ur_code,
        "onrobot_returncode": on_code,
        "ur_output_dir": str(ur_dir),
        "onrobot_output_dir": str(onrobot_dir),
        "ur_stdout": str(run_dir / "ur_sample.stdout"),
        "ur_stderr": str(run_dir / "ur_sample.stderr"),
        "onrobot_stdout": str(run_dir / "onrobot_logger.stdout"),
        "onrobot_stderr": str(run_dir / "onrobot_logger.stderr"),
        "ok": ur_code == 0 and on_code == 0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", default=str(EXP_ROOT / "runs"))
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--onrobot-host", default="192.168.1.1")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--ur-hz", type=float, default=20.0)
    parser.add_argument("--timeout-s", type=float, default=3.0)
    parser.add_argument("--confirm-no-motion", action="store_true")
    parser.add_argument("--confirm-no-contact", action="store_true")
    parser.add_argument("--confirm-cables-slack", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_root).expanduser().resolve() / f"readonly_poweron_{now_stamp()}"
    diag_dir = run_dir / "diagnostics"
    diag_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "run_dir": str(run_dir),
        "robot_host": args.robot_host,
        "onrobot_host": args.onrobot_host,
        "operator_confirmations": {
            "no_motion": args.confirm_no_motion,
            "no_contact": args.confirm_no_contact,
            "cables_slack": args.confirm_cables_slack,
        },
    }

    summary["ubuntu_network"] = run_json(
        "check_ubuntu_network",
        [sys.executable, "scripts/check_ubuntu_network.py", "--robot-host", args.robot_host, "--json-only"],
        UR_SKILL_ROOT,
        diag_dir,
    )
    summary["ur_interfaces"] = run_json(
        "check_ur_interfaces",
        [sys.executable, "scripts/check_ur_interfaces.py", "--host", args.robot_host, "--json-only"],
        UR_SKILL_ROOT,
        diag_dir,
    )
    summary["dashboard_state"] = run_json(
        "check_dashboard_state",
        [sys.executable, "scripts/check_dashboard_state.py", "--host", args.robot_host, "--json-only"],
        UR_SKILL_ROOT,
        diag_dir,
    )
    summary["payload_tcp_state"] = run_json(
        "read_payload_tcp_state",
        [sys.executable, "scripts/read_payload_tcp_state.py", "--host", args.robot_host, "--json-only"],
        UR_SKILL_ROOT,
        diag_dir,
    )
    summary["onrobot_version"] = onrobot_version(args.onrobot_host, args.timeout_s)

    confirmations_ok = all(summary["operator_confirmations"].values())
    dashboard_ok = bool(summary["dashboard_state"].get("ok")) and safety_is_normal(summary["dashboard_state"])
    interfaces_ok = bool(summary["ur_interfaces"].get("ok"))
    payload_tcp_ok = bool(summary["payload_tcp_state"].get("ok"))
    onrobot_ok = bool(summary["onrobot_version"].get("ok"))

    if not confirmations_ok:
        summary["simultaneous_capture"] = {
            "attempted": False,
            "reason": "missing explicit operator confirmations",
        }
    elif not dashboard_ok:
        summary["simultaneous_capture"] = {
            "attempted": False,
            "reason": "Dashboard not reachable or safety mode not normal",
        }
    elif not interfaces_ok or not payload_tcp_ok or not onrobot_ok:
        summary["simultaneous_capture"] = {
            "attempted": False,
            "reason": "required read-only interfaces were not all reachable",
            "interfaces_ok": interfaces_ok,
            "payload_tcp_ok": payload_tcp_ok,
            "onrobot_ok": onrobot_ok,
        }
    else:
        time.sleep(1.0)
        summary["simultaneous_capture"] = run_simultaneous_capture(args, run_dir)

    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_markdown_report(run_dir, summary)

    print(json.dumps(summary, indent=2), flush=True)
    return 0 if summary["simultaneous_capture"].get("ok", False) or not summary["simultaneous_capture"].get("attempted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
