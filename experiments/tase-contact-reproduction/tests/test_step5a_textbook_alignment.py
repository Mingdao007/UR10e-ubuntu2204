#!/usr/bin/env python3
"""Offline tests for the Step5a Local Control textbook migration gate."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import audit_step5a_textbook_alignment as textbook_audit  # noqa: E402


class Step5aTextbookAlignmentTest(unittest.TestCase):
    def test_textbook_alignment_audit_passes_current_tree(self) -> None:
        result = textbook_audit.audit()
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(result["active_baseline"], "step5a_cycloid_no_contact_v3")
        self.assertTrue(result["spec_path"].endswith("step5a_local_control_spec.json"))


if __name__ == "__main__":
    unittest.main()
