#!/usr/bin/env python3
"""Tests for the Step5d Stage25.0 register semantic gate."""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import verify_step5d_register_semantic_gate as gate  # noqa: E402


FIELDS = [
    "write_index",
    "ur_output_double_register_35",
    *[f"ur_output_double_register_{idx}" for idx in range(36, 42)],
    "ur_output_double_register_46",
]


def write_csv(path: Path, rows: list[dict[str, float | int]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class Step5dRegisterSemanticGateTest(unittest.TestCase):
    def test_clean_stage25_qdot_echo_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "clean.csv"
            write_csv(
                csv_path,
                [
                    {
                        "write_index": 1,
                        "ur_output_double_register_35": 25.0,
                        "ur_output_double_register_36": 0.01,
                        "ur_output_double_register_37": -0.01,
                        "ur_output_double_register_38": 0.0,
                        "ur_output_double_register_39": 0.0,
                        "ur_output_double_register_40": 0.0,
                        "ur_output_double_register_41": 0.0,
                        "ur_output_double_register_46": 522.0,
                    }
                ],
            )

            result = gate.verify_csv(csv_path)

        self.assertTrue(result["ok"])
        self.assertEqual(result["checked_stage25_rows"][0]["layout_echo"], 522.0)

    def test_stale_preload_layout_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "stale.csv"
            write_csv(
                csv_path,
                [
                    {
                        "write_index": 1,
                        "ur_output_double_register_35": 25.0,
                        "ur_output_double_register_36": 0.0,
                        "ur_output_double_register_37": 0.0,
                        "ur_output_double_register_38": 0.0,
                        "ur_output_double_register_39": 7.5,
                        "ur_output_double_register_40": 14.0,
                        "ur_output_double_register_41": 25.0,
                        "ur_output_double_register_46": 521.0,
                    }
                ],
            )

            with self.assertRaisesRegex(RuntimeError, "stale preload-layout echo"):
                gate.verify_csv(csv_path)


if __name__ == "__main__":
    unittest.main()
