#!/usr/bin/env python3
"""Offline tests for matched original TASE RNN and QP baselines."""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_contact_qp import build  # noqa: E402
from tase_offline_baselines import (  # noqa: E402
    TaseJointSample,
    TaseOfflineConfig,
    TaseRnnBaseline,
    compare_joint_baselines,
)
from step5d_paper_outer_loop import (  # noqa: E402
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)


@pytest.fixture(scope="module")
def qp_library(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build(tmp_path_factory.mktemp("tase-offline-qp"))


def sample(*, xdot_c: np.ndarray, dt_s: float = 0.002) -> TaseJointSample:
    return TaseJointSample(
        jacobian=np.eye(6),
        xdot_c=xdot_c,
        omega_minus=np.full(6, -0.15),
        omega_plus=np.full(6, 0.15),
        dt_s=dt_s,
    )


def test_original_rnn_and_matched_qp_use_identical_task_and_bounds(qp_library: Path) -> None:
    task = np.array([0.025, -0.012, 0.006, 0.004, -0.003, 0.002])
    result = compare_joint_baselines([sample(xdot_c=task) for _ in range(1600)], qp_library=qp_library)

    assert result["scope"] == "offline_only_no_robot_io_or_physical_claim"
    assert result["shared_inputs"]["samples"] == 1600
    assert result["qp"]["max_equality_residual"] < 1e-6
    assert result["qp"]["max_bound_violation"] == 0.0
    assert result["rnn"]["max_bound_violation"] <= 1e-12
    assert result["rnn"]["final_equality_residual"] < 1e-3
    np.testing.assert_allclose(result["qp"]["qdots"][-1], task, atol=1e-6)


def test_rnn_state_persists_and_invalid_command_freezes_without_reset() -> None:
    solver = TaseRnnBaseline()
    task = sample(xdot_c=np.array([0.02, 0.0, 0.0, 0.0, 0.0, 0.0]))
    first = solver.step(task)
    state_before_invalid = (solver.theta_dot_state, solver.lambda_state)
    invalid = TaseJointSample(
        jacobian=task.jacobian,
        xdot_c=task.xdot_c,
        omega_minus=task.omega_minus,
        omega_plus=task.omega_plus,
        dt_s=task.dt_s,
        cmd_valid=False,
    )
    frozen = solver.step(invalid)

    assert not np.allclose(first.lambda_state, np.zeros(6))
    np.testing.assert_allclose(solver.theta_dot_state, state_before_invalid[0], atol=1e-12)
    np.testing.assert_allclose(solver.lambda_state, state_before_invalid[1], atol=1e-12)
    assert np.all(np.isfinite(frozen.theta_dot_state))


def test_qp_match_accepts_tilted_force_orientation_task(qp_library: Path) -> None:
    # Use the existing paper outer loop so force, unknown reaction normal, and
    # quaternion orientation compliance are the shared task source.
    outer = compute_step5d_outer_loop(
        Step5dOuterLoopConfig(kp=0.0, kf=0.0, ko=2.0, force_target_n=2.0),
        Step5dOuterLoopState(),
        Step5dOuterLoopInputs(
            tcp_pose_base=(0.0, 0.0, 0.0, 0.0, 0.04, 0.0),
            tcp_speed_base=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            force_tcp_n=(0.0, 0.0, -2.0),
            x_pd_base=(0.0, 0.0, 0.0),
            xdot_pd_base=(0.0, 0.0, 0.0),
            control_reaction_normal_base=(0.04, 0.05, -0.998),
            dt_s=0.002,
        ),
    )
    tilted_task = np.asarray(outer.xdot_c, dtype=float)
    result = compare_joint_baselines([sample(xdot_c=tilted_task)], qp_library=qp_library)

    assert "R_d" in outer.diagnostics
    assert "e_qua" in outer.diagnostics
    np.testing.assert_allclose(result["qp"]["qdots"][0], tilted_task, atol=1e-6)
    assert result["rnn"]["max_bound_violation"] <= 1e-12
    assert result["qp"]["max_equality_residual"] < 1e-6
