#!/usr/bin/env python3
"""Run and verify the historical Figure-eight joint Home package."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path

import numpy as np
import pinocchio as pin

from build_joint_home import HOME_POSE, HOME_Q, PROGRAM, CONTROLLER_DIR
from run_contact_home import INSTALLED_LOCK, Observer, validate_robot_sample
from step5d_remote_startup import RemoteDashboardWriter, _ExactLoadAdapter, dashboard_exchange
from step5d_autotune_v4_r014.dispatcher import WriterLock
from step5c_calibrated_kinematics_audit import rotvec_to_matrix


TARGET = f"{CONTROLLER_DIR}/{PROGRAM}.urp"


def _errors(pose: list[float] | np.ndarray, q: list[float] | np.ndarray) -> tuple[float, float, float]:
    actual_pose = np.asarray(pose, dtype=float)
    actual_q = np.asarray(q, dtype=float)
    target_pose = np.asarray(HOME_POSE, dtype=float)
    target_q = np.asarray(HOME_Q, dtype=float)
    position = float(np.linalg.norm(actual_pose[:3] - target_pose[:3]))
    orientation = float(np.linalg.norm(pin.log3(rotvec_to_matrix(actual_pose[3:]) @ rotvec_to_matrix(target_pose[3:]).T)))
    joints = float(np.max(np.abs(actual_q - target_q)))
    return position, orientation, joints


def run(args: argparse.Namespace) -> dict:
    validation = json.loads(args.validation.read_text(encoding="utf-8"))
    if validation.get("status") != "controller read-back verified":
        raise RuntimeError("fresh controller read-back manifest required")
    for suffix in ("script", "txt", "urp"):
        if (args.package_dir / f"{PROGRAM}.{suffix}").read_bytes() != (args.readback_dir / f"{PROGRAM}.{suffix}").read_bytes():
            raise RuntimeError(f"controller read-back differs for .{suffix}")
    dashboard = dashboard_exchange(args.host, ["is in remote control", "safetymode", "robotmode", "running", "programState", "get loaded program"])
    if dashboard.get("is in remote control") != "true" or dashboard.get("safetymode") != "Safetymode: NORMAL" or dashboard.get("robotmode") != "Robotmode: RUNNING" or dashboard.get("running") != "Program running: false":
        raise RuntimeError(f"joint Home preflight failed: {dashboard}")
    observer = Observer(args.host)
    writer = RemoteDashboardWriter(args.host, load_target=TARGET)
    adapter = _ExactLoadAdapter(host=args.host, target=TARGET, program_id=PROGRAM, dashboard_observer=dashboard_exchange, writer=writer, dashboard_port=29999, dashboard_timeout_s=2.0, observe_timeout_s=5.0, poll_interval_s=0.05, monotonic=time.monotonic, sleeper=time.sleep)
    result: dict = {"program": PROGRAM, "home_q": HOME_Q, "home_pose": HOME_POSE, "success": False, "motion": False, "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    stationary_since: float | None = None
    try:
        with WriterLock(INSTALLED_LOCK):
            observer.start()
            deadline = time.monotonic() + 3.0
            while len(observer.rows) < 10 and time.monotonic() < deadline:
                time.sleep(0.02)
            if len(observer.rows) < 10:
                raise RuntimeError("fresh RTDE observer barrier did not complete")
            for row in observer.rows[-10:]:
                validate_robot_sample(row)
                if np.linalg.norm(row["actual_TCP_speed"]) > 0.005 or max(abs(x) for x in row["actual_qd"]) > 0.010:
                    raise RuntimeError("robot residual speed exceeded the joint Home pre-stop envelope")
            result["initial_pose"] = observer.rows[-1]["actual_TCP_pose"]
            result["initial_q"] = observer.rows[-1]["actual_q"]
            result["initial_errors"] = dict(zip(("position_m", "orientation_rad", "joint_max_rad"), _errors(result["initial_pose"], result["initial_q"])))
            result["load"] = adapter.load()
            result["play"] = adapter.play()
            result["motion"] = True
            deadline = time.monotonic() + 70.0
            while time.monotonic() < deadline:
                row = observer.latest()
                validate_robot_sample(row)
                state = dashboard_exchange(args.host, ["is in remote control", "safetymode", "robotmode", "running", "programState", "get loaded program"])
                if state.get("safetymode") != "Safetymode: NORMAL" or state.get("is in remote control") != "true":
                    raise RuntimeError(f"joint Home safety/mode changed: {state}")
                if state.get("get loaded program") != f"Loaded program: {TARGET}":
                    raise RuntimeError("joint Home loaded program changed")
                position_error, orientation_error, joint_error = _errors(row["actual_TCP_pose"], row["actual_q"])
                stopped = state.get("running") == "Program running: false" and str(state.get("programState", "")).startswith("STOPPED")
                if stopped and position_error < 0.001 and orientation_error < 0.005 and joint_error < 0.02 and np.linalg.norm(row["actual_TCP_speed"]) < 0.0005 and max(abs(x) for x in row["actual_qd"]) < 0.001:
                    if stationary_since is None:
                        stationary_since = time.monotonic()
                    if time.monotonic() - stationary_since >= 0.5:
                        result.update(success=True, final_pose=row["actual_TCP_pose"], final_q=row["actual_q"], final_position_error_m=position_error, final_orientation_error_rad=orientation_error, final_joint_max_error_rad=joint_error, final_sample=row, dashboard_after=state)
                        break
                else:
                    stationary_since = None
                time.sleep(0.04)
            else:
                raise RuntimeError("joint Home did not reach verified Home within 70 s")
    except BaseException as exc:
        result["failure"] = f"{type(exc).__name__}: {exc}"
        if adapter.play_issued:
            try:
                result["compensating_stop"] = adapter.stop()
            except Exception as stop_exc:
                result["stop_failure"] = f"{type(stop_exc).__name__}: {stop_exc}"
    finally:
        if getattr(observer, "thread", None) is not None:
            observer.close()
        result["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        (args.output / "rtde.jsonl").write_text("".join(json.dumps(row, default=str) + "\n" for row in observer.rows), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.1.18")
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--readback-dir", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as exc:
        result = {"success": False, "motion": False, "failure": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
