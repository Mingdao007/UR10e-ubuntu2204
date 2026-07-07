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
DEFAULT_QDOT_CAP_RAD_S = 0.15
DEFAULT_QDOT_RAIL_MARGIN_RAD_S = 1e-9
DEFAULT_LOW_FORCE_POSTURE_LOAD_N = 1.0
DEFAULT_LOW_FORCE_POSTURE_BASE_KO = 5.0
DEFAULT_MAX_LOW_FORCE_POSTURE_EFFECTIVE_KO = 1e-9
DEFAULT_MAX_LOW_FORCE_POSTURE_GAIN_SCALE = 1e-9
DEFAULT_LOW_FORCE_POSTURE_GAIN_EFFECTIVE_KO_TOL = 1e-6
DEFAULT_MIN_STAGE25_ACCEPTED_DURATION_S = 60.0
DEFAULT_STAGE25_ACCEPTED_DURATION_TOLERANCE_S = 0.05
DEFAULT_MAX_P0_LIMITED_BASE_UPWARD_M_S = 1e-6
DEFAULT_MAX_P0_LIMITED_TCP_XY_M_S = 0.010000001
DEFAULT_MAX_P0_LIMITED_TCP_Z_M_S = 0.020000001
DEFAULT_MAX_P0_LIMITED_TCP_ANGULAR_RAD_S = 0.015000001
DEFAULT_MAX_STAGE25_ACCEPTED_ROW_GAP_S = 0.020
DEFAULT_P0_RNN_BACKEND = "cupy"
DEFAULT_P0_RNN_MIN_INNER_ITERATIONS = 1024
DEFAULT_P0_RNN_EPSILON = 0.010
DEFAULT_P0_RNN_SIGR_EXPONENT_R = 0.8
DEFAULT_P0_RNN_PARAM_TOL = 1e-6
STAGE25_TOLERANCE = 0.05
INTEGER_TOLERANCE = 1e-9
ENTRY_ECHO_WINDOW_ROWS = 8
P0_LOW_FORCE_POSTURE_POLICY = "freeze_until_contact_v1"
P0_FRAME_TRANSFORM_MODE = "tcp_same_origin_v1"


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
        "cmd_valid": finite_int(row.get("step4e_cmd_valid")),
        "stage25_echo_layout_tag": finite_float(row.get("_step5d_stage25_echo_layout_tag")),
        "stage25_echo_consumed": finite_int(row.get("_step5d_stage25_echo_consumed")),
        "p0_rnn_accepted": finite_int(row.get("_step5d_p0_rnn_accepted")),
        "p0_rnn_reject_reason": str(row.get("_step5d_p0_rnn_reject_reason") or ""),
        "p0_safe_hold_active": finite_int(row.get("_step5d_p0_safe_hold_active")),
        "cmd_valid_reason": str(row.get("_step5d_cmd_valid_reason") or ""),
        "intervention_reason": str(row.get("_step5d_intervention_reason") or ""),
        "outer_approach_normal_m_s": finite_float(row.get("_step5d_outer_xdot_limited_approach_normal_m_s")),
        "jqdot_raw_approach_normal_m_s": finite_float(row.get("_step5d_jqdot_raw_approach_normal_m_s")),
        "jqdot_cmd_approach_normal_m_s": finite_float(row.get("_step5d_jqdot_cmd_approach_normal_m_s")),
        "qdot_max_abs_rad_s": finite_float(row.get("_step5d_qdot_max_abs_rad_s")),
        "constraint_residual_norm": finite_float(row.get("_step5d_constraint_residual_norm")),
        "lambda_norm": finite_float(row.get("_step5d_lambda_norm")),
        "active_bounds_count": finite_int(row.get("_step5d_active_bounds_count")),
        "normal_load_n": finite_float(row.get("_step4e_normal_load_n")),
        "force_norm_n": finite_float(row.get("force_norm_n")),
        "p0_low_force_posture_policy": str(row.get("_step5d_p0_low_force_posture_policy") or ""),
        "p0_low_force_posture_active": finite_int(row.get("_step5d_p0_low_force_posture_active")),
        "p0_posture_gain_scale": finite_float(row.get("_step5d_p0_posture_gain_scale")),
        "p0_effective_ko": finite_float(row.get("_step5d_p0_effective_ko")),
        "p0_frame_transform_valid": finite_int(row.get("_step5d_p0_frame_transform_valid")),
        "p0_frame_transform_mode": str(row.get("_step5d_p0_frame_transform_mode") or ""),
        "p0_frame_transform_reason": str(row.get("_step5d_p0_frame_transform_reason") or ""),
        "p0_limited_tcp_vx_m_s": finite_float(row.get("_step5d_p0_limited_tcp_vx_m_s")),
        "p0_limited_tcp_vy_m_s": finite_float(row.get("_step5d_p0_limited_tcp_vy_m_s")),
        "p0_limited_tcp_vz_m_s": finite_float(row.get("_step5d_p0_limited_tcp_vz_m_s")),
        "p0_limited_tcp_wx_rad_s": finite_float(row.get("_step5d_p0_limited_tcp_wx_rad_s")),
        "p0_limited_tcp_wy_rad_s": finite_float(row.get("_step5d_p0_limited_tcp_wy_rad_s")),
        "p0_limited_tcp_wz_rad_s": finite_float(row.get("_step5d_p0_limited_tcp_wz_rad_s")),
        "p0_limited_base_vz_m_s": finite_float(row.get("_step5d_p0_limited_base_vz_m_s")),
        "p0_tcp_press_speed_m_s": finite_float(row.get("_step5d_p0_tcp_press_speed_m_s")),
        "p0_rnn_inner_iterations": finite_int(row.get("_step5d_rnn_inner_iterations")),
        "p0_rnn_backend": str(row.get("_step5d_rnn_backend") or ""),
        "p0_rnn_epsilon": finite_float(row.get("_step5d_rnn_epsilon")),
        "p0_rnn_sigr_exponent_r": finite_float(row.get("_step5d_rnn_sigr_exponent_r")),
    }


def row_cmd_valid(row: dict[str, str]) -> bool:
    for field in ("step4e_cmd_valid", "_step5d_stage25_echo_cmd_valid"):
        value = finite_float(row.get(field))
        if value is not None:
            return value > 0.5
    return False


def row_stage25_consumed(row: dict[str, str]) -> bool:
    return finite_int(row.get("_step5d_stage25_echo_consumed")) == 1


def row_layout_524(row: dict[str, str]) -> bool:
    value = finite_float(row.get("_step5d_stage25_echo_layout_tag"))
    return value is not None and abs(value - 524.0) <= 1e-3


def row_p0_rnn_accepted(row: dict[str, str]) -> bool:
    return finite_int(row.get("_step5d_p0_rnn_accepted")) == 1


def row_live_speedj_cmd(row: dict[str, str]) -> bool:
    return row_layout_524(row) and row_cmd_valid(row)


def row_accepted_speedj_rnn_evidence(row: dict[str, str]) -> bool:
    return row_live_speedj_cmd(row) and row_p0_rnn_accepted(row)


def stage25_entry_window_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    window = rows[:ENTRY_ECHO_WINDOW_ROWS]
    consumed_first_index = None
    for idx, row in enumerate(window):
        if finite_int(row.get("_step5d_stage25_echo_consumed")) == 1:
            consumed_first_index = idx
            break
    return {
        "rows": len(window),
        "max_rows": ENTRY_ECHO_WINDOW_ROWS,
        "stage25_consumed_seen": consumed_first_index is not None,
        "stage25_consumed_first_row_index": consumed_first_index,
    }


def qdot_cap_from_rows(rows: list[dict[str, str]], fallback: float) -> tuple[float, str]:
    for row in rows:
        value = finite_float(row.get("_step5d_qdot_cap_rad_s"))
        if value is not None and value > 0.0:
            return value, "artifact:_step5d_qdot_cap_rad_s"
    return float(fallback), "default"


def accepted_duration_s(rows: list[dict[str, str]], *, max_gap_s: float) -> tuple[float | None, int, int]:
    segments: list[tuple[float, float]] = []
    current_start: float | None = None
    current_prev: float | None = None
    missing = 0
    gaps = 0
    for row in rows:
        timestamp = finite_float(row.get("t_monotonic_s"))
        if timestamp is None:
            missing += 1
            current_start = None
            current_prev = None
            continue
        if current_start is None or current_prev is None:
            current_start = timestamp
            current_prev = timestamp
            continue
        if timestamp - current_prev > max_gap_s:
            gaps += 1
            segments.append((current_start, current_prev))
            current_start = timestamp
        else:
            current_prev = timestamp
            continue
        current_prev = timestamp
    if current_start is not None and current_prev is not None:
        segments.append((current_start, current_prev))
    if not segments:
        return None, missing, gaps
    return max((end - start for start, end in segments), default=0.0), missing, gaps


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
    qdot_cap_rad_s: float = DEFAULT_QDOT_CAP_RAD_S,
    qdot_rail_margin_rad_s: float = DEFAULT_QDOT_RAIL_MARGIN_RAD_S,
    low_force_posture_load_n: float = DEFAULT_LOW_FORCE_POSTURE_LOAD_N,
    low_force_posture_base_ko: float = DEFAULT_LOW_FORCE_POSTURE_BASE_KO,
    max_low_force_posture_effective_ko: float = DEFAULT_MAX_LOW_FORCE_POSTURE_EFFECTIVE_KO,
    max_low_force_posture_gain_scale: float = DEFAULT_MAX_LOW_FORCE_POSTURE_GAIN_SCALE,
    min_stage25_accepted_duration_s: float = DEFAULT_MIN_STAGE25_ACCEPTED_DURATION_S,
    stage25_accepted_duration_tolerance_s: float = DEFAULT_STAGE25_ACCEPTED_DURATION_TOLERANCE_S,
    max_p0_limited_base_upward_m_s: float = DEFAULT_MAX_P0_LIMITED_BASE_UPWARD_M_S,
    max_p0_limited_tcp_xy_m_s: float = DEFAULT_MAX_P0_LIMITED_TCP_XY_M_S,
    max_p0_limited_tcp_z_m_s: float = DEFAULT_MAX_P0_LIMITED_TCP_Z_M_S,
    max_p0_limited_tcp_angular_rad_s: float = DEFAULT_MAX_P0_LIMITED_TCP_ANGULAR_RAD_S,
    max_stage25_accepted_row_gap_s: float = DEFAULT_MAX_STAGE25_ACCEPTED_ROW_GAP_S,
    expected_p0_rnn_backend: str = DEFAULT_P0_RNN_BACKEND,
    min_p0_rnn_inner_iterations: int = DEFAULT_P0_RNN_MIN_INNER_ITERATIONS,
    expected_p0_rnn_epsilon: float = DEFAULT_P0_RNN_EPSILON,
    expected_p0_rnn_sigr_exponent_r: float = DEFAULT_P0_RNN_SIGR_EXPONENT_R,
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
    qdot_cap_rad_s, qdot_cap_source = qdot_cap_from_rows(rnn_rows, qdot_cap_rad_s)
    if qdot_cap_source == "default":
        blockers.append("qdot_cap_evidence_missing")
    qdot_rail_threshold = float(qdot_cap_rad_s) - float(qdot_rail_margin_rad_s)

    first = first_tick_summary(rnn_rows[0])
    intervention_reason = first["intervention_reason"]
    if "solver_warm_start" not in intervention_reason.split("|"):
        blockers.append("first_speedj_rnn_tick_missing_solver_warm_start")
    entry_window = stage25_entry_window_summary(rnn_rows)
    if not entry_window["stage25_consumed_seen"]:
        blockers.append("stage25_entry_window_not_consumed_by_stage25")

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

    low_force_posture_rows = [
        row
        for row in rnn_rows
        if row_live_speedj_cmd(row)
        and row_stage25_consumed(row)
        and (finite_float(row.get("_step4e_normal_load_n")) is not None)
        and finite_float(row.get("_step4e_normal_load_n")) <= low_force_posture_load_n
    ]
    low_force_posture_missing_rows = 0
    low_force_posture_inactive_rows = 0
    low_force_posture_policy_bad_rows = 0
    low_force_posture_effective_ko_bad_rows = 0
    low_force_posture_gain_scale_bad_rows = 0
    low_force_posture_gain_effective_ko_mismatch_rows = 0
    for row in low_force_posture_rows:
        policy = str(row.get("_step5d_p0_low_force_posture_policy") or "")
        active = finite_int(row.get("_step5d_p0_low_force_posture_active"))
        gain_scale = finite_float(row.get("_step5d_p0_posture_gain_scale"))
        effective_ko = finite_float(row.get("_step5d_p0_effective_ko"))
        if active is None or gain_scale is None or effective_ko is None or not policy:
            low_force_posture_missing_rows += 1
            continue
        if policy != P0_LOW_FORCE_POSTURE_POLICY:
            low_force_posture_policy_bad_rows += 1
        if active != 1:
            low_force_posture_inactive_rows += 1
        if effective_ko > max_low_force_posture_effective_ko:
            low_force_posture_effective_ko_bad_rows += 1
        if gain_scale > max_low_force_posture_gain_scale:
            low_force_posture_gain_scale_bad_rows += 1
        if abs(gain_scale * low_force_posture_base_ko - effective_ko) > DEFAULT_LOW_FORCE_POSTURE_GAIN_EFFECTIVE_KO_TOL:
            low_force_posture_gain_effective_ko_mismatch_rows += 1
    if low_force_posture_rows and low_force_posture_missing_rows:
        blockers.append("p0_low_force_posture_evidence_missing")
    if low_force_posture_policy_bad_rows:
        blockers.append("p0_low_force_posture_policy_mismatch")
    if low_force_posture_inactive_rows:
        blockers.append("p0_low_force_posture_not_active")
    if low_force_posture_effective_ko_bad_rows:
        blockers.append("p0_low_force_posture_effective_ko_exceeds_limit")
    if low_force_posture_gain_scale_bad_rows:
        blockers.append("p0_low_force_posture_gain_scale_exceeds_limit")
    if low_force_posture_gain_effective_ko_mismatch_rows:
        blockers.append("p0_low_force_posture_gain_scale_effective_ko_mismatch")

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
    if first["qdot_max_abs_rad_s"] is None:
        blockers.append("first_speedj_rnn_tick_missing_qdot_max_abs")

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

    first_consumed_rnn_index = next(
        (idx for idx, row in enumerate(rnn_rows) if row_stage25_consumed(row)),
        None,
    )
    missing_acceptance_rows = sum(
        1 for row in rnn_rows if finite_int(row.get("_step5d_p0_rnn_accepted")) is None
    )
    accepted_candidate_rows = [
        row for row in rnn_rows if row_accepted_speedj_rnn_evidence(row)
    ]
    accepted_unconsumed_rows = sum(
        1
        for idx, row in enumerate(rnn_rows)
        if row_accepted_speedj_rnn_evidence(row)
        and not row_stage25_consumed(row)
        and first_consumed_rnn_index is not None
        and idx >= first_consumed_rnn_index
    )
    consumed_accepted_rows = [row for row in accepted_candidate_rows if row_stage25_consumed(row)]
    accepted_rows = consumed_accepted_rows
    accepted_active_bounds_rows = 0
    accepted_high_residual_rows = 0
    accepted_rail_rows = 0
    accepted_qdot_missing_rows = 0
    p0_frame_transform_missing_rows = 0
    p0_frame_transform_invalid_rows = 0
    p0_frame_transform_mode_bad_rows = 0
    p0_limited_base_upward_rows = 0
    p0_limited_tcp_unload_rows = 0
    p0_limited_tcp_component_cap_rows = 0
    p0_rnn_backend_bad_rows = 0
    p0_rnn_tuning_missing_rows = 0
    p0_rnn_inner_iterations_low_rows = 0
    p0_rnn_epsilon_bad_rows = 0
    p0_rnn_sigr_exponent_r_bad_rows = 0
    if missing_acceptance_rows:
        blockers.append("p0_rnn_acceptance_evidence_missing")
    if not accepted_rows:
        blockers.append("no_accepted_speedj_rnn_live_rows")
    if accepted_unconsumed_rows:
        blockers.append("accepted_speedj_rnn_rows_not_consumed_by_stage25")
    (
        stage25_accepted_duration_s,
        stage25_accepted_duration_missing_rows,
        stage25_accepted_duration_gap_rows,
    ) = accepted_duration_s(consumed_accepted_rows, max_gap_s=max_stage25_accepted_row_gap_s)
    if min_stage25_accepted_duration_s > 0.0:
        if stage25_accepted_duration_s is None:
            blockers.append("stage25_accepted_duration_evidence_missing")
        elif stage25_accepted_duration_s + stage25_accepted_duration_tolerance_s < min_stage25_accepted_duration_s:
            blockers.append("stage25_accepted_duration_below_success_target")
            if stage25_accepted_duration_gap_rows:
                blockers.append("stage25_accepted_duration_continuity_gap")
    for row in accepted_rows:
        accepted_active_bounds = finite_int(row.get("_step5d_active_bounds_count"))
        if accepted_active_bounds is not None and accepted_active_bounds > max_active_bounds:
            accepted_active_bounds_rows += 1
        accepted_residual = finite_float(row.get("_step5d_constraint_residual_norm"))
        if accepted_residual is not None and accepted_residual > max_residual_norm:
            accepted_high_residual_rows += 1
        accepted_qdot_max = finite_float(row.get("_step5d_qdot_max_abs_rad_s"))
        if accepted_qdot_max is None:
            accepted_qdot_missing_rows += 1
        elif accepted_qdot_max >= qdot_rail_threshold:
            accepted_rail_rows += 1

        frame_valid = finite_int(row.get("_step5d_p0_frame_transform_valid"))
        frame_mode = str(row.get("_step5d_p0_frame_transform_mode") or "")
        tcp_vx = finite_float(row.get("_step5d_p0_limited_tcp_vx_m_s"))
        tcp_vy = finite_float(row.get("_step5d_p0_limited_tcp_vy_m_s"))
        tcp_vz = finite_float(row.get("_step5d_p0_limited_tcp_vz_m_s"))
        tcp_wx = finite_float(row.get("_step5d_p0_limited_tcp_wx_rad_s"))
        tcp_wy = finite_float(row.get("_step5d_p0_limited_tcp_wy_rad_s"))
        tcp_wz = finite_float(row.get("_step5d_p0_limited_tcp_wz_rad_s"))
        base_vz = finite_float(row.get("_step5d_p0_limited_base_vz_m_s"))
        tcp_press = finite_float(row.get("_step5d_p0_tcp_press_speed_m_s"))
        if (
            frame_valid is None
            or not frame_mode
            or tcp_vx is None
            or tcp_vy is None
            or tcp_vz is None
            or tcp_wx is None
            or tcp_wy is None
            or tcp_wz is None
            or base_vz is None
            or tcp_press is None
        ):
            p0_frame_transform_missing_rows += 1
        else:
            if frame_valid != 1:
                p0_frame_transform_invalid_rows += 1
            if frame_mode != P0_FRAME_TRANSFORM_MODE:
                p0_frame_transform_mode_bad_rows += 1
            if base_vz > max_p0_limited_base_upward_m_s:
                p0_limited_base_upward_rows += 1
            if tcp_vz < -INTEGER_TOLERANCE or tcp_press < -INTEGER_TOLERANCE:
                p0_limited_tcp_unload_rows += 1
            if (
                abs(tcp_vx) > max_p0_limited_tcp_xy_m_s
                or abs(tcp_vy) > max_p0_limited_tcp_xy_m_s
                or tcp_vz > max_p0_limited_tcp_z_m_s
                or abs(tcp_wx) > max_p0_limited_tcp_angular_rad_s
                or abs(tcp_wy) > max_p0_limited_tcp_angular_rad_s
                or abs(tcp_wz) > max_p0_limited_tcp_angular_rad_s
            ):
                p0_limited_tcp_component_cap_rows += 1

        backend = str(row.get("_step5d_rnn_backend") or "")
        inner_iterations = finite_int(row.get("_step5d_rnn_inner_iterations"))
        epsilon = finite_float(row.get("_step5d_rnn_epsilon"))
        sigr_exponent_r = finite_float(row.get("_step5d_rnn_sigr_exponent_r"))
        if not backend or inner_iterations is None or epsilon is None or sigr_exponent_r is None:
            p0_rnn_tuning_missing_rows += 1
        if backend != expected_p0_rnn_backend:
            p0_rnn_backend_bad_rows += 1
        if inner_iterations is not None and inner_iterations < min_p0_rnn_inner_iterations:
            p0_rnn_inner_iterations_low_rows += 1
        if epsilon is not None and abs(epsilon - expected_p0_rnn_epsilon) > DEFAULT_P0_RNN_PARAM_TOL:
            p0_rnn_epsilon_bad_rows += 1
        if (
            sigr_exponent_r is not None
            and abs(sigr_exponent_r - expected_p0_rnn_sigr_exponent_r) > DEFAULT_P0_RNN_PARAM_TOL
        ):
            p0_rnn_sigr_exponent_r_bad_rows += 1
    if accepted_active_bounds_rows:
        blockers.append("accepted_speedj_rnn_tick_active_bounds_exceeds_limit")
    if accepted_high_residual_rows:
        blockers.append("accepted_speedj_rnn_tick_constraint_residual_norm_exceeds_limit")
    if accepted_qdot_missing_rows:
        blockers.append("accepted_speedj_rnn_tick_missing_qdot_max_abs")
    if accepted_rail_rows:
        blockers.append("accepted_speedj_rnn_tick_qdot_hits_rail")
    if p0_frame_transform_missing_rows:
        blockers.append("p0_frame_transform_evidence_missing")
    if p0_frame_transform_invalid_rows:
        blockers.append("p0_frame_transform_invalid")
    if p0_frame_transform_mode_bad_rows:
        blockers.append("p0_frame_transform_mode_mismatch")
    if p0_limited_base_upward_rows:
        blockers.append("p0_limited_base_vz_points_upward")
    if p0_limited_tcp_unload_rows:
        blockers.append("p0_limited_tcp_press_negative")
    if p0_limited_tcp_component_cap_rows:
        blockers.append("p0_limited_tcp_component_exceeds_cap")
    if p0_rnn_tuning_missing_rows:
        blockers.append("p0_rnn_tuning_evidence_missing")
    if p0_rnn_backend_bad_rows:
        blockers.append("p0_rnn_backend_not_cupy")
    if p0_rnn_inner_iterations_low_rows:
        blockers.append("p0_rnn_inner_iterations_below_min")
    if p0_rnn_epsilon_bad_rows:
        blockers.append("p0_rnn_epsilon_mismatch")
    if p0_rnn_sigr_exponent_r_bad_rows:
        blockers.append("p0_rnn_sigr_exponent_r_mismatch")
    accepted_command_rail_fraction = accepted_rail_rows / len(accepted_rows) if accepted_rows else 0.0

    return {
        "ok": not blockers,
        "blockers": blockers,
        "rows": len(rows),
        "speedj_rnn_live_rows": len(mode_rows),
        "stage25_speedj_rnn_live_rows": len(rnn_rows),
        "first_tick": first,
        "entry_window": entry_window,
        "limits": {
            "max_normal_load_n": max_normal_load_n,
            "max_force_norm_n": max_force_norm_n,
            "max_residual_norm": max_residual_norm,
            "max_active_bounds": max_active_bounds,
            "min_lambda_window_ratio": min_lambda_window_ratio,
            "max_normal_tracking_error_m_s": max_normal_tracking_error_m_s,
            "min_first_outer_press_m_s": min_first_outer_press_m_s,
            "qdot_cap_rad_s": qdot_cap_rad_s,
            "qdot_cap_source": qdot_cap_source,
            "qdot_rail_margin_rad_s": qdot_rail_margin_rad_s,
            "qdot_rail_threshold_rad_s": qdot_rail_threshold,
            "low_force_posture_load_n": low_force_posture_load_n,
            "low_force_posture_base_ko": low_force_posture_base_ko,
            "max_low_force_posture_effective_ko": max_low_force_posture_effective_ko,
            "max_low_force_posture_gain_scale": max_low_force_posture_gain_scale,
            "low_force_posture_gain_effective_ko_tol": DEFAULT_LOW_FORCE_POSTURE_GAIN_EFFECTIVE_KO_TOL,
            "min_stage25_accepted_duration_s": min_stage25_accepted_duration_s,
            "stage25_accepted_duration_tolerance_s": stage25_accepted_duration_tolerance_s,
            "p0_frame_transform_mode": P0_FRAME_TRANSFORM_MODE,
            "max_p0_limited_base_upward_m_s": max_p0_limited_base_upward_m_s,
            "max_p0_limited_tcp_xy_m_s": max_p0_limited_tcp_xy_m_s,
            "max_p0_limited_tcp_z_m_s": max_p0_limited_tcp_z_m_s,
            "max_p0_limited_tcp_angular_rad_s": max_p0_limited_tcp_angular_rad_s,
            "max_stage25_accepted_row_gap_s": max_stage25_accepted_row_gap_s,
            "expected_p0_rnn_backend": expected_p0_rnn_backend,
            "min_p0_rnn_inner_iterations": min_p0_rnn_inner_iterations,
            "expected_p0_rnn_epsilon": expected_p0_rnn_epsilon,
            "expected_p0_rnn_sigr_exponent_r": expected_p0_rnn_sigr_exponent_r,
            "p0_rnn_param_tolerance": DEFAULT_P0_RNN_PARAM_TOL,
        },
        "metrics": {
            "max_normal_load_n": max_normal,
            "max_force_norm_n": max_force,
            "lambda_norm_first_to_window_ratio": lambda_ratio,
            "non_stage25_speedj_rnn_live_rows": non_stage25_mode_rows,
            "accepted_speedj_rnn_live_rows": len(accepted_rows),
            "accepted_speedj_rnn_candidate_rows": len(accepted_candidate_rows),
            "p0_rnn_acceptance_missing_rows": missing_acceptance_rows,
            "accepted_active_bounds_rows": accepted_active_bounds_rows,
            "accepted_high_residual_rows": accepted_high_residual_rows,
            "accepted_qdot_missing_rows": accepted_qdot_missing_rows,
            "accepted_qdot_rail_rows": accepted_rail_rows,
            "accepted_command_rail_fraction": accepted_command_rail_fraction,
            "accepted_speedj_rnn_unconsumed_rows": accepted_unconsumed_rows,
            "consumed_accepted_speedj_rnn_live_rows": len(consumed_accepted_rows),
            "stage25_accepted_duration_s": stage25_accepted_duration_s,
            "stage25_accepted_duration_missing_rows": stage25_accepted_duration_missing_rows,
            "stage25_accepted_duration_gap_rows": stage25_accepted_duration_gap_rows,
            "low_force_posture_rows": len(low_force_posture_rows),
            "low_force_posture_missing_rows": low_force_posture_missing_rows,
            "low_force_posture_policy_bad_rows": low_force_posture_policy_bad_rows,
            "low_force_posture_inactive_rows": low_force_posture_inactive_rows,
            "low_force_posture_effective_ko_bad_rows": low_force_posture_effective_ko_bad_rows,
            "low_force_posture_gain_scale_bad_rows": low_force_posture_gain_scale_bad_rows,
            "low_force_posture_gain_effective_ko_mismatch_rows": low_force_posture_gain_effective_ko_mismatch_rows,
            "p0_frame_transform_missing_rows": p0_frame_transform_missing_rows,
            "p0_frame_transform_invalid_rows": p0_frame_transform_invalid_rows,
            "p0_frame_transform_mode_bad_rows": p0_frame_transform_mode_bad_rows,
            "p0_limited_base_upward_rows": p0_limited_base_upward_rows,
            "p0_limited_tcp_unload_rows": p0_limited_tcp_unload_rows,
            "p0_limited_tcp_component_cap_rows": p0_limited_tcp_component_cap_rows,
            "p0_rnn_tuning_missing_rows": p0_rnn_tuning_missing_rows,
            "p0_rnn_backend_bad_rows": p0_rnn_backend_bad_rows,
            "p0_rnn_inner_iterations_low_rows": p0_rnn_inner_iterations_low_rows,
            "p0_rnn_epsilon_bad_rows": p0_rnn_epsilon_bad_rows,
            "p0_rnn_sigr_exponent_r_bad_rows": p0_rnn_sigr_exponent_r_bad_rows,
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
    parser.add_argument("--qdot-cap-rad-s", type=float, default=DEFAULT_QDOT_CAP_RAD_S)
    parser.add_argument("--qdot-rail-margin-rad-s", type=float, default=DEFAULT_QDOT_RAIL_MARGIN_RAD_S)
    parser.add_argument("--low-force-posture-load-n", type=float, default=DEFAULT_LOW_FORCE_POSTURE_LOAD_N)
    parser.add_argument("--low-force-posture-base-ko", type=float, default=DEFAULT_LOW_FORCE_POSTURE_BASE_KO)
    parser.add_argument("--max-low-force-posture-effective-ko", type=float, default=DEFAULT_MAX_LOW_FORCE_POSTURE_EFFECTIVE_KO)
    parser.add_argument("--max-low-force-posture-gain-scale", type=float, default=DEFAULT_MAX_LOW_FORCE_POSTURE_GAIN_SCALE)
    parser.add_argument("--min-stage25-accepted-duration-s", type=float, default=DEFAULT_MIN_STAGE25_ACCEPTED_DURATION_S)
    parser.add_argument("--max-p0-limited-base-upward-m-s", type=float, default=DEFAULT_MAX_P0_LIMITED_BASE_UPWARD_M_S)
    parser.add_argument("--max-p0-limited-tcp-xy-m-s", type=float, default=DEFAULT_MAX_P0_LIMITED_TCP_XY_M_S)
    parser.add_argument("--max-p0-limited-tcp-z-m-s", type=float, default=DEFAULT_MAX_P0_LIMITED_TCP_Z_M_S)
    parser.add_argument("--max-p0-limited-tcp-angular-rad-s", type=float, default=DEFAULT_MAX_P0_LIMITED_TCP_ANGULAR_RAD_S)
    parser.add_argument("--max-stage25-accepted-row-gap-s", type=float, default=DEFAULT_MAX_STAGE25_ACCEPTED_ROW_GAP_S)
    parser.add_argument("--expected-p0-rnn-backend", choices=("numpy", "cupy"), default=DEFAULT_P0_RNN_BACKEND)
    parser.add_argument("--min-p0-rnn-inner-iterations", type=int, default=DEFAULT_P0_RNN_MIN_INNER_ITERATIONS)
    parser.add_argument("--expected-p0-rnn-epsilon", type=float, default=DEFAULT_P0_RNN_EPSILON)
    parser.add_argument("--expected-p0-rnn-sigr-exponent-r", type=float, default=DEFAULT_P0_RNN_SIGR_EXPONENT_R)
    parser.add_argument(
        "--stage25-accepted-duration-tolerance-s",
        type=float,
        default=DEFAULT_STAGE25_ACCEPTED_DURATION_TOLERANCE_S,
    )
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
        qdot_cap_rad_s=args.qdot_cap_rad_s,
        qdot_rail_margin_rad_s=args.qdot_rail_margin_rad_s,
        low_force_posture_load_n=args.low_force_posture_load_n,
        low_force_posture_base_ko=args.low_force_posture_base_ko,
        max_low_force_posture_effective_ko=args.max_low_force_posture_effective_ko,
        max_low_force_posture_gain_scale=args.max_low_force_posture_gain_scale,
        min_stage25_accepted_duration_s=args.min_stage25_accepted_duration_s,
        stage25_accepted_duration_tolerance_s=args.stage25_accepted_duration_tolerance_s,
        max_p0_limited_base_upward_m_s=args.max_p0_limited_base_upward_m_s,
        max_p0_limited_tcp_xy_m_s=args.max_p0_limited_tcp_xy_m_s,
        max_p0_limited_tcp_z_m_s=args.max_p0_limited_tcp_z_m_s,
        max_p0_limited_tcp_angular_rad_s=args.max_p0_limited_tcp_angular_rad_s,
        max_stage25_accepted_row_gap_s=args.max_stage25_accepted_row_gap_s,
        expected_p0_rnn_backend=args.expected_p0_rnn_backend,
        min_p0_rnn_inner_iterations=args.min_p0_rnn_inner_iterations,
        expected_p0_rnn_epsilon=args.expected_p0_rnn_epsilon,
        expected_p0_rnn_sigr_exponent_r=args.expected_p0_rnn_sigr_exponent_r,
    )
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result.get("ok") else 24


if __name__ == "__main__":
    raise SystemExit(main())
