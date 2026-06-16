#!/usr/bin/env python3
"""Offline tests for the Step5d v15a permissive recovery candidate."""

from __future__ import annotations

import sys
import unittest
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import kunwei_rtde_bridge as bridge  # noqa: E402
import step5d_v15_permissive_recovery as v15  # noqa: E402


class Step5dV15PermissiveRecoveryTest(unittest.TestCase):
    def test_predicted_speed_event_enters_hold_not_immediate_stop(self) -> None:
        result = bridge.step5d_v15_permissive_recovery_guard(
            normal_load_n=5.0,
            force_norm_n=5.0,
            actual_tcp_speed_m_s=0.004,
            predicted_tcp_speed_m_s=0.051,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(result["action"], "hold_zero_qdot")
        self.assertEqual(result["reason"], "predicted_tcp_speed_recoverable_hold")

    def test_online_cage_margin_controls_hold_and_stop(self) -> None:
        inside = bridge.step5d_v15a_guard(
            normal_load_n=5.0,
            force_norm_n=5.0,
            actual_tcp_speed_m_s=0.004,
            predicted_tcp_speed_m_s=0.051,
            braking_margin_m=0.010,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        exhausted = bridge.step5d_v15a_guard(
            normal_load_n=5.0,
            force_norm_n=5.0,
            actual_tcp_speed_m_s=0.004,
            predicted_tcp_speed_m_s=0.051,
            braking_margin_m=0.0,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        nonfinite = bridge.step5d_v15a_guard(
            normal_load_n=5.0,
            force_norm_n=5.0,
            actual_tcp_speed_m_s=0.004,
            predicted_tcp_speed_m_s=0.051,
            braking_margin_m=float("nan"),
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(inside["action"], "hold_zero_qdot")
        self.assertEqual(inside["reason"], "predicted_tcp_speed_recoverable_hold")
        self.assertEqual(exhausted["reason"], "tcp_cage_braking_margin_exhausted")
        self.assertEqual(exhausted["action"], "stop_zero_qdot")
        self.assertEqual(nonfinite["reason"], "nonfinite_tcp_cage_braking_margin")
        self.assertEqual(nonfinite["action"], "stop_zero_qdot")

    def test_semantic_failure_remains_hard_stop(self) -> None:
        semantic = bridge.step5d_v15_permissive_recovery_guard(
            normal_load_n=5.0,
            force_norm_n=5.0,
            actual_tcp_speed_m_s=0.004,
            predicted_tcp_speed_m_s=0.0,
            semantic_gate_ok=False,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(semantic["reason"], "semantic_gate_failed")
        self.assertEqual(semantic["action"], "stop_zero_qdot")

    def test_low_load_timeout_is_deferred_when_speed_is_benign(self) -> None:
        result = bridge.step5d_v15_permissive_recovery_guard(
            normal_load_n=0.0,
            force_norm_n=0.2,
            actual_tcp_speed_m_s=0.004,
            predicted_tcp_speed_m_s=0.0,
            prior_hold_s=0.299,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(result["action"], "hold_zero_qdot")
        self.assertEqual(result["reason"], "low_load_reacquire_hold_timeout_deferred")

    def test_bounded_hold_counters_exhaust(self) -> None:
        result = bridge.step5d_v15a_guard(
            normal_load_n=5.0,
            force_norm_n=5.0,
            actual_tcp_speed_m_s=0.012,
            predicted_tcp_speed_m_s=0.0,
            braking_margin_m=0.010,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            prior_consecutive_hold_s=1.199,
            prior_total_hold_s=1.199,
            prior_hold_event_count=1,
            prior_last_hold_reason="early_tcp_escape_recoverable_hold",
            prior_hold_actual_tcp_speed_m_s=0.011,
            active_stage25_s=2.0,
            dt_s=0.002,
        )
        self.assertEqual(result["action"], "stop_zero_qdot")
        self.assertEqual(result["reason"], "hold_consecutive_limit")
        self.assertGreater(result["consecutive_hold_s"], 1.2)

    def test_v15a_profile_uses_guarded_qdot_default(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                "step5d_strict_rnn_liveprep_v15a",
                "--bridge-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(args.bridge_profile, "step5d_strict_rnn_liveprep_v15a")
        self.assertEqual(args.step4e_version, "step5d_strict_rnn_liveprep_v15a")
        self.assertEqual(args.step5d_qdot_limit_rad_s, 0.05)

    def test_v15a_hold_path_freezes_stage_time_and_skips_solver(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")
        self.assertIn(
            "step5d_contact_safety_profile = (\n"
            "        step5d_liveprep_v13_profile or step5d_liveprep_v14_profile or step5d_liveprep_v15_profile or step5d_liveprep_v15a_profile",
            source,
        )
        self.assertIn('step5d_contact_safety["action"] in {"hold_zero_qdot", "stop_zero_qdot"}', source)
        self.assertIn("state.step5d_contact_hold_path_time_s = max(0.0, state.line_stage_s - max(0.0, dt_s))", source)
        self.assertIn("state.line_stage_s = state.step5d_contact_hold_path_time_s", source)
        self.assertIn("step5d_qdot_command = tuple(float(value) for value in zero_qdot.tolist())", source)
        for field in (
            "_step5d_tcp_cage_distance_m",
            "_step5d_tcp_cage_braking_margin_m",
            "_step5d_tcp_cage_signed_distance_m",
            "_step5d_tcp_cage_cell_index",
            "_step5d_tcp_cage_reason",
            "_step5d_hold_event_count",
            "_step5d_consecutive_hold_s",
            "_step5d_total_hold_s",
            "_step5d_hold_duty",
            "_step5d_repeated_hold_count",
        ):
            self.assertIn(field, source)

    def test_stage_table_marks_v15a_readback_verified_without_live_authorization(self) -> None:
        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        retained = next(item for item in table["stages"] if item["id"] == "step5d_strict_rnn_liveprep_v15")
        self.assertFalse(retained["active"])
        self.assertIn("audit gaps", retained["block_reason"])
        stage = next(item for item in table["stages"] if item["id"] == "step5d_strict_rnn_liveprep_v15a")
        self.assertTrue(stage["active"])
        self.assertFalse(stage["complete"])
        self.assertNotIn("live_run_evidence", stage)
        self.assertEqual(stage["contact_policy"]["live_authorization"], "controller_readback_verified_but_live_bridge_not_authorized")
        self.assertTrue(stage["local_delivery_evidence"]["controller_readback_verified"])
        self.assertEqual(
            stage["local_delivery_evidence"]["controller_readback"],
            "runs/controller_readback_step5d_strict_rnn_liveprep_v15a_20260616_155322/manifest.json",
        )
        self.assertEqual(stage["local_analysis_evidence"]["acceptance"]["v14_enters_hold_before_hard_stop"], True)
        self.assertEqual(stage["local_analysis_evidence"]["acceptance"]["success_hold_burden_reported"], True)
        self.assertEqual(stage["local_analysis_evidence"]["acceptance"]["online_tcp_cage_ready"], True)
        self.assertIn("success_hold_duty_by_csv", stage["local_analysis_evidence"])

    def test_offline_replay_acceptance(self) -> None:
        summary = v15.analyze()
        self.assertEqual(
            summary["acceptance"],
            {
                "success_no_hard_stop": True,
                "success_hold_burden_reported": True,
                "v14_enters_hold_before_hard_stop": True,
                "v11_intervenes_before_qdot_rail_or_large_escape": True,
                "cage_margin_hard_stop": True,
                "online_tcp_cage_ready": True,
            },
        )
        self.assertIn(
            summary["replay"]["v14"]["first_hold"]["reason"],
            {"early_tcp_escape_recoverable_hold", "predicted_tcp_speed_recoverable_hold"},
        )
        self.assertEqual(summary["replay"]["v11"]["first_hold"]["reason"], "early_tcp_escape_recoverable_hold")
        self.assertLess(summary["replay"]["v11"]["first_hold"]["actual_tcp_speed_m_s"], 0.050)
        self.assertIn("success_hold_duty_by_csv", summary)
        self.assertIn("success_max_consecutive_hold_s_by_csv", summary)


if __name__ == "__main__":
    unittest.main()
