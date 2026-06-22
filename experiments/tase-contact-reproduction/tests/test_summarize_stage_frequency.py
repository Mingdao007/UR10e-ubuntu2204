#!/usr/bin/env python3
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import summarize_stage_frequency  # noqa: E402


class SummarizeStageFrequencyTest(unittest.TestCase):
    def test_stage_rate_entries_include_human_readable_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "bridge.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "t_monotonic_s",
                        "ur_output_double_register_35",
                        "ur_output_double_register_26",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "t_monotonic_s": "0.0",
                        "ur_output_double_register_35": "25.3",
                        "ur_output_double_register_26": "1",
                    }
                )
                writer.writerow(
                    {
                        "t_monotonic_s": "0.1",
                        "ur_output_double_register_35": "25.3",
                        "ur_output_double_register_26": "2",
                    }
                )
                writer.writerow(
                    {
                        "t_monotonic_s": "0.2",
                        "ur_output_double_register_35": "25.0",
                        "ur_output_double_register_26": "3",
                    }
                )
                writer.writerow(
                    {
                        "t_monotonic_s": "0.3",
                        "ur_output_double_register_35": "25.0",
                        "ur_output_double_register_26": "4",
                    }
                )
            summary = summarize_stage_frequency.summarize(csv_path)

        line_entry = summary["stage25_3_force_acquire_echo_rate"]
        self.assertEqual(line_entry["stage_slug"], "line_entry_gate")
        self.assertEqual(line_entry["stage_title"], "line entry gate")
        self.assertEqual(line_entry["rtde_rows"], 2)
        line_control = summary["stage25_ft_line_control_echo_rate"]
        self.assertEqual(line_control["stage_slug"], "contact_line_control")
        self.assertEqual(line_control["stage_title"], "contact line control")
        self.assertEqual(line_control["rtde_rows"], 2)


if __name__ == "__main__":
    unittest.main()
