#!/usr/bin/env python3
"""Execute the vertical-only recovery relief and leave the robot at clearance.

This is deliberately a small recovery owner used when a terminal contact
attempt stops below the approved Home height.  It does not start force
control, perform a lateral transfer, or reinterpret the failed attempt.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from build_contact_benchmark_triplet import CONTROLLER_DIR
from contact_home_recovery_policy import ReliefForceGuard, validate_lift_sample
from contact_home_motion_profile import HOME_JOINT_SPEED_GUARD_RAD_S
from contact_yield_math import so3_exp
from run_contact_home import INSTALLED_LOCK, Observer, validate_robot_sample
from run_contact_recovery import stationary
from step5d_autotune_v4_r004.transport import LiveR004KunweiTransport
from step5d_autotune_v4_r014.dispatcher import WriterLock
from step5d_remote_startup import (
    RemoteDashboardWriter,
    _ExactLoadAdapter,
    dashboard_exchange,
)


PROGRAM = "step5d_contact_relief_v1"
TARGET = f"{CONTROLLER_DIR}/{PROGRAM}.urp"
HOME_POSE = np.array(
    [0.4620551816, 0.1778825964, 0.03408876139925415, 3.120752062, 0.0, 0.068626833],
    dtype=float,
)
RELIEF_SPEED_LIMIT_M_S = 0.050


def _dashboard(host: str) -> dict[str, str]:
    return dashboard_exchange(
        host,
        ["is in remote control", "safetymode", "robotmode", "running", "programState", "get loaded program"],
    )


def run(args: argparse.Namespace) -> dict:
    package = Path(args.package_dir)
    readback = Path(args.readback_dir)
    manifest = json.loads(Path(args.validation).read_text(encoding="utf-8"))
    if manifest.get("status") != "controller read-back verified":
        raise RuntimeError("fresh relief read-back manifest required")
    for ext in ("script", "txt", "urp"):
        if (package / f"{PROGRAM}.{ext}").read_bytes() != (readback / f"{PROGRAM}.{ext}").read_bytes():
            raise RuntimeError(f"relief read-back differs for .{ext}")

    before = _dashboard(args.host)
    expected = {
        "is in remote control": "true",
        "safetymode": "Safetymode: NORMAL",
        "robotmode": "Robotmode: RUNNING",
        "running": "Program running: false",
    }
    if any(before.get(k) != v for k, v in expected.items()):
        raise RuntimeError(f"relief preflight failed: {before}")

    baseline = json.loads(Path(args.source_run, "software_baseline_receipt.json").read_text(encoding="utf-8"))
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    result: dict = {
        "program": PROGRAM,
        "source_run": str(args.source_run),
        "target_pose": HOME_POSE.tolist(),
        "success": False,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dashboard_before": before,
        "motion": False,
    }
    observer = Observer(args.host)
    sensor = LiveR004KunweiTransport("192.168.50.25", port=5152)
    writer = RemoteDashboardWriter(args.host, load_target=TARGET)
    adapter = _ExactLoadAdapter(
        host=args.host,
        target=TARGET,
        program_id=PROGRAM,
        dashboard_observer=dashboard_exchange,
        writer=writer,
        dashboard_port=29999,
        dashboard_timeout_s=2.0,
        observe_timeout_s=5.0,
        poll_interval_s=0.05,
        monotonic=time.monotonic,
        sleeper=time.sleep,
    )
    rows: list[dict] = []
    wrench_rows: list[dict] = []
    guard = None
    try:
        with WriterLock(INSTALLED_LOCK):
            observer.start()
            deadline = time.monotonic() + 3.0
            while len(observer.rows) < 10 and time.monotonic() < deadline:
                time.sleep(0.02)
            if len(observer.rows) < 10:
                raise RuntimeError("fresh RTDE observer barrier did not complete")
            start = observer.latest()
            validate_robot_sample(start)
            if np.linalg.norm(start["actual_TCP_speed"]) > 0.005 or max(abs(x) for x in start["actual_qd"]) > 0.010:
                raise RuntimeError("relief start speed exceeds bounded pre-stop envelope")
            if np.linalg.norm(np.asarray(start["actual_TCP_pose"])[:2] - HOME_POSE[:2]) > 0.080:
                raise RuntimeError("relief start is outside lateral recovery envelope")
            if float(start["actual_TCP_pose"][2]) > float(HOME_POSE[2]) + 0.001 or float(start["actual_TCP_pose"][2]) < 0.018:
                raise RuntimeError("relief start Z is outside recovery envelope")
            sensor.open()
            raw = received = None
            sensor_deadline = time.monotonic() + 2.0
            while time.monotonic() < sensor_deadline:
                raw, received = sensor.poll()
                if raw is not None and received is not None:
                    break
                time.sleep(0.01)
            if raw is None or received is None:
                raise RuntimeError("fresh Kunwei frame missing at relief entry")
            rotation = so3_exp(np.asarray(start["actual_TCP_pose"][3:], dtype=float))
            guard = ReliefForceGuard(
                initial_raw_wrench=raw,
                no_load_wrench=baseline["mean_wrench_n_nm"],
                baseline_std_wrench=baseline["std_wrench_n_nm"],
                rotation=rotation,
            )
            plan = {
                "start_pose": list(start["actual_TCP_pose"]),
                "lift_pose": [*list(start["actual_TCP_pose"][:2]), float(HOME_POSE[2]), *list(start["actual_TCP_pose"][3:])],
            }
            result["initial_sample"] = start
            result["plan"] = plan
            result["load"] = adapter.load()
            result["play"] = adapter.play()
            result["motion"] = True
            deadline = time.monotonic() + 60.0
            startup_grace_until = time.monotonic() + 0.25
            startup_transients: list[str] = []
            clearance_since = None
            while time.monotonic() < deadline:
                row = observer.latest()
                validate_robot_sample(row)
                rows.append(row)
                raw, received = sensor.poll()
                if raw is None or received is None or not 0 <= time.monotonic() - received < 0.08:
                    raise RuntimeError("Kunwei relief observation stale")
                force = guard.update(raw, now_s=received)
                wrench_rows.append({"received_monotonic_s": received, "wrench_n_nm": raw, "force_check": force})
                try:
                    validate_lift_sample(plan, row["actual_TCP_pose"], row["actual_TCP_speed"], speed_limit_m_s=RELIEF_SPEED_LIMIT_M_S)
                except ValueError as exc:
                    # UR can expose one short downward TCP-speed sample while
                    # a vertical relief program enters RUNNING.  Keep this
                    # narrow, time-bounded retry allowance; every other
                    # geometry, speed, force, or safety failure is terminal.
                    if str(exc) != "lift downward velocity" or time.monotonic() > startup_grace_until:
                        raise
                    startup_transients.append(str(exc))
                state = _dashboard(args.host)
                if state.get("safetymode") != "Safetymode: NORMAL":
                    raise RuntimeError(f"relief safety changed: {state}")
                at_clearance = float(row["actual_TCP_pose"][2]) >= float(HOME_POSE[2]) - 0.0001
                if at_clearance and stationary(row):
                    clearance_since = clearance_since or time.monotonic()
                    if time.monotonic() - clearance_since >= 0.30 and state.get("running") == "Program running: false":
                        if not force.get("released"):
                            raise RuntimeError("clearance reached but force guard did not release")
                        result.update(
                            success=True,
                            clearance_sample=row,
                            clearance_force=force,
                            dashboard_after=state,
                            released=True,
                            startup_transients=startup_transients,
                        )
                        break
                else:
                    clearance_since = None
                time.sleep(0.01)
            else:
                raise RuntimeError("vertical relief timed out")
    except BaseException as exc:
        result["failure"] = f"{type(exc).__name__}: {exc}"
        if adapter.play_issued:
            try:
                result["compensating_stop"] = adapter.stop()
            except Exception as stop_exc:
                result["stop_failure"] = f"{type(stop_exc).__name__}: {stop_exc}"
    finally:
        try:
            if sensor is not None:
                sensor.close()
        except Exception:
            pass
        try:
            observer.close()
        except Exception:
            pass
        result["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (out / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        (out / "rtde.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in observer.rows), encoding="utf-8")
        (out / "wrench.jsonl").write_text("".join(json.dumps(r, default=str) + "\n" for r in wrench_rows), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.1.18")
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--readback-dir", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
