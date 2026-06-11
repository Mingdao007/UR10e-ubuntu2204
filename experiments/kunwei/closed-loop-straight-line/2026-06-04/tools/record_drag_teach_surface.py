#!/usr/bin/env python3
"""Passive RTDE recorder for manual drag-teach surface calibration.

This script only reads Dashboard and RTDE output state. It does not send
URScript, write RTDE inputs, load a program, start a program, enable freedrive,
zero sensors, or command robot motion. The operator must perform any physical
drag/freedrive action manually on the robot/teach pendant side.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = EXPERIMENT_ROOT / "runs"
UR_REALSETUP_SCRIPTS = Path("/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts")
sys.path.insert(0, str(UR_REALSETUP_SCRIPTS))

from _ur_common import RTDEClient, dashboard_exchange, read_rtde_once  # noqa: E402


RTDE_FIELDS = [
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_TCP_force",
    "actual_q",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    "speed_scaling",
    "payload",
    "payload_cog",
    "tcp_offset",
]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_output_dir() -> Path:
    return RUN_ROOT / f"drag_teach_surface_{now_stamp()}"


def vector_norm(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def flatten_sample(sample: dict[str, Any], *, index: int, started_mono: float, started_wall_ns: int) -> dict[str, Any]:
    row: dict[str, Any] = {
        "sample_index": index,
        "t_monotonic_s": time.monotonic() - started_mono,
        "t_wall_ns": time.time_ns(),
        "t_wall_start_ns": started_wall_ns,
    }
    for field in RTDE_FIELDS:
        value = sample[field]
        if isinstance(value, list):
            for idx, item in enumerate(value):
                row[f"{field}_{idx}"] = item
        else:
            row[field] = value
    speed = sample["actual_TCP_speed"]
    force = sample["actual_TCP_force"]
    row["tcp_speed_linear_norm_m_s"] = vector_norm(speed[:3])
    row["tcp_speed_angular_norm_rad_s"] = vector_norm(speed[3:])
    row["tcp_force_norm_n"] = vector_norm(force[:3])
    row["tcp_torque_norm_nm"] = vector_norm(force[3:])
    return row


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    dashboard = dashboard_exchange(
        args.robot_host,
        ["is in remote control", "safetymode", "robotmode", "running", "programState"],
        timeout=args.timeout_s,
    )
    if "NORMAL" not in dashboard.get("safetymode", ""):
        raise RuntimeError(f"Dashboard safety is not NORMAL: {dashboard}")
    if "true" in dashboard.get("running", "").lower():
        raise RuntimeError(f"refusing to record while a program is running: {dashboard}")

    rtde = read_rtde_once(args.robot_host, RTDE_FIELDS, frequency_hz=10.0, timeout=args.timeout_s)
    speed_norm = vector_norm(rtde["actual_TCP_speed"][:3])
    if speed_norm > args.max_initial_speed_m_s:
        raise RuntimeError(
            f"initial TCP speed {speed_norm:.6f} m/s exceeds {args.max_initial_speed_m_s:.6f} m/s"
        )
    return {
        "dashboard": dashboard,
        "rtde_initial": rtde,
        "derived": {
            "initial_tcp_speed_linear_norm_m_s": speed_norm,
            "initial_tcp_force_norm_n": vector_norm(rtde["actual_TCP_force"][:3]),
            "initial_tcp_torque_norm_nm": vector_norm(rtde["actual_TCP_force"][3:]),
            "payload_kg": rtde["payload"],
            "tcp_offset_z_mm": rtde["tcp_offset"][2] * 1000.0,
        },
    }


def write_manifest(output_dir: Path, payload: dict[str, Any]) -> None:
    (output_dir / "manifest.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record(args: argparse.Namespace) -> int:
    args.output_dir.mkdir(parents=True, exist_ok=False)
    pre = preflight(args)
    out_csv = args.output_dir / "drag_teach_rtde.csv"
    manifest: dict[str, Any] = {
        "ok": False,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "robot_host": args.robot_host,
        "output_dir": str(args.output_dir),
        "csv": str(out_csv),
        "args": vars(args) | {"output_dir": str(args.output_dir)},
        "preflight": pre,
        "safety_boundary": [
            "read-only Dashboard preflight",
            "read-only RTDE output recipe",
            "no URScript send",
            "no RTDE input writes",
            "no program load/start",
            "no freedrive enable command",
            "no zero_ftsensor",
            "operator performs any drag/freedrive action manually",
        ],
    }
    write_manifest(args.output_dir, manifest)

    interrupted = False

    def handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
        nonlocal interrupted
        interrupted = True

    old_int = signal.signal(signal.SIGINT, handle_signal)
    old_term = signal.signal(signal.SIGTERM, handle_signal)
    rows = 0
    stop_reason = "duration_complete"
    max_force_norm = 0.0
    max_torque_norm = 0.0
    max_speed_norm = 0.0
    started_mono = time.monotonic()
    started_wall_ns = time.time_ns()
    try:
        with RTDEClient(args.robot_host, timeout=args.timeout_s) as client, out_csv.open(
            "w", encoding="utf-8", newline=""
        ) as f:
            client.negotiate()
            recipe_id, type_names = client.setup_outputs(args.rtde_hz, RTDE_FIELDS)
            client.start()
            writer: csv.DictWriter[str] | None = None
            while True:
                if interrupted:
                    stop_reason = "operator_interrupt"
                    break
                elapsed = time.monotonic() - started_mono
                if elapsed >= args.duration_s:
                    break
                values = client.recv_recipe_sample(recipe_id, type_names)
                sample = {field: value for field, value in zip(RTDE_FIELDS, values)}
                row = flatten_sample(sample, index=rows, started_mono=started_mono, started_wall_ns=started_wall_ns)
                if writer is None:
                    writer = csv.DictWriter(f, fieldnames=list(row.keys()), lineterminator="\n")
                    writer.writeheader()
                writer.writerow(row)
                rows += 1
                max_force_norm = max(max_force_norm, float(row["tcp_force_norm_n"]))
                max_torque_norm = max(max_torque_norm, float(row["tcp_torque_norm_nm"]))
                max_speed_norm = max(max_speed_norm, float(row["tcp_speed_linear_norm_m_s"]))
                if max_force_norm > args.max_force_norm_n:
                    stop_reason = "force_norm_limit"
                    break
                if max_torque_norm > args.max_torque_norm_nm:
                    stop_reason = "torque_norm_limit"
                    break
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)

    duration = time.monotonic() - started_mono
    manifest.update(
        {
            "ok": rows > 0 and stop_reason in {"duration_complete", "operator_interrupt"},
            "stop_reason": stop_reason,
            "recorded_rows": rows,
            "recorded_duration_s": duration,
            "observed": {
                "max_tcp_speed_linear_norm_m_s": max_speed_norm,
                "max_tcp_force_norm_n": max_force_norm,
                "max_tcp_torque_norm_nm": max_torque_norm,
                "approx_row_rate_hz": rows / duration if duration > 0 else None,
            },
        }
    )
    write_manifest(args.output_dir, manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest["ok"] else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--duration-s", type=float, default=90.0)
    parser.add_argument("--rtde-hz", type=float, default=125.0)
    parser.add_argument("--timeout-s", type=float, default=3.0)
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument("--max-initial-speed-m-s", type=float, default=0.002)
    parser.add_argument("--max-force-norm-n", type=float, default=80.0)
    parser.add_argument("--max-torque-norm-nm", type=float, default=5.0)
    args = parser.parse_args(argv)
    if args.duration_s <= 0 or args.rtde_hz <= 0:
        raise SystemExit("duration and rtde_hz must be positive")
    return record(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"recording blocked: {exc}", file=sys.stderr)
        raise SystemExit(2)
