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

    def test_v16_high_contact_policy_is_consumed_before_v13_guard(self) -> None:
        result = bridge.step5d_v15a_guard(
            normal_load_n=21.0,
            force_norm_n=21.0,
            actual_tcp_speed_m_s=0.004,
            predicted_tcp_speed_m_s=0.0,
            braking_margin_m=0.010,
            prior_hold_s=0.0,
            prior_high_window_s=0.048,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
            valid_contact_min_n=5.0,
            valid_contact_max_n=20.0,
            low_load_speed_load_n=5.0,
            hold_timeout_s=0.5,
            high_window_dwell_stop_s=0.05,
            allow_high_contact_below_hard_force=False,
        )
        self.assertEqual(result["action"], "stop_zero_qdot")
        self.assertEqual(result["reason"], "high_contact_window_dwell_stop")

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
        self.assertIn("step5d_liveprep_v18_or_newer_profile", source)
        self.assertIn("or step5d_liveprep_online_cage_profile", source)
        self.assertIn('step5d_contact_safety["action"] in {"hold_zero_qdot", "stop_zero_qdot"}', source)
        self.assertIn('step5d_contact_safety["action"] == "active_reacquire_solver"', source)
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
            "_step5d_active_reacquire_s",
            "_step5d_no_contact_s",
            "v18_v20_locked_normal_settle",
        ):
            self.assertIn(field, source)

    def test_v18_cage_primary_low_load_active_reacquire_does_not_consume_hold_duty(self) -> None:
        result = bridge.step5d_v18_guard(
            normal_load_n=0.0,
            force_norm_n=0.1,
            actual_tcp_speed_m_s=0.004,
            predicted_tcp_speed_m_s=0.0,
            braking_margin_m=0.010,
            prior_total_hold_s=0.8,
            active_stage25_s=2.0,
            dt_s=0.002,
        )
        self.assertEqual(result["state"], "active_reacquire")
        self.assertEqual(result["action"], "active_reacquire_solver")
        self.assertEqual(result["reason"], "cage_primary_no_contact_active_reacquire")
        self.assertEqual(result["total_hold_s"], 0.8)
        self.assertEqual(result["consecutive_hold_s"], 0.0)

        hard_stop = bridge.step5d_v18_guard(
            normal_load_n=0.0,
            force_norm_n=100.0,
            actual_tcp_speed_m_s=0.004,
            predicted_tcp_speed_m_s=0.0,
            braking_margin_m=0.010,
            dt_s=0.002,
        )
        self.assertEqual(hard_stop["action"], "stop_zero_qdot")
        self.assertEqual(hard_stop["reason"], "force_norm_hard_stop")

    def test_stage_table_marks_v15a_retained_after_hold_duty_live_stop(self) -> None:
        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        retained = next(item for item in table["stages"] if item["id"] == "step5d_strict_rnn_liveprep_v15")
        self.assertFalse(retained["active"])
        self.assertIn("audit gaps", retained["block_reason"])
        stage = next(item for item in table["stages"] if item["id"] == "step5d_strict_rnn_liveprep_v15a")
        self.assertFalse(stage["active"])
        self.assertTrue(stage["complete"])
        self.assertIn("hold_duty_limit", stage["block_reason"])
        self.assertEqual(stage["contact_policy"]["live_authorization"], "retained_live_run_evidence_no_current_retry_authorization")
        self.assertTrue(stage["local_delivery_evidence"]["controller_readback_verified"])
        self.assertEqual(
            stage["local_delivery_evidence"]["controller_readback"],
            "runs/controller_readback_step5d_strict_rnn_liveprep_v15a_20260616_155322/manifest.json",
        )
        self.assertEqual(stage["live_run_evidence"]["stop_reason"], "step5d_contact_safety:hold_duty_limit")
        self.assertEqual(
            stage["live_run_evidence"]["run_dir"],
            "runs/bridge_step4e_line_outerloop_step5d_strict_rnn_liveprep_v15a_20260616_160355",
        )
        self.assertGreater(stage["live_run_evidence"]["hold_duty"], stage["guard"]["hold_duty_limit"])
        self.assertEqual(stage["live_run_evidence"]["tcp_cage_reason"], "inside_broad_tcp_cage")
        self.assertEqual(stage["local_analysis_evidence"]["acceptance"]["v14_enters_hold_before_hard_stop"], True)
        self.assertEqual(stage["local_analysis_evidence"]["acceptance"]["success_hold_burden_reported"], True)
        self.assertEqual(stage["local_analysis_evidence"]["acceptance"]["online_tcp_cage_ready"], True)
        self.assertIn("success_hold_duty_by_csv", stage["local_analysis_evidence"])
        candidate = next(item for item in table["stages"] if item["id"] == "step5d_strict_rnn_liveprep_v16")
        self.assertFalse(candidate["active"])
        self.assertTrue(candidate["complete"])
        self.assertIn("hold_duty_limit", candidate["block_reason"])
        self.assertEqual(candidate["contact_policy"]["live_authorization"], "retained_live_run_evidence_no_current_retry_authorization")
        self.assertEqual(candidate["guard"]["target_force_n"], 12.0)
        self.assertEqual(candidate["guard"]["normal_load_filter_alpha"], 0.55)
        self.assertEqual(candidate["guard"]["line_entry_normal_load_min_n"], 5.0)
        self.assertEqual(candidate["guard"]["line_entry_normal_load_max_n"], 20.0)
        self.assertIn("no_lift_no_25_2_no_second_search", candidate["contact_policy"]["timing_policy"])
        self.assertEqual(candidate["contact_policy"]["controller_readback_status"], "verified")
        self.assertEqual(candidate["live_run_evidence"]["stop_reason"], "step5d_contact_safety:hold_duty_limit")
        self.assertEqual(
            candidate["live_run_evidence"]["run_dir"],
            "runs/bridge_step4e_line_outerloop_step5d_strict_rnn_liveprep_v16_20260702_150703",
        )
        self.assertGreater(candidate["live_run_evidence"]["hold_duty"], candidate["guard"]["hold_duty_limit"])
        self.assertEqual(candidate["live_run_evidence"]["stage25_normal_load_max_n"], 8.35160836)
        self.assertTrue(candidate["local_delivery_evidence"]["local_package_validated"])
        self.assertTrue(candidate["local_delivery_evidence"]["controller_readback_verified"])
        self.assertEqual(
            candidate["local_delivery_evidence"]["controller_readback"],
            "runs/controller_readback_step5d_strict_rnn_liveprep_v16_20260702_145838/manifest.json",
        )
        retained_v17 = next(item for item in table["stages"] if item["id"] == "step5d_strict_rnn_liveprep_v17")
        self.assertFalse(retained_v17["active"])
        self.assertTrue(retained_v17["complete"])
        self.assertEqual(retained_v17["live_run_evidence"]["stop_reason"], "step5d_contact_safety:hold_duty_limit")
        self.assertIn("projector", retained_v17["live_run_evidence"]["root_cause_summary"])

        retained_v18 = next(item for item in table["stages"] if item["id"] == "step5d_strict_rnn_liveprep_v18")
        self.assertFalse(retained_v18["active"])
        self.assertTrue(retained_v18["complete"])
        self.assertTrue(retained_v18["local_delivery_evidence"]["controller_readback_verified"])
        self.assertEqual(retained_v18["live_run_evidence"]["stop_reason"], "step5d_contact_safety:cage_primary_tcp_speed_hard_stop")
        self.assertAlmostEqual(retained_v18["live_run_evidence"]["predicted_tcp_speed_stop_m_s"], 0.0504484676)

        retained_v19 = next(item for item in table["stages"] if item["id"] == "step5d_strict_rnn_liveprep_v19")
        self.assertFalse(retained_v19["active"])
        self.assertTrue(retained_v19["complete"])
        self.assertEqual(retained_v19["live_run_evidence"]["stop_reason"], "step5d_contact_safety:cage_primary_tcp_speed_hard_stop")
        self.assertIn("locked-normal settle", retained_v19["live_run_evidence"]["root_cause_summary"])

        current_candidate = next(item for item in table["stages"] if item["id"] == "step5d_strict_rnn_liveprep_v20")
        self.assertTrue(current_candidate["active"])
        self.assertFalse(current_candidate["complete"])
        self.assertEqual(current_candidate["guard"]["target_force_n"], 12.0)
        self.assertEqual(current_candidate["guard"]["line_entry_normal_load_min_n"], 8.0)
        self.assertEqual(current_candidate["guard"]["line_entry_normal_load_max_n"], 13.0)
        self.assertEqual(current_candidate["guard"]["line_entry_raw_sanity_min_n"], 7.5)
        self.assertEqual(current_candidate["guard"]["line_entry_raw_sanity_max_n"], 14.0)
        self.assertEqual(current_candidate["guard"]["line_entry_required_s"], 0.1)
        self.assertEqual(current_candidate["guard"]["runtime_limit_s"], 15.0)
        self.assertEqual(current_candidate["guard"]["raw_normal_guard_n"], 100.0)
        self.assertEqual(current_candidate["guard"]["force_norm_guard_n"], 100.0)
        self.assertEqual(current_candidate["guard"]["torque_norm_guard_nm"], 4.0)
        self.assertEqual(current_candidate["guard"]["normal_follow_settle_s"], 0.15)
        self.assertEqual(current_candidate["guard"]["reacquire_predicted_tcp_speed_cap_m_s"], 0.035)
        self.assertEqual(current_candidate["guard"]["precontact_pose_contract_id"], "pre_contact_search_gravity_down_v1")
        self.assertEqual(current_candidate["guard"]["precontact_pose_target_rotvec_rad"], [3.141592654, 0.0, 0.0])
        self.assertEqual(current_candidate["guard"]["entry_movel_speed_m_s"], 0.060)
        self.assertEqual(current_candidate["guard"]["first_search_far_speed_m_s"], -0.0225)
        self.assertEqual(current_candidate["contact_policy"]["controller_readback_status"], "verified")
        self.assertTrue(current_candidate["local_delivery_evidence"]["local_package_validated"])
        self.assertTrue(current_candidate["local_delivery_evidence"]["controller_readback_verified"])
        self.assertEqual(
            current_candidate["local_delivery_evidence"]["controller_readback"],
            "runs/controller_readback_step5d_strict_rnn_liveprep_v20_20260702_192951/manifest.json",
        )
        current = json.loads((ROOT / "config" / "current_stage.json").read_text(encoding="utf-8"))
        self.assertEqual(current["current_stage_id"], "step5d_strict_rnn_liveprep_v20")
        self.assertEqual(current["program"], "step5d_strict_rnn_liveprep_v20")
        self.assertEqual(current["bridge_profile"]["step4e_version"], "step5d_strict_rnn_liveprep_v20")
        self.assertIn("controller_readback_verified", current["status"])
        self.assertTrue(current["evidence"]["v16_local_package_validated"])
        self.assertTrue(current["evidence"]["v16_controller_readback_verified"])
        self.assertEqual(current["evidence"]["v16_live_stop_reason"], "step5d_contact_safety:hold_duty_limit")
        self.assertEqual(
            current["evidence"]["v16_controller_readback_manifest"],
            "runs/controller_readback_step5d_strict_rnn_liveprep_v16_20260702_145838/manifest.json",
        )
        self.assertTrue(current["evidence"]["v17_local_package_validated"])
        self.assertTrue(current["evidence"]["v17_controller_readback_verified"])
        self.assertEqual(
            current["evidence"]["v17_controller_readback_manifest"],
            "runs/controller_readback_step5d_strict_rnn_liveprep_v17_20260702_155806/manifest.json",
        )
        self.assertEqual(current["evidence"]["v17_live_stop_reason"], "step5d_contact_safety:hold_duty_limit")
        self.assertTrue(current["evidence"]["v18_local_package_validated"])
        self.assertTrue(current["evidence"]["v18_controller_readback_verified"])
        self.assertEqual(
            current["evidence"]["v18_controller_readback_manifest"],
            "runs/controller_readback_step5d_strict_rnn_liveprep_v18_20260702_164653/manifest.json",
        )
        self.assertEqual(current["evidence"]["v18_live_stop_reason"], "step5d_contact_safety:cage_primary_tcp_speed_hard_stop")
        self.assertEqual(current["evidence"]["v18_preload_gate"]["filtered_normal_load_min_n"], 8.0)
        self.assertTrue(current["evidence"]["v19_local_package_validated"])
        self.assertTrue(current["evidence"]["v19_controller_readback_verified"])
        self.assertEqual(
            current["evidence"]["v19_controller_readback_manifest"],
            "runs/controller_readback_step5d_strict_rnn_liveprep_v19_20260702_172839/manifest.json",
        )
        self.assertEqual(current["evidence"]["v19_preload_gate"]["filtered_normal_load_min_n"], 8.0)
        self.assertEqual(current["evidence"]["v19_preload_gate"]["filtered_normal_load_max_n"], 13.0)
        self.assertEqual(current["evidence"]["v19_speed_changes"]["stage22_entry_movel_speed_m_s"], 0.060)
        self.assertEqual(current["evidence"]["v19_speed_changes"]["stage24_far_search_speed_m_s"], -0.0225)
        self.assertEqual(current["evidence"]["v19_active_reacquire_policy"]["predicted_tcp_speed_cap_m_s"], 0.035)
        self.assertEqual(current["evidence"]["step5d_projector_root_cause_fix"]["status"], "present_in_worktree")
        self.assertFalse(current["bridge_trigger"]["bridge_has_started"])
        self.assertTrue(current["bridge_trigger"]["live_motion_authorized"])
        self.assertEqual(current["bridge_trigger"]["allowed_tokens"], ["LIVE STEP5D STRICT RNN LIVEPREP"])

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
