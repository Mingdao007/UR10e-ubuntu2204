#!/usr/bin/env python3
"""Capture UR actual_TCP_force, OnRobot URCap registers, and OnRobot UDP together.

Safety boundary for live use:
- no robot motion commands
- no URScript upload/run
- no UR zero_ftsensor()
- no TCP/payload writes
- no OnRobot BIAS or FILTER commands
- sends OnRobot UDP SPEED, START, and final STOP only when requested
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import socket
import statistics
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


UR_REALSETUP_SCRIPTS = Path("/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts")
ONROBOT_TOOLS = Path(
    "/home/andy/ur10e_ros2_ws/ft_sensor/archive/onrobot/hex_e_v2_3010007655/tools"
)
sys.path.insert(0, str(UR_REALSETUP_SCRIPTS))
sys.path.insert(0, str(ONROBOT_TOOLS))

from _onrobot_highspeed_udp import (  # noqa: E402
    COMMAND_SPEED,
    COMMAND_START,
    COMMAND_STOP,
    UDP_PORT,
    make_udp_socket,
    receive_sample,
    sample_to_row,
    send_command,
)
from _ur_common import RTDEClient, dashboard_exchange, load_defaults, probe_port  # noqa: E402


AXES_FORCE = ["fx_n", "fy_n", "fz_n"]
AXES_TORQUE = ["tx_nm", "ty_nm", "tz_nm"]
AXES_ALL = [*AXES_FORCE, *AXES_TORQUE]
RTDE_UR_FIELDS = [
    "ur_fx_n",
    "ur_fy_n",
    "ur_fz_n",
    "ur_tx_nm",
    "ur_ty_nm",
    "ur_tz_nm",
]
RTDE_SPEED_FIELDS = [
    "ur_vx_mps",
    "ur_vy_mps",
    "ur_vz_mps",
    "ur_wx_radps",
    "ur_wy_radps",
    "ur_wz_radps",
]
RTDE_URCAP_FIELDS = [
    "urcap_fx_n",
    "urcap_fy_n",
    "urcap_fz_n",
    "urcap_tx_nm",
    "urcap_ty_nm",
    "urcap_tz_nm",
]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_output_dir() -> Path:
    return (
        Path("/home/andy/ur10e_ros2_ws/experiments")
        / "20260528_onrobot_three_stream_600s_first_zero"
        / f"run_{now_stamp()}"
    )


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def timing_stats(times: list[float]) -> dict[str, Any]:
    if len(times) < 2:
        return {
            "samples": len(times),
            "first_t_s": times[0] if times else None,
            "last_t_s": times[-1] if times else None,
            "duration_first_last_s": None,
            "interval_rate_hz": None,
        }
    dts = [b - a for a, b in zip(times, times[1:])]
    duration = times[-1] - times[0]
    return {
        "samples": len(times),
        "first_t_s": times[0],
        "last_t_s": times[-1],
        "duration_first_last_s": duration,
        "interval_rate_hz": (len(times) - 1) / duration if duration else None,
        "sample_count_over_duration_hz": len(times) / duration if duration else None,
        "mean_dt_ms": statistics.fmean(dts) * 1000.0,
        "median_dt_ms": statistics.median(dts) * 1000.0,
        "p95_dt_ms": percentile(dts, 0.95) * 1000.0,
        "p99_dt_ms": percentile(dts, 0.99) * 1000.0,
        "min_dt_ms": min(dts) * 1000.0,
        "max_dt_ms": max(dts) * 1000.0,
    }


def vector_runs(rows: list[dict[str, float]], fields: list[str]) -> dict[str, Any]:
    if not rows:
        return {"runs": 0, "distinct_value_transition_rate_hz": None}
    run_lengths: list[int] = []
    previous = tuple(rows[0][field] for field in fields)
    current_len = 1
    for row in rows[1:]:
        current = tuple(row[field] for field in fields)
        if current == previous:
            current_len += 1
        else:
            run_lengths.append(current_len)
            previous = current
            current_len = 1
    run_lengths.append(current_len)
    duration = rows[-1]["t_s"] - rows[0]["t_s"] if len(rows) >= 2 else 0.0
    return {
        "runs": len(run_lengths),
        "distinct_value_transition_rate_hz": (len(run_lengths) - 1) / duration
        if duration
        else None,
        "mean_rows_per_run": statistics.fmean(run_lengths),
        "median_rows_per_run": statistics.median(run_lengths),
        "max_rows_per_run": max(run_lengths),
        "run_length_counts_top10": Counter(run_lengths).most_common(10),
    }


def axis_stats(rows: list[dict[str, float]], prefix: str, fields: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in fields:
        values = [row[field] for row in rows]
        if not values:
            out[field] = None
            continue
        first = values[0]
        zeroed = [value - first for value in values]
        out[field] = {
            "first": first,
            "last": values[-1],
            "last_minus_first": values[-1] - first,
            "zeroed_mean": statistics.fmean(zeroed),
            "zeroed_std": statistics.stdev(zeroed) if len(zeroed) >= 2 else None,
            "zeroed_min": min(zeroed),
            "zeroed_max": max(zeroed),
        }
    return {prefix: out}


def speed_norm_stats(rows: list[dict[str, float]]) -> dict[str, Any]:
    linear = [
        math.sqrt(row["ur_vx_mps"] ** 2 + row["ur_vy_mps"] ** 2 + row["ur_vz_mps"] ** 2)
        for row in rows
    ]
    angular = [
        math.sqrt(
            row["ur_wx_radps"] ** 2 + row["ur_wy_radps"] ** 2 + row["ur_wz_radps"] ** 2
        )
        for row in rows
    ]
    return {
        "linear_max": max(linear) if linear else None,
        "linear_mean": statistics.fmean(linear) if linear else None,
        "angular_max": max(angular) if angular else None,
        "angular_mean": statistics.fmean(angular) if angular else None,
    }


def read_csv_float_rows(path: Path) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append({key: float(value) for key, value in row.items()})
    return rows


def sequence_delta_counts(values: list[int], modulo: int | None = None) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for previous, current in zip(values, values[1:]):
        if modulo is None:
            delta = current - previous
        else:
            delta = (current - previous) % modulo
        counts[str(delta)] += 1
    return dict(counts)


def downsample_xy(x: list[float], y: list[float], max_points: int) -> tuple[list[float], list[float]]:
    if len(x) <= max_points:
        return x, y
    step = math.ceil(len(x) / max_points)
    return x[::step], y[::step]


def plot_zeroed(rtde_rows: list[dict[str, float]], udp_rows: list[dict[str, float]], out_dir: Path, stem: str) -> dict[str, str]:
    plot_dir = out_dir / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    def rtde_zero(field: str) -> tuple[list[float], list[float]]:
        xs = [row["t_s"] for row in rtde_rows]
        first = rtde_rows[0][field]
        ys = [row[field] - first for row in rtde_rows]
        return downsample_xy(xs, ys, 25000)

    def udp_zero(field: str) -> tuple[list[float], list[float]]:
        xs = [row["t_s"] for row in udp_rows]
        first = udp_rows[0][field]
        ys = [row[field] - first for row in udp_rows]
        return downsample_xy(xs, ys, 25000)

    fig, ax = plt.subplots(figsize=(12, 5.6), dpi=160)
    for label, field, color in [
        ("UR actual_TCP_force Fz - first", "ur_fz_n", "#2f6fbb"),
        ("OnRobot URCap Fz - first", "urcap_fz_n", "#b36b00"),
    ]:
        x, y = rtde_zero(field)
        ax.plot(x, y, linewidth=0.8, label=label, color=color)
    x, y = udp_zero("fz_n")
    ax.plot(x, y, linewidth=0.8, label="OnRobot UDP raw Fz - first", color="#287a3e")
    ax.set_title("First-sample-zeroed Fz comparison, 600 s")
    ax.set_xlabel("Time since launcher start (s)")
    ax.set_ylabel("Fz - first sample (N)")
    ax.grid(True, color="#dddddd", linewidth=0.5)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fz_path = plot_dir / f"{stem}_first_zero_fz_overlay.png"
    fig.savefig(fz_path)
    plt.close(fig)
    paths["first_zero_fz_overlay_png"] = str(fz_path)

    fig, axes = plt.subplots(3, 1, figsize=(12, 9), dpi=160, sharex=True)
    for ax, axis, ylabel in zip(axes, ["fx", "fy", "fz"], ["Fx - first (N)", "Fy - first (N)", "Fz - first (N)"]):
        for label, field, color in [
            ("UR actual_TCP_force", f"ur_{axis}_n", "#2f6fbb"),
            ("OnRobot URCap", f"urcap_{axis}_n", "#b36b00"),
        ]:
            x, y = rtde_zero(field)
            ax.plot(x, y, linewidth=0.7, label=label, color=color)
        x, y = udp_zero(f"{axis}_n")
        ax.plot(x, y, linewidth=0.7, label="OnRobot UDP raw", color="#287a3e")
        ax.set_ylabel(ylabel)
        ax.grid(True, color="#dddddd", linewidth=0.5)
    axes[0].set_title("First-sample-zeroed force-axis comparison, 600 s")
    axes[-1].set_xlabel("Time since launcher start (s)")
    axes[0].legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    force_path = plot_dir / f"{stem}_first_zero_force_axes.png"
    fig.savefig(force_path)
    plt.close(fig)
    paths["first_zero_force_axes_png"] = str(force_path)
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument("--rtde-hz", type=float, default=500.0)
    parser.add_argument("--udp-speed-divisor", type=int, default=2)
    parser.add_argument("--udp-timeout-s", type=float, default=3.0)
    parser.add_argument("--rtde-timeout-s", type=float, default=3.0)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--prefix", default="three_stream_600s")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--defaults", default=None)
    return parser


def dashboard_snapshot(host: str, port: int) -> dict[str, str] | dict[str, str]:
    try:
        return dashboard_exchange(
            host,
            ["robotmode", "safetymode", "running", "programState", "is in remote control"],
            port=port,
            timeout=2.0,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def collect(args: argparse.Namespace) -> int:
    defaults = load_defaults(args.defaults)
    robot_host = defaults["robot_host"]
    rtde_port = defaults["rtde_port"]
    compute_box_host = defaults["onrobot_compute_box_host"]
    udp_port = defaults["onrobot_udp_highspeed_port"]
    dash_port = defaults["dashboard_port"]

    if args.confirm != "SPEED2_600S_THREE_STREAM":
        raise SystemExit("missing --confirm SPEED2_600S_THREE_STREAM")
    if args.seconds <= 0:
        raise SystemExit("--seconds must be positive")
    if args.udp_speed_divisor <= 0:
        raise SystemExit("--udp-speed-divisor must be positive")

    output_dir = args.output_dir or default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    stem = f"{args.prefix}_{stamp}"
    rtde_csv = output_dir / f"{stem}_rtde_ur500_urcap125.csv"
    udp_csv = output_dir / f"{stem}_onrobot_udp500_raw.csv"
    summary_path = output_dir / f"{stem}_summary.json"

    dashboard_before = dashboard_snapshot(robot_host, dash_port)
    port_probe = {
        "rtde": probe_port(robot_host, rtde_port, timeout=1.0),
        "dashboard": probe_port(robot_host, dash_port, timeout=1.0),
        "onrobot_tcp": probe_port(compute_box_host, defaults["onrobot_tcp_daq_port"], timeout=1.0),
        "onrobot_udp": {
            "host": compute_box_host,
            "port": udp_port,
            "udp_connect_checked": True,
        },
    }

    started_wall = datetime.now().isoformat(timespec="seconds")
    start_ns_box: dict[str, int] = {}
    start_event = threading.Event()
    rtde_ready = threading.Event()
    udp_ready = threading.Event()
    errors: list[str] = []
    udp_commands: list[dict[str, Any]] = []
    rtde_rows_in_memory: list[dict[str, float]] = []
    udp_rows_in_memory: list[dict[str, float]] = []

    rtde_fields = [
        "actual_TCP_force",
        "actual_TCP_speed",
        *[f"output_double_register_{idx}" for idx in range(24, 30)],
    ]
    rtde_fieldnames = ["sample_index", "t_s", *RTDE_UR_FIELDS, *RTDE_SPEED_FIELDS, *RTDE_URCAP_FIELDS]
    udp_fieldnames = [
        "sample_index",
        "t_s",
        "recv_latency_ms",
        "sequence_number",
        "sample_counter",
        "status",
        *AXES_ALL,
        "fx_raw",
        "fy_raw",
        "fz_raw",
        "tx_raw",
        "ty_raw",
        "tz_raw",
    ]

    def rtde_worker() -> None:
        try:
            with RTDEClient(robot_host, port=rtde_port, timeout=args.rtde_timeout_s) as client:
                client.negotiate()
                recipe_id, type_names = client.setup_outputs(args.rtde_hz, rtde_fields)
                client.start()
                rtde_ready.set()
                start_event.wait()
                with rtde_csv.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=rtde_fieldnames)
                    writer.writeheader()
                    idx = 0
                    while True:
                        values = client.recv_recipe_sample(recipe_id, type_names)
                        t_s = (time.perf_counter_ns() - start_ns_box["start_ns"]) / 1e9
                        tcp_force = values[0]
                        tcp_speed = values[1]
                        urcap = values[2:]
                        row = {
                            "sample_index": idx,
                            "t_s": t_s,
                            **dict(zip(RTDE_UR_FIELDS, tcp_force)),
                            **dict(zip(RTDE_SPEED_FIELDS, tcp_speed)),
                            **dict(zip(RTDE_URCAP_FIELDS, urcap)),
                        }
                        writer.writerow(row)
                        rtde_rows_in_memory.append(row)
                        idx += 1
                        if t_s >= args.seconds:
                            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"rtde_worker {type(exc).__name__}: {exc}")
            rtde_ready.set()

    def udp_worker() -> None:
        started_stream = False
        try:
            with make_udp_socket(compute_box_host, udp_port, args.udp_timeout_s) as sock:
                udp_ready.set()
                start_event.wait()
                udp_commands.append(send_command(sock, COMMAND_SPEED, args.udp_speed_divisor))
                start_count = int(max(args.seconds * 500 * 1.2, args.seconds * 500 + 5000))
                udp_commands.append(send_command(sock, COMMAND_START, start_count))
                started_stream = True
                with udp_csv.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=udp_fieldnames)
                    writer.writeheader()
                    idx = 0
                    while True:
                        before_ns = time.perf_counter_ns()
                        if (before_ns - start_ns_box["start_ns"]) / 1e9 >= args.seconds:
                            break
                        try:
                            sample = receive_sample(sock)
                        except socket.timeout as exc:
                            errors.append(f"udp_worker socket.timeout: {exc}")
                            break
                        after_ns = time.perf_counter_ns()
                        t_s = (after_ns - start_ns_box["start_ns"]) / 1e9
                        latency_ms = (after_ns - before_ns) / 1e6
                        row = sample_to_row(idx, t_s, latency_ms, sample)
                        numeric_row = {key: float(value) for key, value in row.items()}
                        writer.writerow(row)
                        udp_rows_in_memory.append(numeric_row)
                        idx += 1
                udp_commands.append(send_command(sock, COMMAND_STOP, 0))
                started_stream = False
        except Exception as exc:  # noqa: BLE001
            errors.append(f"udp_worker {type(exc).__name__}: {exc}")
            udp_ready.set()
        finally:
            if started_stream:
                try:
                    with make_udp_socket(compute_box_host, udp_port, args.udp_timeout_s) as stop_sock:
                        udp_commands.append(send_command(stop_sock, COMMAND_STOP, 0))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"udp final STOP failed {type(exc).__name__}: {exc}")

    rtde_thread = threading.Thread(target=rtde_worker, name="rtde_worker", daemon=True)
    udp_thread = threading.Thread(target=udp_worker, name="udp_worker", daemon=True)
    rtde_thread.start()
    udp_thread.start()
    rtde_ready.wait(timeout=10.0)
    udp_ready.wait(timeout=10.0)
    if not rtde_ready.is_set() or not udp_ready.is_set():
        errors.append("not all workers became ready within 10 s")
    start_ns_box["start_ns"] = time.perf_counter_ns()
    start_event.set()
    rtde_thread.join(timeout=args.seconds + 30)
    udp_thread.join(timeout=args.seconds + 30)
    if rtde_thread.is_alive():
        errors.append("rtde_worker still alive after join timeout")
    if udp_thread.is_alive():
        errors.append("udp_worker still alive after join timeout")

    dashboard_after = dashboard_snapshot(robot_host, dash_port)

    # If memory was lost due to a future refactor, fall back to CSV reads.
    rtde_rows = rtde_rows_in_memory if rtde_rows_in_memory else read_csv_float_rows(rtde_csv)
    udp_rows = udp_rows_in_memory if udp_rows_in_memory else read_csv_float_rows(udp_csv)
    plot_paths = plot_zeroed(rtde_rows, udp_rows, output_dir, stem) if rtde_rows and udp_rows else {}

    udp_seq = [int(row["sequence_number"]) for row in udp_rows]
    udp_counter = [int(row["sample_counter"]) for row in udp_rows]
    summary = {
        "ok": not errors,
        "label": "three_stream_600s_first_sample_zero_speed2",
        "requested_seconds": args.seconds,
        "rtde_requested_hz": args.rtde_hz,
        "udp_speed_divisor_sent": args.udp_speed_divisor,
        "started_wall": started_wall,
        "finished_wall": datetime.now().isoformat(timespec="seconds"),
        "output_dir": str(output_dir),
        "csv_paths": {
            "rtde_ur500_urcap125": str(rtde_csv),
            "onrobot_udp500_raw": str(udp_csv),
        },
        "plot_paths": plot_paths,
        "summary_path": str(summary_path),
        "dashboard_before": dashboard_before,
        "dashboard_after": dashboard_after,
        "port_probe": port_probe,
        "udp_commands_sent": udp_commands,
        "safety_boundary": [
            "no robot motion commands",
            "no URScript upload/run",
            "no UR zero_ftsensor()",
            "no UR TCP/payload writes",
            "no OnRobot BIAS command",
            "no OnRobot FILTER command",
            "OnRobot UDP SPEED/START/final STOP only",
            "analysis zero is first-sample subtraction only",
        ],
        "errors": errors,
        "rtde_timing": timing_stats([row["t_s"] for row in rtde_rows]),
        "udp_timing": timing_stats([row["t_s"] for row in udp_rows]),
        "ur_actual_tcp_force_tuple_updates": vector_runs(rtde_rows, RTDE_UR_FIELDS),
        "urcap_register_tuple_updates": vector_runs(rtde_rows, RTDE_URCAP_FIELDS),
        "udp_raw_tuple_updates": vector_runs(udp_rows, AXES_ALL),
        "udp_sequence_delta_counts": sequence_delta_counts(udp_seq),
        "udp_sample_counter_delta_mod65536_counts": sequence_delta_counts(udp_counter, modulo=65536),
        "udp_status_counts": dict(Counter(str(int(row["status"])) for row in udp_rows)),
        "first_sample_zero_reference": {
            "ur_actual_tcp_force": {field: rtde_rows[0][field] for field in RTDE_UR_FIELDS}
            if rtde_rows
            else None,
            "onrobot_urcap_registers": {field: rtde_rows[0][field] for field in RTDE_URCAP_FIELDS}
            if rtde_rows
            else None,
            "onrobot_udp_raw": {field: udp_rows[0][field] for field in AXES_ALL}
            if udp_rows
            else None,
        },
        "zeroed_axis_stats": {
            **axis_stats(rtde_rows, "ur_actual_tcp_force", RTDE_UR_FIELDS),
            **axis_stats(rtde_rows, "onrobot_urcap_registers", RTDE_URCAP_FIELDS),
            **axis_stats(udp_rows, "onrobot_udp_raw", AXES_ALL),
        },
        "ur_tcp_speed_norm": speed_norm_stats(rtde_rows),
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 2


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return collect(args)


if __name__ == "__main__":
    raise SystemExit(main())
