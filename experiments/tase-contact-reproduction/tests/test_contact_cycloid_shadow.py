#!/usr/bin/env python3
"""Focused tests for the Step5b ROS2 remote-control plumbing shadow."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers.contact_cycloid_shadow import (  # noqa: E402
    DEFAULT_CONFIG,
    TRACE_FIELDS,
    load_contact_cycloid_config,
    run_contact_cycloid_shadow,
)


RUNS = ROOT / "runs"
_HISTORICAL_STEP5B_CSVS = [
    RUNS / "bridge_step5b_contact_cycloid_baseline_v1_20260612_082352" / "bridge_rtde_500hz.csv",
    RUNS / "bridge_step5b_contact_cycloid_baseline_v1_20260614_222058" / "bridge_rtde_500hz.csv",
    RUNS / "bridge_step5b_contact_cycloid_baseline_v1_20260614_222309" / "bridge_rtde_500hz.csv",
]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "step5b_contact_cycloid"
_FIXTURE_STEP5B_CSVS = [
    FIXTURE_ROOT / "bridge_step5b_contact_cycloid_baseline_v1_20260612_082352" / "bridge_rtde_500hz.csv.gz",
    FIXTURE_ROOT / "bridge_step5b_contact_cycloid_baseline_v1_20260614_222058" / "bridge_rtde_500hz.csv.gz",
    FIXTURE_ROOT / "bridge_step5b_contact_cycloid_baseline_v1_20260614_222309" / "bridge_rtde_500hz.csv.gz",
]
# Keep raw evidence preferred when the ignored archive is present, while
# making this repository's default tests independent of that local archive.
STEP5B_CSVS = [
    historical if historical.is_file() else fixture
    for historical, fixture in zip(_HISTORICAL_STEP5B_CSVS, _FIXTURE_STEP5B_CSVS)
]


class Step5bRos2RemoteShadowTest(unittest.TestCase):
    def test_default_config_launch_and_cli_are_no_motion(self) -> None:
        config = load_contact_cycloid_config(DEFAULT_CONFIG)
        self.assertFalse(config["enable_motion"])
        launch = (WORKSPACE / "src" / "ur10e_example_controllers" / "launch" / "contact_cycloid_shadow.launch.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('DeclareLaunchArgument("enable_motion", default_value="false")', launch)
        source = (WORKSPACE / "src" / "ur10e_example_controllers" / "ur10e_example_controllers" / "contact_cycloid_shadow.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("refuses enable_motion=true", source)

    def test_all_retained_step5b_csv_paths_exist(self) -> None:
        for path in STEP5B_CSVS:
            self.assertTrue(path.exists(), str(path))

    def test_full_step5b_replay_not_mostly_fail_fast_and_no_motion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = run_contact_cycloid_shadow(config_path=DEFAULT_CONFIG, output_dir=Path(tmp))
        self.assertIn(summary["source_mode"], {"historical_raw", "portable_derived_fixture"})
        self.assertTrue(summary["acceptance"]["not_mostly_fail_fast"])
        self.assertEqual(summary["fail_fast_ratio"], 0.0)
        self.assertFalse(summary["cmd_enabled_any"])
        self.assertEqual(summary["cmd_enabled_false_rows"], summary["rows_replayed"])
        self.assertGreater(summary["rows_replayed"], 100000)
        self.assertGreater(summary["active_contact_rows"], 0)
        self.assertEqual(len(summary["source_csvs"]), 3)
        self.assertEqual(summary["role"], "remote_control_plumbing_validation_before_step5d")

    def test_portable_fixture_manifest_is_complete_and_hash_bound(self) -> None:
        manifest_path = FIXTURE_ROOT / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "ur10e.step5b-shadow-derived-fixture-v1")
        self.assertEqual(len(manifest["runs"]), 3)
        self.assertIn("_step4e_desired_x_m", manifest["projected_fields"])
        for entry, fixture in zip(manifest["runs"], _FIXTURE_STEP5B_CSVS):
            self.assertEqual(FIXTURE_ROOT / entry["fixture_path"], fixture)
            self.assertEqual(entry["fixture_rows"], entry["source_rows"])
            digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
            self.assertEqual(entry["fixture_sha256"], digest)

    def test_replay_emits_required_summary_and_trace_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            summary = run_contact_cycloid_shadow(
                config_path=DEFAULT_CONFIG,
                output_dir=out,
                replay_csvs=[STEP5B_CSVS[0]],
                max_rows_per_csv=25,
            )
            payload = json.loads((out / "summary.json").read_text(encoding="utf-8"))
            with (out / "shadow_trace.csv").open(newline="", encoding="utf-8") as handle:
                header = next(csv.reader(handle))
        self.assertEqual(payload["artifact_dir"], summary["artifact_dir"])
        for field in TRACE_FIELDS:
            self.assertIn(field, header)
        self.assertIn("desired_reference_field_coverage", payload)
        self.assertIn("normal_load_n", payload)
        self.assertIn("max_actual_tcp_speed_m_s", payload)

    def test_trace_includes_desired_reference_fields_when_source_has_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = run_contact_cycloid_shadow(config_path=DEFAULT_CONFIG, output_dir=Path(tmp))
        coverage = summary["desired_reference_field_coverage"]
        for field in (
            "_step4e_desired_x_m",
            "_step4e_desired_y_m",
            "_step4e_desired_vx_m_s",
            "_step4e_desired_vy_m_s",
            "_step4e_path_error_x_m",
            "_step4e_path_error_y_m",
        ):
            self.assertGreater(coverage[field]["count"], 0)
            self.assertGreater(coverage[field]["ratio"], 0.9)

    def test_step5d_shadow_remains_diagnostic_not_current_live(self) -> None:
        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        step5b = next(item for item in table["stages"] if item["id"] == "step5b_ros2_remote_shadow_v1")
        step5d = next(item for item in table["stages"] if item["id"] == "step5d_ros2_remote_shadow_v1")
        self.assertFalse(step5b["active"])
        self.assertEqual(step5b["contact_policy"]["live_authorization"], "none")
        self.assertEqual(step5b["contact_policy"]["role"], "remote_control_plumbing_validation_before_step5d")
        self.assertEqual(
            step5b["guard"]["prerequisites"],
            [
                "step5a0_ros2_headless_driver_readiness_pass",
                "step5a_ros2_remote_no_contact_v1_pass",
            ],
        )
        self.assertFalse(step5d["active"])
        self.assertEqual(step5d["contact_policy"]["live_authorization"], "none")
        self.assertIn("diagnostic", step5d["block_reason"])


if __name__ == "__main__":
    unittest.main()
