from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_step5b_autotune_package import build_script, build_txt, validate  # noqa: E402
from build_step4e_p0p1_programs import build_urp  # noqa: E402
from step5b_autotune_contract import (  # noqa: E402
    CONTROLLER_DIRECTORY,
    PROGRAM_BASENAME,
    Candidate,
    bridge_command,
    candidate_token_low31,
    one_step_neighbors,
)
from step5b_autotune_evaluator import evaluate_run  # noqa: E402
from step5b_autotune_numeric_sanity import run_sanity  # noqa: E402
from step5b_autotune_optimizer import (  # noqa: E402
    Observation,
    choose_candidate,
    read_observations,
    tier2_is_unlocked,
)
import step5b_autotune_supervisor as supervisor  # noqa: E402


class Step5bAutotuneContractTest(unittest.TestCase):
    def test_independent_stage_table_is_inactive_and_gpu_required(self) -> None:
        table = json.loads(
            (ROOT / "config" / "step5b_autotune_stage_table.json").read_text(encoding="utf-8")
        )
        stage = table["stages"][0]
        self.assertFalse(stage["active"])
        self.assertTrue(stage["blocked"])
        self.assertEqual(stage["bridge"]["profile"], "step5b_v2")
        self.assertEqual(
            stage["optimizer"]["device_policy"],
            "cuda_required_for_live_no_cpu_fallback",
        )

    def test_candidate_grid_and_handshake_token_are_deterministic(self) -> None:
        candidate = Candidate(target_force_n=12.0)
        candidate.validate()
        self.assertEqual(candidate_token_low31(123, 4, candidate), candidate_token_low31(123, 4, candidate))
        self.assertGreater(candidate_token_low31(123, 4, candidate), 0)
        self.assertEqual(len(one_step_neighbors(candidate, tier2_unlocked=False)), 5)

    def test_bridge_command_preserves_v2_and_locked_safety_caps(self) -> None:
        command = bridge_command(Candidate(target_force_n=15.0), Path("/tmp/trial"), python_executable="python3")
        self.assertEqual(command[command.index("--step4e-version") + 1], "step5b_v2")
        self.assertEqual(command[command.index("--max-normal-force-n") + 1], "50")
        self.assertEqual(command[command.index("--max-force-norm-n") + 1], "60")
        self.assertEqual(command[command.index("--max-torque-norm-nm") + 1], "3.0")
        self.assertNotIn("zero_ftsensor", " ".join(command))


class Step5bAutotunePackageTest(unittest.TestCase):
    def test_package_is_v2_loop_with_home_and_integer_gates(self) -> None:
        stamp = "2026-07-14T1800HKT_STEP5B_CONTACT_CYCLOID_BAYES_LOOP_V2"
        script = build_script(stamp, "2026-07-14T18:00:00+08:00")
        txt = build_txt(stamp)
        urp = build_urp(script, PROGRAM_BASENAME, CONTROLLER_DIRECTORY)
        validate(script, txt, urp, stamp)
        self.assertIn("local skip_lift_attitude = 0", script)
        self.assertIn("write_output_float_register(35, 25.2)", script)
        self.assertIn("read_input_integer_register(24)", script)
        self.assertIn("position_error_m <= 0.003", script)
        self.assertIn("write_output_integer_register(28, candidate_token)", script)
        self.assertIn("release_token == active_token", script)
        self.assertEqual(script.count("\ncodex_step5b_contact_cycloid_bayes_loop_v2()\n"), 1)


class Step5bAutotuneEvaluatorTest(unittest.TestCase):
    def _make_run(self, root: Path, *, load_n: float = 12.0, home_verified: bool = True) -> Path:
        run_dir = root / "trial"
        run_dir.mkdir()
        count = 6001
        times = np.linspace(0.0, 60.0, count)
        df = pd.DataFrame(
            {
                "t_monotonic_s": times,
                "ur_output_double_register_35": np.full(count, 25.0),
                "step4e_progress_m": times,
                "_step4e_normal_load_n": np.full(count, load_n),
                "_step4e_live_normal_candidate_force_n": np.full(count, -999.0),
                "normal_force_n": np.full(count, -load_n),
                "force_norm_n": np.full(count, load_n),
                "mx_nm_zeroed": np.zeros(count),
                "my_nm_zeroed": np.zeros(count),
                "mz_nm_zeroed": np.zeros(count),
                "_step4e_path_error_x_m": np.full(count, 0.0001),
                "_step4e_path_error_y_m": np.full(count, -0.0001),
                "step4e_cmd_vx_m_s": np.full(count, 0.001),
                "step4e_cmd_vy_m_s": np.zeros(count),
                "step4e_cmd_vz_m_s": np.zeros(count),
                "step4e_cmd_wx_rad_s": np.zeros(count),
                "step4e_cmd_wy_rad_s": np.zeros(count),
                "step4e_cmd_wz_rad_s": np.zeros(count),
            }
        )
        df.to_csv(run_dir / "bridge_rtde_500hz.csv", index=False)
        (run_dir / "metadata.json").write_text(
            json.dumps(
                {
                    "args": {
                        "step4e_version": "step5b_v2",
                        "target_force_n": 12.0,
                        "step4e_force_p_gain": 0.001,
                        "step4e_force_i_gain": 0.00001,
                        "step4e_force_damping": 7.0,
                        "step4e_normal_filter_alpha": 0.55,
                    }
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "summary.json").write_text(json.dumps({"stop_reason": "signal_sigint"}), encoding="utf-8")
        (run_dir / "trial_runtime.json").write_text(
            json.dumps({"terminal_reason": 1, "home_verified": home_verified}),
            encoding="utf-8",
        )
        return run_dir

    def test_feasible_full_trial_gets_scalar_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = evaluate_run(self._make_run(Path(temporary)))
        self.assertTrue(result["eligible"])
        self.assertTrue(result["feasible"])
        self.assertIsNotNone(result["objective"])

    def test_zero_load_trial_is_infeasible_without_fake_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = evaluate_run(self._make_run(Path(temporary), load_n=0.0))
        self.assertFalse(result["feasible"])
        self.assertIsNone(result["objective"])
        self.assertIn("missing_or_zero_contact_load", result["failures"])

    def test_raw_normal_guard_and_nonfinite_values_are_infeasible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary), load_n=50.0)
            guarded = evaluate_run(run_dir)
        self.assertIn("raw_normal_guard_reached", guarded["failures"])
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary))
            csv_path = next(run_dir.glob("bridge_rtde_*hz.csv"))
            df = pd.read_csv(csv_path)
            df.loc[3, "step4e_cmd_vx_m_s"] = np.nan
            df.to_csv(csv_path, index=False)
            nonfinite = evaluate_run(run_dir)
        self.assertIn("nonfinite_required_data", nonfinite["failures"])
        self.assertFalse(nonfinite["feasible"])


class Step5bAutotuneOptimizerTest(unittest.TestCase):
    def test_initial_selection_is_bounded_and_round_robin(self) -> None:
        candidate, details = choose_candidate([], require_botorch=False)
        self.assertEqual(candidate.target_force_n, 10.0)
        self.assertEqual(details["selection"], "bounded_initial_exploration")
        candidate.validate()

    def test_tier2_remains_locked_without_repeats(self) -> None:
        observations = [
            Observation(
                Candidate(target_force_n=target, force_damping=5.0 + 0.5 * index),
                True,
                0.1 + 0.01 * index,
                True,
                f"run-{target}-{index}",
            )
            for target in (10.0, 12.0, 15.0)
            for index in range(5)
        ]
        self.assertFalse(tier2_is_unlocked(observations))

    def test_corrupt_jsonl_is_fatal_and_tier2_relock_resets_i_gain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "observations.jsonl"
            path.write_text("{broken\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "corrupt observation JSONL"):
                read_observations(path)
        observations = [Observation(Candidate(10.0, force_i_gain=0.00002), True, 0.1, True, "latest")]
        observations.extend(
            Observation(Candidate(target), True, 0.2 + index, True, f"{target}-{index}")
            for target in (12.0, 15.0)
            for index in range(2)
        )
        selected, details = choose_candidate(observations, require_botorch=False)
        self.assertEqual(details["target_context_n"], 10.0)
        self.assertEqual(selected.force_i_gain, 0.00001)

    def test_bayesian_selection_runs_on_cuda(self) -> None:
        try:
            import torch
        except ImportError:
            self.skipTest("torch not installed")
        if not torch.cuda.is_available():
            self.skipTest("CUDA not available")
        initial_candidate, initial_details = choose_candidate([], require_botorch=True)
        self.assertEqual(initial_candidate.target_force_n, 10.0)
        self.assertEqual(initial_details["device"], "cuda:0")
        self.assertTrue(initial_details["gpu_name"])
        visits = {10.0: 2, 12.0: 3, 15.0: 3}
        observations = [
            Observation(
                Candidate(
                    target_force_n=target,
                    force_p_gain=0.001 + 0.0001 * min(index, 1),
                    force_damping=7.0 + 0.5 * (index % 2),
                ),
                True,
                0.08 + 0.01 * index + 0.001 * target,
                True,
                f"cuda-run-{target}-{index}",
            )
            for target, count in visits.items()
            for index in range(count)
        ]
        candidate, details = choose_candidate(observations, require_botorch=True)
        self.assertEqual(candidate.target_force_n, 10.0)
        self.assertEqual(details["device"], "cuda:0")
        self.assertEqual(details["gpu_workers"], 2)
        self.assertTrue(details["gpu_name"])
        self.assertIn("botorch", details["selection"])


class Step5bAutotuneSanityTest(unittest.TestCase):
    def test_numeric_sanity_passes(self) -> None:
        result = run_sanity()
        self.assertTrue(result["pass"], result)


class Step5bAutotuneSupervisorTest(unittest.TestCase):
    def _write_delivery_fixture(self, root: Path) -> tuple[Path, Path]:
        local_dir = root / "programs" / "step5" / "autotune"
        fetched_dir = root / "runs" / "controller_readback" / "fetched"
        config_dir = root / "config"
        local_dir.mkdir(parents=True)
        fetched_dir.mkdir(parents=True)
        config_dir.mkdir()
        artifacts = []
        for suffix in (".script", ".txt", ".urp"):
            name = f"{PROGRAM_BASENAME}{suffix}"
            payload = f"fixture-{suffix}".encode()
            local_path = local_dir / name
            readback_path = fetched_dir / name
            local_path.write_bytes(payload)
            readback_path.write_bytes(payload)
            artifacts.append(
                {
                    "local_path": str(local_path.relative_to(root)),
                    "readback_path": str(readback_path.relative_to(root)),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        validation_path = fetched_dir.parent / "urp_validation.json"
        validation_path.write_text(
            json.dumps(
                {
                    "pass": True,
                    "state": "controller read-back verified",
                    "basename": PROGRAM_BASENAME,
                }
            ),
            encoding="utf-8",
        )
        delivery_path = config_dir / "step5b_autotune_delivery_v2.json"
        delivery_path.write_text(
            json.dumps(
                {
                    "controller_readback_verified": True,
                    "basename": PROGRAM_BASENAME,
                    "artifacts": artifacts,
                    "urp_validation_evidence": str(validation_path.relative_to(root)),
                }
            ),
            encoding="utf-8",
        )
        return delivery_path, fetched_dir / f"{PROGRAM_BASENAME}.script"

    def test_delivery_evidence_reopens_and_hashes_fresh_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            delivery_path, _ = self._write_delivery_fixture(root)
            with mock.patch.object(supervisor, "EXPERIMENT_ROOT", root), mock.patch.object(
                supervisor, "DELIVERY_EVIDENCE", delivery_path
            ):
                ok, detail = supervisor.verify_delivery_evidence()
        self.assertTrue(ok, detail)

    def test_delivery_evidence_rejects_tampered_fresh_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            delivery_path, fetched_script = self._write_delivery_fixture(root)
            fetched_script.write_text("tampered", encoding="utf-8")
            with mock.patch.object(supervisor, "EXPERIMENT_ROOT", root), mock.patch.object(
                supervisor, "DELIVERY_EVIDENCE", delivery_path
            ):
                ok, detail = supervisor.verify_delivery_evidence()
        self.assertFalse(ok)
        self.assertIn("fresh read-back SHA mismatch", detail)

    def test_observer_failure_stops_bridge_before_capture_marker(self) -> None:
        candidate = Candidate(target_force_n=10.0)
        fake_process = mock.Mock()
        fake_process.poll.return_value = None
        observer_context = mock.MagicMock()
        observer_context.__enter__.return_value = (mock.Mock(), 1, [])
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            supervisor, "tp_observer", return_value=observer_context
        ), mock.patch.object(
            supervisor, "write_handshake"
        ), mock.patch.object(
            supervisor.subprocess, "Popen", return_value=fake_process
        ), mock.patch.object(
            supervisor, "wait_for_bridge_output", side_effect=RuntimeError("observer broke")
        ), mock.patch.object(
            supervisor, "stop_bridge_child", return_value=0
        ) as stop_child:
            outcome = supervisor.run_one_trial(
                Path(temporary), 123, 1, candidate, Path(temporary) / "STOP_REQUESTED"
            )
            self.assertTrue((outcome["run_dir"] / "capture_incomplete.json").is_file())
            self.assertFalse(outcome["capture_complete"])
        stop_child.assert_called_once_with(fake_process, home_observed=False)
        self.assertIn("observer failure", outcome["runtime"]["fatal_detail"])

    def test_stale_home_is_rejected_until_exact_token_run(self) -> None:
        base = {
            "output_int_register_24": 7,
            "output_int_register_25": 3,
            "output_int_register_27": 1,
            "output_int_register_28": 99,
        }
        stale_home = {**base, "output_int_register_26": 50, "output_int_register_28": 98}
        saw_run, home, ack, _, _ = supervisor.handshake_progress(
            stale_home, session_epoch=7, trial_id=3, token=99, saw_run=False, home_release_sent=False
        )
        self.assertFalse(saw_run or home or ack)
        fresh_run = {**base, "output_int_register_26": 20}
        saw_run, home, _, _, _ = supervisor.handshake_progress(
            fresh_run, session_epoch=7, trial_id=3, token=99, saw_run=False, home_release_sent=False
        )
        self.assertTrue(saw_run)
        fresh_home = {**base, "output_int_register_26": 50}
        _, home, _, _, _ = supervisor.handshake_progress(
            fresh_home, session_epoch=7, trial_id=3, token=99, saw_run=saw_run, home_release_sent=False
        )
        self.assertTrue(home)
        wait_ack = {**base, "output_int_register_26": 10}
        _, _, ack, _, _ = supervisor.handshake_progress(
            wait_ack, session_epoch=7, trial_id=3, token=99, saw_run=True, home_release_sent=True
        )
        self.assertTrue(ack)

    def test_bridge_cleanup_escalates_process_group_and_metadata_is_exact(self) -> None:
        fake_process = mock.Mock(pid=4242)
        fake_process.poll.return_value = None
        fake_process.wait.side_effect = [
            subprocess.TimeoutExpired("bridge", 20.0),
            subprocess.TimeoutExpired("bridge", 5.0),
            -9,
        ]
        with mock.patch.object(
            supervisor, "process_group_exists", side_effect=[True, True, True, False]
        ), mock.patch.object(supervisor.os, "killpg") as killpg:
            self.assertEqual(supervisor.stop_bridge_child(fake_process, home_observed=False), -9)
        self.assertEqual(
            [call.args[1] for call in killpg.call_args_list],
            [signal.SIGINT, signal.SIGTERM, signal.SIGKILL],
        )
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "metadata.json").write_text(
                json.dumps({"args": {"step4e_version": "wrong", "output_dir": str(run_dir)}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "profile mismatch"):
                supervisor.verify_bridge_metadata(run_dir, Candidate(10.0))

    def test_surviving_descendant_blocks_capture_and_history_is_globally_sorted(self) -> None:
        fake_process = mock.Mock(pid=4242)
        fake_process.poll.return_value = 0
        with mock.patch.object(supervisor, "process_group_exists", return_value=True), self.assertRaisesRegex(
            RuntimeError, "descendant survived"
        ):
            supervisor.stop_bridge_child(fake_process, home_observed=True)
        with tempfile.TemporaryDirectory() as temporary:
            root_a = Path(temporary) / "a"
            root_b = Path(temporary) / "b"
            root_a.mkdir()
            root_b.mkdir()
            newer = root_a / "bridge_step5b_contact_cycloid_baseline_v2_20260702_120000"
            older = root_b / "bridge_step5b_contact_cycloid_baseline_v2_20260630_120000"
            newer.mkdir()
            older.mkdir()
            with mock.patch.object(supervisor, "historical_run_roots", return_value=[root_a, root_b]):
                ordered = supervisor.historical_run_dirs()
        self.assertEqual([path.name for path in ordered], [older.name, newer.name])

    def test_authorization_ledger_is_a_hard_preflight_gate(self) -> None:
        with mock.patch.object(supervisor, "verify_delivery_evidence", return_value=(True, "ok")), mock.patch.object(
            supervisor, "dependency_status", return_value=(True, "ok")
        ), mock.patch.object(
            supervisor, "gpu_capacity_status", return_value={"ok": True}
        ), mock.patch.object(
            supervisor, "lock_available", return_value=(True, "available")
        ), mock.patch.object(
            supervisor, "local_bridge_processes", return_value=[]
        ):
            result = supervisor.preflight(offline=True)
        self.assertFalse(result["ok"])
        self.assertFalse(result["authorization"]["ok"])
        self.assertIn("live_authorized", result["authorization"]["required_true"])

    def test_diagnostic_spawn_failure_closes_log(self) -> None:
        handle = mock.Mock()
        with mock.patch.object(Path, "open", return_value=handle), mock.patch.object(
            supervisor.subprocess, "Popen", side_effect=OSError("spawn failed")
        ), self.assertRaisesRegex(OSError, "spawn failed"):
            supervisor.start_diagnostic(Path("/tmp/not-used"))
        handle.close.assert_called_once_with()

    def test_session_lock_is_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "session.lock"
            with supervisor.exclusive_lock(path):
                ok, detail = supervisor.lock_available(path)
            self.assertFalse(ok)
            self.assertIn("lock busy", detail)


if __name__ == "__main__":
    unittest.main()
