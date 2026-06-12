#!/usr/bin/env python3
"""Offline checks for Step5c diagnostic DLS and strict-RNN gates."""

from __future__ import annotations

import gzip
import inspect
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5c_joint as step5c  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
from build_step4e_line_programs import line_cfg, load_json  # noqa: E402
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

    def test_bridge_accepts_dryrun_and_blocks_contact_profile(self) -> None:
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
        self.assertIn("STEP5C_DRYRUN_STAGE_ID", source)
        self.assertIn("Blocked Step5c contact", source)

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
        self.assertIn("local qdot_cap_rad_s = 0.200", dry_script)
        self.assertIn("--step5c-qdot-limit-rad-s 0.20", dry_txt)
        self.assertIn("stop_only_quarantine", contact_script)
        self.assertNotIn("speedj(", contact_script)
        self.assertNotIn("speedl(", contact_script)


if __name__ == "__main__":
    unittest.main()
