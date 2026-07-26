#!/usr/bin/env python3
"""Tests for the UR contact force/frame semantic gate."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import ur_contact_semantic_gate as semantic_gate  # noqa: E402


class UrContactSemanticGateTest(unittest.TestCase):
    def test_static_scan_blocks_old_raw_force_orientation_pattern(self) -> None:
        result = semantic_gate.static_scan_step5d_outer_loop()
        self.assertTrue(result["pass"], result)
        self.assertEqual(result["hits"], [])

    def test_replay_v4_stage25_rows_pass_fixed_semantic_contract(self) -> None:
        csv_path = (
            ROOT
            / "runs"
            / "bridge_step5d_strict_rnn_liveprep_v4_20260614_234951"
            / "bridge_rtde_500hz.csv"
        )
        summary, rows = semantic_gate.replay_csv(csv_path, max_rows=8, require_failure_contrast=True)
        self.assertTrue(summary["pass"], summary)
        self.assertLess(summary["max_orientation_error_abs_diff_rad"], 1e-9)
        self.assertGreater(summary["min_R_d_z_dot_R_cur_z"], 0.99)
        self.assertLess(summary["max_outer_xdot_norm"], 1.0)
        self.assertTrue(summary["failure_contrast_required"])
        self.assertTrue(summary["failure_contrast_pass"])
        self.assertGreater(summary["logged_bad_fixed_good_rows"], 0)
        self.assertGreater(summary["max_logged_step4e_orientation_error_rad"], 0.9)
        self.assertTrue(rows)
        self.assertTrue(rows[0]["logged_bad_fixed_good"])
        self.assertLess(float(rows[0]["outer_orientation_error_rad"]), 0.1)
        self.assertGreater(float(rows[0]["logged_step4e_orientation_error_rad"]), 0.9)

    def test_run_gate_writes_offline_artifact(self) -> None:
        csv_path = (
            ROOT
            / "runs"
            / "bridge_step5d_strict_rnn_liveprep_v4_20260614_234951"
            / "bridge_rtde_500hz.csv"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = semantic_gate.run_gate([csv_path], output_dir=Path(tmpdir), max_rows=4)
            self.assertTrue(payload["overall_pass"], payload)
            self.assertTrue(payload["strict_rnn_eq23_sign_gate"]["pass"])
            self.assertEqual(
                payload["strict_rnn_eq23_sign_gate"]["lambda_update_form"],
                "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
            )
            self.assertTrue(payload["replay_summaries"][0]["failure_contrast_required"])
            self.assertTrue(payload["replay_summaries"][0]["failure_contrast_pass"])
            self.assertTrue((Path(tmpdir) / "ur_contact_semantic_gate_summary.json").exists())


if __name__ == "__main__":
    unittest.main()
