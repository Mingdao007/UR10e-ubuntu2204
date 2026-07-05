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


def write_bridge_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str] | None = None) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


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
