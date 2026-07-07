#!/usr/bin/env python3
"""Read-only Step5d no-contact P0 RNN replay/sigr sweep.

This tool replays the recorded Stage25 target sequence continuously. Do not use
sampled rows for solver attribution: the Step5d outer-loop state is stateful.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)
from kunwei_rtde_bridge import (
    STEP5D_LIVEPREP_TRUTH_PATH,
    STEP5D_NO_CONTACT_P0_ANGULAR_LIMIT_RAD_S,
    limit_step5d_no_contact_p0_xdot_components,
    scale_step5d_xdot_for_joint_feasibility,
    step5d_kin,
    step5d_omega_bounds,
    step5d_tcp_jacobian_base,
)


BRIDGE_CSV_FILENAME = "bridge_rtde_500hz.csv"
STAGE_REGISTER = "ur_output_double_register_35"
DEFAULT_R_VALUES = (1.0, 0.8, 0.6, 0.4)
DEFAULT_EPSILON_VALUES = (0.022,)
BASELINE_R = 1.0
BASELINE_EPSILON = 0.022
DEFAULT_QDOT_LIMIT_RAD_S = 0.15
DEFAULT_ALIGNMENT_MEDIAN_MAX = 1e-4
DEFAULT_ALIGNMENT_P99_MAX = 5e-4


def finite_float(value: Any, default: float = math.nan) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def select_stage25_rows(rows: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    return [row for row in rows if finite_float(row.get(STAGE_REGISTER)) == 25.0]


def replay_alignment_ok(
    stats: dict[str, Any],
    *,
    median_max: float = DEFAULT_ALIGNMENT_MEDIAN_MAX,
    p99_max: float = DEFAULT_ALIGNMENT_P99_MAX,
) -> bool:
    median = finite_float(stats.get("median"))
    p99 = finite_float(stats.get("p99"))
    return math.isfinite(median) and math.isfinite(p99) and median <= median_max and p99 <= p99_max


def stats(values: Sequence[float], *, threshold: float = 1e-3) -> dict[str, Any]:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return {
            "n": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "p95": None,
            "p99": None,
            "over_threshold": 0,
            "threshold": threshold,
        }
    quantiles = statistics.quantiles(finite, n=100) if len(finite) >= 100 else []
    return {
        "n": len(finite),
        "min": min(finite),
        "max": max(finite),
        "mean": sum(finite) / len(finite),
        "median": statistics.median(finite),
        "p95": quantiles[94] if quantiles else None,
        "p99": quantiles[98] if quantiles else None,
        "over_threshold": sum(value > threshold for value in finite),
        "threshold": threshold,
    }


def vector(row: dict[str, str], prefix: str, length: int) -> np.ndarray:
    return np.asarray([finite_float(row.get(f"{prefix}_{idx}")) for idx in range(length)], dtype=float)


def named_vector(row: dict[str, str], names: Sequence[str]) -> np.ndarray:
    return np.asarray([finite_float(row.get(name)) for name in names], dtype=float)


def load_bridge_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def resolve_bridge_csv(path: Path) -> Path:
    if path.is_dir():
        return path / BRIDGE_CSV_FILENAME
    return path


def _validate_required(row: dict[str, str], required: Sequence[str]) -> None:
    missing = [name for name in required if name not in row]
    if missing:
        raise RuntimeError("bridge CSV missing required replay columns: " + ", ".join(sorted(missing)))


def precompute_targets(stage25_rows: Sequence[dict[str, str]], csv_path: Path) -> list[dict[str, Any]]:
    if not stage25_rows:
        raise RuntimeError("bridge CSV has no Stage25 rows in ur_output_double_register_35")
    required = [
        *(f"ur_actual_q_{idx}" for idx in range(6)),
        *(f"ur_actual_qd_{idx}" for idx in range(6)),
        *(f"ur_actual_TCP_pose_{idx}" for idx in range(6)),
        *(f"ur_actual_TCP_speed_{idx}" for idx in range(6)),
        "_step4e_force_t_x",
        "_step4e_force_t_y",
        "_step4e_force_t_z",
        "_step4e_control_normal_b_x",
        "_step4e_control_normal_b_y",
        "_step4e_control_normal_b_z",
        "_step4e_desired_x_m",
        "_step4e_desired_y_m",
        "_step4e_desired_vx_m_s",
        "_step4e_desired_vy_m_s",
        "_step5d_constraint_residual_norm",
    ]
    _validate_required(stage25_rows[0], required)

    model_bundle = step5d_kin.build_calibrated_model()
    audit_rows = step5d_kin.finite_run_rows(csv_path)
    tcp_offset = step5d_kin.infer_tcp_offset(model_bundle, audit_rows)["mean"]
    q_min = model_bundle.model.lowerPositionLimit
    q_max = model_bundle.model.upperPositionLimit
    outer_config = Step5dOuterLoopConfig(
        kp=4.0,
        ko=5.0,
        kf=1.0,
        Md_scalar=12.0,
        Bd_scalar=550.0,
        force_target_n=1.0,
        delay_T_s=0.002,
        force_sign_convention="step5_step6_positive_normal_load",
    )
    outer_state = Step5dOuterLoopState()
    previous_t = None
    targets: list[dict[str, Any]] = []
    for row in stage25_rows:
        q = vector(row, "ur_actual_q", 6)
        qd = vector(row, "ur_actual_qd", 6)
        pose = vector(row, "ur_actual_TCP_pose", 6)
        speed = vector(row, "ur_actual_TCP_speed", 6)
        if not all(np.all(np.isfinite(value)) for value in (q, qd, pose, speed)):
            continue
        t_s = finite_float(row.get("t_monotonic_s"))
        dt_s = 0.002 if previous_t is None else max(1e-6, min(0.02, t_s - previous_t))
        previous_t = t_s if math.isfinite(t_s) else previous_t
        jacobian = step5d_tcp_jacobian_base(model_bundle, q, tcp_offset)
        omega_minus, omega_plus = step5d_omega_bounds(
            q,
            q_min,
            q_max,
            alpha_s_inv=1.0,
            qdot_limit_rad_s=DEFAULT_QDOT_LIMIT_RAD_S,
        )
        outer_output = compute_step5d_outer_loop(
            outer_config,
            outer_state,
            Step5dOuterLoopInputs(
                tcp_pose_base=tuple(float(value) for value in pose),
                tcp_speed_base=tuple(float(value) for value in speed),
                force_tcp_n=tuple(
                    float(value)
                    for value in named_vector(row, ("_step4e_force_t_x", "_step4e_force_t_y", "_step4e_force_t_z"))
                ),
                control_reaction_normal_base=tuple(
                    float(value)
                    for value in named_vector(
                        row,
                        (
                            "_step4e_control_normal_b_x",
                            "_step4e_control_normal_b_y",
                            "_step4e_control_normal_b_z",
                        ),
                    )
                ),
                x_pd_base=(finite_float(row.get("_step4e_desired_x_m")), finite_float(row.get("_step4e_desired_y_m")), float(pose[2])),
                xdot_pd_base=(
                    finite_float(row.get("_step4e_desired_vx_m_s")),
                    finite_float(row.get("_step4e_desired_vy_m_s")),
                    0.0,
                ),
                dt_s=dt_s,
                cmd_valid=True,
            ),
        )
        outer_state = outer_output.next_state
        xdot_limited, _ = limit_step5d_no_contact_p0_xdot_components(
            np.asarray(outer_output.xdot_c, dtype=float),
            max_angular_rad_s=STEP5D_NO_CONTACT_P0_ANGULAR_LIMIT_RAD_S,
        )
        xdot_feasible, feasibility = scale_step5d_xdot_for_joint_feasibility(
            xdot_limited,
            jacobian,
            qdot_cap_rad_s=DEFAULT_QDOT_LIMIT_RAD_S,
            safety=0.9,
        )
        targets.append(
            {
                "J": jacobian,
                "xdot_c": xdot_feasible,
                "omega_minus": omega_minus,
                "omega_plus": omega_plus,
                "dt": dt_s,
                "q": q,
                "qd": qd,
                "logged_residual": finite_float(row.get("_step5d_constraint_residual_norm")),
                "feasibility_scale": float(feasibility["xdot_feasibility_scale"]),
            }
        )
    if not targets:
        raise RuntimeError("bridge CSV has no finite Stage25 rows usable for replay")
    return targets


def replay_targets(targets: Sequence[dict[str, Any]], *, r: float, epsilon: float) -> dict[str, Any]:
    solver = StrictTaseRnnSolver(
        StrictRnnConfig(
            paper_truth_path=STEP5D_LIVEPREP_TRUTH_PATH,
            qdot_limit_rad_s=DEFAULT_QDOT_LIMIT_RAD_S,
            epsilon=epsilon,
            sigr_exponent_r=r,
        )
    )
    pending_warm_start = True
    residuals: list[float] = []
    qdot_max: list[float] = []
    active_bounds_rows = 0
    alignment_errors: list[float] = []
    lambda_norms: list[float] = []
    for target in targets:
        if pending_warm_start:
            solver.warm_start(
                J=target["J"],
                xdot_c=target["xdot_c"],
                omega_minus=target["omega_minus"],
                omega_plus=target["omega_plus"],
            )
            pending_warm_start = False
        result = solver.solve(
            actual_q=target["q"],
            actual_qd=target["qd"],
            target_state={
                "J": target["J"],
                "xdot_c": target["xdot_c"],
                "omega_minus": target["omega_minus"],
                "omega_plus": target["omega_plus"],
                "dt": target["dt"],
                "epsilon": epsilon,
                "r": r,
                "cmd_valid": True,
            },
        )
        residuals.append(float(result.residual_norm))
        qdot_max.append(max(abs(float(value)) for value in result.qdot))
        active_bounds = sum(bool(value) for value in result.diagnostics["active_bounds_mask"])
        active_bounds_rows += 1 if active_bounds else 0
        logged = finite_float(target["logged_residual"])
        if math.isfinite(logged):
            alignment_errors.append(abs(float(result.residual_norm) - logged))
        lambda_norms.append(float(np.linalg.norm(np.asarray(result.diagnostics["lambda_state"], dtype=float))))
    return {
        "r": r,
        "epsilon": epsilon,
        "residual_norm": stats(residuals),
        "qdot_max_abs_rad_s": stats(qdot_max, threshold=DEFAULT_QDOT_LIMIT_RAD_S),
        "lambda_norm": stats(lambda_norms),
        "active_bounds_rows": active_bounds_rows,
        "abs_error_vs_logged_residual": stats(alignment_errors),
        "logged_alignment_ok": replay_alignment_ok(stats(alignment_errors)),
    }


def run_sweep(csv_or_run_dir: Path, *, r_values: Sequence[float], epsilon_values: Sequence[float]) -> dict[str, Any]:
    csv_path = resolve_bridge_csv(csv_or_run_dir)
    if not csv_path.exists():
        raise FileNotFoundError(f"bridge CSV not found: {csv_path}")
    rows = load_bridge_rows(csv_path)
    stage25_rows = select_stage25_rows(rows)
    targets = precompute_targets(stage25_rows, csv_path)
    runs = [
        replay_targets(targets, r=float(r_value), epsilon=float(epsilon_value))
        for epsilon_value in epsilon_values
        for r_value in r_values
    ]
    baseline = next(
        (
            item
            for item in runs
            if math.isclose(float(item["r"]), BASELINE_R) and math.isclose(float(item["epsilon"]), BASELINE_EPSILON)
        ),
        None,
    )
    if baseline is None:
        baseline = replay_targets(targets, r=BASELINE_R, epsilon=BASELINE_EPSILON)
    return {
        "ok": True,
        "bridge_csv": str(csv_path),
        "stage_field": STAGE_REGISTER,
        "stage25_rows": len(stage25_rows),
        "replayed_rows": len(targets),
        "default_r_values": list(DEFAULT_R_VALUES),
        "baseline": baseline,
        "runs": runs,
        "baseline_logged_alignment_ok": bool(baseline.get("logged_alignment_ok")),
        "safety_boundary": [
            "offline analysis only",
            "no bridge start",
            "no controller upload",
            "no TP play",
            "no robot motion",
        ],
    }


def parse_csv_floats(raw: str) -> tuple[float, ...]:
    values = tuple(float(piece.strip()) for piece in raw.split(",") if piece.strip())
    if not values:
        raise argparse.ArgumentTypeError("expected at least one float")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_or_run_dir", type=Path)
    parser.add_argument("--r-values", type=parse_csv_floats, default=DEFAULT_R_VALUES)
    parser.add_argument("--epsilon-values", type=parse_csv_floats, default=DEFAULT_EPSILON_VALUES)
    args = parser.parse_args()
    try:
        result = run_sweep(args.csv_or_run_dir, r_values=args.r_values, epsilon_values=args.epsilon_values)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["baseline_logged_alignment_ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
