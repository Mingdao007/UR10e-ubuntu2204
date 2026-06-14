#!/usr/bin/env python3
"""Offline checks for the Step5d strict TASE RNN solver body."""

from __future__ import annotations

import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5c_strict_rnn as strict_rnn  # noqa: E402
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver, project_omega, sigr  # noqa: E402


def verified_truth_file() -> tempfile.NamedTemporaryFile[str]:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False)
    json.dump({"strict_rnn_enabled": True, "pending_pdf_verify": [], "sections": {}}, handle)
    handle.close()
    return handle


class Step5dStrictRnnSolverTest(unittest.TestCase):
    def make_solver(self) -> StrictTaseRnnSolver:
        truth = verified_truth_file()
        self.addCleanup(lambda: Path(truth.name).unlink(missing_ok=True))
        return StrictTaseRnnSolver(StrictRnnConfig(paper_truth_path=Path(truth.name)))

    def test_sigr_matches_paper_power_sign_function(self) -> None:
        self.assertAlmostEqual(float(sigr(1.0, 0.5)), 1.0, places=12)
        self.assertAlmostEqual(float(sigr(-2.0, 0.2)), -(2.0**0.2), places=12)
        self.assertEqual(float(sigr(0.0, 0.5)), 0.0)
        self.assertNotEqual(float(sigr(2.0, 0.5)), float(np.clip(2.0, -0.15, 0.15)))
        np.testing.assert_allclose(sigr(np.array([4.0, -9.0]), 0.5), np.array([2.0, -3.0]))

    def test_projection_is_elementwise_saturation(self) -> None:
        projected = project_omega(
            [0.2, -0.2, 0.03, -0.04, 0.0, 0.5],
            [-0.15, -0.10, -0.02, -0.05, -0.01, -0.03],
            [0.10, 0.15, 0.02, 0.05, 0.01, 0.03],
        )
        np.testing.assert_allclose(projected, [0.10, -0.10, 0.02, -0.04, 0.0, 0.03])

    def test_projection_input_is_positive_j_transpose_lambda(self) -> None:
        solver = self.make_solver()
        solver.theta_dot_state = np.array([0.01, 0.02, 0.0, -0.01, 0.0, 0.03])
        solver.lambda_state = np.array([0.1, -0.2, 0.05, 0.0, -0.1, 0.3])
        jacobian = np.diag([1.0, 2.0, -1.0, 0.5, -2.0, 3.0])

        diag = solver.step(
            J=jacobian,
            xdot_c=np.zeros(6),
            omega_minus=np.full(6, -1.0),
            omega_plus=np.full(6, 1.0),
            dt=0.002,
            epsilon=0.022,
            r=0.2,
        )

        expected = jacobian.T @ np.array([0.1, -0.2, 0.05, 0.0, -0.1, 0.3])
        old_bad_form = np.array([0.01, 0.02, 0.0, -0.01, 0.0, 0.03]) - expected
        self.assertEqual(diag.proj_input_form, "J.T @ lambda_state")
        np.testing.assert_allclose(diag.proj_input, expected)
        self.assertFalse(np.allclose(diag.proj_input, old_bad_form))

    def test_lambda_state_persists_across_ticks(self) -> None:
        solver = self.make_solver()
        jacobian = np.eye(6)
        xdot_c = np.array([0.05, 0.0, 0.0, 0.0, 0.0, 0.0])
        diag1 = solver.step(
            J=jacobian,
            xdot_c=xdot_c,
            omega_minus=np.full(6, -0.15),
            omega_plus=np.full(6, 0.15),
            dt=0.002,
            epsilon=0.022,
            r=0.2,
        )
        diag2 = solver.step(
            J=jacobian,
            xdot_c=xdot_c,
            omega_minus=np.full(6, -0.15),
            omega_plus=np.full(6, 0.15),
            dt=0.002,
            epsilon=0.022,
            r=0.2,
        )
        self.assertFalse(np.allclose(diag1.lambda_state, np.zeros(6)))
        self.assertFalse(np.allclose(diag1.lambda_state, diag2.lambda_state))

    def test_zero_command_zero_state_is_fixed_point(self) -> None:
        solver = self.make_solver()
        for _ in range(500):
            diag = solver.step(
                J=np.eye(6),
                xdot_c=np.zeros(6),
                omega_minus=np.full(6, -0.15),
                omega_plus=np.full(6, 0.15),
                dt=0.002,
                epsilon=0.022,
                r=1.0,
            )
        self.assertLess(np.linalg.norm(diag.theta_dot_state), 1e-4)
        self.assertLess(diag.constraint_residual_norm, 1e-4)
        self.assertLess(np.linalg.norm(diag.lambda_state), 1e-4)

    def test_freeze_keeps_state_unchanged(self) -> None:
        solver = self.make_solver()
        solver.theta_dot_state = np.array([0.05] * 6)
        solver.lambda_state = np.array([0.01] * 6)
        theta_before = solver.theta_dot_state.copy()
        lambda_before = solver.lambda_state.copy()
        solver.step(
            J=np.eye(6),
            xdot_c=np.ones(6),
            omega_minus=np.full(6, -0.15),
            omega_plus=np.full(6, 0.15),
            dt=0.002,
            cmd_valid=False,
        )
        np.testing.assert_allclose(solver.theta_dot_state, theta_before)
        np.testing.assert_allclose(solver.lambda_state, lambda_before)
        solver.freeze()
        np.testing.assert_allclose(solver.theta_dot_state, theta_before)
        np.testing.assert_allclose(solver.lambda_state, lambda_before)

    def test_solve_uses_stateful_step_when_truth_is_verified(self) -> None:
        solver = self.make_solver()
        result = solver.solve(
            actual_q=[0.0] * 6,
            actual_qd=[0.0] * 6,
            target_state={
                "J": np.eye(6),
                "xdot_c": np.zeros(6),
                "omega_minus": np.full(6, -0.15),
                "omega_plus": np.full(6, 0.15),
                "dt": 0.002,
                "epsilon": 0.022,
                "r": 0.2,
            },
        )
        self.assertEqual(len(result.qdot), 6)
        self.assertEqual(result.solver_status, 40.0)
        self.assertIn("proj_input", result.diagnostics)
        self.assertEqual(result.diagnostics["proj_input_form"], "J.T @ lambda_state")

    def test_step_source_has_no_inverse_or_dls_fallback(self) -> None:
        source = inspect.getsource(strict_rnn.StrictTaseRnnSolver.step).lower()
        forbidden = ["pinv", "lstsq", "np.linalg.solve", "scipy", "osqp", "dls", "ik"]
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
