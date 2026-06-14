#!/usr/bin/env python3
"""Offline sign/frame semantic gate for UR contact controllers."""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from contact_semantics import (
    approach_normal_from_reaction,
    orientation_axis_angle_error,
    semantic_boundary_is_consistent,
)
from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
    rotvec_to_matrix,
)


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPLAY_CSVS = [
    EXPERIMENT_ROOT / "runs" / "bridge_step5d_strict_rnn_liveprep_v4_20260614_234951" / "bridge_rtde_500hz.csv",
    EXPERIMENT_ROOT / "runs" / "bridge_step5d_strict_rnn_liveprep_v2_20260614_231720" / "bridge_rtde_500hz.csv",
]
DEFAULT_TOLERANCE_RAD = math.radians(5.0)
DEFAULT_MAX_OUTER_XDOT_NORM = 1.0


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def row_float(row: dict[str, str], key: str) -> float:
    value = row.get(key, "")
    if value == "":
        raise ValueError(f"missing CSV field {key}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"CSV field {key} is not finite")
    return number


def row_vector(row: dict[str, str], prefix: str, count: int) -> tuple[float, ...]:
    return tuple(row_float(row, f"{prefix}_{idx}") for idx in range(count))


def row_named_vector(row: dict[str, str], prefix: str) -> tuple[float, float, float]:
    return (
        row_float(row, f"{prefix}_x"),
        row_float(row, f"{prefix}_y"),
        row_float(row, f"{prefix}_z"),
    )


def stage25_rows(csv_path: Path, *, max_rows: int | None = None) -> list[dict[str, str]]:
    with csv_path.open(encoding="utf-8") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if row.get("ur_output_double_register_35")
            and abs(float(row["ur_output_double_register_35"]) - 25.0) < 1e-6
        ]
    if max_rows is not None:
        rows = rows[:max_rows]
    if not rows:
        raise ValueError(f"no Stage 25.0 rows found in {csv_path}")
    return rows


def static_scan_step5d_outer_loop(source_path: Path = EXPERIMENT_ROOT / "tools" / "step5d_paper_outer_loop.py") -> dict[str, Any]:
    source = source_path.read_text(encoding="utf-8")
    forbidden_tokens = [
        "rotation_matrix_from_z_axis",
        "u_force_base",
        "force_along_normal_n",
    ]
    hits = [token for token in forbidden_tokens if token in source]
    return {
        "source": str(source_path),
        "forbidden_tokens": forbidden_tokens,
        "hits": hits,
        "pass": not hits,
    }


def replay_csv(
    csv_path: Path,
    *,
    tolerance_rad: float = DEFAULT_TOLERANCE_RAD,
    max_outer_xdot_norm: float = DEFAULT_MAX_OUTER_XDOT_NORM,
    max_rows: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state = Step5dOuterLoopState()
    records: list[dict[str, Any]] = []
    config = Step5dOuterLoopConfig()
    for idx, row in enumerate(stage25_rows(csv_path, max_rows=max_rows)):
        pose = row_vector(row, "ur_actual_TCP_pose", 6)
        speed = row_vector(row, "ur_actual_TCP_speed", 6)
        reaction_normal = row_named_vector(row, "_step4e_control_normal_b")
        approach_normal = approach_normal_from_reaction(reaction_normal)
        R_cur = rotvec_to_matrix(pose[3:6])
        contact_orientation_error = orientation_axis_angle_error(R_cur, approach_normal)
        output = compute_step5d_outer_loop(
            config,
            state,
            Step5dOuterLoopInputs(
                tcp_pose_base=pose,  # type: ignore[arg-type]
                tcp_speed_base=speed,  # type: ignore[arg-type]
                force_tcp_n=row_named_vector(row, "_step4e_force_t"),
                control_reaction_normal_base=reaction_normal,
                x_pd_base=(
                    row_float(row, "_step4e_desired_x_m") if row.get("_step4e_desired_x_m") else float(pose[0]),
                    row_float(row, "_step4e_desired_y_m") if row.get("_step4e_desired_y_m") else float(pose[1]),
                    float(pose[2]),
                ),
                xdot_pd_base=(
                    row_float(row, "_step4e_desired_vx_m_s") if row.get("_step4e_desired_vx_m_s") else 0.0,
                    row_float(row, "_step4e_desired_vy_m_s") if row.get("_step4e_desired_vy_m_s") else 0.0,
                    0.0,
                ),
                dt_s=0.002,
                cmd_valid=True,
            ),
        )
        state = output.next_state
        outer_orientation_error = float(output.diagnostics["outer_orientation_angle_rad"])
        xdot_norm = float(np.linalg.norm(np.asarray(output.xdot_c, dtype=float)))
        consistent = semantic_boundary_is_consistent(
            contact_orientation_error_rad=contact_orientation_error,
            outer_orientation_error_rad=outer_orientation_error,
            tolerance_rad=tolerance_rad,
        )
        record = {
            "row_index": idx,
            "write_index": row.get("write_index", ""),
            "contact_orientation_error_rad": contact_orientation_error,
            "outer_orientation_error_rad": outer_orientation_error,
            "orientation_error_abs_diff_rad": abs(contact_orientation_error - outer_orientation_error),
            "logged_step4e_orientation_error_rad": row.get("step4e_orientation_error_rad", ""),
            "R_d_z_dot_R_cur_z": float(output.diagnostics["R_d_z_dot_R_cur_z"]),
            "normal_load_n": float(output.diagnostics["normal_load_n"]),
            "force_error_n": float(output.diagnostics["e_f"]),
            "outer_xdot_norm": xdot_norm,
            "orientation_consistent": consistent,
            "xdot_norm_ok": xdot_norm <= max_outer_xdot_norm,
        }
        records.append(record)

    max_diff = max(float(record["orientation_error_abs_diff_rad"]) for record in records)
    min_dot = min(float(record["R_d_z_dot_R_cur_z"]) for record in records)
    max_xdot = max(float(record["outer_xdot_norm"]) for record in records)
    summary = {
        "csv": str(csv_path),
        "rows": len(records),
        "max_orientation_error_abs_diff_rad": max_diff,
        "min_R_d_z_dot_R_cur_z": min_dot,
        "max_outer_xdot_norm": max_xdot,
        "pass": (
            all(bool(record["orientation_consistent"]) for record in records)
            and all(bool(record["xdot_norm_ok"]) for record in records)
            and min_dot > 0.0
        ),
    }
    return summary, records


def run_gate(
    csv_paths: list[Path],
    *,
    output_dir: Path | None = None,
    max_rows: int | None = None,
    tolerance_rad: float = DEFAULT_TOLERANCE_RAD,
    max_outer_xdot_norm: float = DEFAULT_MAX_OUTER_XDOT_NORM,
) -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = output_dir or EXPERIMENT_ROOT / "runs" / f"ur_contact_semantic_gate_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    static_scan = static_scan_step5d_outer_loop()
    replay_summaries = []
    for csv_path in csv_paths:
        summary, records = replay_csv(
            csv_path,
            tolerance_rad=tolerance_rad,
            max_outer_xdot_norm=max_outer_xdot_norm,
            max_rows=max_rows,
        )
        replay_summaries.append(summary)
        records_path = out_dir / (csv_path.parent.name + "_semantic_rows.csv")
        with records_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0].keys()))
            writer.writeheader()
            writer.writerows(records)
        summary["records_csv"] = str(records_path)

    overall_pass = bool(static_scan["pass"] and all(summary["pass"] for summary in replay_summaries))
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "overall_pass": overall_pass,
        "static_scan": static_scan,
        "replay_summaries": replay_summaries,
        "contract": str(EXPERIMENT_ROOT / "UR_FORCE_FRAME_CONTRACT.md"),
        "safety_boundary": [
            "offline analysis only",
            "no bridge start",
            "no controller upload",
            "no TP play",
            "no robot motion",
        ],
    }
    write_json(out_dir / "ur_contact_semantic_gate_summary.json", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", dest="csv_paths", type=Path, action="append", default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument("--tolerance-deg", type=float, default=5.0)
    parser.add_argument("--max-outer-xdot-norm", type=float, default=DEFAULT_MAX_OUTER_XDOT_NORM)
    args = parser.parse_args()
    csv_paths = args.csv_paths or DEFAULT_REPLAY_CSVS
    payload = run_gate(
        csv_paths,
        output_dir=args.output_dir,
        max_rows=args.max_rows,
        tolerance_rad=math.radians(float(args.tolerance_deg)),
        max_outer_xdot_norm=float(args.max_outer_xdot_norm),
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
