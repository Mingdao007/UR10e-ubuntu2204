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
MODULE_PATH = TOOLS / "build_same_run_integrated_binding_audit.py"
GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"
sys.path.insert(0, str(TOOLS))


def import_audit_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing generator: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_same_run_integrated_binding_audit", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def concurrent_observation_payload() -> dict[str, object]:
    return {
        "observation_id": "fixture-concurrent-observation-001",
        "same_run_concurrent_observation_explicit": True,
        "time_window": {
            "start": "2026-06-21T09:36:00+08:00",
            "end": "2026-06-21T09:36:30+08:00",
            "clock_source": "/clock",
        },
        "surfaces": [
            "p3_visual_rviz_audit",
            "stage_simulated_ft_manifest",
            "step_status_rnn_audit",
            "p2_contact_correlation_audit",
            "tcp_distance_evidence",
        ],
    }


def write_same_run_fixture(
    run_dir: Path,
    *,
    all_same_run: bool,
    external_run_dir: Path | None = None,
    include_concurrent_observation: bool = True,
) -> Path:
    same_run_dir = run_dir
    other_run_dir = external_run_dir or run_dir.parent / f"{run_dir.name}_external"
    surfaces = {
        "p3_visual_rviz_audit": "p3.json",
        "stage_simulated_ft_manifest": "sim_ft.json",
        "step_status_rnn_audit": "step_status.json",
        "p2_contact_correlation_audit": "p2.json",
        "tcp_distance_evidence": "tcp_distance.json",
    }
    source_artifacts: dict[str, str] = {}
    for key, filename in surfaces.items():
        target_dir = same_run_dir if all_same_run or key == "tcp_distance_evidence" else other_run_dir
        path = target_dir / "evidence" / filename
        write_json(path, {"schema": key, "goal_lineage": GOAL_LINEAGE})
        source_artifacts[key] = str(path)
    manifest_path = same_run_dir / "p6_integrated_demo_manifest.json"
    payload: dict[str, object] = {
        "schema": "ur10e_p6_integrated_demo_manifest_v1",
        "goal_lineage": GOAL_LINEAGE,
        "source_artifacts": source_artifacts,
        "same_run_binding": {
            "binding_status": "same_run_integrated_demo_proven" if all_same_run else "cross_run_evidence_only",
            "visual_rviz_simulated_ft_same_run": all_same_run,
            "visual_rviz_physical_gazebo_contact_same_run": all_same_run,
            "step_rnn_physical_gazebo_contact_same_run": all_same_run,
        },
    }
    if include_concurrent_observation:
        payload["concurrent_observation"] = concurrent_observation_payload()
    write_json(manifest_path, payload)
    return manifest_path


class SameRunIntegratedBindingAuditTest(unittest.TestCase):
    def test_default_current_p6_manifest_is_cross_run_and_not_proven(self) -> None:
        audit = import_audit_module()
        payload = audit.build_audit(generated_at="2026-06-21T09:35:00+08:00")

        self.assertEqual(payload["schema"], "ur10e_same_run_integrated_binding_audit_v1")
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertFalse(payload["same_run_integrated_demo_proven"])
        self.assertEqual(payload["binding_status"], "cross_run_evidence_only")
        self.assertIn("required_source_artifacts:cross_run:", " ".join(payload["validation_issues"]))
        self.assertFalse(payload["live_authorization"]["robot_motion_authorized"])

    def test_accepts_complete_same_run_fixture(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="same_run_complete_", dir=RUNS) as tmp:
            manifest_path = write_same_run_fixture(Path(tmp), all_same_run=True)
            payload = audit.build_audit(
                generated_at="2026-06-21T09:36:00+08:00",
                integrated_demo_manifest_path=manifest_path,
            )

        self.assertTrue(payload["same_run_integrated_demo_proven"])
        self.assertEqual(payload["binding_status"], "same_run_integrated_demo_proven")
        self.assertEqual(payload["cross_run_surfaces"], [])
        self.assertEqual(payload["missing_surfaces"], [])
        self.assertTrue(payload["concurrent_observation"]["concurrent_observation_proven"])

    def test_blocks_same_run_paths_without_concurrent_observation(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="same_run_no_observation_", dir=RUNS) as tmp:
            manifest_path = write_same_run_fixture(
                Path(tmp),
                all_same_run=True,
                include_concurrent_observation=False,
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T09:36:30+08:00",
                integrated_demo_manifest_path=manifest_path,
            )

        self.assertFalse(payload["same_run_integrated_demo_proven"])
        self.assertEqual(payload["binding_status"], "same_run_paths_without_concurrent_observation")
        self.assertIn("manifest.concurrent_observation:missing", payload["validation_issues"])

    def test_blocks_cross_run_fixture(self) -> None:
        audit = import_audit_module()
        with ExitStack() as stack:
            source_run = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="same_run_cross_source_", dir=RUNS)))
            external_run = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="same_run_cross_external_", dir=RUNS)))
            manifest_path = write_same_run_fixture(source_run, all_same_run=False, external_run_dir=external_run)
            payload = audit.build_audit(
                generated_at="2026-06-21T09:37:00+08:00",
                integrated_demo_manifest_path=manifest_path,
            )

        self.assertFalse(payload["same_run_integrated_demo_proven"])
        self.assertEqual(payload["binding_status"], "cross_run_evidence_only")
        self.assertIn("p3_visual_rviz_audit", payload["cross_run_surfaces"])
        self.assertIn("manifest.same_run_binding:not_all_true", payload["validation_issues"])

    def test_write_audit_creates_machine_readable_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="same_run_write_", dir=RUNS) as tmp:
            root = Path(tmp)
            manifest_path = write_same_run_fixture(root / "source_run", all_same_run=False)
            output_dir = root / "audit"
            path = audit.write_audit(
                output_dir,
                generated_at="2026-06-21T09:38:00+08:00",
                integrated_demo_manifest_path=manifest_path,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema"], "ur10e_same_run_integrated_binding_audit_v1")
        self.assertFalse(payload["same_run_integrated_demo_proven"])
        self.assertTrue(payload["artifact_path"].endswith("same_run_integrated_binding_audit.json"))


if __name__ == "__main__":
    unittest.main()
