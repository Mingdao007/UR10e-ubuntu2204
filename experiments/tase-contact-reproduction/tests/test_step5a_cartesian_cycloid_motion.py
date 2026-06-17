#!/usr/bin/env python3
"""Focused offline tests for the Step5a Cartesian cycloid live entrypoint."""

from __future__ import annotations

import math
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from sensor_msgs.msg import JointState

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers.no_contact_cycloid_shadow import DEFAULT_CONFIG, _iter_reference_rows, load_no_contact_config  # noqa: E402
from ur10e_example_controllers.step5a_gate_a_audit import audit_gate_a_run  # noqa: E402
from ur10e_example_controllers.step5a_cartesian_cycloid_motion import (  # noqa: E402
    DEFAULT_GATE_A_POSITION_ERROR_LIMIT_M,
    DEFAULT_JOINT_HISTORY_MAX_SAMPLES,
    EXPECTED_CALIBRATION_HASH,
    JOINT_NAMES,
    ObservedJointSample,
    Step5aCartesianCycloidMotion,
    augment_trace_with_observed_fk,
    build_calibrated_model,
    build_cartesian_cycloid_trajectory,
    step5a_acceptance,
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
        self.assertIn("reference_final_offset_xyz_m", metrics)
        self.assertIn("commanded_net_displacement_norm_m", metrics)
        for previous, current in zip(points, points[1:]):
            jump = max(abs(a - b) for a, b in zip(previous.positions, current.positions))
            self.assertLess(jump, 0.02)
        for field in [f"command_{name}_rad" for name in JOINT_NAMES]:
            self.assertIn(field, trace_rows[0])
        self.assertIn("commanded_fk_x_m", trace_rows[0])

    def test_acceptance_summary_requires_all_gate_a_conditions(self) -> None:
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
            "kunwei_monitor": {
                "ok": True,
                "stream_start_command_sent": True,
                "stream_stop_command_sent": True,
                "failure_reason": None,
            },
            "trace_alignment": {"trace_alignment_ok": True},
            "gate_a_thresholds": {
                "velocity_cap_m_s": 0.009,
                "cartesian_position_error_limit_m": DEFAULT_GATE_A_POSITION_ERROR_LIMIT_M,
                "trace_max_sample_gap_s": 0.08,
            },
        }
        self.assertTrue(validate_cartesian_acceptance_summary(dict(payload)))
        self.assertTrue(step5a_acceptance(dict(payload))["gate_a_pass"])

        bad = dict(payload)
        bad["motion_kind"] = "joint_proxy_cycloid_timing_not_cartesian_cycloid"
        self.assertFalse(validate_cartesian_acceptance_summary(bad))
        self.assertIn("cartesian_shape", bad["acceptance"]["failed_conditions"])

        bad = dict(payload)
        bad["rows"] = 1100
        self.assertFalse(validate_cartesian_acceptance_summary(bad))
        self.assertIn("cartesian_shape", bad["acceptance"]["failed_conditions"])

        bad = dict(payload)
        bad["max_reference_speed_m_s"] = 0.010
        self.assertFalse(validate_cartesian_acceptance_summary(bad))
        self.assertIn("speed_cap", bad["acceptance"]["failed_conditions"])

        bad = dict(payload)
        bad["kunwei_artifact_ok"] = False
        bad["kunwei_monitor"] = {"ok": False}
        self.assertFalse(validate_cartesian_acceptance_summary(bad))
        self.assertIn("kunwei_evidence", bad["acceptance"]["failed_conditions"])

        bad = dict(payload)
        bad["max_cartesian_position_error_m"] = 0.059225
        self.assertFalse(validate_cartesian_acceptance_summary(bad))
        self.assertIn("cartesian_equivalence", bad["acceptance"]["failed_conditions"])

    def test_trace_alignment_anchors_row0_and_interpolates_observed_fk(self) -> None:
        model = build_calibrated_model()
        config = load_no_contact_config(DEFAULT_CONFIG)
        short_config = deepcopy(config)
        short_config["duration_s"] = 0.2
        short_config["final_phase_rad"] = float(config["omega_rad_s"]) * 0.2
        start = [0.0, -1.57, 1.57, -1.57, -1.57, 0.0]
        _, trace_rows, _ = build_cartesian_cycloid_trajectory(short_config, model, start)
        send_start = 100.0
        samples = []
        for row in trace_rows[::2]:
            positions = [float(row[f"command_{name}_rad"]) for name in JOINT_NAMES]
            samples.append(ObservedJointSample(send_start + float(row["t_rel_s"]), None, positions))
        positions = [float(trace_rows[-1][f"command_{name}_rad"]) for name in JOINT_NAMES]
        samples.append(ObservedJointSample(send_start + float(trace_rows[-1]["t_rel_s"]), None, positions))

        alignment = augment_trace_with_observed_fk(
            trace_rows,
            model,
            samples,
            send_start,
            start,
            max_sample_gap_s=0.08,
        )
        self.assertTrue(alignment["trace_alignment_ok"])
        self.assertLessEqual(alignment["row0_anchor_error_m"], 1e-9)
        self.assertEqual(float(trace_rows[0]["cartesian_error_m"]), 0.0)
        self.assertIn("observed_sample_t_rel_s", trace_rows[0])
        self.assertIn("observed_shoulder_pan_joint_rad", trace_rows[0])

    def test_full_step5a_reference_endpoint_displacement_is_explicit(self) -> None:
        model = build_calibrated_model()
        config = load_no_contact_config(DEFAULT_CONFIG)
        start = [0.0, -1.57, 1.57, -1.57, -1.57, 0.0]
        _, _, metrics = build_cartesian_cycloid_trajectory(config, model, start)

        self.assertFalse(metrics["reference_returns_to_anchor"])
        self.assertAlmostEqual(metrics["reference_final_offset_xyz_m"][0], 0.06279415498198926, places=12)
        self.assertAlmostEqual(metrics["reference_final_offset_xyz_m"][1], 0.00039829713349634025, places=12)
        self.assertGreater(metrics["commanded_net_displacement_norm_m"], 0.06)
        self.assertLess(metrics["commanded_net_displacement_norm_m"], 0.07)

    def test_joint_state_history_default_retains_full_step5a_window(self) -> None:
        self.assertGreaterEqual(DEFAULT_JOINT_HISTORY_MAX_SAMPLES, 50000)
        node = Step5aCartesianCycloidMotion.__new__(Step5aCartesianCycloidMotion)
        node.args = SimpleNamespace(joint_history_max_samples=DEFAULT_JOINT_HISTORY_MAX_SAMPLES)
        node.joint_history = []
        node.joint_state = None
        msg = JointState()
        msg.name = list(JOINT_NAMES)
        msg.position = [0.1 * index for index, _ in enumerate(JOINT_NAMES)]

        for _ in range(6001):
            Step5aCartesianCycloidMotion._on_joint_state(node, msg)

        self.assertEqual(len(node.joint_history), 6001)

    def test_joint_state_history_respects_explicit_limit(self) -> None:
        node = Step5aCartesianCycloidMotion.__new__(Step5aCartesianCycloidMotion)
        node.args = SimpleNamespace(joint_history_max_samples=3)
        node.joint_history = []
        node.joint_state = None
        msg = JointState()
        msg.name = list(JOINT_NAMES)
        msg.position = [0.1 * index for index, _ in enumerate(JOINT_NAMES)]

        for _ in range(5):
            Step5aCartesianCycloidMotion._on_joint_state(node, msg)

        self.assertEqual(len(node.joint_history), 3)

    def test_current_failed_evidence_run_audits_as_gate_a_failed(self) -> None:
        run_dir = WORKSPACE / "experiments" / "tase-contact-reproduction" / "runs" / "no_contact_test_20260617_143540"
        if not run_dir.exists():
            self.skipTest(f"local evidence run is unavailable: {run_dir}")
        audit = audit_gate_a_run(run_dir)
        self.assertFalse(audit["gate_a_pass"])
        self.assertEqual(audit["judgement"], "Step5a live motion executed, but Gate A failed")
        self.assertIn("speed_cap", audit["acceptance"]["failed_conditions"])
        self.assertIn("cartesian_equivalence", audit["acceptance"]["failed_conditions"])
        self.assertEqual(audit["trace_diagnostics"]["achieved_speed_over_cap_rows"], 21)
        self.assertEqual(audit["trace_diagnostics"]["cartesian_error_over_5mm_rows"], 571)


if __name__ == "__main__":
    unittest.main()
