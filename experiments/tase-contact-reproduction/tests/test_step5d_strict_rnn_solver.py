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
        self.assertEqual(diag.lambda_update_form, "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)")
        np.testing.assert_allclose(diag.proj_input, expected)
        self.assertFalse(np.allclose(diag.proj_input, old_bad_form))

    def test_nonzero_command_converges_with_positive_projection_negative_lambda_update(self) -> None:
        solver = self.make_solver()
        xdot_c = np.array([0.05, 0.0, 0.0, 0.0, 0.0, 0.0])
        residuals = []
        for _ in range(2000):
            diag = solver.step(
                J=np.eye(6),
                xdot_c=xdot_c,
                omega_minus=np.full(6, -0.15),
                omega_plus=np.full(6, 0.15),
                dt=0.002,
                epsilon=0.022,
                r=0.2,
            )
            residuals.append(diag.constraint_residual_norm)

        self.assertEqual(diag.proj_input_form, "J.T @ lambda_state")
        self.assertEqual(diag.lambda_update_form, "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)")
        self.assertLess(residuals[-1], residuals[0])
        self.assertLess(residuals[-1], 1e-3)
        self.assertFalse(any(diag.active_bounds_mask))
        np.testing.assert_allclose(diag.theta_dot_state, xdot_c, atol=1e-3)

    def test_finite_time_update_does_not_overshoot_projection_bound(self) -> None:
        solver = self.make_solver()
        solver.lambda_state = np.array([10.0, -10.0, 10.0, -10.0, 10.0, -10.0])
        lower = np.full(6, -0.30)
        upper = np.full(6, 0.30)
        diag = solver.step(
            J=np.eye(6),
            xdot_c=np.zeros(6),
            omega_minus=lower,
            omega_plus=upper,
            dt=0.020,
            epsilon=0.022,
            r=0.2,
        )
        self.assertLessEqual(max(abs(value) for value in diag.theta_dot_state), 0.30)
        np.testing.assert_allclose(diag.theta_dot_state, diag.projected)
        self.assertTrue(any(diag.theta_dot_update_limited_mask))

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

    def test_warm_start_tracks_command_from_first_tick(self) -> None:
        solver = self.make_solver()
        # Linear/angular-coupled J: from zero state the transient Cartesian
        # velocity follows J@J.T@xdot_c, whose x component opposes the +x press
        # when the command is angular-dominant (the v28 speedj_rnn_live shape).
        jacobian = np.eye(6)
        jacobian[0, 4] = -0.8
        xdot_c = np.array([1e-4, 0.0, 0.0, 0.0, 6e-3, 0.0])
        lower = np.full(6, -0.15)
        upper = np.full(6, 0.15)
        self.assertLess(float((jacobian @ jacobian.T @ xdot_c)[0]), 0.0)

        cold = solver
        cold_press = []
        for _ in range(50):
            diag = cold.step(J=jacobian, xdot_c=xdot_c, omega_minus=lower, omega_plus=upper, dt=0.002, epsilon=0.022, r=0.2)
            cold_press.append(float((jacobian @ np.asarray(diag.theta_dot_state))[0]))
        self.assertLess(min(cold_press), -1e-5)

        warm = self.make_solver()
        warm.warm_start(J=jacobian, xdot_c=xdot_c, omega_minus=lower, omega_plus=upper)
        first = warm.step(J=jacobian, xdot_c=xdot_c, omega_minus=lower, omega_plus=upper, dt=0.002, epsilon=0.022, r=0.2)
        twist = jacobian @ np.asarray(first.theta_dot_state)
        np.testing.assert_allclose(twist, xdot_c, atol=1e-6)
        self.assertGreaterEqual(float(twist[0]), 0.0)
        for _ in range(50):
            diag = warm.step(J=jacobian, xdot_c=xdot_c, omega_minus=lower, omega_plus=upper, dt=0.002, epsilon=0.022, r=0.2)
            self.assertGreater(float((jacobian @ np.asarray(diag.theta_dot_state))[0]), -1e-6)

    def test_warm_start_clips_theta_dot_to_omega_bounds(self) -> None:
        solver = self.make_solver()
        jacobian = np.eye(6)
        xdot_c = np.array([0.05, -0.05, 0.0, 0.0, 0.0, 0.0])
        lower = np.full(6, -0.02)
        upper = np.full(6, 0.02)
        solver.warm_start(
            J=jacobian,
            xdot_c=xdot_c,
            omega_minus=lower,
            omega_plus=upper,
        )
        expected_lambda = np.linalg.solve(jacobian @ jacobian.T + (1e-4**2) * np.eye(6), xdot_c)
        expected_theta = np.clip(jacobian.T @ expected_lambda, lower, upper)
        np.testing.assert_allclose(solver.lambda_state, expected_lambda)
        np.testing.assert_allclose(solver.theta_dot_state, expected_theta)
        self.assertGreater(float(np.linalg.norm(jacobian @ solver.theta_dot_state - xdot_c)), 0.04)

        diag = solver.step(
            J=jacobian,
            xdot_c=xdot_c,
            omega_minus=lower,
            omega_plus=upper,
            dt=0.002,
            epsilon=0.022,
            r=0.2,
        )
        self.assertEqual(diag.active_bounds_mask[:2], (True, True))
        self.assertGreater(diag.constraint_residual_norm, 0.04)

    def test_warm_start_rejects_nonfinite_inputs(self) -> None:
        solver = self.make_solver()
        with self.assertRaises(ValueError):
            solver.warm_start(
                J=np.full((6, 6), np.nan),
                xdot_c=np.zeros(6),
                omega_minus=np.full(6, -0.15),
                omega_plus=np.full(6, 0.15),
            )

    def test_reset_state_clears_theta_dot_and_lambda(self) -> None:
        solver = self.make_solver()
        solver.theta_dot_state = np.array([0.05, -0.04, 0.03, -0.02, 0.01, -0.005])
        solver.lambda_state = np.array([2.0, -3.0, 1.5, -1.0, 0.5, -0.25])

        solver.reset_state()

        np.testing.assert_allclose(solver.theta_dot_state, np.zeros(6), atol=1e-12)
        np.testing.assert_allclose(solver.lambda_state, np.zeros(6), atol=1e-12)

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
        self.assertEqual(
            result.diagnostics["lambda_update_form"],
            "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
        )

    def test_step_source_has_no_inverse_or_dls_fallback(self) -> None:
        source = inspect.getsource(strict_rnn.StrictTaseRnnSolver.step).lower()
        forbidden = ["pinv", "lstsq", "np.linalg.solve", "scipy", "osqp", "dls", "ik"]
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
