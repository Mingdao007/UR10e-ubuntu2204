#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import analyze_step5d_bridge_run  # noqa: E402


FIELDNAMES = [
    "t_monotonic_s",
    "ur_output_double_register_30",
    "ur_output_double_register_35",
    "_step4e_normal_load_n",
    "_step5d_force_settle_filtered_normal_load_n",
    "force_norm_n",
]

V27_MAIN_RUN_ID = "bridge_step5d_strict_rnn_ablation_v27_20260706_024815"
V27_CORROBORATION_RUN_ID = "bridge_step5d_strict_rnn_ablation_v27_20260706_025857"
V27_STARTUP_FAILURE_RUN_ID = "bridge_step5d_strict_rnn_ablation_v27_20260706_020732"
V27_SHADOW_EXPERIMENT_RUN_ID = "bridge_step5d_strict_rnn_ablation_v27_20260706_033032"
V27_FORCE_OVERSHOOT_RUN_ID = "bridge_step5d_strict_rnn_ablation_v27_20260706_040900"
V27_FIX_VALIDATION_SUCCESS_RUN_ID = "bridge_step5d_strict_rnn_ablation_v27_20260706_045513"


def write_bridge_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str] | None = None) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_v27_stage25_slice(
    run_dir: Path,
    *,
    row_count: int,
    consumed_rows: int,
    entry_orientation_error_rad: float,
    max_gap_s: float,
    terminal_reason: str = "force_norm_hard_stop",
    low_load_reason: str = "v25_speedl_low_load_repress_window",
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    csv_path = run_dir / "bridge_rtde_500hz.csv"
    fieldnames = [
        *FIELDNAMES,
        "_step5d_stage25_echo_consumed",
        "_step5d_stage25_echo_layout_tag",
        "_step5d_stage25_echo_cmd_valid",
        "_step5d_stage25_echo_command_norm",
        "_step5d_contact_safety_reason",
        "_step4e_normal_filter_source",
        "step4e_orientation_error_rad",
        "_step5d_outer_orientation_error_rad",
        "step4e_cmd_vz_m_s",
        "step4e_cmd_wx_rad_s",
        "step4e_cmd_wy_rad_s",
        "step4e_cmd_wz_rad_s",
        "_step4e_live_normal_candidate_angle_rad",
        "_step4e_live_normal_angle_from_latch_rad",
    ]
    unconsumed_rows = row_count - consumed_rows
    low_load_start = max(0, row_count - 52)
    rows: list[dict[str, str]] = []
    t_s = 1.0
    for idx in range(row_count):
        if idx == row_count // 2:
            t_s += max_gap_s
        elif idx > 0:
            t_s += 0.0020
        consumed = "0" if idx < unconsumed_rows else "1"
        if idx == row_count - 1 and terminal_reason:
            reason = terminal_reason
        elif idx >= low_load_start and low_load_reason:
            reason = low_load_reason
        else:
            reason = "ok"
        if idx < low_load_start:
            load = 12.0 - 0.01 * idx
        elif idx == low_load_start:
            load = 1.3
        else:
            load = min(26.0, 1.3 + 0.35 * (idx - low_load_start))
        force_norm = 26.8 if idx == row_count - 1 and terminal_reason == "force_norm_hard_stop" else load
        rows.append(
            {
                "t_monotonic_s": f"{t_s:.6f}",
                "ur_output_double_register_30": "11" if idx == row_count - 1 and terminal_reason else "0",
                "ur_output_double_register_35": "25.0",
                "_step4e_normal_load_n": f"{load:.6f}",
                "_step5d_force_settle_filtered_normal_load_n": f"{load:.6f}",
                "force_norm_n": f"{force_norm:.6f}",
                "_step5d_stage25_echo_consumed": consumed,
                "_step5d_stage25_echo_layout_tag": "523",
                "_step5d_stage25_echo_cmd_valid": "1",
                "_step5d_stage25_echo_command_norm": "0.004",
                "_step5d_contact_safety_reason": reason,
                "_step4e_normal_filter_source": "filtered_live" if idx >= 74 else "v18_v20_locked_normal_settle",
                "step4e_orientation_error_rad": f"{entry_orientation_error_rad:.9f}",
                "_step5d_outer_orientation_error_rad": f"{entry_orientation_error_rad:.9f}",
                "step4e_cmd_vz_m_s": "-0.002",
                "step4e_cmd_wx_rad_s": "0.00725",
                "step4e_cmd_wy_rad_s": "-0.01313",
                "step4e_cmd_wz_rad_s": "0.0",
                "_step4e_live_normal_candidate_angle_rad": "0.084",
                "_step4e_live_normal_angle_from_latch_rad": "0.086",
            }
        )
    write_bridge_csv(csv_path, rows, fieldnames=fieldnames)


def write_v27_033032_shadow_experiment_slice(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    csv_path = run_dir / "bridge_rtde_500hz.csv"
    fieldnames = [
        *FIELDNAMES,
        "_step5d_stage25_echo_consumed",
        "_step5d_stage25_echo_layout_tag",
        "_step5d_stage25_echo_cmd_valid",
        "_step5d_stage25_echo_command_norm",
        "_step5d_contact_safety_reason",
        "_step4e_normal_filter_source",
        "step4e_orientation_error_rad",
        "_step5d_outer_orientation_error_rad",
        "step4e_cmd_vx_m_s",
        "step4e_cmd_vy_m_s",
        "step4e_cmd_vz_m_s",
        "step4e_cmd_wx_rad_s",
        "step4e_cmd_wy_rad_s",
        "step4e_cmd_wz_rad_s",
        "_step5d_outer_xdot_limited_approach_normal_m_s",
        "_step4e_live_normal_candidate_angle_rad",
        "_step4e_live_normal_angle_from_latch_rad",
    ]
    rows: list[dict[str, str]] = []
    row_count = 281
    consumed_rows = 278
    unconsumed_rows = row_count - consumed_rows
    for idx in range(row_count):
        t_s = 1.0 + idx * 0.002
        entry_hold = idx < 76
        low_load = 231 <= idx < 280
        terminal = idx == 280
        if entry_hold:
            load = 11.4 + 1.3 * (idx / 75.0)
            angular = (0.0, 0.0, 0.0)
            linear = (0.0003, 0.0002, -0.0004)
            filter_source = "v18_v20_locked_normal_settle"
            normal_lag = 0.110
            reason = "ok"
        elif low_load or terminal:
            load = 0.002 if terminal else max(0.002, 1.3 - 0.03 * (idx - 231))
            angular = (0.00725, -0.01313, 0.0)
            linear = (0.0007, 0.0001, -0.0039)
            filter_source = "freeze_low_force"
            normal_lag = 1.788
            reason = "v25_speedl_hard_low_load_timeout" if terminal else "v25_speedl_low_load_repress_window"
        else:
            peak = 16.3 - abs(idx - 150) * 0.035
            load = max(5.8, peak)
            angular = (0.00725, -0.01313, 0.0)
            linear = (0.0005, 0.0003, -0.0030)
            filter_source = "filtered_live"
            normal_lag = 0.090
            reason = "ok"
        rows.append(
            {
                "t_monotonic_s": f"{t_s:.6f}",
                "ur_output_double_register_30": "11" if terminal else "0",
                "ur_output_double_register_35": "25.0",
                "_step4e_normal_load_n": f"{load:.9f}",
                "_step5d_force_settle_filtered_normal_load_n": f"{load:.9f}",
                "force_norm_n": f"{max(load, 0.0):.9f}",
                "_step5d_stage25_echo_consumed": "0" if idx < unconsumed_rows else "1",
                "_step5d_stage25_echo_layout_tag": "523",
                "_step5d_stage25_echo_cmd_valid": "1",
                "_step5d_stage25_echo_command_norm": "0.004",
                "_step5d_contact_safety_reason": reason,
                "_step4e_normal_filter_source": filter_source,
                "step4e_orientation_error_rad": "0.132466117",
                "_step5d_outer_orientation_error_rad": f"{0.132466117 - 0.00002 * idx:.9f}",
                "step4e_cmd_vx_m_s": f"{linear[0]:.9f}",
                "step4e_cmd_vy_m_s": f"{linear[1]:.9f}",
                "step4e_cmd_vz_m_s": f"{linear[2]:.9f}",
                "step4e_cmd_wx_rad_s": f"{angular[0]:.9f}",
                "step4e_cmd_wy_rad_s": f"{angular[1]:.9f}",
                "step4e_cmd_wz_rad_s": f"{angular[2]:.9f}",
                "_step5d_outer_xdot_limited_approach_normal_m_s": f"{linear[2]:.9f}",
                "_step4e_live_normal_candidate_angle_rad": f"{normal_lag:.9f}",
                "_step4e_live_normal_angle_from_latch_rad": f"{normal_lag:.9f}",
            }
        )
    write_bridge_csv(csv_path, rows, fieldnames=fieldnames)


def write_v27_040900_force_overshoot_slice(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    csv_path = run_dir / "bridge_rtde_500hz.csv"
    fieldnames = [
        *FIELDNAMES,
        "_step5d_stage25_echo_consumed",
        "_step5d_stage25_echo_layout_tag",
        "_step5d_stage25_echo_cmd_valid",
        "_step5d_stage25_echo_command_norm",
        "_step5d_contact_safety_reason",
        "_step4e_normal_filter_source",
        "step4e_orientation_error_rad",
        "_step5d_outer_orientation_error_rad",
        "step4e_cmd_vx_m_s",
        "step4e_cmd_vy_m_s",
        "step4e_cmd_vz_m_s",
        "step4e_cmd_wx_rad_s",
        "step4e_cmd_wy_rad_s",
        "step4e_cmd_wz_rad_s",
        "_step5d_outer_xdot_limited_approach_normal_m_s",
        "_step5d_speedl_orientation_shadow_only",
        "_step5d_speedl_shadow_raw_wx_rad_s",
        "_step5d_speedl_shadow_raw_wy_rad_s",
        "_step5d_speedl_shadow_raw_wz_rad_s",
    ]
    rows: list[dict[str, str]] = []
    row_count = 146
    consumed_rows = 143
    unconsumed_rows = row_count - consumed_rows
    for idx in range(row_count):
        t_s = 1.0 + idx * 0.002
        if idx < 30:
            load = 13.7 - 1.2 * (idx / 29.0)
            vz = 0.0015
        elif idx < 75:
            load = 12.5 - 5.6 * ((idx - 30) / 44.0)
            vz = 0.0038
        else:
            load = min(29.2, 6.9 + 22.3 * ((idx - 75) / 70.0))
            vz = -0.0040
        terminal = idx == row_count - 1
        rows.append(
            {
                "t_monotonic_s": f"{t_s:.6f}",
                "ur_output_double_register_30": "4" if terminal else "0",
                "ur_output_double_register_35": "25.0",
                "_step4e_normal_load_n": f"{load:.9f}",
                "_step5d_force_settle_filtered_normal_load_n": f"{load:.9f}",
                "force_norm_n": f"{max(load, 0.0):.9f}",
                "_step5d_stage25_echo_consumed": "0" if idx < unconsumed_rows else "1",
                "_step5d_stage25_echo_layout_tag": "523",
                "_step5d_stage25_echo_cmd_valid": "1",
                "_step5d_stage25_echo_command_norm": "0.004",
                "_step5d_contact_safety_reason": "force_norm_hard_stop" if terminal else "ok",
                "_step4e_normal_filter_source": "filtered_live",
                "step4e_orientation_error_rad": "0.116000000",
                "_step5d_outer_orientation_error_rad": "0.116000000",
                "step4e_cmd_vx_m_s": "0.000000000",
                "step4e_cmd_vy_m_s": "0.000000000",
                "step4e_cmd_vz_m_s": f"{vz:.9f}",
                "step4e_cmd_wx_rad_s": "0.000000000",
                "step4e_cmd_wy_rad_s": "0.000000000",
                "step4e_cmd_wz_rad_s": "0.000000000",
                "_step5d_outer_xdot_limited_approach_normal_m_s": f"{vz:.9f}",
                "_step5d_speedl_orientation_shadow_only": "1",
                "_step5d_speedl_shadow_raw_wx_rad_s": "0.007250000",
                "_step5d_speedl_shadow_raw_wy_rad_s": "-0.013130000",
                "_step5d_speedl_shadow_raw_wz_rad_s": "0.000000000",
            }
        )
    write_bridge_csv(csv_path, rows, fieldnames=fieldnames)


def write_v27_045513_fix_validation_success_slice(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metadata.json").write_text(
        json.dumps(
            {
                "args": {
                    "bridge_profile": "step5d_strict_rnn_ablation_v27",
                    "bridge_angular_limit_rad_s": 0.015,
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    csv_path = run_dir / "bridge_rtde_500hz.csv"
    fieldnames = [
        *FIELDNAMES,
        "_step5d_stage25_echo_consumed",
        "_step5d_stage25_echo_layout_tag",
        "_step5d_stage25_echo_cmd_valid",
        "_step5d_stage25_echo_command_norm",
        "_step5d_contact_safety_reason",
        "_step4e_normal_filter_source",
        "_step5d_live_control_source",
        "step4e_orientation_error_rad",
        "_step5d_outer_orientation_error_rad",
        "step4e_cmd_vx_m_s",
        "step4e_cmd_vy_m_s",
        "step4e_cmd_vz_m_s",
        "step4e_cmd_wx_rad_s",
        "step4e_cmd_wy_rad_s",
        "step4e_cmd_wz_rad_s",
        "_step5d_outer_xdot_limited_approach_normal_m_s",
        "_step5d_speedl_orientation_shadow_only",
        "_step5d_speedl_shadow_raw_vx_m_s",
        "_step5d_speedl_shadow_raw_vy_m_s",
        "_step5d_speedl_shadow_raw_vz_m_s",
        "_step5d_speedl_shadow_raw_wx_rad_s",
        "_step5d_speedl_shadow_raw_wy_rad_s",
        "_step5d_speedl_shadow_raw_wz_rad_s",
    ]
    rows: list[dict[str, str]] = [
        {
            "t_monotonic_s": "1.900000",
            "ur_output_double_register_30": "11",
            "ur_output_double_register_35": "24.2",
            "_step4e_normal_load_n": "12.000000000",
            "_step5d_force_settle_filtered_normal_load_n": "12.000000000",
            "force_norm_n": "12.000000000",
        },
        {
            "t_monotonic_s": "1.950000",
            "ur_output_double_register_30": "0",
            "ur_output_double_register_35": "25.05",
            "_step4e_normal_load_n": "12.000000000",
            "_step5d_force_settle_filtered_normal_load_n": "12.000000000",
            "force_norm_n": "12.000000000",
        },
    ]
    stage25_rows = 5109
    unconsumed_head = 4
    unconsumed_tail = 5
    t_s = 2.0
    for idx in range(stage25_rows):
        if idx == 4922:
            t_s += 0.048837
        elif idx > 0:
            t_s += 0.002
        sawtooth = ((idx % 200) - 100) / 100.0
        load = 12.0 + 0.8 * sawtooth
        consumed = unconsumed_head <= idx < stage25_rows - unconsumed_tail
        rows.append(
            {
                "t_monotonic_s": f"{t_s:.6f}",
                "ur_output_double_register_30": "0",
                "ur_output_double_register_35": "25.0",
                "_step4e_normal_load_n": f"{load:.9f}",
                "_step5d_force_settle_filtered_normal_load_n": f"{load:.9f}",
                "force_norm_n": f"{abs(load):.9f}",
                "_step5d_stage25_echo_consumed": "1" if consumed else "0",
                "_step5d_stage25_echo_layout_tag": "523",
                "_step5d_stage25_echo_cmd_valid": "1",
                "_step5d_stage25_echo_command_norm": "0.0012",
                "_step5d_contact_safety_reason": "ok",
                "_step4e_normal_filter_source": "filtered_live",
                "_step5d_live_control_source": "step5b_speedl_live_step5d_shadow",
                "step4e_orientation_error_rad": "0.116000000",
                "_step5d_outer_orientation_error_rad": "0.116000000",
                "step4e_cmd_vx_m_s": "0.000150000",
                "step4e_cmd_vy_m_s": "-0.000050000",
                "step4e_cmd_vz_m_s": "0.000250000",
                "step4e_cmd_wx_rad_s": "0.000000000",
                "step4e_cmd_wy_rad_s": "0.000000000",
                "step4e_cmd_wz_rad_s": "0.000000000",
                "_step5d_outer_xdot_limited_approach_normal_m_s": "0.000250000",
                "_step5d_speedl_orientation_shadow_only": "1",
                "_step5d_speedl_shadow_raw_vx_m_s": "0.000150000",
                "_step5d_speedl_shadow_raw_vy_m_s": "-0.000050000",
                "_step5d_speedl_shadow_raw_vz_m_s": "0.000250000",
                "_step5d_speedl_shadow_raw_wx_rad_s": "0.007250000",
                "_step5d_speedl_shadow_raw_wy_rad_s": "-0.013130000",
                "_step5d_speedl_shadow_raw_wz_rad_s": "0.000000000",
            }
        )
    rows.append(
        {
            "t_monotonic_s": f"{t_s + 0.010000:.6f}",
            "ur_output_double_register_30": "1",
            "ur_output_double_register_35": "26.0",
            "_step4e_normal_load_n": "12.000000000",
            "_step5d_force_settle_filtered_normal_load_n": "12.000000000",
            "force_norm_n": "12.000000000",
        }
    )
    write_bridge_csv(csv_path, rows, fieldnames=fieldnames)


class Step5dBridgeRunAnalysisTest(unittest.TestCase):
    def test_v25_preload_failure_reports_short_dwell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            csv_path = run_dir / "bridge_rtde_500hz.csv"
            write_bridge_csv(
                csv_path,
                [
                    {
                        "t_monotonic_s": "0.000",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "25.3",
                        "_step4e_normal_load_n": "11.0",
                        "_step5d_force_settle_filtered_normal_load_n": "10.8",
                        "force_norm_n": "11.2",
                    },
                    {
                        "t_monotonic_s": "0.040",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "25.3",
                        "_step4e_normal_load_n": "11.2",
                        "_step5d_force_settle_filtered_normal_load_n": "11.1",
                        "force_norm_n": "11.3",
                    },
                    {
                        "t_monotonic_s": "0.080",
                        "ur_output_double_register_30": "17",
                        "ur_output_double_register_35": "25.3",
                        "_step4e_normal_load_n": "11.3",
                        "_step5d_force_settle_filtered_normal_load_n": "11.2",
                        "force_norm_n": "11.4",
                    },
                ],
            )

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertFalse(analysis["entered_stage25"])
        self.assertEqual(analysis["stage25_rows"], 0)
        self.assertEqual(analysis["stage25_3_rows"], 3)
        self.assertAlmostEqual(analysis["stage25_3_duration_s"], 0.080)
        self.assertAlmostEqual(analysis["longest_preload_gate_dwell_s"], 0.080)
        self.assertEqual(analysis["required_preload_hold_s"], 0.100)
        self.assertEqual(analysis["max_stage25_3_raw_normal_load_n"], 11.3)
        self.assertEqual(analysis["max_stage25_3_force_norm_n"], 11.4)
        self.assertEqual(analysis["first_tp_stop_reason"], 17)
        self.assertEqual(analysis["classification"], "no_stage25_preload_dwell_short")

    def test_entered_stage25_is_not_classified_as_preload_dwell_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            csv_path = run_dir / "bridge_rtde_500hz.csv"
            write_bridge_csv(
                csv_path,
                [
                    {
                        "t_monotonic_s": "1.000",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "25.3",
                        "_step4e_normal_load_n": "10.8",
                        "_step5d_force_settle_filtered_normal_load_n": "10.8",
                        "force_norm_n": "10.9",
                    },
                    {
                        "t_monotonic_s": "1.120",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "25.0",
                        "_step4e_normal_load_n": "11.2",
                        "_step5d_force_settle_filtered_normal_load_n": "11.0",
                        "force_norm_n": "11.3",
                    },
                ],
            )

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertTrue(analysis["entered_stage25"])
        self.assertEqual(analysis["stage25_rows"], 1)
        self.assertEqual(analysis["classification"], "entered_stage25")

    def test_stage25_cadence_and_consumption_gap_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "bridge_step5d_strict_rnn_ablation_v27_20260706_010000"
            run_dir.mkdir()
            csv_path = run_dir / "bridge_rtde_500hz.csv"
            fieldnames = [
                *FIELDNAMES,
                "_step5d_stage25_echo_consumed",
                "_step5d_stage25_echo_layout_tag",
                "_step5d_stage25_echo_cmd_valid",
                "_step5d_stage25_echo_command_norm",
            ]
            write_bridge_csv(
                csv_path,
                [
                    {
                        "t_monotonic_s": "1.000",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "25.0",
                        "_step4e_normal_load_n": "12.0",
                        "_step5d_force_settle_filtered_normal_load_n": "12.0",
                        "force_norm_n": "12.0",
                        "_step5d_stage25_echo_consumed": "1",
                        "_step5d_stage25_echo_layout_tag": "523",
                        "_step5d_stage25_echo_cmd_valid": "1",
                        "_step5d_stage25_echo_command_norm": "0.001",
                    },
                    {
                        "t_monotonic_s": "1.030",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "25.0",
                        "_step4e_normal_load_n": "12.0",
                        "_step5d_force_settle_filtered_normal_load_n": "12.0",
                        "force_norm_n": "12.0",
                        "_step5d_stage25_echo_consumed": "0",
                        "_step5d_stage25_echo_layout_tag": "0",
                        "_step5d_stage25_echo_cmd_valid": "0",
                        "_step5d_stage25_echo_command_norm": "0",
                    },
                ],
                fieldnames=fieldnames,
            )

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertTrue(analysis["entered_stage25"])
        self.assertEqual(analysis["profile"], "step5d_strict_rnn_ablation_v27")
        self.assertAlmostEqual(analysis["stage25_duration_s"], 0.030)
        self.assertAlmostEqual(analysis["stage25_max_row_gap_s"], 0.030)
        self.assertEqual(analysis["stage25_echo_consumed_rows"], 1)
        self.assertFalse(analysis["stage25_cadence_ok"])
        self.assertFalse(analysis["stage25_consumption_ok"])
        self.assertEqual(analysis["classification"], "stage25_cadence_or_consumption_failure")

    def test_v27_near_complete_consumption_and_good_cadence_classifies_as_control_oscillation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / V27_MAIN_RUN_ID
            write_v27_stage25_slice(
                run_dir,
                row_count=266,
                consumed_rows=262,
                entry_orientation_error_rad=0.104,
                max_gap_s=0.0037,
            )

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertTrue(analysis["stage25_cadence_ok"])
        self.assertTrue(analysis["stage25_consumption_ok"])
        self.assertAlmostEqual(analysis["stage25_consumption_ratio"], 262 / 266)
        self.assertFalse(analysis["stage25_consumption_complete"])
        self.assertEqual(analysis["classification"], "stage25_control_force_oscillation/low_load_timeout")
        self.assertNotEqual(analysis["classification"], "stage25_cadence_or_consumption_failure")
        self.assertGreaterEqual(analysis["stage25_control_attribution"]["entry_orientation_error_rad"], 0.103)
        self.assertGreaterEqual(analysis["stage25_control_attribution"]["angular_saturation_ratio"], 0.95)
        self.assertEqual(analysis["stage25_control_attribution"]["control_oscillation_trigger"], "low_load_repress_window")
        self.assertEqual(analysis["stage25_control_attribution"]["terminal_contact_safety_reason"], "force_norm_hard_stop")

    def test_v27_024815_replay_shape_classifies_control_oscillation_not_cadence_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / V27_MAIN_RUN_ID
            write_v27_stage25_slice(
                run_dir,
                row_count=266,
                consumed_rows=262,
                entry_orientation_error_rad=0.103855666,
                max_gap_s=0.003664,
            )

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertTrue(analysis["stage25_cadence_ok"])
        self.assertTrue(analysis["stage25_consumption_ok"])
        self.assertFalse(analysis["stage25_consumption_complete"])
        self.assertEqual(analysis["stage25_rows"], 266)
        self.assertEqual(analysis["stage25_echo_consumed_rows"], 262)
        self.assertLess(analysis["stage25_max_row_gap_s"], 0.004)
        self.assertEqual(analysis["classification"], "stage25_control_force_oscillation/low_load_timeout")
        self.assertAlmostEqual(
            analysis["stage25_control_attribution"]["entry_orientation_error_rad"],
            0.103855666,
            delta=0.000001,
        )
        self.assertGreaterEqual(analysis["stage25_control_attribution"]["angular_saturation_ratio"], 0.99)
        self.assertGreater(analysis["stage25_control_attribution"]["normal_load_rate_abs_max_n_s"], 1000.0)
        self.assertEqual(analysis["stage25_control_attribution"]["control_oscillation_trigger"], "low_load_repress_window")
        self.assertEqual(analysis["stage25_control_attribution"]["terminal_contact_safety_reason"], "force_norm_hard_stop")

    def test_v27_025857_replay_shape_is_same_control_oscillation_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / V27_CORROBORATION_RUN_ID
            write_v27_stage25_slice(
                run_dir,
                row_count=297,
                consumed_rows=294,
                entry_orientation_error_rad=0.1054944,
                max_gap_s=0.004014,
            )

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertTrue(analysis["stage25_cadence_ok"])
        self.assertTrue(analysis["stage25_consumption_ok"])
        self.assertFalse(analysis["stage25_consumption_complete"])
        self.assertEqual(analysis["stage25_rows"], 297)
        self.assertEqual(analysis["stage25_echo_consumed_rows"], 294)
        self.assertLess(analysis["stage25_max_row_gap_s"], 0.005)
        self.assertEqual(analysis["classification"], "stage25_control_force_oscillation/low_load_timeout")
        self.assertAlmostEqual(
            analysis["stage25_control_attribution"]["entry_orientation_error_rad"],
            0.1054944,
            delta=0.000001,
        )
        self.assertGreaterEqual(analysis["stage25_control_attribution"]["angular_saturation_ratio"], 0.99)

    def test_v27_020732_replay_shape_remains_cadence_or_consumption_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / V27_STARTUP_FAILURE_RUN_ID
            write_v27_stage25_slice(
                run_dir,
                row_count=51,
                consumed_rows=0,
                entry_orientation_error_rad=0.101,
                max_gap_s=2.1,
                terminal_reason="",
                low_load_reason="",
            )

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertFalse(analysis["stage25_cadence_ok"])
        self.assertFalse(analysis["stage25_consumption_ok"])
        self.assertFalse(analysis["stage25_consumption_complete"])
        self.assertEqual(analysis["stage25_consumption_ratio"], 0.0)
        self.assertEqual(analysis["classification"], "stage25_cadence_or_consumption_failure")

    def test_v27_033032_shadow_experiment_failed_low_load_timeout_with_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / V27_SHADOW_EXPERIMENT_RUN_ID
            write_v27_033032_shadow_experiment_slice(run_dir)

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertTrue(analysis["stage25_cadence_ok"])
        self.assertTrue(analysis["stage25_consumption_ok"])
        self.assertEqual(analysis["stage25_rows"], 281)
        self.assertEqual(analysis["classification"], "stage25_control_force_oscillation/low_load_timeout")
        self.assertNotEqual(analysis["classification"], "stage25_cadence_or_consumption_failure")
        attribution = analysis["stage25_control_attribution"]
        self.assertEqual(attribution["control_oscillation_trigger"], "hard_low_load_timeout")
        self.assertEqual(attribution["terminal_contact_safety_reason"], "v25_speedl_hard_low_load_timeout")
        self.assertEqual(
            attribution["orientation_shadow_experiment_classification"],
            "stage25_orientation_shadow_experiment_failed_low_load_timeout",
        )
        segments = attribution["stage25_segment_diagnostics"]
        self.assertEqual(segments["entry_hold"]["rows"], 76)
        self.assertAlmostEqual(segments["entry_hold"]["angular_cmd_norm_max_rad_s"], 0.0, places=9)
        self.assertGreater(segments["entry_hold"]["normal_load_min_n"], 11.0)
        self.assertGreater(segments["post_entry_old_behavior"]["angular_cmd_norm_mean_rad_s"], 0.014)
        self.assertEqual(segments["low_load_repress"]["rows"], 49)
        self.assertLess(segments["low_load_repress"]["normal_load_max_n"], 1.4)
        self.assertGreater(segments["low_load_repress"]["normal_filter_lag_angle_abs_max_rad"], 1.7)
        self.assertGreater(segments["loaded"]["normal_load_max_n"], 16.0)

    def test_v27_040900_force_overshoot_points_to_step5b_live_copy_not_orientation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / V27_FORCE_OVERSHOOT_RUN_ID
            write_v27_040900_force_overshoot_slice(run_dir)

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertTrue(analysis["stage25_cadence_ok"])
        self.assertTrue(analysis["stage25_consumption_ok"])
        self.assertEqual(analysis["classification"], "stage25_control_force_oscillation/force_norm_hard_stop")
        self.assertEqual(
            analysis["evidence_classification"],
            "old_v27_paper_outer_linear_live_gain_mismatch_force_norm_hard_stop",
        )
        self.assertIn("Step5b speedl", analysis["next_action"])
        self.assertNotIn("entry orientation command", analysis["next_action"])
        attribution = analysis["stage25_control_attribution"]
        self.assertAlmostEqual(attribution["angular_cmd_norm_max_rad_s"], 0.0, places=9)
        self.assertAlmostEqual(attribution["linear_vz_cmd_abs_max_m_s"], 0.004, places=9)
        self.assertEqual(attribution["control_oscillation_trigger"], "force_norm_hard_stop")
        self.assertEqual(attribution["terminal_contact_safety_reason"], "force_norm_hard_stop")

    def test_v27_045513_success_is_fix_validation_not_cadence_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / V27_FIX_VALIDATION_SUCCESS_RUN_ID
            write_v27_045513_fix_validation_success_slice(run_dir)

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertFalse(analysis["stage25_cadence_ok"])
        self.assertTrue(analysis["stage25_consumption_ok"])
        self.assertGreater(analysis["stage25_max_row_gap_s"], 0.040)
        self.assertGreaterEqual(analysis["stage25_duration_s"], 10.0)
        self.assertEqual(analysis["first_tp_stop_reason"], 11)
        self.assertEqual(analysis["terminal_tp_stop_reason"], 1)
        self.assertEqual(analysis["classification"], "stage25_fix_validation_success")
        self.assertEqual(analysis["fix_validation_status"], "passed_10s_stage25_window")
        self.assertEqual(analysis["reproduction_status"], "pending_60s_step5b_equivalent_run")
        attribution = analysis["stage25_control_attribution"]
        self.assertEqual(
            attribution["live_control_source_counts"],
            {"step5b_speedl_live_step5d_shadow": 5109},
        )
        self.assertAlmostEqual(attribution["angular_cmd_norm_max_rad_s"], 0.0, places=9)
        self.assertIsNone(attribution["control_oscillation_trigger"])

    def test_no_play_or_false_start_without_stage_echo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            csv_path = run_dir / "bridge_rtde_500hz.csv"
            write_bridge_csv(
                csv_path,
                [
                    {
                        "t_monotonic_s": "2.000",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "0",
                        "_step4e_normal_load_n": "0",
                        "_step5d_force_settle_filtered_normal_load_n": "",
                        "force_norm_n": "0.2",
                    }
                ],
            )

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertEqual(analysis["stage25_rows"], 0)
        self.assertEqual(analysis["stage25_3_rows"], 0)
        self.assertEqual(analysis["classification"], "no_tp_play_or_no_stage_echo")

    def test_missing_required_columns_returns_explicit_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            csv_path = run_dir / "bridge_rtde_500hz.csv"
            write_bridge_csv(
                csv_path,
                [{"t_monotonic_s": "0.0", "ur_output_double_register_35": "25.3"}],
                fieldnames=["t_monotonic_s", "ur_output_double_register_35"],
            )

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertFalse(analysis["ok"])
        self.assertEqual(analysis["classification"], "missing_required_columns")
        self.assertIn("force_norm_n", analysis["missing_columns"])

    def test_stage25_3_duration_sums_contiguous_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            csv_path = run_dir / "bridge_rtde_500hz.csv"
            write_bridge_csv(
                csv_path,
                [
                    {
                        "t_monotonic_s": "0.000",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "25.3",
                        "_step4e_normal_load_n": "11.0",
                        "_step5d_force_settle_filtered_normal_load_n": "10.8",
                        "force_norm_n": "11.2",
                    },
                    {
                        "t_monotonic_s": "0.040",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "25.3",
                        "_step4e_normal_load_n": "11.1",
                        "_step5d_force_settle_filtered_normal_load_n": "10.9",
                        "force_norm_n": "11.3",
                    },
                    {
                        "t_monotonic_s": "0.200",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "0",
                        "_step4e_normal_load_n": "0",
                        "_step5d_force_settle_filtered_normal_load_n": "",
                        "force_norm_n": "0.2",
                    },
                    {
                        "t_monotonic_s": "0.500",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "25.3",
                        "_step4e_normal_load_n": "11.2",
                        "_step5d_force_settle_filtered_normal_load_n": "11.0",
                        "force_norm_n": "11.3",
                    },
                    {
                        "t_monotonic_s": "0.540",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "25.3",
                        "_step4e_normal_load_n": "11.3",
                        "_step5d_force_settle_filtered_normal_load_n": "11.1",
                        "force_norm_n": "11.4",
                    },
                ],
            )

            analysis = analyze_step5d_bridge_run.analyze_run_dir(run_dir)

        self.assertEqual(analysis["stage25_3_rows"], 4)
        self.assertAlmostEqual(analysis["stage25_3_duration_s"], 0.080)

    def test_csv_cli_infers_run_metadata_and_writes_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "bridge_step5d_strict_rnn_liveprep_v24_20260705_000000"
            run_dir.mkdir()
            csv_path = run_dir / "bridge_rtde_500hz.csv"
            (run_dir / "metadata.json").write_text(
                json.dumps({"args": {"bridge_profile": "step5d_strict_rnn_liveprep_v24"}}),
                encoding="utf-8",
            )
            write_bridge_csv(
                csv_path,
                [
                    {
                        "t_monotonic_s": "0.000",
                        "ur_output_double_register_30": "0",
                        "ur_output_double_register_35": "25.3",
                        "_step4e_normal_load_n": "8.0",
                        "_step5d_force_settle_filtered_normal_load_n": "8.0",
                        "force_norm_n": "8.0",
                    }
                ],
            )

            completed = subprocess.run(
                [sys.executable, str(ROOT / "tools" / "analyze_step5d_bridge_run.py"), "--csv", str(csv_path), "--json"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            analysis = json.loads((run_dir / "step5d_bridge_analysis.json").read_text(encoding="utf-8"))

        self.assertEqual(analysis["profile"], "step5d_strict_rnn_liveprep_v24")
        self.assertEqual(analysis["run_dir"], str(run_dir))
        self.assertEqual(analysis["preload_gate"]["raw_min_n"], 7.0)


if __name__ == "__main__":
    unittest.main()
