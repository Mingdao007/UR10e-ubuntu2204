#!/usr/bin/env python3
"""Static no-live checks for active ROS2 build entrypoints."""

from __future__ import annotations

import unittest
from pathlib import Path


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]


class ColconParallelEntrypointsTest(unittest.TestCase):
    def test_active_entrypoints_use_single_tree_parallel_helper(self) -> None:
        for name in (
            "no_contact_test.sh",
            "step5a_gate_a_audit.sh",
            "step5a_driver_lifecycle_check.sh",
            "kunwei_force_check.sh",
            "step5b_zero_policy_check.sh",
        ):
            text = (WORKSPACE / name).read_text(encoding="utf-8")
            self.assertIn("scripts/ur10e_colcon_build.py", text, name)
            self.assertNotIn("colcon build --packages-select", text, name)

    def test_helper_assigns_two_packages_eight_cores_each(self) -> None:
        text = (WORKSPACE / "scripts/ur10e_colcon_build.py").read_text(encoding="utf-8")
        self.assertIn('"CMAKE_BUILD_PARALLEL_LEVEL": "8"', text)
        self.assertIn('"--executor",', text)
        self.assertIn('"parallel",', text)
        self.assertIn('"--parallel-workers",', text)
        self.assertIn('ROOT / ".ur10e_colcon_build.lock"', text)


if __name__ == "__main__":
    unittest.main()
