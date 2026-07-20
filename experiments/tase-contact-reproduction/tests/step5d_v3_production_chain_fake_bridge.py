#!/usr/bin/env python3
"""No-network transport double for the production V3 launcher-chain test."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_live_driver import BridgeMailboxRuntime  # noqa: E402


CSV_FIELDS = (
    "t_monotonic_s",
    "ur_timestamp",
    "ur_runtime_state",
    "ur_safety_mode",
    *(f"ur_output_int_register_{index}" for index in range(24, 31)),
    *(f"ur_actual_TCP_pose_{index}" for index in range(6)),
    *(f"ur_actual_TCP_speed_{index}" for index in range(6)),
    *(f"ur_actual_q_{index}" for index in range(6)),
    *(f"ur_actual_qd_{index}" for index in range(6)),
    "ur_output_double_register_35",
)


def _row(
    *,
    playing: bool,
    stage: float = 0.0,
    home_jitter: bool = False,
    packet: object | None = None,
) -> dict[str, float | int]:
    armed = packet is not None
    row: dict[str, float | int] = {
        "t_monotonic_s": time.monotonic(),
        "ur_timestamp": time.monotonic(),
        "ur_runtime_state": 2 if playing else 1,
        "ur_safety_mode": 1,
        "ur_output_int_register_24": getattr(packet, "campaign_epoch", 0) if armed else (0 if playing else 1),
        "ur_output_int_register_25": getattr(packet, "trial_id", 0) if armed else (0 if playing else 731015534),
        "ur_output_int_register_26": 20 if armed else (10 if playing else 90),
        "ur_output_int_register_27": getattr(packet, "candidate_token", 0) if armed else (0 if playing else 742421926),
        "ur_output_int_register_28": 0 if playing else 20,
        "ur_output_int_register_29": getattr(packet, "execution_profile_id", 0) if armed else (0 if playing else 9001),
        "ur_output_int_register_30": getattr(packet, "command_seq", 0) if armed else (0 if playing else 1),
        "ur_output_double_register_35": stage,
    }
    pose = (
        (0.450020, 0.10, 0.055, 3.128, 0.000080, 0.042)
        if home_jitter
        else (0.45, 0.10, 0.055, 3.128, 0.0, 0.042)
    )
    joints = (
        (0.6201, -1.65, -2.55, -0.49, 1.55, -0.95)
        if home_jitter
        else (0.62, -1.65, -2.55, -0.49, 1.55, -0.95)
    )
    for index in range(6):
        row[f"ur_actual_TCP_pose_{index}"] = pose[index]
        row[f"ur_actual_TCP_speed_{index}"] = 0.0
        row[f"ur_actual_q_{index}"] = joints[index]
        row[f"ur_actual_qd_{index}"] = 0.0
    return row


def _runtime_output(row: dict[str, float | int]) -> dict[str, object]:
    return {
        "timestamp": row["ur_timestamp"],
        "safety_mode": row["ur_safety_mode"],
        **{
            f"output_int_register_{index}": row[f"ur_output_int_register_{index}"]
            for index in range(24, 31)
        },
        **{
            name: [row[f"ur_{name}_{index}"] for index in range(6)]
            for name in ("actual_TCP_pose", "actual_TCP_speed", "actual_q", "actual_qd")
        },
    }


def run(output_dir: Path, mailbox: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "bridge_rtde_500hz.csv"
    home_path = output_dir / "campaign_home_reference.json"
    runtime = BridgeMailboxRuntime(
        mailbox.absolute(),
        campaign_home_reference_path=home_path.absolute(),
    )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerow(_row(playing=False))
        handle.flush()
        os.fsync(handle.fileno())
        (output_dir / "bridge_ready.json").write_text(
            json.dumps(
                {
                    "ok": True,
                    "bridge_profile": "step5d_strict_rnn_autotune_v1",
                    "rtde_send_succeeded": True,
                    "sensor_stream_ready": True,
                    "prewarm_status": "ok",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        time.sleep(0.05)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            row = _row(playing=True)
            writer.writerow(row)
            handle.flush()
            runtime.poll(SimpleNamespace(), _runtime_output(row), connection_epoch=1)
            if home_path.is_file():
                packet = runtime.last_command.packet
                for stage in (22.0, 23.0, 24.0, 24.2):
                    writer.writerow(
                        _row(
                            playing=True,
                            stage=stage,
                            home_jitter=True,
                            packet=packet,
                        )
                    )
                    handle.flush()
                    time.sleep(0.02)
                time.sleep(0.10)
                return 0
            if runtime.mailbox.path.is_file():
                row = _row(playing=True, home_jitter=True)
                writer.writerow(row)
                handle.flush()
                runtime.poll(
                    SimpleNamespace(),
                    _runtime_output(row),
                    connection_epoch=1,
                )
            time.sleep(0.005)
    return 3


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mailbox", type=Path, required=True)
    args = parser.parse_args()
    return run(args.output_dir.absolute(), args.mailbox.absolute())


if __name__ == "__main__":
    raise SystemExit(main())
