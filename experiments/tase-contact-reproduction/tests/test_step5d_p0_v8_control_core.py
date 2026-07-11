#!/usr/bin/env python3
"""Tests for the shared P0 v8 production target builder."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_p0_v8_control_core import (  # noqa: E402
    P0_V8_EFFECTIVE_KO,
    build_p0_v8_target,
    limit_p0_xdot_components,
    low_force_posture_policy,
    press_only_outer_output,
    scale_xdot_for_joint_feasibility,
)


class Step5dP0V8ControlCoreTest(unittest.TestCase):
    def test_low_load_profile_is_fixed_at_effective_ko_point_zero_one(self) -> None:
        policy = low_force_posture_policy(normal_load_n=0.0)

        self.assertEqual(policy["policy"], "weak_posture_v2")
        self.assertAlmostEqual(policy["effective_ko"], P0_V8_EFFECTIVE_KO)
        self.assertAlmostEqual(policy["orientation_gain_scale"], 0.002)

    def test_press_only_target_obeys_canonical_approach_normal(self) -> None:
        output = press_only_outer_output(
            reaction_normal_b=(0.0, 0.0, -1.0),
            force_error_n=1.0,
        )

        np.testing.assert_allclose(output.xdot_c, (0.0, 0.0, 0.00015, 0.0, 0.0, 0.0))

    def test_tcp_component_limiter_rejects_unload_and_preserves_frame_mapping(self) -> None:
        rotation = np.diag((1.0, -1.0, -1.0))
        limited, active, diagnostics = limit_p0_xdot_components(
            (0.0, 0.0, 0.001, 0.0, 0.0, 0.0),
            rotation_base_from_tcp=rotation,
            return_diagnostics=True,
        )

        self.assertTrue(active)
        self.assertEqual(diagnostics["tcp_press_speed_m_s"], 0.0)
        np.testing.assert_allclose(limited, np.zeros(6))

    def test_feasibility_scaling_preserves_direction_and_qdot_headroom(self) -> None:
        desired = np.asarray((0.1, 0.0, 0.0, 0.0, 0.0, 0.0))
        scaled, diagnostics = scale_xdot_for_joint_feasibility(
            desired,
            np.eye(6),
            qdot_cap_rad_s=0.05,
            safety=0.9,
        )

        self.assertAlmostEqual(diagnostics["xdot_feasibility_scale"], 0.45)
        np.testing.assert_allclose(scaled, desired * 0.45)

    def test_full_target_uses_press_plus_weak_posture_and_joint_feasibility(self) -> None:
        target = build_p0_v8_target(
            tcp_pose_base=(0.4, 0.1, 0.3, 0.0, 0.0, 0.0),
            tcp_speed_base=(0.0,) * 6,
            force_tcp_n=(0.0, 0.0, 0.0),
            reaction_normal_b=(0.0, 0.0, -1.0),
            normal_load_n=0.0,
            force_error_n=1.0,
            jacobian=np.eye(6),
        )

        self.assertEqual(target.outer_diagnostics["no_contact_p0_target_policy"], "press_plus_weak_posture_v2")
        self.assertAlmostEqual(target.outer_diagnostics["effective_ko"], 0.01)
        self.assertGreater(target.desired_twist[2], 0.0)
        self.assertLessEqual(max(abs(value) for value in target.desired_twist), 0.045)

    def test_full_target_fails_closed_on_singular_command_jacobian(self) -> None:
        with self.assertRaises(np.linalg.LinAlgError):
            build_p0_v8_target(
                tcp_pose_base=(0.0,) * 6,
                tcp_speed_base=(0.0,) * 6,
                force_tcp_n=(0.0,) * 3,
                reaction_normal_b=(0.0, 0.0, -1.0),
                normal_load_n=0.0,
                force_error_n=0.0,
                jacobian=np.zeros((6, 6)),
            )


if __name__ == "__main__":
    unittest.main()
