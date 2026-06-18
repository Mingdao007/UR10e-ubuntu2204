#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers import step56_simulation_matrix as offline  # noqa: E402
from ur10e_example_controllers import ur10e_gazebo_matrix_runner as gazebo  # noqa: E402


class Ur10eGazeboMatrixTest(unittest.TestCase):
    def test_launch_defaults_to_gui_not_headless(self) -> None:
        launch_path = WORKSPACE / "src" / "ur10e_example_controllers" / "launch" / "ur10e_gazebo_matrix.launch.py"
        source = launch_path.read_text(encoding="utf-8")
        self.assertIn('"headless"', source)
        self.assertIn('default_value="false"', source)
        self.assertNotIn('DeclareLaunchArgument("headless", default_value="true"', source)

    def test_controller_yaml_has_expected_controllers_and_joints(self) -> None:
        payload = yaml.safe_load(gazebo.CONTROLLERS_YAML.read_text(encoding="utf-8"))
        manager = payload["controller_manager"]["ros__parameters"]
        self.assertEqual(manager["joint_state_broadcaster"]["type"], "joint_state_broadcaster/JointStateBroadcaster")
        self.assertEqual(
            manager["joint_trajectory_controller"]["type"],
            "joint_trajectory_controller/JointTrajectoryController",
        )
        controller = payload["joint_trajectory_controller"]["ros__parameters"]
        self.assertEqual(controller["joints"], gazebo.JOINT_NAMES)
        self.assertEqual(controller["command_interfaces"], ["position"])

    def test_generated_robot_description_uses_sim_hardware_not_real_driver(self) -> None:
        robot_description = gazebo.generate_sim_robot_description()
        self.assertIn("ign_ros2_control/IgnitionSystem", robot_description)
        self.assertIn("libign_ros2_control-system.so", robot_description)
        self.assertNotIn("ur_robot_driver/URPositionHardwareInterface", robot_description)

    def test_runner_dry_plan_writes_all_stage_traces_and_matrix_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ur10e_gazebo_matrix_test_") as tmp:
            out = Path(tmp)
            matrix_path = gazebo.run_matrix("all", out, execute=False)
            payload = json.loads(matrix_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "ur10e_gazebo_matrix_result_v1")
            self.assertEqual(payload["mode"], "gazebo_ros2_control")
            self.assertTrue(payload["gazebo_only"])
            self.assertFalse(payload["live_robot_command_authorized"])
            self.assertEqual([stage["stage_id"] for stage in payload["stages"]], list(offline.STAGE_REGISTRY))
            for stage in payload["stages"]:
                with self.subTest(stage=stage["stage_id"]):
                    self.assertEqual(stage["action_name"], gazebo.ACTION_NAME)
                    self.assertFalse(stage["force_physics_closed_loop"])
                    self.assertTrue(stage["acceptance"]["trace_written"])
                    self.assertTrue(stage["acceptance"]["trajectory_duration_nonzero"])
                    self.assertLessEqual(
                        stage["acceptance"]["max_commanded_ik_error_m"],
                        stage["acceptance"]["max_tcp_xy_path_error_limit_m"],
                    )
                    self.assertTrue(Path(stage["trace_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
