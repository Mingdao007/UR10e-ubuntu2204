from __future__ import annotations

import hashlib
import json
import shutil
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
    CONSTRAINT_VIOLATION_REASONS,
    CONTROLLER_DIRECTORY,
    PROGRAM_BASENAME,
    Candidate,
    bridge_command,
    candidate_token_low31,
    one_step_neighbors,
)
from step5b_autotune_evaluator import evaluate_run  # noqa: E402
from step5b_autotune_evidence import (  # noqa: E402
    BACKEND_ID,
    canonical_bytes,
    candidate_uid_from_payload,
    fingerprint_record,
    physical_capture_uid_from_sha256,
    sha256_bytes,
    trial_spec_payload,
    trial_uid_from_identity,
)
from step5b_autotune_numeric_sanity import run_sanity  # noqa: E402
from step5b_autotune_optimizer import (  # noqa: E402
    Observation,
    choose_candidate,
    read_observations,
    tier2_is_unlocked,
)
from step5b_autotune_promotion import (  # noqa: E402
    FINGERPRINT_PATHS,
    STEP5D_ENV_KEYS,
    build_promotion_payload,
    write_promotion_artifacts,
)
import step5b_autotune_supervisor as supervisor  # noqa: E402
import step5b_autotune_evidence as evidence  # noqa: E402


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
        command = bridge_command(Candidate(target_force_n=12.0), Path("/tmp/trial"), python_executable="python3")
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
    def _make_run(
        self,
        root: Path,
        *,
        load_n: float = 12.0,
        home_verified: bool = True,
        include_xy: bool = True,
    ) -> Path:
        run_dir = root / "trial"
        run_dir.mkdir(parents=True)
        candidate = Candidate(target_force_n=12.0)
        token = candidate_token_low31(7, 3, candidate)
        session_uid = "1" * 64
        fingerprint_pre = fingerprint_record("pre")
        fingerprint_post = fingerprint_record("post")
        fingerprint_sha256 = fingerprint_pre["fingerprint"]["combined_sha256"]
        trial_spec = trial_spec_payload(
            session_uid=session_uid,
            trial_id=3,
            candidate=candidate.payload(),
            candidate_token_low31=token,
            fingerprint_pre_sha256=fingerprint_sha256,
        )
        trial_spec_path = run_dir / "trial_spec.json"
        trial_spec_path.write_text(json.dumps(trial_spec), encoding="utf-8")
        trial_spec_sha256 = hashlib.sha256(trial_spec_path.read_bytes()).hexdigest()
        count = 6001
        times = np.linspace(0.0, 60.0, count)
        df = pd.DataFrame(
            {
                "t_monotonic_s": times,
                "sensor_ok": np.ones(count),
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
        if not include_xy:
            df = df.drop(columns=["_step4e_path_error_x_m", "_step4e_path_error_y_m"])
        bridge_csv_path = run_dir / "bridge_rtde_500hz.csv"
        df.to_csv(bridge_csv_path, index=False)
        physical_capture_uid = physical_capture_uid_from_sha256(
            hashlib.sha256(bridge_csv_path.read_bytes()).hexdigest()
        )
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
                        "output_dir": str(run_dir),
                    }
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "summary.json").write_text(json.dumps({"stop_reason": "signal_sigint"}), encoding="utf-8")
        (run_dir / "trial_runtime.json").write_text(
            json.dumps(
                {
                    "schema_version": "step5b_autotune_trial_runtime_v4",
                    "session_epoch": 7,
                    "trial_id": 3,
                    "candidate_token_low31": token,
                    "terminal_reason": 1,
                    "home_verified": home_verified,
                    "fresh_run_observed": True,
                    "home_release_ack": True,
                    "backend_id": BACKEND_ID,
                    "session_uid": session_uid,
                    "candidate_uid": trial_spec["candidate_uid"],
                    "trial_uid": trial_spec["trial_uid"],
                    "trial_spec_sha256": trial_spec_sha256,
                    "physical_capture_uid": physical_capture_uid,
                    "fingerprint_pre_sha256": fingerprint_sha256,
                    "fingerprint_post_sha256": fingerprint_sha256,
                    "fingerprint_verified": True,
                    "fatal_detail": None,
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "capture_complete.json").write_text(
            json.dumps(
                {
                    "schema_version": "step5b_autotune_capture_manifest_v4",
                    "complete": home_verified,
                    "process_group_reaped": True,
                    "fresh_run_observed": True,
                    "home_verified": home_verified,
                    "home_release_ack": True,
                    "backend_id": BACKEND_ID,
                    "session_uid": session_uid,
                    "candidate_uid": trial_spec["candidate_uid"],
                    "trial_uid": trial_spec["trial_uid"],
                    "trial_spec_sha256": trial_spec_sha256,
                    "physical_capture_uid": physical_capture_uid,
                    "fingerprint_pre_sha256": fingerprint_sha256,
                    "fingerprint_post_sha256": fingerprint_sha256,
                    "fingerprint_verified": True,
                    "fatal_detail": None,
                }
            ),
            encoding="utf-8",
        )
        (run_dir / "trial_fingerprint_pre.json").write_text(
            json.dumps(fingerprint_pre), encoding="utf-8"
        )
        (run_dir / "trial_fingerprint_post.json").write_text(
            json.dumps(fingerprint_post), encoding="utf-8"
        )
        return run_dir

    @staticmethod
    def _refresh_capture_identity(run_dir: Path) -> None:
        csv_path = next(run_dir.glob("bridge_rtde_*hz.csv"))
        capture_uid = physical_capture_uid_from_sha256(
            hashlib.sha256(csv_path.read_bytes()).hexdigest()
        )
        for name in ("trial_runtime.json", "capture_complete.json"):
            path = run_dir / name
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["physical_capture_uid"] = capture_uid
            path.write_text(json.dumps(payload), encoding="utf-8")

    def test_feasible_full_trial_gets_scalar_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = evaluate_run(self._make_run(Path(temporary), load_n=14.0))
        self.assertTrue(result["eligible"])
        self.assertTrue(result["feasible"])
        self.assertEqual(result["schema_version"], "step5b_autotune_evaluation_v4")
        self.assertEqual(result["objective_name"], "force_mae_n")
        self.assertAlmostEqual(result["objective"], 2.0)

    def test_xy_metrics_are_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary), load_n=13.0, include_xy=False)
            result = evaluate_run(run_dir)
        self.assertTrue(result["feasible"], result["failures"])
        self.assertAlmostEqual(result["objective"], 1.0)
        self.assertIsNone(result["metrics"]["xy_rmse_m"])

    def test_zero_load_trial_is_infeasible_without_fake_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = evaluate_run(self._make_run(Path(temporary), load_n=0.0))
        self.assertFalse(result["feasible"])
        self.assertFalse(result["eligible"])
        self.assertEqual(result["disposition"], "PLATFORM_FAILURE")
        self.assertIsNone(result["objective"])
        self.assertIn("missing_or_zero_contact_load", result["failures"])

    def test_raw_normal_guard_and_nonfinite_values_are_infeasible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary), load_n=50.0)
            guarded = evaluate_run(run_dir)
        self.assertIn("raw_normal_guard_reached", guarded["failures"])
        self.assertTrue(guarded["eligible"])
        self.assertEqual(guarded["disposition"], "PARAMETER_CONSTRAINT")
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary))
            csv_path = next(run_dir.glob("bridge_rtde_*hz.csv"))
            df = pd.read_csv(csv_path)
            df.loc[3, "step4e_cmd_vx_m_s"] = np.nan
            df.to_csv(csv_path, index=False)
            self._refresh_capture_identity(run_dir)
            nonfinite = evaluate_run(run_dir)
        self.assertIn("nonfinite_required_data", nonfinite["failures"])
        self.assertFalse(nonfinite["feasible"])
        self.assertFalse(nonfinite["eligible"])
        self.assertEqual(nonfinite["disposition"], "PLATFORM_FAILURE")

    def test_nonparameter_abort_short_timing_sensor_and_missing_closure_are_ineligible(self) -> None:
        self.assertNotIn(4, CONSTRAINT_VIOLATION_REASONS)
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary))
            runtime_path = run_dir / "trial_runtime.json"
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime["terminal_reason"] = 4
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
            aborted = evaluate_run(run_dir)
        self.assertFalse(aborted["eligible"])
        self.assertFalse(aborted["feasible"])
        self.assertIsNone(aborted["objective"])
        self.assertEqual(aborted["disposition"], "PLATFORM_FAILURE")
        self.assertIn("nonparameter_terminal_reason_4", aborted["failures"])

        for terminal_reason in (8, 10, 12, 13):
            with self.subTest(terminal_reason=terminal_reason), tempfile.TemporaryDirectory() as temporary:
                run_dir = self._make_run(Path(temporary))
                runtime_path = run_dir / "trial_runtime.json"
                runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
                runtime["terminal_reason"] = terminal_reason
                runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
                platform = evaluate_run(run_dir)
            self.assertFalse(platform["eligible"])
            self.assertEqual(platform["disposition"], "PLATFORM_FAILURE")
            self.assertIn(
                f"nonparameter_terminal_reason_{terminal_reason}", platform["failures"]
            )

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary))
            runtime_path = run_dir / "trial_runtime.json"
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime["terminal_reason"] = 5
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
            parameter_event = evaluate_run(run_dir)
        self.assertTrue(parameter_event["eligible"])
        self.assertEqual(parameter_event["disposition"], "PARAMETER_CONSTRAINT")

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary))
            csv_path = next(run_dir.glob("bridge_rtde_*hz.csv"))
            df = pd.read_csv(csv_path)
            df["t_monotonic_s"] = np.linspace(0.0, 55.0, len(df))
            df.to_csv(csv_path, index=False)
            self._refresh_capture_identity(run_dir)
            short = evaluate_run(run_dir)
        self.assertFalse(short["full_trial"])
        self.assertFalse(short["eligible"])
        self.assertEqual(short["disposition"], "PLATFORM_FAILURE")
        self.assertIn("stage25_duration_lt_59p5s", short["failures"])

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary))
            csv_path = next(run_dir.glob("bridge_rtde_*hz.csv"))
            df = pd.read_csv(csv_path)
            df.loc[100, "sensor_ok"] = 0.0
            df.to_csv(csv_path, index=False)
            self._refresh_capture_identity(run_dir)
            stale = evaluate_run(run_dir)
        self.assertIn("sensor_not_ok_during_stage25", stale["failures"])
        self.assertFalse(stale["eligible"])
        self.assertEqual(stale["disposition"], "PLATFORM_FAILURE")

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary))
            (run_dir / "capture_complete.json").unlink()
            unclosed = evaluate_run(run_dir)
        self.assertFalse(unclosed["eligible"])
        self.assertIn("supervisor_closure_invalid", " ".join(unclosed["failures"]))

    def test_trial_uid_is_capture_derived_and_stable_across_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_dir = self._make_run(root / "first")
            second_dir = root / "second" / "trial"
            second_dir.parent.mkdir()
            shutil.copytree(first_dir, second_dir)
            first = evaluate_run(first_dir)
            second = evaluate_run(second_dir)
            bridge_sha256 = hashlib.sha256(
                next(first_dir.glob("bridge_rtde_*hz.csv")).read_bytes()
            ).hexdigest()
        self.assertEqual(first["trial_uid"], second["trial_uid"])
        self.assertEqual(first["physical_capture_uid"], second["physical_capture_uid"])
        self.assertEqual(
            first["physical_capture_uid"], physical_capture_uid_from_sha256(bridge_sha256)
        )
        self.assertNotEqual(first["run_dir"], second["run_dir"])
        self.assertEqual(first["backend_id"], BACKEND_ID)
        self.assertTrue(first["fingerprint"]["verified"])
        self.assertFalse(second["eligible"])
        self.assertIn("metadata_output_dir_mismatch", second["failures"])

    def test_malformed_evidence_is_durably_quarantined_without_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary))
            (run_dir / "metadata.json").write_text("{broken", encoding="utf-8")
            result = evaluate_run(run_dir)
            quarantine_path = Path(result["quarantine_path"])
            self.assertTrue(quarantine_path.is_file())
            quarantine = json.loads(quarantine_path.read_text(encoding="utf-8"))
        self.assertFalse(result["eligible"])
        self.assertTrue(result["quarantined"])
        self.assertFalse(quarantine["training_eligible"])
        self.assertIn("metadata_malformed", " ".join(result["failures"]))

    def test_pre_post_fingerprint_change_is_integrity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary))
            post_path = run_dir / "trial_fingerprint_post.json"
            post = json.loads(post_path.read_text(encoding="utf-8"))
            payload = post["fingerprint"]
            first_path = next(iter(payload["config_files"]))
            payload["config_files"][first_path] = "0" * 64
            payload["config_sha256"] = sha256_bytes(canonical_bytes(payload["config_files"]))
            core = {key: value for key, value in payload.items() if key != "combined_sha256"}
            payload["combined_sha256"] = sha256_bytes(canonical_bytes(core))
            post_path.write_text(json.dumps(post), encoding="utf-8")
            result = evaluate_run(run_dir)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["disposition"], "INTEGRITY_FAILURE")
        self.assertIn("source_config_fingerprint_changed_during_trial", result["failures"])

    def test_runtime_identity_tamper_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = self._make_run(Path(temporary))
            runtime_path = run_dir / "trial_runtime.json"
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime["candidate_token_low31"] += 1
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
            result = evaluate_run(run_dir)
        self.assertFalse(result["eligible"])
        self.assertEqual(result["disposition"], "INTEGRITY_FAILURE")
        self.assertTrue(result["quarantined"])
        self.assertIn("supervisor_runtime_identity_invalid", " ".join(result["failures"]))


class Step5bAutotuneOptimizerTest(unittest.TestCase):
    def test_initial_selection_is_bounded_and_round_robin(self) -> None:
        candidate, details = choose_candidate([], require_botorch=False)
        self.assertEqual(candidate, Candidate(target_force_n=12.0))
        self.assertEqual(details["selection"], "fixed_12n_baseline_seed")
        candidate.validate()

    def test_tier2_remains_locked_without_repeats(self) -> None:
        observations = [
            Observation(
                Candidate(target_force_n=12.0, force_damping=5.0 + 0.5 * index),
                True,
                0.1 + 0.01 * index,
                True,
                f"run-{index}",
            )
            for index in range(5)
        ]
        self.assertFalse(tier2_is_unlocked(observations))

    def test_corrupt_jsonl_is_quarantined_and_tier2_relock_resets_i_gain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "observations.jsonl"
            path.write_text("{broken\n", encoding="utf-8")
            self.assertEqual(read_observations(path), [])
            self.assertTrue(list((path.parent / "evidence_quarantine").glob("*.json")))
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "step5b_autotune_evaluation_v4",
                        "objective_name": "force_mae_n",
                        "objective_unit": "N",
                        "eligible": False,
                        "feasible": False,
                        "objective": None,
                        "failures": ["unclosed"],
                    }
                ) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(read_observations(path), [])
        observations = [Observation(Candidate(12.0, force_i_gain=0.00002), True, 0.1, True, "latest")]
        observations.extend(
            Observation(Candidate(12.0), True, 0.2 + index, True, f"12-{index}")
            for index in range(2)
        )
        selected, details = choose_candidate(observations, require_botorch=False)
        self.assertEqual(details["target_context_n"], 12.0)
        self.assertEqual(selected.force_i_gain, 0.00001)

    def test_bayesian_selection_runs_on_cuda(self) -> None:
        try:
            import torch
        except ImportError:
            self.skipTest("torch not installed")
        if not torch.cuda.is_available():
            self.skipTest("CUDA not available")
        initial_candidate, initial_details = choose_candidate([], require_botorch=True)
        self.assertEqual(initial_candidate.target_force_n, 12.0)
        self.assertEqual(initial_details["device"], "cuda:0")
        self.assertTrue(initial_details["gpu_name"])
        observations = [
            Observation(
                Candidate(
                    target_force_n=12.0,
                    force_p_gain=0.001 + 0.0001 * min(index, 1),
                    force_damping=7.0 + 0.5 * (index % 2),
                ),
                True,
                0.8 + 0.01 * index,
                True,
                f"cuda-run-{index}",
            )
            for index in range(8)
        ]
        candidate, details = choose_candidate(observations, require_botorch=True)
        self.assertEqual(candidate.target_force_n, 12.0)
        self.assertEqual(details["device"], "cuda:0")
        self.assertEqual(details["gpu_workers"], 1)
        self.assertEqual(details["cuda_fit_mode"], "serial")
        self.assertTrue(details["gpu_name"])
        self.assertIn("botorch", details["selection"])

    def test_history_preserves_trial_backend_and_fingerprint_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "observations.jsonl"
            capture_path = path.parent / "capture.csv"
            capture_path.write_bytes(b"capture")
            capture_sha256 = hashlib.sha256(capture_path.read_bytes()).hexdigest()
            candidate = Candidate(12.0)
            candidate_uid = candidate_uid_from_payload(candidate.payload())
            session_uid = "c" * 64
            trial_uid = trial_uid_from_identity(session_uid, 1, candidate_uid)
            payload = {
                "schema_version": "step5b_autotune_evaluation_v4",
                "backend_id": BACKEND_ID,
                "session_uid": session_uid,
                "trial_id": 1,
                "trial_uid": trial_uid,
                "candidate_uid": candidate_uid,
                "physical_capture_uid": physical_capture_uid_from_sha256(capture_sha256),
                "trial_spec_sha256": "e" * 64,
                "disposition": "OBJECTIVE",
                "objective_name": "force_mae_n",
                "objective_unit": "N",
                "eligible": True,
                "feasible": True,
                "objective": 0.4,
                "full_trial": True,
                "supervisor_closure_verified": True,
                "quarantined": False,
                "quarantine_path": None,
                "failures": [],
                "candidate": candidate.payload(),
                "run_dir": "/copied/path/does/not/define/identity",
                "fingerprint": {
                    "verified": True,
                    "pre_combined_sha256": "d" * 64,
                    "post_combined_sha256": "d" * 64,
                },
                "provenance": {
                    "bridge_csv": {"path": str(capture_path), "sha256": capture_sha256}
                },
            }
            path.write_text(
                json.dumps(payload) + "\n" + json.dumps(payload) + "\n",
                encoding="utf-8",
            )
            observations = read_observations(path)
            self.assertTrue(list((path.parent / "evidence_quarantine").glob("*.json")))
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].trial_uid, trial_uid)
        self.assertEqual(observations[0].backend_id, BACKEND_ID)
        self.assertEqual(observations[0].fingerprint_sha256, "d" * 64)

    def test_parallel_cuda_fit_requires_explicit_verified_attestation(self) -> None:
        with self.assertRaisesRegex(ValueError, "explicit verification attestation"):
            choose_candidate(
                [],
                require_botorch=False,
                cuda_fit_mode="verified_parallel",
            )
        candidate, _ = choose_candidate(
            [],
            require_botorch=False,
            cuda_fit_mode="verified_parallel",
            parallel_cuda_verified=True,
        )
        self.assertEqual(candidate, Candidate(12.0))

    def test_nonfinite_or_invariant_broken_history_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "observations.jsonl"
            payload = Step5bAutotunePromotionTest._evaluation(
                Candidate(12.0), 0.4, root / "capture.csv"
            )
            path.write_text(json.dumps({**payload, "objective": float("nan")}) + "\n", encoding="utf-8")
            self.assertEqual(read_observations(path), [])
            self.assertTrue(list((root / "evidence_quarantine").glob("*.json")))

            shutil.rmtree(root / "evidence_quarantine")
            broken = {
                **payload,
                "quarantined": True,
                "quarantine_path": "/forged/quarantine.json",
                "supervisor_closure_verified": False,
                "failures": ["integrity_failure"],
            }
            path.write_text(json.dumps(broken) + "\n", encoding="utf-8")
            self.assertEqual(read_observations(path), [])
            self.assertTrue(list((root / "evidence_quarantine").glob("*.json")))

    def test_quarantine_write_failure_is_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "observations.jsonl"
            path.write_text("{broken\n", encoding="utf-8")
            with mock.patch.object(evidence, "atomic_write_json", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(RuntimeError, "durable evidence quarantine write failed"):
                    read_observations(path)

    def test_initial_exploration_avoids_unscheduled_duplicate_candidates(self) -> None:
        observations: list[Observation] = []
        sequence: list[Candidate] = []
        for index in range(6):
            candidate, _ = choose_candidate(observations, require_botorch=False)
            sequence.append(candidate)
            observations.append(Observation(candidate, False, None, False, f"run-{index}"))
        self.assertEqual(sequence[3], Candidate(12.0))
        self.assertEqual(len(set(sequence[:3] + sequence[4:])), 5)


class Step5bAutotunePromotionTest(unittest.TestCase):
    @staticmethod
    def _evaluation(candidate: Candidate, objective: float, capture_path: Path) -> dict:
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture_path.write_bytes(f"physical-capture:{capture_path.name}".encode())
        capture_sha256 = hashlib.sha256(capture_path.read_bytes()).hexdigest()
        run_dir = capture_path.parent / f"{capture_path.stem}_evidence"
        run_dir.mkdir(parents=True, exist_ok=True)
        run = str(run_dir)
        candidate_uid = candidate_uid_from_payload(candidate.payload())
        session_uid = hashlib.sha256(("session:" + run).encode()).hexdigest()
        trial_id = 1
        session_epoch = 7
        token = candidate_token_low31(session_epoch, trial_id, candidate)
        fingerprint_pre = fingerprint_record("pre")
        fingerprint_post = fingerprint_record("post")
        fingerprint_sha256 = fingerprint_pre["fingerprint"]["combined_sha256"]
        trial_spec = trial_spec_payload(
            session_uid=session_uid,
            trial_id=trial_id,
            candidate=candidate.payload(),
            candidate_token_low31=token,
            fingerprint_pre_sha256=fingerprint_sha256,
        )
        trial_spec_path = run_dir / "trial_spec.json"
        trial_spec_path.write_text(json.dumps(trial_spec), encoding="utf-8")
        trial_spec_sha256 = hashlib.sha256(trial_spec_path.read_bytes()).hexdigest()
        identity = {
            "backend_id": BACKEND_ID,
            "session_uid": session_uid,
            "candidate_uid": candidate_uid,
            "trial_uid": trial_spec["trial_uid"],
            "trial_spec_sha256": trial_spec_sha256,
            "physical_capture_uid": physical_capture_uid_from_sha256(capture_sha256),
        }
        artifacts = {
            "metadata": {
                "args": {
                    "output_dir": run,
                    "target_force_n": candidate.target_force_n,
                }
            },
            "summary": {"stop_reason": "signal_sigint"},
            "trial_runtime": {
                "schema_version": "step5b_autotune_trial_runtime_v4",
                **identity,
                "session_epoch": session_epoch,
                "trial_id": trial_id,
                "candidate_token_low31": token,
                "fingerprint_pre_sha256": fingerprint_sha256,
                "fingerprint_post_sha256": fingerprint_sha256,
            },
            "capture_complete": {
                "schema_version": "step5b_autotune_capture_manifest_v4",
                **identity,
                "fingerprint_pre_sha256": fingerprint_sha256,
                "fingerprint_post_sha256": fingerprint_sha256,
            },
            "fingerprint_pre": fingerprint_pre,
            "fingerprint_post": fingerprint_post,
        }
        artifact_paths: dict[str, Path] = {"trial_spec": trial_spec_path}
        for role, artifact in artifacts.items():
            path = run_dir / f"{role}.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            artifact_paths[role] = path
        provenance = {
            role: {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for role, path in artifact_paths.items()
        }
        provenance["bridge_csv"] = {
            "path": str(capture_path),
            "sha256": capture_sha256,
        }
        return {
            "schema_version": "step5b_autotune_evaluation_v4",
            "objective_name": "force_mae_n",
            "objective_unit": "N",
            "eligible": True,
            "feasible": True,
            "full_trial": True,
            "supervisor_closure_verified": True,
            "objective": objective,
            "candidate": candidate.payload(),
            "run_dir": run,
            "session_uid": session_uid,
            "trial_id": trial_id,
            "trial_uid": trial_spec["trial_uid"],
            "candidate_uid": candidate_uid,
            "physical_capture_uid": identity["physical_capture_uid"],
            "trial_spec_sha256": trial_spec_sha256,
            "backend_id": BACKEND_ID,
            "fingerprint": {
                "verified": True,
                "pre_combined_sha256": fingerprint_sha256,
                "post_combined_sha256": fingerprint_sha256,
            },
            "quarantined": False,
            "quarantine_path": None,
            "disposition": "OBJECTIVE",
            "metrics": {
                "stage25_duration_s": 60.0,
                "max_path_progress_s": 60.0,
                "max_sample_gap_s": 0.01,
                "time_strictly_monotonic": True,
            },
            "provenance": provenance,
            "failures": [],
        }

    def test_promotion_requires_repeatability_and_maps_step5d_fields(self) -> None:
        candidate = Candidate(12.0, force_p_gain=0.0012, force_damping=6.5, normal_filter_alpha=0.60)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observations = root / "observations.jsonl"
            first = self._evaluation(candidate, 1.0, root / "run-a.csv")
            observations.write_text(json.dumps(first) + "\n", encoding="utf-8")
            blocked = build_promotion_payload(observations)
            self.assertEqual(blocked["status"], "candidate_not_promotable")
            second = self._evaluation(candidate, 1.1, root / "run-b.csv")
            observations.write_text(
                json.dumps(first) + "\n" + json.dumps(second) + "\n",
                encoding="utf-8",
            )
            promoted = write_promotion_artifacts(observations, root)
            self.assertEqual(promoted["status"], "promotable")
            self.assertAlmostEqual(promoted["selection_score_mean_latest_two_force_mae_n"], 1.05)
            self.assertEqual(promoted["step5d_env"]["STEP5D_FORCE_P_GAIN"], 0.0012)
            self.assertEqual(promoted["step5d_env"]["STEP5D_FORCE_DAMPING"], 6.5)
            self.assertEqual(tuple(promoted["step5d_env"]), STEP5D_ENV_KEYS)
            self.assertEqual(
                promoted["source_trial_uids"],
                [first["trial_uid"], second["trial_uid"]],
            )
            env_path = Path(promoted["immutable_env"])
            self.assertIn("export STEP5D_NORMAL_FILTER_ALPHA=0.6", env_path.read_text(encoding="utf-8"))
            repeated = write_promotion_artifacts(observations, root)
            self.assertEqual(repeated["promotion_id"], promoted["promotion_id"])

    def test_promotion_rejects_old_objective_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            observations = Path(temporary) / "observations.jsonl"
            payload = self._evaluation(
                Candidate(12.0), 1.0, observations.parent / "legacy.csv"
            )
            payload["schema_version"] = "step5b_autotune_evaluation_v2"
            observations.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            result = build_promotion_payload(observations)
            self.assertEqual(result["status"], "candidate_not_promotable")
            self.assertTrue(list((observations.parent / "evidence_quarantine").glob("*.json")))

    def test_promotion_rehashes_capture_and_rejects_post_evaluation_tamper(self) -> None:
        candidate = Candidate(12.0)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observations = root / "observations.jsonl"
            first = self._evaluation(candidate, 0.8, root / "first.csv")
            second = self._evaluation(candidate, 0.82, root / "second.csv")
            observations.write_text(
                json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8"
            )
            Path(second["provenance"]["bridge_csv"]["path"]).write_bytes(b"tampered")
            result = build_promotion_payload(observations)
            quarantines = list((root / "evidence_quarantine").glob("*.json"))
        self.assertEqual(result["status"], "candidate_not_promotable")
        self.assertTrue(quarantines)

    def test_promotion_rehashes_non_capture_provenance(self) -> None:
        candidate = Candidate(12.0)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observations = root / "observations.jsonl"
            first = self._evaluation(candidate, 0.8, root / "first.csv")
            second = self._evaluation(candidate, 0.82, root / "second.csv")
            observations.write_text(
                json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8"
            )
            runtime_path = Path(second["provenance"]["trial_runtime"]["path"])
            runtime_path.write_text('{"tampered":true}\n', encoding="utf-8")
            result = build_promotion_payload(observations)
            quarantines = list((root / "evidence_quarantine").glob("*.json"))
        self.assertEqual(result["status"], "candidate_not_promotable")
        self.assertTrue(quarantines)

    def test_repeatable_nonincumbent_cannot_be_promoted(self) -> None:
        repeatable = Candidate(12.0, force_damping=7.0)
        incumbent = Candidate(12.0, force_damping=6.5)
        with tempfile.TemporaryDirectory() as temporary:
            observations = Path(temporary) / "observations.jsonl"
            payloads = [
                self._evaluation(repeatable, 1.0, observations.parent / "repeat-a.csv"),
                self._evaluation(repeatable, 1.05, observations.parent / "repeat-b.csv"),
                self._evaluation(
                    incumbent, 0.8, observations.parent / "incumbent-once.csv"
                ),
            ]
            observations.write_text(
                "".join(json.dumps(payload) + "\n" for payload in payloads),
                encoding="utf-8",
            )
            result = build_promotion_payload(observations)
        self.assertEqual(result["status"], "candidate_not_promotable")
        self.assertEqual(result["incumbent_candidate"], incumbent.payload())

    def test_promotion_rejects_duplicate_or_unclosed_trials_and_fingerprints_consumers(self) -> None:
        candidate = Candidate(12.0)
        with tempfile.TemporaryDirectory() as temporary:
            observations = Path(temporary) / "observations.jsonl"
            duplicate = self._evaluation(
                candidate, 1.0, observations.parent / "same-run.csv"
            )
            forged_identity = {**duplicate, "session_uid": "8" * 64, "trial_id": 2}
            forged_identity["trial_uid"] = trial_uid_from_identity(
                forged_identity["session_uid"],
                forged_identity["trial_id"],
                forged_identity["candidate_uid"],
            )
            observations.write_text(
                json.dumps(duplicate) + "\n" + json.dumps(forged_identity) + "\n",
                encoding="utf-8",
            )
            result = build_promotion_payload(observations)
            self.assertEqual(result["status"], "candidate_not_promotable")
            self.assertTrue(list((observations.parent / "evidence_quarantine").glob("*.json")))

        malformed_failure = {
            "schema_version": "step5b_autotune_evaluation_v4",
            "objective_name": "force_mae_n",
            "objective_unit": "N",
            "eligible": False,
            "feasible": False,
            "objective": None,
            "failures": ["metadata invalid"],
        }
        with tempfile.TemporaryDirectory() as temporary:
            observations = Path(temporary) / "observations.jsonl"
            unclosed = self._evaluation(
                candidate, 1.0, observations.parent / "unclosed.csv"
            )
            unclosed["eligible"] = False
            unclosed["supervisor_closure_verified"] = False
            unclosed["failures"] = ["closure missing"]
            observations.write_text(
                json.dumps(unclosed) + "\n" + json.dumps(malformed_failure) + "\n",
                encoding="utf-8",
            )
            result = build_promotion_payload(observations)
        self.assertEqual(result["status"], "candidate_not_promotable")
        self.assertIn("tools/step5b_autotune_promotion.py", FINGERPRINT_PATHS)
        self.assertIn("tools/step5b_autotune_evidence.py", FINGERPRINT_PATHS)
        self.assertIn("tools/kunwei_rtde_bridge.py", FINGERPRINT_PATHS)
        self.assertIn("tools/step5d_runtime_interface.py", FINGERPRINT_PATHS)


class Step5bAutotuneSanityTest(unittest.TestCase):
    def test_numeric_sanity_passes(self) -> None:
        result = run_sanity()
        self.assertTrue(result["pass"], result)


class Step5bAutotuneSupervisorTest(unittest.TestCase):
    def test_platform_or_integrity_evidence_stops_before_next_candidate(self) -> None:
        for disposition, quarantined in (
            ("PLATFORM_FAILURE", False),
            ("INTEGRITY_FAILURE", True),
        ):
            can_continue, fatal = supervisor.evidence_continuation(
                {
                    "disposition": disposition,
                    "quarantined": quarantined,
                    "failures": ["sensor_or_integrity_failure"],
                }
            )
            self.assertFalse(can_continue)
            self.assertIsNotNone(fatal)
        for disposition in ("OBJECTIVE", "PARAMETER_CONSTRAINT"):
            self.assertEqual(
                supervisor.evidence_continuation(
                    {
                        "disposition": disposition,
                        "quarantined": False,
                        "eligible": True,
                        "supervisor_closure_verified": True,
                        "failures": [],
                    }
                ),
                (True, None),
            )

    def test_abort_reason_can_continue_when_session_stop_is_not_requested(self) -> None:
        reason, fatal, should_continue = supervisor.trial_continuation(
            {"terminal_reason": 4, "home_verified": True, "fatal_detail": None},
            stop_requested=False,
        )
        self.assertEqual(reason, 4)
        self.assertIsNone(fatal)
        self.assertTrue(should_continue)
        self.assertFalse(
            supervisor.trial_continuation(
                {"terminal_reason": 4, "home_verified": True, "fatal_detail": None},
                stop_requested=True,
            )[2]
        )

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
        candidate = Candidate(target_force_n=12.0)
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
                supervisor.verify_bridge_metadata(run_dir, Candidate(12.0))

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

    def test_cross_directory_duplicate_trial_uid_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs_root = root / "runs"
            first_session = runs_root / "step5b_autotune_sessions" / "session_first"
            first_session.mkdir(parents=True)
            first_run = first_session / "trials" / "trial_a"
            first_run.mkdir(parents=True)
            capture_path = first_run / "bridge_rtde_500hz.csv"
            capture_path.write_bytes(b"same-physical-capture")
            capture_sha256 = hashlib.sha256(capture_path.read_bytes()).hexdigest()
            candidate = Candidate(12.0)
            candidate_uid = candidate_uid_from_payload(candidate.payload())
            session_uid = "7" * 64
            trial_uid = trial_uid_from_identity(session_uid, 1, candidate_uid)
            evaluation = {
                "schema_version": "step5b_autotune_evaluation_v4",
                "backend_id": BACKEND_ID,
                "session_uid": session_uid,
                "trial_id": 1,
                "trial_uid": trial_uid,
                "candidate_uid": candidate_uid,
                "physical_capture_uid": physical_capture_uid_from_sha256(capture_sha256),
                "trial_spec_sha256": "e" * 64,
                "objective_name": "force_mae_n",
                "objective_unit": "N",
                "eligible": True,
                "feasible": True,
                "objective": 0.5,
                "full_trial": True,
                "supervisor_closure_verified": True,
                "disposition": "OBJECTIVE",
                "fingerprint": {
                    "verified": True,
                    "pre_combined_sha256": "f" * 64,
                    "post_combined_sha256": "f" * 64,
                },
                "candidate": candidate.payload(),
                "failures": [],
                "quarantined": False,
                "provenance": {
                    "bridge_csv": {"path": str(capture_path), "sha256": capture_sha256}
                },
            }
            first = {**evaluation, "run_dir": str(first_session / "trials" / "trial_a")}
            (first_session / "observations.jsonl").write_text(
                json.dumps(first) + "\n", encoding="utf-8"
            )
            second_session = runs_root / "step5b_autotune_sessions" / "session_second"
            second_run = second_session / "trials" / "trial_b"
            second_run.mkdir(parents=True)
            second_observations = second_session / "observations.jsonl"
            with mock.patch.object(supervisor, "historical_run_roots", return_value=[runs_root]):
                checked = supervisor.enforce_unique_trial_uid(
                    {**evaluation, "run_dir": str(second_run)},
                    observations_path=second_observations,
                    run_dir=second_run,
                )
            quarantine_path = Path(checked["quarantine_path"])
            self.assertTrue(quarantine_path.is_file())
        self.assertFalse(checked["eligible"])
        self.assertFalse(checked["feasible"])
        self.assertIsNone(checked["objective"])
        self.assertIn(
            "duplicate_trial_or_physical_capture_uid_across_history", checked["failures"]
        )
        self.assertEqual(len(checked["duplicate_occurrences"]), 1)

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
