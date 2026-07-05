#!/usr/bin/env python3
"""Focused v27 ablation tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_liveprep as liveprep  # noqa: E402
import kunwei_rtde_bridge as bridge  # noqa: E402
import step5d_runtime_interface as iface  # noqa: E402
import upload_ur_tp_package as upload  # noqa: E402


V27 = "step5d_strict_rnn_ablation_v27"


class Step5dV27AblationTest(unittest.TestCase):
    def test_bridge_parse_args_recognizes_v27_speedl_wide_tube(self) -> None:
        args = bridge.parse_args(
            [
                "--no-start-command",
                "--skip-dashboard-preflight",
                "--bridge-mode",
                "line",
                "--bridge-profile",
                V27,
                "--bridge-path-shape",
                "cycloid",
            ]
        )

        self.assertEqual(args.bridge_profile, V27)
        self.assertEqual(args.step5d_stage25_control_mode, "speedl_cartesian_oracle")
        self.assertEqual(args.step5d_preload_filtered_min_n, 5.0)
        self.assertEqual(args.step5d_preload_filtered_max_n, 22.0)
        self.assertEqual(args.step5d_preload_raw_min_n, 3.0)
        self.assertEqual(args.step5d_preload_raw_max_n, 25.0)
        self.assertEqual(args.step5d_preload_force_norm_max_n, 35.0)
        self.assertEqual(args.max_normal_force_n, 35.0)
        self.assertEqual(args.max_force_norm_n, 35.0)
        self.assertEqual(args.max_torque_norm_nm, 4.0)

    def test_v27_package_keeps_step5b_scaffold_min_delta_and_consumption_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_dir = Path(tmp) / "v27"
            liveprep.write_outputs(
                "2026-07-06T0100HKT_STEP5D_STRICT_RNN_ABLATION_V27",
                "2026-07-06T01:00:00+08:00",
                output_dir=local_dir,
                local_only=True,
                program=V27,
            )
            files = {ext: local_dir / f"{V27}{ext}" for ext in upload.EXTENSIONS}
            result = upload.validate_package(
                files,
                V27,
                liveprep.CONTROLLER_DIR,
                require_exact_cached_script=True,
            )
            script_text = files[".script"].read_text(encoding="utf-8")
            txt_text = files[".txt"].read_text(encoding="utf-8")

        self.assertEqual(result["program"], V27)
        self.assertEqual(result["target_dir"], liveprep.CONTROLLER_DIR)
        self.assertIn("# STAGE25_V27_SCAFFOLD: step5b_v3_scaffold_min_delta", script_text)
        self.assertIn("STAGE25_CADENCE_CONSUMPTION", script_text)
        self.assertIn("local stage25_command_consumed = 0", script_text)
        self.assertIn("write_output_float_register(47, stage25_command_consumed)", script_text)
        self.assertIn("local line_entry_default_normal_load_min_n = 5.000", script_text)
        self.assertIn("local line_entry_default_normal_load_max_n = 22.000", script_text)
        self.assertIn("local line_entry_default_force_norm_max_n = 35.000", script_text)
        self.assertIn("# SAFETY: raw normal guard 35 N, force norm guard 35 N, torque guard 4.0 Nm.", script_text)
        self.assertIn("v27 starts from the proven Step5b v3 scaffold", txt_text)
        self.assertIn("Stage25.0 cadence/command-consumption instrumentation", txt_text)
        self.assertNotIn("step5d_strict_rnn_ablation_v26", script_text + txt_text)

    def test_bridge_csv_fields_include_v27_stage25_timing_and_consumption(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")

        self.assertIn("STEP5D_ABLATION_V27_STAGE_ID", source)
        self.assertIn("step5d_liveprep_v27_profile = args.bridge_profile == STEP5D_ABLATION_V27_STAGE_ID", source)
        self.assertIn("_step5d_stage25_echo_layout_tag", source)
        self.assertIn("_step5d_stage25_echo_cmd_valid", source)
        self.assertIn("_step5d_stage25_echo_command_norm", source)
        self.assertIn("_step5d_stage25_echo_consumed", source)
        self.assertIn("_step5d_stage25_row_gap_s", source)
        self.assertIn("_bridge_loop_rtde_recv_s", source)
        self.assertIn("_bridge_loop_compute_s", source)
        self.assertIn("_bridge_loop_rtde_send_s", source)
        self.assertIn("_bridge_loop_csv_write_s", source)

    def test_v27_runtime_constant_matches_interface(self) -> None:
        self.assertEqual(iface.STEP5D_ABLATION_V27_STAGE_ID, V27)
        self.assertEqual(liveprep.spec_for(V27).program_name, V27)


if __name__ == "__main__":
    unittest.main()
