#!/usr/bin/env python3
"""Offline tests for the Step5/Step6 Local Control textbook gate."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


EXPERIMENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT / "tools"))

import audit_local_control_textbook_alignment as textbook_audit  # noqa: E402


class LocalControlTextbookAlignmentTest(unittest.TestCase):
    def test_textbook_alignment_audit_passes_current_tree(self) -> None:
        result = textbook_audit.audit()
        self.assertTrue(result["ok"], result["failures"])
        self.assertEqual(
            result["covered_stages"],
            [
                "step5a_cycloid_no_contact_v3",
                "step5_contact_cycloid_baseline_v1",
                "step6a_eight_no_contact_v1",
                "step6_contact_eight_baseline_v2",
            ],
        )

    def test_step6a_fixed_z_metadata_matches_safe_frame(self) -> None:
        stage_table = json.loads((EXPERIMENT / "config" / "step6_stage_table.json").read_text(encoding="utf-8"))
        safe_frame = json.loads((EXPERIMENT / "config" / "step6_eight_safe_frame.json").read_text(encoding="utf-8"))
        step6a = {stage["id"]: stage for stage in stage_table["stages"]}["step6a_eight_no_contact_v1"]
        self.assertAlmostEqual(step6a["fixed_base_z_m"], safe_frame["fixed_z"]["fixed_base_z_m"], places=12)


if __name__ == "__main__":
    unittest.main()
