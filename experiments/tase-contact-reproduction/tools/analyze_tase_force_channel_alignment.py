#!/usr/bin/env python3
"""Offline audit of TASE controller force-input versus scored force signal.

This tool reads sealed command timelines and reruns only the stateful outer
force/path loop on the R32 recorded pose, speed, wrench, and dt sequence. It
never connects to or writes to a robot or force sensor. The alternative
filtered-channel replay is open loop and cannot estimate physical force MAE.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any
from datetime import datetime, timezone, timedelta

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ATTEMPTS = (
    "runs/tase-r31-a-baseline-20260925T1510HKT/attempts/0001",
    "runs/tase-r31-a-baseline-repeat-evidenceonly-20260925T1530HKT/attempts/0001",
    "runs/tase-r31-a-baseline-repeat-03-20260925T1730HKT/attempts/0001",
    "runs/tase-r32-rate460-a-v3-capture-evidence-only-20260926T171700HKT/attempts/0001",
)
DEFAULT_PARAMETER_FILE = "config/tase_figure8_integral_1p0_rate460.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"timeline is not a regular file: {path}")
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSON at {path}:{line_number}") from error
            if not isinstance(row, dict):
                raise ValueError(f"timeline row is not an object at {path}:{line_number}")
            time_s = row.get("reference_time_s")
            if isinstance(time_s, (int, float)) and math.isfinite(time_s) and 0 <= time_s < 60:
                rows.append(row)
    return rows


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _signal_metrics(values: list[float], target_n: float = 5.0) -> dict[str, float]:
    errors = [target_n - value for value in values]
    return {
        "mean_n": statistics.fmean(values),
        "signed_target_minus_signal_mean_n": statistics.fmean(errors),
        "sample_mae_n": statistics.fmean(abs(value) for value in errors),
        "sample_rmse_n": math.sqrt(statistics.fmean(value * value for value in errors)),
    }


def _summarize_attempt(attempt_dir: Path, label: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    timeline = attempt_dir / "command_timeline.jsonl"
    attempt_result = attempt_dir / "attempt-result.json"
    if attempt_result.is_symlink() or not attempt_result.is_file():
        raise ValueError(f"attempt result is not a regular file: {attempt_result}")
    result = json.loads(attempt_result.read_text(encoding="utf-8"))
    evidence = result.get("evidence", {})
    if evidence.get("complete_bins") != 550 or result.get("evidence_eligible") is not True:
        raise ValueError(f"{label} is not a complete eligible 60 s A attempt")
    rows = _read_rows(timeline)
    rows = [row for row in rows if 5.0 <= float(row["reference_time_s"]) < 60.0]
    if not rows:
        raise ValueError(f"{label} has no formal `[5,60)` telemetry rows")

    filtered = [float(row["normal_force_filtered_n"]) for row in rows]
    measured = [float(row["normal_force_measured_n"]) for row in rows]
    control = [float(row["normal_force_control_n"]) for row in rows]
    target = [float(row["force_target_n"]) for row in rows]
    logged_error = [float(row["force_error_n"]) for row in rows]
    if any(not math.isclose(c, max(f, m), rel_tol=0.0, abs_tol=1e-8)
           for c, f, m in zip(control, filtered, measured)):
        raise ValueError(f"{label} does not match the one-sided force-envelope identity")
    identity_residual = [e - (t - c) for e, t, c in zip(logged_error, target, control)]
    if max(abs(value) for value in identity_residual) > 1e-7:
        raise ValueError(f"{label} logged outer error does not match target minus control input")

    uplift = [c - f for c, f in zip(control, filtered)]
    clipped = [
        row for row in rows
        if abs(float(row["published_requested_normal_m_s"])
               - float(row["published_task_normal_m_s"])) > 1e-8
    ]
    unclipped = [
        row for row in rows
        if abs(float(row["published_requested_normal_m_s"])
               - float(row["published_task_normal_m_s"])) <= 1e-8
    ]
    task_projection_error = []
    flag_mismatches = []
    normal_axis_sources = set()
    for row in rows:
        inputs = row.get("sfc_replay_inputs", {})
        task_twist = np.asarray(row.get("requested_outer_twist_m_s_rad_s"), dtype=float)
        if task_twist.shape != (6,) or not np.all(np.isfinite(task_twist)):
            raise ValueError(f"{label} is missing the returned task-twist binding")
        recorded_axis = inputs.get("control_reaction_normal_base") if isinstance(inputs, dict) else None
        if recorded_axis is None:
            # R31's v2 trace predates sfc_replay_inputs. Its bound TASE profile
            # used the fixed base-Z reaction normal; R32 records that same axis.
            axis = np.asarray((0.0, 0.0, 1.0), dtype=float)
            normal_axis_sources.add("fixed-base-Z profile (v2 trace; cross-checked against R32 v3)")
        else:
            axis = np.asarray(recorded_axis, dtype=float)
            normal_axis_sources.add("recorded sfc_replay_inputs.control_reaction_normal_base")
        if axis.shape != (3,) or not np.all(np.isfinite(axis)):
            raise ValueError(f"{label} has an invalid normal axis")
        norm = float(np.linalg.norm(axis))
        if norm <= 1e-12:
            raise ValueError(f"{label} has a degenerate control reaction-normal axis")
        approach = -axis / norm
        projected_task_normal = float(task_twist[:3] @ approach)
        saved_task_normal = float(row["published_task_normal_m_s"])
        task_projection_error.append(projected_task_normal - saved_task_normal)
        raw_gap = abs(float(row["published_requested_normal_m_s"]) - saved_task_normal) > 1e-8
        flag_mismatches.append(bool(row.get("published_task_space_saturated")) != raw_gap)
    task_projection_error_max = max(abs(value) for value in task_projection_error)
    if task_projection_error_max > 2e-6:
        raise ValueError(f"{label} saved task twist is not already bound to the task-normal output")
    if any(flag_mismatches):
        raise ValueError(f"{label} published saturation flag differs from raw-request/task-output gap")

    def clipped_summary(group: list[dict[str, Any]]) -> dict[str, Any]:
        group_filtered = [float(row["normal_force_filtered_n"]) for row in group]
        return {
            "rows": len(group),
            "pct_of_formal_rows": 100.0 * len(group) / len(rows),
            "filtered_force_mean_n": statistics.fmean(group_filtered),
            "filtered_sample_mae_n": statistics.fmean(abs(5.0 - value) for value in group_filtered),
        }

    realization_residual = [
        float(row["published_task_normal_m_s"])
        - float(row["published_realized_normal_m_s"])
        for row in rows
    ]
    integral_limit = [float(row["integral_effective_limit_n_s"]) for row in rows]
    at_limit = [
        abs(float(row["integral_state_n_s"])) >= max(0.0, limit - 1e-6)
        for row, limit in zip(rows, integral_limit)
    ]
    summary = {
        "id": label,
        "attempt_dir": attempt_dir.as_posix(),
        "timeline_path": timeline.as_posix(),
        "timeline_sha256": _sha256(timeline),
        "attempt_result_path": attempt_result.as_posix(),
        "attempt_result_sha256": _sha256(attempt_result),
        "formal_binned_mae_n": float(evidence["mae_n"]),
        "formal_bins": int(evidence["complete_bins"]),
        "actual_qd_correlation": float(evidence["qd_correlation"]),
        "formal_rows": len(rows),
        "scored_filtered_channel": _signal_metrics(filtered),
        "unfiltered_measured_normal_channel": _signal_metrics(measured),
        "controller_envelope_channel": _signal_metrics(control),
        "logged_force_error": {
            "mean_n": statistics.fmean(logged_error),
            "sample_mae_n": statistics.fmean(abs(value) for value in logged_error),
            "max_abs_identity_residual_n": max(abs(value) for value in identity_residual),
        },
        "one_sided_envelope_uplift": {
            "mean_n": statistics.fmean(uplift),
            "p50_n": statistics.median(uplift),
            "p95_n": _quantile(uplift, 0.95),
            "max_n": max(uplift),
            "measured_normal_above_filter_pct": 100.0 * sum(value > 1e-9 for value in uplift) / len(rows),
        },
        "task_normal_command_clipping": {
            "rows": len(clipped),
            "pct": 100.0 * len(clipped) / len(rows),
            "meaning": "pre-cap published_requested_normal_m_s differs from published_task_normal_m_s; requested_outer_twist_m_s_rad_s is already the returned task-space-capped vector",
            "clipped_filtered_signal": clipped_summary(clipped),
            "unclipped_filtered_signal": clipped_summary(unclipped),
        },
        "task_limit_field_semantics": {
            "task_vector_source": "requested_outer_twist_m_s_rad_s is the task-space-capped return value from calibrated_runtime.desired_twist",
            "normal_axis_sources": sorted(normal_axis_sources),
            "task_vector_normal_projection_max_abs_error_m_s": task_projection_error_max,
            "published_task_space_saturated_matches_raw_request_gap_all_rows": True,
            "extra_post_return_normal_reclip_observed": False,
        },
        "published_jacobian_realization": {
            "task_minus_final_jqdot_normal_p50_abs_m_s": statistics.median(abs(value) for value in realization_residual),
            "task_minus_final_jqdot_normal_p95_abs_m_s": _quantile([abs(value) for value in realization_residual], 0.95),
            "task_minus_final_jqdot_normal_max_abs_m_s": max(abs(value) for value in realization_residual),
            "mean_signed_task_minus_final_jqdot_normal_m_s": statistics.fmean(realization_residual),
        },
        "integrator": {
            "configured_limit_n_s": 1.0,
            "state_at_effective_limit_pct": 100.0 * sum(at_limit) / len(rows),
            "interpretation": "state occupancy at the configured cap; not by itself proof of windup",
        },
    }
    return summary, rows


def _shadow_replay_r32(rows: list[dict[str, Any]], parameter_file: Path) -> dict[str, Any]:
    """Replay the outer force/path loop on fixed R32 feedback; do not close loop."""

    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    from tase_contact_provider import TaseContactProvider, current_model_binding, load_tase_outer_config
    from tase_figure8_protocol import RATE460_PROTOCOL_ID
    from step5d_autotune_v4.contracts import V4Candidate
    from step5d_autotune_v4_r014.solver_profile import LEGACY_R1
    from contact_yield_live_writer import native_motion_profile
    from contact_yield_live_contract import load_identity_contract
    from step5d_paper_outer_loop import Step5dOuterLoopState

    config, binding = load_tase_outer_config(parameter_file)
    providers: dict[str, Any] = {}
    kwargs = {
        "contract": current_model_binding(),
        "candidate": V4Candidate(),
        "motion_profile": native_motion_profile(),
        "home_pose": load_identity_contract().home_pose,
        "solver_profile": LEGACY_R1,
        "outer_loop_config": config,
        "parameter_binding": binding,
        "protocol_id": RATE460_PROTOCOL_ID,
    }
    try:
        for label in ("logged_control_envelope", "scoring_filtered_channel"):
            provider = TaseContactProvider(**kwargs)
            provider.reference_phase = "path"
            first = rows[0]
            provider.runtime._outer_state = Step5dOuterLoopState(
                force_integral_n_s=float(first["integral_state_n_s"]),
                xdot_p_prev_m_s=tuple(float(value) for value in first["requested_outer_twist_m_s_rad_s"][:3]),
            )
            providers[label] = provider

        deltas = []
        paired_z_commands = []
        reproduction_abs = [[], [], []]
        for row in rows[1:]:
            inputs = row["sfc_replay_inputs"]
            common = {
                "actual_tcp_pose": inputs["actual_tcp_pose_m_rad"],
                "actual_tcp_speed": inputs["actual_tcp_speed_m_s_rad_s"],
                "force_tcp_n": inputs["control_wrench_tcp_n_nm"][:3],
                "internal_setpoint_n": float(row["force_target_n"]),
                "actual_dt_s": float(row["actual_dt_s"]),
                "mode": "path",
                "path_time_s": float(row["reference_time_s"]),
            }
            current = providers["logged_control_envelope"].runtime.desired_twist(
                **common, filtered_normal_n=float(row["normal_force_control_n"])
            )
            filtered = providers["scoring_filtered_channel"].runtime.desired_twist(
                **common, filtered_normal_n=float(row["normal_force_filtered_n"])
            )
            logged = row["requested_outer_twist_m_s_rad_s"]
            for axis in range(3):
                reproduction_abs[axis].append(abs(float(current[axis]) - float(logged[axis])))
            delta = [float(filtered[i]) - float(current[i]) for i in range(3)]
            deltas.append(delta)
            paired_z_commands.append((float(current[2]), float(filtered[2])))

        def summarize(vectors: list[list[float]]) -> dict[str, Any]:
            z = [v[2] for v in vectors]
            abs_z = [abs(value) for value in z]
            return {
                "rows": len(vectors),
                "base_z_command_delta_mean_m_s": statistics.fmean(z),
                "base_z_command_delta_p50_abs_m_s": statistics.median(abs_z),
                "base_z_command_delta_p90_abs_m_s": _quantile(abs_z, 0.90),
                "base_z_command_delta_p95_abs_m_s": _quantile(abs_z, 0.95),
                "base_z_command_delta_max_abs_m_s": max(abs_z),
                "rows_abs_delta_gt_0p1mm_s_pct": 100.0 * sum(value > 1e-4 for value in abs_z) / len(z),
                "opposite_sign_base_z_command_pct": 100.0 * sum(a[2] * b[2] < 0.0 for a, b in zip(vectors, vectors)) / len(z),
            }

        formal_summary = summarize(deltas)
        formal_summary["opposite_sign_base_z_command_pct"] = 100.0 * sum(
            current * filtered < 0.0 for current, filtered in paired_z_commands
        ) / len(paired_z_commands)
        reproduction_max = [max(axis_values) for axis_values in reproduction_abs]
        if max(reproduction_max) > 1e-6:
            raise RuntimeError(
                "current-signal outer-loop replay does not reproduce recorded XYZ request: "
                f"{reproduction_max} m/s"
            )
        return {
            "schema": "tase-r32-force-channel-fixed-input-shadow-replay-v1",
            "window_s": [5.0, 60.0],
            "parameter_file": parameter_file.as_posix(),
            "parameter_file_sha256": _sha256(parameter_file),
            "input_rows_formal_window": len(rows),
            "current_signal_reproduction_xyz_max_abs_error_m_s": reproduction_max,
            "filtered_counterfactual_vs_current": {
                "formal_5_60_s": formal_summary,
            },
            "limitations": [
                "R32 recorded pose, speed, wrench, and dt are held fixed. Candidate twist is not fed back into subsequent observations.",
                "Only the outer loop is replayed. No robot motion, force MAE, safety response, or stable closed-loop trajectory is predicted.",
                "The baseline XYZ reproduction check excludes orientation components because live TASE intentionally substitutes a fixed approved-Home orientation policy.",
            ],
        }
    finally:
        for provider in providers.values():
            provider.close()


def analyze(attempt_dirs: list[Path], parameter_file: Path, output: Path) -> dict[str, Any]:
    labels = ["R31-A1", "R31-A2", "R31-A3", "R32-A1"]
    if len(attempt_dirs) != 4:
        raise ValueError("exactly four validated R31/R32 A attempt directories are required")
    attempts, shadow_rows = [], None
    for label, attempt_dir in zip(labels, attempt_dirs):
        summary, rows = _summarize_attempt(attempt_dir, label)
        attempts.append(summary)
        if label == "R32-A1":
            shadow_rows = rows
    result: dict[str, Any] = {
        "schema": "tase.r31-r32-force-channel-alignment-diagnostic-v1",
        "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "analysis_window_s": [5.0, 60.0],
        "target_force_n": 5.0,
        "analysis_method": {
            "score_sample_signal": "normal_force_filtered_n",
            "controller_signal": "normal_force_control_n = max(normal_force_filtered_n, normal_force_measured_n)",
            "logged_controller_error": "force_error_n = force_target_n - normal_force_control_n",
            "task_normal_clip_flag": "published_task_space_saturated matches abs(pre-cap published_requested_normal_m_s - task-capped published_task_normal_m_s) > 1e-8 m/s; the saved requested_outer_twist vector is already task-space capped",
            "final_jqdot_task_residual": "published_task_normal_m_s - published_realized_normal_m_s",
            "limitation": "Per-command diagnostics use Kunwei signals and are not independent task-force truth; conditional splits are associations, not causality.",
        },
        "runtime_source_references": [
            "tools/tase_contact_provider.py:1411-1424",
            "tools/tase_contact_provider.py:1774-1781",
            "tools/step5d_autotune_v4_r004/calibrated_runtime.py:918-970",
            "tools/step5d_autotune_v4_r004/calibrated_runtime.py:448-486",
        ],
        "runtime_source_sha256": {
            "tase_contact_provider.py": _sha256(ROOT / "tools/tase_contact_provider.py"),
            "calibrated_runtime.py": _sha256(ROOT / "tools/step5d_autotune_v4_r004/calibrated_runtime.py"),
            "step5d_paper_outer_loop.py": _sha256(ROOT / "tools/step5d_paper_outer_loop.py"),
            "analyzer": _sha256(Path(__file__).resolve()),
        },
        "attempts": attempts,
    }
    if shadow_rows is None:
        raise RuntimeError("R32 A source trace was not found")
    result["r32_fixed_input_shadow_replay"] = _shadow_replay_r32(shadow_rows, parameter_file)
    uplift = [a["one_sided_envelope_uplift"]["mean_n"] for a in attempts]
    control_bias = [a["controller_envelope_channel"]["signed_target_minus_signal_mean_n"] for a in attempts]
    score_bias = [a["scored_filtered_channel"]["signed_target_minus_signal_mean_n"] for a in attempts]
    result["cross_run_summary"] = {
        "complete_eligible_A_units": len(attempts),
        "mean_controller_minus_scored_filter_n": {
            "min": min(uplift), "max": max(uplift), "mean": statistics.fmean(uplift),
        },
        "controller_signal_signed_error_n": {
            "min": min(control_bias), "max": max(control_bias), "mean": statistics.fmean(control_bias),
        },
        "scored_filter_signed_error_n": {
            "min": min(score_bias), "max": max(score_bias), "mean": statistics.fmean(score_bias),
        },
        "normal_task_clipping_pct": {
            "min": min(a["task_normal_command_clipping"]["pct"] for a in attempts),
            "max": max(a["task_normal_command_clipping"]["pct"] for a in attempts),
            "mean": statistics.fmean(a["task_normal_command_clipping"]["pct"] for a in attempts),
        },
        "measured_normal_above_filter_pct": {
            "min": min(a["one_sided_envelope_uplift"]["measured_normal_above_filter_pct"] for a in attempts),
            "max": max(a["one_sided_envelope_uplift"]["measured_normal_above_filter_pct"] for a in attempts),
            "mean": statistics.fmean(a["one_sided_envelope_uplift"]["measured_normal_above_filter_pct"] for a in attempts),
        },
        "final_Jqdot_normal_residual_p95_abs_m_s": {
            "min": min(a["published_jacobian_realization"]["task_minus_final_jqdot_normal_p95_abs_m_s"] for a in attempts),
            "max": max(a["published_jacobian_realization"]["task_minus_final_jqdot_normal_p95_abs_m_s"] for a in attempts),
            "mean": statistics.fmean(a["published_jacobian_realization"]["task_minus_final_jqdot_normal_p95_abs_m_s"] for a in attempts),
        },
        "integral_at_1p0_n_s_limit_pct": {
            "min": min(a["integrator"]["state_at_effective_limit_pct"] for a in attempts),
            "max": max(a["integrator"]["state_at_effective_limit_pct"] for a in attempts),
            "mean": statistics.fmean(a["integrator"]["state_at_effective_limit_pct"] for a in attempts),
        },
    }
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", action="append", type=Path, help="exact sealed attempt directory; pass four in R31-A1/A2/A3, R32-A1 order")
    parser.add_argument("--parameter-file", type=Path, default=ROOT / DEFAULT_PARAMETER_FILE)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/tase_r31_r32_force_channel_alignment_20260926.json")
    args = parser.parse_args()
    attempts = [ROOT / value if not value.is_absolute() else value for value in (args.attempt_dir or [Path(p) for p in DEFAULT_ATTEMPTS])]
    parameter_file = ROOT / args.parameter_file if not args.parameter_file.is_absolute() else args.parameter_file
    output = ROOT / args.output if not args.output.is_absolute() else args.output
    result = analyze(attempts, parameter_file, output)
    print(json.dumps({"output": str(output), "cross_run_summary": result["cross_run_summary"], "r32_shadow": result["r32_fixed_input_shadow_replay"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
