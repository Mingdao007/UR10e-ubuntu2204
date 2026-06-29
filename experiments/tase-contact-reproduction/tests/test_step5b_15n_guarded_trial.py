#!/usr/bin/env python3
"""Focused tests for the legacy Step5b 15N guarded trial."""

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


def guarded_args(*extra: str) -> object:
    args = bridge.parse_args(
        [
            "--no-start-command",
            "--skip-dashboard-preflight",
            "--step4e-mode",
            "line",
            "--step4e-version",
            "step5b_v2",
            "--step5b-trial-profile",
            "guarded_15n_sentinel",
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


class Step5b15NGuardedTrialTest(unittest.TestCase):
    def test_guarded_trial_accepts_exact_15n_only(self) -> None:
        args = guarded_args()
        self.assertTrue(bridge.step5b_15n_trial_enabled(args))
        self.assertEqual(args.target_force_n, 15.0)

        fast_normal = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5b_v2",
                "--step5b-trial-profile",
                "guarded_15n_sentinel",
                "--target-force-n",
                "15.0",
                "--step4e-normal-velocity-limit-m-s",
                "0.0100",
                "--step4e-total-linear-limit-m-s",
                "0.0040",
                "--step4e-integral-limit-n-s",
                "1.0",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "normal velocity limit <= 0.0011 m/s"):
            bridge.validate_step5b_15n_trial_args(fast_normal)

        bad = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5b_v2",
                "--step5b-trial-profile",
                "guarded_15n_sentinel",
                "--target-force-n",
                "16.0",
                "--step4e-normal-velocity-limit-m-s",
                "0.0010",
                "--step4e-total-linear-limit-m-s",
                "0.0040",
                "--step4e-integral-limit-n-s",
                "1.0",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "requires --target-force-n 15.0"):
            bridge.validate_step5b_15n_trial_args(bad)

    def test_guarded_trial_is_step5b_only(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step4f_v1",
                "--step5b-trial-profile",
                "guarded_15n_sentinel",
                "--target-force-n",
                "15.0",
            ]
        )
        with self.assertRaisesRegex(SystemExit, "requires Step5b line mode"):
            bridge.validate_step5b_15n_trial_args(args)

    def test_stage25_sentinel_freezes_xy_command(self) -> None:
        args = guarded_args()
        state = bridge.BridgeState()
        latest_output = {
            "actual_TCP_pose": [0.49, 0.14, 0.02, 0.0, 0.0, 0.0],
            "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "output_double_register_35": 25.0,
        }
        values = bridge.compute_bridge_values(
            args,
            [0.0, 0.0, 15.0, 0.0, 0.0, 0.0],
            latest_output,
            1.0,
            state,
            0.002,
        )
        self.assertEqual(values["_step5b_15n_trial_active"], 1.0)
        self.assertEqual(values["_step4e_desired_vx_m_s"], 0.0)
        self.assertEqual(values["_step4e_desired_vy_m_s"], 0.0)
        self.assertEqual(values["_step4e_path_error_x_m"], 0.0)
        self.assertEqual(values["_step4e_path_error_y_m"], 0.0)

    def test_guard_helper_stops_high_force_and_saturation(self) -> None:
        state = bridge.BridgeState()
        self.assertIsNone(
            bridge.step5b_15n_trial_guard_reason(
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
            bridge.step5b_15n_trial_guard_reason(
                normal_load_n=50.1,
                force_norm_n=50.1,
                torque_norm_nm=0.1,
                sensor_ok=1.0,
                normal_velocity_m_s=0.0,
                normal_velocity_limit_m_s=0.001,
                dt_s=0.002,
                state=state,
            ),
            "step5b_15n_trial:normal_load_stop",
        )
        state = bridge.BridgeState()
        reason = None
        for _ in range(100):
            reason = bridge.step5b_15n_trial_guard_reason(
                normal_load_n=15.0,
                force_norm_n=15.0,
                torque_norm_nm=0.1,
                sensor_ok=1.0,
                normal_velocity_m_s=0.001,
                normal_velocity_limit_m_s=0.001,
                dt_s=0.002,
                state=state,
            )
        self.assertEqual(reason, "step5b_15n_trial:normal_velocity_saturation")

    def test_guarded_dropout_requires_half_second_continuous_low_load(self) -> None:
        state = bridge.BridgeState()
        state.step5b_15n_acquired = True
        reason = None
        for _ in range(249):
            reason = bridge.step5b_15n_trial_guard_reason(
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
        self.assertAlmostEqual(state.step5b_15n_low_load_s, 0.498)
        self.assertEqual(
            bridge.step5b_15n_trial_guard_reason(
                normal_load_n=1.0,
                force_norm_n=1.0,
                torque_norm_nm=0.1,
                sensor_ok=1.0,
                normal_velocity_m_s=0.0,
                normal_velocity_limit_m_s=0.001,
                dt_s=0.002,
                state=state,
            ),
            "step5b_15n_trial:low_load_dropout",
        )

    def test_summary_verdict_passes_clean_static_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            bridge_csv = out / "bridge_rtde_500hz.csv"
            fields = [
                "_step5b_15n_trial_active",
                "_step5b_15n_trial_scored_s",
                "_step5b_15n_trial_stop_reason",
                "_step5b_15n_trial_normal_velocity_saturated",
                "_step4e_normal_load_n",
                "force_norm_n",
                "torque_norm_nm",
            ]
            with bridge_csv.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for idx in range(5):
                    writer.writerow(
                        {
                            "_step5b_15n_trial_active": "1",
                            "_step5b_15n_trial_scored_s": str(0.002 * (idx + 1)),
                            "_step5b_15n_trial_stop_reason": "step5b_15n_trial:complete" if idx == 4 else "",
                            "_step5b_15n_trial_normal_velocity_saturated": "0",
                            "_step4e_normal_load_n": "15",
                            "force_norm_n": "15.5",
                            "torque_norm_nm": "0.2",
                        }
                    )
            payload = bridge.write_step5b_15n_trial_summary(
                output_dir=out,
                bridge_csv_path=bridge_csv,
                metadata={"args": {"target_force_n": 15.0}},
                stop_reason="step5b_15n_trial:complete",
            )
            self.assertEqual(payload["verdict"], "pass_to_repeat_static")
            self.assertTrue((out / "step5b_15n_guarded_trial_summary.json").is_file())
            self.assertEqual(
                json.loads((out / "step5b_15n_guarded_trial_summary.json").read_text(encoding="utf-8"))["verdict"],
                "pass_to_repeat_static",
            )


if __name__ == "__main__":
    unittest.main()
