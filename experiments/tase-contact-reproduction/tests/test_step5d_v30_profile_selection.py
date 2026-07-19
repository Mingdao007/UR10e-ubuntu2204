#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_v30_profile_selection as selection  # noqa: E402


class Step5dV30ProfileSelectionTest(unittest.TestCase):
    def test_tracked_selection_is_deterministically_rebuildable(self) -> None:
        tracked = json.loads(
            (ROOT / "config" / "step5d_v30_profile_selection.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(selection.build(), tracked)

    def test_selection_requires_zero_normal_mismatch_before_timing(self) -> None:
        faster_but_wrong = {
            "inner_iterations": 128,
            "selection_eligible": False,
            "full_tick_p99_ms": 0.1,
            "full_tick_max_ms": 0.2,
        }
        slower_and_correct = {
            "inner_iterations": 512,
            "selection_eligible": True,
            "full_tick_p99_ms": 0.7,
            "full_tick_max_ms": 0.9,
        }
        self.assertIs(
            selection.select_candidate([faster_but_wrong, slower_and_correct]),
            slower_and_correct,
        )

    def test_bound_sweep_selects_512_without_changing_fixed_contract(self) -> None:
        payload = selection.build()

        self.assertEqual(payload["selected_inner_iterations"], 512)
        self.assertEqual(payload["selected_profile"]["epsilon"], 0.010)
        self.assertEqual(payload["selected_profile"]["sigr_exponent_r"], 0.8)
        self.assertEqual(payload["selected_profile"]["qdot_cap_rad_s"], 0.05)
        self.assertEqual(payload["selected_profile"]["backend"], "cupy")
        rows = {row["inner_iterations"]: row for row in payload["candidates"]}
        self.assertEqual(rows[128]["normal_sign_mismatch_count"], 167)
        self.assertEqual(rows[256]["normal_sign_mismatch_count"], 39)
        self.assertEqual(rows[512]["normal_sign_mismatch_count"], 0)
        self.assertEqual(rows[512]["execute_count"], 500)
        self.assertTrue(rows[512]["timing_pass"])
        self.assertTrue(rows[512]["bitwise_parallel_equivalence"])
        self.assertLess(rows[512]["safe_hold_max_ms"], 2.0)
        self.assertEqual(rows[512]["safe_hold_deadline_miss_count"], 0)
        self.assertIsInstance(
            payload["common_runtime_environment_sha256"], str
        )
        self.assertEqual(
            payload["fixed_contract"]["runtime_scheduler"],
            {"policy": "SCHED_FIFO", "priority": 20},
        )
        self.assertFalse(payload["claim_boundary"]["v30_offline_ready"])

    def test_pre_512_p0_evidence_uses_immutable_versioned_archives(self) -> None:
        payload = selection.build()
        historical = {
            row["path"]: row
            for row in payload["historical_pre_512_evidence"]
        }
        expected = {
            (
                "config/step5d_p0_v8_offline_simulation_diagnostic_"
                "pre_rnn512_e55c574.json"
            ): "4d5c7bf409a8c6cdc5abf6cfa7a6804799d35c2efb5fe969f6827aae2167ce46",
            (
                "config/step5d_p0_v8_offline_simulation_state_"
                "pre_rnn512_e55c574.json"
            ): "a6b6fbd833980107ba583ed65690ceb394a558c999409a1dc5d5eb0dc21b32a1",
        }

        for path, expected_sha256 in expected.items():
            self.assertIn(path, historical)
            self.assertEqual(historical[path]["sha256"], expected_sha256)
            self.assertTrue(historical[path]["immutable"])
            self.assertEqual(
                historical[path]["status"],
                "historical_superseded_by_v30_rnn512_profile_selection",
            )
        self.assertNotIn(
            "config/step5d_p0_v8_offline_simulation_diagnostic.json",
            historical,
        )
        self.assertNotIn(
            "config/step5d_p0_v8_offline_simulation_state.json",
            historical,
        )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
