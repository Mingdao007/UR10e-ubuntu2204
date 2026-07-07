#!/usr/bin/env python3
"""Offline Step5d strict RNN P0 rail simulation and artifact audit.

This script does not connect to RTDE, Dashboard, URScript, ROS, or controller
state. It runs synthetic strict-RNN cases and optionally audits an existing
bridge CSV for P0 rail evidence.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

import analyze_step5d_bridge_run
from kunwei_rtde_bridge import (
    limit_step5d_live_xdot,
    scale_step5d_xdot_for_joint_feasibility,
    step5d_dls_qdot_oracle,
    step5d_no_contact_p0_qdot_acceptance_gate,
)
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver


SCHEMA = "step5d_rnn_p0_rail_simulation_v1"
SAFETY_BOUNDARY = "offline_only_no_robot_no_bridge_no_controller"
QDOT_CAP_RAD_S = 0.15
QDOT_RAIL_MARGIN_RAD_S = 1e-9
QDOT_RAIL_THRESHOLD_RAD_S = QDOT_CAP_RAD_S - QDOT_RAIL_MARGIN_RAD_S
LEGACY_V4_QDOT_CAP_RAD_S = 0.05
LEGACY_V4_QDOT_RAIL_THRESHOLD_RAD_S = LEGACY_V4_QDOT_CAP_RAD_S - QDOT_RAIL_MARGIN_RAD_S
P0_LINEAR_LIMIT_M_S = 0.004
P0_ANGULAR_LIMIT_RAD_S = 0.015
DT_S = 0.002
EPSILON = 0.022
SIGR_EXPONENT_R = StrictRnnConfig().sigr_exponent_r
REACTION_NORMAL_B = (0.0, 0.0, -1.0)


def finite_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def finite_int(value: object) -> int | None:
    parsed = finite_float(value)
    if parsed is None:
        return None
    rounded = round(parsed)
    if abs(parsed - rounded) > 1e-9:
        return None
    return int(rounded)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def strict_solver(*, sigr_exponent_r: float = SIGR_EXPONENT_R) -> StrictTaseRnnSolver:
    with tempfile.TemporaryDirectory() as tmp:
        truth_path = Path(tmp) / "step5c_tase_paper_truth.json"
        truth_path.write_text(
            json.dumps({"strict_rnn_enabled": True, "pending_pdf_verify": [], "sections": {}}),
            encoding="utf-8",
        )
        return StrictTaseRnnSolver(
            StrictRnnConfig(
                paper_truth_path=truth_path,
                sigr_exponent_r=sigr_exponent_r,
            )
        )


def rail_row_fraction(values: list[float], *, threshold_rad_s: float = QDOT_RAIL_THRESHOLD_RAD_S) -> float:
    if not values:
        return 0.0
    return sum(1 for value in values if value >= threshold_rad_s) / len(values)


def simulate_case(
    *,
    name: str,
    raw_xdot: np.ndarray,
    jacobian: np.ndarray,
    apply_limiter: bool,
    apply_feasibility: bool,
    sigr_exponent_r: float = SIGR_EXPONENT_R,
    warm_start_scale: float = 1.0,
    ticks: int = 20,
) -> dict[str, Any]:
    lower = np.full(6, -QDOT_CAP_RAD_S, dtype=float)
    upper = np.full(6, QDOT_CAP_RAD_S, dtype=float)
    if apply_limiter:
        limited_xdot, limiter_active = limit_step5d_live_xdot(
            raw_xdot,
            max_linear_m_s=P0_LINEAR_LIMIT_M_S,
            max_angular_rad_s=P0_ANGULAR_LIMIT_RAD_S,
        )
    else:
        limited_xdot = raw_xdot.copy()
        limiter_active = False

    if apply_feasibility:
        target_xdot, feasibility = scale_step5d_xdot_for_joint_feasibility(
            limited_xdot,
            jacobian,
            qdot_cap_rad_s=QDOT_CAP_RAD_S,
            safety=0.9,
        )
    else:
        target_xdot = limited_xdot
        required = np.linalg.solve(jacobian, target_xdot)
        required_inf = float(np.max(np.abs(required)))
        feasibility = {
            "qdot_cap_rad_s": QDOT_CAP_RAD_S,
            "jinv_xdot_inf_rad_s": required_inf,
            "jinv_xdot_inf_over_qdot_cap": required_inf / QDOT_CAP_RAD_S,
            "jinv_xdot_solve_status": "ok",
            "xdot_feasibility_scale": 1.0,
            "xdot_norm_pre_feasibility_scale": float(np.linalg.norm(target_xdot)),
            "xdot_norm_post_feasibility_scale": float(np.linalg.norm(target_xdot)),
            "xdot_feasibility_scale_active": False,
        }

    dls_qdot = np.asarray(step5d_dls_qdot_oracle(jacobian, target_xdot, lower, upper), dtype=float)
    solver = strict_solver(sigr_exponent_r=sigr_exponent_r)
    solver.warm_start(J=jacobian, xdot_c=target_xdot, omega_minus=lower, omega_plus=upper)
    solver.lambda_state *= float(warm_start_scale)
    solver.theta_dot_state *= float(warm_start_scale)

    qdot_maxima: list[float] = []
    all_joints_rail_count = 0
    accepted_rail_count = 0
    accepted_count = 0
    active_counts: list[int] = []
    residuals: list[float] = []
    lambda_norms: list[float] = []
    normal_tracking_errors: list[float] = []
    approach_sign_mismatch_count = 0
    for _ in range(ticks):
        result = solver.solve(
            actual_q=[0.0] * 6,
            actual_qd=[0.0] * 6,
            target_state={
                "J": jacobian,
                "xdot_c": target_xdot,
                "omega_minus": lower,
                "omega_plus": upper,
                "dt": DT_S,
                "epsilon": EPSILON,
                "r": sigr_exponent_r,
                "cmd_valid": True,
            },
        )
        qdot = np.asarray(result.qdot, dtype=float)
        qdot_abs = np.abs(qdot)
        qdot_maxima.append(float(np.max(qdot_abs)))
        if bool(np.all(qdot_abs >= QDOT_RAIL_THRESHOLD_RAD_S)):
            all_joints_rail_count += 1
        active_count = int(sum(bool(value) for value in result.diagnostics["active_bounds_mask"]))
        active_counts.append(active_count)
        residuals.append(float(result.residual_norm))
        lambda_norms.append(float(np.linalg.norm(np.asarray(result.diagnostics["lambda_state"], dtype=float))))
        gate = step5d_no_contact_p0_qdot_acceptance_gate(
            qdot=qdot,
            jacobian=jacobian,
            outer_xdot_limited=target_xdot,
            reaction_normal_b=REACTION_NORMAL_B,
            residual_norm=result.residual_norm,
            active_bounds_count=active_count,
            max_tcp_speed_m_s=P0_LINEAR_LIMIT_M_S,
            max_normal_tracking_error_m_s=5e-4,
            max_residual_norm=1e-3,
        )
        normal_tracking_errors.append(float(gate["normal_tracking_error_m_s"]))
        outer_approach = float(gate["outer_approach_normal_m_s"])
        jqdot_approach = float(gate["jqdot_approach_normal_m_s"])
        if (outer_approach > 0.0 and jqdot_approach <= 0.0) or (outer_approach < 0.0 and jqdot_approach >= 0.0):
            approach_sign_mismatch_count += 1
        if bool(gate["accepted"]):
            accepted_count += 1
            if float(np.max(np.abs(np.asarray(gate["qdot"], dtype=float)))) >= QDOT_RAIL_THRESHOLD_RAD_S:
                accepted_rail_count += 1

    linear_norm = float(np.linalg.norm(target_xdot[:3]))
    angular_norm = float(np.linalg.norm(target_xdot[3:]))
    return {
        "name": name,
        "sigr_exponent_r": sigr_exponent_r,
        "ticks": ticks,
        "raw_xdot_norm": float(np.linalg.norm(raw_xdot)),
        "limited_xdot_norm": float(np.linalg.norm(limited_xdot)),
        "joint_feasible_xdot_norm": float(np.linalg.norm(target_xdot)),
        "outer_linear_norm_max": linear_norm,
        "outer_angular_norm_max": angular_norm,
        "outer_limiter_active_fraction": 1.0 if limiter_active else 0.0,
        "feasibility_scale_min": float(feasibility["xdot_feasibility_scale"]),
        "jinv_xdot_inf_over_qdot_cap": float(feasibility["jinv_xdot_inf_over_qdot_cap"]),
        "dls_rail_fraction": sum(1 for value in np.abs(dls_qdot) if value >= QDOT_RAIL_THRESHOLD_RAD_S) / 6.0,
        "rnn_rail_fraction": rail_row_fraction(qdot_maxima),
        "all_joints_rail_fraction": all_joints_rail_count / ticks,
        "accepted_command_rail_fraction": accepted_rail_count / accepted_count if accepted_count else 0.0,
        "accepted_ticks": accepted_count,
        "active_bounds_count_first": active_counts[0],
        "active_bounds_count_max": max(active_counts),
        "active_bounds_count_p50": percentile([float(value) for value in active_counts], 0.5),
        "constraint_residual_norm_first": residuals[0],
        "constraint_residual_norm_p95": percentile(residuals, 0.95),
        "lambda_norm_first": lambda_norms[0],
        "lambda_norm_max": max(lambda_norms),
        "normal_tracking_error_first": normal_tracking_errors[0],
        "approach_normal_sign_mismatch_count": approach_sign_mismatch_count,
    }


def r_sensitivity_case() -> dict[str, Any]:
    xdot = np.asarray([0.0, 0.0, 0.0001, 0.003, 0.0, 0.0], dtype=float)
    jacobian = np.eye(6, dtype=float)
    lower = np.full(6, -QDOT_CAP_RAD_S, dtype=float)
    upper = np.full(6, QDOT_CAP_RAD_S, dtype=float)
    rows: dict[str, Any] = {}
    for r in (0.2, 1.0):
        solver = strict_solver(sigr_exponent_r=r)
        solver.warm_start(J=jacobian, xdot_c=xdot, omega_minus=lower, omega_plus=upper)
        solver.theta_dot_state = np.zeros(6, dtype=float)
        theta_before = solver.theta_dot_state.copy()
        diag = solver.step(
            J=jacobian,
            xdot_c=xdot,
            omega_minus=lower,
            omega_plus=upper,
            dt=DT_S,
            epsilon=EPSILON,
            r=r,
        )
        theta_after = np.asarray(diag.theta_dot_state, dtype=float)
        rows[f"r_{r:.1f}"] = {
            "sigr_exponent_r": r,
            "first_theta_delta_norm": float(np.linalg.norm(theta_after - theta_before)),
            "first_theta_dot_norm": float(np.linalg.norm(theta_after)),
            "active_bounds_count_first": int(sum(bool(value) for value in diag.active_bounds_mask)),
            "constraint_residual_norm_first": float(diag.constraint_residual_norm),
            "theta_dot_update_limited_count": int(sum(bool(value) for value in diag.theta_dot_update_limited_mask)),
        }
    return rows


def synthetic_payload(*, sigr_exponent_r: float = SIGR_EXPONENT_R) -> dict[str, Any]:
    safe_xdot = np.asarray([0.0, 0.0, 0.0001, 0.003, 0.0, 0.0], dtype=float)
    oversized_xdot = np.asarray([0.020, 0.020, 0.020, 0.080, 0.080, 0.080], dtype=float)
    identity_jacobian = np.eye(6, dtype=float)
    low_authority_jacobian = 0.02 * np.eye(6, dtype=float)
    sweep = {}
    for scale in (0.0, 0.25, 0.5, 1.0, 2.0):
        sweep[f"{scale:.2f}x"] = simulate_case(
            name=f"safe_lambda_scale_{scale:.2f}",
            raw_xdot=safe_xdot,
            jacobian=identity_jacobian,
            apply_limiter=True,
            apply_feasibility=True,
            sigr_exponent_r=sigr_exponent_r,
            warm_start_scale=scale,
            ticks=10,
        )
    return {
        "p0_safe_warm_start": simulate_case(
            name="p0_safe_warm_start",
            raw_xdot=safe_xdot,
            jacobian=identity_jacobian,
            apply_limiter=True,
            apply_feasibility=True,
            sigr_exponent_r=sigr_exponent_r,
        ),
        "oversized_after_limiter": simulate_case(
            name="oversized_after_limiter",
            raw_xdot=oversized_xdot,
            jacobian=low_authority_jacobian,
            apply_limiter=True,
            apply_feasibility=True,
            sigr_exponent_r=sigr_exponent_r,
        ),
        "oversized_without_limiter": simulate_case(
            name="oversized_without_limiter",
            raw_xdot=oversized_xdot,
            jacobian=low_authority_jacobian,
            apply_limiter=False,
            apply_feasibility=False,
            sigr_exponent_r=sigr_exponent_r,
        ),
        "r_sensitivity_cold_or_partial_warm_start": r_sensitivity_case(),
        "lambda_scale_sweep": sweep,
    }


def read_bridge_rows(run_dir: Path) -> list[dict[str, str]]:
    csv_path = run_dir / "bridge_rtde_500hz.csv"
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def stage25_rnn_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    selected = []
    for row in rows:
        if str(row.get("_step5d_stage25_control_mode") or "") != "speedj_rnn_live":
            continue
        stage = finite_float(row.get("ur_output_double_register_35"))
        if stage is not None and abs(stage - 25.0) <= 0.05:
            selected.append(row)
    return selected


def csv_audit(run_dir: Path) -> dict[str, Any]:
    rows = stage25_rnn_rows(read_bridge_rows(run_dir))
    qdot_values = []
    active_bounds_values = []
    residual_values = []
    for row in rows:
        qdot = finite_float(row.get("_step5d_rnn_qdot_max_abs_raw_rad_s"))
        if qdot is None:
            qdot = finite_float(row.get("_step5d_qdot_max_abs_rad_s"))
        if qdot is not None:
            qdot_values.append(qdot)
        active_bounds = finite_int(row.get("_step5d_active_bounds_count"))
        if active_bounds is not None:
            active_bounds_values.append(active_bounds)
        residual = finite_float(row.get("_step5d_constraint_residual_norm"))
        if residual is not None:
            residual_values.append(residual)

    persisted_path = run_dir / "step5d_bridge_analysis.json"
    persisted = json.loads(persisted_path.read_text(encoding="utf-8")) if persisted_path.exists() else {}
    current = analyze_step5d_bridge_run.analyze_run_dir(run_dir)
    persisted_classification = persisted.get("classification")
    current_classification = current.get("classification")
    return {
        "run_dir": str(run_dir),
        "stage25_rows": len(rows),
        "qdot_rail_threshold_rad_s": QDOT_RAIL_THRESHOLD_RAD_S,
        "rnn_rail_fraction": rail_row_fraction(qdot_values),
        "legacy_v4_qdot_cap_rad_s": LEGACY_V4_QDOT_CAP_RAD_S,
        "legacy_v4_qdot_rail_threshold_rad_s": LEGACY_V4_QDOT_RAIL_THRESHOLD_RAD_S,
        "legacy_v4_rnn_rail_fraction": rail_row_fraction(
            qdot_values,
            threshold_rad_s=LEGACY_V4_QDOT_RAIL_THRESHOLD_RAD_S,
        ),
        "all_joints_rail_fraction": (
            sum(1 for value in active_bounds_values if value >= 5) / len(active_bounds_values)
            if active_bounds_values
            else 0.0
        ),
        "active_bounds_count_first": active_bounds_values[0] if active_bounds_values else None,
        "active_bounds_count_max": max(active_bounds_values) if active_bounds_values else None,
        "constraint_residual_norm_first": residual_values[0] if residual_values else None,
        "constraint_residual_norm_p95": percentile(residual_values, 0.95),
        "persisted_analysis_classification": persisted_classification,
        "current_analysis_classification": current_classification,
        "persisted_analysis_acceptance_status": persisted.get("acceptance_status"),
        "current_analysis_acceptance_status": current.get("acceptance_status"),
        "persisted_analysis_stale": persisted_classification != current_classification,
    }


def build_payload(run_dir: Path | None, *, sigr_exponent_r: float = SIGR_EXPONENT_R) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "safety_boundary": SAFETY_BOUNDARY,
        "sigr_exponent_r": sigr_exponent_r,
        "qdot_cap_rad_s": QDOT_CAP_RAD_S,
        "qdot_rail_margin_rad_s": QDOT_RAIL_MARGIN_RAD_S,
        "qdot_rail_threshold_rad_s": QDOT_RAIL_THRESHOLD_RAD_S,
        "synthetic": synthetic_payload(sigr_exponent_r=sigr_exponent_r),
    }
    if run_dir is not None:
        payload["csv_audit"] = csv_audit(run_dir)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, help="Optional completed bridge run directory to audit")
    parser.add_argument(
        "--sigr-exponent-r",
        type=float,
        default=SIGR_EXPONENT_R,
        help="Strict RNN sig^r exponent for synthetic cases; default follows production StrictRnnConfig",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON output path")
    parser.add_argument("--json", action="store_true", help="Print JSON payload to stdout")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 0.0 < args.sigr_exponent_r <= 1.0:
        raise SystemExit("--sigr-exponent-r must be in (0, 1]")
    payload = build_payload(args.run_dir, sigr_exponent_r=float(args.sigr_exponent_r))
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    if args.json or not args.output:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
