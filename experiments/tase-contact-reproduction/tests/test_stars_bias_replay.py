#!/usr/bin/env python3

from __future__ import annotations

import csv
import json
import math
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
    evaluate_manifest,
    inventory_runs,
    replay_run,
    validate_reference_pair,
)


class SyntheticRun:
    def __init__(
        self,
        root: Path,
        name: str = "synthetic_contact",
        *,
        tcp_z_m: float = 0.0,
        tcp_z_wave_m: float = 0.0,
        profile: str = "step5d_strict_rnn_contact_v1",
        path_shape: str = "cycloid",
        contact_stage: bool = True,
        phase_offset_s: float = 0.0,
        linear_acceleration_x_m_s2: float = 0.0,
        angular_speed_x_rad_s: float = 0.0,
        angular_acceleration_x_rad_s2: float = 0.0,
        orientation_rotvec: tuple[float, float, float] = (0.0, 0.0, 0.0),
        stage25_force_wave_n: float = 0.0,
        free_static_force_wave_n: float = 0.0,
        bridge_rows: int = 100,
        sensor_rows: int = 200,
    ) -> None:
        self.path = root / name
        self.path.mkdir(parents=True)
        metadata = {
            "args": {
                "bridge_profile": profile,
                "bridge_path_shape": path_shape,
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
            *(f"ur_actual_TCP_accel_{idx}" for idx in range(6)),
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
            for index in range(bridge_rows):
                stage25 = index >= 80
                contact = stage25 and contact_stage
                wave = tcp_z_wave_m if index % 2 == 0 else -tcp_z_wave_m
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
                        "ur_output_double_register_35": 25.0 if stage25 else 0.0,
                        "_step4e_path_time_s": index * 0.01 + phase_offset_s,
                        "ur_actual_TCP_pose_0": index * 0.00001,
                        "ur_actual_TCP_pose_1": 0.0,
                        "ur_actual_TCP_pose_2": tcp_z_m + wave,
                        "ur_actual_TCP_pose_3": orientation_rotvec[0],
                        "ur_actual_TCP_pose_4": orientation_rotvec[1],
                        "ur_actual_TCP_pose_5": orientation_rotvec[2],
                        **{
                            f"ur_actual_TCP_speed_{axis}": (
                                0.001
                                if stage25 and axis == 0
                                else angular_speed_x_rad_s
                                if stage25 and axis == 3
                                else 0.0
                            )
                            for axis in range(6)
                        },
                        **{
                            f"ur_actual_TCP_accel_{axis}": (
                                linear_acceleration_x_m_s2
                                if stage25 and axis == 0
                                else angular_acceleration_x_rad_s2
                                if stage25 and axis == 3
                                else 0.0
                            )
                            for axis in range(6)
                        },
                    }
                )
        with (self.path / "kunwei_sensor_1khz.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=sensor_fields)
            writer.writeheader()
            for index in range(sensor_rows):
                contact = index >= 160 and contact_stage
                stage25 = index >= 160
                force = 5.2 if contact else 0.2
                if stage25 and stage25_force_wave_n:
                    force += (
                        stage25_force_wave_n
                        if (index // 2) % 2 == 0
                        else -stage25_force_wave_n
                    )
                if not stage25 and free_static_force_wave_n:
                    force += (
                        free_static_force_wave_n
                        if (index // 2) % 2 == 0
                        else -free_static_force_wave_n
                    )
                writer.writerow(
                    {
                        "t_monotonic_s": 0.995 + index * 0.005,
                        "zero_event_id": 1,
                        "normal_force_n": force,
                        "force_norm_n": abs(force),
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
        self.assertLessEqual(summary["metrics"]["contact_force_retention_ratio_max"], 1.05)
        self.assertGreaterEqual(summary["metrics"]["contact_force_direction_cosine_min"], 0.99)
        self.assertGreaterEqual(summary["metrics"]["confirmed_contact_rows"], 10)
        self.assertLessEqual(summary["metrics"]["free_static_nis_p95"], 16.812)
        self.assertGreater(summary["metrics"]["frozen_nis_count"], 0)
        self.assertGreater(summary["mode_diagnostics"]["contact_track"]["nis_count"], 0)
        with (first / "corrected_wrench.csv").open(newline="", encoding="utf-8") as stream:
            contact_rows = [row for row in csv.DictReader(stream) if row["contact_mask"] == "1"]
        self.assertTrue(contact_rows)
        self.assertTrue(all(row["update_allowed"] == "0" for row in contact_rows))

    def test_kalman_covariance_stays_positive_semidefinite_when_frozen(self) -> None:
        config = json.loads(self.config.read_text(encoding="utf-8"))["kalman"]
        estimator = BiasRateKalman.from_config(config)
        minimum = math.inf
        for index in range(1000):
            _, _, _, nis, current_minimum = estimator.step(
                np.ones(6) * 0.1,
                0.002,
                update_allowed=index < 300,
            )
            minimum = min(minimum, current_minimum)
            self.assertIsNotNone(nis)
        self.assertGreaterEqual(minimum, -1.0e-12)

    def test_replay_refuses_to_write_inside_source_run(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the read-only source tree"):
            replay_run(self.run, self.run / "derived", self.config)
        self.assertFalse((self.run / "derived").exists())

    def test_plus_10mm_reference_pair_passes_geometry_gate(self) -> None:
        pair_root = self.root / "pair"
        current = SyntheticRun(pair_root, "current", tcp_z_m=0.0).path
        reference_run = SyntheticRun(
            pair_root,
            "reference",
            tcp_z_m=0.01,
            profile="step5d_strict_rnn_no_contact_p0_v7",
            contact_stage=False,
        ).path
        reference_config = json.loads(self.config.read_text(encoding="utf-8"))["reference"]
        reference = ReferenceTrace.from_run(reference_run, reference_config)
        result = validate_reference_pair(current, reference, reference_config)
        self.assertEqual(result["status"], "pass")
        self.assertAlmostEqual(result["mean_z_offset_m"], -0.01)
        self.assertEqual(len(result["reference_wrench_covariance"]), 6)

    def test_reference_rejects_wrong_profile_direction_variation_and_acceleration(self) -> None:
        pair_root = self.root / "negative-pairs"
        reference_config = json.loads(self.config.read_text(encoding="utf-8"))["reference"]
        wrong_profile = SyntheticRun(pair_root, "wrong-profile", contact_stage=False).path
        with self.assertRaisesRegex(ValueError, "reference profile"):
            ReferenceTrace.from_run(wrong_profile, reference_config)
        masked_reference = SyntheticRun(
            pair_root,
            "masked-reference",
            profile="step5d_strict_rnn_no_contact_p0_v7",
            contact_stage=True,
        ).path
        with self.assertRaisesRegex(ValueError, "contact-masked rows"):
            ReferenceTrace.from_run(masked_reference, reference_config)
        noisy_reference = SyntheticRun(
            pair_root,
            "noisy-reference",
            profile="step5d_strict_rnn_no_contact_p0_v7",
            contact_stage=False,
            stage25_force_wave_n=3.0,
        ).path
        with self.assertRaisesRegex(ValueError, "force variance exceeds"):
            ReferenceTrace.from_run(noisy_reference, reference_config)
        reference_run = SyntheticRun(
            pair_root,
            "reference",
            tcp_z_m=0.01,
            profile="step5d_strict_rnn_no_contact_p0_v7",
            contact_stage=False,
        ).path
        reference = ReferenceTrace.from_run(reference_run, reference_config)
        wrong_direction = SyntheticRun(pair_root, "wrong-direction", tcp_z_m=0.02).path
        with self.assertRaisesRegex(ValueError, "geometry validation failed"):
            validate_reference_pair(wrong_direction, reference, reference_config)
        variable_z = SyntheticRun(
            pair_root,
            "variable-z",
            tcp_z_m=0.0,
            tcp_z_wave_m=0.01,
        ).path
        with self.assertRaisesRegex(ValueError, "geometry validation failed"):
            validate_reference_pair(variable_z, reference, reference_config)
        acceleration_mismatch = SyntheticRun(
            pair_root,
            "acceleration-mismatch",
            linear_acceleration_x_m_s2=0.1,
        ).path
        with self.assertRaisesRegex(ValueError, "geometry validation failed"):
            validate_reference_pair(acceleration_mismatch, reference, reference_config)
        path_mismatch = SyntheticRun(
            pair_root,
            "path-mismatch",
            path_shape="line",
        ).path
        with self.assertRaisesRegex(ValueError, "path-shape mismatch"):
            validate_reference_pair(path_mismatch, reference, reference_config)

    def test_reference_rejects_angular_speed_mismatch(self) -> None:
        pair_root = self.root / "angular-speed-pair"
        reference_config = json.loads(self.config.read_text(encoding="utf-8"))["reference"]
        reference_run = SyntheticRun(
            pair_root,
            "reference",
            tcp_z_m=0.01,
            profile="step5d_strict_rnn_no_contact_p0_v7",
            contact_stage=False,
        ).path
        current = SyntheticRun(
            pair_root,
            "current",
            angular_speed_x_rad_s=0.5,
        ).path
        reference = ReferenceTrace.from_run(reference_run, reference_config)
        with self.assertRaisesRegex(ValueError, "geometry validation failed"):
            validate_reference_pair(current, reference, reference_config)

    def test_reference_rejects_angular_acceleration_mismatch(self) -> None:
        pair_root = self.root / "angular-acceleration-pair"
        reference_config = json.loads(self.config.read_text(encoding="utf-8"))["reference"]
        reference_run = SyntheticRun(
            pair_root,
            "reference",
            tcp_z_m=0.01,
            profile="step5d_strict_rnn_no_contact_p0_v7",
            contact_stage=False,
        ).path
        current = SyntheticRun(
            pair_root,
            "current",
            angular_acceleration_x_rad_s2=100.0,
        ).path
        reference = ReferenceTrace.from_run(reference_run, reference_config)
        with self.assertRaisesRegex(ValueError, "geometry validation failed"):
            validate_reference_pair(current, reference, reference_config)

    def test_reference_rotvec_wrap_and_phase_domain_are_handled(self) -> None:
        pair_root = self.root / "domain-pair"
        reference_config = json.loads(self.config.read_text(encoding="utf-8"))["reference"]
        reference_run = SyntheticRun(
            pair_root,
            "reference",
            tcp_z_m=0.01,
            profile="step5d_strict_rnn_no_contact_p0_v7",
            contact_stage=False,
            orientation_rotvec=(0.0, 0.0, -math.pi),
        ).path
        current = SyntheticRun(
            pair_root,
            "current",
            phase_offset_s=-0.01,
            orientation_rotvec=(0.0, 0.0, math.pi),
        ).path
        reference = ReferenceTrace.from_run(reference_run, reference_config)
        result = validate_reference_pair(current, reference, reference_config)
        self.assertEqual(result["status"], "pass")
        summary = replay_run(
            current,
            self.root / "domain-output",
            self.config,
            reference_run_dir=reference_run,
        )
        self.assertEqual(summary["reference_application"]["out_of_domain_stage25_rows"], 1)
        self.assertEqual(summary["reference_application"]["applied_rows"], 19)
        with (self.root / "domain-output" / "corrected_wrench.csv").open(
            newline="", encoding="utf-8"
        ) as stream:
            rows = list(csv.DictReader(stream))
        outside = [
            row for row in rows if row["reference_domain_status"] == "outside_validated_phase"
        ]
        self.assertEqual(len(outside), 1)
        self.assertEqual(outside[0]["reference_applied"], "0")
        self.assertEqual(outside[0]["reference_wrench_fx_n"], "")

    def test_reference_phase_overflow_is_not_applied(self) -> None:
        pair_root = self.root / "overflow-pair"
        reference_config = json.loads(self.config.read_text(encoding="utf-8"))["reference"]
        reference_run = SyntheticRun(
            pair_root,
            "reference",
            tcp_z_m=0.01,
            profile="step5d_strict_rnn_no_contact_p0_v7",
            contact_stage=False,
        ).path
        current = SyntheticRun(
            pair_root,
            "current",
            phase_offset_s=0.01,
        ).path
        reference = ReferenceTrace.from_run(reference_run, reference_config)
        result = validate_reference_pair(current, reference, reference_config)
        self.assertEqual(result["status"], "pass")
        summary = replay_run(
            current,
            self.root / "overflow-output",
            self.config,
            reference_run_dir=reference_run,
        )
        self.assertEqual(summary["reference_application"]["out_of_domain_stage25_rows"], 1)
        self.assertEqual(summary["reference_application"]["applied_rows"], 19)
        with (self.root / "overflow-output" / "corrected_wrench.csv").open(
            newline="", encoding="utf-8"
        ) as stream:
            rows = list(csv.DictReader(stream))
        outside = [
            row for row in rows if row["reference_domain_status"] == "outside_validated_phase"
        ]
        self.assertEqual(len(outside), 1)
        self.assertEqual(outside[0]["reference_applied"], "0")
        self.assertEqual(outside[0]["reference_wrench_fx_n"], "")

    def test_header_only_run_fails_software_data_quality_gate(self) -> None:
        empty_run = SyntheticRun(
            self.root,
            "header-only",
            bridge_rows=0,
            sensor_rows=0,
        ).path
        summary = replay_run(empty_run, self.root / "empty-output", self.config)
        self.assertEqual(summary["acceptance"]["software"], "fail")
        self.assertEqual(summary["alignment"]["causal_coverage"], 0.0)

    def test_no_contact_profile_cannot_pass_contact_promotion(self) -> None:
        no_contact = SyntheticRun(
            self.root,
            "no-contact-promotion",
            profile="step5d_strict_rnn_no_contact_p0_v7",
            contact_stage=True,
        ).path
        summary = replay_run(no_contact, self.root / "no-contact-output", self.config)
        self.assertEqual(summary["acceptance"]["software"], "pass")
        self.assertFalse(summary["metrics"]["promotion_eligible_contact_profile"])
        self.assertEqual(summary["acceptance"]["kalman_promotion"], "not_promoted")

    def test_free_static_nis_blocks_promotion_before_rmse_limit(self) -> None:
        noisy_static = SyntheticRun(
            self.root,
            "noisy-static",
            free_static_force_wave_n=0.06,
        ).path
        summary = replay_run(noisy_static, self.root / "noisy-static-output", self.config)
        self.assertEqual(summary["acceptance"]["software"], "pass")
        self.assertLess(summary["metrics"]["static_force_rmse_n"], 0.1)
        self.assertGreater(summary["metrics"]["free_static_nis_p95"], 16.812)
        self.assertEqual(summary["acceptance"]["kalman_promotion"], "not_promoted")

    def test_inventory_is_deterministic_and_hash_frozen(self) -> None:
        runs_root = self.root / "source-runs"
        for index in range(5):
            SyntheticRun(runs_root, f"synthetic_contact_{index}")
        SyntheticRun(
            runs_root,
            "synthetic_header_only",
            bridge_rows=0,
            sensor_rows=0,
        )
        first = inventory_runs(
            runs_root, self.root / "manifest-one.json", self.config
        )
        second = inventory_runs(
            runs_root, self.root / "manifest-two.json", self.config
        )
        self.assertEqual(first, second)
        self.assertEqual(first["schema_eligible_count"], 6)
        self.assertEqual(first["eligible_count"], 5)
        self.assertEqual(sum(first["counts"].values()), 5)
        self.assertEqual(len(first["analysis_excluded"]), 1)
        self.assertEqual(
            first["analysis_excluded"][0]["relative_run_dir"],
            "synthetic_header_only",
        )
        self.assertFalse(
            first["analysis_excluded"][0]["data_quality"]["checks"]["nonempty"]
        )
        self.assertEqual(len(first["analysis_excluded"][0]["identity_sha256"]), 64)
        self.assertEqual(
            sorted(first["analysis_excluded"][0]["files"]),
            ["bridge_sha256", "metadata_sha256", "sensor_sha256"],
        )
        self.assertTrue(all(len(record["identity_sha256"]) == 64 for record in first["records"]))
        self.assertNotIn("runs_root", first)
        self.assertTrue(all("run_dir" not in record for record in first["records"]))

    def test_evaluation_is_canonical_across_worker_counts_and_uses_train_split(self) -> None:
        runs_root = self.root / "evaluation-runs"
        for index in range(5):
            SyntheticRun(runs_root, f"run-{index}")
        SyntheticRun(
            runs_root,
            "excluded-header-only",
            bridge_rows=0,
            sensor_rows=0,
        )
        manifest_path = self.root / "evaluation-manifest.json"
        manifest = inventory_runs(runs_root, manifest_path, self.config)
        self.assertEqual(manifest["schema_eligible_count"], 6)
        self.assertEqual(manifest["eligible_count"], 5)
        self.assertEqual(len(manifest["analysis_excluded"]), 1)
        first = evaluate_manifest(
            manifest_path,
            runs_root,
            self.root / "evaluation-one",
            self.config,
            jobs=1,
        )
        second = evaluate_manifest(
            manifest_path,
            runs_root,
            self.root / "evaluation-two",
            self.config,
            jobs=2,
        )
        self.assertEqual(
            (self.root / "evaluation-one" / "evaluation.json").read_bytes(),
            (self.root / "evaluation-two" / "evaluation.json").read_bytes(),
        )
        self.assertNotIn(
            str(runs_root).encode(),
            (self.root / "evaluation-one" / "evaluation.json").read_bytes(),
        )
        self.assertGreater(first["training"]["static_difference_count"], 0)
        self.assertEqual(first, second)

        changed_config = self.root / "changed-config.json"
        changed_config.write_text(
            self.config.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "eligibility config SHA"):
            evaluate_manifest(
                manifest_path,
                runs_root,
                self.root / "evaluation-config-mismatch",
                changed_config,
                jobs=1,
            )
        self.assertFalse((self.root / "evaluation-config-mismatch").exists())

        excluded_bridge = (
            runs_root / "excluded-header-only" / "bridge_rtde_500hz.csv"
        )
        excluded_bridge_bytes = excluded_bridge.read_bytes()
        excluded_bridge.write_bytes(excluded_bridge_bytes + b"\n")
        with self.assertRaisesRegex(ValueError, "manifest file hash mismatch"):
            evaluate_manifest(
                manifest_path,
                runs_root,
                self.root / "evaluation-excluded-source-drift",
                self.config,
                jobs=1,
            )
        self.assertFalse((self.root / "evaluation-excluded-source-drift").exists())
        excluded_bridge.write_bytes(excluded_bridge_bytes)

        with (
            runs_root / "run-0" / "bridge_rtde_500hz.csv"
        ).open("a", encoding="utf-8") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "manifest file hash mismatch"):
            evaluate_manifest(
                manifest_path,
                runs_root,
                self.root / "evaluation-source-drift",
                self.config,
                jobs=1,
            )
        self.assertFalse((self.root / "evaluation-source-drift").exists())


if __name__ == "__main__":
    unittest.main()
