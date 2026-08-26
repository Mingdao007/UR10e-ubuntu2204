#!/usr/bin/env python3
"""Compare two Kunwei gravity-fit runs at a common measurement plane.

The unloaded run supplies the axis mapping.  The loaded-minus-unloaded wrench
difference is then fitted to a gravity model so constant sensor bias and the
Kunwei body mass are not silently compared with a whole-assembly scale weight.
This tool never writes UR payload, CoG, TCP, or Kunwei configuration.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from analyze_custom_payload_cog import apply_mapping, rmse, solve_linear_model, torque_design


def force_design(gravity: np.ndarray) -> np.ndarray:
    rows: list[list[float]] = []
    for gx, gy, gz in gravity:
        rows.extend(
            [
                [gx, 1.0, 0.0, 0.0],
                [gy, 0.0, 1.0, 0.0],
                [gz, 0.0, 0.0, 1.0],
            ]
        )
    return np.asarray(rows, dtype=float)


def load_analysis(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("status") != "ok":
        raise ValueError(f"analysis is not usable: {path} status={data.get('status')}")
    if data.get("axis_mapping", {}).get("status") != "ok":
        raise ValueError(f"axis mapping is not usable: {path}")
    return data


def compare_runs(
    unloaded: dict[str, Any],
    loaded: dict[str, Any],
    *,
    known_added_mass_kg: float | None,
    mass_tolerance_kg: float,
) -> dict[str, Any]:
    u_segments = {item["name"]: item for item in unloaded.get("segments", [])}
    l_segments = {item["name"]: item for item in loaded.get("segments", [])}
    names = [name for name in unloaded.get("segment_names", []) if name in l_segments]
    if len(names) < 4:
        raise ValueError(f"need at least four paired segments, got {len(names)}")

    u_gravity = {name: np.asarray(unloaded["gravity_tool_mps2"][idx], dtype=float) for idx, name in enumerate(unloaded["segment_names"]) if idx < len(unloaded.get("gravity_tool_mps2", []))}
    l_gravity = {name: np.asarray(loaded["gravity_tool_mps2"][idx], dtype=float) for idx, name in enumerate(loaded["segment_names"]) if idx < len(loaded.get("gravity_tool_mps2", []))}
    gravity = np.asarray([u_gravity[name] for name in names], dtype=float)
    pose_gravity_delta = np.asarray([np.linalg.norm(l_gravity[name] - u_gravity[name]) for name in names], dtype=float)

    mapping = unloaded["axis_mapping"]["best"]
    loaded_mapping = loaded["axis_mapping"]["best"]
    mapping_match = (
        mapping.get("permutation") == loaded_mapping.get("permutation")
        and mapping.get("signs") == loaded_mapping.get("signs")
    )
    u_force = np.asarray([u_segments[name]["force_n"] for name in names], dtype=float)
    l_force = np.asarray([l_segments[name]["force_n"] for name in names], dtype=float)
    u_moment = np.asarray([u_segments[name]["moment_nm"] for name in names], dtype=float)
    l_moment = np.asarray([l_segments[name]["moment_nm"] for name in names], dtype=float)
    delta_force = apply_mapping(l_force, mapping) - apply_mapping(u_force, mapping)
    delta_moment = apply_mapping(l_moment, mapping) - apply_mapping(u_moment, mapping)

    f_design = force_design(gravity)
    f_params, f_rmse, f_rank, f_cond = solve_linear_model(f_design, delta_force.reshape(-1))
    t_design = torque_design(gravity)
    t_params, t_rmse, t_rank, t_cond = solve_linear_model(t_design, delta_moment.reshape(-1))
    f_pred = (f_design @ f_params).reshape((-1, 3))
    t_pred = (t_design @ t_params).reshape((-1, 3))
    added_mass = float(f_params[0])
    expected_error = None if known_added_mass_kg is None else added_mass - known_added_mass_kg
    if known_added_mass_kg is None:
        status = "pending_reference_mass"
    elif abs(expected_error) <= mass_tolerance_kg:
        status = "agree"
    else:
        status = "disagree"

    return {
        "status": status,
        "created": datetime.now().isoformat(timespec="seconds"),
        "unloaded_analysis": unloaded.get("input_csv"),
        "loaded_analysis": loaded.get("input_csv"),
        "segment_names": names,
        "segment_count": len(names),
        "mapping": {
            "unloaded": mapping,
            "loaded": loaded_mapping,
            "match": mapping_match,
        },
        "gravity_pose_delta_max_mps2": float(pose_gravity_delta.max()),
        "gravity_pose_delta_mean_mps2": float(pose_gravity_delta.mean()),
        "delta_force_n": delta_force.tolist(),
        "delta_moment_nm": delta_moment.tolist(),
        "mass_fit": {
            "added_mass_kg": added_mass,
            "force_bias_n": f_params[1:4].tolist(),
            "force_rmse_n": f_rmse,
            "force_rank": f_rank,
            "force_condition_number": f_cond,
            "moment_bias_nm": t_params[3:6].tolist(),
            "mass_times_cog_delta_kg_m": t_params[:3].tolist(),
            "moment_rmse_nm": t_rmse,
            "moment_rank": t_rank,
            "moment_condition_number": t_cond,
            "force_residuals_n": (delta_force - f_pred).tolist(),
            "moment_residuals_nm": (delta_moment - t_pred).tolist(),
        },
        "reference": {
            "known_added_mass_kg": known_added_mass_kg,
            "error_kg": expected_error,
            "tolerance_kg": mass_tolerance_kg,
        },
        "notes": [
            "The comparison is at the Kunwei measurement plane, not the whole-assembly scale plane.",
            "No UR payload/CoG/TCP or Kunwei configuration write is performed.",
        ],
    }


def write_report(path: Path, result: dict[str, Any]) -> None:
    fit = result["mass_fit"]
    ref = result["reference"]
    lines = [
        "# Kunwei loaded/unloaded differential validation",
        "",
        f"- Status: `{result['status']}`",
        f"- Paired segments: `{result['segment_count']}`",
        f"- Mapping match: `{result['mapping']['match']}`",
        f"- Max gravity-pose mismatch: `{result['gravity_pose_delta_max_mps2']}` m/s²",
        "",
        "## Differential mass fit",
        "",
        "| Quantity | Value |",
        "|---|---:|",
        f"| Added mass fit (kg) | {fit['added_mass_kg']} |",
        f"| Known added mass (kg) | {ref['known_added_mass_kg']} |",
        f"| Error (kg) | {ref['error_kg']} |",
        f"| Tolerance (kg) | {ref['tolerance_kg']} |",
        f"| Force RMSE (N) | {fit['force_rmse_n']} |",
        f"| Moment RMSE (Nm) | {fit['moment_rmse_nm']} |",
        f"| Force rank / condition | {fit['force_rank']} / {fit['force_condition_number']} |",
        f"| Moment rank / condition | {fit['moment_rank']} / {fit['moment_condition_number']} |",
        "",
        "## Interpretation",
        "",
        "该结果只验证 unloaded→loaded 的质量增量；它不把 Kunwei sensor body 或上游 flange 的总重误当成 Kunwei 下游 payload。",
        "在 reference mass 缺失时，状态保持 `pending_reference_mass`，不执行 promotion 或控制器写入。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unloaded_analysis", type=Path)
    parser.add_argument("loaded_analysis", type=Path)
    parser.add_argument("--known-added-mass-kg", type=float, default=None)
    parser.add_argument("--mass-tolerance-kg", type=float, default=0.02)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()

    unloaded = load_analysis(args.unloaded_analysis)
    loaded = load_analysis(args.loaded_analysis)
    result = compare_runs(
        unloaded,
        loaded,
        known_added_mass_kg=args.known_added_mass_kg,
        mass_tolerance_kg=args.mass_tolerance_kg,
    )
    output_dir = args.output_dir or (args.loaded_analysis.parent / "payload_delta")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_json = output_dir / "payload_delta_analysis.json"
    output_report = output_dir / "payload_delta_report.md"
    output_json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_report(output_report, result)
    print(json.dumps({"status": result["status"], "json": str(output_json), "report": str(output_report)}, ensure_ascii=False))
    return 0 if result["status"] in {"agree", "pending_reference_mass"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
