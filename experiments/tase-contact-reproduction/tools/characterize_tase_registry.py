#!/usr/bin/env python3
"""Bounded offline characterization through the common TASE registry.

The input trace is prescribed and deterministic.  Each method receives the
same calibrated UR10e pose/Jacobian, force/reference trace, outer-loop
configuration, and joint-velocity bounds.  Exceptions are recorded as
failure boundaries; outputs are never clipped or replaced with a fallback.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterable

import numpy as np


TOOLS = Path(__file__).resolve().parent
EXPERIMENT_ROOT = TOOLS.parent
REPO_ROOT = EXPERIMENT_ROOT.parents[1]
sys.path.insert(0, str(TOOLS))

from build_contact_qp import build as build_qp  # noqa: E402
from contact_method_registry import default_registry  # noqa: E402
from contact_yield_kinematics import RobotKinematics, load_kinematics  # noqa: E402
from contact_yield_math import so3_log  # noqa: E402
from step5d_paper_outer_loop import (  # noqa: E402
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)
from tase_method_adapters import DEFAULT_OUTER_CONFIG  # noqa: E402


METHOD_NAMES = ("TASE_RNN", "TASE_RNN_MATURE_MINUS", "TASE_QP")
CASE_NAMES = ("aligned_nominal", "orientation_task_stress")
BASE_Q = np.asarray((0.2, -1.1, 1.0, -1.5, 0.7, 0.3), dtype=float)
BOUNDS_LOWER = np.full(6, -0.15, dtype=float)
BOUNDS_UPPER = np.full(6, 0.15, dtype=float)
CASE_DEFINITIONS = {
    "aligned_nominal": {
        "purpose": "nominal feasible shared-task comparison",
        "normal": "-R_current[:,2] plus a small deterministic base-frame tilt",
        "interpretation": "bounded prescribed input; not contact evidence",
    },
    "orientation_task_stress": {
        "purpose": "retain original orientation/task feasibility stress input",
        "normal": "deterministic world-Z-near reaction against arbitrary BASE_Q orientation",
        "interpretation": "QP infeasibility is an input/task boundary, not a solver disadvantage",
    },
}


def _tuple(values: Any) -> tuple[float, ...]:
    return tuple(float(value) for value in np.asarray(values, dtype=float))


def _exception_payload(exc: BaseException, *, phase: str, step: int | None = None,
                       time_s: float | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "phase": phase,
        "type": type(exc).__name__,
        "message": str(exc),
    }
    if step is not None:
        payload["step_index"] = int(step)
    if time_s is not None:
        payload["time_s"] = float(time_s)
    message = str(exc).lower()
    if "bound" in message:
        payload["failure_class"] = "registry_output_or_input_bound_validation"
    elif "stopped" in message:
        payload["failure_class"] = "lifecycle"
    else:
        payload["failure_class"] = "registry_or_backend_exception"
    return payload


def _snapshot_equal(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, separators=(",", ":"), default=str) == json.dumps(
        right, sort_keys=True, separators=(",", ":"), default=str
    )


def _prescribed_sample(
    kinematics: RobotKinematics,
    *,
    case_name: str,
    index: int,
    time_s: float,
    dt_s: float,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Create one deterministic, non-contact prescribed input row."""

    if case_name not in CASE_NAMES:
        raise ValueError(f"unknown characterization case {case_name!r}")

    phase = 0.7 * time_s
    q = BASE_Q + np.asarray(
        (
            0.018 * math.sin(phase),
            0.014 * math.cos(0.81 * phase),
            0.012 * math.sin(0.63 * phase),
            0.010 * math.cos(0.57 * phase),
            0.008 * math.sin(0.49 * phase),
            0.006 * math.cos(0.43 * phase),
        ),
        dtype=float,
    )
    qdot = np.asarray(
        (
            0.018 * 0.7 * math.cos(phase),
            -0.014 * 0.81 * 0.7 * math.sin(0.81 * phase),
            0.012 * 0.63 * 0.7 * math.cos(0.63 * phase),
            -0.010 * 0.57 * 0.7 * math.sin(0.57 * phase),
            0.008 * 0.49 * 0.7 * math.cos(0.49 * phase),
            -0.006 * 0.43 * 0.7 * math.sin(0.43 * phase),
        ),
        dtype=float,
    )
    row = kinematics.pose_and_jacobian(q)
    rotation = np.asarray(row["rotation"], dtype=float)
    if case_name == "aligned_nominal":
        # Keep this prescribed normal close to the current TCP axis so the
        # common task is a nominal feasibility case under unchanged bounds.
        reaction = -rotation[:, 2] + np.asarray(
            (0.012 * math.sin(0.53 * phase), 0.009 * math.cos(0.47 * phase), 0.0),
            dtype=float,
        )
    else:
        # Preserve the original stress input: a world-Z normal against the
        # arbitrary BASE_Q orientation.  Its infeasibility is reported as an
        # orientation/task stress boundary, not as a solver ranking.
        reaction = np.asarray(
            (0.075 * math.sin(0.53 * phase), 0.060 * math.cos(0.47 * phase), -1.0),
            dtype=float,
        )
    reaction /= np.linalg.norm(reaction)
    measured_load = 4.65 + 0.45 * math.sin(0.91 * phase + 0.2)
    force_base = measured_load * reaction
    position = np.asarray(row["position_m"], dtype=float)
    position_offset = np.asarray(
        (0.0018 * math.sin(phase), 0.0012 * math.cos(0.83 * phase), 0.0006 * math.sin(0.61 * phase)),
        dtype=float,
    )
    reference_velocity = np.asarray(
        (
            0.0018 * 0.7 * math.cos(phase),
            -0.0012 * 0.83 * 0.7 * math.sin(0.83 * phase),
            0.0006 * 0.61 * 0.7 * math.cos(0.61 * phase),
        ),
        dtype=float,
    )
    reference_force = 5.0 + 0.35 * math.sin(0.91 * phase + 0.65)
    observation = {
        "time_s": float(time_s),
        "state_age_s": float(min(0.5 * dt_s, 0.079)),
        "position_m": _tuple(position),
        "rotation": rotation,
        "joint_position_rad": _tuple(q),
        "jacobian": np.asarray(row["jacobian"], dtype=float),
        "raw_force_base_n": _tuple(force_base),
        "raw_torque_base_nm": (0.0, 0.0, 0.0),
        "joint_velocity_lower": _tuple(BOUNDS_LOWER),
        "joint_velocity_upper": _tuple(BOUNDS_UPPER),
        "linear_velocity_base_m_s": _tuple(np.asarray(row["jacobian"], dtype=float)[:3, :] @ qdot),
        "angular_velocity_base_rad_s": _tuple(np.asarray(row["jacobian"], dtype=float)[3:, :] @ qdot),
    }
    reference = {
        "position_m": _tuple(position + position_offset),
        "velocity_m_s": _tuple(reference_velocity),
        "reference_force_n": float(reference_force),
        "phase": "prescribed_characterization",
        "path_time_s": float(time_s),
    }
    return observation, reference, {
        "measured_load_n": float(measured_load),
        "reference_force_n": float(reference_force),
        "normal_tilt_rad": float(math.acos(np.clip(float(np.dot(reaction, -rotation[:, 2])), -1.0, 1.0))),
        "jacobian_distance_from_identity": float(np.linalg.norm(np.asarray(row["jacobian"]) - np.eye(6))),
        "qdot_prescribed_norm_rad_s": float(np.linalg.norm(qdot)),
        "twist_consistency_norm": float(
            np.linalg.norm(np.asarray(row["jacobian"], dtype=float) @ qdot - np.concatenate((
                np.asarray(observation["linear_velocity_base_m_s"], dtype=float),
                np.asarray(observation["angular_velocity_base_rad_s"], dtype=float),
            )))
        ),
        "case_name": case_name,
        "reaction_normal_base": _tuple(reaction),
        "sample_index": float(index),
    }


def _trace_summary(
    *,
    residuals: list[float],
    bound_violations: list[float],
    saturations: list[int],
    qdots: list[tuple[float, ...]],
    solver_statuses: list[float],
    sign_metadata: dict[str, str],
) -> dict[str, Any]:
    def stats(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"first": None, "final": None, "max": None, "delta_final_minus_first": None}
        return {
            "first": float(values[0]),
            "final": float(values[-1]),
            "max": float(max(values)),
            "delta_final_minus_first": float(values[-1] - values[0]),
        }

    return {
        "accepted_steps": len(residuals),
        "residual_norm": stats(residuals),
        "bound_violation": stats(bound_violations),
        "saturation": {
            "steps_with_any_active_bound": int(sum(value > 0 for value in saturations)),
            "total_active_joint_samples": int(sum(saturations)),
            "max_active_joints_in_step": int(max(saturations, default=0)),
        },
        "solver_statuses": sorted({float(value) for value in solver_statuses}),
        "first_qdot": list(qdots[0]) if qdots else None,
        "final_qdot": list(qdots[-1]) if qdots else None,
        "sign_metadata": dict(sign_metadata),
    }


def _method_metadata(handle: Any) -> dict[str, Any]:
    backend = handle.backend
    payload: dict[str, Any] = {
        "registered_method": handle.spec.name,
        "solver_name": backend.solver_name,
        "variant": backend.variant,
        "offline_only": bool(backend.offline_only),
        "live_eligible": bool(backend.live_eligible),
        "outer_config": asdict(backend.outer_config),
    }
    sign = getattr(backend.config, "lambda_update_sign", None)
    if sign is not None:
        payload["lambda_update_sign"] = sign
    return payload


def _freeze_outer_trace(
    *,
    samples: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    dt_s: float,
    outer_config: Step5dOuterLoopConfig,
) -> dict[str, Any]:
    """Freeze the shared outer task before any solver sees the trace."""

    state = Step5dOuterLoopState()
    task_rows: list[dict[str, Any]] = []
    for index, (observation, reference, input_meta) in enumerate(samples):
        rotation = np.asarray(observation["rotation"], dtype=float)
        position = np.asarray(observation["position_m"], dtype=float)
        linear = np.asarray(observation["linear_velocity_base_m_s"], dtype=float)
        angular = np.asarray(observation["angular_velocity_base_rad_s"], dtype=float)
        force_base = np.asarray(observation["raw_force_base_n"], dtype=float)
        inputs = Step5dOuterLoopInputs(
            tcp_pose_base=_tuple(np.concatenate((position, so3_log(rotation)))),
            tcp_speed_base=_tuple(np.concatenate((linear, angular))),
            force_tcp_n=_tuple(rotation.T @ force_base),
            x_pd_base=_tuple(reference["position_m"]),
            xdot_pd_base=_tuple(reference["velocity_m_s"]),
            dt_s=float(dt_s),
            control_reaction_normal_base=tuple(input_meta["reaction_normal_base"]),
        )
        config = replace(outer_config, force_target_n=float(reference["reference_force_n"]))
        outer = compute_step5d_outer_loop(config, state, inputs)
        state = outer.next_state
        jacobian = np.asarray(observation["jacobian"], dtype=float)
        xdot_c = np.asarray(outer.xdot_c, dtype=float)
        singular_values = np.linalg.svd(jacobian, compute_uv=False)
        try:
            unconstrained_qdot = np.linalg.solve(jacobian, xdot_c)
            solve_failure = None
            unconstrained_bound_violation = float(
                max(
                    0.0,
                    float(np.max(BOUNDS_LOWER - unconstrained_qdot)),
                    float(np.max(unconstrained_qdot - BOUNDS_UPPER)),
                )
            )
        except np.linalg.LinAlgError as exc:
            unconstrained_qdot = None
            unconstrained_bound_violation = None
            solve_failure = _exception_payload(exc, phase="unconstrained_square_solve", step=index, time_s=float(observation["time_s"]))
        task_row = {
            "step_index": index,
            "time_s": float(observation["time_s"]),
            "desired_current_orientation_angle_rad": float(outer.diagnostics["outer_orientation_angle_rad"]),
            "xdot_c": _tuple(xdot_c),
            "jacobian_singular_values": _tuple(singular_values),
            "unconstrained_qdot": None if unconstrained_qdot is None else _tuple(unconstrained_qdot),
            "unconstrained_bound_violation": unconstrained_bound_violation,
            "unconstrained_within_bounds": (
                None
                if unconstrained_bound_violation is None
                else bool(unconstrained_bound_violation <= 1e-10)
            ),
            "solve_failure": solve_failure,
            "normal_tilt_rad": float(input_meta["normal_tilt_rad"]),
        }
        input_meta["frozen_task"] = task_row
        input_meta["shared_xdot_c"] = _tuple(xdot_c)
        task_rows.append(task_row)

    infeasible = [
        row for row in task_rows
        if row["unconstrained_within_bounds"] is False
    ]
    first = task_rows[0]
    worst = max(
        task_rows,
        key=lambda row: -1.0 if row["unconstrained_bound_violation"] is None else row["unconstrained_bound_violation"],
    )
    return {
        "first_step": first,
        "worst_unconstrained_bound_step": worst,
        "first_infeasible_unconstrained_step": infeasible[0] if infeasible else None,
        "feasible_unconstrained_steps": int(sum(row["unconstrained_within_bounds"] is True for row in task_rows)),
        "infeasible_unconstrained_steps": int(len(infeasible)),
        "max_orientation_angle_rad": float(max(row["desired_current_orientation_angle_rad"] for row in task_rows)),
        "max_singular_value": float(max(max(row["jacobian_singular_values"]) for row in task_rows)),
        "min_singular_value": float(min(min(row["jacobian_singular_values"]) for row in task_rows)),
        "max_twist_consistency_norm": float(max(item[2]["twist_consistency_norm"] for item in samples)),
    }


def _run_method(
    *,
    name: str,
    samples: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    dt_s: float,
    qp_library: Path,
    outer_config: Step5dOuterLoopConfig,
) -> dict[str, Any]:
    registry = default_registry()
    try:
        handle = registry.initialize(name, qp_library=qp_library, outer_config=outer_config)
    except Exception as exc:
        return {
            "method": name,
            "initialization_failure": _exception_payload(exc, phase="initialize"),
        }

    output: dict[str, Any] = {"method": name, "metadata": _method_metadata(handle)}
    residuals: list[float] = []
    bound_violations: list[float] = []
    saturations: list[int] = []
    qdots: list[tuple[float, ...]] = []
    statuses: list[float] = []
    task_deltas: list[float] = []
    sign_metadata: dict[str, str] = {}
    first_failure: dict[str, Any] | None = None

    for index, (observation, reference, _input_meta) in enumerate(samples):
        before = handle.snapshot()
        try:
            result = handle.step(observation, reference, dt_s)
        except Exception as exc:
            after = handle.snapshot()
            first_failure = _exception_payload(
                exc,
                phase="nominal_trace",
                step=index,
                time_s=float(observation["time_s"]),
            )
            first_failure["state_unchanged_after_registry_rollback"] = _snapshot_equal(before, after)
            break

        qdot = np.asarray(result["qdot_rad_s"], dtype=float)
        violation = float(
            max(
                0.0,
                float(np.max(BOUNDS_LOWER - qdot)),
                float(np.max(qdot - BOUNDS_UPPER)),
            )
        )
        solver_diag = result["diagnostics"].get("solver", {})
        active_mask = np.logical_or(
            np.isclose(qdot, BOUNDS_LOWER, atol=1e-7, rtol=0.0),
            np.isclose(qdot, BOUNDS_UPPER, atol=1e-7, rtol=0.0),
        )
        if isinstance(solver_diag.get("active_bounds_mask"), (list, tuple)):
            active_mask = np.asarray(solver_diag["active_bounds_mask"], dtype=bool)
        residuals.append(float(result["residual_norm"]))
        bound_violations.append(violation)
        saturations.append(int(np.count_nonzero(active_mask)))
        qdots.append(_tuple(qdot))
        statuses.append(float(result["solver_status"]))
        task_deltas.append(
            float(
                np.max(
                    np.abs(
                        np.asarray(result["xdot_c"], dtype=float)
                        - np.asarray(_input_meta["shared_xdot_c"], dtype=float)
                    )
                )
            )
        )
        for key in ("proj_input_form", "lambda_update_form"):
            if key in solver_diag:
                sign_metadata[key] = str(solver_diag[key])

    output["trace"] = _trace_summary(
        residuals=residuals,
        bound_violations=bound_violations,
        saturations=saturations,
        qdots=qdots,
        solver_statuses=statuses,
        sign_metadata=sign_metadata,
    )
    output["trace"]["shared_outer_task_match_max_abs"] = max(task_deltas, default=None)
    output["first_failure"] = first_failure

    invalid_observation, invalid_reference, _ = samples[min(len(samples) - 1, 1)]
    invalid_observation = dict(invalid_observation)
    invalid_observation["time_s"] = float(invalid_observation["time_s"]) + dt_s
    invalid_observation["state_age_s"] = 0.08
    before_invalid = handle.snapshot()
    try:
        handle.step(invalid_observation, invalid_reference, dt_s)
    except Exception as exc:
        invalid_result: dict[str, Any] = {
            "accepted": False,
            "exception": _exception_payload(exc, phase="invalid_freshness_input"),
            "state_unchanged": _snapshot_equal(before_invalid, handle.snapshot()),
        }
    else:
        invalid_result = {
            "accepted": True,
            "exception": None,
            "state_unchanged": False,
        }
    output["invalid_input_handling"] = invalid_result
    return output


def characterize(
    *,
    qp_library: Path | str,
    duration_s: float = 2.0,
    dt_s: float = 0.002,
    include_half_dt: bool = True,
    kinematics: RobotKinematics | None = None,
    outer_config: Step5dOuterLoopConfig = DEFAULT_OUTER_CONFIG,
    command: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Run the three methods on identical prescribed input traces."""

    qp_library = Path(qp_library).resolve(strict=True)
    duration_s = float(duration_s)
    dt_s = float(dt_s)
    if not math.isfinite(duration_s) or duration_s <= 0.0:
        raise ValueError("duration_s must be finite and positive")
    if not math.isfinite(dt_s) or dt_s <= 0.0 or dt_s > 0.004:
        raise ValueError("dt_s must be finite, positive, and at most 0.004")
    steps = int(round(duration_s / dt_s))
    if steps < 1 or not math.isclose(steps * dt_s, duration_s, rel_tol=0.0, abs_tol=1e-10):
        raise ValueError("duration_s must be an integer multiple of dt_s")
    kinematics = load_kinematics(require_ur10e=True) if kinematics is None else kinematics
    run_dts = [dt_s]
    if include_half_dt:
        run_dts.append(dt_s / 2.0)

    runs: dict[str, Any] = {}
    all_input_meta: list[dict[str, Any]] = []
    for case_name in CASE_NAMES:
        case_runs: dict[str, Any] = {}
        for run_dt in run_dts:
            run_steps = int(round(duration_s / run_dt))
            samples = [
                _prescribed_sample(
                    kinematics,
                    case_name=case_name,
                    index=index,
                    time_s=index * run_dt,
                    dt_s=run_dt,
                )
                for index in range(run_steps)
            ]
            input_meta = [item[2] for item in samples]
            all_input_meta.extend(input_meta)
            frozen_task = _freeze_outer_trace(
                samples=samples,
                dt_s=run_dt,
                outer_config=outer_config,
            )
            method_runs = {
                name: _run_method(
                    name=name,
                    samples=samples,
                    dt_s=run_dt,
                    qp_library=qp_library,
                    outer_config=outer_config,
                )
                for name in METHOD_NAMES
            }
            case_runs[f"dt_{run_dt:.6f}_s"] = {
                "dt_s": float(run_dt),
                "steps_requested": run_steps,
                "shared_input": {
                    "same_trace_for_all_methods": True,
                    "measured_load_n": {
                        "min": float(min(item["measured_load_n"] for item in input_meta)),
                        "max": float(max(item["measured_load_n"] for item in input_meta)),
                    },
                    "reference_force_n": {
                        "min": float(min(item["reference_force_n"] for item in input_meta)),
                        "max": float(max(item["reference_force_n"] for item in input_meta)),
                    },
                    "normal_tilt_rad": {
                        "max": float(max(item["normal_tilt_rad"] for item in input_meta)),
                    },
                    "jacobian_distance_from_identity": {
                        "max": float(max(item["jacobian_distance_from_identity"] for item in input_meta)),
                    },
                    "prescribed_qdot_norm_rad_s": {
                        "max": float(max(item["qdot_prescribed_norm_rad_s"] for item in input_meta)),
                    },
                    "twist_consistency_norm": {
                        "max": float(max(item["twist_consistency_norm"] for item in input_meta)),
                    },
                    "joint_velocity_bounds_rad_s": {
                        "lower": _tuple(BOUNDS_LOWER),
                        "upper": _tuple(BOUNDS_UPPER),
                    },
                },
                "frozen_task_diagnostics": frozen_task,
                "methods": method_runs,
            }
        runs[case_name] = {
            "definition": CASE_DEFINITIONS[case_name],
            "dt_runs": case_runs,
        }

    return {
        "schema": "tase-registry-characterization-v2",
        "scope": "offline_prescribed_input_no_robot_io_no_physical_claim",
        "physical_claim": False,
        "hardware_or_endpoint_action": False,
        "provenance": {
            "git_head": _git_head(),
            "command": list(command) if command is not None else None,
            "qp_library": str(qp_library),
            "kinematics": {
                "kind": kinematics.kind,
                "calibration_hash": kinematics.calibration_hash,
                "claim_scope": kinematics.claim_scope,
            },
            "source_files": [
                "experiments/tase-contact-reproduction/tools/characterize_tase_registry.py",
                "experiments/tase-contact-reproduction/tools/contact_method_registry.py",
                "experiments/tase-contact-reproduction/tools/tase_method_adapters.py",
                "experiments/tase-contact-reproduction/tools/tase_offline_baselines.py",
                "experiments/tase-contact-reproduction/tools/step5d_paper_outer_loop.py",
                "experiments/tase-contact-reproduction/tools/contact_yield_kinematics.py",
            ],
        },
        "equation_mapping": {
            "source_pdf_page": 6,
            "source_audit": "experiments/tase-contact-reproduction/report/tase-offline-baselines-v1/pdf-equation-parameter-audit.json",
            "eq23_printed_theta": "epsilon * dot(theta_dot_state) = -sigr(theta_dot_state - P_Omega(theta_dot_state - (theta_dot_state - J.T @ lambda_state)))",
            "eq23_simplified_theta": "epsilon * dot(theta_dot_state) = -sigr(theta_dot_state - P_Omega(J.T @ lambda_state))",
            "eq23_printed_lambda": "epsilon * dot(lambda_state) = J @ theta_dot_state - xdot_c",
            "projection_definition": "P_Omega(J.T @ lambda_state), element-wise box projection",
            "lambda_definition": "The local PDF mapping records Eq.23 lambda_state as the opposite sign of the standard Eq.21 Lagrange multiplier.",
            "implementation_boundary": "Printed-plus is the page-6 sign mapping; mature-minus is an explicit local adaptation. Neither is claimed as full original-TASE acceptance.",
            "uncertainty": "The inspected paper gives the continuous equations and proof, but does not prove this explicit-Euler UR10e 6-DOF bounded implementation for changing J and nonzero commands.",
        },
        "shared_contract": {
            "method_names": list(METHOD_NAMES),
            "outer_config": asdict(outer_config),
            "same_force_reference_bounds": True,
            "private_state_per_method": True,
            "input_generation": "two frozen deterministic prescribed cases: aligned nominal and retained orientation/task stress",
            "invalid_input": "state_age_s=0.08, expected common freshness rejection",
        },
        "base_q_rad": _tuple(BASE_Q),
        "case_definitions": CASE_DEFINITIONS,
        "runs": runs,
        "input_trace_summary": {
            "total_rows_across_dt_runs": len(all_input_meta),
            "max_normal_tilt_rad": float(max(item["normal_tilt_rad"] for item in all_input_meta)),
            "max_jacobian_distance_from_identity": float(
                max(item["jacobian_distance_from_identity"] for item in all_input_meta)
            ),
        },
        "interpretation": {
            "residual": "solver task-equality residual on prescribed xdot_c; not a physical force metric",
            "failure_boundary": "first common-registry exception is reported with rollback status; no clipping or fallback is applied",
            "comparison_limit": "A lower residual or later failure is not evidence of physical superiority or contact performance.",
        },
    }


def _git_head() -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-s", type=float, default=2.0)
    parser.add_argument("--dt-s", type=float, default=0.002)
    parser.add_argument("--no-half-dt", action="store_true")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--qp-library", type=Path)
    group.add_argument("--build-qp-dir", type=Path)
    args = parser.parse_args(argv)

    qp_library = args.qp_library
    if qp_library is None:
        assert args.build_qp_dir is not None
        qp_library = build_qp(args.build_qp_dir)
    result = characterize(
        qp_library=qp_library,
        duration_s=args.duration_s,
        dt_s=args.dt_s,
        include_half_dt=not args.no_half_dt,
        command=[sys.executable, str(Path(__file__).resolve()), *(argv or sys.argv[1:])],
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="ascii")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
