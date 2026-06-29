#!/usr/bin/env python3
"""Focused tests for the legacy Step5b 5N-to-15N ramp sentinel."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import kunwei_rtde_bridge as bridge  # noqa: E402


def ramp_args(*extra: str) -> object:
    args = bridge.parse_args(
        [
            "--no-start-command",
            "--skip-dashboard-preflight",
            "--step4e-mode",
            "line",
            "--step4e-version",
            "step5b_v2",
            "--step5b-trial-profile",
            "ramp_5_to_15_sentinel",
            "--target-force-n",
            "15.0",
            "--step4e-force-p-gain",
            "0.00015",
            "--step4e-force-i-gain",
            "0.0",
            "--step4e-force-damping",
            "1.2",
            "--step4e-normal-velocity-limit-m-s",
            "0.0010",
            "--step4e-total-linear-limit-m-s",
            "0.0040",
            "--step4e-integral-limit-n-s",
            "1.0",
            *extra,
        ]
    )
    bridge.validate_common_target_force(args)
    bridge.validate_step5b_15n_trial_args(args)
    return args


def acquired_state() -> bridge.BridgeState:
    state = bridge.BridgeState()
    state.latched_normal_b = (0.0, 0.0, 1.0)
    state.filtered_normal_b = (0.0, 0.0, 1.0)
    state.latched_normal_locked = True
    state.normal_acquired = True
    return state


def compute_at_load(load_n: float, state: bridge.BridgeState | None = None) -> dict[str, object]:
    latest_output = {
        "actual_TCP_pose": [0.49, 0.14, 0.02, 0.0, 0.0, 0.0],
        "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        "output_double_register_35": 25.0,
    }
    return bridge.compute_bridge_values(
        ramp_args(),
        [0.0, 0.0, load_n, 0.0, 0.0, 0.0],
        latest_output,
        1.0,
        state or acquired_state(),
        0.002,
    )


class Step5bRamp5To15SentinelTest(unittest.TestCase):
    def test_ramp_profile_is_step5b_line_15n_only(self) -> None:
        args = ramp_args()
        self.assertTrue(bridge.step5b_ramp_trial_enabled(args))
        self.assertEqual(args.target_force_n, 15.0)

        bad = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step4f_v1",
                "--step5b-trial-profile",
                "ramp_5_to_15_sentinel",
                "--target-force-n",
                "15.0",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "requires Step5b line mode"):
            bridge.validate_step5b_15n_trial_args(bad)

        bad_target = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5b_v2",
                "--step5b-trial-profile",
                "ramp_5_to_15_sentinel",
                "--target-force-n",
                "14.0",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "requires --target-force-n 15.0"):
            bridge.validate_step5b_15n_trial_args(bad_target)

        fast_normal = ramp_args("--step4e-normal-velocity-limit-m-s", "0.0100")
        self.assertAlmostEqual(fast_normal.bridge_normal_velocity_limit_m_s, 0.0100)

    def test_preload_high_load_unloads_without_old_20n_stop(self) -> None:
        values = compute_at_load(26.0)
        self.assertEqual(values["_step5b_ramp_phase"], "pre_unload_to_5")
        self.assertEqual(values["_step5b_ramp_active_target_force_n"], 5.0)
        self.assertEqual(values["_step5b_ramp_stop_reason"], "")
        self.assertEqual(values["_step4e_desired_vx_m_s"], 0.0)
        self.assertEqual(values["_step4e_desired_vy_m_s"], 0.0)
        self.assertGreater(values["step4e_cmd_vz_m_s"], 0.0)

    def test_preload_low_load_presses_along_approach_normal(self) -> None:
        values = compute_at_load(3.0)
        self.assertEqual(values["_step5b_ramp_phase"], "pre_unload_to_5")
        self.assertLess(values["step4e_cmd_vz_m_s"], 0.0)

    def test_ramp_target_schedule_is_monotonic(self) -> None:
        state = bridge.BridgeState()
        bridge.set_step5b_ramp_phase(state, "ramp_5_to_15")
        state.step5b_ramp_phase_s = 2.5
        bridge.step5b_ramp_trial_phase_update(
            normal_load_n=10.0,
            force_norm_n=10.0,
            dt_s=0.0,
            state=state,
        )
        self.assertAlmostEqual(state.step5b_ramp_active_target_force_n, 10.0)
        self.assertAlmostEqual(state.step5b_ramp_alpha, 0.5)

        state.step5b_ramp_phase_s = 5.0
        bridge.step5b_ramp_trial_phase_update(
            normal_load_n=15.0,
            force_norm_n=15.0,
            dt_s=0.0,
            state=state,
        )
        self.assertEqual(state.step5b_ramp_phase, "hold_15")
        self.assertEqual(state.step5b_ramp_active_target_force_n, 15.0)

    def test_ramp_guard_has_50n_minimum(self) -> None:
        state = bridge.BridgeState()
        bridge.set_step5b_ramp_phase(state, "ramp_5_to_15")
        state.step5b_ramp_active_target_force_n = 5.0
        self.assertIsNone(
            bridge.step5b_ramp_trial_guard_reason(
                normal_load_n=10.1,
                force_norm_n=10.1,
                torque_norm_nm=0.1,
                sensor_ok=1.0,
                normal_velocity_m_s=0.0,
                normal_velocity_limit_m_s=0.001,
                dt_s=0.002,
                state=state,
            )
        )
        self.assertIsNone(
            bridge.step5b_ramp_trial_guard_reason(
                normal_load_n=49.9,
                force_norm_n=49.9,
                torque_norm_nm=0.1,
                sensor_ok=1.0,
                normal_velocity_m_s=0.0,
                normal_velocity_limit_m_s=0.001,
                dt_s=0.002,
                state=state,
            )
        )
        self.assertEqual(
            bridge.step5b_ramp_trial_guard_reason(
                normal_load_n=50.1,
                force_norm_n=50.1,
                torque_norm_nm=0.1,
                sensor_ok=1.0,
                normal_velocity_m_s=0.0,
                normal_velocity_limit_m_s=0.001,
                dt_s=0.002,
                state=state,
            ),
            "step5b_ramp_5_to_15:normal_load_stop",
        )

    def test_ramp_dropout_requires_half_second_continuous_low_load(self) -> None:
        state = bridge.BridgeState()
        bridge.set_step5b_ramp_phase(state, "ramp_5_to_15")
        state.step5b_ramp_active_target_force_n = 5.0
        reason = None
        for _ in range(249):
            reason = bridge.step5b_ramp_trial_guard_reason(
                normal_load_n=1.0,
                force_norm_n=1.0,
                torque_norm_nm=0.1,
                sensor_ok=1.0,
                normal_velocity_m_s=0.0,
                normal_velocity_limit_m_s=0.001,
                dt_s=0.002,
                state=state,
            )
        self.assertIsNone(reason)
        self.assertAlmostEqual(state.step5b_ramp_low_load_s, 0.498)
        self.assertEqual(
            bridge.step5b_ramp_trial_guard_reason(
                normal_load_n=1.0,
                force_norm_n=1.0,
                torque_norm_nm=0.1,
                sensor_ok=1.0,
                normal_velocity_m_s=0.0,
                normal_velocity_limit_m_s=0.001,
                dt_s=0.002,
                state=state,
            ),
            "step5b_ramp_5_to_15:low_load_dropout",
        )

        state = bridge.BridgeState()
        bridge.set_step5b_ramp_phase(state, "ramp_5_to_15")
        state.step5b_ramp_active_target_force_n = 10.0
        reason = None
        for _ in range(249):
            reason = bridge.step5b_ramp_trial_guard_reason(
                normal_load_n=2.4,
                force_norm_n=2.4,
                torque_norm_nm=0.1,
                sensor_ok=1.0,
                normal_velocity_m_s=0.0,
                normal_velocity_limit_m_s=0.001,
                dt_s=0.002,
                state=state,
            )
        self.assertIsNone(reason)
        self.assertAlmostEqual(state.step5b_ramp_relative_low_load_s, 0.498)
        self.assertEqual(
            bridge.step5b_ramp_trial_guard_reason(
                normal_load_n=2.4,
                force_norm_n=2.4,
                torque_norm_nm=0.1,
                sensor_ok=1.0,
                normal_velocity_m_s=0.0,
                normal_velocity_limit_m_s=0.001,
                dt_s=0.002,
                state=state,
            ),
            "step5b_ramp_5_to_15:relative_low_load_dropout",
        )

    def test_summary_passes_clean_short_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            bridge_csv = out / "bridge_rtde_500hz.csv"
            fields = [
                "_step5b_ramp_active",
                "_step5b_ramp_phase",
                "_step5b_ramp_stop_reason",
                "_step5b_ramp_active_target_force_n",
                "_step5b_ramp_xy_enabled",
                "_step5b_ramp_normal_velocity_saturated",
                "_step4e_desired_vx_m_s",
                "_step4e_desired_vy_m_s",
                "_step4e_normal_load_n",
                "force_norm_n",
                "torque_norm_nm",
            ]
            with bridge_csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for target in [5, 7, 10, 13, 15]:
                    writer.writerow(
                        {
                            "_step5b_ramp_active": "1",
                            "_step5b_ramp_phase": "ramp_5_to_15",
                            "_step5b_ramp_stop_reason": "",
                            "_step5b_ramp_active_target_force_n": str(target),
                            "_step5b_ramp_xy_enabled": "0",
                            "_step5b_ramp_normal_velocity_saturated": "0",
                            "_step4e_desired_vx_m_s": "0",
                            "_step4e_desired_vy_m_s": "0",
                            "_step4e_normal_load_n": str(target),
                            "force_norm_n": str(target),
                            "torque_norm_nm": "0.1",
                        }
                    )
                for idx in range(5):
                    writer.writerow(
                        {
                            "_step5b_ramp_active": "1",
                            "_step5b_ramp_phase": "move_xy",
                            "_step5b_ramp_stop_reason": "step5b_ramp_5_to_15:complete" if idx == 4 else "",
                            "_step5b_ramp_active_target_force_n": "15",
                            "_step5b_ramp_xy_enabled": "1",
                            "_step5b_ramp_normal_velocity_saturated": "0",
                            "_step4e_desired_vx_m_s": "0.001",
                            "_step4e_desired_vy_m_s": "0.001",
                            "_step4e_normal_load_n": "15",
                            "force_norm_n": "15.5",
                            "torque_norm_nm": "0.1",
                        }
                    )
            payload = bridge.write_step5b_ramp_trial_summary(
                output_dir=out,
                bridge_csv_path=bridge_csv,
                metadata={"args": {"target_force_n": 15.0}},
                stop_reason="step5b_ramp_5_to_15:complete",
            )
            self.assertEqual(payload["verdict"], "pass_ramp_and_short_move")
            self.assertTrue((out / "step5b_ramp_5_to_15_sentinel_summary.json").is_file())
            self.assertEqual(
                json.loads((out / "step5b_ramp_5_to_15_sentinel_summary.json").read_text(encoding="utf-8"))["verdict"],
                "pass_ramp_and_short_move",
            )


if __name__ == "__main__":
    unittest.main()
