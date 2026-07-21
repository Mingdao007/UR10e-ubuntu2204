#!/usr/bin/env python3
"""No-network/no-motion production transport for the r008 rolling chain gate."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from run_step5d_autotune_v3_bridge import V3AsyncBridgeTrialCsvRotator  # noqa: E402
from step5d_autotune_live_driver import BridgeMailboxRuntime, MailboxCommand  # noqa: E402
from step5d_autotune_state_machine import HostCommand, TpLoopState  # noqa: E402
from step5d_production_csv import ProductionCsvWriter  # noqa: E402
from step5d_r006_production_csv_transport import (  # noqa: E402
    HOME_POSE,
    JOINTS,
    POLL_S,
    TIMEOUT_S,
    _atomic_json,
)


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
    "_step5d_stage25_echo_consumed",
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


def _row(
    *, state: TpLoopState, command: MailboxCommand | None, write_index: int, terminal_reason: int = 0
) -> dict[str, float | int | str]:
    packet = None if command is None else command.packet
    binding = None if command is None else command.binding
    identity = packet is not None
    row: dict[str, float | int | str] = {
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
        "_step5d_stage25_echo_consumed": 0,
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
        "ur_output_double_register_35": 40.3 if terminal_reason else 0.0,
        "ur_output_double_register_36": 0.0,
        "ur_output_double_register_37": 0.0,
        "ur_output_double_register_38": 0.0,
        "ur_output_double_register_39": 3.0 if terminal_reason else 0.0,
        "ur_output_double_register_40": 0.001,
        "ur_output_double_register_41": 0.01,
        "ur_output_double_register_42": 0.002,
        "ur_output_double_register_43": 0.02,
        "ur_output_double_register_44": 0.002,
    }
    for index in range(6):
        row[f"ur_actual_TCP_pose_{index}"] = HOME_POSE[index]
        row[f"ur_actual_TCP_speed_{index}"] = 0.0
        row[f"ur_actual_q_{index}"] = JOINTS[index]
        row[f"ur_actual_qd_{index}"] = 0.0
    return row


def _rtde_output(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": row["ur_timestamp"],
        "safety_mode": row["ur_safety_mode"],
        **{
            f"output_int_register_{index}": row[f"ur_output_int_register_{index}"]
            for index in range(24, 35)
        },
        **{
            name: [row[f"ur_{name}_{index}"] for index in range(6)]
            for name in ("actual_TCP_pose", "actual_TCP_speed", "actual_q", "actual_qd")
        },
    }


def _wait_arm(runtime: BridgeMailboxRuntime, sequence: int) -> MailboxCommand:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        command = runtime.mailbox.read_latest()
        if (
            command is not None
            and command.packet.command is HostCommand.ARM
            and command.packet.command_seq == sequence
        ):
            return command
        time.sleep(POLL_S)
    raise TimeoutError(f"timed out waiting for ARM sequence {sequence}")


def _wait_complete(runtime: BridgeMailboxRuntime, sequence: int) -> MailboxCommand:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        command = runtime.mailbox.read_latest()
        if (
            command is not None
            and command.packet.command is HostCommand.COMPLETE_AT_HOME
            and command.packet.command_seq == sequence
        ):
            return command
        time.sleep(POLL_S)
    raise TimeoutError(f"timed out waiting for COMPLETE sequence {sequence}")


def run(bridge_run: Path, mailbox: Path, trial_count: int) -> int:
    bridge_run = bridge_run.resolve()
    mailbox = mailbox.resolve()
    bridge_run.mkdir(parents=True, exist_ok=True)
    mailbox.parent.mkdir(parents=True, exist_ok=True)
    capture_root = (bridge_run / "autotune_trials").resolve()
    csv_path = bridge_run / "bridge_rtde_500hz.csv"
    runtime = BridgeMailboxRuntime(
        mailbox,
        campaign_home_reference_path=(bridge_run / "campaign_home_reference.json").resolve(),
        completion_protocol="v3_full_home_rolling_arm_v1",
    )
    rotator = V3AsyncBridgeTrialCsvRotator(capture_root, CSV_FIELDS)
    arm_sequences: list[int] = []
    logical_sequences: list[int] = []
    rows: list[int] = []
    try:
        with csv_path.open("x", newline="", encoding="utf-8") as handle:
            writer = ProductionCsvWriter(handle, CSV_FIELDS, flush_interval_rows=50)
            previous = _row(state=TpLoopState.READY_HOME, command=None, write_index=0)
            writer.writerow(previous)
            writer.flush(durable=True)
            _atomic_json(
                bridge_run / "bridge_ready.json",
                {
                    "ok": True,
                    "bridge_profile": "step5d_strict_rnn_autotune_v1",
                    "rtde_send_succeeded": True,
                    "sensor_stream_ready": True,
                    "prewarm_status": "ok",
                    "transport": "fake_no_network_no_motion",
                    "motion_capable": False,
                    "controller_connected": False,
                },
            )
            write_index = 1
            for trial_number in range(1, trial_count + 1):
                command = _wait_arm(runtime, trial_number)
                expected_sequence = (trial_number - 1) // 5 + 1
                expected_row = (trial_number - 1) % 5 + 1
                if command.packet.logical_batch_sequence != expected_sequence:
                    raise RuntimeError("ARM logical batch sequence differs")
                if command.binding.batch_row_index != expected_row:
                    raise RuntimeError("ARM logical batch row differs")
                if not runtime.poll(SimpleNamespace(), _rtde_output(previous), connection_epoch=1):
                    raise RuntimeError("production mailbox did not consume rolling ARM")
                run_row = _row(
                    state=TpLoopState.RUN,
                    command=command,
                    write_index=write_index,
                )
                write_index += 1
                writer.writerow(run_row)
                rotator.observe(run_row, active=runtime.active, rtde_output=_rtde_output(run_row))
                terminal = _row(
                    state=TpLoopState.READY_HOME_NEXT,
                    command=command,
                    write_index=write_index,
                    terminal_reason=1,
                )
                write_index += 1
                writer.publish_row_with_partial_visibility(
                    terminal,
                    split_at=max(1, len(CSV_FIELDS) // 2),
                    partial_visible_s=0.01,
                )
                rotator.observe(terminal, active=runtime.active, rtde_output=_rtde_output(terminal))
                previous = terminal
                arm_sequences.append(command.packet.command_seq)
                logical_sequences.append(expected_sequence)
                rows.append(expected_row)
            complete = _wait_complete(runtime, trial_count + 1)
            if not runtime.poll(
                SimpleNamespace(), _rtde_output(previous), connection_epoch=1
            ):
                raise RuntimeError("production mailbox did not consume COMPLETE")
            completed = _row(
                state=TpLoopState.READY_HOME_CLOSED,
                command=complete,
                write_index=write_index,
                terminal_reason=1,
            )
            writer.writerow(completed)
            writer.flush(durable=True)
            _atomic_json(
                bridge_run / "r008_fake_transport_stats.json",
                {
                    "protocol": runtime.completion_protocol,
                    "arm_sequences": arm_sequences,
                    "logical_batch_sequences": logical_sequences,
                    "rows": rows,
                    "writer": writer.stats.__dict__,
                    "trial_count": trial_count,
                    "complete_command_seq": complete.packet.command_seq,
                    "final_state": int(TpLoopState.READY_HOME_CLOSED),
                },
            )
        return 0
    finally:
        rotator.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-run", type=Path, required=True)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument("--trial-count", type=int, default=15)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(run(args.bridge_run, args.mailbox, args.trial_count))
