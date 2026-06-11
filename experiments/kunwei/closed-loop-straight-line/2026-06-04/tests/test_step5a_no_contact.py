#!/usr/bin/env python3
"""Offline checks for Step5a no-contact cycloid package geometry."""

from __future__ import annotations

import gzip
import json
import math
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5a_no_contact as step5a  # noqa: E402


class Step5aNoContactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.frame = step5a.load_safe_frame()
        self.metrics = step5a.build_metrics(self.frame)

    def test_cycloid_no_scale_geometry_and_guard(self) -> None:
        self.assertEqual(self.metrics["path"]["duration_s"], 22.0)
        self.assertAlmostEqual(self.metrics["path"]["final_phase_rad"], 6.0, places=12)
        self.assertEqual(self.metrics["fixed_z"]["fixed_base_z_m"], 0.029423891)
        self.assertLessEqual(self.metrics["envelope"]["base_x_max_m"], 0.4888784335)
        self.assertLessEqual(self.metrics["path"]["max_reference_speed_m_s"], 0.009)
        self.assertLessEqual(self.metrics["physical_path_gate"]["max_target_residual_mm"], 1e-6)

        end = self.metrics["points"]["end"]
        self.assertAlmostEqual(end["local_x_m"], 0.015 * (6.0 - math.sin(6.0)), places=12)
        self.assertAlmostEqual(end["local_y_m"], 0.015 * (1.0 - math.cos(6.0)), places=12)

    def test_script_has_no_contact_policy(self) -> None:
        geom = step5a.line_cfg(step5a.load_json(step5a.CONFIG_PATH))
        script = step5a.build_script("TEST_STAMP", "2026-06-12T00:00:00+08:00", geom, self.metrics)
        self.assertIn("Step5a cycloid no-contact", script)
        self.assertIn("local fixed_base_z_m = 0.029423891", script)
        self.assertIn("local path_duration_s = 22.000", script)
        self.assertIn("local warmup_hold_s = 0.008", script)
        self.assertIn("local fast_hold_s = 0.001", script)
        self.assertIn("local fast_after_s = 0.100", script)
        self.assertIn("if t >= fast_after_s:", script)
        self.assertIn("PHYSICAL_PATH_GATE", script)
        self.assertIn("local velocity_cap_m_s = 0.009", script)
        self.assertIn("x=0.015(0.272727t-sin(0.272727t))", script)
        self.assertIn("no Kunwei/bridge requirement", script)
        self.assertNotIn("read_input_float_register", script)
        self.assertNotIn("force_mode", script)
        self.assertNotIn("zero_ftsensor()", script.replace("no zero_ftsensor()", ""))
        self.assertNotIn("set_tcp", script)
        self.assertNotIn("set_payload", script)

    def test_urp_cached_contents_matches_script(self) -> None:
        geom = step5a.line_cfg(step5a.load_json(step5a.CONFIG_PATH))
        script = step5a.build_script("TEST_STAMP", "2026-06-12T00:00:00+08:00", geom, self.metrics)
        txt = step5a.build_txt("TEST_STAMP", self.metrics)
        urp = step5a.build_urp(script, step5a.PROGRAM_NAME, step5a.CONTROLLER_DIR)
        step5a.validate_package(script, txt, urp, "TEST_STAMP", self.metrics)
        xml = gzip.decompress(urp).decode("utf-8")
        self.assertIn(f'URProgram name="{step5a.PROGRAM_NAME}"', xml)
        self.assertIn(f'directory="{step5a.CONTROLLER_DIR}"', xml)
        self.assertIn(f"{step5a.CONTROLLER_DIR}/{step5a.PROGRAM_NAME}.script", xml)
        self.assertIn("TEST_STAMP", xml)


if __name__ == "__main__":
    unittest.main()
