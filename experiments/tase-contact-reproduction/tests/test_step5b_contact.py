#!/usr/bin/env python3
"""Offline checks for Step5b contact baseline package and bridge contract."""

from __future__ import annotations

import gzip
import inspect
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
        stamp = "2026-06-12T1200HKT_STEP5B_CONTACT_CYCLOID_BASELINE_V1"
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
        self.assertIn("step4e-version=step5b_v1", script)

    def test_bridge_accepts_step5b_profile_name(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5b_v1",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(args.step4e_version, "step5b_v1")
        self.assertIn('"step5b_v1"', inspect.getsource(bridge.main))

    def test_step5b_source_stamp_has_minute_and_timezone(self) -> None:
        stamp = step5b.source_stamp(datetime(2026, 6, 12, 12, 34, tzinfo=timezone.utc))
        self.assertEqual(stamp, "2026-06-12T1234HKT_STEP5B_CONTACT_CYCLOID_BASELINE_V1")


if __name__ == "__main__":
    unittest.main()
