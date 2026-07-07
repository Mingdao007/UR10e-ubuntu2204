#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V4_RUN = ROOT / "runs/bridge_step4e_line_outerloop_step5d_strict_rnn_no_contact_p0_v4_20260707_100803"


class Step5dRnnP0RailSimulationTest(unittest.TestCase):
    def test_synthetic_comparison_separates_safe_and_oversized_rail_behavior(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "simulate_step5d_rnn_p0_rail.py"),
                "--json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["schema"], "step5d_rnn_p0_rail_simulation_v1")
        self.assertEqual(payload["safety_boundary"], "offline_only_no_robot_no_bridge_no_controller")
        self.assertEqual(payload["synthetic"]["p0_safe_warm_start"]["rnn_rail_fraction"], 0.0)
        self.assertEqual(payload["synthetic"]["p0_safe_warm_start"]["active_bounds_count_first"], 0)
        self.assertLessEqual(payload["synthetic"]["p0_safe_warm_start"]["constraint_residual_norm_first"], 1e-3)
        self.assertGreater(payload["synthetic"]["oversized_after_limiter"]["feasibility_scale_min"], 0.0)
        self.assertLess(payload["synthetic"]["oversized_after_limiter"]["feasibility_scale_min"], 1.0)
        self.assertGreaterEqual(payload["synthetic"]["oversized_without_limiter"]["rnn_rail_fraction"], 0.5)
        self.assertGreaterEqual(payload["synthetic"]["oversized_without_limiter"]["dls_rail_fraction"], 0.5)

    def test_v4_run_dir_csv_audit_reports_rail_and_stale_analysis(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "simulate_step5d_rnn_p0_rail.py"),
                "--run-dir",
                str(V4_RUN),
                "--json",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        payload = json.loads(completed.stdout)
        audit = payload["csv_audit"]
        self.assertEqual(audit["run_dir"], str(V4_RUN))
        self.assertEqual(audit["stage25_rows"], 2069)
        self.assertGreater(audit["rnn_rail_fraction"], 0.99)
        self.assertGreater(audit["all_joints_rail_fraction"], 0.99)
        self.assertEqual(audit["persisted_analysis_classification"], "entered_stage25")
        self.assertEqual(audit["current_analysis_classification"], "no_contact_p0_verifier_failed")
        self.assertTrue(audit["persisted_analysis_stale"])


if __name__ == "__main__":
    unittest.main()
