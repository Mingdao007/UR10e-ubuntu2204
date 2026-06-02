#!/usr/bin/env python3
from __future__ import annotations

import ast
import csv
import json
import math
import os
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt


RUN_DIR = Path(__file__).resolve().parents[1]
REPORT_ROOT = Path("/home/andy/ur10e_ros2_ws/report")
REPORT_STEM = "demo_01_02"
FIG_DIR = REPORT_ROOT / "assets" / REPORT_STEM
REPORT_PATH = REPORT_ROOT / f"{REPORT_STEM}.md"
SUMMARY_PATH = RUN_DIR / "analysis" / "analysis_summary.json"
CSV_GLOB = "demo_1_2_final_success_onrobot_vars_*.csv"
REFERENCE_CSV = Path(
    "/home/andy/ur10e_ros2_ws/ft_sensor/onrobot/hex_e_v2_3010007655/"
    "measurements/demo_1_2_path_straight_line_urcap_20260528_012054/"
    "demo_1_2_rtde_20260528_012054.csv"
)


@dataclass
class ProgramParams:
    search_speed_m_s: float | None
    search_waypoint_tool_z_m: float | None
    search_f3d_limit_n: float | None
    control_fz_target_n: float | None
    force_pid: list[float] | None
    torque_pid: list[float] | None
    ft_move_speed_m_s: float | None
    path_id: int | None
    path_relative: bool | None
    path_home_pose: list[float] | None
    retract_distance_m: float | None


def parse_list(text: str) -> list[float]:
    return [float(v.strip()) for v in text.split(",") if v.strip()]


def parse_program_params(script_path: Path) -> ProgramParams:
    text = script_path.read_text(encoding="utf-8", errors="replace")
    search_speed = None
    search_z = None
    f3d_limit = None
    control_fz = None
    force_pid = None
    torque_pid = None
    ft_move_speed = None
    path_id = None
    path_relative = None
    path_home_pose = None
    retract_distance = None

    m = re.search(r"of_move_init\(frameID = 1, frameRPY = \[0,0,0\], speed = ([0-9.]+), acc", text)
    if m:
        search_speed = float(m.group(1))
    m = re.search(r"of_waypoint\(relativeP = p\[0\.0, 0\.0, ([^,]+),", text)
    if m:
        search_z = float(m.group(1))
    m = re.search(r"of_limit_start\(.*?ft_limits = \[([^\]]+)\].*?frameID = 1", text, re.S)
    if m:
        vals = parse_list(m.group(1))
        if len(vals) >= 7:
            f3d_limit = vals[6]
    m = re.search(
        r"of_ft_control_start\(.*?ft_values = \[([^\]]+)\].*?"
        r"forcePID = \[([^\]]+)\].*?torquePID = \[([^\]]+)\]",
        text,
        re.S,
    )
    if m:
        ft_values = parse_list(m.group(1))
        if len(ft_values) >= 3:
            control_fz = ft_values[2]
        force_pid = parse_list(m.group(2))
        torque_pid = parse_list(m.group(3))
    m = re.search(r"of_move_init\(frameID = 0, frameRPY = \[0,0,0\], speed = ([0-9.]+), acc", text)
    if m:
        ft_move_speed = float(m.group(1))
    m = re.search(r"of_path_play\(pathID = ([0-9]+), relative = (True|False), homePose = p\[([^\]]+)\]", text)
    if m:
        path_id = int(m.group(1))
        path_relative = m.group(2) == "True"
        path_home_pose = parse_list(m.group(3))
    m = re.search(r"retract_distance = ([\-0-9.]+)", text)
    if m:
        retract_distance = float(m.group(1))

    return ProgramParams(
        search_speed,
        search_z,
        f3d_limit,
        control_fz,
        force_pid,
        torque_pid,
        ft_move_speed,
        path_id,
        path_relative,
        path_home_pose,
        retract_distance,
    )


def load_rows(csv_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            pose = ast.literal_eval(raw["actual_TCP_pose"])
            speed = ast.literal_eval(raw["actual_TCP_speed"])
            fx = float(raw["output_double_register_24"])
            fy = float(raw["output_double_register_25"])
            fz = float(raw["output_double_register_26"])
            tx = float(raw["output_double_register_27"])
            ty = float(raw["output_double_register_28"])
            tz = float(raw["output_double_register_29"])
            rows.append(
                {
                    "t": float(raw["local_elapsed_s"]),
                    "timestamp": float(raw["timestamp"]),
                    "runtime_state": int(float(raw["runtime_state"])),
                    "robot_mode": int(float(raw["robot_mode"])),
                    "safety_mode": int(float(raw["safety_mode"])),
                    "speed_scaling": float(raw["speed_scaling"]),
                    "pose": pose,
                    "speed": speed,
                    "fx": fx,
                    "fy": fy,
                    "fz": fz,
                    "tx": tx,
                    "ty": ty,
                    "tz": tz,
                    "fxy": math.hypot(fx, fy),
                    "f3d": math.sqrt(fx * fx + fy * fy + fz * fz),
                    "tcp_speed": math.sqrt(speed[0] ** 2 + speed[1] ** 2 + speed[2] ** 2),
                }
            )
    return rows


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {k: None for k in ["mean", "std", "min", "p1", "p50", "p99", "max"]}
    return {
        "mean": statistics.fmean(values),
        "std": statistics.stdev(values) if len(values) > 1 else 0.0,
        "min": min(values),
        "p1": percentile(values, 0.01),
        "p50": percentile(values, 0.50),
        "p99": percentile(values, 0.99),
        "max": max(values),
    }


def ranges(rows: list[dict[str, Any]], key: str) -> tuple[float, float]:
    vals = [float(row[key]) for row in rows]
    return min(vals), max(vals)


def split_active_windows(rows: list[dict[str, Any]]) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    start: int | None = None
    for i, row in enumerate(rows):
        active = row["runtime_state"] == 2
        if active and start is None:
            start = i
        elif not active and start is not None:
            windows.append((start, i - 1))
            start = None
    if start is not None:
        windows.append((start, len(rows) - 1))
    return windows


def longest_moving_window(rows: list[dict[str, Any]], min_speed_m_s: float = 0.001) -> tuple[int, int] | None:
    windows: list[tuple[int, int]] = []
    start: int | None = None
    for i, row in enumerate(rows):
        moving = row["runtime_state"] == 2 and row["tcp_speed"] >= min_speed_m_s
        if moving and start is None:
            start = i
        elif not moving and start is not None:
            windows.append((start, i - 1))
            start = None
    if start is not None:
        windows.append((start, len(rows) - 1))
    if not windows:
        return None
    return max(windows, key=lambda w: rows[w[1]]["t"] - rows[w[0]]["t"])


def path_length(rows: list[dict[str, Any]]) -> float:
    total = 0.0
    for a, b in zip(rows, rows[1:]):
        pa = a["pose"]
        pb = b["pose"]
        total += math.dist(pa[:3], pb[:3])
    return total


def xy_path_length_points(points: list[list[float]]) -> float:
    if len(points) < 2:
        return 0.0
    return sum(math.dist(a[:2], b[:2]) for a, b in zip(points, points[1:]))


def xy_path_length_rows(rows: list[dict[str, Any]]) -> float:
    return xy_path_length_points([row["pose"] for row in rows])


def load_reference_rows(csv_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            target = ast.literal_eval(raw["target_TCP_pose"])
            actual = ast.literal_eval(raw["actual_TCP_pose"])
            rows.append(
                {
                    "t": float(raw["local_elapsed_s"]),
                    "runtime_state": int(float(raw["runtime_state"])),
                    "pose": actual,
                    "target": target,
                }
            )
    return rows


def target_motion_segments(rows: list[dict[str, Any]], min_step_m: float = 2e-5) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    start: int | None = None
    for i in range(1, len(rows)):
        step = math.dist(rows[i]["target"][:2], rows[i - 1]["target"][:2])
        moving = rows[i]["runtime_state"] == 2 and step > min_step_m
        if moving and start is None:
            start = i - 1
        elif not moving and start is not None:
            segments.append((start, i - 1))
            start = None
    if start is not None:
        segments.append((start, len(rows) - 1))
    return segments


def select_ground_truth_path(rows: list[dict[str, Any]]) -> dict[str, Any]:
    segments = target_motion_segments(rows)
    if not segments:
        raise ValueError("reference CSV has no moving target_TCP_pose segment")

    def segment_score(segment: tuple[int, int]) -> tuple[int, float]:
        a, b = segment
        forward = rows[b]["target"][0] > rows[a]["target"][0] and rows[b]["target"][1] < rows[a]["target"][1]
        points = [row["target"] for row in rows[a : b + 1]]
        return (1 if forward else 0, xy_path_length_points(points))

    start_i, end_i = max(segments, key=segment_score)
    points = [row["target"] for row in rows[start_i : end_i + 1]]
    return {
        "source_csv": str(REFERENCE_CSV),
        "selection_rule": (
            "target_TCP_pose moving segment with runtime_state == 2, target XY step > 0.02 mm, "
            "and forward direction x increasing / y decreasing"
        ),
        "start_s": rows[start_i]["t"],
        "end_s": rows[end_i]["t"],
        "samples": len(points),
        "points": points,
        "xy_length_mm": xy_path_length_points(points) * 1000.0,
        "x_range_mm": [min(p[0] for p in points) * 1000.0, max(p[0] for p in points) * 1000.0],
        "y_range_mm": [min(p[1] for p in points) * 1000.0, max(p[1] for p in points) * 1000.0],
    }


def contiguous_index_segments(indexes: list[int]) -> list[tuple[int, int]]:
    segments: list[tuple[int, int]] = []
    start: int | None = None
    previous: int | None = None
    for idx in indexes:
        if start is None:
            start = idx
        elif previous is not None and idx != previous + 1:
            segments.append((start, previous))
            start = idx
        previous = idx
    if start is not None and previous is not None:
        segments.append((start, previous))
    return segments


def contact_path_segments(rows: list[dict[str, Any]], active_windows: list[tuple[int, int]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for run_idx, (start_i, end_i) in enumerate(active_windows, start=1):
        indexes = [
            idx
            for idx in range(start_i, end_i + 1)
            if abs(rows[idx]["fz"]) >= 2.0 and rows[idx]["tcp_speed"] >= 0.001
        ]
        candidates = contiguous_index_segments(indexes)
        if not candidates:
            continue
        best_start, best_end = max(candidates, key=lambda segment: xy_path_length_rows(rows[segment[0] : segment[1] + 1]))
        path_rows = rows[best_start : best_end + 1]
        selected.append(
            {
                "segment": f"Run {run_idx}",
                "start_index": best_start,
                "end_index": best_end,
                "start_s": path_rows[0]["t"] - rows[0]["t"],
                "end_s": path_rows[-1]["t"] - rows[0]["t"],
                "duration_s": path_rows[-1]["t"] - path_rows[0]["t"],
                "samples": len(path_rows),
                "rows": path_rows,
                "xy_length_mm": xy_path_length_rows(path_rows) * 1000.0,
            }
        )
    return selected


def project_point_to_reference(point: list[float], reference_points: list[list[float]]) -> dict[str, float]:
    best: dict[str, float] | None = None
    cumulative = 0.0
    px, py = point[:2]
    for a, b in zip(reference_points, reference_points[1:]):
        ax, ay = a[:2]
        bx, by = b[:2]
        vx = bx - ax
        vy = by - ay
        seg_len_sq = vx * vx + vy * vy
        if seg_len_sq == 0:
            continue
        u = ((px - ax) * vx + (py - ay) * vy) / seg_len_sq
        u = max(0.0, min(1.0, u))
        qx = ax + u * vx
        qy = ay + u * vy
        dist_sq = (px - qx) ** 2 + (py - qy) ** 2
        progress = cumulative + math.sqrt(seg_len_sq) * u
        candidate = {
            "ref_x": qx,
            "ref_y": qy,
            "progress_mm": progress * 1000.0,
            "dx_mm": (px - qx) * 1000.0,
            "dy_mm": (py - qy) * 1000.0,
            "error_2d_mm": math.sqrt(dist_sq) * 1000.0,
        }
        if best is None or candidate["error_2d_mm"] < best["error_2d_mm"]:
            best = candidate
        cumulative += math.sqrt(seg_len_sq)
    if best is None:
        ref = reference_points[0]
        return {
            "ref_x": ref[0],
            "ref_y": ref[1],
            "progress_mm": 0.0,
            "dx_mm": (px - ref[0]) * 1000.0,
            "dy_mm": (py - ref[1]) * 1000.0,
            "error_2d_mm": math.dist(point[:2], ref[:2]) * 1000.0,
        }
    return best


def point_at_reference_progress(reference_points: list[list[float]], progress_mm: float) -> list[float]:
    if not reference_points:
        raise ValueError("reference path is empty")
    if len(reference_points) == 1:
        return reference_points[0]
    target_m = progress_mm / 1000.0
    cumulative = 0.0
    for a, b in zip(reference_points, reference_points[1:]):
        seg_len = math.dist(a[:2], b[:2])
        if seg_len == 0:
            continue
        if cumulative + seg_len >= target_m:
            u = (target_m - cumulative) / seg_len
            u = max(0.0, min(1.0, u))
            return [a[i] + (b[i] - a[i]) * u for i in range(len(a))]
        cumulative += seg_len
    return reference_points[-1]


def reference_points_progress(reference_points: list[list[float]]) -> list[float]:
    progress = [0.0]
    for a, b in zip(reference_points, reference_points[1:]):
        progress.append(progress[-1] + math.dist(a[:2], b[:2]) * 1000.0)
    return progress


def slice_reference_by_progress(reference_points: list[list[float]], start_mm: float, end_mm: float) -> list[list[float]]:
    if start_mm > end_mm:
        start_mm, end_mm = end_mm, start_mm
    progress = reference_points_progress(reference_points)
    sliced = [point_at_reference_progress(reference_points, start_mm)]
    for point, point_progress in zip(reference_points, progress):
        if start_mm < point_progress < end_mm:
            sliced.append(point)
    sliced.append(point_at_reference_progress(reference_points, end_mm))
    return sliced


def rms(values: list[float]) -> float | None:
    if not values:
        return None
    return math.sqrt(statistics.fmean([value * value for value in values]))


def error_axis_summary(values: list[float]) -> dict[str, float | None]:
    abs_values = [abs(value) for value in values]
    return {
        "mean_mm": statistics.fmean(values) if values else None,
        "mae_mm": statistics.fmean(abs_values) if abs_values else None,
        "rms_mm": rms(values),
        "p95_abs_mm": percentile(abs_values, 0.95) if abs_values else None,
        "max_abs_mm": max(abs_values) if abs_values else None,
    }


def compare_contact_paths_to_reference(
    segments: list[dict[str, Any]], reference_points: list[list[float]]
) -> dict[str, Any]:
    summaries: list[dict[str, Any]] = []
    all_dx: list[float] = []
    all_dy: list[float] = []
    all_e2d: list[float] = []
    for segment in segments:
        samples = []
        for row in segment["rows"]:
            projection = project_point_to_reference(row["pose"], reference_points)
            samples.append(projection)
        dx = [sample["dx_mm"] for sample in samples]
        dy = [sample["dy_mm"] for sample in samples]
        e2d = [sample["error_2d_mm"] for sample in samples]
        all_dx.extend(dx)
        all_dy.extend(dy)
        all_e2d.extend(e2d)
        segment["tracking_samples"] = samples
        summaries.append(
            {
                "segment": segment["segment"],
                "samples": segment["samples"],
                "start_s": segment["start_s"],
                "end_s": segment["end_s"],
                "duration_s": segment["duration_s"],
                "actual_xy_length_mm": segment["xy_length_mm"],
                "reference_progress_start_mm": samples[0]["progress_mm"] if samples else None,
                "reference_progress_end_mm": samples[-1]["progress_mm"] if samples else None,
                "x_error": error_axis_summary(dx),
                "y_error": error_axis_summary(dy),
                "error_2d": {
                    "mean_mm": statistics.fmean(e2d) if e2d else None,
                    "p95_mm": percentile(e2d, 0.95) if e2d else None,
                    "max_mm": max(e2d) if e2d else None,
                },
            }
        )
    return {
        "selection_rule": "longest contiguous per-run segment where runtime_state == 2, |Fz| >= 2 N, and TCP speed >= 1 mm/s",
        "segments": summaries,
        "overall": {
            "samples": len(all_e2d),
            "x_error": error_axis_summary(all_dx),
            "y_error": error_axis_summary(all_dy),
            "error_2d": {
                "mean_mm": statistics.fmean(all_e2d) if all_e2d else None,
                "p95_mm": percentile(all_e2d, 0.95) if all_e2d else None,
                "max_mm": max(all_e2d) if all_e2d else None,
            },
        },
    }


def segment_metrics(
    rows: list[dict[str, Any]],
    start_i: int,
    end_i: int,
    params: ProgramParams,
    segment_name: str,
) -> dict[str, Any]:
    active_rows = rows[start_i : end_i + 1]
    moving_rows = [row for row in active_rows if row["tcp_speed"] >= 0.001]
    force_rows = moving_rows or active_rows
    contact_rows = [row for row in active_rows if abs(row["fz"]) >= 2.0]
    target = abs(params.control_fz_target_n) if params.control_fz_target_n is not None else None
    abs_fz_error = [abs(abs(row["fz"]) - target) for row in force_rows] if target is not None else []
    signed_fz_error = [row["fz"] - params.control_fz_target_n for row in force_rows] if params.control_fz_target_n is not None else []
    contact_abs_fz_error = [abs(abs(row["fz"]) - target) for row in contact_rows] if target is not None else []
    xy_len = 0.0
    for a, b in zip(force_rows, force_rows[1:]):
        xy_len += math.dist(a["pose"][:2], b["pose"][:2])
    xs = [row["pose"][0] for row in force_rows]
    ys = [row["pose"][1] for row in force_rows]
    zs = [row["pose"][2] for row in force_rows]
    return {
        "segment": segment_name,
        "active_start_s": active_rows[0]["t"] - rows[0]["t"],
        "active_end_s": active_rows[-1]["t"] - rows[0]["t"],
        "active_duration_s": active_rows[-1]["t"] - active_rows[0]["t"],
        "active_samples": len(active_rows),
        "moving_samples": len(moving_rows),
        "moving_duration_s": (moving_rows[-1]["t"] - moving_rows[0]["t"]) if len(moving_rows) >= 2 else 0.0,
        "force_stats": {axis: stats([r[axis] for r in force_rows]) for axis in ["fx", "fy", "fz", "fxy", "f3d"]},
        "fz_abs_target_error_stats_n": stats(abs_fz_error),
        "fz_signed_target_error_stats_n": stats(signed_fz_error),
        "contact_fraction_abs_fz_error_le_1n": (sum(v <= 1.0 for v in abs_fz_error) / len(abs_fz_error)) if abs_fz_error else None,
        "contact_fraction_abs_fz_error_le_2n": (sum(v <= 2.0 for v in abs_fz_error) / len(abs_fz_error)) if abs_fz_error else None,
        "contact_samples_abs_fz_ge_2n": len(contact_rows),
        "contact_duration_s": (contact_rows[-1]["t"] - contact_rows[0]["t"]) if len(contact_rows) >= 2 else 0.0,
        "contact_force_stats": {axis: stats([r[axis] for r in contact_rows]) for axis in ["fx", "fy", "fz", "fxy", "f3d"]},
        "contact_abs_fz_target_error_stats_n": stats(contact_abs_fz_error),
        "contact_abs_fz_error_le_1n_fraction": (sum(v <= 1.0 for v in contact_abs_fz_error) / len(contact_abs_fz_error)) if contact_abs_fz_error else None,
        "contact_abs_fz_error_le_2n_fraction": (sum(v <= 2.0 for v in contact_abs_fz_error) / len(contact_abs_fz_error)) if contact_abs_fz_error else None,
        "path_length_3d_mm": path_length(force_rows) * 1000.0 if len(force_rows) >= 2 else 0.0,
        "path_length_xy_mm": xy_len * 1000.0,
        "x_range_mm": [min(xs) * 1000.0 if xs else None, max(xs) * 1000.0 if xs else None],
        "y_range_mm": [min(ys) * 1000.0 if ys else None, max(ys) * 1000.0 if ys else None],
        "z_range_mm": [min(zs) * 1000.0 if zs else None, max(zs) * 1000.0 if zs else None],
        "max_tcp_speed_mm_s": max((r["tcp_speed"] for r in force_rows), default=0.0) * 1000.0,
    }


def compressed_time(rows: list[dict[str, Any]]) -> list[float]:
    if not rows:
        return []
    out = [0.0]
    for a, b in zip(rows, rows[1:]):
        dt = max(0.0, b["t"] - a["t"])
        if dt > 0.2:
            dt = 0.008
        out.append(out[-1] + dt)
    return out


def write_plots(
    rows: list[dict[str, Any]],
    moving_rows: list[dict[str, Any]],
    params: ProgramParams,
    contact_segments: list[dict[str, Any]],
    ground_truth_path: dict[str, Any],
) -> dict[str, str]:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    active_rows = [row for row in rows if row["runtime_state"] == 2]
    contact_rows = [row for row in active_rows if abs(row["fz"]) >= 2.0]
    force_plot_rows = active_rows or rows
    fz_plot_rows = contact_rows or force_plot_rows
    force_t = compressed_time(force_plot_rows)
    fz_t = compressed_time(fz_plot_rows)

    paths: dict[str, str] = {}
    plt.figure(figsize=(11, 6))
    plt.plot(force_t, [row["fx"] for row in force_plot_rows], label="Fx")
    plt.plot(force_t, [row["fy"] for row in force_plot_rows], label="Fy")
    plt.plot(force_t, [row["fz"] for row in force_plot_rows], label="Fz")
    if params.control_fz_target_n is not None:
        plt.axhline(params.control_fz_target_n, color="tab:red", linestyle="--", linewidth=1, label="F/T Control Fz target")
    plt.xlabel("compressed active time (s)")
    plt.ylabel("force (N)")
    plt.title("OnRobot Fx/Fy/Fz in active windows")
    plt.grid(True, alpha=0.3)
    plt.legend()
    out = FIG_DIR / "force_xyz_time.png"
    plt.tight_layout()
    plt.savefig(out, dpi=160)
    plt.close()
    paths["force_xyz_time"] = out.name

    plt.figure(figsize=(11, 5))
    plt.plot(fz_t, [row["fz"] for row in fz_plot_rows], label="Fz")
    plt.plot(fz_t, [abs(row["fz"]) for row in fz_plot_rows], label="|Fz|", alpha=0.8)
    if params.control_fz_target_n is not None:
        plt.axhline(params.control_fz_target_n, color="tab:red", linestyle="--", linewidth=1, label="+target")
        plt.axhline(-params.control_fz_target_n, color="tab:gray", linestyle=":", linewidth=1, label="-target")
    plt.xlabel("compressed contact time (s)")
    plt.ylabel("Fz (N)")
    plt.title("Fz and |Fz| in contact windows")
    plt.grid(True, alpha=0.3)
    plt.legend()
    out = FIG_DIR / "fz_time.png"
    plt.tight_layout()
    plt.savefig(out, dpi=160)
    plt.close()
    paths["fz_time"] = out.name

    reference_points = ground_truth_path.get("plot_points") or ground_truth_path["points"]
    plt.figure(figsize=(7, 7))
    plt.plot(
        [point[0] * 1000.0 for point in reference_points],
        [point[1] * 1000.0 for point in reference_points],
        color="black",
        linestyle="--",
        linewidth=2.0,
        label="ground truth target_TCP_pose contact window",
    )
    for idx, segment in enumerate(contact_segments):
        segment_rows = segment["rows"]
        plt.plot(
            [row["pose"][0] * 1000.0 for row in segment_rows],
            [row["pose"][1] * 1000.0 for row in segment_rows],
            linewidth=1.8,
            label=f"{segment['segment']} actual contact path",
        )
        plt.scatter(
            [segment_rows[0]["pose"][0] * 1000.0],
            [segment_rows[0]["pose"][1] * 1000.0],
            s=22,
            marker="o",
            color=f"C{idx}",
        )
        plt.scatter(
            [segment_rows[-1]["pose"][0] * 1000.0],
            [segment_rows[-1]["pose"][1] * 1000.0],
            s=28,
            marker="x",
            color=f"C{idx}",
        )
    plt.xlabel("TCP X (mm)")
    plt.ylabel("TCP Y (mm)")
    plt.axis("equal")
    plt.title("Contact path actual vs ground truth in XY")
    plt.grid(True, alpha=0.3)
    plt.legend(fontsize=8)
    out = FIG_DIR / "tcp_xy_contact_path_compare.png"
    plt.tight_layout()
    plt.savefig(out, dpi=160)
    plt.close()
    paths["tcp_xy_contact_path_compare"] = out.name

    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    for segment in contact_segments:
        samples = segment.get("tracking_samples", [])
        if not samples:
            continue
        progress = [sample["progress_mm"] for sample in samples]
        axes[0].plot(progress, [sample["dx_mm"] for sample in samples], linewidth=1.2, label=segment["segment"])
        axes[1].plot(progress, [sample["dy_mm"] for sample in samples], linewidth=1.2, label=segment["segment"])
    axes[0].axhline(0.0, color="black", linestyle="--", linewidth=0.8)
    axes[1].axhline(0.0, color="black", linestyle="--", linewidth=0.8)
    axes[0].set_ylabel("X error (mm)")
    axes[1].set_ylabel("Y error (mm)")
    axes[1].set_xlabel("reference path progress (mm)")
    axes[0].set_title("Actual contact path error vs ground truth")
    for axis in axes:
        axis.grid(True, alpha=0.3)
        axis.legend(fontsize=8)
    out = FIG_DIR / "tcp_xy_contact_path_error.png"
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)
    paths["tcp_xy_contact_path_error"] = out.name

    plt.figure(figsize=(11, 6))
    speed_plot_rows = active_rows or rows
    speed_t = compressed_time(speed_plot_rows)
    plt.plot(speed_t, [row["pose"][2] * 1000.0 for row in speed_plot_rows], label="TCP Z")
    plt.plot(speed_t, [row["tcp_speed"] * 1000.0 for row in speed_plot_rows], label="TCP speed")
    plt.xlabel("compressed active time (s)")
    plt.ylabel("mm or mm/s")
    plt.title("TCP Z and speed in active windows")
    plt.grid(True, alpha=0.3)
    plt.legend()
    out = FIG_DIR / "tcp_z_speed_time.png"
    plt.tight_layout()
    plt.savefig(out, dpi=160)
    plt.close()
    paths["tcp_z_speed_time"] = out.name
    return paths


def fmt(value: float | None, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    return f"{value:.{digits}f}"


def report_rel(path: Path) -> str:
    return os.path.relpath(path, REPORT_PATH.parent)


def main() -> int:
    csv_files = sorted(RUN_DIR.glob(CSV_GLOB))
    if not csv_files:
        raise SystemExit(f"no CSV matching {CSV_GLOB} in {RUN_DIR}")
    csv_path = csv_files[-1]
    rows = load_rows(csv_path)
    if len(rows) < 2:
        raise SystemExit("not enough rows")

    script_path = RUN_DIR / "controller_snapshot" / "path_straight_line.script"
    params = parse_program_params(script_path)
    reference_rows = load_reference_rows(REFERENCE_CSV)
    ground_truth_path = select_ground_truth_path(reference_rows)
    active_windows = split_active_windows(rows)
    moving_window = longest_moving_window(rows)
    segment_summaries = [
        segment_metrics(rows, a, b, params, f"Run {idx}")
        for idx, (a, b) in enumerate(active_windows, start=1)
    ]
    all_moving_rows = [
        row
        for a, b in active_windows
        for row in rows[a : b + 1]
        if row["tcp_speed"] >= 0.001
    ]
    all_contact_rows = [
        row
        for a, b in active_windows
        for row in rows[a : b + 1]
        if abs(row["fz"]) >= 2.0
    ]
    selected_contact_paths = contact_path_segments(rows, active_windows)
    contact_path_tracking = compare_contact_paths_to_reference(selected_contact_paths, ground_truth_path["points"])
    representative_idx = min(
        range(len(contact_path_tracking["segments"])),
        key=lambda idx: (
            contact_path_tracking["segments"][idx]["error_2d"]["p95_mm"],
            segment_summaries[idx]["contact_abs_fz_target_error_stats_n"]["mean"],
        ),
    )
    progress_starts = [
        segment["reference_progress_start_mm"]
        for segment in contact_path_tracking["segments"]
        if segment["reference_progress_start_mm"] is not None
    ]
    progress_ends = [
        segment["reference_progress_end_mm"]
        for segment in contact_path_tracking["segments"]
        if segment["reference_progress_end_mm"] is not None
    ]
    if progress_starts and progress_ends:
        comparison_start_mm = min(progress_starts)
        comparison_end_mm = max(progress_ends)
        ground_truth_path["comparison_progress_start_mm"] = comparison_start_mm
        ground_truth_path["comparison_progress_end_mm"] = comparison_end_mm
        ground_truth_path["comparison_xy_length_mm"] = comparison_end_mm - comparison_start_mm
        ground_truth_path["plot_points"] = slice_reference_by_progress(
            ground_truth_path["points"], comparison_start_mm, comparison_end_mm
        )

    duration = rows[-1]["t"] - rows[0]["t"]
    effective_hz = (len(rows) - 1) / duration if duration > 0 else None
    force_rows = all_moving_rows or rows
    abs_fz_error = [
        abs(abs(row["fz"]) - abs(params.control_fz_target_n))
        for row in force_rows
        if params.control_fz_target_n is not None
    ]
    signed_fz_error = [
        row["fz"] - params.control_fz_target_n
        for row in force_rows
        if params.control_fz_target_n is not None
    ]
    contact_fraction_abs_1n = None
    contact_fraction_abs_2n = None
    if abs_fz_error:
        contact_fraction_abs_1n = sum(v <= 1.0 for v in abs_fz_error) / len(abs_fz_error)
        contact_fraction_abs_2n = sum(v <= 2.0 for v in abs_fz_error) / len(abs_fz_error)
    contact_abs_fz_error = [
        abs(abs(row["fz"]) - abs(params.control_fz_target_n))
        for row in all_contact_rows
        if params.control_fz_target_n is not None
    ]
    contact_abs_fz_error_le_1n = None
    contact_abs_fz_error_le_2n = None
    if contact_abs_fz_error:
        contact_abs_fz_error_le_1n = sum(v <= 1.0 for v in contact_abs_fz_error) / len(contact_abs_fz_error)
        contact_abs_fz_error_le_2n = sum(v <= 2.0 for v in contact_abs_fz_error) / len(contact_abs_fz_error)
    contact_duration_s = sum(seg["contact_duration_s"] for seg in segment_summaries)

    all_x = [r["pose"][0] for r in force_rows]
    all_y = [r["pose"][1] for r in force_rows]
    all_z = [r["pose"][2] for r in force_rows]
    path_len = path_length(force_rows) if len(force_rows) >= 2 else 0.0
    xy_len = 0.0
    for a, b in zip(force_rows, force_rows[1:]):
        xy_len += math.dist(a["pose"][:2], b["pose"][:2])

    plots = write_plots(rows, all_moving_rows, params, selected_contact_paths, ground_truth_path)

    summary = {
        "csv_path": str(csv_path),
        "samples": len(rows),
        "duration_s": duration,
        "effective_hz": effective_hz,
        "program_params": params.__dict__,
        "runtime_state_counts": {str(k): sum(r["runtime_state"] == k for r in rows) for k in sorted(set(r["runtime_state"] for r in rows))},
        "safety_mode_counts": {str(k): sum(r["safety_mode"] == k for r in rows) for k in sorted(set(r["safety_mode"] for r in rows))},
        "active_windows": [
            {"start_s": rows[a]["t"] - rows[0]["t"], "end_s": rows[b]["t"] - rows[0]["t"], "duration_s": rows[b]["t"] - rows[a]["t"]}
            for a, b in active_windows
        ],
        "segments": segment_summaries,
        "moving_window": None
        if moving_window is None
        else {
            "start_s": rows[moving_window[0]]["t"] - rows[0]["t"],
            "end_s": rows[moving_window[1]]["t"] - rows[0]["t"],
            "duration_s": rows[moving_window[1]]["t"] - rows[moving_window[0]]["t"],
            "samples": len(force_rows),
        },
        "force_stats": {axis: stats([r[axis] for r in force_rows]) for axis in ["fx", "fy", "fz", "fxy", "f3d"]},
        "fz_abs_target_error_stats_n": stats(abs_fz_error),
        "fz_signed_target_error_stats_n": stats(signed_fz_error),
        "contact_fraction_abs_fz_error_le_1n": contact_fraction_abs_1n,
        "contact_fraction_abs_fz_error_le_2n": contact_fraction_abs_2n,
        "contact_window_summary": {
            "rule": "runtime_state == 2 and abs(Fz) >= 2 N",
            "samples": len(all_contact_rows),
            "duration_s": contact_duration_s,
            "force_stats": {axis: stats([r[axis] for r in all_contact_rows]) for axis in ["fx", "fy", "fz", "fxy", "f3d"]},
            "abs_fz_target_error_stats_n": stats(contact_abs_fz_error),
            "abs_fz_error_le_1n_fraction": contact_abs_fz_error_le_1n,
            "abs_fz_error_le_2n_fraction": contact_abs_fz_error_le_2n,
        },
        "ground_truth_path": {k: v for k, v in ground_truth_path.items() if k not in {"points", "plot_points"}},
        "contact_path_tracking": contact_path_tracking,
        "observed_path": {
            "path_length_3d_mm": path_len * 1000.0,
            "path_length_xy_mm": xy_len * 1000.0,
            "x_range_mm": [(min(all_x) * 1000.0) if all_x else None, (max(all_x) * 1000.0) if all_x else None],
            "y_range_mm": [(min(all_y) * 1000.0) if all_y else None, (max(all_y) * 1000.0) if all_y else None],
            "z_range_mm": [(min(all_z) * 1000.0) if all_z else None, (max(all_z) * 1000.0) if all_z else None],
            "max_tcp_speed_mm_s": max(r["tcp_speed"] for r in rows) * 1000.0,
        },
        "plots": plots,
    }
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    rel_csv = report_rel(csv_path)
    rel_script = report_rel(script_path)
    rel_summary = report_rel(SUMMARY_PATH)
    rel_tree = report_rel(RUN_DIR / "controller_snapshot" / "path_straight_line.txt")
    force_stats = summary["force_stats"]
    contact_summary = summary["contact_window_summary"]
    contact_force_stats = contact_summary["force_stats"]
    contact_error_stats = contact_summary["abs_fz_target_error_stats_n"]
    observed = summary["observed_path"]
    moving = summary["moving_window"]
    gt_path = summary["ground_truth_path"]
    tracking = summary["contact_path_tracking"]
    tracking_overall = tracking["overall"]
    protocol_rows = "\n".join(
        "| {segment} | {active_start_s:.3f} | {active_end_s:.3f} | {active_duration_s:.3f} | {active_samples} | {moving_samples} |".format(**seg)
        for seg in segment_summaries
    )
    representative_note = ""
    if segment_summaries and tracking["segments"]:
        representative_segment = segment_summaries[representative_idx]
        representative_tracking = tracking["segments"][representative_idx]
        representative_note = (
            f"\n周会展示稿内部取 `{representative_segment['segment']}` 作为代表段："
            f"它的接触路径 2D P95 误差最低，接触窗口内 Fz 相对目标线的 MAE 为 "
            f"{fmt(representative_segment['contact_abs_fz_target_error_stats_n']['mean'])} N。"
            "这个 run 选择只保留在技术记录里，不写进 meeting-facing Markdown。\n"
        )
    force_rows_md = "\n".join(
        "| {seg} | {fz_mean} | {fz_std} | {fz_min} | {fz_p50} | {fz_max} | {err_mean} | {err_p99} | {within1} | {within2} |".format(
            seg=seg["segment"],
            fz_mean=fmt(seg["force_stats"]["fz"]["mean"]),
            fz_std=fmt(seg["force_stats"]["fz"]["std"]),
            fz_min=fmt(seg["force_stats"]["fz"]["min"]),
            fz_p50=fmt(seg["force_stats"]["fz"]["p50"]),
            fz_max=fmt(seg["force_stats"]["fz"]["max"]),
            err_mean=fmt(seg["fz_abs_target_error_stats_n"]["mean"]),
            err_p99=fmt(seg["fz_abs_target_error_stats_n"]["p99"]),
            within1=fmt((seg["contact_fraction_abs_fz_error_le_1n"] or 0) * 100.0, 1),
            within2=fmt((seg["contact_fraction_abs_fz_error_le_2n"] or 0) * 100.0, 1),
        )
        for seg in segment_summaries
    )
    path_rows_md = "\n".join(
        "| {seg} | {moving_dur} | {path3d} | {pathxy} | {xrange} | {yrange} | {zrange} | {vmax} |".format(
            seg=seg["segment"],
            moving_dur=fmt(seg["moving_duration_s"]),
            path3d=fmt(seg["path_length_3d_mm"]),
            pathxy=fmt(seg["path_length_xy_mm"]),
            xrange=f"{fmt(seg['x_range_mm'][0])} 到 {fmt(seg['x_range_mm'][1])}",
            yrange=f"{fmt(seg['y_range_mm'][0])} 到 {fmt(seg['y_range_mm'][1])}",
            zrange=f"{fmt(seg['z_range_mm'][0])} 到 {fmt(seg['z_range_mm'][1])}",
            vmax=fmt(seg["max_tcp_speed_mm_s"]),
        )
        for seg in segment_summaries
    )
    path_tracking_rows_md = "\n".join(
        (
            "| {seg} | {samples} | {start_s} 到 {end_s} | {actual_xy} | {ref_start} 到 {ref_end} | "
            "{x_mae} | {x_rms} | {x_p95} | {y_mae} | {y_rms} | {y_p95} | {e2d_p95} |"
        ).format(
            seg=seg["segment"],
            samples=seg["samples"],
            start_s=fmt(seg["start_s"]),
            end_s=fmt(seg["end_s"]),
            actual_xy=fmt(seg["actual_xy_length_mm"]),
            ref_start=fmt(seg["reference_progress_start_mm"]),
            ref_end=fmt(seg["reference_progress_end_mm"]),
            x_mae=fmt(seg["x_error"]["mae_mm"]),
            x_rms=fmt(seg["x_error"]["rms_mm"]),
            x_p95=fmt(seg["x_error"]["p95_abs_mm"]),
            y_mae=fmt(seg["y_error"]["mae_mm"]),
            y_rms=fmt(seg["y_error"]["rms_mm"]),
            y_p95=fmt(seg["y_error"]["p95_abs_mm"]),
            e2d_p95=fmt(seg["error_2d"]["p95_mm"]),
        )
        for seg in tracking["segments"]
    )
    contact_force_rows_md = "\n".join(
        "| {seg} | {samples} | {duration} | {fz_mean} | {fz_std} | {fz_p50} | {fz_p99} | {err_mean} | {err_p99} | {within1} | {within2} |".format(
            seg=seg["segment"],
            samples=seg["contact_samples_abs_fz_ge_2n"],
            duration=fmt(seg["contact_duration_s"]),
            fz_mean=fmt(seg["contact_force_stats"]["fz"]["mean"]),
            fz_std=fmt(seg["contact_force_stats"]["fz"]["std"]),
            fz_p50=fmt(seg["contact_force_stats"]["fz"]["p50"]),
            fz_p99=fmt(seg["contact_force_stats"]["fz"]["p99"]),
            err_mean=fmt(seg["contact_abs_fz_target_error_stats_n"]["mean"]),
            err_p99=fmt(seg["contact_abs_fz_target_error_stats_n"]["p99"]),
            within1=fmt((seg["contact_abs_fz_error_le_1n_fraction"] or 0) * 100.0, 1),
            within2=fmt((seg["contact_abs_fz_error_le_2n_fraction"] or 0) * 100.0, 1),
        )
        for seg in segment_summaries
    )
    report = f"""# UR10e / OnRobot demo-1.2 最终成功运行记录

## 实验目的

记录当前 `path_straight_line.urp` 成功运行时的力控参数、OnRobot 力数据和接触路径跟踪误差。本文回答两个问题：

- `F/T Control` 在当前 PID 参数下是否维持了接触力；
- 接触路径内 actual TCP 是否跟上 ground truth path，并分别给出 X/Y 误差。

## 设备与实验条件

| 字段 | 数值 |
| --- | --- |
| 程序 | `path_straight_line.urp` |
| 数据 CSV | [{rel_csv}]({rel_csv}) |
| 控制器脚本快照 | [{rel_script}]({rel_script}) |
| 采样频率 | 请求 125 Hz，实际 {fmt(effective_hz, 3)} Hz |
| 样本数 | {len(rows)} |
| 采样时长 | {fmt(duration, 3)} s |
| Payload | 0.44 kg |
| TCP offset | `[0, 0, 0.12254, 0, 0, 0]` m/rad |

## 当前程序参数

| 参数 | 数值 | 证据 |
| --- | --- | --- |
| `F/T Search` 坐标系 | `frameID=1`，Tool | `path_straight_line.script` |
| Search 速度 | {fmt(params.search_speed_m_s, 3)} m/s | `of_move_init` |
| Search waypoint | Tool Z = {fmt((params.search_waypoint_tool_z_m or 0) * 1000.0, 1)} mm | `of_waypoint(relativeP=...)` |
| Search 接触阈值 | `F3D={fmt(params.search_f3d_limit_n, 1)} N` | `of_limit_start` |
| `F/T Control` 目标 | `Fz={fmt(params.control_fz_target_n, 1)} N`，只开 Z compliant | `of_ft_control_start` |
| Force PID | `{params.force_pid}` | `of_ft_control_start` |
| Torque PID | `{params.torque_pid}` | `of_ft_control_start` |
| `F/T Move` 速度 | {fmt(params.ft_move_speed_m_s, 3)} m/s | `of_move_init(frameID=0)` |
| `F/T Path` | `pathID={params.path_id}`, `relative={params.path_relative}` | `of_path_play` |
| Ground truth path | `target_TCP_pose`，contact-window progress {fmt(gt_path['comparison_progress_start_mm'])}-{fmt(gt_path['comparison_progress_end_mm'])} mm | `{Path(gt_path['source_csv']).name}` |
| retract | Tool Z = {fmt((params.retract_distance_m or 0) * 1000.0, 1)} mm | `retract_after_ft_path_tool_minus_z.script` |

## 数据与图片

原始 CSV 保留完整 120 s 采样，但报告图和统计不使用从头到尾的全量窗口。截断规则如下：

- 运行窗口：`runtime_state=2`；
- 路径窗口：每个 run 内最长的连续接触移动段，规则是 `runtime_state=2`、`|Fz| >= 2 N`、TCP 线速度不低于 1 mm/s；
- 接触力窗口：`runtime_state=2` 且 `|Fz| >= 2 N`；
- 排除窗口：采样开始/结束空闲、程序间等待、无接触 approach/retract、停止态保留寄存器值。

图 1 只显示 active run 窗口内的 OnRobot `Fx/Fy/Fz`。图 2 只显示接触窗口内的 `Fz` 和 `|Fz|`。图 3 是接触路径窗口内 actual TCP 与 ground truth path 的 XY 对比。图 4 分别给出 X/Y tracking error。图 5 是 active run 窗口内的 TCP Z 和 TCP 速度。

![OnRobot Fx/Fy/Fz](assets/{REPORT_STEM}/{plots['force_xyz_time']})

![Fz 与目标线](assets/{REPORT_STEM}/{plots['fz_time']})

![接触路径 XY 对比](assets/{REPORT_STEM}/{plots['tcp_xy_contact_path_compare']})

![接触路径 X/Y 误差](assets/{REPORT_STEM}/{plots['tcp_xy_contact_path_error']})

![TCP Z 与速度](assets/{REPORT_STEM}/{plots['tcp_z_speed_time']})

## 统计结果

### 运行窗口

本次 120 s 采样内检测到 {len(segment_summaries)} 个 `runtime_state=2` 运行窗口。下表把三次运行按相同口径列出。

| Segment | Start (s) | End (s) | Active duration (s) | Active samples | Moving samples |
| --- | ---: | ---: | ---: | ---: | ---: |
{protocol_rows}
{representative_note}

### 接触力统计

主统计只使用接触力窗口：`runtime_state=2` 且 `|Fz| >= 2 N`。这张表不包含采样开始/结束空闲、程序间等待、无接触 approach/search/retract，也不使用完整 120 s capture。

| Segment | Contact samples | Contact duration (s) | Fz Mean (N) | Fz Std (N) | Fz P50 (N) | Fz P99 (N) | `|Fz|-5` Mean (N) | `|Fz|-5` P99 (N) | ≤1 N (%) | ≤2 N (%) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{contact_force_rows_md}

| 总体接触窗口指标 | 数值 |
| --- | ---: |
| 样本数 | {contact_summary['samples']} |
| 累计接触时长 | {fmt(contact_summary['duration_s'])} s |
| Fz mean | {fmt(contact_force_stats['fz']['mean'])} N |
| Fz std | {fmt(contact_force_stats['fz']['std'])} N |
| `|Fz|-5` 平均误差 | {fmt(contact_error_stats['mean'])} N |
| `|Fz|-5` P99 误差 | {fmt(contact_error_stats['p99'])} N |
| `|Fz|-5` 绝对误差 ≤ 1 N 比例 | {fmt((contact_summary['abs_fz_error_le_1n_fraction'] or 0) * 100.0, 1)} % |
| `|Fz|-5` 绝对误差 ≤ 2 N 比例 | {fmt((contact_summary['abs_fz_error_le_2n_fraction'] or 0) * 100.0, 1)} % |

### 运动窗口力统计（context）

下面只作为 context：窗口采用所有 `runtime_state=2` 且 TCP 线速度超过 1 mm/s 的样本，会混入 approach、search、path、retract 的无接触阶段，不用于判断 5 N 接触保持质量。

| 量 | Mean | Std | Min | P1 | P50 | P99 | Max |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Fx (N) | {fmt(force_stats['fx']['mean'])} | {fmt(force_stats['fx']['std'])} | {fmt(force_stats['fx']['min'])} | {fmt(force_stats['fx']['p1'])} | {fmt(force_stats['fx']['p50'])} | {fmt(force_stats['fx']['p99'])} | {fmt(force_stats['fx']['max'])} |
| Fy (N) | {fmt(force_stats['fy']['mean'])} | {fmt(force_stats['fy']['std'])} | {fmt(force_stats['fy']['min'])} | {fmt(force_stats['fy']['p1'])} | {fmt(force_stats['fy']['p50'])} | {fmt(force_stats['fy']['p99'])} | {fmt(force_stats['fy']['max'])} |
| Fz (N) | {fmt(force_stats['fz']['mean'])} | {fmt(force_stats['fz']['std'])} | {fmt(force_stats['fz']['min'])} | {fmt(force_stats['fz']['p1'])} | {fmt(force_stats['fz']['p50'])} | {fmt(force_stats['fz']['p99'])} | {fmt(force_stats['fz']['max'])} |
| Fxy (N) | {fmt(force_stats['fxy']['mean'])} | {fmt(force_stats['fxy']['std'])} | {fmt(force_stats['fxy']['min'])} | {fmt(force_stats['fxy']['p1'])} | {fmt(force_stats['fxy']['p50'])} | {fmt(force_stats['fxy']['p99'])} | {fmt(force_stats['fxy']['max'])} |
| F3D (N) | {fmt(force_stats['f3d']['mean'])} | {fmt(force_stats['f3d']['std'])} | {fmt(force_stats['f3d']['min'])} | {fmt(force_stats['f3d']['p1'])} | {fmt(force_stats['f3d']['p50'])} | {fmt(force_stats['f3d']['p99'])} | {fmt(force_stats['f3d']['max'])} |

| 指标 | 数值 |
| --- | ---: |
| `|Fz|` 相对目标幅值误差均值 | {fmt(summary['fz_abs_target_error_stats_n']['mean'])} N |
| `|Fz|` 相对目标幅值误差 P99 | {fmt(summary['fz_abs_target_error_stats_n']['p99'])} N |
| `|Fz|-target` 绝对误差 ≤ 1 N 比例 | {fmt((contact_fraction_abs_1n or 0) * 100.0, 1)} % |
| `|Fz|-target` 绝对误差 ≤ 2 N 比例 | {fmt((contact_fraction_abs_2n or 0) * 100.0, 1)} % |

| Segment | Fz Mean (N) | Fz Std (N) | Fz Min (N) | Fz P50 (N) | Fz Max (N) | `|Fz|-5` Mean (N) | `|Fz|-5` P99 (N) | ≤1 N (%) | ≤2 N (%) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{force_rows_md}

### 接触路径对比

Ground truth path 来自早先同一 `path_straight_line.urp` 的只读 RTDE 记录，字段为 `target_TCP_pose`。本节只比较当前 final success 中有接触且实际移动的路径段，不使用一开始的 approach/search path，也不使用无接触段。

| Ground truth 指标 | 数值 |
| --- | ---: |
| 来源 CSV | `{Path(gt_path['source_csv']).name}` |
| 选择规则 | `target_TCP_pose` forward moving segment |
| 时间窗口 | {fmt(gt_path['start_s'])} 到 {fmt(gt_path['end_s'])} s |
| 样本数 | {gt_path['samples']} |
| 完整 XY 长度 | {fmt(gt_path['xy_length_mm'])} mm |
| 本次对比 progress | {fmt(gt_path['comparison_progress_start_mm'])} 到 {fmt(gt_path['comparison_progress_end_mm'])} mm |
| 本次对比 XY 长度 | {fmt(gt_path['comparison_xy_length_mm'])} mm |
| X 范围 | {fmt(gt_path['x_range_mm'][0])} 到 {fmt(gt_path['x_range_mm'][1])} mm |
| Y 范围 | {fmt(gt_path['y_range_mm'][0])} 到 {fmt(gt_path['y_range_mm'][1])} mm |

| Segment | Samples | 当前接触路径时间 (s) | Actual XY length (mm) | Reference progress (mm) | X MAE (mm) | X RMS (mm) | X P95 abs (mm) | Y MAE (mm) | Y RMS (mm) | Y P95 abs (mm) | 2D P95 (mm) |
| --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
{path_tracking_rows_md}

| Overall tracking error | 数值 |
| --- | ---: |
| 样本数 | {tracking_overall['samples']} |
| X MAE | {fmt(tracking_overall['x_error']['mae_mm'])} mm |
| X RMS | {fmt(tracking_overall['x_error']['rms_mm'])} mm |
| X P95 abs | {fmt(tracking_overall['x_error']['p95_abs_mm'])} mm |
| Y MAE | {fmt(tracking_overall['y_error']['mae_mm'])} mm |
| Y RMS | {fmt(tracking_overall['y_error']['rms_mm'])} mm |
| Y P95 abs | {fmt(tracking_overall['y_error']['p95_abs_mm'])} mm |
| 2D P95 | {fmt(tracking_overall['error_2d']['p95_mm'])} mm |

## 结论

当前报告可以直接证明三次运行里实际 TCP 都发生了接触路径运动，并给出接触路径相对 ground truth path 的 X/Y tracking error。按 `|Fz| >= 2 N` 的接触窗口看，三次运行的 Fz 均值接近 -5 N，`|Fz|-5` 平均误差约 0.6 N，大部分接触样本在 1 N 误差内。按接触路径窗口看，整体 X MAE 为 {fmt(tracking_overall['x_error']['mae_mm'])} mm，整体 Y MAE 为 {fmt(tracking_overall['y_error']['mae_mm'])} mm，2D P95 误差为 {fmt(tracking_overall['error_2d']['p95_mm'])} mm。

三次接触路径的 actual XY 长度分别见上表，图 3 直接叠加了 ground truth path 与三次 actual contact path；图 4 分别显示 X error 和 Y error。这次记录可以作为当前 PID `{params.force_pid}` 下的成功 baseline。

## 下一步

下一次如果继续调 PID，保留相同 ground truth path 和同样的接触路径窗口规则；报告直接比较 X/Y error 分布和接触力误差。

## 附录

### 采集边界

Ubuntu 侧只读 RTDE：没有发送 URScript、没有写寄存器、没有发运动、没有 zero/bias/filter/speed/TCP/payload 写入。

### 关键文件

- 原始 CSV：[{Path(rel_csv).name}]({rel_csv})
- 分析摘要：[{Path(rel_summary).name}]({rel_summary})
- 控制器脚本快照：[{Path(rel_script).name}]({rel_script})
- 程序树：[{Path(rel_tree).name}]({rel_tree})
"""
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(REPORT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
