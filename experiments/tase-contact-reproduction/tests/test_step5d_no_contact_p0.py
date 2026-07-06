#!/usr/bin/env python3
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_no_contact_p0 as p0  # noqa: E402
import build_step5d_liveprep as liveprep  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
import step5_table  # noqa: E402
import step5d_runtime_interface as iface  # noqa: E402


P0_FIELDS = [
    "t_monotonic_s",
    "ur_output_double_register_35",
    "_step4e_normal_load_n",
    "force_norm_n",
    "_step5d_stage25_control_mode",
    "_step5d_stage25_echo_consumed",
    "_step5d_intervention_reason",
    "_step5d_outer_xdot_limited_approach_normal_m_s",
    "_step5d_jqdot_raw_approach_normal_m_s",
    "_step5d_jqdot_cmd_approach_normal_m_s",
    "_step5d_constraint_residual_norm",
    "_step5d_lambda_norm",
    "_step5d_active_bounds_count",
]


def write_p0_run(run_dir: Path, rows: list[dict[str, str]]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "bridge_rtde_500hz.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=P0_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def good_rows() -> list[dict[str, str]]:
    rows = []
    for idx in range(4):
        rows.append(
            {
                "t_monotonic_s": f"{1.0 + 0.002 * idx:.6f}",
                "ur_output_double_register_35": "25.0",
                "_step4e_normal_load_n": "0.300000",
                "force_norm_n": "0.800000",
                "_step5d_stage25_control_mode": "speedj_rnn_live",
                "_step5d_stage25_echo_consumed": "1",
                "_step5d_intervention_reason": "solver_warm_start" if idx == 0 else "none",
                "_step5d_outer_xdot_limited_approach_normal_m_s": "0.000100000",
                "_step5d_jqdot_raw_approach_normal_m_s": "0.000095000",
                "_step5d_jqdot_cmd_approach_normal_m_s": "0.000094000",
                "_step5d_constraint_residual_norm": "0.000020000",
                "_step5d_lambda_norm": "0.012000000",
                "_step5d_active_bounds_count": "0",
            }
        )
    return rows


class Step5dNoContactP0Test(unittest.TestCase):
    def test_no_contact_p0_package_enters_stage25_without_contact_acquire(self) -> None:
        spec = liveprep.spec_for(iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        stamp = "2026-07-06T2100HKT_STEP5D_STRICT_RNN_NO_CONTACT_P0_V1"
        script = liveprep.build_script(
            stamp,
            "2026-07-06T21:00:00+08:00",
            liveprep.line_cfg(liveprep.load_json(liveprep.CONFIG_PATH)),
            liveprep.load_safe_frame(spec),
            spec,
        )
        txt = liveprep.build_txt(stamp, spec)
        urp = liveprep.build_urp(script, spec.program_name, spec.controller_dir)

        liveprep.validate_package(script, txt, urp, stamp, spec)
        bad_xml = gzip.decompress(urp).decode("utf-8").replace(
            f"\n\ncodex_{spec.program_name}()\n</cachedContents>",
            "\n</cachedContents>",
        )
        with self.assertRaisesRegex(RuntimeError, "cachedContents"):
            liveprep.validate_package(script, txt, gzip.compress(bad_xml.encode("utf-8")), stamp, spec)
        self.assertIn("NO_CONTACT_P0_CAPTURE", script + txt)
        self.assertIn("write_output_float_register(35, 25.95)", script)
        self.assertIn("write_output_float_register(35, 25.0)", script)
        self.assertIn("local joint_layout_code = 524.000", script)
        self.assertIn("speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]", script)
        self.assertIn("codex_wait_for_bridge_ready(60.0)", script)
        self.assertNotIn("codex_wait_for_sensor_ok(60.0)", script)
        self.assertIn("local stage25_runtime_limit_s = 65.000", script)
        self.assertNotIn("write_output_float_register(35, 24.0)", script)
        self.assertNotIn("write_output_float_register(35, 25.3)", script)
        self.assertNotIn("codex_step5d_down_search", script)
        self.assertNotIn("deadband contact acquire", txt)
        self.assertEqual(spec.controller_dir, "/programs/andyl/kunwei/step5")

    def test_no_contact_p0_runtime_interface_defaults_follow_step5d_full_window(self) -> None:
        runtime = iface.resolve_runtime_interface(program=iface.STEP5D_NO_CONTACT_P0_STAGE_ID, root=ROOT, env={})

        self.assertEqual(runtime.program, iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        self.assertEqual(runtime.stage25_control_mode, "speedj_rnn_live")
        self.assertEqual(runtime.bridge_defaults.duration_s, 180.0)
        self.assertEqual(runtime.hard_contract["stage25_success_target_s"], 60.0)
        self.assertEqual(runtime.hard_contract["stage25_runtime_limit_s"], 65.0)
        self.assertEqual(runtime.bridge_defaults.max_normal_force_n, 2.0)
        self.assertEqual(runtime.bridge_defaults.max_force_norm_n, 5.0)
        self.assertEqual(runtime.bridge_defaults.max_torque_norm_nm, 3.0)
        self.assertTrue(runtime.hard_contract["no_contact_p0_capture"])
        self.assertIn("no-contact", runtime.register_contract["stage25_0"])

    def test_no_contact_p0_profile_is_step5_table_reference(self) -> None:
        stage = step5_table.step5_stage(iface.STEP5D_NO_CONTACT_P0_STAGE_ID)

        self.assertEqual(stage["id"], iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        self.assertEqual(stage["shape"], "cycloid")
        self.assertFalse(stage["contact"])
        self.assertTrue(stage["bridge"])
        self.assertTrue(stage["strict_rnn"])
        self.assertEqual(stage["duration_s"], 60.0)
        self.assertEqual(stage["guard"]["stage25_success_target_s"], 60.0)
        self.assertEqual(stage["guard"]["stage25_runtime_limit_s"], 65.0)
        self.assertEqual(stage["runtime_interface_ref"]["stage25_success_target_s"], 60.0)
        self.assertEqual(stage["runtime_interface_ref"]["stage25_runtime_limit_s"], 65.0)

        ref = step5_table.step5_path_reference(
            iface.STEP5D_NO_CONTACT_P0_STAGE_ID,
            pose_xy=(0.0, 0.0),
            elapsed_s=0.0,
        )
        self.assertEqual(ref["stage_id"], iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        self.assertEqual(ref["path_time_s"], 0.0)
        self.assertIn("desired_velocity_xy", ref)

    def test_no_contact_p0_runtime_interface_ignores_ambient_live_caps(self) -> None:
        runtime = iface.resolve_runtime_interface(
            program=iface.STEP5D_NO_CONTACT_P0_STAGE_ID,
            root=ROOT,
            env={
                "BRIDGE_DURATION_S": "180",
                "BRIDGE_TARGET_FORCE_N": "12",
                "BRIDGE_BASELINE_S": "5",
                "BRIDGE_REZERO_S": "1",
                "BRIDGE_FORCE_P_GAIN": "0.9",
                "BRIDGE_FORCE_I_GAIN": "0.8",
                "BRIDGE_FORCE_DAMPING": "0.7",
                "BRIDGE_INTEGRAL_LIMIT_N_S": "0.6",
                "BRIDGE_NORMAL_FILTER_ALPHA": "0.99",
                "BRIDGE_RTDE_HZ": "125",
                "BRIDGE_SENSOR_STALE_S": "9",
                "BRIDGE_SOCKET_TIMEOUT_S": "8",
                "MAX_NORMAL_FORCE_N": "50",
                "MAX_FORCE_NORM_N": "60",
                "MAX_TORQUE_NORM_NM": "4",
                "BRIDGE_MOTION_LIMIT_M_S": "0.5",
                "BRIDGE_TOTAL_LINEAR_LIMIT_M_S": "0.006",
                "BRIDGE_NORMAL_VELOCITY_LIMIT_M_S": "0.5",
                "BRIDGE_ANGULAR_LIMIT_RAD_S": "1.5",
                "BRIDGE_NORMAL_MIN_FORCE_N": "2",
                "STEP5D_STAGE25_CONTROL_MODE": "speedl_cartesian_oracle",
                "STEP5D_PRELOAD_FILTERED_MIN_N": "9",
                "STEP5D_PRELOAD_FILTERED_MAX_N": "99",
                "STEP5D_PRELOAD_RAW_MIN_N": "8",
                "STEP5D_PRELOAD_RAW_MAX_N": "88",
                "STEP5D_PRELOAD_FORCE_NORM_MAX_N": "77",
                "STEP5D_PRELOAD_HOLD_S": "6",
                "STEP5D_PRELOAD_TIMEOUT_S": "9",
            },
        )

        self.assertEqual(runtime.preload_gate.filtered_min_n, 0.0)
        self.assertEqual(runtime.preload_gate.filtered_max_n, 2.0)
        self.assertEqual(runtime.preload_gate.raw_min_n, 0.0)
        self.assertEqual(runtime.preload_gate.raw_max_n, 2.0)
        self.assertEqual(runtime.preload_gate.force_norm_max_n, 5.0)
        self.assertEqual(runtime.preload_gate.hold_s, 0.0)
        self.assertEqual(runtime.preload_gate.timeout_s, 1.0)
        self.assertEqual(runtime.bridge_defaults.duration_s, 180.0)
        self.assertEqual(runtime.bridge_defaults.target_force_n, 1.0)
        self.assertEqual(runtime.bridge_defaults.baseline_s, 1.0)
        self.assertEqual(runtime.bridge_defaults.rezero_s, 0.25)
        self.assertEqual(runtime.bridge_defaults.force_p_gain, 0.001)
        self.assertEqual(runtime.bridge_defaults.force_i_gain, 0.00001)
        self.assertEqual(runtime.bridge_defaults.force_damping, 7.0)
        self.assertEqual(runtime.bridge_defaults.integral_limit_n_s, 1.0)
        self.assertEqual(runtime.bridge_defaults.normal_filter_alpha, 0.55)
        self.assertEqual(runtime.bridge_defaults.rtde_hz, 500.0)
        self.assertEqual(runtime.bridge_defaults.sensor_stale_s, 0.10)
        self.assertEqual(runtime.bridge_defaults.socket_timeout_s, 0.0)
        self.assertEqual(runtime.bridge_defaults.max_normal_force_n, 2.0)
        self.assertEqual(runtime.bridge_defaults.max_force_norm_n, 5.0)
        self.assertEqual(runtime.bridge_defaults.max_torque_norm_nm, 3.0)
        self.assertEqual(runtime.bridge_defaults.normal_velocity_limit_m_s, 0.003)
        self.assertEqual(runtime.bridge_defaults.total_linear_limit_m_s, 0.004)
        self.assertEqual(runtime.bridge_defaults.angular_limit_rad_s, 0.015)
        self.assertEqual(runtime.bridge_defaults.normal_min_force_n, 0.001)

    def test_no_contact_p0_bridge_parse_args_uses_full_window_no_contact_defaults(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                iface.STEP5D_NO_CONTACT_P0_STAGE_ID,
                "--bridge-path-shape",
                "cycloid",
                "--rtde-hz",
                "125",
                "--sensor-stale-s",
                "9",
                "--socket-timeout-s",
                "8",
                "--bridge-force-p-gain",
                "0.9",
                "--bridge-force-i-gain",
                "0.8",
                "--bridge-force-damping",
                "0.7",
                "--bridge-integral-limit-n-s",
                "0.6",
                "--bridge-motion-limit-m-s",
                "0.5",
                "--bridge-total-linear-limit-m-s",
                "0.5",
                "--bridge-normal-velocity-limit-m-s",
                "0.5",
                "--bridge-normal-filter-alpha",
                "0.99",
                "--bridge-angular-limit-rad-s",
                "1.5",
            ]
        )

        self.assertEqual(args.bridge_profile, iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        self.assertEqual(args.step5d_stage25_control_mode, "speedj_rnn_live")
        self.assertEqual(args.duration_s, 180.0)
        self.assertEqual(args.baseline_s, 1.0)
        self.assertEqual(args.rezero_s, 0.25)
        self.assertEqual(args.target_force_n, 1.0)
        self.assertEqual(args.rtde_hz, 500.0)
        self.assertEqual(args.sensor_stale_s, 0.10)
        self.assertEqual(args.socket_timeout_s, 0.0)
        self.assertEqual(args.bridge_force_p_gain, 0.001)
        self.assertEqual(args.step4e_force_p_gain, 0.001)
        self.assertEqual(args.bridge_force_i_gain, 0.00001)
        self.assertEqual(args.step4e_force_i_gain, 0.00001)
        self.assertEqual(args.bridge_force_damping, 7.0)
        self.assertEqual(args.step4e_force_damping, 7.0)
        self.assertEqual(args.bridge_integral_limit_n_s, 1.0)
        self.assertEqual(args.step4e_integral_limit_n_s, 1.0)
        self.assertEqual(args.bridge_motion_limit_m_s, 0.004)
        self.assertEqual(args.step4e_motion_limit_m_s, 0.004)
        self.assertEqual(args.bridge_total_linear_limit_m_s, 0.004)
        self.assertEqual(args.step4e_total_linear_limit_m_s, 0.004)
        self.assertEqual(args.bridge_normal_velocity_limit_m_s, 0.003)
        self.assertEqual(args.step4e_normal_velocity_limit_m_s, 0.003)
        self.assertEqual(args.bridge_normal_filter_alpha, 0.55)
        self.assertEqual(args.step4e_normal_filter_alpha, 0.55)
        self.assertEqual(args.bridge_angular_limit_rad_s, 0.015)
        self.assertEqual(args.step4e_angular_limit_rad_s, 0.015)
        self.assertEqual(args.bridge_normal_min_force_n, 0.001)
        self.assertTrue(args.bridge_integrate_stage25_only)
        self.assertEqual(args.step5d_preload_timeout_s, 1.0)
        self.assertEqual(args.step5d_preload_hold_s, 0.0)
        self.assertEqual(args.max_normal_force_n, 2.0)
        self.assertEqual(args.max_force_norm_n, 5.0)
        self.assertEqual(args.max_torque_norm_nm, 3.0)

    def test_worktree_no_contact_p0_triplet_matches_current_stage_metadata(self) -> None:
        stem = ROOT / "programs" / "step5" / "step5d" / iface.STEP5D_NO_CONTACT_P0_STAGE_ID
        files = {ext: stem.with_suffix(ext) for ext in (".script", ".txt", ".urp")}
        current = json.loads((ROOT / "config" / "current_stage.json").read_text(encoding="utf-8"))
        capture = current["bridge_trigger"]["no_contact_p0_capture"]

        self.assertFalse(capture["capture_authorized"])
        self.assertFalse(capture["passed"])
        self.assertEqual(capture["local_triplet"], "programs/step5/step5d/step5d_strict_rnn_no_contact_p0_v1")
        self.assertEqual(
            capture["controller_target"],
            "/programs/andyl/kunwei/step5/step5d_strict_rnn_no_contact_p0_v1.urp",
        )
        for ext, path in files.items():
            self.assertTrue(path.exists(), path)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), capture["sha256"][ext])

        spec = liveprep.spec_for(iface.STEP5D_NO_CONTACT_P0_STAGE_ID)
        liveprep.validate_package(
            files[".script"].read_text(encoding="utf-8"),
            files[".txt"].read_text(encoding="utf-8"),
            files[".urp"].read_bytes(),
            "STEP5D_STRICT_RNN_NO_CONTACT_P0_V1",
            spec,
        )

    def test_passes_when_first_speedj_rnn_tick_has_warm_start_and_same_normal_sign(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, good_rows())

            result = p0.verify_run_dir(run_dir)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["first_tick"]["intervention_reason"], "solver_warm_start")
        self.assertGreater(result["first_tick"]["jqdot_raw_approach_normal_m_s"], 0.0)
        self.assertEqual(result["blockers"], [])

    def test_fails_without_solver_warm_start_on_first_speedj_rnn_tick(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_intervention_reason"] = "none"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_solver_warm_start", result["blockers"])

    def test_fails_when_raw_jqdot_unloads_while_outer_presses(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_jqdot_raw_approach_normal_m_s"] = "-0.000300000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_press_unload_mismatch", result["blockers"])

    def test_fails_when_outer_command_is_not_pressing(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_outer_xdot_limited_approach_normal_m_s"] = "0.000000000"
        rows[0]["_step5d_jqdot_raw_approach_normal_m_s"] = "0.000000000"
        rows[0]["_step5d_jqdot_cmd_approach_normal_m_s"] = "0.000000000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_outer_not_pressing", result["blockers"])

    def test_fails_when_cmd_jqdot_unloads_while_outer_presses(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_jqdot_cmd_approach_normal_m_s"] = "-0.000300000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_cmd_press_unload_mismatch", result["blockers"])

    def test_fails_when_speedj_rnn_rows_are_not_stage25(self) -> None:
        rows = good_rows()
        for row in rows:
            row["ur_output_double_register_35"] = "24.0"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("no_stage25_speedj_rnn_live_rows", result["blockers"])

    def test_fails_when_any_speedj_rnn_row_is_outside_stage25(self) -> None:
        rows = good_rows()
        rows.append(good_rows()[0] | {"ur_output_double_register_35": "24.0"})
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("non_stage25_speedj_rnn_live_rows_present", result["blockers"])

    def test_fails_when_first_speedj_rnn_tick_was_not_consumed(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_stage25_echo_consumed"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_not_consumed_by_stage25", result["blockers"])

    def test_fails_when_first_speedj_rnn_tick_consumed_evidence_is_fractional(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_stage25_echo_consumed"] = "0.6"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_not_consumed_by_stage25", result["blockers"])

    def test_fails_when_run_is_not_no_contact(self) -> None:
        rows = good_rows()
        rows[0]["_step4e_normal_load_n"] = "5.100000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("normal_load_exceeds_no_contact_limit", result["blockers"])

    def test_fails_when_first_load_or_force_evidence_is_nonfinite(self) -> None:
        rows = good_rows()
        rows[0]["_step4e_normal_load_n"] = "nan"
        rows[0]["force_norm_n"] = "nan"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_normal_load_evidence", result["blockers"])
        self.assertIn("first_speedj_rnn_tick_missing_force_norm_evidence", result["blockers"])

    def test_fails_when_first_lambda_evidence_is_nonfinite(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_lambda_norm"] = "nan"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_lambda_norm", result["blockers"])

    def test_fails_when_active_bounds_evidence_is_fractional(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_active_bounds_count"] = "0.4"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            write_p0_run(run_dir, rows)

            result = p0.verify_run_dir(run_dir)

        self.assertFalse(result["ok"])
        self.assertIn("first_speedj_rnn_tick_missing_active_bounds_count", result["blockers"])

    def test_cli_writes_summary_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            summary = Path(tmp) / "p0_summary.json"
            write_p0_run(run_dir, good_rows())

            completed = subprocess.run(
                [
                    "python3",
                    str(ROOT / "tools" / "verify_step5d_no_contact_p0.py"),
                    str(run_dir),
                    "--output",
                    str(summary),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertTrue(summary.exists())
            self.assertTrue(json.loads(summary.read_text(encoding="utf-8"))["ok"])

    def test_cli_returns_24_when_artifact_fails(self) -> None:
        rows = good_rows()
        rows[0]["_step5d_jqdot_cmd_approach_normal_m_s"] = "-0.000300000"
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            write_p0_run(run_dir, rows)

            completed = subprocess.run(
                [
                    "python3",
                    str(ROOT / "tools" / "verify_step5d_no_contact_p0.py"),
                    str(run_dir),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 24, completed.stdout + completed.stderr)
            self.assertIn("first_speedj_rnn_tick_cmd_press_unload_mismatch", completed.stdout)

    def test_operator_script_exposes_capture_bridge_without_contact_bridge(self) -> None:
        script = (ROOT / "scripts" / "step5d-strict-rnn-p0.sh").read_text(encoding="utf-8")

        self.assertIn("validate-run", script)
        self.assertIn("live-ready", script)
        self.assertIn("capture-ready", script)
        self.assertIn("capture-bridge", script)
        self.assertIn("LIVE STEP5D STRICT RNN NO CONTACT P0", script)
        self.assertIn("step5d_strict_rnn_no_contact_p0_v1", script)
        self.assertIn("BRIDGE_ALLOW_NO_CONTACT_P0_CAPTURE=1", script)
        self.assertIn("verify_step5d_no_contact_p0.py", script)
        self.assertIn("step5d_no_contact_p0_summary", script)
        self.assertIn("BRIDGE_DURATION_S=180", script)
        self.assertNotIn("contact-bridge", script)

    def test_capture_bridge_refuses_without_p0_confirm_before_starting_bridge(self) -> None:
        completed = subprocess.run(
            ["bash", str(ROOT / "scripts" / "step5d-strict-rnn-p0.sh"), "capture-bridge"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 40, completed.stdout + completed.stderr)
        self.assertIn("STEP5D_P0_CONFIRM", completed.stdout + completed.stderr)
        self.assertNotIn("bridge output:", completed.stdout)

    def test_capture_bridge_hands_off_hardened_p0_env_and_validates_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = Path(tmp)
            scripts_dir = sandbox / "scripts"
            tools_dir = sandbox / "tools"
            run_dir = sandbox / "run"
            scripts_dir.mkdir()
            tools_dir.mkdir()
            run_dir.mkdir()
            wrapper = scripts_dir / "step5d-strict-rnn-p0.sh"
            wrapper.write_text((ROOT / "scripts" / "step5d-strict-rnn-p0.sh").read_text(encoding="utf-8"), encoding="utf-8")
            wrapper.chmod(0o755)
            bridge_spy = scripts_dir / "bridge-line-operator.sh"
            bridge_spy.write_text(
                f"""#!/usr/bin/env bash
set -euo pipefail
env >"{sandbox / 'bridge_env.txt'}"
printf '%s\\n' "$@" >"{sandbox / 'bridge_argv.txt'}"
echo "[operator] bridge output: {run_dir}"
""",
                encoding="utf-8",
            )
            bridge_spy.chmod(0o755)
            verifier_spy = tools_dir / "verify_step5d_no_contact_p0.py"
            verifier_spy.write_text(
                f"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path
Path({str(sandbox / 'verifier_argv.txt')!r}).write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
out = Path(sys.argv[sys.argv.index("--output") + 1])
out.write_text(json.dumps({{"ok": True}}), encoding="utf-8")
""",
                encoding="utf-8",
            )
            verifier_spy.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "STEP5D_P0_CONFIRM": "LIVE STEP5D STRICT RNN NO CONTACT P0",
                    "MAX_NORMAL_FORCE_N": "50",
                    "MAX_FORCE_NORM_N": "60",
                    "MAX_TORQUE_NORM_NM": "4",
                    "BRIDGE_DURATION_S": "180",
                    "BRIDGE_TARGET_FORCE_N": "12",
                    "STEP5D_STAGE25_CONTROL_MODE": "speedl_cartesian_oracle",
                    "STEP5D_PRELOAD_FILTERED_MAX_N": "99",
                }
            )

            completed = subprocess.run(
                ["bash", str(wrapper), "capture-bridge"],
                cwd=sandbox,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual((sandbox / "bridge_argv.txt").read_text(encoding="utf-8").strip(), "line-autowatch")
            bridge_env = dict(
                line.split("=", 1)
                for line in (sandbox / "bridge_env.txt").read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            self.assertEqual(bridge_env["BRIDGE_PROFILE"], "step5d_strict_rnn_no_contact_p0_v1")
            self.assertEqual(bridge_env["BRIDGE_ALLOW_NO_CONTACT_P0_CAPTURE"], "1")
            self.assertEqual(bridge_env["BRIDGE_DURATION_S"], "180")
            self.assertEqual(bridge_env["BRIDGE_RTDE_HZ"], "500")
            self.assertEqual(bridge_env["BRIDGE_SENSOR_STALE_S"], "0.10")
            self.assertEqual(bridge_env["BRIDGE_SOCKET_TIMEOUT_S"], "0.0")
            self.assertEqual(bridge_env["BRIDGE_TARGET_FORCE_N"], "1.0")
            self.assertEqual(bridge_env["BRIDGE_FORCE_P_GAIN"], "0.001")
            self.assertEqual(bridge_env["BRIDGE_FORCE_I_GAIN"], "0.00001")
            self.assertEqual(bridge_env["BRIDGE_FORCE_DAMPING"], "7.0")
            self.assertEqual(bridge_env["BRIDGE_INTEGRAL_LIMIT_N_S"], "1.0")
            self.assertEqual(bridge_env["MAX_NORMAL_FORCE_N"], "2")
            self.assertEqual(bridge_env["MAX_FORCE_NORM_N"], "5")
            self.assertEqual(bridge_env["MAX_TORQUE_NORM_NM"], "3.0")
            self.assertEqual(bridge_env["BRIDGE_NORMAL_MIN_FORCE_N"], "0.001")
            self.assertEqual(bridge_env["BRIDGE_NORMAL_FILTER_ALPHA"], "0.55")
            self.assertEqual(bridge_env["BRIDGE_MOTION_LIMIT_M_S"], "0.004")
            self.assertEqual(bridge_env["BRIDGE_NORMAL_VELOCITY_LIMIT_M_S"], "0.003")
            self.assertEqual(bridge_env["STEP5D_STAGE25_CONTROL_MODE"], "speedj_rnn_live")
            self.assertEqual(bridge_env["STEP5D_PRELOAD_FILTERED_MAX_N"], "2.0")
            verifier_args = (sandbox / "verifier_argv.txt").read_text(encoding="utf-8").splitlines()
            self.assertEqual(verifier_args, [str(run_dir), "--output", str(run_dir / "step5d_no_contact_p0_summary.json")])
            self.assertTrue((run_dir / "step5d_no_contact_p0_summary.json").exists())
            self.assertIn("Step5d no-contact P0 summary", completed.stdout)

    def test_capture_ready_hardens_no_contact_caps_against_ambient_env(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "BRIDGE_DURATION_S": "180",
                "BRIDGE_TARGET_FORCE_N": "12",
                "BRIDGE_BASELINE_S": "5",
                "BRIDGE_REZERO_S": "1",
                "BRIDGE_FORCE_P_GAIN": "0.9",
                "BRIDGE_FORCE_I_GAIN": "0.8",
                "BRIDGE_FORCE_DAMPING": "0.7",
                "BRIDGE_INTEGRAL_LIMIT_N_S": "0.6",
                "BRIDGE_NORMAL_FILTER_ALPHA": "0.99",
                "BRIDGE_RTDE_HZ": "125",
                "BRIDGE_SENSOR_STALE_S": "9",
                "BRIDGE_SOCKET_TIMEOUT_S": "8",
                "MAX_NORMAL_FORCE_N": "50",
                "MAX_FORCE_NORM_N": "60",
                "MAX_TORQUE_NORM_NM": "4",
                "BRIDGE_MOTION_LIMIT_M_S": "0.5",
                "BRIDGE_TOTAL_LINEAR_LIMIT_M_S": "0.006",
                "BRIDGE_NORMAL_VELOCITY_LIMIT_M_S": "0.5",
                "BRIDGE_ANGULAR_LIMIT_RAD_S": "1.5",
                "BRIDGE_NORMAL_MIN_FORCE_N": "2",
                "STEP5D_PRELOAD_FILTERED_MAX_N": "99",
                "STEP5D_PRELOAD_TIMEOUT_S": "9",
                "STEP5D_STAGE25_CONTROL_MODE": "speedl_cartesian_oracle",
            }
        )

        completed = subprocess.run(
            ["bash", str(ROOT / "scripts" / "step5d-strict-rnn-p0.sh"), "capture-ready"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("[caps] total_linear=0.004m/s angular=0.015rad/s hard_force=2/5N torque=3Nm", completed.stdout)
        self.assertIn("[tuning] preload filtered=0..2N raw=0..2N force_norm<=5N hold=0s", completed.stdout)


if __name__ == "__main__":
    unittest.main()
