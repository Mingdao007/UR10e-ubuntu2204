#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


REPO = Path("/home/andy/ur10e_ros2_ws")
RUN = REPO / "experiments/kunwei/closed-loop-straight-line/2026-06-04/runs/bridge_step4e_seed_normal_loop_v31_20260612_050155"
ASSETS = REPO / "report/assets/step4e-v31-outer-loop-reproduction-success"


RTDE_COLS = [
    "write_index",
    "t_monotonic_s",
    "normal_force_n",
    "force_norm_n",
    "torque_norm_nm",
    "step4e_progress_m",
    "step4e_force_error_n",
    "step4e_orientation_error_rad",
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
    25.0: "line control",
    26.0: "unload",
    27.0: "retract/home",
    29.0: "final",
}


def stats(series: pd.Series) -> dict[str, float]:
    s = series.dropna()
    if s.empty:
        return {"mean": None, "median": None, "std": None, "min": None, "max": None, "p95_abs": None, "p99_abs": None}
    arr = s.to_numpy(dtype=float)
    return {
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "std": float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "p95_abs": float(np.percentile(np.abs(arr), 95)),
        "p99_abs": float(np.percentile(np.abs(arr), 99)),
    }


def duration_s(group: pd.DataFrame) -> float:
    if len(group) < 2:
        return 0.0
    return float(group["t_monotonic_s"].iloc[-1] - group["t_monotonic_s"].iloc[0])


def mm(value_m: float | None) -> float | None:
    if value_m is None:
        return None
    return float(value_m * 1000.0)


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    summary = json.loads((RUN / "summary.json").read_text())
    metadata = json.loads((RUN / "metadata.json").read_text())
    freq = json.loads((RUN / "stage_frequency_summary.json").read_text())
    quiet = json.loads((RUN / "kunwei_quiet_stream.json").read_text())

    df = pd.read_csv(RUN / "bridge_rtde_500hz.csv", usecols=RTDE_COLS)
    df["t_rel_s"] = df["t_monotonic_s"] - df["t_monotonic_s"].iloc[0]
    df["stage"] = df["ur_output_double_register_35"]
    df["stop_reason_out"] = df["ur_output_double_register_30"]
    df["progress_out_m"] = df["ur_output_double_register_31"]
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
                "tcp_z_min_mm": float(group["tcp_z_mm"].min()),
                "tcp_z_max_mm": float(group["tcp_z_mm"].max()),
                "normal_force_min_n": float(group["normal_force_n"].min()),
                "normal_force_mean_n": float(group["normal_force_n"].mean()),
                "normal_force_max_n": float(group["normal_force_n"].max()),
                "force_norm_max_n": float(group["force_norm_n"].max()),
                "torque_norm_max_nm": float(group["torque_norm_nm"].max()),
                "progress_end_m": float(group["progress_out_m"].iloc[-1]),
            }
        )

    stage25 = df[df["stage"] == 25.0].copy()
    if stage25.empty:
        raise RuntimeError("stage 25.0 line-control rows not found")
    stage25["stage25_t_s"] = stage25["t_monotonic_s"] - stage25["t_monotonic_s"].iloc[0]
    target_force_signed_n = -float(metadata["args"]["target_force_n"])
    force_error = stage25["normal_force_n"] - target_force_signed_n

    start_xy = np.array(metadata["step4e_path"]["start_xy_m"], dtype=float)
    end_xy = np.array(metadata["step4e_path"]["end_xy_m"], dtype=float)
    line_unit = np.array(metadata["step4e_path"]["line_unit_xy"], dtype=float)
    cross_unit = np.array([-line_unit[1], line_unit[0]], dtype=float)
    progress = stage25["progress_out_m"].to_numpy(dtype=float)
    actual_xy = stage25[["ur_actual_TCP_pose_0", "ur_actual_TCP_pose_1"]].to_numpy(dtype=float)
    ref_xy = start_xy + progress[:, None] * line_unit
    error_xy = actual_xy - ref_xy
    along_error = error_xy @ line_unit
    cross_error = error_xy @ cross_unit
    xy_error = np.linalg.norm(error_xy, axis=1)

    stage25_metrics = {
        "rows": int(len(stage25)),
        "duration_s": duration_s(stage25),
        "progress_min_m": float(stage25["progress_out_m"].min()),
        "progress_max_m": float(stage25["progress_out_m"].max()),
        "progress_final_m": float(stage25["progress_out_m"].iloc[-1]),
        "line_length_m": float(metadata["step4e_path"]["line_length_m"]),
        "normal_force_n": stats(stage25["normal_force_n"]),
        "force_error_vs_minus_5_n": {
            "signed_mean_n": float(force_error.mean()),
            "mae_n": float(force_error.abs().mean()),
            "p95_abs_n": float(np.percentile(np.abs(force_error.to_numpy(dtype=float)), 95)),
            "p99_abs_n": float(np.percentile(np.abs(force_error.to_numpy(dtype=float)), 99)),
            "min_n": float(force_error.min()),
            "max_n": float(force_error.max()),
        },
        "force_norm_n": stats(stage25["force_norm_n"]),
        "torque_norm_nm": stats(stage25["torque_norm_nm"]),
        "cmd_vx_mm_s": stats(stage25["step4e_cmd_vx_m_s"] * 1000.0),
        "cmd_vy_mm_s": stats(stage25["step4e_cmd_vy_m_s"] * 1000.0),
        "cmd_vz_mm_s": stats(stage25["step4e_cmd_vz_m_s"] * 1000.0),
    }

    path_metrics = {
        "x_error_mean_mm": float(np.mean(error_xy[:, 0]) * 1000.0),
        "x_error_p95_abs_mm": float(np.percentile(np.abs(error_xy[:, 0]), 95) * 1000.0),
        "x_error_p99_abs_mm": float(np.percentile(np.abs(error_xy[:, 0]), 99) * 1000.0),
        "x_error_max_abs_mm": float(np.max(np.abs(error_xy[:, 0])) * 1000.0),
        "y_error_mean_mm": float(np.mean(error_xy[:, 1]) * 1000.0),
        "y_error_p95_abs_mm": float(np.percentile(np.abs(error_xy[:, 1]), 95) * 1000.0),
        "y_error_p99_abs_mm": float(np.percentile(np.abs(error_xy[:, 1]), 99) * 1000.0),
        "y_error_max_abs_mm": float(np.max(np.abs(error_xy[:, 1])) * 1000.0),
        "xy_error_mean_mm": float(np.mean(xy_error) * 1000.0),
        "xy_error_p95_mm": float(np.percentile(xy_error, 95) * 1000.0),
        "xy_error_p99_mm": float(np.percentile(xy_error, 99) * 1000.0),
        "xy_error_max_mm": float(np.max(xy_error) * 1000.0),
        "along_error_signed_mean_mm": float(np.mean(along_error) * 1000.0),
        "along_error_abs_mean_mm": float(np.mean(np.abs(along_error)) * 1000.0),
        "along_error_p95_abs_mm": float(np.percentile(np.abs(along_error), 95) * 1000.0),
        "along_error_p99_abs_mm": float(np.percentile(np.abs(along_error), 99) * 1000.0),
        "cross_track_signed_mean_mm": float(np.mean(cross_error) * 1000.0),
        "cross_track_abs_mean_mm": float(np.mean(np.abs(cross_error)) * 1000.0),
        "cross_track_p95_abs_mm": float(np.percentile(np.abs(cross_error), 95) * 1000.0),
        "cross_track_p99_abs_mm": float(np.percentile(np.abs(cross_error), 99) * 1000.0),
        "cross_track_max_abs_mm": float(np.max(np.abs(cross_error)) * 1000.0),
    }

    final_row = df.iloc[-1]
    final_output_registers = {
        str(i): float(final_row[f"ur_output_double_register_{i}"])
        for i in range(24, 48)
        if pd.notna(final_row[f"ur_output_double_register_{i}"])
    }
    guard_nonempty = int((df["guard_reason"].fillna("").astype(str).str.len() > 0).sum())
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
            "tp_program": "/programs/andyl/kunwei/step4/step4e_seed_normal_loop_v31.urp",
            "program_stamp": "2026-06-12T0459HKT_STEP4E_SEED_NORMAL_LOOP_V31",
            "bridge_args": metadata["args"],
            "dashboard_preflight": metadata["dashboard_preflight"],
            "guard_contract": metadata["guard_contract"],
            "safety_boundary": metadata["safety_boundary"],
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
            "final_progress_output_register_31_m": final_output_registers.get("31"),
            "line_length_m": float(metadata["step4e_path"]["line_length_m"]),
            "stage25_duration_s": stage25_metrics["duration_s"],
            "guard_reason_nonempty_rows": guard_nonempty,
            "summary_stop_reason": summary["stop_reason"],
            "force_norm_max_n": summary["force_norm_stats_n"]["max"],
            "force_norm_guard_n": metadata["guard_contract"]["max_force_norm_n"],
            "torque_norm_max_nm": summary["torque_norm_stats_nm"]["max"],
            "torque_norm_guard_nm": metadata["guard_contract"]["max_torque_norm_nm"],
            "dashboard_program_state": metadata["dashboard_preflight"].get("programState"),
            "dashboard_safety_mode": metadata["dashboard_preflight"].get("safetymode"),
            "kunwei_quiet_stop_ok": quiet["ok"] and quiet["probe"]["quiet"],
        },
        "summary": summary,
        "frequency": freq,
        "stage_summary": stage_rows,
        "line_control": stage25_metrics,
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
    axes[0].set_title("Step4e v31 full-run overview")
    axes[1].plot(df["t_rel_s"], df["normal_force_n"], lw=0.8, color="#c44536")
    axes[1].axhline(target_force_signed_n, color="#333333", lw=0.8, ls="--", label="-5 N target")
    axes[1].set_ylabel("Normal force (N)")
    axes[1].legend(loc="lower right")
    axes[2].plot(df["t_rel_s"], df["progress_out_m"] * 1000.0, lw=1.0, color="#227c9d")
    axes[2].axhline(metadata["step4e_path"]["line_length_m"] * 1000.0, color="#333333", lw=0.8, ls="--")
    axes[2].set_ylabel("Progress (mm)")
    axes[3].plot(df["t_rel_s"], df["tcp_z_mm"], lw=0.8, color="#5f6f52")
    axes[3].set_ylabel("TCP z (mm)")
    axes[3].set_xlabel("Time since bridge start (s)")
    fig.tight_layout()
    fig.savefig(ASSETS / "full-run-stage-force-progress-z.png")
    plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    axes[0].plot(stage25["stage25_t_s"], stage25["normal_force_n"], lw=0.75, color="#c44536", label="normal force")
    axes[0].axhline(target_force_signed_n, color="#333333", lw=0.9, ls="--", label="-5 N target")
    axes[0].set_ylabel("Normal force (N)")
    axes[0].set_title("Stage25 line-control force and command")
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

    actual_rel_mm = (actual_xy - start_xy) * 1000.0
    ref_rel_mm = (ref_xy - start_xy) * 1000.0
    end_rel_mm = (end_xy - start_xy) * 1000.0
    fig, ax = plt.subplots(figsize=(7.2, 7.0))
    ax.plot(ref_rel_mm[:, 0], ref_rel_mm[:, 1], lw=1.6, color="#222222", label="reference")
    ax.plot(actual_rel_mm[:, 0], actual_rel_mm[:, 1], lw=1.0, color="#227c9d", alpha=0.95, label="actual TCP")
    ax.scatter([0.0, end_rel_mm[0]], [0.0, end_rel_mm[1]], s=25, color="#c44536", zorder=4, label="start/end")
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("X from line start (mm)")
    ax.set_ylabel("Y from line start (mm)")
    ax.set_title("Stage25 XY actual vs reference path")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(ASSETS / "xy-actual-vs-reference.png")
    plt.close(fig)

    fig, axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True)
    t25 = stage25["stage25_t_s"].to_numpy(dtype=float)
    axes[0].plot(t25, error_xy[:, 0] * 1000.0, lw=0.8, color="#227c9d")
    axes[0].axhline(0, color="#222222", lw=0.7)
    axes[0].set_ylabel("X err (mm)")
    axes[0].set_title("Stage25 tracking errors")
    axes[1].plot(t25, error_xy[:, 1] * 1000.0, lw=0.8, color="#5f6f52")
    axes[1].axhline(0, color="#222222", lw=0.7)
    axes[1].set_ylabel("Y err (mm)")
    axes[2].plot(t25, cross_error * 1000.0, lw=0.8, color="#c44536", label="cross-track")
    axes[2].axhline(0, color="#222222", lw=0.7)
    axes[2].set_ylabel("Cross-track (mm)")
    axes[2].set_xlabel("Stage25 time (s)")
    fig.tight_layout()
    fig.savefig(ASSETS / "tracking-errors.png")
    plt.close(fig)


if __name__ == "__main__":
    main()
