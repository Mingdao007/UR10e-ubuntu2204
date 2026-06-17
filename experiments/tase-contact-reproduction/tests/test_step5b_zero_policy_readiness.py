#!/usr/bin/env python3
"""Offline tests for the Step5b zero-policy readiness gate."""

from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers import step5b_zero_policy_readiness as readiness  # noqa: E402


def args(**overrides: object) -> argparse.Namespace:
    values = {
        "sensor_ip": "192.168.50.25",
        "sensor_port": 5152,
        "sample_duration_s": 2.0,
        "baseline_window_s": 1.0,
        "validation_window_s": 0.5,
        "min_samples": 1000,
        "recent_rate_hz_min": 200.0,
        "latest_sample_max_age_s": 0.25,
        "force_norm_zeroed_mean_max_n": 0.75,
        "force_norm_zeroed_max_n": 1.5,
        "normal_load_zeroed_abs_max_n": 0.75,
        "normal_axis": "fz",
        "normal_sign": 1.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def synthetic_samples(*, count: int = 1001, spike_n: float = 0.0) -> list[tuple[float, tuple[float, ...]]]:
    samples = []
    for idx in range(count):
        t_s = 2.0 * idx / float(count - 1)
        fz_n = 0.0
        if idx == count - 1:
            fz_n = spike_n
        samples.append((t_s, (0.1, -0.2, fz_n / readiness.FORCE_KG_TO_N, 0.0, 0.0, 0.0)))
    return samples


class Step5bZeroPolicyReadinessTest(unittest.TestCase):
    def test_synthetic_stable_stream_passes_without_authorizing_motion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            summary, rows = readiness.build_summary(
                samples=synthetic_samples(),
                stream_metrics={
                    "sensor_endpoint": "192.168.50.25:5152",
                    "stream_start_command_sent": True,
                    "stream_stop_command_sent": True,
                    "bytes_received": 28028,
                    "packets_received": 20,
                    "dropped_sync_bytes": 0,
                    "parse_errors": 0,
                },
                output_dir=out,
                raw_frames_path=out / "raw.bin",
                samples_csv_path=out / "samples.csv",
                args=args(),
            )
        self.assertTrue(summary["ok"], summary["failure_reason"])
        self.assertFalse(summary["motion_authorized"])
        self.assertFalse(summary["contact_search_authorized"])
        self.assertFalse(summary["bridge_start_authorized"])
        self.assertFalse(summary["tp_play_authorized"])
        self.assertEqual(summary["force_source"], "kunwei_software_baselined_stream")
        self.assertFalse(summary["zero_policy"]["ur_zero_ftsensor_called"])
        self.assertFalse(summary["zero_policy"]["kunwei_hardware_tare_or_config_written"])
        self.assertTrue(summary["zero_policy"]["software_baseline_subtraction"])
        self.assertEqual(summary["textbook_alignment"]["zero_policy_classification"], "preserved")
        self.assertTrue(summary["contact_force_frame_contract"]["reaction_normal_for_load"])
        self.assertGreater(len(rows), 1000)

    def test_threshold_violation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            summary, _ = readiness.build_summary(
                samples=synthetic_samples(spike_n=3.0),
                stream_metrics={
                    "sensor_endpoint": "192.168.50.25:5152",
                    "stream_start_command_sent": True,
                    "stream_stop_command_sent": True,
                    "bytes_received": 28028,
                    "packets_received": 20,
                    "dropped_sync_bytes": 0,
                    "parse_errors": 0,
                },
                output_dir=out,
                raw_frames_path=out / "raw.bin",
                samples_csv_path=out / "samples.csv",
                args=args(),
            )
        self.assertFalse(summary["ok"])
        self.assertIn("force_norm_zeroed_max_threshold", summary["failure_reason"])
        self.assertIn("normal_load_zeroed_abs_threshold", summary["failure_reason"])
        self.assertFalse(summary["motion_authorized"])

    def test_wrapper_is_readiness_only(self) -> None:
        script = (WORKSPACE / "step5b_zero_policy_check.sh").read_text(encoding="utf-8")
        self.assertIn("step5b_zero_policy_readiness", script)
        self.assertIn("STEP5B_ZERO_POLICY_SAMPLE_DURATION_S", script)
        self.assertIn("summary=${RUN_DIR}/summary.json", script)
        self.assertNotIn("zero_ftsensor", script)
        self.assertNotIn("ros2 launch", script)
        self.assertNotIn("send_goal", script)


if __name__ == "__main__":
    unittest.main()
