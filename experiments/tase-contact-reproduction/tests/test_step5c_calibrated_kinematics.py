#!/usr/bin/env python3
"""Offline tests for the Step5c calibrated-kinematics audit."""

from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5c_calibrated_kinematics_audit as audit  # noqa: E402


class Step5cCalibratedKinematicsTest(unittest.TestCase):
    def test_calibrated_urdf_loads_in_pinocchio(self) -> None:
        model = audit.build_calibrated_model()
        self.assertEqual(model.calibration_hash, audit.EXPECTED_CALIBRATION_HASH)
        self.assertEqual(model.model.nq, 6)
        self.assertEqual(model.model.nv, 6)
        frames = {frame.name for frame in model.model.frames}
        for name in ("base", "tool0", "flange"):
            self.assertIn(name, frames)

    def test_failed_step5c_run_aligns_with_calibrated_fk_and_jacobian(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            summary = audit.audit(output_dir=Path(tmpdir), make_plots=False)
        self.assertTrue(summary["overall_pass"])

        tcp_offset = summary["inferred_tcp_offset_tool0"]
        self.assertAlmostEqual(tcp_offset["norm_m"], 0.122099, delta=0.00025)
        self.assertLess(max(tcp_offset["std_xyz_m"]), 0.00025)

        residual = summary["jacobian_tcp_speed_residual"]
        self.assertLess(residual["linear_vector_rms_m_s"], 1e-5)
        self.assertLess(residual["angular_vector_rms_rad_s"], 1e-5)

        old_failure = summary["old_mujoco_nominal_failure"]
        self.assertIn("ur10e_nominal.xml", old_failure["old_joint_model"])
        self.assertIn("quarantined", old_failure["known_failure"])


if __name__ == "__main__":
    unittest.main()
