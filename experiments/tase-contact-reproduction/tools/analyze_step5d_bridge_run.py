#!/usr/bin/env python3
"""Fast Step5d bridge-run diagnosis from one bridge RTDE CSV."""

from __future__ import annotations

import argparse
import csv
import json
import math
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
        "stage25_first_consumed_t_s": None,
        "stage25_consumption_ok": None,
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
        result["stage25_consumption_ok"] = result["stage25_echo_consumed_rows"] == result["stage25_rows"]

    if result["entered_stage25"]:
        if (
            profile == STEP5D_ABLATION_V27_STAGE_ID
            and (not result["stage25_cadence_ok"] or not result["stage25_consumption_ok"])
        ):
            result["classification"] = "stage25_cadence_or_consumption_failure"
            result["next_action"] = "audit Stage25.0 bridge loop timing, RTDE send blocking, and TP command consumption echo"
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
