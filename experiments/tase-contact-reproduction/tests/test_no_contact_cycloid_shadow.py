#!/usr/bin/env python3
"""Focused tests for the ROS2 remote no-contact cycloid gate."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))
sys.path.insert(0, str(ROOT / "tools"))

import step5_table  # noqa: E402
from ur10e_example_controllers.no_contact_cycloid_shadow import (  # noqa: E402
    DEFAULT_CONFIG,
    TRACE_FIELDS,
    load_no_contact_config,
    run_no_contact_shadow,
)


class NoContactCycloidShadowTest(unittest.TestCase):
    def test_default_config_and_launch_are_no_motion(self) -> None:
        config = load_no_contact_config(DEFAULT_CONFIG)
        self.assertFalse(config["enable_motion"])
        self.assertFalse(config["live_air_motion_authorized"])
        self.assertTrue(config["requires_5a0_driver_readiness"])
        self.assertEqual(config["force_source"], "kunwei_kwr75b_tcp_pre_motion_data_gate")
        self.assertTrue(config["kunwei_data_gate_required"])
        self.assertTrue(config["ur_internal_force_delta_gate"])
        self.assertEqual(config["robot_ip"], "192.168.1.18")
        self.assertEqual(config["reverse_ip"], "192.168.1.10")
        policy = config["contact_policy"]
        self.assertFalse(policy["force_control"])
        self.assertFalse(policy["contact_search"])
        self.assertTrue(policy["kunwei_bridge_required"])
        self.assertTrue(policy["kunwei_data_gate_required"])
        self.assertTrue(policy["ur_internal_force_delta_gate"])
        self.assertFalse(policy["zero_ftsensor"])
        self.assertFalse(policy["tcp_payload_write"])
        self.assertFalse(policy["external_control_urcap"])

        launch = (WORKSPACE / "src" / "ur10e_example_controllers" / "launch" / "no_contact_cycloid_shadow.launch.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('DeclareLaunchArgument("enable_motion", default_value="false")', launch)
        self.assertIn("no_contact_cycloid_shadow", launch)

    def test_no_contact_shadow_emits_schema_and_blocks_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            summary = run_no_contact_shadow(config_path=DEFAULT_CONFIG, output_dir=out)
            payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            with (out / "shadow_trace.csv").open(newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle))
        self.assertEqual(payload["artifact_dir"], summary["artifact_dir"])
        self.assertEqual(summary["mode"], "no_contact_cycloid_shadow_no_live_robot_action")
        self.assertFalse(summary["cmd_enabled_any"])
        self.assertEqual(summary["normal_load_n"]["max"], 0.0)
        self.assertEqual(summary["force_norm_n"]["max"], 0.0)
        self.assertEqual(summary["force_source"], "kunwei_kwr75b_tcp_pre_motion_data_gate")
        self.assertTrue(summary["kunwei_data_gate_required"])
        self.assertEqual(summary["kunwei_bridge_mode"], "direct_tcp_no_legacy_bridge")
        self.assertTrue(summary["ur_internal_force_delta_gate"])
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
        self.assertIn('DeclareLaunchArgument("launch_dashboard_client", default_value="false")', launch)
        self.assertIn('DeclareLaunchArgument("activate_joint_controller", default_value="false")', launch)
        self.assertIn('"activate_joint_controller": activate_joint_controller', launch)

    def test_root_no_contact_script_is_short_safe_entrypoint(self) -> None:
        script_path = WORKSPACE / "no_contact_test.sh"
        script = script_path.read_text(encoding="utf-8")
        self.assertTrue(script_path.exists())
        self.assertTrue(script_path.stat().st_mode & 0o111)
        self.assertIn('SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"', script)
        self.assertIn("source_setup /opt/ros/humble/setup.bash", script)
        self.assertIn("no_contact_cycloid_shadow", script)
        self.assertIn("Could not get configuration package", script)
        self.assertIn("no motion was attempted", script)
        self.assertIn("step5a_driver_readiness_check", script)
        self.assertIn("step5a_cartesian_cycloid_motion", script)
        self.assertNotIn("step5a_joint_proxy_motion_probe", script)
        self.assertIn("READINESS_USE_ROS_CLI_PROBES", script)
        self.assertIn("ros2 topic echo --no-daemon --once /joint_states", script)
        self.assertIn("ros2 control list_controllers", script)
        self.assertIn("joint_state_broadcaster.*active", script)
        self.assertIn("READINESS_PROBE_KILL_AFTER_S", script)
        self.assertIn("setsid timeout --kill-after", script)
        self.assertIn("driver_failure:", script)
        self.assertIn("stop_process_group", script)
        self.assertIn("launch_dashboard_client:=false", script)
        self.assertIn("NO_CONTACT_READINESS_ONLY", script)
        self.assertIn("NO_CONTACT_LIVE_MOTION", script)
        self.assertIn("KUNWEI_FORCE_GATE_REQUIRED", script)
        self.assertIn("kunwei_persistent_gate", script)
        self.assertIn("kunwei_persistent_monitor.json", script)
        self.assertIn("persistent Kunwei monitor failed; no air motion was attempted.", script)
        self.assertIn("refusing to move without Kunwei persistent monitor", script)
        self.assertIn("readiness-only stop; no live motion was attempted.", script)
        self.assertIn("To move the robot in no-contact mode, run: step5a_live_no_contact_test.sh", script)
        self.assertIn("readiness_log_passed", script)
        self.assertIn("Successful 'activate' of hardware 'ur10e'", script)
        self.assertIn("Configured and activated .*joint_state_broadcaster", script)
        self.assertIn("--kunwei-max-force-delta-n", script)
        self.assertIn("step5a_cartesian_cycloid_motion_trace.csv", script)
        self.assertLess(script.find("step5a_driver_readiness_check"), script.find("step5a_cartesian_cycloid_motion"))
        self.assertLess(script.find("Could not get configuration package"), script.find("step5a_cartesian_cycloid_motion"))

        live_probe = (
            WORKSPACE
            / "src"
            / "ur10e_example_controllers"
            / "ur10e_example_controllers"
            / "step5a_cartesian_cycloid_motion.py"
        ).read_text(encoding="utf-8")
        self.assertIn("KunweiPersistentMonitor", live_probe)
        self.assertIn("step5a_live_no_contact_cartesian_cycloid", live_probe)
        self.assertIn("motion_kind", live_probe)
        self.assertIn("cartesian_cycloid", live_probe)
        self.assertIn("ur_internal_force_delta_advisory", live_probe)
        self.assertIn("not_required_for_step5a_force_source", live_probe)
        self.assertIn("LIVE_TRACE_FIELDS", live_probe)
        self.assertIn("send_goal_async", live_probe)
        self.assertIn("cancel_goal_async", live_probe)
        self.assertIn('"Program running: true"', live_probe)
        self.assertNotIn('responses.get("running") != "Program running: false"', live_probe)
        self.assertIn("build_calibrated_model", live_probe)
        self.assertIn("validate_cartesian_acceptance_summary", live_probe)
        self.assertIn("pinocchio_calibrated_tool0_warm_start_deterministic_dls", live_probe)
        self.assertIn("trajectory_authority_entered", live_probe)

        persistent_monitor = (
            WORKSPACE
            / "src"
            / "ur10e_example_controllers"
            / "ur10e_example_controllers"
            / "kunwei_persistent_monitor.py"
        ).read_text(encoding="utf-8")
        self.assertIn("kunwei_kwr75b_tcp_persistent_force_monitor", persistent_monitor)
        self.assertIn("primary_step5a_force_source_persistent_monitor", persistent_monitor)
        self.assertIn("latest_max_age_s", persistent_monitor)
        self.assertIn("recent_sample_count", persistent_monitor)
        self.assertIn("max_observed_force_delta_n", persistent_monitor)
        self.assertIn("START_STREAM", persistent_monitor)
        self.assertIn("STOP_STREAM", persistent_monitor)

        kunwei_gate = (
            WORKSPACE
            / "src"
            / "ur10e_example_controllers"
            / "ur10e_example_controllers"
            / "kunwei_force_gate.py"
        ).read_text(encoding="utf-8")
        self.assertIn("kunwei_kwr75b_tcp_pre_motion_data_gate", kunwei_gate)
        self.assertIn("primary_step5a_force_source_evidence", kunwei_gate)
        self.assertIn("direct_tcp_no_bridge_process", kunwei_gate)
        self.assertIn("START_STREAM", kunwei_gate)
        self.assertIn("STOP_STREAM", kunwei_gate)
        self.assertIn("configuration_write_not_used", kunwei_gate)

        kunwei_script = (WORKSPACE / "kunwei_force_check.sh").read_text(encoding="utf-8")
        self.assertIn("kunwei_force_gate", kunwei_script)
        self.assertIn("kunwei_force_gate.json", kunwei_script)
        self.assertNotIn("no_contact_motion_probe", kunwei_script)

        live_script_path = WORKSPACE / "step5a_live_no_contact_test.sh"
        live_script = live_script_path.read_text(encoding="utf-8")
        self.assertTrue(live_script_path.exists())
        self.assertTrue(live_script_path.stat().st_mode & 0o111)
        self.assertIn("NO_CONTACT_LIVE_MOTION=true", live_script)
        self.assertIn("KUNWEI_FORCE_GATE_REQUIRED=true", live_script)
        self.assertIn('exec "${ROOT}/no_contact_test.sh"', live_script)

        no_contact_script = (WORKSPACE / "no_contact_test.sh").read_text(encoding="utf-8")
        self.assertIn("STEP5A_DRIVER_LAUNCH_ATTEMPTS", no_contact_script)
        self.assertIn("air_motion_launch_attempt_", no_contact_script)
        self.assertIn("5a0 driver startup failed before any motion; retrying", no_contact_script)
        self.assertIn("sent_goal=false", no_contact_script)

        return_script_path = WORKSPACE / "step5a_live_return_to_anchor.sh"
        return_script = return_script_path.read_text(encoding="utf-8")
        self.assertTrue(return_script_path.exists())
        self.assertTrue(return_script_path.stat().st_mode & 0o111)
        self.assertIn("step5a_return_to_anchor_motion", return_script)
        self.assertIn("source_run_dir", return_script)
        self.assertIn("return driver launch attempt", return_script)
        self.assertIn("sent_goal=false", return_script)
        self.assertNotIn("step5a_gate_a_audit", return_script)

        return_last_script_path = WORKSPACE / "step5a_return_last.sh"
        return_last_script = return_last_script_path.read_text(encoding="utf-8")
        self.assertTrue(return_last_script_path.exists())
        self.assertTrue(return_last_script_path.stat().st_mode & 0o111)
        self.assertIn('find "${RUN_ROOT}" -maxdepth 1 -type d -name "no_contact_test_*" | sort -r', return_last_script)
        self.assertIn("step5a_cartesian_cycloid_motion.json", return_last_script)
        self.assertIn('exec "${ROOT}/step5a_live_return_to_anchor.sh" "${SOURCE_RUN_DIR}"', return_last_script)

        gate_d_script_path = WORKSPACE / "step5a_driver_lifecycle_check.sh"
        gate_d_script = gate_d_script_path.read_text(encoding="utf-8")
        self.assertTrue(gate_d_script_path.exists())
        self.assertTrue(gate_d_script_path.stat().st_mode & 0o111)
        self.assertIn("activate_joint_controller:=true", gate_d_script)
        self.assertIn("step5a_driver_readiness_check", gate_d_script)
        self.assertIn("driver_lifecycle_diagnostic_not_step5a_acceptance", gate_d_script)
        self.assertIn("sent_goal=false", gate_d_script)
        self.assertNotIn("step5a_cartesian_cycloid_motion", gate_d_script)
        self.assertNotIn("send_goal_async", gate_d_script)

        gate_a_audit_script_path = WORKSPACE / "step5a_gate_a_audit.sh"
        gate_a_audit_script = gate_a_audit_script_path.read_text(encoding="utf-8")
        self.assertTrue(gate_a_audit_script_path.exists())
        self.assertTrue(gate_a_audit_script_path.stat().st_mode & 0o111)
        self.assertIn("step5a_gate_a_audit", gate_a_audit_script)
        self.assertNotIn("step5a_live_no_contact_test", gate_a_audit_script)
        self.assertNotIn("send_goal_async", gate_a_audit_script)

        setup_py = (WORKSPACE / "src" / "ur10e_example_controllers" / "setup.py").read_text(encoding="utf-8")
        self.assertIn("kunwei_persistent_gate = ur10e_example_controllers.kunwei_persistent_monitor:main", setup_py)
        self.assertIn(
            "step5a_driver_readiness_check = ur10e_example_controllers.step5a_driver_readiness_check:main",
            setup_py,
        )
        self.assertIn(
            "step5a_cartesian_cycloid_motion = ur10e_example_controllers.step5a_cartesian_cycloid_motion:main",
            setup_py,
        )
        self.assertIn(
            "step5a_return_to_anchor_motion = ur10e_example_controllers.step5a_return_to_anchor_motion:main",
            setup_py,
        )
        self.assertIn("step5a_gate_a_audit = ur10e_example_controllers.step5a_gate_a_audit:main", setup_py)
        self.assertIn(
            "step5a_joint_proxy_motion_probe = ur10e_example_controllers.step5a_joint_proxy_motion_probe:main",
            setup_py,
        )


if __name__ == "__main__":
    unittest.main()
