#!/usr/bin/env python3
"""Verify a Step5d strict RNN no-contact P0 bridge artifact.

This verifier is offline-only. It reads a completed run directory or
`bridge_rtde_500hz.csv` and checks that the first `speedj_rnn_live` Stage25
sample contains the strict RNN `solver_warm_start` evidence and does not unload
along the approach normal while the outer command asks for press.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any


DEFAULT_MAX_NORMAL_LOAD_N = 2.0
DEFAULT_MAX_FORCE_NORM_N = 5.0
DEFAULT_MAX_RESIDUAL_NORM = 1e-3
DEFAULT_MAX_ACTIVE_BOUNDS = 0
DEFAULT_MIN_LAMBDA_WINDOW_RATIO = 0.5
DEFAULT_MAX_NORMAL_TRACKING_ERROR_M_S = 5e-4
DEFAULT_MIN_FIRST_OUTER_PRESS_M_S = 1e-9
STAGE25_TOLERANCE = 0.05
INTEGER_TOLERANCE = 1e-9


def finite_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    if math.isfinite(parsed):
        return parsed
    return None


def finite_int(value: object) -> int | None:
    parsed = finite_float(value)
    if parsed is None:
        return None
    rounded = round(parsed)
    if abs(parsed - rounded) > INTEGER_TOLERANCE:
        return None
    return int(rounded)


def csv_path_from_input(path: Path) -> Path:
    if path.is_dir():
        return path / "bridge_rtde_500hz.csv"
    return path


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def speedj_rnn_mode_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    selected = []
    for row in rows:
        mode = str(row.get("_step5d_stage25_control_mode") or "")
        if mode == "speedj_rnn_live":
            selected.append(row)
    return selected


def speedj_rnn_stage25_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    selected = []
    for row in speedj_rnn_mode_rows(rows):
        stage = finite_float(row.get("ur_output_double_register_35"))
        if stage is not None and abs(stage - 25.0) <= STAGE25_TOLERANCE:
            selected.append(row)
    return selected


def finite_values(rows: list[dict[str, str]], field: str) -> tuple[list[float], int]:
    values: list[float] = []
    missing = 0
    for row in rows:
        value = finite_float(row.get(field))
        if value is None:
            missing += 1
        else:
            values.append(value)
    return values, missing


def first_tick_summary(row: dict[str, str]) -> dict[str, Any]:
    return {
        "t_monotonic_s": finite_float(row.get("t_monotonic_s")),
        "stage": finite_float(row.get("ur_output_double_register_35")),
        "stage25_echo_consumed": finite_int(row.get("_step5d_stage25_echo_consumed")),
        "intervention_reason": str(row.get("_step5d_intervention_reason") or ""),
        "outer_approach_normal_m_s": finite_float(row.get("_step5d_outer_xdot_limited_approach_normal_m_s")),
        "jqdot_raw_approach_normal_m_s": finite_float(row.get("_step5d_jqdot_raw_approach_normal_m_s")),
        "jqdot_cmd_approach_normal_m_s": finite_float(row.get("_step5d_jqdot_cmd_approach_normal_m_s")),
        "constraint_residual_norm": finite_float(row.get("_step5d_constraint_residual_norm")),
        "lambda_norm": finite_float(row.get("_step5d_lambda_norm")),
        "active_bounds_count": finite_int(row.get("_step5d_active_bounds_count")),
        "normal_load_n": finite_float(row.get("_step4e_normal_load_n")),
        "force_norm_n": finite_float(row.get("force_norm_n")),
    }


def verify_rows(
    rows: list[dict[str, str]],
    *,
    max_normal_load_n: float = DEFAULT_MAX_NORMAL_LOAD_N,
    max_force_norm_n: float = DEFAULT_MAX_FORCE_NORM_N,
    max_residual_norm: float = DEFAULT_MAX_RESIDUAL_NORM,
    max_active_bounds: int = DEFAULT_MAX_ACTIVE_BOUNDS,
    min_lambda_window_ratio: float = DEFAULT_MIN_LAMBDA_WINDOW_RATIO,
    max_normal_tracking_error_m_s: float = DEFAULT_MAX_NORMAL_TRACKING_ERROR_M_S,
    min_first_outer_press_m_s: float = DEFAULT_MIN_FIRST_OUTER_PRESS_M_S,
) -> dict[str, Any]:
    blockers: list[str] = []
    mode_rows = speedj_rnn_mode_rows(rows)
    rnn_rows = speedj_rnn_stage25_rows(rows)
    if not mode_rows:
        return {
            "ok": False,
            "blockers": ["no_speedj_rnn_live_rows"],
            "rows": len(rows),
            "speedj_rnn_live_rows": 0,
            "stage25_speedj_rnn_live_rows": 0,
            "acceptance_scope": "offline_artifact_verification_only_not_live_run_claim",
        }
    if not rnn_rows:
        return {
            "ok": False,
            "blockers": ["no_stage25_speedj_rnn_live_rows"],
            "rows": len(rows),
            "speedj_rnn_live_rows": len(mode_rows),
            "stage25_speedj_rnn_live_rows": 0,
            "acceptance_scope": "offline_artifact_verification_only_not_live_run_claim",
        }
    non_stage25_mode_rows = len(mode_rows) - len(rnn_rows)
    if non_stage25_mode_rows:
        blockers.append("non_stage25_speedj_rnn_live_rows_present")

    first = first_tick_summary(rnn_rows[0])
    intervention_reason = first["intervention_reason"]
    if "solver_warm_start" not in intervention_reason.split("|"):
        blockers.append("first_speedj_rnn_tick_missing_solver_warm_start")
    if first["stage25_echo_consumed"] != 1:
        blockers.append("first_speedj_rnn_tick_not_consumed_by_stage25")

    outer = first["outer_approach_normal_m_s"]
    raw = first["jqdot_raw_approach_normal_m_s"]
    cmd = first["jqdot_cmd_approach_normal_m_s"]
    if outer is None or raw is None or cmd is None:
        blockers.append("first_speedj_rnn_tick_missing_approach_normal_fields")
    else:
        if outer <= min_first_outer_press_m_s:
            blockers.append("first_speedj_rnn_tick_outer_not_pressing")
        if outer > 0.0 and raw < 0.0:
            blockers.append("first_speedj_rnn_tick_press_unload_mismatch")
        if outer < 0.0 and raw > 0.0:
            blockers.append("first_speedj_rnn_tick_unload_press_mismatch")
        if abs(raw - outer) > max_normal_tracking_error_m_s:
            blockers.append("first_speedj_rnn_tick_normal_tracking_error_exceeds_limit")
        if outer > 0.0 and cmd < 0.0:
            blockers.append("first_speedj_rnn_tick_cmd_press_unload_mismatch")
        if outer < 0.0 and cmd > 0.0:
            blockers.append("first_speedj_rnn_tick_cmd_unload_press_mismatch")
        if abs(cmd - outer) > max_normal_tracking_error_m_s:
            blockers.append("first_speedj_rnn_tick_cmd_normal_tracking_error_exceeds_limit")

    max_normal = None
    max_force = None
    normal_values, normal_missing = finite_values(rnn_rows, "_step4e_normal_load_n")
    force_values, force_missing = finite_values(rnn_rows, "force_norm_n")
    if first["normal_load_n"] is None:
        blockers.append("first_speedj_rnn_tick_missing_normal_load_evidence")
    if normal_missing:
        blockers.append("normal_load_evidence_missing")
    elif normal_values:
        max_normal = max(normal_values)
    if max_normal is not None and max_normal > max_normal_load_n:
        blockers.append("normal_load_exceeds_no_contact_limit")
    if first["force_norm_n"] is None:
        blockers.append("first_speedj_rnn_tick_missing_force_norm_evidence")
    if force_missing:
        blockers.append("force_norm_evidence_missing")
    elif force_values:
        max_force = max(force_values)
    if max_force is not None and max_force > max_force_norm_n:
        blockers.append("force_norm_exceeds_no_contact_limit")

    residual = first["constraint_residual_norm"]
    if residual is None:
        blockers.append("first_speedj_rnn_tick_missing_constraint_residual_norm")
    elif residual > max_residual_norm:
        blockers.append("first_speedj_rnn_tick_constraint_residual_norm_exceeds_limit")

    active_bounds = first["active_bounds_count"]
    if active_bounds is None:
        blockers.append("first_speedj_rnn_tick_missing_active_bounds_count")
    elif active_bounds > max_active_bounds:
        blockers.append("first_speedj_rnn_tick_active_bounds_exceeds_limit")

    lambda_window = rnn_rows[:10]
    lambda_values, lambda_missing = finite_values(lambda_window, "_step5d_lambda_norm")
    if first["lambda_norm"] is None:
        blockers.append("first_speedj_rnn_tick_missing_lambda_norm")
    if lambda_missing or not lambda_values:
        blockers.append("lambda_norm_evidence_missing")
        lambda_ratio = None
    else:
        window_level = sorted(lambda_values)[len(lambda_values) // 2]
        if window_level <= 0.0:
            blockers.append("lambda_norm_window_level_nonpositive")
            lambda_ratio = None
        else:
            lambda_ratio = lambda_values[0] / window_level
        if lambda_ratio is not None and lambda_ratio < min_lambda_window_ratio:
            blockers.append("first_lambda_norm_below_window_level")

    return {
        "ok": not blockers,
        "blockers": blockers,
        "rows": len(rows),
        "speedj_rnn_live_rows": len(mode_rows),
        "stage25_speedj_rnn_live_rows": len(rnn_rows),
        "first_tick": first,
        "limits": {
            "max_normal_load_n": max_normal_load_n,
            "max_force_norm_n": max_force_norm_n,
            "max_residual_norm": max_residual_norm,
            "max_active_bounds": max_active_bounds,
            "min_lambda_window_ratio": min_lambda_window_ratio,
            "max_normal_tracking_error_m_s": max_normal_tracking_error_m_s,
            "min_first_outer_press_m_s": min_first_outer_press_m_s,
        },
        "metrics": {
            "max_normal_load_n": max_normal,
            "max_force_norm_n": max_force,
            "lambda_norm_first_to_window_ratio": lambda_ratio,
            "non_stage25_speedj_rnn_live_rows": non_stage25_mode_rows,
        },
        "acceptance_scope": "offline_artifact_verification_only_not_live_run_claim",
    }


def verify_run_dir(path: Path, **kwargs: Any) -> dict[str, Any]:
    csv_path = csv_path_from_input(path)
    if not csv_path.exists():
        return {
            "ok": False,
            "blockers": ["bridge_rtde_csv_missing"],
            "csv": str(csv_path),
            "acceptance_scope": "offline_artifact_verification_only_not_live_run_claim",
        }
    result = verify_rows(read_rows(csv_path), **kwargs)
    result["csv"] = str(csv_path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Run directory or bridge_rtde_500hz.csv")
    parser.add_argument("--output", type=Path, help="Optional JSON summary output path")
    parser.add_argument("--max-normal-load-n", type=float, default=DEFAULT_MAX_NORMAL_LOAD_N)
    parser.add_argument("--max-force-norm-n", type=float, default=DEFAULT_MAX_FORCE_NORM_N)
    parser.add_argument("--max-residual-norm", type=float, default=DEFAULT_MAX_RESIDUAL_NORM)
    parser.add_argument("--max-active-bounds", type=int, default=DEFAULT_MAX_ACTIVE_BOUNDS)
    parser.add_argument("--min-lambda-window-ratio", type=float, default=DEFAULT_MIN_LAMBDA_WINDOW_RATIO)
    parser.add_argument("--max-normal-tracking-error-m-s", type=float, default=DEFAULT_MAX_NORMAL_TRACKING_ERROR_M_S)
    parser.add_argument("--min-first-outer-press-m-s", type=float, default=DEFAULT_MIN_FIRST_OUTER_PRESS_M_S)
    args = parser.parse_args(argv)

    result = verify_run_dir(
        args.run,
        max_normal_load_n=args.max_normal_load_n,
        max_force_norm_n=args.max_force_norm_n,
        max_residual_norm=args.max_residual_norm,
        max_active_bounds=args.max_active_bounds,
        min_lambda_window_ratio=args.min_lambda_window_ratio,
        max_normal_tracking_error_m_s=args.max_normal_tracking_error_m_s,
        min_first_outer_press_m_s=args.min_first_outer_press_m_s,
    )
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result.get("ok") else 24


if __name__ == "__main__":
    raise SystemExit(main())
