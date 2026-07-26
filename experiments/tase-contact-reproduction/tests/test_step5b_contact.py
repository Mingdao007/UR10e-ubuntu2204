#!/usr/bin/env python3
"""Offline checks for Step5b contact baseline package and bridge contract."""

from __future__ import annotations

import gzip
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5b_contact as step5b  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
from build_step4e_line_programs import line_cfg, load_json  # noqa: E402


class Step5bContactTest(unittest.TestCase):
    def test_package_contains_step5_table_bridge_contract(self) -> None:
        frame = step5b.load_safe_frame()
        geom = line_cfg(load_json(step5b.CONFIG_PATH))
        stamp = "2026-06-12T1200HKT_STEP5B_CONTACT_CYCLOID_BASELINE_V2"
        script = step5b.build_script(stamp, "2026-06-12T12:00:00+08:00", geom, frame)
        txt = step5b.build_txt(stamp)
        urp = step5b.build_urp(script, step5b.PROGRAM_NAME, step5b.CONTROLLER_DIR)
        step5b.validate_package(script, txt, urp, stamp)

        xml = gzip.decompress(urp).decode("utf-8")
        self.assertIn(f'URProgram name="{step5b.PROGRAM_NAME}"', xml)
        self.assertIn(f'directory="{step5b.CONTROLLER_DIR}"', xml)
        self.assertIn(f"{step5b.CONTROLLER_DIR}/{step5b.PROGRAM_NAME}.script", xml)
        self.assertIn("STEP5_TABLE_SOURCE: config/step5_stage_table.json", script)
        self.assertIn("TP_ROLE: executor_and_guard_only", script)
        self.assertIn("step4e-version=step5b_v2", script)
        self.assertIn("local skip_lift_attitude = 0", script)
        self.assertIn("write_output_float_register(35, 25.15)", script)
        self.assertIn("local orientation_skip_error_rad = 0.069813", script)
        self.assertIn("--target-force-n 15.0", txt)
        self.assertIn("--step4e-normal-filter-alpha 0.70", txt)

    def test_bridge_accepts_step5b_profile_name(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5b_v2",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(args.step4e_version, "step5b_v2")
        self.assertIn("step5b_v2", bridge.STEP5B_BRIDGE_PROFILES)
        args_v3 = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5b_v3",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(args_v3.step4e_version, "step5b_v3")
        self.assertIn("step5b_v3", bridge.STEP5B_BRIDGE_PROFILES)

    def test_step5b_source_stamp_has_minute_and_timezone(self) -> None:
        stamp = step5b.source_stamp(datetime(2026, 6, 12, 12, 34, tzinfo=timezone.utc))
        self.assertEqual(stamp, "2026-06-12T1234HKT_STEP5B_CONTACT_CYCLOID_BASELINE_V2")
        stamp_v3 = step5b.source_stamp(datetime(2026, 6, 12, 12, 34, tzinfo=timezone.utc), variant="v3")
        self.assertEqual(stamp_v3, "2026-06-12T1234HKT_STEP5B_CONTACT_CYCLOID_BASELINE_V3")

    def test_v3_removes_lift_attitude_and_second_search(self) -> None:
        frame = step5b.load_safe_frame()
        geom = line_cfg(load_json(step5b.CONFIG_PATH))
        stamp = "2026-06-12T1200HKT_STEP5B_CONTACT_CYCLOID_BASELINE_V3"
        script = step5b.build_script(stamp, "2026-06-12T12:00:00+08:00", geom, frame, variant="v3")
        txt = step5b.build_txt(stamp, variant="v3")
        urp = step5b.build_urp(script, step5b.PROGRAM_NAME_V3, step5b.CONTROLLER_DIR)
        step5b.validate_package(script, txt, urp, stamp, variant="v3")

        self.assertIn("step4e-version=step5b_v3", script)
        self.assertIn("write_output_float_register(35, 25.15)", script)
        self.assertNotIn("write_output_float_register(35, 25.1)", script)
        self.assertNotIn("write_output_float_register(35, 25.2)", script)
        self.assertNotIn("codex_step5b_down_search(24.3, 24.4", script)
        self.assertNotIn("local skip_lift_attitude = 0", script)
        self.assertIn("--target-force-n 12.0", txt)
        self.assertIn("--step4e-normal-filter-alpha 0.55", txt)


if __name__ == "__main__":
    unittest.main()
