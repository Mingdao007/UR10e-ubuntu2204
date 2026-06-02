#!/usr/bin/env python3
"""Build OnRobot Fz comparison assets for the 2026-05-28 meeting deck."""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


URCAP_FIELDS = ["urcap_fx_n", "urcap_fy_n", "urcap_fz_n", "urcap_tx_nm", "urcap_ty_nm", "urcap_tz_nm"]
UDP_FIELDS = ["fx_n", "fy_n", "fz_n", "tx_nm", "ty_nm", "tz_nm"]


@dataclass(frozen=True)
class CleanWindow:
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def transform(self, t_s: float) -> float | None:
        if self.start_s <= t_s < self.end_s:
            return None
        if t_s >= self.end_s:
            return t_s - self.duration_s
        return t_s


@dataclass(frozen=True)
class CleanTimeline:
    windows: tuple[CleanWindow, ...]

    @property
    def duration_s(self) -> float:
        return sum(window.duration_s for window in self.windows)

    def transform(self, t_s: float) -> float | None:
        shift = 0.0
        for window in self.windows:
            if window.start_s <= t_s < window.end_s:
                return None
            if t_s >= window.end_s:
                shift += window.duration_s
        return t_s - shift

    def as_summary(self) -> list[dict[str, float]]:
        return [
            {"start_s": window.start_s, "end_s": window.end_s, "duration_s": window.duration_s}
            for window in self.windows
        ]


@dataclass
class Series:
    times_s: list[float]
    values_n: list[float]


@dataclass
class RtdeFz:
    ur: Series
    urcap: Series
    urcap_updates: Series
    row_count: int
    urcap_update_count: int


@dataclass
class UdpFz:
    raw: Series
    row_count: int


def f(row: dict[str, str], key: str) -> float:
    return float(row[key])


def stdev(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def read_rtde_fz(path: Path, window_s: float, clean: CleanTimeline | None = None) -> RtdeFz:
    read_until = window_s + (clean.duration_s if clean else 0.0)
    ur_times: list[float] = []
    ur_vals: list[float] = []
    urcap_times: list[float] = []
    urcap_vals: list[float] = []
    update_times: list[float] = []
    update_vals: list[float] = []
    ur_ref: float | None = None
    urcap_ref: float | None = None
    previous_tuple: tuple[float, ...] | None = None

    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            original_t = f(row, "t_s")
            if original_t > read_until:
                break
            t_s = clean.transform(original_t) if clean else original_t
            if t_s is None or t_s > window_s:
                continue

            ur_value = f(row, "ur_fz_n")
            urcap_value = f(row, "urcap_fz_n")
            if ur_ref is None:
                ur_ref = ur_value
            if urcap_ref is None:
                urcap_ref = urcap_value
            ur_zeroed = ur_value - ur_ref
            urcap_zeroed = urcap_value - urcap_ref

            ur_times.append(t_s)
            ur_vals.append(ur_zeroed)
            urcap_times.append(t_s)
            urcap_vals.append(urcap_zeroed)

            tup = tuple(f(row, key) for key in URCAP_FIELDS)
            if previous_tuple is None or tup != previous_tuple:
                update_times.append(t_s)
                update_vals.append(urcap_zeroed)
                previous_tuple = tup

    return RtdeFz(
        ur=Series(ur_times, ur_vals),
        urcap=Series(urcap_times, urcap_vals),
        urcap_updates=Series(update_times, update_vals),
        row_count=len(ur_times),
        urcap_update_count=len(update_times),
    )


def read_udp_fz(path: Path, window_s: float, clean: CleanTimeline | None = None) -> UdpFz:
    read_until = window_s + (clean.duration_s if clean else 0.0)
    times: list[float] = []
    values: list[float] = []
    ref: float | None = None

    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            original_t = f(row, "t_s")
            if original_t > read_until:
                break
            t_s = clean.transform(original_t) if clean else original_t
            if t_s is None or t_s > window_s:
                continue
            value = f(row, "fz_n")
            if ref is None:
                ref = value
            times.append(t_s)
            values.append(value - ref)

    return UdpFz(raw=Series(times, values), row_count=len(times))


def rate_hz(times: list[float]) -> float | None:
    if len(times) < 2:
        return None
    duration = times[-1] - times[0]
    return (len(times) - 1) / duration if duration > 0 else None


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def rmse(values: list[float]) -> float | None:
    return math.sqrt(statistics.fmean([value * value for value in values])) if values else None


def bin_means(times: Iterable[float], values: Iterable[float], bin_seconds: float = 1.0) -> dict[str, list[float]]:
    bins: dict[int, list[float]] = {}
    for t_s, value in zip(times, values):
        key = int(t_s // bin_seconds)
        bins.setdefault(key, []).append(value)
    centers: list[float] = []
    means: list[float] = []
    for key in sorted(bins):
        centers.append((key + 0.5) * bin_seconds)
        means.append(statistics.fmean(bins[key]))
    return {"centers_s": centers, "means": means}


def nearest_match(urcap_updates: Series, udp: Series) -> dict[str, object]:
    matched_times: list[float] = []
    matched_udp: list[float] = []
    matched_urcap: list[float] = []
    residuals: list[float] = []
    offsets_ms: list[float] = []

    for t_s, urcap_value in zip(urcap_updates.times_s, urcap_updates.values_n):
        pos = bisect.bisect_left(udp.times_s, t_s)
        candidates = []
        if pos > 0:
            candidates.append(pos - 1)
        if pos < len(udp.times_s):
            candidates.append(pos)
        if not candidates:
            continue
        best = min(candidates, key=lambda idx: abs(udp.times_s[idx] - t_s))
        udp_value = udp.values_n[best]
        matched_times.append(t_s)
        matched_urcap.append(urcap_value)
        matched_udp.append(udp_value)
        residuals.append(urcap_value - udp_value)
        offsets_ms.append((t_s - udp.times_s[best]) * 1000.0)

    residual_bins = bin_means(matched_times, residuals)
    return {
        "times_s": matched_times,
        "urcap_fz_zeroed_n": matched_urcap,
        "matched_udp_fz_zeroed_n": matched_udp,
        "residual_urcap_minus_udp_n": residuals,
        "urcap_bins": bin_means(matched_times, matched_urcap),
        "udp_bins": bin_means(matched_times, matched_udp),
        "residual_bins": residual_bins,
        "summary": {
            "matched_pairs": len(residuals),
            "urcap_mean_n": mean(matched_urcap),
            "udp_mean_n": mean(matched_udp),
            "mean_diff_n": mean(residuals),
            "residual_std_n": stdev(residuals),
            "mae_n": mean([abs(value) for value in residuals]),
            "rmse_n": rmse(residuals),
            "max_abs_n": max([abs(value) for value in residuals]) if residuals else None,
            "one_second_mean_rmse_n": rmse(residual_bins["means"]),
            "nearest_time_offset_abs_mean_ms": mean([abs(value) for value in offsets_ms]),
        },
    }


def plot_onrobot_comparison(
    match: dict[str, object],
    output: Path,
    window_s: float,
    note: str = "",
    simple: bool = False,
) -> None:
    times = match["times_s"]
    urcap = match["urcap_fz_zeroed_n"]
    udp = match["matched_udp_fz_zeroed_n"]
    residuals = match["residual_urcap_minus_udp_n"]
    urcap_bins = match["urcap_bins"]
    udp_bins = match["udp_bins"]
    residual_bins = match["residual_bins"]
    summary = match["summary"]
    stride = max(1, len(times) // 30000)

    if simple:
        fig, ax = plt.subplots(figsize=(12, 5.2), dpi=150)
        ax.plot(times[::stride], urcap[::stride], linewidth=0.55, alpha=0.18, label="URCap matched points")
        ax.plot(times[::stride], udp[::stride], linewidth=0.55, alpha=0.18, label="Nearest UDP points")
        ax.plot(urcap_bins["centers_s"], urcap_bins["means"], linewidth=1.9, label="URCap 1 s mean (125 Hz)")
        ax.plot(udp_bins["centers_s"], udp_bins["means"], linewidth=1.9, label="UDP 1 s mean (SPEED=2, about 500 Hz packets)")
        ax.axhline(summary["urcap_mean_n"], color="#1f77b4", linestyle="--", linewidth=1.0, label="URCap global mean")
        ax.axhline(summary["udp_mean_n"], color="#ff7f0e", linestyle="--", linewidth=1.0, label="UDP global mean")
        ax.set_ylabel("Fz - first sample (N)")
        ax.set_xlabel("Time (s)")
        title = f"OnRobot Fz: 1 s mean and global mean, URCap 125 Hz vs UDP SPEED=2, 0-{window_s:.0f} s"
        if note:
            title = f"{title} ({note})"
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="best", ncol=2)
        fig.tight_layout()
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(output)
        plt.close(fig)
        return

    fig, axes = plt.subplots(2, 1, figsize=(12, 7.5), dpi=150, sharex=True)
    if not simple:
        axes[0].plot(times[::stride], urcap[::stride], linewidth=0.55, alpha=0.18, label="URCap matched points")
        axes[0].plot(times[::stride], udp[::stride], linewidth=0.55, alpha=0.18, label="Nearest UDP points")
    axes[0].plot(urcap_bins["centers_s"], urcap_bins["means"], linewidth=1.9, label="URCap 1 s mean (125 Hz)")
    axes[0].plot(udp_bins["centers_s"], udp_bins["means"], linewidth=1.9, label="UDP 1 s mean (500 Hz source)")
    if not simple:
        axes[0].axhline(summary["urcap_mean_n"], color="#1f77b4", linestyle="--", linewidth=1.0, label="URCap global mean")
        axes[0].axhline(summary["udp_mean_n"], color="#ff7f0e", linestyle="--", linewidth=1.0, label="UDP global mean")
    axes[0].set_ylabel("Fz - first sample (N)")
    title = f"OnRobot Fz: 1 s mean and global mean, URCap 125 Hz vs UDP 500 Hz, 0-{window_s:.0f} s"
    if note:
        title = f"{title} ({note})"
    axes[0].set_title(title)
    axes[0].grid(True, alpha=0.3)
    axes[0].legend(loc="best", ncol=2)

    axes[1].plot(times[::stride], residuals[::stride], linewidth=0.45, alpha=0.18, color="#7c3aed", label="Matched residual")
    axes[1].plot(residual_bins["centers_s"], residual_bins["means"], linewidth=1.8, color="#7c3aed", label="Residual 1 s mean")
    axes[1].axhline(0.0, color="black", linewidth=0.8, alpha=0.5)
    axes[1].axhline(summary["mean_diff_n"], color="#7c3aed", linestyle="--", linewidth=1.0, label="Residual global mean")
    axes[1].set_xlabel("Time (s)")
    axes[1].set_ylabel("URCap - UDP (N)")
    axes[1].grid(True, alpha=0.3)
    axes[1].legend(loc="best")

    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output)
    plt.close(fig)


def plot_cleaned_overlay(rtde: RtdeFz, udp: UdpFz, output: Path, window_s: float, note: str) -> None:
    stride_rtde = max(1, len(rtde.ur.times_s) // 50000)
    stride_udp = max(1, len(udp.raw.times_s) // 50000)
    fig, ax = plt.subplots(figsize=(12, 5.5), dpi=150)
    ax.plot(rtde.ur.times_s[::stride_rtde], rtde.ur.values_n[::stride_rtde], linewidth=0.8, label="UR actual_TCP_force (500 Hz)")
    ax.plot(rtde.urcap.times_s[::stride_rtde], rtde.urcap.values_n[::stride_rtde], linewidth=0.8, label="OnRobot URCap registers (125 Hz)")
    ax.plot(udp.raw.times_s[::stride_udp], udp.raw.values_n[::stride_udp], linewidth=0.8, label="OnRobot UDP raw (500 Hz)")
    title = f"Fz first-sample-zeroed overlay, 0-{window_s:.0f} s"
    if note:
        title = f"{title} ({note})"
    ax.set_title(title)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Fz - first sample (N)")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best")
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output)
    plt.close(fig)


def build_case(
    rtde_path: Path,
    udp_path: Path,
    window_s: float,
    output_dir: Path,
    stem: str,
    clean: CleanTimeline | None = None,
    note: str = "",
    overlay: bool = False,
    simple_comparison: bool = False,
) -> dict[str, object]:
    rtde = read_rtde_fz(rtde_path, window_s, clean=clean)
    udp = read_udp_fz(udp_path, window_s, clean=clean)
    match = nearest_match(rtde.urcap_updates, udp.raw)

    comparison_png = output_dir / f"{stem}_fz_urcap125_udp500_compare.png"
    plot_onrobot_comparison(match, comparison_png, window_s, note=note, simple=simple_comparison)
    overlay_png = None
    if overlay:
        overlay_png = output_dir / f"{stem}_fz_overlay_zeroed.png"
        plot_cleaned_overlay(rtde, udp, overlay_png, window_s, note=note)

    summary = {
        "window_s": window_s,
        "clean_windows": None if clean is None else clean.as_summary(),
        "clean_duration_s": None if clean is None else clean.duration_s,
        "rtde_rows": rtde.row_count,
        "rtde_rate_hz": rate_hz(rtde.ur.times_s),
        "urcap_updates": rtde.urcap_update_count,
        "urcap_update_rate_hz": rate_hz(rtde.urcap_updates.times_s),
        "udp_packets": udp.row_count,
        "udp_rate_hz": rate_hz(udp.raw.times_s),
        "fz_comparison": match["summary"],
        "comparison_png": str(comparison_png),
        "overlay_png": None if overlay_png is None else str(overlay_png),
    }
    (output_dir / f"{stem}_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path("/home/andy/ur10e_ros2_ws/report/assets/demo_01_02_meeting_deck_20260528"))
    args = parser.parse_args()

    run600 = Path("/home/andy/ur10e_ros2_ws/experiments/20260528_onrobot_three_stream_600s_first_zero/run_20260528_043100")
    run90 = Path("/home/andy/ur10e_ros2_ws/experiments/20260528_onrobot_three_stream_coldstart_drift/run_20260528_141149")
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "six_hundred_second": build_case(
            run600 / "three_stream_600s_20260528_043052_rtde_ur500_urcap125.csv",
            run600 / "three_stream_600s_20260528_043052_onrobot_udp500_raw.csv",
            600.0,
            output_dir,
            "three_stream_600s_20260528",
            overlay=True,
            simple_comparison=True,
        ),
        "two_hour_cleaned": build_case(
            run90 / "three_stream_coldstart_drift_20260528_141152_rtde_ur500_urcap125.csv",
            run90 / "three_stream_coldstart_drift_20260528_141152_onrobot_udp500_raw.csv",
            7200.0,
            output_dir,
            "onrobot_three_stream_2h_20260528_artifacts_removed",
            clean=CleanTimeline(
                (
                    CleanWindow(2249.0, 2269.0),
                    CleanWindow(5124.0, 5148.0),
                    CleanWindow(6078.0, 6110.0),
                    CleanWindow(6288.0, 6313.0),
                )
            ),
            note="artifact intervals removed",
            overlay=True,
        ),
    }
    (output_dir / "deck_onrobot_fz_assets_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
