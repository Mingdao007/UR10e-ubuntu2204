#!/usr/bin/env python3
"""No-motion tests for the Step5d post-run force overview."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_step5d_force_overview import build  # noqa: E402


class Step5dForceOverviewTest(unittest.TestCase):
    def make_run(self, root: Path, *, successful: bool) -> Path:
        run_dir = root / "bridge_step5d_test"
        run_dir.mkdir()
        rows = []
        for index in range(601):
            t = index * 0.1
            rows.append(
                {
                    "t_monotonic_s": t,
                    "ur_output_double_register_35": 25.0,
                    "ur_output_double_register_30": 0.0,
                    "ur_safety_mode": 1.0,
                    "_step5d_contact_safety_state": 0.0,
                    "_step4e_normal_load_n": 12.0 + 0.2 * ((index % 5) - 2),
                    "target_force_n": 12.0,
                    "ur_actual_TCP_pose_0": 0.4 + index * 1e-6,
                    "ur_actual_TCP_pose_1": -0.2 + index * 2e-6,
                    "_step4e_desired_x_m": 0.4 + index * 1e-6,
                    "_step4e_desired_y_m": -0.2 + index * 2e-6,
                    "_step5d_stage25_row_gap_s": 0.002,
                    "rtde_feedback_age_s": 0.003,
                    "rtde_sent_echo_heartbeat_gap": 1.0,
                    "_step5d_rnn_vs_oracle_qdot_norm": 1e-8,
                }
            )
        rows.append({**rows[-1], "t_monotonic_s": 60.1, "ur_output_double_register_35": 26.0,
                     "ur_output_double_register_30": 1.0 if successful else 12.0})
        pd.DataFrame(rows).to_csv(run_dir / "bridge_rtde_500hz.csv", index=False)
        marker = {
            "immutable": True,
            "capture_closed": True,
            "capture_succeeded": successful,
            "exit_codes": {"bridge_child": 0, "monitor": 0},
        }
        (run_dir / ".capture_complete.json").write_text(json.dumps(marker), encoding="utf-8")
        return run_dir

    def test_complete_successful_60s_run_generates_png_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = self.make_run(root, successful=True)
            output = root / "out" / "run_force_overview.png"
            summary = root / "out" / "run_force_overview_summary.json"
            payload = build(run_dir, output, summary, None)
            self.assertTrue(payload["automatic_generation_gate"]["eligible"])
            self.assertTrue(payload["generated"])
            self.assertTrue(output.is_file())
            self.assertTrue(summary.is_file())
            self.assertAlmostEqual(payload["force_error_n"]["target_n"], 12.0)

    def test_failed_or_aborted_run_suppresses_polished_overview(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = self.make_run(root, successful=False)
            output = root / "out" / "run_force_overview.png"
            summary = root / "out" / "run_force_overview_summary.json"
            payload = build(run_dir, output, summary, None)
            self.assertFalse(payload["automatic_generation_gate"]["eligible"])
            self.assertFalse(payload["generated"])
            self.assertFalse(output.exists())
            self.assertTrue(summary.is_file())


if __name__ == "__main__":
    unittest.main()
