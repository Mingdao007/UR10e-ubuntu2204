#!/usr/bin/env python3
"""Join force, task-limit, RNN Jqdot, and RTDE speed evidence for sealed R32 A.

This is a fixed-trace diagnostic. It does not replay a candidate controller,
alter commands, connect to a device, or estimate independent contact-force
truth.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ATTEMPT = ROOT / (
    "runs/tase-r32-rate460-a-v3-capture-evidence-only-20260926T171700HKT/"
    "attempts/0001"
)
DEFAULT_JSON = ROOT / "reports/tase_r32_signal_to_motion_chain_20260926.json"
DEFAULT_MD = ROOT / "reports/tase_r32_signal_to_motion_chain_20260926.md"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _finite(values: list[float], name: str) -> np.ndarray:
    result = np.asarray(values, dtype=float)
    if result.ndim != 1 or not np.all(np.isfinite(result)):
        raise ValueError(f"{name} must be a finite vector")
    return result


def _quantile(values: np.ndarray, q: float) -> float:
    if values.size == 0:
        raise ValueError("cannot summarize an empty sample")
    return float(np.quantile(values, q))


def _summary(values: np.ndarray, scale: float = 1.0) -> dict[str, float]:
    v = values * scale
    return {
        "mean": float(np.mean(v)),
        "sd": float(np.std(v)),
        "p05": _quantile(v, 0.05),
        "p50": _quantile(v, 0.50),
        "p95": _quantile(v, 0.95),
        "max_abs": float(np.max(np.abs(v))),
    }


def _corr(a: np.ndarray, b: np.ndarray) -> float | None:
    if a.size < 3 or np.std(a) == 0.0 or np.std(b) == 0.0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _lag_scan(command: np.ndarray, measured: np.ndarray, host_s: np.ndarray,
              published_s: np.ndarray, max_lag: int = 30) -> dict[str, Any]:
    """Positive lag means a measured speed sample occurs after its command."""
    n = min(command.size, measured.size, host_s.size, published_s.size)
    if n < max_lag + 3:
        raise ValueError("not enough samples for the requested lag scan")
    candidates = []
    for lag in range(max_lag + 1):
        x = command[:n - lag] if lag else command[:n]
        y = measured[lag:n]
        candidates.append((lag, _corr(x, y)))
    valid = [(lag, corr) for lag, corr in candidates if corr is not None]
    best_lag, best_corr = max(valid, key=lambda item: item[1])
    offsets = host_s[best_lag:n] - published_s[:n - best_lag]
    return {
        "definition": "Pearson correlation(command[i], RTDE_actual_TCP_speed[i+lag]); positive lag is post-publish",
        "tested_lags_samples": [0, max_lag],
        "lag_sample_interval_median_ms": float(np.median(np.diff(host_s)) * 1000.0),
        "best_positive_correlation_lag_samples": int(best_lag),
        "best_lag_median_publish_to_RTDE_sample_ms": float(np.median(offsets) * 1000.0),
        "best_lag_correlation": float(best_corr),
        "same_row_correlation": _corr(command, measured),
        "correlations_by_lag": [
            {"lag_samples": int(lag), "correlation": corr}
            for lag, corr in candidates
        ],
        "interpretation_limit": "descriptive alignment in a periodic feedback trace; not a causal plant-delay estimate",
    }


def _load_rows(attempt: Path) -> tuple[list[dict[str, Any]], Path, Path]:
    timeline = attempt / "command_timeline.jsonl"
    result_path = attempt / "attempt-result.json"
    for path in (timeline, result_path):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"expected a regular sealed input file: {path}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    evidence = result.get("evidence", {})
    if result.get("evidence_eligible") is not True or evidence.get("complete_bins") != 550:
        raise ValueError("input is not a complete eligible 60 s A attempt")
    rows = []
    with timeline.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            row = json.loads(line)
            t = row.get("reference_time_s")
            if isinstance(t, (float, int)) and math.isfinite(t) and 5.0 <= t < 60.0:
                rows.append(row)
    if len(rows) < 1000:
        raise ValueError("formal timeline unexpectedly short")
    return rows, timeline, result_path


def analyze(attempt: Path) -> dict[str, Any]:
    rows, timeline, result_path = _load_rows(attempt)
    n = len(rows)
    filtered = _finite([r["normal_force_filtered_n"] for r in rows], "filtered force")
    measured = _finite([r["normal_force_measured_n"] for r in rows], "measured force")
    control = _finite([r["normal_force_control_n"] for r in rows], "controller force")
    target = _finite([r["force_target_n"] for r in rows], "target force")
    requested = _finite([r["published_requested_normal_m_s"] for r in rows], "requested normal velocity")
    task = _finite([r["published_task_normal_m_s"] for r in rows], "task normal velocity")
    realized = _finite([r["published_realized_normal_m_s"] for r in rows], "realized normal velocity")
    host_time = _finite([r["host_monotonic_s"] for r in rows], "host monotonic timestamps")
    published_time = _finite([r["published_monotonic_s"] for r in rows], "publish timestamps")
    dt = _finite([r["actual_dt_s"] for r in rows], "actual dt")
    if np.any(np.diff(host_time) <= 0.0) or np.any(dt <= 0.0):
        raise ValueError("formal sample clock is not strictly increasing")
    if not np.allclose(target, 5.0, rtol=0.0, atol=1e-9):
        raise ValueError("trace does not retain the 5 N target for every formal row")
    identity_checked = bool(np.allclose(
        control, np.maximum(filtered, measured), rtol=0.0, atol=1e-8
    ))
    if not identity_checked:
        raise ValueError("controller-force identity max(filtered, measured) failed")

    task_clipped = np.array([
        bool(r["published_task_space_saturated"])
        or abs(float(r["published_requested_normal_m_s"])
               - float(r["published_task_normal_m_s"])) > 1e-8
        for r in rows
    ])
    host_slew = np.array([bool(r["host_slew_limited"]) for r in rows])
    explicit_realization = np.array([
        bool(r["published_explicit_realization_limit"]) for r in rows
    ])
    reported_realization_sat = np.array([
        bool(r["published_realization_saturated"]) for r in rows
    ])

    approach_axes, rtde_normal, jactual_normal, jcmd_normal = [], [], [], []
    command_projection_error, actual_projection_error, bound_active = [], [], []
    for row in rows:
        inp = row["sfc_replay_inputs"]
        reaction = np.asarray(inp["control_reaction_normal_base"], dtype=float)
        if reaction.shape != (3,) or not np.all(np.isfinite(reaction)):
            raise ValueError("trace contains invalid fixed reaction-normal axis")
        reaction_norm = float(np.linalg.norm(reaction))
        if reaction_norm <= 1e-12:
            raise ValueError("trace contains a degenerate reaction-normal axis")
        approach = -reaction / reaction_norm
        approach_axes.append(approach)
        tcp_speed = np.asarray(inp["actual_tcp_speed_m_s_rad_s"][:3], dtype=float)
        jac = np.asarray(row["jacobian_6x6"], dtype=float)
        qdot_actual = np.asarray(row["actual_qd_rad_s"], dtype=float)
        qdot_command = np.asarray(row["published_packet_qdot_rad_s"], dtype=float)
        lower = np.asarray(row["solver_qdot_lower_rad_s"], dtype=float)
        upper = np.asarray(row["solver_qdot_upper_rad_s"], dtype=float)
        if (jac.shape != (6, 6) or qdot_actual.shape != (6,) or
                qdot_command.shape != (6,) or lower.shape != (6,) or upper.shape != (6,)):
            raise ValueError("trace has a malformed Jacobian or joint-vector field")
        if not all(np.all(np.isfinite(x)) for x in (tcp_speed, jac, qdot_actual, qdot_command, lower, upper)):
            raise ValueError("trace has a nonfinite kinematic value")
        cmd_twist = jac @ qdot_command
        actual_twist = jac @ qdot_actual
        rtde_normal.append(float(tcp_speed @ approach))
        jactual_normal.append(float(actual_twist[:3] @ approach))
        jcmd_normal.append(float(cmd_twist[:3] @ approach))
        command_projection_error.append(float(cmd_twist[:3] @ approach - row["published_realized_normal_m_s"]))
        actual_projection_error.append(float(actual_twist[:3] @ approach - tcp_speed @ approach))
        bound_active.append(bool(np.any((qdot_command <= lower + 1e-8) | (qdot_command >= upper - 1e-8))))

    axes = np.asarray(approach_axes)
    if float(np.max(np.linalg.norm(axes - axes[0], axis=1))) > 1e-8:
        raise ValueError("normal axis changes within the trace; fixed-axis reduction is invalid")
    rtde_normal = np.asarray(rtde_normal)
    jactual_normal = np.asarray(jactual_normal)
    jcmd_normal = np.asarray(jcmd_normal)
    command_projection_error = np.asarray(command_projection_error)
    actual_projection_error = np.asarray(actual_projection_error)
    bound_active = np.asarray(bound_active)
    if float(np.max(np.abs(command_projection_error))) > 2e-6:
        raise ValueError("recorded Jqdot projection does not match published normal channel")
    if float(np.max(np.abs(actual_projection_error))) > 2e-5:
        raise ValueError("RTDE Cartesian speed disagrees with calibrated J*actual_qd beyond tolerance")

    force_error_score = target - filtered
    force_error_control = target - control
    task_shortfall = requested - task
    rnn_realization_residual = task - realized
    total_command_shortfall = requested - realized
    force_uplift = control - filtered

    def group_summary(mask: np.ndarray) -> dict[str, Any]:
        if not np.any(mask):
            return {"rows": 0}
        return {
            "rows": int(np.count_nonzero(mask)),
            "pct_of_formal_rows": 100.0 * float(np.mean(mask)),
            "mean_scored_force_n": float(np.mean(filtered[mask])),
            "signed_score_error_target_minus_filter_n": float(np.mean(force_error_score[mask])),
            "sample_force_mae_n": float(np.mean(np.abs(force_error_score[mask]))),
            "mean_controller_force_error_n": float(np.mean(force_error_control[mask])),
            "requested_normal_mean_mm_s": float(np.mean(requested[mask]) * 1000.0),
            "task_after_limit_mean_mm_s": float(np.mean(task[mask]) * 1000.0),
            "final_jqdot_normal_mean_mm_s": float(np.mean(realized[mask]) * 1000.0),
            "task_to_jqdot_residual_p95_abs_mm_s": _quantile(np.abs(rnn_realization_residual[mask]), 0.95) * 1000.0,
        }

    at_task_limit = task_clipped
    no_explicit_limit = ~(host_slew | explicit_realization | bound_active)
    lag = _lag_scan(jcmd_normal, rtde_normal, host_time, published_time)
    return {
        "schema": "tase.r32-signal-to-motion-chain-v1",
        "evidence_class": "sealed_complete_live_A_trace_joined_open_loop_diagnostic",
        "attempt": str(attempt),
        "protocol": "r013_60_rate460",
        "window_s": [5.0, 60.0],
        "rows": n,
        "target_force_n": 5.0,
        "sources": {
            "command_timeline": str(timeline),
            "command_timeline_sha256": _sha256(timeline),
            "attempt_result": str(result_path),
            "attempt_result_sha256": _sha256(result_path),
        },
        "force_input_stage": {
            "identity_checked": identity_checked,
            "controller_minus_scored_filter_mean_n": float(np.mean(force_uplift)),
            "measured_normal_above_filter_pct": 100.0 * float(np.mean(measured > filtered)),
            "score_channel_signed_error_mean_n": float(np.mean(force_error_score)),
            "controller_channel_signed_error_mean_n": float(np.mean(force_error_control)),
            "claim_limit": "all forces are from the same Kunwei feedback path; no independent task-force truth",
        },
        "normal_command_chain": {
            "approach_axis_base": axes[0].tolist(),
            "task_space_limit_flag_pct": 100.0 * float(np.mean(task_clipped)),
            "requested_minus_task_limit": _summary(task_shortfall, 1000.0),
            "task_minus_published_Jqdot": _summary(rnn_realization_residual, 1000.0),
            "requested_minus_published_Jqdot": _summary(total_command_shortfall, 1000.0),
            "task_and_published_Jqdot_same_sign_pct": 100.0 * float(np.mean(task * realized > 0.0)),
            "joint_solver_bound_active_pct": 100.0 * float(np.mean(bound_active)),
            "host_slew_limited_pct": 100.0 * float(np.mean(host_slew)),
            "explicit_realization_limit_pct": 100.0 * float(np.mean(explicit_realization)),
            "published_realization_saturated_flag_pct": 100.0 * float(np.mean(reported_realization_sat)),
            "no_explicit_bound_or_slew_subset": {
                "rows": int(np.count_nonzero(no_explicit_limit)),
                "task_to_Jqdot_residual_p95_abs_mm_s": (
                    _quantile(np.abs(rnn_realization_residual[no_explicit_limit]), 0.95) * 1000.0
                    if np.any(no_explicit_limit) else None
                ),
                "task_to_Jqdot_residual_mean_mm_s": (
                    float(np.mean(rnn_realization_residual[no_explicit_limit]) * 1000.0)
                    if np.any(no_explicit_limit) else None
                ),
            },
            "clipped_vs_unclipped": {
                "clipped": group_summary(at_task_limit),
                "unclipped": group_summary(~at_task_limit),
            },
            "claim_limit": "clipped rows are selected by large error/request; their worse force scores are association, not an isolated causal effect",
        },
        "robot_response_stage": {
            "RTDE_actual_TCP_speed_and_J_actual_qd_normal_max_abs_difference_mm_s": float(np.max(np.abs(actual_projection_error)) * 1000.0),
            "J_published_qdot_matches_recorded_final_normal_max_abs_difference_mm_s": float(np.max(np.abs(command_projection_error)) * 1000.0),
            "published_command_to_RTDE_speed_alignment": lag,
            "claim_limit": "RTDE actual_TCP_speed is robot-state-derived telemetry, not an independent external velocity sensor",
        },
        "timing": {
            "actual_dt_median_ms": float(np.median(dt) * 1000.0),
            "actual_dt_p99_ms": _quantile(dt, 0.99) * 1000.0,
            "host_sample_dt_median_ms": float(np.median(np.diff(host_time)) * 1000.0),
        },
        "conclusion": [
            "The one-sided force envelope explains a deterministic mismatch between the controller error channel and the scored filtered channel, but its physical contribution to MAE is not identified by this fixed trace.",
            "On this trace, the 3 mm/s task normal cap is active on some low-force/high-demand rows; final Jqdot is often materially smaller than the task request. These effects are observed together and cannot be assigned separate MAE shares from the same closed-loop trace.",
            "RTDE speed follows the calibrated J*actual_qd identity in the saved data; command-to-speed correlation is delayed and descriptive, not a force-control causal estimate.",
            "No controller values or live profile are changed or promoted by this analysis.",
        ],
    }


def render_markdown(data: dict[str, Any]) -> str:
    force = data["force_input_stage"]
    chain = data["normal_command_chain"]
    response = data["robot_response_stage"]
    groups = chain["clipped_vs_unclipped"]
    lag = response["published_command_to_RTDE_speed_alignment"]
    return f"""# R32 A force-to-motion chain diagnostic

**Evidence class:** sealed complete live A trace, joined offline diagnostic only. The controller, task limits, and live profile were not changed.

Input: `{data['attempt']}`, protocol `{data['protocol']}`, formal window `[5,60)`, {data['rows']} rows. Timeline SHA-256: `{data['sources']['command_timeline_sha256']}`.

| Stage | Observed on this trace | What it means |
|---|---|---|
| Force input | `max(filtered, measured)` is {force['controller_minus_scored_filter_mean_n']:.3f} N above the scored filter on average; controller-channel mean error is {force['controller_channel_signed_error_mean_n']:.3f} N while score-channel mean error is {force['score_channel_signed_error_mean_n']:.3f} N. | This is an exact same-sensor signal mismatch; it is not independent force truth and does not alone prove a physical MAE cause. |
| Task-space normal limit | Active on {chain['task_space_limit_flag_pct']:.2f}% of formal rows. On clipped rows, score-channel sample MAE is {groups['clipped']['sample_force_mae_n']:.3f} N; unclipped rows are {groups['unclipped']['sample_force_mae_n']:.3f} N. | The clipped subset is selected by higher error and requested speed, so this contrast is associative, not causal. |
| RNN joint-velocity realization | `task request − final Jqdot` absolute p95 is {chain['task_minus_published_Jqdot']['p95']:.3f} mm/s; even the no-explicit-bound/slew subset has p95 {chain['no_explicit_bound_or_slew_subset']['task_to_Jqdot_residual_p95_abs_mm_s']:.3f} mm/s. | This is a measured calibrated-Jacobian command residual. It is distinct from joint-velocity correlation and suggests the RNN output does not fully realize every normal task request. |
| Robot speed readback | RTDE `actual_TCP_speed` and `J·actual_qd` normal projections differ by at most {response['RTDE_actual_TCP_speed_and_J_actual_qd_normal_max_abs_difference_mm_s']:.6f} mm/s in the saved values. Command/readback correlation peaks at lag {lag['best_positive_correlation_lag_samples']} samples ({lag['best_lag_median_publish_to_RTDE_sample_ms']:.2f} ms median publish-to-sample offset; correlation {lag['best_lag_correlation']:.3f}). | Internal command/readback alignment only; RTDE speed is state-derived and the peak lag is not a causal plant-delay estimate. |

The exact command decomposition is `requested → task-capped → final Jqdot`. It shows where command magnitude changes, but because the trace is a closed-loop outcome, it cannot allocate a causal share of force MAE to the force envelope, task cap, or RNN realization. The reported force signals all come from the Kunwei feedback chain.

## Claim boundary

- Fixed-input/observational analysis only; no candidate commands were fed back into the robot.
- The control-force and scored-force channels are not independent measurements.
- This does not establish that removing the envelope, increasing the task-speed cap, or changing the solver improves contact force.
- Live TASE-RNN A remains unchanged. TASE-QP and SFC+TASE remain at their previously recorded evidence levels; no new live run is admitted by this report.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempt-dir", type=Path, default=DEFAULT_ATTEMPT)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MD)
    args = parser.parse_args()
    attempt = args.attempt_dir if args.attempt_dir.is_absolute() else ROOT / args.attempt_dir
    json_out = args.json_out if args.json_out.is_absolute() else ROOT / args.json_out
    markdown_out = args.markdown_out if args.markdown_out.is_absolute() else ROOT / args.markdown_out
    result = analyze(attempt)
    json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_out.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({
        "json": str(json_out),
        "markdown": str(markdown_out),
        "task_limit_pct": result["normal_command_chain"]["task_space_limit_flag_pct"],
        "task_to_Jqdot_p95_mm_s": result["normal_command_chain"]["task_minus_published_Jqdot"]["p95"],
        "peak_response_lag_samples": result["robot_response_stage"]["published_command_to_RTDE_speed_alignment"]["best_positive_correlation_lag_samples"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
