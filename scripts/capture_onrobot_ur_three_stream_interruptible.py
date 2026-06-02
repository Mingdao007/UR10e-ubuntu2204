#!/usr/bin/env python3
"""Interruptible long capture for UR + OnRobot three-stream drift checks.

This variant is built for hour-scale runs. It streams CSV rows to disk, writes
periodic checkpoints, handles Ctrl+C/SIGTERM by asking both workers to stop,
sends OnRobot UDP STOP in cleanup, then summarizes and plots whatever duration
was captured.

Safety boundary for live use:
- no robot motion commands
- no URScript upload/run
- no UR zero_ftsensor()
- no TCP/payload writes
- no OnRobot BIAS or FILTER commands
- sends OnRobot UDP SPEED, START, and final STOP only after explicit confirm
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import signal
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
    "/home/andy/ur10e_ros2_ws/ft_sensor/onrobot/hex_e_v2_3010007655/tools"
)
sys.path.insert(0, str(UR_REALSETUP_SCRIPTS))
sys.path.insert(0, str(ONROBOT_TOOLS))

from _onrobot_highspeed_udp import (  # noqa: E402
    COMMAND_SPEED,
    COMMAND_START,
    COMMAND_STOP,
    make_udp_socket,
    receive_sample,
    sample_to_row,
    send_command,
)
from _ur_common import RTDEClient, dashboard_exchange, load_defaults, probe_port  # noqa: E402


RTDE_UR_FIELDS = ["ur_fx_n", "ur_fy_n", "ur_fz_n", "ur_tx_nm", "ur_ty_nm", "ur_tz_nm"]
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
UDP_VALUE_FIELDS = ["fx_n", "fy_n", "fz_n", "tx_nm", "ty_nm", "tz_nm"]
UDP_RAW_FIELDS = ["fx_raw", "fy_raw", "fz_raw", "tx_raw", "ty_raw", "tz_raw"]


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_output_dir() -> Path:
    return (
        Path("/home/andy/ur10e_ros2_ws/experiments")
        / f"{datetime.now():%Y%m%d}_onrobot_three_stream_interruptible_drift"
        / f"run_{now_stamp()}"
    )


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    values.sort()
    rank = (len(values) - 1) * p
    low = int(rank)
    high = min(low + 1, len(values) - 1)
    frac = rank - low
    return values[low] * (1.0 - frac) + values[high] * frac


class RunningStats:
    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.min: float | None = None
        self.max: float | None = None
        self.first: float | None = None
        self.last: float | None = None

    def push(self, value: float) -> None:
        if self.first is None:
            self.first = value
        self.last = value
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (value - self.mean)

    def as_dict(self) -> dict[str, Any]:
        if self.n == 0:
            return {"samples": 0}
        variance = self.m2 / (self.n - 1) if self.n >= 2 else 0.0
        return {
            "samples": self.n,
            "first": self.first,
            "last": self.last,
            "last_minus_first": None if self.first is None or self.last is None else self.last - self.first,
            "mean": self.mean,
            "std": math.sqrt(variance),
            "min": self.min,
            "max": self.max,
        }


class ZeroedStats:
    def __init__(self) -> None:
        self.first: float | None = None
        self.stats = RunningStats()
        self.zero_min: float | None = None
        self.zero_max: float | None = None

    def push(self, value: float) -> None:
        if self.first is None:
            self.first = value
        zeroed = value - self.first
        self.stats.push(zeroed)
        self.zero_min = zeroed if self.zero_min is None else min(self.zero_min, zeroed)
        self.zero_max = zeroed if self.zero_max is None else max(self.zero_max, zeroed)
        self.last = value

    def as_dict(self) -> dict[str, Any]:
        out = self.stats.as_dict()
        out["first"] = self.first
        out["last"] = getattr(self, "last", None)
        out["last_minus_first"] = None if self.first is None or getattr(self, "last", None) is None else self.last - self.first
        out["zeroed_mean"] = out.pop("mean", None)
        out["zeroed_std"] = out.pop("std", None)
        out["zeroed_min"] = self.zero_min
        out["zeroed_max"] = self.zero_max
        return out


def csv_float(value: str) -> float:
    return float(value)


def timing_summary(times: list[float]) -> dict[str, Any]:
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
        "p95_dt_ms": percentile(dts[:], 0.95) * 1000.0,
        "p99_dt_ms": percentile(dts[:], 0.99) * 1000.0,
        "min_dt_ms": min(dts) * 1000.0,
        "max_dt_ms": max(dts) * 1000.0,
    }


def run_length_summary(run_lengths: Counter[int], runs: int, duration_s: float | None) -> dict[str, Any]:
    if runs == 0:
        return {"runs": 0, "distinct_value_transition_rate_hz": None}
    total_rows = sum(length * count for length, count in run_lengths.items())
    expanded_for_median: list[int] = []
    for length, count in run_lengths.items():
        expanded_for_median.extend([length] * min(count, 100000))
    return {
        "runs": runs,
        "distinct_value_transition_rate_hz": (runs - 1) / duration_s if duration_s else None,
        "mean_rows_per_run": total_rows / runs,
        "median_rows_per_run": statistics.median(expanded_for_median) if expanded_for_median else None,
        "max_rows_per_run": max(run_lengths) if run_lengths else None,
        "run_length_counts_top10": run_lengths.most_common(10),
    }


def summarize_rtde_csv(path: Path) -> dict[str, Any]:
    times: list[float] = []
    ur_zero = {field: ZeroedStats() for field in RTDE_UR_FIELDS}
    urcap_zero = {field: ZeroedStats() for field in RTDE_URCAP_FIELDS}
    speed_linear = RunningStats()
    speed_angular = RunningStats()
    ur_runs = Counter()
    urcap_runs = Counter()
    previous_ur: tuple[float, ...] | None = None
    previous_urcap: tuple[float, ...] | None = None
    current_ur_len = 0
    current_urcap_len = 0

    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            t = csv_float(row["t_s"])
            times.append(t)
            for field in RTDE_UR_FIELDS:
                ur_zero[field].push(csv_float(row[field]))
            for field in RTDE_URCAP_FIELDS:
                urcap_zero[field].push(csv_float(row[field]))
            linear = math.sqrt(
                csv_float(row["ur_vx_mps"]) ** 2
                + csv_float(row["ur_vy_mps"]) ** 2
                + csv_float(row["ur_vz_mps"]) ** 2
            )
            angular = math.sqrt(
                csv_float(row["ur_wx_radps"]) ** 2
                + csv_float(row["ur_wy_radps"]) ** 2
                + csv_float(row["ur_wz_radps"]) ** 2
            )
            speed_linear.push(linear)
            speed_angular.push(angular)

            ur_tuple = tuple(csv_float(row[field]) for field in RTDE_UR_FIELDS)
            urcap_tuple = tuple(csv_float(row[field]) for field in RTDE_URCAP_FIELDS)
            if previous_ur is None or ur_tuple != previous_ur:
                if current_ur_len:
                    ur_runs[current_ur_len] += 1
                previous_ur = ur_tuple
                current_ur_len = 1
            else:
                current_ur_len += 1
            if previous_urcap is None or urcap_tuple != previous_urcap:
                if current_urcap_len:
                    urcap_runs[current_urcap_len] += 1
                previous_urcap = urcap_tuple
                current_urcap_len = 1
            else:
                current_urcap_len += 1
    if current_ur_len:
        ur_runs[current_ur_len] += 1
    if current_urcap_len:
        urcap_runs[current_urcap_len] += 1
    duration = (times[-1] - times[0]) if len(times) >= 2 else None
    return {
        "rtde_timing": timing_summary(times),
        "ur_actual_tcp_force_tuple_updates": run_length_summary(ur_runs, sum(ur_runs.values()), duration),
        "urcap_register_tuple_updates": run_length_summary(urcap_runs, sum(urcap_runs.values()), duration),
        "zeroed_axis_stats": {
            "ur_actual_tcp_force": {field: stat.as_dict() for field, stat in ur_zero.items()},
            "onrobot_urcap_registers": {field: stat.as_dict() for field, stat in urcap_zero.items()},
        },
        "first_sample_zero_reference": {
            "ur_actual_tcp_force": {field: stat.first for field, stat in ur_zero.items()},
            "onrobot_urcap_registers": {field: stat.first for field, stat in urcap_zero.items()},
        },
        "ur_tcp_speed_norm": {
            "linear": speed_linear.as_dict(),
            "angular": speed_angular.as_dict(),
        },
    }


def summarize_udp_csv(path: Path) -> dict[str, Any]:
    times: list[float] = []
    udp_zero = {field: ZeroedStats() for field in UDP_VALUE_FIELDS}
    tuple_runs = Counter()
    previous_tuple: tuple[float, ...] | None = None
    current_len = 0
    status_counts: Counter[str] = Counter()
    seq_deltas: Counter[str] = Counter()
    counter_deltas: Counter[str] = Counter()
    previous_seq: int | None = None
    previous_counter: int | None = None

    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            times.append(csv_float(row["t_s"]))
            for field in UDP_VALUE_FIELDS:
                udp_zero[field].push(csv_float(row[field]))
            status_counts[str(int(csv_float(row["status"])))] += 1
            seq = int(csv_float(row["sequence_number"]))
            counter = int(csv_float(row["sample_counter"]))
            if previous_seq is not None:
                seq_deltas[str(seq - previous_seq)] += 1
            if previous_counter is not None:
                counter_deltas[str((counter - previous_counter) % 65536)] += 1
            previous_seq = seq
            previous_counter = counter

            value_tuple = tuple(csv_float(row[field]) for field in UDP_VALUE_FIELDS)
            if previous_tuple is None or value_tuple != previous_tuple:
                if current_len:
                    tuple_runs[current_len] += 1
                previous_tuple = value_tuple
                current_len = 1
            else:
                current_len += 1
    if current_len:
        tuple_runs[current_len] += 1
    duration = (times[-1] - times[0]) if len(times) >= 2 else None
    return {
        "udp_timing": timing_summary(times),
        "udp_raw_tuple_updates": run_length_summary(tuple_runs, sum(tuple_runs.values()), duration),
        "udp_status_counts": dict(status_counts),
        "udp_sequence_delta_counts": dict(seq_deltas),
        "udp_sample_counter_delta_mod65536_counts": dict(counter_deltas),
        "zeroed_axis_stats": {
            "onrobot_udp_raw": {field: stat.as_dict() for field, stat in udp_zero.items()}
        },
        "first_sample_zero_reference": {
            "onrobot_udp_raw": {field: stat.first for field, stat in udp_zero.items()}
        },
    }


def read_downsample(path: Path, fields: list[str], max_points: int) -> dict[str, list[float]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = sum(1 for _ in handle) - 1
    step = max(1, math.ceil(rows / max_points))
    out = {"t_s": []}
    for field in fields:
        out[field] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for idx, row in enumerate(csv.DictReader(handle)):
            if idx % step:
                continue
            out["t_s"].append(csv_float(row["t_s"]))
            for field in fields:
                out[field].append(csv_float(row[field]))
    return out


def zeroed_series(data: dict[str, list[float]], field: str) -> list[float]:
    values = data[field]
    first = values[0] if values else 0.0
    return [value - first for value in values]


def plot_zeroed(rtde_csv: Path, udp_csv: Path, output_dir: Path, stem: str) -> dict[str, str]:
    plot_dir = output_dir / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    rtde = read_downsample(
        rtde_csv,
        ["ur_fx_n", "ur_fy_n", "ur_fz_n", "urcap_fx_n", "urcap_fy_n", "urcap_fz_n"],
        max_points=30000,
    )
    udp = read_downsample(udp_csv, ["fx_n", "fy_n", "fz_n"], max_points=30000)

    fig, ax = plt.subplots(figsize=(12, 5.6), dpi=160)
    ax.plot(rtde["t_s"], zeroed_series(rtde, "ur_fz_n"), linewidth=0.8, label="UR actual_TCP_force Fz - first")
    ax.plot(rtde["t_s"], zeroed_series(rtde, "urcap_fz_n"), linewidth=0.8, label="OnRobot URCap Fz - first")
    ax.plot(udp["t_s"], zeroed_series(udp, "fz_n"), linewidth=0.8, label="OnRobot UDP raw Fz - first")
    ax.set_title("First-sample-zeroed Fz comparison")
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
        ax.plot(rtde["t_s"], zeroed_series(rtde, f"ur_{axis}_n"), linewidth=0.7, label="UR actual_TCP_force")
        ax.plot(rtde["t_s"], zeroed_series(rtde, f"urcap_{axis}_n"), linewidth=0.7, label="OnRobot URCap")
        ax.plot(udp["t_s"], zeroed_series(udp, f"{axis}_n"), linewidth=0.7, label="OnRobot UDP raw")
        ax.set_ylabel(ylabel)
        ax.grid(True, color="#dddddd", linewidth=0.5)
    axes[0].set_title("First-sample-zeroed force-axis comparison")
    axes[-1].set_xlabel("Time since launcher start (s)")
    axes[0].legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    force_path = plot_dir / f"{stem}_first_zero_force_axes.png"
    fig.savefig(force_path)
    plt.close(fig)
    paths["first_zero_force_axes_png"] = str(force_path)
    return paths


def dashboard_snapshot(host: str, port: int) -> dict[str, str]:
    try:
        return dashboard_exchange(
            host,
            ["robotmode", "safetymode", "running", "programState", "is in remote control"],
            port=port,
            timeout=2.0,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=7200.0)
    parser.add_argument("--rtde-hz", type=float, default=500.0)
    parser.add_argument("--udp-speed-divisor", type=int, default=2)
    parser.add_argument("--udp-timeout-s", type=float, default=3.0)
    parser.add_argument("--rtde-timeout-s", type=float, default=3.0)
    parser.add_argument("--checkpoint-interval-s", type=float, default=300.0)
    parser.add_argument("--flush-every", type=int, default=1000)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--prefix", default="three_stream_interruptible")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--defaults", default=None)
    return parser


def collect(args: argparse.Namespace) -> int:
    defaults = load_defaults(args.defaults)
    robot_host = defaults["robot_host"]
    rtde_port = defaults["rtde_port"]
    compute_box_host = defaults["onrobot_compute_box_host"]
    udp_port = defaults["onrobot_udp_highspeed_port"]
    dash_port = defaults["dashboard_port"]

    if args.confirm != "INTERRUPTIBLE_THREE_STREAM_SPEED2":
        raise SystemExit("missing --confirm INTERRUPTIBLE_THREE_STREAM_SPEED2")
    if args.seconds <= 0:
        raise SystemExit("--seconds must be positive")
    if args.flush_every <= 0:
        raise SystemExit("--flush-every must be positive")

    output_dir = args.output_dir or default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    stem = f"{args.prefix}_{stamp}"
    rtde_csv = output_dir / f"{stem}_rtde_ur500_urcap125.csv"
    udp_csv = output_dir / f"{stem}_onrobot_udp500_raw.csv"
    summary_path = output_dir / f"{stem}_summary.json"
    checkpoint_path = output_dir / f"{stem}_checkpoint.json"

    stop_event = threading.Event()
    start_event = threading.Event()
    rtde_ready = threading.Event()
    udp_ready = threading.Event()
    start_ns_box: dict[str, int] = {}
    stop_reason = {"value": "duration"}
    errors: list[str] = []
    udp_commands: list[dict[str, Any]] = []
    counts = {"rtde_rows": 0, "udp_rows": 0}

    def signal_handler(signum: int, _frame: Any) -> None:
        stop_reason["value"] = f"signal_{signum}"
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    dashboard_before = dashboard_snapshot(robot_host, dash_port)
    port_probe = {
        "rtde": probe_port(robot_host, rtde_port, timeout=1.0),
        "dashboard": probe_port(robot_host, dash_port, timeout=1.0),
        "onrobot_tcp": probe_port(compute_box_host, defaults["onrobot_tcp_daq_port"], timeout=1.0),
        "onrobot_udp": {"host": compute_box_host, "port": udp_port, "udp_connect_checked": True},
    }

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
        *UDP_VALUE_FIELDS,
        *UDP_RAW_FIELDS,
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
                    while not stop_event.is_set():
                        values = client.recv_recipe_sample(recipe_id, type_names)
                        t_s = (time.perf_counter_ns() - start_ns_box["start_ns"]) / 1e9
                        row = {
                            "sample_index": idx,
                            "t_s": f"{t_s:.9f}",
                            **dict(zip(RTDE_UR_FIELDS, values[0])),
                            **dict(zip(RTDE_SPEED_FIELDS, values[1])),
                            **dict(zip(RTDE_URCAP_FIELDS, values[2:])),
                        }
                        writer.writerow(row)
                        idx += 1
                        counts["rtde_rows"] = idx
                        if idx % args.flush_every == 0:
                            handle.flush()
                        if t_s >= args.seconds:
                            stop_reason["value"] = "duration"
                            stop_event.set()
                    handle.flush()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"rtde_worker {type(exc).__name__}: {exc}")
            rtde_ready.set()
            stop_event.set()

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
                    while not stop_event.is_set():
                        before_ns = time.perf_counter_ns()
                        if (before_ns - start_ns_box["start_ns"]) / 1e9 >= args.seconds:
                            stop_reason["value"] = "duration"
                            stop_event.set()
                            break
                        try:
                            sample = receive_sample(sock)
                        except socket.timeout:
                            if stop_event.is_set():
                                break
                            errors.append("udp_worker socket.timeout")
                            stop_event.set()
                            break
                        after_ns = time.perf_counter_ns()
                        row = sample_to_row(
                            idx,
                            (after_ns - start_ns_box["start_ns"]) / 1e9,
                            (after_ns - before_ns) / 1e6,
                            sample,
                        )
                        writer.writerow(row)
                        idx += 1
                        counts["udp_rows"] = idx
                        if idx % args.flush_every == 0:
                            handle.flush()
                    handle.flush()
                udp_commands.append(send_command(sock, COMMAND_STOP, 0))
                started_stream = False
        except Exception as exc:  # noqa: BLE001
            errors.append(f"udp_worker {type(exc).__name__}: {exc}")
            udp_ready.set()
            stop_event.set()
        finally:
            if started_stream:
                try:
                    with make_udp_socket(compute_box_host, udp_port, args.udp_timeout_s) as stop_sock:
                        udp_commands.append(send_command(stop_sock, COMMAND_STOP, 0))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"udp final STOP failed {type(exc).__name__}: {exc}")

    def checkpoint() -> None:
        elapsed = (time.perf_counter_ns() - start_ns_box.get("start_ns", time.perf_counter_ns())) / 1e9
        write_json(
            checkpoint_path,
            {
                "ok_so_far": not errors,
                "stop_reason": stop_reason["value"],
                "elapsed_s": elapsed,
                "requested_seconds": args.seconds,
                "rtde_rows": counts["rtde_rows"],
                "udp_rows": counts["udp_rows"],
                "csv_paths": {
                    "rtde_ur500_urcap125": str(rtde_csv),
                    "onrobot_udp500_raw": str(udp_csv),
                },
                "udp_commands_sent": udp_commands,
                "errors": errors,
                "updated_wall": datetime.now().isoformat(timespec="seconds"),
            },
        )

    rtde_thread = threading.Thread(target=rtde_worker, name="rtde_worker", daemon=True)
    udp_thread = threading.Thread(target=udp_worker, name="udp_worker", daemon=True)
    started_wall = datetime.now().isoformat(timespec="seconds")
    rtde_thread.start()
    udp_thread.start()
    rtde_ready.wait(timeout=10.0)
    udp_ready.wait(timeout=10.0)
    if not rtde_ready.is_set() or not udp_ready.is_set():
        errors.append("not all workers became ready within 10 s")
        stop_reason["value"] = "startup_error"
        stop_event.set()

    start_ns_box["start_ns"] = time.perf_counter_ns()
    start_event.set()
    last_checkpoint = time.perf_counter()

    while rtde_thread.is_alive() or udp_thread.is_alive():
        if not stop_event.is_set() and (time.perf_counter_ns() - start_ns_box["start_ns"]) / 1e9 >= args.seconds:
            stop_reason["value"] = "duration"
            stop_event.set()
        if time.perf_counter() - last_checkpoint >= args.checkpoint_interval_s:
            checkpoint()
            last_checkpoint = time.perf_counter()
        time.sleep(0.5)

    rtde_thread.join(timeout=5.0)
    udp_thread.join(timeout=5.0)
    checkpoint()
    dashboard_after = dashboard_snapshot(robot_host, dash_port)

    summary: dict[str, Any] = {
        "ok": not errors,
        "label": "three_stream_interruptible_first_sample_zero_speed2",
        "stop_reason": stop_reason["value"],
        "requested_seconds": args.seconds,
        "started_wall": started_wall,
        "finished_wall": datetime.now().isoformat(timespec="seconds"),
        "output_dir": str(output_dir),
        "csv_paths": {
            "rtde_ur500_urcap125": str(rtde_csv),
            "onrobot_udp500_raw": str(udp_csv),
        },
        "summary_path": str(summary_path),
        "checkpoint_path": str(checkpoint_path),
        "dashboard_before": dashboard_before,
        "dashboard_after": dashboard_after,
        "port_probe": port_probe,
        "udp_speed_divisor_sent": args.udp_speed_divisor,
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
            "interruptible: Ctrl+C/SIGTERM stops and summarizes partial data",
        ],
        "errors": errors,
    }
    if rtde_csv.exists() and rtde_csv.stat().st_size > 0:
        rtde_summary = summarize_rtde_csv(rtde_csv)
        summary.update(rtde_summary)
    if udp_csv.exists() and udp_csv.stat().st_size > 0:
        udp_summary = summarize_udp_csv(udp_csv)
        summary["udp_timing"] = udp_summary["udp_timing"]
        summary["udp_raw_tuple_updates"] = udp_summary["udp_raw_tuple_updates"]
        summary["udp_status_counts"] = udp_summary["udp_status_counts"]
        summary["udp_sequence_delta_counts"] = udp_summary["udp_sequence_delta_counts"]
        summary["udp_sample_counter_delta_mod65536_counts"] = udp_summary["udp_sample_counter_delta_mod65536_counts"]
        summary.setdefault("zeroed_axis_stats", {}).update(udp_summary["zeroed_axis_stats"])
        summary.setdefault("first_sample_zero_reference", {}).update(udp_summary["first_sample_zero_reference"])
    if rtde_csv.exists() and udp_csv.exists() and counts["rtde_rows"] > 0 and counts["udp_rows"] > 0:
        summary["plot_paths"] = plot_zeroed(rtde_csv, udp_csv, output_dir, stem)
    write_json(summary_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if not errors else 2


def main() -> int:
    parser = build_parser()
    return collect(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
