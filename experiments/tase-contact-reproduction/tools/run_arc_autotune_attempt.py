#!/usr/bin/env python3
"""Run one fully recorded Step4d arc autotune attempt.

The attempt owner serializes package delivery, bridge, Dashboard, video and
recovery.  It accepts a candidate only after local package validation and a
fresh controller read-back.  On every normal or failed terminal path it checks
the approved Home and invokes the segmented Home owner if the TP program did
not return there itself.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from contact_yield_live_contract import load_identity_contract
from contact_yield_supervisor import VideoRecorder, stationary
from run_contact_home import Observer
from step5d_remote_startup import (
    RemoteDashboardWriter,
    _ExactLoadAdapter,
    dashboard_exchange,
)


HOST_DEFAULT = "192.168.1.18"
SENSOR_IP = "192.168.50.25"
SENSOR_PORT = 5152
CONTROLLER_DIR = "/programs/andyl/kunwei/step4"
EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
SKILLS = Path("/home/andy/codex-private-skills-shared-main/skills")
VALIDATOR = SKILLS / "ur10e-tp-package-delivery/scripts/validate_ur_tp_package.py"
TP_ACCESS = SKILLS / "ur10e-controller-access/scripts/ur10e_controller_ssh.py"
LOCK_PATH = Path("/tmp/tase-arc-autotune-live-writer.lock")


def _dashboard(host: str) -> dict[str, str]:
    return dashboard_exchange(
        host,
        ["is in remote control", "safetymode", "running", "programState", "robotmode", "get loaded program"],
    )


def _require_idle_dashboard(host: str) -> dict[str, str]:
    row = _dashboard(host)
    required = {
        "is in remote control": "true",
        "safetymode": "Safetymode: NORMAL",
        "running": "Program running: false",
        "robotmode": "Robotmode: RUNNING",
    }
    bad = {key: row.get(key) for key, value in required.items() if row.get(key) != value}
    if bad:
        raise RuntimeError(f"arc attempt admission failed: {bad}")
    return row


def _home_error(row: dict, home: np.ndarray) -> tuple[float, float]:
    import pinocchio as pin
    from step5c_calibrated_kinematics_audit import rotvec_to_matrix

    pose = np.asarray(row["actual_TCP_pose"], dtype=float)
    pos = float(np.linalg.norm(pose[:3] - home[:3]))
    angle = float(np.linalg.norm(pin.log3(rotvec_to_matrix(pose[3:]) @ rotvec_to_matrix(home[3:]).T)))
    return pos, angle


def _fresh_home_gate(host: str, home: np.ndarray) -> tuple[Observer, dict, dict]:
    observer = Observer(host)
    observer.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        row = observer.latest()
        if stationary(row):
            pos, angle = _home_error(row, home)
            if pos <= 0.001 and angle <= 0.005:
                return observer, row, {"position_error_m": pos, "orientation_error_rad": angle}
        time.sleep(0.05)
    observer.close()
    row = observer.latest() if observer.rows else {}
    pos, angle = _home_error(row, home) if row else (float("inf"), float("inf"))
    raise RuntimeError(f"approved Home gate failed: position={pos:g} angle={angle:g}")


def _local_validate(package: Path, basename: str, stamp: str, output: Path) -> dict:
    command = [
        "/usr/bin/python3",
        str(VALIDATOR),
        "--package-dir",
        str(package),
        "--basename",
        basename,
        "--controller-dir",
        CONTROLLER_DIR,
        "--expected-stamp",
        stamp,
        "--expected-installation",
        "/programs/default.installation",
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
    result = json.loads(completed.stdout)
    (output / "local-validation.json").write_text(completed.stdout)
    if result.get("pass") is not True:
        raise RuntimeError(f"local TP validation failed: {result}")
    return {"command": command, "result": result}


def _deploy_readback(package: Path, basename: str, output: Path, stamp: str) -> dict:
    deploy_dir = output / "controller-deploy-readback"
    deploy_dir.mkdir()
    command = [
        "/usr/bin/python3",
        str(TP_ACCESS),
        "deploy-readback-triplet",
        "--local-directory",
        str(package),
        "--basename",
        basename,
        "--controller-directory",
        CONTROLLER_DIR,
        "--readback-directory",
        str(deploy_dir),
        "--confirm-deploy",
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    (output / "deploy-readback.json").write_text(
        json.dumps({"command": command, "stdout": completed.stdout, "stderr": completed.stderr}, indent=2) + "\n"
    )
    validation_command = [
        "/usr/bin/python3",
        str(VALIDATOR),
        "--package-dir",
        str(package),
        "--compare-dir",
        str(deploy_dir),
        "--basename",
        basename,
        "--controller-dir",
        CONTROLLER_DIR,
        "--expected-stamp",
        stamp,
        "--expected-installation",
        "/programs/default.installation",
    ]
    check = subprocess.run(validation_command, check=True, capture_output=True, text=True, timeout=20)
    value = json.loads(check.stdout)
    (output / "controller-readback-validation.json").write_text(check.stdout)
    if value.get("pass") is not True or value.get("state") != "controller read-back verified":
        raise RuntimeError(f"controller read-back validation failed: {value}")
    return {
        "deploy_command": command,
        "validation_command": validation_command,
        "readback_dir": str(deploy_dir),
        "validation": value,
    }


def _stop_bridge(process: subprocess.Popen, reason: str) -> dict:
    event = {"reason": reason, "pid": process.pid, "signal": None}
    if process.poll() is not None:
        event["returncode"] = process.returncode
        return event
    process.send_signal(signal.SIGINT)
    event["signal"] = "SIGINT"
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        event["signal"] = "SIGTERM"
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            event["signal"] = "SIGKILL"
            process.wait(timeout=2)
    event["returncode"] = process.returncode
    return event


def _run_segmented_home(host: str, output: Path) -> dict:
    command = [
        os.environ.get("PYTHON", ".venv-contact-six/bin/python"),
        "-B",
        "tools/run_segmented_home_recovery.py",
        "--output",
        str(output),
        "--host",
        host,
        "--execute",
    ]
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONPATH"] = f"{EXPERIMENT_ROOT / 'tools'}:/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages"
    env["AMENT_PREFIX_PATH"] = "/opt/ros/humble"
    completed = subprocess.run(command, cwd=EXPERIMENT_ROOT, env=env, capture_output=True, text=True, timeout=240)
    (output / "segmented-home-stdout.txt").write_text(completed.stdout)
    (output / "segmented-home-stderr.txt").write_text(completed.stderr)
    summary = output / "summary.json"
    return json.loads(summary.read_text()) if summary.exists() else {"success": False, "returncode": completed.returncode}


def run(args: argparse.Namespace) -> dict:
    output = Path(args.output).resolve()
    if output.exists():
        raise RuntimeError(f"attempt output already exists: {output}")
    output.mkdir(parents=True)
    binding = json.loads((Path(args.package_dir) / f"{args.basename}.binding.json").read_text())
    stamp = str(binding["stamp"])
    result: dict = {
        "schema": "tase_arc_autotune_attempt_v1",
        "attempt_id": output.name,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "candidate": binding.get("candidate"),
        "law_identity": binding.get("law_identity"),
        "path_identity": binding.get("path_identity"),
        "package": {"dir": str(Path(args.package_dir).resolve()), "basename": args.basename, "stamp": stamp},
        "live": bool(args.execute),
        "success": False,
        "failure": None,
        "recovery": None,
    }
    lock_handle = LOCK_PATH.open("a+")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    observer = None
    video = None
    bridge = None
    writer = None
    adapter = None
    home = np.asarray(load_identity_contract().home_pose, dtype=float)
    try:
        dashboard_before = _require_idle_dashboard(args.host)
        result["dashboard_before"] = dashboard_before
        observer, home_row, home_gate = _fresh_home_gate(args.host, home)
        result["home_gate_before"] = home_gate
        local = _local_validate(Path(args.package_dir), args.basename, stamp, output)
        result["local_validation"] = local
        if not args.execute:
            result["state"] = "read_only_preflight"
            return result
        readback = _deploy_readback(Path(args.package_dir), args.basename, output, stamp)
        result["controller_readback"] = readback
        video = VideoRecorder(args.video_url, output)
        video.start()
        bridge_output = output / "bridge"
        bridge_output.mkdir()
        bridge_command = [
            os.environ.get("PYTHON", ".venv-contact-six/bin/python"),
            "-B",
            "tools/kunwei_rtde_bridge.py",
            "--allow-kunwei-stream-command",
            "--write-rtde-inputs",
            "--baseline-s",
            "5",
            "--rezero-s",
            "1",
            "--duration-s",
            str(args.bridge_duration_s),
            "--rtde-hz",
            "500",
            "--socket-timeout-s",
            "0.0",
            "--sensor-stale-s",
            "0.10",
            "--target-force-n",
            "5",
            "--normal-axis",
            "fz",
            "--normal-sign",
            "1",
            "--max-normal-force-n",
            "20",
            "--max-force-norm-n",
            "50",
            "--max-torque-norm-nm",
            "0.6",
            "--output-dir",
            str(bridge_output),
        ]
        env = os.environ.copy()
        env.pop("VIRTUAL_ENV", None)
        env.pop("PYTHONHOME", None)
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONPATH"] = f"{EXPERIMENT_ROOT / 'tools'}:/opt/ros/humble/lib/python3.10/site-packages:/opt/ros/humble/local/lib/python3.10/dist-packages"
        env["AMENT_PREFIX_PATH"] = "/opt/ros/humble"
        bridge_stdout = (output / "bridge-stdout.txt").open("w")
        bridge_stderr = (output / "bridge-stderr.txt").open("w")
        bridge = subprocess.Popen(bridge_command, cwd=EXPERIMENT_ROOT, env=env, stdout=bridge_stdout, stderr=bridge_stderr)
        result["bridge_command"] = bridge_command
        result["bridge_pid"] = bridge.pid
        time.sleep(1.0)
        if bridge.poll() is not None:
            raise RuntimeError(f"bridge exited before Dashboard Play: rc={bridge.returncode}")
        target = f"{CONTROLLER_DIR}/{args.basename}.urp"
        writer = RemoteDashboardWriter(args.host, load_target=target)
        adapter = _ExactLoadAdapter(
            host=args.host,
            target=target,
            program_id=args.basename,
            dashboard_observer=dashboard_exchange,
            writer=writer,
            dashboard_port=29999,
            dashboard_timeout_s=2.0,
            observe_timeout_s=8.0,
            poll_interval_s=0.05,
            monotonic=time.monotonic,
            sleeper=time.sleep,
        )
        result["load"] = adapter.load()
        result["play"] = adapter.play()
        dashboard_events = []
        saw_running = False
        safety_fault = None
        deadline = time.monotonic() + float(args.bridge_duration_s) + 30.0
        while bridge.poll() is None and time.monotonic() < deadline:
            state = _dashboard(args.host)
            dashboard_events.append({"at": time.monotonic(), **state})
            running = state.get("running") == "Program running: true"
            saw_running = saw_running or running
            if state.get("safetymode") != "Safetymode: NORMAL":
                safety_fault = {"reason": "safety_not_normal", "state": state}
                break
            if saw_running and state.get("running") == "Program running: false":
                break
            if video is not None:
                video.check()
            time.sleep(0.25)
        result["dashboard_events"] = dashboard_events[-100:]
        result["safety_fault"] = safety_fault
        result["bridge_stop"] = _stop_bridge(bridge, "TP stopped" if saw_running else "attempt owner cleanup")
        if adapter.play_issued and _dashboard(args.host).get("running") == "Program running: true":
            result["dashboard_stop"] = writer.write("stop").response
        # Wait for a stopped Dashboard before closing the observer and invoking
        # any recovery route.
        stop_deadline = time.monotonic() + 8.0
        last_state = None
        while time.monotonic() < stop_deadline:
            last_state = _dashboard(args.host)
            if last_state.get("running") == "Program running: false":
                break
            time.sleep(0.1)
        result["dashboard_after"] = last_state
        result["bridge_returncode"] = bridge.returncode
        bridge_csv = bridge_output / "bridge_rtde_500hz.csv"
        if bridge_csv.exists():
            analysis_path = output / "arc-analysis.json"
            analysis_cmd = [
                os.environ.get("PYTHON", ".venv-contact-six/bin/python"),
                "-B",
                "tools/analyze_step2d_circle_run.py",
                str(bridge_csv),
                "--target-force-n",
                "5",
                "--output",
                str(analysis_path),
            ]
            analyzed = subprocess.run(analysis_cmd, cwd=EXPERIMENT_ROOT, env=env, capture_output=True, text=True)
            result["analysis_command"] = analysis_cmd
            result["analysis_returncode"] = analyzed.returncode
            if analysis_path.exists():
                result["analysis"] = json.loads(analysis_path.read_text())
        # Refresh the Home predicate.  A normal full circle should have TP
        # auto-homed; otherwise the same segmented owner repairs it.
        if observer is not None:
            final_row = observer.latest()
            final_pos, final_angle = _home_error(final_row, home)
            result["home_gate_after"] = {
                "position_error_m": final_pos,
                "orientation_error_rad": final_angle,
                "stationary": stationary(final_row),
            }
        else:
            final_pos, final_angle = float("inf"), float("inf")
        result["success"] = bool(
            safety_fault is None
            and result.get("analysis", {}).get("coverage", {}).get("class") == "full_path"
            and final_pos <= 0.001
            and final_angle <= 0.005
            and result.get("dashboard_after", {}).get("running") == "Program running: false"
        )
        if not result["success"] and safety_fault is None:
            # Release the owner lock before the independent segmented Home
            # owner takes its lock/commands. The original attempt remains
            # failed if it did not complete a full arc.
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            recovery_dir = output / "segmented-home-recovery"
            result["recovery"] = _run_segmented_home(args.host, recovery_dir)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            result["success"] = bool(result["recovery"].get("success")) and bool(
                result.get("analysis", {}).get("coverage", {}).get("class") == "full_path"
            )
        result["state"] = "SEALED" if result["success"] else "FAILED_ATTEMPT_HOME_CHECKED"
    except Exception as exc:
        result["failure"] = f"{type(exc).__name__}: {exc}"
        result["state"] = "FAILED_ATTEMPT"
        if bridge is not None:
            result["bridge_stop"] = _stop_bridge(bridge, "exception cleanup")
        if adapter is not None and adapter.play_issued:
            try:
                result["dashboard_stop"] = writer.write("stop").response
            except Exception as stop_exc:
                result["dashboard_stop_error"] = f"{type(stop_exc).__name__}: {stop_exc}"
    finally:
        if video is not None:
            video.close()
        if observer is not None:
            observer.close()
        try:
            bridge_stdout.close()  # type: ignore[name-defined]
            bridge_stderr.close()  # type: ignore[name-defined]
        except Exception:
            pass
        result["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (output / "attempt.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--basename", default="step4d_circle_autotune_v1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", default=HOST_DEFAULT)
    parser.add_argument("--video-url", default="rtsp://127.0.0.1:8554/arm")
    parser.add_argument("--bridge-duration-s", type=float, default=180.0)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        value = run(args)
    except Exception as exc:
        value = {"success": False, "state": "BLOCKED", "failure": f"{type(exc).__name__}: {exc}"}
        Path(args.output).mkdir(parents=True, exist_ok=True)
        (Path(args.output) / "attempt.json").write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps(value, indent=2, default=str))
    return 0 if value.get("success") or value.get("state") == "read_only_preflight" else 1


if __name__ == "__main__":
    raise SystemExit(main())
