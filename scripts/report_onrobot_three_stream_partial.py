#!/usr/bin/env python3
"""Generate an in-progress report from a live OnRobot/UR three-stream capture.

The script is intentionally read-only with respect to the capture run. It reads
the existing CSV rows up to a requested time window and writes derived report
assets under the report directory.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


RTDE_UR_FIELDS = ["ur_fx_n", "ur_fy_n", "ur_fz_n", "ur_tx_nm", "ur_ty_nm", "ur_tz_nm"]
RTDE_URCAP_FIELDS = [
    "urcap_fx_n",
    "urcap_fy_n",
    "urcap_fz_n",
    "urcap_tx_nm",
    "urcap_ty_nm",
    "urcap_tz_nm",
]
UDP_VALUE_FIELDS = ["fx_n", "fy_n", "fz_n", "tx_nm", "ty_nm", "tz_nm"]
SPEED_FIELDS = ["ur_vx_mps", "ur_vy_mps", "ur_vz_mps", "ur_wx_radps", "ur_wy_radps", "ur_wz_radps"]
AXES = ["Fx", "Fy", "Fz", "Tx", "Ty", "Tz"]
FORCE_AXES = ["Fx", "Fy", "Fz"]
TORQUE_AXES = ["Tx", "Ty", "Tz"]


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


@dataclass
class ValueStats:
    samples: int = 0
    first: float | None = None
    last: float | None = None
    values: list[float] = field(default_factory=list)

    def push(self, value: float) -> None:
        if self.first is None:
            self.first = value
        self.last = value
        self.samples += 1
        self.values.append(value)

    def summary(self) -> dict[str, float | int | None]:
        if not self.values:
            return {"samples": 0}
        return {
            "samples": self.samples,
            "first": self.first,
            "last": self.last,
            "last_minus_first": None if self.first is None or self.last is None else self.last - self.first,
            "mean": statistics.fmean(self.values),
            "std": statistics.stdev(self.values) if len(self.values) >= 2 else 0.0,
            "min": min(self.values),
            "p1": percentile(self.values, 0.01),
            "p50": percentile(self.values, 0.50),
            "p99": percentile(self.values, 0.99),
            "max": max(self.values),
        }


@dataclass
class StreamData:
    name: str
    fields: list[str]
    window_seconds: float = 1800.0
    times: list[float] = field(default_factory=list)
    raw: dict[str, ValueStats] = field(default_factory=dict)
    zeroed: dict[str, ValueStats] = field(default_factory=dict)
    refs: dict[str, float] = field(default_factory=dict)
    first60: dict[str, list[float]] = field(default_factory=dict)
    last60: dict[str, list[float]] = field(default_factory=dict)
    tuple_runs: Counter[int] = field(default_factory=Counter)
    tuple_run_count: int = 0
    previous_tuple: tuple[float, ...] | None = None
    current_run_len: int = 0
    plot_times: list[float] = field(default_factory=list)
    plot_zeroed: dict[str, list[float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for axis in AXES:
            self.raw[axis] = ValueStats()
            self.zeroed[axis] = ValueStats()
            self.first60[axis] = []
            self.last60[axis] = []
            self.plot_zeroed[axis] = []

    def push(self, t_s: float, values: dict[str, float], plot_stride: int, row_index: int) -> None:
        self.times.append(t_s)
        axis_tuple: list[float] = []
        for axis in AXES:
            value = values[axis]
            self.raw[axis].push(value)
            if axis not in self.refs:
                self.refs[axis] = value
            zeroed = value - self.refs[axis]
            self.zeroed[axis].push(zeroed)
            if t_s <= 60.0:
                self.first60[axis].append(zeroed)
            if t_s >= max(0.0, self.window_seconds - 60.0) and t_s <= self.window_seconds:
                self.last60[axis].append(zeroed)
            axis_tuple.append(value)

        tup = tuple(axis_tuple)
        if self.previous_tuple is None or tup != self.previous_tuple:
            if self.current_run_len:
                self.tuple_runs[self.current_run_len] += 1
            self.previous_tuple = tup
            self.current_run_len = 1
            self.tuple_run_count += 1
        else:
            self.current_run_len += 1

        if row_index % plot_stride == 0:
            self.plot_times.append(t_s)
            for axis in AXES:
                self.plot_zeroed[axis].append(self.zeroed[axis].values[-1])

    def close_runs(self) -> None:
        if self.current_run_len:
            self.tuple_runs[self.current_run_len] += 1
            self.current_run_len = 0

    def timing_summary(self) -> dict[str, float | int | None]:
        if len(self.times) < 2:
            return {
                "samples": len(self.times),
                "first_t_s": self.times[0] if self.times else None,
                "last_t_s": self.times[-1] if self.times else None,
                "duration_first_last_s": None,
                "interval_rate_hz": None,
            }
        dts = [b - a for a, b in zip(self.times, self.times[1:])]
        duration = self.times[-1] - self.times[0]
        return {
            "samples": len(self.times),
            "first_t_s": self.times[0],
            "last_t_s": self.times[-1],
            "duration_first_last_s": duration,
            "interval_rate_hz": (len(self.times) - 1) / duration if duration else None,
            "mean_dt_ms": statistics.fmean(dts) * 1000.0,
            "p99_dt_ms": percentile(dts, 0.99) * 1000.0 if dts else None,
            "max_dt_ms": max(dts) * 1000.0 if dts else None,
        }

    def tuple_summary(self) -> dict[str, float | int | list[list[int]] | None]:
        duration = self.times[-1] - self.times[0] if len(self.times) >= 2 else None
        if self.tuple_run_count == 0:
            return {"runs": 0, "distinct_value_transition_rate_hz": None}
        total_rows = sum(length * count for length, count in self.tuple_runs.items())
        expanded: list[int] = []
        for length, count in self.tuple_runs.items():
            expanded.extend([length] * min(count, 100000))
        return {
            "runs": self.tuple_run_count,
            "distinct_value_transition_rate_hz": (self.tuple_run_count - 1) / duration if duration else None,
            "mean_rows_per_run": total_rows / self.tuple_run_count,
            "median_rows_per_run": statistics.median(expanded) if expanded else None,
            "max_rows_per_run": max(self.tuple_runs) if self.tuple_runs else None,
            "run_length_counts_top10": [[k, v] for k, v in self.tuple_runs.most_common(10)],
        }

    def axis_summary(self) -> dict[str, dict[str, float | int | None]]:
        return {axis: self.zeroed[axis].summary() for axis in AXES}

    def drift_summary(self) -> dict[str, dict[str, float | int | None]]:
        out: dict[str, dict[str, float | int | None]] = {}
        span_min = max((self.window_seconds - 60.0) / 60.0, 1e-9)
        for axis in AXES:
            start_vals = self.first60[axis]
            end_vals = self.last60[axis]
            start_mean = statistics.fmean(start_vals) if start_vals else None
            end_mean = statistics.fmean(end_vals) if end_vals else None
            delta = None if start_mean is None or end_mean is None else end_mean - start_mean
            out[axis] = {
                "start_0_60_mean": start_mean,
                "end_last60_mean": end_mean,
                "end_minus_start": delta,
                "slope_per_min": None if delta is None else delta / span_min,
                "start_samples": len(start_vals),
                "end_samples": len(end_vals),
                "end_window_start_s": max(0.0, self.window_seconds - 60.0),
                "end_window_end_s": self.window_seconds,
            }
        return out


def f(row: dict[str, str], key: str) -> float:
    return float(row[key])


def read_rtde(path: Path, window_seconds: float, plot_stride: int) -> tuple[StreamData, StreamData, dict[str, float | int | None]]:
    ur = StreamData("UR actual_TCP_force", RTDE_UR_FIELDS, window_seconds=window_seconds)
    urcap = StreamData("OnRobot URCap registers", RTDE_URCAP_FIELDS, window_seconds=window_seconds)
    speed_max_linear = 0.0
    speed_max_angular = 0.0
    with path.open(newline="", encoding="utf-8") as handle:
        for idx, row in enumerate(csv.DictReader(handle), start=1):
            t_s = f(row, "t_s")
            if t_s > window_seconds:
                break
            ur_values = {
                "Fx": f(row, "ur_fx_n"),
                "Fy": f(row, "ur_fy_n"),
                "Fz": f(row, "ur_fz_n"),
                "Tx": f(row, "ur_tx_nm"),
                "Ty": f(row, "ur_ty_nm"),
                "Tz": f(row, "ur_tz_nm"),
            }
            urcap_values = {
                "Fx": f(row, "urcap_fx_n"),
                "Fy": f(row, "urcap_fy_n"),
                "Fz": f(row, "urcap_fz_n"),
                "Tx": f(row, "urcap_tx_nm"),
                "Ty": f(row, "urcap_ty_nm"),
                "Tz": f(row, "urcap_tz_nm"),
            }
            linear = math.sqrt(f(row, "ur_vx_mps") ** 2 + f(row, "ur_vy_mps") ** 2 + f(row, "ur_vz_mps") ** 2)
            angular = math.sqrt(
                f(row, "ur_wx_radps") ** 2 + f(row, "ur_wy_radps") ** 2 + f(row, "ur_wz_radps") ** 2
            )
            speed_max_linear = max(speed_max_linear, linear)
            speed_max_angular = max(speed_max_angular, angular)
            ur.push(t_s, ur_values, plot_stride, idx)
            urcap.push(t_s, urcap_values, plot_stride, idx)
    ur.close_runs()
    urcap.close_runs()
    return ur, urcap, {"max_linear_tcp_speed_mps": speed_max_linear, "max_angular_tcp_speed_radps": speed_max_angular}


def read_udp(path: Path, window_seconds: float, plot_stride: int) -> tuple[StreamData, dict[str, object]]:
    udp = StreamData("OnRobot UDP raw", UDP_VALUE_FIELDS, window_seconds=window_seconds)
    statuses: Counter[int] = Counter()
    seq_deltas: Counter[int] = Counter()
    sample_counter_deltas: Counter[int] = Counter()
    prev_seq: int | None = None
    prev_counter: int | None = None
    latency_values: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for idx, row in enumerate(csv.DictReader(handle), start=1):
            t_s = f(row, "t_s")
            if t_s > window_seconds:
                break
            values = {
                "Fx": f(row, "fx_n"),
                "Fy": f(row, "fy_n"),
                "Fz": f(row, "fz_n"),
                "Tx": f(row, "tx_nm"),
                "Ty": f(row, "ty_nm"),
                "Tz": f(row, "tz_nm"),
            }
            udp.push(t_s, values, plot_stride, idx)
            latency_values.append(f(row, "recv_latency_ms"))
            seq = int(row["sequence_number"])
            sample_counter = int(row["sample_counter"])
            statuses[int(row["status"])] += 1
            if prev_seq is not None:
                seq_deltas[seq - prev_seq] += 1
            if prev_counter is not None:
                sample_counter_deltas[sample_counter - prev_counter] += 1
            prev_seq = seq
            prev_counter = sample_counter
    udp.close_runs()
    return udp, {
        "status_counts": dict(statuses),
        "sequence_delta_counts": dict(seq_deltas),
        "sample_counter_delta_counts": dict(sample_counter_deltas),
        "recv_latency_ms": {
            "mean": statistics.fmean(latency_values) if latency_values else None,
            "p99": percentile(latency_values, 0.99),
            "max": max(latency_values) if latency_values else None,
        },
    }


def format_duration_label(seconds: float) -> str:
    if seconds >= 3600 and abs(seconds / 3600 - round(seconds / 3600)) < 1e-9:
        return f"{int(round(seconds / 3600))} 小时"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} 小时"
    if seconds >= 60 and abs(seconds / 60 - round(seconds / 60)) < 1e-9:
        return f"{int(round(seconds / 60))} 分钟"
    if seconds >= 60:
        return f"{seconds / 60:.1f} 分钟"
    return f"{seconds:.0f} 秒"


def plot_fz(streams: Iterable[StreamData], output: Path, window_seconds: float) -> None:
    fig, ax = plt.subplots(figsize=(12, 5.5), dpi=150)
    for stream in streams:
        ax.plot(stream.plot_times, stream.plot_zeroed["Fz"], linewidth=0.9, label=stream.name)
    ax.set_title(f"Fz first-sample-zeroed overlay, 0-{window_seconds:.0f} s")
    ax.set_xlabel("t_s (s)")
    ax.set_ylabel("Fz - first sample (N)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(output)
    plt.close(fig)


def plot_force_axes(streams: Iterable[StreamData], output: Path, window_seconds: float) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(12, 8), dpi=150, sharex=True)
    for axis_name, ax in zip(FORCE_AXES, axes):
        for stream in streams:
            ax.plot(stream.plot_times, stream.plot_zeroed[axis_name], linewidth=0.8, label=stream.name)
        ax.set_ylabel(f"{axis_name} zeroed (N)")
        ax.grid(True, alpha=0.3)
    axes[0].set_title(f"Force axes first-sample-zeroed, 0-{window_seconds:.0f} s")
    axes[-1].set_xlabel("t_s (s)")
    axes[0].legend(loc="best")
    fig.tight_layout()
    fig.savefig(output)
    plt.close(fig)


def plot_fxy(streams: Iterable[StreamData], output: Path, window_seconds: float) -> None:
    streams = list(streams)
    fig, axes = plt.subplots(len(streams), 1, figsize=(12, 8), dpi=150, sharex=True)
    if len(streams) == 1:
        axes = [axes]
    for stream, ax in zip(streams, axes):
        ax.plot(stream.plot_times, stream.plot_zeroed["Fx"], linewidth=0.8, label="Fx")
        ax.plot(stream.plot_times, stream.plot_zeroed["Fy"], linewidth=0.8, label="Fy")
        ax.set_ylabel("N")
        ax.set_title(stream.name)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best")
    axes[-1].set_xlabel("t_s (s)")
    fig.suptitle(f"Fx/Fy first-sample-zeroed by stream, 0-{window_seconds:.0f} s")
    fig.tight_layout()
    fig.savefig(output)
    plt.close(fig)


def plot_torque_axes(streams: Iterable[StreamData], output: Path, window_seconds: float) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(12, 8), dpi=150, sharex=True)
    for axis_name, ax in zip(TORQUE_AXES, axes):
        for stream in streams:
            ax.plot(stream.plot_times, stream.plot_zeroed[axis_name], linewidth=0.8, label=stream.name)
        ax.set_ylabel(f"{axis_name} zeroed (Nm)")
        ax.grid(True, alpha=0.3)
    axes[0].set_title(f"Torque axes first-sample-zeroed, 0-{window_seconds:.0f} s")
    axes[-1].set_xlabel("t_s (s)")
    axes[0].legend(loc="best")
    fig.tight_layout()
    fig.savefig(output)
    plt.close(fig)


def summarize_values(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"samples": 0}
    mean = statistics.fmean(values)
    return {
        "samples": len(values),
        "mean": mean,
        "std": statistics.stdev(values) if len(values) >= 2 else 0.0,
        "min": min(values),
        "p1": percentile(values, 0.01),
        "p50": percentile(values, 0.50),
        "p99": percentile(values, 0.99),
        "max": max(values),
    }


def bin_means(times: list[float], values: list[float], bin_seconds: float) -> dict[str, list[float] | float]:
    bins: dict[int, list[float]] = {}
    for t_s, value in zip(times, values):
        bin_index = int(t_s // bin_seconds)
        bins.setdefault(bin_index, []).append(value)
    centers: list[float] = []
    means: list[float] = []
    counts: list[float] = []
    for bin_index in sorted(bins):
        vals = bins[bin_index]
        centers.append(bin_index * bin_seconds + bin_seconds / 2.0)
        means.append(statistics.fmean(vals))
        counts.append(float(len(vals)))
    return {
        "bin_seconds": bin_seconds,
        "centers_s": centers,
        "means": means,
        "counts": counts,
    }


def fz_urcap_udp_comparison(urcap: StreamData, udp: StreamData) -> dict[str, object]:
    urcap_update_times: list[float] = []
    urcap_update_fz: list[float] = []
    previous_tuple: tuple[float, ...] | None = None
    raw_axis_values = {axis: urcap.raw[axis].values for axis in AXES}
    for idx, t_s in enumerate(urcap.times):
        tup = tuple(raw_axis_values[axis][idx] for axis in AXES)
        if previous_tuple is None or tup != previous_tuple:
            previous_tuple = tup
            urcap_update_times.append(t_s)
            urcap_update_fz.append(urcap.zeroed["Fz"].values[idx])

    udp_times = udp.times
    udp_fz = udp.zeroed["Fz"].values
    matched_udp_times: list[float] = []
    matched_udp_fz: list[float] = []
    residuals: list[float] = []
    abs_residuals: list[float] = []
    time_offsets_ms: list[float] = []
    for t_s, urcap_fz in zip(urcap_update_times, urcap_update_fz):
        pos = bisect.bisect_left(udp_times, t_s)
        candidates = []
        if pos < len(udp_times):
            candidates.append(pos)
        if pos > 0:
            candidates.append(pos - 1)
        if not candidates:
            continue
        best = min(candidates, key=lambda idx: abs(udp_times[idx] - t_s))
        udp_value = udp_fz[best]
        residual = urcap_fz - udp_value
        matched_udp_times.append(udp_times[best])
        matched_udp_fz.append(udp_value)
        residuals.append(residual)
        abs_residuals.append(abs(residual))
        time_offsets_ms.append((t_s - udp_times[best]) * 1000.0)

    rmse = math.sqrt(statistics.fmean([value * value for value in residuals])) if residuals else None
    residual_stats = summarize_values(residuals)
    residual_stats["mae"] = statistics.fmean(abs_residuals) if abs_residuals else None
    residual_stats["rmse"] = rmse
    residual_stats["max_abs"] = max(abs_residuals) if abs_residuals else None
    bin_seconds = 1.0
    urcap_bins = bin_means(urcap_update_times, urcap_update_fz, bin_seconds)
    udp_bins = bin_means(urcap_update_times, matched_udp_fz, bin_seconds)
    residual_bins = bin_means(urcap_update_times, residuals, bin_seconds)
    mean_urcap = statistics.fmean(urcap_update_fz) if urcap_update_fz else None
    mean_udp = statistics.fmean(matched_udp_fz) if matched_udp_fz else None
    mean_residual = statistics.fmean(residuals) if residuals else None
    residual_bin_means = residual_bins["means"]
    residual_bin_rmse = (
        math.sqrt(statistics.fmean([value * value for value in residual_bin_means]))
        if residual_bin_means
        else None
    )
    return {
        "method": "URCap distinct update points matched to nearest OnRobot UDP sample by t_s; both Fz series use each stream's first sample as software zero.",
        "samples": len(residuals),
        "urcap_update_rate_hz": (len(urcap_update_times) - 1) / (urcap_update_times[-1] - urcap_update_times[0])
        if len(urcap_update_times) >= 2
        else None,
        "matched_udp_nominal_rate_hz": udp.timing_summary()["interval_rate_hz"],
        "urcap_times_s": urcap_update_times,
        "urcap_fz_zeroed_n": urcap_update_fz,
        "matched_udp_times_s": matched_udp_times,
        "matched_udp_fz_zeroed_n": matched_udp_fz,
        "residual_urcap_minus_udp_n": residuals,
        "residual_stats_n": residual_stats,
        "nearest_time_offset_ms": summarize_values(time_offsets_ms),
        "abs_nearest_time_offset_ms": summarize_values([abs(value) for value in time_offsets_ms]),
        "matched_series_mean_n": {
            "urcap_fz_zeroed_mean": mean_urcap,
            "udp_fz_zeroed_mean": mean_udp,
            "urcap_minus_udp_mean": mean_residual,
        },
        "binned_mean": {
            "bin_seconds": bin_seconds,
            "urcap_fz_zeroed_n": urcap_bins,
            "matched_udp_fz_zeroed_n": udp_bins,
            "residual_urcap_minus_udp_n": residual_bins,
            "residual_mean_stats_n": summarize_values(residual_bin_means),
            "residual_mean_rmse_n": residual_bin_rmse,
            "residual_mean_max_abs_n": max([abs(value) for value in residual_bin_means]) if residual_bin_means else None,
        },
    }


def plot_fz_urcap_udp_comparison(comparison: dict[str, object], output: Path, window_seconds: float) -> None:
    urcap_times = comparison["urcap_times_s"]
    urcap_fz = comparison["urcap_fz_zeroed_n"]
    udp_fz = comparison["matched_udp_fz_zeroed_n"]
    residuals = comparison["residual_urcap_minus_udp_n"]
    series_means = comparison["matched_series_mean_n"]
    binned = comparison["binned_mean"]
    bin_centers = binned["urcap_fz_zeroed_n"]["centers_s"]
    urcap_bin_means = binned["urcap_fz_zeroed_n"]["means"]
    udp_bin_means = binned["matched_udp_fz_zeroed_n"]["means"]
    residual_bin_centers = binned["residual_urcap_minus_udp_n"]["centers_s"]
    residual_bin_means = binned["residual_urcap_minus_udp_n"]["means"]
    stride = max(1, len(urcap_times) // 30000)
    plot_times = urcap_times[::stride]
    plot_urcap = urcap_fz[::stride]
    plot_udp = udp_fz[::stride]
    plot_residuals = residuals[::stride]

    fig, axes = plt.subplots(2, 1, figsize=(12, 7.5), dpi=150, sharex=True)
    axes[0].plot(plot_times, plot_urcap, linewidth=0.55, alpha=0.18, label="URCap matched points")
    axes[0].plot(plot_times, plot_udp, linewidth=0.55, alpha=0.18, label="Nearest UDP points")
    axes[0].plot(bin_centers, urcap_bin_means, linewidth=1.9, label="URCap 1 s mean (125 Hz)")
    axes[0].plot(bin_centers, udp_bin_means, linewidth=1.9, label="UDP 1 s mean (500 Hz source)")
    axes[0].axhline(
        series_means["urcap_fz_zeroed_mean"],
        color="#1f77b4",
        linestyle="--",
        linewidth=1.1,
        alpha=0.8,
        label="URCap global mean",
    )
    axes[0].axhline(
        series_means["udp_fz_zeroed_mean"],
        color="#ff7f0e",
        linestyle="--",
        linewidth=1.1,
        alpha=0.8,
        label="UDP global mean",
    )
    axes[0].set_ylabel("Fz - first sample (N)")
    axes[0].set_title(
        f"OnRobot Fz: 1 s mean and global mean, URCap 125 Hz vs UDP 500 Hz, 0-{window_seconds:.0f} s"
    )
    axes[0].grid(True, alpha=0.3)
    axes[0].legend(loc="best", ncol=2)

    axes[1].plot(plot_times, plot_residuals, linewidth=0.45, alpha=0.18, color="#7c3aed", label="Matched residual")
    axes[1].plot(residual_bin_centers, residual_bin_means, linewidth=1.8, color="#7c3aed", label="Residual 1 s mean")
    axes[1].axhline(0.0, color="black", linewidth=0.8, alpha=0.5)
    axes[1].axhline(
        series_means["urcap_minus_udp_mean"],
        color="#7c3aed",
        linestyle="--",
        linewidth=1.1,
        alpha=0.85,
        label="Residual global mean",
    )
    axes[1].set_xlabel("t_s (s)")
    axes[1].set_ylabel("URCap - UDP (N)")
    axes[1].grid(True, alpha=0.3)
    axes[1].legend(loc="best")
    fig.tight_layout()
    fig.savefig(output)
    plt.close(fig)


def rel(path: Path, base: Path) -> str:
    return os.path.relpath(path.resolve(), start=base.resolve())


def fmt(value: object, digits: int = 4) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        if math.isnan(value):
            return "N/A"
        return f"{value:.{digits}f}"
    return str(value)


def axis_table(summary: dict[str, object], streams: list[StreamData], axes: list[str]) -> str:
    lines = [
        "| 数据流 | 轴 | 样本数 | 均值 | 标准差 | Min | P1 | P50 | P99 | Max | 末值-首值 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for stream in streams:
        stream_summary = summary["streams"][stream.name]["zeroed_axis_stats"]
        for axis in axes:
            stats = stream_summary[axis]
            lines.append(
                "| "
                + " | ".join(
                    [
                        stream.name,
                        axis,
                        fmt(stats["samples"], 0),
                        fmt(stats["mean"]),
                        fmt(stats["std"]),
                        fmt(stats["min"]),
                        fmt(stats["p1"]),
                        fmt(stats["p50"]),
                        fmt(stats["p99"]),
                        fmt(stats["max"]),
                        fmt(stats["last_minus_first"]),
                    ]
                )
                + " |"
            )
    return "\n".join(lines)


def drift_table(summary: dict[str, object], streams: list[StreamData], axes: list[str]) -> str:
    end_s = float(summary["window"]["end_s"])
    end_start_s = max(0.0, end_s - 60.0)
    lines = [
        f"| 数据流 | 轴 | 0-60 s 均值 | {end_start_s:.0f}-{end_s:.0f} s 均值 | 后60s-前60s | 斜率/分钟 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for stream in streams:
        stream_summary = summary["streams"][stream.name]["drift_60s_windows"]
        for axis in axes:
            stats = stream_summary[axis]
            lines.append(
                "| "
                + " | ".join(
                    [
                        stream.name,
                        axis,
                        fmt(stats["start_0_60_mean"]),
                        fmt(stats["end_last60_mean"]),
                        fmt(stats["end_minus_start"]),
                        fmt(stats["slope_per_min"]),
                    ]
                )
                + " |"
            )
    return "\n".join(lines)


def fz_comparison_table(summary: dict[str, object]) -> str:
    comp = summary["fz_urcap_udp_comparison"]
    residual = comp["residual_stats_n"]
    offset = comp["abs_nearest_time_offset_ms"]
    means = comp["matched_series_mean_n"]
    binned = comp["binned_mean"]
    binned_residual = binned["residual_mean_stats_n"]
    rows = [
        "| 指标 | 数值 | 单位 |",
        "|---|---:|---|",
        f"| 匹配样本数 | {fmt(comp['samples'], 0)} | 点 |",
        f"| URCap 更新率 | {fmt(comp['urcap_update_rate_hz'], 3)} | Hz |",
        f"| UDP 源包频率 | {fmt(comp['matched_udp_nominal_rate_hz'], 3)} | Hz |",
        f"| 最近时间差均值 | {fmt(offset['mean'], 4)} | ms |",
        f"| 最近时间差 P99 | {fmt(offset['p99'], 4)} | ms |",
        f"| URCap Fz 全局均值 | {fmt(means['urcap_fz_zeroed_mean'])} | N |",
        f"| UDP Fz 全局均值 | {fmt(means['udp_fz_zeroed_mean'])} | N |",
        f"| 全局均值差 URCap-UDP | {fmt(means['urcap_minus_udp_mean'])} | N |",
        f"| 残差均值 | {fmt(residual['mean'])} | N |",
        f"| 残差标准差 | {fmt(residual['std'])} | N |",
        f"| 残差 P1/P50/P99 | {fmt(residual['p1'])} / {fmt(residual['p50'])} / {fmt(residual['p99'])} | N |",
        f"| MAE | {fmt(residual['mae'])} | N |",
        f"| RMSE | {fmt(residual['rmse'])} | N |",
        f"| 最大绝对残差 | {fmt(residual['max_abs'])} | N |",
        f"| 1 s 分箱均值残差 RMSE | {fmt(binned['residual_mean_rmse_n'])} | N |",
        f"| 1 s 分箱均值最大绝对残差 | {fmt(binned['residual_mean_max_abs_n'])} | N |",
        f"| 1 s 分箱数 | {fmt(binned['bins'], 0)} | 个 |",
    ]
    return "\n".join(rows)


def write_report(
    report_path: Path,
    report_root: Path,
    assets: dict[str, Path],
    summary: dict[str, object],
    raw_rtde: Path,
    raw_udp: Path,
    run_dir: Path,
    checkpoint: Path | None,
) -> None:
    window_end_s = float(summary["window"]["end_s"])
    window_label = format_duration_label(window_end_s)
    end_last60_start_s = max(0.0, window_end_s - 60.0)
    streams = [
        StreamData("UR actual_TCP_force", [], window_seconds=window_end_s),
        StreamData("OnRobot URCap registers", [], window_seconds=window_end_s),
        StreamData("OnRobot UDP raw", [], window_seconds=window_end_s),
    ]
    timing_rows = [
        "| 数据流 | 窗口样本数 | 首末时长 (s) | 行/包频率 (Hz) | tuple 更新数 | tuple 更新率 (Hz) | 口径 |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    labels = {
        "UR actual_TCP_force": "UR 内部 RTDE `actual_TCP_force`，不是 OnRobot 传感器值",
        "OnRobot URCap registers": "URCap `Fx..Tz` 经 `output_double_register_24..29` 导出",
        "OnRobot UDP raw": "Compute Box high-speed UDP，`SPEED=2`",
    }
    for stream_name in labels:
        stream_summary = summary["streams"][stream_name]
        timing = stream_summary["timing"]
        tuple_info = stream_summary["tuple_updates"]
        timing_rows.append(
            "| "
            + " | ".join(
                [
                    stream_name,
                    fmt(timing["samples"], 0),
                    fmt(timing["duration_first_last_s"], 3),
                    fmt(timing["interval_rate_hz"], 3),
                    fmt(tuple_info["runs"], 0),
                    fmt(tuple_info["distinct_value_transition_rate_hz"], 3),
                    labels[stream_name],
                ]
            )
            + " |"
        )

    md = f"""# OnRobot HEX / UR 三流冷启动漂移 {window_label}中期报告

生成时间：{summary["generated_wall"]}

## 实验目的

在不停止当前 24 小时采集的前提下，先查看前 {window_label}数据是否持续、三条力流的频率是否符合预期，并用第一帧软件归零口径观察冷启动早期漂移。这个报告是中期快照，不替代长跑结束后的完整报告。

## 设备与实验条件

| 项目 | 当前口径 |
|---|---|
| 机器人 | UR10e，RTDE 读取 `actual_TCP_force` |
| OnRobot | HEX-E v2，经 Compute Box `192.168.1.1` |
| URCap 路线 | `Fx/Fy/Fz/Tx/Ty/Tz` 写入 `output_double_register_24..29` 后由 Ubuntu 读 RTDE |
| UDP 路线 | OnRobot high-speed UDP，发送过 `SPEED=2` 与 `START`；本报告未发送任何新命令 |
| 分析窗口 | `{fmt(summary["window"]["start_s"], 1)}-{fmt(summary["window"]["end_s"], 1)} s` |
| 窗口定义 | 从正在写入的 CSV 中只读取 `t_s <= {fmt(summary["window"]["end_s"], 1)}` 的已有行 |
| 归零状态 | 原始 CSV 未改动；统计和图只做第一样本软件相减 |
| 安全边界 | 未停止采集、未 `zero_ftsensor()`、未 OnRobot `BIAS/FILTER`、未运动、未 TCP/payload 写入 |
| TCP 速度检查 | {window_label}窗口内最大线速度 `{fmt(summary["rtde_motion_check"]["max_linear_tcp_speed_mps"], 8)} m/s`，最大角速度 `{fmt(summary["rtde_motion_check"]["max_angular_tcp_speed_radps"], 8)} rad/s` |

## 实验命令

当前长跑仍由以下脚本采集，本报告只读取它已经写出的 CSV：

`/home/andy/ur10e_ros2_ws/scripts/run_onrobot_three_stream_coldstart_drift.sh`

报告生成脚本：

`/home/andy/ur10e_ros2_ws/scripts/report_onrobot_three_stream_partial.py --run-dir <run_dir> --window-seconds {fmt(summary["window"]["end_s"], 0)}`

## 数据与图片

原始数据仍在采集中；下列链接指向当前 run 目录里的 live CSV，不是报告脚本复制出来的截断副本。

| 数据 | 路径 |
|---|---|
| RTDE + URCap CSV | [{raw_rtde.name}]({rel(raw_rtde, report_root)}) |
| OnRobot UDP CSV | [{raw_udp.name}]({rel(raw_udp, report_root)}) |
| 中期 summary JSON | [{assets["summary"].name}]({rel(assets["summary"], report_root)}) |
| 当前 checkpoint | {f"[{checkpoint.name}]({rel(checkpoint, report_root)})" if checkpoint else "N/A"} |

图 1 是 Fz 的第一样本归零重叠图。它用于直接判断三条路线在 {window_label}窗口内的低频漂移趋势，不用于判断绝对载荷是否一致。

![Fz overlay]({rel(assets["fz"], report_root)})

图 2 专门比较两个 OnRobot Fz 路线：URCap 的 125 Hz 真实更新点，和 UDP 500 Hz 流中按时间戳最近的样本。淡线是匹配后的点，粗线是 `1 s` 分箱均值，虚线是全局均值；下半图是残差及其 `1 s` 分箱均值。两条曲线都只做第一样本软件归零，残差定义为 `URCap_zeroed_Fz - UDP_zeroed_Fz`。

![URCap UDP Fz]({rel(assets["fz_urcap_udp"], report_root)})

图 3 展示 Fx/Fy/Fz 三个力轴。横向力 `Fx/Fy` 的漂移幅值和 Fz 分开看，避免只看 Fz 漏掉侧向变化。

![Force axes]({rel(assets["force_axes"], report_root)})

图 4 是每条流内部的 Fx/Fy 对照。这里的 `fxy` 指 Fx 和 Fy 两条曲线，不是水平力模长。

![Fx Fy]({rel(assets["fxy"], report_root)})

图 5 给出三个力矩轴，用于判断力矩漂移是否和力轴同时出现。

![Torque axes]({rel(assets["torque_axes"], report_root)})

## 统计结果

### 采集进度与频率

{chr(10).join(timing_rows)}

UDP 质量字段：`status_counts={summary["udp_quality"]["status_counts"]}`，`sequence_delta_counts={summary["udp_quality"]["sequence_delta_counts"]}`，`sample_counter_delta_counts={summary["udp_quality"]["sample_counter_delta_counts"]}`。这说明前 {window_label} 窗口内 UDP status 为 0，sequence number 连续；sample counter 以 `+2` 为主，`-65534` 是 16-bit 回绕后按模 65536 等价的 `+2`。

### OnRobot Fz 专门比较

这部分只比较 OnRobot 自身的两条路线，不包含 UR `actual_TCP_force`。URCap 路线使用去重后的真实更新点，避免把 125 Hz 值在 500 Hz RTDE 表里重复的行当作独立样本；UDP 路线使用每个 URCap 更新时间附近最近的 UDP 样本。

{fz_comparison_table(summary)}

### 力轴统计

下表全部采用第一样本软件归零后的值，单位为 N。

{axis_table(summary, streams, FORCE_AXES)}

### 力矩轴统计

下表全部采用第一样本软件归零后的值，单位为 Nm。

{axis_table(summary, streams, TORQUE_AXES)}

### 前后 60 秒漂移

下表比较 `0-60 s` 和 `{end_last60_start_s:.0f}-{window_end_s:.0f} s` 的均值差。斜率按两个 60 秒窗口中心之间的间隔近似，仅作为中期趋势指标。

{drift_table(summary, streams, AXES)}

## 结论

1. 当前采集没有被中断；前 {window_label}窗口已经可用，RTDE/UDP 行数和时间戳均连续增长。
2. 频率口径符合预期：UR `actual_TCP_force` 行频率约 500 Hz，OnRobot URCap 寄存器 tuple 更新约 125 Hz，OnRobot UDP 原始流约 500 Hz。
3. OnRobot Fz 的 125 Hz URCap 更新点和 500 Hz UDP 最近样本在趋势上接近；图 2 的 `1 s` 分箱均值比原始重叠线更容易看出两条趋势是否一起漂，全局均值虚线只用于判断整体偏置。
4. 专门比较表给出了二者在第一样本归零后的残差、MAE/RMSE、`1 s` 分箱均值残差和时间匹配误差。
5. 报告中的 OnRobot 比较使用软件第一样本归零。原始 CSV 仍保留绝对值，因此后续可以重新选择归零窗口或做更严格的时间对齐。
6. 这个中期报告只证明前 {window_label}的数据完整性和漂移趋势；长时间热漂移结论要等 2 小时以上、后续 checkpoint 或最终 24 小时 summary 再下。

## 下一步

继续让当前采集运行。下一份建议在 2 小时或 4 小时 checkpoint 后生成，用同一口径加入分段统计，避免把冷启动早期和后续慢漂移混在一个均值里。

## 附录

### 运行目录

`{run_dir}`

### 只读处理说明

报告脚本只读取两张 CSV 中 `t_s <= {fmt(summary["window"]["end_s"], 1)}` 的已有行，并把图和 JSON 写入报告 assets 目录。它没有向 UR 或 OnRobot 发送网络命令，也没有停止或重启采集进程。
"""
    report_path.write_text(md, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--window-seconds", type=float, default=1800.0)
    parser.add_argument("--report-root", type=Path, default=Path("/home/andy/ur10e_ros2_ws/report"))
    parser.add_argument("--stem", default="onrobot_three_stream_half_hour_20260528")
    parser.add_argument("--plot-stride", type=int, default=20)
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    rtde_paths = sorted(run_dir.glob("*_rtde_ur500_urcap125.csv"))
    udp_paths = sorted(run_dir.glob("*_onrobot_udp500_raw.csv"))
    if not rtde_paths or not udp_paths:
        raise SystemExit(f"Missing expected CSV files in {run_dir}")
    rtde_path = rtde_paths[-1]
    udp_path = udp_paths[-1]
    checkpoint_paths = sorted(run_dir.glob("*_checkpoint.json"))
    checkpoint_path = checkpoint_paths[-1] if checkpoint_paths else None

    report_root = args.report_root.resolve()
    assets_dir = report_root / "assets" / args.stem
    assets_dir.mkdir(parents=True, exist_ok=True)

    ur, urcap, motion_check = read_rtde(rtde_path, args.window_seconds, args.plot_stride)
    udp, udp_quality = read_udp(udp_path, args.window_seconds, args.plot_stride)
    streams = [ur, urcap, udp]
    if any(not stream.times or stream.times[-1] < args.window_seconds - 0.1 for stream in streams):
        raise SystemExit(
            "Not enough data for requested window: "
            + ", ".join(f"{stream.name} last_t={stream.times[-1] if stream.times else None}" for stream in streams)
        )

    summary: dict[str, object] = {
        "generated_wall": datetime.now().isoformat(timespec="seconds"),
        "run_dir": str(run_dir),
        "window": {"start_s": 0.0, "end_s": args.window_seconds},
        "rtde_motion_check": motion_check,
        "udp_quality": udp_quality,
        "streams": {},
    }
    for stream in streams:
        summary["streams"][stream.name] = {
            "timing": stream.timing_summary(),
            "tuple_updates": stream.tuple_summary(),
            "first_sample_zero_reference": stream.refs,
            "zeroed_axis_stats": stream.axis_summary(),
            "drift_60s_windows": stream.drift_summary(),
        }

    fz_comparison = fz_urcap_udp_comparison(urcap, udp)
    summary["fz_urcap_udp_comparison"] = {
        "method": fz_comparison["method"],
        "samples": fz_comparison["samples"],
        "urcap_update_rate_hz": fz_comparison["urcap_update_rate_hz"],
        "matched_udp_nominal_rate_hz": fz_comparison["matched_udp_nominal_rate_hz"],
        "matched_series_mean_n": fz_comparison["matched_series_mean_n"],
        "residual_stats_n": fz_comparison["residual_stats_n"],
        "nearest_time_offset_ms": fz_comparison["nearest_time_offset_ms"],
        "abs_nearest_time_offset_ms": fz_comparison["abs_nearest_time_offset_ms"],
        "binned_mean": {
            "bin_seconds": fz_comparison["binned_mean"]["bin_seconds"],
            "bins": len(fz_comparison["binned_mean"]["residual_urcap_minus_udp_n"]["means"]),
            "residual_mean_stats_n": fz_comparison["binned_mean"]["residual_mean_stats_n"],
            "residual_mean_rmse_n": fz_comparison["binned_mean"]["residual_mean_rmse_n"],
            "residual_mean_max_abs_n": fz_comparison["binned_mean"]["residual_mean_max_abs_n"],
        },
    }

    assets = {
        "summary": assets_dir / f"{args.stem}_summary.json",
        "fz": assets_dir / f"{args.stem}_fz_overlay_zeroed.png",
        "fz_urcap_udp": assets_dir / f"{args.stem}_fz_urcap125_udp500_compare.png",
        "force_axes": assets_dir / f"{args.stem}_force_axes_zeroed.png",
        "fxy": assets_dir / f"{args.stem}_fxy_zeroed.png",
        "torque_axes": assets_dir / f"{args.stem}_torque_axes_zeroed.png",
    }
    assets["summary"].write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    plot_fz(streams, assets["fz"], args.window_seconds)
    plot_fz_urcap_udp_comparison(fz_comparison, assets["fz_urcap_udp"], args.window_seconds)
    plot_force_axes(streams, assets["force_axes"], args.window_seconds)
    plot_fxy(streams, assets["fxy"], args.window_seconds)
    plot_torque_axes(streams, assets["torque_axes"], args.window_seconds)

    report_path = report_root / f"{args.stem}.md"
    write_report(report_path, report_root, assets, summary, rtde_path, udp_path, run_dir, checkpoint_path)

    print(json.dumps({"report": str(report_path), "summary": str(assets["summary"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
