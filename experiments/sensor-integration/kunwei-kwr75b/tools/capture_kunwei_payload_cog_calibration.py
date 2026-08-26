#!/usr/bin/env python3
"""Capture a no-contact Kunwei + UR pose sweep for payload/CoG fitting.

This is an operator-guided logger.  It does not command robot motion, write
RTDE inputs, call ``zero_ftsensor()``, or issue Kunwei zero/tare/configuration
commands.  Kunwei stream start/stop packets are the only sensor-side commands
and are enabled by default, matching the existing gravity-axis logger.
"""

from __future__ import annotations

import argparse
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from capture_kunwei_gravity_axis_calibration import (
    SharedState,
    capture_segment,
    dashboard_exchange,
    rtde_worker,
    sensor_worker,
    write_json,
)


DEFAULT_P0_JOINT_DEG = [
    26.00022327384637,
    -94.99769405105059,
    -124.99880022678312,
    -1.9992690094169994,
    -7.000387466216799,
    -140.53747057064948,
]


def _format_joint_deg(joint_deg: Sequence[float]) -> str:
    return "[" + ", ".join(str(float(value)) for value in joint_deg) + "] deg"


def build_pose_plan(p0_joint_deg: Sequence[float]) -> list[dict[str, Any]]:
    if len(p0_joint_deg) != 6:
        raise ValueError("p0_joint_deg must contain exactly six joint angles")
    p0 = [float(value) for value in p0_joint_deg]
    p1 = p0.copy()
    p1[3] += 45.0
    p2 = p0.copy()
    p2[3] -= 45.0
    p3 = p0.copy()
    p3[4] -= 60.0
    return [
        {
            "name": "P0",
            "joint_deg": p0,
            "unique_pose": True,
            "operator_action": f"手动移动到 launch P0 {_format_joint_deg(p0)}；保持无接触并确认稳定。",
        },
        {
            "name": "P1",
            "joint_deg": p1,
            "unique_pose": True,
            "operator_action": f"仅将 Wrist1 相对 P0 +45 deg；resolved pose {_format_joint_deg(p1)}，保持无接触并确认稳定。",
        },
        {
            "name": "P2",
            "joint_deg": p2,
            "unique_pose": True,
            "operator_action": f"仅将 Wrist1 相对 P0 -45 deg；resolved pose {_format_joint_deg(p2)}，保持无接触并确认稳定。",
        },
        {
            "name": "P3",
            "joint_deg": p3,
            "unique_pose": True,
            "operator_action": f"Wrist1 返回 P0、仅将 Wrist2 相对 P0 -60 deg；resolved pose {_format_joint_deg(p3)}，保持无接触并确认稳定。",
        },
        {
            "name": "P0_return_repeat_drift_validation",
            "joint_deg": p0.copy(),
            "unique_pose": False,
            "exclude_from_unique_pose_count": True,
            "purpose": "drift_validation",
            "operator_action": f"返回 launch P0 {_format_joint_deg(p0)} 作为 drift validation repeat；不计入 unique-pose count，保持无接触并确认稳定。",
        },
    ]


DEFAULT_PLAN = build_pose_plan(DEFAULT_P0_JOINT_DEG)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_output_dir() -> Path:
    return (
        Path("/home/andy/ur10e_ros2_ws/experiments/sensor-integration/kunwei-kwr75b")
        / "measurements"
        / "gravity_axis_calibration"
        / f"{now_stamp()}_rg2_open_custom_compare"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--rtde-hz", type=float, default=125.0)
    parser.add_argument("--rtde-timeout-s", type=float, default=3.0)
    parser.add_argument("--connect-timeout-s", type=float, default=4.0)
    parser.add_argument("--segment-duration-s", type=float, default=5.0)
    parser.add_argument("--settle-s", type=float, default=2.0)
    parser.add_argument("--flush-every", type=int, default=250)
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument(
        "--p0-joint-deg",
        type=float,
        nargs=6,
        default=DEFAULT_P0_JOINT_DEG,
        metavar=("BASE", "SHOULDER", "ELBOW", "WRIST1", "WRIST2", "WRIST3"),
        help="Launch P0 joint angles in degrees; all plan poses preserve P0 Base/Shoulder/Elbow/Wrist3.",
    )
    parser.add_argument("--builtin-payload-kg", type=float, default=1.55)
    parser.add_argument("--builtin-cog-mm", type=float, nargs=3, default=[1.0, 13.0, 51.0])
    parser.add_argument("--condition-label", default="empty_current")
    parser.add_argument("--known-added-mass-kg", type=float, default=None)
    parser.add_argument(
        "--measurement-plane",
        default="UR flange -> Kunwei sensor -> QC/RG2 downstream stack",
    )
    parser.add_argument(
        "--sensor-origin-in-tool-m",
        type=float,
        nargs=3,
        default=None,
        metavar=("X", "Y", "Z"),
        help="Known sensor-origin position expressed in the UR tool frame.",
    )
    parser.add_argument(
        "--no-start-command",
        action="store_true",
        help="Do not send Kunwei stream start (only for an already-running stream).",
    )
    parser.add_argument("--no-stop-command", action="store_true")
    args = parser.parse_args()
    pose_plan = build_pose_plan(args.p0_joint_deg)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "gravity_axis_calibration.csv"
    raw_path = args.output_dir / "kunwei_raw_frames.bin"
    summary_path = args.output_dir / "summary.json"
    metadata_path = args.output_dir / "metadata.json"

    metadata = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "purpose": f"Kunwei + UR pose payload/CoG and gravity-axis comparison; condition={args.condition_label}",
        "safety": [
            "Operator-guided poses; this script commands no robot motion.",
            "No UR RTDE inputs, payload/TCP settings, or zero_ftsensor calls are written.",
            "No Kunwei zero/tare/filter/IP/configuration command is sent.",
            "Kunwei stream start/stop commands are sent unless disabled by CLI flags.",
            "Stop on contact, cable tension, unexpected safety mode, or program start.",
        ],
        "reference_builtin": {
            "payload_kg": args.builtin_payload_kg,
            "cog_mm": args.builtin_cog_mm,
        },
        "condition": {
            "label": args.condition_label,
            "known_added_mass_kg": args.known_added_mass_kg,
            "measurement_plane": args.measurement_plane,
        },
        "sensor_origin_in_tool_m": args.sensor_origin_in_tool_m,
        "pose_plan": pose_plan,
        "unique_pose_count": sum(1 for pose in pose_plan if pose["unique_pose"]),
        "args": {**vars(args), "output_dir": str(args.output_dir)},
        "outputs": {
            "csv": str(csv_path),
            "raw_frames": str(raw_path),
            "summary": str(summary_path),
            "metadata": str(metadata_path),
        },
    }
    write_json(metadata_path, metadata)

    shared = SharedState()

    def signal_handler(signum: int, _frame: Any) -> None:
        shared.add_event("signal", signum=signum)
        with shared.lock:
            shared.stop = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        dash = dashboard_exchange(
            args.robot_host,
            ["robotmode", "safetymode", "programState"],
            timeout=2.0,
        )
        print("Dashboard:", dash)
        shared.add_event("dashboard_preflight", responses=dash)
    except Exception as exc:
        print(f"Dashboard preflight warning: {type(exc).__name__}: {exc}")
        shared.add_event(
            "dashboard_preflight_warning",
            error=f"{type(exc).__name__}: {exc}",
        )

    rtde_thread = threading.Thread(target=rtde_worker, args=(args, shared), daemon=True)
    sensor_thread = threading.Thread(
        target=sensor_worker,
        args=(args, shared, csv_path, raw_path),
        daemon=True,
    )
    rtde_thread.start()
    sensor_thread.start()
    time.sleep(1.0)

    with shared.lock:
        if shared.stop:
            raise SystemExit("capture failed during startup; see events in summary")

    print("\nPayload/CoG calibration plan (RG2 open, empty, no contact)")
    print("| segment | operator action |")
    print("|---|---|")
    for pose in pose_plan:
        print(f"| {pose['name']} | {pose['operator_action']} |")

    for idx, pose in enumerate(pose_plan):
        name = pose["name"]
        action = pose["operator_action"]
        if idx > 0:
            with shared.lock:
                shared.segment = "transition"
                shared.segment_index = idx
            input(f"\nMove to {name}: {action}\nPress Enter when stable: ")
            if args.settle_s > 0:
                print(f"Settling for {args.settle_s:.1f}s...")
                time.sleep(args.settle_s)
        capture_segment(shared, name, idx, args.segment_duration_s)
        with shared.lock:
            if shared.stop:
                break

    with shared.lock:
        shared.stop = True
    rtde_thread.join(timeout=3.0)
    sensor_thread.join(timeout=3.0)

    with shared.lock:
        summary = {
            "ok": not any(event["event"].endswith("_error") for event in shared.events),
            "output_dir": str(args.output_dir),
            "csv": str(csv_path),
            "raw_frames": str(raw_path),
            "sensor_samples": shared.sensor_samples,
            "rtde_samples": shared.rtde_samples,
            "parse_errors": shared.parse_errors,
            "dropped_sync_bytes": shared.dropped_sync_bytes,
            "events": shared.events,
            "segment_force_stats_si": {
                segment: {col: stats.as_dict() for col, stats in stats_by_col.items()}
                for segment, stats_by_col in shared.segment_stats.items()
            },
            "segment_rtde_first_last": shared.segment_first_last_rtde,
        }
    write_json(summary_path, summary)
    print(f"\nSaved: {args.output_dir}")
    print(f"CSV: {csv_path}")
    print(f"Summary: {summary_path}")
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
