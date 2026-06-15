#!/usr/bin/env python3
"""Offline checks for Step5c diagnostic DLS and strict-RNN gates."""

from __future__ import annotations

import gzip
import inspect
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5c_joint as step5c  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
from build_step4e_line_programs import line_cfg, load_json  # noqa: E402
from step5c_dls_joint_solver import JointCommandResult  # noqa: E402
from step5c_dls_joint_solver import JointSolverConfig, Step5cDlsJointSolver  # noqa: E402
from step5c_strict_rnn import PaperTruthPendingError, StrictTaseRnnSolver  # noqa: E402


class Step5cJointTest(unittest.TestCase):
    def test_solver_returns_finite_bounded_qdot_and_rejects_invalid(self) -> None:
        solver = Step5cDlsJointSolver(JointSolverConfig(qdot_limit_rad_s=0.20))
        result = solver.solve(
            (-1.57, -1.20, 1.80, -2.10, -1.57, 0.0),
            (0.001, -0.0005, 0.0, 0.0, 0.0, 0.0),
        )
        self.assertEqual(len(result.qdot), 6)
        self.assertLessEqual(result.max_abs_qdot_rad_s, 0.20)
        self.assertGreaterEqual(result.solver_status, 40.0)
        with self.assertRaises(ValueError):
            solver.solve((0.0, 0.0, float("nan"), 0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 0.0, 0.0, 0.0))

    def test_bridge_parses_dryrun_and_blocks_step5c_profiles_in_main(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5c_speedj_dryrun_v1",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(args.step4e_version, "step5c_speedj_dryrun_v1")
        source = inspect.getsource(bridge.main)
        self.assertIn("Blocked Step5c dry-run", source)
        self.assertIn("Blocked Step5c contact", source)
        with self.assertRaisesRegex(SystemExit, "Blocked Step5c dry-run"):
            bridge.main(
                [
                    "--no-start-command",
                    "--skip-dashboard-preflight",
                    "--step4e-mode",
                    "line",
                    "--step4e-version",
                    "step5c_speedj_dryrun_v1",
                ]
            )
        with self.assertRaisesRegex(SystemExit, "Blocked Step5c contact"):
            bridge.main(
                [
                    "--no-start-command",
                    "--skip-dashboard-preflight",
                    "--step4e-mode",
                    "line",
                    "--step4e-version",
                    "step5c_joint_rnn_cycloid_v1",
                ]
            )

    def test_step5c_qdot_register_helper_matches_tp_executor_contract(self) -> None:
        qdot = (0.01, -0.02, 0.03, -0.04, 0.05, -0.06)
        values = bridge.step5c_joint_register_values(
            qdot,
            cmd_valid=1.0,
            path_time_s=1.23,
            force_error_n=-0.5,
            pose_or_orientation_error=0.07,
            solver_status=41.0,
        )
        carrier_names = [
            "step4e_cmd_vx_m_s",
            "step4e_cmd_vy_m_s",
            "step4e_cmd_vz_m_s",
            "step4e_cmd_wx_rad_s",
            "step4e_cmd_wy_rad_s",
            "step4e_cmd_wz_rad_s",
        ]
        for idx, carrier_name in enumerate(carrier_names):
            self.assertEqual(values[carrier_name], qdot[idx])
            self.assertEqual(values[f"_step5c_cmd_qd{idx}_rad_s"], qdot[idx])
        self.assertEqual(values["step4e_cmd_valid"], 1.0)
        self.assertEqual(values["step4e_progress_m"], 1.23)
        self.assertEqual(values["step4e_force_error_n"], -0.5)
        self.assertEqual(values["step4e_orientation_error_rad"], 0.07)
        self.assertEqual(values["step4e_controller_state"], 41.0)
        self.assertEqual(values["_step5c_solver_status"], 41.0)

        geom = line_cfg(load_json(step5c.CONFIG_PATH))
        dry_frame = step5c.load_step5c_frame(step5c.DRYRUN_STAGE_ID)
        script = step5c._archived_dryrun_motion_script(
            "2026-06-12T1200HKT_STEP5C_SPEEDJ_DRYRUN_V1",
            "2026-06-12T12:00:00+08:00",
            geom,
            dry_frame,
        )
        reads = re.findall(r"local (cmd_qd[0-5]) = read_input_float_register\((\d+)\)", script)
        self.assertEqual(
            reads,
            [(f"cmd_qd{idx}", str(37 + idx)) for idx in range(6)],
        )
        self.assertIn("local cmd_valid = read_input_float_register(43)", script)
        self.assertIn("local progress_s = read_input_float_register(44)", script)
        self.assertIn("speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]", script)

    def test_step5c_metadata_marks_step4e_fields_as_qdot_carriers(self) -> None:
        metadata = bridge.step5c_register_metadata()
        self.assertEqual(metadata["contract"]["input_double_register_37"], "qd0_rad_s")
        self.assertEqual(metadata["contract"]["input_double_register_42"], "qd5_rad_s")
        self.assertEqual(metadata["contract"]["input_double_register_43"], "cmd_valid")
        self.assertEqual(metadata["contract"]["input_double_register_44"], "path_time_s")
        self.assertEqual(metadata["contract"]["input_double_register_47"], "solver_status")
        self.assertEqual(
            metadata["carriers"]["input_double_register_37"]["step4e_carrier_name"],
            "step4e_cmd_vx_m_s",
        )
        self.assertEqual(
            metadata["carriers"]["input_double_register_37"]["step5c_debug_column"],
            "_step5c_cmd_qd0_rad_s",
        )
        self.assertIn("_step5c_cmd_qd5_rad_s", bridge.STEP5C_DIAG_FIELDS)
        self.assertIn("failure_contrast_only", metadata["legacy_mujoco_model_role"])

    def test_step5c_compute_values_writes_qdot_to_carriers_and_debug_columns(self) -> None:
        qdot = (0.01, -0.02, 0.03, -0.04, 0.05, -0.06)

        class FakeSolver:
            def solve(self, _actual_q: object, _target_twist_base: object) -> JointCommandResult:
                return JointCommandResult(
                    qdot=qdot,
                    solver_status=42.0,
                    max_abs_qdot_rad_s=0.06,
                    clipped=False,
                    projected=True,
                    residual_norm=0.001,
                )

        args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5c_speedj_dryrun_v1",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        original_solver = bridge.step5c_solver
        try:
            bridge.step5c_solver = lambda _args: FakeSolver()  # type: ignore[assignment]
            values = bridge.compute_step4e_values(
                args,
                [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                {
                    "actual_TCP_pose": [0.43301, 0.10802, 0.03, 0.0, 0.0, 0.0],
                    "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                    "actual_q": [-1.57, -1.20, 1.80, -2.10, -1.57, 0.0],
                    "output_double_register_35": 25.0,
                },
                1.0,
                bridge.Step4EState(),
                0.002,
            )
        finally:
            bridge.step5c_solver = original_solver  # type: ignore[assignment]

        carrier_names = [
            "step4e_cmd_vx_m_s",
            "step4e_cmd_vy_m_s",
            "step4e_cmd_vz_m_s",
            "step4e_cmd_wx_rad_s",
            "step4e_cmd_wy_rad_s",
            "step4e_cmd_wz_rad_s",
        ]
        for idx, carrier_name in enumerate(carrier_names):
            self.assertEqual(values[carrier_name], qdot[idx])
            self.assertEqual(values[f"_step5c_cmd_qd{idx}_rad_s"], qdot[idx])
        self.assertEqual(values["step4e_cmd_valid"], 1.0)
        self.assertEqual(values["step4e_controller_state"], 42.0)
        self.assertEqual(values["_step5c_solver_status"], 42.0)
        self.assertEqual(values["_step5c_qdot_projected"], 1.0)

    def test_strict_rnn_refuses_pending_paper_truth(self) -> None:
        with self.assertRaises(PaperTruthPendingError):
            StrictTaseRnnSolver()

    def test_packages_contain_speedj_joint_register_contract(self) -> None:
        geom = line_cfg(load_json(step5c.CONFIG_PATH))
        dry_frame = step5c.load_step5c_frame(step5c.DRYRUN_STAGE_ID)
        dry_stamp = "2026-06-12T1200HKT_STEP5C_SPEEDJ_DRYRUN_V1"
        dry_script = step5c.dryrun_script(dry_stamp, "2026-06-12T12:00:00+08:00", geom, dry_frame)
        dry_txt = step5c.dryrun_txt(dry_stamp)
        dry_urp = step5c.build_urp(dry_script, step5c.DRYRUN_PROGRAM, step5c.CONTROLLER_DIR)
        step5c.validate_package(step5c.DRYRUN_PROGRAM, dry_script, dry_txt, dry_urp, dry_stamp)

        contact_stamp = "2026-06-12T1200HKT_STEP5C_JOINT_RNN_CYCLOID_V1"
        contact_script = step5c.contact_quarantine_script(contact_stamp, "2026-06-12T12:00:00+08:00")
        contact_txt = step5c.contact_quarantine_txt(contact_stamp)
        contact_urp = step5c.build_urp(contact_script, step5c.CONTACT_PROGRAM, step5c.CONTROLLER_DIR)
        step5c.validate_package(step5c.CONTACT_PROGRAM, contact_script, contact_txt, contact_urp, contact_stamp)

        xml = gzip.decompress(contact_urp).decode("utf-8")
        self.assertIn(f'URProgram name="{step5c.CONTACT_PROGRAM}"', xml)
        self.assertIn("stop_only_quarantine", dry_script)
        self.assertIn("wrong XY/Z direction", dry_txt)
        self.assertNotIn("speedj(", dry_script)
        self.assertNotIn("speedl(", dry_script)
        self.assertNotIn("force_mode(", dry_script)
        self.assertIn("stop_only_quarantine", contact_script)
        self.assertNotIn("speedj(", contact_script)
        self.assertNotIn("speedl(", contact_script)
        self.assertNotIn("force_mode(", contact_script)


if __name__ == "__main__":
    unittest.main()
