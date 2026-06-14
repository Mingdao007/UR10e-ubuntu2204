#!/usr/bin/env python3
"""Offline checks for the Step5d paper-form outer loop."""

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

import step5d_paper_outer_loop as outer_loop  # noqa: E402
from contact_semantics import approach_normal_from_reaction, force_motion_acceleration_base, signed_normal_load_n  # noqa: E402
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver  # noqa: E402
from step5d_paper_outer_loop import (  # noqa: E402
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
    quaternion_orientation_error,
    rotation_matrix_to_quaternion,
    rotvec_to_matrix,
    rnn_target_state_from_outer_loop,
)


def verified_truth_file() -> tempfile.NamedTemporaryFile[str]:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False)
    json.dump({"strict_rnn_enabled": True, "pending_pdf_verify": [], "sections": {}}, handle)
    handle.close()
    return handle


class Step5dPaperOuterLoopTest(unittest.TestCase):
    def make_inputs(
        self,
        *,
        tcp_pose_base: tuple[float, float, float, float, float, float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        force_tcp_n: tuple[float, float, float] = (0.0, 0.0, -2.0),
        control_reaction_normal_base: tuple[float, float, float] = (0.0, 0.0, -1.0),
        x_pd_base: tuple[float, float, float] = (0.0, 0.0, 0.0),
        xdot_pd_base: tuple[float, float, float] = (0.0, 0.0, 0.0),
        dt_s: float = 0.002,
        cmd_valid: bool = True,
    ) -> Step5dOuterLoopInputs:
        return Step5dOuterLoopInputs(
            tcp_pose_base=tcp_pose_base,
            tcp_speed_base=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            force_tcp_n=force_tcp_n,
            control_reaction_normal_base=control_reaction_normal_base,
            x_pd_base=x_pd_base,
            xdot_pd_base=xdot_pd_base,
            dt_s=dt_s,
            cmd_valid=cmd_valid,
        )

    def test_quaternion_orientation_error_identity_is_zero(self) -> None:
        Q_d = rotation_matrix_to_quaternion(np.eye(3))
        Q_cur = rotation_matrix_to_quaternion(np.eye(3))
        e_qua, e_o = quaternion_orientation_error(Q_d, Q_cur)
        np.testing.assert_allclose(e_qua, [1.0, 0.0, 0.0, 0.0], atol=1e-12)
        np.testing.assert_allclose(e_o, [0.0, 0.0, 0.0], atol=1e-12)

    def test_quaternion_orientation_error_known_yaw(self) -> None:
        yaw_rad = 0.2
        Q_d = rotation_matrix_to_quaternion(np.eye(3))
        Q_cur = rotation_matrix_to_quaternion(rotvec_to_matrix((0.0, 0.0, yaw_rad)))
        _e_qua, e_o = quaternion_orientation_error(Q_d, Q_cur)
        np.testing.assert_allclose(e_o, [0.0, 0.0, np.sin(yaw_rad * 0.5)], atol=1e-12)

    def test_outer_loop_builds_phi_motion_and_quaternion_outputs(self) -> None:
        config = Step5dOuterLoopConfig(force_target_n=2.0)
        output = compute_step5d_outer_loop(
            config,
            Step5dOuterLoopState(),
            self.make_inputs(
                x_pd_base=(0.1, -0.1, 0.0),
                xdot_pd_base=(0.01, 0.02, 0.0),
            ),
        )
        np.testing.assert_allclose(output.xdot_p, [0.41, -0.38, 0.0], atol=1e-12)
        np.testing.assert_allclose(output.xdot_o, [0.0, 0.0, 0.0], atol=1e-12)
        np.testing.assert_allclose(output.xdot_c, [0.41, -0.38, 0.0, 0.0, 0.0, 0.0], atol=1e-12)
        self.assertTrue(output.cmd_valid)
        self.assertEqual(output.diagnostics["force_sign_convention"], "step5_step6_positive_normal_load")
        self.assertAlmostEqual(output.diagnostics["normal_load_n"], 2.0, places=12)
        self.assertAlmostEqual(output.diagnostics["outer_orientation_angle_rad"], 0.0, places=12)
        self.assertAlmostEqual(output.diagnostics["R_d_z_dot_R_cur_z"], 1.0, places=12)
        for key in ["R_d", "Phi_O", "Phi_bar_O", "e_qua", "e_o", "xdot_c", "control_reaction_normal_base", "approach_normal_base"]:
            self.assertIn(key, output.diagnostics)

    def test_force_integral_and_normal_velocity_follow_eq16_17_discretization(self) -> None:
        config = Step5dOuterLoopConfig(kp=0.0, ko=0.0, kf=1.0, force_target_n=5.0)
        output = compute_step5d_outer_loop(
            config,
            Step5dOuterLoopState(),
            self.make_inputs(dt_s=0.1),
        )
        expected_integral = 0.3
        expected_z = ((3.0 + expected_integral) / 12.0) * 0.1
        self.assertAlmostEqual(output.next_state.force_integral_n_s, expected_integral, places=12)
        np.testing.assert_allclose(output.xdot_p, [0.0, 0.0, expected_z], atol=1e-12)
        self.assertAlmostEqual(output.diagnostics["e_f"], 3.0, places=12)
        np.testing.assert_allclose(output.diagnostics["xddot_p"], [0.0, 0.0, 0.275], atol=1e-12)

    def test_contact_semantics_positive_load_and_press_direction(self) -> None:
        reaction_normal = np.array([0.0, 0.0, -1.0])
        measured_environment_force = np.array([0.0, 0.0, -2.0])
        self.assertAlmostEqual(signed_normal_load_n(measured_environment_force, reaction_normal), 2.0, places=12)
        np.testing.assert_allclose(approach_normal_from_reaction(reaction_normal), [0.0, 0.0, 1.0], atol=1e-12)
        press_accel = force_motion_acceleration_base(
            force_error=3.0,
            force_integral=0.0,
            kf=1.0,
            Md=12.0,
            Bd=0.0,
            xdot_p_prev_base=(0.0, 0.0, 0.0),
            reaction_normal=reaction_normal,
        )
        unload_accel = force_motion_acceleration_base(
            force_error=-3.0,
            force_integral=0.0,
            kf=1.0,
            Md=12.0,
            Bd=0.0,
            xdot_p_prev_base=(0.0, 0.0, 0.0),
            reaction_normal=reaction_normal,
        )
        self.assertGreater(press_accel[2], 0.0)
        self.assertLess(unload_accel[2], 0.0)

    def test_outer_loop_uses_control_normal_not_raw_force_for_orientation(self) -> None:
        output = compute_step5d_outer_loop(
            Step5dOuterLoopConfig(kp=0.0, ko=5.0, force_target_n=5.0),
            Step5dOuterLoopState(),
            self.make_inputs(
                force_tcp_n=(0.0, 0.0, 2.0),
                control_reaction_normal_base=(0.0, 0.0, -1.0),
            ),
        )
        self.assertAlmostEqual(output.diagnostics["outer_orientation_angle_rad"], 0.0, places=12)
        np.testing.assert_allclose(output.xdot_o, [0.0, 0.0, 0.0], atol=1e-12)

    def test_invalid_command_freezes_outer_state(self) -> None:
        state = Step5dOuterLoopState(force_integral_n_s=1.0, xdot_p_prev_m_s=(0.01, 0.02, 0.03))
        output = compute_step5d_outer_loop(
            Step5dOuterLoopConfig(),
            state,
            self.make_inputs(cmd_valid=False),
        )
        self.assertFalse(output.cmd_valid)
        self.assertEqual(output.next_state, state)
        np.testing.assert_allclose(output.xdot_c, np.zeros(6), atol=1e-12)

    def test_outer_loop_output_feeds_strict_rnn_solver(self) -> None:
        truth = verified_truth_file()
        self.addCleanup(lambda: Path(truth.name).unlink(missing_ok=True))
        solver = StrictTaseRnnSolver(StrictRnnConfig(paper_truth_path=Path(truth.name)))
        output = compute_step5d_outer_loop(
            Step5dOuterLoopConfig(kp=0.0, ko=0.0, force_target_n=2.0),
            Step5dOuterLoopState(),
            self.make_inputs(),
        )
        target_state = rnn_target_state_from_outer_loop(
            output,
            J=np.eye(6),
            omega_minus=np.full(6, -0.15),
            omega_plus=np.full(6, 0.15),
            dt_s=0.002,
            epsilon=0.022,
            r=0.2,
        )
        result = solver.solve(actual_q=[0.0] * 6, actual_qd=[0.0] * 6, target_state=target_state)
        self.assertEqual(len(result.qdot), 6)
        self.assertEqual(result.diagnostics["xdot_c"], output.xdot_c)

    def test_module_has_no_old_orientation_or_dls_shortcut(self) -> None:
        source = inspect.getsource(outer_loop).lower()
        forbidden = ["cross3", "pinv", "lstsq", "np.linalg.solve", "scipy", "osqp", "dls", "rotation_matrix_from_z_axis", "u_force_base"]
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
