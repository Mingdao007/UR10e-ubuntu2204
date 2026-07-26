#!/usr/bin/env python3
"""Filesystem-only producer for the Step5d offline composition certificate.

This process has no controller, Dashboard, RTDE, sensor, or motion endpoint.
It reads the production AtomicCommandMailbox and publishes the production
growing-CSV byte contract consumed by BridgeCsvFollower.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_live_driver import AtomicCommandMailbox, MailboxCommand
from step5d_autotune_state_machine import HostCommand, TpLoopState
from step5d_autotune_v3.runtime_profile import load_launch_profile
from step5d_production_csv import ProductionCsvWriter


CSV_FIELDS = (
    "write_index",
    "t_wall_ns",
    "t_monotonic_s",
    "sensor_age_s",
    "rtde_feedback_age_s",
    "normal_force_n",
    "force_norm_n",
    "torque_norm_nm",
    "heartbeat",
    "sensor_ok",
    "stop_request",
    "guard_reason",
    "rtde_connected",
    "step4e_controller_state",
    "ur_timestamp",
    "ur_runtime_state",
    "ur_safety_mode",
    *(f"ur_actual_TCP_pose_{index}" for index in range(6)),
    *(f"ur_actual_TCP_speed_{index}" for index in range(6)),
    *(f"ur_actual_q_{index}" for index in range(6)),
    *(f"ur_actual_qd_{index}" for index in range(6)),
    *(f"ur_output_int_register_{index}" for index in range(24, 35)),
    "ur_output_double_register_30",
    *(f"ur_output_double_register_{index}" for index in range(35, 45)),
)
HOME_POSE = (0.45, 0.10, 0.055, 3.128, 0.0, 0.042)
JOINTS = (0.62, -1.65, -2.55, -0.49, 1.55, -0.95)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(dict(payload), allow_nan=False, separators=(",", ":"), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


def _row(
    command: MailboxCommand | None,
    *,
    state: TpLoopState,
    write_index: int,
    terminal_reason: int = 0,
) -> dict[str, Any]:
    packet = None if command is None else command.packet
    binding = None if command is None else command.binding
    identity = packet is not None
    row: dict[str, Any] = {
        "write_index": write_index,
        "t_wall_ns": time.time_ns(),
        "t_monotonic_s": time.monotonic(),
        "sensor_age_s": 0.001,
        "rtde_feedback_age_s": 0.001,
        "normal_force_n": 0.0,
        "force_norm_n": 0.0,
        "torque_norm_nm": 0.0,
        "heartbeat": write_index,
        "sensor_ok": 1,
        "stop_request": 0,
        "guard_reason": "",
        "rtde_connected": 1,
        "step4e_controller_state": 0,
        "ur_timestamp": time.monotonic(),
        "ur_runtime_state": 2,
        "ur_safety_mode": 1,
        "ur_output_int_register_24": getattr(packet, "campaign_epoch", 0),
        "ur_output_int_register_25": getattr(packet, "trial_id", 0),
        "ur_output_int_register_26": int(state),
        "ur_output_int_register_27": getattr(packet, "candidate_token", 0),
        "ur_output_int_register_28": terminal_reason,
        "ur_output_int_register_29": getattr(packet, "execution_profile_id", 0),
        "ur_output_int_register_30": getattr(packet, "command_seq", 0),
        "ur_output_int_register_31": getattr(binding, "batch_row_index", 0) or 0,
        "ur_output_int_register_32": 2 if identity else 0,
        "ur_output_int_register_33": 0x7F if identity else 0,
        "ur_output_int_register_34": getattr(packet, "logical_batch_sequence", 0),
        "ur_output_double_register_30": float(terminal_reason),
        "ur_output_double_register_35": 0.0,
        "ur_output_double_register_36": 0.0,
        "ur_output_double_register_37": 0.0,
        "ur_output_double_register_38": 0.0,
        "ur_output_double_register_39": 0.0,
        "ur_output_double_register_40": 0.0,
        "ur_output_double_register_41": 0.0,
        "ur_output_double_register_42": 0.0,
        "ur_output_double_register_43": 0.0,
        "ur_output_double_register_44": 0.002,
    }
    for index in range(6):
        row[f"ur_actual_TCP_pose_{index}"] = HOME_POSE[index]
        row[f"ur_actual_TCP_speed_{index}"] = 0.0
        row[f"ur_actual_q_{index}"] = JOINTS[index]
        row[f"ur_actual_qd_{index}"] = 0.0
    return row


def _wait_for_file(path: Path, role: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file() and not path.is_symlink():
            return
        time.sleep(0.005)
    raise TimeoutError(f"offline composition timed out waiting for {role}")


def _wait_runner_home_barrier(path: Path, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.is_file() and not path.is_symlink():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if payload.get("state") == "WAITING_FOR_HOME":
                return
        time.sleep(0.005)
    raise TimeoutError("offline composition timed out waiting for runner Home barrier")


def _wait_arm(mailbox: AtomicCommandMailbox, sequence: int) -> MailboxCommand:
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        command = mailbox.read_latest()
        if (
            command is not None
            and command.packet.command is HostCommand.ARM
            and command.packet.command_seq == sequence
        ):
            return command
        time.sleep(0.005)
    raise TimeoutError(f"offline composition timed out waiting for ARM {sequence}")


def _write_capture(bridge_run: Path, command: MailboxCommand) -> None:
    path = bridge_run / "autotune_trials" / command.binding.trial_uid / "capture.csv"
    path.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
    path.write_text("time,value\n0,0\n", encoding="utf-8")


def run(
    *,
    bridge_run: Path,
    mailbox_path: Path,
    launch_profile: Path,
    runner_ready: Path,
    runner_status: Path,
    trial_count: int,
) -> dict[str, Any]:
    if trial_count not in {1, 10, 100}:
        raise ValueError("offline composition trial count must be exactly 1, 10, or 100")
    if bridge_run.exists() or bridge_run.is_symlink():
        raise ValueError("offline bridge run must be a new path")
    bridge_run.mkdir(parents=True, mode=0o700)
    mailbox_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_payload = json.loads(launch_profile.read_text(encoding="utf-8"))
    profile = load_launch_profile(
        launch_profile,
        expected_tp_program_id=str(profile_payload["tp_program_id"]),
    )
    mailbox = AtomicCommandMailbox(
        mailbox_path,
        network_mode=True,
        launch_profile=profile,
    )
    csv_path = bridge_run / "bridge_rtde_500hz.csv"
    sequences: list[int] = []
    with csv_path.open("x", encoding="utf-8", newline="") as handle:
        writer = ProductionCsvWriter(handle, CSV_FIELDS, flush_interval_rows=50)
        _write_json(
            bridge_run / "bridge_ready.json",
            {
                "schema": "step5d.offline-no-motion/bridge-ready-v1",
                "ok": True,
                "transport": "filesystem_only",
                "controller_connected": False,
                "controller_io": False,
                "motion_capable": False,
                "live_authorization": False,
                "trial_count": trial_count,
            },
        )
        _wait_for_file(runner_ready, "runner readiness")
        _wait_runner_home_barrier(runner_status)
        writer.writerow(_row(None, state=TpLoopState.READY_HOME, write_index=0))
        writer.flush(durable=True)
        for index in range(1, trial_count + 1):
            command = _wait_arm(mailbox, index)
            writer.writerow(
                _row(
                    command,
                    state=TpLoopState.ARMED,
                    write_index=index * 3 - 2,
                )
            )
            writer.writerow(
                _row(
                    command,
                    state=TpLoopState.RUN,
                    write_index=index * 3 - 1,
                )
            )
            _write_capture(bridge_run, command)
            writer.writerow(
                _row(
                    command,
                    state=TpLoopState.READY_HOME_NEXT,
                    write_index=index * 3,
                    terminal_reason=1,
                )
            )
            sequences.append(command.packet.command_seq)
        writer.flush(durable=True)
        writer_stats = writer.stats.__dict__
    result = {
        "schema": "step5d.offline-no-motion/bridge-composition-v1",
        "trial_count": trial_count,
        "arm_command_sequences": sequences,
        "writer": writer_stats,
        "controller_io": False,
        "live_authorization": False,
    }
    _write_json(bridge_run / "offline_bridge_stats.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-run", type=Path, required=True)
    parser.add_argument("--mailbox", dest="mailbox_path", type=Path, required=True)
    parser.add_argument("--launch-profile", type=Path, required=True)
    parser.add_argument("--runner-ready", type=Path, required=True)
    parser.add_argument("--runner-status", type=Path, required=True)
    parser.add_argument("--trial-count", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(run(**vars(args)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
