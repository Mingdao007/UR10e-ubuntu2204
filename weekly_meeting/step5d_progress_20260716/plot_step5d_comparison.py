#!/usr/bin/env python3
"""Recompute the Step5B/Step5D comparison and build report figures.

This script is deliberately read-only with respect to experiment evidence. It
selects the main contact-control interval, reconstructs the projected normal
load from the force and normal-vector columns, and writes only derived report
artifacts to this report directory.

Historical comparison time base:
    elapsed = t_monotonic_s - first_main_control_t_monotonic_s

This is intentionally different from the future autotune campaign's frozen
path-time contract. Using path time for the historical Step5D run produces 545
instead of 550 comparison bins and therefore fails validation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


TARGET_N = 12.0
BIN_S = 0.1
COMPARISON_START_S = 5.0
COMPARISON_END_S = 60.0
EXPECTED_BINS = 550

INK = "#172026"
MUTED = "#5c6872"
LINE = "#d9e0e6"
PANEL = "#f6f8fa"
GREEN = "#2f8068"
BLUE = "#2e6ea6"
AMBER = "#9a6b22"
DANGER = "#a84432"
LIGHT_BLUE = "#eaf2f8"
LIGHT_GREEN = "#e9f3ef"

RUN_SPECS = (
    {
        "key": "step5b_a",
        "label": "Step5B Reference A",
        "state": 30,
        "color": BLUE,
        "sha256": "b8351dd7641300703dbd3a53eb247beadba5b00833b1126bf2abc094edaf368c",
    },
    {
        "key": "step5b_b",
        "label": "Step5B Reference B",
        "state": 30,
        "color": AMBER,
        "sha256": "9dfddc7dc9410b412f25017eaad715488bdaed2889805875b247efb9aa3a5c85",
    },
    {
        "key": "step5d",
        "label": "Step5D",
        "state": 524,
        "color": GREEN,
        "sha256": "521b8dcdea0a9698dec64a4b0d7112b14d9e300d55fec7d4ebaec8271c2633ff",
    },
)

REQUIRED_COLUMNS = (
    "t_monotonic_s",
    "step4e_controller_state",
    "_step4e_force_b_x",
    "_step4e_force_b_y",
    "_step4e_force_b_z",
    "_step4e_control_normal_b_x",
    "_step4e_control_normal_b_y",
    "_step4e_control_normal_b_z",
    "_step4e_normal_load_n",
    "ur_actual_TCP_speed_0",
    "ur_actual_TCP_speed_1",
    "ur_actual_TCP_speed_2",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--step5b-a", type=Path, required=True)
    parser.add_argument("--step5b-b", type=Path, required=True)
    parser.add_argument("--step5d", type=Path, required=True)
    parser.add_argument("--five-n-summary", type=Path, required=True)
    parser.add_argument("--ten-n-summary", type=Path, required=True)
    parser.add_argument("--twelve-n-summary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    return parser.parse_args()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def require_columns(frame: pd.DataFrame, columns: Iterable[str], label: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{label}: missing required columns: {missing}")


def population_std(values: np.ndarray) -> float:
    return float(np.std(values, ddof=0))


def prepare_run(path: Path, spec: Mapping[str, Any]) -> Dict[str, Any]:
    actual_hash = sha256(path)
    if actual_hash != spec["sha256"]:
        raise ValueError(
            f"{spec['label']}: SHA-256 mismatch; expected {spec['sha256']}, got {actual_hash}"
        )

    frame = pd.read_csv(path, low_memory=False)
    require_columns(frame, REQUIRED_COLUMNS, str(spec["label"]))
    state = pd.to_numeric(frame["step4e_controller_state"], errors="coerce")
    selected = frame.loc[state == float(spec["state"])].copy()
    if selected.empty:
        raise ValueError(f"{spec['label']}: no main contact-control rows")

    selected["t_monotonic_s"] = pd.to_numeric(selected["t_monotonic_s"], errors="coerce")
    selected = selected.dropna(subset=["t_monotonic_s"]).copy()
    t0 = float(selected["t_monotonic_s"].iloc[0])
    selected["elapsed_s"] = selected["t_monotonic_s"] - t0

    force = selected[
        ["_step4e_force_b_x", "_step4e_force_b_y", "_step4e_force_b_z"]
    ].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    normal = selected[
        [
            "_step4e_control_normal_b_x",
            "_step4e_control_normal_b_y",
            "_step4e_control_normal_b_z",
        ]
    ].apply(pd.to_numeric, errors="coerce").to_numpy(float)
    speed = selected[
        ["ur_actual_TCP_speed_0", "ur_actual_TCP_speed_1", "ur_actual_TCP_speed_2"]
    ].apply(pd.to_numeric, errors="coerce").to_numpy(float)

    selected["normal_load_n"] = np.einsum("ij,ij->i", force, normal)
    selected["normal_speed_mm_s"] = 1000.0 * np.einsum("ij,ij->i", speed, normal)
    direct = pd.to_numeric(selected["_step4e_normal_load_n"], errors="coerce").to_numpy(float)
    max_load_disagreement = float(np.nanmax(np.abs(direct - selected["normal_load_n"].to_numpy())))
    if max_load_disagreement > 1e-5:
        raise ValueError(
            f"{spec['label']}: projected-load reconstruction disagrees by {max_load_disagreement:.6g} N"
        )

    plot_rows = selected.loc[
        (selected["elapsed_s"] >= 0.0) & (selected["elapsed_s"] < COMPARISON_END_S)
    ].copy()
    plot_rows["bin"] = np.floor(plot_rows["elapsed_s"] / BIN_S + 1e-9).astype(int)
    plot_binned = (
        plot_rows.groupby("bin", sort=True)
        .agg(
            elapsed_s=("elapsed_s", "mean"),
            normal_load_n=("normal_load_n", "mean"),
            normal_speed_rms_mm_s=(
                "normal_speed_mm_s",
                lambda x: float(np.sqrt(np.mean(np.square(x)))),
            ),
        )
        .reset_index()
    )

    comparison = selected.loc[
        (selected["elapsed_s"] >= COMPARISON_START_S)
        & (selected["elapsed_s"] < COMPARISON_END_S)
    ].copy()
    comparison["bin"] = np.floor(
        (comparison["elapsed_s"] - COMPARISON_START_S) / BIN_S + 1e-9
    ).astype(int)
    binned = (
        comparison.groupby("bin", sort=True)
        .agg(
            elapsed_s=("elapsed_s", "mean"),
            normal_load_n=("normal_load_n", "mean"),
            normal_speed_rms_mm_s=(
                "normal_speed_mm_s",
                lambda x: float(np.sqrt(np.mean(np.square(x)))),
            ),
        )
        .reset_index()
    )
    if binned["bin"].tolist() != list(range(EXPECTED_BINS)):
        raise ValueError(
            f"{spec['label']}: expected bins 0..{EXPECTED_BINS - 1}, got "
            f"{len(binned)} bins spanning {binned['bin'].min()}..{binned['bin'].max()}"
        )

    loads = binned["normal_load_n"].to_numpy(float)
    normal_speed_rms = float(
        np.sqrt(np.mean(np.square(comparison["normal_speed_mm_s"].to_numpy(float))))
    )
    metrics = {
        "bins": int(len(loads)),
        "mean_load_n": float(np.mean(loads)),
        "force_std_n": population_std(loads),
        "force_mae_n": float(np.mean(np.abs(loads - TARGET_N))),
        "within_1n_pct": float(100.0 * np.mean(np.abs(loads - TARGET_N) <= 1.0)),
        "normal_speed_rms_mm_s": normal_speed_rms,
        "min_bin_mean_n": float(np.min(loads)),
        "max_bin_mean_n": float(np.max(loads)),
    }

    windows: List[Dict[str, float]] = []
    for start in np.arange(0.0, 60.0, 5.0):
        window = selected.loc[
            (selected["elapsed_s"] >= start) & (selected["elapsed_s"] < start + 5.0),
            "normal_load_n",
        ].to_numpy(float)
        if window.size == 0:
            raise ValueError(f"{spec['label']}: empty 5 s window starting at {start}")
        windows.append(
            {
                "start_s": float(start),
                "end_s": float(start + 5.0),
                "mean_n": float(np.mean(window)),
                "std_n": population_std(window),
                "rows": int(window.size),
            }
        )

    speed_rms_bins = binned["normal_speed_rms_mm_s"].to_numpy(float)
    absolute_error_bins = np.abs(loads - TARGET_N)
    correlation = float(np.corrcoef(speed_rms_bins, absolute_error_bins)[0, 1])

    return {
        "spec": dict(spec),
        "sha256": actual_hash,
        "metrics": metrics,
        "windows": windows,
        "plot_binned": plot_binned,
        "binned": binned,
        "correlation": correlation,
        "main_control_span_s": float(selected["elapsed_s"].max()),
        "projection_check_max_abs_n": max_load_disagreement,
    }


def mean_metrics(runs: Iterable[Mapping[str, Any]]) -> Dict[str, float]:
    keys = (
        "mean_load_n",
        "force_std_n",
        "force_mae_n",
        "within_1n_pct",
        "normal_speed_rms_mm_s",
    )
    selected = list(runs)
    return {
        key: float(np.mean([run["metrics"][key] for run in selected])) for key in keys
    }


def load_target_selection_evidence(
    five_path: Path, ten_path: Path, twelve_path: Path
) -> Dict[str, Any]:
    five = json.loads(five_path.read_text())
    ten = json.loads(ten_path.read_text())
    twelve = json.loads(twelve_path.read_text())
    ten_load = ten["stats"]["normal_load_n"]
    twelve_load = twelve["stats"]["normal_load_n"]
    return {
        "five_n": {
            "attempt": "5 to 15 N ramp",
            "stop_reason": "low-load dropout",
            "target_max_n": float(five["metrics"]["target_max_n"]),
            "target_reached_final": bool(five["metrics"]["target_reached_final"]),
            "sha256": sha256(five_path),
        },
        "ten_n": {
            "duration_s": float(ten["active_window"]["duration_s"]),
            "mean_n": float(ten_load["mean"]),
            "median_n": float(ten_load["median"]),
            "p05_n": float(ten_load["p05"]),
            "p95_n": float(ten_load["p95"]),
            "max_n": float(ten_load["max"]),
            "below_5n_s": float(ten["stats"]["normal_load_lt_5_s"]),
            "above_20n_s": float(ten["stats"]["normal_load_gt_20_s"]),
            "sha256": sha256(ten_path),
        },
        "twelve_n": {
            "duration_s": float(twelve["active_window"]["duration_s"]),
            "mean_n": float(twelve_load["mean"]),
            "median_n": float(twelve_load["median"]),
            "p05_n": float(twelve_load["p05"]),
            "p95_n": float(twelve_load["p95"]),
            "max_n": float(twelve_load["max"]),
            "below_5n_s": float(twelve["stats"]["normal_load_lt_5_s"]),
            "above_20n_s": float(twelve["stats"]["normal_load_gt_20_s"]),
            "sha256": sha256(twelve_path),
        },
    }


def style_axis(axis: plt.Axes) -> None:
    axis.set_facecolor("white")
    axis.grid(True, color=LINE, linewidth=0.8, alpha=0.9)
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)
    axis.spines["left"].set_color(MUTED)
    axis.spines["bottom"].set_color(MUTED)
    axis.tick_params(colors=MUTED, labelsize=9)


def save_figure(fig: plt.Figure, assets: Path, stem: str) -> None:
    svg_path = assets / f"{stem}.svg"
    fig.savefig(svg_path, bbox_inches="tight", facecolor="white")
    # Matplotlib's SVG writer leaves spaces at the ends of many path lines.
    # Normalize them so regenerated report figures remain git-diff clean.
    svg_text = svg_path.read_text(encoding="utf-8")
    svg_path.write_text(
        "\n".join(line.rstrip() for line in svg_text.splitlines()) + "\n",
        encoding="utf-8",
    )
    fig.savefig(assets / f"{stem}.png", dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def plot_normal_load(runs: List[Mapping[str, Any]], assets: Path) -> None:
    fig, axes = plt.subplots(3, 1, figsize=(12.0, 7.6), sharex=True, sharey=True)
    fig.suptitle("Normal load versus time", x=0.08, ha="left", fontsize=17, color=INK, weight="bold")
    for axis, run in zip(axes, runs):
        data = run["plot_binned"]
        axis.axhspan(11.0, 13.0, color=LIGHT_BLUE, alpha=0.8, zorder=0)
        axis.axvspan(0.0, 5.0, color=LINE, alpha=0.65, zorder=0)
        axis.axhline(TARGET_N, color=INK, linewidth=1.0, linestyle="--", zorder=1)
        axis.plot(
            data["elapsed_s"],
            data["normal_load_n"],
            color=run["spec"]["color"],
            linewidth=1.15,
        )
        axis.text(
            0.012,
            0.84,
            run["spec"]["label"],
            transform=axis.transAxes,
            color=INK,
            fontsize=10.5,
            weight="bold",
        )
        axis.set_ylim(4.5, 20.2)
        axis.set_ylabel("Load [N]", color=INK, fontsize=10)
        style_axis(axis)
    axes[-1].set_xlim(0.0, 60.0)
    axes[-1].set_xlabel("Elapsed main-control time [s]", color=INK, fontsize=10.5)
    axes[0].text(2.5, 18.6, "excluded transition", ha="center", va="center", fontsize=8.5, color=MUTED)
    axes[0].text(58.8, 12.2, "12 N", ha="right", va="bottom", fontsize=8.5, color=INK)
    fig.text(
        0.08,
        0.925,
        "100 ms mean load; first 5 s shown for context and excluded from headline metrics",
        ha="left",
        fontsize=10,
        color=MUTED,
    )
    fig.subplots_adjust(left=0.08, right=0.98, top=0.87, bottom=0.08, hspace=0.12)
    save_figure(fig, assets, "normal_load_vs_time")


def plot_metric_comparison(runs: List[Mapping[str, Any]], b_mean: Mapping[str, float], assets: Path) -> None:
    panels = (
        ("force_std_n", "Force standard deviation", "N", "lower is better"),
        ("force_mae_n", "Force MAE", "N", "lower is better"),
        ("within_1n_pct", "Samples within +/-1 N", "%", "higher is better"),
        ("normal_speed_rms_mm_s", "Actual normal-speed RMS", "mm/s", "lower is better"),
    )
    labels = ["B Ref A", "B Ref B", "B mean", "Step5D"]
    x = np.arange(4)
    fig, axes = plt.subplots(2, 2, figsize=(12.0, 7.6))
    fig.suptitle("Matched-condition metric comparison", x=0.08, ha="left", fontsize=17, color=INK, weight="bold")
    for axis, (key, title, unit, direction) in zip(axes.flat, panels):
        values = [runs[0]["metrics"][key], runs[1]["metrics"][key], b_mean[key], runs[2]["metrics"][key]]
        axis.axvspan(1.55, 2.45, color=LIGHT_BLUE, alpha=0.75, zorder=0)
        axis.scatter(x[:2], values[:2], s=52, facecolors="white", edgecolors=[BLUE, AMBER], linewidths=1.8, zorder=3)
        axis.scatter(x[2], values[2], s=72, marker="D", color=BLUE, zorder=3)
        axis.scatter(x[3], values[3], s=72, marker="s", color=GREEN, zorder=3)
        axis.plot(x[:2], values[:2], color=LINE, linewidth=1.0, zorder=1)
        for xi, value in zip(x, values):
            precision = 1 if key == "within_1n_pct" else 3
            axis.annotate(
                f"{value:.{precision}f}",
                (xi, value),
                xytext=(0, 8),
                textcoords="offset points",
                ha="center",
                fontsize=8.5,
                color=INK,
            )
        axis.set_title(title, loc="left", fontsize=12, color=INK, weight="bold")
        axis.text(1.0, 1.02, direction, transform=axis.transAxes, ha="right", fontsize=8.5, color=MUTED)
        axis.set_ylabel(unit, color=INK, fontsize=9.5)
        axis.set_xticks(x)
        axis.set_xticklabels(labels, fontsize=8.5)
        axis.set_ylim(0, max(values) * 1.25)
        style_axis(axis)
    fig.text(
        0.08,
        0.925,
        "Two physical Step5B references are retained; the comparison baseline is their arithmetic mean",
        ha="left",
        fontsize=10,
        color=MUTED,
    )
    fig.subplots_adjust(left=0.08, right=0.98, top=0.84, bottom=0.10, wspace=0.20, hspace=0.32)
    save_figure(fig, assets, "metric_small_multiples")


def plot_five_second_windows(runs: List[Mapping[str, Any]], assets: Path) -> None:
    fig, axis = plt.subplots(figsize=(12.0, 5.9))
    fig.suptitle("Five-second stability windows", x=0.08, ha="left", fontsize=17, color=INK, weight="bold")
    centers = np.arange(2.5, 60.0, 5.0)
    offsets = (-0.65, 0.0, 0.65)
    markers = ("o", "^", "s")
    for run, offset, marker in zip(runs, offsets, markers):
        means = np.array([window["mean_n"] for window in run["windows"]])
        stds = np.array([window["std_n"] for window in run["windows"]])
        axis.errorbar(
            centers + offset,
            means,
            yerr=stds,
            fmt=marker,
            markersize=5,
            capsize=2.5,
            elinewidth=1.0,
            linewidth=1.0,
            color=run["spec"]["color"],
            label=run["spec"]["label"],
        )
    axis.axvspan(0.0, 5.0, color=LINE, alpha=0.65, zorder=0)
    axis.axhspan(11.0, 13.0, color=LIGHT_BLUE, alpha=0.55, zorder=0)
    axis.axhline(TARGET_N, color=INK, linewidth=1.0, linestyle="--")
    axis.text(2.5, 17.2, "transition", ha="center", fontsize=8.5, color=MUTED)
    axis.set_xlim(0.0, 60.0)
    axis.set_ylim(6.0, 18.0)
    axis.set_xlabel("Window center [s]", color=INK, fontsize=10.5)
    axis.set_ylabel("Raw-row mean +/- population std [N]", color=INK, fontsize=10)
    axis.legend(frameon=False, ncol=3, loc="upper right", fontsize=9)
    style_axis(axis)
    fig.text(
        0.08,
        0.92,
        "Each marker summarizes all raw samples in one 5 s window; the first window is context only",
        ha="left",
        fontsize=10,
        color=MUTED,
    )
    fig.subplots_adjust(left=0.08, right=0.98, top=0.86, bottom=0.14)
    save_figure(fig, assets, "five_second_stability_windows")


def plot_motion_force_relationship(runs: List[Mapping[str, Any]], assets: Path) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(12.0, 4.7), sharex=True, sharey=True)
    fig.suptitle(
        "100 ms normal-motion activity versus force error",
        x=0.08,
        ha="left",
        fontsize=17,
        color=INK,
        weight="bold",
    )
    all_x = np.concatenate([run["binned"]["normal_speed_rms_mm_s"].to_numpy(float) for run in runs])
    all_y = np.concatenate([np.abs(run["binned"]["normal_load_n"].to_numpy(float) - TARGET_N) for run in runs])
    x_max = float(np.max(all_x)) * 1.04
    y_max = float(np.max(all_y)) * 1.04
    for axis, run in zip(axes, runs):
        x = run["binned"]["normal_speed_rms_mm_s"].to_numpy(float)
        y = np.abs(run["binned"]["normal_load_n"].to_numpy(float) - TARGET_N)
        axis.scatter(x, y, s=12, alpha=0.30, color=run["spec"]["color"], edgecolors="none")
        axis.set_title(run["spec"]["label"], fontsize=10.5, color=INK, weight="bold")
        axis.text(
            0.96,
            0.94,
            f"r = {run['correlation']:+.2f}\n550 bins",
            transform=axis.transAxes,
            ha="right",
            va="top",
            fontsize=8.5,
            color=MUTED,
        )
        axis.set_xlim(0.0, x_max)
        axis.set_ylim(0.0, y_max)
        axis.set_xlabel("normal-speed RMS [mm/s]", fontsize=9, color=INK)
        style_axis(axis)
    axes[0].set_ylabel("absolute force error [N]", fontsize=9.5, color=INK)
    fig.text(
        0.08,
        0.91,
        "Same 5-60 s interval and 100 ms grain as the headline force metrics; correlation is descriptive, not causal",
        ha="left",
        fontsize=10,
        color=MUTED,
    )
    fig.subplots_adjust(left=0.08, right=0.98, top=0.82, bottom=0.17, wspace=0.16)
    save_figure(fig, assets, "motion_force_relationship")


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    assets = output_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    paths = {
        "step5b_a": args.step5b_a,
        "step5b_b": args.step5b_b,
        "step5d": args.step5d,
    }
    runs = [prepare_run(paths[spec["key"]], spec) for spec in RUN_SPECS]
    b_mean = mean_metrics(runs[:2])
    d = runs[2]["metrics"]
    changes = {
        "force_std_reduction_pct": 100.0 * (1.0 - d["force_std_n"] / b_mean["force_std_n"]),
        "force_mae_reduction_pct": 100.0 * (1.0 - d["force_mae_n"] / b_mean["force_mae_n"]),
        "within_1n_change_pp": d["within_1n_pct"] - b_mean["within_1n_pct"],
        "normal_speed_rms_reduction_pct": 100.0
        * (1.0 - d["normal_speed_rms_mm_s"] / b_mean["normal_speed_rms_mm_s"]),
        "mean_load_change_n": d["mean_load_n"] - b_mean["mean_load_n"],
    }

    target_selection = load_target_selection_evidence(
        args.five_n_summary, args.ten_n_summary, args.twelve_n_summary
    )

    plot_normal_load(runs, assets)
    plot_metric_comparison(runs, b_mean, assets)
    plot_five_second_windows(runs, assets)
    plot_motion_force_relationship(runs, assets)

    metrics = {
        "schema": "step5d_group_meeting_metrics_v1",
        "comparison": {
            "target_force_n": TARGET_N,
            "time_window_s": [COMPARISON_START_S, COMPARISON_END_S],
            "bin_width_s": BIN_S,
            "time_basis": "monotonic elapsed from first main contact-control row",
            "headline_metrics_use": "550 mean-load values from fixed 100 ms bins",
            "normal_speed_metric_use": "raw rows in the same 5-60 s interval",
            "first_5s": "shown as transition context; excluded from headline metrics",
            "runs": {
                run["spec"]["key"]: {
                    "label": run["spec"]["label"],
                    "sha256": run["sha256"],
                    "metrics": run["metrics"],
                    "five_second_windows": run["windows"],
                    "normal_speed_force_error_correlation": run["correlation"],
                    "projection_check_max_abs_n": run["projection_check_max_abs_n"],
                }
                for run in runs
            },
            "step5b_mean": b_mean,
            "step5d_change": changes,
        },
        "target_selection": target_selection,
        "surface_preparation": {
            "abrasive_grits": [240, 320, 400, 600],
            "common_to_step5b_and_step5d": True,
            "roughness_measurement_available": False,
        },
        "autotune": {
            "physical_tuning_results_available": False,
            "baseline": {"P": 0.001, "I": 1e-5, "damping": 7.0},
            "lattice": "theta = theta0 * 2^(k/4)",
            "adjacent_ratio": float(2 ** 0.25),
            "objective": "mean absolute error of 550 100 ms mean-load bins relative to 12 N",
            "success_mae_n": 0.30,
            "repeatability_relative_difference_pct": 15.0,
        },
    }
    (output_dir / "metrics.json").write_text(
        json.dumps(json_safe(metrics), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(json.dumps(json_safe({"ok": True, "metrics": metrics["comparison"]}), indent=2))


if __name__ == "__main__":
    main()
