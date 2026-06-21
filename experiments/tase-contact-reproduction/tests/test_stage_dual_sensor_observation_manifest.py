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
OBSERVATION_ID = "stage-step5b-same-run-001"


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


def write_json(path: Path, payload: dict[str, object]) -> Path:
    return write_file(path, json.dumps(payload, indent=2) + "\n")


def row_summary_payload(stage_id: str = "step5b") -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_real_aligned_gui_matrix_row_v2",
        "stage": stage_id,
        "visual_evidence_captured": True,
        "scripted_camera_evidence_captured": True,
        "trace_path": "command_trace.csv",
        "model_composition_audit": {"eoat_collision_count": 2},
    }


def contact_pair_payload() -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_contact_pair_log_v1",
        "stage_id": "step5b",
        "observation_id": OBSERVATION_ID,
        "observation_scope": "same_run_stage_gazebo_row",
        "time_window": {
            "start": "2026-06-21T17:12:00+08:00",
            "end": "2026-06-21T17:12:10+08:00",
            "clock_source": "/clock",
        },
        "parse_issues": [],
        "rows": [
            {
                "stamp_s": 1.25,
                "collision1": "step5_contact_surface::surface::collision",
                "collision2": "real_aligned_eoat_visual_stack::eoat_contact_pad_link::eoat_contact_pad_collision",
                "position_m": [0.0, 0.0, 0.01],
                "normal": [0.0, 0.0, 1.0],
                "normal_source": "gazebo_contact_message_normal",
                "contact_count": 3,
            }
        ],
    }


def wrench_adapter_payload() -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_contact_wrench_adapter_report_v1",
        "stage_id": "step5b",
        "observation_id": OBSERVATION_ID,
        "observation_scope": "same_run_stage_gazebo_row",
        "time_window": {
            "start": "2026-06-21T17:12:00+08:00",
            "end": "2026-06-21T17:12:10+08:00",
            "clock_source": "/clock",
        },
        "claim_tier": "physical Gazebo collision/contact physics",
        "force_source": "gazebo_contact",
        "trace_written": True,
        "total_contact_wrench_proven": True,
        "wrench_aggregation_policy": "total_contact_wrench",
        "verified_native_wrench_row_count": 1,
        "total_contact_wrench_row_count": 1,
        "blockers": [],
        "wrench_trace": {
            "rows": [
                {
                    "header": {"stamp_s": 1.25, "frame_id": "base"},
                    "source": "gazebo_contact",
                    "status": "valid",
                    "contact_state": "contact",
                    "normal_load_n": 12.5,
                    "baseline_policy": "gazebo_contact_zero_no_contact_baseline",
                }
            ]
        },
    }


def simulated_ft_manifest_payload(stage_id: str = "step5b") -> dict[str, object]:
    return {
        "schema": "ur10e_step_simulated_ft_evidence_pack_v1",
        "claim_tier": "simulated_ft",
        "stages": {
            stage_id: {
                "claim_tier": "simulated_ft",
                "valid": True,
                "evidence_fields_present": {
                    "stamp": True,
                    "frame_id": True,
                    "source": True,
                    "status": True,
                    "baseline": True,
                    "log_evidence": True,
                },
            }
        },
    }


def step_status_payload(stage_id: str = "step5b") -> dict[str, object]:
    return {
        "schema": "ur10e_step_status_rnn_audit_v1",
        "step_status_matrix": [{"stage_id": stage_id, "claim_tier": "simulated_ft"}],
    }


def surface_paths(root: Path) -> dict[str, Path]:
    return {
        "stage_row_summary": write_json(root / "row_summary.json", row_summary_payload()),
        "stage_contact_pair_log": write_json(root / "gazebo_contact_pair_log.json", contact_pair_payload()),
        "stage_contact_wrench_adapter": write_json(root / "stage_contact_wrench_adapter.json", wrench_adapter_payload()),
        "stage_simulated_ft_manifest": write_json(
            root / "step_simulated_ft_evidence_manifest.json",
            simulated_ft_manifest_payload(),
        ),
        "step_status_rnn_audit": write_json(root / "step_status_rnn_audit.json", step_status_payload()),
        "visual_evidence": write_file(root / "scripted_camera_final.png", "png-placeholder\n"),
        "tcp_path_evidence": write_file(root / "command_trace.csv", "t,x,y,z\n"),
    }


def placeholder_surface_paths(root: Path) -> dict[str, Path]:
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
                observation_id=OBSERVATION_ID,
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
        self.assertTrue(payload["content_validation"]["surface_content_proven"])
        self.assertFalse(payload["live_authorization"]["real_bench_live_contact_authorized"])

    def test_blocks_placeholder_surfaces_without_content_evidence(self) -> None:
        manifest = import_manifest_module()
        with tempfile.TemporaryDirectory(prefix="stage_observation_placeholder_", dir=RUNS) as tmp:
            payload = manifest.build_manifest(
                stage_id="step5b",
                observation_id="stage-step5b-placeholder-001",
                time_start="2026-06-21T17:12:00+08:00",
                time_end="2026-06-21T17:12:10+08:00",
                clock_source="/clock",
                surfaces=placeholder_surface_paths(Path(tmp)),
                generated_at="2026-06-21T17:12:20+08:00",
            )

        self.assertFalse(payload["same_run_stage_dual_sensor_observation_proven"])
        self.assertIn("same_run_stage_dual_sensor_observation:not_proven", payload["blockers"])
        self.assertFalse(payload["content_validation"]["surface_content_proven"])
        self.assertIn(
            "stage_contact_wrench_adapter.total_contact_wrench_proven:not_true",
            payload["validation_issues"],
        )
        self.assertIn(
            "stage_contact_pair_log.observation_id:mismatch_or_missing",
            payload["validation_issues"],
        )

    def test_blocks_contact_artifacts_with_mismatched_observation_id(self) -> None:
        manifest = import_manifest_module()
        with tempfile.TemporaryDirectory(prefix="stage_observation_id_mismatch_", dir=RUNS) as tmp:
            root = Path(tmp)
            surfaces = surface_paths(root)
            contact = json.loads(surfaces["stage_contact_pair_log"].read_text(encoding="utf-8"))
            contact["observation_id"] = "different-contact-observation"
            write_json(surfaces["stage_contact_pair_log"], contact)
            adapter = json.loads(surfaces["stage_contact_wrench_adapter"].read_text(encoding="utf-8"))
            adapter["observation_id"] = "different-adapter-observation"
            write_json(surfaces["stage_contact_wrench_adapter"], adapter)
            payload = manifest.build_manifest(
                stage_id="step5b",
                observation_id=OBSERVATION_ID,
                time_start="2026-06-21T17:12:00+08:00",
                time_end="2026-06-21T17:12:10+08:00",
                clock_source="/clock",
                surfaces=surfaces,
                generated_at="2026-06-21T17:12:20+08:00",
            )

        self.assertFalse(payload["same_run_stage_dual_sensor_observation_proven"])
        self.assertIn("stage_contact_pair_log.observation_id:mismatch_or_missing", payload["validation_issues"])
        self.assertIn("stage_contact_wrench_adapter.observation_id:mismatch_or_missing", payload["validation_issues"])

    def test_blocks_contact_artifacts_with_mismatched_time_window(self) -> None:
        manifest = import_manifest_module()
        with tempfile.TemporaryDirectory(prefix="stage_observation_time_mismatch_", dir=RUNS) as tmp:
            root = Path(tmp)
            surfaces = surface_paths(root)
            contact = json.loads(surfaces["stage_contact_pair_log"].read_text(encoding="utf-8"))
            contact["time_window"]["end"] = "2026-06-21T17:12:11+08:00"
            write_json(surfaces["stage_contact_pair_log"], contact)
            adapter = json.loads(surfaces["stage_contact_wrench_adapter"].read_text(encoding="utf-8"))
            adapter["time_window"]["clock_source"] = "wall_time"
            write_json(surfaces["stage_contact_wrench_adapter"], adapter)
            payload = manifest.build_manifest(
                stage_id="step5b",
                observation_id=OBSERVATION_ID,
                time_start="2026-06-21T17:12:00+08:00",
                time_end="2026-06-21T17:12:10+08:00",
                clock_source="/clock",
                surfaces=surfaces,
                generated_at="2026-06-21T17:12:20+08:00",
            )

        self.assertFalse(payload["same_run_stage_dual_sensor_observation_proven"])
        self.assertIn("stage_contact_pair_log.time_window.end:mismatch", payload["validation_issues"])
        self.assertIn("stage_contact_wrench_adapter.time_window.clock_source:mismatch", payload["validation_issues"])

    def test_blocks_invalid_manifest_time_window_order(self) -> None:
        manifest = import_manifest_module()
        with tempfile.TemporaryDirectory(prefix="stage_observation_bad_time_order_", dir=RUNS) as tmp:
            payload = manifest.build_manifest(
                stage_id="step5b",
                observation_id=OBSERVATION_ID,
                time_start="2026-06-21T17:12:10+08:00",
                time_end="2026-06-21T17:12:00+08:00",
                clock_source="/clock",
                surfaces=surface_paths(Path(tmp)),
                generated_at="2026-06-21T17:12:20+08:00",
            )

        self.assertFalse(payload["same_run_stage_dual_sensor_observation_proven"])
        self.assertIn("time_window.order:invalid", payload["validation_issues"])

    def test_blocks_standalone_contact_witness_scope(self) -> None:
        manifest = import_manifest_module()
        with tempfile.TemporaryDirectory(prefix="stage_observation_standalone_scope_", dir=RUNS) as tmp:
            root = Path(tmp)
            surfaces = surface_paths(root)
            contact = json.loads(surfaces["stage_contact_pair_log"].read_text(encoding="utf-8"))
            contact["observation_scope"] = "standalone_p2_contact_witness"
            write_json(surfaces["stage_contact_pair_log"], contact)
            adapter = json.loads(surfaces["stage_contact_wrench_adapter"].read_text(encoding="utf-8"))
            adapter["observation_scope"] = "standalone_p2_contact_witness"
            write_json(surfaces["stage_contact_wrench_adapter"], adapter)
            payload = manifest.build_manifest(
                stage_id="step5b",
                observation_id=OBSERVATION_ID,
                time_start="2026-06-21T17:12:00+08:00",
                time_end="2026-06-21T17:12:10+08:00",
                clock_source="/clock",
                surfaces=surfaces,
                generated_at="2026-06-21T17:12:20+08:00",
            )

        self.assertFalse(payload["same_run_stage_dual_sensor_observation_proven"])
        self.assertIn("stage_contact_pair_log.observation_scope:not_same_run_stage_gazebo_row", payload["validation_issues"])
        self.assertIn(
            "stage_contact_wrench_adapter.observation_scope:not_same_run_stage_gazebo_row",
            payload["validation_issues"],
        )

    def test_blocks_missing_adapter_and_cross_run_surfaces(self) -> None:
        manifest = import_manifest_module()
        with tempfile.TemporaryDirectory(prefix="stage_observation_a_", dir=RUNS) as left:
            with tempfile.TemporaryDirectory(prefix="stage_observation_b_", dir=RUNS) as right:
                surfaces = surface_paths(Path(left))
                surfaces["stage_contact_wrench_adapter"] = Path(left) / "missing_adapter.json"
                surfaces["stage_simulated_ft_manifest"] = write_file(Path(right) / "step_simulated_ft_evidence_manifest.json")
                payload = manifest.build_manifest(
                    stage_id="step5b",
                    observation_id=OBSERVATION_ID,
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
                observation_id=OBSERVATION_ID,
                time_start="2026-06-21T17:12:00+08:00",
                time_end="2026-06-21T17:12:10+08:00",
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
