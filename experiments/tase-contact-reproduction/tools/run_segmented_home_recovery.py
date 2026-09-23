#!/usr/bin/env python3
"""Return a stopped robot to the approved Home in bounded fresh segments.

The installed Home helper deliberately rejects a lateral transfer above 3 mm.
This owner keeps that bound: after each completed segment it takes a fresh
stationary RTDE sample, rebuilds the Home triplet for the next target, uploads
and reads it back, then runs exactly one monitored segment.  No segment is
allowed to turn into an unbounded direct move.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from build_contact_home import BASENAME, build as build_home
from build_contact_benchmark_triplet import CONTROLLER_DIR
from contact_yield_live_contract import load_identity_contract
from contact_yield_math import so3_exp, so3_log
from contact_home_recovery_policy import MAX_RISE_M
from run_contact_home import Observer, run as run_home
from step5d_remote_startup import dashboard_exchange


HOST_DEFAULT = "192.168.1.18"
VIDEO_DEFAULT = "rtsp://127.0.0.1:8554/arm"
OWNER = Path("/home/andy/codex-private-skills-shared-main/skills")
TP_ACCESS = OWNER / "ur10e-controller-access/scripts/ur10e_controller_ssh.py"
ROOT = Path(__file__).resolve().parents[1]
PRESERVED_HOME = ROOT / "report/contact-six-qp-20260917/preserved-home.json"

# Leave margin below the package's hard 3 mm / 20 mrad corridor.  The target
# is recomputed from the fresh post-segment pose, so this is a per-segment
# bound rather than a claim about the whole transfer.
MAX_SEGMENT_LATERAL_M = 0.0020
MAX_SEGMENT_ANGLE_RAD = 0.012
MAX_SEGMENTS = 40
STATIONARY_SPEED_M_S = 0.0005
STATIONARY_QD_RAD_S = 0.001
CLEARANCE_FLOOR_Z_M = 0.033
CLEARANCE_TOLERANCE_M = 0.0001


def _stationary(row: dict) -> bool:
    return (
        float(np.linalg.norm(np.asarray(row["actual_TCP_speed"], dtype=float)))
        < STATIONARY_SPEED_M_S
        and float(np.max(np.abs(np.asarray(row["actual_qd"], dtype=float))))
        < STATIONARY_QD_RAD_S
    )


def _dashboard(host: str) -> dict:
    state = dashboard_exchange(
        host, ["is in remote control", "safetymode", "running", "programState", "robotmode"]
    )
    expected = {
        "is in remote control": "true",
        "safetymode": "Safetymode: NORMAL",
        "running": "Program running: false",
        "robotmode": "Robotmode: RUNNING",
    }
    bad = {k: state.get(k) for k, v in expected.items() if state.get(k) != v}
    if bad:
        raise RuntimeError(f"segmented Home requires stopped Remote/NORMAL robot: {bad}")
    return state


def _fresh_stationary(host: str) -> tuple[Observer, dict]:
    observer = Observer(host)
    observer.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        row = observer.latest()
        if _stationary(row):
            # Package binding uses the stricter historical stationary proof;
            # the looser runtime gate alone can capture a small post-stop TCP
            # velocity (and make transform() reject an otherwise safe segment).
            time.sleep(0.05)
            row = observer.latest()
            strict_speed = float(np.linalg.norm(np.asarray(row["actual_TCP_speed"], dtype=float))) < 1e-5
            strict_qd = float(np.max(np.abs(np.asarray(row["actual_qd"], dtype=float)))) < 1e-4
            if _stationary(row) and strict_speed and strict_qd:
                return observer, row
    observer.close()
    raise RuntimeError("fresh stationary RTDE sample unavailable")


def _target_between(start: np.ndarray, target: np.ndarray, fraction: float) -> np.ndarray:
    start_rot = so3_exp(start[3:])
    turn = so3_log(so3_exp(target[3:]) @ start_rot.T)
    rot = so3_exp(float(fraction) * turn) @ start_rot
    # Pinocchio's log3 is already used by the Home tools; avoid importing the
    # larger runtime here by using the stable library implementation.
    import pinocchio as pin

    out = start.copy()
    out[:3] = start[:3] + float(fraction) * (target[:3] - start[:3])
    out[3:] = pin.log3(rot)
    return out


def _segment_count(start: np.ndarray, target: np.ndarray) -> int:
    lateral = float(np.linalg.norm(start[:2] - target[:2]))
    angle = float(np.linalg.norm(so3_log(so3_exp(target[3:]) @ so3_exp(start[3:]).T)))
    count = max(
        1,
        int(math.ceil(lateral / MAX_SEGMENT_LATERAL_M)),
        int(math.ceil(angle / MAX_SEGMENT_ANGLE_RAD)),
    )
    if count > MAX_SEGMENTS:
        raise RuntimeError(f"segmented Home needs {count} segments, exceeds bound {MAX_SEGMENTS}")
    return count


def _next_clearance_target_z(start_z: float) -> float | None:
    """Return one vertical-only clearance step within the existing rise bound."""
    start = float(start_z)
    if not math.isfinite(start):
        raise ValueError("vertical clearance start Z is nonfinite")
    remaining = CLEARANCE_FLOOR_Z_M - start
    if remaining <= CLEARANCE_TOLERANCE_M:
        return None
    if remaining <= MAX_RISE_M:
        return CLEARANCE_FLOOR_Z_M
    step_target = start + MAX_RISE_M
    if step_target - start > MAX_RISE_M:
        step_target = math.nextafter(step_target, start)
    return step_target


def _home_receipt(start: dict, target: np.ndarray, final_target: np.ndarray, q: list[float], index: int) -> dict:
    preserved = json.loads(PRESERVED_HOME.read_text())
    preserved.update(
        {
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "user_home_confirmed": True,
            "user_instruction": "approved Figure-eight Home; segmented clearance transfer",
            "rtde": start,
            "home_pose": target.tolist(),
            "home_q": q,
            "original_home_pose": list(final_target),
            "final_home_pose": final_target.tolist(),
            "clearance_entry": True,
            "bounded_recovery": True,
            "bounded_withdrawal": False,
            "segmented_recovery": True,
            "recovery_route": "segmented_bounded_home_v2",
            "segment_index": index,
            "actual_motion_performed": False,
        }
    )
    return preserved


def _geometry_q(start: dict, target: np.ndarray) -> list[float]:
    """Run the existing calibrated sampled IK/speed proof for one segment."""
    from run_contact_recovery import check_geometry

    s = np.asarray(start["actual_TCP_pose"], dtype=float)
    plan = {
        "start_pose": s.tolist(),
        "lift_pose": [s[0], s[1], max(float(s[2]), float(target[2])), s[3], s[4], s[5]],
        "home_pose": target.tolist(),
    }
    proof = check_geometry(start, plan)
    if proof.get("pass") is not True:
        raise RuntimeError(f"calibrated Home geometry failed: {proof}")
    return proof["home_q"]


def _deploy_and_readback(package_dir: Path, readback_root: Path, stamp: str) -> dict:
    readback_root.mkdir(parents=True, exist_ok=False)
    deploy_cmd = [
        "/usr/bin/python3",
        str(TP_ACCESS),
        "deploy-readback-triplet",
        "--local-directory",
        str(package_dir),
        "--basename",
        BASENAME,
        "--controller-directory",
        CONTROLLER_DIR,
        "--readback-directory",
        str(readback_root / "deploy"),
        "--confirm-deploy",
    ]
    proc = subprocess.run(deploy_cmd, check=True, capture_output=True, text=True, timeout=60)
    (readback_root / "deploy-readback.json").write_text(
        json.dumps({"command": deploy_cmd, "stdout": proc.stdout, "stderr": proc.stderr}, indent=2) + "\n"
    )
    # This is a fresh GET plus the same validator used by the recovery owner.
    from contact_recovery_readback import fetch_recovery_readback

    proof = fetch_recovery_readback(readback_root / "proof", package_dir, basenames=(BASENAME,))
    validation = proof / f"{BASENAME}-validation.json"
    value = json.loads(validation.read_text())
    if value.get("pass") is not True or value.get("state") != "controller read-back verified":
        raise RuntimeError(f"fresh Home package read-back failed: {value}")
    return {
        "deploy_command": deploy_cmd,
        "deploy_stdout": proc.stdout,
        "proof": str(proof),
        "validation": str(validation),
        "readback_dir": str(proof / "readback" / BASENAME),
        "stamp": stamp,
    }


def _run_vertical_clearance_withdrawal(
    *, host: str, output: Path, fresh: dict, final_target: np.ndarray, index: int,
    video_url: str = VIDEO_DEFAULT, video_policy: str = "required"
) -> dict:
    """Raise a low stopped pose to the existing 33 mm Home clearance floor.

    This is the old bounded-withdrawal package, kept separate from the later
    clearance-entry segmented transfer.  It preserves XY and orientation and
    therefore does not turn a low-contact pose into a lateral move while the
    tool is still below the floor.
    """
    from build_contact_home import build as build_contact_home

    start_pose = np.asarray(fresh["actual_TCP_pose"], dtype=float)
    target = start_pose.copy()
    next_z = _next_clearance_target_z(float(start_pose[2]))
    if next_z is None:
        return {"skipped": True, "reason": "already_at_clearance_floor", "start_pose": start_pose.tolist()}
    target[2] = next_z
    rise = float(target[2] - start_pose[2])
    if rise > MAX_RISE_M + 1e-12:
        raise RuntimeError(f"vertical clearance rise {rise:g}m exceeds 15mm bound")
    from run_contact_recovery import check_geometry

    plan = {
        "start_pose": start_pose.tolist(),
        "lift_pose": target.tolist(),
        "home_pose": target.tolist(),
    }
    geometry = check_geometry(fresh, plan)
    segment = output / f"vertical-clearance-{index:02d}"
    package = segment / "package"
    package.mkdir(parents=True)
    preserved = json.loads(PRESERVED_HOME.read_text())
    preserved.update(
        {
            "observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "user_home_confirmed": True,
            "user_instruction": "approved Figure-eight Home; vertical clearance withdrawal",
            "rtde": fresh,
            "home_pose": target.tolist(),
            "home_q": geometry["home_q"],
            "original_home_pose": final_target.tolist(),
            "final_home_pose": final_target.tolist(),
            "clearance_entry": False,
            "bounded_recovery": False,
            "bounded_withdrawal": True,
            "segmented_recovery": True,
            "recovery_route": "vertical_clearance_withdrawal_then_segmented_home",
            "segment_index": index,
            "actual_motion_performed": False,
        }
    )
    receipt = segment / "home-receipt.json"
    receipt.write_text(json.dumps(preserved, indent=2) + "\n")
    binding = build_contact_home(receipt, package)
    validation_cmd = [
        "/usr/bin/python3",
        str(OWNER / "ur10e-tp-package-delivery/scripts/validate_ur_tp_package.py"),
        "--package-dir",
        str(package),
        "--basename",
        BASENAME,
        "--controller-dir",
        CONTROLLER_DIR,
        "--expected-stamp",
        binding["stamp"],
        "--expected-installation",
        "/programs/default.installation",
    ]
    local = subprocess.run(validation_cmd, check=True, capture_output=True, text=True, timeout=20)
    (segment / "local-validation.json").write_text(local.stdout)
    if json.loads(local.stdout).get("pass") is not True:
        raise RuntimeError("vertical clearance Home package local validation failed")
    readback = _deploy_and_readback(package, segment / "readback", binding["stamp"])
    home_args = SimpleNamespace(
        host=host,
        home_receipt=receipt,
        validation=Path(readback["validation"]),
        package_dir=package,
        readback_dir=Path(readback["readback_dir"]),
        output=segment / "home-run",
        video_url=video_url,
        video_policy=video_policy,
        execute=True,
    )
    home_result = run_home(home_args)
    record = {
        "index": index,
        "start_pose": start_pose.tolist(),
        "target_pose": target.tolist(),
        "rise_m": rise,
        "geometry": geometry,
        "package": str(package),
        "readback": readback,
        "home_result": home_result,
    }
    (segment / "vertical-clearance.json").write_text(json.dumps(record, indent=2, default=str) + "\n")
    if home_result.get("success") is not True:
        raise RuntimeError(f"vertical clearance withdrawal failed: {home_result}")
    return record


def run(args: argparse.Namespace) -> dict:
    if not PRESERVED_HOME.exists():
        raise RuntimeError(f"preserved Home receipt missing: {PRESERVED_HOME}")
    contract = load_identity_contract()
    final_target = np.asarray(contract.home_pose, dtype=float)
    output = Path(args.output)
    if output.exists():
        raise RuntimeError(f"output already exists: {output}")
    output.mkdir(parents=True)
    _dashboard(args.host)
    observer, row = _fresh_stationary(args.host)
    observer.close()
    start = np.asarray(row["actual_TCP_pose"], dtype=float)
    result = {
        "schema": "segmented-home-recovery-v2",
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "fresh RTDE after direct/staged Home corridor rejection",
        "approved_home": final_target.tolist(),
        "initial_pose": start.tolist(),
        "segment_limits": {
            "lateral_m": MAX_SEGMENT_LATERAL_M,
            "angle_rad": MAX_SEGMENT_ANGLE_RAD,
            "package_corridor_lateral_m": 0.003,
            "package_corridor_angle_rad": 0.020,
        },
        "segments": [],
        "success": False,
    }
    (output / "initial-sample.json").write_text(json.dumps(row, indent=2) + "\n")

    # Replan from each fresh post-segment sample.  A small final residual can
    # never silently accumulate into a target outside the package corridor.
    current = start
    for index in range(1, MAX_SEGMENTS + 1):
        _dashboard(args.host)
        observer, fresh = _fresh_stationary(args.host)
        observer.close()
        current = np.asarray(fresh["actual_TCP_pose"], dtype=float)
        if current[2] < CLEARANCE_FLOOR_Z_M - CLEARANCE_TOLERANCE_M:
            try:
                vertical = _run_vertical_clearance_withdrawal(
                    host=args.host,
                    output=output,
                    fresh=fresh,
                    final_target=final_target,
                    index=index,
                    video_url=getattr(args, "video_url", VIDEO_DEFAULT),
                    video_policy=getattr(args, "video_policy", "required"),
                )
            except Exception as exc:
                result["state"] = "BLOCKED"
                result["failure"] = f"{type(exc).__name__}: {exc}"
                break
            result["vertical_clearance_withdrawal"] = vertical
            result.setdefault("vertical_clearance_withdrawals", []).append(vertical)
            observer2, fresh2 = _fresh_stationary(args.host)
            observer2.close()
            current = np.asarray(fresh2["actual_TCP_pose"], dtype=float)
            fresh = fresh2
            if current[2] < CLEARANCE_FLOOR_Z_M - CLEARANCE_TOLERANCE_M:
                # The next bounded vertical step gets a new RTDE sample,
                # geometry proof, package read-back, and force monitor.
                continue
        lateral = float(np.linalg.norm(current[:2] - final_target[:2]))
        angle = float(np.linalg.norm(so3_log(so3_exp(final_target[3:]) @ so3_exp(current[3:]).T)))
        position_error = float(np.linalg.norm(current[:3] - final_target[:3]))
        if lateral < 0.0005 and angle < 0.003 and position_error < 0.001:
            result["success"] = True
            result["final_pose"] = current.tolist()
            result["final_position_error_m"] = position_error
            result["final_orientation_error_rad"] = angle
            break
        count = _segment_count(current, final_target)
        target = _target_between(current, final_target, 1.0 / count)
        # Keep the lateral/orientation transfer on the already-proved 33 mm
        # clearance plane. Once those residuals are small, admit only a pure
        # vertical final rise; this prevents the Home owner from rotating
        # while it is still below its target clearance height.
        remaining_xy = float(np.linalg.norm(current[:2] - final_target[:2]))
        remaining_angle = float(np.linalg.norm(so3_log(so3_exp(final_target[3:]) @ so3_exp(current[3:]).T)))
        if current[2] < final_target[2] - 0.0002:
            if remaining_xy > 0.0005 or remaining_angle > 0.003:
                target[2] = current[2]
            else:
                target[:3] = current[:3]
                target[3:] = current[3:]
                target[2] = min(float(final_target[2]), float(current[2]) + 0.0009)
        segment = output / f"segment-{index:02d}"
        package = segment / "package"
        package.mkdir(parents=True)
        q = _geometry_q(fresh, target)
        receipt = _home_receipt(fresh, target, final_target, q, index)
        receipt_path = segment / "home-receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        binding = build_home(receipt_path, package)
        validation = segment / "local-validation.json"
        validator = OWNER / "ur10e-tp-package-delivery/scripts/validate_ur_tp_package.py"
        cmd = [
            "/usr/bin/python3",
            str(validator),
            "--package-dir",
            str(package),
            "--basename",
            BASENAME,
            "--controller-dir",
            CONTROLLER_DIR,
            "--expected-stamp",
            binding["stamp"],
            "--expected-installation",
            "/programs/default.installation",
        ]
        local = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=20)
        validation.write_text(local.stdout)
        local_value = json.loads(local.stdout)
        if local_value.get("pass") is not True:
            raise RuntimeError(f"local Home package validation failed: {local_value}")
        readback = None
        if args.execute:
            readback = _deploy_and_readback(package, segment / "readback", binding["stamp"])
        segment_record = {
            "index": index,
            "fresh_start": fresh,
            "target_pose": target.tolist(),
            "planned_segments_from_fresh_start": count,
            "lateral_m": float(np.linalg.norm(current[:2] - target[:2])),
            "angle_rad": float(np.linalg.norm(so3_log(so3_exp(target[3:]) @ so3_exp(current[3:]).T))),
            "package": str(package),
            "readback": readback,
            "local_validation": str(validation),
        }
        (segment / "planned.json").write_text(json.dumps(segment_record, indent=2) + "\n")
        if not args.execute:
            result["segments"].append(segment_record)
            result["state"] = "read_only_plan"
            break
        home_out = segment / "home-run"
        assert readback is not None
        home_args = SimpleNamespace(
            host=args.host,
            home_receipt=receipt_path,
            validation=Path(readback["validation"]),
            package_dir=package,
            readback_dir=Path(readback["readback_dir"]),
            output=home_out,
            video_url=getattr(args, "video_url", VIDEO_DEFAULT),
            video_policy=getattr(args, "video_policy", "required"),
            execute=True,
        )
        home_result = run_home(home_args)
        segment_record["home_result"] = home_result
        (segment / "planned.json").write_text(json.dumps(segment_record, indent=2) + "\n")
        result["segments"].append(segment_record)
        if home_result.get("success") is not True:
            # run_contact_home has already issued its compensating STOP. Keep
            # the attempt failed and stop; the outer fault owner must handle a
            # safety/communication fault rather than hiding it in a loop.
            result["state"] = "BLOCKED"
            result["failure"] = home_result.get("failure", "segmented Home segment failed")
            break
    else:
        result["state"] = "BLOCKED"
        result["failure"] = "maximum segmented Home count exhausted"

    if result.get("success"):
        result["state"] = "HOME_RECOVERED"
    result["ended_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    (output / "summary.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host", default=HOST_DEFAULT)
    parser.add_argument("--video-url", default=VIDEO_DEFAULT)
    parser.add_argument("--video-policy", choices=("required", "evidence-only"), default="required")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        value = run(args)
    except Exception as exc:  # persist a machine-readable blocked receipt
        value = {"success": False, "state": "BLOCKED", "failure": f"{type(exc).__name__}: {exc}"}
        if args.output:
            Path(args.output).mkdir(parents=True, exist_ok=True)
            (Path(args.output) / "summary.json").write_text(json.dumps(value, indent=2) + "\n")
    print(json.dumps(value, indent=2, default=str))
    return 0 if value.get("success") or value.get("state") == "read_only_plan" else 1


if __name__ == "__main__":
    raise SystemExit(main())
