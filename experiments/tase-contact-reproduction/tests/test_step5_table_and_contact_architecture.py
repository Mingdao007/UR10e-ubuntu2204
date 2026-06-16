#!/usr/bin/env python3
"""Offline checks for Step5 table-driven contact architecture."""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import kunwei_rtde_bridge as bridge  # noqa: E402
import step5_table  # noqa: E402


def coordinates_in_basis(
    xy: tuple[float, float],
    anchor: tuple[float, float],
    u_along: tuple[float, float],
    p_lateral: tuple[float, float],
) -> tuple[float, float]:
    dx = xy[0] - anchor[0]
    dy = xy[1] - anchor[1]
    return dx * u_along[0] + dy * u_along[1], dx * p_lateral[0] + dy * p_lateral[1]


class Step5TableAndContactArchitectureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.table = step5_table.load_step5_table()
        self.no_contact = step5_table.active_no_contact_stage(self.table)
        self.contact = step5_table.step5_stage("step5_contact_cycloid_baseline_v1", self.table)
        self.frame = step5_table.load_stage_frame(self.contact)

    def test_step5_table_separates_no_contact_and_contact_owners(self) -> None:
        self.assertEqual(self.no_contact["owner"], "TP")
        self.assertFalse(self.no_contact["contact"])
        self.assertFalse(self.no_contact["bridge"])
        self.assertEqual(self.no_contact["filter_policy"]["normal_filter"], "none")

        self.assertEqual(self.contact["owner"], "bridge+TP")
        self.assertTrue(self.contact["contact"])
        self.assertTrue(self.contact["bridge"])
        self.assertEqual(self.contact["contact_policy"]["reference_owner"], "bridge")
        self.assertEqual(self.contact["contact_policy"]["tp_role"], "executor_and_guard_only")
        self.assertEqual(self.contact["filter_policy"]["normal_filter"], "v31_filtered_live")
        self.assertEqual(self.contact["filter_policy"]["alpha"], 0.35)
        self.assertEqual(self.contact["filter_policy"]["min_force_n"], 2.0)

        regs = self.table["register_contract"]["command_registers"]
        self.assertEqual(
            [regs[name] for name in ("qd0_rad_s", "qd1_rad_s", "qd2_rad_s", "qd3_rad_s", "qd4_rad_s", "qd5_rad_s", "cmd_valid", "path_time_s")],
            list(range(37, 45)),
        )

        dry = step5_table.step5_stage("step5c_speedj_dryrun_v1", self.table)
        contact = step5_table.step5_stage("step5c_joint_rnn_cycloid_v1", self.table)
        strict = step5_table.step5_stage("step5c_strict_rnn_dryrun_v1", self.table)
        liveprep_v1 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v1", self.table)
        liveprep_v2 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v2", self.table)
        liveprep_v3 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v3", self.table)
        liveprep_v4 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v4", self.table)
        liveprep_v5 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v5", self.table)
        liveprep_v6 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v6", self.table)
        liveprep_v7 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v7", self.table)
        liveprep_v8 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v8", self.table)
        liveprep_v9 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v9", self.table)
        liveprep_v10 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v10", self.table)
        liveprep_v11 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v11", self.table)
        liveprep_v12 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v12", self.table)
        liveprep_v13 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v13", self.table)
        liveprep_v14 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v14", self.table)
        liveprep_v15 = step5_table.step5_stage("step5d_strict_rnn_liveprep_v15", self.table)
        liveprep = liveprep_v15
        step5d = step5_table.step5_stage("step5d_strict_rnn_reproduction_v1", self.table)
        self.assertFalse(dry["active"])
        self.assertTrue(dry["blocked"])
        self.assertFalse(contact["active"])
        self.assertTrue(contact["blocked"])
        self.assertFalse(strict["active"])
        self.assertTrue(strict["blocked"])
        self.assertTrue(dry["joint_space"])
        self.assertTrue(contact["joint_space"])
        self.assertEqual(dry["cadence"]["motion"], "quarantine_stop_only")
        self.assertEqual(contact["cadence"]["motion"], "quarantine_stop_only")
        self.assertEqual(dry["guard"]["qdot_cap_rad_s"], 0.2)
        self.assertEqual(contact["guard"]["qdot_cap_rad_s"], 0.15)
        self.assertTrue(liveprep_v1["complete"])
        self.assertIn("0.106", liveprep_v1["live_run_evidence"]["result"])
        self.assertIn("heartbeat stale", liveprep_v1["live_run_evidence"]["result"])
        self.assertEqual(liveprep_v1["live_run_evidence"]["followup"], "superseded by step5d_strict_rnn_liveprep_v2")
        self.assertTrue(liveprep_v2["complete"])
        self.assertEqual(liveprep_v2["live_run_evidence"]["followup"], "superseded by step5d_strict_rnn_liveprep_v3")
        self.assertIn("65.5", liveprep_v2["live_run_evidence"]["result"])
        self.assertTrue(liveprep_v3["complete"])
        self.assertEqual(liveprep_v3["live_run_evidence"]["followup"], "superseded by step5d_strict_rnn_liveprep_v4")
        self.assertIn("did not enter Stage 25.0", liveprep_v3["live_run_evidence"]["result"])
        self.assertTrue(liveprep_v4["complete"])
        self.assertEqual(liveprep_v4["delivery_evidence"]["program_basename"], "step5d_strict_rnn_liveprep_v4")
        self.assertIn("Step5d outer loop", liveprep_v4["semantic_failure"]["root_cause"])
        self.assertIn("180", liveprep_v4["semantic_failure"]["root_cause"])
        self.assertIn("Superseded by v5", liveprep_v4["success_condition"])
        self.assertTrue(liveprep_v5["complete"])
        self.assertEqual(liveprep_v5["delivery_evidence"]["program_basename"], "step5d_strict_rnn_liveprep_v5")
        self.assertTrue(liveprep_v5["delivery_evidence"]["controller_readback_verified"])
        self.assertIn("17-21N", liveprep_v5["live_run_evidence"]["result"])
        self.assertEqual(liveprep_v5["live_run_evidence"]["followup"], "superseded by step5d_strict_rnn_liveprep_v6")
        self.assertTrue(liveprep_v6["complete"])
        self.assertEqual(liveprep_v6["local_delivery_evidence"]["program_basename"], "step5d_strict_rnn_liveprep_v6")
        self.assertTrue(liveprep_v6["local_delivery_evidence"]["controller_readback_verified"])
        self.assertIn("20-22N", liveprep_v6["live_run_evidence"]["result"])
        self.assertEqual(liveprep_v6["live_run_evidence"]["followup"], "superseded by step5d_strict_rnn_liveprep_v7")
        self.assertTrue(liveprep_v7["complete"])
        self.assertEqual(liveprep_v7["local_delivery_evidence"]["program_basename"], "step5d_strict_rnn_liveprep_v7")
        self.assertIn("Superseded by v8", liveprep_v7["success_condition"])
        self.assertFalse(liveprep_v8["active"])
        self.assertFalse(liveprep_v8["blocked"])
        self.assertTrue(liveprep_v8["complete"])
        self.assertFalse(liveprep_v8["completion_target"])
        self.assertEqual(liveprep_v8["local_delivery_evidence"]["program_basename"], "step5d_strict_rnn_liveprep_v8")
        self.assertIn("low-load dropout", liveprep_v8["block_reason"])
        self.assertEqual(liveprep_v8["live_run_evidence"]["followup"], "superseded by step5d_strict_rnn_liveprep_v9")
        self.assertFalse(liveprep_v9["active"])
        self.assertTrue(liveprep_v9["complete"])
        self.assertIn("hunted", liveprep_v9["block_reason"])
        self.assertEqual(liveprep_v9["live_run_evidence"]["followup"], "superseded by step5d_strict_rnn_liveprep_v10")
        self.assertFalse(liveprep_v10["active"])
        self.assertTrue(liveprep_v10["complete"])
        self.assertIn("scalar admittance", liveprep_v10["block_reason"])
        self.assertIn("flipped sign", liveprep_v10["live_run_evidence"]["root_cause"])
        self.assertEqual(liveprep_v10["live_run_evidence"]["followup"], "superseded by step5d_strict_rnn_liveprep_v11")
        self.assertFalse(liveprep_v11["active"])
        self.assertTrue(liveprep_v11["complete"])
        self.assertIn("ROBOT_EMERGENCY_STOP", liveprep_v11["block_reason"])
        self.assertEqual(liveprep_v11["live_run_evidence"]["run_dir"], "runs/bridge_step5d_strict_rnn_liveprep_v11_20260615_204601")
        self.assertIn("Stage 25.0 ran", liveprep_v11["live_run_evidence"]["result"])
        for archived_stage in (
            liveprep_v1,
            liveprep_v2,
            liveprep_v3,
            liveprep_v4,
            liveprep_v5,
            liveprep_v6,
            liveprep_v7,
            liveprep_v8,
            liveprep_v9,
            liveprep_v10,
            liveprep_v11,
        ):
            evidence = archived_stage.get("local_delivery_evidence") or archived_stage["delivery_evidence"]
            self.assertEqual(evidence["local_program_dir"], "programs/step5/step5d")
            self.assertEqual(evidence["controller_dir"], "/programs/andyl/kunwei/step5/step5d")
            self.assertTrue(evidence["archived_to_step5d_dir"])
        self.assertFalse(liveprep_v12["active"])
        self.assertTrue(liveprep_v12["local_delivery_evidence"]["controller_readback_verified"])
        self.assertIn("Retained v12", liveprep_v12["block_reason"])
        self.assertFalse(liveprep_v13["active"])
        self.assertTrue(liveprep_v13["local_delivery_evidence"]["controller_readback_verified"])
        self.assertIn("known P1", liveprep_v13["block_reason"])
        self.assertFalse(liveprep_v14["active"])
        self.assertTrue(liveprep_v14["local_delivery_evidence"]["controller_readback_verified"])
        self.assertEqual(
            liveprep_v14["live_run_evidence"]["stop_reason"],
            "step5d_contact_safety:predicted_tcp_speed_watchdog",
        )
        self.assertTrue(liveprep["active"])
        self.assertFalse(liveprep["blocked"])
        self.assertFalse(liveprep["complete"])
        self.assertFalse(liveprep["completion_target"])
        self.assertIn("Controller read-back verified", liveprep["block_reason"])
        self.assertEqual(liveprep["local_delivery_evidence"]["local_program_dir"], "programs/step5")
        self.assertEqual(liveprep["local_delivery_evidence"]["controller_dir"], "/programs/andyl/kunwei/step5")
        self.assertFalse(liveprep["local_delivery_evidence"]["archived_to_step5d_dir"])
        self.assertTrue(liveprep["local_delivery_evidence"]["semantic_gate_pass"])
        self.assertEqual(liveprep["local_delivery_evidence"]["program_basename"], "step5d_strict_rnn_liveprep_v15")
        self.assertEqual(
            liveprep["local_delivery_evidence"]["stamp"],
            "2026-06-16T1517HKT_STEP5D_STRICT_RNN_LIVEPREP_V15",
        )
        self.assertTrue(liveprep["local_delivery_evidence"]["local_package_validated"])
        self.assertTrue(liveprep["local_delivery_evidence"]["controller_readback_verified"])
        self.assertEqual(
            liveprep["local_delivery_evidence"]["controller_readback"],
            "runs/controller_readback_step5d_strict_rnn_liveprep_v15_20260616_151843/manifest.json",
        )
        self.assertTrue(liveprep["local_delivery_evidence"]["local_controller_readback_sha_match"])
        self.assertTrue(liveprep["local_delivery_evidence"]["fetched_back_urp_internal_gate_pass"])
        self.assertEqual(
            liveprep["local_delivery_evidence"]["sha256"][".script"],
            "09e10425efee2e1ed097ae8f2c402b29c1edc1fb720b05463255e6102b564a56",
        )
        self.assertEqual(
            liveprep["local_delivery_evidence"]["sha256"][".txt"],
            "c4b9503b77828769d18b20c09ed9af9894779442a05617fdd26142e1725a2e0a",
        )
        self.assertEqual(
            liveprep["local_delivery_evidence"]["sha256"][".urp"],
            "53e8ad0726e030c90add8eaeb0855ff948245392b6ddb15b3986cbb265fb279c",
        )
        self.assertEqual(liveprep["local_delivery_evidence"]["offline_replay_summary"], "runs/step5d_v15_permissive_recovery_offline_20260616/summary.json")
        self.assertNotIn("live_run_evidence", liveprep)
        self.assertIn("does not authorize bridge start", liveprep["success_condition"])
        self.assertTrue(liveprep["strict_rnn"])
        self.assertEqual(liveprep["guard"]["qdot_cap_rad_s"], 0.05)
        self.assertEqual(liveprep["guard"]["qdot_slew_rad_s2"], 0.2)
        self.assertEqual(liveprep["guard"]["raw_normal_guard_n"], 50.0)
        self.assertEqual(liveprep["guard"]["force_norm_guard_n"], 60.0)
        self.assertAlmostEqual(liveprep["guard"]["semantic_orientation_tolerance_rad"], math.radians(5.0))
        self.assertEqual(liveprep["guard"]["second_contact_near_start_depth_m"], 0.0)
        self.assertEqual(liveprep["guard"]["second_contact_far_speed_m_s"], -0.0025)
        self.assertEqual(liveprep["guard"]["second_contact_near_speed_m_s"], -0.0025)
        self.assertEqual(liveprep["guard"]["line_entry_normal_load_min_n"], 2.0)
        self.assertEqual(liveprep["guard"]["line_entry_normal_load_max_n"], 15.0)
        self.assertEqual(liveprep["guard"]["line_entry_force_norm_max_n"], 25.0)
        self.assertEqual(liveprep["guard"]["line_entry_required_s"], 0.15)
        self.assertNotIn("line_entry_settle_velocity_ready_m_s", liveprep["guard"])
        self.assertNotIn("admittance_mass", liveprep["guard"])
        self.assertNotIn("admittance_damping", liveprep["guard"])
        self.assertEqual(liveprep["guard"]["normal_load_filter_alpha"], 0.15)
        self.assertEqual(liveprep["guard"]["deadband_acquire_v_max_m_s"], 0.0012)
        self.assertEqual(liveprep["guard"]["deadband_acquire_slew_m_s2"], 0.012)
        self.assertEqual(liveprep["guard"]["line_entry_recovery_normal_load_min_n"], 0.0)
        self.assertEqual(liveprep["guard"]["line_entry_recovery_normal_load_max_n"], 40.0)
        self.assertEqual(liveprep["guard"]["line_entry_force_norm_stop_n"], 25.0)
        self.assertEqual(liveprep["guard"]["line_entry_timeout_s"], 10.0)
        self.assertEqual(liveprep["guard"]["hard_low_load_n"], 0.25)
        self.assertEqual(liveprep["guard"]["soft_low_load_n"], 0.5)
        self.assertEqual(liveprep["guard"]["valid_contact_min_n"], 0.5)
        self.assertEqual(liveprep["guard"]["valid_contact_max_n"], 15.0)
        self.assertEqual(liveprep["guard"]["force_norm_valid_max_n"], 25.0)
        self.assertEqual(liveprep["guard"]["low_load_speed_stop_m_s"], 0.025)
        self.assertEqual(liveprep["guard"]["absolute_speed_stop_m_s"], 0.05)
        self.assertEqual(liveprep["guard"]["actual_speed_dwell_stop_s"], 0.004)
        self.assertEqual(liveprep["guard"]["low_load_hold_timeout_s"], 0.3)
        self.assertEqual(liveprep["guard"]["high_window_dwell_stop_s"], 0.03)
        self.assertEqual(liveprep["guard"]["orientation_skip_lift_error_rad"], 0.069813)
        self.assertEqual(liveprep["contact_policy"]["tp_role"], "stage_split_executor_and_guard_only")
        self.assertEqual(liveprep["contact_policy"]["deadband_acquire_stage"], "25.3")
        self.assertEqual(liveprep["semantic_control_gate"]["contract"], "UR_FORCE_FRAME_CONTRACT.md")
        self.assertEqual(liveprep["semantic_control_gate"]["approach_normal"], "-reaction_normal")
        self.assertEqual(liveprep["semantic_control_gate"]["normal_load_n"], "dot(force_base, reaction_normal)")
        contact_safety = liveprep["semantic_control_gate"]["stage25_contact_safety"]
        self.assertIn("predicted_tcp_speed_recoverable_hold", contact_safety["recoverable_hold_reasons"])
        self.assertIn("low_load_reacquire_hold_timeout_deferred", contact_safety["recoverable_hold_reasons"])
        self.assertIn("tcp_cage_braking_margin_exhausted", contact_safety["hard_stop_reasons"])
        self.assertIn("actual_tcp_speed_watchdog_dwell", contact_safety["hard_stop_reasons"])
        self.assertIn("Do not normalize raw live force", liveprep["semantic_control_gate"]["forbidden"])
        self.assertIn("not an independent closed-loop stability proof", liveprep["semantic_control_gate"]["gate_scope"])
        self.assertIn("v11 escape", liveprep["semantic_control_gate"]["failure_contrast"])
        self.assertIn("hard-stops", liveprep["semantic_control_gate"]["failure_contrast"])
        self.assertIn("zero-qdot hold", liveprep["semantic_control_gate"]["failure_contrast"])
        self.assertTrue(any("v11" in gate and "replay" in gate and "hard-stop" in gate for gate in liveprep["liveprep_gates"]))
        self.assertTrue(any("0.050 rad/s" in gate for gate in liveprep["liveprep_gates"]))
        self.assertTrue(any("controller upload/read-back verification" in gate for gate in liveprep["liveprep_gates"]))
        self.assertEqual(contact_safety["danger_action"], "stop_request=1_zero_qdot_for_hard_stops_only")
        self.assertEqual(liveprep["filter_policy"]["force_sign_convention"], "step5_step6_positive_normal_load")
        self.assertEqual(liveprep["filter_policy"]["semantic_contract"], "reaction normal for load, approach normal for posture/press direction")
        self.assertEqual(step5d["guard"]["qdot_cap_rad_s"], 0.30)
        self.assertFalse(step5d["active"])
        self.assertTrue(step5d["blocked"])
        self.assertFalse(step5d["complete"])
        self.assertTrue(step5d["completion_target"])
        self.assertTrue(step5d["strict_rnn"])
        self.assertTrue(step5d["contact"])
        self.assertTrue(step5d["joint_space"])
        self.assertEqual(step5d["contact_policy"]["joint_solver"], "strict_tase_rnn_only")
        self.assertFalse(step5d["contact_policy"]["dls_fallback_allowed"])
        self.assertFalse(step5d["contact_policy"]["ik_fallback_allowed"])
        self.assertIn("Pinocchio", step5d["contact_policy"]["calibrated_kinematics_backend"])
        self.assertTrue(any("strict_rnn_enabled=true" in gate for gate in step5d["rnn_completion_gates"]))
        self.assertTrue(any("DLS" in gate and "fallback" in gate for gate in step5d["rnn_completion_gates"]))
        self.assertTrue(any("step5d_full_chain_sanity.py" in gate for gate in step5d["rnn_completion_gates"]))
        self.assertIn("Do not mark complete", step5d["success_condition"])

        kin_gate = self.table["calibrated_kinematics_gate"]
        self.assertEqual(kin_gate["calibration_hash"], "calib_7367377276742883610")
        self.assertEqual(kin_gate["audit_tool"], "tools/step5c_calibrated_kinematics_audit.py")
        self.assertIn("tool0", kin_gate["pinocchio_frames"])
        self.assertTrue(kin_gate["required_evidence"]["qdot_register_path_fixed"])
        self.assertTrue(
            any("ur10e_nominal.xml" in banned for banned in kin_gate["banned_for_live_step5c"])
        )

        qdot_gate = self.table["qdot_register_path_gate"]
        self.assertEqual(qdot_gate["status"], "offline_contract_fixed")
        self.assertIn("no live bridge", qdot_gate["scope"])
        self.assertEqual(
            [qdot_gate["command_registers"][str(idx)] for idx in range(37, 45)],
            [
                "qd0_rad_s",
                "qd1_rad_s",
                "qd2_rad_s",
                "qd3_rad_s",
                "qd4_rad_s",
                "qd5_rad_s",
                "cmd_valid",
                "path_time_s",
            ],
        )
        self.assertEqual(qdot_gate["diagnostic_registers"]["47"], "solver_status")
        self.assertTrue(any("hard-blocks" in condition for condition in qdot_gate["pass_conditions"]))

    def test_step5d_truth_resolves_force_sign_but_keeps_live_blockers(self) -> None:
        truth = json.loads((ROOT / "config" / "step5c_tase_paper_truth.json").read_text(encoding="utf-8"))
        self.assertEqual(truth["parameters"]["qdot_bound_rad_s"], 0.30)
        self.assertEqual(truth["parameters"]["force_target_n"], 5.0)
        self.assertNotIn("force_sign_convention", truth["open_questions"])
        self.assertNotIn("force_sign_convention", truth["pending_pdf_verify"])
        self.assertEqual(
            truth["implementation_assumptions"]["force_sign_convention"]["value"],
            "step5_step6_positive_normal_load",
        )
        self.assertIn("alpha_escape_velocity_gain", truth["pending_pdf_verify"])

    def test_bridge_step5_contact_reference_is_table_driven(self) -> None:
        origin, u_along, p_lateral = step5_table.basis_xy(self.frame)
        ref = bridge.step5_contact_path_reference(origin, 60.0)
        along, lateral = coordinates_in_basis(ref["desired_xy"], origin, u_along, p_lateral)
        self.assertAlmostEqual(ref["phase_rad"], 6.0, places=12)
        self.assertAlmostEqual(ref["progress"], 60.0, places=12)
        self.assertAlmostEqual(along, 0.015 * (6.0 - math.sin(6.0)), places=12)
        self.assertAlmostEqual(lateral, 0.015 * (1.0 - math.cos(6.0)), places=12)
        self.assertIn("desired_xy", ref)
        self.assertIn("desired_velocity_xy", ref)
        self.assertIn("path_error_xy", ref)

        offset_pose = (ref["desired_xy"][0] + 0.002, ref["desired_xy"][1] - 0.001)
        offset_ref = bridge.step5_contact_path_reference(offset_pose, 60.0)
        self.assertAlmostEqual(offset_ref["path_error_xy"][0], -0.002, places=12)
        self.assertAlmostEqual(offset_ref["path_error_xy"][1], 0.001, places=12)

    def test_bridge_default_target_force_is_contact_baseline(self) -> None:
        args = bridge.parse_args(["--no-start-command"])
        self.assertEqual(args.target_force_n, 5.0)

    def test_v31_filtered_live_normal_alpha_and_holds(self) -> None:
        current = (0.0, 0.0, 1.0)
        candidate = (0.0, 1.0, 0.0)
        updated, source = bridge.v31_filtered_live_normal(
            current,
            candidate,
            3.0,
            sensor_ok=1.0,
            alpha=0.35,
            min_force_n=2.0,
        )
        expected = bridge.normalize3((0.0, 0.35, 0.65))
        self.assertEqual(source, "filtered_live_alpha")
        for got, want in zip(updated, expected):
            self.assertAlmostEqual(got, want, places=12)

        stale, source = bridge.v31_filtered_live_normal(current, candidate, 3.0, sensor_ok=0.0)
        self.assertEqual(stale, current)
        self.assertEqual(source, "hold_stale")

        low_force, source = bridge.v31_filtered_live_normal(current, candidate, 1.0, sensor_ok=1.0)
        self.assertEqual(low_force, current)
        self.assertEqual(source, "hold_low_force")

        reverse, source = bridge.v31_filtered_live_normal(current, (0.0, 0.0, -1.0), 3.0, sensor_ok=1.0)
        self.assertEqual(reverse, current)
        self.assertEqual(source, "hold_reverse")


if __name__ == "__main__":
    unittest.main()
