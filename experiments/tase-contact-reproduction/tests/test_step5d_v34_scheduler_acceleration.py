#!/usr/bin/env python3
"""Regression tests for v34 matched acceleration and late FIFO lifecycle."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import analyze_step5d_bridge_run as analyzer  # noqa: E402
import build_step5d_liveprep as builder  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
from step5d_control_contract import (  # noqa: E402
    ControlCandidate,
    Step5dObservation,
    apply_direction_preserving_slew,
)
from step5d_runtime_interface import STEP5D_ABLATION_V34_STAGE_ID, build_stage_env  # noqa: E402


def observation() -> Step5dObservation:
    return Step5dObservation(
        sequence=1,
        timestamp_s=1.0,
        q=(0.0,) * 6,
        qd=(0.0,) * 6,
        tcp_pose=(0.0,) * 6,
        tcp_twist=(0.0,) * 6,
        wrench=(0.0,) * 6,
        jacobian=tuple(tuple(float(i == j) for j in range(6)) for i in range(6)),
        desired_twist=(1.0,) * 6,
        reaction_normal=(0.0, 0.0, -1.0),
        approach_normal=(0.0, 0.0, 1.0),
        command_frame="base",
        normal_frame="base",
        normal_motion_policy="frame_contract_only",
    )


class Step5dV34SchedulerAccelerationTest(unittest.TestCase):
    def test_generated_tp_and_stage_bind_both_accelerations_to_point_one(self) -> None:
        spec = builder.ABLATION_SPECS[STEP5D_ABLATION_V34_STAGE_ID]
        self.assertEqual(builder.joint_accel_rad_s2(spec), 0.1)
        self.assertEqual(builder.host_qdot_slew_rad_s2(spec), 0.1)
        script = (ROOT / "programs" / "step5" / "step5d" / f"{STEP5D_ABLATION_V34_STAGE_ID}.script").read_text(encoding="utf-8")
        self.assertIn("local joint_accel_rad_s2 = 0.100", script)
        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        row = next(item for item in table["stages"] if item.get("id") == STEP5D_ABLATION_V34_STAGE_ID)
        self.assertEqual(row["runtime_profile"]["command_slew_rad_s2"], 0.1)
        self.assertEqual(row["runtime_profile"]["tp_speedj_acceleration_rad_s2"], 0.1)

    def test_direction_preserving_slew_limits_each_tick_to_point_one_times_dt(self) -> None:
        raw = ControlCandidate(
            qdot=(0.5, -0.5, 0.25, -0.25, 0.1, -0.1),
            predicted_twist=(0.5, -0.5, 0.25, -0.25, 0.1, -0.1),
            residual_norm=0.0,
            active_bounds_count=0,
            frame_id="base",
            solver_status="40",
        )
        dt_s = 0.002
        slewed = apply_direction_preserving_slew(
            observation(), raw, previous_qdot=(0.0,) * 6, dt_s=dt_s, max_slew_rad_s2=0.1
        )
        self.assertLessEqual(max(abs(value) for value in slewed.qdot), 0.1 * dt_s + 1e-15)

    def test_stage_env_keeps_permissive_guards_and_v34_profile(self) -> None:
        env = build_stage_env(STEP5D_ABLATION_V34_STAGE_ID)
        self.assertEqual(env["BRIDGE_PROFILE"], STEP5D_ABLATION_V34_STAGE_ID)
        self.assertEqual(env["STEP5D_QDOT_LIMIT_RAD_S"], "0.500")
        self.assertEqual(env["STEP5D_RNN_INNER_ITERATIONS"], "512")
        self.assertEqual(env["BRIDGE_SENSOR_STALE_S"], "2.00")
        self.assertEqual(env["STEP5D_PRELOAD_RAW_MIN_N"], "3.0")
        self.assertEqual(env["STEP5D_PRELOAD_RAW_MAX_N"], "25.0")

    def test_late_fifo_promotes_only_control_thread_and_keeps_quota(self) -> None:
        initial = {"policy": "SCHED_OTHER", "policy_value": os.SCHED_OTHER, "priority": 0}
        control = {"policy": "SCHED_FIFO", "policy_value": os.SCHED_FIFO, "priority": 20}
        before = {
            "current_tid": 10,
            "thread_count": 2,
            "policy_counts": {"SCHED_OTHER/0": 2},
            "threads": [
                {"tid": 10, "is_control_thread": True, "policy": "SCHED_OTHER", "policy_value": os.SCHED_OTHER, "priority": 0},
                {"tid": 11, "is_control_thread": False, "policy": "SCHED_OTHER", "policy_value": os.SCHED_OTHER, "priority": 0},
            ],
        }
        after = {
            "current_tid": 10,
            "thread_count": 2,
            "policy_counts": {"SCHED_FIFO/20": 1, "SCHED_OTHER/0": 1},
            "threads": [
                {"tid": 10, "is_control_thread": True, "policy": "SCHED_FIFO", "policy_value": os.SCHED_FIFO, "priority": 20},
                {"tid": 11, "is_control_thread": False, "policy": "SCHED_OTHER", "policy_value": os.SCHED_OTHER, "priority": 0},
            ],
        }
        quota = {"sched_rt_period_us": 1_000_000, "sched_rt_runtime_us": 950_000}
        with (
            patch.object(bridge, "runtime_scheduler_metadata", side_effect=[initial, control]),
            patch.object(bridge, "runtime_thread_scheduler_snapshot", side_effect=[before, after]),
            patch.object(bridge, "linux_rt_bandwidth_metadata", side_effect=[quota, quota]),
            patch.object(bridge.os, "sched_setscheduler") as set_scheduler,
        ):
            lifecycle = bridge.promote_v34_control_thread_scheduler(STEP5D_ABLATION_V34_STAGE_ID)
        set_scheduler.assert_has_calls([call(0, os.SCHED_FIFO, os.sched_param(20))])
        self.assertTrue(lifecycle["promotion_verified"])
        self.assertEqual(lifecycle["helper_non_other_thread_count"], 0)
        self.assertTrue(lifecycle["kernel_rt_bandwidth_unchanged"])

    def test_analyzer_v34_acceptance_requires_zero_gap_scheduler_orientation_and_oracle(self) -> None:
        result = {
            "stage25_rows": 30_000,
            "stage25_row_gap_count": 0,
            "stage25_cadence_ok": True,
            "terminal_tp_stop_reason": 1,
            "stage25_control_attribution": {
                "command_layout_tag_counts": {"524": 30_000},
                "rnn_accepted_continuous_duration_s": 60.01,
                "feedback_age_p99_s": 0.009,
                "sent_echo_heartbeat_gap_max": 5,
                "xy_tracking_error_p95_m": 0.0004,
                "xy_tracking_error_max_m": 0.0009,
                "command_actual_qd_correlation": 0.95,
                "command_actual_qd_lag_s": 0.018,
                "rnn_accepted_consumed_ratio": 0.99,
                "rnn_accepted_rows": 30_000,
                "orientation_error_p95_rad": 0.02,
                "orientation_error_max_rad": 0.04,
                "orientation_stable_target_divergence_windows": 0,
                "rnn_oracle_qdot_delta_max": 9e-7,
                "rnn_oracle_qdot_delta_rows": 30_000,
                "raw_rnn_residual_rows": 30_000,
                "raw_rnn_residual_max": 1e-7,
                "post_slew_command_residual_rows": 30_000,
                "post_slew_command_residual_max": 1e-3,
                "legacy_raw_residual_mismatch_rows": 0,
                "scheduler_promotion_verified": True,
                "scheduler_initial_policy": "SCHED_OTHER",
                "scheduler_control_policy": "SCHED_FIFO",
                "scheduler_control_priority": 20,
                "scheduler_helper_non_other_thread_count": 0,
                "scheduler_kernel_rt_bandwidth_unchanged": True,
                "scheduler_python_gc_enabled_during_control": False,
                "final_safety_mode": 1,
                "gross_guard_rows": 0,
                "terminal_contact_safety_reason": None,
            },
        }
        self.assertTrue(analyzer.stage25_speedj_rnn_live_success(STEP5D_ABLATION_V34_STAGE_ID, result, "speedj_rnn_live"))
        result["stage25_row_gap_count"] = 1
        self.assertFalse(analyzer.stage25_speedj_rnn_live_success(STEP5D_ABLATION_V34_STAGE_ID, result, "speedj_rnn_live"))

    def test_raw_and_post_slew_residual_fields_are_distinct(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")
        self.assertIn('values["_step5d_raw_rnn_residual_norm"] = step5d_result.residual_norm', source)
        self.assertIn('values["_step5d_post_slew_residual_norm"] = float(', source)
        candidate_block = source.split("if candidate_v30 is not None:", 1)[1].split(
            'values["_step5d_engage_gate_ok"]', 1
        )[0]
        self.assertNotIn('_step5d_constraint_residual_norm', candidate_block)

    def test_feedback_nonfinite_age_is_stale_and_preserves_dwell(self) -> None:
        freshness = bridge.RTDEFeedbackFreshness()
        self.assertFalse(freshness.update_guard(float("inf"), 10.0))
        self.assertTrue(freshness.update_guard(float("inf"), 10.101))
        self.assertEqual(freshness.observe(float("nan"), 10.102), float("inf"))


if __name__ == "__main__":
    unittest.main()
