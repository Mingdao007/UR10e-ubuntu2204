#!/usr/bin/env python3
"""Replay alternative KWR75 normal feedback through the TASE outer loop.

This is fixed-observation, task-space command analysis. It validates the
existing fused-signal replay against sealed commands, then changes only the
normal feedback value from max(filtered, measured) to filtered-only. It does
not solve qdot, feed commands back into observations, or estimate force MAE.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from analyze_tase_resident_evidence import reduce_attempt
from contact_yield_live_writer import R006Candidate, _select_tase_solver_profile, native_motion_profile
from step5d_paper_outer_loop import Step5dOuterLoopState
from tase_contact_provider import (
    R013_RATE460_PROTOCOL_ID,
    TaseContactProvider,
    current_model_binding,
    load_tase_outer_config,
)


SCHEMA = "tase.force-feedback-selection-outer-loop-replay-v1"
FORMAL_START_S = 5.0
PATH_END_S = 60.0
BIN_WIDTH_S = 0.1
TARGET_FORCE_N = 5.0


class ReplayError(RuntimeError):
    """A sealed A attempt does not support the declared command replay."""


def _finite_array(value: Any, shape: tuple[int, ...], name: str) -> np.ndarray:
    result = np.asarray(value, dtype=float)
    if result.shape != shape or not np.isfinite(result).all():
        raise ReplayError(f"{name} must be a finite array of shape {shape}")
    return result


def _read_source(attempt_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    attempt_dir = Path(attempt_dir).resolve(strict=True)
    reduction = reduce_attempt(attempt_dir, verify_seal=True)
    if (
        reduction.get("protocol_id") != R013_RATE460_PROTOCOL_ID
        or reduction.get("path_complete") is not True
        or reduction.get("home_verified") is not True
        or reduction.get("evidence_eligible") is not True
        or reduction.get("formal_bins") != 550
    ):
        raise ReplayError("source must be a sealed, eligible rate460 A PATH with Home and 550 bins")

    receipt = json.loads((attempt_dir / "attempt-result.json").read_text(encoding="utf-8"))
    binding = receipt.get("parameter_binding")
    if not isinstance(binding, Mapping) or binding.get("candidate_id", "").split("-")[-1] != "A":
        raise ReplayError("source attempt is not an A baseline")
    parameter_file = Path(str(binding.get("source", ""))).resolve(strict=True)
    config, loaded_binding = load_tase_outer_config(parameter_file)
    if loaded_binding.get("protocol_id") != R013_RATE460_PROTOCOL_ID:
        raise ReplayError("A parameter file is not bound to the rate460 protocol")
    if loaded_binding.get("force_integral_policy") != "legacy-clamp-v1":
        raise ReplayError("this replay requires the historical A integral policy")
    if not math.isclose(float(loaded_binding["Md_scalar"]), float(binding["Md_scalar"]), abs_tol=1e-12):
        raise ReplayError("receipt and applied Md differ")
    if not math.isclose(float(loaded_binding["Bd_scalar"]), float(binding["Bd_scalar"]), abs_tol=1e-12):
        raise ReplayError("receipt and applied Bd differ")

    timeline = attempt_dir / "command_timeline.jsonl"
    rows: list[dict[str, Any]] = []
    with timeline.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("reference_phase") != "path":
                continue
            if "actual_TCP_force" in row:
                raise ReplayError("source command row contains UR internal force; refusing mixed-source data")
            inputs = row.get("sfc_replay_inputs")
            provenance = row.get("sfc_replay_input_provenance")
            if not isinstance(inputs, Mapping) or not isinstance(provenance, Mapping):
                raise ReplayError("PATH row lacks synchronized force/pose inputs and provenance")
            if (
                provenance.get("control_wrench_tcp_n_nm") != "kunwei_baseline_corrected"
                or provenance.get("native_wrench_tcp_n_nm") != "kunwei_native"
            ):
                raise ReplayError("PATH row force provenance is not Kunwei KWR75")
            rows.append(row)
    if not rows:
        raise ReplayError("source command timeline has no PATH rows")
    return {"reduction": reduction, "receipt": receipt, "binding": binding,
            "parameter_file": parameter_file, "config": config}, rows


def _new_provider(root: Path, parameter_file: Path) -> TaseContactProvider:
    home = json.loads((root / "config/figure8_home_v1.json").read_text(encoding="utf-8"))
    pose = home.get("pose_m_rad")
    if not isinstance(pose, list) or len(pose) != 6:
        raise ReplayError("approved Figure-eight Home pose is invalid")
    outer_config, binding = load_tase_outer_config(parameter_file)
    return TaseContactProvider(
        contract=current_model_binding(),
        candidate=R006Candidate(),
        motion_profile=native_motion_profile(),
        home_pose=pose,
        solver_profile=_select_tase_solver_profile(
            family="tase_sfc_tangent_augment", library="/tmp/not-used-by-rnn.so",
        ),
        outer_loop_config=outer_config,
        parameter_binding=binding,
        protocol_id=R013_RATE460_PROTOCOL_ID,
    )


def _run_outer_loop(
    provider: TaseContactProvider,
    rows: Sequence[Mapping[str, Any]],
    *,
    signal_field: str,
    initial_integral_n_s: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    runtime = provider.runtime
    runtime.reset_outer_loop_state()
    runtime._outer_state = Step5dOuterLoopState(
        force_integral_n_s=initial_integral_n_s,
        # PATH-start velocity is not in the compact trace. The fused command
        # replay must match throughout the formal window before this seeded
        # boundary can be used to interpret the counterfactual.
        xdot_p_prev_m_s=(0.0, 0.0, 0.0),
    )
    provider.reference_phase = "path"
    twists = np.empty((len(rows), 6), dtype=float)
    integrals = np.empty(len(rows), dtype=float)
    for index, row in enumerate(rows):
        inputs = row["sfc_replay_inputs"]
        pose = _finite_array(inputs["actual_tcp_pose_m_rad"], (6,), "TCP pose")
        speed = _finite_array(inputs["actual_tcp_speed_m_s_rad_s"], (6,), "TCP speed")
        wrench = _finite_array(inputs["control_wrench_tcp_n_nm"], (6,), "KWR75 corrected wrench")
        dt_s = float(row["actual_dt_s"])
        time_s = float(row["reference_time_s"])
        target = float(row["force_target_n"])
        normal_value = float(row[signal_field])
        if not all(math.isfinite(value) for value in (dt_s, time_s, target, normal_value)):
            raise ReplayError("source PATH row has a nonfinite time or force value")
        twist = runtime.desired_twist(
            actual_tcp_pose=pose,
            actual_tcp_speed=speed,
            force_tcp_n=wrench[:3],
            filtered_normal_n=normal_value,
            internal_setpoint_n=target,
            actual_dt_s=dt_s,
            mode="path",
            path_time_s=time_s,
        )
        _, orientation_error = runtime.path_errors(
            actual_tcp_pose=pose,
            path_time_s=time_s,
            motion_kp=runtime.candidate.motion_kp,
        )
        twists[index] = (*twist[:3], *(
            runtime.outer_loop_config.ko
            * runtime.outer_loop_config.orientation_gain_scale
            * value
            for value in orientation_error
        ))
        integrals[index] = float(runtime._outer_state.force_integral_n_s)
    times = np.asarray([float(row["reference_time_s"]) for row in rows], dtype=float)
    return times, twists, integrals


def _filter_recurrence_summary(
    rows: Sequence[Mapping[str, Any]], *, tau_s: float,
) -> dict[str, Any]:
    if not rows or not math.isfinite(tau_s) or tau_s <= 0.0:
        raise ReplayError("filtered-force recurrence inputs are invalid")
    previous = float(rows[0]["normal_force_filtered_n"])
    residuals: list[float] = []
    for row in rows[1:]:
        dt_s = float(row["actual_dt_s"])
        measured = float(row["normal_force_measured_n"])
        filtered = float(row["normal_force_filtered_n"])
        if not all(math.isfinite(value) for value in (dt_s, measured, filtered)) or dt_s <= 0.0:
            raise ReplayError("filtered-force recurrence row contains invalid data")
        alpha = -math.expm1(-dt_s / tau_s)
        predicted = previous + alpha * (measured - previous)
        residuals.append(filtered - predicted)
        previous = filtered
    absolute = np.abs(np.asarray(residuals, dtype=float))
    max_abs = float(np.max(absolute)) if absolute.size else 0.0
    return {
        "filter_tau_s": float(tau_s),
        "updates_compared": len(residuals),
        "mean_abs_recurrence_residual_n": float(np.mean(absolute)) if absolute.size else 0.0,
        "p95_abs_recurrence_residual_n": float(np.quantile(absolute, 0.95)) if absolute.size else 0.0,
        "max_abs_recurrence_residual_n": max_abs,
        "matches_one_pole_actual_dt_recurrence": max_abs <= 1e-12,
    }


def _window_summary(
    times: Sequence[float],
    baseline_twists: np.ndarray,
    candidate_twists: np.ndarray,
    baseline_integrals: np.ndarray,
    candidate_integrals: np.ndarray,
    *,
    start_s: float,
    end_s: float,
) -> dict[str, Any]:
    stamps = np.asarray(times, dtype=float)
    mask = (stamps >= start_s) & (stamps < end_s)
    if not np.any(mask):
        raise ReplayError(f"no samples in [{start_s},{end_s})")
    baseline = baseline_twists[mask]
    candidate = candidate_twists[mask]
    delta = candidate - baseline
    normal_delta = delta[:, 2]
    integral_delta = candidate_integrals[mask] - baseline_integrals[mask]
    bin_total = int(round((end_s - start_s) / BIN_WIDTH_S))
    indices = np.floor((stamps[mask] - start_s) / BIN_WIDTH_S + 1e-9).astype(int)
    bin_means = np.asarray([
        float(np.mean(normal_delta[indices == index]))
        for index in range(bin_total)
        if np.any(indices == index)
    ])
    abs_normal = np.abs(normal_delta)
    abs_integral = np.abs(integral_delta)
    return {
        "window_s": [start_s, end_s],
        "sample_count": int(np.count_nonzero(mask)),
        "required_100ms_bins": bin_total,
        "nonempty_100ms_bins": int(bin_means.size),
        "complete_100ms_bin_coverage": bool(bin_means.size == bin_total),
        "normal_base_z_command_delta_m_s": {
            "mean_candidate_minus_fused": float(np.mean(normal_delta)),
            "mean_absolute": float(np.mean(abs_normal)),
            "p95_absolute": float(np.quantile(abs_normal, 0.95)),
            "max_absolute": float(np.max(abs_normal)),
            "fraction_more_negative": float(np.mean(normal_delta < -1e-12)),
            "fraction_sign_reversal": float(np.mean(
                (baseline[:, 2] * candidate[:, 2] < 0.0)
                & (np.abs(baseline[:, 2]) > 1e-8)
                & (np.abs(candidate[:, 2]) > 1e-8)
            )),
            "100ms_bin_mean_absolute": float(np.mean(np.abs(bin_means))) if bin_means.size else None,
            "100ms_bin_p95_absolute": float(np.quantile(np.abs(bin_means), 0.95)) if bin_means.size else None,
        },
        "tangential_xy_command_delta_m_s": {
            "mean_vector_norm": float(np.mean(np.linalg.norm(delta[:, :2], axis=1))),
            "max_vector_norm": float(np.max(np.linalg.norm(delta[:, :2], axis=1))),
        },
        "integral_state_delta_n_s": {
            "mean_candidate_minus_fused": float(np.mean(integral_delta)),
            "mean_absolute": float(np.mean(abs_integral)),
            "p95_absolute": float(np.quantile(abs_integral, 0.95)),
            "max_absolute": float(np.max(abs_integral)),
        },
        "not_force_metric": True,
    }


def replay_attempt(attempt_dir: Path, *, root: Path) -> dict[str, Any]:
    attempt_dir = Path(attempt_dir).resolve(strict=True)
    source, rows = _read_source(attempt_dir)
    provider = _new_provider(root, source["parameter_file"])
    config = source["config"]
    binding = source["binding"]
    first = rows[0]
    # The source trace stores the post-step unclamped state and exact fused
    # force error. Recover the common PATH-origin integral before changing
    # the signal; the missing velocity state is validated by fused replay.
    initial_integral = (
        float(first["integral_raw_state_n_s"])
        - float(first["force_error_n"]) * float(first["actual_dt_s"])
    )
    baseline_times, baseline_twists, baseline_integrals = _run_outer_loop(
        provider, rows,
        signal_field="normal_force_control_n",
        initial_integral_n_s=initial_integral,
    )
    source_twists = np.asarray([
        _finite_array(row["requested_outer_twist_m_s_rad_s"], (6,), "recorded outer twist")
        for row in rows
    ])
    formal = (baseline_times >= FORMAL_START_S) & (baseline_times < PATH_END_S)
    if int(np.count_nonzero(formal)) == 0:
        raise ReplayError("formal window has no source commands")
    fused_replay_error = np.max(np.abs(baseline_twists[formal] - source_twists[formal]), axis=1)
    if float(np.max(fused_replay_error)) > 1e-9:
        raise ReplayError(
            "fused outer-loop replay does not reproduce sealed formal twists: "
            f"max abs error {float(np.max(fused_replay_error)):.6g} m/s or rad/s"
        )
    candidate_times, candidate_twists, candidate_integrals = _run_outer_loop(
        provider, rows,
        signal_field="normal_force_filtered_n",
        initial_integral_n_s=initial_integral,
    )
    if not np.array_equal(candidate_times, baseline_times):
        raise ReplayError("counterfactual time base differs from the fused replay")

    expected_full_bins = {
        int(math.floor(t / BIN_WIDTH_S + 1e-9))
        for t in baseline_times if 0.0 <= t < PATH_END_S
    }
    expected_formal_bins = {
        int(math.floor((t - FORMAL_START_S) / BIN_WIDTH_S + 1e-9))
        for t in baseline_times if FORMAL_START_S <= t < PATH_END_S
    }
    if len(expected_full_bins) != 600 or len(expected_formal_bins) != 550:
        raise ReplayError("source command rows do not cover all declared 100 ms PATH bins")

    source_guard_reference_counts = {
        "force_rise_guard_rows": sum(bool(row.get("force_rise_guard")) for row in rows),
        "force_preempt_warm_start_rows": sum(bool(row.get("force_preempt_warm_start")) for row in rows),
        "force_preempt_direction_retry_rows": sum(bool(row.get("force_preempt_direction_retry")) for row in rows),
    }
    return {
        "attempt_id": attempt_dir.name,
        "attempt_dir": str(attempt_dir),
        "candidate_id": binding.get("candidate_id"),
        "parameter_file": str(source["parameter_file"]),
        "Md_scalar": float(binding["Md_scalar"]),
        "Bd_scalar": float(binding["Bd_scalar"]),
        "integral_policy": str(binding["force_integral_policy"]),
        "integral_limit_n_s": float(config.force_integral_limit_n_s),
        "filtered_signal_recurrence": _filter_recurrence_summary(
            rows, tau_s=float(provider.lifecycle_observer.tau_s),
        ),
        "sealed_source_formal_force_mae_n": source["reduction"].get("formal_bin_mean_mae_n"),
        "sealed_source_force_mae_is_not_counterfactual": True,
        "sealed_source_path_rows": len(rows),
        "sealed_source_full_bins": len(expected_full_bins),
        "sealed_source_formal_bins": len(expected_formal_bins),
        "fused_replay_formal_max_abs_twist_error": float(np.max(fused_replay_error)),
        "source_guard_reference_counts_only": source_guard_reference_counts,
        "candidate_guard_decisions_recomputed": False,
        "windows": {
            "diagnostic_0_60": _window_summary(
                baseline_times, baseline_twists, candidate_twists,
                baseline_integrals, candidate_integrals,
                start_s=0.0, end_s=PATH_END_S,
            ),
            "formal_5_60": _window_summary(
                baseline_times, baseline_twists, candidate_twists,
                baseline_integrals, candidate_integrals,
                start_s=FORMAL_START_S, end_s=PATH_END_S,
            ),
        },
    }


def run_replay(attempt_dirs: Sequence[Path], *, root: Path, output: Path) -> dict[str, Any]:
    attempts = [replay_attempt(path, root=root) for path in attempt_dirs]
    document = {
        "schema": SCHEMA,
        "evidence_class": "sealed_Kunwei_fixed_observation_task_space_outer_loop_command_replay",
        "protocol_id": R013_RATE460_PROTOCOL_ID,
        "control_signal_change": "max(filtered_normal_n, measured_normal_n) -> filtered_normal_n",
        "unchanged_inputs_and_guard_definitions": [
            "baseline-corrected Kunwei wrench vector",
            "native/raw Kunwei wrench and full-force measurements from the sealed source",
            "existing force-rise and force-preempt thresholds; source trigger counts are reference-only",
            "actual RTDE TCP pose/speed and the fixed recorded reference/dt",
            "Md, Bd, target force, integral policy and integral bound",
        ],
        "filter_recurrence_semantics": (
            "Check the sealed KWR75 filtered channel against the provider's declared one-pole tau and each row's actual dt; it is a consistency check, not a force-truth validation."
        ),
        "closed_loop": False,
        "qdot_solver_replayed": False,
        "joint_or_task_space_limits_recomputed": False,
        "candidate_guard_decisions_recomputed": False,
        "force_mae_recomputed": False,
        "physical_candidate_executed": False,
        "independent_force_truth": False,
        "interpretation_limit": (
            "Fused replay reproduces the sealed formal outer-loop twist before the filtered-only comparison. "
            "The counterfactual changes only the KWR75 normal value supplied to the outer loop. It does not "
            "recompute RNN qdot, feed candidate commands into robot observations, or predict force MAE. "
            "Raw/full-force inputs and guard thresholds are unchanged, but candidate guard decisions are not "
            "recomputed; in particular, qdot-dependent direction retry is not evaluated. No live suitability follows."
        ),
        "attempts": attempts,
    }
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    result = run_replay(args.attempt_dir, root=args.root.resolve(strict=True), output=args.output)
    print(json.dumps({
        "output": str(args.output.resolve()),
        "attempt_count": len(result["attempts"]),
        "closed_loop": result["closed_loop"],
        "qdot_solver_replayed": result["qdot_solver_replayed"],
        "force_mae_recomputed": result["force_mae_recomputed"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
