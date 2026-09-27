#!/usr/bin/env python3
"""Replay D-arm integral feedback with downstream-only saturation feedback.

This is a fixed-observation command replay. It replays the recorded KWR75 force
error sequence and source realization-limit events; it does not predict a
closed-loop force MAE.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r004.calibrated_runtime import (
    OUTER_OUTPUT_SATURATION_TOL_M_S,
)
from step5d_autotune_v4_r013.live_runtime import R013_NORMAL_VELOCITY_LIMIT_M_S
from step5d_paper_outer_loop import conditional_double_clamp_step


DEFAULT_SOURCE = Path("reports/tase_r013_integral_conditional_freeze_semantics_20260927.json")
DEFAULT_OUTPUT = Path("reports/tase_r013_integral_final_feedback_replay_20260927.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected an object")
            rows.append(value)
    if not rows:
        raise ValueError(f"{path}: no command timeline rows")
    return rows


def _initial_state_and_velocity(
    first: Mapping[str, Any], *, p_gain: float, i_gain: float, damping: float
) -> tuple[float, float]:
    dt = float(first["actual_dt_s"])
    error = float(first["force_error_n"])
    state = float(first["integral_state_n_s"])
    if (
        bool(first.get("integral_final_output_frozen"))
        or bool(first.get("integral_velocity_saturated"))
        or bool(first.get("integral_state_clamped"))
        or bool(first.get("integral_authority_clamped"))
    ):
        raise ValueError("first timeline row cannot identify the pre-update integral state")
    initial_state = state - error * dt
    raw_velocity = float(first["integral_raw_normal_velocity_m_s"])
    denominator = 1.0 - damping * dt
    if denominator <= 0.0:
        raise ValueError("first timeline interval is incompatible with the damping recurrence")
    initial_velocity = (
        raw_velocity - dt * (p_gain * error + i_gain * state)
    ) / denominator
    return initial_state, initial_velocity


def _downstream_freeze_sign(row: Mapping[str, Any]) -> int:
    """Return sign only for directional shortfall after task-space limiting."""

    if not bool(row.get("published_feedback_present")):
        return 0
    if not (
        bool(row.get("published_realization_saturated"))
        or bool(row.get("host_slew_limited"))
    ):
        return 0
    try:
        task_normal = float(row["published_task_normal_m_s"])
        realized_normal = float(row["published_realized_normal_m_s"])
        force_error = float(row["force_error_n"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("published realization feedback is incomplete") from exc
    if not all(math.isfinite(value) for value in (task_normal, realized_normal, force_error)):
        raise ValueError("published realization feedback is nonfinite")
    residual = task_normal - realized_normal
    product = force_error * residual
    if abs(residual) <= OUTER_OUTPUT_SATURATION_TOL_M_S or product == 0.0:
        return 0
    return 1 if product > 0.0 else -1


def _step(
    row: Mapping[str, Any],
    *,
    state: float,
    velocity: float,
    feedback_freeze: bool,
    p_gain: float,
    i_gain: float,
    damping: float,
    state_cap: float,
    authority_error: float,
) -> Any:
    return conditional_double_clamp_step(
        force_error_n=float(row["force_error_n"]),
        integral_state_n_s=state,
        normal_velocity_m_s=velocity,
        dt_s=float(row["actual_dt_s"]),
        force_p_gain=p_gain,
        force_i_gain=i_gain,
        force_damping=damping,
        normal_velocity_limit_m_s=R013_NORMAL_VELOCITY_LIMIT_M_S,
        state_limit_n_s=state_cap,
        authority_error_n=authority_error,
        freeze_integral=feedback_freeze,
    )


def _summarize_window(
    rows: Sequence[Mapping[str, Any]],
    candidate_states: Sequence[float],
    candidate_velocities: Sequence[float],
    *,
    start_s: float,
    end_s: float,
) -> dict[str, Any]:
    selected = [
        index
        for index, row in enumerate(rows)
        if start_s <= float(row["reference_time_s"]) < end_s
    ]
    if not selected:
        return {"rows": 0, "candidate_force_mae_computed": False}
    state_delta = [
        candidate_states[index] - float(rows[index]["integral_state_n_s"])
        for index in selected
    ]
    velocity_delta = [
        candidate_velocities[index]
        - float(rows[index]["integral_applied_normal_velocity_m_s"])
        for index in selected
    ]
    abs_state = sorted(abs(value) for value in state_delta)
    abs_velocity = sorted(abs(value) for value in velocity_delta)

    def quantile(values: Sequence[float], fraction: float) -> float:
        return float(values[min(len(values) - 1, int(round((len(values) - 1) * fraction)))])

    return {
        "rows": len(selected),
        "integral_state_delta_abs_mean_n_s": sum(abs(value) for value in state_delta) / len(state_delta),
        "integral_state_delta_abs_p95_n_s": quantile(abs_state, 0.95),
        "integral_state_delta_abs_max_n_s": max(abs_state),
        "normal_velocity_delta_abs_mean_mm_s": 1000.0 * sum(abs(value) for value in velocity_delta) / len(velocity_delta),
        "normal_velocity_delta_abs_p95_mm_s": 1000.0 * quantile(abs_velocity, 0.95),
        "normal_velocity_delta_abs_max_mm_s": 1000.0 * max(abs_velocity),
        "candidate_force_mae_computed": False,
    }


def audit_attempt(
    timeline_path: Path, attempt_result_path: Path, *, source_id: str
) -> dict[str, Any]:
    rows = _read_jsonl(timeline_path)
    attempt_result = json.loads(attempt_result_path.read_text(encoding="utf-8"))
    parameters = attempt_result["applied_runtime_parameters"]
    Md = float(parameters["Md_scalar"])
    Bd = float(parameters["Bd_scalar"])
    state_cap = float(parameters["force_integral_limit_n_s"])
    authority_error = float(parameters["force_integral_authority_error_n"])
    kf = 1.0
    p_gain = 1.0 / Md
    i_gain = kf / Md
    damping = Bd / Md
    effective_limit = min(state_cap, authority_error * p_gain / i_gain)
    initial_state, initial_velocity = _initial_state_and_velocity(
        rows[0], p_gain=p_gain, i_gain=i_gain, damping=damping
    )

    source_state = initial_state
    source_velocity = initial_velocity
    candidate_state = initial_state
    candidate_velocity = initial_velocity
    candidate_freeze_rows = 0
    source_feedback_freeze_rows = 0
    candidate_states: list[float] = []
    candidate_velocities: list[float] = []
    max_source_state_error = 0.0
    max_source_velocity_error = 0.0
    source_mismatch_rows = 0
    prior_candidate_sign = 0

    for row in rows:
        source_freeze = bool(row["integral_final_output_frozen"])
        source_feedback_freeze_rows += int(source_freeze)
        source_out = _step(
            row,
            state=source_state,
            velocity=source_velocity,
            feedback_freeze=source_freeze,
            p_gain=p_gain,
            i_gain=i_gain,
            damping=damping,
            state_cap=state_cap,
            authority_error=authority_error,
        )
        state_error = abs(source_out.integral_state_n_s - float(row["integral_state_n_s"]))
        velocity_error = abs(
            source_out.applied_normal_velocity_m_s
            - float(row["integral_applied_normal_velocity_m_s"])
        )
        max_source_state_error = max(max_source_state_error, state_error)
        max_source_velocity_error = max(max_source_velocity_error, velocity_error)
        if (
            state_error > 1e-10
            or velocity_error > 1e-10
            or source_out.conditional_frozen != bool(row["integral_conditional_frozen"])
        ):
            source_mismatch_rows += 1
        source_state = source_out.integral_state_n_s
        source_velocity = source_out.applied_normal_velocity_m_s

        candidate_feedback_freeze = (
            prior_candidate_sign != 0
            and float(row["force_error_n"]) * prior_candidate_sign > 0.0
        )
        candidate_freeze_rows += int(candidate_feedback_freeze)
        candidate_out = _step(
            row,
            state=candidate_state,
            velocity=candidate_velocity,
            feedback_freeze=bool(candidate_feedback_freeze),
            p_gain=p_gain,
            i_gain=i_gain,
            damping=damping,
            state_cap=state_cap,
            authority_error=authority_error,
        )
        candidate_states.append(candidate_out.integral_state_n_s)
        candidate_velocities.append(candidate_out.applied_normal_velocity_m_s)
        candidate_state = candidate_out.integral_state_n_s
        candidate_velocity = candidate_out.applied_normal_velocity_m_s
        prior_candidate_sign = _downstream_freeze_sign(row)

    official = attempt_result.get("evidence", {})
    timing = official.get("timing_evidence", {})
    return {
        "source_attempt_id": str(attempt_result.get("sequence", source_id)),
        "source_timeline": str(timeline_path),
        "source_timeline_sha256": _sha256(timeline_path),
        "source_attempt_result": str(attempt_result_path),
        "source_attempt_result_sha256": _sha256(attempt_result_path),
        "source_evidence_sha256": official.get("evidence_sha256"),
        "source_path_complete": bool(official.get("metrics", {}).get("complete")),
        "source_formal_bins": official.get("complete_bins"),
        "source_formal_mae_n": official.get("mae_n"),
        "source_tp_rate_hz": timing.get("layer_rates_hz", {}).get("tp_consumed_packet_echoes"),
        "source_force_metric_is_candidate_score": False,
        "timeline_rows": len(rows),
        "source_feedback_replay_mismatch_rows": source_mismatch_rows,
        "source_feedback_replay_max_state_error_n_s": max_source_state_error,
        "source_feedback_replay_max_velocity_error_m_s": max_source_velocity_error,
        "source_feedback_freeze_rows": source_feedback_freeze_rows,
        "candidate_final_realization_only_freeze_rows": candidate_freeze_rows,
        "effective_integral_limit_n_s": effective_limit,
        "windows": {
            "diagnostic_0_60": _summarize_window(
                rows, candidate_states, candidate_velocities, start_s=0.0, end_s=60.0
            ),
            "formal_5_60": _summarize_window(
                rows, candidate_states, candidate_velocities, start_s=5.0, end_s=60.0
            ),
        },
    }


def build_report(source_path: Path) -> dict[str, Any]:
    source = json.loads(source_path.read_text(encoding="utf-8"))
    entries = source["D_conditional_double_clamp"]["attempts"]
    attempts: list[dict[str, Any]] = []
    for entry in entries:
        attempt_dir = Path(entry["attempt_dir"])
        if not attempt_dir.is_absolute():
            attempt_dir = Path.cwd() / attempt_dir
        attempts.append(
            audit_attempt(
                attempt_dir / "command_timeline.jsonl",
                attempt_dir / "attempt-result.json",
                source_id=str(entry["candidate_id"]),
            )
        )
    return {
        "schema": "tase.integral.final-realization-only-fixed-observation-replay.v1",
        "protocol_id": "figure8_window60_r013_rate460_v1",
        "candidate_id": "conditional-double-clamp-final-realization-only-feedback-offline-v1",
        "evidence_class": "sealed_D_arm_fixed_Kunwei_observation_command_replay",
        "source_summary": str(source_path),
        "source_summary_sha256": _sha256(source_path),
        "analysis_script": str(Path(__file__).resolve()),
        "analysis_script_sha256": _sha256(Path(__file__).resolve()),
        "controller_source_hashes": {
            name: {
                "path": str(source_file),
                "sha256": _sha256(source_file),
            }
            for name, source_file in {
                "outer_loop": Path(__file__).resolve().with_name("step5d_paper_outer_loop.py"),
                "calibrated_runtime": Path(__file__).resolve().parent
                / "step5d_autotune_v4_r004"
                / "calibrated_runtime.py",
                "r013_live_runtime": Path(__file__).resolve().parent
                / "step5d_autotune_v4_r013"
                / "live_runtime.py",
            }.items()
        },
        "source_attempts_denominator": source["D_conditional_double_clamp"]["attempts_denominator"],
        "source_full_path_attempts_replayed": len(attempts),
        "source_attempts": attempts,
        "parameters": {
            "Md": 9.565272137974492,
            "Bd": 693.6559295653944,
            "configured_state_limit_n_s": 1.0,
            "authority_error_n": 0.5,
            "effective_integral_limit_n_s": 0.5,
            "normal_velocity_limit_m_s": R013_NORMAL_VELOCITY_LIMIT_M_S,
            "downstream_feedback_tolerance_m_s": OUTER_OUTPUT_SATURATION_TOL_M_S,
        },
        "candidate_feedback_rule": "freeze same-direction integration on the next tick only when published task-space normal velocity differs from final realized Jqdot normal velocity, a final realization or host-slew limit is recorded, and the residual would worsen under the current force-error sign; retain the existing local conditional clamp and allow reverse-error unwind",
        "replay_contract": {
            "source_policy_exactly_reproduced": True,
            "candidate_closed_loop": False,
            "candidate_force_mae_computed": False,
            "candidate_outputs_recomputed_from_fixed_source_force_error": True,
            "realization_events_reused_from_source_trace": True,
            "task_space_limit_remains_handled_by_local_conditional_clamp": True,
            "interpretation": "This replay tests whether the published-output feedback adds a distinct downstream freeze beyond the controller's own normal velocity clamp. It cannot estimate candidate force tracking because candidate output is not fed back into robot or KWR75 observations.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = build_report(args.source)
    for attempt in result["source_attempts"]:
        if attempt["source_feedback_replay_mismatch_rows"]:
            raise SystemExit(
                f"source policy did not reproduce for {attempt['source_attempt_id']}: "
                f"{attempt['source_feedback_replay_mismatch_rows']} rows differ"
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "attempts": len(result["source_attempts"]), "all_source_replays_exact": True}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
