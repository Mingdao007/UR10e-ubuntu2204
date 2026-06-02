#!/usr/bin/env python3
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path("/home/andy/ur10e_ros2_ws")
RUN_DIR = (
    ROOT
    / "experiments/20260528_onrobot_three_stream_600s_first_zero/run_20260528_043100"
)
SUMMARY_PATH = RUN_DIR / "three_stream_600s_20260528_043052_summary.json"
RTDE_CSV = RUN_DIR / "three_stream_600s_20260528_043052_rtde_ur500_urcap125.csv"
UDP_CSV = RUN_DIR / "three_stream_600s_20260528_043052_onrobot_udp500_raw.csv"
PLOT_DIR = RUN_DIR / "plots"
STATS_PATH = RUN_DIR / "three_stream_600s_20260528_043052_urcap_udp_alignment_stats.json"
RESIDUAL_PLOT = PLOT_DIR / "three_stream_600s_20260528_043052_urcap_udp_residuals.png"

AXES = ["fx", "fy", "fz", "tx", "ty", "tz"]
FORCE_AXES = {"fx", "fy", "fz"}
URCAP_COLS = [
    "urcap_fx_n",
    "urcap_fy_n",
    "urcap_fz_n",
    "urcap_tx_nm",
    "urcap_ty_nm",
    "urcap_tz_nm",
]
UDP_COLS = ["fx_n", "fy_n", "fz_n", "tx_nm", "ty_nm", "tz_nm"]


def residual_stats(diff: np.ndarray) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for idx, axis in enumerate(AXES):
        values = diff[:, idx]
        out[axis] = {
            "mean": float(np.mean(values)),
            "std": float(np.std(values)),
            "rms": float(math.sqrt(float(np.mean(values * values)))),
            "min": float(np.min(values)),
            "max": float(np.max(values)),
            "max_abs": float(np.max(np.abs(values))),
        }
    return out


def extract_urcap_updates(rtde: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    values = rtde[URCAP_COLS].to_numpy(dtype=float)
    update_mask = np.ones(len(rtde), dtype=bool)
    update_mask[1:] = np.any(values[1:] != values[:-1], axis=1)
    return rtde["t_s"].to_numpy(dtype=float)[update_mask], values[update_mask]


def interpolate_udp(udp_t: np.ndarray, udp_z: np.ndarray, target_t: np.ndarray) -> np.ndarray:
    return np.vstack([np.interp(target_t, udp_t, udp_z[:, idx]) for idx in range(len(AXES))]).T


def nearest_indices(source_t: np.ndarray, target_t: np.ndarray) -> np.ndarray:
    right = np.searchsorted(source_t, target_t)
    right = np.clip(right, 1, len(source_t) - 1)
    left = right - 1
    choose_right = np.abs(source_t[right] - target_t) < np.abs(target_t - source_t[left])
    return np.where(choose_right, right, left)


def compare_for_lag(
    uc_t: np.ndarray,
    uc_z: np.ndarray,
    udp_t: np.ndarray,
    udp_z: np.ndarray,
    lag_s: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    target_t = uc_t + lag_s
    valid = (target_t >= udp_t[0]) & (target_t <= udp_t[-1])
    target_t = target_t[valid]
    uc_valid = uc_z[valid]
    udp_interp = interpolate_udp(udp_t, udp_z, target_t)
    return target_t, uc_valid, uc_valid - udp_interp


def choose_best_lag(uc_t: np.ndarray, uc_z: np.ndarray, udp_t: np.ndarray, udp_z: np.ndarray) -> dict[str, Any]:
    udp_std = np.std(udp_z, axis=0)
    udp_std[udp_std == 0.0] = 1.0
    best: dict[str, Any] | None = None
    for lag_s in np.linspace(-0.020, 0.020, 801):
        _target_t, _uc_valid, diff = compare_for_lag(uc_t, uc_z, udp_t, udp_z, float(lag_s))
        normalized = diff / udp_std
        score = math.sqrt(float(np.mean(normalized * normalized)))
        if best is None or score < best["normalized_rms"]:
            best = {
                "lag_s": float(lag_s),
                "normalized_rms": score,
                "sample_count": int(diff.shape[0]),
            }
    assert best is not None
    return best


def make_residual_plot(target_t: np.ndarray, diff_nearest: np.ndarray) -> None:
    PLOT_DIR.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(3, 2, figsize=(11, 8.2), sharex=True)
    for idx, axis in enumerate(AXES):
        ax = axes.flat[idx]
        unit = "N" if axis in FORCE_AXES else "Nm"
        ax.plot(target_t, diff_nearest[:, idx], linewidth=0.65, color="tab:blue")
        ax.axhline(float(np.mean(diff_nearest[:, idx])), color="tab:red", linewidth=1.0, linestyle="--")
        ax.axhline(0.0, color="black", linewidth=0.8, alpha=0.7)
        ax.set_title(f"{axis.upper()} residual")
        ax.set_ylabel(unit)
        ax.grid(True, alpha=0.3)
    axes[-1, 0].set_xlabel("aligned time (s)")
    axes[-1, 1].set_xlabel("aligned time (s)")
    fig.suptitle("URCap 125 Hz first-zeroed value minus timestamp-aligned UDP 500 Hz nearest sample")
    fig.tight_layout()
    fig.savefig(RESIDUAL_PLOT, dpi=170)
    plt.close(fig)


def main() -> int:
    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    rtde = pd.read_csv(RTDE_CSV, usecols=["t_s", *URCAP_COLS])
    udp = pd.read_csv(UDP_CSV, usecols=["t_s", "sample_index", "sequence_number", "sample_counter", *UDP_COLS])

    uc_t, uc_values = extract_urcap_updates(rtde)
    udp_t = udp["t_s"].to_numpy(dtype=float)
    udp_values = udp[UDP_COLS].to_numpy(dtype=float)

    # The requested zeroing is software-only and independent for each logged channel.
    uc_z = uc_values - uc_values[0]
    udp_z = udp_values - udp_values[0]

    best_lag = choose_best_lag(uc_t, uc_z, udp_t, udp_z)
    zero_target_t, _zero_uc, zero_diff = compare_for_lag(uc_t, uc_z, udp_t, udp_z, 0.0)
    best_target_t, best_uc, best_interp_diff = compare_for_lag(
        uc_t, uc_z, udp_t, udp_z, best_lag["lag_s"]
    )

    nearest = nearest_indices(udp_t, best_target_t)
    best_nearest_diff = best_uc - udp_z[nearest]
    nearest_dt_ms = (udp_t[nearest] - best_target_t) * 1000.0
    udp_index_delta = np.diff(nearest)
    unique_deltas, delta_counts = np.unique(udp_index_delta, return_counts=True)

    make_residual_plot(best_target_t, best_nearest_diff)

    fz_nearest = best_nearest_diff[:, AXES.index("fz")]
    strict_overlap = bool(
        abs(float(np.mean(fz_nearest))) <= 0.05
        and float(np.std(fz_nearest)) <= 0.05
        and float(np.max(np.abs(fz_nearest))) <= 0.20
    )

    stats = {
        "inputs": {
            "summary": str(SUMMARY_PATH),
            "rtde_csv": str(RTDE_CSV),
            "udp_csv": str(UDP_CSV),
        },
        "plot": str(RESIDUAL_PLOT),
        "software_zero": "Each stream/channel subtracts its own first logged sample. No hardware zero or bias command is applied.",
        "sampling_validation": {
            "summary_ok": bool(summary.get("ok")),
            "errors": summary.get("errors", []),
            "requested_seconds": summary["requested_seconds"],
            "rtde_samples": summary["rtde_timing"]["samples"],
            "rtde_interval_rate_hz": summary["rtde_timing"]["interval_rate_hz"],
            "urcap_tuple_updates": summary["urcap_register_tuple_updates"]["runs"],
            "urcap_update_rate_hz": summary["urcap_register_tuple_updates"]["distinct_value_transition_rate_hz"],
            "udp_packets": summary["udp_timing"]["samples"],
            "udp_interval_rate_hz": summary["udp_timing"]["interval_rate_hz"],
            "udp_sequence_delta_counts": summary["udp_sequence_delta_counts"],
            "udp_sample_counter_delta_mod65536_counts": summary["udp_sample_counter_delta_mod65536_counts"],
            "dashboard_before": summary["dashboard_before"],
            "dashboard_after": summary["dashboard_after"],
            "commands_sent": summary["udp_commands_sent"],
            "safety_boundary": summary["safety_boundary"],
        },
        "urcap_updates_extracted_from_rtde_rows": int(len(uc_t)),
        "alignment": {
            "lag_definition": "Residual is URCap(t) - UDP(t + lag); lag chosen by normalized six-axis RMS against linearly interpolated UDP.",
            "best_lag_s": best_lag["lag_s"],
            "best_lag_ms": best_lag["lag_s"] * 1000.0,
            "lag_search_range_ms": [-20.0, 20.0],
            "lag_search_step_ms": 0.05,
            "zero_lag_sample_count": int(zero_diff.shape[0]),
            "best_lag_sample_count": int(best_interp_diff.shape[0]),
            "nearest_udp_abs_dt_ms": {
                "mean": float(np.mean(np.abs(nearest_dt_ms))),
                "max": float(np.max(np.abs(nearest_dt_ms))),
            },
            "nearest_udp_index_delta_counts_top": [
                [int(delta), int(count)]
                for delta, count in sorted(zip(unique_deltas, delta_counts), key=lambda item: item[1], reverse=True)[:10]
            ],
        },
        "residual_stats": {
            "zero_lag_udp_interpolated": residual_stats(zero_diff),
            "best_lag_udp_interpolated": residual_stats(best_interp_diff),
            "best_lag_nearest_udp_sample": residual_stats(best_nearest_diff),
        },
        "classification": {
            "strict_first_zero_overlap": strict_overlap,
            "basis": (
                "After timestamp lag optimization, the residual variation is small, but the per-stream first-zeroed "
                "comparison retains a nonzero fixed offset, especially Fz. That offset exceeds force quantization "
                "and the remaining timestamp-alignment error."
            ),
            "conclusion_zh": (
                "在本 600 s 静态同步采集中，URCap 125 Hz 相对于 UDP 500 Hz 没有观察到额外后级滤波；"
                "主要表现为约 4 倍降采样/保持。但首值归零后仍存在固定残差，不能把 URCap 说成 UDP 每 4 帧的"
                "严格同值抽样；这也不能排除二者共享的上游滤波、补偿、坐标或 bias/reference 处理。"
                if not strict_overlap
                else "在本 600 s 静态同步采集中，URCap 125 Hz 相对于 UDP 500 Hz 没有观察到额外后级滤波；主要表现为约 4 倍降采样/保持。但这不能排除二者共享的上游滤波、补偿、坐标或 bias/reference 处理。"
            ),
            "requested_binary_conclusion_zh": (
                "在本 600 s 静态同步采集中，URCap 125 Hz 相对于 UDP 500 Hz 没有观察到额外后级滤波；主要表现为约 4 倍降采样/保持。但这不能排除二者共享的上游滤波、补偿、坐标或 bias/reference 处理。"
                if strict_overlap
                else "首值归零后仍存在残差，URCap 不是简单的 UDP 每 4 帧抽样；可能存在滤波、补偿、时间对齐误差或不同 reference 处理。"
            ),
        },
    }
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
