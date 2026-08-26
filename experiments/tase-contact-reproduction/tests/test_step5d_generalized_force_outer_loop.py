#!/usr/bin/env python3
"""Focused offline tests for the generalized force outer loop."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np
from scipy.linalg import solve_continuous_lyapunov


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_generalized_force_outer_loop import (  # noqa: E402
    GeneralizedForceConfigV1,
    GeneralizedForceInputsV1,
    GeneralizedForceStateV1,
    NORMAL_MODE_MEASURED_ONLY,
    force_rate_shape,
    nonlinear_integral_term,
    step_generalized_force_outer_loop,
)
from step5d_generalized_force_stability import (  # noqa: E402
    check_common_lyapunov,
    force_state_matrix,
    interval_vertices,
    sampled_period_bound,
    search_common_lyapunov,
    uub_radius,
)


class GeneralizedForceOuterLoopTest(unittest.TestCase):
    def inputs(
        self,
        *,
        force=(0.0, 0.0, -2.0),
        position=(0.0, 0.0, 0.0),
        desired_position=(0.1, 0.0, 0.0),
        desired_velocity=(0.0, 0.0, 0.0),
        reaction=(0.0, 0.0, -1.0),
        dt=0.1,
        valid=True,
    ) -> GeneralizedForceInputsV1:
        return GeneralizedForceInputsV1(
            tcp_position_base=position,
            force_base_n=force,
            desired_position_base=desired_position,
            desired_velocity_base=desired_velocity,
            dt_s=dt,
            reaction_normal_base=reaction,
            command_valid=valid,
        )

    def test_nonlinear_integral_is_odd_and_sector_bounded(self) -> None:
        values = [0.05, 0.5, 2.0]
        positive = [
            nonlinear_integral_term(
                value,
                linear_gain_m_s_per_n_s=2.0,
                quadratic_ratio=3.0,
                transition_n_s=0.5,
            )
            for value in values
        ]
        for value, term in zip(values, positive, strict=True):
            negative = nonlinear_integral_term(
                -value,
                linear_gain_m_s_per_n_s=2.0,
                quadratic_ratio=3.0,
                transition_n_s=0.5,
            )
            self.assertAlmostEqual(term, -negative, places=12)
            self.assertGreaterEqual(term / value, 2.0)
            self.assertLessEqual(term / value, 8.0)

    def test_force_rate_is_bounded_odd_and_dissipative(self) -> None:
        for rate in np.linspace(-20.0, 20.0, 41):
            shaped = force_rate_shape(rate, scale_n_per_s=2.0, exponent=2.0)
            opposite = force_rate_shape(-rate, scale_n_per_s=2.0, exponent=2.0)
            self.assertAlmostEqual(shaped, -opposite, places=12)
            self.assertLessEqual(abs(shaped), 1.0 + 1e-12)
            self.assertGreaterEqual(rate * shaped, -1e-12)

    def test_force_rate_deadband_gates_small_measured_variation(self) -> None:
        self.assertEqual(
            force_rate_shape(
                0.4,
                scale_n_per_s=1.0,
                exponent=1.0,
                deadband_n_per_s=0.5,
            ),
            0.0,
        )
        self.assertGreater(
            force_rate_shape(
                1.0,
                scale_n_per_s=1.0,
                exponent=1.0,
                deadband_n_per_s=0.5,
            ),
            0.0,
        )

    def test_zero_extensions_match_force_pid_euler_step(self) -> None:
        config = GeneralizedForceConfigV1(
            force_target_n=5.0,
            force_p_gain_m_s2_per_n=0.2,
            force_i_gain_m_s2_per_n_s=0.3,
            force_damping_s_inv=1.5,
            nonlinear_i_ratio=0.0,
            force_rate_gain_m_s2=0.0,
            normal_mode="provided",
            position_gain_s_inv=0.0,
            normal_velocity_limit_m_s=10.0,
        )
        state = GeneralizedForceStateV1(
            integral_error_n_s=0.4,
            normal_velocity_m_s=0.1,
            filtered_force_n=2.0,
            force_filter_initialized=True,
            normal_estimate_base=(0.0, 0.0, -1.0),
        )
        output = step_generalized_force_outer_loop(config, state, self.inputs(dt=0.1))
        error = 3.0
        integral = 0.4 + error * 0.1
        acceleration = 0.2 * error + 0.3 * integral - 1.5 * 0.1
        expected_velocity = 0.1 + acceleration * 0.1
        self.assertAlmostEqual(output.force_error_n, error, places=12)
        self.assertAlmostEqual(output.normal_velocity_m_s, expected_velocity, places=12)
        np.testing.assert_allclose(output.xdot_base_m_s, [0.0, 0.0, expected_velocity], atol=1e-12)

    def test_measured_only_normal_is_unit_and_rate_limited(self) -> None:
        config = GeneralizedForceConfigV1(
            normal_mode=NORMAL_MODE_MEASURED_ONLY,
            normal_filter_tau_s=0.01,
            normal_rate_limit_rad_s=0.2,
            position_gain_s_inv=0.0,
            normal_velocity_limit_m_s=10.0,
        )
        state = GeneralizedForceStateV1()
        first = step_generalized_force_outer_loop(config, state, self.inputs(dt=0.01))
        second = step_generalized_force_outer_loop(
            config,
            first.next_state,
            self.inputs(force=(-2.0, 0.0, -2.0), dt=0.01),
        )
        normal = np.asarray(second.diagnostics["normal_base"], dtype=float)
        self.assertAlmostEqual(float(np.linalg.norm(normal)), 1.0, places=12)
        self.assertLessEqual(float(second.diagnostics["normal_rate_rad_s"]), 0.2 + 1e-10)

    def test_invalid_input_freezes_command_and_state(self) -> None:
        state = GeneralizedForceStateV1(integral_error_n_s=0.7, normal_velocity_m_s=0.02)
        output = step_generalized_force_outer_loop(
            GeneralizedForceConfigV1(), state, self.inputs(valid=False)
        )
        self.assertFalse(output.command_valid)
        self.assertEqual(output.next_state, state)
        np.testing.assert_allclose(output.xdot_base_m_s, [0.0, 0.0, 0.0], atol=1e-12)

    def test_lyapunov_certificate_and_sampled_bound_are_recomputed(self) -> None:
        matrix = force_state_matrix(
            effective_stiffness_n_per_m=100.0,
            integral_slope_m_s2_per_n_s=0.1,
            force_p_gain_m_s2_per_n=1.0 / 12.0,
            force_damping_s_inv=550.0 / 12.0,
        )
        self.assertTrue(np.all(np.real(np.linalg.eigvals(matrix)) < 0.0))
        P = solve_continuous_lyapunov(matrix.T, -np.eye(3))
        certificate = check_common_lyapunov(P, (matrix,))
        self.assertTrue(certificate.feasible)
        period_bound = sampled_period_bound(P, (matrix,))
        self.assertGreater(period_bound, 0.0)
        sampled = check_common_lyapunov(
            P,
            (matrix,),
            sample_period_s=period_bound * 0.25,
        )
        self.assertTrue(sampled.feasible)
        self.assertGreater(
            uub_radius(
                P,
                np.eye(3),
                disturbance_gain=0.2,
                disturbance_bound=0.1,
            ),
            0.0,
        )

    def test_common_lyapunov_search_is_fail_closed_for_broad_uncertainty(self) -> None:
        narrow = interval_vertices(
            stiffness_bounds_n_per_m=(90.0, 110.0),
            integral_slope_bounds_m_s2_per_n_s=(1.0 / 12.0, 1.25 / 12.0),
            force_p_gain_m_s2_per_n=1.0 / 12.0,
            force_damping_s_inv=550.0 / 12.0,
        )
        narrow_certificate = search_common_lyapunov(
            narrow,
            seed=17,
            maxiter=80,
            population_size=8,
        )
        self.assertTrue(narrow_certificate.feasible)
        self.assertIsNotNone(narrow_certificate.sampled_period_bound_s)

        broad = interval_vertices(
            stiffness_bounds_n_per_m=(80.0, 160.0),
            integral_slope_bounds_m_s2_per_n_s=(1.0 / 12.0, 1.0 / 6.0),
            force_p_gain_m_s2_per_n=1.0 / 12.0,
            force_damping_s_inv=550.0 / 12.0,
        )
        broad_certificate = search_common_lyapunov(
            broad,
            seed=17,
            maxiter=80,
            population_size=8,
        )
        self.assertFalse(broad_certificate.feasible)
        self.assertIsNone(broad_certificate.sampled_period_bound_s)


if __name__ == "__main__":
    unittest.main()
