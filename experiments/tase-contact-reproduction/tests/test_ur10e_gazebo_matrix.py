#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers import step56_simulation_matrix as offline  # noqa: E402
from ur10e_example_controllers import ur10e_gazebo_matrix_runner as gazebo  # noqa: E402

TOOLS = WORKSPACE / "experiments" / "tase-contact-reproduction" / "tools"
sys.path.insert(0, str(TOOLS))
import build_gazebo_visual_world as visual_world  # noqa: E402
import gazebo_tcp_marker_follower as tcp_marker  # noqa: E402


class Ur10eGazeboMatrixTest(unittest.TestCase):
    def test_launch_defaults_to_gui_not_headless(self) -> None:
        launch_path = WORKSPACE / "src" / "ur10e_example_controllers" / "launch" / "ur10e_gazebo_matrix.launch.py"
        source = launch_path.read_text(encoding="utf-8")
        self.assertIn('"headless"', source)
        self.assertIn('default_value="false"', source)
        self.assertNotIn('DeclareLaunchArgument("headless", default_value="true"', source)
        self.assertIn('name="robot_state_publisher"', source)
        self.assertIn('"world_path"', source)
        self.assertIn('"gui_config_path"', source)

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

    def test_world_loads_user_commands_for_robot_spawn(self) -> None:
        world_path = WORKSPACE / "src" / "ur10e_example_controllers" / "worlds" / "step5_table_world.sdf"
        source = world_path.read_text(encoding="utf-8")
        self.assertIn("gz-sim-user-commands-system", source)
        self.assertIn("gz::sim::systems::UserCommands", source)

    def test_generated_robot_description_uses_sim_hardware_not_real_driver(self) -> None:
        robot_description = gazebo.generate_sim_robot_description()
        self.assertIn("ign_ros2_control/IgnitionSystem", robot_description)
        self.assertIn("libign_ros2_control-system.so", robot_description)
        self.assertNotIn("ur_robot_driver/URPositionHardwareInterface", robot_description)
        self.assertIn(gazebo.TCP_VISUAL_LINK, robot_description)
        root = ET.fromstring(robot_description)
        self.assertIsNotNone(root.find(f"./link[@name='{gazebo.TCP_VISUAL_LINK}']"))
        self.assertIsNotNone(root.find(f"./joint[@name='{gazebo.TCP_VISUAL_JOINT}']"))

    def test_stage_visual_world_keeps_only_relevant_surface_and_path_markers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ur10e_gazebo_visual_world_test_") as tmp:
            output = Path(tmp) / "step5b_visual.sdf"
            visual_world.build_visual_world(
                "step5b",
                WORKSPACE / "src" / "ur10e_example_controllers" / "worlds" / "step5_table_world.sdf",
                output,
            )
            source = output.read_text(encoding="utf-8")
            self.assertIn("step5_contact_surface", source)
            self.assertIn("step5b_reference_path_visual", source)
            self.assertNotIn("step6_contact_surface", source)
            self.assertNotIn("step7_large_platform_contact_surface", source)
            self.assertNotIn("step8_large_platform_contact_surface", source)
            root = ET.parse(output).getroot()
            surface = root.find("./world/model[@name='step5_contact_surface']")
            self.assertIsNotNone(surface)
            pose = [float(value) for value in surface.findtext("pose").split()]
            size = [
                float(value)
                for value in surface.findtext("./link/collision/geometry/box/size").split()
            ]
            last = gazebo.build_reference_rows("step5b")[-1]
            self.assertLessEqual(abs(last.x_m - pose[0]), 0.5 * size[0])
            self.assertLessEqual(abs(last.y_m - pose[1]), 0.5 * size[1])

    def test_stage_visual_world_manifest_and_sdf_for_all_stages(self) -> None:
        base_world = WORKSPACE / "src" / "ur10e_example_controllers" / "worlds" / "step5_table_world.sdf"
        contact_stage_ids = {"step5b", "step5d", "step6b", "step7", "step8"}
        with tempfile.TemporaryDirectory(prefix="ur10e_gazebo_visual_world_all_stage_test_") as tmp:
            out = Path(tmp)
            for stage_id, keep_models in visual_world.SURFACE_MODELS_BY_STAGE.items():
                with self.subTest(stage=stage_id):
                    output = visual_world.build_visual_world(stage_id, base_world, out / f"{stage_id}_visual.sdf")
                    manifest_path = output.with_suffix(".manifest.json")
                    self.assertTrue(manifest_path.is_file())
                    completed = subprocess.run(["ign", "sdf", "-k", str(output)], check=False, capture_output=True, text=True)
                    self.assertEqual(completed.returncode, 0, completed.stderr)

                    source = output.read_text(encoding="utf-8")
                    for model_name in keep_models:
                        self.assertIn(model_name, source)
                    for model_name in visual_world.ALL_STAGE_VISUAL_MODELS - set(keep_models):
                        self.assertNotIn(model_name, source)
                    self.assertIn(f"{stage_id}_reference_path_visual", source)

                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    self.assertEqual(manifest["stage_id"], stage_id)
                    self.assertTrue(manifest["path_inside_surface_xy"])
                    self.assertEqual(manifest["force_loop_expected"], stage_id in contact_stage_ids)
                    bounds = manifest["path_bounds_xy_m"]
                    surface = manifest["surface"]
                    self.assertLessEqual(bounds["min_x_m"], surface["max_x_m"])
                    self.assertGreaterEqual(bounds["max_x_m"], surface["min_x_m"])

    def test_tcp_marker_model_sdf_is_non_colliding_and_high_contrast(self) -> None:
        source = tcp_marker.build_marker_model_sdf("active_tcp_marker")
        self.assertIn('model name="active_tcp_marker"', source)
        self.assertIn("tcp_magenta_sphere", source)
        self.assertIn("tcp_white_mast", source)
        self.assertIn("1 0 1 1", source)
        self.assertNotIn("<collision", source)

    def test_tcp_marker_pose_uses_runner_fk_for_step5b_final_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ur10e_gazebo_tcp_marker_test_") as tmp:
            trace_path, _, _ = gazebo.build_command_trace("step5b", Path(tmp))
            with trace_path.open(newline="", encoding="utf-8") as handle:
                final = list(csv.DictReader(handle))[-1]
            joint_positions = [float(final[f"command_{name}_rad"]) for name in gazebo.JOINT_NAMES]
            marker_pose = tcp_marker.marker_pose_from_joint_positions(joint_positions)
            self.assertAlmostEqual(marker_pose.x_m, float(final["commanded_fk_x_m"]), places=6)
            self.assertAlmostEqual(marker_pose.y_m, float(final["commanded_fk_y_m"]), places=6)
            self.assertAlmostEqual(marker_pose.z_m, float(final["commanded_fk_z_m"]), places=6)

    def test_step5b_final_marker_pose_lies_inside_visual_surface(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ur10e_gazebo_tcp_surface_test_") as tmp:
            out = Path(tmp)
            world = visual_world.build_visual_world(
                "step5b",
                WORKSPACE / "src" / "ur10e_example_controllers" / "worlds" / "step5_table_world.sdf",
                out / "step5b_visual.sdf",
            )
            trace_path, _, _ = gazebo.build_command_trace("step5b", out / "runner")
            with trace_path.open(newline="", encoding="utf-8") as handle:
                final = list(csv.DictReader(handle))[-1]
            marker_pose = tcp_marker.marker_pose_from_joint_positions(
                [float(final[f"command_{name}_rad"]) for name in gazebo.JOINT_NAMES]
            )
            root = ET.parse(world).getroot()
            surface = root.find("./world/model[@name='step5_contact_surface']")
            self.assertIsNotNone(surface)
            pose = [float(value) for value in surface.findtext("pose").split()]
            size = [
                float(value)
                for value in surface.findtext("./link/collision/geometry/box/size").split()
            ]
            self.assertLessEqual(abs(marker_pose.x_m - pose[0]), 0.5 * size[0])
            self.assertLessEqual(abs(marker_pose.y_m - pose[1]), 0.5 * size[1])

    def test_tcp_marker_artifacts_write_manifest_and_trace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ur10e_gazebo_tcp_marker_artifacts_test_") as tmp:
            out = Path(tmp)
            pose = tcp_marker.MarkerPose(t_s=0.0, x_m=0.48, y_m=0.22, z_m=0.006)
            artifacts = tcp_marker.write_marker_artifacts(
                out,
                stage_id="step5b",
                world_name="ur10e_step5_table_world",
                model_name="active_tcp_marker",
                pose_records=[pose],
                pose_source="unit_test_fk",
            )
            self.assertTrue(artifacts.manifest_path.is_file())
            self.assertTrue(artifacts.trace_path.is_file())
            payload = json.loads(artifacts.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["stage_id"], "step5b")
            self.assertEqual(payload["pose_source"], "unit_test_fk")
            self.assertEqual(payload["model_name"], "active_tcp_marker")
            self.assertEqual(payload["pose_count"], 1)

    def test_tcp_marker_create_request_uses_sdf_filename_for_gazebo_create(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ur10e_gazebo_tcp_marker_spawn_test_") as tmp:
            marker_sdf = Path(tmp) / "active_tcp_marker.sdf"
            marker_sdf.write_text(tcp_marker.build_marker_model_sdf("active_tcp_marker"), encoding="utf-8")
            request = tcp_marker.build_create_request(
                model_name="active_tcp_marker",
                pose=tcp_marker.MarkerPose(t_s=0.0, x_m=0.48, y_m=0.22, z_m=0.006),
                marker_sdf_path=marker_sdf,
            )
            self.assertIn(f'sdf_filename: "{marker_sdf}"', request)
            self.assertIn('name: "active_tcp_marker"', request)
            self.assertNotIn("sdf:", request)

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
