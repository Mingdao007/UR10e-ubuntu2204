#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import os
import shutil
import subprocess
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
STEP2C_V4_RUN_DIR = (
    ROOT
    / "experiments/kunwei/closed-loop-straight-line/2026-06-04/runs/"
    "bridge_step2c_admittance_search30_v4_search2ms_line1ms_alpha70_20260608_135945"
)
STEP2C_RESULT_RUN_DIR = (
    ROOT
    / "experiments/kunwei/closed-loop-straight-line/2026-06-04/runs/"
    "bridge_step2c_final_autowatch_search2ms_line1ms_alpha70_20260608_154123"
)
STEP2C_RESULT_LABEL = "Step2C final"
STEP2C_RESULT_FILE_PREFIX = "step2c_final"
STEP2C_REFERENCE = ROOT / "experiments/kunwei/closed-loop-straight-line/2026-06-04/config/straight_line_reference.json"
STEP2C_RESULT_VIDEO_PREVIEW = Path("/home/andy/.cache/codex/phone-photo-intake/previews/IMG_1735_step2c_final.mov")
STEP4D_RUN_DIR = (
    ROOT
    / "experiments/kunwei/closed-loop-straight-line/2026-06-04/runs/"
    "bridge_step4d_circle_v1_autowatch_detsearch_attitude_20260608_165457"
)
STEP4D_RESULT_LABEL = "Step4D circle"
STEP4D_RESULT_FILE_PREFIX = "step4d_circle"

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


def copy_to_assets(source: Path, filename: str) -> dict[str, str] | None:
    if not source.exists():
        return None
    report_path = REPORT_ASSETS / filename
    shutil.copy2(source, report_path)
    weekly_path = WEEKLY_ASSETS / filename
    shutil.copy2(report_path, weekly_path)
    return {"report": rel_from_report(report_path), "weekly": rel_from_weekly(weekly_path)}


def run_media_command(command: list[str]) -> bool:
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def percentile(values: Iterable[float], q: float) -> float | None:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return None
    index = int(round((len(clean) - 1) * q))
    return clean[index]


def mean(values: Iterable[float]) -> float | None:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if not clean:
        return None
    return float(np.mean(clean))


def std(values: Iterable[float]) -> float:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    if len(clean) < 2:
        return 0.0
    return float(np.std(clean, ddof=1))


def analyze_step2c_run(run_dir: Path, *, label: str, file_prefix: str) -> dict:
    bridge_csv = run_dir / "bridge_rtde_500hz.csv"
    sensor_csv = run_dir / "kunwei_sensor_1khz.csv"
    frequency_json = run_dir / "stage_frequency_summary.json"

    bridge = pd.read_csv(bridge_csv)
    sensor = pd.read_csv(sensor_csv)
    frequency = load_json(frequency_json)
    reference = load_json(STEP2C_REFERENCE)["reference_line"]
    start = reference["contact_start_xyz_m"]
    unit = reference["xy_unit_vector"]
    reference_length_m = float(reference["xy_length_m"])

    stage25 = bridge[np.isclose(bridge["ur_output_double_register_35"].astype(float), 25.0, atol=0.05)].copy()
    if stage25.empty:
        raise RuntimeError(f"no stage25 rows in {bridge_csv}")

    t0 = float(stage25["t_monotonic_s"].iloc[0])
    stage25["_stage_t_s"] = stage25["t_monotonic_s"].astype(float) - t0
    stage25["_fz_error_n"] = stage25["fz_n_zeroed"].astype(float) + 5.0

    dx = stage25["ur_actual_TCP_pose_0"].astype(float) - float(start[0])
    dy = stage25["ur_actual_TCP_pose_1"].astype(float) - float(start[1])
    progress = dx * float(unit[0]) + dy * float(unit[1])
    projected_x = float(start[0]) + progress * float(unit[0])
    projected_y = float(start[1]) + progress * float(unit[1])
    xy_error = np.sqrt(
        (stage25["ur_actual_TCP_pose_0"].astype(float) - projected_x) ** 2
        + (stage25["ur_actual_TCP_pose_1"].astype(float) - projected_y) ** 2
    )
    cross_error = dx * (-float(unit[1])) + dy * float(unit[0])

    raw_stage25 = sensor[
        (sensor["t_monotonic_s"].astype(float) >= float(stage25["t_monotonic_s"].iloc[0]))
        & (sensor["t_monotonic_s"].astype(float) <= float(stage25["t_monotonic_s"].iloc[-1]))
    ].copy()
    raw_all_duration = float(sensor["t_monotonic_s"].iloc[-1] - sensor["t_monotonic_s"].iloc[0])
    raw_stage25_duration = float(raw_stage25["t_monotonic_s"].iloc[-1] - raw_stage25["t_monotonic_s"].iloc[0])

    stage25_duration = float(stage25["_stage_t_s"].iloc[-1])
    dx_path = float(stage25["ur_actual_TCP_pose_0"].iloc[-1] - stage25["ur_actual_TCP_pose_0"].iloc[0])
    dy_path = float(stage25["ur_actual_TCP_pose_1"].iloc[-1] - stage25["ur_actual_TCP_pose_1"].iloc[0])
    fz_values = stage25["fz_n_zeroed"].astype(float).to_numpy()
    abs_error = np.abs(stage25["_fz_error_n"].to_numpy())

    return {
        "label": label,
        "file_prefix": file_prefix,
        "run_name": run_dir.name,
        "run_dir": str(run_dir),
        "missing_data_requests": [],
        "reference_length_m": reference_length_m,
        "frequency": frequency,
        "stage25": {
            "rows": int(len(stage25)),
            "duration_s": stage25_duration,
            "rtde_row_rate_hz": (len(stage25) - 1) / stage25_duration if stage25_duration > 0 else None,
            "echo_rate_hz": frequency["stage25_ft_line_control_echo_rate"]["echo_rate_hz"],
            "fz_mean_n": float(np.mean(fz_values)),
            "fz_std_n": float(np.std(fz_values, ddof=1)),
            "fz_min_n": float(np.min(fz_values)),
            "fz_max_n": float(np.max(fz_values)),
            "signed_error_mean_n": float(np.mean(stage25["_fz_error_n"])),
            "signed_error_mae_n": float(np.mean(abs_error)),
            "signed_error_p95_abs_n": percentile(abs_error, 0.95),
            "cmdz_mean_mm_s": mean(stage25["ur_output_double_register_33"].astype(float) * 1000.0),
            "cmdz_min_mm_s": float(stage25["ur_output_double_register_33"].astype(float).min() * 1000.0),
            "cmdz_max_mm_s": float(stage25["ur_output_double_register_33"].astype(float).max() * 1000.0),
            "xy_displacement_mm": math.hypot(dx_path, dy_path) * 1000.0,
            "progress_start_mm": float(progress.iloc[0] * 1000.0),
            "progress_end_mm": float(progress.iloc[-1] * 1000.0),
            "xy_error_mean_mm": float(np.mean(xy_error) * 1000.0),
            "xy_error_p95_mm": percentile(xy_error * 1000.0, 0.95),
            "xy_error_max_mm": float(np.max(xy_error) * 1000.0),
            "cross_error_abs_mean_mm": float(np.mean(np.abs(cross_error)) * 1000.0),
            "cross_error_abs_p95_mm": percentile(np.abs(cross_error) * 1000.0, 0.95),
            "z_start_mm": float(stage25["ur_actual_TCP_pose_2"].iloc[0] * 1000.0),
            "z_end_mm": float(stage25["ur_actual_TCP_pose_2"].iloc[-1] * 1000.0),
        },
        "raw_sensor": {
            "rows": int(len(sensor)),
            "duration_s": raw_all_duration,
            "rate_hz": (len(sensor) - 1) / raw_all_duration if raw_all_duration > 0 else None,
            "fz_min_n": float(sensor["fz_n_zeroed"].astype(float).min()),
            "fz_mean_n": float(sensor["fz_n_zeroed"].astype(float).mean()),
            "fz_max_n": float(sensor["fz_n_zeroed"].astype(float).max()),
            "force_norm_max_n": float(sensor["force_norm_n"].astype(float).max()),
            "torque_norm_max_nm": float(sensor["torque_norm_nm"].astype(float).max()),
        },
        "raw_stage25": {
            "rows": int(len(raw_stage25)),
            "duration_s": raw_stage25_duration,
            "rate_hz": (len(raw_stage25) - 1) / raw_stage25_duration if raw_stage25_duration > 0 else None,
            "fz_min_n": float(raw_stage25["fz_n_zeroed"].astype(float).min()),
            "fz_mean_n": float(raw_stage25["fz_n_zeroed"].astype(float).mean()),
            "fz_max_n": float(raw_stage25["fz_n_zeroed"].astype(float).max()),
            "force_norm_max_n": float(raw_stage25["force_norm_n"].astype(float).max()),
            "torque_norm_max_nm": float(raw_stage25["torque_norm_nm"].astype(float).max()),
        },
        "series": {
            "stage25_t_s": stage25["_stage_t_s"].to_numpy(dtype=float),
            "fz_n": fz_values,
            "fz_error_n": stage25["_fz_error_n"].to_numpy(dtype=float),
            "progress_mm": progress.to_numpy(dtype=float) * 1000.0,
            "xy_error_mm": xy_error.to_numpy(dtype=float) * 1000.0,
            "x_mm": stage25["ur_actual_TCP_pose_0"].to_numpy(dtype=float) * 1000.0,
            "y_mm": stage25["ur_actual_TCP_pose_1"].to_numpy(dtype=float) * 1000.0,
            "reference_x_mm": projected_x.to_numpy(dtype=float) * 1000.0,
            "reference_y_mm": projected_y.to_numpy(dtype=float) * 1000.0,
        },
    }


def strip_step2c_run_series(step2c_run: dict) -> dict:
    return {key: value for key, value in step2c_run.items() if key != "series"}


def analyze_step4d_run(run_dir: Path, *, label: str, file_prefix: str) -> dict:
    bridge_csv = run_dir / "bridge_rtde_500hz.csv"
    summary_json = run_dir / "step4d_circle_analysis_summary.json"
    frequency_json = run_dir / "stage_frequency_summary.json"
    metadata_json = run_dir / "metadata.json"

    bridge = pd.read_csv(bridge_csv)
    summary = load_json(summary_json)
    if "baseline_step2c_v4" in summary:
        summary["baseline_step2c_v4"]["note"] = (
            "Use only as prior Step2C baseline; Step4D changes geometry and attitude compliance."
        )
    frequency = load_json(frequency_json)
    metadata = load_json(metadata_json)
    config = load_json(STEP2C_REFERENCE)
    reference = config["reference_line"]
    circle_cfg = config["step2d_circle"]
    unit = reference["xy_unit_vector"]

    stage25 = bridge[np.isclose(bridge["ur_output_double_register_35"].astype(float), 25.0, atol=0.05)].copy()
    if "ur_runtime_state" in stage25.columns:
        stage25 = stage25[np.isclose(stage25["ur_runtime_state"].astype(float), 2.0, atol=0.05)].copy()
    speed_cols = [f"ur_actual_TCP_speed_{idx}" for idx in range(3)]
    if all(col in stage25.columns for col in speed_cols):
        speed_norm = np.sqrt(sum(stage25[col].astype(float) ** 2 for col in speed_cols))
        min_tcp_speed = float(summary.get("selection", {}).get("min_tcp_speed_m_s", 0.001))
        stage25 = stage25[speed_norm >= min_tcp_speed].copy()
    if stage25.empty:
        raise RuntimeError(f"no moving stage25 rows in {bridge_csv}")

    t0 = float(stage25["t_monotonic_s"].iloc[0])
    stage25["_stage_t_s"] = stage25["t_monotonic_s"].astype(float) - t0
    normal_values = stage25["normal_force_n"].astype(float).to_numpy()
    target = float(summary["target_force_n"])
    stage25["_normal_error_n"] = stage25["normal_force_n"].astype(float) + target

    radius = float(summary["circle"]["radius_m"])
    progress = stage25["ur_output_double_register_31"].astype(float).to_numpy()
    theta = progress / radius if radius > 0 else np.zeros_like(progress)
    x0 = float(stage25["ur_actual_TCP_pose_0"].iloc[0])
    y0 = float(stage25["ur_actual_TCP_pose_1"].iloc[0])
    ux = float(unit[0])
    uy = float(unit[1])
    direction = 1.0 if float(circle_cfg.get("circle_direction", 1.0)) >= 0 else -1.0
    e1x = direction * -uy
    e1y = direction * ux
    desired_x = x0 + radius * (1.0 - np.cos(theta)) * ux + radius * np.sin(theta) * e1x
    desired_y = y0 + radius * (1.0 - np.cos(theta)) * uy + radius * np.sin(theta) * e1y

    x = stage25["ur_actual_TCP_pose_0"].astype(float).to_numpy()
    y = stage25["ur_actual_TCP_pose_1"].astype(float).to_numpy()
    xy_error = np.sqrt((x - desired_x) ** 2 + (y - desired_y) ** 2) * 1000.0
    center_x, center_y = [float(value) for value in summary["circle"]["derived_center_xy_m"]]
    radial_error = (np.sqrt((x - center_x) ** 2 + (y - center_y) ** 2) - radius) * 1000.0
    stage25_duration = float(stage25["_stage_t_s"].iloc[-1])
    stop_counts = {str(key): int(value) for key, value in summary.get("stop_reason_counts", {}).items()}

    return {
        "label": label,
        "file_prefix": file_prefix,
        "run_name": run_dir.name,
        "run_dir": str(run_dir),
        "bridge_csv": str(bridge_csv),
        "summary": summary,
        "frequency": frequency,
        "metadata": metadata,
        "stage25": {
            "rows": int(summary["samples_stage25"]),
            "duration_s": stage25_duration,
            "rtde_row_rate_hz": (len(stage25) - 1) / stage25_duration if stage25_duration > 0 else None,
            "echo_rate_hz": frequency["stage25_ft_line_control_echo_rate"]["echo_rate_hz"],
            "bridge_write_rate_hz": frequency["bridge_write_rate_hz"],
            "rtde_output_logging_rate_hz": frequency["rtde_output_logging_rate_hz"],
            "target_force_n": target,
            "normal_force_mean_n": summary["normal_force_n"]["mean"],
            "normal_force_std_n": summary["normal_force_n"]["std"],
            "normal_force_min_n": summary["normal_force_n"]["min"],
            "normal_force_max_n": summary["normal_force_n"]["max"],
            "normal_error_mean_n": summary["signed_normal_target_error_n"]["mean"],
            "normal_error_mae_n": summary["abs_normal_target_error_n"]["mean"],
            "normal_error_p95_abs_n": summary["abs_normal_target_error_n"]["p95"],
            "normal_error_max_abs_n": summary["abs_normal_target_error_n"]["max"],
            "lateral_force_p95_n": summary["lateral_force_n"]["p95"],
            "torque_norm_p95_nm": summary["torque_norm_nm"]["p95"],
        },
        "circle": {
            "completed": stop_counts.get("1", 0) > 0,
            "stop_reason_counts": stop_counts,
            "radius_mm": radius * 1000.0,
            "arc_progress_end_mm": summary["circle"]["arc_progress_end_m"] * 1000.0,
            "theta_end_rad": summary["circle"]["theta_end_rad"],
            "closure_error_mm": summary["circle"]["closure_error_mm"],
            "radial_error_mean_mm": summary["circle"]["radial_error_mm"]["mean"],
            "radial_error_p95_mm": summary["circle"]["radial_error_mm"]["p95"],
            "radial_error_max_mm": summary["circle"]["radial_error_mm"]["max"],
            "xy_error_mean_mm": summary["circle"]["xy_tracking_error_mm"]["mean"],
            "xy_error_p95_mm": summary["circle"]["xy_tracking_error_mm"]["p95"],
            "xy_error_max_mm": summary["circle"]["xy_tracking_error_mm"]["max"],
        },
        "series": {
            "stage25_t_s": stage25["_stage_t_s"].to_numpy(dtype=float),
            "normal_force_n": normal_values,
            "normal_error_n": stage25["_normal_error_n"].to_numpy(dtype=float),
            "progress_mm": progress * 1000.0,
            "radial_error_mm": radial_error,
            "xy_error_mm": xy_error,
            "x_mm": x * 1000.0,
            "y_mm": y * 1000.0,
            "reference_x_mm": desired_x * 1000.0,
            "reference_y_mm": desired_y * 1000.0,
        },
    }


def strip_step4d_run_series(step4d_run: dict) -> dict:
    return {key: value for key, value in step4d_run.items() if key != "series"}


def build_step2c_result_figures(result: dict, old_step2c: dict, v4: dict) -> dict[str, dict[str, str]]:
    figures: dict[str, dict[str, str]] = {}
    s = result["series"]
    stage25 = result["stage25"]
    old_stage25 = old_step2c["stage25_force_all"]
    old_path = old_step2c["path_metrics"]
    v4_stage25 = v4["stage25"]
    prefix = result["file_prefix"]
    label = result["label"]

    fig, axes = plt.subplots(2, 1, figsize=(10.2, 6.4), sharex=True)
    axes[0].plot(s["stage25_t_s"], s["fz_n"], color="#2f8068", linewidth=1.1, label=f"{label} measured Fz")
    axes[0].axhline(-5.0, color="#172026", linewidth=0.9, linestyle="--", label="target -5 N")
    axes[0].set_ylabel("Fz (N)")
    axes[0].grid(True, alpha=0.25)
    axes[0].legend(loc="upper right", fontsize=8)
    axes[1].plot(s["stage25_t_s"], s["fz_error_n"], color="#9a6b22", linewidth=1.1)
    axes[1].axhline(0.0, color="#172026", linewidth=0.9, linestyle="--")
    axes[1].axhline(1.0, color="#7b8794", linewidth=0.8, linestyle=":")
    axes[1].axhline(-1.0, color="#7b8794", linewidth=0.8, linestyle=":")
    axes[1].set_xlabel("Stage25 time (s)")
    axes[1].set_ylabel("Fz - target (N)")
    axes[1].grid(True, alpha=0.25)
    fig.suptitle(f"{label} force tracking, stage25", y=0.995)
    fig.tight_layout()
    figures[f"{prefix}_fz_tracking"] = save_and_copy(fig, f"{prefix}_fz_tracking.png")

    fig, axes = plt.subplots(2, 1, figsize=(10.2, 6.0))
    axes[0].plot(s["progress_mm"], s["xy_error_mm"], color="#2e6ea6", linewidth=1.1)
    axes[0].set_ylabel("XY error (mm)")
    axes[0].grid(True, alpha=0.25)
    axes[1].plot(s["reference_x_mm"], s["reference_y_mm"], color="#172026", linestyle="--", linewidth=1.4, label="reference")
    axes[1].plot(s["x_mm"], s["y_mm"], color="#2f8068", linewidth=1.4, label="actual")
    axes[1].set_xlabel("TCP X (mm)")
    axes[1].set_ylabel("TCP Y (mm)")
    axes[1].axis("equal")
    axes[1].grid(True, alpha=0.25)
    axes[1].legend(loc="best", fontsize=8)
    fig.suptitle(f"{label} path tracking, stage25", y=0.995)
    fig.tight_layout()
    figures[f"{prefix}_path_tracking"] = save_and_copy(fig, f"{prefix}_path_tracking.png")

    fig, axes = plt.subplots(1, 2, figsize=(10.2, 4.6))
    axes[0].bar(
        ["old Step2C", "V4", "final"],
        [old_stage25["signed_error_mae_n"], v4_stage25["signed_error_mae_n"], stage25["signed_error_mae_n"]],
        color=["#7b8794", "#9a6b22", "#2f8068"],
    )
    axes[0].set_ylabel("Fz error MAE (N)")
    axes[0].set_title("Force tracking")
    axes[0].grid(axis="y", alpha=0.25)
    axes[1].bar(
        ["old Step2C", "V4", "final"],
        [old_path["xy_error_p95_mm"], v4_stage25["xy_error_p95_mm"], stage25["xy_error_p95_mm"]],
        color=["#7b8794", "#9a6b22", "#2e6ea6"],
    )
    axes[1].set_ylabel("XY error p95 (mm)")
    axes[1].set_title("Path tracking")
    axes[1].grid(axis="y", alpha=0.25)
    fig.suptitle("Step2C tradeoff: previous, V4, final", y=0.995)
    fig.tight_layout()
    figures[f"{prefix}_tradeoff"] = save_and_copy(fig, f"{prefix}_tradeoff.png")
    return figures


def build_step4d_result_figures(result: dict) -> dict[str, dict[str, str]]:
    figures: dict[str, dict[str, str]] = {}
    s = result["series"]
    circle = result["circle"]
    stage25 = result["stage25"]
    prefix = result["file_prefix"]

    fig, ax = plt.subplots(figsize=(8.2, 7.2))
    ax.plot(s["reference_x_mm"], s["reference_y_mm"], color="#172026", linestyle="--", linewidth=1.4, label="reference circle")
    ax.plot(s["x_mm"], s["y_mm"], color="#2f8068", linewidth=1.4, label="actual TCP")
    ax.scatter([s["x_mm"][0]], [s["y_mm"][0]], color="#2e6ea6", s=26, label="start", zorder=4)
    ax.scatter([s["x_mm"][-1]], [s["y_mm"][-1]], color="#9a6b22", s=26, label="end", zorder=4)
    ax.set_xlabel("TCP X (mm)")
    ax.set_ylabel("TCP Y (mm)")
    ax.set_title(
        f"Step4D circle path, closure {fmt(circle['closure_error_mm'], 3)} mm"
    )
    ax.axis("equal")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    figures[f"{prefix}_path_tracking"] = save_and_copy(fig, f"{prefix}_path_tracking.png")

    fig, axes = plt.subplots(3, 1, figsize=(10.2, 8.0), sharex=False)
    axes[0].plot(s["stage25_t_s"], s["normal_force_n"], color="#2f8068", linewidth=1.1, label="measured normal force")
    axes[0].axhline(-stage25["target_force_n"], color="#172026", linewidth=0.9, linestyle="--", label="target -5 N")
    axes[0].set_ylabel("Normal force (N)")
    axes[0].grid(True, alpha=0.25)
    axes[0].legend(loc="upper right", fontsize=8)
    axes[1].plot(s["stage25_t_s"], s["normal_error_n"], color="#9a6b22", linewidth=1.1)
    axes[1].axhline(0.0, color="#172026", linewidth=0.9, linestyle="--")
    axes[1].set_ylabel("Normal - target (N)")
    axes[1].grid(True, alpha=0.25)
    axes[2].plot(s["progress_mm"], s["radial_error_mm"], color="#2e6ea6", linewidth=1.1, label="signed radial error")
    axes[2].plot(s["progress_mm"], s["xy_error_mm"], color="#7b8794", linewidth=1.1, label="XY tracking error")
    axes[2].axhline(0.0, color="#172026", linewidth=0.9, linestyle="--")
    axes[2].set_xlabel("Arc progress (mm)")
    axes[2].set_ylabel("Error (mm)")
    axes[2].grid(True, alpha=0.25)
    axes[2].legend(loc="upper right", fontsize=8)
    fig.suptitle("Step4D force and circle tracking, stage25", y=0.995)
    fig.tight_layout()
    figures[f"{prefix}_force_path"] = save_and_copy(fig, f"{prefix}_force_path.png")
    return figures


def build_step2c_result_media_assets() -> dict[str, dict[str, str] | None]:
    assets: dict[str, dict[str, str] | None] = {
        "tp_image": None,
        "video_mp4": None,
        "video_poster": None,
    }
    if STEP2C_RESULT_VIDEO_PREVIEW.exists():
        report_mp4 = REPORT_ASSETS / f"{STEP2C_RESULT_FILE_PREFIX}_experiment.mp4"
        if run_media_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(STEP2C_RESULT_VIDEO_PREVIEW),
                "-vf",
                "scale=720:-2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-an",
                str(report_mp4),
            ]
        ):
            weekly_mp4 = WEEKLY_ASSETS / report_mp4.name
            shutil.copy2(report_mp4, weekly_mp4)
            assets["video_mp4"] = {"report": rel_from_report(report_mp4), "weekly": rel_from_weekly(weekly_mp4)}

        report_poster = REPORT_ASSETS / f"{STEP2C_RESULT_FILE_PREFIX}_experiment_poster.jpg"
        if run_media_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                "8",
                "-i",
                str(STEP2C_RESULT_VIDEO_PREVIEW),
                "-vf",
                "scale=720:-2,crop=720:900:0:220",
                "-frames:v",
                "1",
                str(report_poster),
            ]
        ):
            weekly_poster = WEEKLY_ASSETS / report_poster.name
            shutil.copy2(report_poster, weekly_poster)
            assets["video_poster"] = {"report": rel_from_report(report_poster), "weekly": rel_from_weekly(weekly_poster)}
    return assets


def build_figures(short_kunwei: dict, short_onrobot: dict, long_kunwei: dict, long_onrobot: dict) -> dict[str, dict[str, str]]:
    figures: dict[str, dict[str, str]] = {}

    fig, axes = plt.subplots(3, 1, figsize=(10.2, 8.2), sharex=True)
    pairs = [
        ("Fz", "Fz_N", "fz_n", "Fz first-value-zeroed (N)"),
        ("Fx", "Fx_N", "fx_n", "Fx first-value-zeroed (N)"),
        ("Fy", "Fy_N", "fy_n", "Fy first-value-zeroed (N)"),
    ]
    for ax, (_, k_col, o_col, ylabel) in zip(axes, pairs):
        plot_envelope(ax, short_kunwei["series"][k_col], "Kunwei TCP raw 1 kHz", "#2f8068")
        plot_envelope(ax, short_onrobot["series"][o_col], "OnRobot UDP raw 500 Hz", "#2e6ea6")
        ax.axhline(0.0, color="#7b8794", linewidth=0.8, linestyle="--")
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.25)
    axes[0].legend(loc="upper right", fontsize=8)
    axes[-1].set_xlabel("Time (s)")
    fig.suptitle("First 600 s force drift comparison, first-value software zero", y=0.995)
    fig.tight_layout()
    figures["first600_force_axes"] = save_and_copy(fig, "first600_onrobot_kunwei_force_axes_envelope.png")

    fig, ax = plt.subplots(figsize=(10.2, 4.8))
    plot_envelope(ax, short_kunwei["series"]["Fz_N"], "Kunwei TCP raw 1 kHz", "#2f8068")
    plot_envelope(ax, short_onrobot["series"]["fz_n"], "OnRobot UDP raw 500 Hz", "#2e6ea6")
    ax.axhline(0.0, color="#7b8794", linewidth=0.8, linestyle="--")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Fz first-value-zeroed (N)")
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
    ax.set_ylabel("Std after first-value zero (N)")
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
    fig.suptitle("6 h force drift comparison, first-value software zero", y=0.995)
    fig.tight_layout()
    figures["sixh_force_axes"] = save_and_copy(fig, "sixh_onrobot_kunwei_force_axes_envelope.png")

    fig, ax = plt.subplots(figsize=(10.2, 4.8))
    plot_precomputed_envelope(ax, long_kunwei["envelopes"]["Fz_N"], "Kunwei TCP raw 1 kHz", "#2f8068")
    plot_precomputed_envelope(ax, long_onrobot["envelopes"]["fz_n"], "OnRobot UDP raw 500 Hz", "#2e6ea6")
    ax.axhline(0.0, color="#7b8794", linewidth=0.8, linestyle="--")
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Fz first-value-zeroed (N)")
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
    ax.set_ylabel("Std after first-value zero (N)")
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


def available_duration_metrics(long_summary: dict) -> dict[str, float]:
    kunwei_s = float(long_overall_metrics(long_summary)["duration_s"])
    onrobot_summary = load_json(ONROBOT_LONG_SUMMARY)
    onrobot_s = float(onrobot_summary["udp_timing"]["duration_first_last_s"])
    return {
        "kunwei_s": kunwei_s,
        "onrobot_udp_s": onrobot_s,
        "common_max_s": min(kunwei_s, onrobot_s),
        "report_long_window_s": LONG_WINDOW_S,
        "kunwei_h": kunwei_s / 3600.0,
        "onrobot_udp_h": onrobot_s / 3600.0,
        "common_max_h": min(kunwei_s, onrobot_s) / 3600.0,
        "report_long_window_h": LONG_WINDOW_S / 3600.0,
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


def rows_for_step2c_comparison(old_step2c: dict, v4: dict, result: dict) -> str:
    old_stage25 = old_step2c["stage25_force_all"]
    old_path = old_step2c["path_metrics"]
    old_echo = old_step2c["stage25_echo"]
    v4_stage25 = v4["stage25"]
    result_stage25 = result["stage25"]
    rows = [
        "| Result | stage25 echo (Hz) | stage25 duration (s) | Fz mean/std (N) | Fz error MAE (N) | Fz p95 abs err (N) | XY p95 (mm) | note |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
        "| "
        + " | ".join(
            [
                "previous Step2C",
                fmt(old_echo["rate_hz"], 2),
                fmt(old_stage25["duration_s"], 3),
                f"{fmt(old_stage25['fz_mean_n'], 2)} / {fmt(old_stage25['fz_std_n'], 2)}",
                fmt(old_stage25["signed_error_mae_n"], 2),
                fmt(old_stage25["signed_error_p95_abs_n"], 2),
                fmt(old_path["xy_error_p95_mm"], 3),
                "first successful line run",
            ]
        )
        + " |",
        "| "
        + " | ".join(
            [
                "Step2C V4",
                fmt(v4_stage25["echo_rate_hz"], 2),
                fmt(v4_stage25["duration_s"], 3),
                f"{fmt(v4_stage25['fz_mean_n'], 2)} / {fmt(v4_stage25['fz_std_n'], 2)}",
                fmt(v4_stage25["signed_error_mae_n"], 2),
                fmt(v4_stage25["signed_error_p95_abs_n"], 2),
                fmt(v4_stage25["xy_error_p95_mm"], 3),
                "489.83 Hz measured echo cadence evidence",
            ]
        )
        + " |",
        "| "
        + " | ".join(
            [
                result["label"],
                fmt(result_stage25["echo_rate_hz"], 2),
                fmt(result_stage25["duration_s"], 3),
                f"{fmt(result_stage25['fz_mean_n'], 2)} / {fmt(result_stage25['fz_std_n'], 2)}",
                fmt(result_stage25["signed_error_mae_n"], 2),
                fmt(result_stage25["signed_error_p95_abs_n"], 2),
                fmt(result_stage25["xy_error_p95_mm"], 3),
                "selected final run; lower Fz error than V4",
            ]
        )
        + " |",
    ]
    return "\n".join(rows)


def rows_for_step4d_result(step4d: dict) -> str:
    stage25 = step4d["stage25"]
    circle = step4d["circle"]
    return "\n".join(
        [
            "| Metric | Step4D result |",
            "|---|---:|",
            f"| run | `{step4d['run_name']}` |",
            f"| circle completed | {'yes' if circle['completed'] else 'no'} |",
            f"| stage25 samples | `{fmt(stage25['rows'])}` |",
            f"| stage25 echo rate | `{fmt(stage25['echo_rate_hz'], 2)} Hz` |",
            f"| radius / arc progress | `{fmt(circle['radius_mm'], 3)} / {fmt(circle['arc_progress_end_mm'], 3)} mm` |",
            f"| closure error | `{fmt(circle['closure_error_mm'], 3)} mm` |",
            f"| radial error mean / p95 | `{fmt(circle['radial_error_mean_mm'], 3)} / {fmt(circle['radial_error_p95_mm'], 3)} mm` |",
            f"| XY tracking error mean / p95 | `{fmt(circle['xy_error_mean_mm'], 3)} / {fmt(circle['xy_error_p95_mm'], 3)} mm` |",
            f"| normal-force error MAE / p95 | `{fmt(stage25['normal_error_mae_n'], 3)} / {fmt(stage25['normal_error_p95_abs_n'], 3)} N` |",
            f"| lateral force p95 | `{fmt(stage25['lateral_force_p95_n'], 3)} N` |",
            f"| torque norm p95 | `{fmt(stage25['torque_norm_p95_nm'], 3)} Nm` |",
        ]
    )


def strip_plot_data(data: dict) -> dict:
    return {
        key: value
        for key, value in data.items()
        if key not in {"series", "raw_series", "envelopes"}
    }


def write_summary_json(
    short_kunwei: dict,
    short_onrobot: dict,
    long_kunwei: dict,
    long_onrobot: dict,
    figures: dict,
    media_assets: dict,
    long_summary: dict,
    step2c: dict,
    step2c_v4: dict,
    step2c_result: dict,
    step4d_result: dict,
) -> Path:
    available = available_duration_metrics(long_summary)
    payload = {
        "comparison_note": "Both streams use first-value software zero in their own selected windows; no device-side zero/tare was executed and no new experiment is required for this report version.",
        "available_duration": {
            "kunwei_s": available["kunwei_s"],
            "onrobot_udp_s": available["onrobot_udp_s"],
            "common_max_s": available["common_max_s"],
            "report_long_window_s": available["report_long_window_s"],
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
        "step2c_v4": strip_step2c_run_series(step2c_v4),
        "step2c_result": strip_step2c_run_series(step2c_result),
        "step4d_result": strip_step4d_run_series(step4d_result),
        "missing_data_requests": step2c_result.get("missing_data_requests", []),
        "media_assets": media_assets,
        "figures": figures,
    }
    out = REPORT_ASSETS / "analysis-summary.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    shutil.copy2(out, WEEKLY_ASSETS / out.name)
    return out


def build_markdown(
    short_kunwei: dict,
    short_onrobot: dict,
    long_kunwei: dict,
    long_onrobot: dict,
    figures: dict,
    media_assets: dict,
    summary_json: Path,
    long_summary: dict,
    step2c: dict,
    step2c_v4: dict,
    step2c_result: dict,
    step4d_result: dict,
) -> str:
    overall = long_overall_metrics(long_summary)
    available = available_duration_metrics(long_summary)
    stage25 = step2c["stage25_force_all"]
    stage25_after = step2c["stage25_force_after_0p5s"]
    path_metrics = step2c["path_metrics"]
    raw_stage25 = step2c["raw_sensor_stage25"]
    stage25_echo = step2c["stage25_echo"]
    summary = step2c_metrics(step2c)
    v4 = step2c_v4["stage25"]
    result = step2c_result["stage25"]
    result_raw = step2c_result["raw_stage25"]
    result_freq = step2c_result["frequency"]
    result_video = media_assets.get("video_mp4", {}).get("report") if media_assets.get("video_mp4") else None
    result_poster = media_assets.get("video_poster", {}).get("report") if media_assets.get("video_poster") else None
    result_prefix = step2c_result["file_prefix"]
    step4d_stage25 = step4d_result["stage25"]
    step4d_circle = step4d_result["circle"]
    step4d_prefix = step4d_result["file_prefix"]

    return f"""# Kunwei KWR75 当前进展报告（2026-06-08）

## 实验目的

这份报告把 Kunwei KWR75/KWR75B 当前证据单独整理出来，用于说明四件事：传感器与通信链路是否已经可用，长时间无运动 `1 kHz` 采集是否稳定，当前 Step2C 闭环直线实验走到什么程度，以及 Step4D 圆轨迹接触实验是否完成。报告包含两层 OnRobot/Kunwei 对比：前 `600 s` 用于短窗口 noise/drift 判断，`6 h` 用于长时间漂移判断。当前版本只使用已有日志，不重做实验；两者都按各自窗口第一帧做 software zero，只作为 drift/noise 口径对照，不作为同机械状态下的绝对标定结论。

结论先给出：Kunwei TCP raw logging 已经支撑 `19 h 15 min`、约 `1 kHz`、无 parse error 的长跑；Step2C final 在 line 阶段保持 URScript stage25 echo cadence 约 `{fmt(result['echo_rate_hz'], 2)} Hz`，路径跟踪 p95 约 `{fmt(result['xy_error_p95_mm'], 3)} mm`，Fz error MAE 从 V4 的 `{fmt(v4['signed_error_mae_n'], 2)} N` 降到 `{fmt(result['signed_error_mae_n'], 2)} N`。Step4D 已完成从 middle-half 直线路径生成的完整圆轨迹，arc progress 约 `{fmt(step4d_circle['arc_progress_end_mm'], 3)} mm`，closure error 约 `{fmt(step4d_circle['closure_error_mm'], 3)} mm`，radial error p95 约 `{fmt(step4d_circle['radial_error_p95_mm'], 3)} mm`。这说明 final 已经不只是 frequency 进展，也把 force ripple/误差压低了一档；Step4D 则证明圆轨迹 scaffold、确定性接触搜索和姿态 admittance 能跑完整圈。但它们都仍是 selected single-run evidence，不等于完整统计验证。

## 设备与实验条件

| 项目 | 当前口径 |
|---|---|
| 传感器 | Kunwei KWR75/KWR75B 六轴力/力矩传感器 |
| 当前主采集路线 | Ubuntu TCP client -> `192.168.50.25:5152`，converted result stream |
| Ubuntu bench IP | `192.168.50.26/24` on `enp3s0` |
| Vendor GUI 状态 | Windows 11 原生 `SensorLinker.exe` 已验证 live 数据与 CSV 记录；Ubuntu/Wine 不是当前默认路线 |
| 长时采集状态 | 无机器人运动、无接触操作，只测传感器通信和静态读数 |
| zero 口径 | 本报告 OnRobot/Kunwei 对比均为 first-value software zero；未调用 Kunwei hardware tare、OnRobot device bias/tare 或 UR `zero_ftsensor()` |
| Step2C 参考线 | 长度约 `63.58 mm` 的 XY straight-line reference |
| Step4D 圆轨迹 | 取 Step2C contact path 的 middle half 作为直径；半径约 `{fmt(step4d_circle['radius_mm'], 3)} mm`，full-circle arc 约 `99.871 mm` |
| 本报告图表口径 | 统计用选定窗口内全样本；长 trace 图用 min/max envelope，不用等间隔抽样线作为主证据 |
| 最长可用公共窗口 | Kunwei `{fmt(available['kunwei_h'], 2)} h`，OnRobot UDP `{fmt(available['onrobot_udp_h'], 2)} h`；本报告长对比采用更适合汇报的 `{fmt(available['report_long_window_h'], 2)} h` |
| Step2C final 口径 | final 从已有 `bridge_rtde_500hz.csv`、`kunwei_sensor_1khz.csv` 和 `stage_frequency_summary.json` 计算；不补实验、不补写 `summary.json` |

## 实验命令

长时采集由 `capture_kunwei_kwr75_1khz.py` 运行，核心参数是 `--transport tcp-client --sensor-ip 192.168.50.25 --sensor-port 5152 --duration-s 86400 --checkpoint-interval-s 900`。旧 Step2C 主 run 使用 `search5_guard20_line2ms_alpha70_vlim5` 版本；final 使用 `step2c_final` 程序包和 `search2ms_line1ms_alpha70` autowatch bridge，line 阶段目标仍是 `-5 N`。Step4D 使用 `step4d_circle_detsearch_attitude_v1` TP package，Python bridge 只写 Kunwei zeroed force/torque、target、heartbeat 和状态 register；机器人运动仍由 TP 上已打开的 URP 执行，UR 通过 Cartesian `speedl` twist 负责 IK。

本报告的生成脚本只读取已有 CSV/JSON 并写出报告资产，没有向 UR、OnRobot 或 Kunwei 发送命令，也没有做视频多帧抽样或视频帧分析；HTML evidence clip 只使用转码 mp4 和一个 poster。

## 数据与图片

| artifact | 路径 |
|---|---|
| 本报告 summary | [{rel_from_report(summary_json)}]({rel_from_report(summary_json)}) |
| Kunwei 19h15min raw CSV | [../ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/data.csv](../ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/data.csv) |
| Kunwei 19h15min logger summary | [../ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/summary.json](../ft_sensor/kunwei/kwr75b/measurements/19h15min/capture/summary.json) |
| Step2C metrics | [assets/step2c-kunwei-search5-guard20-line2ms/analysis-metrics.json](assets/step2c-kunwei-search5-guard20-line2ms/analysis-metrics.json) |
| Step2C V4 comparison run | [../experiments/kunwei/closed-loop-straight-line/2026-06-04/runs/{step2c_v4['run_name']}](../experiments/kunwei/closed-loop-straight-line/2026-06-04/runs/{step2c_v4['run_name']}) |
| Step2C final run | [../experiments/kunwei/closed-loop-straight-line/2026-06-04/runs/{step2c_result['run_name']}](../experiments/kunwei/closed-loop-straight-line/2026-06-04/runs/{step2c_result['run_name']}) |
| Step2C final video | {f'[{result_video}]({result_video})' if result_video else 'N/A'} |
| Step4D circle run | [../experiments/kunwei/closed-loop-straight-line/2026-06-04/runs/{step4d_result['run_name']}](../experiments/kunwei/closed-loop-straight-line/2026-06-04/runs/{step4d_result['run_name']}) |
| OnRobot 600s UDP raw CSV | [../experiments/20260528_onrobot_three_stream_600s_first_zero/run_20260528_043100/three_stream_600s_20260528_043052_onrobot_udp500_raw.csv](../experiments/20260528_onrobot_three_stream_600s_first_zero/run_20260528_043100/three_stream_600s_20260528_043052_onrobot_udp500_raw.csv) |
| OnRobot 6h UDP raw CSV | [../experiments/20260530_onrobot_three_stream_coldstart_drift/run_20260530_175217/three_stream_24h_20260530_20260530_175220_onrobot_udp500_raw.csv](../experiments/20260530_onrobot_three_stream_coldstart_drift/run_20260530_175217/three_stream_24h_20260530_20260530_175220_onrobot_udp500_raw.csv) |

图 1 是本报告最主要的 OnRobot/Kunwei 前 `600 s` 对比图。两条曲线都先减去各自窗口第一帧，因此显示的是本窗口内的相对变化。这个 zero 是软件分析口径，不是 device-side zero/tare。阴影是每个时间 bin 内的 min/max envelope，实线是 bin mean；统计表仍使用窗口内所有样本。

![OnRobot vs Kunwei first 600s force axes]({figures['first600_force_axes']['report']})

图 2 单独展开 Fz。Kunwei 前 `600 s` 的 first-value-zeroed Fz 标准差是 `{fmt(short_kunwei['zeroed_stats']['Fz_N']['std'], 4)} N`，OnRobot UDP raw 是 `{fmt(short_onrobot['zeroed_stats']['fz_n']['std'], 4)} N`。这个数值不能直接解释成传感器规格优劣，因为两个窗口的安装、载荷和日期不同。

![OnRobot vs Kunwei first 600s Fz]({figures['first600_fz']['report']})

图 3 把前三个力轴的 first-value-zeroed 标准差放在同一张图里，用于快速看 `600 s` 窗口内的波动量级。

![OnRobot vs Kunwei first 600s std]({figures['first600_std']['report']})

图 4 是本次新增的 `{fmt(available['report_long_window_h'], 2)} h` 长时间 Fz 对比。当前本地数据的最长公共窗口是 `{fmt(available['common_max_h'], 2)} h`，但本报告采用 `{fmt(available['report_long_window_h'], 2)} h` 作为主图口径，避免把会议汇报拖进过长的历史细节。统计仍使用 `{fmt(available['report_long_window_h'], 2)} h` 内全样本，图中阴影仍是 min/max envelope。

![OnRobot vs Kunwei 6h Fz]({figures['sixh_fz']['report']})

图 5 展示 `6 h` 的 Fx/Fy/Fz 三轴上下文。它用于判断 Fz 漂移是否伴随横向力变化。

![OnRobot vs Kunwei 6h force axes]({figures['sixh_force_axes']['report']})

图 6 是 `6 h` 窗口下三个力轴的 first-value-zeroed 标准差。

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
| 旧成功 run | `{summary['run_name']}` |
| V4 comparison run | `{step2c_v4['run_name']}` |
| final run | `{step2c_result['run_name']}` |
| 旧 run 是否完成 line | `{summary['line_completed']}` |
| bridge stop reason | `{summary['bridge_stop_reason']}` |
| final bridge write / RTDE log rate | `{fmt(result_freq['bridge_write_rate_hz'], 2)} / {fmt(result_freq['rtde_output_logging_rate_hz'], 2)} Hz` |
| final raw sensor stage25 rate | `{fmt(result_raw['rate_hz'], 2)} Hz` |
| final stage25 echo rate | `{fmt(result['echo_rate_hz'], 2)} Hz` |
| final stage25 Fz mean/std | `{fmt(result['fz_mean_n'], 2)} / {fmt(result['fz_std_n'], 2)} N` |
| final stage25 Fz error MAE | `{fmt(result['signed_error_mae_n'], 2)} N` |
| final raw stage25 Fz min | `{fmt(result_raw['fz_min_n'], 2)} N` |
| final XY error mean / p95 | `{fmt(result['xy_error_mean_mm'], 3)} / {fmt(result['xy_error_p95_mm'], 3)} mm` |

V4 的主要意义是 frequency 层面的进展：stage25 echo cadence 从旧成功 Step2C 的约 `{fmt(stage25_echo['rate_hz'], 2)} Hz` 提高到约 `{fmt(v4['echo_rate_hz'], 2)} Hz`，但 Fz error MAE 升到 `{fmt(v4['signed_error_mae_n'], 2)} N`。final 保持同样的 measured URScript echo/motion-gate cadence，同时把 Fz error MAE 降到 `{fmt(result['signed_error_mae_n'], 2)} N`、p95 abs error 降到 `{fmt(result['signed_error_p95_abs_n'], 2)} N`。这里仍然只写成 measured cadence，不写成内部 servo loop 频率；力控制结论也只针对这次 selected final run。

{rows_for_step2c_comparison(step2c, step2c_v4, step2c_result)}

![Step2C final force tracking]({figures[f'{result_prefix}_fz_tracking']['report']})

![Step2C final path tracking]({figures[f'{result_prefix}_path_tracking']['report']})

![Step2C final tradeoff]({figures[f'{result_prefix}_tradeoff']['report']})

{f'![Step2C final experiment poster]({result_poster})' if result_poster else ''}

{f'[Step2C final experiment video]({result_video})' if result_video else ''}

### Step4D 圆轨迹接触进展

Step4D 把 Step2C contact path 的中间一半作为直径，生成半径约 `{fmt(step4d_circle['radius_mm'], 3)} mm` 的完整圆。程序复用 Step2C final 风格的高点进入、确定性接触搜索、卸载和回撤框架；圆阶段用 Cartesian `speedl` twist 走轨迹，Z 方向继续用 signed Fz velocity admittance，`wx/wy` 姿态修正来自 filtered Fx/Fy 与 Mx/My 的 bounded velocity admittance。

{rows_for_step4d_result(step4d_result)}

这次 Step4D 的关键结果是 stop reason 进入 `circle_complete`，arc progress 到 `{fmt(step4d_circle['arc_progress_end_mm'], 3)} mm`，接近 full-circle nominal `99.871 mm`。closure error 约 `{fmt(step4d_circle['closure_error_mm'], 3)} mm`，radial error p95 约 `{fmt(step4d_circle['radial_error_p95_mm'], 3)} mm`。力控制部分还没有达到 Step2C final 的力误差水平，normal-force error MAE 为 `{fmt(step4d_stage25['normal_error_mae_n'], 3)} N`，所以这里的结论应写成“圆轨迹 contact scaffold 已跑通”，而不是“圆轨迹 force quality 已收敛”。

![Step4D circle path tracking]({figures[f'{step4d_prefix}_path_tracking']['report']})

![Step4D circle force and path evidence]({figures[f'{step4d_prefix}_force_path']['report']})

### OnRobot vs Kunwei 前 600s

{rows_for_force_table(short_kunwei, short_onrobot)}

{rows_for_torque_table(short_kunwei, short_onrobot)}

比较限制必须写清楚：Kunwei 的前 `600 s` 来自 `19h15min` 未做 device-side zero/tare 的静态长跑，OnRobot 来自 `20260528` 的 dedicated `600s_first_zero` run；两者不是同一天、同治具、同预载的同步 A/B。这里能比较的是当前可用 raw stream 在自身 first-value software zero 口径下的短窗口稳定性和采样路线差异。

### OnRobot vs Kunwei 6h

本地可用数据里，Kunwei 最长为 `{fmt(available['kunwei_h'], 2)} h`，OnRobot UDP raw 最长为 `{fmt(available['onrobot_udp_h'], 2)} h`，两者最长公共窗口为 `{fmt(available['common_max_h'], 2)} h`。本报告采用 `{fmt(available['report_long_window_h'], 2)} h` 作为长时间对比主口径；这个窗口已经足够覆盖慢漂移趋势，也更适合会议图表。

{rows_for_long_force_table(long_kunwei, long_onrobot)}

这个 `6 h` 对比仍然不是严格同治具同步 A/B。它更适合回答“当前两条 raw stream 在首值归零后的长窗口稳定性量级如何”，不适合回答“哪个传感器绝对零点更准”。

## 结论

1. Kunwei TCP raw logging 路线已经可用：`19 h 15 min` 内约 `1 kHz`，`parse_errors=0`，`dropped_sync_bytes=0`。
2. Kunwei 已经从传感器 bring-up 进入机器人闭环验证阶段。旧 Step2C、V4 和 final 都能完成搜索、直线、卸载和回撤；均值层面能围绕 `-5 N` 工作。
3. final 的 stage25 measured echo cadence 约 `{fmt(result['echo_rate_hz'], 2)} Hz`，比旧成功 Step2C 的 `{fmt(stage25_echo['rate_hz'], 2)} Hz` 明显提高，并且 Fz error MAE 比 V4 更低；但这仍不能写成 UR 内部 servo loop 频率。
4. Step4D 首次把 “middle-half 直线路径作为直径 -> 完整圆 -> 接触搜索 -> 姿态 admittance -> 回撤” 这一套流程跑完；但圆轨迹与 Step2C 直线任务几何不同，不能把两者的 force/path 指标当作同任务直接排序。
5. OnRobot/Kunwei 前 `600 s` 与 `6 h` 对比图说明两条 raw stream 都可以用 first-value software zero 做短窗口和长窗口漂移分析；但由于机械状态不同，报告只解释相对漂移和波动，不解释绝对偏置或规格优劣。

## 下一步

- Step2C 下一步应围绕 final 的重复性和 contact-entry transient 继续验证；频率证据已经足够支持约 `490 Hz` measured echo cadence 进入报告，force quality 也相对 V4 有改善，但还不应该外推成跨治具、跨日期的传感器绝对性能结论。
- Step4D 下一步应先围绕完整圆的重复性、entry transient 和 normal-force ripple 收敛，不要急着把它写成传感器绝对性能或最终算法效果。若要进一步降低圆轨迹误差，再考虑是否把 IK/trajectory optimization 从 UR 内部逐步外移到脚本侧。
- 本版本不需要新做 OnRobot/Kunwei A/B 实验；当前会议材料只使用已有日志，并明确标注为 first-value software zero 的历史窗口比较。若未来要回答绝对标定问题，再另开同机械状态、同无接触窗口、明确 device-side zero/tare 策略的实验。
- 如果目标是机器人侧 `500 Hz` 运动闭环，需要另开 `servoj/speedj`、多线程 URScript 或外部实时接口路线，而不是从当前 `speedl` echo 推断。

## 附录

### 报告生成命令

```bash
python3 /home/andy/ur10e_ros2_ws/weekly_meeting/build_kunwei_kwr75_report.py
```

### 生成口径

脚本对 `600 s` 窗口保留短窗口点列；对 `6 h` 窗口只保留统计量和时间 bin envelope，不把千万级样本全部留在内存里。统计直接使用窗口内所有样本；图形先按时间 bin 聚合为 min/max/mean envelope，保留尖峰范围，不使用等间隔抽样折线作为主要证据。HTML deck 使用生成的数据图和压缩后的 Step2C final 视频，不嵌入原始 MOV。
"""


def html_metric(label: str, value: str) -> str:
    return f"<div class=\"metric\"><span>{label}</span><strong>{value}</strong></div>"


def build_html(
    short_kunwei: dict,
    short_onrobot: dict,
    long_kunwei: dict,
    long_onrobot: dict,
    figures: dict,
    media_assets: dict,
    long_summary: dict,
    step2c: dict,
    step2c_v4: dict,
    step2c_result: dict,
    step4d_result: dict,
) -> str:
    overall = long_overall_metrics(long_summary)
    available = available_duration_metrics(long_summary)
    stage25 = step2c["stage25_force_all"]
    path_metrics = step2c["path_metrics"]
    stage25_echo = step2c["stage25_echo"]
    summary = step2c_metrics(step2c)
    v4 = step2c_v4["stage25"]
    result = step2c_result["stage25"]
    result_raw = step2c_result["raw_stage25"]
    result_freq = step2c_result["frequency"]
    result_prefix = step2c_result["file_prefix"]
    force_img = figures["first600_force_axes"]["weekly"]
    fz_img = figures["first600_fz"]["weekly"]
    std_img = figures["first600_std"]["weekly"]
    sixh_force_img = figures["sixh_force_axes"]["weekly"]
    sixh_fz_img = figures["sixh_fz"]["weekly"]
    sixh_std_img = figures["sixh_std"]["weekly"]
    result_fz_img = figures[f"{result_prefix}_fz_tracking"]["weekly"]
    result_path_img = figures[f"{result_prefix}_path_tracking"]["weekly"]
    result_tradeoff_img = figures[f"{result_prefix}_tradeoff"]["weekly"]
    result_video = media_assets.get("video_mp4", {}).get("weekly") if media_assets.get("video_mp4") else None
    result_video_poster = media_assets.get("video_poster", {}).get("weekly") if media_assets.get("video_poster") else None
    step4d_stage25 = step4d_result["stage25"]
    step4d_circle = step4d_result["circle"]
    step4d_prefix = step4d_result["file_prefix"]
    step4d_path_img = figures[f"{step4d_prefix}_path_tracking"]["weekly"]
    step4d_force_path_img = figures[f"{step4d_prefix}_force_path"]["weekly"]
    result_video_html = (
        f'<figure class="span-5 media-video"><video controls preload="metadata" poster="{result_video_poster or ""}" src="{result_video}"></video><figcaption>Fig. F-A. Step2C final experiment evidence clip from the 2026-06-08 Step2C final run.</figcaption></figure>'
        if result_video
        else ""
    )

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
    figure video {{
      width: 100%;
      max-height: 620px;
      object-fit: contain;
      background: #101820;
      display: block;
    }}
    .media-video {{ background: #101820; }}
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
    <a href="#step4d">Step4D</a>
    <a href="#compare">600 s Compare</a>
    <a href="#long-compare">6 h Compare</a>
    <a href="#next">Next</a>
  </nav>
  <main>
    <section id="summary">
      <div class="eyebrow">Kunwei KWR75 / UR10e</div>
      <h1>Kunwei force sensor progress report</h1>
      <p class="lead">Kunwei is no longer just a bring-up task: the TCP raw logging path has a stable 19 h 15 min run, Step2C has completed a closed-loop straight-line contact task, and Step4D has completed the first full contact circle with deterministic search and attitude admittance. The OnRobot/Kunwei comparison in this deck uses existing logs only, with first-value software zero inside each selected window.</p>
      <div class="grid">
        {html_metric("Long raw capture", "69.3M samples")}
        {html_metric("Average raw rate", f"{fmt(overall['rate_hz'], 3)} Hz")}
        {html_metric("Frame errors", "0 parse / 0 sync")}
        {html_metric("Step2C final echo", f"{fmt(result['echo_rate_hz'], 1)} Hz")}
        {html_metric("Step4D closure", f"{fmt(step4d_circle['closure_error_mm'], 3)} mm")}
        {html_metric("Long comparison", "6 h selected")}
      </div>
    </section>
    <section id="link">
      <div class="eyebrow">Sensor link</div>
      <h2>1 kHz TCP logging is stable for the current bench</h2>
      <p>The 19 h 15 min no-motion run received converted Kunwei frames continuously over TCP. This validates the current Ubuntu collection path, but it is not a 24 h run and it is not a device-side zero/tare calibration.</p>
      <div class="grid">
        {html_metric("Duration", f"{fmt(overall['duration_s'] / 3600.0, 3)} h")}
        {html_metric("Fz last-first", f"{fmt(overall['fz_last_first'], 4)} N")}
        {html_metric("Fz std", f"{fmt(overall['fz_std'], 4)} N")}
        {html_metric("Samples", fmt(overall["samples"]))}
      </div>
    </section>
    <section id="step2c">
      <div class="eyebrow">Robot experiment</div>
      <h2>Step2C final holds ~490 Hz measured echo and improves force tracking</h2>
      <p>The selected final run keeps measured stage25 echo cadence at {fmt(result['echo_rate_hz'], 2)} Hz while reducing Fz error MAE from V4's {fmt(v4['signed_error_mae_n'], 2)} N to {fmt(result['signed_error_mae_n'], 2)} N. RTDE logging stays near 500 Hz and Kunwei raw stage25 remains near {fmt(result_raw['rate_hz'], 2)} Hz. This is measured URScript echo/motion-gate evidence, not a claim about the internal servo loop.</p>
      <div class="grid">
        {html_metric("Line outcome", "complete")}
        {html_metric("Stage25 echo", f"{fmt(result['echo_rate_hz'], 2)} Hz")}
        {html_metric("RTDE log", f"{fmt(result_freq['rtde_output_logging_rate_hz'], 2)} Hz")}
        {html_metric("Kunwei raw", f"{fmt(result_raw['rate_hz'], 2)} Hz")}
        {html_metric("Final Fz MAE", f"{fmt(result['signed_error_mae_n'], 2)} N")}
        {html_metric("Final XY p95", f"{fmt(result['xy_error_p95_mm'], 3)} mm")}
        {html_metric("V4 Fz MAE", f"{fmt(v4['signed_error_mae_n'], 2)} N")}
        {html_metric("Old echo", f"{fmt(stage25_echo['rate_hz'], 2)} Hz")}
        <figure class="span-7"><img src="{result_fz_img}" alt="Step2C final force tracking"><figcaption>Fig. 1. Final Fz tracking keeps the mean near target and reduces error relative to V4.</figcaption></figure>
        <figure class="span-5"><img src="{result_tradeoff_img}" alt="Step2C final tradeoff chart"><figcaption>Fig. 2. Final keeps the cadence/path improvement and lowers Fz MAE versus V4.</figcaption></figure>
        <figure class="span-12"><img src="{result_path_img}" alt="Step2C final path tracking"><figcaption>Fig. 3. Final path tracking remains tight; XY p95 is about {fmt(result['xy_error_p95_mm'], 3)} mm.</figcaption></figure>
        {result_video_html}
      </div>
    </section>
    <section id="step4d">
      <div class="eyebrow">Robot experiment</div>
      <h2>Step4D completes a full contact circle with attitude admittance</h2>
      <p>Step4D uses the middle half of the prior contact path as the circle diameter, then runs deterministic contact search, signed-Fz velocity admittance, and bounded wx/wy attitude admittance while UR handles IK through Cartesian speedl twist. The selected run reached circle_complete with {fmt(step4d_circle['arc_progress_end_mm'], 3)} mm arc progress, {fmt(step4d_circle['closure_error_mm'], 3)} mm closure error, and {fmt(step4d_circle['radial_error_p95_mm'], 3)} mm radial p95. This is circular-contact scaffold evidence, not a same-task ranking against Step2C straight-line force quality.</p>
      <div class="grid">
        {html_metric("Circle outcome", "full circle")}
        {html_metric("Stage25 echo", f"{fmt(step4d_stage25['echo_rate_hz'], 2)} Hz")}
        {html_metric("Radius", f"{fmt(step4d_circle['radius_mm'], 3)} mm")}
        {html_metric("Arc progress", f"{fmt(step4d_circle['arc_progress_end_mm'], 3)} mm")}
        {html_metric("Closure error", f"{fmt(step4d_circle['closure_error_mm'], 3)} mm")}
        {html_metric("Radial p95", f"{fmt(step4d_circle['radial_error_p95_mm'], 3)} mm")}
        {html_metric("Normal MAE", f"{fmt(step4d_stage25['normal_error_mae_n'], 2)} N")}
        {html_metric("Torque p95", f"{fmt(step4d_stage25['torque_norm_p95_nm'], 3)} Nm")}
        <figure class="span-6"><img src="{step4d_path_img}" alt="Step4D circle path tracking"><figcaption>Fig. 4. Actual TCP path closes the commanded full-circle contact trajectory; closure error is {fmt(step4d_circle['closure_error_mm'], 3)} mm.</figcaption></figure>
        <figure class="span-6"><img src="{step4d_force_path_img}" alt="Step4D force and path evidence"><figcaption>Fig. 5. Normal-force ripple remains the next target; radial path tracking is already tight for this first complete circle.</figcaption></figure>
      </div>
    </section>
    <section id="compare">
      <div class="eyebrow">Sensor comparison</div>
      <h2>OnRobot vs Kunwei, first 600 s</h2>
      <p>Both traces are first-value software zeroed inside their own 600 s windows. No device-side zero/tare was executed for this report version. The shaded region is a min/max envelope; statistics use all samples in the selected window.</p>
      <div class="grid">
        <figure class="span-12"><img src="{force_img}" alt="First 600 s force axes comparison"><figcaption>Fig. 1. Fx/Fy/Fz first-value-zeroed envelopes. This compares short-window stability, not absolute bias.</figcaption></figure>
        <figure class="span-7"><img src="{fz_img}" alt="First 600 s Fz comparison"><figcaption>Fig. 2. Fz detail, first-value-zeroed within each sensor's own run.</figcaption></figure>
        <figure class="span-5"><img src="{std_img}" alt="First 600 s force standard deviation"><figcaption>Fig. 3. Force-axis standard deviation in the first 600 s windows.</figcaption></figure>
      </div>
    </section>
    <section id="long-compare">
      <div class="eyebrow">Sensor comparison</div>
      <h2>OnRobot vs Kunwei, 6 h</h2>
      <p>The local common maximum is {fmt(available['common_max_h'], 2)} h, limited by the OnRobot UDP run. This deck uses {fmt(available['report_long_window_h'], 2)} h as the main long-window comparison so the result stays readable and avoids overfitting the meeting story to a tail segment.</p>
      <div class="grid">
        {html_metric("Kunwei available", f"{fmt(available['kunwei_h'], 2)} h")}
        {html_metric("OnRobot available", f"{fmt(available['onrobot_udp_h'], 2)} h")}
        {html_metric("Common max", f"{fmt(available['common_max_h'], 2)} h")}
        {html_metric("Selected window", f"{fmt(available['report_long_window_h'], 2)} h")}
        <figure class="span-12"><img src="{sixh_fz_img}" alt="6 h Fz comparison"><figcaption>Fig. 4. Fz first-value-zeroed envelope for the selected 6 h comparison window. Statistics use all samples in the window.</figcaption></figure>
        <figure class="span-12"><img src="{sixh_force_img}" alt="6 h force axes comparison"><figcaption>Fig. 5. Fx/Fy/Fz first-value-zeroed envelopes over 6 h. This is a long-window drift comparison, not an absolute calibration claim.</figcaption></figure>
        <figure class="span-12"><img src="{sixh_std_img}" alt="6 h force standard deviation"><figcaption>Fig. 6. Force-axis standard deviation over the selected 6 h window.</figcaption></figure>
      </div>
    </section>
    <section id="next">
      <div class="eyebrow">Next action</div>
      <h2>Stabilize contact entry before chasing higher frequency</h2>
      <p>The next Step2C work should confirm repeatability and reduce contact-entry transient after the final-run improvement. The report should keep frequency layers separate: sensor raw stream, bridge/RTDE logging, measured URScript echo cadence, and unvalidated internal servo-loop behavior.</p>
      <div class="grid">
        <table class="span-12">
          <thead><tr><th>Decision</th><th>Current evidence</th><th>Default next move</th></tr></thead>
          <tbody>
            <tr><td>Logging route</td><td>Kunwei TCP raw is stable at 1 kHz class</td><td>Use it as the default Kunwei collector</td></tr>
            <tr><td>Force control</td><td>Final mean Fz is near target and error MAE is {fmt(result['signed_error_mae_n'], 2)} N</td><td>Confirm repeatability before broader force-quality claims</td></tr>
            <tr><td>Circle contact</td><td>Step4D completed the full circle with {fmt(step4d_circle['closure_error_mm'], 3)} mm closure error</td><td>Reduce contact-entry transient and normal-force ripple before stronger algorithm claims</td></tr>
            <tr><td>Frequency claim</td><td>Final stage25 echo is {fmt(result['echo_rate_hz'], 2)} Hz; RTDE logging is {fmt(result_freq['rtde_output_logging_rate_hz'], 2)} Hz</td><td>Report measured cadence, not internal servo-loop frequency</td></tr>
            <tr><td>A/B comparison</td><td>Existing 600 s and 6 h windows differ in setup/date/load</td><td>Use first-value software zero for this report; reserve same-fixture testing only for future absolute calibration claims</td></tr>
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

    step2c_v4 = analyze_step2c_run(STEP2C_V4_RUN_DIR, label="Step2C V4", file_prefix="step2c_v4")
    step2c_result = analyze_step2c_run(
        STEP2C_RESULT_RUN_DIR,
        label=STEP2C_RESULT_LABEL,
        file_prefix=STEP2C_RESULT_FILE_PREFIX,
    )
    step4d_result = analyze_step4d_run(
        STEP4D_RUN_DIR,
        label=STEP4D_RESULT_LABEL,
        file_prefix=STEP4D_RESULT_FILE_PREFIX,
    )
    figures = build_figures(short_kunwei, short_onrobot, long_kunwei, long_onrobot)
    figures.update(build_step2c_result_figures(step2c_result, step2c, step2c_v4))
    figures.update(build_step4d_result_figures(step4d_result))
    media_assets = build_step2c_result_media_assets()
    summary_json = write_summary_json(
        short_kunwei,
        short_onrobot,
        long_kunwei,
        long_onrobot,
        figures,
        media_assets,
        long_summary,
        step2c,
        step2c_v4,
        step2c_result,
        step4d_result,
    )

    REPORT_MD.write_text(
        build_markdown(
            short_kunwei,
            short_onrobot,
            long_kunwei,
            long_onrobot,
            figures,
            media_assets,
            summary_json,
            long_summary,
            step2c,
            step2c_v4,
            step2c_result,
            step4d_result,
        ),
        encoding="utf-8",
    )
    REPORT_HTML.write_text(
        build_html(
            short_kunwei,
            short_onrobot,
            long_kunwei,
            long_onrobot,
            figures,
            media_assets,
            long_summary,
            step2c,
            step2c_v4,
            step2c_result,
            step4d_result,
        ),
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
