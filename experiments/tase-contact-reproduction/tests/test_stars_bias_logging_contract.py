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
        self.assertIn("anchor_ready", bridge.BASELINE_DIAGNOSTIC_FIELDS)
        self.assertIn("diagnostic_bias_fz_n", bridge.DIAGNOSTIC_BIAS_FIELDS)
        self.assertIn("baseline_drift_mz_nm", bridge.BASELINE_DRIFT_FIELDS)

    def test_campaign_anchor_quality_accepts_clean_static_window(self) -> None:
        bootstrap = [1.0, 2.0, 3.0, 0.1, 0.2, 0.3]
        samples = [
            [1.01, 2.01, 3.01, 0.1001, 0.2001, 0.3001]
            for _ in range(bridge.CAMPAIGN_ANCHOR_MIN_SAMPLES)
        ]
        result = bridge.evaluate_baseline_window(
            samples,
            bootstrap_baseline=bootstrap,
            normal_axis="fz",
            normal_sign=1.0,
            tcp_linear_max_m_s=0.0,
            tcp_angular_max_rad_s=0.0,
            qd_max_rad_s=0.0,
        )
        self.assertTrue(result["qualified"])
        self.assertEqual(result["flags"], ())

    def test_contaminated_diagnostic_is_complete_but_not_qualified(self) -> None:
        bootstrap = [0.0] * 6
        samples = [
            [0.0, 0.0, 3.0, 0.0, 0.0, 0.0]
            for _ in range(bridge.CAMPAIGN_ANCHOR_MIN_SAMPLES)
        ]
        result = bridge.evaluate_baseline_window(
            samples,
            bootstrap_baseline=bootstrap,
            normal_axis="fz",
            normal_sign=1.0,
            tcp_linear_max_m_s=0.0,
            tcp_angular_max_rad_s=0.0,
            qd_max_rad_s=0.0,
        )
        self.assertTrue(result["complete"])
        self.assertFalse(result["qualified"])
        self.assertIn("normal_contact_threshold", result["flags"])

        update = bridge.resolve_dual_baseline_update(
            epoch=1,
            bootstrap_baseline=bootstrap,
            campaign_anchor=bootstrap,
            anchor_ready=False,
            candidate_baseline=result["mean"],
            complete=result["complete"],
            qualified=result["qualified"],
        )
        self.assertFalse(update["ready"])
        self.assertTrue(update["retry_required"])
        self.assertEqual(update["applied"], bootstrap)

    def test_later_diagnostic_never_overwrites_campaign_anchor(self) -> None:
        anchor = [1.0, 2.0, 3.0, 0.1, 0.2, 0.3]
        contaminated = [10.0, 20.0, 30.0, 1.0, 2.0, 3.0]
        update = bridge.resolve_dual_baseline_update(
            epoch=2,
            bootstrap_baseline=[0.0] * 6,
            campaign_anchor=anchor,
            anchor_ready=True,
            candidate_baseline=contaminated,
            complete=True,
            qualified=False,
        )
        self.assertTrue(update["ready"])
        self.assertEqual(update["applied"], anchor)
        self.assertEqual(update["anchor"], anchor)
        self.assertEqual(update["diagnostic"], contaminated)
        self.assertFalse(update["retry_required"])

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
