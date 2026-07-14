#!/usr/bin/env python3
"""No-motion tests for the Step5d parallel workflow composition."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from run_step5d_parallel_workflow import (  # noqa: E402
    FEATURE_WINDOWS_S,
    FORMAL_SOURCE_FILES,
    SHORT_TIMING_SAMPLES,
    formal_task,
    functional_tasks,
    postprocess_tasks,
    source_fingerprint,
)
from ur10e_parallel import ResourceProfile, TaskRunner  # noqa: E402


class Step5dParallelWorkflowTest(unittest.TestCase):
    def profile(self, root: Path, *, parallel: bool) -> ResourceProfile:
        return ResourceProfile(
            cpu_workers=4,
            gpu_workers=3,
            gpu_vram_limit_pct=85.0,
            parallel=parallel,
            lock_root=root / "locks",
        )

    def test_short_rnn_windows_are_feature_bearing_and_diagnostic_only(self) -> None:
        self.assertEqual(
            SHORT_TIMING_SAMPLES,
            {
                "solver_samples": 128,
                "tick_samples": 256,
                "safe_hold_samples": 256,
                "component_diagnostic_samples": 64,
                "inner_iterations": 512,
            },
        )
        self.assertEqual(FEATURE_WINDOWS_S, {"mujoco_startup": 0.08, "mujoco_steady": 0.40})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay = root / "replay.csv"
            model = root / "model.json"
            replay.write_text("header\n", encoding="utf-8")
            model.write_text("{}\n", encoding="utf-8")
            tasks = functional_tasks(root / "out", replay_csv=replay, model_manifest=model)
        by_id = {task.task_id: task for task in tasks}
        lane = by_id["persistent-rnn-gpu-lane"]
        self.assertEqual(lane.claim_class, "diagnostic_only")
        self.assertEqual(lane.resource, "gpu_rnn")
        self.assertIn("run_step5d_rnn_diagnostic_lane.py", " ".join(lane.command))

    def test_formal_fingerprint_binds_workflow_evaluator_replay_bundle_and_environment(self) -> None:
        self.assertIn("tools/run_step5d_parallel_workflow.py", FORMAL_SOURCE_FILES)
        self.assertIn("tools/step5d_timing_acceptance.py", FORMAL_SOURCE_FILES)
        with tempfile.TemporaryDirectory() as directory:
            replay = Path(directory) / "replay.csv"
            replay.write_text("first\n", encoding="utf-8")
            first = source_fingerprint(
                replay_csv=replay, environment={"PYTHONPATH": "/a"}, bundle=b"bundle-a",
            )
            replay.write_text("second\n", encoding="utf-8")
            replay_changed = source_fingerprint(
                replay_csv=replay, environment={"PYTHONPATH": "/a"}, bundle=b"bundle-a",
            )
            environment_changed = source_fingerprint(
                replay_csv=replay, environment={"PYTHONPATH": "/b"}, bundle=b"bundle-a",
            )
            bundle_changed = source_fingerprint(
                replay_csv=replay, environment={"PYTHONPATH": "/b"}, bundle=b"bundle-b",
            )
            execution_changed = source_fingerprint(
                replay_csv=replay, environment={"PYTHONPATH": "/b"}, bundle=b"bundle-b",
                execution_contract={"formal": True},
            )
        self.assertNotEqual(first, replay_changed)
        self.assertNotEqual(replay_changed, environment_changed)
        self.assertNotEqual(environment_changed, bundle_changed)
        self.assertNotEqual(bundle_changed, execution_changed)

    def test_offline_all_formal_gate_depends_on_every_functional_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay = root / "replay.csv"
            model = root / "model.json"
            replay.write_text("header\n", encoding="utf-8")
            model.write_text("{}\n", encoding="utf-8")
            functional = functional_tasks(root / "out", replay_csv=replay, model_manifest=model)
            formal = formal_task(
                root / "out",
                replay_csv=replay,
                dependencies=[task.task_id for task in functional],
            )
        self.assertEqual(formal.resource, "formal_timing")
        self.assertEqual(formal.claim_class, "formal_raw_capture")
        self.assertEqual(set(formal.dependencies), {task.task_id for task in functional})

    def test_postprocess_parallel_and_serial_have_identical_derived_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "immutable-run"
            run_dir.mkdir()
            marker = {"immutable": True, "capture_closed": True}
            (run_dir / ".capture_complete.json").write_text(
                json.dumps(marker) + "\n", encoding="utf-8"
            )
            fields = [
                "t_monotonic_s",
                "ur_output_double_register_26",
                "ur_output_double_register_30",
                "ur_output_double_register_35",
                "ur_output_double_register_36",
                "_step4e_normal_load_n",
                "_step5d_force_settle_filtered_normal_load_n",
                "force_norm_n",
            ]
            with (run_dir / "bridge_rtde_500hz.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                for index in range(20):
                    writer.writerow(
                        {
                            "t_monotonic_s": index * 0.002,
                            "ur_output_double_register_26": index % 2,
                            "ur_output_double_register_30": 0,
                            "ur_output_double_register_35": 20,
                            "ur_output_double_register_36": 20.95,
                            "_step4e_normal_load_n": 10,
                            "_step5d_force_settle_filtered_normal_load_n": 10,
                            "force_norm_n": 10,
                        }
                    )

            payloads: list[dict[str, object]] = []
            for name, parallel in (("serial", False), ("parallel", True)):
                output = root / name
                runner = TaskRunner(
                    root=ROOT,
                    output_root=output,
                    profile=self.profile(root / name, parallel=parallel),
                    gpu_usage=lambda: 0.0,
                )
                results = runner.run(postprocess_tasks(output, run_dir))
                self.assertTrue(all(result.status == "passed" for result in results.values()))
                payloads.append(
                    {
                        "frequency": json.loads(
                            (output / "frequency-summary/stage_frequency_summary.json").read_text()
                        ),
                        "analysis": json.loads(
                            (output / "step5d-analysis/step5d_bridge_analysis.json").read_text()
                        ),
                        "checksums": json.loads(
                            (output / "source-checksums/derived_checksums.json").read_text()
                        ),
                    }
                )
            self.assertEqual(payloads[0], payloads[1])

    def test_force_overview_waits_for_analysis_and_is_diagnostic_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "immutable-run"
            run_dir.mkdir()
            (run_dir / "bridge_rtde_500hz.csv").write_text("t_monotonic_s\n0\n", encoding="utf-8")
            tasks = {item.task_id: item for item in postprocess_tasks(root / "out", run_dir)}
        overview = tasks["force-overview"]
        self.assertEqual(set(overview.dependencies), {"frequency-summary", "step5d-analysis"})
        self.assertEqual(overview.claim_class, "diagnostic_only")
        self.assertIn("build_step5d_force_overview.py", " ".join(overview.command))


if __name__ == "__main__":
    unittest.main()
