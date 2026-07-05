#!/usr/bin/env python3
"""Fast Step5d bridge-run diagnosis from one bridge RTDE CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from step5d_runtime_interface import (
    STEP5D_ABLATION_V25_STAGE_ID,
    STEP5D_ABLATION_V26_STAGE_ID,
    STEP5D_ABLATION_V27_STAGE_ID,
    STEP5D_ABLATION_V28_STAGE_ID,
    STEP5D_LIVEPREP_V24_STAGE_ID,
    STEP5D_V27_SPEEDL_LIVE_CONTROL_SOURCE,
    Step5dPreloadGate,
    default_preload_gate,
    stage25_success_target_s,
    uses_step5b_speedl_live_source,
)


ANALYSIS_FILENAME = "step5d_bridge_analysis.json"
BRIDGE_CSV_FILENAME = "bridge_rtde_500hz.csv"
METADATA_FILENAME = "metadata.json"
SUMMARY_FILENAME = "summary.json"
STAGE_TOL = 0.005
STAGE25_MAX_ROW_GAP_S = 0.020
STAGE25_MIN_CONSUMPTION_RATIO = 0.95
STAGE25_SUCCESS_MIN_CONSUMPTION_RATIO = 0.98
STAGE25_ORIENTATION_ENTRY_HOLD_S = 0.150
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
        for profile in (
            STEP5D_ABLATION_V28_STAGE_ID,
            STEP5D_ABLATION_V27_STAGE_ID,
            STEP5D_ABLATION_V26_STAGE_ID,
            STEP5D_ABLATION_V25_STAGE_ID,
            STEP5D_LIVEPREP_V24_STAGE_ID,
        ):
            if profile in name:
                return profile
    return STEP5D_ABLATION_V25_STAGE_ID


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


def angular_norms(rows: list[dict[str, str]]) -> list[float]:
    norms: list[float] = []
    for row in rows:
        wx = finite_float(row.get("step4e_cmd_wx_rad_s"))
        wy = finite_float(row.get("step4e_cmd_wy_rad_s"))
        wz = finite_float(row.get("step4e_cmd_wz_rad_s"))
        if math.isfinite(wx) and math.isfinite(wy) and math.isfinite(wz):
            norms.append(math.sqrt(wx * wx + wy * wy + wz * wz))
    return norms


def linear_norms(rows: list[dict[str, str]]) -> list[float]:
    norms: list[float] = []
    for row in rows:
        vx = finite_float(row.get("step4e_cmd_vx_m_s"))
        vy = finite_float(row.get("step4e_cmd_vy_m_s"))
        vz = finite_float(row.get("step4e_cmd_vz_m_s"))
        if math.isfinite(vx) and math.isfinite(vy) and math.isfinite(vz):
            norms.append(math.sqrt(vx * vx + vy * vy + vz * vz))
    return norms


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


def stage25_segment_metrics(rows: list[dict[str, str]], angular_limit: float) -> dict[str, Any]:
    angular = angular_norms(rows)
    linear = linear_norms(rows)
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
        "linear_vz_cmd_mean_m_s": mean_or_none(finite_values(rows, "step4e_cmd_vz_m_s")),
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


def last_contact_safety_reason(rows: list[dict[str, str]]) -> str | None:
    for row in reversed(rows):
        reason = row.get("_step5d_contact_safety_reason") or ""
        if reason and reason != "ok":
            return reason
    return None


def stage25_control_attribution(rows: list[dict[str, str]], metadata: dict[str, Any]) -> dict[str, Any]:
    normal_loads = finite_values(rows, "_step4e_normal_load_n")
    force_norms = finite_values(rows, "force_norm_n")
    rates = load_rates(rows)
    angular = angular_norms(rows)
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
    return {
        "stage25_rows": len(rows),
        "entry_orientation_error_rad": first_finite(rows, "step4e_orientation_error_rad"),
        "entry_outer_orientation_error_rad": first_finite(rows, "_step5d_outer_orientation_error_rad"),
        "angular_limit_rad_s": angular_limit if math.isfinite(angular_limit) else None,
        "angular_cmd_norm_max_rad_s": max_or_none(angular),
        "angular_cmd_norm_mean_rad_s": mean_or_none(angular),
        "shadow_raw_angular_cmd_norm_max_rad_s": max_or_none(shadow_angular),
        "shadow_raw_angular_cmd_norm_mean_rad_s": mean_or_none(shadow_angular),
        "orientation_shadow_only_rows": sum(
            1 for value in finite_values(rows, "_step5d_speedl_orientation_shadow_only") if value >= 0.5
        ),
        "angular_saturation_rows": saturated,
        "angular_saturation_ratio": angular_saturation_ratio,
        "linear_vz_cmd_abs_max_m_s": max_or_none([abs(value) for value in finite_values(rows, "step4e_cmd_vz_m_s")]),
        "linear_vz_cmd_mean_m_s": mean_or_none(finite_values(rows, "step4e_cmd_vz_m_s")),
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


def stage25_speedl_fix_success(profile: str, result: dict[str, Any]) -> bool:
    target_s = stage25_success_target_s(profile)
    if not uses_step5b_speedl_live_source(profile) or target_s is None:
        return False
    attribution = result.get("stage25_control_attribution")
    if not isinstance(attribution, dict):
        return False
    stage25_rows = int(result.get("stage25_rows") or 0)
    source_counts = attribution.get("live_control_source_counts")
    angular_max = finite_float(attribution.get("angular_cmd_norm_max_rad_s"))
    normal_min = finite_float(attribution.get("normal_load_min_n"))
    normal_max = finite_float(attribution.get("normal_load_max_n"))
    force_norm_max = finite_float(attribution.get("force_norm_max_n"))
    shadow_only_rows = int(attribution.get("orientation_shadow_only_rows") or 0)
    trigger = attribution.get("control_oscillation_trigger")
    return (
        stage25_rows > 0
        and float(result.get("stage25_duration_s") or 0.0) >= target_s
        and finite_float(result.get("stage25_consumption_ratio")) >= STAGE25_SUCCESS_MIN_CONSUMPTION_RATIO
        and result.get("terminal_tp_stop_reason") == 1
        and isinstance(source_counts, dict)
        and source_counts == {STEP5D_V27_SPEEDL_LIVE_CONTROL_SOURCE: stage25_rows}
        and math.isfinite(angular_max)
        and angular_max <= 1e-6
        and shadow_only_rows == stage25_rows
        and math.isfinite(normal_min)
        and math.isfinite(normal_max)
        and normal_min >= 9.0
        and normal_max <= 15.0
        and math.isfinite(force_norm_max)
        and force_norm_max < 60.0
        and trigger is None
    )


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
        "stage25_row_rate_hz": 0.0,
        "stage25_cadence_ok": None,
        "stage25_echo_consumed_rows": 0,
        "stage25_consumption_ratio": 0.0,
        "stage25_consumption_complete": None,
        "stage25_first_consumed_t_s": None,
        "stage25_consumption_ok": None,
        "stage25_control_attribution": {},
        "stage25_3_rows": 0,
        "stage25_3_duration_s": 0.0,
        "longest_preload_gate_dwell_s": 0.0,
        "required_preload_hold_s": gate.hold_s,
        "max_stage25_3_raw_normal_load_n": None,
        "max_stage25_3_force_norm_n": None,
        "first_tp_stop_reason": None,
        "terminal_tp_stop_reason": None,
        "stage25_success_target_s": stage25_success_target_s(profile),
        "fix_validation_status": None,
        "reproduction_status": None,
        "classification": "ambiguous_requires_manual_audit",
        "next_action": "manual audit of stage echo, preload gate, and a small CSV slice",
    }


def analyze_csv(csv_path: Path, *, run_dir: Path | None = None) -> dict[str, Any]:
    metadata = read_json(run_dir / METADATA_FILENAME) if run_dir is not None else {}
    profile, gate = preload_gate_for(run_dir)
    result = base_analysis(csv_path, run_dir, profile, gate)
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
            if result["first_tp_stop_reason"] is None and math.isfinite(stop_reason) and stop_reason != 0.0:
                result["first_tp_stop_reason"] = maybe_int(stop_reason)
            if math.isfinite(stop_reason) and stop_reason != 0.0:
                result["terminal_tp_stop_reason"] = maybe_int(stop_reason)

            if stage_is(stage, 25.0):
                result["stage25_rows"] += 1
                result["entered_stage25"] = True
                stage25_rows.append(row)
                if math.isfinite(t_s):
                    if stage25_segment_start_t is None:
                        stage25_segment_start_t = t_s
                    if stage25_previous_t is not None:
                        result["stage25_max_row_gap_s"] = max(
                            result["stage25_max_row_gap_s"],
                            max(0.0, t_s - stage25_previous_t),
                        )
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
    if result["stage25_duration_s"] > 0.0:
        result["stage25_row_rate_hz"] = result["stage25_rows"] / result["stage25_duration_s"]
    if result["entered_stage25"]:
        result["stage25_cadence_ok"] = result["stage25_max_row_gap_s"] <= STAGE25_MAX_ROW_GAP_S
        result["stage25_consumption_ratio"] = (
            result["stage25_echo_consumed_rows"] / result["stage25_rows"]
            if result["stage25_rows"] > 0
            else 0.0
        )
        result["stage25_consumption_complete"] = result["stage25_echo_consumed_rows"] == result["stage25_rows"]
        result["stage25_consumption_ok"] = result["stage25_consumption_ratio"] >= STAGE25_MIN_CONSUMPTION_RATIO
        result["stage25_control_attribution"] = stage25_control_attribution(stage25_rows, metadata)

    if result["entered_stage25"]:
        oscillation_reason = stage25_control_oscillation_reason(result["stage25_control_attribution"])
        if stage25_speedl_fix_success(profile, result):
            if profile == STEP5D_ABLATION_V28_STAGE_ID:
                result["classification"] = "stage25_full_run_success"
                result["fix_validation_status"] = "passed_60s_full_run"
                result["reproduction_status"] = "passed_60s_step5b_equivalent_run"
                result["next_action"] = (
                    "archive v28 as the 60s Step5b-speedl-live / Step5d-shadow full-run evidence; "
                    "do not re-enable orientation servo until the expected-normal error case is closed"
                )
            else:
                result["classification"] = "stage25_fix_validation_success"
                result["fix_validation_status"] = "passed_10s_stage25_window"
                result["reproduction_status"] = "pending_60s_step5b_equivalent_run"
                result["next_action"] = (
                    "retain v27 as successful 10s fix-validation evidence and generate a v28 60s full-run package "
                    "before any full reproduction claim"
                )
        elif uses_step5b_speedl_live_source(profile) and (
            not result["stage25_cadence_ok"] or not result["stage25_consumption_ok"]
        ):
            result["classification"] = "stage25_cadence_or_consumption_failure"
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
