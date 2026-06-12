#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


REPO = Path("/home/andy/ur10e_ros2_ws")
RUN_V1 = (
    REPO
    / "experiments/kunwei/closed-loop-straight-line/2026-06-04"
    / "runs/bridge_step6b_contact_eight_baseline_v1_20260612_223047"
)
RUN_V2 = (
    REPO
    / "experiments/kunwei/closed-loop-straight-line/2026-06-04"
    / "runs/bridge_step6b_contact_eight_baseline_v2_20260612_225841"
)
RUN = RUN_V1
ASSETS = REPO / "report/assets/step6b-contact-eight-baseline"


RTDE_COLS = [
    "write_index",
    "t_monotonic_s",
    "normal_force_n",
    "fz_n_zeroed",
    "force_norm_n",
    "torque_norm_nm",
    "target_force_n",
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
    "_step4e_path_error_x_m",
    "_step4e_path_error_y_m",
    "_step4e_desired_x_m",
    "_step4e_desired_y_m",
    "_step4e_desired_vx_m_s",
    "_step4e_desired_vy_m_s",
    "_step4e_path_time_s",
    "_step4e_actual_speed_norm_m_s",
    "_step4e_normal_filter_source",
    "_step4e_normal_follow_mode",
    "_step4e_normal_load_n",
    "_step4e_normal_force_error_n",
    "_step4e_normal_acquired",
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
    25.05: "normal latch",
    25.1: "detach/lift",
    25.2: "orientation correction",
    25.3: "force reacquire",
    25.0: "8-shaped contact",
    26.0: "unload",
    27.0: "retract/home",
    29.0: "final",
}


def stats(values: pd.Series | np.ndarray) -> dict[str, float | int | None]:
    arr = pd.Series(values).dropna().to_numpy(dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return {
            "samples": 0,
            "mean": None,
            "median": None,
            "std": None,
            "min": None,
            "max": None,
            "p95_abs": None,
            "p99_abs": None,
            "max_abs": None,
        }
    return {
        "samples": int(len(arr)),
        "mean": float(np.mean(arr)),
        "median": float(np.median(arr)),
        "std": float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "p95_abs": float(np.percentile(np.abs(arr), 95)),
        "p99_abs": float(np.percentile(np.abs(arr), 99)),
        "max_abs": float(np.max(np.abs(arr))),
    }


def duration_s(group: pd.DataFrame) -> float:
    if len(group) < 2:
        return 0.0
    return float(group["t_monotonic_s"].iloc[-1] - group["t_monotonic_s"].iloc[0])


def final_registers(df: pd.DataFrame) -> dict[str, float]:
    row = df.iloc[-1]
    registers: dict[str, float] = {}
    for idx in range(24, 48):
        key = f"ur_output_double_register_{idx}"
        value = row[key]
        if pd.notna(value):
            registers[str(idx)] = float(value)
    return registers


def set_equal_xy_limits(ax: plt.Axes, x: np.ndarray, y: np.ndarray) -> None:
    xmin, xmax = float(np.min(x)), float(np.max(x))
    ymin, ymax = float(np.min(y)), float(np.max(y))
    span = max(xmax - xmin, ymax - ymin, 1.0)
    cx = 0.5 * (xmin + xmax)
    cy = 0.5 * (ymin + ymax)
    pad = 0.08 * span
    ax.set_xlim(cx - 0.5 * span - pad, cx + 0.5 * span + pad)
    ax.set_ylim(cy - 0.5 * span - pad, cy + 0.5 * span + pad)


def signed_target_force_n(metadata: dict) -> float:
    return -float(metadata["args"]["target_force_n"])


def fz_constant_force_metrics(stage25: pd.DataFrame, metadata: dict) -> dict:
    target_n = signed_target_force_n(metadata)
    fz = stage25["fz_n_zeroed"]
    fz_error = fz - target_n
    return {
        "target_fz_n": target_n,
        "fz_n": stats(fz),
        "fz_error_n": {
            "samples": int(len(fz_error)),
            "signed_mean_n": float(fz_error.mean()),
            "mae_n": float(fz_error.abs().mean()),
            "p95_abs_n": float(np.percentile(np.abs(fz_error), 95)),
            "p99_abs_n": float(np.percentile(np.abs(fz_error), 99)),
            "min_n": float(fz_error.min()),
            "max_n": float(fz_error.max()),
        },
        "projected_normal_load_n": stats(stage25["_step4e_normal_load_n"]),
        "projected_load_error_vs_5_n": {
            "signed_mean_n": float(stage25["_step4e_normal_force_error_n"].mean()),
            "mae_n": float(stage25["_step4e_normal_force_error_n"].abs().mean()),
            "p95_abs_n": float(np.percentile(np.abs(stage25["_step4e_normal_force_error_n"]), 95)),
        },
    }


def attitude_compliance_metrics(stage25: pd.DataFrame) -> dict:
    angular_cmd = np.linalg.norm(
        stage25[["step4e_cmd_wx_rad_s", "step4e_cmd_wy_rad_s", "step4e_cmd_wz_rad_s"]].to_numpy(dtype=float),
        axis=1,
    )
    return {
        "orientation_error_deg": stats(np.degrees(stage25["step4e_orientation_error_rad"])),
        "angular_command_deg_s": stats(np.degrees(angular_cmd)),
    }


def run_payload(run: Path) -> dict:
    summary = json.loads((run / "summary.json").read_text())
    metadata = json.loads((run / "metadata.json").read_text())
    frequency = json.loads((run / "stage_frequency_summary.json").read_text())
    quiet = json.loads((run / "kunwei_quiet_stream.json").read_text())
    df = pd.read_csv(run / "bridge_rtde_500hz.csv", usecols=RTDE_COLS)
    df["t_rel_s"] = df["t_monotonic_s"] - df["t_monotonic_s"].iloc[0]
    df["stage"] = df["ur_output_double_register_35"].round(2)
    df["path_time_s"] = df["_step4e_path_time_s"]
    df["tcp_z_mm"] = df["ur_actual_TCP_pose_2"] * 1000.0
    stage25 = df[df["stage"] == 25.0].copy()
    if stage25.empty:
        raise RuntimeError(f"stage 25.0 contact rows not found: {run}")
    stage25["stage25_t_s"] = stage25["t_monotonic_s"] - stage25["t_monotonic_s"].iloc[0]
    actual_xy = stage25[["ur_actual_TCP_pose_0", "ur_actual_TCP_pose_1"]].to_numpy(dtype=float)
    desired_xy = stage25[["_step4e_desired_x_m", "_step4e_desired_y_m"]].to_numpy(dtype=float)
    error_xy = actual_xy - desired_xy
    xy_error = np.linalg.norm(error_xy, axis=1)
    start_xy = desired_xy[0]
    actual_rel_mm = (actual_xy - start_xy) * 1000.0
    desired_rel_mm = (desired_xy - start_xy) * 1000.0
    cmd_xy = np.linalg.norm(
        stage25[["step4e_cmd_vx_m_s", "step4e_cmd_vy_m_s"]].to_numpy(dtype=float),
        axis=1,
    )
    ref_speed = np.linalg.norm(
        stage25[["_step4e_desired_vx_m_s", "_step4e_desired_vy_m_s"]].to_numpy(dtype=float),
        axis=1,
    )
    final_output_registers = final_registers(df)
    path_metrics = {
        "x_error_mm": stats(error_xy[:, 0] * 1000.0),
        "y_error_mm": stats(error_xy[:, 1] * 1000.0),
        "xy_error_mm": {
            "samples": int(len(xy_error)),
            "mean": float(np.mean(xy_error) * 1000.0),
            "median": float(np.median(xy_error) * 1000.0),
            "std": float(np.std(xy_error * 1000.0, ddof=1)),
            "min": float(np.min(xy_error) * 1000.0),
            "p95": float(np.percentile(xy_error, 95) * 1000.0),
            "p99": float(np.percentile(xy_error, 99) * 1000.0),
            "max": float(np.max(xy_error) * 1000.0),
        },
        "span_mm": {
            "desired_x": float((stage25["_step4e_desired_x_m"].max() - stage25["_step4e_desired_x_m"].min()) * 1000.0),
            "desired_y": float((stage25["_step4e_desired_y_m"].max() - stage25["_step4e_desired_y_m"].min()) * 1000.0),
            "actual_x": float((stage25["ur_actual_TCP_pose_0"].max() - stage25["ur_actual_TCP_pose_0"].min()) * 1000.0),
            "actual_y": float((stage25["ur_actual_TCP_pose_1"].max() - stage25["ur_actual_TCP_pose_1"].min()) * 1000.0),
        },
        "cmd_xy_mm_s": stats(cmd_xy * 1000.0),
        "ref_speed_mm_s": stats(ref_speed * 1000.0),
        "cmd_xy_over_14_5_mm_s_pct": float(np.mean(cmd_xy >= 0.0145) * 100.0),
    }
    force_error = stage25["normal_force_n"] + float(metadata["args"]["target_force_n"])
    fz_metrics = fz_constant_force_metrics(stage25, metadata)
    attitude_metrics = attitude_compliance_metrics(stage25)
    stage25_contact = {
        "rows": int(len(stage25)),
        "duration_s": duration_s(stage25),
        "path_time_start_s": float(stage25["path_time_s"].min()),
        "path_time_final_s": float(stage25["path_time_s"].max()),
        "normal_force_n": stats(stage25["normal_force_n"]),
        "fz_constant_force": fz_metrics,
        "force_error_vs_minus_5_n": {
            "signed_mean_n": float(force_error.mean()),
            "mae_n": float(force_error.abs().mean()),
            "p95_abs_n": float(np.percentile(np.abs(force_error), 95)),
            "p99_abs_n": float(np.percentile(np.abs(force_error), 99)),
        },
        "force_norm_n": stats(stage25["force_norm_n"]),
        "torque_norm_nm": stats(stage25["torque_norm_nm"]),
        "attitude_compliance": attitude_metrics,
        "actual_speed_norm_mm_s": stats(stage25["_step4e_actual_speed_norm_m_s"] * 1000.0),
    }
    return {
        "run_id": run.name,
        "run": run,
        "summary": summary,
        "metadata": metadata,
        "frequency": frequency,
        "quiet": quiet,
        "df": df,
        "stage25": stage25,
        "t25": stage25["stage25_t_s"].to_numpy(dtype=float),
        "actual_rel_mm": actual_rel_mm,
        "desired_rel_mm": desired_rel_mm,
        "error_xy": error_xy,
        "xy_error": xy_error,
        "cmd_xy": cmd_xy,
        "ref_speed": ref_speed,
        "path_tracking": path_metrics,
        "stage25_contact": stage25_contact,
        "fz_constant_force": fz_metrics,
        "attitude_compliance": attitude_metrics,
        "success_checks": {
            "final_stage_output_register_35": final_output_registers.get("35"),
            "final_stop_reason_output_register_30": final_output_registers.get("30"),
            "final_path_time_output_register_31_s": final_output_registers.get("31"),
            "stage25_duration_s": duration_s(stage25),
            "guard_reason_nonempty_rows": int((df["guard_reason"].fillna("").astype(str).str.len() > 0).sum()),
            "force_norm_max_n": summary["force_norm_stats_n"]["max"],
            "normal_force_min_n": summary["normal_force_stats_n"]["min"],
            "torque_norm_max_nm": summary["torque_norm_stats_nm"]["max"],
            "kunwei_quiet_stop_ok": bool(quiet["ok"] and quiet["probe"]["quiet"]),
        },
    }


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)

    summary = json.loads((RUN / "summary.json").read_text())
    metadata = json.loads((RUN / "metadata.json").read_text())
    frequency = json.loads((RUN / "stage_frequency_summary.json").read_text())
    quiet = json.loads((RUN / "kunwei_quiet_stream.json").read_text())

    df = pd.read_csv(RUN / "bridge_rtde_500hz.csv", usecols=RTDE_COLS)
    df["t_rel_s"] = df["t_monotonic_s"] - df["t_monotonic_s"].iloc[0]
    df["stage"] = df["ur_output_double_register_35"].round(2)
    df["path_time_s"] = df["_step4e_path_time_s"]
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
                "path_time_end_s": float(group["path_time_s"].max()),
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
    force_error = stage25["normal_force_n"] - target_force_signed_n
    fz_metrics = fz_constant_force_metrics(stage25, metadata)
    attitude_metrics = attitude_compliance_metrics(stage25)

    actual_xy = stage25[["ur_actual_TCP_pose_0", "ur_actual_TCP_pose_1"]].to_numpy(dtype=float)
    desired_xy = stage25[["_step4e_desired_x_m", "_step4e_desired_y_m"]].to_numpy(dtype=float)
    error_xy = actual_xy - desired_xy
    xy_error = np.linalg.norm(error_xy, axis=1)
    start_xy = desired_xy[0]
    actual_rel_mm = (actual_xy - start_xy) * 1000.0
    desired_rel_mm = (desired_xy - start_xy) * 1000.0

    filter_counts = {
        str(key): int(value)
        for key, value in stage25["_step4e_normal_filter_source"]
        .fillna("")
        .value_counts()
        .to_dict()
        .items()
    }
    guard_nonempty = int((df["guard_reason"].fillna("").astype(str).str.len() > 0).sum())
    final_output_registers = final_registers(df)
    stage25_duration_s = duration_s(stage25)

    stage25_metrics = {
        "rows": int(len(stage25)),
        "duration_s": stage25_duration_s,
        "path_time_start_s": float(stage25["path_time_s"].min()),
        "path_time_final_s": float(stage25["path_time_s"].max()),
        "path_time_max_s": float(stage25["path_time_s"].max()),
        "normal_force_n": stats(stage25["normal_force_n"]),
        "fz_constant_force": fz_metrics,
        "force_error_vs_minus_5_n": {
            "samples": int(len(force_error)),
            "signed_mean_n": float(force_error.mean()),
            "mae_n": float(force_error.abs().mean()),
            "p95_abs_n": float(np.percentile(np.abs(force_error), 95)),
            "p99_abs_n": float(np.percentile(np.abs(force_error), 99)),
            "min_n": float(force_error.min()),
            "max_n": float(force_error.max()),
        },
        "force_norm_n": stats(stage25["force_norm_n"]),
        "torque_norm_nm": stats(stage25["torque_norm_nm"]),
        "attitude_compliance": attitude_metrics,
        "cmd_vx_mm_s": stats(stage25["step4e_cmd_vx_m_s"] * 1000.0),
        "cmd_vy_mm_s": stats(stage25["step4e_cmd_vy_m_s"] * 1000.0),
        "cmd_vz_mm_s": stats(stage25["step4e_cmd_vz_m_s"] * 1000.0),
        "actual_speed_norm_mm_s": stats(stage25["_step4e_actual_speed_norm_m_s"] * 1000.0),
        "normal_filter_source_counts": filter_counts,
    }

    path_metrics = {
        "x_error_mm": stats(error_xy[:, 0] * 1000.0),
        "y_error_mm": stats(error_xy[:, 1] * 1000.0),
        "xy_error_mm": {
            "samples": int(len(xy_error)),
            "mean": float(np.mean(xy_error) * 1000.0),
            "median": float(np.median(xy_error) * 1000.0),
            "std": float(np.std(xy_error * 1000.0, ddof=1)),
            "min": float(np.min(xy_error) * 1000.0),
            "p95": float(np.percentile(xy_error, 95) * 1000.0),
            "p99": float(np.percentile(xy_error, 99) * 1000.0),
            "max": float(np.max(xy_error) * 1000.0),
        },
        "span_mm": {
            "desired_x": float((stage25["_step4e_desired_x_m"].max() - stage25["_step4e_desired_x_m"].min()) * 1000.0),
            "desired_y": float((stage25["_step4e_desired_y_m"].max() - stage25["_step4e_desired_y_m"].min()) * 1000.0),
            "actual_x": float((stage25["ur_actual_TCP_pose_0"].max() - stage25["ur_actual_TCP_pose_0"].min()) * 1000.0),
            "actual_y": float((stage25["ur_actual_TCP_pose_1"].max() - stage25["ur_actual_TCP_pose_1"].min()) * 1000.0),
        },
    }
    v2_payload = run_payload(RUN_V2)
    comparison = {
        "v1_xy_error_mean_mm": path_metrics["xy_error_mm"]["mean"],
        "v2_xy_error_mean_mm": v2_payload["path_tracking"]["xy_error_mm"]["mean"],
        "v1_xy_error_p95_mm": path_metrics["xy_error_mm"]["p95"],
        "v2_xy_error_p95_mm": v2_payload["path_tracking"]["xy_error_mm"]["p95"],
        "v1_xy_error_max_mm": path_metrics["xy_error_mm"]["max"],
        "v2_xy_error_max_mm": v2_payload["path_tracking"]["xy_error_mm"]["max"],
        "v1_cmd_xy_max_mm_s": float(np.max(np.linalg.norm(stage25[["step4e_cmd_vx_m_s", "step4e_cmd_vy_m_s"]].to_numpy(dtype=float), axis=1)) * 1000.0),
        "v2_cmd_xy_max_mm_s": v2_payload["path_tracking"]["cmd_xy_mm_s"]["max"],
        "v1_cmd_xy_over_14_5_mm_s_pct": float(
            np.mean(np.linalg.norm(stage25[["step4e_cmd_vx_m_s", "step4e_cmd_vy_m_s"]].to_numpy(dtype=float), axis=1) >= 0.0145)
            * 100.0
        ),
        "v2_cmd_xy_over_14_5_mm_s_pct": v2_payload["path_tracking"]["cmd_xy_over_14_5_mm_s_pct"],
    }

    step6_path = metadata["step4e_path"].get("step6_safe_frame", {})
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
            "tp_program": "/programs/andyl/kunwei/step6/step6b_contact_eight_baseline_v1.urp",
            "local_triplet": "experiments/kunwei/closed-loop-straight-line/2026-06-04/programs/step6/step6b_contact_eight_baseline_v1.{script,txt,urp}",
            "program_stamp": "2026-06-12T2217HKT_STEP6B_CONTACT_EIGHT_BASELINE_V1",
            "bridge_args": metadata["args"],
            "dashboard_preflight": metadata["dashboard_preflight"],
            "guard_contract": metadata["guard_contract"],
            "safety_boundary": metadata["safety_boundary"],
            "step6_path": {
                "formula": "along=0.04*sin(0.2t), lateral=0.01*sin(0.4t)",
                "duration_s": metadata["step4e_path"].get("step6_curve_duration_s"),
                "safe_frame": step6_path,
            },
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
            "target_path_time_s": 30.0,
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
        "fz_constant_force": fz_metrics,
        "attitude_compliance": attitude_metrics,
        "path_tracking": path_metrics,
        "v2": {
            "run_id": v2_payload["run_id"],
            "protocol": {
                "tp_program": "/programs/andyl/kunwei/step6/step6b_contact_eight_baseline_v2.urp",
                "local_triplet": "experiments/kunwei/closed-loop-straight-line/2026-06-04/programs/step6/step6b_contact_eight_baseline_v2.{script,txt,urp}",
                "program_stamp": "2026-06-12T2256HKT_STEP6B_CONTACT_EIGHT_BASELINE_V2",
                "bridge_args": v2_payload["metadata"]["args"],
                "guard_contract": v2_payload["metadata"]["guard_contract"],
            },
            "success_checks": v2_payload["success_checks"],
            "summary": v2_payload["summary"],
            "frequency": v2_payload["frequency"],
            "stage25_contact": v2_payload["stage25_contact"],
            "fz_constant_force": v2_payload["fz_constant_force"],
            "attitude_compliance": v2_payload["attitude_compliance"],
            "path_tracking": v2_payload["path_tracking"],
        },
        "comparison": comparison,
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
    axes[0].set_title("Step6b contact 8-shaped full-run overview")
    axes[1].plot(df["t_rel_s"], df["normal_force_n"], lw=0.75, color="#c44536")
    axes[1].axhline(target_force_signed_n, color="#333333", lw=0.8, ls="--", label="-5 N target")
    axes[1].set_ylabel("Normal force (N)")
    axes[1].legend(loc="lower right")
    axes[2].plot(df["t_rel_s"], df["path_time_s"], lw=1.0, color="#227c9d")
    axes[2].axhline(30.0, color="#333333", lw=0.8, ls="--")
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
    axes[0].set_title("Stage25 contact force and command")
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

    fig, ax = plt.subplots(figsize=(7.4, 7.0))
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
    set_equal_xy_limits(
        ax,
        np.concatenate([desired_rel_mm[:, 0], actual_rel_mm[:, 0]]),
        np.concatenate([desired_rel_mm[:, 1], actual_rel_mm[:, 1]]),
    )
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("X from reference start (mm)")
    ax.set_ylabel("Y from reference start (mm)")
    ax.set_title("Stage25 8-shaped XY actual vs reference path")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(ASSETS / "xy-actual-vs-reference.png")
    plt.close(fig)

    t25 = stage25["stage25_t_s"].to_numpy(dtype=float)
    fig, axes = plt.subplots(3, 1, figsize=(10, 7), sharex=True)
    axes[0].plot(t25, error_xy[:, 0] * 1000.0, lw=0.8, color="#227c9d")
    axes[0].axhline(0, color="#222222", lw=0.7)
    axes[0].set_ylabel("X err (mm)")
    axes[0].set_title("Stage25 8-shaped tracking errors")
    axes[1].plot(t25, error_xy[:, 1] * 1000.0, lw=0.8, color="#5f6f52")
    axes[1].axhline(0, color="#222222", lw=0.7)
    axes[1].set_ylabel("Y err (mm)")
    axes[2].plot(t25, xy_error * 1000.0, lw=0.8, color="#c44536")
    axes[2].set_ylabel("XY norm (mm)")
    axes[2].set_xlabel("Stage25 time (s)")
    fig.tight_layout()
    fig.savefig(ASSETS / "tracking-errors.png")
    plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(8.5, 6.0))
    labels = ["X span", "Y span"]
    desired_spans = [path_metrics["span_mm"]["desired_x"], path_metrics["span_mm"]["desired_y"]]
    actual_spans = [path_metrics["span_mm"]["actual_x"], path_metrics["span_mm"]["actual_y"]]
    x = np.arange(len(labels))
    axes[0].bar(x - 0.18, desired_spans, width=0.36, label="reference", color="#222222")
    axes[0].bar(x + 0.18, actual_spans, width=0.36, label="actual TCP", color="#227c9d")
    axes[0].set_xticks(x, labels)
    axes[0].set_ylabel("Span (mm)")
    axes[0].set_title("Reference span vs actual TCP span")
    axes[0].legend(loc="upper left")
    axes[1].plot(t25, stage25["_step4e_desired_vx_m_s"] * 1000.0, lw=0.8, label="ref vx")
    axes[1].plot(t25, stage25["_step4e_desired_vy_m_s"] * 1000.0, lw=0.8, label="ref vy")
    axes[1].plot(t25, stage25["step4e_cmd_vx_m_s"] * 1000.0, lw=0.8, label="cmd vx")
    axes[1].plot(t25, stage25["step4e_cmd_vy_m_s"] * 1000.0, lw=0.8, label="cmd vy")
    axes[1].set_ylabel("XY velocity (mm/s)")
    axes[1].set_xlabel("Stage25 time (s)")
    axes[1].legend(loc="upper right", ncol=4)
    fig.tight_layout()
    fig.savefig(ASSETS / "xy-span-and-command-limit.png")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.4, 7.0))
    ax.plot(
        v2_payload["desired_rel_mm"][:, 0],
        v2_payload["desired_rel_mm"][:, 1],
        lw=1.6,
        color="#222222",
        label="reference",
    )
    ax.plot(
        v2_payload["actual_rel_mm"][:, 0],
        v2_payload["actual_rel_mm"][:, 1],
        lw=1.0,
        color="#0f766e",
        alpha=0.95,
        label="v2 actual TCP",
    )
    ax.scatter(
        [v2_payload["desired_rel_mm"][0, 0], v2_payload["desired_rel_mm"][-1, 0]],
        [v2_payload["desired_rel_mm"][0, 1], v2_payload["desired_rel_mm"][-1, 1]],
        s=25,
        color="#c44536",
        zorder=4,
        label="start/end",
    )
    set_equal_xy_limits(
        ax,
        np.concatenate([v2_payload["desired_rel_mm"][:, 0], v2_payload["actual_rel_mm"][:, 0]]),
        np.concatenate([v2_payload["desired_rel_mm"][:, 1], v2_payload["actual_rel_mm"][:, 1]]),
    )
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("X from reference start (mm)")
    ax.set_ylabel("Y from reference start (mm)")
    ax.set_title("Step6b v2 Stage25 XY actual vs reference path")
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(ASSETS / "v2-xy-actual-vs-reference.png")
    plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(10, 6.2), sharex=False)
    axes[0].plot(t25, xy_error * 1000.0, lw=0.75, color="#c44536", label="v1 XY error")
    axes[0].plot(
        v2_payload["t25"],
        v2_payload["xy_error"] * 1000.0,
        lw=0.75,
        color="#0f766e",
        label="v2 XY error",
    )
    axes[0].set_ylabel("XY error (mm)")
    axes[0].set_title("Stage25 tracking error: v1 vs v2")
    axes[0].legend(loc="upper right")
    axes[1].plot(t25, error_xy[:, 1] * 1000.0, lw=0.75, color="#c44536", label="v1 Y error")
    axes[1].plot(
        v2_payload["t25"],
        v2_payload["error_xy"][:, 1] * 1000.0,
        lw=0.75,
        color="#0f766e",
        label="v2 Y error",
    )
    axes[1].axhline(0, color="#222222", lw=0.7)
    axes[1].set_ylabel("Y error (mm)")
    axes[1].set_xlabel("Stage25 time (s)")
    axes[1].legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(ASSETS / "v1-v2-tracking-error-comparison.png")
    plt.close(fig)

    v1_cmd_xy = np.linalg.norm(stage25[["step4e_cmd_vx_m_s", "step4e_cmd_vy_m_s"]].to_numpy(dtype=float), axis=1)
    v1_ref_speed = np.linalg.norm(stage25[["_step4e_desired_vx_m_s", "_step4e_desired_vy_m_s"]].to_numpy(dtype=float), axis=1)
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.2), sharex=True)
    axes[0].plot(t25, v1_ref_speed * 1000.0, lw=0.75, color="#222222", label="reference speed")
    axes[0].plot(t25, v1_cmd_xy * 1000.0, lw=0.75, color="#c44536", label="v1 command XY")
    axes[0].axhline(4.0, color="#c44536", lw=0.8, ls="--", label="v1 motion cap")
    axes[0].axhline(6.0, color="#8c6d1f", lw=0.8, ls="--", label="v1 total cap")
    axes[0].set_ylabel("Speed (mm/s)")
    axes[0].set_title("Command speed and caps: v1 vs v2")
    axes[0].legend(loc="upper right", ncol=2)
    axes[1].plot(v2_payload["t25"], v2_payload["ref_speed"] * 1000.0, lw=0.75, color="#222222", label="reference speed")
    axes[1].plot(v2_payload["t25"], v2_payload["cmd_xy"] * 1000.0, lw=0.75, color="#0f766e", label="v2 command XY")
    axes[1].axhline(15.0, color="#0f766e", lw=0.8, ls="--", label="v2 cap")
    axes[1].set_ylabel("Speed (mm/s)")
    axes[1].set_xlabel("Stage25 time (s)")
    axes[1].legend(loc="upper right", ncol=3)
    fig.tight_layout()
    fig.savefig(ASSETS / "v1-v2-command-speed-comparison.png")
    plt.close(fig)

    v1_fz_error = stage25["fz_n_zeroed"] - target_force_signed_n
    v2_target_force_signed_n = signed_target_force_n(v2_payload["metadata"])
    v2_stage25 = v2_payload["stage25"]
    v2_fz_error = v2_stage25["fz_n_zeroed"] - v2_target_force_signed_n
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.5), sharex=False)
    axes[0].plot(t25, stage25["fz_n_zeroed"], lw=0.75, color="#c44536", label="v1 raw Fz")
    axes[0].plot(v2_payload["t25"], v2_stage25["fz_n_zeroed"], lw=0.75, color="#0f766e", label="v2 raw Fz")
    axes[0].axhline(target_force_signed_n, color="#222222", lw=0.9, ls="--", label="-5 N target")
    axes[0].set_ylabel("Fz (N)")
    axes[0].set_title("Stage25 constant-force evidence: raw Fz")
    axes[0].legend(loc="lower right", ncol=3)
    axes[1].plot(t25, stage25["_step4e_normal_load_n"], lw=0.75, color="#c44536", label="v1 projected normal load")
    axes[1].plot(
        v2_payload["t25"],
        v2_stage25["_step4e_normal_load_n"],
        lw=0.75,
        color="#0f766e",
        label="v2 projected normal load",
    )
    axes[1].axhline(float(metadata["args"]["target_force_n"]), color="#222222", lw=0.9, ls="--", label="5 N load target")
    axes[1].set_ylabel("Projected load (N)")
    axes[1].set_xlabel("Stage25 time (s)")
    axes[1].legend(loc="upper right", ncol=3)
    fig.tight_layout()
    fig.savefig(ASSETS / "stage25-fz-constant-force-v1-v2.png")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 4.6))
    ax.plot(t25, v1_fz_error, lw=0.75, color="#c44536", label="v1 Fz - (-5 N)")
    ax.plot(v2_payload["t25"], v2_fz_error, lw=0.75, color="#0f766e", label="v2 Fz - (-5 N)")
    ax.axhline(0.0, color="#222222", lw=0.8)
    ax.set_ylabel("Signed Fz error (N)")
    ax.set_xlabel("Stage25 time (s)")
    ax.set_title("Stage25 signed Fz error relative to target")
    ax.legend(loc="lower right")
    fig.tight_layout()
    fig.savefig(ASSETS / "stage25-fz-error-v1-v2.png")
    plt.close(fig)

    v1_w_norm = np.linalg.norm(
        stage25[["step4e_cmd_wx_rad_s", "step4e_cmd_wy_rad_s", "step4e_cmd_wz_rad_s"]].to_numpy(dtype=float),
        axis=1,
    )
    v2_w_norm = np.linalg.norm(
        v2_stage25[["step4e_cmd_wx_rad_s", "step4e_cmd_wy_rad_s", "step4e_cmd_wz_rad_s"]].to_numpy(dtype=float),
        axis=1,
    )
    fig, axes = plt.subplots(2, 1, figsize=(10, 6.5), sharex=False)
    axes[0].plot(t25, np.degrees(stage25["step4e_orientation_error_rad"]), lw=0.75, color="#c44536", label="v1")
    axes[0].plot(
        v2_payload["t25"],
        np.degrees(v2_stage25["step4e_orientation_error_rad"]),
        lw=0.75,
        color="#0f766e",
        label="v2",
    )
    axes[0].set_ylabel("Orientation error (deg)")
    axes[0].set_title("Stage25 attitude compliance evidence")
    axes[0].legend(loc="upper right")
    axes[1].plot(t25, np.degrees(v1_w_norm), lw=0.75, color="#c44536", label="v1 angular command")
    axes[1].plot(v2_payload["t25"], np.degrees(v2_w_norm), lw=0.75, color="#0f766e", label="v2 angular command")
    axes[1].set_ylabel("Angular command (deg/s)")
    axes[1].set_xlabel("Stage25 time (s)")
    axes[1].legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(ASSETS / "stage25-attitude-compliance-v1-v2.png")
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
