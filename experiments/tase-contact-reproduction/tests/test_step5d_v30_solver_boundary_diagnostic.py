#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_v30_solver_boundary_diagnostic as diagnostic  # noqa: E402


class Step5dV30SolverBoundaryDiagnosticTest(unittest.TestCase):
    def test_tracked_diagnostic_is_deterministically_rebuildable(self) -> None:
        tracked = json.loads(
            (
                ROOT
                / "config"
                / "step5d_v30_solver_batch_boundary_diagnostic.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(diagnostic.build(), tracked)

    def test_all_observed_outliers_are_boundaries_not_discarded(self) -> None:
        payload = diagnostic.build()
        aggregate = payload["aggregate"]

        self.assertEqual(aggregate["solver_samples"], 40_000)
        self.assertEqual(aggregate["deadline_outliers"], 6)
        self.assertEqual(aggregate["batch_boundary_outliers"], 6)
        self.assertEqual(aggregate["interior_outliers"], 0)
        self.assertTrue(aggregate["all_outliers_at_batch_boundaries"])
        self.assertLess(aggregate["max_interior_ms"], 0.5)
        self.assertGreater(aggregate["max_batch_boundary_ms"], 5.0)
        self.assertFalse(payload["claim_boundary"]["formal_timing_passed"])

    def test_interior_outlier_is_not_relabelled_as_boundary(self) -> None:
        values = [0.2] * 10_000
        values[101] = 2.1

        result = diagnostic.analyze_solver_samples(values)

        self.assertEqual(result["batch_boundary_outlier_count"], 0)
        self.assertEqual(result["interior_outlier_indices"], [101])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
