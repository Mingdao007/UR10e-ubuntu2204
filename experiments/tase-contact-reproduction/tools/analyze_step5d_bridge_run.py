#!/usr/bin/env python3
"""Fast Step5d bridge-run diagnosis from one bridge RTDE CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from step5d_runtime_interface import (
    STEP5D_ABLATION_V25_STAGE_ID,
    STEP5D_ABLATION_V26_STAGE_ID,
    STEP5D_ABLATION_V27_STAGE_ID,
    STEP5D_ABLATION_V28_STAGE_ID,
    STEP5D_ABLATION_V29_STAGE_ID,
    STEP5D_ABLATION_V31_STAGE_ID,
    STEP5D_ABLATION_V32_STAGE_ID,
    STEP5D_ABLATION_V33C20_STAGE_ID,
    STEP5D_ABLATION_V33_STAGE_ID,
    STEP5D_LIVEPREP_V24_STAGE_ID,
    STEP5D_NO_CONTACT_P0_STAGE_ID,
    STEP5D_NO_CONTACT_P0_V8_STAGE_ID,
    STEP5D_V27_SPEEDL_LIVE_CONTROL_SOURCE,
    Step5dPreloadGate,
    default_preload_gate,
    stage25_success_target_s,
    uses_step5b_speedl_live_source,
)
import verify_step5d_no_contact_p0
import verify_step5d_no_contact_p0_v8


ANALYSIS_FILENAME = "step5d_bridge_analysis.json"
BRIDGE_CSV_FILENAME = "bridge_rtde_500hz.csv"
METADATA_FILENAME = "metadata.json"
SUMMARY_FILENAME = "summary.json"
STAGE_TOL = 0.005
STAGE25_MAX_ROW_GAP_S = 0.020
STAGE25_MIN_CONSUMPTION_RATIO = 0.95
STAGE25_SUCCESS_MIN_CONSUMPTION_RATIO = 0.98
# Stage25 success-gate normal-load band, calibrated to the proven-stable
# Step5b v3 @12N 60s baseline (runs/bridge_step5b_contact_cycloid_baseline_v3_
# 20260702_113430: observed envelope 5.3..20.6 N) plus margin. The lower bound
# stays above the 2.0 N hard low-load guard; the previous 9.0/15.0 literals
# were calibrated on the v27 10s diagnostic window and rejected runs that the
# reference Step5b controller itself produces over a full 60s pass.
STAGE25_SUCCESS_NORMAL_LOAD_MIN_N = 5.0
STAGE25_SUCCESS_NORMAL_LOAD_MAX_N = 21.0
STAGE25_ORIENTATION_ENTRY_HOLD_S = 0.150
STAGE25_BENIGN_GAP_MAX_S = 0.040
STAGE25_BENIGN_GAP_OUTLIER_LIMIT = 1
STAGE25_BENIGN_RTDE_PHASE_S = 0.0001
STAGE25_SPEEDL_CARTESIAN_MODE = "speedl_cartesian_oracle"
STAGE25_SPEEDJ_DLS_MODE = "speedj_dls_oracle"
STAGE25_SPEEDJ_RNN_MODE = "speedj_rnn_live"
STAGE25_CARTESIAN_LAYOUT_TAG = 523
STAGE25_JOINT_LAYOUT_TAG = 524
NO_CONTACT_P0_PROFILE_RE = re.compile(r"step5d_strict_rnn_no_contact_p0_v\d+")
REQUIRED_COLUMNS = {
    "t_monotonic_s",
    "ur_output_double_register_30",
    "ur_output_double_register_35",
    "_step4e_normal_load_n",
    "_step5d_force_settle_filtered_normal_load_n",
    "force_norm_n",
}


def finite_float(value: Any) -> float:
    if value in (None, ""):
        return math.nan
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return math.nan
    return parsed if math.isfinite(parsed) else math.nan


def percentile_or_none(values: list[float], fraction: float) -> float | None:
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        return None
    index = min(len(finite) - 1, max(0, int(math.ceil(fraction * len(finite))) - 1))
    return finite[index]


def pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    centered_x = [value - mean_x for value in xs]
    centered_y = [value - mean_y for value in ys]
    denominator = math.sqrt(
        sum(value * value for value in centered_x)
        * sum(value * value for value in centered_y)
    )
    if denominator <= 1e-15:
        return None
    return sum(x * y for x, y in zip(centered_x, centered_y)) / denominator


def command_actual_qd_alignment(rows: list[dict[str, str]], max_lag_s: float = 0.020) -> dict[str, float | None]:
    times = [finite_float(row.get("t_monotonic_s")) for row in rows]
    dts = [
        later - earlier
        for earlier, later in zip(times, times[1:])
        if math.isfinite(earlier) and math.isfinite(later) and later > earlier
    ]
    dt_s = statistics.median(dts) if dts else 0.002
    max_lag_ticks = max(0, int(math.floor(max_lag_s / dt_s + 1e-9)))
    best_correlation: float | None = None
    best_lag_ticks = 0
    for lag_ticks in range(max_lag_ticks + 1):
        commands: list[float] = []
        actuals: list[float] = []
        for index in range(0, max(0, len(rows) - lag_ticks)):
            actual_row = rows[index + lag_ticks]
            for axis in range(6):
                command = finite_float(rows[index].get(f"_step5d_post_slew_qd{axis}_rad_s"))
                actual = finite_float(actual_row.get(f"ur_actual_qd_{axis}"))
                if math.isfinite(command) and math.isfinite(actual):
                    commands.append(command)
                    actuals.append(actual)
        correlation = pearson_correlation(commands, actuals)
        if correlation is not None and (best_correlation is None or correlation > best_correlation):
            best_correlation = correlation
            best_lag_ticks = lag_ticks
    return {
        "command_actual_qd_correlation": best_correlation,
        "command_actual_qd_lag_s": best_lag_ticks * dt_s if best_correlation is not None else None,
    }


def maybe_int(value: float) -> int | float:
    rounded = int(round(value))
    if math.isclose(value, float(rounded), abs_tol=1e-9):
        return rounded
    return value


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def infer_step5d_profile(run_dir: Path | None, metadata: dict[str, Any]) -> str:
    args = metadata.get("args")
    if isinstance(args, dict):
        for key in ("bridge_profile", "bridge_version", "step4e_version"):
            value = str(args.get(key) or "")
            if value.startswith("step5d_strict_rnn_"):
                return value
    if run_dir is not None:
        name = run_dir.name
        no_contact_match = NO_CONTACT_P0_PROFILE_RE.search(name)
        if no_contact_match:
            return no_contact_match.group(0)
        for profile in (
            STEP5D_NO_CONTACT_P0_STAGE_ID,
            STEP5D_ABLATION_V33C20_STAGE_ID,
            STEP5D_ABLATION_V33_STAGE_ID,
            STEP5D_ABLATION_V32_STAGE_ID,
            STEP5D_ABLATION_V31_STAGE_ID,
            STEP5D_ABLATION_V29_STAGE_ID,
            STEP5D_ABLATION_V28_STAGE_ID,
            STEP5D_ABLATION_V27_STAGE_ID,
            STEP5D_ABLATION_V26_STAGE_ID,
            STEP5D_ABLATION_V25_STAGE_ID,
            STEP5D_LIVEPREP_V24_STAGE_ID,
        ):
            if profile in name:
                return profile
    return STEP5D_ABLATION_V25_STAGE_ID


def is_no_contact_p0_profile(profile: str) -> bool:
    return bool(NO_CONTACT_P0_PROFILE_RE.fullmatch(profile))


def no_contact_p0_verification(
    profile: str,
    *,
    run_dir: Path | None,
    csv_path: Path,
) -> tuple[dict[str, Any] | None, float | None, str | None]:
    if not is_no_contact_p0_profile(profile):
        return None, None, None
    if profile != STEP5D_NO_CONTACT_P0_V8_STAGE_ID:
        return (
            verify_step5d_no_contact_p0.verify_run_dir(run_dir if run_dir is not None else csv_path),
            None,
            "verify_step5d_no_contact_p0.py",
        )
    if run_dir is None:
        return (
            {"ok": False, "canary_passed": False, "p0_v8_passed": False, "blockers": ["run_dir_missing"]},
            None,
            "verify_step5d_no_contact_p0_v8.py",
        )
    manifest = read_json(run_dir / "bridge_run_manifest.json")
    phase_s = finite_float((manifest.get("p0_v8_canary") or {}).get("phase_s"))
    configured_phase_s = verify_step5d_no_contact_p0_v8.configured_direct_duration()
    if not math.isclose(phase_s, configured_phase_s, abs_tol=1e-9):
        return (
            {
                "ok": False,
                "canary_passed": False,
                "p0_v8_passed": False,
                "blockers": ["bridge_run_manifest_canary_phase_missing_or_invalid"],
            },
            None,
            "verify_step5d_no_contact_p0_v8.py",
        )
    try:
        verified = verify_step5d_no_contact_p0_v8.verify(run_dir, phase_s=phase_s)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        verified = {
            "ok": False,
            "canary_passed": False,
            "p0_v8_passed": False,
            "blockers": [f"p0_v8_verifier_error:{type(exc).__name__}"],
        }
    return verified, phase_s, "verify_step5d_no_contact_p0_v8.py"


def preload_gate_for(run_dir: Path | None) -> tuple[str, Step5dPreloadGate]:
    metadata = read_json(run_dir / METADATA_FILENAME) if run_dir is not None else {}
    profile = infer_step5d_profile(run_dir, metadata)
    defaults = default_preload_gate(profile)
    args = metadata.get("args")
    if not isinstance(args, dict):
        return profile, defaults

    def arg_float(key: str, default: float) -> float:
        parsed = finite_float(args.get(key))
        return default if not math.isfinite(parsed) else parsed

    return profile, Step5dPreloadGate(
        filtered_min_n=arg_float("step5d_preload_filtered_min_n", defaults.filtered_min_n),
        filtered_max_n=arg_float("step5d_preload_filtered_max_n", defaults.filtered_max_n),
        raw_min_n=arg_float("step5d_preload_raw_min_n", defaults.raw_min_n),
        raw_max_n=arg_float("step5d_preload_raw_max_n", defaults.raw_max_n),
        force_norm_max_n=arg_float("step5d_preload_force_norm_max_n", defaults.force_norm_max_n),
        hold_s=arg_float("step5d_preload_hold_s", defaults.hold_s),
        timeout_s=arg_float("step5d_preload_timeout_s", defaults.timeout_s),
        cmd_limit_m_s=defaults.cmd_limit_m_s,
        recovery_normal_load_min_n=defaults.recovery_normal_load_min_n,
        recovery_normal_load_max_n=defaults.recovery_normal_load_max_n,
        force_norm_stop_n=defaults.force_norm_stop_n,
    )


def stage_is(stage: float, target: float) -> bool:
    return math.isfinite(stage) and abs(stage - target) < STAGE_TOL


def preload_ready(row: dict[str, str], gate: Step5dPreloadGate) -> bool:
    raw_load = finite_float(row.get("_step4e_normal_load_n"))
    filtered_load = finite_float(row.get("_step5d_force_settle_filtered_normal_load_n"))
    force_norm = finite_float(row.get("force_norm_n"))
    if not math.isfinite(filtered_load):
        filtered_load = raw_load
    return (
        math.isfinite(raw_load)
        and math.isfinite(filtered_load)
        and math.isfinite(force_norm)
        and gate.raw_min_n <= raw_load <= gate.raw_max_n
        and gate.filtered_min_n <= filtered_load <= gate.filtered_max_n
        and force_norm <= gate.force_norm_max_n
    )


def metadata_float(metadata: dict[str, Any], *keys: str) -> float:
    args = metadata.get("args")
    if not isinstance(args, dict):
        return math.nan
    for key in keys:
        parsed = finite_float(args.get(key))
        if math.isfinite(parsed):
            return parsed
    return math.nan


def metadata_text(metadata: dict[str, Any], *keys: str) -> str | None:
    args = metadata.get("args")
    if not isinstance(args, dict):
        return None
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def finite_values(rows: list[dict[str, str]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        parsed = finite_float(row.get(key))
        if math.isfinite(parsed):
            values.append(parsed)
    return values


def first_finite(rows: list[dict[str, str]], key: str) -> float:
    for row in rows:
        parsed = finite_float(row.get(key))
        if math.isfinite(parsed):
            return parsed
    return math.nan


def last_finite(rows: list[dict[str, str]], key: str) -> float:
    for row in reversed(rows):
        value = finite_float(row.get(key))
        if math.isfinite(value):
            return value
    return math.nan


def range_or_none(values: list[float]) -> float | None:
    return None if not values else max(values) - min(values)


def min_or_none(values: list[float]) -> float | None:
    return None if not values else min(values)


def max_or_none(values: list[float]) -> float | None:
    return None if not values else max(values)


def mean_or_none(values: list[float]) -> float | None:
    return None if not values else sum(values) / len(values)


def load_rates(rows: list[dict[str, str]]) -> list[float]:
    rates: list[float] = []
    previous_t: float | None = None
    previous_load: float | None = None
    for row in rows:
        t_s = finite_float(row.get("t_monotonic_s"))
        load = finite_float(row.get("_step4e_normal_load_n"))
        if (
            previous_t is not None
            and previous_load is not None
            and math.isfinite(t_s)
            and math.isfinite(load)
            and t_s > previous_t
        ):
            rates.append((load - previous_load) / (t_s - previous_t))
        if math.isfinite(t_s) and math.isfinite(load):
            previous_t = t_s
            previous_load = load
    return rates


def row_is_joint_qd_carrier(row: dict[str, str]) -> bool:
    command_layout = finite_float(row.get("step4e_controller_state"))
    return math.isfinite(command_layout) and int(round(command_layout)) == STAGE25_JOINT_LAYOUT_TAG


def angular_norms(rows: list[dict[str, str]]) -> list[float]:
    norms: list[float] = []
    for row in rows:
        if row_is_joint_qd_carrier(row):
            continue
        wx = finite_float(row.get("step4e_cmd_wx_rad_s"))
        wy = finite_float(row.get("step4e_cmd_wy_rad_s"))
        wz = finite_float(row.get("step4e_cmd_wz_rad_s"))
        if math.isfinite(wx) and math.isfinite(wy) and math.isfinite(wz):
            norms.append(math.sqrt(wx * wx + wy * wy + wz * wz))
    return norms


def angular_command_source(rows: list[dict[str, str]], angular: list[float]) -> str:
    command_layout_counts = numeric_tag_counts(rows, "step4e_controller_state")
    if str(STAGE25_JOINT_LAYOUT_TAG) in command_layout_counts and str(STAGE25_CARTESIAN_LAYOUT_TAG) not in command_layout_counts:
        return "unavailable_joint_qd_carrier"
    if angular:
        return "cartesian_step4e_cmd_w_fields"
    return "unavailable_missing_cartesian_fields"


def angular_saturation_claim(source: str) -> str:
    if source == "unavailable_joint_qd_carrier":
        return "unavailable_joint_layout"
    if source == "cartesian_step4e_cmd_w_fields":
        return "cartesian_angular_velocity"
    return "unavailable_missing_fields"


def linear_norms(rows: list[dict[str, str]]) -> list[float]:
    norms: list[float] = []
    for row in rows:
        if row_is_joint_qd_carrier(row):
            continue
        vx = finite_float(row.get("step4e_cmd_vx_m_s"))
        vy = finite_float(row.get("step4e_cmd_vy_m_s"))
        vz = finite_float(row.get("step4e_cmd_vz_m_s"))
        if math.isfinite(vx) and math.isfinite(vy) and math.isfinite(vz):
            norms.append(math.sqrt(vx * vx + vy * vy + vz * vz))
    return norms


def cartesian_command_values(rows: list[dict[str, str]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        if row_is_joint_qd_carrier(row):
            continue
        value = finite_float(row.get(key))
        if math.isfinite(value):
            values.append(value)
    return values


def cartesian_command_source(rows: list[dict[str, str]], values: list[float], *, field_group: str) -> str:
    command_layout_counts = numeric_tag_counts(rows, "step4e_controller_state")
    if str(STAGE25_JOINT_LAYOUT_TAG) in command_layout_counts and str(STAGE25_CARTESIAN_LAYOUT_TAG) not in command_layout_counts:
        return "unavailable_joint_qd_carrier"
    if values:
        return field_group
    return "unavailable_missing_cartesian_fields"


def cartesian_command_claim(source: str) -> str:
    if source == "unavailable_joint_qd_carrier":
        return "unavailable_joint_layout"
    if source.startswith("cartesian_"):
        return "cartesian_velocity"
    return "unavailable_missing_fields"


def shadow_raw_angular_norms(rows: list[dict[str, str]]) -> list[float]:
    norms: list[float] = []
    for row in rows:
        wx = finite_float(row.get("_step5d_speedl_shadow_raw_wx_rad_s"))
        wy = finite_float(row.get("_step5d_speedl_shadow_raw_wy_rad_s"))
        wz = finite_float(row.get("_step5d_speedl_shadow_raw_wz_rad_s"))
        if math.isfinite(wx) and math.isfinite(wy) and math.isfinite(wz):
            norms.append(math.sqrt(wx * wx + wy * wy + wz * wz))
    return norms


def reason_counts(rows: list[dict[str, str]], key: str) -> dict[str, int]:
    counts = Counter(row.get(key) or "" for row in rows)
    return dict(sorted((name, count) for name, count in counts.items() if name))


def numeric_tag_counts(rows: list[dict[str, str]], key: str) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        parsed = finite_float(row.get(key))
        if math.isfinite(parsed):
            counts[str(maybe_int(parsed))] += 1
    return dict(sorted(counts.items()))


def stage25_control_mode_from_rows(rows: list[dict[str, str]]) -> str | None:
    counts = Counter(row.get("_step5d_stage25_control_mode") or "" for row in rows)
    modes = [(mode, count) for mode, count in counts.items() if mode]
    if len(modes) == 1:
        return modes[0][0]
    return None


def approach_normal_tracking(rows: list[dict[str, str]]) -> dict[str, Any]:
    outer_values: list[float] = []
    jqdot_values: list[float] = []
    sign_mismatch_rows = 0
    press_unload_mismatch_rows = 0
    for row in rows:
        outer = finite_float(row.get("_step5d_outer_xdot_limited_approach_normal_m_s"))
        jqdot = finite_float(row.get("_step5d_jqdot_cmd_approach_normal_m_s"))
        if not (math.isfinite(outer) and math.isfinite(jqdot)):
            continue
        outer_values.append(outer)
        jqdot_values.append(jqdot)
        if abs(outer) > 1e-9 and abs(jqdot) > 1e-9 and outer * jqdot < 0.0:
            sign_mismatch_rows += 1
        if outer > 1e-9 and jqdot < -1e-9:
            press_unload_mismatch_rows += 1
    sample_rows = len(outer_values)
    return {
        "approach_normal_sample_rows": sample_rows,
        "approach_normal_outer_mean_m_s": mean_or_none(outer_values),
        "approach_normal_jqdot_cmd_mean_m_s": mean_or_none(jqdot_values),
        "approach_normal_sign_mismatch_rows": sign_mismatch_rows,
        "approach_normal_sign_mismatch_ratio": 0.0 if sample_rows == 0 else sign_mismatch_rows / sample_rows,
        "approach_normal_press_unload_mismatch_rows": press_unload_mismatch_rows,
        "approach_normal_press_unload_mismatch_ratio": (
            0.0 if sample_rows == 0 else press_unload_mismatch_rows / sample_rows
        ),
    }


def stage25_segment_metrics(rows: list[dict[str, str]], angular_limit: float) -> dict[str, Any]:
    angular = angular_norms(rows)
    linear = linear_norms(rows)
    linear_vz = cartesian_command_values(rows, "step4e_cmd_vz_m_s")
    normal_loads = finite_values(rows, "_step4e_normal_load_n")
    force_norms = finite_values(rows, "force_norm_n")
    orientation = finite_values(rows, "step4e_orientation_error_rad")
    outer_orientation = finite_values(rows, "_step5d_outer_orientation_error_rad")
    t_values = finite_values(rows, "t_monotonic_s")
    saturation_threshold = angular_limit * 0.99 if math.isfinite(angular_limit) and angular_limit > 0.0 else math.inf
    saturated = sum(1 for value in angular if value >= saturation_threshold)
    return {
        "rows": len(rows),
        "duration_s": max(t_values) - min(t_values) if len(t_values) >= 2 else 0.0,
        "normal_load_min_n": min_or_none(normal_loads),
        "normal_load_max_n": max_or_none(normal_loads),
        "normal_load_mean_n": mean_or_none(normal_loads),
        "force_norm_max_n": max_or_none(force_norms),
        "linear_cmd_norm_abs_max_m_s": max_or_none(linear),
        "linear_cmd_norm_mean_m_s": mean_or_none(linear),
        "linear_cmd_norm_source": cartesian_command_source(rows, linear, field_group="cartesian_step4e_cmd_v_fields"),
        "linear_vz_cmd_mean_m_s": mean_or_none(linear_vz),
        "linear_vz_cmd_source": cartesian_command_source(rows, linear_vz, field_group="cartesian_step4e_cmd_vz_field"),
        "linear_approach_cmd_mean_m_s": mean_or_none(
            finite_values(rows, "_step5d_outer_xdot_limited_approach_normal_m_s")
        ),
        "angular_cmd_norm_max_rad_s": max_or_none(angular),
        "angular_cmd_norm_mean_rad_s": mean_or_none(angular),
        "angular_saturation_ratio": 0.0 if not angular else saturated / len(angular),
        "normal_filter_source_counts": reason_counts(rows, "_step4e_normal_filter_source"),
        "contact_safety_reason_counts": reason_counts(rows, "_step5d_contact_safety_reason"),
        "normal_filter_lag_angle_abs_max_rad": max_or_none(
            [abs(value) for value in finite_values(rows, "_step4e_live_normal_candidate_angle_rad")]
        ),
        "normal_filter_latch_lag_angle_abs_max_rad": max_or_none(
            [abs(value) for value in finite_values(rows, "_step4e_live_normal_angle_from_latch_rad")]
        ),
        "orientation_error_first_rad": orientation[0] if orientation else None,
        "orientation_error_last_rad": orientation[-1] if orientation else None,
        "outer_orientation_error_first_rad": outer_orientation[0] if outer_orientation else None,
        "outer_orientation_error_last_rad": outer_orientation[-1] if outer_orientation else None,
    }


def stage25_segment_diagnostics(rows: list[dict[str, str]], angular_limit: float) -> dict[str, Any]:
    first_t = first_finite(rows, "t_monotonic_s")
    entry_rows: list[dict[str, str]] = []
    post_entry_rows: list[dict[str, str]] = []
    shadow_only_rows: list[dict[str, str]] = []
    if math.isfinite(first_t):
        release_index: int | None = None
        for idx, row in enumerate(rows):
            t_s = finite_float(row.get("t_monotonic_s"))
            row_angular = angular_norms([row])
            if row_angular and row_angular[0] <= 1e-6:
                shadow_only_rows.append(row)
            if (
                release_index is None
                and math.isfinite(t_s)
                and t_s - first_t <= STAGE25_ORIENTATION_ENTRY_HOLD_S + 1e-9
                and row_angular
                and row_angular[0] > 1e-6
            ):
                release_index = idx
        if release_index is not None:
            entry_rows = rows[:release_index]
            post_entry_rows = rows[release_index:]
        else:
            for row in rows:
                t_s = finite_float(row.get("t_monotonic_s"))
                if math.isfinite(t_s) and t_s - first_t <= STAGE25_ORIENTATION_ENTRY_HOLD_S + 1e-9:
                    entry_rows.append(row)
                elif math.isfinite(t_s):
                    post_entry_rows.append(row)
    loaded_rows = [
        row
        for row in rows
        if (row.get("_step5d_contact_safety_reason") or "") == "ok"
        and finite_float(row.get("_step4e_normal_load_n")) >= 5.0
    ]
    low_load_repress_rows = [
        row
        for row in rows
        if (row.get("_step5d_contact_safety_reason") or "") == "v25_speedl_low_load_repress_window"
    ]
    return {
        "entry_hold": stage25_segment_metrics(entry_rows, angular_limit),
        "shadow_only": stage25_segment_metrics(shadow_only_rows, angular_limit),
        "post_entry_old_behavior": stage25_segment_metrics(post_entry_rows, angular_limit),
        "loaded": stage25_segment_metrics(loaded_rows, angular_limit),
        "low_load_repress": stage25_segment_metrics(low_load_repress_rows, angular_limit),
    }


def orientation_shadow_experiment_classification(
    segments: dict[str, Any],
    trigger: str | None,
) -> str | None:
    entry = segments.get("entry_hold") if isinstance(segments, dict) else {}
    post_entry = segments.get("post_entry_old_behavior") if isinstance(segments, dict) else {}
    if not isinstance(entry, dict) or not isinstance(post_entry, dict):
        return None
    entry_rows = int(entry.get("rows") or 0)
    entry_angular_max = finite_float(entry.get("angular_cmd_norm_max_rad_s"))
    post_entry_angular_mean = finite_float(post_entry.get("angular_cmd_norm_mean_rad_s"))
    if (
        trigger == "hard_low_load_timeout"
        and entry_rows > 0
        and math.isfinite(entry_angular_max)
        and entry_angular_max <= 1e-6
        and math.isfinite(post_entry_angular_mean)
        and post_entry_angular_mean > 0.005
    ):
        return "stage25_orientation_shadow_experiment_failed_low_load_timeout"
    return None


def is_benign_stage25_row_gap(gap_s: float, row: dict[str, str]) -> bool:
    if not (STAGE25_MAX_ROW_GAP_S < gap_s <= STAGE25_BENIGN_GAP_MAX_S):
        return False
    send_s = finite_float(row.get("_bridge_loop_rtde_send_s"))
    recv_s = finite_float(row.get("_bridge_loop_rtde_recv_s"))
    return (
        math.isfinite(send_s)
        and math.isfinite(recv_s)
        and send_s <= STAGE25_BENIGN_RTDE_PHASE_S
        and recv_s <= STAGE25_BENIGN_RTDE_PHASE_S
    )


def last_contact_safety_reason(rows: list[dict[str, str]]) -> str | None:
    for row in reversed(rows):
        reason = row.get("_step5d_contact_safety_reason") or ""
        if reason and reason != "ok":
            return reason
    return None


def row_has_command_layout(row: dict[str, str], layout_tag: int) -> bool:
    tag = finite_float(row.get("step4e_controller_state"))
    return math.isfinite(tag) and maybe_int(tag) == layout_tag


def longest_continuous_duration_s(times: list[float], *, max_gap_s: float) -> float:
    finite_times = sorted(value for value in times if math.isfinite(value))
    if len(finite_times) < 2:
        return 0.0
    best = 0.0
    start = finite_times[0]
    previous = finite_times[0]
    for value in finite_times[1:]:
        if value - previous > max_gap_s:
            best = max(best, previous - start)
            start = value
        previous = value
    return max(best, previous - start)


def stage25_control_attribution(rows: list[dict[str, str]], metadata: dict[str, Any]) -> dict[str, Any]:
    normal_loads = finite_values(rows, "_step4e_normal_load_n")
    lambda_norms = finite_values(rows, "_step5d_lambda_norm")
    force_norms = finite_values(rows, "force_norm_n")
    rates = load_rates(rows)
    angular = angular_norms(rows)
    linear_vz = cartesian_command_values(rows, "step4e_cmd_vz_m_s")
    shadow_angular = shadow_raw_angular_norms(rows)
    angular_limit = metadata_float(metadata, "bridge_angular_limit_rad_s", "step4e_angular_limit_rad_s")
    if not math.isfinite(angular_limit) and angular:
        angular_limit = max(angular)
    saturation_threshold = angular_limit * 0.99 if math.isfinite(angular_limit) and angular_limit > 0.0 else math.inf
    saturated = sum(1 for value in angular if value >= saturation_threshold)
    sources = Counter(row.get("_step4e_normal_filter_source") or "" for row in rows)
    reasons = Counter(row.get("_step5d_contact_safety_reason") or "" for row in rows)
    contact_reason_counts = dict(sorted((key, value) for key, value in reasons.items() if key))
    load_min = min_or_none(normal_loads)
    load_range = range_or_none(normal_loads)
    angular_saturation_ratio = 0.0 if not angular else saturated / len(angular)
    angular_source = angular_command_source(rows, angular)
    linear_vz_source = cartesian_command_source(rows, linear_vz, field_group="cartesian_step4e_cmd_vz_field")
    has_low_load_repress = int(contact_reason_counts.get("v25_speedl_low_load_repress_window", 0)) > 0
    has_low_load_timeout = int(contact_reason_counts.get("v25_speedl_hard_low_load_timeout", 0)) > 0
    has_force_hard_stop = int(contact_reason_counts.get("force_norm_hard_stop", 0)) > 0
    low_load_excursion = (
        isinstance(load_min, (int, float))
        and isinstance(load_range, (int, float))
        and load_min < 2.0
        and load_range > 8.0
    )
    angular_saturated = angular_saturation_ratio >= 0.90
    control_oscillation_trigger: str | None = None
    if has_low_load_timeout:
        control_oscillation_trigger = "hard_low_load_timeout"
    elif has_low_load_repress:
        control_oscillation_trigger = "low_load_repress_window"
    elif low_load_excursion and angular_saturated:
        control_oscillation_trigger = "low_load_excursion_with_angular_saturation"
    elif has_force_hard_stop:
        control_oscillation_trigger = "force_norm_hard_stop"
    segment_diagnostics = stage25_segment_diagnostics(rows, angular_limit)
    accepted_rnn_rows = [
        row
        for row in rows
        if finite_float(row.get("_step5d_rnn_accepted")) >= 0.5
        and finite_float(row.get("_step5d_safe_hold_active")) < 0.5
        and finite_float(row.get("step4e_cmd_valid")) >= 0.5
        and row_has_command_layout(row, STAGE25_JOINT_LAYOUT_TAG)
        and finite_float(row.get("_step5d_stage25_echo_consumed")) >= 0.5
    ]
    accepted_rnn_times = [finite_float(row.get("t_monotonic_s")) for row in accepted_rnn_rows]
    accepted_rnn_times = [value for value in accepted_rnn_times if math.isfinite(value)]
    feedback_ages = finite_values(rows, "rtde_feedback_age_s")
    heartbeat_gaps = finite_values(rows, "rtde_sent_echo_heartbeat_gap")
    if not heartbeat_gaps:
        heartbeat_gaps = [
            max(0.0, sent - echoed)
            for row in rows
            for sent, echoed in [
                (
                    finite_float(row.get("heartbeat")),
                    finite_float(row.get("ur_output_double_register_26")),
                )
            ]
            if math.isfinite(sent) and math.isfinite(echoed)
        ]
    drained_packets = finite_values(rows, "rtde_packets_drained")
    xy_errors = [
        math.hypot(x_error, y_error)
        for row in rows
        for x_error, y_error in [
            (
                finite_float(row.get("_step4e_path_error_x_m")),
                finite_float(row.get("_step4e_path_error_y_m")),
            )
        ]
        if math.isfinite(x_error) and math.isfinite(y_error)
    ]
    rnn_oracle_delta = finite_values(rows, "_step5d_rnn_vs_oracle_qdot_norm")
    qd_alignment = command_actual_qd_alignment(rows)
    final_safety_mode = last_finite(rows, "ur_safety_mode")
    gross_guard_rows = sum(1 for row in rows if str(row.get("guard_reason") or "").strip())
    return {
        "stage25_rows": len(rows),
        "entry_orientation_error_rad": first_finite(rows, "step4e_orientation_error_rad"),
        "entry_outer_orientation_error_rad": first_finite(rows, "_step5d_outer_orientation_error_rad"),
        "angular_limit_rad_s": angular_limit if math.isfinite(angular_limit) else None,
        "angular_cmd_norm_max_rad_s": max_or_none(angular),
        "angular_cmd_norm_mean_rad_s": mean_or_none(angular),
        "angular_cmd_norm_source": angular_source,
        "angular_saturation_claim": angular_saturation_claim(angular_source),
        "shadow_raw_angular_cmd_norm_max_rad_s": max_or_none(shadow_angular),
        "shadow_raw_angular_cmd_norm_mean_rad_s": mean_or_none(shadow_angular),
        "orientation_shadow_only_rows": sum(
            1 for value in finite_values(rows, "_step5d_speedl_orientation_shadow_only") if value >= 0.5
        ),
        "angular_saturation_rows": saturated,
        "angular_saturation_ratio": angular_saturation_ratio,
        "linear_vz_cmd_abs_max_m_s": max_or_none([abs(value) for value in linear_vz]),
        "linear_vz_cmd_mean_m_s": mean_or_none(linear_vz),
        "linear_vz_cmd_source": linear_vz_source,
        "linear_vz_claim": cartesian_command_claim(linear_vz_source),
        "linear_approach_cmd_abs_max_m_s": max_or_none(
            [abs(value) for value in finite_values(rows, "_step5d_outer_xdot_limited_approach_normal_m_s")]
        ),
        "linear_approach_cmd_mean_m_s": mean_or_none(finite_values(rows, "_step5d_outer_xdot_limited_approach_normal_m_s")),
        "normal_load_min_n": load_min,
        "normal_load_max_n": max_or_none(normal_loads),
        "normal_load_range_n": load_range,
        "force_norm_max_n": max_or_none(force_norms),
        "normal_load_rate_abs_max_n_s": max_or_none([abs(value) for value in rates]),
        "normal_load_rate_max_n_s": max_or_none(rates),
        "normal_load_rate_min_n_s": min_or_none(rates),
        "normal_filter_source_counts": dict(sorted((key, value) for key, value in sources.items() if key)),
        "live_control_source_counts": reason_counts(rows, "_step5d_live_control_source"),
        "layout_tag_counts": numeric_tag_counts(rows, "_step5d_stage25_echo_layout_tag"),
        "command_layout_tag_counts": numeric_tag_counts(rows, "step4e_controller_state"),
        "contact_safety_reason_counts": contact_reason_counts,
        "terminal_contact_safety_reason": last_contact_safety_reason(rows),
        "control_oscillation_trigger": control_oscillation_trigger,
        "normal_filter_lag_angle_abs_max_rad": max_or_none(
            [abs(value) for value in finite_values(rows, "_step4e_live_normal_candidate_angle_rad")]
        ),
        "normal_filter_latch_lag_angle_abs_max_rad": max_or_none(
            [abs(value) for value in finite_values(rows, "_step4e_live_normal_angle_from_latch_rad")]
        ),
        "stage25_segment_diagnostics": segment_diagnostics,
        "orientation_shadow_experiment_classification": orientation_shadow_experiment_classification(
            segment_diagnostics,
            control_oscillation_trigger,
        ),
        "rnn_solver_eval_rows": len(lambda_norms),
        "rnn_lambda_norm_first": lambda_norms[0] if lambda_norms else None,
        "rnn_lambda_norm_last": lambda_norms[-1] if lambda_norms else None,
        "rnn_accepted_rows": len(accepted_rnn_rows),
        "rnn_accepted_duration_s": (
            max(accepted_rnn_times) - min(accepted_rnn_times)
            if len(accepted_rnn_times) >= 2
            else 0.0
        ),
        "rnn_accepted_continuous_duration_s": longest_continuous_duration_s(
            accepted_rnn_times,
            max_gap_s=STAGE25_MAX_ROW_GAP_S,
        ),
        "rnn_reject_reason_counts": reason_counts(rows, "_step5d_rnn_reject_reason"),
        "rnn_safe_hold_rows": sum(1 for value in finite_values(rows, "_step5d_safe_hold_active") if value >= 0.5),
        "rnn_accepted_consumed_ratio": len(accepted_rnn_rows) / len(rows) if rows else 0.0,
        "feedback_age_p99_s": percentile_or_none(feedback_ages, 0.99),
        "feedback_age_max_s": max_or_none(feedback_ages),
        "sent_echo_heartbeat_gap_max": max_or_none(heartbeat_gaps),
        "drained_packet_count_p99": percentile_or_none(drained_packets, 0.99),
        "drained_packet_count_max": max_or_none(drained_packets),
        "xy_tracking_error_p95_m": percentile_or_none(xy_errors, 0.95),
        "xy_tracking_error_max_m": max_or_none(xy_errors),
        "rnn_oracle_qdot_delta_p99": percentile_or_none(rnn_oracle_delta, 0.99),
        "rnn_oracle_qdot_delta_max": max_or_none(rnn_oracle_delta),
        "final_safety_mode": maybe_int(final_safety_mode) if math.isfinite(final_safety_mode) else None,
        "gross_guard_rows": gross_guard_rows,
        **qd_alignment,
        **approach_normal_tracking(rows),
    }


def stage25_control_oscillation_reason(attribution: dict[str, Any]) -> str | None:
    trigger = attribution.get("control_oscillation_trigger")
    if trigger in {
        "hard_low_load_timeout",
        "low_load_repress_window",
        "low_load_excursion_with_angular_saturation",
    }:
        return "low_load_timeout"
    if trigger == "force_norm_hard_stop":
        return "force_norm_hard_stop"
    return None


def old_v27_paper_outer_linear_live_gain_mismatch(attribution: dict[str, Any]) -> bool:
    if attribution.get("control_oscillation_trigger") != "force_norm_hard_stop":
        return False
    live_source_counts = attribution.get("live_control_source_counts")
    if isinstance(live_source_counts, dict) and STEP5D_V27_SPEEDL_LIVE_CONTROL_SOURCE in live_source_counts:
        return False
    angular_max = finite_float(attribution.get("angular_cmd_norm_max_rad_s"))
    linear_vz_max = finite_float(attribution.get("linear_vz_cmd_abs_max_m_s"))
    linear_approach_max = finite_float(attribution.get("linear_approach_cmd_abs_max_m_s"))
    force_norm_max = finite_float(attribution.get("force_norm_max_n"))
    linear_live_near_cap = (
        (math.isfinite(linear_vz_max) and linear_vz_max >= 0.0038)
        or (math.isfinite(linear_approach_max) and linear_approach_max >= 0.0038)
    )
    return (
        math.isfinite(angular_max)
        and angular_max <= 1e-6
        and linear_live_near_cap
        and math.isfinite(force_norm_max)
        and force_norm_max >= 20.0
    )


def stage25_speedl_fix_success(
    profile: str,
    result: dict[str, Any],
    *,
    control_mode: str | None,
) -> bool:
    target_s = stage25_success_target_s(profile)
    if not uses_step5b_speedl_live_source(profile) or target_s is None:
        return False
    attribution = result.get("stage25_control_attribution")
    if not isinstance(attribution, dict):
        return False
    stage25_rows = int(result.get("stage25_rows") or 0)
    source_counts = attribution.get("live_control_source_counts")
    layout_counts = attribution.get("layout_tag_counts")
    if control_mode is not None and control_mode not in {"", "speedl_cartesian_oracle"}:
        return False
    angular_max = finite_float(attribution.get("angular_cmd_norm_max_rad_s"))
    angular_limit = finite_float(attribution.get("angular_limit_rad_s"))
    angular_saturation = finite_float(attribution.get("angular_saturation_ratio"))
    normal_min = finite_float(attribution.get("normal_load_min_n"))
    normal_max = finite_float(attribution.get("normal_load_max_n"))
    force_norm_max = finite_float(attribution.get("force_norm_max_n"))
    shadow_only_rows = int(attribution.get("orientation_shadow_only_rows") or 0)
    trigger = attribution.get("control_oscillation_trigger")
    # Legacy ablation logs may omit live_control_source even when speedl control was active.
    # Accept that path only when the run declares speedl control mode.
    source_ok = False
    if isinstance(source_counts, dict):
        source_ok = source_counts == {STEP5D_V27_SPEEDL_LIVE_CONTROL_SOURCE: stage25_rows}
        if not source_ok and control_mode in {None, "", "speedl_cartesian_oracle"} and source_counts == {}:
            source_ok = True
    elif control_mode in {None, "", "speedl_cartesian_oracle"}:
        source_ok = True
    layout_ok = not isinstance(layout_counts, dict) or str(STAGE25_JOINT_LAYOUT_TAG) not in layout_counts

    return (
        stage25_rows > 0
        and float(result.get("stage25_duration_s") or 0.0) >= target_s
        and result.get("stage25_cadence_ok") is True
        and finite_float(result.get("stage25_consumption_ratio")) >= STAGE25_SUCCESS_MIN_CONSUMPTION_RATIO
        and result.get("terminal_tp_stop_reason") == 1
        and source_ok
        and layout_ok
        # Live angular is either fully zeroed (v27/v28 shadow-only isolation
        # runs) or the Step5b orientation follow inside its limit (v29 step);
        # in both cases it must stay off the angular cap essentially always.
        and math.isfinite(angular_max)
        and (
            angular_max <= 1e-6
            or (
                math.isfinite(angular_limit)
                and angular_max <= angular_limit * 1.001
                and math.isfinite(angular_saturation)
                and angular_saturation < 0.05
            )
        )
        and shadow_only_rows == stage25_rows
        and math.isfinite(normal_min)
        and math.isfinite(normal_max)
        and normal_min >= STAGE25_SUCCESS_NORMAL_LOAD_MIN_N
        and normal_max <= STAGE25_SUCCESS_NORMAL_LOAD_MAX_N
        and math.isfinite(force_norm_max)
        and force_norm_max < 60.0
        and trigger is None
    )


def stage25_speedj_dls_branch_success(profile: str, result: dict[str, Any], control_mode: str | None) -> bool:
    if control_mode != STAGE25_SPEEDJ_DLS_MODE:
        return False
    target_s = stage25_success_target_s(profile)
    if not uses_step5b_speedl_live_source(profile) or target_s is None:
        return False
    attribution = result.get("stage25_control_attribution")
    if not isinstance(attribution, dict):
        return False
    layout_counts = attribution.get("layout_tag_counts")
    normal_min = finite_float(attribution.get("normal_load_min_n"))
    normal_max = finite_float(attribution.get("normal_load_max_n"))
    force_norm_max = finite_float(attribution.get("force_norm_max_n"))
    return (
        int(result.get("stage25_rows") or 0) > 0
        and float(result.get("stage25_duration_s") or 0.0) >= target_s
        and result.get("stage25_cadence_ok") is True
        and finite_float(result.get("stage25_consumption_ratio")) >= STAGE25_SUCCESS_MIN_CONSUMPTION_RATIO
        and result.get("terminal_tp_stop_reason") == 1
        and isinstance(layout_counts, dict)
        and int(layout_counts.get(str(STAGE25_JOINT_LAYOUT_TAG), 0))
        >= int(result.get("stage25_rows") or 0) * STAGE25_SUCCESS_MIN_CONSUMPTION_RATIO
        and str(STAGE25_CARTESIAN_LAYOUT_TAG) not in layout_counts
        and attribution.get("terminal_contact_safety_reason") is None
        and math.isfinite(normal_min)
        and normal_min >= STAGE25_SUCCESS_NORMAL_LOAD_MIN_N
        and math.isfinite(normal_max)
        and normal_max <= STAGE25_SUCCESS_NORMAL_LOAD_MAX_N
        and math.isfinite(force_norm_max)
        and force_norm_max < 60.0
    )


def stage25_speedj_rnn_live_success(profile: str, result: dict[str, Any], control_mode: str | None) -> bool:
    supported_profiles = {
        STEP5D_ABLATION_V29_STAGE_ID,
        STEP5D_ABLATION_V33C20_STAGE_ID,
        STEP5D_ABLATION_V33_STAGE_ID,
    }
    if profile not in supported_profiles or control_mode != STAGE25_SPEEDJ_RNN_MODE:
        return False
    target_s = stage25_success_target_s(profile)
    if target_s is None:
        return False
    attribution = result.get("stage25_control_attribution")
    if not isinstance(attribution, dict):
        return False
    layout_counts = attribution.get("command_layout_tag_counts")
    normal_min = finite_float(attribution.get("normal_load_min_n"))
    normal_max = finite_float(attribution.get("normal_load_max_n"))
    force_norm_max = finite_float(attribution.get("force_norm_max_n"))
    accepted_duration_s = finite_float(attribution.get("rnn_accepted_continuous_duration_s"))
    if profile in {STEP5D_ABLATION_V33C20_STAGE_ID, STEP5D_ABLATION_V33_STAGE_ID}:
        feedback_age_p99 = finite_float(attribution.get("feedback_age_p99_s"))
        heartbeat_gap_max = finite_float(attribution.get("sent_echo_heartbeat_gap_max"))
        xy_error_p95 = finite_float(attribution.get("xy_tracking_error_p95_m"))
        xy_error_max = finite_float(attribution.get("xy_tracking_error_max_m"))
        qd_correlation = finite_float(attribution.get("command_actual_qd_correlation"))
        qd_lag_s = finite_float(attribution.get("command_actual_qd_lag_s"))
        accepted_ratio = finite_float(attribution.get("rnn_accepted_consumed_ratio"))
        v33_acceptance = (
            math.isfinite(feedback_age_p99) and feedback_age_p99 <= 0.010
            and math.isfinite(heartbeat_gap_max) and heartbeat_gap_max <= 5
            and math.isfinite(xy_error_p95) and xy_error_p95 <= 0.0005
            and math.isfinite(xy_error_max) and xy_error_max <= 0.001
            and math.isfinite(qd_correlation) and qd_correlation >= 0.9
            and math.isfinite(qd_lag_s) and qd_lag_s <= 0.020
            and math.isfinite(accepted_ratio) and accepted_ratio >= 0.98
            and attribution.get("final_safety_mode") == 1
            and int(attribution.get("gross_guard_rows") or 0) == 0
        )
    else:
        v33_acceptance = True
    legacy_force_window_acceptance = (
        math.isfinite(normal_min)
        and normal_min >= STAGE25_SUCCESS_NORMAL_LOAD_MIN_N
        and math.isfinite(normal_max)
        and normal_max <= STAGE25_SUCCESS_NORMAL_LOAD_MAX_N
        and math.isfinite(force_norm_max)
        and force_norm_max < 60.0
        if profile == STEP5D_ABLATION_V29_STAGE_ID
        else True
    )
    return (
        int(result.get("stage25_rows") or 0) > 0
        and accepted_duration_s >= target_s
        and result.get("stage25_cadence_ok") is True
        and result.get("terminal_tp_stop_reason") == 1
        and isinstance(layout_counts, dict)
        and int(layout_counts.get(str(STAGE25_JOINT_LAYOUT_TAG), 0))
        >= int(result.get("stage25_rows") or 0) * STAGE25_SUCCESS_MIN_CONSUMPTION_RATIO
        and legacy_force_window_acceptance
        and attribution.get("terminal_contact_safety_reason") is None
        and v33_acceptance
    )


def stage25_speedj_rnn_short_soft_hold_failure(result: dict[str, Any], control_mode: str | None) -> bool:
    if control_mode != STAGE25_SPEEDJ_RNN_MODE:
        return False
    attribution = result.get("stage25_control_attribution")
    if not isinstance(attribution, dict):
        return False
    return (
        result.get("terminal_tp_stop_reason") == 12
        and attribution.get("terminal_contact_safety_reason") == "soft_low_contact_hold"
    )


def stage25_speedj_rnn_cold_start_evidence(attribution: dict[str, Any]) -> bool:
    opposition_rows = int(attribution.get("approach_normal_press_unload_mismatch_rows") or 0)
    sign_mismatch_rows = int(attribution.get("approach_normal_sign_mismatch_rows") or 0)
    first_lambda = finite_float(attribution.get("rnn_lambda_norm_first"))
    last_lambda = finite_float(attribution.get("rnn_lambda_norm_last"))
    lambda_ramping = (
        math.isfinite(first_lambda)
        and math.isfinite(last_lambda)
        and last_lambda > max(first_lambda * 1.25, first_lambda + 1e-6)
    )
    return opposition_rows > 0 or sign_mismatch_rows > 0 or lambda_ramping


def base_analysis(csv_path: Path, run_dir: Path | None, profile: str, gate: Step5dPreloadGate) -> dict[str, Any]:
    return {
        "ok": True,
        "run_dir": None if run_dir is None else str(run_dir),
        "bridge_csv": str(csv_path),
        "profile": profile,
        "preload_gate": asdict(gate),
        "entered_stage25": False,
        "stage25_rows": 0,
        "stage25_duration_s": 0.0,
        "stage25_max_row_gap_s": 0.0,
        "stage25_benign_row_gap_count": 0,
        "stage25_row_gap_count": 0,
        "stage25_row_rate_hz": 0.0,
        "stage25_cadence_ok": None,
        "stage25_control_mode": None,
        "stage25_echo_consumed_rows": 0,
        "stage25_consumption_ratio": 0.0,
        "stage25_consumption_complete": None,
        "stage25_first_consumed_t_s": None,
        "stage25_consumption_ok": None,
        "stage25_control_attribution": {},
        "stage25_3_rows": 0,
        "stage25_3_duration_s": 0.0,
        "stage25_05_rows": 0,
        "stage25_05_cmd_valid_rows": 0,
        "max_tp_stage": None,
        "stage20_bridge_ready_rows": 0,
        "stage20_bridge_ready_statuses": [],
        "stage20_bridge_ready_reached": False,
        "longest_preload_gate_dwell_s": 0.0,
        "required_preload_hold_s": gate.hold_s,
        "max_stage25_3_raw_normal_load_n": None,
        "max_stage25_3_force_norm_n": None,
        "first_tp_stop_reason": None,
        "terminal_tp_stop_reason": None,
        "stage25_success_target_s": stage25_success_target_s(profile),
        "fix_validation_status": None,
        "reproduction_status": None,
        "acceptance_status": None,
        "classification": "ambiguous_requires_manual_audit",
        "next_action": "manual audit of stage echo, preload gate, and a small CSV slice",
    }


def analyze_csv(csv_path: Path, *, run_dir: Path | None = None) -> dict[str, Any]:
    metadata = read_json(run_dir / METADATA_FILENAME) if run_dir is not None else {}
    stage25_control_mode = metadata_text(metadata, "step5d_stage25_control_mode")
    profile, gate = preload_gate_for(run_dir)
    no_contact_p0_profile = is_no_contact_p0_profile(profile)
    no_contact_p0_verifier, no_contact_p0_v8_phase_s, no_contact_p0_verifier_tool = (
        no_contact_p0_verification(profile, run_dir=run_dir, csv_path=csv_path)
    )
    result = base_analysis(csv_path, run_dir, profile, gate)
    result["stage25_control_mode"] = stage25_control_mode
    if no_contact_p0_verifier is not None:
        result["no_contact_p0_verifier"] = no_contact_p0_verifier
        result["no_contact_p0_verifier_tool"] = no_contact_p0_verifier_tool
    if profile == STEP5D_NO_CONTACT_P0_V8_STAGE_ID:
        result["no_contact_p0_v8_phase_s"] = no_contact_p0_v8_phase_s
    if not csv_path.exists():
        result.update(
            {
                "ok": False,
                "missing_columns": sorted(REQUIRED_COLUMNS),
                "classification": "missing_required_columns",
                "next_action": "rerun on a run directory containing bridge_rtde_500hz.csv",
            }
        )
        return result

    stage25_3_segment_start_t: float | None = None
    stage25_3_segment_last_t: float | None = None
    stage25_segment_start_t: float | None = None
    stage25_segment_last_t: float | None = None
    stage25_previous_t: float | None = None
    ready_start_t: float | None = None
    stage25_rows: list[dict[str, str]] = []
    stage20_statuses: set[float] = set()

    def close_stage25_3_segment() -> None:
        nonlocal stage25_3_segment_start_t, stage25_3_segment_last_t
        if stage25_3_segment_start_t is not None and stage25_3_segment_last_t is not None:
            result["stage25_3_duration_s"] += max(
                0.0,
                stage25_3_segment_last_t - stage25_3_segment_start_t,
            )
        stage25_3_segment_start_t = None
        stage25_3_segment_last_t = None

    def close_stage25_segment() -> None:
        nonlocal stage25_segment_start_t, stage25_segment_last_t
        if stage25_segment_start_t is not None and stage25_segment_last_t is not None:
            result["stage25_duration_s"] += max(
                0.0,
                stage25_segment_last_t - stage25_segment_start_t,
            )
        stage25_segment_start_t = None
        stage25_segment_last_t = None

    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(REQUIRED_COLUMNS.difference(reader.fieldnames or []))
        if missing:
            result.update(
                {
                    "ok": False,
                    "missing_columns": missing,
                    "classification": "missing_required_columns",
                    "next_action": "rerun analyzer on a complete bridge_rtde_500hz.csv",
                }
            )
            return result

        for row in reader:
            t_s = finite_float(row.get("t_monotonic_s"))
            stage = finite_float(row.get("ur_output_double_register_35"))
            stop_reason = finite_float(row.get("ur_output_double_register_30"))
            stage20_status = finite_float(row.get("ur_output_double_register_36"))
            if math.isfinite(stage):
                current_max_stage = result["max_tp_stage"]
                result["max_tp_stage"] = stage if current_max_stage is None else max(current_max_stage, stage)
            if not no_contact_p0_profile:
                if result["first_tp_stop_reason"] is None and math.isfinite(stop_reason) and stop_reason != 0.0:
                    result["first_tp_stop_reason"] = maybe_int(stop_reason)
                if math.isfinite(stop_reason) and stop_reason != 0.0:
                    result["terminal_tp_stop_reason"] = maybe_int(stop_reason)
            if stage_is(stage, 20.0):
                result["stage20_bridge_ready_rows"] += 1
                if math.isfinite(stage20_status):
                    stage20_statuses.add(stage20_status)
                    if stage_is(stage20_status, 20.95):
                        result["stage20_bridge_ready_reached"] = True

            if stage_is(stage, 25.0):
                result["stage25_rows"] += 1
                result["entered_stage25"] = True
                stage25_rows.append(row)
                if math.isfinite(t_s):
                    if stage25_segment_start_t is None:
                        stage25_segment_start_t = t_s
                    if stage25_previous_t is not None:
                        gap_s = max(0.0, t_s - stage25_previous_t)
                        result["stage25_max_row_gap_s"] = max(
                            result["stage25_max_row_gap_s"],
                            gap_s,
                        )
                        if gap_s > STAGE25_MAX_ROW_GAP_S:
                            result["stage25_row_gap_count"] += 1
                            if is_benign_stage25_row_gap(gap_s, row):
                                result["stage25_benign_row_gap_count"] += 1
                    stage25_previous_t = t_s
                    stage25_segment_last_t = t_s
                consumed = finite_float(row.get("_step5d_stage25_echo_consumed"))
                if math.isfinite(consumed) and consumed >= 0.5:
                    result["stage25_echo_consumed_rows"] += 1
                    if result["stage25_first_consumed_t_s"] is None and math.isfinite(t_s):
                        result["stage25_first_consumed_t_s"] = t_s
            else:
                close_stage25_segment()
                stage25_previous_t = None

            if stage_is(stage, 25.05):
                result["stage25_05_rows"] += 1
                cmd_valid = finite_float(row.get("step4e_cmd_valid"))
                if math.isfinite(cmd_valid) and cmd_valid >= 0.5:
                    result["stage25_05_cmd_valid_rows"] += 1

            if stage_is(stage, 25.3):
                result["stage25_3_rows"] += 1
                if math.isfinite(t_s):
                    if stage25_3_segment_start_t is None:
                        stage25_3_segment_start_t = t_s
                    stage25_3_segment_last_t = t_s

                raw_load = finite_float(row.get("_step4e_normal_load_n"))
                force_norm = finite_float(row.get("force_norm_n"))
                if math.isfinite(raw_load):
                    current = result["max_stage25_3_raw_normal_load_n"]
                    result["max_stage25_3_raw_normal_load_n"] = raw_load if current is None else max(current, raw_load)
                if math.isfinite(force_norm):
                    current = result["max_stage25_3_force_norm_n"]
                    result["max_stage25_3_force_norm_n"] = force_norm if current is None else max(current, force_norm)

                if math.isfinite(t_s) and preload_ready(row, gate):
                    if ready_start_t is None:
                        ready_start_t = t_s
                    result["longest_preload_gate_dwell_s"] = max(
                        result["longest_preload_gate_dwell_s"],
                        t_s - ready_start_t,
                    )
                else:
                    ready_start_t = None
            else:
                close_stage25_3_segment()
                ready_start_t = None

    close_stage25_3_segment()
    close_stage25_segment()
    result["stage20_bridge_ready_statuses"] = [maybe_int(value) for value in sorted(stage20_statuses)]
    if result["stage25_duration_s"] > 0.0:
        result["stage25_row_rate_hz"] = result["stage25_rows"] / result["stage25_duration_s"]
    if result["entered_stage25"]:
        result["stage25_cadence_ok"] = (
            result["stage25_row_gap_count"] == 0
            or (
                result["stage25_row_gap_count"] == result["stage25_benign_row_gap_count"]
                and result["stage25_benign_row_gap_count"] <= STAGE25_BENIGN_GAP_OUTLIER_LIMIT
            )
        )
        result["stage25_consumption_ratio"] = (
            result["stage25_echo_consumed_rows"] / result["stage25_rows"]
            if result["stage25_rows"] > 0
            else 0.0
        )
        result["stage25_consumption_complete"] = result["stage25_echo_consumed_rows"] == result["stage25_rows"]
        result["stage25_consumption_ok"] = result["stage25_consumption_ratio"] >= STAGE25_MIN_CONSUMPTION_RATIO
        result["stage25_control_attribution"] = stage25_control_attribution(stage25_rows, metadata)
        if stage25_control_mode in {None, ""}:
            stage25_control_mode = stage25_control_mode_from_rows(stage25_rows)
            result["stage25_control_mode"] = stage25_control_mode

    if no_contact_p0_verifier is not None and no_contact_p0_verifier.get("ok") is False:
        result["classification"] = (
            "p0_v8_canary_verifier_failed"
            if profile == STEP5D_NO_CONTACT_P0_V8_STAGE_ID
            else "no_contact_p0_verifier_failed"
        )
        result["acceptance_status"] = (
            "failed_p0_v8_canary_verifier"
            if profile == STEP5D_NO_CONTACT_P0_V8_STAGE_ID
            else "failed_no_contact_p0_verifier"
        )
        result["next_action"] = (
            "keep strict RNN contact live gated; fix P0 command-path evidence until "
            f"{no_contact_p0_verifier_tool} passes on the completed artifact"
        )
    elif profile == STEP5D_NO_CONTACT_P0_V8_STAGE_ID and no_contact_p0_verifier is not None:
        phase_s = float(no_contact_p0_v8_phase_s or 0.0)
        canary_passed = no_contact_p0_verifier.get("canary_passed") is True
        p0_passed = no_contact_p0_verifier.get("p0_v8_passed") is True
        configured_phase_s = verify_step5d_no_contact_p0_v8.configured_direct_duration()
        if math.isclose(phase_s, configured_phase_s, abs_tol=1e-9) and p0_passed:
            result["classification"] = "p0_v8_passed"
            result["fix_validation_status"] = "passed_direct_duration_no_contact_p0_v8"
            result["reproduction_status"] = "contact_run_not_started"
            result["acceptance_status"] = "p0_v8_direct_duration_passed"
            result["next_action"] = (
                "bind this direct-duration P0 v8 artifact to the frozen fingerprint; v30 contact remains gated by "
                "timing/readback, Review v3 1+1 or valid degraded 1+0, and explicit live/contact authorization"
            )
        else:
            result["classification"] = "p0_v8_verifier_claim_mismatch"
            result["acceptance_status"] = "failed_p0_v8_claim_boundary"
            result["next_action"] = "keep v30 gated; reconcile the P0 v8 verifier phase and claim fields"
    elif result["entered_stage25"]:
        oscillation_reason = stage25_control_oscillation_reason(result["stage25_control_attribution"])
        attribution = result["stage25_control_attribution"]
        feedback_age_max = finite_float(attribution.get("feedback_age_max_s"))
        heartbeat_gap_max = finite_float(attribution.get("sent_echo_heartbeat_gap_max"))
        xy_error_max = finite_float(attribution.get("xy_tracking_error_max_m"))
        if (
            profile in {
                STEP5D_ABLATION_V31_STAGE_ID,
                STEP5D_ABLATION_V32_STAGE_ID,
                STEP5D_ABLATION_V33C20_STAGE_ID,
                STEP5D_ABLATION_V33_STAGE_ID,
            }
            and (
                (math.isfinite(feedback_age_max) and feedback_age_max > 0.05)
                or (math.isfinite(heartbeat_gap_max) and heartbeat_gap_max > 5)
            )
            and math.isfinite(xy_error_max)
            and xy_error_max > 0.001
        ):
            result["classification"] = "stale_feedback_xy_tracking_failure"
            result["acceptance_status"] = "failed_feedback_freshness_and_xy_tracking"
            result["next_action"] = (
                "drain all currently readable RTDE output packets and control only from the latest sample; "
                "then replay the Step5b-equivalent outer and rerun the freshness/XY canary"
            )
        elif stage25_speedl_fix_success(profile, result, control_mode=stage25_control_mode):
            if profile == STEP5D_ABLATION_V28_STAGE_ID:
                result["classification"] = "stage25_full_run_success"
                result["fix_validation_status"] = "passed_60s_full_run"
                result["reproduction_status"] = "passed_60s_step5b_equivalent_run"
                result["acceptance_status"] = "speedl_full_run_passed"
                result["next_action"] = (
                    "archive v28 as the 60s Step5b-speedl-live orientation-follow / Step5d-shadow "
                    "full-run evidence; strict RNN live remains blocked pending no-contact P0 "
                    "warm_start verification and explicit live authorization"
                )
            else:
                result["classification"] = "stage25_fix_validation_success"
                result["fix_validation_status"] = "passed_10s_stage25_window"
                result["reproduction_status"] = "pending_60s_step5b_equivalent_run"
                result["acceptance_status"] = "speedl_fix_validation_passed"
                result["next_action"] = (
                    "retain v27 as successful 10s fix-validation evidence and generate a v28 60s full-run package "
                    "before any full reproduction claim"
                )
        elif stage25_speedj_dls_branch_success(profile, result, stage25_control_mode):
            result["classification"] = "stage25_speedj_dls_branch_success"
            result["acceptance_status"] = "excluded_from_speedl_acceptance"
            result["next_action"] = (
                "archive this as speedj_dls_oracle branch evidence for layout-524/speedj stability; "
                "keep it out of speedl full-run acceptance and compare RNN live qdot against DLS"
            )
        elif stage25_speedj_rnn_live_success(profile, result, stage25_control_mode):
            if profile == STEP5D_ABLATION_V33C20_STAGE_ID:
                result["classification"] = "v33c20_canary_passed"
                result["fix_validation_status"] = "passed_20s_fresh_feedback_canary"
                result["reproduction_status"] = "pending_v33_60s_full_run"
                result["acceptance_status"] = "v33c20_acceptance_contract_passed"
                result["next_action"] = (
                    "freeze the accepted v33c20 live artifact, promote the separately delivered v33 60s package, "
                    "and require a new explicit full-run authorization"
                )
            elif profile == STEP5D_ABLATION_V33_STAGE_ID:
                result["classification"] = "v33_full_run_passed"
                result["fix_validation_status"] = "passed_60s_fresh_feedback_full_run"
                result["reproduction_status"] = "passed_60s_strict_rnn_live_candidate_run"
                result["acceptance_status"] = "v33_full_acceptance_contract_passed"
                result["next_action"] = "archive the v33 full-run evidence and keep reproduction claims owner-gated"
            else:
                result["classification"] = "stage25_speedj_rnn_live_success"
                result["fix_validation_status"] = "passed_60s_strict_rnn_live"
                result["reproduction_status"] = "passed_60s_strict_rnn_live_candidate_run"
                result["acceptance_status"] = "speedj_rnn_live_full_run_passed"
                result["next_action"] = (
                    "archive v29 as strict RNN speedj live evidence; keep controller/package publication "
                    "and live claims gated by owner audit and explicit user authorization"
                )
        elif stage25_speedj_rnn_short_soft_hold_failure(result, stage25_control_mode):
            attribution = result["stage25_control_attribution"]
            cold_start_evidence = isinstance(attribution, dict) and stage25_speedj_rnn_cold_start_evidence(attribution)
            result["classification"] = (
                "stage25_speedj_rnn_short_soft_hold_failure"
                if cold_start_evidence
                else "stage25_speedj_rnn_soft_hold_failure"
            )
            result["acceptance_status"] = "failed_speedj_rnn_branch"
            if cold_start_evidence:
                result["next_action"] = (
                    "strict RNN cold-start transient: solver state resets to zero at the Stage25 lifecycle "
                    "boundary while already preloaded, so early Cartesian velocity follows J@J.T@xdot_c and "
                    "the angular-dominant entry command unloads the contact below 5N before lambda converges "
                    "(see approach_normal_* opposition and rnn_lambda_norm_first/last still ramping); "
                    "fix offline via solver warm_start at the lifecycle boundary, then re-verify with a "
                    "no-contact P0 pass before any live retry"
                )
            else:
                result["next_action"] = (
                    "speedj_rnn_live stopped in soft_low_contact_hold without enough cold-start-specific "
                    "opposition or lambda-ramp evidence; inspect RNN solver evidence before attributing "
                    "root cause, and keep strict RNN live blocked pending no-contact P0 verification"
                )
        elif (
            profile in {STEP5D_ABLATION_V27_STAGE_ID, STEP5D_ABLATION_V28_STAGE_ID}
            and stage25_control_mode not in {None, "", "speedl_cartesian_oracle"}
        ):
            result["classification"] = "stage25_control_mode_mismatch"
            result["acceptance_status"] = "excluded_from_speedl_acceptance"
            result["next_action"] = (
                f"stage25 control mode is {stage25_control_mode!r}, expected speedl_cartesian_oracle for v27/v28 step5d fix validation; "
                "continue with the next control-mode branch and keep this run out of speedl full-run acceptance"
            )
        elif uses_step5b_speedl_live_source(profile) and (
            not result["stage25_cadence_ok"] or not result["stage25_consumption_ok"]
        ):
            result["classification"] = "stage25_cadence_or_consumption_failure"
            result["acceptance_status"] = "failed_stage25_cadence_or_consumption"
            result["next_action"] = "audit Stage25.0 bridge loop timing, RTDE send blocking, and TP command consumption echo"
        elif oscillation_reason is not None:
            result["classification"] = f"stage25_control_force_oscillation/{oscillation_reason}"
            if (
                profile == STEP5D_ABLATION_V27_STAGE_ID
                and old_v27_paper_outer_linear_live_gain_mismatch(result["stage25_control_attribution"])
            ):
                result["evidence_classification"] = (
                    "old_v27_paper_outer_linear_live_gain_mismatch_force_norm_hard_stop"
                )
                result["next_action"] = (
                    "keep bridge live-gated; copy Step5b speedl live vx/vy/vz, keep Step5d paper/RNN outputs "
                    "shadow-only, and audit the offline fix before another live retry"
                )
            else:
                result["next_action"] = (
                    "keep bridge live-gated; audit Stage25.0 live command source, force response, "
                    "and orientation attribution before another live retry"
                )
        else:
            result["classification"] = "entered_stage25"
            result["next_action"] = "audit Stage25.0 behavior and acceptance evidence"
    elif result["stage25_3_rows"] == 0:
        if (
            result["stage25_05_rows"] > 0
            and result["stage25_05_cmd_valid_rows"] == 0
            and result["terminal_tp_stop_reason"] == 12
        ):
            result["classification"] = "stage25_05_cmd_valid_timeout"
            result["acceptance_status"] = "failed_stage25_05_transport_packet"
            result["next_action"] = (
                "preserve the run as Stage25.05 transport evidence; verify that the stage-aware publisher "
                "passes latch-ready cmd_valid without requiring the Stage25 joint marker"
            )
        elif (
            result["stage20_bridge_ready_rows"] > 0
            and not result["stage20_bridge_ready_reached"]
            and (result["max_tp_stage"] is None or float(result["max_tp_stage"]) <= 20.1)
        ):
            result["classification"] = "stage20_bridge_ready_handshake_failed"
            result["next_action"] = "fix P0 bridge/TP lifecycle handshake before retrying capture"
        else:
            result["classification"] = "no_tp_play_or_no_stage_echo"
            result["next_action"] = "check TP Play, loaded-program state, and stage echo before CSV diagnosis"
    elif result["longest_preload_gate_dwell_s"] < result["required_preload_hold_s"]:
        result["classification"] = "no_stage25_preload_dwell_short"
        result["next_action"] = "diagnose Stage25.3 preload dwell/gate; do not treat this run as Stage25.0 acceptance"

    return result


def analyze_run_dir(run_dir: Path) -> dict[str, Any]:
    return analyze_csv(run_dir / BRIDGE_CSV_FILENAME, run_dir=run_dir)


def default_output_path(csv_path: Path, run_dir: Path | None) -> Path:
    if run_dir is not None:
        return run_dir / ANALYSIS_FILENAME
    return csv_path.with_name(ANALYSIS_FILENAME)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run-dir", type=Path, help="Bridge run directory containing bridge_rtde_500hz.csv")
    source.add_argument("--csv", type=Path, help="Path to bridge_rtde_500hz.csv")
    parser.add_argument("--output", type=Path, help=f"Output JSON path, default: {ANALYSIS_FILENAME} beside CSV")
    parser.add_argument("--json", action="store_true", help="Print the analysis JSON to stdout")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    csv_path = args.csv if args.csv is not None else args.run_dir / BRIDGE_CSV_FILENAME
    run_dir = args.run_dir if args.run_dir is not None else csv_path.parent
    analysis = analyze_csv(csv_path, run_dir=run_dir)
    output = args.output or default_output_path(csv_path, run_dir)
    output.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(analysis, indent=2, sort_keys=True))
    return 0 if analysis.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
