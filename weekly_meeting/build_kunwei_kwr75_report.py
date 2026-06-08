#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path("/home/andy/ur10e_ros2_ws")
REPORT_STEM = "kunwei-kwr75-progress-20260608"
DECK_STEM = "kunwei_kwr75_meeting_deck_20260608"

REPORT_DIR = ROOT / "report"
REPORT_ASSETS = REPORT_DIR / "assets" / REPORT_STEM
WEEKLY_DIR = ROOT / "weekly_meeting"
WEEKLY_ASSETS = WEEKLY_DIR / "assets" / "kunwei_kwr75_20260608"

REPORT_MD = REPORT_DIR / f"{REPORT_STEM}.md"
REPORT_HTML = WEEKLY_DIR / f"{DECK_STEM}.html"

KUNWEI_LONG_CSV = ROOT / "ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/data.csv"
KUNWEI_LONG_SUMMARY = ROOT / "ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/summary.json"
KUNWEI_LONG_REPORT_SUMMARY = REPORT_DIR / "assets/kunwei-19h15-1khz-drift/analysis-summary.json"
STEP2C_METRICS = REPORT_DIR / "assets/step2c-kunwei-search5-guard20-line2ms/analysis-metrics.json"

ONROBOT_600_DIR = ROOT / "experiments/20260528_onrobot_three_stream_600s_first_zero/run_20260528_043100"
ONROBOT_UDP_CSV = ONROBOT_600_DIR / "three_stream_600s_20260528_043052_onrobot_udp500_raw.csv"
ONROBOT_RTDE_CSV = ONROBOT_600_DIR / "three_stream_600s_20260528_043052_rtde_ur500_urcap125.csv"
ONROBOT_SUMMARY = ONROBOT_600_DIR / "three_stream_600s_20260528_043052_summary.json"
ONROBOT_ALIGNMENT = ONROBOT_600_DIR / "three_stream_600s_20260528_043052_urcap_udp_alignment_stats.json"

ONROBOT_LONG_DIR = ROOT / "experiments/20260530_onrobot_three_stream_coldstart_drift/run_20260530_175217"
ONROBOT_LONG_UDP_CSV = ONROBOT_LONG_DIR / "three_stream_24h_20260530_20260530_175220_onrobot_udp500_raw.csv"
ONROBOT_LONG_SUMMARY = ONROBOT_LONG_DIR / "three_stream_24h_20260530_20260530_175220_summary.json"

WINDOW_S = 600.0
LONG_WINDOW_S = 21600.0
COMMON_MAX_WINDOW_S = 31632.103973266


@dataclass
class RunningStats:
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0
    first: float | None = None
    last: float | None = None
    min: float | None = None
    max: float | None = None

    def add(self, value: float) -> None:
        if self.n == 0:
            self.first = value
            self.min = value
            self.max = value
        self.n += 1
        delta = value - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (value - self.mean)
        self.last = value
        self.min = value if self.min is None or value < self.min else self.min
        self.max = value if self.max is None or value > self.max else self.max

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "n": self.n,
            "first": self.first,
            "last": self.last,
            "last_first": None if self.first is None or self.last is None else self.last - self.first,
            "mean": self.mean if self.n else None,
            "std": math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0,
            "min": self.min,
            "max": self.max,
        }


@dataclass
class VectorStats:
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0
    first: float | None = None
    last: float | None = None
    min: float | None = None
    max: float | None = None

    def add_many(self, values: np.ndarray) -> None:
        if values.size == 0:
            return
        values = values.astype(float, copy=False)
        if self.n == 0:
            self.first = float(values[0])
        self.last = float(values[-1])
        chunk_n = int(values.size)
        chunk_mean = float(values.mean())
        chunk_m2 = float(((values - chunk_mean) ** 2).sum())
        chunk_min = float(values.min())
        chunk_max = float(values.max())
        if self.n == 0:
            self.n = chunk_n
            self.mean = chunk_mean
            self.m2 = chunk_m2
            self.min = chunk_min
            self.max = chunk_max
            return
        new_n = self.n + chunk_n
        delta = chunk_mean - self.mean
        self.m2 = self.m2 + chunk_m2 + delta * delta * self.n * chunk_n / new_n
        self.mean = self.mean + delta * chunk_n / new_n
        self.n = new_n
        self.min = chunk_min if self.min is None else min(self.min, chunk_min)
        self.max = chunk_max if self.max is None else max(self.max, chunk_max)

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "n": self.n,
            "first": self.first,
            "last": self.last,
            "last_first": None if self.first is None or self.last is None else self.last - self.first,
            "mean": self.mean if self.n else None,
            "std": math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0,
            "min": self.min,
            "max": self.max,
        }


class EnvelopeBins:
    def __init__(self, window_s: float, bins: int = 1800) -> None:
        self.window_s = window_s
        self.bins = bins
        self.count = np.zeros(bins, dtype=np.int64)
        self.t_sum = np.zeros(bins, dtype=float)
        self.value_sum = np.zeros(bins, dtype=float)
        self.min = np.full(bins, np.inf, dtype=float)
        self.max = np.full(bins, -np.inf, dtype=float)

    def add_many(self, t_values: np.ndarray, values: np.ndarray) -> None:
        if values.size == 0:
            return
        indices = np.floor(t_values / self.window_s * self.bins).astype(np.int64)
        indices = np.clip(indices, 0, self.bins - 1)
        np.add.at(self.count, indices, 1)
        np.add.at(self.t_sum, indices, t_values)
        np.add.at(self.value_sum, indices, values)
        np.minimum.at(self.min, indices, values)
        np.maximum.at(self.max, indices, values)

    def as_dict(self) -> dict[str, list[float]]:
        mask = self.count > 0
        return {
            "t": (self.t_sum[mask] / self.count[mask]).tolist(),
            "min": self.min[mask].tolist(),
            "max": self.max[mask].tolist(),
            "mean": (self.value_sum[mask] / self.count[mask]).tolist(),
        }


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fmt(value: float | int | None, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:.{digits}f}"


def rel_from_report(path: Path) -> str:
    return os.path.relpath(path, REPORT_DIR)


def rel_from_weekly(path: Path) -> str:
    return os.path.relpath(path, WEEKLY_DIR)


def ensure_dirs() -> None:
    REPORT_ASSETS.mkdir(parents=True, exist_ok=True)
    WEEKLY_ASSETS.mkdir(parents=True, exist_ok=True)


def scan_window(
    path: Path,
    time_col: str,
    columns: dict[str, str],
    *,
    window_s: float = WINDOW_S,
) -> dict:
    stats = {name: RunningStats() for name in columns}
    zero = {name: None for name in columns}
    zeroed_stats = {name: RunningStats() for name in columns}
    series: dict[str, list[tuple[float, float]]] = {name: [] for name in columns}
    raw_series: dict[str, list[tuple[float, float]]] = {name: [] for name in columns}
    first_t: float | None = None
    last_t: float | None = None
    rows = 0

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            source_t = float(row[time_col])
            if first_t is None:
                first_t = source_t
            t = source_t - first_t
            if t > window_s:
                break
            last_t = t
            rows += 1
            for name, csv_col in columns.items():
                value = float(row[csv_col])
                if zero[name] is None:
                    zero[name] = value
                stats[name].add(value)
                zeroed = value - float(zero[name])
                zeroed_stats[name].add(zeroed)
                series[name].append((t, zeroed))
                raw_series[name].append((t, value))

    duration = last_t if last_t is not None else 0.0
    return {
        "path": str(path),
        "rows": rows,
        "duration_s": duration,
        "rate_hz": (rows - 1) / duration if rows > 1 and duration > 0 else None,
        "software_zero": zero,
        "stats": {name: item.as_dict() for name, item in stats.items()},
        "zeroed_stats": {name: item.as_dict() for name, item in zeroed_stats.items()},
        "series": series,
        "raw_series": raw_series,
    }


def scan_window_envelope(
    path: Path,
    time_col: str,
    columns: dict[str, str],
    *,
    window_s: float,
    bins: int = 1800,
    chunksize: int = 500_000,
) -> dict:
    raw_stats = {name: VectorStats() for name in columns}
    zeroed_stats = {name: VectorStats() for name in columns}
    front_60s_stats = {name: VectorStats() for name in columns}
    back_60s_stats = {name: VectorStats() for name in columns}
    envelopes = {name: EnvelopeBins(window_s, bins=bins) for name in columns}
    zero: dict[str, float | None] = {name: None for name in columns}
    first_t: float | None = None
    last_t: float | None = None
    rows = 0
    usecols = [time_col, *columns.values()]

    for frame in pd.read_csv(path, usecols=usecols, chunksize=chunksize):
        if first_t is None:
            first_t = float(frame[time_col].iloc[0])
        frame["_elapsed_s"] = frame[time_col].astype(float) - first_t
        frame = frame[frame["_elapsed_s"] <= window_s]
        if frame.empty:
            break
        t_values = frame["_elapsed_s"].to_numpy(dtype=float)
        last_t = float(t_values[-1])
        rows += int(len(frame))
        front_mask = t_values <= 60.0
        back_mask = t_values >= max(0.0, window_s - 60.0)
        for name, csv_col in columns.items():
            raw_values = frame[csv_col].to_numpy(dtype=float)
            if zero[name] is None:
                zero[name] = float(raw_values[0])
            zeroed = raw_values - float(zero[name])
            raw_stats[name].add_many(raw_values)
            zeroed_stats[name].add_many(zeroed)
            if front_mask.any():
                front_60s_stats[name].add_many(zeroed[front_mask])
            if back_mask.any():
                back_60s_stats[name].add_many(zeroed[back_mask])
            envelopes[name].add_many(t_values, zeroed)
        if last_t >= window_s:
            break

    duration = last_t if last_t is not None else 0.0
    front_back_delta = {}
    for name in columns:
        front = front_60s_stats[name].as_dict()
        back = back_60s_stats[name].as_dict()
        front_back_delta[name] = {
            "front_60s_mean": front["mean"],
            "back_60s_mean": back["mean"],
            "back_minus_front": None
            if front["mean"] is None or back["mean"] is None
            else float(back["mean"]) - float(front["mean"]),
        }
    return {
        "path": str(path),
        "rows": rows,
        "duration_s": duration,
        "rate_hz": (rows - 1) / duration if rows > 1 and duration > 0 else None,
        "window_s": window_s,
        "software_zero": zero,
        "stats": {name: item.as_dict() for name, item in raw_stats.items()},
        "zeroed_stats": {name: item.as_dict() for name, item in zeroed_stats.items()},
        "front_back_60s": front_back_delta,
        "envelopes": {name: item.as_dict() for name, item in envelopes.items()},
    }


def envelope(points: list[tuple[float, float]], bins: int = 1200) -> dict[str, list[float]]:
    if not points:
        return {"t": [], "min": [], "max": [], "mean": []}
    t0 = points[0][0]
    t1 = points[-1][0]
    if t1 <= t0:
        value = points[0][1]
        return {"t": [t0], "min": [value], "max": [value], "mean": [value]}
    width = (t1 - t0) / bins
    buckets: list[dict[str, float | int] | None] = [None] * bins
    for t, value in points:
        index = min(bins - 1, max(0, int((t - t0) / width)))
        bucket = buckets[index]
        if bucket is None:
            buckets[index] = {"t_sum": t, "sum": value, "n": 1, "min": value, "max": value}
        else:
            bucket["t_sum"] = float(bucket["t_sum"]) + t
            bucket["sum"] = float(bucket["sum"]) + value
            bucket["n"] = int(bucket["n"]) + 1
            bucket["min"] = min(float(bucket["min"]), value)
            bucket["max"] = max(float(bucket["max"]), value)
    compact = [bucket for bucket in buckets if bucket is not None]
    return {
        "t": [float(bucket["t_sum"]) / int(bucket["n"]) for bucket in compact],
        "min": [float(bucket["min"]) for bucket in compact],
        "max": [float(bucket["max"]) for bucket in compact],
        "mean": [float(bucket["sum"]) / int(bucket["n"]) for bucket in compact],
    }


def plot_envelope(ax: plt.Axes, points: list[tuple[float, float]], label: str, color: str) -> None:
    env = envelope(points)
    ax.fill_between(env["t"], env["min"], env["max"], color=color, alpha=0.18, linewidth=0)
    ax.plot(env["t"], env["mean"], color=color, linewidth=1.6, label=label)


def plot_precomputed_envelope(ax: plt.Axes, env: dict[str, list[float]], label: str, color: str) -> None:
    ax.fill_between(env["t"], env["min"], env["max"], color=color, alpha=0.18, linewidth=0)
    ax.plot(env["t"], env["mean"], color=color, linewidth=1.6, label=label)


def save_and_copy(fig: plt.Figure, filename: str) -> dict[str, str]:
    report_path = REPORT_ASSETS / filename
    fig.savefig(report_path, dpi=180)
    plt.close(fig)
    weekly_path = WEEKLY_ASSETS / filename
    shutil.copy2(report_path, weekly_path)
    return {"report": rel_from_report(report_path), "weekly": rel_from_weekly(weekly_path)}


def build_figures(short_kunwei: dict, short_onrobot: dict, long_kunwei: dict, long_onrobot: dict) -> dict[str, dict[str, str]]:
    figures: dict[str, dict[str, str]] = {}

    fig, axes = plt.subplots(3, 1, figsize=(10.2, 8.2), sharex=True)
    pairs = [
        ("Fz", "Fz_N", "fz_n", "Fz first-zeroed (N)"),
        ("Fx", "Fx_N", "fx_n", "Fx first-zeroed (N)"),
        ("Fy", "Fy_N", "fy_n", "Fy first-zeroed (N)"),
    ]
    for ax, (_, k_col, o_col, ylabel) in zip(axes, pairs):
        plot_envelope(ax, short_kunwei["series"][k_col], "Kunwei TCP raw 1 kHz", "#2f8068")
        plot_envelope(ax, short_onrobot["series"][o_col], "OnRobot UDP raw 500 Hz", "#2e6ea6")
        ax.axhline(0.0, color="#7b8794", linewidth=0.8, linestyle="--")
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.25)
    axes[0].legend(loc="upper right", fontsize=8)
    axes[-1].set_xlabel("Time (s)")
    fig.suptitle("First 600 s force drift comparison, first-sample software zero", y=0.995)
    fig.tight_layout()
    figures["first600_force_axes"] = save_and_copy(fig, "first600_onrobot_kunwei_force_axes_envelope.png")

    fig, ax = plt.subplots(figsize=(10.2, 4.8))
    plot_envelope(ax, short_kunwei["series"]["Fz_N"], "Kunwei TCP raw 1 kHz", "#2f8068")
    plot_envelope(ax, short_onrobot["series"]["fz_n"], "OnRobot UDP raw 500 Hz", "#2e6ea6")
    ax.axhline(0.0, color="#7b8794", linewidth=0.8, linestyle="--")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Fz first-zeroed (N)")
    ax.set_title("Fz drift comparison, first 600 s")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    figures["first600_fz"] = save_and_copy(fig, "first600_onrobot_kunwei_fz_envelope.png")

    fig, ax = plt.subplots(figsize=(8.2, 4.6))
    labels = ["Kunwei Fz", "OnRobot Fz", "Kunwei Fx", "OnRobot Fx", "Kunwei Fy", "OnRobot Fy"]
    values = [
        short_kunwei["zeroed_stats"]["Fz_N"]["std"],
        short_onrobot["zeroed_stats"]["fz_n"]["std"],
        short_kunwei["zeroed_stats"]["Fx_N"]["std"],
        short_onrobot["zeroed_stats"]["fx_n"]["std"],
        short_kunwei["zeroed_stats"]["Fy_N"]["std"],
        short_onrobot["zeroed_stats"]["fy_n"]["std"],
    ]
    colors = ["#2f8068", "#2e6ea6", "#2f8068", "#2e6ea6", "#2f8068", "#2e6ea6"]
    ax.bar(labels, values, color=colors)
    ax.set_ylabel("Std after first-sample zero (N)")
    ax.set_title("First 600 s force noise/drift scale")
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(axis="x", rotation=25)
    fig.tight_layout()
    figures["first600_std"] = save_and_copy(fig, "first600_onrobot_kunwei_force_std.png")

    fig, axes = plt.subplots(3, 1, figsize=(10.2, 8.2), sharex=True)
    for ax, (_, k_col, o_col, ylabel) in zip(axes, pairs):
        plot_precomputed_envelope(ax, long_kunwei["envelopes"][k_col], "Kunwei TCP raw 1 kHz", "#2f8068")
        plot_precomputed_envelope(ax, long_onrobot["envelopes"][o_col], "OnRobot UDP raw 500 Hz", "#2e6ea6")
        ax.axhline(0.0, color="#7b8794", linewidth=0.8, linestyle="--")
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.25)
    axes[0].legend(loc="upper right", fontsize=8)
    axes[-1].set_xlabel("Time (s)")
    fig.suptitle("6 h force drift comparison, first-sample software zero", y=0.995)
    fig.tight_layout()
    figures["sixh_force_axes"] = save_and_copy(fig, "sixh_onrobot_kunwei_force_axes_envelope.png")

    fig, ax = plt.subplots(figsize=(10.2, 4.8))
    plot_precomputed_envelope(ax, long_kunwei["envelopes"]["Fz_N"], "Kunwei TCP raw 1 kHz", "#2f8068")
    plot_precomputed_envelope(ax, long_onrobot["envelopes"]["fz_n"], "OnRobot UDP raw 500 Hz", "#2e6ea6")
    ax.axhline(0.0, color="#7b8794", linewidth=0.8, linestyle="--")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Fz first-zeroed (N)")
    ax.set_title("Fz drift comparison, 6 h")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    figures["sixh_fz"] = save_and_copy(fig, "sixh_onrobot_kunwei_fz_envelope.png")

    fig, ax = plt.subplots(figsize=(8.2, 4.6))
    values = [
        long_kunwei["zeroed_stats"]["Fz_N"]["std"],
        long_onrobot["zeroed_stats"]["fz_n"]["std"],
        long_kunwei["zeroed_stats"]["Fx_N"]["std"],
        long_onrobot["zeroed_stats"]["fx_n"]["std"],
        long_kunwei["zeroed_stats"]["Fy_N"]["std"],
        long_onrobot["zeroed_stats"]["fy_n"]["std"],
    ]
    ax.bar(labels, values, color=colors)
    ax.set_ylabel("Std after first-sample zero (N)")
    ax.set_title("6 h force noise/drift scale")
    ax.grid(axis="y", alpha=0.25)
    ax.tick_params(axis="x", rotation=25)
    fig.tight_layout()
    figures["sixh_std"] = save_and_copy(fig, "sixh_onrobot_kunwei_force_std.png")

    return figures


def rows_for_force_table(kunwei: dict, onrobot: dict) -> str:
    mapping = [
        ("Fx", "Fx_N", "fx_n"),
        ("Fy", "Fy_N", "fy_n"),
        ("Fz", "Fz_N", "fz_n"),
    ]
    rows = [
        "| Sensor | Axis | samples | duration (s) | rate (Hz) | zeroed mean (N) | zeroed std (N) | zeroed last-first (N) | raw min/max (N) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for axis, k_col, o_col in mapping:
        for label, data, col in [
            ("Kunwei TCP raw", kunwei, k_col),
            ("OnRobot UDP raw", onrobot, o_col),
        ]:
            z = data["zeroed_stats"][col]
            raw = data["stats"][col]
            rows.append(
                "| "
                + " | ".join(
                    [
                        label,
                        axis,
                        fmt(data["rows"]),
                        fmt(data["duration_s"], 3),
                        fmt(data["rate_hz"], 3),
                        fmt(z["mean"], 4),
                        fmt(z["std"], 4),
                        fmt(z["last_first"], 4),
                        f"{fmt(raw['min'], 3)} / {fmt(raw['max'], 3)}",
                    ]
                )
                + " |"
            )
    return "\n".join(rows)


def rows_for_long_force_table(kunwei: dict, onrobot: dict) -> str:
    mapping = [
        ("Fx", "Fx_N", "fx_n"),
        ("Fy", "Fy_N", "fy_n"),
        ("Fz", "Fz_N", "fz_n"),
    ]
    rows = [
        "| Sensor | Axis | samples | duration (h) | rate (Hz) | zeroed std (N) | zeroed last-first (N) | back60-front60 mean (N) | raw min/max (N) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for axis, k_col, o_col in mapping:
        for label, data, col in [
            ("Kunwei TCP raw", kunwei, k_col),
            ("OnRobot UDP raw", onrobot, o_col),
        ]:
            z = data["zeroed_stats"][col]
            raw = data["stats"][col]
            fb = data["front_back_60s"][col]
            rows.append(
                "| "
                + " | ".join(
                    [
                        label,
                        axis,
                        fmt(data["rows"]),
                        fmt(data["duration_s"] / 3600.0, 3),
                        fmt(data["rate_hz"], 3),
                        fmt(z["std"], 4),
                        fmt(z["last_first"], 4),
                        fmt(fb["back_minus_front"], 4),
                        f"{fmt(raw['min'], 3)} / {fmt(raw['max'], 3)}",
                    ]
                )
                + " |"
            )
    return "\n".join(rows)


def rows_for_torque_table(kunwei: dict, onrobot: dict) -> str:
    mapping = [
        ("Mx/Tx", "Mx_Nm", "tx_nm"),
        ("My/Ty", "My_Nm", "ty_nm"),
        ("Mz/Tz", "Mz_Nm", "tz_nm"),
    ]
    rows = [
        "| Sensor | Axis | zeroed std (Nm) | zeroed last-first (Nm) | raw min/max (Nm) |",
        "|---|---|---:|---:|---|",
    ]
    for axis, k_col, o_col in mapping:
        for label, data, col in [
            ("Kunwei TCP raw", kunwei, k_col),
            ("OnRobot UDP raw", onrobot, o_col),
        ]:
            z = data["zeroed_stats"][col]
            raw = data["stats"][col]
            rows.append(
                f"| {label} | {axis} | {fmt(z['std'], 5)} | {fmt(z['last_first'], 5)} | {fmt(raw['min'], 5)} / {fmt(raw['max'], 5)} |"
            )
    return "\n".join(rows)


def long_overall_metrics(long_summary: dict) -> dict:
    overall = long_summary["overall"]
    stats = overall.get("stats", overall.get("axes", {}))
    fz = stats["Fz_N"]
    return {
        "samples": overall.get("samples", overall.get("sample_count")),
        "duration_s": overall.get("duration_first_last_s", overall.get("duration_first_to_last_s")),
        "rate_hz": overall.get("rate_hz_by_first_last", overall.get("sample_rate_hz")),
        "parse_errors": overall.get("summary_parse_errors", overall.get("parse_errors")),
        "dropped_sync_bytes": overall.get("summary_dropped_sync_bytes", overall.get("dropped_sync_bytes")),
        "fz_mean": fz["mean"],
        "fz_std": fz["std"],
        "fz_last_first": fz.get("last_first", fz.get("last_minus_first")),
    }


def step2c_metrics(step2c: dict) -> dict:
    summary = step2c["summary"]
    return {
        "run_name": Path(summary["paths"]["summary"]).parent.name,
        "line_completed": "yes" if step2c["stage25_force_all"]["rows"] > 0 and summary["stop_reason"] == "duration" else "partial",
        "bridge_stop_reason": summary["stop_reason"],
        "bridge_writes": summary["bridge_writes"],
        "rtde_output_rate_hz": summary["rtde_output_timing"]["rate_hz"],
        "raw_sensor_real_run_rate_hz": step2c["raw_sensor_real_run"]["rate_hz"],
    }


def strip_plot_data(data: dict) -> dict:
    return {
        key: value
        for key, value in data.items()
        if key not in {"series", "raw_series", "envelopes"}
    }


def write_summary_json(short_kunwei: dict, short_onrobot: dict, long_kunwei: dict, long_onrobot: dict, figures: dict, long_summary: dict, step2c: dict) -> Path:
    payload = {
        "comparison_note": "Both streams use first-sample software zero in their own selected windows; this is not a same-fixture absolute calibration comparison.",
        "available_duration": {
            "kunwei_s": 69325.77287676797,
            "onrobot_udp_s": COMMON_MAX_WINDOW_S,
            "common_max_s": COMMON_MAX_WINDOW_S,
            "report_long_window_s": LONG_WINDOW_S,
        },
        "short_600s": {
            "window_s": WINDOW_S,
            "kunwei_tcp_raw": strip_plot_data(short_kunwei),
            "onrobot_udp_raw": strip_plot_data(short_onrobot),
        },
        "long_6h": {
            "window_s": LONG_WINDOW_S,
            "kunwei_tcp_raw": strip_plot_data(long_kunwei),
            "onrobot_udp_raw": strip_plot_data(long_onrobot),
        },
        "long_run_overall": long_summary.get("overall", {}),
        "step2c_summary": step2c.get("summary", {}),
        "figures": figures,
    }
    out = REPORT_ASSETS / "analysis-summary.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    shutil.copy2(out, WEEKLY_ASSETS / out.name)
    return out


def build_markdown(short_kunwei: dict, short_onrobot: dict, long_kunwei: dict, long_onrobot: dict, figures: dict, summary_json: Path, long_summary: dict, step2c: dict) -> str:
    overall = long_overall_metrics(long_summary)
    stage25 = step2c["stage25_force_all"]
    stage25_after = step2c["stage25_force_after_0p5s"]
    path_metrics = step2c["path_metrics"]
    raw_stage25 = step2c["raw_sensor_stage25"]
    stage25_echo = step2c["stage25_echo"]
    summary = step2c_metrics(step2c)

    return f"""# Kunwei KWR75 当前进展报告（2026-06-08）

## 实验目的

这份报告把 Kunwei KWR75/KWR75B 当前证据单独整理出来，用于说明三件事：传感器与通信链路是否已经可用，长时间无运动 `1 kHz` 采集是否稳定，以及当前 Step2C 闭环直线实验走到什么程度。报告包含两层 OnRobot/Kunwei 对比：前 `600 s` 用于短窗口 noise/drift 判断，`6 h` 用于长时间漂移判断。两者都只作为 drift/noise 口径对照，不作为同机械状态下的绝对标定结论。

结论先给出：Kunwei TCP raw logging 已经支撑 `19 h 15 min`、约 `1 kHz`、无 parse error 的长跑；Step2C 已经完成 `search5 + guard20` 下的闭环直线，力均值能靠近 `-5 N`，但进入 line 阶段的瞬态和 Fz 波动仍是主要问题。机器人侧运动闭环频率不能写成 `500 Hz`，本轮 stage25 echo/motion gate 实测约 `{fmt(stage25_echo['rate_hz'], 2)} Hz`。

## 设备与实验条件

| 项目 | 当前口径 |
|---|---|
| 传感器 | Kunwei KWR75/KWR75B 六轴力/力矩传感器 |
| 当前主采集路线 | Ubuntu TCP client -> `192.168.50.25:5152`，converted result stream |
| Ubuntu bench IP | `192.168.50.26/24` on `enp3s0` |
| Vendor GUI 状态 | Windows 11 原生 `SensorLinker.exe` 已验证 live 数据与 CSV 记录；Ubuntu/Wine 不是当前默认路线 |
| 长时采集状态 | 无机器人运动、无接触操作，只测传感器通信和静态读数 |
| Step2C zero 口径 | bridge 软件 baseline；未调用 Kunwei hardware tare，未调用 UR `zero_ftsensor()` |
| Step2C 参考线 | 长度约 `63.58 mm` 的 XY straight-line reference |
| 本报告图表口径 | 统计用选定窗口内全样本；长 trace 图用 min/max envelope，不用等间隔抽样线作为主证据 |
| 最长可用公共窗口 | Kunwei `19.26 h`，OnRobot UDP `8.79 h`；本报告长对比采用更适合汇报的 `6 h` |

## 实验命令

长时采集由 `capture_kunwei_kwr75_1khz.py` 运行，核心参数是 `--transport tcp-client --sensor-ip 192.168.50.25 --sensor-port 5152 --duration-s 86400 --checkpoint-interval-s 900`。Step2C 主 run 使用 `search5_guard20_line2ms_alpha70_vlim5` 版本，bridge 以 `--rtde-hz 500 --sensor-stale-s 0.10 --target-force-n 5 --max-normal-force-n 20` 运行。

本报告的生成脚本只读取已有 CSV/JSON 并写出报告资产，没有向 UR、OnRobot 或 Kunwei 发送命令，也没有做视频抽帧。

## 数据与图片

| artifact | 路径 |
|---|---|
| 本报告 summary | [{rel_from_report(summary_json)}]({rel_from_report(summary_json)}) |
| Kunwei 19h15min raw CSV | [../ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/data.csv](../ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/data.csv) |
| Kunwei 19h15min logger summary | [../ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/summary.json](../ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/summary.json) |
| Step2C metrics | [assets/step2c-kunwei-search5-guard20-line2ms/analysis-metrics.json](assets/step2c-kunwei-search5-guard20-line2ms/analysis-metrics.json) |
| OnRobot 600s UDP raw CSV | [../experiments/20260528_onrobot_three_stream_600s_first_zero/run_20260528_043100/three_stream_600s_20260528_043052_onrobot_udp500_raw.csv](../experiments/20260528_onrobot_three_stream_600s_first_zero/run_20260528_043100/three_stream_600s_20260528_043052_onrobot_udp500_raw.csv) |
| OnRobot 6h UDP raw CSV | [../experiments/20260530_onrobot_three_stream_coldstart_drift/run_20260530_175217/three_stream_24h_20260530_20260530_175220_onrobot_udp500_raw.csv](../experiments/20260530_onrobot_three_stream_coldstart_drift/run_20260530_175217/three_stream_24h_20260530_20260530_175220_onrobot_udp500_raw.csv) |

图 1 是本报告最主要的 OnRobot/Kunwei 前 `600 s` 对比图。两条曲线都先减去各自窗口第一帧，因此显示的是本窗口内的相对变化。阴影是每个时间 bin 内的 min/max envelope，实线是 bin mean；统计表仍使用窗口内所有样本。

![OnRobot vs Kunwei first 600s force axes]({figures['first600_force_axes']['report']})

图 2 单独展开 Fz。Kunwei 前 `600 s` 的 first-zeroed Fz 标准差是 `{fmt(short_kunwei['zeroed_stats']['Fz_N']['std'], 4)} N`，OnRobot UDP raw 是 `{fmt(short_onrobot['zeroed_stats']['fz_n']['std'], 4)} N`。这个数值不能直接解释成传感器规格优劣，因为两个窗口的安装、载荷和日期不同。

![OnRobot vs Kunwei first 600s Fz]({figures['first600_fz']['report']})

图 3 把前三个力轴的 first-zeroed 标准差放在同一张图里，用于快速看 `600 s` 窗口内的波动量级。

![OnRobot vs Kunwei first 600s std]({figures['first600_std']['report']})

图 4 是本次新增的 `6 h` 长时间 Fz 对比。当前本地数据的最长公共窗口是 `8.79 h`，但本报告采用 `6 h` 作为主图口径，避免把会议汇报拖进过长的历史细节。统计仍使用 `6 h` 内全样本，图中阴影仍是 min/max envelope。

![OnRobot vs Kunwei 6h Fz]({figures['sixh_fz']['report']})

图 5 展示 `6 h` 的 Fx/Fy/Fz 三轴上下文。它用于判断 Fz 漂移是否伴随横向力变化。

![OnRobot vs Kunwei 6h force axes]({figures['sixh_force_axes']['report']})

图 6 是 `6 h` 窗口下三个力轴的 first-zeroed 标准差。

![OnRobot vs Kunwei 6h std]({figures['sixh_std']['report']})

## 统计结果

### Kunwei 19h15min 长时采集

| 指标 | 数值 |
|---|---:|
| 样本数 | `{fmt(overall['samples'])}` |
| first-to-last duration | `{fmt(overall['duration_s'], 3)} s` |
| 平均采样率 | `{fmt(overall['rate_hz'], 6)} Hz` |
| parse errors | `{fmt(overall['parse_errors'])}` |
| dropped sync bytes | `{fmt(overall['dropped_sync_bytes'])}` |
| Fz mean/std | `{fmt(overall['fz_mean'], 4)} / {fmt(overall['fz_std'], 4)} N` |
| Fz last-first | `{fmt(overall['fz_last_first'], 4)} N` |

这条结果支持把 Kunwei TCP route 作为当前 bench 的可用 `1 kHz` logging route。它仍不是完整 24h 结论，因为用户在约 `19 h 15 min` 主动停止。

### Step2C 闭环直线进展

| 项目 | 结果 |
|---|---:|
| 主 run | `{summary['run_name']}` |
| 是否完成 line | `{summary['line_completed']}` |
| bridge stop reason | `{summary['bridge_stop_reason']}` |
| bridge writes | `{fmt(summary['bridge_writes'])}` |
| RTDE output rate | `{fmt(summary['rtde_output_rate_hz'], 2)} Hz` |
| raw sensor real-run rate | `{fmt(summary['raw_sensor_real_run_rate_hz'], 2)} Hz` |
| stage25 echo rate | `{fmt(stage25_echo['rate_hz'], 2)} Hz` |
| stage25 Fz mean/std | `{fmt(stage25['fz_mean_n'], 2)} / {fmt(stage25['fz_std_n'], 2)} N` |
| stage25 error MAE | `{fmt(stage25['signed_error_mae_n'], 2)} N` |
| stage25 after 0.5s error MAE | `{fmt(stage25_after['signed_error_mae_n'], 2)} N` |
| raw stage25 Fz min | `{fmt(raw_stage25['fz_min_n'], 2)} N` |
| XY error mean / p95 | `{fmt(path_metrics['xy_error_mean_mm'], 3)} / {fmt(path_metrics['xy_error_p95_mm'], 3)} mm` |

这说明本轮主要瓶颈不是路径跟踪：XY error mean 约 `{fmt(path_metrics['xy_error_mean_mm'], 3)} mm`，p95 约 `{fmt(path_metrics['xy_error_p95_mm'], 3)} mm`。下一步应优先降低进入 line 阶段的力瞬态和稳态 Fz 波动。

### OnRobot vs Kunwei 前 600s

{rows_for_force_table(short_kunwei, short_onrobot)}

{rows_for_torque_table(short_kunwei, short_onrobot)}

比较限制必须写清楚：Kunwei 的前 `600 s` 来自 `19h15min` 未归零静态长跑，OnRobot 来自 `20260528` 的 dedicated `600s_first_zero` run；两者不是同一天、同治具、同预载的同步 A/B。这里能比较的是当前可用 raw stream 在自身 first-zero 口径下的短窗口稳定性和采样路线差异。

### OnRobot vs Kunwei 6h

本地可用数据里，Kunwei 最长为 `19.26 h`，OnRobot UDP raw 最长为 `8.79 h`，两者最长公共窗口为 `8.79 h`。本报告采用 `6 h` 作为长时间对比主口径；这个窗口已经足够覆盖慢漂移趋势，也更适合会议图表。

{rows_for_long_force_table(long_kunwei, long_onrobot)}

这个 `6 h` 对比仍然不是严格同治具同步 A/B。它更适合回答“当前两条 raw stream 的长窗口稳定性量级如何”，不适合回答“哪个传感器绝对零点更准”。

## 结论

1. Kunwei TCP raw logging 路线已经可用：`19 h 15 min` 内约 `1 kHz`，`parse_errors=0`，`dropped_sync_bytes=0`。
2. Kunwei 已经从传感器 bring-up 进入机器人闭环验证阶段。Step2C 主 run 能完成搜索、直线、卸载和回撤；均值层面能围绕 `-5 N` 工作。
3. 当前不能把 Step2C 写成机器人侧 `500 Hz` 闭环。bridge/RTDE logging 是 500Hz 级，但 URScript stage25 echo/motion gate 约 `{fmt(stage25_echo['rate_hz'], 2)} Hz`。
4. OnRobot/Kunwei 前 `600 s` 与 `6 h` 对比图说明两条 raw stream 都可以做短窗口和长窗口漂移分析；但由于机械状态不同，报告只解释相对漂移和波动，不解释绝对偏置或规格优劣。

## 下一步

- Step2C 默认加入 settle stage，或先把 `normal velocity limit` 从 `±5 mm/s` 降到 `±3 mm/s`、`alpha` 从 `0.70` 降到 `0.50`，目标是降低 stage25 开头瞬态。
- 如果要正式做 OnRobot vs Kunwei A/B，应在同一机械状态、同一无接触窗口、明确 zero/tare 策略下同步或连续采集，不能把当前两个历史窗口当成严格标定对照。
- 如果目标是机器人侧 `500 Hz` 运动闭环，需要另开 `servoj/speedj`、多线程 URScript 或外部实时接口路线，而不是从当前 `speedl` echo 推断。

## 附录

### 报告生成命令

```bash
python3 /home/andy/ur10e_ros2_ws/weekly_meeting/build_kunwei_kwr75_report.py
```

### 生成口径

脚本对 `600 s` 窗口保留短窗口点列；对 `6 h` 窗口只保留统计量和时间 bin envelope，不把千万级样本全部留在内存里。统计直接使用窗口内所有样本；图形先按时间 bin 聚合为 min/max/mean envelope，保留尖峰范围，不使用等间隔抽样折线作为主要证据。HTML deck 只使用生成的数据图，没有抽取或嵌入新的视频帧。
"""


def html_metric(label: str, value: str) -> str:
    return f"<div class=\"metric\"><span>{label}</span><strong>{value}</strong></div>"


def build_html(short_kunwei: dict, short_onrobot: dict, long_kunwei: dict, long_onrobot: dict, figures: dict, long_summary: dict, step2c: dict) -> str:
    overall = long_overall_metrics(long_summary)
    stage25 = step2c["stage25_force_all"]
    path_metrics = step2c["path_metrics"]
    stage25_echo = step2c["stage25_echo"]
    summary = step2c_metrics(step2c)
    force_img = figures["first600_force_axes"]["weekly"]
    fz_img = figures["first600_fz"]["weekly"]
    std_img = figures["first600_std"]["weekly"]
    sixh_force_img = figures["sixh_force_axes"]["weekly"]
    sixh_fz_img = figures["sixh_fz"]["weekly"]
    sixh_std_img = figures["sixh_std"]["weekly"]

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kunwei KWR75 Progress Report</title>
  <style>
    :root {{
      --ink: #172026;
      --muted: #5c6872;
      --line: #d9e0e6;
      --panel: #f6f8fa;
      --green: #2f8068;
      --blue: #2e6ea6;
      --amber: #9a6b22;
      --white: #ffffff;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      color: var(--ink);
      background: #eef2f5;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.45;
    }}
    nav {{
      position: sticky;
      top: 0;
      z-index: 5;
      display: flex;
      gap: 10px;
      align-items: center;
      padding: 10px 22px;
      background: rgba(255,255,255,.94);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(8px);
      overflow-x: auto;
    }}
    nav a {{
      color: var(--muted);
      text-decoration: none;
      white-space: nowrap;
      font-size: 13px;
      font-weight: 650;
      padding: 6px 10px;
      border: 1px solid transparent;
      border-radius: 6px;
    }}
    nav a:hover {{ border-color: var(--line); color: var(--ink); background: var(--panel); }}
    main {{ width: min(1320px, 100%); margin: 0 auto; }}
    section {{
      min-height: calc(100vh - 48px);
      padding: 42px 36px 48px;
      background: var(--white);
      border-bottom: 1px solid var(--line);
      scroll-margin-top: 58px;
    }}
    .eyebrow {{
      color: var(--blue);
      font-size: 13px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0;
      margin-bottom: 8px;
    }}
    h1, h2, h3 {{ margin: 0; letter-spacing: 0; }}
    h1 {{ font-size: 44px; line-height: 1.06; max-width: 940px; overflow-wrap: anywhere; }}
    h2 {{ font-size: 34px; line-height: 1.1; margin-bottom: 14px; }}
    h3 {{ font-size: 18px; margin-bottom: 8px; }}
    p {{ color: var(--muted); max-width: 880px; margin: 12px 0 0; overflow-wrap: anywhere; }}
    .lead {{ font-size: 19px; max-width: 940px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 18px;
      margin-top: 26px;
      align-items: stretch;
    }}
    .span-3 {{ grid-column: span 3; }}
    .span-4 {{ grid-column: span 4; }}
    .span-5 {{ grid-column: span 5; }}
    .span-6 {{ grid-column: span 6; }}
    .span-7 {{ grid-column: span 7; }}
    .span-8 {{ grid-column: span 8; }}
    .span-12 {{ grid-column: span 12; }}
    .metric {{
      grid-column: span 3;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 15px 16px;
      min-height: 90px;
    }}
    .metric span {{ display: block; color: var(--muted); font-size: 13px; margin-bottom: 8px; }}
    .metric strong {{ display: block; font-size: 24px; line-height: 1.1; overflow-wrap: anywhere; }}
    figure {{
      margin: 0;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }}
    figure img {{
      width: 100%;
      min-height: 260px;
      object-fit: contain;
      background: #fff;
      padding: 8px;
      display: block;
    }}
    figcaption {{
      min-height: 45px;
      padding: 10px 12px;
      color: var(--muted);
      font-size: 13px;
      border-top: 1px solid var(--line);
      background: #fff;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
      background: #fff;
      border: 1px solid var(--line);
    }}
    th, td {{
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{ background: var(--panel); font-size: 12px; text-transform: uppercase; color: var(--muted); }}
    .note {{ color: var(--amber); font-weight: 700; }}
    @media (max-width: 860px) {{
      section {{ padding: 28px 18px 34px; }}
      h1 {{ font-size: 34px; }}
      h2 {{ font-size: 28px; }}
      .grid {{ grid-template-columns: 1fr; }}
      .grid > * {{ grid-column: span 1 !important; }}
      nav {{ padding: 9px 12px; }}
    }}
  </style>
</head>
<body>
  <nav>
    <a href="#summary">Summary</a>
    <a href="#link">1 kHz Link</a>
    <a href="#step2c">Step2C</a>
    <a href="#compare">600 s Compare</a>
    <a href="#long-compare">6 h Compare</a>
    <a href="#next">Next</a>
  </nav>
  <main>
    <section id="summary">
      <div class="eyebrow">Kunwei KWR75 / UR10e</div>
      <h1>Kunwei force sensor progress report</h1>
      <p class="lead">Kunwei is no longer just a bring-up task: the TCP raw logging path has a stable 19 h 15 min run, and Step2C has completed a closed-loop straight-line contact task. The remaining issue is force transient and Fz stability, not basic connectivity.</p>
      <div class="grid">
        {html_metric("Long raw capture", "69.3M samples")}
        {html_metric("Average raw rate", f"{fmt(overall['rate_hz'], 3)} Hz")}
        {html_metric("Frame errors", "0 parse / 0 sync")}
        {html_metric("Step2C state", "line completed")}
        {html_metric("Long comparison", "6 h selected")}
      </div>
    </section>
    <section id="link">
      <div class="eyebrow">Sensor link</div>
      <h2>1 kHz TCP logging is stable for the current bench</h2>
      <p>The 19 h 15 min no-motion run received converted Kunwei frames continuously over TCP. This validates the current Ubuntu collection path, but it is not a 24 h run and it is not a zero-load calibration.</p>
      <div class="grid">
        {html_metric("Duration", f"{fmt(overall['duration_s'] / 3600.0, 3)} h")}
        {html_metric("Fz last-first", f"{fmt(overall['fz_last_first'], 4)} N")}
        {html_metric("Fz std", f"{fmt(overall['fz_std'], 4)} N")}
        {html_metric("Samples", fmt(overall["samples"]))}
      </div>
    </section>
    <section id="step2c">
      <div class="eyebrow">Robot experiment</div>
      <h2>Step2C completed the line, but not as a 500 Hz motion loop</h2>
      <p>The bridge and RTDE logs are 500 Hz class. The URScript stage25 echo/motion gate is about {fmt(stage25_echo['rate_hz'], 2)} Hz, so the report should not call the robot-side motion loop 500 Hz.</p>
      <div class="grid">
        {html_metric("Run", summary["run_name"])}
        {html_metric("Stage25 Fz mean", f"{fmt(stage25['fz_mean_n'], 2)} N")}
        {html_metric("Stage25 error MAE", f"{fmt(stage25['signed_error_mae_n'], 2)} N")}
        {html_metric("XY error p95", f"{fmt(path_metrics['xy_error_p95_mm'], 3)} mm")}
      </div>
    </section>
    <section id="compare">
      <div class="eyebrow">Sensor comparison</div>
      <h2>OnRobot vs Kunwei, first 600 s</h2>
      <p>Both traces are first-sample software zeroed inside their own 600 s windows. The shaded region is a min/max envelope; statistics use all samples in the selected window.</p>
      <div class="grid">
        <figure class="span-12"><img src="{force_img}" alt="First 600 s force axes comparison"><figcaption>Fig. 1. Fx/Fy/Fz first-zeroed envelopes. This compares short-window stability, not absolute bias.</figcaption></figure>
        <figure class="span-7"><img src="{fz_img}" alt="First 600 s Fz comparison"><figcaption>Fig. 2. Fz detail, first-zeroed within each sensor's own run.</figcaption></figure>
        <figure class="span-5"><img src="{std_img}" alt="First 600 s force standard deviation"><figcaption>Fig. 3. Force-axis standard deviation in the first 600 s windows.</figcaption></figure>
      </div>
    </section>
    <section id="long-compare">
      <div class="eyebrow">Sensor comparison</div>
      <h2>OnRobot vs Kunwei, 6 h</h2>
      <p>The local common maximum is 8.79 h, limited by the OnRobot UDP run. This deck uses 6 h as the main long-window comparison so the result stays readable and avoids overfitting the meeting story to a tail segment.</p>
      <div class="grid">
        {html_metric("Kunwei available", "19.26 h")}
        {html_metric("OnRobot available", "8.79 h")}
        {html_metric("Common max", "8.79 h")}
        {html_metric("Selected window", "6.00 h")}
        <figure class="span-12"><img src="{sixh_fz_img}" alt="6 h Fz comparison"><figcaption>Fig. 4. Fz first-zeroed envelope for the selected 6 h comparison window. Statistics use all samples in the window.</figcaption></figure>
        <figure class="span-12"><img src="{sixh_force_img}" alt="6 h force axes comparison"><figcaption>Fig. 5. Fx/Fy/Fz first-zeroed envelopes over 6 h. This is a long-window drift comparison, not an absolute calibration claim.</figcaption></figure>
        <figure class="span-12"><img src="{sixh_std_img}" alt="6 h force standard deviation"><figcaption>Fig. 6. Force-axis standard deviation over the selected 6 h window.</figcaption></figure>
      </div>
    </section>
    <section id="next">
      <div class="eyebrow">Next action</div>
      <h2>Stabilize contact entry before chasing higher frequency</h2>
      <p>The next Step2C change should reduce line-entry force transient: add a settle stage, or minimally lower normal velocity limit to +/-3 mm/s and alpha to 0.50. A formal OnRobot/Kunwei A/B needs same fixture, same zero/tare strategy, and same no-contact window.</p>
      <div class="grid">
        <table class="span-12">
          <thead><tr><th>Decision</th><th>Current evidence</th><th>Default next move</th></tr></thead>
          <tbody>
            <tr><td>Logging route</td><td>Kunwei TCP raw is stable at 1 kHz class</td><td>Use it as the default Kunwei collector</td></tr>
            <tr><td>Force control</td><td>Mean Fz is near target, but transient and ripple remain large</td><td>Add settle or soften normal correction</td></tr>
            <tr><td>Frequency claim</td><td>RTDE/bridge are 500 Hz class; stage25 echo is ~250 Hz</td><td>Keep these frequency layers separate</td></tr>
            <tr><td>A/B comparison</td><td>Existing 600 s windows differ in setup/date/load</td><td>Run a same-fixture comparison if absolute claims are needed</td></tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>
</body>
</html>
"""


def main() -> None:
    ensure_dirs()
    long_summary = load_json(KUNWEI_LONG_REPORT_SUMMARY)
    step2c = load_json(STEP2C_METRICS)
    # Load these to fail early if the referenced provenance changes or disappears.
    load_json(KUNWEI_LONG_SUMMARY)
    load_json(ONROBOT_SUMMARY)
    load_json(ONROBOT_ALIGNMENT)
    load_json(ONROBOT_LONG_SUMMARY)

    short_kunwei = scan_window(
        KUNWEI_LONG_CSV,
        "t_monotonic_s",
        {
            "Fx_N": "Fx_N",
            "Fy_N": "Fy_N",
            "Fz_N": "Fz_N",
            "Mx_Nm": "Mx_Nm",
            "My_Nm": "My_Nm",
            "Mz_Nm": "Mz_Nm",
        },
    )
    short_onrobot = scan_window(
        ONROBOT_UDP_CSV,
        "t_s",
        {
            "fx_n": "fx_n",
            "fy_n": "fy_n",
            "fz_n": "fz_n",
            "tx_nm": "tx_nm",
            "ty_nm": "ty_nm",
            "tz_nm": "tz_nm",
        },
    )
    long_kunwei = scan_window_envelope(
        KUNWEI_LONG_CSV,
        "t_monotonic_s",
        {
            "Fx_N": "Fx_N",
            "Fy_N": "Fy_N",
            "Fz_N": "Fz_N",
            "Mx_Nm": "Mx_Nm",
            "My_Nm": "My_Nm",
            "Mz_Nm": "Mz_Nm",
        },
        window_s=LONG_WINDOW_S,
    )
    long_onrobot = scan_window_envelope(
        ONROBOT_LONG_UDP_CSV,
        "t_s",
        {
            "fx_n": "fx_n",
            "fy_n": "fy_n",
            "fz_n": "fz_n",
            "tx_nm": "tx_nm",
            "ty_nm": "ty_nm",
            "tz_nm": "tz_nm",
        },
        window_s=LONG_WINDOW_S,
    )

    figures = build_figures(short_kunwei, short_onrobot, long_kunwei, long_onrobot)
    summary_json = write_summary_json(short_kunwei, short_onrobot, long_kunwei, long_onrobot, figures, long_summary, step2c)

    REPORT_MD.write_text(
        build_markdown(short_kunwei, short_onrobot, long_kunwei, long_onrobot, figures, summary_json, long_summary, step2c),
        encoding="utf-8",
    )
    REPORT_HTML.write_text(
        build_html(short_kunwei, short_onrobot, long_kunwei, long_onrobot, figures, long_summary, step2c),
        encoding="utf-8",
    )
    print(json.dumps({
        "markdown": str(REPORT_MD),
        "html": str(REPORT_HTML),
        "summary": str(summary_json),
        "report_assets": str(REPORT_ASSETS),
        "weekly_assets": str(WEEKLY_ASSETS),
    }, indent=2))


if __name__ == "__main__":
    main()
