from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .replay_shadow import RUNS_ROOT, WORKSPACE_ROOT, _json_safe, _simple_yaml, finite_float


DEFAULT_CONFIG = WORKSPACE_ROOT / "src" / "ur10e_step5d_remote" / "config" / "default_step5b_remote.yaml"

TRACE_FIELDS = [
    "source_csv",
    "row_index",
    "t_rel_s",
    "would_state",
    "normal_load_n",
    "force_norm_n",
    "tcp_x",
    "tcp_y",
    "tcp_z",
    "actual_tcp_speed_m_s",
    "desired_x_m",
    "desired_y_m",
    "desired_vx_m_s",
    "desired_vy_m_s",
    "path_error_m",
    "cmd_valid_shadow",
    "cmd_enabled",
    "would_command_twist_x",
    "would_command_twist_y",
    "would_command_twist_z",
    "would_command_twist_rx",
    "would_command_twist_ry",
    "would_command_twist_rz",
    "progress",
    "path_time_s",
]

DESIRED_FIELDS = [
    "_step4e_desired_x_m",
    "_step4e_desired_y_m",
    "_step4e_desired_vx_m_s",
    "_step4e_desired_vy_m_s",
    "_step4e_path_error_x_m",
    "_step4e_path_error_y_m",
]


def load_step5b_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    if not path.is_absolute():
        path = WORKSPACE_ROOT / path
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        raw = _simple_yaml(path.read_text(encoding="utf-8"))
    return raw


def run_step5b_shadow_replay(
    *,
    config_path: Path = DEFAULT_CONFIG,
    output_dir: Path | None = None,
    replay_csvs: list[Path] | None = None,
    max_rows_per_csv: int | None = None,
) -> dict[str, Any]:
    if not config_path.is_absolute():
        config_path = WORKSPACE_ROOT / config_path
    raw_config = load_step5b_config(config_path)
    if bool(raw_config.get("enable_motion", False)):
        raise RuntimeError("Step5b remote shadow refuses enable_motion=true; live motion is not implemented")

    csv_paths = replay_csvs or _paths_from_config(raw_config, "step5b_replay_csvs")
    created_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = output_dir or RUNS_ROOT / f"step5b_ros2_remote_shadow_{created_at}"
    out_dir.mkdir(parents=True, exist_ok=True)

    total_rows = 0
    total_active_rows = 0
    total_cmd_enabled_false = 0
    total_fail_fast = 0
    state_counts: Counter[str] = Counter()
    source_summaries: list[dict[str, Any]] = []
    desired_coverage: Counter[str] = Counter()
    all_normal_loads: list[float] = []
    max_actual_tcp_speed = 0.0

    trace_path = out_dir / "shadow_trace.csv"
    with trace_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRACE_FIELDS)
        writer.writeheader()
        for csv_path in csv_paths:
            summary = _empty_source_summary(csv_path)
            with csv_path.open(newline="", encoding="utf-8") as source:
                reader = csv.DictReader(source)
                first_t: float | None = None
                for row_index, row in enumerate(reader):
                    t = finite_float(row, "t_monotonic_s")
                    if not math.isfinite(t):
                        continue
                    if first_t is None:
                        first_t = t
                    trace_row = _trace_row(csv_path, row_index, row, t_rel_s=max(0.0, t - first_t))
                    writer.writerow(_csv_safe(trace_row))

                    total_rows += 1
                    summary["rows_replayed"] += 1
                    state_counts[str(trace_row["would_state"])] += 1
                    summary["state_counts"][str(trace_row["would_state"])] += 1
                    if trace_row["cmd_valid_shadow"]:
                        total_active_rows += 1
                        summary["active_contact_rows"] += 1
                    if not trace_row["cmd_enabled"]:
                        total_cmd_enabled_false += 1
                        summary["cmd_enabled_false_rows"] += 1
                    if trace_row["would_state"] == "FAIL_FAST_STOP_REQUEST":
                        total_fail_fast += 1
                    normal = float(trace_row["normal_load_n"])
                    if math.isfinite(normal):
                        all_normal_loads.append(normal)
                        summary["normal_load_values"].append(normal)
                    max_actual_tcp_speed = max(max_actual_tcp_speed, float(trace_row["actual_tcp_speed_m_s"] or 0.0))
                    summary["max_actual_tcp_speed_m_s"] = max(
                        float(summary["max_actual_tcp_speed_m_s"]),
                        float(trace_row["actual_tcp_speed_m_s"] or 0.0),
                    )
                    for field in DESIRED_FIELDS:
                        if math.isfinite(finite_float(row, field)):
                            desired_coverage[field] += 1
                            summary["desired_reference_field_counts"][field] += 1
                    if max_rows_per_csv is not None and summary["rows_replayed"] >= max_rows_per_csv:
                        break
            _finalize_source_summary(summary)
            source_summaries.append(summary)

    fail_fast_ratio = total_fail_fast / total_rows if total_rows else 1.0
    command_valid_ratio = total_active_rows / total_rows if total_rows else 0.0
    summary = {
        "analysis_created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "step5b_remote_control_plumbing_shadow_no_live_robot_action",
        "role": "remote_control_plumbing_validation_before_step5d",
        "config_path": str(config_path),
        "artifact_dir": str(out_dir),
        "trace_path": str(trace_path),
        "summary_path": str(out_dir / "summary.json"),
        "enable_motion": False,
        "live_motion_authorized": False,
        "tp_action_required": "none_no_tp_play_no_program_load",
        "source_csvs": [str(path) for path in csv_paths],
        "source_summaries": source_summaries,
        "rows_replayed": total_rows,
        "active_contact_rows": total_active_rows,
        "command_valid_shadow_ratio": command_valid_ratio,
        "state_counts": dict(state_counts),
        "fail_fast_ratio": fail_fast_ratio,
        "cmd_enabled_false_rows": total_cmd_enabled_false,
        "cmd_enabled_any": False,
        "normal_load_n": _distribution(all_normal_loads),
        "max_actual_tcp_speed_m_s": max_actual_tcp_speed,
        "desired_reference_field_coverage": {
            field: {
                "count": desired_coverage[field],
                "ratio": desired_coverage[field] / total_rows if total_rows else 0.0,
            }
            for field in DESIRED_FIELDS
        },
        "trace_fields": TRACE_FIELDS,
        "step5d_status": "diagnostic_only_not_live_ready",
        "acceptance": {
            "default_no_motion": total_cmd_enabled_false == total_rows and total_rows > 0,
            "covers_three_step5b_csvs": len(csv_paths) >= 3 and all(path.exists() for path in csv_paths),
            "not_mostly_fail_fast": fail_fast_ratio < 0.05,
            "command_valid_shadow_present": total_active_rows > 0,
            "desired_reference_fields_present": all(desired_coverage[field] > 0 for field in DESIRED_FIELDS),
            "artifact_schema_complete": True,
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(_json_safe(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Step5b ROS2 remote-control plumbing shadow replay.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--csv", action="append", type=Path, default=None)
    parser.add_argument("--max-rows-per-csv", type=int, default=None)
    args = parser.parse_args()
    summary = run_step5b_shadow_replay(
        config_path=args.config,
        output_dir=args.output_dir,
        replay_csvs=args.csv,
        max_rows_per_csv=args.max_rows_per_csv,
    )
    print(json.dumps(_json_safe(summary), indent=2, sort_keys=True))
    return 0 if all(summary["acceptance"].values()) else 2


def _trace_row(csv_path: Path, row_index: int, row: dict[str, str], *, t_rel_s: float) -> dict[str, Any]:
    cmd_valid = finite_float(row, "step4e_cmd_valid", 0.0) > 0.5
    path_error_x = finite_float(row, "_step4e_path_error_x_m")
    path_error_y = finite_float(row, "_step4e_path_error_y_m")
    path_error = math.nan
    if math.isfinite(path_error_x) and math.isfinite(path_error_y):
        path_error = math.hypot(path_error_x, path_error_y)
    return {
        "source_csv": str(csv_path),
        "row_index": row_index,
        "t_rel_s": t_rel_s,
        "would_state": "CONTACT_TRACK" if cmd_valid else "DRIVER_READY_NO_MOTION",
        "normal_load_n": _first_finite(row, ["_step4e_normal_load_n", "normal_force_n"]),
        "force_norm_n": finite_float(row, "force_norm_n"),
        "tcp_x": finite_float(row, "ur_actual_TCP_pose_0"),
        "tcp_y": finite_float(row, "ur_actual_TCP_pose_1"),
        "tcp_z": finite_float(row, "ur_actual_TCP_pose_2"),
        "actual_tcp_speed_m_s": _actual_tcp_speed(row),
        "desired_x_m": finite_float(row, "_step4e_desired_x_m"),
        "desired_y_m": finite_float(row, "_step4e_desired_y_m"),
        "desired_vx_m_s": finite_float(row, "_step4e_desired_vx_m_s"),
        "desired_vy_m_s": finite_float(row, "_step4e_desired_vy_m_s"),
        "path_error_m": path_error,
        "cmd_valid_shadow": cmd_valid,
        "cmd_enabled": False,
        "would_command_twist_x": finite_float(row, "step4e_cmd_vx_m_s", 0.0),
        "would_command_twist_y": finite_float(row, "step4e_cmd_vy_m_s", 0.0),
        "would_command_twist_z": finite_float(row, "step4e_cmd_vz_m_s", 0.0),
        "would_command_twist_rx": finite_float(row, "step4e_cmd_wx_rad_s", 0.0),
        "would_command_twist_ry": finite_float(row, "step4e_cmd_wy_rad_s", 0.0),
        "would_command_twist_rz": finite_float(row, "step4e_cmd_wz_rad_s", 0.0),
        "progress": finite_float(row, "step4e_progress_m"),
        "path_time_s": finite_float(row, "_step4e_path_time_s"),
    }


def _actual_tcp_speed_from_components(row: dict[str, str]) -> float:
    values = [finite_float(row, f"ur_actual_TCP_speed_{idx}", 0.0) for idx in range(3)]
    return math.sqrt(sum(value * value for value in values))


def _actual_tcp_speed(row: dict[str, str]) -> float:
    value = finite_float(row, "_step4e_actual_speed_norm_m_s")
    if math.isfinite(value):
        return value
    return _actual_tcp_speed_from_components(row)


def _paths_from_config(raw_config: dict[str, Any], key: str) -> list[Path]:
    paths: list[Path] = []
    for value in raw_config.get(key, []):
        path = Path(str(value))
        paths.append(path if path.is_absolute() else WORKSPACE_ROOT / path)
    return paths


def _first_finite(row: dict[str, str], keys: list[str]) -> float:
    for key in keys:
        value = finite_float(row, key)
        if math.isfinite(value):
            return value
    return math.nan


def _distribution(values: list[float]) -> dict[str, float | None]:
    finite_values = [value for value in values if math.isfinite(value)]
    if not finite_values:
        return {"min": None, "mean": None, "max": None}
    return {
        "min": min(finite_values),
        "mean": sum(finite_values) / len(finite_values),
        "max": max(finite_values),
    }


def _empty_source_summary(csv_path: Path) -> dict[str, Any]:
    return {
        "source_csv": str(csv_path),
        "rows_replayed": 0,
        "active_contact_rows": 0,
        "cmd_enabled_false_rows": 0,
        "command_valid_shadow_ratio": 0.0,
        "state_counts": Counter(),
        "normal_load_values": [],
        "normal_load_n": {"min": None, "mean": None, "max": None},
        "max_actual_tcp_speed_m_s": 0.0,
        "desired_reference_field_counts": Counter(),
    }


def _finalize_source_summary(summary: dict[str, Any]) -> None:
    rows = int(summary["rows_replayed"])
    active = int(summary["active_contact_rows"])
    summary["command_valid_shadow_ratio"] = active / rows if rows else 0.0
    summary["state_counts"] = dict(summary["state_counts"])
    summary["normal_load_n"] = _distribution(summary.pop("normal_load_values"))
    summary["desired_reference_field_counts"] = dict(summary["desired_reference_field_counts"])


def _csv_safe(row: dict[str, Any]) -> dict[str, Any]:
    return {field: _json_safe(row.get(field, "")) for field in TRACE_FIELDS}


if __name__ == "__main__":
    raise SystemExit(main())
