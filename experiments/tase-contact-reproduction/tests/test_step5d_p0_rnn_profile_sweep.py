#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import sweep_step5d_p0_rnn_profiles as sweep  # noqa: E402


class Step5dP0RnnProfileSweepTest(unittest.TestCase):
    def test_profiles_keep_v8_first_and_deduplicate_grid(self) -> None:
        rows = sweep.profiles((512, 1024), (0.010,), (0.8,))

        self.assertEqual(rows[0], sweep.RnnProfile(**sweep.BASELINE))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].inner_iterations, 512)

    def test_profile_changes_only_rnn_parameters(self) -> None:
        payload = sweep.RnnProfile(256, 0.005, 0.6).payload()

        self.assertEqual(payload["backend"], "cupy")
        self.assertEqual(payload["qdot_cap_rad_s"], 0.05)
        self.assertEqual(payload["effective_ko"], 0.01)
        self.assertFalse(payload["dls_runtime_fallback_allowed"])
        self.assertEqual(len(sweep.RnnProfile(256, 0.005, 0.6).fingerprint()), 64)

    def test_invalid_profiles_fail_closed(self) -> None:
        for profile in (
            sweep.RnnProfile(0, 0.01, 0.8),
            sweep.RnnProfile(128, 0.0, 0.8),
            sweep.RnnProfile(128, 0.01, 1.1),
        ):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                profile.validate()

    def test_choose_requires_requested_gate_and_prefers_lowest_timing(self) -> None:
        rows = [
            {
                "quality_eligible": True,
                "timing_eligible": False,
                "wall_timing": {"p99_ms": 0.7, "max_ms": 0.9},
                "profile": {
                    "inner_iterations": 128,
                    "epsilon": 0.01,
                    "sigr_exponent_r": 0.8,
                },
            },
            {
                "quality_eligible": True,
                "timing_eligible": True,
                "wall_timing": {"p99_ms": 0.8, "max_ms": 1.1},
                "profile": {
                    "inner_iterations": 256,
                    "epsilon": 0.01,
                    "sigr_exponent_r": 0.8,
                },
            },
        ]

        self.assertIs(sweep.choose(rows, require_timing=False), rows[0])
        self.assertIs(sweep.choose(rows, require_timing=True), rows[1])

    def test_choose_prefers_minimal_parameter_drift_over_timing_noise(self) -> None:
        changed = {
            "quality_eligible": True,
            "timing_eligible": True,
            "wall_timing": {"p99_ms": 0.70, "max_ms": 0.90},
            "profile": {
                "inner_iterations": 128,
                "epsilon": 0.005,
                "sigr_exponent_r": 1.0,
            },
        }
        minimal = {
            "quality_eligible": True,
            "timing_eligible": True,
            "wall_timing": {"p99_ms": 0.80, "max_ms": 1.00},
            "profile": {
                "inner_iterations": 128,
                "epsilon": 0.010,
                "sigr_exponent_r": 0.8,
            },
        }

        self.assertIs(
            sweep.choose([changed, minimal], require_timing=True), minimal
        )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
