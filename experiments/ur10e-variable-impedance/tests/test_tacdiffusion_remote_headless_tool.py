from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

from ur10e_vic.tacdiffusion.checkpoint import CheckpointBinding, write_checkpoint_binding
from ur10e_vic.tacdiffusion.mainline_dataset import write_mainline_dataset
from ur10e_vic.tacdiffusion.mainline_model import MainlineModelConfig, train_mainline_model
from ur10e_vic.tacdiffusion.raw_artifact import DurableRawFrameWriter
from ur10e_vic.tacdiffusion.trajectory import TRAJECTORY_FAMILIES, episode_plan

TOOLS = Path(__file__).parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import tacdiffusion_remote_headless as operator_tool


def invoke(*arguments: str) -> dict[str, object]:
    return operator_tool.run(operator_tool.build_parser().parse_args(list(arguments)))


class RemoteHeadlessOperatorTests(unittest.TestCase):
    def test_operator_has_no_socket_import_and_plan_is_deterministic_50_episode_fixture(self):
        source = (TOOLS / "tacdiffusion_remote_headless.py").read_text(encoding="utf-8")
        imports = [node for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.Import, ast.ImportFrom))]
        imported_names = {alias.name.split(".")[0] for node in imports for alias in node.names}
        self.assertNotIn("socket", imported_names)
        self.assertNotIn("requests", imported_names)
        first = invoke("plan-campaign")
        second = invoke("plan-campaign")
        self.assertEqual(first, second)
        self.assertTrue(first["fixture_only"])
        self.assertEqual(len(first["episodes"]), 50)
        self.assertEqual(set(first["trajectory_families"]), set(TRAJECTORY_FAMILIES))
        self.assertEqual(len(set(entry["trajectory_family"] for entry in first["episodes"])), 7)
        expected = episode_plan(50, seed=0)
        for entry, (_, family, profile) in zip(first["episodes"], expected):
            self.assertEqual(entry["trajectory_family"], family)
            self.assertEqual(entry["speed_scale"], profile.speed_scale)
            self.assertEqual(entry["normal_force_target_n"], profile.normal_force_target_n)
            self.assertEqual(entry["preload_n"], profile.preload_n)
        self.assertEqual(first["episodes"][0]["dispatch_id"], "dispatch-000")
        self.assertEqual(first["episodes"][-1]["dispatch_id"], "dispatch-049")
        self.assertEqual(first["sleep_calls"], 0)
        self.assertFalse(first["live_io"])

    def test_queue_commands_are_local_persistent_and_reconcile_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory) / "queue.json")
            common = ("queue", "--state", state, "--campaign-home", "home", "--max-pending", "10")
            self.assertEqual(invoke(*common, "init")["mode"], "OPEN")
            invoke(*common, "append", "--episode-id", "episode-1", "--seed", "1")
            claimed = invoke(*common, "next")
            self.assertEqual(claimed["decision"], "ITEM")
            self.assertEqual(claimed["item"]["episode_id"], "episode-1")
            self.assertEqual(invoke(*common, "status")["inflight"]["episode_id"], "episode-1")
            reconciled = invoke(*common, "reconcile", "--outcome", "failed", "--result-identity", "result-1")
            self.assertEqual(reconciled["failed_count"], 1)
            ended = invoke(*common, "end")
            self.assertEqual(ended["mode"], "COMPLETE")
            self.assertFalse(ended["live_io"])

            drain_state = str(Path(directory) / "drain.json")
            drain_common = ("queue", "--state", drain_state, "--campaign-home", "home", "--max-pending", "10")
            invoke(*drain_common, "init")
            invoke(*drain_common, "append", "--episode-id", "drain-1", "--seed", "1")
            invoke(*drain_common, "drain")
            self.assertEqual(invoke(*drain_common, "status")["mode"], "DRAIN")
            empty_state = str(Path(directory) / "empty-drain.json")
            empty_common = ("queue", "--state", empty_state, "--campaign-home", "home", "--max-pending", "10")
            invoke(*empty_common, "init")
            self.assertEqual(invoke(*empty_common, "drain")["mode"], "COMPLETE")

    def test_dataset_checkpoint_promotion_receiver_and_lifecycle_commands_are_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = np.zeros((4, 84), dtype=np.float32)
            actions = np.zeros((4, 12), dtype=np.float32)
            dataset = root / "dataset.npz"
            manifest = write_mainline_dataset(
                dataset,
                observations=observations,
                actions=actions,
                episode_ids=["episode-a", "episode-a", "episode-b", "episode-b"],
                splits=["train", "train", "validation", "validation"],
                timestamps_s=[0.0, 0.002, 0.0, 0.002],
                source_raw_artifact_hashes={"episode-a": "a" * 64, "episode-b": "b" * 64},
                surface_calibration_sha256="c" * 64,
                action_profile_sha256="d" * 64,
                filter_profile_sha256="e" * 64,
                normalization_sha256="f" * 64,
            )
            manifest_path = root / "dataset.json"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            dataset_result = invoke("validate-dataset", "--dataset", str(dataset), "--manifest", str(manifest_path))
            self.assertTrue(dataset_result["validated"])
            self.assertEqual(dataset_result["row_count"], 4)

            checkpoint = root / "model.npz"
            train_mainline_model(observations, actions, ["train", "train", "validation", "validation"], checkpoint_path=checkpoint, config=MainlineModelConfig(hidden_dimension=16), epochs=1, batch_size=2)
            benchmark = invoke("benchmark-checkpoint", "--checkpoint", str(checkpoint), "--iterations", "1", "--warmup-iterations", "0")
            self.assertEqual(benchmark["benchmark"]["diffusion_steps"], 50)
            self.assertEqual(benchmark["benchmark"]["distinct_timesteps"], 50)
            self.assertIn(benchmark["benchmark"]["selected_rate_hz"], (50, 100, None))

            raw = root / "raw.jsonl"
            with DurableRawFrameWriter(raw, episode_id="empty"):
                pass
            model_hash = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
            binding_path = root / "binding.json"
            binding = CheckpointBinding("ur10e_tacdiffusion_checkpoint/v3", "1" * 64, "2" * 64, "3" * 64, "4" * 64, "5" * 64, ("6" * 64,), "7" * 64, {"observation_dimension": 84, "action_dimension": 12, "diffusion_steps": 50, "model_update_rate_hz": 100}, "8" * 64, model_hash)
            write_checkpoint_binding(binding_path, binding)
            promotion_path = root / "promotion.json"
            blocked = invoke("promotion", "write-blocked", "--manifest", str(promotion_path), "--checkpoint-binding", str(binding_path), "--model-checkpoint", str(checkpoint), "--raw-replay", str(raw))
            self.assertFalse(blocked["active_allowed"])
            validated = invoke("promotion", "validate", "--manifest", str(promotion_path))
            self.assertTrue(validated["validated"])
            self.assertFalse(validated["active_allowed"])

            receiver = invoke("receiver-report")
            self.assertTrue(receiver["report_only"])
            self.assertFalse(receiver["physical_io_enabled"])
            self.assertEqual(len(receiver["source_sha256"]), 64)

            capture = root / "dashboard.txt"
            capture.write_text("is in remote control: true\nRTDE read-only reachable: true\nprogram loaded: false\nProgram running: false\n", encoding="utf-8")
            dashboard = invoke("parse-dashboard", str(capture))
            self.assertTrue(dashboard["evidence"]["remote_control"])
            self.assertFalse(dashboard["evidence"]["program_loaded"])
            self.assertEqual(list(dashboard["statuses"]), ["REMOTE_CONTROL", "DASHBOARD_REACHABLE", "RTDE_READ_ONLY_REACHABLE", "LOAD", "PLAY", "BRIDGE_READY", "ARM", "MOTION"])
            lifecycle = invoke("lifecycle-no-motion", "--capture", str(capture))
            self.assertTrue(lifecycle["captured_evidence_only"])
            self.assertTrue(lifecycle["no_motion"])
            self.assertEqual(lifecycle["issued_commands"], [])
            self.assertEqual(lifecycle["next_required"], ["LOAD"])
            self.assertEqual(lifecycle["sleep_calls"], 0)

    def test_campaign_command_uses_runner_home_ack_consume_failure_continuation_and_terminal_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            result = invoke("run-offline-campaign", "--state-dir", str(Path(directory) / "campaign"), "--fail-modulo", "7", "--terminal", "DRAIN")
            self.assertTrue(result["fixture_only"])
            self.assertEqual(result["episodes_requested"], 50)
            self.assertEqual(result["episodes_executed"], 50)
            self.assertEqual(result["executions"], 50)
            self.assertEqual(result["episodes_failed"], 8)
            self.assertTrue(result["failure_continuation"])
            self.assertEqual(result["expert_reset_count"], 50)
            self.assertEqual(result["filter_reset_count"], 50)
            self.assertEqual(result["retract_count"], 50)
            self.assertEqual(result["home_ack_count"], 50)
            self.assertEqual(result["home_consume_count"], 50)
            self.assertEqual(result["wait_forever_decision"], "WAITING_FOR_EPISODE")
            self.assertEqual(result["terminal_decision"], "COMPLETE")
            self.assertEqual(result["queue_mode"], "COMPLETE")
            self.assertTrue(result["home_ack_consume_required"])
            self.assertEqual(result["sleep_calls"], 0)
            self.assertFalse(result["live_io"])


if __name__ == "__main__":
    unittest.main()
