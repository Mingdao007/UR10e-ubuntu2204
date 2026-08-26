"""Replay historical high-force PATH traces through the R013 scalar primitive.

This is an invariant regression only.  Recorded force remains an exogenous
input; the replay does not predict R013 force, MAE, or live safety.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from step5d_paper_outer_loop import conditional_double_clamp_step


def _rows(path: Path, attempts: set[int]) -> dict[int, list[dict[str, Any]]]:
    result = {attempt: [] for attempt in attempts}
    with Path(path).open("r", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            attempt = row.get("attempt_ordinal")
            if attempt in result:
                result[int(attempt)].append(row)
    missing = [attempt for attempt, rows in result.items() if len(rows) < 2]
    if missing:
        raise ValueError(f"historical PATH traces missing attempts {missing}")
    return result


def replay_trace(
    rows: Sequence[Mapping[str, Any]],
    *,
    force_p_gain: float,
    force_i_gain: float,
    force_damping: float,
    target_force_n: float,
    normal_velocity_limit_m_s: float,
) -> dict[str, Any]:
    integral = 0.0
    velocity = 0.0
    previous_time: float | None = None
    max_abs_integral = 0.0
    max_abs_i_term = 0.0
    freeze_count = 0
    saturation_count = 0
    directional_continuation_violations = 0
    state_violations = 0
    authority_violations = 0
    force_norm_max = 0.0
    for row in rows:
        timestamp = float(row["monotonic_s"])
        dt_s = 0.002 if previous_time is None else min(0.05, max(0.0, timestamp - previous_time))
        previous_time = timestamp
        force = float(row["filtered_normal_n"])
        force_norm_max = max(force_norm_max, float(row.get("force_norm_n", abs(force))))
        old_integral = integral
        result = conditional_double_clamp_step(
            force_error_n=target_force_n - force,
            integral_state_n_s=integral,
            normal_velocity_m_s=velocity,
            dt_s=dt_s,
            force_p_gain=force_p_gain,
            force_i_gain=force_i_gain,
            force_damping=force_damping,
            normal_velocity_limit_m_s=normal_velocity_limit_m_s,
            state_limit_n_s=1.0,
            authority_error_n=0.5,
        )
        integral = result.integral_state_n_s
        velocity = result.applied_normal_velocity_m_s
        max_abs_integral = max(max_abs_integral, abs(integral))
        max_abs_i_term = max(max_abs_i_term, abs(result.i_term))
        freeze_count += int(result.conditional_frozen)
        saturation_count += int(result.velocity_saturated)
        state_violations += int(abs(integral) > 1.0 + 1e-12)
        authority_violations += int(abs(result.i_term) > 0.5 * force_p_gain + 1e-12)
        if result.conditional_frozen and not math.isclose(
            integral, old_integral, rel_tol=0.0, abs_tol=1e-12
        ):
            directional_continuation_violations += 1
    return {
        "sample_count": len(rows),
        "force_norm_max_n": force_norm_max,
        "max_abs_integral_n_s": max_abs_integral,
        "max_abs_i_term": max_abs_i_term,
        "state_limit_n_s": 1.0,
        "i_term_authority_limit": 0.5 * force_p_gain,
        "saturation_count": saturation_count,
        "freeze_count": freeze_count,
        "state_violation_count": state_violations,
        "authority_violation_count": authority_violations,
        "directional_continuation_violation_count": directional_continuation_violations,
        "passed": state_violations == authority_violations == directional_continuation_violations == 0,
    }


def build_report(
    path: Path,
    attempts: Iterable[int],
    *,
    force_p_gain: float,
    force_i_gain: float,
    force_damping: float,
    target_force_n: float,
    normal_velocity_limit_m_s: float,
) -> dict[str, Any]:
    selected = {int(value) for value in attempts}
    rows = _rows(path, selected)
    traces = {
        str(attempt): replay_trace(
            rows[attempt],
            force_p_gain=force_p_gain,
            force_i_gain=force_i_gain,
            force_damping=force_damping,
            target_force_n=target_force_n,
            normal_velocity_limit_m_s=normal_velocity_limit_m_s,
        )
        for attempt in sorted(selected)
    }
    return {
        "schema": "step5d.autotune-v4/r013-historical-anti-windup-replay-v1",
        "source": str(Path(path).resolve()),
        "attempts": sorted(selected),
        "controller": {
            "force_p_gain": force_p_gain,
            "force_i_gain": force_i_gain,
            "force_damping": force_damping,
            "target_force_n": target_force_n,
            "normal_velocity_limit_m_s": normal_velocity_limit_m_s,
            "integral_policy": "conditional-double-clamp-v1",
        },
        "claim_boundary": "invariant regression only; does not predict R013 force, MAE, or live safety",
        "traces": traces,
        "passed": all(trace["passed"] for trace in traces.values()),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--attempt", type=int, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force-p-gain", type=float, default=0.0028284271248)
    parser.add_argument("--force-i-gain", type=float, default=0.001810193359837562)
    parser.add_argument("--force-damping", type=float, default=28.0)
    parser.add_argument("--target-force-n", type=float, default=5.0)
    parser.add_argument("--normal-velocity-limit-m-s", type=float, default=0.003)
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = build_report(
        args.trace,
        args.attempt,
        force_p_gain=args.force_p_gain,
        force_i_gain=args.force_i_gain,
        force_damping=args.force_damping,
        target_force_n=args.target_force_n,
        normal_velocity_limit_m_s=args.normal_velocity_limit_m_s,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
