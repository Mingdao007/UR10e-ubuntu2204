#!/usr/bin/env python3
"""Analyze a Step2D circular Kunwei contact run."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = EXPERIMENT_ROOT / "config" / "straight_line_reference.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def as_float(row: dict[str, str], key: str) -> float | None:
    value = row.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"samples": 0}
    return {
        "samples": len(values),
        "mean": statistics.fmean(values),
        "std": statistics.stdev(values) if len(values) >= 2 else 0.0,
        "min": min(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values),
    }


def speed_norm(row: dict[str, str]) -> float | None:
    values = [as_float(row, f"ur_actual_TCP_speed_{idx}") for idx in range(3)]
    if any(value is None for value in values):
        return None
    return math.sqrt(sum(float(value) ** 2 for value in values))


def circle_config(config: dict[str, Any]) -> dict[str, float]:
    line = config["reference_line"]
    circle = config.get("step2d_circle", {})
    start_fraction = float(circle.get("diameter_start_fraction", 0.25))
    end_fraction = float(circle.get("diameter_end_fraction", 0.75))
    ux, uy = [float(v) for v in line["xy_unit_vector"]]
    length = float(line["xy_length_m"])
    radius = 0.5 * abs(end_fraction - start_fraction) * length
    return {
        "ux": ux,
        "uy": uy,
        "perp_x": -uy,
        "perp_y": ux,
        "radius_m": radius,
        "arc_length_m": 2.0 * math.pi * radius,
        "direction": 1.0 if float(circle.get("circle_direction", 1.0)) >= 0 else -1.0,
    }


def stage25_rows(rows: list[dict[str, str]], min_tcp_speed_m_s: float) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for row in rows:
        stage = as_float(row, "ur_output_double_register_35")
        if stage is None or abs(stage - 25.0) > 0.05:
            continue
        runtime_state = as_float(row, "ur_runtime_state")
        if runtime_state is not None and int(runtime_state) != 2:
            continue
        norm = speed_norm(row)
        if norm is not None and norm < min_tcp_speed_m_s:
            continue
        selected.append(row)
    return selected


def circle_errors(rows: list[dict[str, str]], circle: dict[str, float]) -> dict[str, Any]:
    if not rows:
        return {
            "radial_error_mm": {"samples": 0},
            "xy_tracking_error_mm": {"samples": 0},
            "closure_error_mm": None,
        }
    x0 = as_float(rows[0], "ur_actual_TCP_pose_0")
    y0 = as_float(rows[0], "ur_actual_TCP_pose_1")
    if x0 is None or y0 is None:
        return {
            "radial_error_mm": {"samples": 0},
            "xy_tracking_error_mm": {"samples": 0},
            "closure_error_mm": None,
        }
    ux = circle["ux"]
    uy = circle["uy"]
    e1x = circle["direction"] * circle["perp_x"]
    e1y = circle["direction"] * circle["perp_y"]
    radius = circle["radius_m"]
    cx = x0 + radius * ux
    cy = y0 + radius * uy
    radial_errors: list[float] = []
    tracking_errors: list[float] = []
    theta_values: list[float] = []
    for row in rows:
        x = as_float(row, "ur_actual_TCP_pose_0")
        y = as_float(row, "ur_actual_TCP_pose_1")
        progress = as_float(row, "ur_output_double_register_31")
        if x is None or y is None:
            continue
        dx = x - cx
        dy = y - cy
        radial_errors.append((math.sqrt(dx * dx + dy * dy) - radius) * 1000.0)
        if progress is None:
            continue
        theta = progress / radius if radius > 0 else 0.0
        theta_values.append(theta)
        desired_x = x0 + radius * (1.0 - math.cos(theta)) * ux + radius * math.sin(theta) * e1x
        desired_y = y0 + radius * (1.0 - math.cos(theta)) * uy + radius * math.sin(theta) * e1y
        tracking_errors.append(math.sqrt((x - desired_x) ** 2 + (y - desired_y) ** 2) * 1000.0)
    x_last = as_float(rows[-1], "ur_actual_TCP_pose_0")
    y_last = as_float(rows[-1], "ur_actual_TCP_pose_1")
    closure = None
    if x_last is not None and y_last is not None:
        closure = math.sqrt((x_last - x0) ** 2 + (y_last - y0) ** 2) * 1000.0
    return {
        "radius_m": radius,
        "derived_center_xy_m": [cx, cy],
        "theta_end_rad": theta_values[-1] if theta_values else None,
        "arc_progress_end_m": rows and as_float(rows[-1], "ur_output_double_register_31"),
        "radial_error_mm": stats([abs(v) for v in radial_errors]),
        "signed_radial_error_mm": stats(radial_errors),
        "xy_tracking_error_mm": stats(tracking_errors),
        "closure_error_mm": closure,
    }


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    config = load_json(args.config)
    circle = circle_config(config)
    rows = read_rows(args.bridge_csv)
    selected = stage25_rows(rows, args.min_tcp_speed_m_s)
    normal = [value for row in selected if (value := as_float(row, "normal_force_n")) is not None]
    force_norm = [value for row in selected if (value := as_float(row, "force_norm_n")) is not None]
    torque_norm = [value for row in selected if (value := as_float(row, "torque_norm_nm")) is not None]
    target_values = [value for row in selected if (value := as_float(row, "target_force_n")) is not None]
    target = args.target_force_n if args.target_force_n is not None else (statistics.fmean(target_values) if target_values else None)
    abs_target_error = [abs(abs(value) - target) for value in normal] if target is not None else []
    signed_target_error = [value + target for value in normal] if target is not None else []
    mx = [value for row in selected if (value := as_float(row, "mx_nm_zeroed")) is not None]
    my = [value for row in selected if (value := as_float(row, "my_nm_zeroed")) is not None]
    fx = [value for row in selected if (value := as_float(row, "fx_n_zeroed")) is not None]
    fy = [value for row in selected if (value := as_float(row, "fy_n_zeroed")) is not None]
    lateral_force = [math.sqrt(x * x + y * y) for x, y in zip(fx, fy)]
    stop_reasons = Counter(
        str(int(value))
        for row in rows
        if (value := as_float(row, "ur_output_double_register_30")) is not None
    )
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "bridge_csv": str(args.bridge_csv),
        "config": str(args.config),
        "samples_total": len(rows),
        "samples_stage25": len(selected),
        "selection": {
            "stage": 25.0,
            "runtime_state": 2,
            "min_tcp_speed_m_s": args.min_tcp_speed_m_s,
        },
        "circle": circle_errors(selected, circle),
        "target_force_n": target,
        "normal_force_n": stats(normal),
        "force_norm_n": stats(force_norm),
        "torque_norm_nm": stats(torque_norm),
        "abs_normal_target_error_n": stats(abs_target_error),
        "signed_normal_target_error_n": stats(signed_target_error),
        "lateral_force_n": stats(lateral_force),
        "mx_nm_zeroed": stats(mx),
        "my_nm_zeroed": stats(my),
        "stop_reason_counts": dict(stop_reasons),
        "stop_reason_codes": {
            "1": "circle_complete",
            "2": "heartbeat_stale",
            "3": "sensor_not_ok",
            "4": "external_stop_request",
            "5": "normal_force_guard",
            "6": "force_norm_guard",
            "7": "torque_norm_guard",
            "8": "normal_correction_guard",
            "9": "path_progress_guard",
            "10": "runtime_limit",
            "11": "contact_search_trigger",
            "12": "attitude_proxy_guard",
        },
        "baseline_step2c_v4": {
            "stage25_echo_hz": 489.8278793286306,
            "fz_mae_n": 1.8668028413172717,
            "xy_p95_mm": 0.08250615739360033,
            "note": "Use only as prior Step2C baseline; Step2D changes geometry and attitude compliance.",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bridge_csv", type=Path)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--target-force-n", type=float)
    parser.add_argument("--min-tcp-speed-m-s", type=float, default=0.001)
    args = parser.parse_args(argv)
    summary = analyze(args)
    if args.output is None:
        args.output = args.bridge_csv.with_name("step2d_circle_analysis_summary.json")
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["samples_stage25"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
