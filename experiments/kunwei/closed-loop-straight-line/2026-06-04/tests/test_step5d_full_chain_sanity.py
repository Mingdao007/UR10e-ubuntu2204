#!/usr/bin/env python3
"""Offline checks for the Step5d full-chain sanity runner."""

from __future__ import annotations

import inspect
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import kunwei_rtde_bridge as bridge  # noqa: E402
import step5d_full_chain_sanity as sanity  # noqa: E402


class Step5dFullChainSanityTest(unittest.TestCase):
    def test_full_chain_sanity_produces_offline_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = sanity.run_sanity(output_dir=Path(tmpdir), sample_limit=8)
        self.assertTrue(summary["overall_pass"])
        self.assertEqual(summary["assumptions"]["contact_evidence"], "not_claimed")
        self.assertEqual(summary["assumptions"]["position_error_mode"], "zeroed_for_structural_sanity")
        self.assertEqual(summary["assumptions"]["force_input"], "synthetic_no_contact_unit_normal")
        self.assertTrue(summary["gates"]["register_order_pass"])
        self.assertTrue(summary["gates"]["qdot_no_explosion_pass"])
        self.assertIn("qdot_within_nominal_limit", summary["metrics"])
        self.assertIn("constraint_residual_norm_rms", summary["metrics"])

    def test_bridge_knows_step5d_but_hard_blocks_live_start(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_reproduction_v1",
            ]
        )
        self.assertEqual(args.step4e_version, "step5d_strict_rnn_reproduction_v1")
        with self.assertRaisesRegex(SystemExit, "Blocked Step5d reproduction"):
            bridge.main(
                [
                    "--no-start-command",
                    "--skip-dashboard-preflight",
                    "--step4e-mode",
                    "line",
                    "--step4e-version",
                    "step5d_strict_rnn_reproduction_v1",
                ]
            )

    def test_full_chain_sanity_has_no_dls_or_live_side_effect_path(self) -> None:
        source = inspect.getsource(sanity).lower()
        forbidden = ["dls", "force_mode", "speedj(", "speedl(", "dashboard_exchange"]
        for token in forbidden:
            self.assertNotIn(token, source)
        self.assertIn("no bridge start", source)
        self.assertIn("no controller upload", source)


if __name__ == "__main__":
    unittest.main()
