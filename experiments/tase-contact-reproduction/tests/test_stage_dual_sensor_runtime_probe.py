#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
MODULE_PATH = TOOLS / "capture_stage_dual_sensor_runtime_probe.py"


def import_probe_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing probe: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("capture_stage_dual_sensor_runtime_probe", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command_result(stdout: str, returncode: int = 0) -> dict[str, Any]:
    return {
        "command": ["test"],
        "returncode": returncode,
        "timed_out": False,
        "stdout": stdout,
        "stderr": "",
    }


def visual_only_command_results() -> dict[str, dict[str, Any]]:
    return {
        "ros_topic_list": command_result(
            "\n".join(
                [
                    "/clock [rosgraph_msgs/msg/Clock]",
                    "/joint_states [sensor_msgs/msg/JointState]",
                    "/tf [tf2_msgs/msg/TFMessage]",
                    "/tf_static [tf2_msgs/msg/TFMessage]",
                    "/joint_trajectory_controller/state [control_msgs/msg/JointTrajectoryControllerState]",
                ]
            )
            + "\n"
        ),
        "ros_node_list": command_result("/controller_manager\n/robot_state_publisher\n"),
        "ign_topic_list": command_result(
            "\n".join(
                [
                    "/clock",
                    "/ur10e_visual_audit/step5a/close_detail/image",
                    "/ur10e_visual_audit/step5a/close_detail/camera_info",
                    "/world/ur10e_step5_table_world/state",
                ]
            )
            + "\n"
        ),
        "ign_service_list": command_result("/world/ur10e_step5_table_world/state\n"),
    }


def dual_sensor_topic_command_results() -> dict[str, dict[str, Any]]:
    results = visual_only_command_results()
    results["ros_topic_list"]["stdout"] += (
        "/ur10e/simulated_ft/wrench [geometry_msgs/msg/WrenchStamped]\n"
        "/ur10e/simulated_ft/status [diagnostic_msgs/msg/DiagnosticStatus]\n"
        "/ur10e/step_status [std_msgs/msg/String]\n"
    )
    results["ign_topic_list"]["stdout"] += "/ur10e/contact/gazebo/step5b/contacts\n"
    return results


class StageDualSensorRuntimeProbeTest(unittest.TestCase):
    def test_visual_runtime_topics_do_not_upgrade_without_contact_or_sim_ft(self) -> None:
        probe = import_probe_module()
        payload = probe.build_probe(
            stage_id="step5b",
            observation_id="stage-step5b-runtime-probe-001",
            pids=[1002405],
            process_env={
                "ROS_DOMAIN_ID": "215",
                "ROS_LOCALHOST_ONLY": "0",
                "IGN_PARTITION": "ur10e_visible_control_review_1002384",
                "GZ_PARTITION": "ur10e_visible_control_review_1002384",
            },
            command_results=visual_only_command_results(),
            generated_start="2026-06-21T11:45:00+00:00",
            generated_end="2026-06-21T11:45:05+00:00",
        )

        self.assertEqual(payload["schema"], "ur10e_stage_dual_sensor_runtime_probe_v1")
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertFalse(payload["same_run_stage_dual_sensor_observation_proven"])
        self.assertTrue(payload["classification"]["visual_runtime_supported"])
        self.assertFalse(payload["classification"]["contact_runtime_topics_available"])
        self.assertFalse(payload["classification"]["simulated_ft_runtime_topics_available"])
        self.assertFalse(payload["capture_path_topics_available"])
        self.assertIn("stage_contact_pair_or_state_topic:missing", payload["blockers"])
        self.assertIn("stage_simulated_ft_wrench_topic:missing", payload["blockers"])
        self.assertFalse(payload["live_authorization"]["real_bench_live_contact_authorized"])

    def test_dual_sensor_topics_only_make_capture_path_available_not_proven(self) -> None:
        probe = import_probe_module()
        payload = probe.build_probe(
            stage_id="step5b",
            observation_id="stage-step5b-runtime-probe-002",
            pids=[1002405],
            process_env={"ROS_DOMAIN_ID": "215", "IGN_PARTITION": "same_run_partition"},
            command_results=dual_sensor_topic_command_results(),
            generated_start="2026-06-21T11:46:00+00:00",
            generated_end="2026-06-21T11:46:05+00:00",
        )

        self.assertTrue(payload["classification"]["visual_runtime_supported"])
        self.assertTrue(payload["classification"]["contact_runtime_topics_available"])
        self.assertTrue(payload["classification"]["simulated_ft_runtime_topics_available"])
        self.assertTrue(payload["capture_path_topics_available"])
        self.assertFalse(payload["same_run_stage_dual_sensor_observation_proven"])
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertIn("same_run_stage_dual_sensor_observation:not_proven", payload["blockers"])
        self.assertIn("physical Gazebo collision/contact physics", payload["forbidden_claim"])

    def test_command_failure_is_recorded_as_blocker(self) -> None:
        probe = import_probe_module()
        results = visual_only_command_results()
        results["ign_topic_list"] = command_result("", returncode=1)
        payload = probe.build_probe(
            stage_id="step5b",
            observation_id="stage-step5b-runtime-probe-003",
            pids=[],
            process_env={},
            command_results=results,
            generated_start="2026-06-21T11:47:00+00:00",
            generated_end="2026-06-21T11:47:05+00:00",
        )

        self.assertIn("runtime_probe_command_failures:ign_topic_list", payload["blockers"])
        self.assertFalse(payload["capture_path_topics_available"])


if __name__ == "__main__":
    unittest.main()
