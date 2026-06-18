#!/usr/bin/env python3
"""Offline tests for the Step5b contact-core replay validator."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5b_contact_core_replay as replay  # noqa: E402


class Step5bContactCoreReplayTest(unittest.TestCase):
    def write_metadata(self, directory: Path) -> None:
        (directory / "metadata.json").write_text(
            json.dumps(
                {
                    "args": {
                        "bridge_mode": "line",
                        "bridge_profile": "step5b_v1",
                        "bridge_path_shape": "cycloid",
                        "rtde_hz": 500.0,
                        "target_force_n": 5.0,
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def write_one_row_csv(self, path: Path, *, cmd_valid: float = 1.0, omit: set[str] | None = None) -> None:
        omit = omit or set()
        fields = [
            "t_monotonic_s",
            "sensor_ok",
            "fx_n_zeroed",
            "fy_n_zeroed",
            "fz_n_zeroed",
            "mx_nm_zeroed",
            "my_nm_zeroed",
            "mz_nm_zeroed",
            *[f"ur_actual_TCP_pose_{idx}" for idx in range(6)],
            "ur_output_double_register_35",
            *replay.COMMAND_COLUMNS,
        ]
        fields = [field for field in fields if field not in omit]
        row = {
            "t_monotonic_s": "1.0",
            "sensor_ok": "1",
            "fx_n_zeroed": "0",
            "fy_n_zeroed": "0",
            "fz_n_zeroed": "5",
            "mx_nm_zeroed": "0",
            "my_nm_zeroed": "0",
            "mz_nm_zeroed": "0",
            "ur_actual_TCP_pose_0": "0.487795411149049",
            "ur_actual_TCP_pose_1": "0.12932679270060748",
            "ur_actual_TCP_pose_2": "0.02",
            "ur_actual_TCP_pose_3": "0",
            "ur_actual_TCP_pose_4": "0",
            "ur_actual_TCP_pose_5": "0",
            "ur_output_double_register_35": "25.05",
            "step4e_cmd_valid": str(cmd_valid),
            "step4e_cmd_vx_m_s": "0",
            "step4e_cmd_vy_m_s": "0",
            "step4e_cmd_vz_m_s": "0",
            "step4e_cmd_wx_rad_s": "0",
            "step4e_cmd_wy_rad_s": "0",
            "step4e_cmd_wz_rad_s": "0",
        }
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow({field: row[field] for field in fields})

    def test_default_retained_csv_paths_exist(self) -> None:
        self.assertEqual(len(replay.DEFAULT_CSVS), 3)
        for path in replay.DEFAULT_CSVS:
            self.assertTrue(path.is_file(), path)
            self.assertTrue(path.with_name("metadata.json").is_file(), path)

    def test_limited_replay_emits_required_summary_and_trace_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "replay"
            rc = replay.main(
                [
                    "--csv",
                    str(replay.DEFAULT_CSVS[1]),
                    "--output-dir",
                    str(output_dir),
                    "--max-rows-per-csv",
                    "25000",
                ]
            )
            self.assertEqual(rc, 0)
            summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["row_counts"]["mismatch_rows"], 0)
            self.assertGreater(summary["row_counts"]["replayable_rows"], 0)
            self.assertIn("offline analysis only", summary["offline_safety_boundary"])
            self.assertIn(str(replay.DEFAULT_CSVS[1].resolve()), summary["parameter_source"])
            with (output_dir / "replay_trace.csv").open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, replay.TRACE_FIELDS)

    def test_missing_metadata_fails_without_explicit_default_allowance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "bridge_rtde_500hz.csv"
            self.write_one_row_csv(csv_path)
            with self.assertRaisesRegex(RuntimeError, "missing metadata"):
                replay.main(["--csv", str(csv_path), "--output-dir", str(root / "out")])
            rc = replay.main(
                [
                    "--csv",
                    str(csv_path),
                    "--output-dir",
                    str(root / "out_allowed"),
                    "--allow-default-params",
                ]
            )
            self.assertEqual(rc, 0)

    def test_missing_command_columns_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_metadata(root)
            csv_path = root / "bridge_rtde_500hz.csv"
            self.write_one_row_csv(csv_path, omit={"step4e_cmd_vx_m_s"})
            with self.assertRaisesRegex(RuntimeError, "missing command columns"):
                replay.main(["--csv", str(csv_path), "--output-dir", str(root / "out")])

    def test_cmd_valid_mismatch_fails_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_metadata(root)
            csv_path = root / "bridge_rtde_500hz.csv"
            self.write_one_row_csv(csv_path, cmd_valid=0.0)
            output_dir = root / "out"
            rc = replay.main(["--csv", str(csv_path), "--output-dir", str(output_dir)])
            self.assertEqual(rc, 1)
            summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["ok"])
            self.assertEqual(summary["row_counts"]["mismatch_rows"], 1)


if __name__ == "__main__":
    unittest.main()
