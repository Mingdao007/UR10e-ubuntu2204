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
MODULE_PATH = TOOLS / "build_per_stage_dual_sensor_contact_audit.py"
sys.path.insert(0, str(TOOLS))


def import_audit_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing generator: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_per_stage_dual_sensor_contact_audit", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def stage_manifest_payload(stage_id: str = "step5b") -> dict[str, object]:
    return {
        "schema": "ur10e_step_simulated_ft_evidence_pack_v1",
        "claim_tier": "simulated_ft",
        "stage_count": 1,
        "valid_stage_count": 1,
        "all_contact_stages_valid": True,
        "stages": {
            stage_id: {
                "stage_id": stage_id,
                "claim_tier": "simulated_ft",
                "valid": True,
                "source": "simulated_ft",
                "frame_id": "base",
                "sample_count": 3,
                "first_stamp_s": 0.1,
                "last_stamp_s": 0.2,
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


def row_summary_payload(contact_path: Path, *, stage_id: str = "step5b") -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_real_aligned_gui_matrix_row_v2",
        "stage": stage_id,
        "view": "close_detail",
        "observer_visual_pass": True,
        "visual_evidence_captured": True,
        "scripted_camera_evidence_captured": True,
        "trace_path": "runner/step5b/command_trace.csv",
        "final_visual_pose_world": {"frame": "gazebo_world", "x_m": 0.0, "y_m": 0.0, "z_m": 0.2},
        "force_contact_source": "gazebo_joint_state_fk_virtual_surface_model",
        "force_contact_physics_proven": False,
        "gazebo_contact_pair_log_path": str(contact_path),
        "model_composition_audit": {
            "eoat_collision_count": 7,
            "present_eoat_contact_collisions": ["eoat_contact_pad_collision", "eoat_contact_probe_collision"],
        },
    }


def contact_pair_payload(*, include_wrench: bool = False) -> dict[str, object]:
    row: dict[str, object] = {
        "stamp_s": 0.125,
        "stamp_evidence": True,
        "collision1": "step5_contact_surface::surface::collision",
        "collision2": "real_aligned_eoat_visual_stack::eoat_contact_pad_link::eoat_contact_pad_collision",
        "position_m": [0.0, 0.0, 0.01],
        "normal": [0.0, 0.0, 1.0],
        "normal_source": "gazebo_contact_message_normal",
        "contact_count": 4,
        "depth_m": 0.001,
    }
    if include_wrench:
        row["native_gazebo_contact_wrench"] = {
            "source": "gazebo_contact_message_wrench",
            "force_source_class": "gazebo_contact",
            "frame_id": "base",
            "status": "valid",
            "wrench_stamp_s": 0.125,
            "wrench_stamp_evidence": True,
        }
        row["raw_gazebo_contact_wrench_count"] = 4
    return {
        "schema": "ur10e_gazebo_contact_pair_log_v1",
        "claim_tier": "visual_only",
        "target_claim_tier": "physical Gazebo collision/contact physics",
        "topic": "/ur10e/contact/gazebo/step5b/contacts",
        "row_count": 1,
        "parse_issues": [],
        "rows": [row],
    }


def adapter_payload() -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_contact_wrench_adapter_report_v1",
        "claim_tier": "physical Gazebo collision/contact physics",
        "force_source": "gazebo_contact",
        "trace_written": True,
        "native_wrench_row_count": 1,
        "verified_native_wrench_row_count": 1,
        "total_contact_wrench_row_count": 1,
        "total_contact_wrench_proven": True,
        "wrench_aggregation_policy": "total_contact_wrench",
        "blockers": [],
        "wrench_trace": {
            "schema": "ur10e_canonical_wrench_trace_v1",
            "claim_tier": "physical Gazebo collision/contact physics",
            "force_source": "gazebo_contact",
            "rows": [
                {
                    "header": {"stamp_s": 0.126, "frame_id": "base"},
                    "source": "gazebo_contact",
                    "status": "valid",
                    "contact_state": "contact",
                    "normal_load_n": 12.5,
                    "baseline_policy": "gazebo_contact_zero_no_contact_baseline",
                }
            ],
        },
    }


def observation_payload() -> dict[str, object]:
    return {
        "observation_id": "stage-step5b-dual-sensor-fixture-001",
        "explicit": True,
        "time_window": {
            "start": "2026-06-21T16:55:00+08:00",
            "end": "2026-06-21T16:55:10+08:00",
            "clock_source": "/clock",
        },
        "surfaces": {
            "stage_row_summary": "row_summary.json",
            "stage_contact_pair_log": "gazebo_contact_pair_log.json",
            "stage_contact_wrench_adapter": "stage_contact_wrench_adapter.json",
            "stage_simulated_ft_manifest": "step_simulated_ft_evidence_manifest.json",
            "step_status_rnn_audit": "step_status_rnn_audit.json",
            "visual_evidence": "scripted_camera_final.png",
            "tcp_path_evidence": "command_trace.csv",
        },
    }


def step_status_payload(stage_id: str = "step5b") -> dict[str, object]:
    return {
        "schema": "ur10e_step_status_rnn_audit_v1",
        "step_status_matrix": [
            {
                "stage_id": stage_id,
                "claim_tier": "simulated_ft",
                "gazebo_contact_physics_status": "per_stage_fixture",
                "simulated_ft_status": "per_stage_canonical_log_evidence_attached",
                "current_blocker": "fixture",
            }
        ],
    }


def write_fixture(root: Path, *, include_adapter: bool, include_observation: bool) -> dict[str, Path | None]:
    contact_path = root / "gazebo_contact_pair_log.json"
    row_path = root / "row_summary.json"
    stage_manifest_path = root / "step_simulated_ft_evidence_manifest.json"
    step_status_path = root / "step_status_rnn_audit.json"
    adapter_path = root / "stage_contact_wrench_adapter.json" if include_adapter else None
    observation_path = root / "same_run_observation.json" if include_observation else None
    write_json(contact_path, contact_pair_payload(include_wrench=include_adapter))
    write_json(row_path, row_summary_payload(contact_path))
    write_json(stage_manifest_path, stage_manifest_payload())
    write_json(step_status_path, step_status_payload())
    if adapter_path:
        write_json(adapter_path, adapter_payload())
    if observation_path:
        write_json(observation_path, observation_payload())
    return {
        "row": row_path,
        "contact": contact_path,
        "manifest": stage_manifest_path,
        "step": step_status_path,
        "adapter": adapter_path,
        "observation": observation_path,
    }


class PerStageDualSensorContactAuditTest(unittest.TestCase):
    def test_current_step5b_clean_row_stays_simulated_ft_and_blocks_physical_contact(self) -> None:
        audit = import_audit_module()
        payload = audit.build_audit(generated_at="2026-06-21T16:55:00+08:00")

        self.assertEqual(payload["schema"], "ur10e_per_stage_dual_sensor_contact_audit_v1")
        self.assertEqual(payload["stage_id"], "step5b")
        self.assertEqual(payload["claim_tier"], "simulated_ft")
        self.assertTrue(payload["stage_simulated_ft"]["valid"])
        self.assertTrue(payload["stage_contact_pair_log"]["evidence"])
        self.assertEqual(payload["stage_contact_pair_log"]["native_wrench_row_count"], 0)
        self.assertFalse(payload["per_stage_physical_gazebo_contact"]["per_stage_physical_gazebo_contact_proven"])
        self.assertFalse(payload["same_run_stage_dual_sensor_observation"]["same_run_stage_dual_sensor_observation_proven"])
        self.assertIn("stage_total_contact_wrench:not_proven", payload["blockers"])

    def test_accepts_complete_stage_specific_same_run_fixture(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="stage_dual_sensor_complete_", dir=RUNS) as tmp:
            paths = write_fixture(Path(tmp), include_adapter=True, include_observation=True)
            payload = audit.build_audit(
                generated_at="2026-06-21T16:55:30+08:00",
                stage_row_summary_path=paths["row"],
                stage_contact_pair_log_path=paths["contact"],
                stage_simulated_ft_manifest_path=paths["manifest"],
                step_status_audit_path=paths["step"],
                stage_contact_wrench_adapter_path=paths["adapter"],
                same_run_observation_manifest_path=paths["observation"],
            )

        self.assertEqual(payload["claim_tier"], "physical Gazebo collision/contact physics")
        self.assertTrue(payload["per_stage_physical_gazebo_contact"]["per_stage_physical_gazebo_contact_proven"])
        self.assertTrue(payload["same_run_stage_dual_sensor_observation"]["same_run_stage_dual_sensor_observation_proven"])
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["cross_run_surfaces"], [])

    def test_blocks_complete_physical_fixture_without_same_run_observation(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="stage_dual_sensor_no_observation_", dir=RUNS) as tmp:
            paths = write_fixture(Path(tmp), include_adapter=True, include_observation=False)
            payload = audit.build_audit(
                generated_at="2026-06-21T16:56:00+08:00",
                stage_row_summary_path=paths["row"],
                stage_contact_pair_log_path=paths["contact"],
                stage_simulated_ft_manifest_path=paths["manifest"],
                step_status_audit_path=paths["step"],
                stage_contact_wrench_adapter_path=paths["adapter"],
            )

        self.assertTrue(payload["per_stage_physical_gazebo_contact"]["per_stage_physical_gazebo_contact_proven"])
        self.assertFalse(payload["same_run_stage_dual_sensor_observation"]["same_run_stage_dual_sensor_observation_proven"])
        self.assertIn("same_run_stage_dual_sensor_observation:not_proven", payload["blockers"])
        self.assertIn("same_run_stage_dual_sensor_observation:missing", payload["validation_issues"])

    def test_write_audit_creates_stage_scoped_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="stage_dual_sensor_write_", dir=RUNS) as tmp:
            root = Path(tmp)
            paths = write_fixture(root / "source", include_adapter=False, include_observation=False)
            output_dir = root / "audit"
            path = audit.write_audit(
                output_dir,
                generated_at="2026-06-21T16:56:30+08:00",
                stage_row_summary_path=paths["row"],
                stage_contact_pair_log_path=paths["contact"],
                stage_simulated_ft_manifest_path=paths["manifest"],
                step_status_audit_path=paths["step"],
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema"], "ur10e_per_stage_dual_sensor_contact_audit_v1")
        self.assertTrue(payload["artifact_path"].endswith("step5b_per_stage_dual_sensor_contact_audit.json"))
        self.assertFalse(payload["per_stage_physical_gazebo_contact"]["per_stage_physical_gazebo_contact_proven"])


if __name__ == "__main__":
    unittest.main()
