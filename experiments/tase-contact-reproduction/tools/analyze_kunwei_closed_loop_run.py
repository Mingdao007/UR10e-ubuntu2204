#!/usr/bin/env python3
"""Analyze a Kunwei closed-loop straight-line run."""

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


def xy_error_mm(rows: list[dict[str, str]], ux: float, uy: float) -> list[float]:
    if not rows:
        return []
    x0 = as_float(rows[0], "ur_actual_TCP_pose_0")
    y0 = as_float(rows[0], "ur_actual_TCP_pose_1")
    if x0 is None or y0 is None:
        return []
    errors: list[float] = []
    for row in rows:
        x = as_float(row, "ur_actual_TCP_pose_0")
        y = as_float(row, "ur_actual_TCP_pose_1")
        if x is None or y is None:
            continue
        dx = x - x0
        dy = y - y0
        progress = dx * ux + dy * uy
        perp_x = dx - progress * ux
        perp_y = dy - progress * uy
        errors.append(math.sqrt(perp_x * perp_x + perp_y * perp_y) * 1000.0)
    return errors


def selected_rows(
    rows: list[dict[str, str]],
    progress_offset_m: float,
    progress_start_m: float,
    progress_end_m: float,
    min_speed_m_s: float,
) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for row in rows:
        runtime_state = as_float(row, "ur_runtime_state")
        if runtime_state is not None and int(runtime_state) != 2:
            continue
        progress = as_float(row, "ur_output_double_register_31")
        if progress is not None:
            absolute_progress = progress + progress_offset_m
            if absolute_progress < progress_start_m or absolute_progress > progress_end_m:
                continue
        norm = speed_norm(row)
        if norm is not None and norm < min_speed_m_s:
            continue
        selected.append(row)
    return selected


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    config = load_json(args.config)
    rows = read_rows(args.bridge_csv)
    line = config["reference_line"]
    ux, uy = line["xy_unit_vector"]
    selected = selected_rows(
        rows,
        args.progress_offset_m,
        args.progress_start_m,
        args.progress_end_m,
        args.min_tcp_speed_m_s,
    )

    normal = [value for row in selected if (value := as_float(row, "normal_force_n")) is not None]
    force_norm = [value for row in selected if (value := as_float(row, "force_norm_n")) is not None]
    torque_norm = [value for row in selected if (value := as_float(row, "torque_norm_nm")) is not None]
    target_values = [value for row in selected if (value := as_float(row, "target_force_n")) is not None]
    target = args.target_force_n if args.target_force_n is not None else (statistics.fmean(target_values) if target_values else None)
    abs_target_error = [abs(abs(value) - target) for value in normal] if target is not None else []
    signed_target_error = [value - target for value in normal] if target is not None else []
    xy_errors = xy_error_mm(selected, ux, uy)
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
        "samples_selected": len(selected),
        "selection": {
            "runtime_state": 2,
            "progress_offset_m": args.progress_offset_m,
            "progress_start_m": args.progress_start_m,
            "progress_end_m": args.progress_end_m,
            "min_tcp_speed_m_s": args.min_tcp_speed_m_s,
        },
        "target_force_n": target,
        "normal_force_n": stats(normal),
        "force_norm_n": stats(force_norm),
        "torque_norm_nm": stats(torque_norm),
        "abs_normal_target_error_n": stats(abs_target_error),
        "signed_normal_target_error_n": stats(signed_target_error),
        "xy_path_error_mm": stats(xy_errors),
        "stop_reason_counts": dict(stop_reasons),
        "stop_reason_codes": {
            "1": "path_complete",
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
        },
        "baseline_onrobot": config["baseline_onrobot"],
    }


def main(argv: list[str] | None = None) -> int:
    config = load_json(CONFIG_PATH)
    line = config["reference_line"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bridge_csv", type=Path)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--target-force-n", type=float)
    parser.add_argument("--progress-offset-m", type=float, default=line["contact_progress_start_m"])
    parser.add_argument("--progress-start-m", type=float, default=line["contact_progress_start_m"])
    parser.add_argument("--progress-end-m", type=float, default=line["contact_progress_end_m"])
    parser.add_argument("--min-tcp-speed-m-s", type=float, default=0.001)
    args = parser.parse_args(argv)

    summary = analyze(args)
    if args.output is None:
        args.output = args.bridge_csv.with_name("analysis_summary.json")
    args.output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["samples_selected"] > 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
