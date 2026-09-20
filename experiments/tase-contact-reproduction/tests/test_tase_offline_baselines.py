#!/usr/bin/env python3
"""Offline tests for the equation-aligned TASE RNN hypothesis and QP."""

from __future__ import annotations

import json
import importlib.util
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
    TaseQpBaseline,
    TaseRnnBaseline,
    compare_joint_baselines,
)
from step5c_strict_rnn import PaperTruthPendingError, StrictRnnConfig, StrictTaseRnnSolver  # noqa: E402
from step5d_paper_outer_loop import (  # noqa: E402
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)
from step5c_strict_rnn import sigr  # noqa: E402


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
    assert result["qp"]["max_valid_equality_residual"] < 1e-6
    assert result["qp"]["max_bound_violation"] == 0.0
    assert result["rnn"]["max_bound_violation"] <= 1e-12
    assert result["rnn"]["final_valid_equality_residual"] < 1e-3
    np.testing.assert_allclose(result["qp"]["qdots"][-1], task, atol=1e-6)


def test_offline_hypothesis_preserves_unresolved_truth_and_live_gate() -> None:
    truth_path = ROOT / "config" / "step5c_tase_offline_baseline_truth.json"
    payload = json.loads(truth_path.read_text(encoding="utf-8"))
    assert payload["offline_hypothesis"] is True
    assert payload["strict_rnn_enabled"] is False
    assert payload["pending_pdf_verify"]
    assert payload["sections"]["step5c_strict_contact"]["pending_pdf_verify"]
    assert payload["live_authorization"] == "blocked"

    with pytest.raises(PaperTruthPendingError):
        StrictTaseRnnSolver(StrictRnnConfig(paper_truth_path=truth_path))
    assert TaseRnnBaseline().snapshot()["theta_dot_state"] == [0.0] * 6


def test_sigr_matches_local_legacy_finite_time_implementation() -> None:
    legacy_path = (
        ROOT.parents[1]
        / "experiments/archive/legacy/tase-mujoco-reproduction-2026-05-23/src/tase_repro/finite_time.py"
    )
    spec = importlib.util.spec_from_file_location("legacy_tase_finite_time", legacy_path)
    assert spec is not None and spec.loader is not None
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    values = np.array([-2.0, -0.25, 0.0, 0.4, 3.0])
    np.testing.assert_allclose(sigr(values, 0.2), legacy.sigr(values, 0.2), atol=1e-12)


def test_invalid_samples_hold_both_states_and_are_excluded_from_residuals(qp_library: Path) -> None:
    task = np.array([0.01, -0.004, 0.002, 0.001, 0.0, -0.001])
    valid = sample(xdot_c=task)
    invalid = TaseJointSample(
        jacobian=valid.jacobian,
        xdot_c=valid.xdot_c,
        omega_minus=valid.omega_minus,
        omega_plus=valid.omega_plus,
        dt_s=valid.dt_s,
        cmd_valid=False,
    )
    result = compare_joint_baselines([valid, invalid, valid], qp_library=qp_library)

    assert result["shared_inputs"]["samples"] == 3
    assert result["shared_inputs"]["valid_samples"] == 2
    assert result["shared_inputs"]["invalid_samples"] == 1
    assert len(result["rnn"]["qdots"]) == 3
    assert len(result["qp"]["qdots"]) == 3
    np.testing.assert_allclose(result["rnn"]["qdots"][1], result["rnn"]["qdots"][0], atol=1e-12)
    np.testing.assert_allclose(result["qp"]["qdots"][1], result["qp"]["qdots"][0], atol=1e-12)


def test_snapshot_restore_replays_rnn_and_qp_states(qp_library: Path) -> None:
    config = TaseOfflineConfig()
    rnn = TaseRnnBaseline(config)
    qp = TaseQpBaseline(qp_library, config)
    J = np.eye(6)
    J[0, 1] = 0.04
    J[1, 0] = -0.03
    J[2, 3] = 0.02
    samples = [
        TaseJointSample(
            jacobian=J,
            xdot_c=np.array([0.004, -0.003, 0.002, 0.001, -0.002, 0.001]),
            omega_minus=np.full(6, -0.15),
            omega_plus=np.full(6, 0.15),
            dt_s=0.002,
        )
        for _ in range(8)
    ]
    for sample_item in samples[:3]:
        rnn.step(sample_item)
        qp.step(sample_item)
    rnn_snapshot = rnn.snapshot()
    qp_snapshot = qp.snapshot()
    assert set(rnn_snapshot) == {"theta_dot_state", "lambda_state"}
    assert set(qp_snapshot) == {"x", "y"}

    def replay() -> tuple[list[tuple[float, ...]], list[tuple[float, ...]]]:
        rnn_qdots = []
        qp_qdots = []
        for sample_item in samples[3:]:
            rnn_qdots.append(rnn.step(sample_item).theta_dot_state)
            qp_qdots.append(qp.step(sample_item).qdot)  # type: ignore[union-attr]
        return rnn_qdots, qp_qdots

    first_replay = replay()
    rnn.restore(rnn_snapshot)
    qp.restore(qp_snapshot)
    second_replay = replay()
    np.testing.assert_allclose(first_replay[0], second_replay[0], atol=1e-12)
    np.testing.assert_allclose(first_replay[1], second_replay[1], atol=1e-9)


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
    assert result["qp"]["max_valid_equality_residual"] < 1e-6


def test_same_outer_loop_feeds_both_on_bounded_nonidentity_jacobian_changes(qp_library: Path) -> None:
    state = Step5dOuterLoopState()
    samples = []
    jacobians = []
    for index in range(80):
        phase = index * 0.07
        jacobian = np.eye(6)
        jacobian[0, 1] = 0.04 + 0.01 * np.sin(phase)
        jacobian[1, 0] = -0.03 + 0.008 * np.cos(phase)
        jacobian[2, 3] = 0.02 * np.sin(phase)
        jacobian[3, 2] = -0.015 * np.cos(phase)
        jacobians.append(jacobian)
        outer = compute_step5d_outer_loop(
            Step5dOuterLoopConfig(kp=0.5, kf=0.0, ko=1.5, force_target_n=2.0),
            state,
            Step5dOuterLoopInputs(
                tcp_pose_base=(0.0, 0.0, 0.0, 0.0, 0.02 * np.sin(phase), 0.0),
                tcp_speed_base=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                force_tcp_n=(0.0, 0.0, -2.0),
                x_pd_base=(0.002 * np.sin(phase), 0.001 * np.cos(phase), 0.0),
                xdot_pd_base=(0.0005 * np.cos(phase), -0.0005 * np.sin(phase), 0.0),
                control_reaction_normal_base=(0.04, 0.05, -0.998),
                dt_s=0.002,
            ),
        )
        state = outer.next_state
        samples.append(
            TaseJointSample(
                jacobian=jacobian,
                xdot_c=np.asarray(outer.xdot_c),
                omega_minus=np.full(6, -0.15),
                omega_plus=np.full(6, 0.15),
                dt_s=0.002,
            )
        )

    result = compare_joint_baselines(samples, qp_library=qp_library)
    assert any(not np.allclose(jacobian, np.eye(6)) for jacobian in jacobians)
    assert result["shared_inputs"]["valid_samples"] == 80
    assert result["qp"]["max_valid_equality_residual"] < 1e-6
    assert result["rnn"]["max_bound_violation"] <= 1e-12
