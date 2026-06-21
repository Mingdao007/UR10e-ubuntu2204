#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
RUNS = ROOT / "runs"
MODULE_PATH = TOOLS / "build_dual_sensor_total_wrench_audit.py"
sys.path.insert(0, str(TOOLS))


def import_audit_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing generator: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_dual_sensor_total_wrench_audit", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def stage_manifest_payload() -> dict[str, object]:
    stages = {
        stage_id: {
            "stage_id": stage_id,
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
        for stage_id in ["step5b", "step5d", "step6b", "step7", "step8"]
    }
    return {
        "schema": "ur10e_step_simulated_ft_evidence_pack_v1",
        "claim_tier": "simulated_ft",
        "all_contact_stages_valid": True,
        "stage_count": 5,
        "valid_stage_count": 5,
        "stages": stages,
    }


def dual_sensor_observation_payload() -> dict[str, object]:
    return {
        "observation_id": "fixture-dual-sensor-observation-001",
        "explicit": True,
        "time_window": {
            "start": "2026-06-21T09:56:00+08:00",
            "end": "2026-06-21T09:56:30+08:00",
            "clock_source": "/clock",
        },
        "surfaces": {
            "stage_simulated_ft_manifest": "stage_simulated_ft_manifest.json",
            "p2_contact_correlation_audit": "p2_contact_correlation_audit.json",
            "step_status_rnn_audit": "step_status_rnn_audit.json",
        },
    }


def total_wrench_row_payload() -> dict[str, object]:
    return {
        "header": {"stamp_s": 0.1, "frame_id": "base"},
        "source": "gazebo_contact",
        "status": "valid",
        "contact_state": "contact",
        "normal_load_n": 12.5,
        "baseline_policy": "gazebo_contact_zero_no_contact_baseline",
        "diagnostic_flags": [
            "gazebo_contact_wrench_adapter",
            "gazebo_contact_message_wrench",
            "total_contact_wrench",
            "total_contact_wrench_component_count=4",
        ],
    }


def p2_payload(*, total_wrench: bool, same_run_dual: bool) -> dict[str, object]:
    wrench_policy = "total_contact_wrench" if total_wrench else "single_native_contact_point_wrench_sample_no_total_contact_wrench_claim"
    return {
        "schema": "ur10e_gazebo_p2_contact_correlation_audit_v1",
        "claim_tier": "physical Gazebo collision/contact physics",
        "physical_gazebo_contact_gate": {
            "force_contact_physics_proven": True,
            "contact_pair_log_evidence": True,
            "wrench_contact_correlation": True,
        },
        "claim_boundary_gate": {
            "total_contact_wrench_proven": total_wrench,
            "real_bench_live_contact_authorized": False,
        },
        "same_run_concurrent_dual_sensor_observation": dual_sensor_observation_payload()
        if same_run_dual
        else False,
        "wrench_evidence": {
            "adapter_report_schema": "ur10e_gazebo_contact_wrench_adapter_report_v1",
            "source": "gazebo_contact" if total_wrench else None,
            "trace_written": total_wrench,
            "verified_native_wrench_row_count": 1 if total_wrench else 0,
            "total_contact_wrench_row_count": 1 if total_wrench else 0,
            "adapter_report_blockers": [],
            "total_contact_wrench_blockers": [],
            "wrench_aggregation_policy": wrench_policy,
            "total_contact_wrench_proven": total_wrench,
            "rows": [total_wrench_row_payload()] if total_wrench else [],
        },
    }


def step_payload(*, total_wrench: bool, same_run_dual: bool) -> dict[str, object]:
    return {
        "schema": "ur10e_step_status_rnn_audit_v1",
        "p2_physical_gazebo_contact": {
            "claim_tier": "physical Gazebo collision/contact physics",
            "force_contact_physics_proven": True,
            "scope": "fixture",
            "total_contact_wrench_proven": total_wrench,
            "same_run_concurrent_dual_sensor_observation": dual_sensor_observation_payload()
            if same_run_dual
            else False,
        },
    }


def write_fixture(run_dir: Path, *, total_wrench: bool, same_run_dual: bool) -> tuple[Path, Path, Path]:
    stage_path = run_dir / "stage_simulated_ft_manifest.json"
    p2_path = run_dir / "p2_contact_correlation_audit.json"
    step_path = run_dir / "step_status_rnn_audit.json"
    write_json(stage_path, stage_manifest_payload())
    write_json(p2_path, p2_payload(total_wrench=total_wrench, same_run_dual=same_run_dual))
    write_json(step_path, step_payload(total_wrench=total_wrench, same_run_dual=same_run_dual))
    return stage_path, p2_path, step_path


class DualSensorTotalWrenchAuditTest(unittest.TestCase):
    def test_default_current_artifacts_keep_full_acceptance_blockers(self) -> None:
        audit = import_audit_module()
        payload = audit.build_audit(generated_at="2026-06-21T09:55:00+08:00")

        self.assertEqual(payload["schema"], "ur10e_dual_sensor_total_wrench_audit_v1")
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertFalse(payload["total_contact_wrench_proven"])
        self.assertFalse(payload["same_run_dual_sensor_observation_proven"])
        self.assertIn("total_contact_wrench:not_proven", payload["blockers"])
        self.assertIn("same_run_dual_sensor_observation:not_proven", payload["blockers"])
        self.assertIn("required_surfaces:cross_run:", " ".join(payload["validation_issues"]))
        self.assertFalse(payload["live_authorization"]["robot_motion_authorized"])

    def test_accepts_complete_same_run_fixture(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="dual_sensor_complete_", dir=RUNS) as tmp:
            stage_path, p2_path, step_path = write_fixture(Path(tmp), total_wrench=True, same_run_dual=True)
            payload = audit.build_audit(
                generated_at="2026-06-21T09:56:00+08:00",
                stage_simulated_ft_manifest_path=stage_path,
                p2_contact_correlation_audit_path=p2_path,
                step_status_audit_path=step_path,
            )

        self.assertTrue(payload["total_contact_wrench_proven"])
        self.assertTrue(payload["same_run_dual_sensor_observation_proven"])
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["cross_run_surfaces"], [])
        observation = payload["same_run_dual_sensor_observation"][
            "same_run_concurrent_dual_sensor_observation"
        ]
        self.assertTrue(observation["same_run_concurrent_dual_sensor_observation_proven"])

    def test_blocks_legacy_boolean_same_run_dual_sensor_flag(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="dual_sensor_legacy_bool_", dir=RUNS) as tmp:
            root = Path(tmp)
            stage_path = root / "stage_simulated_ft_manifest.json"
            p2_path = root / "p2_contact_correlation_audit.json"
            step_path = root / "step_status_rnn_audit.json"
            write_json(stage_path, stage_manifest_payload())
            p2 = p2_payload(total_wrench=True, same_run_dual=False)
            p2["same_run_concurrent_dual_sensor_observation"] = True
            step = step_payload(total_wrench=True, same_run_dual=False)
            step["p2_physical_gazebo_contact"]["same_run_concurrent_dual_sensor_observation"] = True
            write_json(p2_path, p2)
            write_json(step_path, step)
            payload = audit.build_audit(
                generated_at="2026-06-21T09:56:30+08:00",
                stage_simulated_ft_manifest_path=stage_path,
                p2_contact_correlation_audit_path=p2_path,
                step_status_audit_path=step_path,
            )

        self.assertTrue(payload["total_contact_wrench_proven"])
        self.assertFalse(payload["same_run_dual_sensor_observation_proven"])
        self.assertIn("same_run_dual_sensor_observation:not_proven", payload["blockers"])
        self.assertIn(
            "same_run_concurrent_dual_sensor_observation:legacy_true_flag_only",
            payload["validation_issues"],
        )

    def test_blocks_header_only_total_wrench_rows(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="dual_sensor_header_only_", dir=RUNS) as tmp:
            root = Path(tmp)
            stage_path = root / "stage_simulated_ft_manifest.json"
            p2_path = root / "p2_contact_correlation_audit.json"
            step_path = root / "step_status_rnn_audit.json"
            write_json(stage_path, stage_manifest_payload())
            p2 = p2_payload(total_wrench=True, same_run_dual=True)
            p2["wrench_evidence"]["rows"] = [{"header": {"stamp_s": 0.1, "frame_id": "base"}}]
            write_json(p2_path, p2)
            write_json(step_path, step_payload(total_wrench=True, same_run_dual=True))
            payload = audit.build_audit(
                generated_at="2026-06-21T09:56:45+08:00",
                stage_simulated_ft_manifest_path=stage_path,
                p2_contact_correlation_audit_path=p2_path,
                step_status_audit_path=step_path,
            )

        self.assertFalse(payload["total_contact_wrench_proven"])
        self.assertFalse(payload["same_run_dual_sensor_observation_proven"])
        self.assertIn("total_contact_wrench:not_proven", payload["blockers"])
        self.assertIn("total_contact_wrench_evidence:not_proven", payload["validation_issues"])

    def test_blocks_cross_run_fixture_even_with_positive_flags(self) -> None:
        audit = import_audit_module()
        with ExitStack() as stack:
            stage_run = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="dual_sensor_stage_", dir=RUNS)))
            p2_run = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="dual_sensor_p2_", dir=RUNS)))
            step_run = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="dual_sensor_step_", dir=RUNS)))
            stage_path, _, _ = write_fixture(stage_run, total_wrench=True, same_run_dual=True)
            _, p2_path, _ = write_fixture(p2_run, total_wrench=True, same_run_dual=True)
            _, _, step_path = write_fixture(step_run, total_wrench=True, same_run_dual=True)
            payload = audit.build_audit(
                generated_at="2026-06-21T09:57:00+08:00",
                stage_simulated_ft_manifest_path=stage_path,
                p2_contact_correlation_audit_path=p2_path,
                step_status_audit_path=step_path,
            )

        self.assertFalse(payload["total_contact_wrench_proven"])
        self.assertFalse(payload["same_run_dual_sensor_observation_proven"])
        self.assertIn("total_contact_wrench:not_proven", payload["blockers"])
        self.assertIn("same_run_dual_sensor_observation:not_proven", payload["blockers"])
        self.assertIn("required_surfaces:cross_run:", " ".join(payload["validation_issues"]))

    def test_write_audit_creates_machine_readable_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="dual_sensor_write_", dir=RUNS) as tmp:
            root = Path(tmp)
            stage_path, p2_path, step_path = write_fixture(root / "source_run", total_wrench=False, same_run_dual=False)
            output_dir = root / "audit"
            path = audit.write_audit(
                output_dir,
                generated_at="2026-06-21T09:58:00+08:00",
                stage_simulated_ft_manifest_path=stage_path,
                p2_contact_correlation_audit_path=p2_path,
                step_status_audit_path=step_path,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema"], "ur10e_dual_sensor_total_wrench_audit_v1")
        self.assertFalse(payload["total_contact_wrench_proven"])
        self.assertTrue(payload["artifact_path"].endswith("dual_sensor_total_wrench_audit.json"))


if __name__ == "__main__":
    unittest.main()
