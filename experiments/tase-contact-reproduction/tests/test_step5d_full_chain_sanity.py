#!/usr/bin/env python3
"""Offline checks for the Step5d full-chain sanity runner."""

from __future__ import annotations

import inspect
import gzip
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import kunwei_rtde_bridge as bridge  # noqa: E402
import build_step5d_liveprep as liveprep  # noqa: E402
import contact_semantics  # noqa: E402
import step_pose_contract  # noqa: E402
import step5d_v11_escape_replay as escape_replay  # noqa: E402
import step5d_full_chain_sanity as sanity  # noqa: E402


class Step5dFullChainSanityTest(unittest.TestCase):
    def test_non_current_step5d_packages_are_archived_under_step5d_subdir(self) -> None:
        step5_dir = ROOT / "programs" / "step5"
        archive_dir = step5_dir / "step5d"
        current = json.loads((ROOT / "config" / "current_stage.json").read_text(encoding="utf-8"))
        current_program = current["program"]
        stray_non_current = [
            path.relative_to(ROOT).as_posix()
            for path in step5_dir.glob("step5d*.*")
            if path.is_file() and path.stem != current_program
        ]
        self.assertEqual(stray_non_current, [])

        archived_triplets = sorted(path.name for path in archive_dir.glob("step5d*.*"))
        self.assertIn("step5d_strict_rnn_liveprep_v24.urp", archived_triplets)
        self.assertIn("step5d_strict_rnn_ablation_v25.urp", archived_triplets)
        self.assertIn("step5d_strict_rnn_ablation_v26.urp", archived_triplets)

        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))

        def assert_archived_local_triplet(value: object) -> None:
            if not isinstance(value, str) or "step5d_strict_rnn" not in value:
                return
            if current_program in value:
                self.assertTrue(value.startswith(f"programs/step5/{current_program}"))
            else:
                self.assertTrue(value.startswith("programs/step5/step5d/"), value)

        assert_archived_local_triplet(current.get("local_triplet"))
        for key, value in current.get("evidence", {}).items():
            if key.endswith("_local_triplet"):
                assert_archived_local_triplet(value)
        for stage in table.get("stages", []):
            evidence = stage.get("local_delivery_evidence")
            if isinstance(evidence, dict):
                if evidence.get("archived_to_step5d_dir") is True:
                    assert_archived_local_triplet(evidence.get("local_triplet"))

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
        self.assertEqual(summary["assumptions"]["projection_input_form"], "J.T @ lambda_state")
        self.assertEqual(
            summary["assumptions"]["lambda_update_form"],
            "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
        )
        self.assertTrue(summary["gates"]["register_order_pass"])
        self.assertTrue(summary["gates"]["qdot_within_nominal_limit_pass"])
        self.assertTrue(summary["gates"]["strict_rnn_eq23_sign_gate_pass"])
        self.assertEqual(
            summary["strict_rnn_eq23_sign_gate"]["status"],
            "local_discrete_sign_gate_passed_current_variant",
        )
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

    def test_step5d_ablation_package_is_non_quarantine_multimode_executor(self) -> None:
        stamp = "2026-07-03T0100HKT_STEP5D_STRICT_RNN_ABLATION_V25"
        spec = liveprep.spec_for("step5d_strict_rnn_ablation_v25")
        geom = liveprep.line_cfg(liveprep.load_json(liveprep.CONFIG_PATH))
        frame = liveprep.load_safe_frame(spec)
        script = liveprep.build_script(stamp, "2026-06-14T12:00:00+08:00", geom, frame, spec)
        txt = liveprep.build_txt(stamp, spec)
        urp = liveprep.build_urp(script, spec.program_name, liveprep.CONTROLLER_DIR)
        urp_again = liveprep.build_urp(script, spec.program_name, liveprep.CONTROLLER_DIR)
        liveprep.validate_package(script, txt, urp, stamp, spec)
        self.assertEqual(urp, urp_again)
        xml = gzip.decompress(urp).decode("utf-8")
        self.assertIn(f'URProgram name="{spec.program_name}"', xml)
        self.assertIn("multimode_executor_and_guard_only", script)
        self.assertIn("local stage25_layout_tag = read_input_float_register(47)", script)
        self.assertIn("local cartesian_layout_code = 523.000", script)
        self.assertIn("local joint_layout_code = 524.000", script)
        self.assertIn("speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy, cmd_wz]", script)
        self.assertIn("speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]", script)
        self.assertIn("local cartesian_linear_cap_m_s = 0.004", script)
        self.assertIn("local cartesian_angular_cap_rad_s = 0.150", script)
        self.assertIn("local qdot_cap_rad_s = 0.050", script)
        self.assertIn("STAGE25_CONTACT_SAFETY", script)
        self.assertIn("speedl_cartesian_oracle", script + txt)
        self.assertIn("speedj_dls_oracle", txt)
        self.assertIn("speedj_rnn_live", txt)
        self.assertIn("RNN is shadow-only", txt)
        self.assertIn("cage margin exhaustion", script + txt)
        self.assertIn("PRECONTACT_POSE_CONTRACT: pre_contact_search_gravity_down_v1", script)
        self.assertIn("local target_rx = 3.141592654", script)
        self.assertIn("local target_ry = 0.000000000", script)
        self.assertIn("local target_rz = 0.000000000", script)
        self.assertIn("TCP +Z targets base -Z", script)
        self.assertIn("config/step_pose_contract_table.json", script + txt)
        self.assertIn("stop_request", script)
        self.assertNotIn("local skip_lift_attitude = 0", script)
        self.assertNotIn("write_output_float_register(35, 25.1)", script)
        self.assertNotIn("write_output_float_register(35, 25.2)", script)
        self.assertNotIn("codex_step5d_down_search(24.3, 24.4", script)
        self.assertIn("Stage 25.3 consumes 37..39 as Cartesian deadband-acquire vx/vy/vz", script)
        self.assertIn("v25 preload overrides in 40/41/42/44/46/47", script)
        self.assertIn("local line_entry_default_normal_load_min_n = 10.500", script)
        self.assertIn("local line_entry_default_normal_load_max_n = 12.800", script)
        self.assertIn("local line_entry_default_force_norm_max_n = 25.000", script)
        self.assertIn("local line_entry_default_required_s = 0.100", script)
        self.assertIn("local line_entry_param_valid_code = 521.000", script)
        self.assertIn("local candidate_min_n = read_input_float_register(40)", script)
        self.assertIn("local candidate_required_s = read_input_float_register(44)", script)
        self.assertIn("write_output_float_register(35, 25.95)", script)
        self.assertIn("local register_clear_required_s = 0.006", script)
        self.assertIn("local register_clear_zero_tol = 0.000500", script)
        self.assertNotIn("qdot_clear_cap_rad_s", script)
        self.assertNotIn("qdot_clear_required_s", script)
        self.assertIn("not (cartesian_layout_ok or joint_layout_ok)", script)
        self.assertIn("Stage 25.95 requires the bridge to clear registers 37..47", txt)
        self.assertIn("registers 37..42 near zero", txt)
        self.assertNotIn("local line_entry_settle_cmd_max_m_s", script)
        self.assertNotIn("codex_abs(cmd_vx) <= line_entry_settle_cmd_max_m_s", script)
        self.assertIn("local line_entry_recovery_normal_load_min_n = 0.000", script)
        self.assertIn("local line_entry_recovery_normal_load_max_n = 20.000", script)
        self.assertIn("local line_entry_force_norm_stop_n = 25.000", script)
        self.assertNotIn("or normal_load < line_entry_recovery_normal_load_min_n", script)
        self.assertIn("elif stop_reason == 17.0:\n    return True", script)
        self.assertIn("local normal_load = target_force - force_error", script)
        self.assertIn("codex_wait_for_fresh_heartbeat(60.0)", script)
        self.assertIn("to 60.0 s for a fresh bridge heartbeat", txt)
        self.assertIn("local line_entry_default_timeout_s = 10.000", script)
        self.assertIn(f"local line_runtime_limit_s = {liveprep.LINE_RUNTIME_LIMIT_S:.3f}", script)
        self.assertIn(f"local line_success_progress_m = {liveprep.DIAGNOSTIC_WINDOW_S:.9f}", script)
        self.assertIn("speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0]", script)
        self.assertIn("movel(entry_xy_pose, a=0.090, v=0.060, r=0.0)", script)
        self.assertIn("40.000, -0.0225, -0.0025)", script)
        self.assertIn("codex_abs(normal_force) > 25.0", script)
        self.assertIn("force_norm > 25.0", script)
        self.assertIn("torque_norm > 4.0", script)
        self.assertIn("deadband contact acquire", txt)
        self.assertIn("--target-force-n 12.0", txt)
        self.assertIn("Stage 25.0 supports Cartesian speedl layout", txt)
        self.assertIn("filtered normal_load between 10.5 N and 12.8 N", txt)
        self.assertIn("raw normal_load is sanity-checked between 9.5 N and 13.5 N", txt)
        self.assertIn("registers 40/41 are filtered", txt)
        self.assertIn("Stage 22 entry movel is 0.060 m/s", txt)
        self.assertIn("Stage 24 far search", txt)
        self.assertIn("no second contact search", txt)
        self.assertNotIn("stop_only_quarantine", script + txt)
        self.assertNotIn("STEP5D_STRICT_RNN_LIVEPREP_V24", script + txt)
        self.assertNotIn("step5d_strict_rnn_liveprep_v24", script + txt)
        self.assertNotIn("STEP5D_STRICT_RNN_LIVEPREP_V12", script + txt)
        self.assertNotIn("STEP5D_STRICT_RNN_LIVEPREP_V13", script + txt)
        self.assertNotIn("STEP5D_STRICT_RNN_LIVEPREP_V14", script + txt)
        self.assertNotIn("STEP5D_STRICT_RNN_LIVEPREP_V15", script + txt)
        self.assertNotIn("STEP5D_STRICT_RNN_LIVEPREP_V16", script + txt)
        self.assertNotIn("STEP5D_STRICT_RNN_LIVEPREP_V17", script + txt)
        self.assertNotIn("STEP5D_STRICT_RNN_LIVEPREP_V18", script + txt)
        self.assertNotIn("STEP5D_STRICT_RNN_LIVEPREP_V19", script + txt)
        self.assertNotIn("STEP5D_STRICT_RNN_LIVEPREP_V20", script + txt)
        self.assertNotIn("STEP5D_STRICT_RNN_LIVEPREP_V21", script + txt)

    def test_step5d_precontact_pose_contract_targets_gravity_down(self) -> None:
        error = step_pose_contract.validate_contract_axis()
        self.assertLess(error, 1e-6)
        rotvec = step_pose_contract.contract_target_rotvec_rad()
        self.assertEqual(rotvec, (3.141592654, 0.0, 0.0))
        tcp_z = step_pose_contract.tool_z_axis_from_rotvec(rotvec)
        self.assertAlmostEqual(tcp_z[0], 0.0, places=9)
        self.assertAlmostEqual(tcp_z[1], 0.0, places=9)
        self.assertAlmostEqual(tcp_z[2], -1.0, places=9)

    def test_step5d_write_outputs_can_reuse_existing_metadata_without_rewriting(self) -> None:
        original_dir = liveprep.LOCAL_PROGRAM_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            liveprep.LOCAL_PROGRAM_DIR = Path(tmpdir)
            try:
                first = liveprep.write_outputs(
                    "2026-07-03T0100HKT_STEP5D_STRICT_RNN_ABLATION_V25",
                    "2026-07-03T01:00:00+08:00",
                    program="step5d_strict_rnn_ablation_v25",
                )
                self.assertTrue(any(first["changed"].values()))
                second = liveprep.write_outputs(
                    reuse_existing_metadata=True,
                    program="step5d_strict_rnn_ablation_v25",
                )
                self.assertTrue(second["reused_existing_metadata"])
                self.assertFalse(any(second["changed"].values()))
                self.assertEqual(second["stamp"], first["stamp"])
                self.assertEqual(second["generated_at"], first["generated_at"])
            finally:
                liveprep.LOCAL_PROGRAM_DIR = original_dir

    def test_step5d_write_outputs_can_make_local_only_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir) / "candidate"
            result = liveprep.write_outputs(
                "2026-07-03T0100HKT_STEP5D_STRICT_RNN_ABLATION_V25",
                "2026-07-03T01:00:00+08:00",
                output_dir=out_dir,
                local_only=True,
                program="step5d_strict_rnn_ablation_v25",
            )

            marker_path = Path(result["local_candidate_marker"])
            self.assertTrue(marker_path.is_file())
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertTrue(marker["local_only"])
            self.assertTrue(marker["not_delivered"])
            self.assertEqual(marker["status"], "local package verified")
            self.assertEqual(marker["program"], "step5d_strict_rnn_ablation_v25")
            self.assertEqual(marker["target_dir"], liveprep.CONTROLLER_DIR)
            self.assertEqual(marker["semantic_fingerprint"], result["semantic_fingerprint"])
            self.assertIn("do not open on Teach Pendant", marker["safety_boundary"])
            self.assertTrue((out_dir / "step5d_strict_rnn_ablation_v25.urp").is_file())

    def test_step5d_semantic_fingerprint_ignores_source_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            first = liveprep.write_outputs(
                "2026-07-03T0100HKT_STEP5D_STRICT_RNN_ABLATION_V25",
                "2026-07-03T01:00:00+08:00",
                output_dir=Path(tmpdir) / "candidate_a",
                local_only=True,
                program="step5d_strict_rnn_ablation_v25",
            )
            second = liveprep.write_outputs(
                "2026-07-03T0115HKT_STEP5D_STRICT_RNN_ABLATION_V25",
                "2026-07-03T01:15:00+08:00",
                output_dir=Path(tmpdir) / "candidate_b",
                local_only=True,
                program="step5d_strict_rnn_ablation_v25",
            )

        self.assertEqual(first["semantic_fingerprint"], second["semantic_fingerprint"])

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
        self.assertEqual(args.bridge_profile, "step5d_strict_rnn_liveprep_v11")
        self.assertEqual(args.bridge_mode, "line")
        self.assertEqual(args.step5d_qdot_limit_rad_s, 0.30)
        self.assertFalse(args.disable_dashboard_program_watch)
        bridge_args = bridge.parse_args(
            [
                "--no-start-command",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                "step5d_strict_rnn_liveprep_v14",
                "--bridge-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(bridge_args.bridge_profile, "step5d_strict_rnn_liveprep_v14")
        self.assertEqual(bridge_args.step4e_version, "step5d_strict_rnn_liveprep_v14")
        self.assertEqual(bridge_args.bridge_path_shape, "cycloid")
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
        v14_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v14",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v14_args.step5d_qdot_limit_rad_s, 0.05)
        v15_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v15",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v15_args.step5d_qdot_limit_rad_s, 0.05)
        v15a_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v15a",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v15a_args.step5d_qdot_limit_rad_s, 0.05)
        v16_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v16",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v16_args.step5d_qdot_limit_rad_s, 0.05)
        v17_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v17",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v17_args.step5d_qdot_limit_rad_s, 0.05)
        v18_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v18",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v18_args.step5d_qdot_limit_rad_s, 0.05)
        v19_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v19",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v19_args.step5d_qdot_limit_rad_s, 0.05)
        v20_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v20",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v20_args.step5d_qdot_limit_rad_s, 0.05)
        v21_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v21",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v21_args.step5d_qdot_limit_rad_s, 0.05)
        self.assertEqual(v21_args.step5d_preload_filtered_min_n, 7.5)
        self.assertEqual(v21_args.step5d_preload_filtered_max_n, 14.0)
        self.assertEqual(v21_args.step5d_preload_raw_min_n, 7.0)
        self.assertEqual(v21_args.step5d_preload_raw_max_n, 15.0)
        v22_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v22",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v22_args.step5d_qdot_limit_rad_s, 0.05)
        self.assertEqual(v22_args.step5d_preload_filtered_min_n, 7.5)
        self.assertEqual(v22_args.step5d_preload_filtered_max_n, 14.0)
        self.assertEqual(v22_args.step5d_preload_raw_min_n, 7.0)
        self.assertEqual(v22_args.step5d_preload_raw_max_n, 15.0)
        v24_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v24",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v24_args.step5d_qdot_limit_rad_s, 0.05)
        self.assertEqual(v24_args.step5d_preload_filtered_min_n, 7.5)
        self.assertEqual(v24_args.step5d_preload_filtered_max_n, 14.0)
        self.assertEqual(v24_args.step5d_preload_raw_min_n, 7.0)
        self.assertEqual(v24_args.step5d_preload_raw_max_n, 15.0)
        v25_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_ablation_v25",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v25_args.step5d_qdot_limit_rad_s, 0.05)
        self.assertEqual(v25_args.step5d_preload_filtered_min_n, 10.5)
        self.assertEqual(v25_args.step5d_preload_filtered_max_n, 12.8)
        self.assertEqual(v25_args.step5d_preload_raw_min_n, 9.5)
        self.assertEqual(v25_args.step5d_preload_raw_max_n, 13.5)
        self.assertEqual(v25_args.step5d_stage25_control_mode, "speedl_cartesian_oracle")
        self.assertEqual(v25_args.bridge_angular_limit_rad_s, 0.150)
        v26_args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_ablation_v26",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(v26_args.step5d_qdot_limit_rad_s, 0.05)
        self.assertEqual(v26_args.step5d_preload_filtered_min_n, 7.0)
        self.assertEqual(v26_args.step5d_preload_filtered_max_n, 18.0)
        self.assertEqual(v26_args.step5d_preload_raw_min_n, 5.0)
        self.assertEqual(v26_args.step5d_preload_raw_max_n, 20.0)
        self.assertEqual(v26_args.step5d_stage25_control_mode, "speedl_cartesian_oracle")
        self.assertEqual(v26_args.bridge_angular_limit_rad_s, 0.015)
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
        bridge_operator = (ROOT / "scripts" / "bridge-line-operator.sh").read_text(encoding="utf-8")
        self.assertIn("current_step5d_version()", operator)
        self.assertIn('STEP5D_VERSION="${STEP5D_VERSION:-$(current_step5d_version)}"', operator)
        self.assertIn('BRIDGE_OPERATOR="${SCRIPT_DIR}/bridge-line-operator.sh"', operator)
        self.assertIn('READBACK_GATE="${ROOT}/tools/verify_step5d_current_binding.py"', operator)
        self.assertIn('RUNTIME_INTERFACE="${ROOT}/tools/step5d_runtime_interface.py"', operator)
        self.assertIn('Bridge profile: ${STEP5D_VERSION}', operator)
        self.assertIn('STEP5D_CONFIRM', operator)
        self.assertIn('require_current_stage_readback_gate', operator)
        self.assertIn('python3 "${READBACK_GATE}" --root "${ROOT}" --program "${STEP5D_VERSION}"', operator)
        self.assertIn('Force target defaults to 12.0 N', operator)
        self.assertIn('v24 default preload gate is filtered 7.5-14 N', operator)
        self.assertIn('raw-sanity 7-15 N', operator)
        self.assertIn('WAIT_FOR_PLAY_S="${WAIT_FOR_PLAY_S:-20}"', operator)
        self.assertIn('AUTOWATCH_WAIT_FOR_PLAY_S="${AUTOWATCH_WAIT_FOR_PLAY_S:-20}"', operator)
        self.assertIn('current v24/v25/v26 defaults to 25 N', operator)
        self.assertIn('v27 default tube is filtered 5-22 N', operator)
        self.assertIn('STEP5D_DEFAULT_MAX_NORMAL_FORCE_N="${STEP5D_DEFAULT_MAX_NORMAL_FORCE_N:-25}"', operator)
        self.assertIn('STEP5D_DEFAULT_MAX_FORCE_NORM_N="${STEP5D_DEFAULT_MAX_FORCE_NORM_N:-25}"', operator)
        self.assertIn('tase_protocol_table.py" operator-env step5d-liveprep', operator)
        self.assertIn('STEP5D_DEFAULT_MAX_NORMAL_FORCE_N="${STEP5D_DEFAULT_MAX_NORMAL_FORCE_N:-${TASE_STEP5D_MAX_NORMAL_FORCE_N}}"', operator)
        self.assertIn('STEP5D_DEFAULT_MAX_FORCE_NORM_N="${STEP5D_DEFAULT_MAX_FORCE_NORM_N:-${TASE_STEP5D_MAX_FORCE_NORM_N}}"', operator)
        self.assertIn('STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N:-10.5}"', operator)
        self.assertIn('STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N:-7.0}"', operator)
        self.assertIn('STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N:-18.0}"', operator)
        self.assertIn('STEP5D_DEFAULT_PRELOAD_RAW_MIN_N="${STEP5D_DEFAULT_PRELOAD_RAW_MIN_N:-5.0}"', operator)
        self.assertIn('STEP5D_DEFAULT_PRELOAD_RAW_MAX_N="${STEP5D_DEFAULT_PRELOAD_RAW_MAX_N:-20.0}"', operator)
        self.assertIn('STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N:-${TASE_STEP5D_PRELOAD_FILTERED_MIN_N}}"', operator)
        self.assertIn('STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N="${STEP5D_DEFAULT_PRELOAD_FILTERED_MAX_N:-${TASE_STEP5D_PRELOAD_FILTERED_MAX_N}}"', operator)
        self.assertIn('STEP5D_DEFAULT_PRELOAD_RAW_MIN_N="${STEP5D_DEFAULT_PRELOAD_RAW_MIN_N:-${TASE_STEP5D_PRELOAD_RAW_MIN_N}}"', operator)
        self.assertIn('STEP5D_DEFAULT_PRELOAD_RAW_MAX_N="${STEP5D_DEFAULT_PRELOAD_RAW_MAX_N:-${TASE_STEP5D_PRELOAD_RAW_MAX_N}}"', operator)
        self.assertIn('elif [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v26" ]]', operator)
        self.assertIn('elif [[ "${STEP5D_VERSION}" == "step5d_strict_rnn_ablation_v27" ]]', operator)
        self.assertIn('STEP5D_STAGE25_CONTROL_MODE_DEFAULT="${STEP5D_STAGE25_CONTROL_MODE_DEFAULT:-${TASE_STEP5D_STAGE25_CONTROL_MODE_DEFAULT}}"', operator)
        self.assertIn('STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S="${STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S:-0.150}"', operator)
        self.assertIn('STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S="${STEP5D_DEFAULT_ANGULAR_LIMIT_RAD_S:-${TASE_STEP5D_ANGULAR_LIMIT_RAD_S}}"', operator)
        self.assertIn('BRIDGE_ANGULAR_LIMIT_RAD_S="${BRIDGE_ANGULAR_LIMIT_RAD_S:-0.150}"', bridge_operator)
        self.assertIn('BRIDGE_ANGULAR_LIMIT_RAD_S="${BRIDGE_ANGULAR_LIMIT_RAD_S:-0.015}"', bridge_operator)
        self.assertIn('STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-0.150}"', base)
        self.assertIn('STEP4E_ANGULAR_LIMIT_RAD_S="${STEP4E_ANGULAR_LIMIT_RAD_S:-0.015}"', base)
        self.assertIn(
            'MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-${STEP5D_MAX_NORMAL_FORCE_N:-${STEP5D_DEFAULT_MAX_NORMAL_FORCE_N}}}"',
            operator,
        )
        self.assertIn(
            'MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-${STEP5D_MAX_FORCE_NORM_N:-${STEP5D_DEFAULT_MAX_FORCE_NORM_N}}}"',
            operator,
        )
        self.assertIn(
            'MAX_TORQUE_NORM_NM="${MAX_TORQUE_NORM_NM:-${STEP5D_MAX_TORQUE_NORM_NM:-${STEP5D_DEFAULT_MAX_TORQUE_NORM_NM}}}"',
            operator,
        )
        self.assertNotIn('MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-${STEP5D_MAX_NORMAL_FORCE_N:-100}}"', operator)
        self.assertIn('BRIDGE_PROFILE="${STEP5D_VERSION}"', operator)
        self.assertIn('"${BRIDGE_OPERATOR}" line-bridge-fast', operator)
        self.assertNotIn('STEP4E_VERSION="${STEP5D_VERSION}"', operator)
        self.assertIn('--bridge-profile "${BRIDGE_PROFILE}"', bridge_operator)
        self.assertIn('--bridge-mode "${BRIDGE_MODE}"', bridge_operator)
        self.assertIn('Type START_BRIDGE_${CONFIRM_TOKEN}_${BRIDGE_PROFILE^^} to continue:', bridge_operator)
        self.assertIn('PROGRAM_LINE="/programs/andyl/kunwei/step5/${STEP4E_VERSION}.urp"', base)
        self.assertIn('PROGRAM_LINE="/programs/andyl/kunwei/step5/step5d/${STEP4E_VERSION}.urp"', base)
        self.assertIn('"step5d_strict_rnn_liveprep_v10" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v11"', base)
        self.assertIn('"step5d_strict_rnn_liveprep_v12" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v13" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v14" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v15" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v15a" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v16" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v17" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v18" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v19" || "${STEP4E_VERSION}" == "step5d_strict_rnn_liveprep_v20"', base)
        self.assertIn("current_step5d_profile()", bridge_operator)
        self.assertIn("refusing Step5d alias: current_stage does not name", bridge_operator)
        self.assertIn("refusing Step5d alias: current_stage does not name", base)
        self.assertNotIn('BRIDGE_PROFILE="${BRIDGE_PROFILE:-step5d_strict_rnn_liveprep_v21}"', bridge_operator)
        self.assertNotIn('STEP4E_VERSION="${STEP4E_VERSION:-step5d_strict_rnn_liveprep_v20}"', base)
        self.assertIn('--step5d-stage25-control-mode "${STEP5D_STAGE25_CONTROL_MODE:-${STEP5D_STAGE25_CONTROL_MODE_DEFAULT}}"', bridge_operator)
        self.assertIn('--step5d-preload-filtered-min-n "${STEP5D_PRELOAD_FILTERED_MIN_N:-${STEP5D_DEFAULT_PRELOAD_FILTERED_MIN_N}}"', bridge_operator)
        self.assertIn("Step5d v26 tube ablation diagnostic", bridge_operator)
        self.assertIn("Step5d v26 tube ablation diagnostic", base)
        self.assertIn("7-18N filtered preload with 5-20N raw sanity", bridge_operator)
        self.assertIn("default speedl_cartesian_oracle", bridge_operator)
        self.assertIn("step5d_live_ready", bridge_operator)
        bridge_source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")
        self.assertIn('v18_v20_locked_normal_settle', bridge_source)
        self.assertIn('v20_low_load_active_reacquire', bridge_source)
        self.assertIn('step5d_contact_safety["action"] == "active_reacquire_solver"', bridge_source)
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
        v16_min, v16_max, v16_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v16")
        self.assertEqual((v16_min, v16_max, v16_force_max), (5.0, 20.0, 25.0))
        v17_min, v17_max, v17_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v17")
        self.assertEqual((v17_min, v17_max, v17_force_max), (8.0, 18.0, 25.0))
        v18_min, v18_max, v18_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v18")
        self.assertEqual((v18_min, v18_max, v18_force_max), (8.0, 18.0, 25.0))
        v19_min, v19_max, v19_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v19")
        self.assertEqual((v19_min, v19_max, v19_force_max), (8.0, 13.0, 25.0))
        v20_min, v20_max, v20_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v20")
        self.assertEqual((v20_min, v20_max, v20_force_max), (8.0, 13.0, 25.0))
        v21_min, v21_max, v21_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v21")
        self.assertEqual((v21_min, v21_max, v21_force_max), (7.5, 14.0, 25.0))
        v22_min, v22_max, v22_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v22")
        self.assertEqual((v22_min, v22_max, v22_force_max), (7.5, 14.0, 25.0))
        v23_min, v23_max, v23_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v23")
        self.assertEqual((v23_min, v23_max, v23_force_max), (7.5, 14.0, 25.0))
        v24_min, v24_max, v24_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_liveprep_v24")
        self.assertEqual((v24_min, v24_max, v24_force_max), (7.5, 14.0, 25.0))
        v25_min, v25_max, v25_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_ablation_v25")
        self.assertEqual((v25_min, v25_max, v25_force_max), (10.5, 12.8, 25.0))
        v26_min, v26_max, v26_force_max = bridge.step5d_liveprep_contact_window_limits("step5d_strict_rnn_ablation_v26")
        self.assertEqual((v26_min, v26_max, v26_force_max), (7.0, 18.0, 25.0))
        for field in (
            "_step5d_reacquire_speed_cap_active",
            "_step5d_reacquire_speed_cap_m_s",
            "_step5d_reacquire_speed_cap_original_m_s",
            "_step5d_active_reacquire_s",
            "_step5d_no_contact_s",
            "_step5d_search_pose_contract_active",
            "_step5d_search_pose_contract_ok",
            "_step5d_search_pose_contract_axis_error_rad",
            "_step5d_search_pose_contract_tcp_z_dot_down",
            "_step5d_rnn_raw_qd0_rad_s",
            "_step5d_post_slew_qd0_rad_s",
            "_step5d_outer_xdot_limited_approach_normal_m_s",
            "_step5d_jqdot_raw_approach_normal_m_s",
            "_step5d_jqdot_cmd_approach_normal_m_s",
            "_step5d_lambda_norm",
            "_step5d_active_bound_qd0",
            "_step5d_post_rnn_normal_guard_action",
            "_step5d_normal_direction_guard_zeroed_qdot",
            "_step5d_intervention_reason",
            "_step5d_qdot_cap_rad_s",
            "_step5d_jinv_xdot_inf_rad_s",
            "_step5d_jinv_xdot_inf_over_qdot_cap",
            "_step5d_jinv_xdot_solve_status",
            "_step5d_xdot_feasibility_scale",
            "_step5d_xdot_norm_pre_feasibility_scale",
            "_step5d_xdot_norm_post_feasibility_scale",
            "_step5d_xdot_feasibility_scale_active",
            "_step5d_lambda_state_0",
        ):
            self.assertIn(field, bridge.STEP5D_DIAG_FIELDS)
        speed_limited, original_speed, speed_active = bridge.limit_step5d_predicted_tcp_speed(
            [0.1, 0.0, 0.0, 0.0, 0.0, 0.0],
            np.eye(6),
            max_tcp_speed_m_s=0.035,
        )
        self.assertTrue(speed_active)
        self.assertAlmostEqual(original_speed, 0.1)
        self.assertLessEqual(float(np.linalg.norm(speed_limited[:3])), 0.035 + 1e-12)
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

    def test_step5d_v23_qdot_diagnostics_and_normal_guard(self) -> None:
        diag = bridge.step5d_qdot_diagnostic_values(
            jacobian=np.eye(6),
            raw_qdot=[0.0, 0.0, -0.002, 0.0, 0.0, 0.0],
            post_slew_qdot=[0.0, 0.0, -0.001, 0.0, 0.0, 0.0],
            final_qdot=[0.0, 0.0, -0.0005, 0.0, 0.0, 0.0],
            outer_xdot_limited=[0.0, 0.0, -0.003, 0.0, 0.0, 0.0],
            reaction_normal_b=[0.0, 0.0, 1.0],
            lambda_state=[3.0, 4.0],
            active_bounds_mask=[True, False, True, False, False, False],
            intervention_reason="qdot_slew_limited",
        )
        self.assertEqual(diag["_step5d_rnn_raw_qd2_rad_s"], -0.002)
        self.assertEqual(diag["_step5d_post_slew_qd2_rad_s"], -0.001)
        self.assertAlmostEqual(diag["_step5d_outer_xdot_limited_approach_normal_m_s"], 0.003)
        self.assertAlmostEqual(diag["_step5d_jqdot_cmd_approach_normal_m_s"], 0.0005)
        self.assertAlmostEqual(diag["_step5d_lambda_norm"], 5.0)
        self.assertEqual(diag["_step5d_active_bound_qd0"], 1.0)
        self.assertEqual(diag["_step5d_intervention_reason"], "qdot_slew_limited")

        hold = bridge.step5d_post_rnn_normal_direction_guard(
            qdot=[0.0, 0.0, -0.002, 0.0, 0.0, 0.0],
            jacobian=np.eye(6),
            reaction_normal_b=[0.0, 0.0, 1.0],
            actual_tcp_speed_b=[0.0, 0.0, 0.0],
            normal_load_n=14.5,
            force_norm_n=14.5,
            previous_normal_load_n=14.4,
            prior_dwell_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(hold["action"], "hold_zero_qdot")
        self.assertEqual(hold["reason"], "post_rnn_high_load_press_hold")

        stop = bridge.step5d_post_rnn_normal_direction_guard(
            qdot=[0.0, 0.0, -0.002, 0.0, 0.0, 0.0],
            jacobian=np.eye(6),
            reaction_normal_b=[0.0, 0.0, 1.0],
            actual_tcp_speed_b=[0.0, 0.0, 0.0],
            normal_load_n=18.5,
            force_norm_n=18.5,
            previous_normal_load_n=18.4,
            prior_dwell_s=0.003,
            dt_s=0.002,
        )
        self.assertEqual(stop["action"], "stop_zero_qdot")
        self.assertEqual(stop["reason"], "post_rnn_high_load_press_dwell_stop")

        tracking_hold = bridge.step5d_post_rnn_tracking_guard(
            qdot=[0.0, 0.0, 0.002, 0.0, 0.0, 0.0],
            jacobian=np.eye(6),
            outer_xdot_limited=[0.0, 0.0, -0.001, 0.0, 0.0, 0.0],
            reaction_normal_b=[0.0, 0.0, 1.0],
            actual_tcp_speed_b=[0.0, 0.0, 0.0],
            prior_dwell_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(tracking_hold["action"], "hold_zero_qdot")
        self.assertEqual(tracking_hold["reason"], "post_rnn_tracking_reversed_unload_hold")

        tracking_stop = bridge.step5d_post_rnn_tracking_guard(
            qdot=[0.0, 0.0, 0.002, 0.0, 0.0, 0.0],
            jacobian=np.eye(6),
            outer_xdot_limited=[0.0, 0.0, -0.001, 0.0, 0.0, 0.0],
            reaction_normal_b=[0.0, 0.0, 1.0],
            actual_tcp_speed_b=[0.0, 0.0, 0.0],
            prior_dwell_s=0.003,
            dt_s=0.002,
        )
        self.assertEqual(tracking_stop["action"], "stop_zero_qdot")
        self.assertEqual(tracking_stop["reason"], "post_rnn_tracking_reversed_unload_stop")

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
        self.assertEqual(low_load_speed_first["action"], "hold_zero_qdot")
        self.assertEqual(low_load_speed_first["reason"], "low_load_actual_tcp_speed_watchdog_dwell_hold")
        self.assertAlmostEqual(float(low_load_speed_first["actual_speed_violation_s"]), 0.002)
        self.assertEqual(low_load_speed_first["actual_speed_violation_count"], 1)

        low_load_speed_second = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=0.8,
            force_norm_n=0.9,
            actual_tcp_speed_m_s=0.026,
            predicted_tcp_speed_m_s=0.0,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=float(low_load_speed_first["actual_speed_violation_s"]),
            prior_actual_speed_violation_count=int(low_load_speed_first["actual_speed_violation_count"]),
            dt_s=0.002,
        )
        self.assertEqual(low_load_speed_second["action"], "stop_zero_qdot")
        self.assertEqual(low_load_speed_second["reason"], "low_load_actual_tcp_speed_watchdog_dwell")
        self.assertEqual(low_load_speed_second["actual_speed_violation_count"], 2)

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
        self.assertEqual(actual_spike["action"], "hold_zero_qdot")
        self.assertEqual(actual_spike["reason"], "actual_tcp_speed_watchdog_dwell_hold")
        self.assertEqual(actual_spike["actual_speed_violation_count"], 1)

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

        predicted_overrides_actual_dwell = bridge.step5d_v13_contact_safety_guard(
            normal_load_n=5.0,
            force_norm_n=5.0,
            actual_tcp_speed_m_s=0.051,
            predicted_tcp_speed_m_s=0.051,
            prior_hold_s=0.0,
            prior_high_window_s=0.0,
            prior_actual_speed_violation_s=0.0,
            dt_s=0.002,
        )
        self.assertEqual(predicted_overrides_actual_dwell["action"], "stop_zero_qdot")
        self.assertEqual(predicted_overrides_actual_dwell["reason"], "predicted_tcp_speed_watchdog")
        self.assertEqual(predicted_overrides_actual_dwell["actual_speed_violation_count"], 1)

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
            prior_actual_speed_violation_count=1,
            dt_s=0.002,
        )
        self.assertEqual(recovered["action"], "pass_solver")
        self.assertEqual(recovered["hold_s"], 0.0)
        self.assertEqual(recovered["high_window_s"], 0.0)
        self.assertEqual(recovered["actual_speed_violation_s"], 0.0)
        self.assertEqual(recovered["actual_speed_violation_count"], 0)

    def test_step5d_v13_actual_speed_hold_skips_solver_path(self) -> None:
        source = inspect.getsource(bridge.compute_step4e_values)
        hold_gate = 'step5d_contact_safety["action"] in {"hold_zero_qdot", "stop_zero_qdot"}'
        solver_entry = "jacobian = step5d_tcp_jacobian_base"
        self.assertIn(hold_gate, source)
        self.assertIn("state.line_stage_s = state.step5d_contact_hold_path_time_s", source)
        self.assertLess(source.index(hold_gate), source.index(solver_entry))

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
        actual_speed_violation_count = 0
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
                prior_actual_speed_violation_count=actual_speed_violation_count,
                dt_s=0.002,
            )
            hold_s = float(result["hold_s"])
            actual_speed_violation_s = float(result["actual_speed_violation_s"])
            actual_speed_violation_count = int(result["actual_speed_violation_count"])
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
