#!/usr/bin/env python3
"""Offline Step5b contact-control core replay validator.

This validator replays retained bridge CSV logs through the pure ROS2-ready
Step5b contact core and compares the resulting command registers to the logged
`step4e_cmd_*` columns. It is offline-only: no bridge start, no RTDE, no
Dashboard, no TP play, no robot motion, and no zero/tare operation.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers import step5b_contact_control_core as core  # noqa: E402


DEFAULT_CSVS = [
    EXPERIMENT_ROOT / "runs" / "bridge_step5b_contact_cycloid_baseline_v1_20260612_082352" / "bridge_rtde_500hz.csv",
    EXPERIMENT_ROOT / "runs" / "bridge_step5b_contact_cycloid_baseline_v1_20260614_222309" / "bridge_rtde_500hz.csv",
    EXPERIMENT_ROOT / "runs" / "bridge_step5b_contact_cycloid_baseline_v1_20260616_164129" / "bridge_rtde_500hz.csv",
]
STAGE_TABLE_PATH = EXPERIMENT_ROOT / "config" / "step5_stage_table.json"
SAFE_FRAME_PATH = EXPERIMENT_ROOT / "config" / "step5_safe_frame.json"
STAGE_ID = "step5_contact_cycloid_baseline_v1"
COMMAND_COLUMNS = [
    "step4e_cmd_valid",
    "step4e_cmd_vx_m_s",
    "step4e_cmd_vy_m_s",
    "step4e_cmd_vz_m_s",
    "step4e_cmd_wx_rad_s",
    "step4e_cmd_wy_rad_s",
    "step4e_cmd_wz_rad_s",
]
TWIST_COLUMNS = COMMAND_COLUMNS[1:]
TRACE_FIELDS = [
    "source_csv",
    "row_index",
    "relative_time_s",
    "robot_stage",
    "replayable",
    "exclusion_reason",
    "logged_cmd_valid",
    "core_cmd_valid",
    "cmd_valid_error",
    "logged_cmd_vx_m_s",
    "core_cmd_vx_m_s",
    "error_cmd_vx_m_s",
    "logged_cmd_vy_m_s",
    "core_cmd_vy_m_s",
    "error_cmd_vy_m_s",
    "logged_cmd_vz_m_s",
    "core_cmd_vz_m_s",
    "error_cmd_vz_m_s",
    "logged_cmd_wx_rad_s",
    "core_cmd_wx_rad_s",
    "error_cmd_wx_rad_s",
    "logged_cmd_wy_rad_s",
    "core_cmd_wy_rad_s",
    "error_cmd_wy_rad_s",
    "logged_cmd_wz_rad_s",
    "core_cmd_wz_rad_s",
    "error_cmd_wz_rad_s",
    "twist_vector_error",
    "normal_load_n",
    "force_error_n",
    "path_time_s",
    "desired_x_m",
    "desired_y_m",
    "hold_reason",
    "normal_filter_source",
    "fail_reason",
]
OFFLINE_SAFETY_BOUNDARY = [
    "offline analysis only",
    "no bridge start",
    "no RTDE write",
    "no Dashboard command",
    "no TP Play/upload",
    "no URScript send",
    "no robot motion",
    "no zero_ftsensor",
    "no Kunwei tare/config/write",
    "no payload/TCP/safety write",
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_stage(table: dict[str, Any]) -> dict[str, Any]:
    for stage in table.get("stages", []):
        if stage.get("id") == STAGE_ID:
            return stage
    raise RuntimeError(f"missing Step5b stage {STAGE_ID!r} in {STAGE_TABLE_PATH}")


def finite_float(row: dict[str, str], key: str) -> float:
    value = row.get(key, "")
    if value == "":
        return math.nan
    try:
        return float(value)
    except ValueError:
        return math.nan


def fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.12g}"
    return str(value)


def is_step5b_control_stage(stage: float) -> bool:
    return (
        abs(stage - 25.05) < 0.03
        or abs(stage - 25.1) < 0.03
        or abs(stage - 25.2) < 0.05
        or abs(stage - 25.3) < 0.05
        or abs(stage - 25.0) < 0.05
    )


def is_stage25_line(stage: float) -> bool:
    return math.isfinite(stage) and abs(stage - 25.0) < 0.05


def load_params_for_csv(csv_path: Path, stage: dict[str, Any], *, allow_default_params: bool) -> tuple[core.Step5bContactParams, str]:
    metadata_path = csv_path.with_name("metadata.json")
    if not metadata_path.is_file():
        if not allow_default_params:
            raise RuntimeError(f"missing metadata.json for {csv_path}")
        return core.Step5bContactParams.from_metadata_args({}, stage=stage), "explicit-default-params"
    metadata = load_json(metadata_path)
    args = metadata.get("args")
    if not isinstance(args, dict):
        if not allow_default_params:
            raise RuntimeError(f"metadata args missing or invalid: {metadata_path}")
        return core.Step5bContactParams.from_metadata_args({}, stage=stage), "explicit-default-params"
    params = core.Step5bContactParams.from_metadata_args(args, stage=stage)
    if params.bridge_profile != "step5b_v1":
        raise RuntimeError(f"{csv_path} metadata is not Step5b: bridge_profile={params.bridge_profile!r}")
    if params.bridge_mode != "line":
        raise RuntimeError(f"{csv_path} metadata is not line mode: bridge_mode={params.bridge_mode!r}")
    return params, f"{metadata_path}:args"


def row_sample(row: dict[str, str], params: core.Step5bContactParams) -> tuple[core.Step5bSample | None, str]:
    stage = finite_float(row, "ur_output_double_register_35")
    if not math.isfinite(stage):
        return None, "missing_robot_stage"
    pose = tuple(finite_float(row, f"ur_actual_TCP_pose_{idx}") for idx in range(6))
    if not all(math.isfinite(value) for value in pose):
        return None, "missing_tcp_pose"
    wrench = tuple(
        finite_float(row, key)
        for key in ("fx_n_zeroed", "fy_n_zeroed", "fz_n_zeroed", "mx_nm_zeroed", "my_nm_zeroed", "mz_nm_zeroed")
    )
    if not all(math.isfinite(value) for value in wrench):
        return None, "missing_wrench"
    speed = tuple(finite_float(row, f"ur_actual_TCP_speed_{idx}") for idx in range(6))
    speed_or_none = speed if all(math.isfinite(value) for value in speed) else None
    sensor_ok = finite_float(row, "sensor_ok")
    if not math.isfinite(sensor_ok):
        sensor_ok = 0.0
    return (
        core.Step5bSample(
            tcp_pose=pose,  # type: ignore[arg-type]
            tcp_wrench=wrench,  # type: ignore[arg-type]
            sensor_ok=sensor_ok,
            robot_stage=stage,
            dt_s=params.sample_period_s,
            tcp_speed=speed_or_none,  # type: ignore[arg-type]
        ),
        "",
    )


def empty_trace_row(csv_path: Path, row_index: int, relative_time_s: float, stage: float, reason: str) -> dict[str, Any]:
    row = {field: "" for field in TRACE_FIELDS}
    row.update(
        {
            "source_csv": str(csv_path),
            "row_index": row_index,
            "relative_time_s": relative_time_s,
            "robot_stage": stage,
            "replayable": 0,
            "exclusion_reason": reason,
            "fail_reason": reason,
        }
    )
    return row


def replay_csv(
    csv_path: Path,
    *,
    params: core.Step5bContactParams,
    basis: core.Step5bPathBasis,
    trace_writer: csv.DictWriter[str],
    abs_tol: float,
    max_rows: int | None,
) -> dict[str, Any]:
    if not csv_path.is_file():
        raise RuntimeError(f"missing CSV: {csv_path}")
    state = core.Step5bContactState()
    rows = 0
    replayable = 0
    stage25_active = 0
    mismatches = 0
    max_axis_errors = {column: 0.0 for column in COMMAND_COLUMNS}
    max_vector_error = 0.0
    exclusions: Counter[str] = Counter()
    first_t: float | None = None

    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        missing_columns = [column for column in COMMAND_COLUMNS if column not in fieldnames]
        if missing_columns:
            raise RuntimeError(f"{csv_path} missing command columns: {', '.join(missing_columns)}")
        for row_index, row in enumerate(reader):
            if max_rows is not None and rows >= max_rows:
                break
            rows += 1
            t_mono = finite_float(row, "t_monotonic_s")
            if math.isfinite(t_mono) and first_t is None:
                first_t = t_mono
            relative_time = (t_mono - first_t) if (math.isfinite(t_mono) and first_t is not None) else row_index * params.sample_period_s
            stage = finite_float(row, "ur_output_double_register_35")
            sample, sample_reason = row_sample(row, params)
            if sample is None:
                exclusions[sample_reason] += 1
                trace_writer.writerow({key: fmt(value) for key, value in empty_trace_row(csv_path, row_index, relative_time, stage, sample_reason).items()})
                continue
            result, state = core.compute_step5b_contact_sample(sample, state, params, basis)
            if is_stage25_line(stage):
                stage25_active += 1
            if not is_step5b_control_stage(stage):
                exclusions["non_control_stage"] += 1
                trace_writer.writerow(
                    {
                        key: fmt(value)
                        for key, value in empty_trace_row(csv_path, row_index, relative_time, stage, "non_control_stage").items()
                    }
                )
                continue

            logged = {column: finite_float(row, column) for column in COMMAND_COLUMNS}
            if not all(math.isfinite(value) for value in logged.values()):
                exclusions["missing_logged_command"] += 1
                trace_writer.writerow(
                    {
                        key: fmt(value)
                        for key, value in empty_trace_row(csv_path, row_index, relative_time, stage, "missing_logged_command").items()
                    }
                )
                continue

            core_values = {
                "step4e_cmd_valid": result.cmd_valid,
                "step4e_cmd_vx_m_s": result.cmd_vx_m_s,
                "step4e_cmd_vy_m_s": result.cmd_vy_m_s,
                "step4e_cmd_vz_m_s": result.cmd_vz_m_s,
                "step4e_cmd_wx_rad_s": result.cmd_wx_rad_s,
                "step4e_cmd_wy_rad_s": result.cmd_wy_rad_s,
                "step4e_cmd_wz_rad_s": result.cmd_wz_rad_s,
            }
            errors = {column: core_values[column] - logged[column] for column in COMMAND_COLUMNS}
            axis_abs = {column: abs(value) for column, value in errors.items()}
            vector_error = math.sqrt(sum(errors[column] * errors[column] for column in TWIST_COLUMNS))
            row_mismatch = any(value > abs_tol for value in axis_abs.values())
            if row_mismatch:
                mismatches += 1
            replayable += 1
            for column, value in axis_abs.items():
                max_axis_errors[column] = max(max_axis_errors[column], value)
            max_vector_error = max(max_vector_error, vector_error)

            trace_writer.writerow(
                {
                    "source_csv": str(csv_path),
                    "row_index": row_index,
                    "relative_time_s": fmt(relative_time),
                    "robot_stage": fmt(stage),
                    "replayable": 1,
                    "exclusion_reason": "",
                    "logged_cmd_valid": fmt(logged["step4e_cmd_valid"]),
                    "core_cmd_valid": fmt(result.cmd_valid),
                    "cmd_valid_error": fmt(errors["step4e_cmd_valid"]),
                    "logged_cmd_vx_m_s": fmt(logged["step4e_cmd_vx_m_s"]),
                    "core_cmd_vx_m_s": fmt(result.cmd_vx_m_s),
                    "error_cmd_vx_m_s": fmt(errors["step4e_cmd_vx_m_s"]),
                    "logged_cmd_vy_m_s": fmt(logged["step4e_cmd_vy_m_s"]),
                    "core_cmd_vy_m_s": fmt(result.cmd_vy_m_s),
                    "error_cmd_vy_m_s": fmt(errors["step4e_cmd_vy_m_s"]),
                    "logged_cmd_vz_m_s": fmt(logged["step4e_cmd_vz_m_s"]),
                    "core_cmd_vz_m_s": fmt(result.cmd_vz_m_s),
                    "error_cmd_vz_m_s": fmt(errors["step4e_cmd_vz_m_s"]),
                    "logged_cmd_wx_rad_s": fmt(logged["step4e_cmd_wx_rad_s"]),
                    "core_cmd_wx_rad_s": fmt(result.cmd_wx_rad_s),
                    "error_cmd_wx_rad_s": fmt(errors["step4e_cmd_wx_rad_s"]),
                    "logged_cmd_wy_rad_s": fmt(logged["step4e_cmd_wy_rad_s"]),
                    "core_cmd_wy_rad_s": fmt(result.cmd_wy_rad_s),
                    "error_cmd_wy_rad_s": fmt(errors["step4e_cmd_wy_rad_s"]),
                    "logged_cmd_wz_rad_s": fmt(logged["step4e_cmd_wz_rad_s"]),
                    "core_cmd_wz_rad_s": fmt(result.cmd_wz_rad_s),
                    "error_cmd_wz_rad_s": fmt(errors["step4e_cmd_wz_rad_s"]),
                    "twist_vector_error": fmt(vector_error),
                    "normal_load_n": fmt(result.normal_load_n),
                    "force_error_n": fmt(result.force_error_n),
                    "path_time_s": fmt(result.path_time_s),
                    "desired_x_m": fmt(result.desired_x_m),
                    "desired_y_m": fmt(result.desired_y_m),
                    "hold_reason": result.hold_reason,
                    "normal_filter_source": result.normal_filter_source,
                    "fail_reason": "mismatch" if row_mismatch else "",
                }
            )

    return {
        "csv": str(csv_path),
        "rows": rows,
        "replayable_rows": replayable,
        "stage25_active_rows": stage25_active,
        "mismatch_rows": mismatches,
        "max_axis_errors": max_axis_errors,
        "max_vector_error": max_vector_error,
        "exclusions": dict(sorted(exclusions.items())),
    }


def default_output_dir() -> Path:
    return EXPERIMENT_ROOT / "runs" / f"step5b_contact_core_replay_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", action="append", dest="csvs", type=Path, help="Step5b bridge CSV to replay; may repeat")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--abs-tol", type=float, default=1e-6)
    parser.add_argument("--max-rows-per-csv", type=int, default=None)
    parser.add_argument("--allow-default-params", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.abs_tol < 0.0:
        raise SystemExit("--abs-tol must be non-negative")
    if args.max_rows_per_csv is not None and args.max_rows_per_csv <= 0:
        raise SystemExit("--max-rows-per-csv must be positive")

    csv_paths = [path.resolve() for path in (args.csvs or DEFAULT_CSVS)]
    table = load_json(STAGE_TABLE_PATH)
    stage = find_stage(table)
    basis = core.Step5bPathBasis.from_safe_frame(load_json(SAFE_FRAME_PATH))
    output_dir = (args.output_dir or default_output_dir()).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_path = output_dir / "replay_trace.csv"
    summary_path = output_dir / "summary.json"

    per_csv: list[dict[str, Any]] = []
    parameter_sources: dict[str, str] = {}
    with trace_path.open("w", newline="", encoding="utf-8") as trace_handle:
        trace_writer: csv.DictWriter[str] = csv.DictWriter(trace_handle, fieldnames=TRACE_FIELDS)
        trace_writer.writeheader()
        for csv_path in csv_paths:
            params, source = load_params_for_csv(csv_path, stage, allow_default_params=args.allow_default_params)
            parameter_sources[str(csv_path)] = source
            per_csv.append(
                replay_csv(
                    csv_path,
                    params=params,
                    basis=basis,
                    trace_writer=trace_writer,
                    abs_tol=args.abs_tol,
                    max_rows=args.max_rows_per_csv,
                )
            )

    total_rows = sum(item["rows"] for item in per_csv)
    total_replayable = sum(item["replayable_rows"] for item in per_csv)
    total_stage25 = sum(item["stage25_active_rows"] for item in per_csv)
    total_mismatches = sum(item["mismatch_rows"] for item in per_csv)
    total_exclusions: Counter[str] = Counter()
    max_axis_errors = {column: 0.0 for column in COMMAND_COLUMNS}
    max_vector_error = 0.0
    for item in per_csv:
        total_exclusions.update(item["exclusions"])
        max_vector_error = max(max_vector_error, float(item["max_vector_error"]))
        for column, value in item["max_axis_errors"].items():
            max_axis_errors[column] = max(max_axis_errors[column], float(value))
    ok = total_mismatches == 0 and total_replayable > 0
    summary = {
        "ok": ok,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_csvs": [str(path) for path in csv_paths],
        "output_dir": str(output_dir),
        "trace_csv": str(trace_path),
        "summary_json": str(summary_path),
        "tolerance_abs": args.abs_tol,
        "row_counts": {
            "total_rows": total_rows,
            "replayable_rows": total_replayable,
            "stage25_active_rows": total_stage25,
            "mismatch_rows": total_mismatches,
        },
        "max_axis_errors": max_axis_errors,
        "max_vector_error": max_vector_error,
        "exclusions": dict(sorted(total_exclusions.items())),
        "per_csv": per_csv,
        "parameter_source": parameter_sources,
        "stage_table": str(STAGE_TABLE_PATH),
        "safe_frame": str(SAFE_FRAME_PATH),
        "offline_safety_boundary": OFFLINE_SAFETY_BOUNDARY,
    }
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
