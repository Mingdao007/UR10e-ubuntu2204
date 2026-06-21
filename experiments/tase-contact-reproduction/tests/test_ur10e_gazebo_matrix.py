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
import run_gazebo_gui_matrix_row as gui_row  # noqa: E402


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
        self.assertIn(gazebo.EOAT_VISUAL_LINK, robot_description)
        root = ET.fromstring(robot_description)
        self.assertIsNone(root.find(f"./link[@name='{gazebo.TCP_VISUAL_LINK}']"))
        self.assertIsNone(root.find(f"./joint[@name='{gazebo.TCP_VISUAL_JOINT}']"))
        self.assertIsNotNone(root.find(f"./link[@name='{gazebo.EOAT_VISUAL_LINK}']"))

    def test_generated_robot_description_includes_real_aligned_eoat_visual_stack(self) -> None:
        robot_description = gazebo.generate_sim_robot_description()
        root = ET.fromstring(robot_description)
        stack = root.find(f"./link[@name='{gazebo.EOAT_VISUAL_LINK}']")
        self.assertIsNotNone(stack)
        joint = root.find(f"./joint[@name='{gazebo.EOAT_VISUAL_JOINT}']")
        self.assertIsNotNone(joint)
        self.assertEqual(joint.find("parent").attrib["link"], "tool0")
        self.assertEqual(joint.find("child").attrib["link"], gazebo.EOAT_VISUAL_LINK)
        self.assertEqual(joint.find("origin").attrib["xyz"], "0 0 0")

        visual_names = {visual.attrib.get("name") for visual in stack.findall("visual")}
        self.assertTrue(gazebo.EOAT_REQUIRED_VISUAL_NAMES.issubset(visual_names))
        self.assertEqual(visual_names, {gazebo.EOAT_REAL_MESH_VISUAL_NAME})
        mesh = stack.find(f"./visual[@name='{gazebo.EOAT_REAL_MESH_VISUAL_NAME}']/geometry/mesh")
        self.assertIsNotNone(mesh)
        self.assertEqual(mesh.attrib["filename"], gazebo.EOAT_REAL_MESH_URI)
        self.assertEqual(mesh.attrib["scale"], "0.001 0.001 0.001")
        self.assertIsNotNone(stack.find("inertial"))
        tool0 = root.find("./link[@name='tool0']")
        tool0_visual_names = {visual.attrib.get("name") for visual in tool0.findall("visual")}
        self.assertFalse(gazebo.TOOL0_EOAT_VIEWER_VISUAL_NAMES - tool0_visual_names)
        collision_names = {collision.attrib.get("name") for collision in stack.findall("collision")}
        self.assertTrue(gazebo.EOAT_REQUIRED_COLLISION_NAMES.issubset(collision_names))
        contact_pad = stack.find("./collision[@name='eoat_contact_pad_collision']/geometry/box")
        self.assertIsNotNone(contact_pad)
        self.assertEqual(contact_pad.attrib["size"], "0.052 0.052 0.010")

    def test_model_composition_audit_reports_proxy_and_removed_redundant_tcp_marker(self) -> None:
        robot_description = gazebo.generate_sim_robot_description()
        audit = gazebo.build_model_composition_audit(robot_description)
        self.assertEqual(audit["schema"], "ur10e_gazebo_model_composition_audit_v2")
        self.assertEqual(audit["eoat_joint_parent"], "tool0")
        self.assertEqual(audit["missing_eoat_visuals"], [])
        self.assertEqual(audit["missing_tool0_viewer_affordance_visuals"], [])
        self.assertTrue(audit["eoat_inertial_present"])
        self.assertTrue(audit["actual_eoat_mesh_visual_present"])
        self.assertEqual(audit["eoat_primary_visual_mesh_uri"], gazebo.EOAT_REAL_MESH_URI)
        self.assertEqual(audit["eoat_primary_visual_mesh_scale"], [0.001, 0.001, 0.001])
        self.assertEqual(audit["eoat_primitive_visual_remnants"], [])
        self.assertIn("actual_local_stl_primary_visual", audit["eoat_visual_proxy_policy"])
        self.assertEqual(audit["missing_eoat_collisions"], [])
        self.assertEqual(audit["present_eoat_collisions"], sorted(gazebo.EOAT_REQUIRED_COLLISION_NAMES))
        self.assertEqual(audit["eoat_collision_count"], len(gazebo.EOAT_REQUIRED_COLLISION_NAMES))
        self.assertIn("contact_pair_log_missing", audit["eoat_collision_policy"])
        self.assertFalse(audit["tcp_visual_link_present"])
        self.assertIn("removed_from_generated_robot_description", audit["redundant_tcp_marker_policy"])
        self.assertEqual(audit["force_contact_source"], gazebo.FORCE_CONTACT_SOURCE)
        self.assertFalse(audit["force_contact_physics_proven"])

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
            self.assertIn("step5b_surface_viewer_affordance", source)
            self.assertIn("surface_front_rim", source)
            self.assertIn("surface_front_left_witness_post", source)
            self.assertIn("contact_target_center_marker", source)
            self.assertIn("contact_target_cross_x", source)
            self.assertIn("contact_target_vertical_witness", source)
            self.assertIn("gz-sim-sensors-system", source)
            self.assertIn("gz::sim::systems::Contact", source)
            self.assertIn("step5b_surface_contact_sensor", source)
            self.assertIn("/ur10e/contact/gazebo/step5b/contacts", source)
            self.assertIn("step5b_close_detail_scripted_camera", source)
            self.assertIn("/ur10e_visual_audit/step5b/close_detail/image", source)
            self.assertIn("step5b_side_view_scripted_camera", source)
            self.assertIn("/ur10e_visual_audit/step5b/side_view/image", source)
            self.assertNotIn("step6_contact_surface", source)
            manifest = json.loads(output.with_suffix(".manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["contact_pair_logging"]["topic"],
                "/ur10e/contact/gazebo/step5b/contacts",
            )
            self.assertEqual(manifest["contact_pair_logging"]["claim_tier"], "visual_only")
            self.assertFalse(manifest["contact_pair_logging"]["contact_pair_log_evidence"])
            self.assertEqual(
                sorted(manifest["scripted_cameras"]),
                ["close_detail", "context_overview", "interaction_view", "side_view"],
            )
            self.assertEqual(
                manifest["scripted_cameras"]["close_detail"]["capture_policy"],
                "scripted_gazebo_camera_clean_no_gui_panels_observer_review_still_required",
            )
            close_camera = manifest["scripted_cameras"]["close_detail"]
            interaction_camera = manifest["scripted_cameras"]["interaction_view"]
            side_camera = manifest["scripted_cameras"]["side_view"]
            self.assertLess(close_camera["target_xyz_m"][2], close_camera["base_target_xyz_m"][2])
            self.assertEqual(close_camera["target_offset_xyz_m"], [0.0, 0.0, -0.095])
            self.assertGreater(close_camera["horizontal_fov_rad"], interaction_camera["horizontal_fov_rad"])
            self.assertEqual(side_camera["target_offset_xyz_m"], [0.0, 0.0, 0.0])
            self.assertEqual(
                manifest["surface_viewer_affordance"]["policy"],
                "non_colliding_viewer_affordance_surface_outline_and_contact_target_marker",
            )
            self.assertEqual(manifest["surface_viewer_affordance"]["style"], visual_world.VISUAL_AFFORDANCE_STYLE)
            self.assertIn("not_primary_visual_cue", manifest["surface_viewer_affordance"]["primitive_proxy_visibility_policy"])
            self.assertEqual(manifest["reference_path_style"], visual_world.VISUAL_AFFORDANCE_STYLE)
            self.assertIn("not_primary_visual_cue", manifest["reference_path_visibility_policy"])
            self.assertTrue(manifest["surface_mesh_visual"]["primary_visual_uses_real_mesh"])
            self.assertEqual(manifest["surface_mesh_visual"]["mesh_uri"], gazebo.CONTACT_SURFACE_REAL_MESH_URI)
            self.assertEqual(manifest["surface_mesh_visual"]["scale"], "0.001 0.001 0.001")
            self.assertIn("two_piece_surface_smooth_v11_3mm_thick.stl", manifest["surface_mesh_visual"]["mesh_source_asset"])
            self.assertNotIn("step7_large_platform_contact_surface", source)
            self.assertNotIn("step8_large_platform_contact_surface", source)
            root = ET.parse(output).getroot()
            surface = root.find("./world/model[@name='step5_contact_surface']")
            self.assertIsNotNone(surface)
            surface_affordance = root.find("./world/model[@name='step5b_surface_viewer_affordance']")
            self.assertIsNotNone(surface_affordance)
            path_visual = root.find("./world/model[@name='step5b_reference_path_visual']")
            self.assertIsNotNone(path_visual)
            for source in (
                ET.tostring(surface_affordance, encoding="unicode"),
                ET.tostring(path_visual, encoding="unicode"),
            ):
                self.assertNotIn("1.0 0.85 0.0 1.0", source)
                self.assertNotIn("1.0 0.0 1.0 1.0", source)
                self.assertNotIn("0.0 1.0 1.0 1.0", source)
                self.assertNotIn("0.0 1.0 0.0 1.0", source)
                self.assertNotIn("1.0 0.0 0.0 1.0", source)
            pose = [float(value) for value in surface.findtext("pose").split()]
            size = [
                float(value)
                for value in surface.findtext("./link/collision/geometry/box/size").split()
            ]
            last = gazebo.build_reference_rows("step5b")[-1]
            last_world = gazebo.gazebo_world_xyz_from_base_xyz((last.x_m, last.y_m, last.z_m))
            self.assertLessEqual(abs(last_world[0] - pose[0]), 0.5 * size[0])
            self.assertLessEqual(abs(last_world[1] - pose[1]), 0.5 * size[1])

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
                    self.assertTrue(manifest["surface_viewer_affordance"]["added"])
                    self.assertIn("non_colliding_viewer_affordance", manifest["surface_viewer_affordance"]["policy"])
                    self.assertEqual(manifest["surface_viewer_affordance"]["style"], visual_world.VISUAL_AFFORDANCE_STYLE)
                    self.assertEqual(manifest["reference_path_style"], visual_world.VISUAL_AFFORDANCE_STYLE)
                    self.assertTrue(manifest["path_inside_surface_xy"])
                    self.assertEqual(manifest["force_loop_expected"], stage_id in contact_stage_ids)
                    self.assertEqual(manifest["active_tcp_reference_frame"], gazebo.ACTIVE_TCP_FRAME)
                    self.assertEqual(manifest["surface_frame"], gazebo.GAZEBO_WORLD_FRAME)
                    self.assertEqual(manifest["tool_frame"], gazebo.TOOL0_FRAME)
                    self.assertEqual(manifest["base_to_gazebo_world_rpy"], list(gazebo.BASE_TO_GAZEBO_WORLD_RPY))
                    self.assertEqual(manifest["final_visual_pose_world"]["frame"], gazebo.GAZEBO_WORLD_FRAME)
                    bounds = manifest["path_bounds_xy_m"]
                    base_bounds = manifest["path_bounds_base_xy_m"]
                    self.assertAlmostEqual(bounds["min_x_m"], -base_bounds["max_x_m"])
                    self.assertAlmostEqual(bounds["max_x_m"], -base_bounds["min_x_m"])
                    self.assertAlmostEqual(bounds["min_y_m"], -base_bounds["max_y_m"])
                    self.assertAlmostEqual(bounds["max_y_m"], -base_bounds["min_y_m"])
                    surface = manifest["surface"]
                    self.assertAlmostEqual(surface["top_z_m"], surface["z_m"] + 0.5 * surface["size_z_m"])
                    self.assertLessEqual(bounds["min_x_m"], surface["max_x_m"])
                    self.assertGreaterEqual(bounds["max_x_m"], surface["min_x_m"])
                    sanity = manifest["surface_tcp_sanity"]
                    self.assertTrue(sanity["final_reference_xy_inside_surface"])
                    self.assertEqual(sanity["reaction_normal_base"], [0.0, 0.0, 1.0])
                    self.assertEqual(sanity["approach_normal_base"], [0.0, 0.0, -1.0])
                    self.assertEqual(sanity["approach_dot_reaction"], -1.0)
                    if stage_id in contact_stage_ids:
                        self.assertIsNotNone(manifest["contact_target_pose_base"])
                        self.assertIsNotNone(manifest["contact_target_pose_world"])
                        self.assertEqual(manifest["contact_target_pose_world"]["frame"], gazebo.GAZEBO_WORLD_FRAME)
                        self.assertTrue(sanity["contact_target_xy_inside_surface"])
                        self.assertTrue(sanity["contact_target_world_xy_inside_surface"])
                        self.assertAlmostEqual(sanity["contact_target_z_minus_surface_top_m"], 0.0)
                        self.assertTrue(manifest["contact_pair_logging"]["enabled"])
                        self.assertEqual(manifest["contact_pair_logging"]["surface_model"], keep_models[0])
                        self.assertIn(manifest["contact_pair_logging"]["topic"], source)
                    else:
                        self.assertIsNone(manifest["contact_target_pose_base"])
                        self.assertIsNone(manifest["contact_target_pose_world"])
                        self.assertIsNone(sanity["contact_target_xy_inside_surface"])
                        self.assertFalse(manifest["contact_pair_logging"]["enabled"])

    def test_tcp_marker_model_sdf_default_is_non_colliding_debug_high_contrast(self) -> None:
        source = tcp_marker.build_marker_model_sdf("active_tcp_marker")
        self.assertIn('model name="active_tcp_marker"', source)
        self.assertIn("tcp_magenta_sphere", source)
        self.assertIn("tcp_contact_pad_orange", source)
        self.assertIn("tcp_probe_sleeve_yellow", source)
        self.assertIn("tcp_tool_plate_silver", source)
        self.assertIn("tcp_sensor_body_teal", source)
        self.assertIn("tcp_white_mast", source)
        self.assertIn("1 0 1 1", source)
        self.assertNotIn("<collision", source)

    def test_tcp_marker_model_sdf_observer_subtle_is_non_colliding_and_low_dominance(self) -> None:
        source = tcp_marker.build_marker_model_sdf("active_tcp_marker", marker_style="observer_subtle")
        self.assertIn('model name="active_tcp_marker"', source)
        self.assertIn("tcp_magenta_sphere", source)
        self.assertIn("tcp_contact_pad_orange", source)
        self.assertIn("tcp_probe_sleeve_yellow", source)
        self.assertIn("tcp_tool_plate_silver", source)
        self.assertIn("tcp_sensor_body_teal", source)
        self.assertIn("tcp_white_mast", source)
        self.assertIn("<size>0.024 0.024 0.004</size>", source)
        self.assertIn("<radius>0.007</radius>", source)
        self.assertIn("<emissive>0 0 0 1</emissive>", source)
        self.assertNotIn("1 0 1 1", source)
        self.assertNotIn("1 0.45 0 1", source)
        self.assertNotIn("0 1 1 1", source)
        self.assertNotIn("<collision", source)

    def test_gui_row_defaults_to_observer_subtle_marker_style(self) -> None:
        args = gui_row.parse_args(
            [
                "row",
                "--run-dir",
                "/tmp/ur10e_marker_style_parse_fixture",
                "--stage",
                "step5b",
                "--view",
                "close_detail",
            ]
        )
        self.assertEqual(args.marker_style, "observer_subtle")

    def test_tcp_marker_follower_defaults_to_debug_marker_style(self) -> None:
        args = tcp_marker.parse_args(
            [
                "--stage",
                "step5b",
                "--output-dir",
                "/tmp/ur10e_marker_style_parse_fixture",
            ]
        )
        self.assertEqual(args.marker_style, "debug")

    def test_tcp_marker_pose_uses_runner_fk_for_step5b_final_command(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ur10e_gazebo_tcp_marker_test_") as tmp:
            trace_path, _, _ = gazebo.build_command_trace("step5b", Path(tmp))
            with trace_path.open(newline="", encoding="utf-8") as handle:
                final = list(csv.DictReader(handle))[-1]
            joint_positions = [float(final[f"command_{name}_rad"]) for name in gazebo.JOINT_NAMES]
            marker_pose = tcp_marker.marker_pose_from_joint_positions(joint_positions)
            self.assertEqual(final["commanded_fk_frame"], gazebo.ACTIVE_TCP_FRAME)
            expected_world = gazebo.gazebo_world_xyz_from_base_xyz(
                (
                    float(final["commanded_active_tcp_x_m"]),
                    float(final["commanded_active_tcp_y_m"]),
                    float(final["commanded_active_tcp_z_m"]),
                )
            )
            self.assertAlmostEqual(marker_pose.x_m, expected_world[0], places=6)
            self.assertAlmostEqual(marker_pose.y_m, expected_world[1], places=6)
            self.assertAlmostEqual(marker_pose.z_m, expected_world[2], places=6)
            active_minus_tool0_m = (
                sum(
                    (
                        float(final[f"commanded_active_tcp_{axis}_m"])
                        - float(final[f"commanded_tool0_{axis}_m"])
                    )
                    ** 2
                    for axis in ("x", "y", "z")
                )
                ** 0.5
            )
            self.assertGreater(active_minus_tool0_m, 0.05)

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
            self.assertEqual(payload["schema"], "ur10e_gazebo_tcp_marker_manifest_v2")
            self.assertEqual(payload["stage_id"], "step5b")
            self.assertEqual(payload["pose_source"], "unit_test_fk")
            self.assertEqual(payload["pose_frame"], gazebo.GAZEBO_WORLD_FRAME)
            self.assertEqual(payload["source_frame"], gazebo.ACTIVE_TCP_FRAME)
            self.assertEqual(payload["tool_frame"], gazebo.TOOL0_FRAME)
            self.assertEqual(payload["base_to_gazebo_world_rpy"], list(gazebo.BASE_TO_GAZEBO_WORLD_RPY))
            self.assertEqual(payload["active_tcp_offset_tool0_m"], list(gazebo.ACTIVE_TCP_OFFSET_TOOL0_M))
            self.assertEqual(payload["model_name"], "active_tcp_marker")
            self.assertEqual(payload["marker_style"], "debug")
            self.assertEqual(payload["pose_count"], 1)

    def test_pose_info_reader_accepts_multiple_json_messages(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ur10e_pose_info_multi_json_test_") as tmp:
            pose_info = Path(tmp) / "pose_info.json"
            first = {
                "pose": [
                    {
                        "name": "active_tcp_marker",
                        "id": 1,
                        "position": {"x": 0.1},
                        "orientation": {"w": 1.0},
                    }
                ]
            }
            second = {
                "pose": [
                    {
                        "name": "active_tcp_marker",
                        "id": 2,
                        "position": {"x": 0.2},
                        "orientation": {"w": 1.0},
                    },
                    {
                        "name": "wrist_3_link",
                        "id": 3,
                        "position": {"z": 0.3},
                        "orientation": {"w": 1.0},
                    },
                ]
            }
            pose_info.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
            names, pose_by_name = gui_row._load_pose_info_names(pose_info)
            self.assertIn("active_tcp_marker", names)
            self.assertIn("wrist_3_link", names)
            self.assertEqual(pose_by_name["active_tcp_marker"]["id"], 2)

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

    def test_non_contact_default_action_timeout_covers_gui_load_evidence(self) -> None:
        prior_gui_evidence = {
            "step5a": (22.0, 45.2),
            "step5c": (60.0, 82.8),
            "step6a": (30.0, 52.8),
        }
        for stage_id, (duration_s, prior_video_duration_s) in prior_gui_evidence.items():
            with self.subTest(stage=stage_id):
                timeout_s = gazebo.default_action_result_timeout_s(
                    duration_s=duration_s,
                    entry_duration_s=4.0,
                )
                old_timeout_s = max(duration_s + 4.0 + 10.0, 30.0)
                self.assertGreater(timeout_s, old_timeout_s)
                self.assertGreaterEqual(timeout_s, prior_video_duration_s + 5.0)

    def test_action_timing_evidence_records_actual_vs_commanded_ratio(self) -> None:
        evidence = gazebo.build_action_timing_evidence(
            planned_trajectory_duration_s=60.0,
            entry_duration_s=4.0,
            result_wait_elapsed_s=102.4,
            observed_joint_state_samples=512,
            action_success=True,
        )
        self.assertEqual(evidence["schema"], "ur10e_gazebo_action_timing_evidence_v2")
        self.assertAlmostEqual(evidence["commanded_goal_duration_s"], 64.0)
        self.assertAlmostEqual(evidence["actual_vs_commanded_duration_ratio"], 1.6)
        self.assertAlmostEqual(evidence["inferred_speed_scale_from_action_result"], 0.625)
        self.assertEqual(
            evidence["duration_ratio_clock_domain"],
            "wall_clock_monotonic_vs_commanded_trajectory_time_from_start",
        )
        self.assertTrue(evidence["sim_time_real_time_factor_confound"])
        self.assertTrue(evidence["rtf_or_controller_speed_unresolved"])
        self.assertFalse(evidence["controller_speed_scaling_measured"])
        self.assertEqual(evidence["evidence_status"], "action_result_timing_recorded")
        self.assertEqual(
            evidence["timing_root_cause_status"],
            "wall_clock_action_elapsed_slowdown_recorded_rtf_or_controller_speed_unresolved",
        )
        self.assertIn("Gazebo real-time factor may explain the ratio", evidence["claim_limit"])

    def test_runner_dry_plan_writes_all_stage_traces_and_matrix_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ur10e_gazebo_matrix_test_") as tmp:
            out = Path(tmp)
            matrix_path = gazebo.run_matrix("all", out, execute=False)
            payload = json.loads(matrix_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "ur10e_gazebo_matrix_result_v1")
            self.assertEqual(payload["mode"], "gazebo_ros2_control")
            self.assertTrue(payload["gazebo_only"])
            self.assertFalse(payload["live_robot_command_authorized"])
            self.assertTrue(Path(payload["model_composition_audit_path"]).is_file())
            self.assertFalse(payload["model_composition_audit"]["tcp_visual_link_present"])
            self.assertEqual(payload["model_composition_audit"]["missing_eoat_visuals"], [])
            self.assertEqual([stage["stage_id"] for stage in payload["stages"]], list(offline.STAGE_REGISTRY))
            for stage in payload["stages"]:
                with self.subTest(stage=stage["stage_id"]):
                    self.assertEqual(stage["action_name"], gazebo.ACTION_NAME)
                    self.assertFalse(stage["force_physics_closed_loop"])
                    self.assertFalse(stage["software_force_loop_success"])
                    self.assertFalse(stage["force_contact_physics_proven"])
                    self.assertTrue(stage["acceptance"]["trace_written"])
                    self.assertTrue(stage["acceptance"]["trajectory_duration_nonzero"])
                    self.assertLessEqual(
                        stage["acceptance"]["max_commanded_ik_error_m"],
                        stage["acceptance"]["max_tcp_xy_path_error_limit_m"],
                    )
                    self.assertTrue(Path(stage["trace_path"]).is_file())

    def test_observer_visual_gate_rejects_old_global_flags_without_active_tcp_relation(self) -> None:
        old_row = {
            "gui_evidence_captured": True,
            "robot_posture_visible": True,
            "eoat_tooling_visible": True,
            "tcp_marker_visible": True,
            "surface_path_visible": True,
            "pose_source": "joint_states_to_runner_fk_tool0_base",
        }
        payload = gazebo.populate_observer_visual_pass(old_row)
        self.assertFalse(payload["observer_visual_pass"])
        self.assertIn("observer_review_present", payload["observer_visual_failure_reasons"])
        self.assertIn("robot_tool_surface_relation_visible", payload["observer_visual_failure_reasons"])
        self.assertIn("active_tcp_pose_source_valid", payload["observer_visual_failure_reasons"])
        self.assertIn("active_tcp_pose_frame_valid", payload["observer_visual_failure_reasons"])
        self.assertIn("clean_scene_capture", payload["observer_visual_failure_reasons"])

    def test_observer_visual_gate_rejects_missing_pose_frame(self) -> None:
        row = {
            "observer_review_present": True,
            "observer_visual_review_source": "human_observer_row_review_v1",
            "gui_evidence_captured": True,
            "robot_posture_visible": True,
            "eoat_tooling_visible": True,
            "tcp_marker_visible": True,
            "surface_path_visible": True,
            "robot_tool_surface_relation_visible": True,
            "pose_source": tcp_marker.POSE_SOURCE_ACTIVE_TCP,
            "clean_scene_capture": True,
        }
        payload = gazebo.populate_observer_visual_pass(row)
        self.assertFalse(payload["observer_visual_pass"])
        self.assertIn("active_tcp_pose_frame_valid", payload["observer_visual_failure_reasons"])

    def test_observer_visual_gate_accepts_active_tcp_eoat_clean_relation_with_actual_meshes(self) -> None:
        row = {
            "observer_review_present": True,
            "observer_visual_review_source": "human_observer_row_review_v1",
            "gui_evidence_captured": True,
            "robot_posture_visible": True,
            "eoat_tooling_visible": True,
            "actual_eoat_mesh_visual_present": True,
            "actual_contact_surface_mesh_visual_present": True,
            "primitive_proxy_not_primary_visual": True,
            "primitive_proxy_not_main_visual_cue": True,
            "observer_level_demo_realism": True,
            "tcp_marker_visible": True,
            "surface_path_visible": True,
            "robot_tool_surface_relation_visible": True,
            "pose_source": tcp_marker.POSE_SOURCE_ACTIVE_TCP,
            "pose_frame": gazebo.GAZEBO_WORLD_FRAME,
            "clean_scene_capture": True,
        }
        payload = gazebo.populate_observer_visual_pass(row)
        self.assertTrue(payload["observer_visual_pass"])
        self.assertEqual(payload["observer_visual_failure_reasons"], [])

    def test_observer_visual_gate_rejects_proxy_main_visual_cue_even_with_meshes(self) -> None:
        row = {
            "observer_review_present": True,
            "observer_visual_review_source": "human_observer_row_review_v1",
            "gui_evidence_captured": True,
            "robot_posture_visible": True,
            "eoat_tooling_visible": True,
            "actual_eoat_mesh_visual_present": True,
            "actual_contact_surface_mesh_visual_present": True,
            "primitive_proxy_not_primary_visual": True,
            "primitive_proxy_not_main_visual_cue": False,
            "observer_level_demo_realism": False,
            "tcp_marker_visible": True,
            "surface_path_visible": True,
            "robot_tool_surface_relation_visible": True,
            "pose_source": tcp_marker.POSE_SOURCE_ACTIVE_TCP,
            "pose_frame": gazebo.GAZEBO_WORLD_FRAME,
            "clean_scene_capture": True,
        }
        payload = gazebo.populate_observer_visual_pass(row)
        self.assertFalse(payload["observer_visual_pass"])
        self.assertIn("primitive_proxy_not_main_visual_cue", payload["observer_visual_failure_reasons"])
        self.assertIn("observer_level_demo_realism", payload["observer_visual_failure_reasons"])

    def test_gui_row_summary_requires_explicit_observer_review(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ur10e_gui_row_no_review_test_") as tmp:
            run_dir = Path(tmp)
            case_dir = self._write_gui_row_fixture(run_dir, observer_review=False)
            row = gui_row.build_row_summary(
                stage="step5b",
                view="close_detail",
                case_dir=case_dir,
                run_dir=run_dir,
                runner_rc=0,
                video_duration_s=12.0,
                gui_config_path=gui_row.DEFAULT_GUI_CONFIG_DIR / "close_detail.config",
            )
            self.assertFalse(row["observer_visual_pass"])
            self.assertFalse(row["observer_review_present"])
            self.assertIn("observer_review_present", row["observer_visual_failure_reasons"])
            self.assertIn("robot_arm_visible", row["observer_visual_failure_reasons"])
            self.assertEqual(row["pose_frame"], gazebo.GAZEBO_WORLD_FRAME)
            self.assertEqual(row["pose_source"], tcp_marker.POSE_SOURCE_ACTIVE_TCP)

    def test_gui_row_summary_populates_observer_visual_pass_from_review_and_marker_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ur10e_gui_row_review_test_") as tmp:
            run_dir = Path(tmp)
            case_dir = self._write_gui_row_fixture(run_dir, observer_review=True)
            row = gui_row.build_row_summary(
                stage="step5b",
                view="close_detail",
                case_dir=case_dir,
                run_dir=run_dir,
                runner_rc=0,
                video_duration_s=12.0,
                gui_config_path=gui_row.DEFAULT_GUI_CONFIG_DIR / "close_detail.config",
            )
            self.assertTrue(row["observer_visual_pass"], row["observer_visual_failure_reasons"])
            self.assertTrue(row["observer_review_present"])
            self.assertEqual(row["observer_visual_review_source"], "human_observer_row_review_v1")
            self.assertEqual(row["marker_style"], "observer_subtle")
            self.assertEqual(row["observer_visual_criteria"]["active_tcp_pose_source_valid"], True)
            self.assertEqual(row["observer_visual_criteria"]["active_tcp_pose_frame_valid"], True)
            self.assertEqual(
                row["live_scene_content_branch"],
                "enhanced_marker_present_but_robot_eoat_visuals_incomplete_in_live_ecm",
            )
            self.assertTrue(row["live_scene_enhanced_marker_visuals_present"])
            self.assertFalse(row["live_scene_tool0_eoat_visuals_present"])
            self.assertFalse(row["live_scene_eoat_affordance_visuals_present"])
            self.assertTrue(row["live_scene_content"]["does_not_override_observer_visual_gate"])
            self.assertTrue(row["actual_eoat_mesh_visual_present"])
            self.assertEqual(row["actual_eoat_mesh_visual_uri"], gazebo.EOAT_REAL_MESH_URI)
            self.assertTrue(row["actual_contact_surface_mesh_visual_present"])
            self.assertEqual(row["actual_contact_surface_mesh_uri"], gazebo.CONTACT_SURFACE_REAL_MESH_URI)
            self.assertTrue(row["primitive_proxy_not_primary_visual"])
            self.assertTrue(row["primitive_proxy_not_main_visual_cue"])
            self.assertTrue(row["observer_level_demo_realism"])
            self.assertTrue(row["scene_model_list_captured"])
            self.assertTrue(row["scene_info_captured"])
            self.assertEqual(row["scripted_camera_sha256"], "fixture-sha256")
            self.assertEqual(row["git_provenance"]["commit"], "fixture-commit")
            self.assertEqual(row["surface_frame"], gazebo.GAZEBO_WORLD_FRAME)
            self.assertEqual(row["surface_tcp_sanity"]["approach_normal_base"], [0.0, 0.0, -1.0])
            self.assertAlmostEqual(row["actual_vs_commanded_duration_ratio"], 1.6)
            self.assertAlmostEqual(row["inferred_speed_scale_from_action_result"], 0.625)
            self.assertEqual(
                row["duration_ratio_clock_domain"],
                "wall_clock_monotonic_vs_commanded_trajectory_time_from_start",
            )
            self.assertTrue(row["sim_time_real_time_factor_confound"])
            self.assertTrue(row["rtf_or_controller_speed_unresolved"])
            self.assertFalse(row["controller_speed_scaling_measured"])
            self.assertEqual(
                row["timing_root_cause_status"],
                "wall_clock_action_elapsed_slowdown_recorded_rtf_or_controller_speed_unresolved",
            )

    def test_visual_audit_summary_uses_per_row_observer_pass_counts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ur10e_gui_summary_test_") as tmp:
            run_dir = Path(tmp)
            case_dir = self._write_gui_row_fixture(run_dir, observer_review=True)
            row = gui_row.build_row_summary(
                stage="step5b",
                view="close_detail",
                case_dir=case_dir,
                run_dir=run_dir,
                runner_rc=0,
                video_duration_s=12.0,
                gui_config_path=gui_row.DEFAULT_GUI_CONFIG_DIR / "close_detail.config",
            )
            gui_row.write_row_summary(row, case_dir / "row_summary.json")
            summary = gui_row.build_visual_audit_summary(
                run_dir,
                stages=("step5b",),
                views=("close_detail",),
            )
            self.assertEqual(summary["schema"], "ur10e_gazebo_real_aligned_visual_audit_summary_v2")
            self.assertEqual(summary["expected_rows"], 1)
            self.assertEqual(summary["observer_visual_pass_count"], 1)
            self.assertEqual(summary["observer_visual_fail_count"], 0)
            self.assertTrue(summary["all_rows_observer_visual_pass"])
            self.assertEqual(summary["visual_review_status"], "per_row_observer_visual_pass")
            self.assertEqual(summary["timing_evidence_row_count"], 1)
            self.assertEqual(summary["actual_vs_commanded_timing_row_count"], 1)
            self.assertAlmostEqual(
                summary["representative_timing_rows"][0]["actual_vs_commanded_duration_ratio"],
                1.6,
            )
            self.assertTrue(summary["representative_timing_rows"][0]["sim_time_real_time_factor_confound"])

    def test_repo_gui_configs_are_clean_and_cover_required_view_roles(self) -> None:
        expected = {
            "context_overview.config": "context_overview",
            "interaction_view.config": "interaction_view",
            "side_view.config": "side_view",
            "close_detail.config": "close_detail",
        }
        for filename, view in expected.items():
            with self.subTest(view=view):
                path = gui_row.DEFAULT_GUI_CONFIG_DIR / filename
                self.assertTrue(path.is_file())
                source = path.read_text(encoding="utf-8")
                self.assertIn("<camera_pose>", source)
                self.assertNotIn("ComponentInspector", source)
                self.assertNotIn("EntityTree", source)
                self.assertTrue(gui_row.gui_config_is_clean(path))

    def test_gui_row_plugin_path_includes_local_and_ros_control_plugin_dirs(self) -> None:
        local_prefix = WORKSPACE / "install" / "ur10e_example_controllers"
        plugin_dirs = gui_row.gazebo_system_plugin_lib_dirs(local_prefix)

        self.assertEqual(plugin_dirs[0], local_prefix.resolve() / "lib")
        if Path("/opt/ros/humble/lib/libign_ros2_control-system.so").is_file():
            self.assertIn(Path("/opt/ros/humble/lib"), plugin_dirs)

    def _write_gui_row_fixture(self, run_dir: Path, *, observer_review: bool) -> Path:
        case_dir = gui_row.row_case_dir(run_dir, "step5b", "close_detail")
        (case_dir / "runner").mkdir(parents=True)
        (case_dir / "marker").mkdir(parents=True)
        (run_dir / "_visual_worlds").mkdir(parents=True)
        for name in ("start_root.png", "mid_root.png", "final_root.png", "gui_recording.mp4"):
            (case_dir / name).write_bytes(b"fixture")
        timing_evidence = gazebo.build_action_timing_evidence(
            planned_trajectory_duration_s=60.0,
            entry_duration_s=4.0,
            result_wait_elapsed_s=102.4,
            observed_joint_state_samples=512,
            action_success=True,
        )
        matrix_summary = {
            "schema": "ur10e_gazebo_matrix_result_v1",
            "stages": [
                {
                    "stage_id": "step5b",
                    "trace_path": str(case_dir / "runner" / "step5b" / "command_trace.csv"),
                    "force_contact_source": gazebo.FORCE_CONTACT_SOURCE,
                    "force_contact_physics_proven": False,
                    "execution": {
                        "action_accepted": True,
                        "result_status": 4,
                        "result_error_code": 0,
                        "result_error_string": "Goal successfully reached!",
                        "ok": True,
                        "observed_motion": True,
                        "blocker": None,
                        "result_timeout_s": 138.0,
                        "result_wait_elapsed_s": 102.4,
                        "timing_evidence": timing_evidence,
                        "force_closed_loop": True,
                    },
                    "acceptance": {
                        "force_loop_trace_written": True,
                        "force_contact_physics_proven": False,
                    },
                    "force_loop": {
                        "settled_within_tolerance_fraction": 0.91,
                    },
                }
            ],
        }
        matrix_summary["git_provenance"] = {
            "repo": "/tmp/fixture",
            "branch": "fixture-branch",
            "commit": "fixture-commit",
            "upstream": "origin/fixture-branch",
            "dirty": False,
            "dirty_entries": [],
            "ahead": 0,
            "behind": 0,
        }
        matrix_summary["model_composition_audit"] = {
            "schema": "ur10e_gazebo_model_composition_audit_v2",
            "actual_eoat_mesh_visual_present": True,
            "eoat_primary_visual_mesh_name": gazebo.EOAT_REAL_MESH_VISUAL_NAME,
            "eoat_primary_visual_mesh_uri": gazebo.EOAT_REAL_MESH_URI,
            "eoat_primitive_visual_remnants": [],
        }
        (case_dir / "runner" / "matrix_summary.json").write_text(
            json.dumps(matrix_summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        marker_manifest = {
            "schema": "ur10e_gazebo_tcp_marker_manifest_v2",
            "stage_id": "step5b",
            "spawned": True,
            "marker_style": "observer_subtle",
            "pose_count": 3,
            "pose_source": tcp_marker.POSE_SOURCE_ACTIVE_TCP,
            "pose_frame": gazebo.GAZEBO_WORLD_FRAME,
            "source_frame": gazebo.ACTIVE_TCP_FRAME,
            "tool_frame": gazebo.TOOL0_FRAME,
            "final_pose": {"x_m": -0.48, "y_m": 0.02, "z_m": 0.008044839},
        }
        (case_dir / "marker" / "tcp_marker_manifest.json").write_text(
            json.dumps(marker_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        scene_dir = case_dir / "scene_introspection"
        scene_dir.mkdir(parents=True)
        pose_names = [
            "active_tcp_marker",
            "wrist_3_link",
            *[f"active_tcp_marker::tcp_marker_link::{name}" for name in sorted(gui_row.ENHANCED_MARKER_VISUAL_NAMES)],
            *[
                f"wrist_3_link_fixed_joint_lump__{name}_visual_1"
                for name in sorted(gazebo.TOOL0_EOAT_VIEWER_VISUAL_NAMES)
            ],
            *[
                f"wrist_3_link_fixed_joint_lump__{name}_visual_2"
                for name in sorted(gazebo.EOAT_VIEWER_AFFORDANCE_VISUAL_NAMES)
            ],
        ]
        pose_info = {
            "pose": [
                {
                    "name": name,
                    "id": index,
                    "position": {"x": 0.0, "y": 0.0, "z": 0.01 * index},
                    "orientation": {"w": 1.0},
                }
                for index, name in enumerate(pose_names, start=1)
            ]
        }
        (scene_dir / "pose_info.json").write_text(json.dumps(pose_info, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (scene_dir / "model_list.txt").write_text("active_tcp_marker\nur10e_gazebo_matrix\n", encoding="utf-8")
        introspection_summary = {
            "scene_info.json": {"returncode": 0, "timed_out": False, "bytes": 8},
            "model_list.txt": {"returncode": 0, "timed_out": False, "bytes": 36},
        }
        (scene_dir / "introspection_summary.json").write_text(
            json.dumps(introspection_summary, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        scripted_camera_capture = {
            "schema": "ur10e_gazebo_scripted_camera_capture_v1",
            "topic": "/ur10e_visual_audit/step5b/close_detail/image",
            "output": str(case_dir / "scripted_camera_final.png"),
            "captured": True,
            "ok": True,
            "sha256": "fixture-sha256",
            "observer_review_still_required": True,
            "clean_scene_capture": True,
        }
        (case_dir / "scripted_camera_final.png").write_bytes(b"fixture")
        (case_dir / "scripted_camera_capture.json").write_text(
            json.dumps(scripted_camera_capture, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        visual_manifest = {
            "schema": "ur10e_gazebo_stage_visual_world_manifest_v2",
            "stage_id": "step5b",
            "surface_frame": gazebo.GAZEBO_WORLD_FRAME,
            "surface": {"top_z_m": 0.008044839},
            "final_visual_pose_world": {"frame": gazebo.GAZEBO_WORLD_FRAME, "x_m": -0.48, "y_m": 0.02, "z_m": 0.008044839},
            "contact_target_pose_world": {"frame": gazebo.GAZEBO_WORLD_FRAME, "x_m": -0.48, "y_m": 0.02, "z_m": 0.008044839},
            "surface_tcp_sanity": {
                "same_frame": gazebo.GAZEBO_WORLD_FRAME,
                "final_reference_xy_inside_surface": True,
                "contact_target_world_xy_inside_surface": True,
                "contact_target_z_minus_surface_top_m": 0.0,
                "reaction_normal_base": [0.0, 0.0, 1.0],
                "approach_normal_base": [0.0, 0.0, -1.0],
                "approach_dot_reaction": -1.0,
            },
            "surface_mesh_visual": {
                "primary_visual_name": gazebo.CONTACT_SURFACE_REAL_MESH_VISUAL_NAME,
                "primary_visual_uses_real_mesh": True,
                "mesh_uri": gazebo.CONTACT_SURFACE_REAL_MESH_URI,
                "scale": "0.001 0.001 0.001",
                "pose_xyz_rpy": "0.0364678879 0.075 -0.0968663777 1.57079632679 0 0",
                "collision_primitive_remains": True,
                "collision_primitive_policy": "box collision retained only as simplified collision/contact sensor surface",
            },
        }
        (run_dir / "_visual_worlds" / "step5b_visual.manifest.json").write_text(
            json.dumps(visual_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if observer_review:
            review = {
                "observer_visual_review_source": "human_observer_row_review_v1",
                "reviewed_at": "2026-06-20T17:55:24+08:00",
                "robot_posture_visible": True,
                "eoat_tooling_visible": True,
                "tcp_marker_visible": True,
                "surface_path_visible": True,
                "robot_tool_surface_relation_visible": True,
                "primitive_proxy_not_main_visual_cue": True,
                "observer_level_demo_realism": True,
                "clean_scene_capture": True,
                "obstructive_ui_panels_absent": True,
                "notes": "Fixture review confirms coherent close-detail relation.",
            }
            (case_dir / "observer_review.json").write_text(
                json.dumps(review, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return case_dir


if __name__ == "__main__":
    unittest.main()
