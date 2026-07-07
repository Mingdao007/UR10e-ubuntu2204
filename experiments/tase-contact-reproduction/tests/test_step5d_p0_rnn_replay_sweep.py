#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_p0_rnn_replay_sweep as sweep  # noqa: E402


class Step5dP0RnnReplaySweepTest(unittest.TestCase):
    def test_stage25_selection_uses_tp_stage_register_35(self) -> None:
        rows = [
            {
                "ur_output_double_register_24": "25.0",
                "ur_output_double_register_35": "26.0",
            },
            {
                "ur_output_double_register_24": "0.0",
                "ur_output_double_register_35": "25.0",
            },
            {
                "ur_output_double_register_24": "25.0",
                "ur_output_double_register_35": "25.95",
            },
        ]

        selected = sweep.select_stage25_rows(rows)

        self.assertEqual(selected, [rows[1]])

    def test_default_sweep_includes_current_and_lower_sigr_exponents(self) -> None:
        self.assertEqual(sweep.DEFAULT_R_VALUES, (1.0, 0.8, 0.6, 0.4))

    def test_logged_alignment_requires_tight_r1_replay_error(self) -> None:
        self.assertTrue(sweep.replay_alignment_ok({"median": 6.6e-6, "p99": 1.9e-4}))
        self.assertFalse(sweep.replay_alignment_ok({"median": 0.003, "p99": 0.02}))

    def test_run_sweep_always_validates_explicit_r1_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "bridge_rtde_500hz.csv"
            csv_path.write_text(
                f"{sweep.STAGE_REGISTER},_step5d_constraint_residual_norm\n25.0,0.01\n",
                encoding="utf-8",
            )
            calls: list[tuple[float, float]] = []

            def fake_precompute(stage25_rows, replay_csv_path):
                self.assertEqual(len(stage25_rows), 1)
                self.assertEqual(replay_csv_path, csv_path)
                return [{"target": "continuous-state"}]

            def fake_replay(_targets, *, r, epsilon):
                calls.append((r, epsilon))
                return {
                    "r": r,
                    "epsilon": epsilon,
                    "logged_alignment_ok": r == sweep.BASELINE_R and epsilon == sweep.BASELINE_EPSILON,
                }

            with (
                mock.patch.object(sweep, "precompute_targets", side_effect=fake_precompute),
                mock.patch.object(sweep, "replay_targets", side_effect=fake_replay),
            ):
                result = sweep.run_sweep(csv_path, r_values=(0.8,), epsilon_values=(sweep.BASELINE_EPSILON,))

        self.assertEqual(calls, [(0.8, sweep.BASELINE_EPSILON), (sweep.BASELINE_R, sweep.BASELINE_EPSILON)])
        self.assertEqual([(item["r"], item["epsilon"]) for item in result["runs"]], [(0.8, sweep.BASELINE_EPSILON)])
        self.assertEqual((result["baseline"]["r"], result["baseline"]["epsilon"]), (sweep.BASELINE_R, sweep.BASELINE_EPSILON))
        self.assertTrue(result["baseline_logged_alignment_ok"])
        self.assertIn("no bridge start", result["safety_boundary"])

    def test_replay_targets_warm_starts_and_aligns_against_logged_residual(self) -> None:
        events: list[str] = []

        class FakeSolver:
            def __init__(self, config):
                self.config = config
                events.append(f"init:r={config.sigr_exponent_r}:eps={config.epsilon}")

            def warm_start(self, **_kwargs):
                events.append("warm_start")

            def solve(self, *, actual_q, actual_qd, target_state):
                events.append(f"solve:r={target_state['r']}:eps={target_state['epsilon']}")
                return SimpleNamespace(
                    residual_norm=0.01001,
                    qdot=[0.01, -0.02, 0.0, 0.0, 0.0, 0.0],
                    diagnostics={
                        "active_bounds_mask": [False, False, False, False, False, False],
                        "lambda_state": [1.0, 2.0],
                    },
                )

        target = {
            "J": [[1.0]],
            "xdot_c": [0.0],
            "omega_minus": [-0.15],
            "omega_plus": [0.15],
            "dt": 0.002,
            "q": [0.0] * 6,
            "qd": [0.0] * 6,
            "logged_residual": 0.01001,
        }
        targets = [target] * 100

        with mock.patch.object(sweep, "StrictTaseRnnSolver", FakeSolver):
            result = sweep.replay_targets(targets, r=sweep.BASELINE_R, epsilon=sweep.BASELINE_EPSILON)

        self.assertEqual(events[:2], ["init:r=1.0:eps=0.022", "warm_start"])
        self.assertEqual(events.count("solve:r=1.0:eps=0.022"), 100)
        self.assertEqual(result["residual_norm"]["n"], 100)
        self.assertEqual(result["active_bounds_rows"], 0)
        self.assertTrue(result["logged_alignment_ok"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
