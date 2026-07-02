#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "build_step5b_diagnostic_overview.py"


def import_tool():
    spec = importlib.util.spec_from_file_location("build_step5b_diagnostic_overview", TOOL_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot import {TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_full_fixture(run_dir: Path) -> None:
    rows = []
    for i in range(240):
        t = i * 0.002
        if i < 30:
            stage = 24.0
        elif i < 210:
            stage = 25.0
        else:
            stage = 26.0
        phase = max(0, min(i - 30, 179)) / 179.0
        x_ref = 0.45 + 0.03 * phase
        y_ref = 0.12 + 0.005 * math.sin(phase * math.pi)
        x_actual = x_ref + 0.0002 * math.sin(i / 7.0)
        y_actual = y_ref - 0.0001 * math.cos(i / 9.0)
        cmd_vx = 0.003 if stage == 25.0 else 0.0
        cmd_vy = 0.0005 * math.sin(i / 8.0) if stage == 25.0 else 0.0
        cmd_vz = -0.00025 if stage == 25.0 else 0.0
        cmd_wx = 0.018 * math.sin(i / 13.0) if stage == 25.0 else 0.0
        cmd_wy = 0.012 * math.cos(i / 11.0) if stage == 25.0 else 0.0
        normal_load = 15.0 + 1.5 * math.sin(i / 12.0) if stage == 25.0 else 0.5
        row = {
            "write_index": i,
            "t_monotonic_s": 100.0 + t,
            "sensor_age_s": 0.004 + 0.0002 * math.sin(i / 5.0),
            "normal_force_n": -normal_load,
            "force_norm_n": abs(normal_load) + 0.2,
            "heartbeat": i,
            "sensor_ok": 1,
            "stop_request": 0,
            "target_force_n": 15.0,
            "torque_norm_nm": 0.08 + 0.01 * math.sin(i / 20.0),
            "fx_n_zeroed": 0.1,
            "fy_n_zeroed": -0.2,
            "fz_n_zeroed": -normal_load,
            "step4e_cmd_vx_m_s": cmd_vx,
            "step4e_cmd_vy_m_s": cmd_vy,
            "step4e_cmd_vz_m_s": cmd_vz,
            "step4e_cmd_wx_rad_s": cmd_wx,
            "step4e_cmd_wy_rad_s": cmd_wy,
            "step4e_cmd_wz_rad_s": 0.0,
            "step4e_cmd_valid": 1 if stage == 25.0 else 0,
            "step4e_progress_m": phase,
            "step4e_force_error_n": 15.0 - normal_load,
            "step4e_orientation_error_rad": 0.02 + 0.005 * math.sin(i / 10.0),
            "step4e_controller_state": 1,
            "guard_reason": "",
            "baseline_ready": 1,
            "rtde_connected": 1,
            "_step4e_control_normal_b_x": 0.0,
            "_step4e_control_normal_b_y": 0.0,
            "_step4e_control_normal_b_z": 1.0,
            "_step4e_live_normal_angle_from_latch_rad": 0.01 * math.sin(i / 19.0),
            "_step4e_normal_filter_source": "filtered_live",
            "_step4e_normal_load_n": normal_load,
            "_step4e_normal_force_error_n": 15.0 - normal_load,
            "_step4e_path_shape": "cycloid",
            "_step4e_path_time_s": max(0.0, t - 0.06),
            "_step4e_desired_x_m": x_ref,
            "_step4e_desired_y_m": y_ref,
            "_step4e_desired_vx_m_s": cmd_vx,
            "_step4e_desired_vy_m_s": cmd_vy,
            "_step4e_path_error_x_m": x_actual - x_ref,
            "_step4e_path_error_y_m": y_actual - y_ref,
            "_step4e_actual_speed_norm_m_s": abs(cmd_vx) + 0.0003,
            "ur_actual_TCP_pose_0": x_actual,
            "ur_actual_TCP_pose_1": y_actual,
            "ur_actual_TCP_pose_2": 0.03,
            "ur_actual_TCP_pose_3": 2.1,
            "ur_actual_TCP_pose_4": -2.0,
            "ur_actual_TCP_pose_5": 0.0,
            "ur_actual_TCP_speed_0": cmd_vx,
            "ur_actual_TCP_speed_1": cmd_vy,
            "ur_actual_TCP_speed_2": cmd_vz,
            "ur_actual_TCP_speed_3": cmd_wx,
            "ur_actual_TCP_speed_4": cmd_wy,
            "ur_actual_TCP_speed_5": 0.0,
            "ur_output_double_register_35": stage,
        }
        rows.append(row)
    pd.DataFrame(rows).to_csv(run_dir / "bridge_rtde_500hz.csv", index=False)
    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "args": {
                    "target_force_n": 15.0,
                    "step4e_angular_limit_rad_s": 0.15,
                    "step4e_total_linear_limit_m_s": 0.004,
                    "step4e_normal_velocity_limit_m_s": 0.01,
                    "max_force_norm_n": 60.0,
                    "max_torque_norm_nm": 3.0,
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "stop_reason": "fixture_complete",
                "parse_errors": 0,
                "rtde_reconnect_event_count": 0,
                "rtde_output_timing": {"rate_hz": 500.0},
                "bridge_write_timing": {"rate_hz": 500.0},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (run_dir / "stage_frequency_summary.json").write_text(
        json.dumps({"stage25_ft_line_control_echo_rate": {"rtde_rows": 180}}, indent=2),
        encoding="utf-8",
    )


class Step5bDiagnosticOverviewTest(unittest.TestCase):
    def test_builds_one_big_png_and_summary_from_full_fixture(self) -> None:
        tool = import_tool()
        with tempfile.TemporaryDirectory(prefix="step5b_diag_full_") as tmp:
            run_dir = Path(tmp)
            write_full_fixture(run_dir)
            payload = tool.build_figure(run_dir, max_points=200)

            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["schema"], "step5b_diagnostic_overview_v1")
            self.assertTrue((run_dir / "step5b_diagnostic_overview.png").is_file())
            self.assertTrue((run_dir / "step5b_diagnostic_overview_summary.json").is_file())
            self.assertEqual(payload["active_window"]["rows"], 180)
            self.assertTrue(payload["panels"]["TCP XY actual vs reference"]["supported"])
            self.assertTrue(payload["panels"]["Limit usage ratios"]["supported"])
            self.assertEqual(payload["mac_transfer"]["attempted"], False)

    def test_missing_columns_still_emit_png_and_list_unsupported_panels(self) -> None:
        tool = import_tool()
        with tempfile.TemporaryDirectory(prefix="step5b_diag_missing_") as tmp:
            run_dir = Path(tmp)
            pd.DataFrame(
                {
                    "t_monotonic_s": [10.0, 10.002, 10.004],
                    "ur_output_double_register_35": [25.0, 25.0, 25.0],
                    "_step4e_normal_load_n": [14.0, 15.0, 16.0],
                    "target_force_n": [15.0, 15.0, 15.0],
                }
            ).to_csv(run_dir / "bridge_rtde_500hz.csv", index=False)
            payload = tool.build_figure(run_dir, max_points=200)

            self.assertTrue(payload["ok"], payload)
            self.assertTrue((run_dir / "step5b_diagnostic_overview.png").is_file())
            self.assertFalse(payload["panels"]["Raw signed Fz tracking"]["supported"])
            self.assertTrue(payload["panels"]["Projected normal load"]["supported"])

    def test_mac_transfer_is_suppressed_when_run_did_not_complete(self) -> None:
        tool = import_tool()
        with tempfile.TemporaryDirectory(prefix="step5b_diag_incomplete_") as tmp:
            run_dir = Path(tmp)
            write_full_fixture(run_dir)
            (run_dir / "summary.json").write_text(
                json.dumps(
                    {
                        "stop_reason": "step5d_contact_safety:hold_duty_limit",
                        "parse_errors": 0,
                        "rtde_reconnect_event_count": 0,
                        "rtde_output_timing": {"rate_hz": 500.0},
                        "bridge_write_timing": {"rate_hz": 500.0},
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            rc = tool.main([str(run_dir), "--mac-target", "example.invalid:/tmp/step5b_plots"])

            self.assertEqual(rc, 0)
            payload = json.loads((run_dir / "step5b_diagnostic_overview_summary.json").read_text(encoding="utf-8"))
            self.assertFalse(payload["completed_run"])
            self.assertEqual(payload["mac_transfer"]["attempted"], False)
            self.assertTrue(payload["mac_transfer"]["skipped"])
            self.assertEqual(payload["mac_transfer"]["bridge_stop_reason"], "step5d_contact_safety:hold_duty_limit")

    def test_operator_postprocess_hooks_step5b_v2_and_v3(self) -> None:
        operator = (ROOT / "scripts" / "step4e-line-v1-operator.sh").read_text(encoding="utf-8")
        self.assertIn('if [[ "${STEP4E_VERSION}" == "step5b_v2" || "${STEP4E_VERSION}" == "step5b_v3" ]]; then', operator)
        self.assertIn('tools/build_step5b_diagnostic_overview.py', operator)
        self.assertIn('STEP5B_DIAGNOSTIC_SEND_MAC="${STEP5B_DIAGNOSTIC_SEND_MAC:-1}"', operator)
        self.assertIn('STEP5B_DIAGNOSTIC_MAC_TARGET="${STEP5B_DIAGNOSTIC_MAC_TARGET:-andyl@100.127.94.11:/Users/andyl/Downloads/ur10e_step5b_plots/}"', operator)


if __name__ == "__main__":
    unittest.main()
