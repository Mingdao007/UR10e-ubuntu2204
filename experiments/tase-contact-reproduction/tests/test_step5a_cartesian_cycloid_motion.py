#!/usr/bin/env python3
"""Focused offline tests for the Step5a Cartesian cycloid live entrypoint."""

from __future__ import annotations

import math
import sys
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers.no_contact_cycloid_shadow import DEFAULT_CONFIG, _iter_reference_rows, load_no_contact_config  # noqa: E402
from ur10e_example_controllers.step5a_cartesian_cycloid_motion import (  # noqa: E402
    EXPECTED_CALIBRATION_HASH,
    JOINT_NAMES,
    build_calibrated_model,
    build_cartesian_cycloid_trajectory,
    validate_cartesian_acceptance_summary,
)


class Step5aCartesianCycloidMotionTest(unittest.TestCase):
    def test_calibrated_model_and_full_reference_are_step5a_shape(self) -> None:
        model = build_calibrated_model()
        self.assertEqual(model.calibration_hash, EXPECTED_CALIBRATION_HASH)
        self.assertEqual(model.model.nq, 6)
        self.assertEqual(model.model.nv, 6)
        config = load_no_contact_config(DEFAULT_CONFIG)
        rows = _iter_reference_rows(config)
        self.assertEqual(len(rows), 1101)
        self.assertAlmostEqual(float(rows[-1]["phase_rad"]), 6.0, places=12)
        self.assertLessEqual(max(float(row["reference_speed_m_s"]) for row in rows), float(config["velocity_cap_m_s"]))

    def test_short_cartesian_ik_trajectory_is_continuous(self) -> None:
        model = build_calibrated_model()
        config = load_no_contact_config(DEFAULT_CONFIG)
        short_config = deepcopy(config)
        short_config["duration_s"] = 0.2
        short_config["final_phase_rad"] = float(config["omega_rad_s"]) * 0.2
        start = [0.0, -1.57, 1.57, -1.57, -1.57, 0.0]
        points, trace_rows, metrics = build_cartesian_cycloid_trajectory(short_config, model, start)
        self.assertEqual(len(points), 11)
        self.assertEqual(len(trace_rows), 11)
        self.assertLess(metrics["max_commanded_position_error_m"], 5e-4)
        self.assertTrue(math.isfinite(metrics["max_commanded_fk_speed_m_s"]))
        for previous, current in zip(points, points[1:]):
            jump = max(abs(a - b) for a, b in zip(previous.positions, current.positions))
            self.assertLess(jump, 0.02)
        for field in [f"command_{name}_rad" for name in JOINT_NAMES]:
            self.assertIn(field, trace_rows[0])
        self.assertIn("commanded_fk_x_m", trace_rows[0])

    def test_acceptance_summary_rejects_wrong_kind_rows_speed_and_kunwei(self) -> None:
        payload = {
            "role": "step5a_live_no_contact_cartesian_cycloid",
            "motion_kind": "cartesian_cycloid",
            "rows": 1101,
            "phase_final": 6.0,
            "sent_goal": True,
            "accepted": True,
            "result_error_code": 0,
            "kunwei_artifact_ok": True,
            "velocity_cap_m_s": 0.009,
            "max_reference_speed_m_s": 0.009,
            "max_commanded_fk_speed_m_s": 0.009,
            "max_achieved_speed_m_s": 0.009,
            "max_cartesian_position_error_m": 0.001,
            "anchor_pose_base": {"frame": "base_to_tool0"},
            "ik_source": {"solver": "pinocchio_calibrated_tool0_warm_start_deterministic_dls"},
            "contact_policy": {"force_control": False, "contact_search": False},
        }
        self.assertTrue(validate_cartesian_acceptance_summary(dict(payload)))

        bad = dict(payload)
        bad["motion_kind"] = "joint_proxy_cycloid_timing_not_cartesian_cycloid"
        with self.assertRaisesRegex(RuntimeError, "wrong motion kind"):
            validate_cartesian_acceptance_summary(bad)

        bad = dict(payload)
        bad["rows"] = 1100
        with self.assertRaisesRegex(RuntimeError, "wrong row count"):
            validate_cartesian_acceptance_summary(bad)

        bad = dict(payload)
        bad["max_reference_speed_m_s"] = 0.010
        with self.assertRaisesRegex(RuntimeError, "velocity cap"):
            validate_cartesian_acceptance_summary(bad)

        bad = dict(payload)
        bad["kunwei_artifact_ok"] = False
        with self.assertRaisesRegex(RuntimeError, "Kunwei artifact"):
            validate_cartesian_acceptance_summary(bad)


if __name__ == "__main__":
    unittest.main()
