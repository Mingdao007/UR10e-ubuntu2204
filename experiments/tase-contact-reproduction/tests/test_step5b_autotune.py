from __future__ import annotations

import json
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
from step5b_autotune_optimizer import Observation, choose_candidate, tier2_is_unlocked  # noqa: E402
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
        stamp = "2026-07-14T1800HKT_STEP5B_CONTACT_CYCLOID_BAYES_LOOP_V1"
        script = build_script(stamp, "2026-07-14T18:00:00+08:00")
        txt = build_txt(stamp)
        urp = build_urp(script, PROGRAM_BASENAME, CONTROLLER_DIRECTORY)
        validate(script, txt, urp, stamp)
        self.assertIn("local skip_lift_attitude = 0", script)
        self.assertIn("write_output_float_register(35, 25.2)", script)
        self.assertIn("read_input_integer_register(24)", script)
        self.assertIn("position_error_m <= 0.003", script)
        self.assertEqual(script.count("\ncodex_step5b_contact_cycloid_bayes_loop_v1()\n"), 1)


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
                "_step4e_live_normal_candidate_force_n": np.full(count, load_n),
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

    def test_bayesian_selection_runs_on_cuda(self) -> None:
        try:
            import torch
        except ImportError:
            self.skipTest("torch not installed")
        if not torch.cuda.is_available():
            self.skipTest("CUDA not available")
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
        self.assertIn("botorch", details["selection"])


class Step5bAutotuneSanityTest(unittest.TestCase):
    def test_numeric_sanity_passes(self) -> None:
        result = run_sanity()
        self.assertTrue(result["pass"], result)


class Step5bAutotuneSupervisorTest(unittest.TestCase):
    def test_observer_failure_stops_bridge_before_capture_marker(self) -> None:
        candidate = Candidate(target_force_n=10.0)
        fake_process = mock.Mock()
        fake_process.poll.return_value = None
        observer_context = mock.MagicMock()
        observer_context.__enter__.return_value = (mock.Mock(), 1, [])
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            supervisor, "tp_observer", return_value=observer_context
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
            self.assertTrue((outcome["run_dir"] / "capture_complete.json").is_file())
        stop_child.assert_called_once_with(fake_process, home_observed=False)
        self.assertIn("observer failure", outcome["runtime"]["fatal_detail"])


if __name__ == "__main__":
    unittest.main()
