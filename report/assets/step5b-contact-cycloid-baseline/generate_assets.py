#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


REPO = Path("/home/andy/ur10e_ros2_ws")
RUN = (
    REPO
    / "experiments/kunwei/closed-loop-straight-line/2026-06-04"
    / "runs/bridge_step5b_contact_cycloid_baseline_v1_20260612_082352"
)
ASSETS = REPO / "report/assets/step5b-contact-cycloid-baseline"


RTDE_COLS = [
    "write_index",
    "t_monotonic_s",
    "normal_force_n",
    "force_norm_n",
    "torque_norm_nm",
    "target_force_n",
    "step4e_progress_m",
    "step4e_force_error_n",
    "step4e_cmd_vx_m_s",
    "step4e_cmd_vy_m_s",
    "step4e_cmd_vz_m_s",
    "step4e_cmd_wx_rad_s",
    "step4e_cmd_wy_rad_s",
    "step4e_cmd_wz_rad_s",
    "step4e_cmd_valid",
    "guard_reason",
    "_step4e_normal_load_n",
    "_step4e_normal_force_error_n",
    "_step4e_line_stage_s",
    "_step4e_path_error_x_m",
    "_step4e_path_error_y_m",
    "_step4e_desired_x_m",
    "_step4e_desired_y_m",
    "_step4e_desired_vx_m_s",
    "_step4e_desired_vy_m_s",
    "_step4e_normal_filter_source",
    "_step4e_normal_follow_mode",
    "_step4e_live_normal_candidate_force_n",
    "ur_actual_TCP_pose_0",
    "ur_actual_TCP_pose_1",
    "ur_actual_TCP_pose_2",
    "ur_actual_TCP_speed_0",
    "ur_actual_TCP_speed_1",
    "ur_actual_TCP_speed_2",
    "ur_runtime_state",
    "ur_robot_mode",
    "ur_safety_mode",
    "ur_speed_scaling",
    "ur_output_double_register_24",
    "ur_output_double_register_25",
    "ur_output_double_register_26",
    "ur_output_double_register_27",
    "ur_output_double_register_28",
    "ur_output_double_register_29",
    "ur_output_double_register_30",
    "ur_output_double_register_31",
    "ur_output_double_register_32",
    "ur_output_double_register_33",
    "ur_output_double_register_34",
    "ur_output_double_register_35",
    "ur_output_double_register_36",
    "ur_output_double_register_37",
    "ur_output_double_register_38",
    "ur_output_double_register_39",
    "ur_output_double_register_40",
    "ur_output_double_register_41",
    "ur_output_double_register_42",
    "ur_output_double_register_43",
    "ur_output_double_register_44",
    "ur_output_double_register_45",
    "ur_output_double_register_46",
    "ur_output_double_register_47",
]


STAGE_LABELS = {
    20.0: "start/wait",
    22.0: "XY entry",
    23.0: "software zero",
    24.0: "first far search",
    24.2: "first near search",
    24.3: "second far search",
    24.4: "second near search",
    25.05: "normal latch",
    25.1: "detach/lift",
    25.2: "orientation correction",
    25.3: "force reacquire",
    25.0: "cycloid contact",
    26.0: "unload",
    27.0: "retract/home",
    29.0: "final",
}


def stats(series: pd.Series | np.ndarray) -> dict[str, float | int | None]:
    s = pd.Series(series).dropna().to_numpy(dtype=float)
    s = s[np.isfinite(s)]
    if len(s) == 0:
        return {
            "samples": 0,
            "mean": None,
            "median": None,
            "std": None,
            "min": None,
            "max": None,
            "p95_abs": None,
            "p99_abs": None,
        }
    return {
        "samples": int(len(s)),
        "mean": float(np.mean(s)),
        "median": float(np.median(s)),
        "std": float(np.std(s, ddof=1)) if len(s) > 1 else 0.0,
        "min": float(np.min(s)),
        "max": float(np.max(s)),
        "p95_abs": float(np.percentile(np.abs(s), 95)),
        "p99_abs": float(np.percentile(np.abs(s), 99)),
    }


def duration_s(group: pd.DataFrame) -> float:
    if len(group) < 2:
        return 0.0
    return float(group["t_monotonic_s"].iloc[-1] - group["t_monotonic_s"].iloc[0])


def final_registers(df: pd.DataFrame) -> dict[str, float]:
    final_row = df.iloc[-1]
    registers = {}
    for idx in range(24, 48):
        key = f"ur_output_double_register_{idx}"
        value = final_row[key]
        if pd.notna(value):
            registers[str(idx)] = float(value)
    return registers


def axis_limit(ax: plt.Axes, values_x: np.ndarray, values_y: np.ndarray) -> None:
    xmin, xmax = float(np.min(values_x)), float(np.max(values_x))
    ymin, ymax = float(np.min(values_y)), float(np.max(values_y))
    span = max(xmax - xmin, ymax - ymin, 1.0)
    cx = 0.5 * (xmin + xmax)
    cy = 0.5 * (ymin + ymax)
    pad = span * 0.08
    ax.set_xlim(cx - 0.5 * span - pad, cx + 0.5 * span + pad)
    ax.set_ylim(cy - 0.5 * span - pad, cy + 0.5 * span + pad)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    summary = json.loads((RUN / "summary.json").read_text())
    metadata = json.loads((RUN / "metadata.json").read_text())
    frequency = json.loads((RUN / "stage_frequency_summary.json").read_text())
    quiet = json.loads((RUN / "kunwei_quiet_stream.json").read_text())

    df = pd.read_csv(RUN / "bridge_rtde_500hz.csv", usecols=RTDE_COLS)
    df["t_rel_s"] = df["t_monotonic_s"] - df["t_monotonic_s"].iloc[0]
    df["stage"] = df["ur_output_double_register_35"].round(2)
    df["path_time_s"] = df["ur_output_double_register_31"]
    df["tcp_z_mm"] = df["ur_actual_TCP_pose_2"] * 1000.0

    stage_rows = []
    for stage, group in df.dropna(subset=["stage"]).groupby("stage", sort=True):
        dur = duration_s(group)
        stage_rows.append(
            {
                "stage": float(stage),
                "meaning": STAGE_LABELS.get(float(stage), ""),
                "rows": int(len(group)),
                "duration_s": dur,
                "row_rate_hz": float(len(group) / dur) if dur > 0 else None,
                "path_time_end_s": float(group["path_time_s"].iloc[-1]),
                "tcp_z_min_mm": float(group["tcp_z_mm"].min()),
                "tcp_z_max_mm": float(group["tcp_z_mm"].max()),
                "normal_force_min_n": float(group["normal_force_n"].min()),
                "normal_force_mean_n": float(group["normal_force_n"].mean()),
                "normal_force_max_n": float(group["normal_force_n"].max()),
                "force_norm_max_n": float(group["force_norm_n"].max()),
                "torque_norm_max_nm": float(group["torque_norm_nm"].max()),
            }
        )

    stage25 = df[df["stage"] == 25.0].copy()
    if stage25.empty:
        raise RuntimeError("stage 25.0 contact rows not found")
    stage25["stage25_t_s"] = stage25["t_monotonic_s"] - stage25["t_monotonic_s"].iloc[0]

    target_force_signed_n = -float(metadata["args"]["target_force_n"])
    signed_error = stage25["normal_force_n"] - target_force_signed_n

    actual_xy = stage25[["ur_actual_TCP_pose_0", "ur_actual_TCP_pose_1"]].to_numpy(dtype=float)
    desired_xy = stage25[["_step4e_desired_x_m", "_step4e_desired_y_m"]].to_numpy(dtype=float)
    error_xy = actual_xy - desired_xy
    xy_error = np.linalg.norm(error_xy, axis=1)
    start_xy = desired_xy[0]
    actual_rel_mm = (actual_xy - start_xy) * 1000.0
    desired_rel_mm = (desired_xy - start_xy) * 1000.0

    filter_counts = {
        str(key): int(value)
        for key, value in stage25["_step4e_normal_filter_source"].fillna("").value_counts().to_dict().items()
    }
    guard_nonempty = int((df["guard_reason"].fillna("").astype(str).str.len() > 0).sum())
    final_output_registers = final_registers(df)
    stage25_duration_s = duration_s(stage25)

    path_metrics = {
        "x_error_mm": stats(error_xy[:, 0] * 1000.0),
        "y_error_mm": stats(error_xy[:, 1] * 1000.0),
        "xy_error_mm": {
            "samples": int(len(xy_error)),
            "mean": float(np.mean(xy_error) * 1000.0),
            "median": float(np.median(xy_error) * 1000.0),
            "p95": float(np.percentile(xy_error, 95) * 1000.0),
            "p99": float(np.percentile(xy_error, 99) * 1000.0),
            "max": float(np.max(xy_error) * 1000.0),
        },
    }
    stage25_metrics = {
        "rows": int(len(stage25)),
        "duration_s": stage25_duration_s,
        "path_time_start_s": float(stage25["path_time_s"].iloc[0]),
        "path_time_final_s": float(stage25["path_time_s"].iloc[-1]),
        "path_time_max_s": float(stage25["path_time_s"].max()),
        "normal_force_n": stats(stage25["normal_force_n"]),
        "signed_error_vs_minus_5_n": {
            "samples": int(len(signed_error)),
            "mean_n": float(signed_error.mean()),
            "mae_n": float(signed_error.abs().mean()),
            "p95_abs_n": float(np.percentile(np.abs(signed_error), 95)),
            "p99_abs_n": float(np.percentile(np.abs(signed_error), 99)),
            "min_n": float(signed_error.min()),
            "max_n": float(signed_error.max()),
        },
        "force_norm_n": stats(stage25["force_norm_n"]),
        "torque_norm_nm": stats(stage25["torque_norm_nm"]),
        "cmd_vx_mm_s": stats(stage25["step4e_cmd_vx_m_s"] * 1000.0),
        "cmd_vy_mm_s": stats(stage25["step4e_cmd_vy_m_s"] * 1000.0),
        "cmd_vz_mm_s": stats(stage25["step4e_cmd_vz_m_s"] * 1000.0),
        "normal_filter_source_counts": filter_counts,
    }

    metrics = {
        "run_id": RUN.name,
        "generated_from": {
            "summary_json": str(RUN / "summary.json"),
            "metadata_json": str(RUN / "metadata.json"),
            "stage_frequency_summary_json": str(RUN / "stage_frequency_summary.json"),
            "bridge_csv": str(RUN / "bridge_rtde_500hz.csv"),
            "sensor_csv": str(RUN / "kunwei_sensor_1khz.csv"),
            "quiet_stream_json": str(RUN / "kunwei_quiet_stream.json"),
        },
        "protocol": {
            "tp_program": "/programs/andyl/kunwei/step5/step5b_contact_cycloid_baseline_v1.urp",
            "local_triplet": "experiments/kunwei/closed-loop-straight-line/2026-06-04/programs/step5/step5b_contact_cycloid_baseline_v1.{script,txt,urp}",
            "program_stamp": "2026-06-12T0821HKT_STEP5B_CONTACT_CYCLOID_BASELINE_V1",
            "bridge_args": metadata["args"],
            "dashboard_preflight": metadata["dashboard_preflight"],
            "guard_contract": metadata["guard_contract"],
            "safety_boundary": metadata["safety_boundary"],
            "step5_path": metadata["step4e_path"],
            "baseline": {
                "ready": summary["baseline_ready"],
                "samples": summary["baseline_samples"],
                "si_offsets": summary["baseline_si_offsets"],
                "zero_events": summary["zero_events"],
            },
            "kunwei_quiet_stop": quiet,
        },
        "success_checks": {
            "final_stage_output_register_35": final_output_registers.get("35"),
            "final_stop_reason_output_register_30": final_output_registers.get("30"),
            "final_path_time_output_register_31_s": final_output_registers.get("31"),
            "target_path_time_s": float(metadata["step4e_path"]["curve_duration_s"]),
            "stage25_duration_s": stage25_duration_s,
            "guard_reason_nonempty_rows": guard_nonempty,
            "summary_stop_reason": summary["stop_reason"],
            "force_norm_max_n": summary["force_norm_stats_n"]["max"],
            "force_norm_guard_n": metadata["guard_contract"]["max_force_norm_n"],
            "normal_force_min_n": summary["normal_force_stats_n"]["min"],
            "normal_force_guard_n": metadata["guard_contract"]["max_normal_force_n"],
            "torque_norm_max_nm": summary["torque_norm_stats_nm"]["max"],
            "torque_norm_guard_nm": metadata["guard_contract"]["max_torque_norm_nm"],
            "kunwei_quiet_stop_ok": bool(quiet["ok"] and quiet["probe"]["quiet"]),
            "parse_errors": summary["parse_errors"],
            "dropped_sync_bytes": summary["dropped_sync_bytes"],
            "rtde_reconnect_event_count": summary["rtde_reconnect_event_count"],
        },
        "summary": summary,
        "frequency": frequency,
        "stage_summary": stage_rows,
        "stage25_contact": stage25_metrics,
        "path_tracking": path_metrics,
        "final_output_registers": final_output_registers,
    }
    (ASSETS / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")

    plt.rcParams.update(
        {
            "figure.dpi": 140,
            "savefig.dpi": 180,
            "font.size": 9,
            "axes.grid": True,
            "grid.alpha": 0.28,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )

    fig, axes = plt.subplots(4, 1, figsize=(10, 8), sharex=True)
    axes[0].plot(df["t_rel_s"], df["stage"], lw=1.0, color="#2f4858")
    axes[0].set_ylabel("Stage")
    axes[0].set_title("Step5b contact cycloid full-run overview")
    axes[1].plot(df["t_rel_s"], df["normal_force_n"], lw=0.75, color="#c44536")
    axes[1].axhline(target_force_signed_n, color="#333333", lw=0.8, ls="--", label="-5 N target")
    axes[1].set_ylabel("Normal force (N)")
    axes[1].legend(loc="lower right")
    axes[2].plot(df["t_rel_s"], df["path_time_s"], lw=1.0, color="#227c9d")
    axes[2].axhline(metadata["step4e_path"]["curve_duration_s"], color="#333333", lw=0.8, ls="--")
    axes[2].set_ylabel("Path time (s)")
    axes[3].plot(df["t_rel_s"], df["tcp_z_mm"], lw=0.8, color="#5f6f52")
    axes[3].set_ylabel("TCP z (mm)")
    axes[3].set_xlabel("Time since bridge start (s)")
    fig.tight_layout()
    fig.savefig(ASSETS / "full-run-stage-force-path-time-z.png")
    plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    axes[0].plot(stage25["stage25_t_s"], stage25["normal_force_n"], lw=0.75, color="#c44536", label="normal force")
    axes[0].axhline(target_force_signed_n, color="#333333", lw=0.9, ls="--", label="-5 N target")
    axes[0].set_ylabel("Normal force (N)")
    axes[0].set_title("Stage25 cycloid contact force and command")
    axes[0].legend(loc="lower right")
    axes[1].plot(stage25["stage25_t_s"], stage25["step4e_cmd_vx_m_s"] * 1000.0, lw=0.8, label="cmd vx")
    axes[1].plot(stage25["stage25_t_s"], stage25["step4e_cmd_vy_m_s"] * 1000.0, lw=0.8, label="cmd vy")
    axes[1].plot(stage25["stage25_t_s"], stage25["step4e_cmd_vz_m_s"] * 1000.0, lw=0.8, label="cmd vz")
    axes[1].set_ylabel("Command (mm/s)")
    axes[1].set_xlabel("Stage25 time (s)")
    axes[1].legend(loc="upper right", ncol=3)
    fig.tight_layout()
    fig.savefig(ASSETS / "stage25-force-command.png")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.2, 7.0))
    ax.plot(desired_rel_mm[:, 0], desired_rel_mm[:, 1], lw=1.6, color="#222222", label="reference")
    ax.plot(actual_rel_mm[:, 0], actual_rel_mm[:, 1], lw=1.0, color="#227c9d", alpha=0.95, label="actual TCP")
    ax.scatter(
        [desired_rel_mm[0, 0], desired_rel_mm[-1, 0]],
        [desired_rel_mm[0, 1], desired_rel_mm[-1, 1]],
        s=25,
        color="#c44536",
        zorder=4,
        label="start/end",
    )
    axis_limit(ax, np.concatenate([desired_rel_mm[:, 0], actual_rel_mm[:, 0]]), np.concatenate([desired_rel_mm[:, 1], actual_rel_mm[:, 1]]))
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("X from reference start (mm)")
    ax.set_ylabel("Y from reference start (mm)")
    ax.set_title("Stage25 cycloid XY actual vs reference path")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(ASSETS / "xy-actual-vs-reference.png")
    plt.close(fig)

    t25 = stage25["stage25_t_s"].to_numpy(dtype=float)
    fig, axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True)
    axes[0].plot(t25, error_xy[:, 0] * 1000.0, lw=0.8, color="#227c9d")
    axes[0].axhline(0, color="#222222", lw=0.7)
    axes[0].set_ylabel("X err (mm)")
    axes[0].set_title("Stage25 cycloid tracking errors")
    axes[1].plot(t25, error_xy[:, 1] * 1000.0, lw=0.8, color="#5f6f52")
    axes[1].axhline(0, color="#222222", lw=0.7)
    axes[1].set_ylabel("Y err (mm)")
    axes[2].plot(t25, xy_error * 1000.0, lw=0.8, color="#c44536")
    axes[2].set_ylabel("XY norm (mm)")
    axes[2].set_xlabel("Stage25 time (s)")
    fig.tight_layout()
    fig.savefig(ASSETS / "tracking-errors.png")
    plt.close(fig)

    source_order = sorted(filter_counts)
    source_values = [filter_counts[key] for key in source_order]
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    ax.bar(source_order, source_values, color=["#227c9d", "#c44536", "#5f6f52"][: len(source_order)])
    ax.set_ylabel("Rows")
    ax.set_title("Stage25 normal filter source")
    ax.tick_params(axis="x", rotation=15)
    for idx, value in enumerate(source_values):
        ax.text(idx, value, str(value), ha="center", va="bottom", fontsize=8)
    fig.tight_layout()
    fig.savefig(ASSETS / "normal-filter-source-counts.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
