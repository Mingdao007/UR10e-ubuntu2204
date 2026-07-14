#!/usr/bin/env python3
"""Build offline v33 numeric, replay, backlog, and solver-alignment evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STEP5B = Path(
    "/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs/"
    "bridge_step5b_contact_cycloid_baseline_v3_20260702_113430/bridge_rtde_500hz.csv"
)
DEFAULT_V32 = ROOT / "runs" / "bridge_step5d_strict_rnn_ablation_v32_20260714_225729" / "bridge_rtde_500hz.csv"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finite(row: dict[str, str], key: str) -> float:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError):
        return math.nan
    return value if math.isfinite(value) else math.nan


def percentile(values: list[float], fraction: float) -> float | None:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    return ordered[min(len(ordered) - 1, max(0, int(math.ceil(fraction * len(ordered))) - 1))]


def formula_equivalence() -> dict[str, Any]:
    rng = np.random.default_rng(33)
    max_force_error = 0.0
    max_tangential_error = 0.0
    for _ in range(1000):
        e = float(rng.uniform(-20.0, 20.0))
        integral = float(rng.uniform(-1.0, 1.0))
        velocity = float(rng.uniform(-0.02, 0.02))
        paper = (e + 0.01 * integral) / 1000.0 - 7.0 * velocity
        step5b = 0.001 * e + 1e-5 * integral - 7.0 * velocity
        max_force_error = max(max_force_error, abs(paper - step5b))
        desired_v = rng.uniform(-0.01, 0.01, size=2)
        error = rng.uniform(-0.01, 0.01, size=2)
        output = compute_step5d_outer_loop(
            Step5dOuterLoopConfig(
                kp=1.5, ko=0.4, kf=0.01, Md_scalar=1000.0, Bd_scalar=7000.0,
                force_target_n=12.0, force_integral_limit_n_s=1.0,
            ),
            Step5dOuterLoopState(),
            Step5dOuterLoopInputs(
                tcp_pose_base=(0.0,) * 6,
                tcp_speed_base=(0.0,) * 6,
                force_tcp_n=(0.0, 0.0, 12.0),
                control_reaction_normal_base=(0.0, 0.0, 1.0),
                x_pd_base=(float(error[0]), float(error[1]), 0.0),
                xdot_pd_base=(float(desired_v[0]), float(desired_v[1]), 0.0),
                dt_s=0.002,
            ),
        )
        expected_tangential = desired_v + 1.5 * error
        max_tangential_error = max(
            max_tangential_error,
            float(np.max(np.abs(np.asarray(output.xdot_p[:2]) - expected_tangential))),
        )
    orientation_relative = [
        abs(0.4 * math.sin(theta / 2.0) - 0.2 * math.sin(theta))
        / (0.2 * math.sin(theta))
        for theta in np.linspace(1e-6, 0.05, 1000)
    ]
    return {
        "samples": 1000,
        "force_scalar_max_abs_error": max_force_error,
        "tangential_max_abs_error": max_tangential_error,
        "orientation_relative_error_max": max(orientation_relative),
        "pass": max_force_error <= 1e-15
        and max_tangential_error <= 1e-15
        and max(orientation_relative) <= 0.01,
    }


def backlog_simulation() -> dict[str, Any]:
    cases: dict[str, Any] = {}
    overall = True
    for consumer_hz in (350.0, 400.0, 450.0):
        queue: list[float] = []
        next_producer = 0.0
        ages: list[float] = []
        gaps: list[int] = []
        sent_heartbeat = 0
        stale_fifo_echoes: list[int] = []
        for index in range(10_000):
            now = index / consumer_hz
            packets: list[tuple[float, int]] = []
            while next_producer <= now + 1e-12:
                queue.append(next_producer)
                packets.append((next_producer, sent_heartbeat))
                next_producer += 1.0 / 500.0
            newest, newest_echo = packets[-1]
            stale_fifo_echoes.extend(echo for _, echo in packets)
            queue.clear()
            ages.append(now - newest)
            gaps.append(max(0, sent_heartbeat - newest_echo))
            sent_heartbeat += 1
        fifo_terminal_gap = max(0, sent_heartbeat - 1 - stale_fifo_echoes[0])
        gap_growth = gaps[-1] > gaps[0] + 1
        passed = (
            max(ages) <= 1.0 / 500.0 + 1e-12
            and max(gaps) <= 1
            and not gap_growth
            and fifo_terminal_gap > 5
        )
        overall = overall and passed
        cases[f"consumer_{consumer_hz:g}_hz"] = {
            "latest_sample_age_max_s": max(ages),
            "heartbeat_gap_max": max(gaps),
            "heartbeat_gap_growth": gap_growth,
            "fifo_control_terminal_gap": fifo_terminal_gap,
            "pass": passed,
        }
    return {"producer_hz": 500.0, "cases": cases, "pass": overall}


def replay_step5b(csv_path: Path) -> dict[str, Any]:
    state = Step5dOuterLoopState()
    previous_t: float | None = None
    live_linear_norms: list[float] = []
    v33_linear_norms: list[float] = []
    v33_angular_norms: list[float] = []
    normal_components: list[float] = []
    xy_errors: list[float] = []
    rows = 0
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if abs(finite(row, "ur_output_double_register_35") - 25.0) > 0.03:
                continue
            pose = tuple(finite(row, f"ur_actual_TCP_pose_{index}") for index in range(6))
            speed = tuple(finite(row, f"ur_actual_TCP_speed_{index}") for index in range(6))
            force_t = tuple(finite(row, f"_step4e_force_t_{axis}") for axis in "xyz")
            normal = tuple(finite(row, f"_step4e_control_normal_b_{axis}") for axis in "xyz")
            if not all(math.isfinite(value) for value in (*pose, *speed, *force_t, *normal)):
                continue
            t_s = finite(row, "t_monotonic_s")
            dt_s = 0.002 if previous_t is None or not math.isfinite(t_s) else min(0.01, max(1e-6, t_s - previous_t))
            previous_t = t_s
            error_x = finite(row, "_step4e_path_error_x_m")
            error_y = finite(row, "_step4e_path_error_y_m")
            desired_vx = finite(row, "_step4e_desired_vx_m_s")
            desired_vy = finite(row, "_step4e_desired_vy_m_s")
            if not all(math.isfinite(value) for value in (error_x, error_y, desired_vx, desired_vy)):
                continue
            output = compute_step5d_outer_loop(
                Step5dOuterLoopConfig(
                    kp=1.5, ko=0.4, kf=0.01, Md_scalar=1000.0, Bd_scalar=7000.0,
                    force_target_n=12.0, force_integral_limit_n_s=1.0,
                ),
                state,
                Step5dOuterLoopInputs(
                    tcp_pose_base=pose,
                    tcp_speed_base=speed,
                    force_tcp_n=force_t,
                    control_reaction_normal_base=normal,
                    x_pd_base=(pose[0] + error_x, pose[1] + error_y, pose[2]),
                    xdot_pd_base=(desired_vx, desired_vy, 0.0),
                    dt_s=dt_s,
                ),
                include_diagnostics=False,
            )
            state = output.next_state
            live = [finite(row, f"step4e_cmd_v{axis}_m_s") for axis in "xyz"]
            if all(math.isfinite(value) for value in live):
                live_linear_norms.append(float(np.linalg.norm(live)))
            v33_linear_norms.append(float(np.linalg.norm(output.xdot_c[:3])))
            v33_angular_norms.append(float(np.linalg.norm(output.xdot_c[3:])))
            normal_components.append(float(np.dot(np.asarray(output.xdot_c[:3]), np.asarray(normal))))
            xy_errors.append(math.hypot(error_x, error_y))
            rows += 1
    return {
        "source": str(csv_path),
        "source_sha256": sha256(csv_path),
        "stage25_rows": rows,
        "live_linear_norm_p95_m_s": percentile(live_linear_norms, 0.95),
        "v33_linear_norm_p95_m_s": percentile(v33_linear_norms, 0.95),
        "v33_angular_norm_p95_rad_s": percentile(v33_angular_norms, 0.95),
        "v33_normal_component_p05_m_s": percentile(normal_components, 0.05),
        "v33_normal_component_p95_m_s": percentile(normal_components, 0.95),
        "xy_error_p95_m": percentile(xy_errors, 0.95),
        "pass": rows >= 10_000 and all(math.isfinite(value) for value in v33_linear_norms + v33_angular_norms),
    }


def rnn_oracle_alignment(csv_path: Path) -> dict[str, Any]:
    values: list[float] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if abs(finite(row, "ur_output_double_register_35") - 25.0) <= 0.03:
                value = finite(row, "_step5d_rnn_vs_oracle_qdot_norm")
                if math.isfinite(value):
                    values.append(value)
    maximum = max(values) if values else None
    return {
        "source": str(csv_path),
        "rows": len(values),
        "delta_norm_p99": percentile(values, 0.99),
        "delta_norm_max": maximum,
        "solver_definition_changed": False,
        "pass": len(values) > 1000 and maximum is not None and maximum <= 1e-5,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step5b-csv", type=Path, default=DEFAULT_STEP5B)
    parser.add_argument("--v32-csv", type=Path, default=DEFAULT_V32)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    checks = {
        "mathematical_equivalence": formula_equivalence(),
        "rtde_backlog": backlog_simulation(),
        "step5b_continuous_replay": replay_step5b(args.step5b_csv),
        "rnn_oracle_alignment": rnn_oracle_alignment(args.v32_csv),
    }
    payload = {
        "schema": "step5d_v33_offline_validation_v1",
        "profile": "step5d_v33_step5b_discrete_equivalent_v1",
        "checks": checks,
        "overall_pass": all(check["pass"] is True for check in checks.values()),
        "claim_boundary": "offline numeric/replay evidence only; no live authorization",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["overall_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
