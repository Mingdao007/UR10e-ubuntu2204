#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import hashlib
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


def source_dual_sensor_observation_payload() -> dict[str, object]:
    return {
        "observation_id": "fixture-concurrent-observation-001",
        "explicit": True,
        "time_window": {
            "start": "2026-06-21T09:36:00+08:00",
            "end": "2026-06-21T09:36:30+08:00",
            "clock_source": "/clock",
        },
        "surfaces": {
            "stage_simulated_ft_manifest": "sim_ft.json",
            "p2_contact_correlation_audit": "p2.json",
            "step_status_rnn_audit": "step_status.json",
        },
    }


def p3_payload() -> dict[str, object]:
    return {
        "schema": "ur10e_p3_visual_rviz_evidence_audit_v1",
        "goal_lineage": GOAL_LINEAGE,
        "audit_coverage": {
            "gazebo_rows": 32,
            "gazebo_observer_visual_pass_count": 32,
            "rviz_all_required_items_evidenced": True,
            "rviz_rendered_screenshot_evidence_present": True,
        },
    }


def stage_simulated_ft_manifest_payload() -> dict[str, object]:
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
        "goal_lineage": GOAL_LINEAGE,
        "claim_tier": "simulated_ft",
        "stage_count": 5,
        "valid_stage_count": 5,
        "all_contact_stages_valid": True,
        "stages": stages,
    }


def total_wrench_row_payload() -> dict[str, object]:
    return {
        "header": {"stamp_s": 0.125, "frame_id": "base"},
        "source": "gazebo_contact",
        "status": "valid",
        "contact_state": "contact",
        "normal_load_n": 245.4,
        "baseline_policy": "gazebo_contact_zero_no_contact_baseline",
        "diagnostic_flags": [
            "gazebo_contact_wrench_adapter",
            "gazebo_contact_message_wrench",
            "total_contact_wrench",
        ],
    }


def p2_contact_payload(*, same_run_dual: bool = True, goal_lineage: str = GOAL_LINEAGE) -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_p2_contact_correlation_audit_v1",
        "goal_lineage": goal_lineage,
        "claim_tier": "physical Gazebo collision/contact physics",
        "same_run_concurrent_dual_sensor_observation": (
            source_dual_sensor_observation_payload() if same_run_dual else False
        ),
        "physical_gazebo_contact_gate": {
            "force_contact_physics_proven": True,
            "contact_pair_log_evidence": True,
            "wrench_contact_correlation": True,
        },
        "claim_boundary_gate": {
            "total_contact_wrench_proven": True,
            "real_bench_live_contact_authorized": False,
        },
        "wrench_evidence": {
            "adapter_report_schema": "ur10e_gazebo_contact_wrench_adapter_report_v1",
            "source": "gazebo_contact",
            "trace_written": True,
            "verified_native_wrench_row_count": 1,
            "total_contact_wrench_row_count": 1,
            "adapter_report_blockers": [],
            "total_contact_wrench_blockers": [],
            "wrench_aggregation_policy": "total_contact_wrench",
            "total_contact_wrench_proven": True,
            "rows": [total_wrench_row_payload()],
        },
    }


def step_status_payload(*, same_run_dual: bool = True) -> dict[str, object]:
    rows = [{"stage_id": stage_id, "claim_tier": "visual_only"} for stage_id in ["step5a", "step5c", "step6a"]]
    rows.extend(
        {
            "stage_id": stage_id,
            "claim_tier": "simulated_ft",
            "per_stage_simulated_ft_log_evidence": True,
        }
        for stage_id in ["step5b", "step5d", "step6b", "step7", "step8"]
    )
    return {
        "schema": "ur10e_step_status_rnn_audit_v1",
        "goal_lineage": GOAL_LINEAGE,
        "p2_physical_gazebo_contact": {
            "claim_tier": "physical Gazebo collision/contact physics",
            "eoat_collision_count": 7,
            "contact_pair_log_evidence": True,
            "adapter_verified_gazebo_contact_wrench": True,
            "wrench_contact_correlation": True,
            "force_contact_physics_proven": True,
            "total_contact_wrench_proven": True,
            "same_run_concurrent_dual_sensor_observation": (
                source_dual_sensor_observation_payload() if same_run_dual else False
            ),
        },
        "step_status_matrix": rows,
    }


def tcp_distance_payload(*, row_count: int = 3) -> dict[str, object]:
    return {
        "schema": "ur10e_p6_tcp_distance_evidence_audit_v1",
        "claim_tier": "simulated_ft",
        "status": "supported",
        "tcp_distance_time_series_supported": True,
        "planned_tcp_distance_row_count": row_count,
    }


def plot_payload(name: str) -> dict[str, object]:
    return {
        "path": f"plots/{name}.svg",
        "unit_labels": ["s", "N"],
        "frame_label": "base",
        "claim_tier": "simulated_ft",
        "supported": True,
    }


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_same_run_fixture(
    run_dir: Path,
    *,
    all_same_run: bool,
    external_run_dir: Path | None = None,
    include_concurrent_observation: bool = True,
    hollow_sources: bool = False,
    source_same_run_dual: bool = True,
    p2_goal_lineage: str = GOAL_LINEAGE,
    include_manifest_content: bool = True,
    include_source_hashes: bool = True,
    tcp_distance_row_count: int = 3,
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
    payloads: dict[str, dict[str, object]] = {
        "p3_visual_rviz_audit": p3_payload(),
        "stage_simulated_ft_manifest": stage_simulated_ft_manifest_payload(),
        "step_status_rnn_audit": step_status_payload(same_run_dual=source_same_run_dual),
        "p2_contact_correlation_audit": p2_contact_payload(
            same_run_dual=source_same_run_dual,
            goal_lineage=p2_goal_lineage,
        ),
        "tcp_distance_evidence": tcp_distance_payload(row_count=tcp_distance_row_count),
    }
    for key, filename in surfaces.items():
        target_dir = same_run_dir if all_same_run or key == "tcp_distance_evidence" else other_run_dir
        path = target_dir / "evidence" / filename
        write_json(path, {"schema": key, "goal_lineage": GOAL_LINEAGE} if hollow_sources else payloads[key])
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
    if include_manifest_content:
        payload.update(
            {
                "fail_closed": True,
                "claim_tier": "visual_only",
                "platform_trajectory_evidence": "platform_trajectory.json",
                "eoat_tooling_evidence": "eoat_tooling.json",
                "contact_surface_evidence": "contact_surface.json",
                "tcp_distance_evidence": source_artifacts["tcp_distance_evidence"],
                "simulated_ft_artifacts": ["sim_ft.json"],
                "step_rnn_pipeline_artifact": source_artifacts["step_status_rnn_audit"],
                "gazebo_gui_evidence_paths": ["gazebo.png"],
                "rviz_evidence_paths": ["rviz.png"],
                "plots": {name: plot_payload(name) for name in [
                    "wrench_vs_time",
                    "contact_state_vs_time",
                    "tcp_distance_to_surface_vs_time",
                    "force_threshold_crossing",
                    "latency_staleness",
                    "gravity_residual",
                ]},
            }
        )
    if include_source_hashes:
        payload["source_artifact_sha256"] = {
            key: sha256_file(Path(value))
            for key, value in source_artifacts.items()
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
        self.assertTrue(payload["source_content_validation"]["source_content_proven"])

    def test_blocks_same_run_fixture_with_hollow_source_json(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="same_run_hollow_sources_", dir=RUNS) as tmp:
            manifest_path = write_same_run_fixture(Path(tmp), all_same_run=True, hollow_sources=True)
            payload = audit.build_audit(
                generated_at="2026-06-21T09:36:10+08:00",
                integrated_demo_manifest_path=manifest_path,
            )

        self.assertFalse(payload["same_run_integrated_demo_proven"])
        self.assertFalse(payload["source_content_validation"]["source_content_proven"])
        self.assertIn("artifact_content.p3_visual_rviz_audit.schema:unsupported_or_missing", payload["validation_issues"])
        self.assertIn("artifact_content.stage_simulated_ft_manifest:not_all_valid", payload["validation_issues"])

    def test_blocks_same_run_fixture_when_source_dual_observation_is_self_reported_only(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="same_run_missing_source_dual_", dir=RUNS) as tmp:
            manifest_path = write_same_run_fixture(
                Path(tmp),
                all_same_run=True,
                source_same_run_dual=False,
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T09:36:15+08:00",
                integrated_demo_manifest_path=manifest_path,
            )

        self.assertFalse(payload["same_run_integrated_demo_proven"])
        self.assertIn(
            "artifact_content.same_run_concurrent_dual_sensor_observation:not_proven",
            payload["validation_issues"],
        )
        self.assertIn(
            "artifact_content.same_run_concurrent_dual_sensor_observation:detail_missing",
            payload["validation_issues"],
        )

    def test_blocks_same_run_fixture_when_source_observation_surface_names_do_not_match_artifact_rows(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="same_run_surface_mismatch_", dir=RUNS) as tmp:
            manifest_path = write_same_run_fixture(Path(tmp), all_same_run=True)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            p2_path = Path(manifest["source_artifacts"]["p2_contact_correlation_audit"])
            p2_payload = json.loads(p2_path.read_text(encoding="utf-8"))
            p2_payload["same_run_concurrent_dual_sensor_observation"]["surfaces"][
                "stage_simulated_ft_manifest"
            ] = "wrong_sim_ft.json"
            write_json(p2_path, p2_payload)
            manifest["source_artifact_sha256"]["p2_contact_correlation_audit"] = sha256_file(p2_path)
            write_json(manifest_path, manifest)
            payload = audit.build_audit(
                generated_at="2026-06-21T09:36:18+08:00",
                integrated_demo_manifest_path=manifest_path,
            )

        self.assertFalse(payload["same_run_integrated_demo_proven"])
        self.assertIn(
            "artifact_content.same_run_concurrent_dual_sensor_observation.surfaces.stage_simulated_ft_manifest:artifact_row_mismatch",
            payload["validation_issues"],
        )

    def test_blocks_same_run_fixture_when_p2_source_lineage_is_wrong(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="same_run_wrong_p2_lineage_", dir=RUNS) as tmp:
            manifest_path = write_same_run_fixture(
                Path(tmp),
                all_same_run=True,
                p2_goal_lineage="/tmp/stale-or-wrong-lineage.md",
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T09:36:20+08:00",
                integrated_demo_manifest_path=manifest_path,
            )

        self.assertFalse(payload["same_run_integrated_demo_proven"])
        self.assertIn(
            "artifact_content.p2_contact_correlation_audit.goal_lineage:mismatch_or_missing",
            payload["validation_issues"],
        )

    def test_blocks_same_run_fixture_with_incomplete_p6_manifest_content(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="same_run_incomplete_manifest_", dir=RUNS) as tmp:
            manifest_path = write_same_run_fixture(
                Path(tmp),
                all_same_run=True,
                include_manifest_content=False,
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T09:36:25+08:00",
                integrated_demo_manifest_path=manifest_path,
            )

        self.assertFalse(payload["same_run_integrated_demo_proven"])
        self.assertIn("integrated_demo_manifest.fail_closed:not_true", payload["validation_issues"])
        self.assertIn("integrated_demo_manifest.plots:missing", payload["validation_issues"])

    def test_blocks_same_run_fixture_with_stale_source_artifact_sha(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="same_run_stale_hash_", dir=RUNS) as tmp:
            manifest_path = write_same_run_fixture(Path(tmp), all_same_run=True)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source_artifact_sha256"]["p2_contact_correlation_audit"] = "0" * 64
            write_json(manifest_path, manifest)
            payload = audit.build_audit(
                generated_at="2026-06-21T09:36:30+08:00",
                integrated_demo_manifest_path=manifest_path,
            )

        self.assertFalse(payload["same_run_integrated_demo_proven"])
        self.assertFalse(payload["source_content_validation"]["source_content_proven"])
        self.assertIn("source_artifact_sha256.p2_contact_correlation_audit:mismatch", payload["validation_issues"])

    def test_blocks_same_run_fixture_with_empty_tcp_distance_time_series(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="same_run_empty_tcp_distance_", dir=RUNS) as tmp:
            manifest_path = write_same_run_fixture(
                Path(tmp),
                all_same_run=True,
                tcp_distance_row_count=0,
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T09:36:35+08:00",
                integrated_demo_manifest_path=manifest_path,
            )

        self.assertFalse(payload["same_run_integrated_demo_proven"])
        self.assertIn("artifact_content.tcp_distance_evidence:not_supported", payload["validation_issues"])

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
