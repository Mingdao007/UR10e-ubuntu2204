#!/usr/bin/env python3
"""Regression tests for v33 latest-sample feedback and Step5b-equivalent outer."""

from __future__ import annotations

import math
import struct
import sys
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import kunwei_rtde_bridge as bridge  # noqa: E402
import analyze_step5d_bridge_run as analyzer  # noqa: E402
from step5d_paper_outer_loop import (  # noqa: E402
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
)


class Step5dV33FreshFeedbackTest(unittest.TestCase):
    def test_force_and_tangential_update_match_step5b_discrete_form(self) -> None:
        state = Step5dOuterLoopState(
            force_integral_n_s=0.25,
            xdot_p_prev_m_s=(0.003, -0.002, 0.004),
        )
        dt_s = 0.002
        result = compute_step5d_outer_loop(
            Step5dOuterLoopConfig(
                kp=1.5,
                ko=0.4,
                kf=0.01,
                Md_scalar=1000.0,
                Bd_scalar=7000.0,
                force_target_n=12.0,
                force_integral_limit_n_s=1.0,
            ),
            state,
            Step5dOuterLoopInputs(
                tcp_pose_base=(0.1, -0.2, 0.3, 0.0, 0.0, 0.0),
                tcp_speed_base=(0.0,) * 6,
                force_tcp_n=(0.0, 0.0, 10.0),
                control_reaction_normal_base=(0.0, 0.0, 1.0),
                x_pd_base=(0.102, -0.204, 0.3),
                xdot_pd_base=(0.005, 0.001, 0.0),
                dt_s=dt_s,
            ),
        )
        expected_integral = 0.25 + 2.0 * dt_s
        expected_xy = np.array([0.005, 0.001]) + 1.5 * np.array([0.002, -0.004])
        expected_z = 0.004 + dt_s * (-(0.001 * 2.0 + 1e-5 * expected_integral) - 7.0 * 0.004)
        np.testing.assert_allclose(result.xdot_p[:2], expected_xy, atol=1e-12)
        self.assertAlmostEqual(result.xdot_p[2], expected_z, places=12)
        self.assertAlmostEqual(result.next_state.force_integral_n_s, expected_integral, places=12)

    def test_orientation_is_step5b_equivalent_below_point_zero_five_rad(self) -> None:
        for theta in np.linspace(1e-6, 0.05, 50):
            paper_magnitude = 0.4 * math.sin(theta / 2.0)
            step5b_magnitude = 0.2 * math.sin(theta)
            self.assertLessEqual(abs(paper_magnitude - step5b_magnitude) / step5b_magnitude, 0.01)

    def test_drain_returns_only_newest_packet(self) -> None:
        client = bridge.RTDEBridgeClient.__new__(bridge.RTDEBridgeClient)
        client.sock = object()
        packets = deque(
            (ord("U"), bytes([7]) + struct.pack("!d", timestamp))
            for timestamp in (1.0, 1.002, 1.004)
        )
        client._recv_packet = packets.popleft
        readiness = [([client.sock], [], []), ([client.sock], [], []), ([client.sock], [], []), ([], [], [])]
        with patch.object(bridge.select, "select", side_effect=readiness):
            sample, drained = client.recv_latest_available_sample(7, ["DOUBLE"])
        self.assertEqual(drained, 3)
        self.assertEqual(sample, {"timestamp": 1.004})

    def test_500hz_producer_with_slower_consumer_has_bounded_latest_age(self) -> None:
        for consumer_hz in (350.0, 400.0, 450.0):
            queue: list[float] = []
            next_producer = 0.0
            ages: list[float] = []
            heartbeat_gaps: list[int] = []
            sent_heartbeat = 0
            for consumer_index in range(2000):
                now = consumer_index / consumer_hz
                packets: list[tuple[float, int]] = []
                while next_producer <= now + 1e-12:
                    queue.append(next_producer)
                    packets.append((next_producer, sent_heartbeat))
                    next_producer += 1.0 / 500.0
                newest, echoed_heartbeat = packets[-1]
                queue.clear()
                ages.append(now - newest)
                heartbeat_gaps.append(max(0, sent_heartbeat - echoed_heartbeat))
                sent_heartbeat += 1
            self.assertLessEqual(max(ages), 1.0 / 500.0 + 1e-12)
            self.assertEqual(max(heartbeat_gaps), 0)

    def test_feedback_guard_requires_continuous_dwell(self) -> None:
        tracker = bridge.RTDEFeedbackFreshness()
        self.assertFalse(tracker.update_guard(0.060, 10.000))
        self.assertFalse(tracker.update_guard(0.001, 10.050))
        self.assertFalse(tracker.update_guard(0.060, 20.000))
        self.assertFalse(tracker.update_guard(0.060, 20.099))
        self.assertTrue(tracker.update_guard(0.060, 20.100))

    def test_feedback_guard_uses_elapsed_time_after_missed_slots(self) -> None:
        tracker = bridge.RTDEFeedbackFreshness()
        self.assertFalse(tracker.update_guard(0.060, 100.000))
        self.assertTrue(tracker.update_guard(0.060, 100.125))

    def test_command_actual_qd_alignment_recovers_two_tick_lag(self) -> None:
        rows: list[dict[str, str]] = []
        commands = [math.sin(index * 0.17) for index in range(80)]
        for index, command in enumerate(commands):
            row = {"t_monotonic_s": f"{index * 0.002:.6f}"}
            for axis in range(6):
                row[f"_step5d_post_slew_qd{axis}_rad_s"] = f"{command * (axis + 1):.12f}"
                delayed = commands[index - 2] * (axis + 1) if index >= 2 else 0.0
                row[f"ur_actual_qd_{axis}"] = f"{delayed:.12f}"
            rows.append(row)
        result = analyzer.command_actual_qd_alignment(rows)
        self.assertGreaterEqual(float(result["command_actual_qd_correlation"] or 0.0), 0.999)
        self.assertAlmostEqual(float(result["command_actual_qd_lag_s"] or 0.0), 0.004, places=6)

    def test_v33_canary_has_a_positive_acceptance_path(self) -> None:
        result = {
            "stage25_rows": 10_000,
            "stage25_cadence_ok": True,
            "terminal_tp_stop_reason": 1,
            "stage25_control_attribution": {
                "command_layout_tag_counts": {"524": 10_000},
                "rnn_accepted_continuous_duration_s": 20.01,
                "feedback_age_p99_s": 0.009,
                "sent_echo_heartbeat_gap_max": 5,
                "xy_tracking_error_p95_m": 0.0004,
                "xy_tracking_error_max_m": 0.0009,
                "command_actual_qd_correlation": 0.95,
                "command_actual_qd_lag_s": 0.018,
                "rnn_accepted_consumed_ratio": 0.99,
                "final_safety_mode": 1,
                "gross_guard_rows": 0,
                "terminal_contact_safety_reason": None,
            },
        }
        self.assertTrue(
            analyzer.stage25_speedj_rnn_live_success(
                "step5d_strict_rnn_ablation_v33c20",
                result,
                "speedj_rnn_live",
            )
        )

    def test_metadata_free_v33c20_run_name_is_inferred_exactly(self) -> None:
        run_dir = Path("runs/bridge_step5d_strict_rnn_ablation_v33c20_20260714_000000")
        self.assertEqual(
            analyzer.infer_step5d_profile(run_dir, {}),
            "step5d_strict_rnn_ablation_v33c20",
        )


if __name__ == "__main__":
    unittest.main()
