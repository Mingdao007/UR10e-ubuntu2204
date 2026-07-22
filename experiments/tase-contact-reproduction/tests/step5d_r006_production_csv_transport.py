#!/usr/bin/env python3
"""No-network/no-motion transport for the r006 production-chain release gate.

Only RTDE/TP register observations are simulated.  CSV publication, mailbox
consumption, per-trial rotation, terminal sealing, and byte durability use the
production implementations.
"""

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

from run_step5d_autotune_v3_bridge import (  # noqa: E402
    V3AsyncBridgeTrialCsvRotator,
)
from step5d_autotune_live_driver import (  # noqa: E402
    BridgeMailboxRuntime,
    MailboxCommand,
)
from step5d_autotune_state_machine import HostCommand, TpLoopState  # noqa: E402
from step5d_production_csv import ProductionCsvWriter  # noqa: E402
from ur10e_experiment_runtime.physical_prior import (  # noqa: E402
    STEP5D_V3_PHYSICAL_PRIOR,
)


POLL_S = 0.005
TIMEOUT_S = 30.0
HOME_POSE = (0.45, 0.10, 0.055, 3.128, 0.0, 0.042)
JOINTS = (0.62, -1.65, -2.55, -0.49, 1.55, -0.95)
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
    *(f"ur_output_int_register_{index}" for index in range(24, 34)),
    "ur_output_double_register_30",
    *(f"ur_output_double_register_{index}" for index in range(35, 45)),
)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, allow_nan=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _row(
    *,
    state: TpLoopState,
    packet: Any | None,
    write_index: int,
    terminal_reason: int = 0,
) -> dict[str, float | int | str]:
    identity = packet is not None
    pose = (
        (
            *STEP5D_V3_PHYSICAL_PRIOR.precontact_xyz_m,
            *STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad,
        )
        if state is TpLoopState.READY_NEAR
        else HOME_POSE
    )
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
        "ur_output_int_register_31": 1 if identity else 0,
        "ur_output_int_register_32": 1 if identity else 0,
        "ur_output_int_register_33": 0x7F if identity else 0,
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
        row[f"ur_actual_TCP_pose_{index}"] = pose[index]
        row[f"ur_actual_TCP_speed_{index}"] = 0.0
        row[f"ur_actual_q_{index}"] = JOINTS[index]
        row[f"ur_actual_qd_{index}"] = 0.0
    return row


def _rtde_output(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": row["ur_timestamp"],
        "safety_mode": row["ur_safety_mode"],
        **{
            f"output_int_register_{index}": row[
                f"ur_output_int_register_{index}"
            ]
            for index in range(24, 31)
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


def run(bridge_run: Path, mailbox: Path) -> int:
    bridge_run = bridge_run.resolve()
    mailbox = mailbox.resolve()
    bridge_run.mkdir(parents=True, exist_ok=True)
    mailbox.parent.mkdir(parents=True, exist_ok=True)
    capture_root = (bridge_run / "autotune_trials").resolve()
    csv_path = bridge_run / "bridge_rtde_500hz.csv"
    stats_path = bridge_run / "r006_fake_transport_stats.json"
    home_path = (bridge_run / "campaign_home_reference.json").resolve()
    runtime = BridgeMailboxRuntime(
        mailbox,
        campaign_home_reference_path=home_path,
        completion_protocol="v3_direct_arm_v1",
    )
    rotator = V3AsyncBridgeTrialCsvRotator(capture_root, CSV_FIELDS)
    try:
        with csv_path.open("x", newline="", encoding="utf-8") as handle:
            writer = ProductionCsvWriter(handle, CSV_FIELDS, flush_interval_rows=50)
            home = _row(
                state=TpLoopState.READY_HOME,
                packet=None,
                write_index=0,
            )
            writer.writerow(home)
            # bridge_ready may be published only after the initial complete
            # READY_HOME row is durably observable by the formal runner.
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

            arm1 = _wait_arm(runtime, 1)
            if not runtime.poll(SimpleNamespace(), _rtde_output(home), connection_epoch=1):
                raise RuntimeError("production mailbox did not consume ARM1")
            if runtime.active is None or runtime.active.packet != arm1.packet:
                raise RuntimeError("production mailbox did not retain ARM1 binding")

            run1 = _row(
                state=TpLoopState.RUN,
                packet=arm1.packet,
                write_index=1,
            )
            writer.writerow(run1)
            rotator.observe(run1, active=runtime.active, rtde_output=_rtde_output(run1))

            terminal1 = _row(
                state=TpLoopState.READY_NEAR,
                packet=arm1.packet,
                write_index=2,
                terminal_reason=1,
            )
            writer.publish_row_with_partial_visibility(
                terminal1,
                split_at=max(1, len(CSV_FIELDS) // 2),
                partial_visible_s=0.20,
            )
            rotator.observe(
                terminal1,
                active=runtime.active,
                rtde_output=_rtde_output(terminal1),
            )

            arm2 = _wait_arm(runtime, 2)
            if not runtime.poll(
                SimpleNamespace(),
                _rtde_output(terminal1),
                connection_epoch=1,
            ):
                raise RuntimeError("production mailbox did not consume ARM2")
            if runtime.active is None or runtime.active.packet != arm2.packet:
                raise RuntimeError("production mailbox did not retain ARM2 binding")

            # Preserve the bridge's ordinary 50-row buffering contract.  ARM2
            # becomes visible only at the normal buffer flush boundary.
            for offset in range(50):
                run2 = _row(
                    state=TpLoopState.RUN,
                    packet=arm2.packet,
                    write_index=3 + offset,
                )
                writer.writerow(run2)
            _atomic_json(
                stats_path,
                {
                    "protocol": runtime.completion_protocol,
                    "arm_sequences": [arm1.packet.command_seq, arm2.packet.command_seq],
                    "commands": [arm1.packet.command.name, arm2.packet.command.name],
                    "writer": writer.stats.__dict__,
                    "partial_visibility_exercised": True,
                    "terminal_capture_sealed": (
                        capture_root / arm1.binding.trial_uid / "capture.csv"
                    ).is_file(),
                },
            )
        return 0
    finally:
        rotator.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-run", type=Path, required=True)
    parser.add_argument("--mailbox", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    raise SystemExit(run(arguments.bridge_run, arguments.mailbox))
