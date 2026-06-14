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
        stamp = "2026-06-14T1200HKT_STEP5D_STRICT_RNN_LIVEPREP_V4"
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
        self.assertIn("local qdot_cap_rad_s = 0.300", script)
        self.assertIn("local skip_lift_attitude = 0", script)
        self.assertIn("local orientation_skip_error_rad = 0.069813", script)
        self.assertIn("if stop_reason == 0.0 and skip_lift_attitude == 0:", script)
        self.assertIn("local line_entry_normal_load_min_n = 2.000", script)
        self.assertIn("local line_entry_normal_load_max_n = 15.000", script)
        self.assertIn("local line_entry_force_norm_max_n = 25.000", script)
        self.assertIn("local line_entry_required_s = 0.050", script)
        self.assertIn("local line_entry_timeout_s = 10.000", script)
        self.assertIn("speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0]", script)
        self.assertIn("codex_abs(normal_force) > 100.0", script)
        self.assertIn("force_norm > 100.0", script)
        self.assertNotIn("speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy", script)
        self.assertNotIn("stop_only_quarantine", script + txt)

    def test_bridge_allows_liveprep_profile_but_keeps_full_reproduction_blocked(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--step4e-mode",
                "line",
                "--step4e-version",
                "step5d_strict_rnn_liveprep_v5",
                "--step4e-path-shape",
                "cycloid",
            ]
        )
        self.assertEqual(args.step4e_version, "step5d_strict_rnn_liveprep_v5")
        self.assertEqual(args.step5d_qdot_limit_rad_s, 0.30)
        self.assertFalse(args.disable_dashboard_program_watch)
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

    def test_step5d_operator_points_to_v5_controller_package(self) -> None:
        operator = (ROOT / "scripts" / "step5d-liveprep-operator.sh").read_text(encoding="utf-8")
        base = (ROOT / "scripts" / "step4e-line-v1-operator.sh").read_text(encoding="utf-8")
        self.assertIn('STEP5D_VERSION="${STEP5D_VERSION:-step5d_strict_rnn_liveprep_v5}"', operator)
        self.assertIn('/programs/andyl/kunwei/step5/${STEP5D_VERSION}.urp', operator)
        self.assertIn('MAX_NORMAL_FORCE_N="${MAX_NORMAL_FORCE_N:-100}"', operator)
        self.assertIn('MAX_FORCE_NORM_N="${MAX_FORCE_NORM_N:-100}"', operator)
        self.assertIn('STEP4E_VERSION="step5d_strict_rnn_liveprep_v5"', base)
        self.assertIn('PROGRAM_LINE="/programs/andyl/kunwei/step5/${STEP4E_VERSION}.urp"', base)
        self.assertIn('EXPECTED_BASENAME="${STEP4E_VERSION}.urp"', base)
        self.assertIn('RUN_LABEL="${STEP4E_VERSION}"', base)

    def test_step5d_v4_live_limiter_and_contact_window_gate(self) -> None:
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
        self.assertTrue(bridge.step5d_contact_window_ready(normal_load_n=15.0, force_norm_n=24.9))
        self.assertFalse(bridge.step5d_contact_window_ready(normal_load_n=15.1, force_norm_n=6.0))
        self.assertFalse(bridge.step5d_contact_window_ready(normal_load_n=5.0, force_norm_n=25.1))

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

    def test_full_chain_sanity_has_no_dls_or_live_side_effect_path(self) -> None:
        source = inspect.getsource(sanity).lower()
        forbidden = ["dls", "force_mode", "speedj(", "speedl(", "dashboard_exchange"]
        for token in forbidden:
            self.assertNotIn(token, source)
        self.assertIn("no bridge start", source)
        self.assertIn("no controller upload", source)


if __name__ == "__main__":
    unittest.main()
