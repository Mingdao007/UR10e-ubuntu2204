#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
sys.path.insert(0, str(PACKAGE))


class CanonicalSimulatedFtRuntimeTest(unittest.TestCase):
    def test_module_and_console_script_are_registered(self) -> None:
        module_path = PACKAGE / "ur10e_example_controllers" / "canonical_simulated_ft_runtime.py"
        setup_path = PACKAGE / "setup.py"
        self.assertTrue(module_path.is_file(), module_path)
        setup_source = setup_path.read_text(encoding="utf-8")
        self.assertIn(
            "canonical_simulated_ft_runtime = ur10e_example_controllers.canonical_simulated_ft_runtime:main",
            setup_source,
        )

    def test_launch_file_exposes_remappable_topics_and_no_live_defaults(self) -> None:
        launch_path = PACKAGE / "launch" / "canonical_simulated_ft_runtime.launch.py"
        self.assertTrue(launch_path.is_file(), launch_path)
        source = launch_path.read_text(encoding="utf-8")
        for name in [
            "canonical_wrench_topic",
            "simulated_ft_wrench_topic",
            "simulated_ft_status_topic",
            "contact_state_topic",
            "controller_status_topic",
            "run_metadata_topic",
            "dry_run_summary",
            "publish_hz",
            "max_samples",
        ]:
            self.assertIn(f'DeclareLaunchArgument("{name}"', source)
            self.assertIn(f'LaunchConfiguration("{name}")', source)
        self.assertIn('executable="canonical_simulated_ft_runtime"', source)
        self.assertIn('"live_robot_command_authorized": False', source)
        self.assertIn('"bridge_start_authorized": False', source)
        self.assertNotIn("zero_ftsensor", source)

    def test_default_runtime_config_matches_canonical_contract(self) -> None:
        spec = importlib.util.find_spec("ur10e_example_controllers.canonical_simulated_ft_runtime")
        self.assertIsNotNone(spec)
        from ur10e_example_controllers import canonical_simulated_ft_runtime as runtime
        from ur10e_example_controllers import canonical_wrench_contract as contract

        config = runtime.build_runtime_config({})
        self.assertEqual(config.canonical_wrench_topic, contract.CANONICAL_WRENCH_TOPIC)
        self.assertEqual(config.simulated_ft_wrench_topic, contract.SIMULATED_FT_WRENCH_TOPIC)
        self.assertEqual(config.simulated_ft_status_topic, contract.SIMULATED_FT_STATUS_TOPIC)
        self.assertFalse(config.live_robot_command_authorized)
        self.assertFalse(config.bridge_start_authorized)
        self.assertEqual(config.source_switching_policy, "launch_config_or_remap_only_no_controller_logic")

    def test_dry_run_payload_contains_all_runtime_topics_and_valid_schema(self) -> None:
        from ur10e_example_controllers import canonical_simulated_ft_runtime as runtime
        from ur10e_example_controllers import canonical_wrench_contract as contract

        config = runtime.build_runtime_config({"max_samples": "4", "publish_hz": "20.0"})
        payload = runtime.build_dry_run_payload(config)
        self.assertEqual(payload["schema"], "ur10e_canonical_simulated_ft_runtime_dry_run_v1")
        self.assertFalse(payload["live_robot_command_authorized"])
        self.assertFalse(payload["bridge_start_authorized"])
        self.assertEqual(payload["topics"]["canonical_wrench"], contract.CANONICAL_WRENCH_TOPIC)
        self.assertEqual(payload["topics"]["simulated_ft_wrench"], contract.SIMULATED_FT_WRENCH_TOPIC)
        self.assertEqual(payload["topics"]["simulated_ft_status"], contract.SIMULATED_FT_STATUS_TOPIC)
        self.assertEqual(payload["topics"]["contact_state"], contract.CONTACT_STATE_TOPIC)
        self.assertEqual(payload["topics"]["controller_status"], contract.CONTROLLER_STATUS_TOPIC)
        self.assertEqual(payload["topics"]["run_metadata"], contract.RUN_METADATA_TOPIC)
        self.assertEqual(payload["wrench_trace"]["force_source"], contract.SOURCE_SIMULATED_FT)
        self.assertEqual(payload["wrench_trace"]["sample_count"], 4)
        self.assertFalse(payload["wrench_trace"]["schema_issues"])
        self.assertEqual(len(payload["status_rows"]), 4)
        self.assertEqual(len(payload["contact_state_rows"]), 4)
        json.dumps(payload)

    def test_empty_launch_dry_run_summary_argument_means_no_summary_file(self) -> None:
        from ur10e_example_controllers import canonical_simulated_ft_runtime as runtime

        args = runtime.parse_args(["--dry-run-summary", ""])
        config = runtime._config_from_args(args)
        self.assertIsNone(config.dry_run_summary)

    def test_runtime_source_has_no_live_or_private_gazebo_dependency(self) -> None:
        module_path = PACKAGE / "ur10e_example_controllers" / "canonical_simulated_ft_runtime.py"
        source = module_path.read_text(encoding="utf-8")
        forbidden = [
            "gazebo_joint_state_fk_virtual_surface_model",
            "kunwei_rtde_bridge",
            "zero_ftsensor",
            "load_program",
            "play_program",
            "URScript",
            "speedl(",
        ]
        for token in forbidden:
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
