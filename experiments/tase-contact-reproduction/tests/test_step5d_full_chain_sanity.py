#!/usr/bin/env python3
"""Offline checks for the Step5d full-chain sanity runner."""

from __future__ import annotations

import inspect
import gzip
import csv
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import kunwei_rtde_bridge as bridge  # noqa: E402
import build_step5d_liveprep as liveprep  # noqa: E402
import contact_semantics  # noqa: E402
import step5d_v11_escape_replay as escape_replay  # noqa: E402
import step5d_full_chain_sanity as sanity  # noqa: E402


class Step5dFullChainSanityTest(unittest.TestCase):
    def test_full_chain_sanity_produces_offline_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = sanity.run_sanity(output_dir=Path(tmpdir), sample_limit=8)
        self.assertTrue(summary["overall_pass"])
        self.assertEqual(summary["assumptions"]["contact_evidence"], "not_claimed")
        self.assertEqual(summary["assumptions"]["position_error_mode"], "zeroed_for_structural_sanity")
        self.assertEqual(summary["assumptions"]["force_input"], "synthetic_environment_on_tool_force_along_reaction_normal")
        self.assertEqual(summary["assumptions"]["orientation_target_axis"], "approach_normal_base = -control_reaction_normal_base")
        self.assertEqual(summary["assumptions"]["force_target_n"], 5.0)
        self.assertEqual(summary["assumptions"]["force_sign_convention"], "step5_step6_positive_normal_load")
        self.assertEqual(summary["assumptions"]["qdot_limit_rad_s"], 0.30)
        self.assertTrue(summary["gates"]["register_order_pass"])
        self.assertTrue(summary["gates"]["qdot_within_nominal_limit_pass"])
        self.assertTrue(summary["metrics"]["qdot_within_nominal_limit"])
        self.assertIn("constraint_residual_norm_rms", summary["metrics"])

    def test_bridge_knows_step5d_but_hard_blocks_live_start(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_reproduction_v1",
            ]
        )
        self.assertEqual(args.step4e_version, "step5d_strict_rnn_reproduction_v1")
        with self.assertRaisesRegex(SystemExit, "Blocked Step5d reproduction"):
            bridge.main(
                [
                    "--no-start-command",
                    "--skip-dashboard-preflight",
                    "--step4e-mode",
                    "line",
                    "--step4e-version",
                    "step5d_strict_rnn_reproduction_v1",
                ]
            )

    def test_step5d_liveprep_package_is_non_quarantine_speedj_executor(self) -> None:
        stamp = "2026-06-15T1200HKT_STEP5D_STRICT_RNN_LIVEPREP_V13"
        geom = liveprep.line_cfg(liveprep.load_json(liveprep.CONFIG_PATH))
        frame = liveprep.load_safe_frame()
        script = liveprep.build_script(stamp, "2026-06-14T12:00:00+08:00", geom, frame)
        txt = liveprep.build_txt(stamp)
        urp = liveprep.build_urp(script, liveprep.PROGRAM_NAME, liveprep.CONTROLLER_DIR)
        liveprep.validate_package(script, txt, urp, stamp)
        xml = gzip.decompress(urp).decode("utf-8")
        self.assertIn(f'URProgram name="{liveprep.PROGRAM_NAME}"', xml)
        self.assertIn("joint_executor_and_guard_only", script)
        self.assertIn("speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]", script)
        self.assertIn("local qdot_cap_rad_s = 0.050", script)
        self.assertIn("STAGE25_CONTACT_SAFETY", script)
        self.assertIn("cmd_valid=1 zero-qdot hold", script)
        self.assertIn("stop_request", script)
        self.assertIn("local skip_lift_attitude = 0", script)
        self.assertIn("local orientation_skip_error_rad = 0.069813", script)
        self.assertIn("if stop_reason == 0.0 and skip_lift_attitude == 0:", script)
        self.assertIn("codex_step5d_down_search(24.3, 24.4, 0.035, 0.000, 45.000, -0.0025, -0.0025)", script)
        self.assertIn("Stage 25.3 consumes 37..39 as Cartesian deadband-acquire vx/vy/vz", script)
        self.assertIn("local line_entry_normal_load_min_n = 2.000", script)
        self.assertIn("local line_entry_normal_load_max_n = 15.000", script)
        self.assertIn("local line_entry_force_norm_max_n = 25.000", script)
        self.assertIn("local line_entry_required_s = 0.150", script)
        self.assertNotIn("local line_entry_settle_cmd_max_m_s", script)
        self.assertNotIn("codex_abs(cmd_vx) <= line_entry_settle_cmd_max_m_s", script)
        self.assertIn("local line_entry_recovery_normal_load_min_n = 0.000", script)
        self.assertIn("local line_entry_recovery_normal_load_max_n = 40.000", script)
        self.assertIn("local line_entry_force_norm_stop_n = 100.000", script)
        self.assertNotIn("or normal_load < line_entry_recovery_normal_load_min_n", script)
        self.assertIn("elif stop_reason == 17.0:\n    return True", script)
        self.assertIn("local normal_load = target_force - force_error", script)
        self.assertIn("local line_entry_timeout_s = 10.000", script)
        self.assertIn("speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0]", script)
        self.assertIn("codex_abs(normal_force) > 100.0", script)
        self.assertIn("force_norm > 100.0", script)
        self.assertIn("deadband contact acquire", txt)
        self.assertIn("predicted TCP speed", txt)
        self.assertIn("2.0 N", txt)
        self.assertIn("15.0 N", txt)
        self.assertNotIn("speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy", script)
        self.assertNotIn("stop_only_quarantine", script + txt)
        self.assertNotIn("STEP5D_STRICT_RNN_LIVEPREP_V12", script + txt)

    def test_bridge_allows_liveprep_profile_but_keeps_full_reproduction_blocked(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v11",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(args.step4e_version, "step5d_strict_rnn_liveprep_v11")
        self.assertEqual(args.step5d_qdot_limit_rad_s, 0.30)
        self.assertFalse(args.disable_dashboard_program_watch)
        v12_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v12",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v12_args.step5d_qdot_limit_rad_s, 0.05)
        v13_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v13",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v13_args.step5d_qdot_limit_rad_s, 0.05)
        with self.assertRaisesRegex(SystemExit, "Blocked Step5d reproduction"):
            bridge.main(
                [
                    "--no-start-command",
                    "--skip-dashboard-preflight",
                    "--step4e-mode",
                    "line",
                    "--step4e-version",
                    "step5d_strict_rnn_reproduction_v1",
                ]
            )

    def test_step5d_contact_search_orientation_uses_approach_axis_branch(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")
        self.assertIn("Step5d must stay in this group", source)
        self.assertIn("or step5d_liveprep_profile\n        or step6b_profile", source)
        self.assertIn("TCP z must target the approach axis -n_control_b", source)
        self.assertIn(
            "if (v21_profile or v22_profile or angular_speedl_profile)\n        else n_control_b",
            source,
        )

    def test_step5d_operator_points_to_current_controller_package(self) -> None:
        operator = (ROOT / "scripts" / "step5d-liveprep-operator.sh").read_text(encoding="utf-8")
        base = (ROOT / "scripts" / "step4e-line-v1-operator.sh").read_text(encoding="utf-8")
        self.assertIn('STEP5D_VERSION="${STEP5D_VERSION:-step5d_strict_rnn_liveprep_v13}"', operator)
        self.assertIn('Bridge profile: ${STEP5D_VERSION}', operator)
        self.assertIn('STEP5D_CONFIRM', operator)
        self.assertIn('MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-100}"', operator)
        self.assertIn('MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-100}"', operator)
        self.assertIn('PROGRAM_LINE="/programs/andyl/kunwei/step5/${STEP4E_VERSION}.urp"', base)
        self.assertIn('PROGRAM_LINE="/programs/andyl/kunwei/step5/step5d/${STEP4E_VERSION}.urp"', base)
        self.assertIn('"step5d_strict_rnn_liveprep_v10" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v11"', base)
        self.assertIn('"step5d_strict_rnn_liveprep_v12" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v13"', base)
        self.assertNotIn('if [[ "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v9" ]]; then\n  PROGRAM_LINE="/programs/andyl/kunwei/step5/${STEP4E_VERSION}.urp"', base)
        self.assertIn('EXPECTED_BASENAME="${STEP4E_VERSION}.urp"', base)
        self.assertIn('RUN_LABEL="${STEP4E_VERSION}"', base)

    def test_step5d_v8_live_limiter_pid_and_contact_window_gate(self) -> None:
        limited, active = bridge.limit_step5d_live_xdot(
            [3.0, 4.0, 0.0, 0.2, 0.0, 0.0],
            max_linear_m_s=0.006,
            max_angular_rad_s=0.015,
        )
        self.assertTrue(active)
        self.assertLessEqual(float((limited[:3] @ limited[:3]) ** 0.5), 0.006 + 1e-12)
        self.assertLessEqual(float((limited[3:] @ limited[3:]) ** 0.5), 0.015 + 1e-12)
        self.assertTrue(bridge.step5d_force_settle_ready(normal_load_n=5.5, force_norm_n=6.0, target_force_n=5.0))
        self.assertFalse(bridge.step5d_force_settle_ready(normal_load_n=18.0, force_norm_n=18.3, target_force_n=5.0))
        self.assertFalse(bridge.step5d_contact_window_ready(normal_load_n=1.9, force_norm_n=6.0))
        self.assertTrue(bridge.step5d_contact_window_ready(normal_load_n=2.0, force_norm_n=6.0))
        v7_min, v7_max, v7_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v7")
        self.assertEqual((v7_min, v7_max, v7_force_max), (2.0, 15.0, 25.0))
        v8_min, v8_max, v8_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v8")
        self.assertEqual((v8_min, v8_max, v8_force_max), (3.0, 8.0, 25.0))
        v11_min, v11_max, v11_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v11")
        self.assertEqual((v11_min, v11_max, v11_force_max), (2.0, 15.0, 25.0))
        v12_min, v12_max, v12_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v12")
        self.assertEqual((v12_min, v12_max, v12_force_max), (2.0, 15.0, 25.0))
        v13_min, v13_max, v13_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v13")
        self.assertEqual((v13_min, v13_max, v13_force_max), (2.0, 15.0, 25.0))
        self.assertEqual(
            bridge.step5d_v12_line_guard(
                normal_load_n=0.4,
                force_norm_n=1.0,
                tcp_linear_speed_m_s=0.01,
                prior_loss_s=0.0,
                dt_s=0.002,
            ),
            (False, 0.0, "lost_contact_low_load"),
        )
        self.assertEqual(
            bridge.step5d_v12_line_guard(
                normal_load_n=5.0,
                force_norm_n=1.0,
                tcp_linear_speed_m_s=0.051,
                prior_loss_s=0.0,
                dt_s=0.002,
            ),
            (False, 0.0, "tcp_speed_watchdog"),
        )
        ok, loss_s, reason = bridge.step5d_v12_line_guard(
            normal_load_n=16.0,
            force_norm_n=16.0,
            tcp_linear_speed_m_s=0.01,
            prior_loss_s=0.028,
            dt_s=0.100,
        )
        self.assertFalse(ok)
        self.assertAlmostEqual(loss_s, 0.038)
        self.assertEqual(reason, "contact_window_timeout")
        qdot_limited, qdot_active = bridge.limit_step5d_qdot_slew(
            [0.05, -0.05, 0.02, 0.0, 0.03, -0.03],
            None,
            dt_s=0.100,
        )
        self.assertTrue(qdot_active)
        self.assertLessEqual(max(abs(float(value)) for value in qdot_limited), 0.002 + 1e-12)
        self.assertTrue(bridge.step5d_contact_window_ready(normal_load_n=8.0, force_norm_n=24.9, min_normal_load_n=v8_min, max_normal_load_n=v8_max, max_force_norm_n=v8_force_max))
        self.assertFalse(bridge.step5d_contact_window_ready(normal_load_n=20.0, force_norm_n=20.0, min_normal_load_n=v8_min, max_normal_load_n=v8_max, max_force_norm_n=v8_force_max))
        self.assertFalse(bridge.step5d_contact_window_ready(normal_load_n=30.0, force_norm_n=30.0, min_normal_load_n=v8_min, max_normal_load_n=v8_max, max_force_norm_n=v8_force_max))
        self.assertTrue(bridge.step5d_v8_recovery_window_ok(normal_load_n=20.0, force_norm_n=20.0))
        self.assertTrue(bridge.step5d_v8_recovery_window_ok(normal_load_n=30.0, force_norm_n=30.0))
        self.assertFalse(bridge.step5d_v8_recovery_window_ok(normal_load_n=0.23, force_norm_n=0.52))
        self.assertTrue(bridge.step5d_v9_recovery_window_ok(normal_load_n=0.23, force_norm_n=0.52))
        self.assertTrue(bridge.step5d_v9_recovery_window_ok(normal_load_n=0.0, force_norm_n=0.52))
        self.assertFalse(bridge.step5d_v8_recovery_window_ok(normal_load_n=40.1, force_norm_n=40.1))
        self.assertFalse(bridge.step5d_v9_recovery_window_ok(normal_load_n=40.1, force_norm_n=40.1))
        self.assertFalse(bridge.step5d_v8_recovery_window_ok(normal_load_n=5.0, force_norm_n=100.1))
        self.assertFalse(bridge.step5d_v9_recovery_window_ok(normal_load_n=5.0, force_norm_n=100.1))

    def test_step5d_v13_contact_safety_hold_and_stop_policy(self) -> None:
        hold = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=0.0,
            force_norm_n=0.1,
            actual_tcp_speed_m_s=0.010,
            predicted_tcp_speed_m_s=0.0,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(hold["action"], "hold_zero_qdot")
        self.assertEqual(hold["reason"], "hard_lost_contact_hold")

        soft_hold = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=0.30,
            force_norm_n=0.4,
            actual_tcp_speed_m_s=0.010,
            predicted_tcp_speed_m_s=0.0,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(soft_hold["reason"], "soft_low_contact_hold")

        low_load_speed_first = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=0.8,
            force_norm_n=0.9,
            actual_tcp_speed_m_s=0.026,
            predicted_tcp_speed_m_s=0.0,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(low_load_speed_first["action"], "pass_solver")
        self.assertAlmostEqual(float(low_load_speed_first["actual_speed_violation_s"]), 0.002)

        low_load_speed_second = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=0.8,
            force_norm_n=0.9,
            actual_tcp_speed_m_s=0.026,
            predicted_tcp_speed_m_s=0.0,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=float(low_load_speed_first["actual_speed_violation_s"]),
            dt_s=0.002,
        )
        self.assertEqual(low_load_speed_second["action"], "stop_zero_qdot")
        self.assertEqual(low_load_speed_second["reason"], "low_load_actual_tcp_speed_watchdog_dwell")

        actual_spike = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=5.0,
            force_norm_n=5.0,
            actual_tcp_speed_m_s=0.051,
            predicted_tcp_speed_m_s=0.0,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(actual_spike["action"], "pass_solver")

        predicted = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=5.0,
            force_norm_n=5.0,
            actual_tcp_speed_m_s=0.001,
            predicted_tcp_speed_m_s=0.051,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(predicted["action"], "stop_zero_qdot")
        self.assertEqual(predicted["reason"], "predicted_tcp_speed_watchdog")

        timeout = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=0.0,
            force_norm_n=0.1,
            actual_tcp_speed_m_s=0.001,
            predicted_tcp_speed_m_s=0.0,
            prior_hold_s=0.299,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(timeout["reason"], "low_load_hold_timeout")

        high = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=16.0,
            force_norm_n=16.0,
            actual_tcp_speed_m_s=0.001,
            predicted_tcp_speed_m_s=0.0,
            prior_hold_s=0.0,
            prior_high_window_s=0.029,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(high["reason"], "high_contact_window_dwell_stop")

        recovered = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=5.0,
            force_norm_n=5.0,
            actual_tcp_speed_m_s=0.001,
            predicted_tcp_speed_m_s=0.0,
            prior_hold_s=0.2,
            prior_high_window_s=0.02,
            prior_actual_speed_violation_s=0.002,
            dt_s=0.002,
        )
        self.assertEqual(recovered["action"], "pass_solver")
        self.assertEqual(recovered["hold_s"], 0.0)
        self.assertEqual(recovered["high_window_s"], 0.0)
        self.assertEqual(recovered["actual_speed_violation_s"], 0.0)

    def test_step5d_v13_replays_v11_low_load_as_hold_then_speed_stop(self) -> None:
        with escape_replay.DEFAULT_CSV.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        escape_replay.annotate_relative_times(rows)
        stage25 = escape_replay.stage_rows(rows, 25.0)
        first_low = next(
            row for row in stage25
            if escape_replay.finite_float(row, "_step4e_normal_load_n") < bridge.STEP5D_V13_SOFT_LOW_LOAD_N
        )
        hold = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=escape_replay.finite_float(first_low, "_step4e_normal_load_n"),
            force_norm_n=escape_replay.finite_float(first_low, "force_norm_n"),
            actual_tcp_speed_m_s=escape_replay.linear_speed(first_low),
            predicted_tcp_speed_m_s=0.0,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(hold["action"], "hold_zero_qdot")
        self.assertEqual(hold["reason"], "hard_lost_contact_hold")

        hold_s = float(hold["hold_s"])
        actual_speed_violation_s = 0.0
        stop = None
        for row in stage25:
            result = bridge.step5d_v13_contact_safety_guard(
                normal_load_n=escape_replay.finite_float(row, "_step4e_normal_load_n"),
                force_norm_n=escape_replay.finite_float(row, "force_norm_n"),
                actual_tcp_speed_m_s=escape_replay.linear_speed(row),
                predicted_tcp_speed_m_s=0.0,
                prior_hold_s=hold_s,
                prior_high_window_s=0.0,
                prior_actual_speed_violation_s=actual_speed_violation_s,
                dt_s=0.002,
            )
            hold_s = float(result["hold_s"])
            actual_speed_violation_s = float(result["actual_speed_violation_s"])
            if result["action"] == "stop_zero_qdot":
                stop = result
                break
        self.assertIsNotNone(stop)
        self.assertEqual(stop["action"], "stop_zero_qdot")
        self.assertEqual(stop["reason"], "low_load_actual_tcp_speed_watchdog_dwell")

    def test_step5d_force_frame_load_uses_reaction_normal_dot_product(self) -> None:
        force_base = (0.0, 3.0, 4.0)
        reaction_normal = (0.0, 0.6, 0.8)
        self.assertAlmostEqual(contact_semantics.signed_normal_load_n(force_base, reaction_normal), 5.0)
        self.assertNotEqual(contact_semantics.signed_normal_load_n(force_base, reaction_normal), force_base[2])
        press_cmd, _, press_v = bridge.step5d_v8_force_pid_settle_velocity(
            normal_load_n=2.0,
            target_force_n=5.0,
            integral_error_n_s=0.0,
            dt_s=0.002,
            reaction_normal_b=(0.0, 0.0, 1.0),
            normal_velocity_m_s=0.0,
            kp_m_s_per_n=0.0007,
            ki_m_s_per_n_s=0.00008,
            damping=0.35,
            integral_limit_n_s=10.0,
            v_press_max_m_s=0.003,
            v_unload_max_m_s=0.003,
        )
        self.assertGreater(press_v, 0.0)
        self.assertLess(press_cmd[2], 0.0)
        unload_cmd, _, unload_v = bridge.step5d_v8_force_pid_settle_velocity(
            normal_load_n=20.0,
            target_force_n=5.0,
            integral_error_n_s=0.0,
            dt_s=0.002,
            reaction_normal_b=(0.0, 0.0, 1.0),
            normal_velocity_m_s=0.0,
            kp_m_s_per_n=0.0007,
            ki_m_s_per_n_s=0.00008,
            damping=0.35,
            integral_limit_n_s=10.0,
            v_press_max_m_s=0.003,
            v_unload_max_m_s=0.003,
        )
        self.assertLess(unload_v, 0.0)
        self.assertGreater(unload_cmd[2], 0.0)
        v10_press_cmd, v10_filtered, v10_press_v = bridge.step5d_v10_admittance_settle_velocity(
            normal_load_n=2.0,
            filtered_normal_load_n=None,
            target_force_n=5.0,
            settle_velocity_m_s=0.0,
            dt_s=0.002,
            reaction_normal_b=(0.0, 0.0, 1.0),
            v_max_m_s=0.003,
        )
        self.assertEqual(v10_filtered, 2.0)
        self.assertGreater(v10_press_v, 0.0)
        self.assertLess(v10_press_cmd[2], 0.0)
        _, _, v10_unload_v = bridge.step5d_v10_admittance_settle_velocity(
            normal_load_n=20.0,
            filtered_normal_load_n=20.0,
            target_force_n=5.0,
            settle_velocity_m_s=0.003,
            dt_s=0.002,
            reaction_normal_b=(0.0, 0.0, 1.0),
            v_max_m_s=0.003,
        )
        self.assertGreater(v10_unload_v, -0.003)
        self.assertLess(v10_unload_v, 0.003)
        v11_press_cmd, v11_filtered, v11_press_v = bridge.step5d_v11_deadband_acquire_velocity(
            normal_load_n=1.0,
            filtered_normal_load_n=None,
            settle_velocity_m_s=0.0,
            dt_s=0.010,
            reaction_normal_b=(0.0, 0.0, 1.0),
        )
        self.assertEqual(v11_filtered, 1.0)
        self.assertAlmostEqual(v11_press_v, 0.00012, places=12)
        self.assertLess(v11_press_cmd[2], 0.0)
        v11_hold_cmd, _, v11_hold_v = bridge.step5d_v11_deadband_acquire_velocity(
            normal_load_n=5.0,
            filtered_normal_load_n=5.0,
            settle_velocity_m_s=0.0005,
            dt_s=0.010,
            reaction_normal_b=(0.0, 0.0, 1.0),
        )
        self.assertAlmostEqual(v11_hold_v, 0.00038, places=12)
        self.assertLess(v11_hold_cmd[2], 0.0)
        v11_unload_cmd, _, v11_unload_v = bridge.step5d_v11_deadband_acquire_velocity(
            normal_load_n=20.0,
            filtered_normal_load_n=20.0,
            settle_velocity_m_s=0.0,
            dt_s=0.010,
            reaction_normal_b=(0.0, 0.0, 1.0),
        )
        self.assertAlmostEqual(v11_unload_v, -0.00012, places=12)
        self.assertGreater(v11_unload_cmd[2], 0.0)

        v6_min, v6_max, v6_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v6")
        self.assertEqual((v6_min, v6_max, v6_force_max), (2.0, 40.0, 45.0))

    def test_step5d_v9_replays_v8_low_load_dropout_as_recoverable(self) -> None:
        csv_path = (
            ROOT
            / "runs"
            / "bridge_step5d_strict_rnn_liveprep_v8_20260615_183351"
            / "bridge_rtde_500hz.csv"
        )
        with csv_path.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        dropout_rows = []
        for row in rows:
            stage_value = row["ur_output_double_register_35"]
            if not stage_value or abs(float(stage_value) - 25.3) >= 0.05:
                continue
            normal_load = float(row["_step4e_normal_load_n"])
            force_norm = float(row["force_norm_n"])
            if normal_load < 0.5:
                dropout_rows.append((normal_load, force_norm))

        self.assertTrue(dropout_rows)
        for normal_load, force_norm in dropout_rows:
            self.assertFalse(bridge.step5d_v8_recovery_window_ok(normal_load_n=normal_load, force_norm_n=force_norm))
            self.assertTrue(bridge.step5d_v9_recovery_window_ok(normal_load_n=normal_load, force_norm_n=force_norm))

    def test_step5d_v4_contact_window_replays_v3_25_3_data(self) -> None:
        csv_path = (
            ROOT
            / "runs"
            / "bridge_step5d_strict_rnn_liveprep_v3_20260614_233207"
            / "bridge_rtde_500hz.csv"
        )
        with csv_path.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        best_v3_rows = 0
        best_v4_rows = 0
        current_v3_rows = 0
        current_v4_rows = 0
        for row in rows:
            stage_value = row["ur_output_double_register_35"]
            if not stage_value or abs(float(stage_value) - 25.3) >= 0.05:
                continue
            normal_load = float(row["_step4e_normal_load_n"])
            force_norm = float(row["force_norm_n"])
            v3_ready = bridge.step5d_force_settle_ready(
                normal_load_n=normal_load,
                force_norm_n=force_norm,
                target_force_n=5.0,
            )
            v4_ready = bridge.step5d_contact_window_ready(normal_load_n=normal_load, force_norm_n=force_norm)
            current_v3_rows = current_v3_rows + 1 if v3_ready else 0
            current_v4_rows = current_v4_rows + 1 if v4_ready else 0
            best_v3_rows = max(best_v3_rows, current_v3_rows)
            best_v4_rows = max(best_v4_rows, current_v4_rows)

        self.assertEqual(best_v3_rows, 0)
        self.assertGreaterEqual(best_v4_rows, 25)  # 50 ms at 500 Hz.

    def test_step5d_v6_contact_window_replays_v5_25_0_blocked_rows(self) -> None:
        csv_path = (
            ROOT
            / "runs"
            / "bridge_step5d_strict_rnn_liveprep_v5_20260615_171934"
            / "bridge_rtde_500hz.csv"
        )
        with csv_path.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        v6_min, v6_max, v6_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v6")
        stage25_rows = []
        for row in rows:
            stage_value = row["ur_output_double_register_35"]
            if not stage_value or abs(float(stage_value) - 25.0) >= 0.05:
                continue
            normal_load = float(row["_step4e_normal_load_n"])
            force_norm = float(row["force_norm_n"])
            stage25_rows.append((normal_load, force_norm))
            self.assertFalse(bridge.step5d_contact_window_ready(normal_load_n=normal_load, force_norm_n=force_norm))
            self.assertTrue(
                bridge.step5d_contact_window_ready(
                    normal_load_n=normal_load,
                    force_norm_n=force_norm,
                    min_normal_load_n=v6_min,
                    max_normal_load_n=v6_max,
                    max_force_norm_n=v6_force_max,
                )
            )

        self.assertEqual(len(stage25_rows), 53)
        self.assertGreater(min(load for load, _ in stage25_rows), 16.0)
        self.assertLess(max(load for load, _ in stage25_rows), 22.0)

    def test_step5d_v7_failure_contrast_replays_v6_second_contact_overpressure(self) -> None:
        csv_path = (
            ROOT
            / "runs"
            / "bridge_step5d_strict_rnn_liveprep_v6_20260615_173634"
            / "bridge_rtde_500hz.csv"
        )
        with csv_path.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))

        stage24_3 = [row for row in rows if row["ur_output_double_register_35"] and abs(float(row["ur_output_double_register_35"]) - 24.3) < 0.05]
        stage24_4 = [row for row in rows if row["ur_output_double_register_35"] and abs(float(row["ur_output_double_register_35"]) - 24.4) < 0.05]
        stage25 = [row for row in rows if row["ur_output_double_register_35"] and abs(float(row["ur_output_double_register_35"]) - 25.0) < 0.05]

        self.assertGreater(len(stage24_3), 1000)
        self.assertEqual(len(stage24_4), 0)
        z0 = float(stage24_3[0]["ur_actual_TCP_pose_2"])
        z_min = min(float(row["ur_actual_TCP_pose_2"]) for row in stage24_3)
        self.assertLess(z0 - z_min, 0.021)
        self.assertGreater(float(stage24_3[-1]["_step4e_normal_load_n"]), 19.0)
        self.assertGreater(float(stage25[0]["_step4e_normal_load_n"]), 22.0)
        self.assertEqual(float(rows[-1]["ur_output_double_register_30"]), 12.0)

    def test_full_chain_sanity_has_no_dls_or_live_side_effect_path(self) -> None:
        source = inspect.getsource(sanity).lower()
        forbidden = ["dls", "force_mode", "speedj(", "speedl(", "dashboard_exchange"]
        for token in forbidden:
            self.assertNotIn(token, source)
        self.assertIn("no bridge start", source)
        self.assertIn("no controller upload", source)


if __name__ == "__main__":
    unittest.main()
