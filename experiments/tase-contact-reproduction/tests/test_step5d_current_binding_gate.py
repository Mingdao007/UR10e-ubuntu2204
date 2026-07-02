#!/usr/bin/env python3
"""Tests for the Step5d current binding gate."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_current_binding as gate  # noqa: E402


class Step5dCurrentBindingGateTest(unittest.TestCase):
    def test_current_stage_readback_and_runtime_binding_pass(self) -> None:
        result = gate.verify_binding(ROOT)

        self.assertTrue(result["ok"])
        self.assertTrue(result["program"].startswith("step5d_strict_rnn_liveprep_"))
        self.assertIn("controller_readback_step5d_strict_rnn_liveprep", result["manifest"])
        self.assertEqual(result["runtime_interface"]["program"], result["program"])
        self.assertEqual(len(result["local_triplet"]), 3)


if __name__ == "__main__":
    unittest.main()
