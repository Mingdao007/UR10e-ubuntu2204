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
        self.assertIn("tests/test_step5d_remote_minimal_runtime.py", text)
        self.assertIn("tests/test_step5d_remote_minimal_live.py", text)
        self.assertIn("tests/test_step5d_paper_outer_loop.py", text)
        self.assertIn("PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", text)
        self.assertIn("uv sync --frozen --only-group test-hermetic", text)
        self.assertNotIn("run_ur10e_impacted_tests.py", text)
        self.assertNotIn("ur10e_test_dependency_map_v1.json", text)
        self.assertNotIn("requirements-test.txt", text)
        self.assertNotIn("/home/andy/.local/bin/check.sh", text)


if __name__ == "__main__":
    unittest.main()
