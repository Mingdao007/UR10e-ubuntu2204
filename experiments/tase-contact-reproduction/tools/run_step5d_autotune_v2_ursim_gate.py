#!/usr/bin/env python3
"""Run the no-motion Step5d startup gate in a digest-pinned URSim container."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from step5d_autotune_v2.dashboard import DashboardClient, DashboardError
from step5d_autotune_v2.startup_smoke import RtdeHoldSession, verify_play_startup


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "universalrobots/ursim_e-series:5.25.2@sha256:a4c4365207d54d1a1a4ead87526ff3781e2e98ae703c72f362060a46688fa7a4"
PROGRAM = "step5d_strict_rnn_autotune_v2.urp"
CONTAINER_PROGRAM_DIR = "/ursim/programs/andyl/kunwei/step5"
DASHBOARD_PROGRAM = f"andyl/kunwei/step5/{PROGRAM}"
GUI_VOLUME = "step5d-ursim-gui-5-25-2-ur10-v1"
URCAP_VOLUME = "step5d-ursim-urcaps-5-25-2-ur10-v1"
PROGRAMS_VOLUME = "step5d-ursim-programs-5-25-2-ur10-v1"
CONTROL_VOLUME = "step5d-ursim-control-5-25-2-ur10-v1"


def _write(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _wait_port(host: str, port: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"URSim port {port} did not become ready")


def _wait_dashboard(host: str, timeout_s: float) -> tuple[DashboardClient, str]:
    deadline = time.monotonic() + timeout_s
    last_error = "no attempt"
    while time.monotonic() < deadline:
        client = DashboardClient(host, connect_timeout_s=1.0, response_timeout_s=2.0)
        try:
            version = client.command("PolyscopeVersion", ("URSoftware",)).matched_line
            robot_mode = client.command("robotmode", ("Robotmode:",)).matched_line
            if "NO_CONTROLLER" in robot_mode:
                last_error = robot_mode
                time.sleep(0.5)
                continue
            return client, version
        except DashboardError as exc:
            last_error = str(exc)
            time.sleep(0.5)
    raise RuntimeError(f"URSim Dashboard did not become command-ready: {last_error}")


def _load_program(
    dashboard: DashboardClient,
    dashboard_path: str,
    timeout_s: float,
) -> str:
    deadline = time.monotonic() + timeout_s
    last_response = "no attempt"
    while time.monotonic() < deadline:
        response = dashboard.command(
            f"load {dashboard_path}",
            ("Loading program:", "Error while loading program:"),
        ).matched_line
        if response.startswith("Loading program:"):
            return response
        last_response = response
        time.sleep(0.5)
    raise RuntimeError(f"URSim program loader did not become ready: {last_response}")


def _docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
    )


def run(output: Path, *, timeout_s: float = 90.0) -> dict[str, Any]:
    name = f"codex-step5d-ursim-{os.getpid()}"
    program_dir = (ROOT / "programs/step5/step5d").resolve()
    started = False
    dashboard: DashboardClient | None = None
    try:
        _docker("info")
        _docker("pull", IMAGE)
        _docker("volume", "inspect", GUI_VOLUME)
        _docker("volume", "inspect", URCAP_VOLUME)
        _docker("volume", "inspect", PROGRAMS_VOLUME)
        _docker("volume", "inspect", CONTROL_VOLUME)
        _docker(
            "run", "-d", "--rm", "--name", name,
            "-e", "ROBOT_MODEL=UR10",
            "--mount", f"source={GUI_VOLUME},target=/ursim/GUI",
            "--mount", f"source={URCAP_VOLUME},target=/ursim/.urcaps",
            "--mount", f"source={PROGRAMS_VOLUME},target=/ursim/programs.UR10",
            "--mount", f"source={CONTROL_VOLUME},target=/ursim/.urcontrol",
            IMAGE,
        )
        started = True
        _docker("exec", name, "mkdir", "-p", CONTAINER_PROGRAM_DIR)
        for suffix in (".urp", ".script", ".txt"):
            source = program_dir / f"step5d_strict_rnn_autotune_v2{suffix}"
            _docker("cp", str(source), f"{name}:{CONTAINER_PROGRAM_DIR}/")
        host = _docker(
            "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name
        ).stdout.strip()
        if not host:
            raise RuntimeError("URSim container has no IP address")
        _wait_port(host, 29999, timeout_s)
        _wait_port(host, 30004, timeout_s)
        dashboard, version = _wait_dashboard(host, timeout_s)
        loaded = _load_program(dashboard, DASHBOARD_PROGRAM, timeout_s)
        dashboard.command("power on", ("Powering on", "Robot is already powered on"))
        time.sleep(2.0)
        dashboard.command("brake release", ("Brake releasing", "Brake release"))
        time.sleep(2.0)
        with RtdeHoldSession(host, timeout_s=5.0) as session:
            result = verify_play_startup(
                session,
                trigger_play=lambda: dashboard.command("play", ("Starting program",)),
                timeout_s=timeout_s,
            )
        dashboard.command("stop", ("Stopped", "Stop"))
        payload = {
            "schema": "step5d.autotune.ursim-startup-gate/v1",
            "ok": True,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "image": IMAGE,
            "robot_model": "UR10",
            "safety_setup": "official_noVNC_confirmation_persisted",
            "gui_volume": GUI_VOLUME,
            "programs_volume": PROGRAMS_VOLUME,
            "control_volume": CONTROL_VOLUME,
            "software_version": version,
            "loaded_program": loaded,
            "motion_allowed": False,
            "host_command": "HOLD",
            "phases": list(result.phases),
            "startup_duration_s": result.startup_duration_s,
            "first_active_state": result.first_active_state,
            "consumed_command_seq": result.consumed_command_seq,
            "samples": result.samples,
        }
    except Exception as exc:
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail = f"{detail}: {exc.stderr.strip()}"
        payload = {
            "schema": "step5d.autotune.ursim-startup-gate/v1",
            "ok": False,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "image": IMAGE,
            "motion_allowed": False,
            "error": f"{type(exc).__name__}: {detail}",
        }
    finally:
        if dashboard is not None:
            try:
                dashboard.command("stop", ("Stopped", "Stop"))
            except Exception:
                pass
        if started:
            _docker("stop", "--time", "5", name, check=False)
    _write(output, payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=90.0)
    args = parser.parse_args()
    payload = run(args.output, timeout_s=args.timeout_s)
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
