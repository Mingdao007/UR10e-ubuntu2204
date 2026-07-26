#!/usr/bin/env python3
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = ROOT / "step5b_ros2_headless_live.sh"


class Step5bRos2HeadlessLiveEntrypointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = ENTRYPOINT.read_text(encoding="utf-8")

    def test_formal_live_entrypoint_targets_velocity_admittance_runner(self) -> None:
        self.assertIn('RUNNER="step5b_velocity_admittance_runner"', self.text)
        self.assertIn('ros2 run ur10e_example_controllers "${RUNNER}"', self.text)
        self.assertNotIn("step5b_contact_live_runner", self.text)

    def test_readiness_is_forward_velocity_controller_based(self) -> None:
        self.assertIn('VELOCITY_CONTROLLER="${VELOCITY_CONTROLLER:-forward_velocity_controller}"', self.text)
        self.assertIn('initial_joint_controller:="${VELOCITY_CONTROLLER}"', self.text)
        self.assertIn("velocity_controller_available", self.text)
        self.assertIn("ros2 control list_controllers", self.text)
        self.assertNotIn("ros2 action list", self.text)
        self.assertNotIn("scaled_joint_trajectory_controller/follow_joint_trajectory", self.text)

    def test_live_run_remains_locked_until_auditor_acceptance(self) -> None:
        run_case = re.search(r"\n  run\)\n(?P<body>.*?)\n\n    run_dir=", self.text, re.S)
        self.assertIsNotNone(run_case)
        body = run_case.group("body")
        self.assertIn("temporarily locked", body)
        self.assertIn("exit 44", body)


if __name__ == "__main__":
    unittest.main()
