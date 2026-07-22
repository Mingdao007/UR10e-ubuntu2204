#!/usr/bin/env python3
"""Tests for the local project validation entrypoint."""

from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProjectCheckEntrypointTest(unittest.TestCase):
    def test_check_sh_is_project_local_and_uses_known_pytest_gate(self) -> None:
        check = ROOT / "check.sh"
        self.assertTrue(check.is_file(), "missing project-local check.sh")
        mode = os.stat(check).st_mode
        self.assertTrue(mode & stat.S_IXUSR, "check.sh must be executable")

        text = check.read_text(encoding="utf-8")
        self.assertIn("tools/run_ur10e_impacted_tests.py", text)
        dependency_map = (ROOT / "config/ur10e_test_dependency_map_v1.json").read_text()
        self.assertIn("tools/validate_tase_protocol_table.py", dependency_map)
        self.assertIn("tools/validate_cross_step_parameter_table.py", dependency_map)
        self.assertIn("PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", text)
        self.assertIn("UR10E_CHANGED_PATHS_FILE", text)
        self.assertIn("UR10E_FULL_SUITE", text)
        self.assertIn("UR10E_TEST_REUSE", text)
        self.assertIn("UR10E_PARALLEL", text)
        self.assertIn("uv sync --frozen --only-group test-hermetic", text)
        self.assertNotIn("requirements-test.txt", text)
        self.assertNotIn("/home/andy/.local/bin/check.sh", text)


if __name__ == "__main__":
    unittest.main()
