#!/usr/bin/env python3
"""Interactive Kunwei gravity-axis calibration capture.

This logger is read-only with respect to the UR controller. It subscribes to
UR RTDE outputs and records Kunwei converted raw frames. It does not move the
robot, write RTDE inputs, call UR zero_ftsensor(), or send Kunwei zero/tare or
configuration commands.

It sends the documented Kunwei stream start command 0x48 AA 0D 0A unless
--no-start-command is set, and sends stop 0x43 AA 0D 0A on exit unless
--no-stop-command is set.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import signal
import socket
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


KUNWEI_TOOLS = Path("/home/andy/ur10e_ros2_ws/experiments/sensor-integration/kunwei-kwr75b/tools")
UR_REALSETUP_SCRIPTS = Path("/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts")
sys.path.insert(0, str(KUNWEI_TOOLS))
sys.path.insert(0, str(UR_REALSETUP_SCRIPTS))

from capture_kunwei_kwr75_1khz import (  # noqa: E402
    FORCE_KG_TO_N,
    MOMENT_KG_M_TO_NM,
    START_STREAM,
    STOP_STREAM,
    parse_frame,
    pop_frames,
)
from _ur_common import RTDEClient, dashboard_exchange  # noqa: E402


RTDE_FIELDS = [
    "actual_q",
    "actual_TCP_pose",
    "actual_TCP_speed",
    "runtime_state",
    "robot_mode",
    "safety_mode",
]
POSE_COLS = [f"ur_actual_TCP_pose_{idx}" for idx in range(6)]
Q_COLS = [f"ur_actual_q_{idx}" for idx in range(6)]
SPEED_COLS = [f"ur_actual_TCP_speed_{idx}" for idx in range(6)]
SI_COLS = ["fx_n", "fy_n", "fz_n", "mx_nm", "my_nm", "mz_nm"]
MANUAL_COLS = [
    "Fx_kg_manual",
    "Fy_kg_manual",
    "Fz_kg_manual",
    "Mx_kg_m_manual",
    "My_kg_m_manual",
    "Mz_kg_m_manual",
]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_output_dir() -> Path:
    return (
        Path("/home/andy/ur10e_ros2_ws/experiments/sensor-integration/kunwei-kwr75b/measurements")
        / "gravity_axis_calibration"
        / now_stamp()
    )


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.10g}" if math.isfinite(value) else ""
    return str(value)


class OnlineStats:
    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.min: float | None = None
        self.max: float | None = None

    def push(self, value: float) -> None:
        self.n += 1
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)
        delta = value - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (value - self.mean)

    def as_dict(self) -> dict[str, Any]:
        if not self.n:
            return {"samples": 0}
        variance = self.m2 / (self.n - 1) if self.n > 1 else 0.0
        return {
            "samples": self.n,
            "mean": self.mean,
            "std": math.sqrt(variance),
            "min": self.min,
            "max": self.max,
        }


class SharedState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop = False
        self.segment = "idle"
        self.segment_index = -1
        self.latest_rtde: dict[str, Any] = {}
        self.latest_rtde_mono: float | None = None
        self.rtde_samples = 0
        self.sensor_samples = 0
        self.parse_errors = 0
        self.dropped_sync_bytes = 0
        self.segment_stats: dict[str, dict[str, OnlineStats]] = defaultdict(
            lambda: {col: OnlineStats() for col in SI_COLS}
        )
        self.segment_first_last_rtde: dict[str, dict[str, Any]] = defaultdict(dict)
        self.events: list[dict[str, Any]] = []

    def set_segment(self, name: str, index: int) -> None:
        with self.lock:
            self.segment = name
            self.segment_index = index
            self.events.append(
                {
                    "event": "segment_start",
                    "segment": name,
                    "segment_index": index,
                    "t_wall": datetime.now().isoformat(timespec="milliseconds"),
                    "t_mono": time.monotonic(),
                }
            )

    def add_event(self, event: str, **kwargs: Any) -> None:
        with self.lock:
            self.events.append(
                {
                    "event": event,
                    "t_wall": datetime.now().isoformat(timespec="milliseconds"),
                    "t_mono": time.monotonic(),
                    **kwargs,
                }
            )


def rtde_worker(args: argparse.Namespace, shared: SharedState) -> None:
    try:
        with RTDEClient(args.robot_host, timeout=args.rtde_timeout_s) as client:
            client.negotiate()
            recipe, types = client.setup_outputs(args.rtde_hz, RTDE_FIELDS)
            client.start()
            while True:
                with shared.lock:
                    if shared.stop:
                        return
                values = client.recv_recipe_sample(recipe, types)
                now = time.monotonic()
                sample = {field: value for field, value in zip(RTDE_FIELDS, values)}
                with shared.lock:
                    shared.latest_rtde = sample
                    shared.latest_rtde_mono = now
                    shared.rtde_samples += 1
    except Exception as exc:
        shared.add_event("rtde_error", error=f"{type(exc).__name__}: {exc}")
        with shared.lock:
            shared.stop = True


def sensor_worker(args: argparse.Namespace, shared: SharedState, csv_path: Path, raw_path: Path) -> None:
    fieldnames = [
        "sample_index",
        "t_wall",
        "t_mono",
        "segment",
        "segment_index",
        "rtde_age_s",
        "rtde_samples",
        *MANUAL_COLS,
        *SI_COLS,
        *POSE_COLS,
        *Q_COLS,
        *SPEED_COLS,
        "ur_runtime_state",
        "ur_robot_mode",
        "ur_safety_mode",
    ]
    buffer = bytearray()
    try:
        with socket.create_connection((args.sensor_ip, args.sensor_port), timeout=args.connect_timeout_s) as sock:
            sock.settimeout(0.2)
            if not args.no_start_command:
                sock.sendall(START_STREAM)
                shared.add_event("kunwei_start_command_sent", command_hex=START_STREAM.hex(" "))
            with csv_path.open("w", newline="", encoding="utf-8") as csv_file, raw_path.open("wb") as raw_file:
                writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
                writer.writeheader()
                while True:
                    with shared.lock:
                        if shared.stop:
                            break
                    try:
                        chunk = sock.recv(8192)
                    except socket.timeout:
                        continue
                    if not chunk:
                        shared.add_event("kunwei_socket_closed")
                        break
                    buffer.extend(chunk)
                    frames, dropped = pop_frames(buffer, 0x48)
                    if dropped:
                        with shared.lock:
                            shared.dropped_sync_bytes += dropped
                    for frame in frames:
                        try:
                            raw_values = parse_frame(frame)
                        except ValueError:
                            with shared.lock:
                                shared.parse_errors += 1
                            continue
                        t_mono = time.monotonic()
                        si_values = [
                            raw_values[0] * FORCE_KG_TO_N,
                            raw_values[1] * FORCE_KG_TO_N,
                            raw_values[2] * FORCE_KG_TO_N,
                            raw_values[3] * MOMENT_KG_M_TO_NM,
                            raw_values[4] * MOMENT_KG_M_TO_NM,
                            raw_values[5] * MOMENT_KG_M_TO_NM,
                        ]
                        raw_file.write(frame)
                        with shared.lock:
                            shared.sensor_samples += 1
                            sample_index = shared.sensor_samples
                            segment = shared.segment
                            segment_index = shared.segment_index
                            latest_rtde = dict(shared.latest_rtde)
                            latest_rtde_mono = shared.latest_rtde_mono
                            rtde_samples = shared.rtde_samples
                            for col, value in zip(SI_COLS, si_values):
                                shared.segment_stats[segment][col].push(value)
                            if latest_rtde:
                                by_seg = shared.segment_first_last_rtde[segment]
                                by_seg.setdefault("first", latest_rtde)
                                by_seg["last"] = latest_rtde
                        pose = latest_rtde.get("actual_TCP_pose") or [""] * 6
                        q = latest_rtde.get("actual_q") or [""] * 6
                        speed = latest_rtde.get("actual_TCP_speed") or [""] * 6
                        row = {
                            "sample_index": sample_index,
                            "t_wall": datetime.now().isoformat(timespec="milliseconds"),
                            "t_mono": t_mono,
                            "segment": segment,
                            "segment_index": segment_index,
                            "rtde_age_s": "" if latest_rtde_mono is None else t_mono - latest_rtde_mono,
                            "rtde_samples": rtde_samples,
                            **dict(zip(MANUAL_COLS, raw_values)),
                            **dict(zip(SI_COLS, si_values)),
                            **dict(zip(POSE_COLS, pose)),
                            **dict(zip(Q_COLS, q)),
                            **dict(zip(SPEED_COLS, speed)),
                            "ur_runtime_state": latest_rtde.get("runtime_state", ""),
                            "ur_robot_mode": latest_rtde.get("robot_mode", ""),
                            "ur_safety_mode": latest_rtde.get("safety_mode", ""),
                        }
                        writer.writerow({key: csv_value(value) for key, value in row.items()})
                        if sample_index % args.flush_every == 0:
                            csv_file.flush()
                            raw_file.flush()
                csv_file.flush()
                raw_file.flush()
            if not args.no_stop_command:
                try:
                    sock.sendall(STOP_STREAM)
                    shared.add_event("kunwei_stop_command_sent", command_hex=STOP_STREAM.hex(" "))
                except OSError as exc:
                    shared.add_event("kunwei_stop_command_failed", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        shared.add_event("sensor_error", error=f"{type(exc).__name__}: {exc}")
        with shared.lock:
            shared.stop = True


def capture_segment(shared: SharedState, name: str, index: int, duration_s: float) -> None:
    shared.set_segment(name, index)
    print(f"\n[{name}] recording {duration_s:.1f}s. Hold still, no contact.")
    deadline = time.monotonic() + duration_s
    while time.monotonic() < deadline:
        time.sleep(0.2)
        with shared.lock:
            if shared.stop:
                return
    shared.add_event("segment_end", segment=name, segment_index=index)
    print(f"[{name}] done.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--rtde-hz", type=float, default=125.0)
    parser.add_argument("--rtde-timeout-s", type=float, default=3.0)
    parser.add_argument("--connect-timeout-s", type=float, default=4.0)
    parser.add_argument("--segment-duration-s", type=float, default=5.0)
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument("--flush-every", type=int, default=250)
    parser.add_argument("--no-start-command", action="store_true")
    parser.add_argument("--no-stop-command", action="store_true")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "gravity_axis_calibration.csv"
    raw_path = args.output_dir / "kunwei_raw_frames.bin"
    summary_path = args.output_dir / "summary.json"
    metadata_path = args.output_dir / "metadata.json"

    metadata = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "purpose": "Kunwei K raw frame to UR TCP/tool frame gravity-axis calibration",
        "safety": [
            "No robot motion is commanded by this script.",
            "No UR RTDE inputs are written.",
            "No UR zero_ftsensor call is made.",
            "No Kunwei zero/tare/config command is sent.",
            "Kunwei stream start/stop commands are sent unless disabled by CLI flags.",
        ],
        "segments": [
            {"name": "P0_wrist3_0_current", "operator_action": "current horizontal pose; hold still"},
            {"name": "P1_wrist3_plus_90", "operator_action": "rotate only wrist3 +90 deg from P0; hold still"},
            {"name": "P2_wrist3_plus_180", "operator_action": "rotate only wrist3 +180 deg from P0; hold still"},
            {"name": "P3_wrist3_plus_270", "operator_action": "rotate only wrist3 +270 deg from P0; hold still"},
            {"name": "P4_return_P0", "operator_action": "return wrist3 to P0; hold still"},
        ],
        "args": vars(args) | {"output_dir": str(args.output_dir)},
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
        dash = dashboard_exchange(args.robot_host, ["robotmode", "safetymode", "programState"], timeout=2.0)
        print("Dashboard:", dash)
        shared.add_event("dashboard_preflight", responses=dash)
    except Exception as exc:
        print(f"Dashboard preflight warning: {type(exc).__name__}: {exc}")
        shared.add_event("dashboard_preflight_warning", error=f"{type(exc).__name__}: {exc}")

    rtde_thread = threading.Thread(target=rtde_worker, args=(args, shared), daemon=True)
    sensor_thread = threading.Thread(target=sensor_worker, args=(args, shared, csv_path, raw_path), daemon=True)
    rtde_thread.start()
    sensor_thread.start()
    time.sleep(1.0)

    with shared.lock:
        if shared.stop:
            raise SystemExit("capture failed during startup; see events in summary")

    plan = [
        ("P0_wrist3_0_current", "Hold current horizontal wrist3=0 pose."),
        ("P1_wrist3_plus_90", "Move only wrist3 to P0 + 90 deg, then press Enter."),
        ("P2_wrist3_plus_180", "Move only wrist3 to P0 + 180 deg, then press Enter."),
        ("P3_wrist3_plus_270", "Move only wrist3 to P0 + 270 deg, then press Enter."),
        ("P4_return_P0", "Return only wrist3 to P0, then press Enter."),
    ]

    print("\nGravity axis calibration plan")
    print("| segment | operator action |")
    print("|---|---|")
    for name, action in plan:
        print(f"| {name} | {action} |")

    for idx, (name, action) in enumerate(plan):
        if idx > 0:
            with shared.lock:
                shared.segment = "transition"
                shared.segment_index = idx
            input(f"\nMove to {name}: {action}\nPress Enter when stable: ")
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
            "ok": True,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
