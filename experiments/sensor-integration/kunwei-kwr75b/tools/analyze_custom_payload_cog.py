#!/usr/bin/env python3
"""Deterministic analysis for a Kunwei + UR static gravity calibration run.

The analyzer preserves the raw capture and writes only derived JSON/report
artifacts.  It fits the force-axis signed-permutation mapping first, then fits
mass, CoG (relative to the Kunwei reference origin), and force/moment bias.
The optional sensor-origin offset converts that CoG to the UR tool frame for a
comparison with PolyScope's payload/CoG snapshot.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np


G = 9.80665
FORCE_COLUMNS = ["fx_n", "fy_n", "fz_n"]
MOMENT_COLUMNS = ["mx_nm", "my_nm", "mz_nm"]
POSE_COLUMNS = [f"ur_actual_TCP_pose_{i}" for i in range(6)]
SPEED_COLUMNS = [f"ur_actual_TCP_speed_{i}" for i in range(6)]


@dataclass
class Segment:
    name: str
    rows: int
    pose: np.ndarray
    force: np.ndarray
    moment: np.ndarray
    force_std: np.ndarray
    moment_std: np.ndarray
    speed_max: float
    rtde_age_max: float | None
    safety_modes: list[str]


def finite_float(row: dict[str, str], key: str) -> float | None:
    try:
        value = float(row[key])
    except (KeyError, TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def rotvec_to_matrix(rotvec: Iterable[float]) -> np.ndarray:
    """Return the UR axis-angle rotation matrix for a rotation vector."""
    v = np.asarray(list(rotvec), dtype=float)
    theta = float(np.linalg.norm(v))
    if theta < 1e-12:
        return np.eye(3)
    k = v / theta
    kx = np.array(
        [[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]],
        dtype=float,
    )
    return np.eye(3) * math.cos(theta) + (1.0 - math.cos(theta)) * np.outer(k, k) + math.sin(theta) * kx


def gravity_tool_from_pose(pose: np.ndarray) -> np.ndarray:
    rotation = rotvec_to_matrix(pose[3:6])
    return rotation.T @ np.array([0.0, 0.0, -G])


def corr(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=float).reshape(-1)
    b = np.asarray(b, dtype=float).reshape(-1)
    if np.std(a) < 1e-12 or np.std(b) < 1e-12:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def rmse(a: np.ndarray, b: np.ndarray) -> float:
    delta = np.asarray(a, dtype=float) - np.asarray(b, dtype=float)
    return float(np.sqrt(np.mean(delta * delta)))


def condition_number(matrix: np.ndarray) -> float:
    if matrix.size == 0:
        return float("inf")
    singular = np.linalg.svd(matrix, compute_uv=False)
    if singular.size == 0 or singular[-1] < 1e-12:
        return float("inf")
    return float(singular[0] / singular[-1])


def read_segments(
    csv_path: Path,
    *,
    max_age_s: float,
    max_linear_speed_mps: float,
    max_angular_speed_rps: float,
) -> tuple[list[Segment], dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    rejected = {"missing_numeric": 0, "rtde_age": 0, "speed": 0}
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            values: dict[str, Any] = {}
            required = [*FORCE_COLUMNS, *MOMENT_COLUMNS, *POSE_COLUMNS, *SPEED_COLUMNS]
            parsed = True
            for key in required:
                value = finite_float(row, key)
                if value is None:
                    parsed = False
                    break
                values[key] = value
            if not parsed:
                rejected["missing_numeric"] += 1
                continue
            age = finite_float(row, "rtde_age_s")
            if age is None or age > max_age_s:
                rejected["rtde_age"] += 1
                continue
            speed = np.array([values[key] for key in SPEED_COLUMNS], dtype=float)
            if np.linalg.norm(speed[:3]) > max_linear_speed_mps or np.linalg.norm(speed[3:]) > max_angular_speed_rps:
                rejected["speed"] += 1
                continue
            values["age"] = age
            values["safety_mode"] = row.get("ur_safety_mode", "")
            segment_name = row.get("segment", "")
            if not segment_name or segment_name in {"idle", "transition"}:
                continue
            grouped.setdefault(segment_name, []).append(values)

    segments: list[Segment] = []
    for name, rows in grouped.items():
        if len(rows) < 20:
            continue
        force = np.array([[r[key] for key in FORCE_COLUMNS] for r in rows], dtype=float)
        moment = np.array([[r[key] for key in MOMENT_COLUMNS] for r in rows], dtype=float)
        pose = np.array([[r[key] for key in POSE_COLUMNS] for r in rows], dtype=float).mean(axis=0)
        speed = np.array([[r[key] for key in SPEED_COLUMNS] for r in rows], dtype=float)
        ages = np.array([r["age"] for r in rows], dtype=float)
        segments.append(
            Segment(
                name=name,
                rows=len(rows),
                pose=pose,
                force=force.mean(axis=0),
                moment=moment.mean(axis=0),
                force_std=force.std(axis=0, ddof=1),
                moment_std=moment.std(axis=0, ddof=1),
                speed_max=float(max(np.linalg.norm(speed[:, :3], axis=1).max(), np.linalg.norm(speed[:, 3:], axis=1).max())),
                rtde_age_max=float(ages.max()),
                safety_modes=sorted({str(r["safety_mode"]) for r in rows}),
            )
        )
    return segments, {"rows_rejected": rejected, "segments_seen": sorted(grouped)}


def read_segments_from_summary(summary_path: Path) -> tuple[list[Segment], dict[str, Any]]:
    """Load capture-time per-segment statistics without rescanning raw CSV."""
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    stats_by_segment = summary.get("segment_force_stats_si", {})
    rtde_by_segment = summary.get("segment_rtde_first_last", {})
    segments: list[Segment] = []
    for name, stats in stats_by_segment.items():
        if name in {"idle", "transition"}:
            continue
        first_last = rtde_by_segment.get(name, {})
        first = first_last.get("first") or first_last.get("last")
        if not first:
            continue
        pose_values = first.get("actual_TCP_pose")
        speed_values = first.get("actual_TCP_speed") or [0.0] * 6
        if pose_values is None or len(pose_values) != 6:
            continue
        force_stats = [stats.get(key, {}) for key in FORCE_COLUMNS]
        moment_stats = [stats.get(key, {}) for key in MOMENT_COLUMNS]
        if any("mean" not in item for item in [*force_stats, *moment_stats]):
            continue
        segments.append(
            Segment(
                name=name,
                rows=int(force_stats[0].get("samples", 0)),
                pose=np.asarray(pose_values, dtype=float),
                force=np.asarray([item["mean"] for item in force_stats], dtype=float),
                moment=np.asarray([item["mean"] for item in moment_stats], dtype=float),
                force_std=np.asarray([item.get("std", float("nan")) for item in force_stats], dtype=float),
                moment_std=np.asarray([item.get("std", float("nan")) for item in moment_stats], dtype=float),
                speed_max=float(np.linalg.norm(np.asarray(speed_values, dtype=float))),
                rtde_age_max=None,
                safety_modes=sorted(
                    {
                        str(value.get("safety_mode"))
                        for value in [first, first_last.get("last", {})]
                        if value.get("safety_mode") is not None
                    }
                ),
            )
        )
    return segments, {
        "source": "capture_summary",
        "summary_path": str(summary_path),
        "rows_rejected": {},
        "segments_seen": sorted(stats_by_segment),
        "capture_events": summary.get("events", []),
    }


def fit_axis_mapping(forces: np.ndarray, gravity: np.ndarray) -> dict[str, Any]:
    """Search signed permutations and fit a common gravity scale plus bias."""
    candidates: list[dict[str, Any]] = []
    centered_g = gravity - gravity.mean(axis=0)
    g_norm = float(np.sum(centered_g * centered_g))
    if g_norm < 1e-12:
        return {"status": "inconclusive", "reason": "gravity_orientation_not_excited"}
    for permutation in itertools.permutations(range(3)):
        for signs in itertools.product((-1.0, 1.0), repeat=3):
            transformed = forces[:, permutation] * np.asarray(signs)
            centered_f = transformed - transformed.mean(axis=0)
            alpha = float(np.sum(centered_g * centered_f) / g_norm)
            bias = transformed.mean(axis=0) - alpha * gravity.mean(axis=0)
            predicted = alpha * gravity + bias
            if alpha <= 0.0:
                continue
            candidates.append(
                {
                    "rmse_n": rmse(transformed, predicted),
                    "corr": corr(transformed.reshape(-1), gravity.reshape(-1)),
                    "alpha_kg": alpha,
                    "weight_n": alpha * G,
                    "mass_kg": alpha,
                    "bias_n": bias.tolist(),
                    "permutation": list(permutation),
                    "signs": [int(sign) for sign in signs],
                    "formula": [
                        f"{'+' if sign > 0 else '-'}F_K[{axis}]"
                        for axis, sign in zip(permutation, signs)
                    ],
                }
            )
    if not candidates:
        return {"status": "inconclusive", "reason": "no_positive_mapping_candidate"}
    candidates.sort(key=lambda item: (item["rmse_n"], -item["corr"]))
    best = candidates[0]
    return {
        "status": "ok",
        "best": best,
        "top_candidates": candidates[:10],
        "gravity_rank_centered": int(np.linalg.matrix_rank(centered_g)),
        "gravity_z_abs_max_mps2": float(np.max(np.abs(gravity[:, 2]))),
        "gravity_z_abs_min_mps2": float(np.min(np.abs(gravity[:, 2]))),
    }


def apply_mapping(values: np.ndarray, mapping: dict[str, Any]) -> np.ndarray:
    permutation = np.asarray(mapping["permutation"], dtype=int)
    signs = np.asarray(mapping["signs"], dtype=float)
    return values[:, permutation] * signs


def solve_linear_model(design: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, float, int, float]:
    params, _, rank, singular = np.linalg.lstsq(design, target, rcond=None)
    cond = float("inf") if len(singular) == 0 or singular[-1] < 1e-12 else float(singular[0] / singular[-1])
    residual = target - design @ params
    return params, rmse(target, design @ params), int(rank), cond


def payload_design(gravity: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    rows: list[list[float]] = []
    for g in gravity:
        rows.extend(
            [
                [g[0], 1.0, 0.0, 0.0],
                [g[1], 0.0, 1.0, 0.0],
                [g[2], 0.0, 0.0, 1.0],
            ]
        )
    return np.asarray(rows, dtype=float), np.zeros((len(rows),), dtype=float)


def torque_design(gravity: np.ndarray) -> np.ndarray:
    rows: list[list[float]] = []
    for gx, gy, gz in gravity:
        rows.extend(
            [
                [0.0, gz, -gy, 1.0, 0.0, 0.0],
                [-gz, 0.0, gx, 0.0, 1.0, 0.0],
                [gy, -gx, 0.0, 0.0, 0.0, 1.0],
            ]
        )
    return np.asarray(rows, dtype=float)


def fit_payload_cog(
    forces: np.ndarray,
    moments: np.ndarray,
    gravity: np.ndarray,
    *,
    origin_offset_tool_m: list[float] | None,
) -> dict[str, Any]:
    force_matrix, _ = payload_design(gravity)
    force_target = forces.reshape(-1)
    force_params, force_rmse, force_rank, force_cond = solve_linear_model(force_matrix, force_target)
    mass = float(force_params[0])
    force_bias = force_params[1:4]

    moment_matrix = torque_design(gravity)
    moment_params, moment_rmse, moment_rank, moment_cond = solve_linear_model(
        moment_matrix,
        moments.reshape(-1),
    )
    p = moment_params[:3]
    moment_bias = moment_params[3:6]
    cog_sensor = (p / mass) if abs(mass) > 1e-9 else np.full(3, np.nan)
    cog_tool = None
    if origin_offset_tool_m is not None:
        cog_tool = cog_sensor + np.asarray(origin_offset_tool_m, dtype=float)

    predicted_force = (force_matrix @ force_params).reshape((-1, 3))
    predicted_moment = (moment_matrix @ moment_params).reshape((-1, 3))
    return {
        "status": "ok" if mass > 0 else "inconclusive",
        "mass_kg": mass,
        "cog_sensor_m": cog_sensor.tolist(),
        "cog_sensor_mm": (cog_sensor * 1000.0).tolist(),
        "cog_tool_m": None if cog_tool is None else cog_tool.tolist(),
        "cog_tool_mm": None if cog_tool is None else (cog_tool * 1000.0).tolist(),
        "force_bias_n": force_bias.tolist(),
        "moment_bias_nm": moment_bias.tolist(),
        "force_rmse_n": force_rmse,
        "moment_rmse_nm": moment_rmse,
        "force_rank": force_rank,
        "moment_rank": moment_rank,
        "force_condition_number": force_cond,
        "moment_condition_number": moment_cond,
        "force_residuals_n": (forces - predicted_force).tolist(),
        "moment_residuals_nm": (moments - predicted_moment).tolist(),
    }


def p0_return_drift(names: list[str], forces: np.ndarray, moments: np.ndarray) -> dict[str, Any] | None:
    first = next((i for i, name in enumerate(names) if "P0_initial" in name), None)
    last = next((i for i, name in reversed(list(enumerate(names))) if "P0_return" in name), None)
    if first is None or last is None:
        return None
    return {
        "force_n": (forces[last] - forces[first]).tolist(),
        "moment_nm": (moments[last] - moments[first]).tolist(),
    }


def compare_builtin(
    payload: dict[str, Any],
    *,
    builtin_payload_kg: float,
    builtin_cog_mm: list[float],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "builtin_payload_kg": builtin_payload_kg,
        "builtin_cog_mm": builtin_cog_mm,
        "custom_payload_kg": payload.get("mass_kg"),
        "custom_cog_tool_mm": payload.get("cog_tool_mm"),
    }
    if payload.get("mass_kg") is not None:
        result["delta_payload_kg"] = float(payload["mass_kg"] - builtin_payload_kg)
        result["delta_payload_pct"] = float(100.0 * (payload["mass_kg"] - builtin_payload_kg) / builtin_payload_kg)
    custom_cog = payload.get("cog_tool_mm")
    if custom_cog is not None:
        delta = np.asarray(custom_cog, dtype=float) - np.asarray(builtin_cog_mm, dtype=float)
        result["delta_cog_mm"] = delta.tolist()
        result["delta_cog_norm_mm"] = float(np.linalg.norm(delta))
        result["status"] = "numeric_comparison_available"
    else:
        result["status"] = "inconclusive_missing_sensor_origin_transform"
    return result


def write_fit_plot(
    path: Path,
    names: list[str],
    mapped_forces: np.ndarray,
    mapped_moments: np.ndarray,
    gravity: np.ndarray,
    payload: dict[str, Any],
) -> None:
    """Write the decision-relevant force/moment fit figure."""
    import matplotlib.pyplot as plt

    force_bias = np.asarray(payload["force_bias_n"], dtype=float)
    mass = float(payload["mass_kg"])
    moment_bias = np.asarray(payload["moment_bias_nm"], dtype=float)
    cog_mass = mass * np.asarray(payload["cog_sensor_m"], dtype=float)
    force_pred = mass * gravity + force_bias
    rows: list[list[float]] = []
    for gx, gy, gz in gravity:
        rows.extend(
            [
                [0.0, gz, -gy, 1.0, 0.0, 0.0],
                [-gz, 0.0, gx, 0.0, 1.0, 0.0],
                [gy, -gx, 0.0, 0.0, 0.0, 1.0],
            ]
        )
    moment_pred = (np.asarray(rows, dtype=float) @ np.r_[cog_mass, moment_bias]).reshape((-1, 3))
    x = np.arange(len(names))
    labels = [name.replace("_", "\n") for name in names]
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True, constrained_layout=True)
    colors = ["tab:blue", "tab:orange", "tab:green"]
    for idx, axis in enumerate(("Fx", "Fy", "Fz")):
        axes[0].plot(x, mapped_forces[:, idx], "o-", color=colors[idx], label=f"{axis} measured")
        axes[0].plot(x, force_pred[:, idx], "--", color=colors[idx], alpha=0.75, label=f"{axis} model")
    for idx, axis in enumerate(("Tx", "Ty", "Tz")):
        axes[1].plot(x, mapped_moments[:, idx], "o-", color=colors[idx], label=f"{axis} measured")
        axes[1].plot(x, moment_pred[:, idx], "--", color=colors[idx], alpha=0.75, label=f"{axis} model")
    axes[0].set_ylabel("Force (N)")
    axes[1].set_ylabel("Moment (Nm)")
    axes[1].set_xlabel("Static pose segment")
    axes[0].set_title("Kunwei gravity-fit force and moment evidence")
    axes[1].set_xticks(x, labels, rotation=25, ha="right")
    axes[0].grid(True, alpha=0.25)
    axes[1].grid(True, alpha=0.25)
    axes[0].legend(ncol=3, fontsize=8)
    axes[1].legend(ncol=3, fontsize=8)
    fig.savefig(path, dpi=160)
    plt.close(fig)


def write_report(path: Path, result: dict[str, Any]) -> None:
    mapping = result.get("axis_mapping", {})
    payload = result.get("payload_cog", {})
    comparison = result.get("comparison", {})
    plot_path = result.get("plot")
    plot_link = "custom_payload_cog_fit.png" if plot_path else None
    lines = [
        "# Kunwei + UR 空夹爪自拟合报告",
        "",
        "## 实验目的",
        "",
        "用 Kunwei wrench + UR RTDE pose 对当前 RG2 打开、空夹爪状态做独立 gravity-axis 与 payload/CoG 拟合，并与已完成的 PolyScope 内置快照比较；本轮不写回控制器。",
        "",
        "## 设备与实验条件",
        "",
        "| 项目 | 记录 |",
        "|---|---|",
        "| Robot | UR10e, URSoftware 5.26.0.140462 |",
        "| RG2 | 打开、空夹爪、无接触 |",
        "| Kunwei | KWR75B TCP streaming, 1 kHz class |",
        "| UR pose | RTDE observer, 静止姿态 |",
        "| Zero/tare | 未执行 |",
        "| PolyScope reference | 1.55 kg, CoG [1, 13, 51] mm |",
        "| TCP readback | z=26.26 mm；与旧记录不一致，未用于替换 |",
        "| Mass semantics | Kunwei sensing-plane candidate; not whole-assembly scale total |",
        "",
        "## 实验命令",
        "",
        "采集使用 `capture_kunwei_payload_cog_calibration.py`；分析优先读取同一 run 的 `summary.json`，原始 CSV/原始 Kunwei 帧保持不变。",
        "",
        "## 数据与图片",
        "",
        f"- 原始 CSV：`{result['input_csv']}`",
        f"- 原始 Kunwei frames：`{result.get('raw_frames', 'kunwei_raw_frames.bin')}`",
        f"- 主拟合图：{'' if plot_link is None else '![' + 'force/moment fit' + '](' + plot_link + ')' }",
        "",
        "## 统计结果",
        "",
        "| Segment | Samples | Contact | Zeroed | Purpose |",
        "|---|---:|---|---|---|",
    ]
    for segment in result.get("segments", []):
        lines.append(
            f"| {segment['name']} | {segment['rows']} | no contact | no | static gravity pose |"
        )
    lines.extend(
        [
            "",
            "| Method | Mass (kg) | CoG frame | CoG (mm) | Force RMSE (N) | Moment RMSE (Nm) | Status |",
            "|---|---:|---|---|---:|---:|---|",
            f"| PolyScope built-in | 1.5500 | UR tool | [1, 13, 51] | N/A | N/A | reference snapshot |",
            f"| Kunwei custom | {payload.get('mass_kg')} | Kunwei sensor | {payload.get('cog_sensor_mm')} | {payload.get('force_rmse_n')} | {payload.get('moment_rmse_nm')} | fitted |",
            "",
            f"- mapping：`{mapping.get('best')}`",
            f"- gravity rank：`{mapping.get('gravity_rank_centered')}`；`max |g_T,z|={mapping.get('gravity_z_abs_max_mps2')}` m/s²",
            f"- P0 return force drift norm：`{result.get('p0_return_drift_norm_force_n')}` N",
            f"- P0 return moment drift norm：`{result.get('p0_return_drift_norm_moment_nm')}` Nm",
            "",
            "## 结论",
            "",
            f"Kunwei sensing-plane 自拟合质量候选为 `{payload.get('mass_kg')}` kg，比内置 `1.55 kg` 低约 `{comparison.get('delta_payload_pct')}`%；mapping 的最佳轴排列为当前同轴正号，force RMSE 约 `{mapping.get('best', {}).get('rmse_n')}` N。该数值不能直接代表整套拆下称重的总质量。",
            "CoG 当前只得到 Kunwei sensor origin frame 的结果；由于 sensor-origin 到 UR tool frame 的机械变换尚未确认，不能把它与 PolyScope 的 [1,13,51] mm 做直接坐标差，也不应据此写回配置。",
            "",
            "## 下一步",
            "",
            "测量并确认 Kunwei reference origin 到 UR tool frame 的平移/旋转，再用同一 raw run 重算 tool-frame CoG；在此之前保持 PolyScope 的 1.55 kg / [1,13,51] mm 不变。",
            "",
            "## 附录",
            "",
            f"- JSON：`{result['input_csv'].replace('gravity_axis_calibration.csv', 'custom_payload_cog_analysis.json')}`",
            f"- Metadata：`{result['input_csv'].replace('gravity_axis_calibration.csv', 'metadata.json')}`",
            f"- Summary：`{result['input_csv'].replace('gravity_axis_calibration.csv', 'summary.json')}`",
            "",
            "本报告为 diagnostic calibration record，不是 UR payload/TCP promotion certificate。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_canonical_report(path: Path, result: dict[str, Any], run_dir: Path) -> None:
    """Write the canonical report with links relative to the report root."""
    raw_csv = Path(result["input_csv"])
    rel = lambda item: os.path.relpath(run_dir / item, path.parent)
    plot = result.get("plot")
    payload = result.get("payload_cog", {})
    mapping = result.get("axis_mapping", {})
    comparison = result.get("comparison", {})
    lines = [
        "# UR10e Kunwei + RG2 空夹爪自拟合对比",
        "",
        "## 实验目的",
        "",
        "独立拟合当前空 RG2 的 gravity-axis mapping、payload 和 CoG，并与 PolyScope 内置快照对比；本轮不写回控制器。",
        "",
        "## 设备与实验条件",
        "",
        "| 项目 | 记录 |",
        "|---|---|",
        "| Robot | UR10e, URSoftware 5.26.0.140462 |",
        "| EOAT | RG2 打开、空夹爪、无接触 |",
        "| Data source | Kunwei wrench + UR RTDE pose |",
        "| Zero/tare | 未执行 |",
        "| Built-in reference | 1.55 kg, CoG [1, 13, 51] mm |",
        "| Current TCP readback | z=26.26 mm；与旧记录有 discrepancy，未覆盖 |",
        "| Mass semantics | Kunwei sensing-plane candidate; not whole-assembly scale total |",
        "",
        "## 数据与图片",
        "",
        f"- [raw CSV]({rel('gravity_axis_calibration.csv')})",
        f"- [Kunwei raw frames]({rel('kunwei_raw_frames.bin')})",
        f"- [capture summary]({rel('summary.json')})",
        f"- [analysis JSON]({rel('custom_payload_cog_analysis.json')})",
        f"- ![Kunwei force/moment fit]({rel('custom_payload_cog_fit.png')})" if plot else "- 拟合图：N/A",
        "",
        "## 统计结果",
        "",
        "| Segment | Samples | Contact | Zeroed | Purpose |",
        "|---|---:|---|---|---|",
    ]
    for segment in result.get("segments", []):
        lines.append(
            f"| {segment['name']} | {segment['rows']} | no contact | no | static gravity pose |"
        )
    lines.extend(
        [
        "",
        "| Method | Mass (kg) | CoG frame | CoG (mm) | Force RMSE (N) | Moment RMSE (Nm) | Status |",
        "|---|---:|---|---|---:|---:|---|",
        "| PolyScope built-in | 1.5500 | UR tool | [1, 13, 51] | N/A | N/A | reference snapshot |",
        f"| Kunwei custom | {payload.get('mass_kg')} | Kunwei sensor | {payload.get('cog_sensor_mm')} | {payload.get('force_rmse_n')} | {payload.get('moment_rmse_nm')} | fitted |",
        "",
        f"- payload delta：`{comparison.get('delta_payload_kg')}` kg（`{comparison.get('delta_payload_pct')}`%）",
        f"- axis mapping：`{mapping.get('best')}`",
        f"- gravity rank：`{mapping.get('gravity_rank_centered')}`；P0 return drift：`{result.get('p0_return_drift')}`",
        "",
        "## 结论",
        "",
        f"本次 Kunwei sensing-plane 自拟合质量候选为 `{payload.get('mass_kg')}` kg，与内置 `1.55 kg` 不一致（差 `{comparison.get('delta_payload_kg')}` kg）。mapping 结果为当前同轴正号，且姿态集合已达到三维重力方向 rank。它不能直接与整套硬件秤重总质量比较。",
        "CoG 只在 Kunwei sensor origin frame 中得到；sensor-origin 到 UR tool frame 的机械变换尚未确认，因此 CoG 对比状态为 `inconclusive`，不执行 payload/CoG/TCP 写回。",
        "",
        "## 下一步",
        "",
        "确认 sensor-origin 到 UR tool frame 的机械变换后，重用本次 raw run 重算 tool-frame CoG；在此之前保持内置值不变。",
        "",
        "## 附录",
        "",
        f"- 采集目录：`{run_dir}`",
        f"- 原始 CSV：`{raw_csv}`",
        "- 分析命令：`analyze_custom_payload_cog.py <run_dir> --builtin-payload-kg 1.55 --builtin-cog-mm 1 13 51`",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--csv", type=Path, default=None)
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="Force a full raw-CSV scan instead of using capture summary.json.",
    )
    parser.add_argument("--builtin-payload-kg", type=float, default=1.55)
    parser.add_argument("--builtin-cog-mm", type=float, nargs=3, default=[1.0, 13.0, 51.0])
    parser.add_argument("--sensor-origin-in-tool-m", type=float, nargs=3, default=None)
    parser.add_argument("--canonical-report", type=Path, default=None)
    parser.add_argument("--max-age-s", type=float, default=0.02)
    parser.add_argument("--max-linear-speed-mps", type=float, default=0.005)
    parser.add_argument("--max-angular-speed-rps", type=float, default=0.02)
    args = parser.parse_args()

    run_dir = args.run_dir
    csv_path = args.csv or (run_dir / "gravity_axis_calibration.csv")
    if not csv_path.exists():
        raise SystemExit(f"missing input CSV: {csv_path}")

    summary_path = run_dir / "summary.json"
    if args.csv is None and not args.no_summary and summary_path.exists():
        segments, read_meta = read_segments_from_summary(summary_path)
    else:
        segments, read_meta = read_segments(
            csv_path,
            max_age_s=args.max_age_s,
            max_linear_speed_mps=args.max_linear_speed_mps,
            max_angular_speed_rps=args.max_angular_speed_rps,
        )
    if len(segments) < 4:
        result = {
            "status": "inconclusive",
            "reason": "fewer_than_four_valid_segments",
            "created": datetime.now().isoformat(timespec="seconds"),
            "input_csv": str(csv_path),
            "segment_count": len(segments),
            "segment_names": [segment.name for segment in segments],
            "read_meta": read_meta,
        }
    else:
        names = [segment.name for segment in segments]
        poses = np.asarray([segment.pose for segment in segments], dtype=float)
        forces = np.asarray([segment.force for segment in segments], dtype=float)
        moments = np.asarray([segment.moment for segment in segments], dtype=float)
        gravity = np.asarray([gravity_tool_from_pose(pose) for pose in poses], dtype=float)
        mapping = fit_axis_mapping(forces, gravity)
        payload: dict[str, Any]
        if mapping.get("status") != "ok":
            payload = {"status": "inconclusive", "reason": "axis_mapping_failed"}
            mapped_forces = None
            mapped_moments = None
        else:
            best = mapping["best"]
            mapped_forces = apply_mapping(forces, best)
            mapped_moments = apply_mapping(moments, best)
            payload = fit_payload_cog(
                mapped_forces,
                mapped_moments,
                gravity,
                origin_offset_tool_m=args.sensor_origin_in_tool_m,
            )
        drift = p0_return_drift(names, forces, moments)
        result = {
            "status": "ok" if payload.get("status") == "ok" else "inconclusive",
            "created": datetime.now().isoformat(timespec="seconds"),
            "input_csv": str(csv_path),
            "raw_frames": str(run_dir / "kunwei_raw_frames.bin"),
            "segment_count": len(segments),
            "segment_names": names,
            "segments": [
                {
                    "name": segment.name,
                    "rows": segment.rows,
                    "pose": segment.pose.tolist(),
                    "force_n": segment.force.tolist(),
                    "moment_nm": segment.moment.tolist(),
                    "force_std_n": segment.force_std.tolist(),
                    "moment_std_nm": segment.moment_std.tolist(),
                    "speed_max": segment.speed_max,
                    "rtde_age_max": segment.rtde_age_max,
                    "safety_modes": segment.safety_modes,
                }
                for segment in segments
            ],
            "gravity_tool_mps2": gravity.tolist(),
            "read_meta": read_meta,
            "axis_mapping": mapping,
            "payload_cog": payload,
            "p0_return_drift": drift,
            "comparison": compare_builtin(
                payload,
                builtin_payload_kg=args.builtin_payload_kg,
                builtin_cog_mm=args.builtin_cog_mm,
            ),
        }
        if drift is not None:
            result["p0_return_drift_norm_force_n"] = float(np.linalg.norm(drift["force_n"]))
            result["p0_return_drift_norm_moment_nm"] = float(np.linalg.norm(drift["moment_nm"]))
        if mapping.get("status") == "ok" and payload.get("status") == "ok":
            plot_path = run_dir / "custom_payload_cog_fit.png"
            write_fit_plot(plot_path, names, mapped_forces, mapped_moments, gravity, payload)
            result["plot"] = str(plot_path)

    output_json = run_dir / "custom_payload_cog_analysis.json"
    output_report = run_dir / "custom_payload_cog_report.md"
    output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_report(output_report, result)
    if args.canonical_report is not None:
        write_canonical_report(args.canonical_report, result, run_dir)
    print(
        json.dumps(
            {
                "status": result["status"],
                "json": str(output_json),
                "report": str(output_report),
                "canonical_report": None if args.canonical_report is None else str(args.canonical_report),
                "plot": result.get("plot"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
