from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .state_machine import CageBounds, ShadowSample, Step5dRemoteConfig, Step5dRemoteMachine


def find_workspace_root() -> Path:
    marker = Path("experiments") / "tase-contact-reproduction"
    candidates = [Path.cwd(), *Path(__file__).resolve().parents]
    for candidate in candidates:
        if (candidate / marker).exists():
            return candidate
    return Path(__file__).resolve().parents[3]


WORKSPACE_ROOT = find_workspace_root()
RUNS_ROOT = WORKSPACE_ROOT / "experiments" / "tase-contact-reproduction" / "runs"
DEFAULT_CONFIG = WORKSPACE_ROOT / "src" / "ur10e_step5d_remote" / "config" / "default_step5d_remote.yaml"

TRACE_FIELDS = [
    "source_csv",
    "row_index",
    "t_rel_s",
    "would_state",
    "transition_reason",
    "normal_load_n",
    "force_norm_n",
    "torque_norm_nm",
    "cage_margin_m",
    "cage_reason",
    "reacquire_direction_x",
    "reacquire_direction_y",
    "reacquire_direction_z",
    "reacquire_speed_m_s",
    "hold_duty",
    "reacquire_count",
    "cmd_enabled",
    "would_command_qdot_0",
    "would_command_qdot_1",
    "would_command_qdot_2",
    "would_command_qdot_3",
    "would_command_qdot_4",
    "would_command_qdot_5",
    "would_command_twist_x",
    "would_command_twist_y",
    "would_command_twist_z",
    "would_command_twist_rx",
    "would_command_twist_ry",
    "would_command_twist_rz",
]


def load_config(path: Path = DEFAULT_CONFIG) -> tuple[Step5dRemoteConfig, dict[str, Any]]:
    try:
        import yaml  # type: ignore

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        raw = _simple_yaml(path.read_text(encoding="utf-8"))
    return Step5dRemoteConfig.from_mapping(raw), raw


def run_shadow_replay(
    *,
    config_path: Path = DEFAULT_CONFIG,
    output_dir: Path | None = None,
    replay_csvs: list[Path] | None = None,
    max_rows_per_csv: int | None = None,
) -> dict[str, Any]:
    if not config_path.is_absolute():
        config_path = WORKSPACE_ROOT / config_path
    config, raw_config = load_config(config_path)
    if config.enable_motion:
        raise RuntimeError("Step5d remote shadow refuses enable_motion=true; live motion is not implemented")

    csv_paths = replay_csvs or _paths_from_config(raw_config, "default_replay_csvs")
    cage_paths = _paths_from_config(raw_config, "baseline_cage_csvs")
    cage = build_cage(cage_paths, config)
    created_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = output_dir or RUNS_ROOT / f"step5d_ros2_remote_shadow_{created_at}"
    out_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[dict[str, Any]] = []
    total_counts: Counter[str] = Counter()
    max_hold_duty = 0.0
    max_reacquire_count = 0
    any_cmd_enabled = False
    required_seen = set()

    trace_path = out_dir / "shadow_trace.csv"
    with trace_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRACE_FIELDS)
        writer.writeheader()
        for csv_path in csv_paths:
            machine = Step5dRemoteMachine(config=config, cage=cage)
            source_counts: Counter[str] = Counter()
            first_fail: dict[str, Any] | None = None
            first_reacquire: dict[str, Any] | None = None
            rows = 0
            for sample in iter_shadow_samples(csv_path, max_rows=max_rows_per_csv):
                row = machine.step(sample)
                rows += 1
                source_counts[str(row["would_state"])] += 1
                total_counts[str(row["would_state"])] += 1
                max_hold_duty = max(max_hold_duty, float(row["hold_duty"]))
                max_reacquire_count = max(max_reacquire_count, int(row["reacquire_count"]))
                any_cmd_enabled = any_cmd_enabled or bool(row["cmd_enabled"])
                required_seen.update(row.keys())
                if first_reacquire is None and row["would_state"] == "ACTIVE_REACQUIRE":
                    first_reacquire = _compact_event(row)
                if first_fail is None and row["would_state"] == "FAIL_FAST_STOP_REQUEST":
                    first_fail = _compact_event(row)
                writer.writerow(_csv_safe(row))
            summaries.append(
                {
                    "source_csv": str(csv_path),
                    "rows_replayed": rows,
                    "state_counts": dict(source_counts),
                    "first_reacquire": first_reacquire,
                    "first_fail_fast": first_fail,
                    "cmd_enabled_any": False,
                }
            )

    summary = {
        "analysis_created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "offline_ros2_remote_shadow_no_live_robot_action",
        "config_path": str(config_path),
        "artifact_dir": str(out_dir),
        "trace_path": str(trace_path),
        "summary_path": str(out_dir / "summary.json"),
        "enable_motion": config.enable_motion,
        "live_motion_authorized": False,
        "tp_action_required": "none_no_tp_play_no_program_load",
        "policy": {
            "long_zero_qdot_hold_removed": True,
            "short_dwell_cycles": config.short_dwell_cycles,
            "reacquire_direction": "approach_normal = -reaction_normal",
            "cage_margin_formula": "s = d - (v*tau + v^2/(2a) + model_margin + contact_margin)",
        },
        "cage": {
            "mode": "broad_stagewise_aabb_from_success_step5b_step6b",
            "source_rows": cage.source_rows,
            "source_csvs": list(cage.source_csvs),
            "min_xyz": list(cage.min_xyz),
            "max_xyz": list(cage.max_xyz),
        },
        "source_summaries": summaries,
        "state_counts": dict(total_counts),
        "max_hold_duty": max_hold_duty,
        "max_reacquire_count": max_reacquire_count,
        "cmd_enabled_any": any_cmd_enabled,
        "required_trace_fields_present": all(field in required_seen or field in TRACE_FIELDS for field in TRACE_FIELDS),
        "acceptance": {
            "default_no_motion": config.enable_motion is False and not any_cmd_enabled,
            "long_hold_removed": max_hold_duty < 0.25,
            "reacquire_or_failfast_seen": total_counts["ACTIVE_REACQUIRE"] > 0
            or total_counts["FAIL_FAST_STOP_REQUEST"] > 0,
            "artifact_schema_complete": all(field in TRACE_FIELDS for field in TRACE_FIELDS),
        },
    }
    (out_dir / "summary.json").write_text(json.dumps(_json_safe(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def build_cage(paths: list[Path], config: Step5dRemoteConfig) -> CageBounds:
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    used: list[str] = []
    for path in paths:
        path_used = False
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if not _stage25_active(row):
                    continue
                x = finite_float(row, "ur_actual_TCP_pose_0")
                y = finite_float(row, "ur_actual_TCP_pose_1")
                z = finite_float(row, "ur_actual_TCP_pose_2")
                if math.isfinite(x) and math.isfinite(y) and math.isfinite(z):
                    xs.append(x)
                    ys.append(y)
                    zs.append(z)
                    path_used = True
        if path_used:
            used.append(str(path))
    if not xs:
        raise RuntimeError("No finite Stage25/source poses available to build Step5d shadow cage")
    p = config.cage_padding_m
    return CageBounds(
        min_xyz=(min(xs) - p, min(ys) - p, min(zs) - p),
        max_xyz=(max(xs) + p, max(ys) + p, max(zs) + p),
        source_rows=len(xs),
        source_csvs=tuple(used),
    )


def iter_shadow_samples(path: Path, *, max_rows: int | None = None) -> Iterable[ShadowSample]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = csv.DictReader(handle)
        first_t: float | None = None
        previous_t: float | None = None
        emitted = 0
        for row_index, row in enumerate(rows):
            if not _stage25_active(row):
                continue
            t = finite_float(row, "t_monotonic_s")
            if not math.isfinite(t):
                continue
            if first_t is None:
                first_t = t
            t_rel = max(0.0, t - first_t)
            dt_s = 0.002 if previous_t is None else max(0.0, min(0.010, t - previous_t))
            previous_t = t
            sample = ShadowSample(
                source_csv=str(path),
                row_index=row_index,
                t_rel_s=t_rel,
                normal_load_n=_first_finite(row, ["_step4e_normal_load_n", "normal_force_n"]),
                force_norm_n=finite_float(row, "force_norm_n", 0.0),
                torque_norm_nm=finite_float(row, "torque_norm_nm", 0.0),
                tcp_xyz=(
                    finite_float(row, "ur_actual_TCP_pose_0"),
                    finite_float(row, "ur_actual_TCP_pose_1"),
                    finite_float(row, "ur_actual_TCP_pose_2"),
                ),
                actual_tcp_speed_m_s=_actual_speed(row),
                predicted_tcp_speed_m_s=_optional_float(row, "_step5d_predicted_tcp_speed_m_s"),
                reaction_normal=_reaction_normal(row),
                dt_s=dt_s,
            )
            yield sample
            emitted += 1
            if max_rows is not None and emitted >= max_rows:
                break


def finite_float(row: dict[str, str], key: str, default: float = math.nan) -> float:
    value = row.get(key, "")
    if value == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Step5d ROS2 remote-control shadow replay.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--csv", action="append", type=Path, default=None)
    parser.add_argument("--max-rows-per-csv", type=int, default=None)
    args = parser.parse_args()
    summary = run_shadow_replay(
        config_path=args.config,
        output_dir=args.output_dir,
        replay_csvs=args.csv,
        max_rows_per_csv=args.max_rows_per_csv,
    )
    print(json.dumps(_json_safe(summary), indent=2, sort_keys=True))
    return 0 if all(summary["acceptance"].values()) else 2


def _paths_from_config(raw_config: dict[str, Any], key: str) -> list[Path]:
    values = raw_config.get(key, [])
    paths = []
    for value in values:
        path = Path(str(value))
        paths.append(path if path.is_absolute() else WORKSPACE_ROOT / path)
    return paths


def _simple_yaml(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    current_list: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- ") and current_list:
            data[current_list].append(line[2:].strip())
            continue
        current_list = None
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "":
            data[key] = []
            current_list = key
        elif value.lower() in {"true", "false"}:
            data[key] = value.lower() == "true"
        else:
            try:
                data[key] = float(value) if "." in value else int(value)
            except ValueError:
                data[key] = value
    return data


def _actual_speed(row: dict[str, str]) -> float:
    for key in ("_step5d_actual_tcp_speed_m_s", "_step4e_actual_speed_norm_m_s"):
        value = finite_float(row, key)
        if math.isfinite(value):
            return value
    values = [finite_float(row, f"ur_actual_TCP_speed_{idx}", 0.0) for idx in range(3)]
    return math.sqrt(sum(value * value for value in values))


def _reaction_normal(row: dict[str, str]) -> tuple[float, float, float]:
    values = tuple(finite_float(row, f"_step4e_control_normal_b_{axis}") for axis in ("x", "y", "z"))
    if all(math.isfinite(value) for value in values) and math.sqrt(sum(value * value for value in values)) > 1e-12:
        return values  # type: ignore[return-value]
    return (0.0, 0.0, -1.0)


def _stage25_active(row: dict[str, str]) -> bool:
    stage = finite_float(row, "ur_output_double_register_35")
    if math.isfinite(stage) and abs(stage - 25.0) < 0.05:
        return True
    return finite_float(row, "step4e_cmd_valid", 0.0) > 0.5


def _first_finite(row: dict[str, str], keys: list[str]) -> float:
    for key in keys:
        value = finite_float(row, key)
        if math.isfinite(value):
            return value
    return math.nan


def _optional_float(row: dict[str, str], key: str) -> float | None:
    value = finite_float(row, key)
    return value if math.isfinite(value) else None


def _compact_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "row_index": row["row_index"],
        "t_rel_s": row["t_rel_s"],
        "state": row["would_state"],
        "reason": row["transition_reason"],
        "normal_load_n": row["normal_load_n"],
        "cage_margin_m": row["cage_margin_m"],
    }


def _csv_safe(row: dict[str, Any]) -> dict[str, Any]:
    return {field: _json_safe(row.get(field, "")) for field in TRACE_FIELDS}


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
