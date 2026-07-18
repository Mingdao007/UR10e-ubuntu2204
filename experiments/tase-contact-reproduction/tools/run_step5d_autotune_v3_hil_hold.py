#!/usr/bin/env python3
"""Serialized full-production-bridge HOLD-only HIL gate for Step5d V3.

Normal execution is live-gated.  ``--check`` is offline and only validates the
current profile, wrapper, and zero-command argv without starting a process.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

import verify_step5d_autotune_v3_hil_authorization as authorization_gate
from step5d_autotune_v3.launcher import build_bridge_argv, check_effective_config
from step5d_autotune_v3.runtime_profile import (
    DEFAULT_OVERLAY,
    CONTROL_PROFILE_ID,
    RELEASE_STAGE_ID,
    TP_PROGRAM_ID,
    load_launch_profile,
    overlay_fingerprint,
)
from step5d_autotune_v3.runtime_calibration import bootstrap_stable_cuda_runtime
from step5d_autotune_v3.state import atomic_json


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "tools/run_step5d_autotune_v3_bridge.py"
RESULT_SCHEMA = "step5d.autotune-v3/hil-full-bridge-hold-result-v1"
PREFLIGHT_SCHEMA = "step5d.autotune-v3/hil-preflight-snapshot-v1"
STATIONARY_WINDOW_S = 5.0
TCP_SPEED_LIMIT_M_S = 0.001
JOINT_DRIFT_LIMIT_RAD = 0.002
TCP_TRANSLATION_DRIFT_LIMIT_M = 0.0005
TCP_ORIENTATION_DRIFT_LIMIT_RAD = 0.005


class HilHoldError(RuntimeError):
    pass


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _float(row: Mapping[str, Any], name: str) -> float:
    try:
        value = float(row[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise HilHoldError(f"HIL sample lacks finite {name}") from exc
    if not math.isfinite(value):
        raise HilHoldError(f"HIL sample {name} is non-finite")
    return value


def _norm(values: Iterable[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def evaluate_hold_rows(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise HilHoldError("HIL bridge CSV contains no samples")
    for row in rows:
        if int(_float(row, "command")) != 0:
            raise HilHoldError("HIL HOLD observed a nonzero command register")
    ready = [row for row in rows if int(_float(row, "ur_output_int_register_26")) == 10]
    if not ready:
        raise HilHoldError("HIL never observed TP READY_HOME")
    start = _float(ready[0], "t_monotonic_s")
    end = _float(ready[-1], "t_monotonic_s")
    if end - start < STATIONARY_WINDOW_S:
        raise HilHoldError("HIL READY_HOME stationary dwell is shorter than 5 seconds")
    identity_fields = [
        "ur_output_int_register_24",
        "ur_output_int_register_25",
        "ur_output_int_register_27",
        "ur_output_int_register_29",
        "ur_output_int_register_30",
    ]
    if any(int(_float(row, field)) != 0 for row in ready for field in identity_fields):
        raise HilHoldError("HIL READY_HOME identity is not all zero")
    if any(int(_float(row, "ur_output_int_register_28")) != 0 for row in ready):
        raise HilHoldError("HIL READY_HOME terminal reason is nonzero")
    first_q = [_float(ready[0], f"ur_actual_q_{index}") for index in range(6)]
    first_pose = [
        _float(ready[0], f"ur_actual_TCP_pose_{index}") for index in range(6)
    ]
    max_speed = max(
        _norm(_float(row, f"ur_actual_TCP_speed_{index}") for index in range(3))
        for row in ready
    )
    max_joint_drift = max(
        max(
            abs(_float(row, f"ur_actual_q_{index}") - first_q[index])
            for index in range(6)
        )
        for row in ready
    )
    max_translation_drift = max(
        _norm(
            _float(row, f"ur_actual_TCP_pose_{index}") - first_pose[index]
            for index in range(3)
        )
        for row in ready
    )
    max_orientation_drift = max(
        _norm(
            _float(row, f"ur_actual_TCP_pose_{index}") - first_pose[index]
            for index in range(3, 6)
        )
        for row in ready
    )
    limits = {
        "tcp_speed_m_s": TCP_SPEED_LIMIT_M_S,
        "joint_drift_rad": JOINT_DRIFT_LIMIT_RAD,
        "tcp_translation_drift_m": TCP_TRANSLATION_DRIFT_LIMIT_M,
        "tcp_orientation_drift_rad": TCP_ORIENTATION_DRIFT_LIMIT_RAD,
    }
    observed = {
        "tcp_speed_m_s": max_speed,
        "joint_drift_rad": max_joint_drift,
        "tcp_translation_drift_m": max_translation_drift,
        "tcp_orientation_drift_rad": max_orientation_drift,
    }
    exceeded = [name for name in limits if observed[name] > limits[name]]
    if exceeded:
        raise HilHoldError(f"HIL stationary limits exceeded: {exceeded}")
    return {
        "ready_home_samples": len(ready),
        "ready_home_dwell_s": end - start,
        "observed_maxima": observed,
        "limits": limits,
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise HilHoldError(f"cannot read bridge CSV: {exc}") from exc


def _stop_v3_program(robot_host: str) -> dict[str, Any]:
    from preflight_readonly import dashboard_exchange

    result = dashboard_exchange(
        robot_host,
        ["stop", "programState"],
        timeout=2.0,
    )
    state = str(result.get("programState", "")).upper()
    if "STOPPED" not in state:
        raise HilHoldError(f"V3 HIL could not prove TP program STOPPED: {result}")
    return result


def _validate_preflight(path: Path, authorization: Mapping[str, Any]) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise HilHoldError("fresh V3 HIL preflight snapshot is required")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != PREFLIGHT_SCHEMA:
        raise HilHoldError("V3 HIL preflight schema differs")
    if payload.get("ok") is not True or payload.get("fresh") is not True:
        raise HilHoldError("V3 HIL preflight did not pass freshly")
    expected = {
        "candidate_stage_id": RELEASE_STAGE_ID,
        "control_profile_id": CONTROL_PROFILE_ID,
        "tp_program_id": TP_PROGRAM_ID,
        "authorization_id": authorization["authorization_id"],
        "identity": authorization["identity"],
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise HilHoldError(f"V3 HIL preflight {key} differs")
    predicates = payload.get("predicates") or {}
    required = {
        "safety_normal",
        "program_loaded_stopped",
        "robot_stationary",
        "no_existing_writer",
        "mailbox_hold_zero",
        "runtime_dependencies",
    }
    if set(predicates) != required or not all(
        isinstance(predicates[name], dict) and predicates[name].get("ok") is True
        for name in required
    ):
        raise HilHoldError("V3 HIL preflight predicates are incomplete")
    return payload


def build_hold_command(
    *,
    runtime_root: Path,
    launch_profile_path: Path,
) -> tuple[list[str], dict[str, Any]]:
    profile = load_launch_profile(launch_profile_path)
    report = check_effective_config(
        runtime_root=runtime_root,
        launch_profile_path=launch_profile_path,
        trial_overlay=DEFAULT_OVERLAY,
    )
    governed = build_bridge_argv(
        runtime_root,
        launch_profile=profile,
        trial_overlay=DEFAULT_OVERLAY,
    )
    command = [sys.executable, str(WRAPPER), *governed[2:]]
    for flag in (
        "--step5d-autotune-campaign-epoch",
        "--step5d-autotune-trial-id",
        "--step5d-autotune-command",
        "--step5d-autotune-candidate-token",
        "--step5d-autotune-execution-profile-id",
        "--step5d-autotune-command-sequence",
    ):
        index = command.index(flag)
        if command[index + 1] != "0":
            raise HilHoldError(f"HIL HOLD command is nonzero at {flag}")
    mailbox = runtime_root / "command.json"
    if mailbox.exists() or mailbox.is_symlink():
        raise HilHoldError("HIL HOLD mailbox must be absent before bridge start")
    return command, report


def run_gate(args: argparse.Namespace) -> dict[str, Any]:
    runtime_root = args.output_root.absolute() / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    command, check = build_hold_command(
        runtime_root=runtime_root,
        launch_profile_path=args.launch_profile,
    )
    if args.check:
        return {
            "schema": RESULT_SCHEMA,
            "ok": True,
            "claim": "offline_static_check_only",
            "bridge_started": False,
            "command_sha256": _sha256_json(command[2:]),
            "identity": {
                "release_stage_id": RELEASE_STAGE_ID,
                "control_profile_id": CONTROL_PROFILE_ID,
                "tp_program_id": TP_PROGRAM_ID,
            },
            "launch_profile_fingerprint": check["launch_profile_fingerprint"],
            "trial_overlay_fingerprint": check["trial_overlay_fingerprint"],
        }
    authorization = authorization_gate.verify_authorization(
        args.authorization,
        expected_thread_id=args.expected_thread_id,
        root=ROOT,
    )
    preflight = _validate_preflight(args.preflight, authorization)
    output_dir = Path(check["effective_config"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    ticket = {
        "schema": "step5d.autotune-v3/runtime-ticket-v1",
        "parent_pid": os.getpid(),
        "argv_sha256": _sha256_json(command[2:]),
        "authorization_id": authorization["authorization_id"],
        "authorization_scope": authorization["scope"],
        "identity": authorization["identity"],
        "launch_profile_fingerprint": check["launch_profile_fingerprint"],
        "trial_overlay_fingerprint": check["trial_overlay_fingerprint"],
        "release_stage_id": RELEASE_STAGE_ID,
        "control_profile_id": CONTROL_PROFILE_ID,
        "tp_program_id": TP_PROGRAM_ID,
    }
    ticket_path = runtime_root / "runtime_ticket.json"
    atomic_json(ticket_path, ticket)
    environment = {
        **os.environ,
        "STEP5D_V3_RUNTIME_TICKET": str(ticket_path),
        "STEP5D_BRIDGE_LAUNCH_NONCE": uuid.uuid4().hex,
    }
    log_path = args.output_root / "bridge.log"
    ready_announced = False
    dashboard_stop: dict[str, Any] | None = None
    dashboard_stop_error: str | None = None
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
        try:
            ready = output_dir / "bridge_ready.json"
            deadline = time.monotonic() + args.play_timeout_s
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise HilHoldError("production bridge exited before HOLD readiness")
                if ready.is_file():
                    print("READY_FOR_TP_PLAY_V3_HOLD", flush=True)
                    ready_announced = True
                    break
                time.sleep(0.05)
            else:
                raise HilHoldError("production bridge readiness timeout")
            csv_path = output_dir / "bridge_rtde_500hz.csv"
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise HilHoldError("production bridge exited during HOLD observation")
                if csv_path.is_file():
                    try:
                        stationary = evaluate_hold_rows(_read_csv(csv_path))
                    except HilHoldError:
                        pass
                    else:
                        break
                time.sleep(0.1)
            else:
                raise HilHoldError("TP Play/READY_HOME stationary observation timeout")
        finally:
            if ready_announced:
                try:
                    dashboard_stop = _stop_v3_program(
                        str(check["effective_config"]["robot_host"])
                    )
                except Exception as exc:
                    dashboard_stop_error = f"{type(exc).__name__}:{exc}"
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5.0)
            atomic_json(
                args.output_root / "cleanup.json",
                {
                    "program_stop_attempted": ready_announced,
                    "program_stop": dashboard_stop,
                    "program_stop_error": dashboard_stop_error,
                    "bridge_exit_code": process.returncode,
                },
            )
    if dashboard_stop_error is not None:
        raise HilHoldError(f"V3 HIL program-stop cleanup failed: {dashboard_stop_error}")
    summary_path = output_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("zero_events") != []:
        raise HilHoldError("HIL HOLD observed a sensor zero event")
    result = {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "claim": "target_controller_full_bridge_hold_no_motion",
        "authorization_id": authorization["authorization_id"],
        "controller_identity_sha256": preflight["controller_identity_sha256"],
        "stationary": stationary,
        "zero_events": [],
        "program_stop": dashboard_stop,
        "bridge_exit_code": process.returncode,
        "bridge_output": str(output_dir),
        "command_sha256": _sha256_json(command[2:]),
        "launch_profile_fingerprint": check["launch_profile_fingerprint"],
        "trial_overlay_fingerprint": check["trial_overlay_fingerprint"],
    }
    atomic_json(args.output_root / "hil_full_bridge_hold_result.json", result)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--launch-profile",
        type=Path,
        default=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
    )
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--expected-thread-id")
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--play-timeout-s", type=float, default=90.0)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.check and (
        args.authorization is None
        or not args.expected_thread_id
        or args.preflight is None
    ):
        parser.error("live HIL requires --authorization, --expected-thread-id, and --preflight")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = run_gate(args)
    except Exception as exc:
        result = {"schema": RESULT_SCHEMA, "ok": False, "blocker": str(exc)}
        try:
            atomic_json(args.output_root / "hil_full_bridge_hold_result.json", result)
        except Exception as evidence_exc:
            result["evidence_write_blocker"] = str(evidence_exc)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    if "--check" not in sys.argv[1:]:
        bootstrap_stable_cuda_runtime()
    raise SystemExit(main())
