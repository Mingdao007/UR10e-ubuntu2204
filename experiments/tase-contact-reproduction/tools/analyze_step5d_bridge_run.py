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
    STEP5D_LIVEPREP_V24_STAGE_ID,
    Step5dPreloadGate,
    default_preload_gate,
)


ANALYSIS_FILENAME = "step5d_bridge_analysis.json"
BRIDGE_CSV_FILENAME = "bridge_rtde_500hz.csv"
METADATA_FILENAME = "metadata.json"
SUMMARY_FILENAME = "summary.json"
STAGE_TOL = 0.005
STAGE25_MAX_ROW_GAP_S = 0.020
STAGE25_MIN_CONSUMPTION_RATIO = 0.95
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
    angular_limit = metadata_float(metadata, "bridge_angular_limit_rad_s", "step4e_angular_limit_rad_s")
    if not math.isfinite(angular_limit) and angular:
        angular_limit = max(angular)
    saturation_threshold = angular_limit * 0.99 if math.isfinite(angular_limit) and angular_limit > 0.0 else math.inf
    saturated = sum(1 for value in angular if value >= saturation_threshold)
    sources = Counter(row.get("_step4e_normal_filter_source") or "" for row in rows)
    reasons = Counter(row.get("_step5d_contact_safety_reason") or "" for row in rows)
    reason_counts = dict(sorted((key, value) for key, value in reasons.items() if key))
    load_min = min_or_none(normal_loads)
    load_range = range_or_none(normal_loads)
    angular_saturation_ratio = 0.0 if not angular else saturated / len(angular)
    has_low_load_repress = int(reason_counts.get("v25_speedl_low_load_repress_window", 0)) > 0
    has_low_load_timeout = int(reason_counts.get("v25_speedl_hard_low_load_timeout", 0)) > 0
    has_force_hard_stop = int(reason_counts.get("force_norm_hard_stop", 0)) > 0
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
    return {
        "stage25_rows": len(rows),
        "entry_orientation_error_rad": first_finite(rows, "step4e_orientation_error_rad"),
        "entry_outer_orientation_error_rad": first_finite(rows, "_step5d_outer_orientation_error_rad"),
        "angular_limit_rad_s": angular_limit if math.isfinite(angular_limit) else None,
        "angular_cmd_norm_max_rad_s": max_or_none(angular),
        "angular_cmd_norm_mean_rad_s": mean_or_none(angular),
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
        "contact_safety_reason_counts": reason_counts,
        "terminal_contact_safety_reason": last_contact_safety_reason(rows),
        "control_oscillation_trigger": control_oscillation_trigger,
        "normal_filter_lag_angle_abs_max_rad": max_or_none(
            [abs(value) for value in finite_values(rows, "_step4e_live_normal_candidate_angle_rad")]
        ),
        "normal_filter_latch_lag_angle_abs_max_rad": max_or_none(
            [abs(value) for value in finite_values(rows, "_step4e_live_normal_angle_from_latch_rad")]
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
        if profile == STEP5D_ABLATION_V27_STAGE_ID and (
            not result["stage25_cadence_ok"] or not result["stage25_consumption_ok"]
        ):
            result["classification"] = "stage25_cadence_or_consumption_failure"
            result["next_action"] = "audit Stage25.0 bridge loop timing, RTDE send blocking, and TP command consumption echo"
        elif oscillation_reason is not None:
            result["classification"] = f"stage25_control_force_oscillation/{oscillation_reason}"
            result["next_action"] = "keep bridge live-gated; fix Stage25.0 entry orientation command before another live retry"
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
