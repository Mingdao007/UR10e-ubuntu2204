#!/usr/bin/env python3
"""Evaluate one completed Step5b autotune trial without touching hardware."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from step5b_autotune_contract import (
    BRIDGE_PROFILE,
    CONSTRAINT_VIOLATION_REASONS,
    FATAL_SESSION_REASONS,
    OBJECTIVE_NAME,
    OBJECTIVE_UNIT,
    Candidate,
    is_known_bad_history_path,
)


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def numeric(df: pd.DataFrame, column: str) -> pd.Series:
    if column not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype=float)
    return pd.to_numeric(df[column], errors="coerce")


def finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def active_stage25(df: pd.DataFrame) -> pd.DataFrame:
    stage = numeric(df, "ur_output_double_register_35")
    return df[(stage - 25.0).abs() < 0.05].copy()


def signed_normal_load(active: pd.DataFrame) -> pd.Series:
    """Return the force-frame-contract load without magnitude/sign substitution."""
    return numeric(active, "_step4e_normal_load_n")


def elapsed_s(active: pd.DataFrame) -> float:
    times = numeric(active, "t_monotonic_s").dropna()
    if len(times) < 2:
        return 0.0
    return max(0.0, float(times.iloc[-1] - times.iloc[0]))


def candidate_from_metadata(metadata: dict[str, Any]) -> Candidate:
    args = metadata.get("args") if isinstance(metadata.get("args"), dict) else {}
    return Candidate(
        target_force_n=float(args["target_force_n"]),
        force_p_gain=float(args["step4e_force_p_gain"]),
        force_i_gain=float(args["step4e_force_i_gain"]),
        force_damping=float(args["step4e_force_damping"]),
        normal_filter_alpha=float(args["step4e_normal_filter_alpha"]),
    )


def command_tv(active: pd.DataFrame) -> float:
    columns = [
        "step4e_cmd_vx_m_s",
        "step4e_cmd_vy_m_s",
        "step4e_cmd_vz_m_s",
        "step4e_cmd_wx_rad_s",
        "step4e_cmd_wy_rad_s",
        "step4e_cmd_wz_rad_s",
    ]
    if any(column not in active.columns for column in columns) or len(active) < 2:
        return 1.0
    values = np.column_stack([numeric(active, column).to_numpy(dtype=float) for column in columns])
    scales = np.array([0.004, 0.004, 0.01, 0.15, 0.15, 0.005], dtype=float)
    normalized_delta = np.diff(values, axis=0) / scales
    row_tv = np.linalg.norm(np.nan_to_num(normalized_delta, nan=0.0), axis=1)
    # A 10% cap change per 2 ms sample is already a large command discontinuity.
    return float(np.clip(np.mean(row_tv) / 0.10, 0.0, 1.0))


def near_limit_duty(active: pd.DataFrame) -> float:
    if active.empty:
        return 1.0
    linear = np.column_stack(
        [numeric(active, f"step4e_cmd_v{axis}_m_s").to_numpy(dtype=float) for axis in "xyz"]
    )
    angular = np.column_stack(
        [numeric(active, f"step4e_cmd_w{axis}_rad_s").to_numpy(dtype=float) for axis in "xyz"]
    )
    linear_norm = np.linalg.norm(np.nan_to_num(linear, nan=0.0), axis=1)
    angular_xy = np.linalg.norm(np.nan_to_num(angular[:, :2], nan=0.0), axis=1)
    normal = np.abs(np.nan_to_num(linear[:, 2], nan=0.0))
    near = (linear_norm >= 0.98 * 0.004) | (angular_xy >= 0.98 * 0.15) | (normal >= 0.98 * 0.01)
    return float(np.mean(near))


def evaluate_run(run_dir: Path, *, allow_history: bool = False) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    metadata = load_json(run_dir / "metadata.json")
    bridge_summary = load_json(run_dir / "summary.json")
    runtime = load_json(run_dir / "trial_runtime.json")
    csv_paths = sorted(run_dir.glob("bridge_rtde_*hz.csv"))
    failures: list[str] = []

    if is_known_bad_history_path(run_dir):
        failures.append("known_false_or_zero_history_run")
    if not metadata:
        failures.append("metadata_missing")
    args = metadata.get("args") if isinstance(metadata.get("args"), dict) else {}
    if str(args.get("step4e_version", "")) != BRIDGE_PROFILE:
        failures.append("wrong_bridge_profile")
    if not csv_paths:
        failures.append("bridge_csv_missing")

    candidate: Candidate | None = None
    try:
        candidate = candidate_from_metadata(metadata)
        candidate.validate(tier2_unlocked=True)
    except (KeyError, TypeError, ValueError) as exc:
        failures.append(f"candidate_metadata_invalid:{exc}")

    if failures or candidate is None or not csv_paths:
        return {
            "schema_version": "step5b_autotune_evaluation_v3",
            "run_dir": str(run_dir),
            "eligible": False,
            "feasible": False,
            "objective": None,
            "objective_name": OBJECTIVE_NAME,
            "objective_unit": OBJECTIVE_UNIT,
            "failures": failures,
        }

    df = pd.read_csv(csv_paths[0], low_memory=False)
    active = active_stage25(df)
    duration_s = elapsed_s(active)
    progress = numeric(active, "step4e_progress_m").dropna()
    max_progress_s = float(progress.max()) if not progress.empty else 0.0
    load_raw = signed_normal_load(active)
    load = load_raw.replace([np.inf, -np.inf], np.nan).dropna()
    target = candidate.target_force_n

    if len(active) < 5000:
        failures.append("insufficient_stage25_samples")
    if duration_s < 55.0:
        failures.append("stage25_duration_lt_55s")
    if max_progress_s < 59.9:
        failures.append("path_progress_lt_59p9s")
    if load.empty or float(load.median()) < 2.0:
        failures.append("missing_or_zero_contact_load")

    required_columns = [
        "_step4e_normal_load_n",
        "force_norm_n",
        "mx_nm_zeroed",
        "my_nm_zeroed",
        "mz_nm_zeroed",
        *[f"step4e_cmd_v{axis}_m_s" for axis in "xyz"],
        *[f"step4e_cmd_w{axis}_rad_s" for axis in "xyz"],
    ]
    missing_required = [column for column in required_columns if column not in active]
    if missing_required:
        failures.append("required_columns_missing:" + ",".join(missing_required))
    elif any(not np.isfinite(numeric(active, column).to_numpy(dtype=float)).all() for column in required_columns):
        failures.append("nonfinite_required_data")

    terminal_reason = runtime.get("terminal_reason")
    if terminal_reason is not None:
        terminal_reason = int(terminal_reason)
        if terminal_reason in CONSTRAINT_VIOLATION_REASONS:
            failures.append(f"constraint_terminal_reason_{terminal_reason}")
        if terminal_reason in FATAL_SESSION_REASONS:
            failures.append(f"fatal_terminal_reason_{terminal_reason}")
    if runtime and not bool(runtime.get("home_verified", False)):
        failures.append("home_not_verified")

    force_norm = numeric(active, "force_norm_n")
    torque_cols = [column for column in ("mx_nm_zeroed", "my_nm_zeroed", "mz_nm_zeroed") if column in active]
    torque_norm = (
        np.linalg.norm(np.column_stack([numeric(active, column) for column in torque_cols]), axis=1)
        if len(torque_cols) == 3
        else np.array([])
    )
    if force_norm.notna().any() and float(force_norm.max()) >= 60.0:
        failures.append("force_norm_guard_reached")
    if load_raw.notna().any() and float(load_raw.abs().max()) >= 50.0:
        failures.append("raw_normal_guard_reached")
    if torque_norm.size and float(np.nanmax(torque_norm)) >= 3.0:
        failures.append("torque_guard_reached")

    full_trial = duration_s >= 55.0 and max_progress_s >= 59.9
    history_eligible = (
        allow_history
        and not runtime
        and full_trial
        and not is_known_bad_history_path(run_dir)
        and str(bridge_summary.get("stop_reason", "")) in {"signal_sigint", "complete"}
    )
    eligible = bool(runtime) or history_eligible
    if not eligible:
        failures.append("missing_supervisor_runtime_or_history_gate")

    force_error = load.to_numpy(dtype=float) - target
    force_mae_n = float(np.mean(np.abs(force_error))) if load.size else math.inf
    force_rmse_n = float(np.sqrt(np.mean(force_error**2))) if load.size else math.inf
    force_p99_absolute_error_n = float(np.quantile(np.abs(force_error), 0.99)) if load.size else math.inf
    force_nrmse = float(np.clip(np.sqrt(np.mean(force_error**2)) / target, 0.0, 1.0)) if load.size else 1.0
    p99_error = float(np.clip(np.quantile(np.abs(force_error), 0.99) / target, 0.0, 1.0)) if load.size else 1.0
    path_x = numeric(active, "_step4e_path_error_x_m")
    path_y = numeric(active, "_step4e_path_error_y_m")
    path_sq = path_x**2 + path_y**2
    xy_rmse_m = float(np.sqrt(path_sq.dropna().mean())) if path_sq.notna().any() else math.inf
    xy_metric = float(np.clip(xy_rmse_m / 0.005, 0.0, 1.0)) if math.isfinite(xy_rmse_m) else 1.0
    tv_metric = command_tv(active)
    limit_duty = near_limit_duty(active)

    metrics = {
        "force_mae_n": force_mae_n if math.isfinite(force_mae_n) else None,
        "force_rmse_n": force_rmse_n if math.isfinite(force_rmse_n) else None,
        "force_p99_absolute_error_n": force_p99_absolute_error_n if math.isfinite(force_p99_absolute_error_n) else None,
        "force_nrmse": force_nrmse,
        "force_p99_absolute_error_over_target": p99_error,
        "xy_rmse_m": xy_rmse_m if math.isfinite(xy_rmse_m) else None,
        "xy_rmse_over_5mm": xy_metric,
        "normalized_command_total_variation": tv_metric,
        "near_limit_dwell_duty": limit_duty,
        "stage25_duration_s": duration_s,
        "stage25_samples": int(len(active)),
        "max_path_progress_s": max_progress_s,
        "normal_load_mean_n": float(load.mean()) if load.size else None,
        "normal_load_p99_n": float(load.quantile(0.99)) if load.size else None,
    }
    feasible = eligible and not failures
    objective = force_mae_n if feasible and math.isfinite(force_mae_n) else None
    return {
        "schema_version": "step5b_autotune_evaluation_v3",
        "run_dir": str(run_dir),
        "eligible": eligible,
        "feasible": feasible,
        "objective": objective,
        "objective_name": OBJECTIVE_NAME,
        "objective_unit": OBJECTIVE_UNIT,
        "candidate": candidate.payload(),
        "metrics": metrics,
        "failures": failures,
        "provenance": {
            "metadata": str(run_dir / "metadata.json"),
            "summary": str(run_dir / "summary.json"),
            "trial_runtime": str(run_dir / "trial_runtime.json") if runtime else None,
            "bridge_csv": str(csv_paths[0]),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--allow-history", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate_run(args.run_dir, allow_history=args.allow_history)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["eligible"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
