#!/usr/bin/env python3
"""Replay Step5d v11 escape data against the v12 Stage25 guard."""

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
DEFAULT_RUN_DIR = EXPERIMENT_ROOT / "runs" / "bridge_step5d_strict_rnn_liveprep_v11_20260615_204601"
DEFAULT_CSV = DEFAULT_RUN_DIR / "bridge_rtde_500hz.csv"


def finite_float(row: dict[str, str], key: str, default: float = math.nan) -> float:
    value = row.get(key, "")
    if value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def linear_speed(row: dict[str, str]) -> float:
    values = [finite_float(row, f"ur_actual_TCP_speed_{idx}", 0.0) for idx in range(3)]
    return math.sqrt(sum(value * value for value in values))


def stage_rows(rows: list[dict[str, str]], stage: float) -> list[dict[str, str]]:
    result = []
    for row in rows:
        value = finite_float(row, "ur_output_double_register_35")
        if math.isfinite(value) and abs(value - stage) < 0.05:
            result.append(row)
    return result


def annotate_relative_times(rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    first_t = finite_float(rows[0], "t_monotonic_s")
    if not math.isfinite(first_t):
        raise RuntimeError("CSV does not contain a finite t_monotonic_s in the first row")
    for row in rows:
        t_monotonic_s = finite_float(row, "t_monotonic_s")
        if not math.isfinite(t_monotonic_s):
            raise RuntimeError("CSV contains a row without finite t_monotonic_s")
        row["t_rel_s"] = f"{max(0.0, t_monotonic_s - first_t):.9f}"


def seconds_after_stage_start(row: dict[str, str], stage_first_t: float) -> float:
    return finite_float(row, "t_rel_s") - stage_first_t


def duration_s(rows: list[dict[str, str]]) -> float:
    if len(rows) < 2:
        return 0.0
    return max(0.0, finite_float(rows[-1], "t_rel_s", 0.0) - finite_float(rows[0], "t_rel_s", 0.0))


def displacement_m(rows: list[dict[str, str]]) -> list[float]:
    if not rows:
        return [0.0, 0.0, 0.0]
    first = rows[0]
    last = rows[-1]
    return [
        finite_float(last, f"ur_actual_TCP_pose_{idx}", 0.0)
        - finite_float(first, f"ur_actual_TCP_pose_{idx}", 0.0)
        for idx in range(3)
    ]


def first_speed_crossing(rows: list[dict[str, str]], threshold_m_s: float) -> dict[str, Any] | None:
    for index, row in enumerate(rows):
        speed = linear_speed(row)
        if speed > threshold_m_s:
            return {
                "stage25_index": index,
                "t_rel_s": finite_float(row, "t_rel_s"),
                "seconds_after_stage25_start": seconds_after_stage_start(row, finite_float(rows[0], "t_rel_s")),
                "linear_speed_m_s": speed,
                "normal_load_n": finite_float(row, "_step4e_normal_load_n"),
                "tcp_z_m": finite_float(row, "ur_actual_TCP_pose_2"),
            }
    return None


def replay_v12_guard(rows: list[dict[str, str]]) -> dict[str, Any]:
    loss_s = 0.0
    previous_t: float | None = None
    for index, row in enumerate(rows):
        t_rel_s = finite_float(row, "t_rel_s")
        dt_s = 0.002 if previous_t is None else max(0.0, t_rel_s - previous_t)
        previous_t = t_rel_s
        ok, loss_s, reason = bridge.step5d_v12_line_guard(
            normal_load_n=finite_float(row, "_step4e_normal_load_n"),
            force_norm_n=finite_float(row, "force_norm_n"),
            tcp_linear_speed_m_s=linear_speed(row),
            prior_loss_s=loss_s,
            dt_s=dt_s,
        )
        if not ok:
            return {
                "triggered": True,
                "reason": reason,
                "stage25_index": index,
                "t_rel_s": t_rel_s,
                "dt_s": dt_s,
                "loss_s": loss_s,
                "normal_load_n": finite_float(row, "_step4e_normal_load_n"),
                "force_norm_n": finite_float(row, "force_norm_n"),
                "tcp_linear_speed_m_s": linear_speed(row),
                "tcp_z_m": finite_float(row, "ur_actual_TCP_pose_2"),
            }
    return {"triggered": False, "reason": "no_v12_guard_trigger"}


def replay_v13_contact_safety(rows: list[dict[str, str]]) -> dict[str, Any]:
    hold_s = 0.0
    high_window_s = 0.0
    actual_speed_violation_s = 0.0
    previous_t: float | None = None
    first_hold: dict[str, Any] | None = None
    for index, row in enumerate(rows):
        t_rel_s = finite_float(row, "t_rel_s")
        dt_s = 0.002 if previous_t is None else max(0.0, t_rel_s - previous_t)
        previous_t = t_rel_s
        result = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=finite_float(row, "_step4e_normal_load_n"),
            force_norm_n=finite_float(row, "force_norm_n"),
            actual_tcp_speed_m_s=linear_speed(row),
            predicted_tcp_speed_m_s=0.0,
            prior_hold_s=hold_s,
            prior_high_window_s=high_window_s,
            prior_actual_speed_violation_s=actual_speed_violation_s,
            dt_s=dt_s,
        )
        hold_s = float(result["hold_s"])
        high_window_s = float(result["high_window_s"])
        actual_speed_violation_s = float(result["actual_speed_violation_s"])
        if first_hold is None and result["action"] == "hold_zero_qdot":
            first_hold = {
                "stage25_index": index,
                "t_rel_s": t_rel_s,
                "dt_s": dt_s,
                "reason": result["reason"],
                "normal_load_n": finite_float(row, "_step4e_normal_load_n"),
                "force_norm_n": finite_float(row, "force_norm_n"),
                "tcp_linear_speed_m_s": linear_speed(row),
                "cmd_valid_expected": 1.0,
                "qdot_expected_rad_s": [0.0] * 6,
            }
        if result["action"] == "stop_zero_qdot":
            return {
                "triggered": True,
                "reason": result["reason"],
                "stage25_index": index,
                "t_rel_s": t_rel_s,
                "dt_s": dt_s,
                "hold_s": hold_s,
                "high_window_s": high_window_s,
                "actual_speed_violation_s": actual_speed_violation_s,
                "normal_load_n": finite_float(row, "_step4e_normal_load_n"),
                "force_norm_n": finite_float(row, "force_norm_n"),
                "tcp_linear_speed_m_s": linear_speed(row),
                "stop_request_expected": 1.0,
                "qdot_expected_rad_s": [0.0] * 6,
                "first_hold": first_hold,
            }
    return {
        "triggered": False,
        "reason": "no_v13_contact_safety_stop",
        "first_hold": first_hold,
    }


def analyze(csv_path: Path = DEFAULT_CSV) -> dict[str, Any]:
    with csv_path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    annotate_relative_times(rows)
    stage25_3 = stage_rows(rows, 25.3)
    stage25_0 = stage_rows(rows, 25.0)
    if not stage25_0:
        raise RuntimeError(f"No Stage 25.0 rows in {csv_path}")
    guard_trigger = replay_v12_guard(stage25_0)
    v13_trigger = replay_v13_contact_safety(stage25_0)
    first_0_02 = first_speed_crossing(stage25_0, 0.02)
    first_0_05 = first_speed_crossing(stage25_0, 0.05)
    peak_row = max(stage25_0, key=linear_speed)
    stage25_first_t = finite_float(stage25_0[0], "t_rel_s")
    first_low_load = next(
        (
            {
                "stage25_index": index,
                "t_rel_s": finite_float(row, "t_rel_s"),
                "seconds_after_stage25_start": seconds_after_stage_start(row, stage25_first_t),
                "normal_load_n": finite_float(row, "_step4e_normal_load_n"),
                "tcp_linear_speed_m_s": linear_speed(row),
                "tcp_z_m": finite_float(row, "ur_actual_TCP_pose_2"),
            }
            for index, row in enumerate(stage25_0)
            if finite_float(row, "_step4e_normal_load_n") < bridge.STEP5D_V12_LINE_CONTACT_LOW_STOP_N
        ),
        None,
    )
    return {
        "source_csv": str(csv_path),
        "analysis_created_at": datetime.now().isoformat(timespec="seconds"),
        "conclusion": (
            "v11 Stage 25.3 deadband acquire was slow and released, but Stage 25.0 lost contact; "
            "cmd_valid stayed true and qdot/speedj saturated until operator E-stop."
        ),
        "stage25_3": {
            "rows": len(stage25_3),
            "duration_s": duration_s(stage25_3),
            "start_t_rel_s": finite_float(stage25_3[0], "t_rel_s") if stage25_3 else None,
            "end_t_rel_s": finite_float(stage25_3[-1], "t_rel_s") if stage25_3 else None,
            "max_tcp_linear_speed_m_s": max((linear_speed(row) for row in stage25_3), default=0.0),
        },
        "stage25_0": {
            "rows": len(stage25_0),
            "duration_s": duration_s(stage25_0),
            "start_t_rel_s": finite_float(stage25_0[0], "t_rel_s"),
            "end_t_rel_s": finite_float(stage25_0[-1], "t_rel_s"),
            "max_tcp_linear_speed_m_s": linear_speed(peak_row),
            "mean_tcp_linear_speed_m_s": sum(linear_speed(row) for row in stage25_0) / len(stage25_0),
            "displacement_m": displacement_m(stage25_0),
            "peak_speed": {
                "t_rel_s": finite_float(peak_row, "t_rel_s"),
                "normal_load_n": finite_float(peak_row, "_step4e_normal_load_n"),
                "force_norm_n": finite_float(peak_row, "force_norm_n"),
                "tcp_z_m": finite_float(peak_row, "ur_actual_TCP_pose_2"),
                "qdot_cmd_rad_s": [finite_float(peak_row, f"_step5c_cmd_qd{idx}_rad_s") for idx in range(6)],
            },
            "first_low_load": first_low_load,
            "first_speed_gt_0_02_m_s": first_0_02,
            "first_speed_gt_0_05_m_s": first_0_05,
        },
        "v12_guard_replay": {
            **guard_trigger,
            "trigger_before_speed_gt_0_02_m_s": (
                bool(guard_trigger.get("triggered"))
                and first_0_02 is not None
                and float(guard_trigger["t_rel_s"]) < float(first_0_02["t_rel_s"])
            ),
            "trigger_before_speed_gt_0_05_m_s": (
                bool(guard_trigger.get("triggered"))
                and first_0_05 is not None
                and float(guard_trigger["t_rel_s"]) < float(first_0_05["t_rel_s"])
            ),
        },
        "v12_guard_parameters": {
            "qdot_limit_rad_s": bridge.STEP5D_V12_QDOT_LIMIT_RAD_S,
            "qdot_slew_rad_s2": bridge.STEP5D_V12_QDOT_SLEW_RAD_S2,
            "line_low_load_stop_n": bridge.STEP5D_V12_LINE_CONTACT_LOW_STOP_N,
            "line_contact_min_n": bridge.STEP5D_V12_LINE_CONTACT_MIN_N,
            "line_contact_max_n": bridge.STEP5D_V12_LINE_CONTACT_MAX_N,
            "line_force_norm_max_n": bridge.STEP5D_V12_LINE_FORCE_NORM_MAX_N,
            "line_contact_loss_limit_s": bridge.STEP5D_V12_LINE_CONTACT_LOSS_LIMIT_S,
            "line_tcp_speed_max_m_s": bridge.STEP5D_V12_LINE_TCP_SPEED_MAX_M_S,
        },
        "v13_contact_safety_replay": {
            **v13_trigger,
            "trigger_before_speed_gt_0_05_m_s": (
                bool(v13_trigger.get("triggered"))
                and first_0_05 is not None
                and float(v13_trigger["t_rel_s"]) < float(first_0_05["t_rel_s"])
            ),
            "first_hold_before_speed_gt_0_02_m_s": (
                v13_trigger.get("first_hold") is not None
                and first_0_02 is not None
                and float(v13_trigger["first_hold"]["t_rel_s"]) < float(first_0_02["t_rel_s"])
            ),
        },
        "v13_contact_safety_parameters": {
            "hard_low_load_n": bridge.STEP5D_V13_HARD_LOW_LOAD_N,
            "soft_low_load_n": bridge.STEP5D_V13_SOFT_LOW_LOAD_N,
            "valid_contact_min_n": bridge.STEP5D_V13_VALID_CONTACT_MIN_N,
            "valid_contact_max_n": bridge.STEP5D_V13_VALID_CONTACT_MAX_N,
            "force_norm_valid_max_n": bridge.STEP5D_V13_FORCE_NORM_VALID_MAX_N,
            "low_load_speed_stop_m_s": bridge.STEP5D_V13_LOW_LOAD_SPEED_STOP_M_S,
            "absolute_speed_stop_m_s": bridge.STEP5D_V13_ABSOLUTE_SPEED_STOP_M_S,
            "actual_speed_dwell_stop_s": bridge.STEP5D_V13_ACTUAL_SPEED_DWELL_STOP_S,
            "low_load_hold_timeout_s": bridge.STEP5D_V13_LOW_LOAD_HOLD_TIMEOUT_S,
            "high_window_dwell_stop_s": bridge.STEP5D_V13_HIGH_WINDOW_DWELL_STOP_S,
        },
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    summary = analyze(args.csv)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.output, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
