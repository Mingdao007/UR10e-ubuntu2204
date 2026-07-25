import json
import hashlib
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

import numpy as np

from ur10e_vic.tacdiffusion.mainline_dataset import validate_mainline_dataset, write_mainline_dataset, write_mainline_manifest
from ur10e_vic.tacdiffusion.mainline_model import DeterministicMainlinePredictor, MainlineModelConfig
from ur10e_vic.tacdiffusion.promotion import (
    ShadowTrace,
    evaluate_offline_replay,
    run_offline_shadow_gate,
    validate_live_authorization,
    validate_promotion_manifest,
    write_blocked_promotion_manifest,
    write_promotion_manifest,
)
from ur10e_vic.tacdiffusion.remote_headless import (
    ArmAuthorization,
    BridgeAuthorization,
    LoadAuthorization,
    MotionAuthorization,
    PlayAuthorization,
    RemoteEvidence,
    RemoteLifecycleExecutor,
    RemoteLifecycleMode,
    RemoteStage,
    inspect_remote_headless,
)
from ur10e_vic.tacdiffusion.direct_torque_receiver import parse_receiver_source, receiver_empty_wait
from ur10e_vic.tacdiffusion.raw_artifact import DurableRawFrameWriter, RawFrameRecord, derive_training_view, read_raw_frames
from ur10e_vic.tacdiffusion.mainline_model import ConditionalActionModel, benchmark_runtime, load_mainline_checkpoint, train_mainline_model, torch
from ur10e_vic.tacdiffusion.checkpoint import CheckpointBinding, validate_checkpoint_binding, write_checkpoint_binding
from ur10e_vic.tacdiffusion.environment import immutable_environment_fingerprint


def raw_frame(episode_id, index, *, model_sequence=None, source_timestamp_s=None, frame_timestamp_s=None, model_timestamp_s=None):
    timestamp = index * 0.002 if frame_timestamp_s is None else frame_timestamp_s
    source_timestamp = timestamp if source_timestamp_s is None else source_timestamp_s
    # 500 Hz evidence holds each 100 Hz model packet for five control ticks.
    model_index = index // 5
    model_timestamp = model_index * 0.01 if model_timestamp_s is None else model_timestamp_s
    return RawFrameRecord(
        episode_id=episode_id,
        sample_index=index,
        source_timestamp_s=source_timestamp,
        frame_timestamp_s=timestamp,
        model_timestamp_s=model_timestamp,
        raw_f_df=(1,) * 6,
        filtered_f_ff=(2,) * 6,
        filter_velocity=(3,) * 6,
        stiffness_k=(600,) * 6,
        damping_d=(40,) * 6,
        model_sequence=model_index if model_sequence is None else model_sequence,
        inference_latency_s=0.001,
    )


class SyntheticFixtureTransport:
    """In-memory fixture only; this class has no socket or controller path."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def mutate(self, stage, authorization):
        self.calls.append((stage, type(authorization).__name__))
        return self.responses.get(stage, {
            RemoteStage.LOAD: "load accepted: true",
            RemoteStage.PLAY: "play accepted: true",
            RemoteStage.BRIDGE_READY: "bridge ready: true",
            RemoteStage.ARM: "arm accepted: true",
            RemoteStage.MOTION: "motion accepted: true",
        }[stage])


class RemoteAndDataTests(unittest.TestCase):
    def test_remote_gates_are_independent_and_no_motion_by_default(self):
        report = inspect_remote_headless(RemoteEvidence(True, True, True, program_loaded=True))
        statuses = dict(report.statuses)
        self.assertTrue(statuses["REMOTE_CONTROL"])
        self.assertTrue(statuses["LOAD"])
        self.assertFalse(statuses["PLAY"])
        self.assertFalse(statuses["BRIDGE_READY"])
        self.assertTrue(report.no_motion)

    def test_dashboard_and_receiver_parsers_reject_ambiguous_or_live_contracts(self):
        from ur10e_vic.tacdiffusion.remote_headless import parse_dashboard_response, parse_load_response, parse_play_response, build_dry_run_command_plan
        text = "is in remote control: true\nRTDE read-only reachable: true\nprogram loaded: false\nProgram running: false\n"
        evidence = parse_dashboard_response(text)
        self.assertTrue(evidence.remote_control)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            parse_load_response("load accepted: true\nload accepted: false\n")
        self.assertTrue(parse_play_response("play accepted: true"))
        self.assertEqual([name for name, _ in build_dry_run_command_plan()], ["REMOTE_CONTROL", "DASHBOARD_REACHABLE", "RTDE_READ_ONLY_REACHABLE", "LOAD", "PLAY", "BRIDGE_READY", "ARM", "MOTION"])
        with self.assertRaisesRegex(ValueError, "stale"):
            parse_dashboard_response(text, captured_at_s=1.0, now_s=3.0, max_age_s=1.0)
        contract = parse_receiver_source()
        self.assertFalse(contract.physical_io_enabled)
        self.assertEqual(receiver_empty_wait(queue_empty=True, explicit_end=False, explicit_drain=False), "WAIT_FOREVER")
        self.assertEqual(receiver_empty_wait(queue_empty=True, explicit_end=True, explicit_drain=False), "RETURN_CAMPAIGN_HOME")

    def test_remote_no_motion_executor_has_zero_mutation_calls_and_stops_before_load(self):
        transport = SyntheticFixtureTransport()
        report = RemoteLifecycleExecutor(
            RemoteEvidence(True, True, True),
            transport=transport,
        ).execute()
        self.assertTrue(report.no_motion)
        self.assertEqual(report.issued_commands, ())
        self.assertEqual(transport.calls, [])
        self.assertEqual(report.next_required, ("LOAD",))
        self.assertEqual(report.sleep_calls, 0)
        self.assertEqual(report.mode, "NO_MOTION")
        self.assertEqual(
            [name for name, _ in report.statuses],
            ["REMOTE_CONTROL", "DASHBOARD_REACHABLE", "RTDE_READ_ONLY_REACHABLE", "LOAD", "PLAY", "BRIDGE_READY", "ARM", "MOTION"],
        )

    def test_remote_synthetic_fixture_gates_are_ordered_and_typed(self):
        transport = SyntheticFixtureTransport()
        authorizations = {
            RemoteStage.LOAD: LoadAuthorization("fixture-load"),
            RemoteStage.PLAY: PlayAuthorization("fixture-play"),
            RemoteStage.BRIDGE_READY: BridgeAuthorization("fixture-bridge"),
            RemoteStage.ARM: ArmAuthorization("fixture-arm"),
            RemoteStage.MOTION: MotionAuthorization("fixture-motion"),
        }
        report = RemoteLifecycleExecutor(
            RemoteEvidence(True, True, True),
            transport=transport,
            mode=RemoteLifecycleMode.SYNTHETIC_FIXTURE,
            authorizations=authorizations,
        ).execute()
        self.assertFalse(report.no_motion)
        self.assertEqual(report.mode, "SYNTHETIC_FIXTURE")
        self.assertEqual(report.next_required, ())
        self.assertEqual(report.issued_commands, ("LOAD", "PLAY", "BRIDGE_READY", "ARM", "MOTION"))
        self.assertEqual([stage.value for stage, _ in transport.calls], ["LOAD", "PLAY", "BRIDGE_READY", "ARM", "MOTION"])
        self.assertEqual(report.sleep_calls, 0)

    def test_remote_cross_gate_authorization_and_negative_response_fail_closed_without_retry(self):
        transport = SyntheticFixtureTransport()
        load = LoadAuthorization("fixture-load")
        report = RemoteLifecycleExecutor(
            RemoteEvidence(True, True, True),
            transport=transport,
            mode=RemoteLifecycleMode.SYNTHETIC_FIXTURE,
            authorizations={RemoteStage.LOAD: load, RemoteStage.PLAY: load},
        ).execute()
        self.assertTrue(report.no_motion)
        self.assertEqual(report.next_required, ("PLAY",))
        self.assertEqual(report.issued_commands, ())
        self.assertEqual(transport.calls, [])
        self.assertIn("typed authorization", report.failure_reason)

        transport = SyntheticFixtureTransport({RemoteStage.LOAD: "load accepted: false"})
        report = RemoteLifecycleExecutor(
            RemoteEvidence(True, True, True),
            transport=transport,
            mode=RemoteLifecycleMode.SYNTHETIC_FIXTURE,
            authorizations={
                RemoteStage.LOAD: load,
                RemoteStage.PLAY: PlayAuthorization("fixture-play"),
                RemoteStage.BRIDGE_READY: BridgeAuthorization("fixture-bridge"),
                RemoteStage.ARM: ArmAuthorization("fixture-arm"),
                RemoteStage.MOTION: MotionAuthorization("fixture-motion"),
            },
        ).execute()
        self.assertTrue(report.no_motion)
        self.assertEqual(report.next_required, ("LOAD",))
        self.assertEqual(report.issued_commands, ("LOAD",))
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("negative", report.failure_reason)

        transport = SyntheticFixtureTransport()
        report = RemoteLifecycleExecutor(
            RemoteEvidence(True, True, True),
            transport=transport,
            mode=RemoteLifecycleMode.SYNTHETIC_FIXTURE,
            authorizations={RemoteStage.LOAD: True},
        ).execute()
        self.assertTrue(report.no_motion)
        self.assertEqual(transport.calls, [])
        self.assertIn("typed authorization", report.failure_reason)

    def test_raw_500hz_artifact_is_durable_and_training_view_does_not_delete_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.jsonl"
            with DurableRawFrameWriter(path, episode_id="episode-1") as writer:
                for index in range(20):
                    writer.append(raw_frame("episode-1", index))
            frames = read_raw_frames(path)
            self.assertEqual(len(frames), 20)
            self.assertEqual(len(derive_training_view(path, target_rate_hz=50)), 2)
            self.assertEqual(len(derive_training_view(path, target_rate_hz=100)), 4)
            self.assertEqual(len(read_raw_frames(path)), 20)
            self.assertEqual(frames[0].source_timestamp_s, 0.0)
            self.assertEqual(frames[0].frame_timestamp_s, 0.0)
            self.assertEqual(frames[0].model_timestamp_s, 0.0)
            self.assertEqual(frames[0].model_sequence, 0)
            self.assertEqual(frames[0].action_schema, "ur10e_tacdiffusion_action/v2")

    def test_raw_writer_reopens_and_appends_without_truncating_valid_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.jsonl"
            with DurableRawFrameWriter(path, episode_id="episode-1") as writer:
                writer.append(raw_frame("episode-1", 0))
                writer.append(raw_frame("episode-1", 1))
            with DurableRawFrameWriter(path, episode_id="episode-1") as writer:
                writer.append(raw_frame("episode-1", 2))
            frames = read_raw_frames(path)
            self.assertEqual([frame.sample_index for frame in frames], [0, 1, 2])
            self.assertEqual(frames[-1].model_sequence, 0)

    def test_raw_writer_discards_only_incomplete_non_newline_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.jsonl"
            with DurableRawFrameWriter(path, episode_id="episode-1") as writer:
                writer.append(raw_frame("episode-1", 0))
            with path.open("ab") as handle:
                handle.write(b'{"schema":"ur10e_tacdiffusion_raw_frame/v3"')
            with DurableRawFrameWriter(path, episode_id="episode-1") as writer:
                writer.append(raw_frame("episode-1", 1))
            self.assertEqual([frame.sample_index for frame in read_raw_frames(path)], [0, 1])

    def test_raw_writer_rejects_complete_valid_json_without_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.jsonl"
            with DurableRawFrameWriter(path, episode_id="episode-1") as writer:
                writer.append(raw_frame("episode-1", 0))
            complete_row = json.dumps(raw_frame("episode-1", 1).as_json(), sort_keys=True, separators=(",", ":")).encode("utf-8")
            with path.open("ab") as handle:
                handle.write(complete_row)
            with self.assertRaisesRegex(ValueError, "complete JSON row"):
                DurableRawFrameWriter(path, episode_id="episode-1")

    def test_raw_writer_rejects_complete_invalid_tail_and_identity_or_regressions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.jsonl"
            with DurableRawFrameWriter(path, episode_id="episode-1") as writer:
                writer.append(raw_frame("episode-1", 0, model_sequence=1))
                with self.assertRaisesRegex(ValueError, "timestamp"):
                    writer.append(raw_frame("episode-1", 1, source_timestamp_s=0.0))
                with self.assertRaisesRegex(ValueError, "sequence"):
                    writer.append(raw_frame("episode-1", 1, model_sequence=0))
            with path.open("ab") as handle:
                handle.write(b"not-json\n")
            with self.assertRaisesRegex(ValueError, "row 1"):
                DurableRawFrameWriter(path, episode_id="episode-1")
            with self.assertRaisesRegex(ValueError, "row 1"):
                read_raw_frames(path)

    def test_raw_writer_rejects_model_timestamp_and_sequence_regressions_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.jsonl"
            with DurableRawFrameWriter(path, episode_id="episode-1") as writer:
                writer.append(raw_frame("episode-1", 0, source_timestamp_s=0.02, frame_timestamp_s=0.02, model_sequence=3, model_timestamp_s=0.01))
            with DurableRawFrameWriter(path, episode_id="episode-1") as writer:
                with self.assertRaisesRegex(ValueError, "model timestamp"):
                    writer.append(raw_frame("episode-1", 1, source_timestamp_s=0.022, frame_timestamp_s=0.022, model_sequence=4, model_timestamp_s=0.001))
                with self.assertRaisesRegex(ValueError, "model sequence"):
                    writer.append(raw_frame("episode-1", 1, source_timestamp_s=0.024, frame_timestamp_s=0.024, model_sequence=2, model_timestamp_s=0.02))

    def test_raw_writer_requires_per_row_durability_and_enforces_model_frame_causality(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "episode.jsonl"
            with self.assertRaisesRegex(ValueError, "flush_every"):
                DurableRawFrameWriter(path, episode_id="episode-1", flush_every=2)
            with self.assertRaisesRegex(ValueError, "model timestamp"):
                raw_frame("episode-1", 0, model_timestamp_s=0.001)

    def test_environment_cache_and_checkpoint_binding_are_hash_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.txt"
            source.write_text("immutable\n", encoding="utf-8")
            fingerprint = immutable_environment_fingerprint([source], cache_path=root / "env.json")
            self.assertEqual(fingerprint, immutable_environment_fingerprint([source], cache_path=root / "env.json"))
            binding = CheckpointBinding("ur10e_tacdiffusion_checkpoint/v3", "a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64, ("f" * 64,), "1" * 64, {"observation_dimension": 84, "action_dimension": 12, "diffusion_steps": 50, "model_update_rate_hz": 100}, fingerprint, "2" * 64)
            write_checkpoint_binding(root / "checkpoint.json", binding)
            self.assertEqual(validate_checkpoint_binding(root / "checkpoint.json"), binding)

    def test_84d_12d_dataset_is_hash_bound_and_model_reads_continuous_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = np.zeros((6, 84), dtype=np.float32)
            actions = np.zeros((6, 12), dtype=np.float32)
            manifest = write_mainline_dataset(root / "mainline.npz", observations=observations, actions=actions, episode_ids=["a"] * 3 + ["b"] * 3, splits=["train"] * 3 + ["validation"] * 3, timestamps_s=np.arange(6, dtype=np.float64) * 0.002, source_raw_artifact_hashes={"a": "d" * 64, "b": "e" * 64}, surface_calibration_sha256="a" * 64, action_profile_sha256="b" * 64, filter_profile_sha256="f" * 64, normalization_sha256="c" * 64)
            self.assertEqual(validate_mainline_dataset(root / "mainline.npz", manifest)["action_dimension"], 12)
            predictor = DeterministicMainlinePredictor(MainlineModelConfig(model_update_rate_hz=100))
            self.assertEqual(len(predictor.predict(np.zeros(84))), 12)

    def test_mainline_dataset_rejects_order_or_split_tamper_and_rewrites_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = np.zeros((4, 84), dtype=np.float32)
            actions = np.zeros((4, 12), dtype=np.float32)
            kwargs = dict(
                observations=observations,
                actions=actions,
                episode_ids=["a", "a", "b", "b"],
                splits=["train", "train", "validation", "validation"],
                timestamps_s=np.asarray([0.0, 0.002, 0.0, 0.002], dtype=np.float64),
                source_raw_artifact_hashes={"a": "d" * 64, "b": "e" * 64},
                surface_calibration_sha256="a" * 64,
                action_profile_sha256="b" * 64,
                filter_profile_sha256="f" * 64,
                normalization_sha256="c" * 64,
            )
            dataset = root / "mainline.npz"
            manifest = write_mainline_dataset(dataset, **kwargs)
            manifest_path = root / "mainline.manifest.json"
            write_mainline_manifest(manifest_path, manifest)
            original = manifest_path.read_bytes()
            write_mainline_manifest(manifest_path, manifest)
            self.assertEqual(manifest_path.read_bytes(), original)
            self.assertEqual(validate_mainline_dataset(dataset, manifest)["row_count"], 4)
            with self.assertRaisesRegex(ValueError, "strictly increase"):
                write_mainline_dataset(dataset, **{**kwargs, "timestamps_s": [0.0, 0.0, 0.0, 0.002]})
            with self.assertRaisesRegex(ValueError, "leakage"):
                write_mainline_dataset(dataset, **{**kwargs, "splits": ["train", "validation", "validation", "validation"]})
            with np.load(dataset, allow_pickle=False) as payload:
                tampered = {key: payload[key] for key in payload.files}
            tampered["actions"] = tampered["actions"].copy()
            tampered["actions"][0, 0] = 1.0
            np.savez_compressed(dataset, **tampered)
            with self.assertRaisesRegex(ValueError, "artifact hash"):
                validate_mainline_dataset(dataset, manifest)

    def test_mainline_dataset_is_nonempty_and_source_hashes_cover_exactly_episodes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = dict(
                observations=np.zeros((1, 84), dtype=np.float32),
                actions=np.zeros((1, 12), dtype=np.float32),
                episode_ids=["episode"],
                splits=["train"],
                timestamps_s=[0.0],
                surface_calibration_sha256="a" * 64,
                action_profile_sha256="b" * 64,
                filter_profile_sha256="c" * 64,
                normalization_sha256="d" * 64,
            )
            with self.assertRaisesRegex(ValueError, "cover exactly"):
                write_mainline_dataset(root / "missing.npz", source_raw_artifact_hashes={}, **common)
            with self.assertRaisesRegex(ValueError, "nonempty"):
                write_mainline_dataset(
                    root / "empty.npz",
                    observations=np.zeros((0, 84), dtype=np.float32),
                    actions=np.zeros((0, 12), dtype=np.float32),
                    episode_ids=[],
                    splits=[],
                    timestamps_s=np.asarray([], dtype=np.float64),
                    source_raw_artifact_hashes={},
                    surface_calibration_sha256="a" * 64,
                    action_profile_sha256="b" * 64,
                    filter_profile_sha256="c" * 64,
                    normalization_sha256="d" * 64,
                )

    def test_shortest_shadow_gate_uses_virtual_clock_and_two_representative_traces(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.jsonl"
            with DurableRawFrameWriter(path, episode_id="episode-1") as writer:
                for index in range(50):
                    writer.append(raw_frame("episode-1", index))
            replay = evaluate_offline_replay(path)
            self.assertTrue(replay.passed)
            result = run_offline_shadow_gate(offline_replay_artifact=path, traces=(ShadowTrace("smooth_low_curvature", 45.0, 0.001, 5.0), ShadowTrace("turning_high_curvature", 45.0, 0.002, 8.0)))
            self.assertFalse(result.active_allowed)
            self.assertIn("shadow_artifact_paths_missing", result.blockers)
            self.assertEqual(result.total_virtual_duration_s, 90.0)
            self.assertEqual(result.sleep_calls, 0)

    def test_empty_valid_raw_artifact_returns_finite_zero_replay_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty-raw.jsonl"
            with DurableRawFrameWriter(path, episode_id="empty-episode"):
                pass
            metrics = evaluate_offline_replay(path)
            self.assertFalse(metrics.passed)
            self.assertEqual(metrics.blockers, ("empty_raw_artifact",))
            self.assertEqual(metrics.row_count, 0)
            self.assertEqual(metrics.max_raw_force_norm_n, 0.0)
            self.assertEqual(metrics.max_filtered_force_norm_n, 0.0)
            self.assertEqual(metrics.max_filter_velocity, 0.0)
            self.assertEqual(metrics.p99_inference_latency_s, 0.0)

    def test_bare_replay_boolean_cannot_promote(self):
        result = run_offline_shadow_gate(offline_replay_passed=True, traces=(ShadowTrace("smooth_low_curvature", 45.0, 0.001, 5.0), ShadowTrace("turning_high_curvature", 45.0, 0.002, 8.0)))
        self.assertFalse(result.active_allowed)
        self.assertIn("offline_replay_artifact_missing", result.blockers)

    @unittest.skipUnless(torch is not None, "PyTorch is unavailable")
    def test_real_conditional_model_trains_validates_benchmarks_and_exports_torchscript(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = np.zeros((8, 84), dtype=np.float32)
            observations[:, 0] = np.arange(8)
            actions = np.zeros((8, 12), dtype=np.float32)
            actions[:, 0] = observations[:, 0] * 0.1
            splits = ["train"] * 6 + ["validation"] * 2
            checkpoint = root / "model.pt"
            config = MainlineModelConfig(hidden_dimension=64, model_update_rate_hz=100)
            result = train_mainline_model(observations, actions, splits, checkpoint_path=checkpoint, config=config, epochs=1, batch_size=2)
            self.assertEqual(result["model_schema"], "ur10e_tacdiffusion_checkpoint/v3")
            self.assertEqual(result["diffusion_steps"], 50)
            model = load_mainline_checkpoint(checkpoint)["model"]
            benchmark = benchmark_runtime(model, iterations=3, warmup_iterations=1)
            self.assertEqual(benchmark["diffusion_steps"], 50)
            self.assertEqual(benchmark["distinct_timesteps"], 50)
            self.assertEqual(benchmark["warmup_iterations"], 1)
            self.assertTrue(benchmark["warmup_excluded"])
            self.assertIn("selected_rate_hz", benchmark)
            self.assertIn(benchmark["selected_rate_hz"], (50, 100, None))

    def test_mainline_model_rejects_unsupported_rates_and_tampered_checkpoint(self):
        for rate in (200, 500):
            with self.assertRaisesRegex(ValueError, "rate"):
                MainlineModelConfig(model_update_rate_hz=rate)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = np.zeros((4, 84), dtype=np.float32)
            actions = np.zeros((4, 12), dtype=np.float32)
            checkpoint = root / "model.pt"
            train_mainline_model(observations, actions, ["train", "train", "validation", "validation"], checkpoint_path=checkpoint, config=MainlineModelConfig(hidden_dimension=32), epochs=1, batch_size=2)
            self.assertEqual(load_mainline_checkpoint(checkpoint)["config"].diffusion_steps, 50)
            tampered = root / "tampered.pt"
            tampered.write_bytes(b"tampered checkpoint bytes")
            with self.assertRaises(ValueError):
                load_mainline_checkpoint(tampered)

    def test_mainline_model_requires_loaded_marker_and_strict_split_and_normalization(self):
        with self.assertRaisesRegex(ValueError, "loaded checkpoint"):
            benchmark_runtime(ConditionalActionModel(MainlineModelConfig(hidden_dimension=32)), iterations=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = np.zeros((4, 84), dtype=np.float32)
            actions = np.zeros((4, 12), dtype=np.float32)
            checkpoint = root / "model.pt"
            train_mainline_model(observations, actions, ["train", "train", "validation", "validation"], checkpoint_path=checkpoint, config=MainlineModelConfig(hidden_dimension=32), epochs=1, batch_size=2)
            for kind in ("std", "indices"):
                tampered = root / f"tampered-{kind}.pt"
                if torch is None:
                    with np.load(checkpoint, allow_pickle=False) as payload:
                        arrays = {key: payload[key] for key in payload.files}
                    if kind == "std":
                        arrays["observation_std"] = np.zeros(84, dtype=np.float32)
                    else:
                        arrays["train_indices"] = np.asarray([0, 2], dtype=np.int64)
                        arrays["validation_indices"] = np.asarray([3], dtype=np.int64)
                    with tampered.open("wb") as handle:
                        np.savez(handle, **arrays)
                else:
                    payload = torch.load(checkpoint, map_location="cpu", weights_only=False)
                    if kind == "std":
                        payload["normalization"]["observation_std"][0] = 0.0
                    else:
                        payload["train_indices"] = [0, 2]
                        payload["validation_indices"] = [3]
                    torch.save(payload, tampered)
                with self.assertRaises(ValueError):
                    load_mainline_checkpoint(tampered)


class CheckpointBindingTests(unittest.TestCase):
    @staticmethod
    def binding(rate_hz=100):
        return CheckpointBinding(
            "ur10e_tacdiffusion_checkpoint/v3",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "d" * 64,
            "e" * 64,
            ("f" * 64, "0" * 64),
            "1" * 64,
            {"observation_dimension": 84, "action_dimension": 12, "diffusion_steps": 50, "model_update_rate_hz": rate_hz},
            "2" * 64,
            "3" * 64,
        )

    def test_v3_round_trip_is_deterministic_and_crash_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint-binding.json"
            binding = self.binding()
            write_checkpoint_binding(path, binding)
            first = path.read_bytes()
            write_checkpoint_binding(path, binding)
            self.assertEqual(first, path.read_bytes())
            self.assertEqual(validate_checkpoint_binding(path), binding)
            self.assertEqual(json.loads(first)["schema_version"], "ur10e_tacdiffusion_checkpoint/v3")

    def test_tampered_source_hash_and_extra_or_missing_lineage_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "checkpoint-binding.json"
            binding = self.binding()
            write_checkpoint_binding(path, binding)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["source_hashes"][0] = "g" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source_hashes"):
                validate_checkpoint_binding(path)
            payload = binding.payload()
            payload.pop("dataset_sha256")
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "lineage"):
                validate_checkpoint_binding(path)
            payload = binding.payload()
            payload["unexpected_lineage"] = "x"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "lineage"):
                validate_checkpoint_binding(path)

    def test_v2_and_invalid_model_rates_are_rejected_without_torch(self):
        binding = self.binding()
        with self.assertRaisesRegex(ValueError, "unsupported checkpoint schema"):
            replace(binding, schema_version="ur10e_tacdiffusion_checkpoint/v2")
        for rate_hz in (200, 500):
            with self.assertRaisesRegex(ValueError, "50 or 100"):
                replace(binding, model_config={**binding.model_config, "model_update_rate_hz": rate_hz})


class PromotionManifestTests(unittest.TestCase):
    def _fixtures(self, root):
        root.mkdir(parents=True, exist_ok=True)
        model = root / "model-fixture.bin"
        model.write_bytes(b"deterministic model fixture")
        binding = CheckpointBindingTests.binding()
        binding = replace(binding, checkpoint_sha256=hashlib.sha256(model.read_bytes()).hexdigest())
        binding_path = root / "checkpoint-binding.json"
        write_checkpoint_binding(binding_path, binding)
        raw_path = root / "raw-fixture.jsonl"
        with DurableRawFrameWriter(raw_path, episode_id="fixture-episode") as writer:
            for index in range(50):
                writer.append(raw_frame("fixture-episode", index))
        shadows = []
        for name in ("smooth_low_curvature", "turning_high_curvature"):
            shadow_path = root / f"{name}-fixture.json"
            unsigned = {
                "name": name,
                "capture_mode": "live_shadow",
                "hardware_run": True,
                "fixture": True,
                "duration_s": 45.0,
                "max_tracking_error_m": 0.001,
                "max_force_norm_n": 5.0,
                "nonfinite_count": 0,
                "nonfinite_rows": 0,
            }
            payload = {**unsigned, "artifact_sha256": hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
            shadow_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            shadows.append(shadow_path)
        return binding_path, model, raw_path, shadows

    @staticmethod
    def resign(path, payload):
        unsigned = dict(payload)
        unsigned.pop("manifest_sha256", None)
        payload["manifest_sha256"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

    @staticmethod
    def live_authorization_payload(auth_path, promotion_path, binding_path, binding):
        unsigned = {
            "schema": "ur10e_live_authorization/v3",
            "explicit_live_authorization": True,
            "promotion_manifest_relative_path": str(promotion_path.relative_to(auth_path.parent)),
            "promotion_manifest_sha256": hashlib.sha256(promotion_path.read_bytes()).hexdigest(),
            "checkpoint_binding_relative_path": str(binding_path.relative_to(auth_path.parent)),
            "checkpoint_binding_sha256": hashlib.sha256(binding_path.read_bytes()).hexdigest(),
            "surface_calibration_sha256": binding.surface_calibration_sha256,
            "action_profile_sha256": binding.action_profile_sha256,
            "filter_profile_sha256": binding.filter_profile_sha256,
            "normalization_sha256": binding.normalization_sha256,
        }
        return {**unsigned, "authorization_sha256": hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}

    def test_fixture_manifest_is_hash_linked_but_blocked_without_real_live_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding, model, raw, shadows = self._fixtures(root)
            manifest_path = root / "promotion-fixture.json"
            manifest = write_promotion_manifest(manifest_path, checkpoint_binding_path=binding, model_checkpoint_path=model, raw_replay_artifact_path=raw, shadow_artifact_paths=shadows)
            self.assertFalse(manifest["active_allowed"])
            self.assertIn("synthetic_fixture_not_live_evidence", manifest["blockers"])
            self.assertEqual(manifest["representative_names"], ["smooth_low_curvature", "turning_high_curvature"])
            self.assertEqual(manifest["total_virtual_duration_s"], 90.0)
            self.assertEqual(manifest["sleep_calls"], 0)
            validated = validate_promotion_manifest(manifest_path)
            self.assertEqual(validated["manifest_sha256"], manifest["manifest_sha256"])
            self.assertEqual(validated["offline_replay_metrics"]["row_count"], 50)

    def test_blocked_no_shadow_manifest_is_deterministic_and_inactive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding, model, raw, _ = self._fixtures(root)
            manifest_path = root / "blocked-fixture.json"
            manifest = write_blocked_promotion_manifest(manifest_path, checkpoint_binding_path=binding, model_checkpoint_path=model, raw_replay_artifact_path=raw)
            self.assertFalse(manifest["active_allowed"])
            self.assertEqual(manifest["representative_names"], [])
            self.assertEqual(manifest["shadow_artifacts"], [])
            self.assertIn("shadow_artifacts_missing", manifest["blockers"])
            self.assertEqual(validate_promotion_manifest(manifest_path)["active_allowed"], False)

    def test_promotion_rejects_wrong_checkpoint_duplicate_or_swapped_shadows_and_tamper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding, model, raw, shadows = self._fixtures(root)
            with self.assertRaisesRegex(ValueError, "model checkpoint hash"):
                model.write_bytes(b"tampered model")
                write_promotion_manifest(root / "wrong-model.json", checkpoint_binding_path=binding, model_checkpoint_path=model, raw_replay_artifact_path=raw, shadow_artifact_paths=shadows)
            binding, model, raw, shadows = self._fixtures(root / "second-fixture")
            with self.assertRaisesRegex(ValueError, "distinct"):
                write_promotion_manifest(root / "duplicate.json", checkpoint_binding_path=binding, model_checkpoint_path=model, raw_replay_artifact_path=raw, shadow_artifact_paths=(shadows[0], shadows[0]))
            with self.assertRaisesRegex(ValueError, "name mismatch"):
                write_promotion_manifest(root / "swapped.json", checkpoint_binding_path=binding, model_checkpoint_path=model, raw_replay_artifact_path=raw, shadow_artifact_paths=(shadows[1], shadows[0]))
            manifest_path = root / "tamper.json"
            write_promotion_manifest(manifest_path, checkpoint_binding_path=binding, model_checkpoint_path=model, raw_replay_artifact_path=raw, shadow_artifact_paths=shadows)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["raw_replay_artifact_sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "self-hash"):
                validate_promotion_manifest(manifest_path)

    def test_resigned_status_tamper_is_rejected_for_extra_removed_or_flipped_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding, model, raw, shadows = self._fixtures(root)
            manifest_path = root / "status-tamper.json"
            write_promotion_manifest(manifest_path, checkpoint_binding_path=binding, model_checkpoint_path=model, raw_replay_artifact_path=raw, shadow_artifact_paths=shadows)
            original = json.loads(manifest_path.read_text(encoding="utf-8"))
            for mutate, expected in (
                (lambda value: value["blockers"].append("manual_extra"), "blockers"),
                (lambda value: value.__setitem__("blockers", []), "blockers"),
                (lambda value: value.__setitem__("active_allowed", True), "synthetic fixture"),
            ):
                candidate = json.loads(json.dumps(original))
                mutate(candidate)
                self.resign(manifest_path, candidate)
                with self.assertRaisesRegex(ValueError, expected):
                    validate_promotion_manifest(manifest_path)

    def test_resigned_blocked_reason_mutation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding, model, raw, _ = self._fixtures(root)
            manifest_path = root / "blocked-tamper.json"
            write_blocked_promotion_manifest(manifest_path, checkpoint_binding_path=binding, model_checkpoint_path=model, raw_replay_artifact_path=raw)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["blockers"][0] = "operator_declared_block"
            self.resign(manifest_path, payload)
            with self.assertRaisesRegex(ValueError, "blockers"):
                validate_promotion_manifest(manifest_path)
            with self.assertRaisesRegex(ValueError, "unsupported blocked"):
                write_blocked_promotion_manifest(manifest_path, checkpoint_binding_path=binding, model_checkpoint_path=model, raw_replay_artifact_path=raw, reason="operator_declared_block")

    def test_v3_live_authorization_rejects_blocked_and_tampered_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binding_path, model, raw, _ = self._fixtures(root)
            promotion_path = root / "blocked-promotion.json"
            write_blocked_promotion_manifest(promotion_path, checkpoint_binding_path=binding_path, model_checkpoint_path=model, raw_replay_artifact_path=raw)
            binding = validate_checkpoint_binding(binding_path)
            authorization_path = root / "live-authorization.json"
            authorization = self.live_authorization_payload(authorization_path, promotion_path, binding_path, binding)
            authorization_path.write_text(json.dumps(authorization, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not active-allowed"):
                validate_live_authorization(authorization_path)
            absolute = dict(authorization)
            absolute["promotion_manifest_relative_path"] = str(promotion_path)
            unsigned = dict(absolute)
            unsigned.pop("authorization_sha256")
            absolute["authorization_sha256"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            authorization_path.write_text(json.dumps(absolute, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "paths must be relative"):
                validate_live_authorization(authorization_path)
            tampered = dict(authorization)
            tampered["promotion_manifest_sha256"] = "0" * 64
            unsigned = dict(tampered)
            unsigned.pop("authorization_sha256")
            tampered["authorization_sha256"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            authorization_path.write_text(json.dumps(tampered, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "promotion manifest file hash"):
                validate_live_authorization(authorization_path)


if __name__ == "__main__":
    unittest.main()
