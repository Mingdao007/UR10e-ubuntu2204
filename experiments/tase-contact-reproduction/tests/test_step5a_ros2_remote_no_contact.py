#!/usr/bin/env python3
"""Focused tests for the Step5a ROS2 remote no-contact gate."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_step5d_remote"))
sys.path.insert(0, str(ROOT / "tools"))

import step5_table  # noqa: E402
from ur10e_step5d_remote.replay_step5a_no_contact import (  # noqa: E402
    DEFAULT_CONFIG,
    TRACE_FIELDS,
    load_step5a_config,
    run_step5a_no_contact_replay,
)


class Step5aRos2RemoteNoContactTest(unittest.TestCase):
    def test_default_config_and_launch_are_no_motion(self) -> None:
        config = load_step5a_config(DEFAULT_CONFIG)
        self.assertFalse(config["enable_motion"])
        self.assertFalse(config["live_air_motion_authorized"])
        self.assertTrue(config["requires_5a0_driver_readiness"])
        self.assertEqual(config["robot_ip"], "192.168.1.18")
        self.assertEqual(config["reverse_ip"], "192.168.1.10")
        policy = config["contact_policy"]
        self.assertFalse(policy["force_control"])
        self.assertFalse(policy["contact_search"])
        self.assertFalse(policy["kunwei_bridge_required"])
        self.assertFalse(policy["zero_ftsensor"])
        self.assertFalse(policy["tcp_payload_write"])
        self.assertFalse(policy["external_control_urcap"])

        launch = (WORKSPACE / "src" / "ur10e_step5d_remote" / "launch" / "step5a_remote_shadow.launch.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('DeclareLaunchArgument("enable_motion", default_value="false")', launch)
        self.assertIn("replay_step5a_no_contact", launch)

    def test_no_contact_shadow_emits_schema_and_blocks_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            summary = run_step5a_no_contact_replay(config_path=DEFAULT_CONFIG, output_dir=out)
            payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            with (out / "shadow_trace.csv").open(newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle))
        self.assertEqual(payload["artifact_dir"], summary["artifact_dir"])
        self.assertEqual(summary["mode"], "step5a_ros2_remote_no_contact_shadow_no_live_robot_action")
        self.assertFalse(summary["cmd_enabled_any"])
        self.assertEqual(summary["normal_load_n"]["max"], 0.0)
        self.assertEqual(summary["force_norm_n"]["max"], 0.0)
        self.assertLessEqual(summary["max_reference_speed_m_s"], summary["velocity_cap_m_s"])
        self.assertTrue(all(summary["acceptance"].values()))
        for field in TRACE_FIELDS:
            self.assertIn(field, header)

    def test_stage_table_places_step5a_remote_before_step5b_contact(self) -> None:
        table = step5_table.load_step5_table()
        remote_5a = step5_table.step5_stage("step5a_ros2_remote_no_contact_v1", table)
        remote_5b = step5_table.step5_stage("step5b_ros2_remote_shadow_v1", table)
        self.assertEqual(remote_5a["owner"], "ROS2 remote shadow")
        self.assertFalse(remote_5a["active"])
        self.assertFalse(remote_5a["contact"])
        self.assertFalse(remote_5a["bridge"])
        self.assertTrue(remote_5a["guard"]["requires_5a0_driver_readiness"])
        self.assertFalse(remote_5a["guard"]["enable_motion_default"])
        self.assertEqual(remote_5a["contact_policy"]["live_authorization"], "none_until_explicit_step5a_remote_air_motion_gate")
        self.assertEqual(remote_5a["filter_policy"]["normal_filter"], "none")
        self.assertEqual(
            remote_5b["guard"]["prerequisites"],
            [
                "step5a0_ros2_headless_driver_readiness_pass",
                "step5a_ros2_remote_no_contact_v1_pass",
            ],
        )

    def test_bringup_defaults_match_remote_control_no_motion_gate(self) -> None:
        launch = (WORKSPACE / "src" / "ur10e_bringup" / "launch" / "ur10e_control.launch.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('DeclareLaunchArgument("launch_rviz", default_value="false")', launch)
        self.assertIn('DeclareLaunchArgument("headless_mode", default_value="true")', launch)
        self.assertIn('DeclareLaunchArgument("reverse_ip", default_value="192.168.1.10")', launch)
        self.assertIn('DeclareLaunchArgument("activate_joint_controller", default_value="false")', launch)
        self.assertIn('"activate_joint_controller": activate_joint_controller', launch)


if __name__ == "__main__":
    unittest.main()
