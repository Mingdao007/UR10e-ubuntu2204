#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers import step5b_simulation_mvp as sim  # noqa: E402


class Step5bSimulationMvpTest(unittest.TestCase):
    def test_safe_frame_local_zero_maps_to_origin(self) -> None:
        context = sim.load_context()
        mapped = sim.local_to_base_xy(context.basis, 0.0, 0.0)
        self.assertAlmostEqual(mapped[0], context.basis.origin_xy_m[0])
        self.assertAlmostEqual(mapped[1], context.basis.origin_xy_m[1])

    def test_preposition_is_single_goal_and_speed_limited(self) -> None:
        context = sim.load_context()
        start = sim.default_preposition_start_xyz(context.basis)
        target = sim.preposition_target_xyz(context.basis, start[2])
        plan = sim.plan_continuous_preposition(start, target, max_speed_m_s=0.020, sample_period_s=0.020)
        self.assertEqual(plan["stage"], 22.0)
        self.assertEqual(plan["goal_count"], 1)
        self.assertTrue(plan["legacy_repeated_short_goals_rejected"])
        self.assertLessEqual(plan["max_velocity_m_s"], 0.020 + 1e-9)
        self.assertAlmostEqual(plan["rows"][0]["velocity_norm_m_s"], 0.0)
        self.assertAlmostEqual(plan["rows"][-1]["velocity_norm_m_s"], 0.0)
        self.assertAlmostEqual(plan["rows"][-1]["tcp_x_m"], target[0])
        self.assertAlmostEqual(plan["rows"][-1]["tcp_y_m"], target[1])

    def test_force_trace_uses_reaction_and_approach_contract(self) -> None:
        artifact = sim.build_artifact()
        force = artifact["simulated_force_evidence"]
        baseline = artifact["preposition_simulated_force_evidence"]
        self.assertEqual(force["schema"], "ur10e_canonical_wrench_trace_v1")
        self.assertEqual(force["force_source"], "simulated_ft")
        self.assertEqual(force["claim_tier"], "simulated_ft")
        self.assertEqual(force["reaction_normal"], [0.0, 0.0, 1.0])
        self.assertEqual(force["approach_normal"], [0.0, 0.0, -1.0])
        self.assertEqual(force["normal_load_definition"], "dot(force_base, reaction_normal)")
        self.assertEqual(baseline["sample_count"], artifact["preposition"]["sample_count"])
        self.assertEqual(force["sample_count"], artifact["contact_phase"]["sample_count"])
        self.assertFalse(force["schema_issues"])
        self.assertEqual(baseline["max_normal_load_n"], 0.0)
        self.assertEqual({row["contact_state"] for row in baseline["rows"]}, {"no_contact"})
        self.assertAlmostEqual(force["max_normal_load_n"], artifact["contact_phase"]["target_load_n"])
        self.assertEqual({row["contact_state"] for row in force["rows"]}, {"contact"})
        self.assertTrue(artifact["acceptance"]["preposition_baseline_no_contact_ok"])
        self.assertTrue(artifact["acceptance"]["contact_phase_simulated_ft_ok"])

    def test_artifact_schema_and_source_paths(self) -> None:
        artifact = sim.build_artifact()
        self.assertEqual(artifact["schema"], "ur10e_step5b_simulation_mvp_v1")
        self.assertEqual(artifact["mode"], "offline_no_motion")
        self.assertFalse(artifact["live_robot_command_authorized"])
        self.assertTrue(artifact["step5b_live_locked"])
        self.assertTrue(artifact["calibrated_urdf_exists"])
        self.assertEqual(artifact["acceptance"]["goal_count_max"], 1)
        self.assertTrue(artifact["acceptance"]["canonical_wrench_schema_ok"])
        json.dumps(artifact)

    def test_artifact_records_frames_units_and_contact_geometry(self) -> None:
        artifact = sim.build_artifact()
        self.assertEqual(artifact["frames"]["gazebo_world"], "world")
        self.assertEqual(artifact["frames"]["robot_base"], "base")
        self.assertEqual(artifact["frames"]["tool"], "tool0")
        self.assertEqual(artifact["frames"]["ft_sensor"], "ft_sensor")
        self.assertEqual(artifact["frames"]["contact_tip"], "contact_tip")
        self.assertEqual(artifact["frames"]["force_vector"], "base")
        self.assertEqual(artifact["units"]["position"], "m")
        self.assertEqual(artifact["units"]["force"], "N")
        self.assertEqual(
            artifact["canonical_wrench_contract"]["source_switching_policy"],
            "launch_config_or_remap_only_no_controller_logic",
        )
        self.assertEqual(
            artifact["expected_contact_geometry"]["surface_top_z_m"],
            sim.CONTACT_SURFACE_Z_M,
        )
        self.assertEqual(
            artifact["expected_contact_geometry"]["safe_frame_origin_xy_m"],
            artifact["safe_frame"]["origin_xy_m"],
        )
        self.assertEqual(
            artifact["expected_contact_geometry"]["contact_phase_target_load_n"],
            artifact["stage"]["target_force_n"],
        )

    def test_world_sdf_has_contact_surface_and_origin_marker(self) -> None:
        tree = ET.parse(sim.WORLD_PATH)
        models = {element.attrib["name"] for element in tree.findall(".//model")}
        self.assertIn("step5_contact_surface", models)
        self.assertIn("step5_safe_frame_origin_marker", models)

    def test_headless_launch_uses_gazebo_server_only(self) -> None:
        launch_path = WORKSPACE / "src" / "ur10e_example_controllers" / "launch" / "step5b_simulation_mvp.launch.py"
        launch_source = launch_path.read_text(encoding="utf-8")
        self.assertIn('-s --headless-rendering"', launch_source)

    def test_simulation_urdf_strips_real_ros2_control_driver(self) -> None:
        source_urdf = sim.CALIBRATED_URDF.read_text(encoding="utf-8")
        self.assertIn("ur_robot_driver/URPositionHardwareInterface", source_urdf)
        sanitized = sim.strip_ros2_control_blocks(source_urdf)
        self.assertNotIn("ros2_control", sanitized)
        self.assertNotIn("ur_robot_driver/URPositionHardwareInterface", sanitized)
        self.assertIn('name="tool0"', sanitized)


if __name__ == "__main__":
    unittest.main()
