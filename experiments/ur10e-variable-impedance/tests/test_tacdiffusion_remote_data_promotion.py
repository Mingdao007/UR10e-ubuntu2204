import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from ur10e_vic.tacdiffusion.mainline_dataset import validate_mainline_dataset, write_mainline_dataset
from ur10e_vic.tacdiffusion.mainline_model import DeterministicMainlinePredictor, MainlineModelConfig
from ur10e_vic.tacdiffusion.promotion import ShadowTrace, evaluate_offline_replay, run_offline_shadow_gate
from ur10e_vic.tacdiffusion.remote_headless import RemoteEvidence, inspect_remote_headless
from ur10e_vic.tacdiffusion.direct_torque_receiver import parse_receiver_source, receiver_empty_wait
from ur10e_vic.tacdiffusion.raw_artifact import DurableRawFrameWriter, RawFrameRecord, derive_training_view, read_raw_frames
from ur10e_vic.tacdiffusion.mainline_model import ConditionalActionModel, benchmark_runtime, train_mainline_model, torch
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
        self.assertEqual([name for name, _ in build_dry_run_command_plan()], ["REMOTE_CONTROL", "LOAD", "PLAY", "BRIDGE_READY", "ARM", "MOTION"])
        contract = parse_receiver_source()
        self.assertFalse(contract.physical_io_enabled)
        self.assertEqual(receiver_empty_wait(queue_empty=True, explicit_end=False, explicit_drain=False), "WAIT_FOREVER")
        self.assertEqual(receiver_empty_wait(queue_empty=True, explicit_end=True, explicit_drain=False), "RETURN_CAMPAIGN_HOME")

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
            binding = CheckpointBinding("ur10e_tacdiffusion_checkpoint/v2", "a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64, ("f" * 64,), "1" * 64, {"observation_dimension": 84, "action_dimension": 12}, fingerprint, "2" * 64)
            write_checkpoint_binding(root / "checkpoint.json", binding)
            self.assertEqual(validate_checkpoint_binding(root / "checkpoint.json"), binding)

    def test_84d_12d_dataset_is_hash_bound_and_model_reads_continuous_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = np.zeros((6, 84), dtype=np.float32)
            actions = np.zeros((6, 12), dtype=np.float32)
            manifest = write_mainline_dataset(root / "mainline.npz", observations=observations, actions=actions, episode_ids=["a"] * 3 + ["b"] * 3, splits=["train"] * 3 + ["validation"] * 3, timestamps_s=np.arange(6) * 0.002, surface_calibration_sha256="a" * 64, action_profile_sha256="b" * 64, normalization_sha256="c" * 64)
            self.assertEqual(validate_mainline_dataset(root / "mainline.npz", manifest)["action_dimension"], 12)
            predictor = DeterministicMainlinePredictor(MainlineModelConfig(model_update_rate_hz=100))
            self.assertEqual(len(predictor.predict(np.zeros(84))), 12)

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
            result = train_mainline_model(observations, actions, splits, checkpoint_path=checkpoint, epochs=1, batch_size=2)
            self.assertEqual(result["model_schema"], "ur10e_tacdiffusion_checkpoint/v3")
            self.assertEqual(result["diffusion_steps"], 50)
            model = ConditionalActionModel()
            benchmark = benchmark_runtime(model, iterations=3)
            self.assertEqual(benchmark["diffusion_steps"], 50)
            self.assertEqual(benchmark["distinct_timesteps"], 50)
            self.assertIn("selected_rate_hz", benchmark)


if __name__ == "__main__":
    unittest.main()
