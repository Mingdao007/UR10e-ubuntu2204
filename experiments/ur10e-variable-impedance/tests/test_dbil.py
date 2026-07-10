from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from ur10e_vic.cli import _observation_from_mapping, _validate_lock
from ur10e_vic.dbil.config import DBILConfig
from ur10e_vic.dbil.convert_parkour import REQUIRED_COLUMNS, convert_parkour_dataset
from ur10e_vic.dbil.dataset import DatasetStats, load_dataset, write_dataset_manifest
from ur10e_vic.dbil.model import (
    ConditionalDiffusionTransformer,
    sample_s_zft,
    training_loss,
)
from ur10e_vic.dbil import inference as inference_module
from ur10e_vic.dbil.inference import (
    benchmark_paced_shadow_predictor,
    benchmark_shadow_predictor,
)
from ur10e_vic.dbil.timing import (
    TimingEvidence,
    parse_independent_paced_timing,
    select_model_rate_hz,
)
from ur10e_vic.dbil.upstream_core import (
    UPSTREAM_VARIANT,
    UpstreamNoisePredictor,
    add_upstream_noise,
    quaternion_slerp,
    reconstruct_upstream,
    upstream_stiffness_estimate,
    upstream_training_loss,
)
from ur10e_vic.contracts import PoseSample
from ur10e_vic.policies import DBILPrediction

from helpers import observation


ROOT = Path(__file__).resolve().parents[1]


class DBILTests(unittest.TestCase):
    def test_paced_harness_runs_independent_rates_and_counts_skips(self) -> None:
        class FakeClock:
            now = 0.0

            def perf_counter(self):
                return self.now

            def sleep(self, duration):
                self.now += max(0.0, duration)

        clock = FakeClock()

        class FakePredictor:
            class Device:
                type = "cpu"

                def __str__(self):
                    return "cpu"

            device = Device()

            def predict(self, item):
                del item
                clock.now += 0.03
                return DBILPrediction(
                    PoseSample((0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
                    1.0,
                    "a" * 64,
                )

        original_perf = inference_module.time.perf_counter
        original_sleep = inference_module.time.sleep
        original_sync = inference_module._synchronize
        inference_module.time.perf_counter = clock.perf_counter
        inference_module.time.sleep = clock.sleep
        inference_module._synchronize = lambda _device: None
        try:
            result = benchmark_paced_shadow_predictor(
                FakePredictor(),
                observation(),
                duration_per_rate_s=60.0,
                warmup_iterations=1,
            )
            unpaced = benchmark_shadow_predictor(
                FakePredictor(),
                observation(),
                duration_s=60.0,
                warmup_iterations=1,
            )
        finally:
            inference_module.time.perf_counter = original_perf
            inference_module.time.sleep = original_sleep
            inference_module._synchronize = original_sync
        self.assertEqual(
            [item["rate_hz"] for item in result["rate_results"]],
            [200, 100, 50],
        )
        self.assertTrue(
            all(item["duration_s"] >= 60.0 for item in result["rate_results"])
        )
        self.assertGreater(result["rate_results"][0]["skipped_releases"], 0)
        self.assertIsNone(result["selected_rate_hz"])
        self.assertTrue(result["shadow_only"])
        self.assertEqual(unpaced["timing_mode"], "unpaced_throughput_diagnostic")
        self.assertFalse(unpaced["selection_eligible"])
        self.assertIsNone(unpaced["selected_rate_hz"])
        self.assertTrue(unpaced["shadow_only"])
        self.assertTrue(
            all(
                item["selection_eligible"] is False
                for item in unpaced["rate_diagnostics"]
            )
        )
    def test_paper_architecture_and_active_gate_are_pinned(self) -> None:
        config = DBILConfig()
        self.assertEqual(
            (
                config.history_window,
                config.hidden_dim,
                config.attention_heads,
                config.transformer_layers,
                config.denoising_steps,
                config.seed,
            ),
            (16, 512, 4, 6, 20, 42),
        )
        self.assertFalse(config.active_enabled)
        with self.assertRaisesRegex(ValueError, "active mode"):
            DBILConfig(active_enabled=True)

    def test_upstream_lock(self) -> None:
        payload = _validate_lock(ROOT / "config/dbil_upstream_lock.json")
        self.assertEqual(
            payload["upstream_commit"],
            "8c05a4d4aca8012927a9fdf5bfcd8313247f6a30",
        )

    def test_portable_dataset_manifest_and_stats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "parkour.npz"
            pose = np.zeros((3, 16, 7), dtype=np.float32)
            pose[:, :, 3] = 1.0
            wrench = np.zeros((3, 16, 6), dtype=np.float32)
            target = np.zeros((3, 16, 7), dtype=np.float32)
            target[:, :, 3] = 1.0
            np.savez(
                dataset,
                pose_history=pose,
                wrench_history=wrench,
                target_s_zft=target,
                split=np.asarray((0, 1, 2), dtype=np.int8),
            )
            manifest_path = root / "manifest.json"
            stats_path = root / "stats.json"
            manifest = write_dataset_manifest(
                dataset, manifest_path, stats_output_path=stats_path
            )
            self.assertEqual(manifest["sample_count"], 3)
            self.assertEqual(len(manifest["dataset_sha256"]), 64)
            self.assertEqual(manifest["dataset_artifact"], "parkour.npz")
            self.assertNotIn("dataset_path", manifest)
            self.assertEqual(len(load_dataset(dataset)), 3)
            self.assertTrue(stats_path.exists())

            pose[:, :, 3] = 2.0
            np.savez(
                dataset,
                pose_history=pose,
                wrench_history=wrench,
                target_s_zft=target,
                split=np.asarray((0, 1, 2), dtype=np.int8),
            )
            with self.assertRaisesRegex(ValueError, "unit norm"):
                load_dataset(dataset)
            with self.assertRaisesRegex(ValueError, "finite"):
                DatasetStats(
                    context_mean=(float("nan"),) + (0.0,) * 12,
                    context_std=(1.0,) * 13,
                    target_mean=(0.0,) * 7,
                    target_std=(1.0,) * 7,
                )

    def test_upstream_text_converter_uses_file_level_splits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Parkour"
            source.mkdir()
            values = {
                "f_x": 1.0,
                "f_y": 0.0,
                "f_z": 0.0,
                "m_x": 0.0,
                "m_y": 0.0,
                "m_z": 0.0,
                "x": 0.1,
                "y": 0.0,
                "z": 0.0,
                "x0": 0.0,
                "y0": 0.0,
                "z0": 0.0,
                "u_x": 1.0,
                "u_y": 0.0,
                "u_z": 0.0,
                "theta": 0.0,
                "u0_x": 1.0,
                "u0_y": 0.0,
                "u0_z": 0.0,
                "theta0": 0.0,
            }
            text = "\t".join(REQUIRED_COLUMNS) + "\n"
            text += "\n".join(
                "\t".join(str(values[name]) for name in REQUIRED_COLUMNS)
                for _ in range(16)
            )
            for index in range(4):
                (source / f"run_{index}.txt").write_text(text + "\n", encoding="utf-8")
            dataset = root / "parkour.npz"
            manifest = root / "manifest.json"
            stats = root / "stats.json"
            result = convert_parkour_dataset(source, dataset, manifest, stats)
            self.assertEqual(result["sample_count"], 4)
            self.assertFalse(result["upstream_binding_verified"])
            self.assertEqual(result["source_root"], "Parkour")
            self.assertFalse(Path(result["source_root"]).is_absolute())
            converted = load_dataset(dataset)
            self.assertEqual(converted.target_s_zft.shape, (4, 16, 7))
            self.assertEqual(set(converted.split.tolist()), {0, 1, 2})

    def test_model_rate_selects_highest_accepted_candidate(self) -> None:
        evidence = (
            TimingEvidence(200, 60.0, 0.0041, 0),
            TimingEvidence(100, 60.0, 0.0079, 0),
            TimingEvidence(50, 60.0, 0.0100, 0),
        )
        self.assertEqual(select_model_rate_hz(evidence), 100)
        missed = (TimingEvidence(50, 60.0, 0.0100, 1),)
        self.assertIsNone(select_model_rate_hz(missed))
        with self.assertRaisesRegex(ValueError, "at least 60 seconds"):
            TimingEvidence(200, 59.99, 0.001, 0)

    def test_select_rate_requires_complete_independent_paced_bundle(self) -> None:
        rates = []
        for rate, p99 in ((200, 0.0041), (100, 0.0079), (50, 0.0100)):
            item = TimingEvidence(rate, 60.0, p99, 0)
            rates.append(
                {
                    "rate_hz": rate,
                    "duration_s": 60.0,
                    "period_s": 1.0 / rate,
                    "scheduled_ticks": rate * 60,
                    "executed_ticks": rate * 60,
                    "skipped_releases": 0,
                    "deadline_misses": 0,
                    "nonfinite_outputs": 0,
                    "latency_p99_s": p99,
                    "accepted": item.accepted,
                }
            )
        payload = {
            "schema_version": 1,
            "timing_mode": "independent_wall_clock_paced_trials",
            "duration_per_rate_s": 60.0,
            "rate_results": rates,
            "selected_rate_hz": 100,
            "selection_eligible": True,
            "shadow_only": True,
            "active_enabled": False,
        }
        bundle = parse_independent_paced_timing(payload)
        self.assertEqual(bundle.selected_rate_hz, 100)
        with self.assertRaisesRegex(ValueError, "paced timing bundle object"):
            parse_independent_paced_timing(
                [
                    {
                        "rate_hz": 50,
                        "duration_s": 60.0,
                        "p99_latency_s": 0.01,
                        "deadline_misses": 0,
                    }
                ]
            )
        with self.assertRaisesRegex(ValueError, "exactly 200/100/50"):
            parse_independent_paced_timing(
                {**payload, "rate_results": rates[:-1], "selected_rate_hz": None}
            )
        one_tick_rates = [dict(item) for item in rates]
        for item in one_tick_rates:
            item["scheduled_ticks"] = 1
            item["executed_ticks"] = 1
        with self.assertRaisesRegex(ValueError, "does not cover"):
            parse_independent_paced_timing(
                {**payload, "rate_results": one_tick_rates, "selected_rate_hz": None}
            )
        with self.assertRaisesRegex(ValueError, "not explicitly selection-eligible"):
            parse_independent_paced_timing(
                {**payload, "selection_eligible": False}
            )

    def test_model_dependency_is_explicit_when_torch_is_absent(self) -> None:
        try:
            import torch  # noqa: F401
        except ModuleNotFoundError:
            with self.assertRaisesRegex(RuntimeError, "PyTorch is not installed"):
                ConditionalDiffusionTransformer(DBILConfig())

    def test_torch_model_reconstructs_full_window_when_available(self) -> None:
        try:
            import torch
        except ModuleNotFoundError:
            self.skipTest("optional PyTorch environment is not active")
        config = DBILConfig()
        model = ConditionalDiffusionTransformer(config)
        context = torch.zeros((1, 16, 13), dtype=torch.float32)
        target = torch.zeros((1, 16, 7), dtype=torch.float32)
        output = model(context, target, torch.zeros((1,), dtype=torch.long))
        self.assertEqual(tuple(output.shape), (1, 16, 7))
        loss = training_loss(model, context, target, config)
        self.assertTrue(torch.isfinite(loss))
        sample = sample_s_zft(model, context, config)
        self.assertEqual(tuple(sample.shape), (1, 16, 7))

    def test_upstream_stiffness_core_is_finite_and_bounded(self) -> None:
        zeros = np.zeros((16, 3), dtype=float)
        error = np.full((16, 3), 0.01, dtype=float)
        force = np.full((16, 3), 2.0, dtype=float)
        translational, rotational = upstream_stiffness_estimate(
            np.tile((1.0, 0.0, 0.0), (16, 1)),
            error,
            zeros,
            zeros,
            zeros,
            force,
            zeros,
            0.1,
        )
        self.assertTrue(np.isfinite(translational).all())
        self.assertTrue(np.all((0.0 <= translational) & (translational <= 800.0)))
        self.assertTrue(np.allclose(rotational, 150.0))

    def test_upstream_slerp_cross_attention_core_when_torch_available(self) -> None:
        try:
            import torch
        except ModuleNotFoundError:
            self.skipTest("optional PyTorch environment is not active")
        config = DBILConfig(diffusion_variant=UPSTREAM_VARIANT)
        clean = torch.zeros((1, 16, 7), dtype=torch.float32)
        clean[..., 3] = 1.0
        noisy = clean.clone()
        noisy[..., 3] = np.cos(np.pi / 4.0)
        noisy[..., 6] = np.sin(np.pi / 4.0)
        wrench = torch.zeros((1, 16, 6), dtype=torch.float32)
        position, quaternion, scale, step = add_upstream_noise(
            clean, noisy, config, noiseadding_steps=20, timestep=7
        )
        self.assertEqual(tuple(position.shape), (1, 16, 3))
        self.assertTrue(torch.allclose(torch.linalg.vector_norm(quaternion, dim=-1), torch.ones((1, 16)), atol=1e-5))
        self.assertTrue(torch.isfinite(scale))
        self.assertEqual(int(step), 7)
        self.assertTrue(
            torch.allclose(
                quaternion_slerp(clean[..., 3:7], -clean[..., 3:7], 0.5),
                clean[..., 3:7],
                atol=1e-6,
            )
        )

        model = UpstreamNoisePredictor(config)
        output = model(position, quaternion, step, wrench)
        self.assertEqual(tuple(output.shape), (1, 16, 7))
        loss = upstream_training_loss(
            model,
            clean,
            noisy,
            wrench,
            config,
            noiseadding_steps=20,
            timestep=7,
        )
        self.assertTrue(torch.isfinite(loss))

        class IdentityNoise(torch.nn.Module):
            def forward(self, pos, quat, timestep, wrench_values):
                del timestep, wrench_values
                result = torch.zeros((*pos.shape[:-1], 7), dtype=pos.dtype)
                result[..., 3] = 1.0
                return result

        reconstructed = reconstruct_upstream(
            IdentityNoise(), noisy, wrench, config
        )
        self.assertTrue(torch.allclose(reconstructed, noisy, atol=1e-6))

    def test_observation_json_adapter_is_explicit_and_validated(self) -> None:
        identity = [
            [1.0 if row == column else 0.0 for column in range(6)]
            for row in range(6)
        ]
        payload = {
            "schema_version": 1,
            "sequence": 1,
            "timestamp_s": 0.1,
            "pose_history": [[0, 0, 0, 1, 0, 0, 0]] * 16,
            "twist_history": [[0] * 6] * 16,
            "wrench_history": [[0] * 6] * 16,
            "nominal_zft": [0, 0, 0, 1, 0, 0, 0],
            "joint_position_rad": [0] * 6,
            "joint_velocity_rad_s": [0] * 6,
            "jacobian_base": identity,
            "lineage": {
                "frame_id": "base",
                "sensor_id": "kunwei",
                "calibration_hash": "c" * 64,
            },
        }
        observation = _observation_from_mapping(payload)
        self.assertEqual(observation.sequence, 1)
        with self.assertRaisesRegex(ValueError, "schema_version=1"):
            _observation_from_mapping({**payload, "schema_version": 2})


if __name__ == "__main__":
    unittest.main()
