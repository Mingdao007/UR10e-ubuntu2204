#!/usr/bin/env python3
"""Offline checks for STARS-inspired F/T bias logging dimensions."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import kunwei_rtde_bridge as bridge  # noqa: E402


class StarsBiasLoggingContractTest(unittest.TestCase):
    def test_bias_logging_schema_keeps_estimator_and_control_masks_separate(self) -> None:
        for field in [
            "zero_event_id",
            "contact_mask",
            "bias_estimation_contact_mask",
            "bias_contact_reason",
            "control_contact_window",
        ]:
            self.assertIn(field, bridge.BIAS_BRIDGE_LOG_FIELDS)
        self.assertIn("bias_est_fx_n", bridge.BIAS_ESTIMATE_FIELDS)
        self.assertIn("bias_est_mz_nm", bridge.BIAS_ESTIMATE_FIELDS)
        self.assertIn("bias_rate_est_fx_n_per_s", bridge.BIAS_RATE_ESTIMATE_FIELDS)
        self.assertIn("bias_rate_est_mz_nm_per_s", bridge.BIAS_RATE_ESTIMATE_FIELDS)
        self.assertIn("ur_actual_qdd_0", bridge.KINEMATIC_DERIVED_FIELDS)
        self.assertIn("ur_actual_TCP_accel_5", bridge.KINEMATIC_DERIVED_FIELDS)
        self.assertIn("ur_kinematics_dt_s", bridge.KINEMATIC_DERIVED_FIELDS)

    def test_bias_contact_mask_uses_control_window_as_separate_reason(self) -> None:
        mask, reason = bridge.bias_contact_mask(
            baseline_ready=True,
            normal_load_n=0.1,
            force_norm_n=0.2,
            control_contact_window=True,
        )
        self.assertEqual(mask, 1)
        self.assertEqual(reason, "control_contact_window")

        mask, reason = bridge.bias_contact_mask(
            baseline_ready=True,
            normal_load_n=0.1,
            force_norm_n=0.2,
            control_contact_window=False,
        )
        self.assertEqual(mask, 0)
        self.assertEqual(reason, "no_contact")

        mask, reason = bridge.bias_contact_mask(
            baseline_ready=True,
            normal_load_n=0.8,
            force_norm_n=0.8,
            control_contact_window=False,
        )
        self.assertEqual(mask, 1)
        self.assertEqual(reason, "normal_load_threshold")

    def test_derived_kinematics_row_records_joint_and_tcp_acceleration(self) -> None:
        previous = {
            "actual_qd": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5],
            "actual_TCP_speed": [0.0, 0.02, 0.04, 0.06, 0.08, 0.10],
        }
        current = {
            "actual_qd": [0.1, 0.1, 0.0, 0.5, 0.4, 0.2],
            "actual_TCP_speed": [0.01, 0.02, 0.02, 0.10, 0.08, 0.04],
        }
        row = bridge.derived_kinematics_row(current, previous, 0.1)
        self.assertAlmostEqual(row["ur_actual_qdd_0"], 1.0)
        self.assertAlmostEqual(row["ur_actual_qdd_2"], -2.0)
        self.assertAlmostEqual(row["ur_actual_TCP_accel_0"], 0.1)
        self.assertAlmostEqual(row["ur_actual_TCP_accel_5"], -0.6)
        self.assertAlmostEqual(row["ur_kinematics_dt_s"], 0.1)

    def test_derived_kinematics_first_sample_is_blankable_nan(self) -> None:
        current = {
            "actual_qd": [0.0] * 6,
            "actual_TCP_speed": [0.0] * 6,
        }
        row = bridge.derived_kinematics_row(current, None, None)
        self.assertTrue(math.isnan(row["ur_actual_qdd_0"]))
        self.assertTrue(math.isnan(row["ur_actual_TCP_accel_0"]))
        self.assertTrue(math.isnan(row["ur_kinematics_dt_s"]))

    def test_bias_contact_thresholds_are_parseable_logging_options(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--bias-contact-normal-threshold-n",
                "1.25",
                "--bias-contact-force-norm-threshold-n",
                "3.5",
            ]
        )
        self.assertEqual(args.bias_contact_normal_threshold_n, 1.25)
        self.assertEqual(args.bias_contact_force_norm_threshold_n, 3.5)


if __name__ == "__main__":
    unittest.main()
