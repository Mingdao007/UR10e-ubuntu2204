from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from ur10e_vic.tacdiffusion.benchmark import (
    benchmark_paced_predictor,
    validate_paced_benchmark_candidate,
)
from ur10e_vic.tacdiffusion.cli import build_parser, run
from ur10e_vic.tacdiffusion.dataset import (
    assign_episode_grouped_splits,
    load_expert_dataset_npz,
    validate_dataset_manifest,
    write_dataset_manifest,
    write_expert_dataset_npz,
)
from ur10e_vic.tacdiffusion.model import (
    CHECKPOINT_SCHEMA_VERSION,
    MODEL_VARIANT,
    ConditionalDDPM,
    TacDiffusionDDPMConfig,
    evaluate_checkpoint,
    load_predictor,
    torch_available,
    train_ddpm,
    validate_checkpoint_manifest,
)
from tacdiffusion_expert_fixtures import write_expert_trace_manifest


def _write_trace_manifest(
    root: Path,
    episode: str,
    split: str,
    *,
    controller_verified: bool = True,
    sample_count: int = 2,
) -> Path:
    return write_expert_trace_manifest(
        root,
        episode,
        split,
        sample_count=sample_count,
        controller_contract_valid=controller_verified,
    )


def _calibration_hash(path: Path) -> str:
    return str(
        json.loads(path.read_text(encoding="utf-8"))["artifact_bindings"]
        ["sensor_calibration"]["sha256"]
    )


def _dataset_bundle(root: Path):
    episode_ids = tuple(f"episode-{index}" for index in range(6) for _ in range(2))
    splits = assign_episode_grouped_splits(episode_ids)
    condition = np.arange(len(episode_ids) * 36, dtype=np.float32).reshape(-1, 36)
    condition *= 1e-3
    expert_f_ff = np.arange(len(episode_ids) * 6, dtype=np.float32).reshape(-1, 6)
    expert_f_ff *= 1e-2
    paths = []
    for episode in sorted(set(episode_ids)):
        split = splits[episode_ids.index(episode)]
        paths.append(_write_trace_manifest(root, episode, split))
    dataset_path = root / "expert.npz"
    write_expert_dataset_npz(
        dataset_path,
        condition=condition,
        expert_f_ff=expert_f_ff,
        episode_id=episode_ids,
        split=splits,
        canonical_frame_id="tool0_tcp",
        frame_calibration_sha256=_calibration_hash(paths[0]),
        expert_trace_manifest_paths=paths,
    )
    manifest_path = root / "dataset-manifest.json"
    manifest = write_dataset_manifest(dataset_path, manifest_path, paths)
    return dataset_path, manifest_path, paths, manifest


class DatasetPipelineTests(unittest.TestCase):
    def test_formal_npz_is_episode_grouped_hash_bound_and_portable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset_path, manifest_path, trace_paths, manifest = _dataset_bundle(root)
            dataset = load_expert_dataset_npz(dataset_path)
            self.assertEqual(dataset.condition.shape, (12, 36))
            self.assertEqual(dataset.expert_f_ff.shape, (12, 6))
            self.assertFalse(dataset.condition.flags.writeable)
            self.assertEqual(manifest["dataset_artifact"], "expert.npz")
            self.assertNotIn(str(root), json.dumps(manifest))
            self.assertTrue(manifest["episode_grouped_split_verified"])
            self.assertFalse(manifest["frame_leakage_detected"])
            self.assertEqual(len(manifest["dataset_sha256"]), 64)
            self.assertEqual(len(manifest["statistics_sha256"]), 64)
            self.assertEqual(
                validate_dataset_manifest(dataset_path, manifest_path, trace_paths),
                manifest,
            )
            parser = build_parser()
            args = parser.parse_args(
                [
                    "validate-dataset",
                    "--dataset",
                    str(dataset_path),
                    "--manifest",
                    str(manifest_path),
                    *sum(
                        (["--trace-manifest", str(path)] for path in trace_paths),
                        [],
                    ),
                ]
            )
            self.assertTrue(run(args)["training_eligible"])

    def test_episode_frame_leakage_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace = _write_trace_manifest(root, "same-episode", "train")
            with self.assertRaisesRegex(ValueError, "frame leakage"):
                write_expert_dataset_npz(
                    root / "leak.npz",
                    condition=np.zeros((2, 36), dtype=np.float32),
                    expert_f_ff=np.zeros((2, 6), dtype=np.float32),
                    episode_id=("same-episode", "same-episode"),
                    split=("train", "validation"),
                    canonical_frame_id="tool0_tcp",
                    frame_calibration_sha256=_calibration_hash(trace),
                    expert_trace_manifest_paths=(trace,),
                )

    def test_legacy_or_unverified_manifest_cannot_self_assert_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            condition = np.zeros((2, 36), dtype=np.float32)
            action = np.zeros((2, 6), dtype=np.float32)
            with self.assertRaisesRegex(ValueError, "legacy v27/v29"):
                write_expert_dataset_npz(
                    root / "legacy.npz",
                    condition=condition,
                    expert_f_ff=action,
                    episode_id=("episode", "episode"),
                    split=("train", "train"),
                    canonical_frame_id="tool0_tcp",
                    frame_calibration_sha256="1" * 64,
                    expert_trace_manifest_paths=(root / "does-not-matter.json",),
                    source_kind="legacy_v29_replay",
                )
            unverified = _write_trace_manifest(
                root, "episode", "train", controller_verified=False
            )
            with self.assertRaisesRegex(ValueError, "do not verify the controller"):
                write_expert_dataset_npz(
                    root / "unverified.npz",
                    condition=condition,
                    expert_f_ff=action,
                    episode_id=("episode", "episode"),
                    split=("train", "train"),
                    canonical_frame_id="tool0_tcp",
                    frame_calibration_sha256=_calibration_hash(unverified),
                    expert_trace_manifest_paths=(unverified,),
                )

    def test_trace_manifest_file_rehash_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset_path, manifest_path, trace_paths, _ = _dataset_bundle(root)
            trace_paths[0].write_text(
                trace_paths[0].read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "artifact hash mismatch"):
                validate_dataset_manifest(dataset_path, manifest_path, trace_paths)

    def test_trace_manifest_sample_count_must_match_episode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace = _write_trace_manifest(
                root, "episode", "train", sample_count=3
            )
            with self.assertRaisesRegex(ValueError, "sample count mismatch"):
                write_expert_dataset_npz(
                    root / "count-mismatch.npz",
                    condition=np.zeros((2, 36), dtype=np.float32),
                    expert_f_ff=np.zeros((2, 6), dtype=np.float32),
                    episode_id=("episode", "episode"),
                    split=("train", "train"),
                    canonical_frame_id="tool0_tcp",
                    frame_calibration_sha256=_calibration_hash(trace),
                    expert_trace_manifest_paths=(trace,),
                )


class ModelContractTests(unittest.TestCase):
    def test_clean_room_ddpm_configuration_is_pinned_and_inactive(self) -> None:
        config = TacDiffusionDDPMConfig()
        self.assertEqual(
            (
                config.observation_steps,
                config.per_observation_dim,
                config.condition_dim,
                config.action_dim,
                config.embedding_dim,
                config.hidden_dim,
                config.diffusion_steps,
                config.beta_start,
                config.beta_end,
                config.seed,
            ),
            (2, 18, 36, 6, 128, 512, 50, 1e-4, 0.02, 42),
        )
        self.assertFalse(config.active_enabled)
        with self.assertRaisesRegex(ValueError, "active mode"):
            TacDiffusionDDPMConfig(active_enabled=True)

    @unittest.skipIf(torch_available(), "explicit missing-PyTorch path only")
    def test_missing_torch_has_explicit_non_model_fallback_boundary(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "require optional PyTorch"):
            ConditionalDDPM()

    def test_legacy_v1_checkpoint_manifest_is_explicitly_nonfaithful(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "legacy.pt"
            checkpoint.write_bytes(b"legacy")
            manifest = root / "legacy.json"
            manifest.write_text('{"schema_version": 1}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "nonfaithful"):
                validate_checkpoint_manifest(checkpoint, manifest)

    @unittest.skipUnless(torch_available(), "optional PyTorch environment is not active")
    def test_denoiser_has_separate_embeddings_timesiren_and_bn_gelu_blocks(self) -> None:
        model = ConditionalDDPM()
        estimator = model.noise_estimator
        self.assertIsNot(
            estimator.current_observation_embedding,
            estimator.previous_observation_embedding,
        )
        names = {type(module).__name__ for module in estimator.modules()}
        self.assertIn("TimeSiren", names)
        self.assertIn("BatchNorm1d", names)
        self.assertIn("GELU", names)

    @unittest.skipUnless(torch_available(), "optional PyTorch environment is not active")
    def test_train_checkpoint_and_infer_are_hash_bound_when_torch_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset_path, dataset_manifest_path, trace_paths, dataset_manifest = (
                _dataset_bundle(root)
            )
            checkpoint = root / "model.pt"
            checkpoint_manifest = root / "model-manifest.json"
            trained = train_ddpm(
                dataset_path=dataset_path,
                dataset_manifest_path=dataset_manifest_path,
                expert_trace_manifest_paths=trace_paths,
                checkpoint_path=checkpoint,
                checkpoint_manifest_path=checkpoint_manifest,
                epochs=1,
                batch_size=4,
                device="cpu",
                evidence_scope="fixture_only",
            )
            validated = validate_checkpoint_manifest(
                checkpoint,
                checkpoint_manifest,
                expected_dataset_sha256=str(dataset_manifest["dataset_sha256"]),
            )
            self.assertEqual(trained, validated)
            predictor = load_predictor(
                checkpoint,
                expected_checkpoint_sha256=str(trained["checkpoint_sha256"]),
                expected_dataset_sha256=str(trained["dataset_sha256"]),
                device="cpu",
            )
            result = predictor.predict(np.zeros(36), seed=42)
            self.assertEqual(len(result.raw_f_df), 6)
            self.assertEqual(result.checkpoint_sha256, trained["checkpoint_sha256"])
            self.assertEqual(trained["schema_version"], CHECKPOINT_SCHEMA_VERSION)
            self.assertEqual(trained["model_variant"], MODEL_VARIANT)
            self.assertEqual(trained["normalization"]["scope"], "train_split_only")
            self.assertFalse(trained["resume"]["resumed"])

            resumed_checkpoint = root / "model-resumed.pt"
            resumed_manifest = root / "model-resumed-manifest.json"
            resumed = train_ddpm(
                dataset_path=dataset_path,
                dataset_manifest_path=dataset_manifest_path,
                expert_trace_manifest_paths=trace_paths,
                checkpoint_path=resumed_checkpoint,
                checkpoint_manifest_path=resumed_manifest,
                epochs=1,
                batch_size=4,
                device="cpu",
                resume_checkpoint_path=checkpoint,
                evidence_scope="fixture_only",
            )
            self.assertTrue(resumed["resume"]["resumed"])
            self.assertTrue(resumed["resume"]["optimizer_state_restored"])
            self.assertTrue(resumed["resume"]["scheduler_state_restored"])
            self.assertTrue(resumed["resume"]["rng_state_restored"])
            self.assertEqual(resumed["epoch_completed"], 2)
            evaluation = evaluate_checkpoint(
                checkpoint_path=resumed_checkpoint,
                checkpoint_manifest_path=resumed_manifest,
                dataset_path=dataset_path,
                dataset_manifest_path=dataset_manifest_path,
                expert_trace_manifest_paths=trace_paths,
                split="validation",
                max_samples=1,
                device="cpu",
            )
            self.assertEqual(
                evaluation["schema"], "ur10e_tacdiffusion_evaluation/v1"
            )
            self.assertEqual(evaluation["evidence_scope"], "fixture_only")
            self.assertFalse(evaluation["simulation_run"])
            self.assertFalse(evaluation["active_enabled"])


class PacedBenchmarkTests(unittest.TestCase):
    class FakeClock:
        def __init__(self) -> None:
            self.now = 0.0

        def perf_counter(self) -> float:
            return self.now

        def sleep(self, duration: float) -> None:
            self.now += max(0.0, duration)

    class FakePredictor:
        device = "cpu"

        def __init__(self, clock, latency_s=0.0005) -> None:
            self.clock = clock
            self.latency_s = latency_s

        def predict(self, _condition, *, seed):
            del seed
            self.clock.now += self.latency_s
            return (0.0,) * 6

    def test_short_benchmark_is_raw_diagnostic_never_acceptance(self) -> None:
        clock = self.FakeClock()
        candidate = benchmark_paced_predictor(
            self.FakePredictor(clock),
            (0.0,) * 36,
            duration_per_rate_s=0.02,
            checkpoint_sha256="a" * 64,
            dataset_sha256="b" * 64,
            clock=clock.perf_counter,
            sleeper=clock.sleep,
        )
        self.assertFalse(candidate["producer_selection_eligible"])
        validated = validate_paced_benchmark_candidate(
            candidate,
            expected_checkpoint_sha256="a" * 64,
            expected_dataset_sha256="b" * 64,
        )
        self.assertEqual(
            validated["validation_status"], "validated_short_diagnostic_only"
        )
        self.assertFalse(validated["selection_eligible"])
        self.assertIsNone(validated["selected_rate_hz"])

    def test_complete_60s_raw_ticks_select_highest_passing_rate(self) -> None:
        clock = self.FakeClock()
        candidate = benchmark_paced_predictor(
            self.FakePredictor(clock),
            (0.0,) * 36,
            duration_per_rate_s=60.0,
            checkpoint_sha256="c" * 64,
            dataset_sha256="d" * 64,
            clock=clock.perf_counter,
            sleeper=clock.sleep,
        )
        validated = validate_paced_benchmark_candidate(
            candidate,
            expected_checkpoint_sha256="c" * 64,
            expected_dataset_sha256="d" * 64,
        )
        self.assertTrue(validated["selection_eligible"])
        self.assertEqual(validated["selected_rate_hz"], 500)
        self.assertEqual(
            [item["rate_hz"] for item in validated["recomputed_rate_results"]],
            [500, 200, 100, 50],
        )

    def test_harness_hash_and_raw_summary_tampering_fail_closed(self) -> None:
        clock = self.FakeClock()
        candidate = benchmark_paced_predictor(
            self.FakePredictor(clock),
            (0.0,) * 36,
            duration_per_rate_s=0.02,
            checkpoint_sha256="e" * 64,
            dataset_sha256="f" * 64,
            clock=clock.perf_counter,
            sleeper=clock.sleep,
        )
        candidate["artifact_bindings"]["harness_source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "harness source binding"):
            validate_paced_benchmark_candidate(
                candidate,
                expected_checkpoint_sha256="e" * 64,
                expected_dataset_sha256="f" * 64,
            )


if __name__ == "__main__":
    unittest.main()
