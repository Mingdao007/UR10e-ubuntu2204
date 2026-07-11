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
from step5d_paper_outer_loop import (  # noqa: E402
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
    rotvec_to_matrix,
)


class Step5dP0V8ControlCoreTest(unittest.TestCase):
    def test_bridge_and_simulator_bind_the_same_complete_target_builder(self) -> None:
        bridge_source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(
            encoding="utf-8"
        )
        simulator_source = (ROOT / "tools" / "ur10e_mujoco_adapter.py").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            "build_p0_v8_target as shared_build_p0_v8_target",
            bridge_source,
        )
        self.assertEqual(bridge_source.count("shared_build_p0_v8_target("), 1)
        self.assertIn("step5d_p0_v8_target.raw_outer_twist", bridge_source)
        self.assertIn("step5d_p0_v8_target.limited_twist", bridge_source)
        self.assertIn("step5d_p0_v8_target.desired_twist", bridge_source)
        self.assertIn("step5d_p0_v8_target.posture_policy", bridge_source)
        self.assertIn("step5d_p0_v8_target.outer_diagnostics", bridge_source)
        self.assertIn("step5d_p0_v8_target.frame_diagnostics", bridge_source)
        self.assertIn("step5d_p0_v8_target.feasibility_diagnostics", bridge_source)
        self.assertNotIn("press_plus_weak_posture_v2", bridge_source)
        self.assertIn("target = build_p0_v8_target(", simulator_source)

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

    def test_complete_builder_matches_pre_refactor_bridge_composition(self) -> None:
        pose = np.asarray((0.42, -0.08, 0.31, 0.17, -0.23, 0.09), dtype=float)
        speed = np.asarray((0.001, -0.002, 0.0005, 0.01, -0.02, 0.015), dtype=float)
        force = np.asarray((0.3, -0.2, 0.4), dtype=float)
        reaction = np.asarray((0.2, -0.1, 0.97), dtype=float)
        reaction /= np.linalg.norm(reaction)
        jacobian = np.diag((0.3, 0.25, 0.2, 0.8, 0.7, 0.6))
        normal_load_n = 0.4
        force_error_n = 0.6
        dt_s = 0.002

        policy = low_force_posture_policy(
            normal_load_n=normal_load_n,
            base_ko=5.0,
            low_load_n=1.0,
            high_load_n=2.0,
            low_ko=0.01,
            policy="weak_posture_v2",
        )
        press = press_only_outer_output(
            reaction_normal_b=reaction,
            force_error_n=force_error_n,
            press_speed_m_s=0.00015,
        )
        posture = compute_step5d_outer_loop(
            Step5dOuterLoopConfig(
                kp=0.0,
                ko=5.0,
                orientation_gain_scale=float(policy["orientation_gain_scale"]),
                kf=0.0,
                Md_scalar=12.0,
                Bd_scalar=550.0,
                force_target_n=0.0,
                delay_T_s=dt_s,
                force_sign_convention="step5_step6_positive_normal_load",
            ),
            Step5dOuterLoopState(),
            Step5dOuterLoopInputs(
                tcp_pose_base=tuple(float(value) for value in pose),
                tcp_speed_base=tuple(float(value) for value in speed),
                force_tcp_n=tuple(float(value) for value in force),
                control_reaction_normal_base=tuple(float(value) for value in reaction),
                x_pd_base=tuple(float(value) for value in pose[:3]),
                xdot_pd_base=(0.0, 0.0, 0.0),
                dt_s=dt_s,
                cmd_valid=True,
            ),
            include_diagnostics=True,
        )
        legacy_raw = np.asarray(posture.xdot_c, dtype=float)
        legacy_raw[:3] = np.asarray(press.xdot_c[:3], dtype=float)
        legacy_limited, legacy_active = limit_p0_xdot_components(
            legacy_raw,
            rotation_base_from_tcp=rotvec_to_matrix(pose[3:6]),
        )
        legacy_feasible, legacy_feasibility = scale_xdot_for_joint_feasibility(
            legacy_limited,
            jacobian,
            qdot_cap_rad_s=0.05,
            safety=0.9,
        )

        target = build_p0_v8_target(
            tcp_pose_base=pose,
            tcp_speed_base=speed,
            force_tcp_n=force,
            reaction_normal_b=reaction,
            normal_load_n=normal_load_n,
            force_error_n=force_error_n,
            jacobian=jacobian,
            dt_s=dt_s,
            qdot_cap_rad_s=0.05,
        )

        np.testing.assert_allclose(target.raw_outer_twist, legacy_raw, atol=1e-15)
        np.testing.assert_allclose(target.limited_twist, legacy_limited, atol=1e-15)
        np.testing.assert_allclose(target.desired_twist, legacy_feasible, atol=1e-15)
        self.assertEqual(target.limiter_active, legacy_active)
        self.assertEqual(target.posture_policy, policy)
        self.assertEqual(
            target.feasibility_diagnostics["xdot_feasibility_scale"],
            legacy_feasibility["xdot_feasibility_scale"],
        )

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
