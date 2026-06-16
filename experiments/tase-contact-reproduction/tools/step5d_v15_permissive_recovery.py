#!/usr/bin/env python3
"""Offline Step5d v15 permissive-recovery analyzer and replay gate."""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import kunwei_rtde_bridge as bridge


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = EXPERIMENT_ROOT / "runs"
SUCCESS_CSVS = [
    RUN_ROOT / "bridge_step5b_contact_cycloid_baseline_v1_20260612_082352" / "bridge_rtde_500hz.csv",
    RUN_ROOT / "bridge_step5b_contact_cycloid_baseline_v1_20260614_222309" / "bridge_rtde_500hz.csv",
    RUN_ROOT / "bridge_step6b_contact_eight_baseline_v1_20260612_223047" / "bridge_rtde_500hz.csv",
    RUN_ROOT / "bridge_step6b_contact_eight_baseline_v2_20260612_225841" / "bridge_rtde_500hz.csv",
    RUN_ROOT / "bridge_step6b_contact_eight_baseline_v2_20260614_223106" / "bridge_rtde_500hz.csv",
]
V11_CSV = RUN_ROOT / "bridge_step5d_strict_rnn_liveprep_v11_20260615_204601" / "bridge_rtde_500hz.csv"
V14_CSV = RUN_ROOT / "bridge_step4e_line_outerloop_step5d_strict_rnn_liveprep_v14_20260615_231805" / "bridge_rtde_500hz.csv"
TCP_CAGE_COLUMNS = (
    "_step5d_tcp_cage_distance_m",
    "_step5d_tcp_cage_braking_margin_m",
    "_step5d_tcp_cage_signed_distance_m",
)


def finite_float(row: dict[str, str], key: str, default: float = math.nan) -> float:
    value = row.get(key, "")
    if value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def read_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    first_t = next((finite_float(row, "t_monotonic_s") for row in rows if math.isfinite(finite_float(row, "t_monotonic_s"))), 0.0)
    for row in rows:
        t_monotonic_s = finite_float(row, "t_monotonic_s")
        row["_t_rel_s"] = f"{max(0.0, t_monotonic_s - first_t):.9f}" if math.isfinite(t_monotonic_s) else ""
    return rows


def linear_speed(row: dict[str, str]) -> float:
    logged = finite_float(row, "_step5d_actual_tcp_speed_m_s")
    if math.isfinite(logged):
        return logged
    values = [finite_float(row, f"ur_actual_TCP_speed_{idx}", 0.0) for idx in range(3)]
    return math.sqrt(sum(value * value for value in values))


def predicted_speed(row: dict[str, str]) -> float:
    return finite_float(row, "_step5d_predicted_tcp_speed_m_s")


def normal_load(row: dict[str, str]) -> float:
    return finite_float(row, "_step4e_normal_load_n")


def force_norm(row: dict[str, str]) -> float:
    return finite_float(row, "force_norm_n")


def qdot_max_abs(row: dict[str, str]) -> float:
    logged = finite_float(row, "_step5d_qdot_max_abs_rad_s")
    if math.isfinite(logged):
        return logged
    values = [abs(finite_float(row, f"_step5c_cmd_qd{idx}_rad_s")) for idx in range(6)]
    finite_values = [value for value in values if math.isfinite(value)]
    return max(finite_values, default=math.nan)


def control_normal(row: dict[str, str]) -> tuple[float, float, float] | None:
    values = [finite_float(row, f"_step4e_control_normal_b_{axis}") for axis in ("x", "y", "z")]
    if not all(math.isfinite(value) for value in values):
        return None
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 1e-12:
        return None
    return tuple(value / norm for value in values)


def angle_between(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    dot = max(-1.0, min(1.0, sum(a[idx] * b[idx] for idx in range(3))))
    return math.acos(dot)


def stage25_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    selected = [
        row for row in rows
        if math.isfinite(finite_float(row, "ur_output_double_register_35"))
        and abs(finite_float(row, "ur_output_double_register_35") - 25.0) < 0.05
    ]
    return selected if selected else [row for row in rows if finite_float(row, "step4e_cmd_valid", 0.0) > 0.5]


def main_tracking_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    selected = stage25_rows(rows)
    if len(selected) < 3:
        return selected
    start = finite_float(selected[0], "_t_rel_s")
    end = finite_float(selected[-1], "_t_rel_s")
    if not math.isfinite(start) or not math.isfinite(end) or end - start <= 1.2:
        return selected
    trimmed = [
        row for row in selected
        if finite_float(row, "_t_rel_s") >= start + 0.5 and finite_float(row, "_t_rel_s") <= end - 0.5
    ]
    return trimmed or selected


def percentile(values: list[float], q: float) -> float:
    finite_values = sorted(value for value in values if math.isfinite(value))
    if not finite_values:
        return math.nan
    if len(finite_values) == 1:
        return finite_values[0]
    position = (len(finite_values) - 1) * q
    lo = int(math.floor(position))
    hi = int(math.ceil(position))
    if lo == hi:
        return finite_values[lo]
    frac = position - lo
    return finite_values[lo] * (1.0 - frac) + finite_values[hi] * frac


def deltas(values: list[float], window: int) -> list[float]:
    return [
        abs(values[index] - values[index - window])
        for index in range(window, len(values))
        if math.isfinite(values[index]) and math.isfinite(values[index - window])
    ]


def summarize_csv(csv_path: Path, *, kind: str) -> dict[str, Any]:
    rows = main_tracking_rows(read_rows(csv_path))
    speeds = [linear_speed(row) for row in rows]
    loads = [normal_load(row) for row in rows]
    predicted = [predicted_speed(row) for row in rows]
    qdots = [qdot_max_abs(row) for row in rows]
    normals = [control_normal(row) for row in rows]
    normal_jumps = [
        angle_between(normals[index], normals[index - 1])
        for index in range(1, len(normals))
        if normals[index] is not None and normals[index - 1] is not None
    ]
    return {
        "kind": kind,
        "csv": str(csv_path),
        "rows": len(rows),
        "duration_s": max(0.0, finite_float(rows[-1], "_t_rel_s") - finite_float(rows[0], "_t_rel_s")) if len(rows) > 1 else 0.0,
        "actual_speed_m_s": {
            "p99": percentile(speeds, 0.99),
            "p999": percentile(speeds, 0.999),
            "max": max((value for value in speeds if math.isfinite(value)), default=math.nan),
        },
        "predicted_speed_m_s": {
            "p99": percentile(predicted, 0.99),
            "p999": percentile(predicted, 0.999),
            "max": max((value for value in predicted if math.isfinite(value)), default=math.nan),
        },
        "load_jump_n": {
            f"{window}_tick": {
                "p99": percentile(deltas(loads, window), 0.99),
                "p999": percentile(deltas(loads, window), 0.999),
                "max": max(deltas(loads, window), default=math.nan),
            }
            for window in (1, 2, 5, 10, 25)
        },
        "normal_angle_jump_rad": {
            "p99": percentile(normal_jumps, 0.99),
            "max": max(normal_jumps, default=math.nan),
        },
        "qdot_max_abs_rad_s": {
            "p99": percentile(qdots, 0.99),
            "max": max((value for value in qdots if math.isfinite(value)), default=math.nan),
        },
    }


def summarize_tcp_cage_availability(paths: list[Path]) -> dict[str, Any]:
    availability = {}
    any_available = False
    for path in paths:
        rows = read_rows(path)
        fields = set(rows[0].keys()) if rows else set()
        present = [column for column in TCP_CAGE_COLUMNS if column in fields]
        finite_counts = {
            column: sum(1 for row in rows if math.isfinite(finite_float(row, column)))
            for column in present
        }
        any_available = any_available or any(count > 0 for count in finite_counts.values())
        availability[str(path)] = {
            "present_columns": present,
            "finite_counts": finite_counts,
        }
    return {
        "status": "available" if any_available else "unavailable_in_source_csvs",
        "expected_columns": list(TCP_CAGE_COLUMNS),
        "availability_by_csv": availability,
        "note": "Historical CSVs do not contain TCP cage distance or braking-margin fields; v15 keeps cage behavior covered by synthetic hard-boundary replay until a future bridge logs these columns.",
    }


def replay_v15_guard(csv_path: Path) -> dict[str, Any]:
    all_rows = read_rows(csv_path)
    stage_rows = stage25_rows(all_rows)
    predicted_active = [row for row in stage_rows if math.isfinite(predicted_speed(row))]
    rows = predicted_active if predicted_active else main_tracking_rows(all_rows)
    hold_s = 0.0
    high_window_s = 0.0
    actual_speed_violation_s = 0.0
    actual_speed_violation_count = 0
    previous_t: float | None = None
    first_hold: dict[str, Any] | None = None
    first_stop: dict[str, Any] | None = None
    action_counts: dict[str, int] = {}
    for index, row in enumerate(rows):
        t_rel_s = finite_float(row, "_t_rel_s")
        dt_s = 0.002 if previous_t is None else max(0.0, t_rel_s - previous_t)
        previous_t = t_rel_s
        pred = predicted_speed(row)
        result = bridge.step5d_v15_permissive_recovery_guard(
            normal_load_n=normal_load(row),
            force_norm_n=force_norm(row),
            actual_tcp_speed_m_s=linear_speed(row),
            predicted_tcp_speed_m_s=pred if math.isfinite(pred) else None,
            prior_hold_s=hold_s,
            prior_high_window_s=high_window_s,
            prior_actual_speed_violation_s=actual_speed_violation_s,
            prior_actual_speed_violation_count=actual_speed_violation_count,
            dt_s=dt_s,
        )
        action = str(result["action"])
        action_counts[action] = action_counts.get(action, 0) + 1
        hold_s = float(result["hold_s"])
        high_window_s = float(result["high_window_s"])
        actual_speed_violation_s = float(result["actual_speed_violation_s"])
        actual_speed_violation_count = int(result["actual_speed_violation_count"])
        event = {
            "index": index,
            "t_rel_s": t_rel_s,
            "reason": result["reason"],
            "normal_load_n": normal_load(row),
            "actual_tcp_speed_m_s": linear_speed(row),
            "predicted_tcp_speed_m_s": pred,
            "hold_s": hold_s,
        }
        if first_hold is None and action == "hold_zero_qdot":
            first_hold = event
        if action == "stop_zero_qdot":
            first_stop = event
            break
    return {
        "csv": str(csv_path),
        "rows": len(rows),
        "action_counts": action_counts,
        "first_hold": first_hold,
        "first_stop": first_stop,
    }


def synthetic_cage_replay() -> dict[str, Any]:
    inside = bridge.step5d_v15_permissive_recovery_guard(
        normal_load_n=5.0,
        force_norm_n=5.0,
        actual_tcp_speed_m_s=0.004,
        predicted_tcp_speed_m_s=0.055,
        braking_margin_m=0.010,
        prior_hold_s=0.0,
        prior_high_window_s=0.0,
        prior_actual_speed_violation_s=0.0,
        dt_s=0.002,
    )
    exhausted = bridge.step5d_v15_permissive_recovery_guard(
        normal_load_n=5.0,
        force_norm_n=5.0,
        actual_tcp_speed_m_s=0.004,
        predicted_tcp_speed_m_s=0.055,
        braking_margin_m=0.0,
        prior_hold_s=0.0,
        prior_high_window_s=0.0,
        prior_actual_speed_violation_s=0.0,
        dt_s=0.002,
    )
    return {
        "inside_margin_action": inside["action"],
        "inside_margin_reason": inside["reason"],
        "exhausted_margin_action": exhausted["action"],
        "exhausted_margin_reason": exhausted["reason"],
    }


def analyze() -> dict[str, Any]:
    all_csvs = SUCCESS_CSVS + [V11_CSV, V14_CSV]
    success = [summarize_csv(path, kind="success") for path in SUCCESS_CSVS]
    negative = [summarize_csv(path, kind="negative") for path in (V11_CSV, V14_CSV)]
    success_speed_p999 = max(item["actual_speed_m_s"]["p999"] for item in success)
    success_speed_max = max(item["actual_speed_m_s"]["max"] for item in success)
    v11_replay = replay_v15_guard(V11_CSV)
    v14_replay = replay_v15_guard(V14_CSV)
    success_replays = [replay_v15_guard(path) for path in SUCCESS_CSVS]
    cage = synthetic_cage_replay()
    return {
        "analysis_created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "offline_only_no_live_robot_action",
        "source_csvs": {
            "success": [str(path) for path in SUCCESS_CSVS],
            "negative": [str(V11_CSV), str(V14_CSV)],
        },
        "distributions": {
            "success": success,
            "negative": negative,
            "tcp_cage_margin": summarize_tcp_cage_availability(all_csvs),
        },
        "candidate_parameters": {
            "actual_speed_hold_source": "max success main-tracking p99.9 and max; do not use load jump alone",
            "actual_speed_hold_floor_m_s": success_speed_p999,
            "actual_speed_success_max_m_s": success_speed_max,
            "predicted_speed_policy": "predicted speed above the v14 0.050 m/s intervention threshold is recoverable hold unless a hard boundary is already violated",
            "hard_stop_boundaries": [
                "tcp_cage_distance_or_braking_margin_exhausted",
                "semantic_gate_failed",
                "hard_force_or_torque_or_joint_or_sensor_gate",
                "hold_timeout",
                "repeated_hold_limit",
                "nonfinite_inputs",
            ],
        },
        "replay": {
            "success": success_replays,
            "v11": v11_replay,
            "v14": v14_replay,
            "cage": cage,
        },
        "acceptance": {
            "success_no_hard_stop": all(item["first_stop"] is None for item in success_replays),
            "v14_enters_hold_before_hard_stop": v14_replay["first_hold"] is not None
            and v14_replay["first_hold"]["reason"] in {
                "predicted_tcp_speed_recoverable_hold",
                "low_load_predicted_tcp_speed_recoverable_hold",
                "actual_tcp_speed_watchdog_dwell_hold",
                "low_load_actual_tcp_speed_watchdog_dwell_hold",
            },
            "v11_hard_stops_before_escape_pass": v11_replay["first_stop"] is not None,
            "cage_margin_hard_stop": cage["inside_margin_action"] == "hold_zero_qdot"
            and cage["exhausted_margin_action"] == "stop_zero_qdot",
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    summary = analyze()
    if args.output is not None:
        write_json(args.output, summary)
    print(json.dumps(json_safe(summary), indent=2, sort_keys=True))
    return 0 if all(summary["acceptance"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
