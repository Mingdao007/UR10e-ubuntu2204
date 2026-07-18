#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from stars_bias_replay import (  # noqa: E402
    BiasRateKalman,
    ReferenceTrace,
    _causal_rows,
    inventory_runs,
    replay_run,
    validate_reference_pair,
)


class SyntheticRun:
    def __init__(
        self,
        root: Path,
        name: str = "synthetic_no_contact",
        *,
        tcp_z_m: float = 0.0,
    ) -> None:
        self.path = root / name
        self.path.mkdir(parents=True)
        metadata = {
            "args": {
                "bridge_profile": "step5d_strict_rnn_no_contact_p0_v7",
                "bridge_path_shape": "cycloid",
                "bias_contact_normal_threshold_n": 0.75,
                "bias_contact_force_norm_threshold_n": 2.0,
            }
        }
        (self.path / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        bridge_fields = [
            "t_monotonic_s",
            "normal_force_n",
            "force_norm_n",
            "baseline_ready",
            "zero_event_id",
            "bias_estimation_contact_mask",
            "control_contact_window",
            "_step4e_normal_acquired",
            "_step5d_force_settle_ready",
            "_step5d_contact_safety_state",
            "step4e_cmd_valid",
            "ur_output_double_register_35",
            "_step4e_path_time_s",
            *(f"ur_actual_TCP_pose_{idx}" for idx in range(6)),
            *(f"ur_actual_TCP_speed_{idx}" for idx in range(6)),
        ]
        sensor_fields = [
            "t_monotonic_s",
            "zero_event_id",
            "normal_force_n",
            "force_norm_n",
            "bias_estimation_contact_mask",
            "fx_n_zeroed",
            "fy_n_zeroed",
            "fz_n_zeroed",
            "mx_nm_zeroed",
            "my_nm_zeroed",
            "mz_nm_zeroed",
            "bias_est_fx_n",
            "bias_est_fy_n",
            "bias_est_fz_n",
            "bias_est_mx_nm",
            "bias_est_my_nm",
            "bias_est_mz_nm",
        ]
        with (self.path / "bridge_rtde_500hz.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=bridge_fields)
            writer.writeheader()
            for index in range(100):
                contact = index >= 80
                writer.writerow(
                    {
                        "t_monotonic_s": 1.0 + index * 0.01,
                        "normal_force_n": 5.2 if contact else 0.2,
                        "force_norm_n": 5.2 if contact else 0.2,
                        "baseline_ready": 1,
                        "zero_event_id": 1,
                        "bias_estimation_contact_mask": int(contact),
                        "control_contact_window": int(contact),
                        "_step4e_normal_acquired": int(contact),
                        "_step5d_force_settle_ready": int(contact),
                        "_step5d_contact_safety_state": int(contact),
                        "step4e_cmd_valid": int(contact),
                        "ur_output_double_register_35": 25.0 if contact else 0.0,
                        "_step4e_path_time_s": index * 0.01,
                        "ur_actual_TCP_pose_0": index * 0.00001,
                        "ur_actual_TCP_pose_1": 0.0,
                        "ur_actual_TCP_pose_2": tcp_z_m,
                        "ur_actual_TCP_pose_3": 0.0,
                        "ur_actual_TCP_pose_4": 0.0,
                        "ur_actual_TCP_pose_5": 0.0,
                        **{f"ur_actual_TCP_speed_{axis}": 0.001 if contact and axis == 0 else 0.0 for axis in range(6)},
                    }
                )
        with (self.path / "kunwei_sensor_1khz.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=sensor_fields)
            writer.writeheader()
            for index in range(200):
                contact = index >= 160
                force = 5.2 if contact else 0.2
                writer.writerow(
                    {
                        "t_monotonic_s": 0.995 + index * 0.005,
                        "zero_event_id": 1,
                        "normal_force_n": force,
                        "force_norm_n": force,
                        "bias_estimation_contact_mask": int(contact),
                        "fx_n_zeroed": force,
                        "fy_n_zeroed": 0.0,
                        "fz_n_zeroed": 0.0,
                        "mx_nm_zeroed": 0.002,
                        "my_nm_zeroed": 0.0,
                        "mz_nm_zeroed": 0.0,
                        "bias_est_fx_n": 1.0,
                        "bias_est_fy_n": 2.0,
                        "bias_est_fz_n": 3.0,
                        "bias_est_mx_nm": 0.1,
                        "bias_est_my_nm": 0.2,
                        "bias_est_mz_nm": 0.3,
                    }
                )


class StarsBiasReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.run = SyntheticRun(self.root).path
        self.config = ROOT / "config" / "ft_bias" / "stars_lite_v1.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_causal_join_never_selects_future_sensor_sample(self) -> None:
        rows = list(
            _causal_rows(
                self.run / "bridge_rtde_500hz.csv",
                self.run / "kunwei_sensor_1khz.csv",
            )
        )
        self.assertEqual(len(rows), 100)
        for _, bridge, _, sensor in rows:
            self.assertLessEqual(float(sensor["t_monotonic_s"]), float(bridge["t_monotonic_s"]))

    def test_replay_is_deterministic_and_freezes_all_contact_updates(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        summary = replay_run(self.run, first, self.config)
        replay_run(self.run, second, self.config)
        self.assertEqual(
            (first / "corrected_wrench.csv").read_bytes(),
            (second / "corrected_wrench.csv").read_bytes(),
        )
        self.assertEqual((first / "summary.json").read_bytes(), (second / "summary.json").read_bytes())
        self.assertEqual(summary["forbidden_update_count"], 0)
        self.assertEqual(summary["alignment"]["future_sample_count"], 0)
        self.assertEqual(summary["acceptance"]["software"], "pass")
        self.assertEqual(summary["acceptance"]["kalman_promotion"], "pass")
        self.assertEqual(summary["acceptance"]["reference_physical_validation"], "not_evaluated")
        self.assertLessEqual(summary["metrics"]["static_force_rmse_n"], 0.10)
        self.assertLessEqual(summary["metrics"]["static_torque_rmse_nm"], 0.01)
        self.assertGreaterEqual(summary["metrics"]["contact_force_retention_ratio"], 0.95)
        with (first / "corrected_wrench.csv").open(newline="", encoding="utf-8") as stream:
            contact_rows = [row for row in csv.DictReader(stream) if row["contact_mask"] == "1"]
        self.assertTrue(contact_rows)
        self.assertTrue(all(row["update_allowed"] == "0" for row in contact_rows))

    def test_kalman_covariance_stays_positive_semidefinite_when_frozen(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))["kalman"]
        estimator = BiasRateKalman.from_config(config)
        minimum = 1.0
        for index in range(1000):
            _, _, _, _, minimum = estimator.step(
                np.ones(6) * 0.1,
                0.002,
                update_allowed=index < 300,
            )
        self.assertGreaterEqual(minimum, -1.0e-12)

    def test_replay_refuses_to_write_inside_source_run(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the read-only source tree"):
            replay_run(self.run, self.run / "derived", self.config)
        self.assertFalse((self.run / "derived").exists())

    def test_plus_10mm_reference_pair_passes_geometry_gate(self) -> None:
        pair_root = self.root / "pair"
        current = SyntheticRun(pair_root, "current", tcp_z_m=0.0).path
        reference_run = SyntheticRun(pair_root, "reference", tcp_z_m=0.01).path
        reference = ReferenceTrace.from_run(reference_run)
        reference_config = json.loads(self.config.read_text(encoding="utf-8"))["reference"]
        result = validate_reference_pair(current, reference, reference_config)
        self.assertEqual(result["status"], "pass")
        self.assertAlmostEqual(abs(result["mean_z_offset_m"]), 0.01)

    def test_inventory_is_deterministic_and_hash_frozen(self) -> None:
        runs_root = self.root / "source-runs"
        for index in range(5):
            SyntheticRun(runs_root, f"synthetic_no_contact_{index}")
        first = inventory_runs(runs_root, self.root / "manifest-one.json")
        second = inventory_runs(runs_root, self.root / "manifest-two.json")
        self.assertEqual(first, second)
        self.assertEqual(first["eligible_count"], 5)
        self.assertEqual(sum(first["counts"].values()), 5)
        self.assertTrue(all(len(record["identity_sha256"]) == 64 for record in first["records"]))


if __name__ == "__main__":
    unittest.main()
