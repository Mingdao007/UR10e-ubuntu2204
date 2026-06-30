#!/usr/bin/env python3
"""Build the Step5b post-run one-page diagnostic overview."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402


DEFAULT_MAC_TARGET = "andyl@100.127.94.11:/Users/andyl/Downloads/ur10e_step5b_plots/"
CSV_NAME = "bridge_rtde_500hz.csv"
PNG_NAME = "step5b_diagnostic_overview.png"
SUMMARY_NAME = "step5b_diagnostic_overview_summary.json"


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive provenance only
        return {"_load_error": f"{type(exc).__name__}: {exc}"}


def numeric(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(df[col], errors="coerce")


def finite_stats(values: pd.Series) -> dict[str, float | int | None]:
    arr = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if arr.empty:
        return {"samples": 0, "mean": None, "median": None, "p05": None, "p95": None, "p99": None, "min": None, "max": None}
    return {
        "samples": int(arr.shape[0]),
        "mean": float(arr.mean()),
        "median": float(arr.median()),
        "p05": float(arr.quantile(0.05)),
        "p95": float(arr.quantile(0.95)),
        "p99": float(arr.quantile(0.99)),
        "min": float(arr.min()),
        "max": float(arr.max()),
    }


def finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def first_available_float(*values: Any, default: float | None = None) -> float | None:
    for value in values:
        parsed = finite_float(value)
        if parsed is not None:
            return parsed
    return default


def median_col(df: pd.DataFrame, col: str) -> float | None:
    series = numeric(df, col).dropna()
    if series.empty:
        return None
    return float(series.median())


def vector_norm(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    if any(col not in df.columns for col in cols):
        return pd.Series(dtype=float)
    data = np.vstack([numeric(df, col).to_numpy(dtype=float) for col in cols]).T
    return pd.Series(np.linalg.norm(data, axis=1), index=df.index)


def downsample(df: pd.DataFrame, max_points: int) -> pd.DataFrame:
    if len(df) <= max_points:
        return df
    step = max(1, int(math.ceil(len(df) / max_points)))
    return df.iloc[::step].copy()


def dwell_seconds(t_s: pd.Series, mask: pd.Series) -> float:
    t = pd.to_numeric(t_s, errors="coerce")
    m = mask.fillna(False).astype(bool)
    if len(t) < 2 or not m.any():
        return 0.0
    dt = t.diff().shift(-1)
    median_dt = dt[dt > 0].median()
    if not math.isfinite(float(median_dt)):
        median_dt = 0.0
    dt = dt.fillna(float(median_dt)).clip(lower=0.0)
    return float(dt[m].sum())


def missing_cols(df: pd.DataFrame, cols: list[str]) -> list[str]:
    return [col for col in cols if col not in df.columns]


def unsupported(ax: plt.Axes, title: str, missing: list[str], panel_status: dict[str, Any]) -> None:
    panel_status[title] = {"supported": False, "missing_fields": missing}
    ax.set_title(title)
    ax.axis("off")
    ax.text(
        0.5,
        0.5,
        "unsupported\nmissing: " + ", ".join(missing[:6]) + ("..." if len(missing) > 6 else ""),
        ha="center",
        va="center",
        fontsize=9,
        color="#991b1b",
        transform=ax.transAxes,
    )


def mark_supported(panel_status: dict[str, Any], title: str, note: str = "") -> None:
    panel_status[title] = {"supported": True, "note": note}


def style_axis(ax: plt.Axes) -> None:
    ax.grid(True, color="#d1d5db", linewidth=0.55, alpha=0.75)
    ax.tick_params(labelsize=8)
    ax.title.set_fontsize(10)
    ax.xaxis.label.set_fontsize(8)
    ax.yaxis.label.set_fontsize(8)


def active_window(df: pd.DataFrame) -> tuple[pd.Series, str]:
    if "ur_output_double_register_35" in df.columns:
        stage = numeric(df, "ur_output_double_register_35")
        mask = (stage - 25.0).abs() < 0.05
        if mask.any():
            return mask.fillna(False), "stage_id ~= 25.0 contact/path window"
    return pd.Series(True, index=df.index), "full log fallback: no Stage25 contact/path window found"


def target_force(active: pd.DataFrame, metadata: dict[str, Any]) -> float | None:
    args = metadata.get("args") if isinstance(metadata.get("args"), dict) else {}
    return first_available_float(
        median_col(active, "target_force_n"),
        args.get("target_force_n"),
        args.get("step4e_target_force_n"),
        args.get("bridge_target_force_n"),
        default=None,
    )


def limit_values(metadata: dict[str, Any]) -> dict[str, float | None]:
    args = metadata.get("args") if isinstance(metadata.get("args"), dict) else {}
    return {
        "angular_rad_s": first_available_float(args.get("step4e_angular_limit_rad_s"), args.get("bridge_angular_limit_rad_s")),
        "total_linear_m_s": first_available_float(args.get("step4e_total_linear_limit_m_s"), args.get("bridge_total_linear_limit_m_s")),
        "normal_velocity_m_s": first_available_float(args.get("step4e_normal_velocity_limit_m_s"), args.get("bridge_normal_velocity_limit_m_s")),
        "force_norm_n": first_available_float(args.get("max_force_norm_n")),
        "torque_norm_nm": first_available_float(args.get("max_torque_norm_nm")),
    }


def normal_command(df: pd.DataFrame) -> pd.Series:
    cmd_cols = ["step4e_cmd_vx_m_s", "step4e_cmd_vy_m_s", "step4e_cmd_vz_m_s"]
    normal_cols = ["_step4e_control_normal_b_x", "_step4e_control_normal_b_y", "_step4e_control_normal_b_z"]
    if not missing_cols(df, cmd_cols + normal_cols):
        return (
            numeric(df, cmd_cols[0]) * numeric(df, normal_cols[0])
            + numeric(df, cmd_cols[1]) * numeric(df, normal_cols[1])
            + numeric(df, cmd_cols[2]) * numeric(df, normal_cols[2])
        )
    if "step4e_cmd_vz_m_s" in df.columns:
        return numeric(df, "step4e_cmd_vz_m_s")
    return pd.Series(dtype=float)


def add_stage_shading(ax: plt.Axes, plot_df: pd.DataFrame, t_col: str) -> None:
    if "ur_output_double_register_35" not in plot_df.columns:
        return
    stage = numeric(plot_df, "ur_output_double_register_35")
    colors = {
        24.0: "#e8f1ff",
        24.2: "#dbeafe",
        24.3: "#fff7ed",
        24.4: "#ffedd5",
        25.0: "#dcfce7",
        25.1: "#f3e8ff",
        25.2: "#ede9fe",
        25.3: "#fef9c3",
        26.0: "#e5e7eb",
        27.0: "#f3f4f6",
    }
    t = numeric(plot_df, t_col)
    for stage_id, color in colors.items():
        mask = (stage - stage_id).abs() < 0.05
        if not mask.any():
            continue
        idxs = list(plot_df.index[mask])
        spans: list[tuple[Any, Any]] = []
        start = prev = idxs[0]
        for idx in idxs[1:]:
            if idx == prev + 1:
                prev = idx
            else:
                spans.append((start, prev))
                start = prev = idx
        spans.append((start, prev))
        for start, end in spans:
            ax.axvspan(float(t.loc[start]), float(t.loc[end]), color=color, alpha=0.38, linewidth=0)


def build_figure(run_dir: Path, max_points: int = 5000) -> dict[str, Any]:
    csv_path = run_dir / CSV_NAME
    png_path = run_dir / PNG_NAME
    summary_path = run_dir / SUMMARY_NAME
    metadata = load_json(run_dir / "metadata.json")
    bridge_summary = load_json(run_dir / "summary.json")
    stage_summary = load_json(run_dir / "stage_frequency_summary.json")

    if not csv_path.is_file():
        payload = {
            "ok": False,
            "issue": "bridge_rtde_500hz.csv not found",
            "run_dir": str(run_dir),
            "paths": {"summary": str(summary_path)},
        }
        summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return payload

    df = pd.read_csv(csv_path)
    if "t_monotonic_s" not in df.columns or df.empty:
        payload = {
            "ok": False,
            "issue": "missing t_monotonic_s or empty CSV",
            "run_dir": str(run_dir),
            "paths": {"bridge_csv": str(csv_path), "summary": str(summary_path)},
        }
        summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return payload

    mask, window_note = active_window(df)
    active = df.loc[mask].copy()
    if active.empty:
        active = df.copy()
        window_note = "full log fallback: active window mask was empty"
    active_t0 = float(numeric(active, "t_monotonic_s").iloc[0])
    full_t0 = float(numeric(df, "t_monotonic_s").iloc[0])
    active["t_active_s"] = numeric(active, "t_monotonic_s") - active_t0
    df["t_full_s"] = numeric(df, "t_monotonic_s") - full_t0
    plot = downsample(active, max_points=max_points)
    full_plot = downsample(df, max_points=max_points)
    target_n = target_force(active, metadata)
    limits = limit_values(metadata)
    panel_status: dict[str, Any] = {}

    fig, axes = plt.subplots(5, 3, figsize=(22, 27), dpi=155)
    ax = axes.ravel()
    fig.suptitle(f"Step5b diagnostic overview: {run_dir.name}", fontsize=16)

    # 1. Raw signed Fz.
    title = "Raw signed Fz tracking"
    if missing_cols(plot, ["fz_n_zeroed"]):
        unsupported(ax[0], title, ["fz_n_zeroed"], panel_status)
    else:
        add_stage_shading(ax[0], plot, "t_active_s")
        ax[0].plot(plot["t_active_s"], numeric(plot, "fz_n_zeroed"), color="#111827", linewidth=0.9, label="fz_n_zeroed")
        if target_n is not None:
            ax[0].axhline(-target_n, color="#dc2626", linestyle="--", linewidth=1.0, label="-target")
        ax[0].set_title(title)
        ax[0].set_xlabel("active time (s)")
        ax[0].set_ylabel("N")
        ax[0].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title)

    # 2. Projected normal load.
    title = "Projected normal load"
    if missing_cols(plot, ["_step4e_normal_load_n"]):
        unsupported(ax[1], title, ["_step4e_normal_load_n"], panel_status)
    else:
        ax[1].plot(plot["t_active_s"], numeric(plot, "_step4e_normal_load_n"), color="#2563eb", linewidth=0.95, label="normal_load")
        if target_n is not None:
            ax[1].axhline(target_n, color="#dc2626", linestyle="--", linewidth=1.0, label="target")
        ax[1].set_title(title)
        ax[1].set_xlabel("active time (s)")
        ax[1].set_ylabel("N")
        ax[1].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title)

    # 3. Force error and force norm.
    title = "Force error and force norm"
    needed_any = [c for c in ["step4e_force_error_n", "_step4e_normal_force_error_n", "force_norm_n"] if c in plot.columns]
    if not needed_any:
        unsupported(ax[2], title, ["step4e_force_error_n", "_step4e_normal_force_error_n", "force_norm_n"], panel_status)
    else:
        if "step4e_force_error_n" in plot.columns:
            ax[2].plot(plot["t_active_s"], numeric(plot, "step4e_force_error_n"), color="#9333ea", linewidth=0.85, label="force_error")
        if "_step4e_normal_force_error_n" in plot.columns:
            ax[2].plot(plot["t_active_s"], numeric(plot, "_step4e_normal_force_error_n"), color="#7c3aed", linewidth=0.75, linestyle="--", label="normal_force_error")
        if "force_norm_n" in plot.columns:
            ax[2].plot(plot["t_active_s"], numeric(plot, "force_norm_n"), color="#f97316", linewidth=0.75, label="force_norm")
        ax[2].axhline(0.0, color="#6b7280", linewidth=0.6)
        ax[2].set_title(title)
        ax[2].set_xlabel("active time (s)")
        ax[2].set_ylabel("N")
        ax[2].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title)

    # 4. XY actual vs reference.
    title = "TCP XY actual vs reference"
    xy_cols = ["ur_actual_TCP_pose_0", "ur_actual_TCP_pose_1", "_step4e_desired_x_m", "_step4e_desired_y_m"]
    miss = missing_cols(plot, xy_cols)
    if miss:
        unsupported(ax[3], title, miss, panel_status)
    else:
        x_all = pd.concat([numeric(plot, xy_cols[0]), numeric(plot, xy_cols[2])]).dropna()
        y_all = pd.concat([numeric(plot, xy_cols[1]), numeric(plot, xy_cols[3])]).dropna()
        x0 = float(x_all.min()) if not x_all.empty else 0.0
        y0 = float(y_all.min()) if not y_all.empty else 0.0
        ax[3].plot((numeric(plot, xy_cols[2]) - x0) * 1000.0, (numeric(plot, xy_cols[3]) - y0) * 1000.0, color="#dc2626", linewidth=1.2, label="reference")
        ax[3].plot((numeric(plot, xy_cols[0]) - x0) * 1000.0, (numeric(plot, xy_cols[1]) - y0) * 1000.0, color="#111827", linewidth=0.9, label="actual")
        ax[3].set_aspect("equal", adjustable="box")
        ax[3].set_title(title)
        ax[3].set_xlabel("X offset (mm)")
        ax[3].set_ylabel("Y offset (mm)")
        ax[3].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title, "offset is from active-window min XY for readability")

    # 5. XY error.
    title = "XY tracking error"
    miss = missing_cols(plot, ["_step4e_path_error_x_m", "_step4e_path_error_y_m"])
    if miss:
        unsupported(ax[4], title, miss, panel_status)
    else:
        ax[4].plot(plot["t_active_s"], numeric(plot, "_step4e_path_error_x_m") * 1000.0, color="#2563eb", linewidth=0.85, label="err_x")
        ax[4].plot(plot["t_active_s"], numeric(plot, "_step4e_path_error_y_m") * 1000.0, color="#f97316", linewidth=0.85, label="err_y")
        ax[4].axhline(0.0, color="#6b7280", linewidth=0.6)
        ax[4].set_title(title)
        ax[4].set_xlabel("active time (s)")
        ax[4].set_ylabel("mm")
        ax[4].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title)

    # 6. Velocity norms.
    title = "TCP and command velocity"
    cmd_lin = vector_norm(plot, ["step4e_cmd_vx_m_s", "step4e_cmd_vy_m_s", "step4e_cmd_vz_m_s"])
    actual_speed = numeric(plot, "_step4e_actual_speed_norm_m_s")
    ur_speed = vector_norm(plot, ["ur_actual_TCP_speed_0", "ur_actual_TCP_speed_1", "ur_actual_TCP_speed_2"])
    if cmd_lin.empty and actual_speed.empty and ur_speed.empty:
        unsupported(ax[5], title, ["cmd velocity or actual TCP speed"], panel_status)
    else:
        if not actual_speed.empty:
            ax[5].plot(plot["t_active_s"], actual_speed * 1000.0, color="#111827", linewidth=0.85, label="actual_speed_norm")
        if not ur_speed.empty:
            ax[5].plot(plot["t_active_s"], ur_speed * 1000.0, color="#374151", linewidth=0.75, linestyle="--", label="UR TCP speed")
        if not cmd_lin.empty:
            ax[5].plot(plot["t_active_s"], cmd_lin * 1000.0, color="#2563eb", linewidth=0.85, label="cmd linear norm")
        if limits["total_linear_m_s"] is not None:
            ax[5].axhline(limits["total_linear_m_s"] * 1000.0, color="#dc2626", linestyle="--", linewidth=0.9, label="linear limit")
        ax[5].set_title(title)
        ax[5].set_xlabel("active time (s)")
        ax[5].set_ylabel("mm/s")
        ax[5].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title)

    # 7. Command components and normal command.
    title = "Linear command components"
    if missing_cols(plot, ["step4e_cmd_vx_m_s", "step4e_cmd_vy_m_s", "step4e_cmd_vz_m_s"]):
        unsupported(ax[6], title, ["step4e_cmd_vx_m_s", "step4e_cmd_vy_m_s", "step4e_cmd_vz_m_s"], panel_status)
    else:
        for col, color in [("step4e_cmd_vx_m_s", "#2563eb"), ("step4e_cmd_vy_m_s", "#16a34a"), ("step4e_cmd_vz_m_s", "#f97316")]:
            ax[6].plot(plot["t_active_s"], numeric(plot, col) * 1000.0, linewidth=0.75, color=color, label=col.replace("step4e_cmd_", "").replace("_m_s", ""))
        n_cmd = normal_command(plot)
        if not n_cmd.empty:
            ax[6].plot(plot["t_active_s"], n_cmd * 1000.0, linewidth=0.9, color="#111827", label="normal cmd")
        if limits["normal_velocity_m_s"] is not None:
            ax[6].axhline(limits["normal_velocity_m_s"] * 1000.0, color="#dc2626", linestyle="--", linewidth=0.8, label="normal limit")
            ax[6].axhline(-limits["normal_velocity_m_s"] * 1000.0, color="#dc2626", linestyle="--", linewidth=0.8)
        ax[6].set_title(title)
        ax[6].set_xlabel("active time (s)")
        ax[6].set_ylabel("mm/s")
        ax[6].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title, "normal cmd is dot(linear cmd, control normal) when normal columns exist")

    # 8. Angular command.
    title = "Angular command and limit"
    if missing_cols(plot, ["step4e_cmd_wx_rad_s", "step4e_cmd_wy_rad_s", "step4e_cmd_wz_rad_s"]):
        unsupported(ax[7], title, ["step4e_cmd_wx_rad_s", "step4e_cmd_wy_rad_s", "step4e_cmd_wz_rad_s"], panel_status)
    else:
        for col, color in [("step4e_cmd_wx_rad_s", "#7c3aed"), ("step4e_cmd_wy_rad_s", "#0f766e"), ("step4e_cmd_wz_rad_s", "#f97316")]:
            ax[7].plot(plot["t_active_s"], numeric(plot, col), linewidth=0.65, color=color, label=col.replace("step4e_cmd_", "").replace("_rad_s", ""))
        ang = vector_norm(plot, ["step4e_cmd_wx_rad_s", "step4e_cmd_wy_rad_s", "step4e_cmd_wz_rad_s"])
        ax[7].plot(plot["t_active_s"], ang, color="#111827", linewidth=0.9, label="angular norm")
        if limits["angular_rad_s"] is not None:
            ax[7].axhline(limits["angular_rad_s"], color="#dc2626", linestyle="--", linewidth=0.9, label="angular limit")
        ax[7].set_title(title)
        ax[7].set_xlabel("active time (s)")
        ax[7].set_ylabel("rad/s")
        ax[7].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title)

    # 9. Orientation error.
    title = "Orientation / attitude error"
    orient_cols = [col for col in ["step4e_orientation_error_rad", "_step5d_contact_orientation_error_rad", "_step5d_outer_orientation_error_rad"] if col in plot.columns]
    if not orient_cols:
        unsupported(ax[8], title, ["step4e_orientation_error_rad"], panel_status)
    else:
        colors = ["#111827", "#2563eb", "#f97316"]
        for col, color in zip(orient_cols, colors):
            ax[8].plot(plot["t_active_s"], numeric(plot, col), linewidth=0.85, color=color, label=col)
        ax[8].set_title(title)
        ax[8].set_xlabel("active time (s)")
        ax[8].set_ylabel("rad")
        ax[8].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title)

    # 10. Normal vector and filter diagnostics.
    title = "Normal vector and filter"
    normal_cols = ["_step4e_control_normal_b_x", "_step4e_control_normal_b_y", "_step4e_control_normal_b_z"]
    if missing_cols(plot, normal_cols):
        unsupported(ax[9], title, normal_cols, panel_status)
    else:
        for col, color in zip(normal_cols, ["#2563eb", "#16a34a", "#f97316"]):
            ax[9].plot(plot["t_active_s"], numeric(plot, col), linewidth=0.75, color=color, label=col.rsplit("_", 1)[-1])
        if "_step4e_live_normal_angle_from_latch_rad" in plot.columns:
            ax9b = ax[9].twinx()
            ax9b.plot(plot["t_active_s"], numeric(plot, "_step4e_live_normal_angle_from_latch_rad"), color="#7c3aed", linewidth=0.75, alpha=0.75, label="angle from latch")
            ax9b.set_ylabel("rad", fontsize=8)
            ax9b.tick_params(labelsize=8)
        ax[9].set_title(title)
        ax[9].set_xlabel("active time (s)")
        ax[9].set_ylabel("unit component")
        ax[9].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title)

    # 11. Torque norm.
    title = "Torque norm"
    if "torque_norm_nm" not in plot.columns:
        unsupported(ax[10], title, ["torque_norm_nm"], panel_status)
    else:
        ax[10].plot(plot["t_active_s"], numeric(plot, "torque_norm_nm"), color="#0f766e", linewidth=0.85, label="torque_norm")
        if limits["torque_norm_nm"] is not None:
            ax[10].axhline(limits["torque_norm_nm"], color="#dc2626", linestyle="--", linewidth=0.9, label="torque limit")
        ax[10].set_title(title)
        ax[10].set_xlabel("active time (s)")
        ax[10].set_ylabel("Nm")
        ax[10].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title)

    # 12. Sensor age and connectivity.
    title = "Sensor age and RTDE connectivity"
    if "sensor_age_s" not in plot.columns:
        unsupported(ax[11], title, ["sensor_age_s"], panel_status)
    else:
        ax[11].plot(plot["t_active_s"], numeric(plot, "sensor_age_s") * 1000.0, color="#111827", linewidth=0.8, label="sensor_age")
        if "rtde_connected" in plot.columns:
            ax11b = ax[11].twinx()
            ax11b.plot(plot["t_active_s"], numeric(plot, "rtde_connected"), color="#2563eb", linewidth=0.7, alpha=0.7, label="rtde_connected")
            ax11b.set_ylabel("bool", fontsize=8)
            ax11b.tick_params(labelsize=8)
        ax[11].set_title(title)
        ax[11].set_xlabel("active time (s)")
        ax[11].set_ylabel("ms")
        ax[11].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title)

    # 13. Limit usage ratios.
    title = "Limit usage ratios"
    ratios: dict[str, pd.Series] = {}
    active_cmd_lin = vector_norm(plot, ["step4e_cmd_vx_m_s", "step4e_cmd_vy_m_s", "step4e_cmd_vz_m_s"])
    active_ang = vector_norm(plot, ["step4e_cmd_wx_rad_s", "step4e_cmd_wy_rad_s", "step4e_cmd_wz_rad_s"])
    active_ncmd = normal_command(plot).abs()
    if limits["total_linear_m_s"] and not active_cmd_lin.empty:
        ratios["linear"] = active_cmd_lin / limits["total_linear_m_s"]
    if limits["angular_rad_s"] and not active_ang.empty:
        ratios["angular"] = active_ang / limits["angular_rad_s"]
    if limits["normal_velocity_m_s"] and not active_ncmd.empty:
        ratios["normal"] = active_ncmd / limits["normal_velocity_m_s"]
    if limits["force_norm_n"] and "force_norm_n" in plot.columns:
        ratios["force"] = numeric(plot, "force_norm_n") / limits["force_norm_n"]
    if limits["torque_norm_nm"] and "torque_norm_nm" in plot.columns:
        ratios["torque"] = numeric(plot, "torque_norm_nm") / limits["torque_norm_nm"]
    if not ratios:
        unsupported(ax[12], title, ["limit values and command/force fields"], panel_status)
    else:
        colors = ["#2563eb", "#7c3aed", "#16a34a", "#f97316", "#0f766e"]
        for (name, series), color in zip(ratios.items(), colors):
            ax[12].plot(plot["t_active_s"], series, linewidth=0.8, color=color, label=name)
        ax[12].axhline(1.0, color="#dc2626", linestyle="--", linewidth=0.9, label="limit")
        ax[12].set_ylim(bottom=0.0)
        ax[12].set_title(title)
        ax[12].set_xlabel("active time (s)")
        ax[12].set_ylabel("ratio")
        ax[12].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title)

    # 14. Stage/event timeline.
    title = "Stage and event timeline"
    if "ur_output_double_register_35" not in full_plot.columns:
        unsupported(ax[13], title, ["ur_output_double_register_35"], panel_status)
    else:
        ax[13].plot(full_plot["t_full_s"], numeric(full_plot, "ur_output_double_register_35"), color="#111827", linewidth=0.8, label="stage id")
        if "stop_request" in full_plot.columns:
            ax13b = ax[13].twinx()
            ax13b.plot(full_plot["t_full_s"], numeric(full_plot, "stop_request"), color="#dc2626", linewidth=0.7, alpha=0.65, label="stop_request")
            ax13b.set_ylabel("stop", fontsize=8)
            ax13b.tick_params(labelsize=8)
        ax[13].set_title(title)
        ax[13].set_xlabel("full run time (s)")
        ax[13].set_ylabel("stage id")
        ax[13].legend(fontsize=7, loc="best")
        mark_supported(panel_status, title)

    # 15. Text summary.
    title = "Run summary"
    ax[14].axis("off")
    load = numeric(active, "_step4e_normal_load_n") if "_step4e_normal_load_n" in active.columns else pd.Series(dtype=float)
    fz = numeric(active, "fz_n_zeroed") if "fz_n_zeroed" in active.columns else pd.Series(dtype=float)
    normal_load_stats = finite_stats(load)
    raw_fz_stats = finite_stats(fz)
    high_20_s = dwell_seconds(active["t_active_s"], load > 20.0) if not load.empty else 0.0
    low_5_s = dwell_seconds(active["t_active_s"], load < 5.0) if not load.empty else 0.0
    limit_rows = limit_hit_counts(active, limits)
    filter_counts = {}
    if "_step4e_normal_filter_source" in active.columns:
        filter_counts = {str(k): int(v) for k, v in active["_step4e_normal_filter_source"].value_counts(dropna=False).head(6).items()}
    text_lines = [
        f"run: {run_dir.name}",
        f"window: {window_note}",
        f"target: {target_n if target_n is not None else 'missing'} N",
        f"stop: {bridge_summary.get('stop_reason', 'missing')}",
        f"rtde: {bridge_summary.get('rtde_output_timing', {}).get('rate_hz', 'missing')} Hz",
        f"bridge writes: {bridge_summary.get('bridge_write_timing', {}).get('rate_hz', 'missing')} Hz",
        f"parse errors: {bridge_summary.get('parse_errors', 'missing')}",
        f"rtde reconnects: {bridge_summary.get('rtde_reconnect_event_count', 'missing')}",
        f"normal load mean/p95/p99/max: {fmt_stat(normal_load_stats, 'mean')} / {fmt_stat(normal_load_stats, 'p95')} / {fmt_stat(normal_load_stats, 'p99')} / {fmt_stat(normal_load_stats, 'max')} N",
        f"raw Fz mean/min/max: {fmt_stat(raw_fz_stats, 'mean')} / {fmt_stat(raw_fz_stats, 'min')} / {fmt_stat(raw_fz_stats, 'max')} N",
        f"load <5N dwell: {low_5_s:.3f} s",
        f"load >20N dwell: {high_20_s:.3f} s",
        f"limit near-hit rows: {limit_rows}",
        f"filter source: {filter_counts or 'missing'}",
        f"stage rows: {stage_summary.get('stage25_ft_line_control_echo_rate', {}).get('rtde_rows', 'missing')}",
    ]
    ax[14].set_title(title)
    ax[14].text(0.0, 1.0, "\n".join(text_lines), ha="left", va="top", fontsize=9, family="monospace", transform=ax[14].transAxes)
    panel_status[title] = {"supported": True, "note": "text summary"}

    for axis in ax[:14]:
        style_axis(axis)

    fig.subplots_adjust(left=0.055, right=0.965, bottom=0.04, top=0.955, hspace=0.55, wspace=0.32)
    fig.savefig(png_path)
    plt.close(fig)

    payload = {
        "ok": True,
        "schema": "step5b_diagnostic_overview_v1",
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "active_window": {
            "note": window_note,
            "rows": int(active.shape[0]),
            "duration_s": float(numeric(active, "t_active_s").max()) if not active.empty else 0.0,
        },
        "target_force_n": target_n,
        "limits": limits,
        "paths": {
            "bridge_csv": str(csv_path),
            "png": str(png_path),
            "summary": str(summary_path),
        },
        "panels": panel_status,
        "stats": {
            "normal_load_n": normal_load_stats,
            "raw_fz_n": raw_fz_stats,
            "normal_load_lt_5_s": low_5_s,
            "normal_load_gt_20_s": high_20_s,
            "limit_near_hit_rows": limit_rows,
        },
        "source_summaries": {
            "bridge_stop_reason": bridge_summary.get("stop_reason"),
            "parse_errors": bridge_summary.get("parse_errors"),
            "rtde_reconnect_event_count": bridge_summary.get("rtde_reconnect_event_count"),
            "stage25_rows": stage_summary.get("stage25_ft_line_control_echo_rate", {}).get("rtde_rows") if isinstance(stage_summary.get("stage25_ft_line_control_echo_rate"), dict) else None,
        },
        "mac_transfer": {"attempted": False, "ok": None, "target": None},
    }
    summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def fmt_stat(stats_payload: dict[str, Any], key: str) -> str:
    value = stats_payload.get(key)
    return "missing" if value is None else f"{float(value):.3f}"


def limit_hit_counts(active: pd.DataFrame, limits: dict[str, float | None]) -> dict[str, int]:
    counts: dict[str, int] = {}
    t = numeric(active, "t_active_s")
    lin = vector_norm(active, ["step4e_cmd_vx_m_s", "step4e_cmd_vy_m_s", "step4e_cmd_vz_m_s"])
    ang = vector_norm(active, ["step4e_cmd_wx_rad_s", "step4e_cmd_wy_rad_s", "step4e_cmd_wz_rad_s"])
    ncmd = normal_command(active).abs()
    candidates = {
        "linear": (lin, limits.get("total_linear_m_s")),
        "angular": (ang, limits.get("angular_rad_s")),
        "normal": (ncmd, limits.get("normal_velocity_m_s")),
        "force": (numeric(active, "force_norm_n"), limits.get("force_norm_n")),
        "torque": (numeric(active, "torque_norm_nm"), limits.get("torque_norm_nm")),
    }
    for name, (series, limit) in candidates.items():
        if series.empty or not limit or limit <= 0:
            continue
        mask = series >= 0.99 * limit
        counts[f"{name}_rows_ge_99pct"] = int(mask.sum())
        counts[f"{name}_dwell_s_ge_99pct"] = round(dwell_seconds(t, mask), 6)
    return counts


def transfer_to_mac(run_dir: Path, payload: dict[str, Any], mac_target: str, timeout_s: float) -> dict[str, Any]:
    png_path = run_dir / PNG_NAME
    transfer: dict[str, Any] = {"attempted": True, "ok": False, "target": mac_target}
    if ":" not in mac_target:
        transfer["issue"] = "mac_target must be HOST:DIR"
        return transfer
    host, remote_dir = mac_target.split(":", 1)
    remote_dir = remote_dir.rstrip("/")
    remote_png = f"{remote_dir}/{run_dir.name}_diagnostic_overview.png"
    remote_summary = f"{remote_dir}/{run_dir.name}_diagnostic_overview_summary.json"
    transfer["remote_paths"] = {"png": f"{host}:{remote_png}", "summary": f"{host}:{remote_summary}"}
    if shutil.which("ssh") is None or shutil.which("scp") is None:
        transfer["issue"] = "ssh or scp not found"
        return transfer
    try:
        subprocess.run(["ssh", host, "mkdir", "-p", remote_dir], check=True, capture_output=True, text=True, timeout=timeout_s)
        subprocess.run(["scp", str(png_path), f"{host}:{remote_png}"], check=True, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.CalledProcessError as exc:
        transfer["issue"] = f"{exc.cmd}: rc={exc.returncode} stderr={exc.stderr.strip()}"
        return transfer
    except subprocess.TimeoutExpired as exc:
        transfer["issue"] = f"timeout after {exc.timeout}s: {exc.cmd}"
        return transfer
    transfer["ok"] = True
    return transfer


def transfer_summary_to_mac(run_dir: Path, transfer: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    if not transfer.get("ok") or not transfer.get("target"):
        return transfer
    mac_target = str(transfer["target"])
    if ":" not in mac_target:
        transfer["ok"] = False
        transfer["issue"] = "mac_target must be HOST:DIR"
        return transfer
    host, remote_dir = mac_target.split(":", 1)
    remote_dir = remote_dir.rstrip("/")
    remote_summary = f"{remote_dir}/{run_dir.name}_diagnostic_overview_summary.json"
    summary_path = run_dir / SUMMARY_NAME
    try:
        subprocess.run(["scp", str(summary_path), f"{host}:{remote_summary}"], check=True, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.CalledProcessError as exc:
        transfer["ok"] = False
        transfer["issue"] = f"{exc.cmd}: rc={exc.returncode} stderr={exc.stderr.strip()}"
    except subprocess.TimeoutExpired as exc:
        transfer["ok"] = False
        transfer["issue"] = f"timeout after {exc.timeout}s: {exc.cmd}"
    return transfer


def write_payload_with_transfer(run_dir: Path, payload: dict[str, Any], transfer: dict[str, Any]) -> None:
    payload["mac_transfer"] = transfer
    (run_dir / SUMMARY_NAME).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--mac-target", default=None, help=f"Optional HOST:DIR target, e.g. {DEFAULT_MAC_TARGET}")
    parser.add_argument("--transfer-timeout-s", type=float, default=60.0)
    parser.add_argument("--max-points", type=int, default=5000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    run_dir = args.run_dir.resolve()
    payload = build_figure(run_dir, max_points=max(500, int(args.max_points)))
    if not payload.get("ok"):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2
    if args.mac_target:
        transfer = transfer_to_mac(run_dir, payload, args.mac_target, timeout_s=float(args.transfer_timeout_s))
        write_payload_with_transfer(run_dir, payload, transfer)
        if transfer.get("ok"):
            transfer = transfer_summary_to_mac(run_dir, transfer, timeout_s=float(args.transfer_timeout_s))
            write_payload_with_transfer(run_dir, payload, transfer)
        if transfer.get("ok"):
            print(f"[step5b-diagnostic] sent overview to {args.mac_target}")
        else:
            print(f"[step5b-diagnostic] Mac transfer failed: {transfer.get('issue')}", file=sys.stderr)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
