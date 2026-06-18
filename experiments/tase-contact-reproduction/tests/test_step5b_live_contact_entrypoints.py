#!/usr/bin/env python3
"""Static checks for Step5b live-contact user entrypoints."""

from __future__ import annotations

import unittest
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[3]


class Step5bLiveContactEntrypointTest(unittest.TestCase):
    def test_live_bridge_wrapper_is_gate_first_tp_bridge_route(self) -> None:
        script = (WORKSPACE / "step5b_contact_bridge.sh").read_text(encoding="utf-8")
        self.assertIn("selected_route=tp_bridge_step5b_v1", script)
        self.assertIn("step5b_zero_policy_readiness_gate", script)
        self.assertIn("STEP5B_CONFIRM", script)
        self.assertIn("LIVE STEP5B CONTACT RUN", script)
        self.assertIn("step5b-contact-operator.sh", script)
        self.assertIn("step5b_live_contact_run_summary.json", script)
        self.assertIn('"target_force_n": 5.0', script)
        self.assertIn('"force_norm_gt_n": 1.5', script)
        self.assertIn('"normal_force_lte_n": -1.0', script)
        self.assertIn('"raw_normal_guard_n": 50.0', script)
        self.assertIn('"force_norm_guard_n": 60.0', script)
        self.assertIn('"torque_guard_nm": 3.0', script)
        self.assertIn('"tp_play_or_upload_by_script": False', script)
        self.assertNotIn("ros2 action send_goal", script)
        self.assertNotIn("dashboard load", script)
        self.assertNotIn("play\\n", script)
        self.assertNotIn("zero_ftsensor(", script)

    def test_combo_wrapper_runs_readiness_before_live_bridge(self) -> None:
        script = (WORKSPACE / "step5b_live_contact.sh").read_text(encoding="utf-8")
        readiness_index = script.index("step5b_zero_policy_check.sh")
        live_index = script.index("step5b_contact_bridge.sh")
        self.assertLess(readiness_index, live_index)
        self.assertIn("STEP5B_CONFIRM", script)
        self.assertIn("LIVE STEP5B CONTACT RUN", script)
        self.assertIn("STEP5B_ZERO_POLICY_RUN_DIR", script)
        self.assertNotIn("ros2 action send_goal", script)
        self.assertNotIn("zero_ftsensor(", script)


if __name__ == "__main__":
    unittest.main()
