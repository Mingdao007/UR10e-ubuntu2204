#!/usr/bin/env python3
"""Offline replay of the v20 detached orientation controller.

This is P0-a for the v21 recovery path. It reads a recorded v20 bridge CSV,
replays stage 25.2 from the recorded entry pose and locked normal, and compares
the old command convention against the corrected full-axis convention.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Iterable


DEFAULT_CSV = Path(
    "/home/andy/ur10e_ros2_ws/experiments/kunwei/closed-loop-straight-line/"
    "2026-06-04/runs/bridge_step4e_line_outerloop_v20_autowatch_20260609_174428/"
    "bridge_rtde_500hz.csv"
)


def clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


def dot3(a: Iterable[float], b: Iterable[float]) -> float:
    av = list(a)
    bv = list(b)
    return av[0] * bv[0] + av[1] * bv[1] + av[2] * bv[2]


def cross3(a: Iterable[float], b: Iterable[float]) -> tuple[float, float, float]:
    av = list(a)
    bv = list(b)
    return (
        av[1] * bv[2] - av[2] * bv[1],
        av[2] * bv[0] - av[0] * bv[2],
        av[0] * bv[1] - av[1] * bv[0],
    )


def norm3(values: Iterable[float]) -> float:
    return math.sqrt(dot3(values, values))


def normalize3(values: Iterable[float]) -> tuple[float, float, float]:
    vals = list(values)
    length = norm3(vals)
    if length < 1e-12:
        return (0.0, 0.0, 1.0)
    return (vals[0] / length, vals[1] / length, vals[2] / length)


def matmul3(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [
        [sum(a[row][idx] * b[idx][col] for idx in range(3)) for col in range(3)]
        for row in range(3)
    ]


def rotvec_to_matrix(rx: float, ry: float, rz: float) -> list[list[float]]:
    theta = math.sqrt(rx * rx + ry * ry + rz * rz)
    if theta < 1e-12:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    kx, ky, kz = rx / theta, ry / theta, rz / theta
    c = math.cos(theta)
    s = math.sin(theta)
    v = 1.0 - c
    return [
        [c + kx * kx * v, kx * ky * v - kz * s, kx * kz * v + ky * s],
        [ky * kx * v + kz * s, c + ky * ky * v, ky * kz * v - kx * s],
        [kz * kx * v - ky * s, kz * ky * v + kx * s, c + kz * kz * v],
    ]


def tcp_z_axis(matrix: list[list[float]]) -> tuple[float, float, float]:
    return (matrix[0][2], matrix[1][2], matrix[2][2])


def orientation_error(matrix: list[list[float]], normal: tuple[float, float, float]) -> float:
    axis = cross3(tcp_z_axis(matrix), normal)
    return math.asin(clamp(norm3(axis), -1.0, 1.0))


def command_for(
    matrix: list[list[float]],
    normal: tuple[float, float, float],
    gain: float,
    angular_limit: float,
    wy_sign: float,
    consume_wz: bool,
) -> tuple[float, float, float]:
    axis = cross3(tcp_z_axis(matrix), normal)
    cmd = (gain * axis[0], gain * wy_sign * axis[1], gain * axis[2] if consume_wz else 0.0)
    norm = norm3(cmd)
    if norm > angular_limit:
        scale = angular_limit / norm
        cmd = (cmd[0] * scale, cmd[1] * scale, cmd[2] * scale)
    return cmd


def replay(
    rows: list[dict[str, float]],
    wy_sign: float,
    consume_wz: bool,
    extend_to_s: float | None,
) -> dict[str, object]:
    first = rows[0]
    normal = normalize3(
        (
            first["_step4e_latched_normal_b_x"],
            first["_step4e_latched_normal_b_y"],
            first["_step4e_latched_normal_b_z"],
        )
    )
    matrix = rotvec_to_matrix(
        first["ur_actual_TCP_pose_3"],
        first["ur_actual_TCP_pose_4"],
        first["ur_actual_TCP_pose_5"],
    )
    errors = [orientation_error(matrix, normal)]
    cmd_norms: list[float] = []
    elapsed_s = 0.0
    observed_dt: list[float] = []
    for previous, current in zip(rows, rows[1:]):
        dt_s = max(0.0, current["t_monotonic_s"] - previous["t_monotonic_s"])
        if dt_s > 0.0:
            observed_dt.append(dt_s)
        elapsed_s += dt_s
        cmd = command_for(matrix, normal, 0.20, 0.10, wy_sign, consume_wz)
        cmd_norms.append(norm3(cmd))
        delta = rotvec_to_matrix(cmd[0] * dt_s, cmd[1] * dt_s, cmd[2] * dt_s)
        matrix = matmul3(delta, matrix)
        errors.append(orientation_error(matrix, normal))
    recorded_exit_error = errors[-1]
    if extend_to_s is not None and elapsed_s < extend_to_s:
        dt_s = sorted(observed_dt)[len(observed_dt) // 2] if observed_dt else 0.002
        while elapsed_s + 1e-12 < extend_to_s:
            step = min(dt_s, extend_to_s - elapsed_s)
            cmd = command_for(matrix, normal, 0.20, 0.10, wy_sign, consume_wz)
            cmd_norms.append(norm3(cmd))
            delta = rotvec_to_matrix(cmd[0] * step, cmd[1] * step, cmd[2] * step)
            matrix = matmul3(delta, matrix)
            elapsed_s += step
            errors.append(orientation_error(matrix, normal))
    deltas = [b - a for a, b in zip(errors, errors[1:])]
    return {
        "rows": len(rows),
        "wy_sign": wy_sign,
        "consume_wz": consume_wz,
        "entry_error_rad": errors[0],
        "recorded_exit_error_rad": recorded_exit_error,
        "exit_error_rad": errors[-1],
        "elapsed_s": elapsed_s,
        "extended_to_s": extend_to_s,
        "min_error_rad": min(errors),
        "max_error_rad": max(errors),
        "strictly_decreasing_fraction": sum(1 for value in deltas if value < 0.0) / len(deltas),
        "strictly_increasing_fraction": sum(1 for value in deltas if value > 0.0) / len(deltas),
        "mean_cmd_norm_rad_s": sum(cmd_norms) / len(cmd_norms),
        "normal_b": normal,
    }


def read_stage_rows(csv_path: Path) -> list[dict[str, float]]:
    cols = [
        "t_monotonic_s",
        "ur_output_double_register_35",
        "ur_actual_TCP_pose_3",
        "ur_actual_TCP_pose_4",
        "ur_actual_TCP_pose_5",
        "_step4e_latched_normal_b_x",
        "_step4e_latched_normal_b_y",
        "_step4e_latched_normal_b_z",
    ]
    rows: list[dict[str, float]] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                parsed = {col: float(row[col]) for col in cols}
            except (KeyError, ValueError):
                continue
            if abs(parsed["ur_output_double_register_35"] - 25.2) < 0.05:
                rows.append(parsed)
    if len(rows) < 2:
        raise SystemExit(f"not enough v20 stage 25.2 rows in {csv_path}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--extend-to-s", type=float, default=8.0)
    args = parser.parse_args()
    rows = read_stage_rows(args.csv)
    extend_to_s = args.extend_to_s if args.extend_to_s > 0.0 else None
    result = {
        "csv": str(args.csv),
        "stage": 25.2,
        "old_v20_wy_minus_wz_zero": replay(rows, wy_sign=-1.0, consume_wz=False, extend_to_s=extend_to_s),
        "corrected_full_axis": replay(rows, wy_sign=1.0, consume_wz=True, extend_to_s=extend_to_s),
        "acceptance": {
            "corrected_exit_error_at_8s_lt_0p05": None,
            "corrected_monotonic_and_exit_lt_0p055": None,
            "old_replays_divergence_gt_0p60": None,
        },
    }
    result["acceptance"]["corrected_exit_error_at_8s_lt_0p05"] = (
        result["corrected_full_axis"]["exit_error_rad"] < 0.05
    )
    result["acceptance"]["old_replays_divergence_gt_0p60"] = (
        result["old_v20_wy_minus_wz_zero"]["exit_error_rad"] > 0.60
    )
    result["acceptance"]["corrected_monotonic_and_exit_lt_0p055"] = (
        result["corrected_full_axis"]["strictly_decreasing_fraction"] == 1.0
        and result["corrected_full_axis"]["exit_error_rad"] < 0.055
    )
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    practical_gate = (
        result["acceptance"]["corrected_monotonic_and_exit_lt_0p055"]
        and result["acceptance"]["old_replays_divergence_gt_0p60"]
    )
    return 0 if practical_gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
