"""Offline first-entry command-jump diagnostic for the yield controller.

This exercises the current controller against synthetic Home observations only.
It compares the first entry qdot with the immediately preceding baseline qdot
under the existing host slew and TP acceleration envelopes. It is not a
physical stationarity or hardware qualification result.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np

from contact_yield_math import projector_tangent
from contact_yield_protocol import QP_LIBRARY_PATH
from contact_yield_runner import make_system
from contact_yield_live_writer import native_motion_profile


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_PATH = EXPERIMENT_ROOT / "tools/contact_yield_controller.py"
DT_S = 0.002


def _run_case(
    name: str,
    *,
    force: tuple[float, float, float],
    displaced: tuple[float, float, float],
    estimator_parameters: dict[str, object] | None,
    warm_steps: int,
) -> dict[str, object]:
    controller, plant, origin = make_system(
        method="SFC",
        material="stiff_low_mu",
        dt_s=DT_S,
        timeline="diagnostic",
        qp_library=QP_LIBRARY_PATH,
        estimator_parameters=estimator_parameters or {},
    )
    try:
        baseline: dict[str, object] | None = None
        for tick in range(warm_steps):
            observation = dict(plant.observe()["observation"])
            observation["time_s"] = tick * DT_S
            observation["raw_force_base_n"] = force
            observation["position_m"] = np.asarray(origin) + np.asarray(displaced)
            observation["linear_velocity_base_m_s"] = (0.0, 0.0, 0.0)
            baseline = controller.step(
                observation,
                {
                    "phase": "baseline",
                    "path_time_s": None,
                    "force_n": 5.0,
                    "position_m": tuple(origin),
                    "velocity_m_s": (0.0, 0.0, 0.0),
                },
                DT_S,
            )

        assert baseline is not None
        entry_reference = controller.task.entry_reference(0.0)
        observation = dict(plant.observe()["observation"])
        observation["time_s"] = warm_steps * DT_S
        observation["raw_force_base_n"] = force
        observation["position_m"] = np.asarray(origin) + np.asarray(displaced)
        observation["linear_velocity_base_m_s"] = (0.0, 0.0, 0.0)
        entry = controller.step(
            observation,
            {
                "phase": "entry",
                "path_time_s": None,
                "force_n": 5.0,
                "position_m": tuple(origin) + np.asarray(entry_reference["position_m"]),
                "velocity_m_s": entry_reference["velocity_m_s"],
            },
            DT_S,
        )
        baseline_qdot = np.asarray(baseline["qdot_rad_s"], dtype=float)
        entry_qdot = np.asarray(entry["qdot_rad_s"], dtype=float)
        jump = entry_qdot - baseline_qdot
        baseline_normal = np.asarray(baseline["inward_normal_base"], dtype=float)
        entry_normal = np.asarray(entry["inward_normal_base"], dtype=float)
        baseline_tangent = projector_tangent(baseline_normal)
        entry_tangent = projector_tangent(entry_normal)
        return {
            "case": name,
            "warm_steps": warm_steps,
            "displaced_m": list(displaced),
            "baseline_command_tangent_m_s": float(
                np.linalg.norm(baseline_tangent @ np.asarray(baseline["twist_base"][:3]))
            ),
            "entry_command_tangent_m_s": float(
                np.linalg.norm(entry_tangent @ np.asarray(entry["twist_base"][:3]))
            ),
            "entry_native_tangent_m_s": float(
                np.linalg.norm(entry["native_law_tangent_velocity_m_s"])
            ),
            "baseline_qdot_rad_s": baseline_qdot.tolist(),
            "entry_qdot_rad_s": entry_qdot.tolist(),
            "jump_qdot_rad_s": jump.tolist(),
            "max_abs_jump_rad_s": float(np.max(np.abs(jump))),
            "l2_jump_rad_s": float(np.linalg.norm(jump)),
        }
    finally:
        controller.close()


def run() -> dict[str, object]:
    profile = native_motion_profile()
    host_slew = float(profile.host_qdot_slew_rad_s2)
    tp_acceleration = float(profile.tp_speedj_accel_rad_s2)
    host_delta = host_slew * DT_S
    tp_delta = tp_acceleration * DT_S
    cases = [
        _run_case(
            "warm_tilted_baseline",
            force=(4.0, 0.0, 5.0),
            displaced=(0.0, 0.0, 0.0),
            estimator_parameters={
                "initial_inward_normal_base": (0.2, 0.1, -math.sqrt(0.95)),
            },
            warm_steps=3,
        ),
        _run_case(
            "displaced_position",
            force=(0.0, 0.0, 5.0),
            displaced=(0.002, 0.0, 0.0),
            estimator_parameters=None,
            warm_steps=1,
        ),
    ]
    for case in cases:
        case["host_slew_rad_s2"] = host_slew
        case["host_delta_limit_rad_s"] = host_delta
        case["tp_acceleration_rad_s2"] = tp_acceleration
        case["tp_delta_limit_rad_s"] = tp_delta
        case["host_admission_pass"] = bool(case["max_abs_jump_rad_s"] <= host_delta + 1e-12)
        case["tp_acceleration_pass"] = bool(case["max_abs_jump_rad_s"] <= tp_delta + 1e-12)
    return {
        "schema": "ur10e.contact-yield-entry-jump-v1",
        "claim_scope": (
            "offline synthetic command-law diagnostic only; no physical stationarity, "
            "force prediction, or hardware qualification"
        ),
        "controller_sha256": hashlib.sha256(CONTROLLER_PATH.read_bytes()).hexdigest(),
        "dt_s": DT_S,
        "admission": {
            "host_policy": "existing runtime command_history intersection",
            "host_qdot_slew_rad_s2": host_slew,
            "host_delta_limit_rad_s": host_delta,
            "tp_speedj_acceleration_rad_s2": tp_acceleration,
            "tp_delta_limit_rad_s": tp_delta,
            "limits_relaxed": False,
        },
        "initialization_assumption": (
            "fresh controller per case; baseline ticks advance native state; "
            "baseline tangent command is suppressed; first entry resumes native response"
        ),
        "physical_limitation": (
            "passing qdot jump admission does not guarantee stationary physical XY; "
            "baseline has no lateral restoring authority and disturbances or estimated-normal tilt can move TCP XY"
        ),
        "cases": cases,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    encoded = json.dumps(run(), indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
        print(args.output)


if __name__ == "__main__":
    main()
