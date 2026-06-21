#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
RUNS = ROOT / "runs"
MODULE_PATH = TOOLS / "build_stage_dual_sensor_observation_manifest.py"
sys.path.insert(0, str(TOOLS))


def import_manifest_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing generator: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_stage_dual_sensor_observation_manifest", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_file(path: Path, content: str = "{}\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def surface_paths(root: Path) -> dict[str, Path]:
    return {
        "stage_row_summary": write_file(root / "row_summary.json"),
        "stage_contact_pair_log": write_file(root / "gazebo_contact_pair_log.json"),
        "stage_contact_wrench_adapter": write_file(root / "stage_contact_wrench_adapter.json"),
        "stage_simulated_ft_manifest": write_file(root / "step_simulated_ft_evidence_manifest.json"),
        "step_status_rnn_audit": write_file(root / "step_status_rnn_audit.json"),
        "visual_evidence": write_file(root / "scripted_camera_final.png", "png-placeholder\n"),
        "tcp_path_evidence": write_file(root / "command_trace.csv", "t,x,y,z\n"),
    }


class StageDualSensorObservationManifestTest(unittest.TestCase):
    def test_accepts_complete_same_run_stage_surfaces(self) -> None:
        manifest = import_manifest_module()
        with tempfile.TemporaryDirectory(prefix="stage_observation_complete_", dir=RUNS) as tmp:
            root = Path(tmp)
            payload = manifest.build_manifest(
                stage_id="step5b",
                observation_id="stage-step5b-same-run-001",
                time_start="2026-06-21T17:12:00+08:00",
                time_end="2026-06-21T17:12:10+08:00",
                clock_source="/clock",
                surfaces=surface_paths(root),
                generated_at="2026-06-21T17:12:20+08:00",
            )

        self.assertEqual(payload["schema"], "ur10e_stage_dual_sensor_observation_manifest_v1")
        self.assertTrue(payload["same_run_stage_dual_sensor_observation_proven"])
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["missing_surfaces"], [])
        self.assertEqual(payload["cross_run_surfaces"], [])
        self.assertEqual(set(payload["surfaces"]), set(manifest.REQUIRED_SURFACES))
        self.assertFalse(payload["live_authorization"]["real_bench_live_contact_authorized"])

    def test_blocks_missing_adapter_and_cross_run_surfaces(self) -> None:
        manifest = import_manifest_module()
        with tempfile.TemporaryDirectory(prefix="stage_observation_a_", dir=RUNS) as left:
            with tempfile.TemporaryDirectory(prefix="stage_observation_b_", dir=RUNS) as right:
                surfaces = surface_paths(Path(left))
                surfaces["stage_contact_wrench_adapter"] = Path(left) / "missing_adapter.json"
                surfaces["stage_simulated_ft_manifest"] = write_file(Path(right) / "step_simulated_ft_evidence_manifest.json")
                payload = manifest.build_manifest(
                    stage_id="step5b",
                    observation_id="stage-step5b-cross-run-001",
                    time_start="2026-06-21T17:13:00+08:00",
                    time_end="2026-06-21T17:13:10+08:00",
                    clock_source="/clock",
                    surfaces=surfaces,
                    generated_at="2026-06-21T17:13:20+08:00",
                )

        self.assertFalse(payload["same_run_stage_dual_sensor_observation_proven"])
        self.assertIn("same_run_stage_dual_sensor_observation:not_proven", payload["blockers"])
        self.assertIn("stage_contact_wrench_adapter", payload["missing_surfaces"])
        self.assertIn("stage_simulated_ft_manifest", payload["cross_run_surfaces"])
        self.assertIn("required_stage_surfaces:missing:stage_contact_wrench_adapter", payload["validation_issues"])

    def test_write_manifest_creates_stage_scoped_artifact(self) -> None:
        manifest = import_manifest_module()
        with tempfile.TemporaryDirectory(prefix="stage_observation_write_", dir=RUNS) as tmp:
            root = Path(tmp)
            output_path = manifest.write_manifest(
                root / "out",
                stage_id="step5b",
                observation_id="stage-step5b-write-001",
                time_start="2026-06-21T17:14:00+08:00",
                time_end="2026-06-21T17:14:10+08:00",
                clock_source="/clock",
                surfaces=surface_paths(root / "run"),
                generated_at="2026-06-21T17:14:20+08:00",
            )
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(output_path.name, "step5b_same_run_stage_dual_sensor_observation_manifest.json")
        self.assertEqual(payload["artifact_path"], str(output_path))
        self.assertTrue(payload["same_run_stage_dual_sensor_observation_proven"])


if __name__ == "__main__":
    unittest.main()
