#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
MODULE_PATH = TOOLS / "build_p6_integrated_demo_readiness_audit.py"
sys.path.insert(0, str(TOOLS))

EXPECTED_TIERS = [
    "visual_only",
    "virtual/software force-loop",
    "simulated_ft",
    "physical Gazebo collision/contact physics",
    "real bench/live contact",
]
GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"


def import_audit_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing generator: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_p6_integrated_demo_readiness_audit", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _p3_payload() -> dict[str, object]:
    return {
        "schema": "ur10e_p3_visual_rviz_evidence_audit_v1",
        "goal_lineage": GOAL_LINEAGE,
        "audit_coverage": {
            "gazebo_rows": 32,
            "gazebo_observer_visual_pass_count": 32,
            "rviz_all_required_items_evidenced": True,
            "rviz_rendered_screenshot_evidence_present": True,
            "full_p3_acceptance_allowed": False,
        },
    }


def _post_gate_visual_foundation_row() -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_real_aligned_gui_matrix_row_v2",
        "stage": "step5b",
        "view": "close_detail",
        "observer_visual_pass": True,
        "observer_visual_failure_reasons": [],
        "observer_visual_gate_version": "observer_visual_gate_v3_actual_mesh_foundation",
        "observer_visual_reviewed_at": "2026-06-21T11:58:50+08:00",
        "actual_eoat_mesh_visual_present": True,
        "actual_contact_surface_mesh_visual_present": True,
        "actual_eoat_mesh_visual_uri": "package://ur10e_example_controllers/meshes/eoat/ur5e_ksm8n_ball_transfer_tool_v13_assembly.stl",
        "actual_contact_surface_mesh_uri": "package://ur10e_example_controllers/meshes/contact_surface/two_piece_surface_smooth_v11_3mm_thick.stl",
        "primitive_proxy_not_primary_visual": True,
        "primitive_proxy_not_main_visual_cue": True,
        "observer_level_demo_realism": True,
        "visual_evidence_captured": True,
        "scripted_camera_evidence_captured": True,
        "scripted_camera_final_png": "scripted_camera_final.png",
        "scripted_camera_sha256": "fixture-sha256",
        "video_path": "gui_recording.mp4",
        "marker_style": "observer_subtle",
        "force_loop_success": False,
        "force_contact_physics_proven": False,
        "git_provenance": {
            "branch": "archive/ur10e-materials-20260520-20260602",
            "commit": "92fac338689ed793c9425e8b0e6527587f85f131",
            "dirty": False,
        },
        "observer_visual_criteria": {
            "actual_eoat_mesh_visual_present": True,
            "actual_contact_surface_mesh_visual_present": True,
            "primitive_proxy_not_main_visual_cue": True,
            "observer_level_demo_realism": True,
        },
    }


def _write_post_gate_visual_foundation_row(root: Path) -> Path:
    path = root / "post_gate_visual_foundation_row.json"
    path.write_text(json.dumps(_post_gate_visual_foundation_row(), indent=2), encoding="utf-8")
    return path


def _step_payload(*, strict_ready: bool = False) -> dict[str, object]:
    contact_rows = [
        {
            "stage_id": stage_id,
            "claim_tier": "simulated_ft",
            "simulated_ft_status": "per_stage_canonical_log_evidence_attached",
            "per_stage_simulated_ft_log_evidence": True,
            "gazebo_contact_physics_status": "standalone_p2_witness_proven_not_stage_specific",
            "current_blocker": "per-stage Gazebo contact physics not proven",
        }
        for stage_id in ["step5b", "step5d", "step6b", "step7", "step8"]
    ]
    no_contact_rows = [
        {"stage_id": "step5a", "claim_tier": "visual_only"},
        {"stage_id": "step5c", "claim_tier": "visual_only"},
        {"stage_id": "step6a", "claim_tier": "visual_only"},
    ]
    return {
        "schema": "ur10e_step_status_rnn_audit_v1",
        "goal_lineage": GOAL_LINEAGE,
        "audit_coverage": {
            "stage_rows": 8,
            "stage_simulated_ft_manifest_status": "valid",
            "per_stage_simulated_ft_attached_count": 5,
            "p2_claim_tier": "physical Gazebo collision/contact physics",
            "p2_scope": "standalone_p2_witness_single_contact_point_wrench",
            "stage_specific_contact_physics_proven": False,
            "full_acceptance_allowed": False,
        },
        "p2_physical_gazebo_contact": {
            "claim_tier": "physical Gazebo collision/contact physics",
            "eoat_collision_count": 7,
            "contact_pair_log_evidence": True,
            "adapter_verified_gazebo_contact_wrench": True,
            "wrench_contact_correlation": True,
            "force_contact_physics_proven": True,
            "scope": "standalone_p2_witness_single_contact_point_wrench",
            "stage_specific_contact_physics_proven": False,
            "total_contact_wrench_proven": False,
            "same_run_concurrent_dual_sensor_observation": False,
        },
        "step_status_matrix": no_contact_rows + contact_rows,
        "rnn_interface_table": [
            {
                "interface": "inner_strict_rnn_solver",
                "status": "offline_solver_source_present" if strict_ready else "blocked_pending_pdf_truth_extraction",
                "claim_tier": "virtual/software force-loop",
            }
        ],
    }


def _artifact_row(root: Path, surface: str) -> dict[str, object]:
    path = root / f"{surface}.json"
    path.write_text(json.dumps({"surface": surface}, indent=2), encoding="utf-8")
    return {
        "surface": surface,
        "path": str(path),
        "exists": True,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _concurrent_observation() -> dict[str, object]:
    return {
        "observation_id": "fixture-concurrent-observation-001",
        "concurrent_observation_proven": True,
        "same_run_concurrent_observation_explicit": True,
        "time_window": {
            "start": "2026-06-21T07:21:00+08:00",
            "end": "2026-06-21T07:21:30+08:00",
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


def _dual_sensor_observation() -> dict[str, object]:
    return {
        "same_run_dual_sensor_observation_proven": True,
        "same_run_concurrent_dual_sensor_observation_explicit": True,
        "same_run_concurrent_dual_sensor_observation": {
            "observation_id": "fixture-dual-sensor-observation-001",
            "same_run_concurrent_dual_sensor_observation_proven": True,
            "same_run_concurrent_dual_sensor_observation_explicit": True,
            "time_window": {
                "start": "2026-06-21T07:22:00+08:00",
                "end": "2026-06-21T07:22:30+08:00",
                "clock_source": "/clock",
            },
            "surfaces": {
                "stage_simulated_ft_manifest": "stage_simulated_ft_manifest.json",
                "p2_contact_correlation_audit": "p2_contact_correlation_audit.json",
                "step_status_rnn_audit": "step_status_rnn_audit.json",
            },
        },
    }


def _write_tcp_distance_evidence(root: Path, *, supported: bool = True) -> Path:
    path = root / "tcp_distance_evidence.json"
    path.write_text(
        json.dumps(
            {
                "schema": "ur10e_p6_tcp_distance_evidence_audit_v1",
                "claim_tier": "simulated_ft" if supported else "visual_only",
                "status": "supported" if supported else "not_supported_missing_same_run_tcp_distance_time_series",
                "tcp_distance_time_series_supported": supported,
                "candidate_source_audit": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def _demo_manifest_payload(root: Path, *, tcp_distance_supported: bool = True) -> dict[str, object]:
    tcp_distance_evidence = _write_tcp_distance_evidence(root, supported=tcp_distance_supported)
    return {
        "schema": "ur10e_p6_integrated_demo_manifest_v1",
        "goal_lineage": GOAL_LINEAGE,
        "fail_closed": True,
        "claim_tier": "visual_only",
        "platform_trajectory_evidence": "platform_trajectory.json",
        "eoat_tooling_evidence": "eoat_tooling.json",
        "contact_surface_evidence": "contact_surface.json",
        "tcp_distance_evidence": str(tcp_distance_evidence),
        "simulated_ft_artifacts": ["sim_ft.json"],
        "step_rnn_pipeline_artifact": "step_status.json",
        "gazebo_gui_evidence_paths": ["gazebo.png"],
        "rviz_evidence_paths": ["rviz.png"],
        "plots": {
            name: {
                "path": f"{name}.png",
                "unit_labels": ["s", "N"],
                "frame_label": "base",
                "claim_tier": "simulated_ft",
                "supported": True,
            }
            for name in [
                "wrench_vs_time",
                "contact_state_vs_time",
                "tcp_distance_to_surface_vs_time",
                "force_threshold_crossing",
                "latency_staleness",
                "gravity_residual",
            ]
        },
    }


class P6IntegratedDemoReadinessAuditTest(unittest.TestCase):
    def test_default_current_artifacts_block_p6_without_demo_manifest(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_empty_timed_audit_fixture_") as tmp:
            payload = audit.build_audit(
                generated_at="2026-06-21T07:15:00+08:00",
                handoff_root=Path(tmp),
            )

        self.assertEqual(payload["schema"], "ur10e_p6_integrated_demo_readiness_audit_v1")
        self.assertEqual(payload["claim_boundary_gate"]["tiers"], EXPECTED_TIERS)
        self.assertFalse(payload["live_authorization"]["robot_motion_authorized"])
        self.assertFalse(payload["live_authorization"]["real_bench_live_contact_authorized"])
        self.assertIn("0910_step_status_pdf_truth_binding", payload["source_artifacts"]["step_status_rnn_audit"])
        self.assertIn("0818_p1_sim_ft_hard_floor", payload["source_artifacts"]["p1_simulated_ft_manifest"])
        self.assertIn("115619_subtle_affordance_gui", payload["source_artifacts"]["post_gate_visual_foundation_row"])
        self.assertTrue(payload["post_gate_visual_foundation"]["post_gate_visual_foundation_ready"])
        self.assertEqual(payload["step_status_rnn"]["p2_claim_tier"], "physical Gazebo collision/contact physics")
        self.assertEqual(payload["step_status_rnn"]["p2_scope"], "standalone_p2_witness_single_contact_point_wrench")
        self.assertEqual(payload["step_status_rnn"]["stage_simulated_ft_manifest_status"], "valid")
        self.assertEqual(payload["step_status_rnn"]["per_stage_simulated_ft_attached_count"], 5)
        self.assertFalse(payload["step_status_rnn"]["stage_specific_contact_physics_proven"])
        strict_gate = payload["step_status_rnn"]["strict_rnn_final_acceptance_gate"]
        self.assertEqual(strict_gate["claim_tier"], "virtual/software force-loop")
        self.assertFalse(strict_gate["strict_rnn_final_acceptance_allowed"])
        self.assertIn("paper_truth:pending_pdf_verify", strict_gate["blockers"])
        self.assertTrue(strict_gate["evidence"]["paper_truth_pdf_audit_ok"])
        self.assertEqual(strict_gate["evidence"]["paper_truth_pdf_remaining_pending_count"], 9)

        gates = payload["readiness_gates"]
        self.assertTrue(gates["p3_visual_rviz_ready"])
        self.assertTrue(gates["post_gate_visual_foundation_ready"])
        self.assertTrue(gates["stage_matrix_present"])
        self.assertTrue(gates["contact_stage_simulated_ft_ready"])
        self.assertFalse(gates["integrated_demo_manifest_valid"])
        self.assertFalse(gates["p6_integrated_demo_readiness_allowed"])
        self.assertIn("integrated_demo_manifest:not_valid", gates["p6_integrated_demo_blockers"])
        self.assertIn("step_status_full_acceptance:not_allowed", gates["p6_integrated_demo_blockers"])
        self.assertIn("strict_rnn_final_acceptance:not_proven", gates["p6_integrated_demo_blockers"])

        final_gate = payload["full_goal_acceptance_gate"]
        self.assertFalse(final_gate["full_goal_acceptance_allowed"])
        self.assertTrue(final_gate["claim_boundary_schema_valid"])
        self.assertNotIn("claim_boundary_ready_for_full_acceptance_claim", final_gate)
        self.assertIn("p6:integrated_demo_manifest:not_valid", final_gate["full_goal_acceptance_blockers"])
        self.assertIn("per_stage_physical_gazebo_contact:not_proven", final_gate["full_goal_acceptance_blockers"])
        self.assertIn("strict_rnn_final_acceptance:not_proven", final_gate["full_goal_acceptance_blockers"])
        self.assertIn("same_run_integrated_binding:not_proven", final_gate["full_goal_acceptance_blockers"])
        self.assertIn("timed_audit_coverage:not_verified", final_gate["full_goal_acceptance_blockers"])
        self.assertIn("real_bench_live_contact:not_authorized", final_gate["full_goal_acceptance_blockers"])
        self.assertFalse(payload["same_run_binding"]["same_run_integrated_demo_proven"])
        self.assertFalse(payload["timed_audit_coverage"]["full_acceptance_timed_audit_ready"])
        self.assertEqual(payload["timed_audit_coverage"]["latest_opus_record"]["status"], "missing")
        self.assertTrue(payload["claim_boundary_validation"]["claim_boundary_schema_valid"])
        self.assertFalse(payload["claim_boundary_validation"]["full_acceptance_claim_allowed"])
        self.assertNotIn("ready_for_full_acceptance_claim", payload["claim_boundary_validation"])
        self.assertEqual(len(payload["stage_status_matrix"]), 8)

    def test_fixture_with_valid_manifest_still_requires_upstream_step_acceptance(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_integrated_demo_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            payload = audit.build_audit(
                generated_at="2026-06-21T07:20:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                handoff_root=root / "missing_handoffs",
            )

        self.assertTrue(payload["integrated_demo_manifest"]["valid"])
        self.assertTrue(payload["post_gate_visual_foundation"]["post_gate_visual_foundation_ready"])
        self.assertTrue(payload["readiness_gates"]["post_gate_visual_foundation_ready"])
        self.assertEqual(payload["integrated_demo_manifest"]["claim_tier"], "visual_only")
        self.assertFalse(payload["readiness_gates"]["p6_integrated_demo_readiness_allowed"])
        self.assertIn(
            "step_status_full_acceptance:not_allowed",
            payload["readiness_gates"]["p6_integrated_demo_blockers"],
        )
        self.assertFalse(payload["full_goal_acceptance_gate"]["full_goal_acceptance_allowed"])
        self.assertIn(
            "p6:step_status_full_acceptance:not_allowed",
            payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"],
        )
        self.assertIn(
            "per_stage_physical_gazebo_contact:not_proven",
            payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"],
        )
        self.assertIn("total_contact_wrench:not_proven", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])
        self.assertIn("same_run_integrated_binding:not_proven", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])
        self.assertIn("timed_audit_coverage:not_verified", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])

    def test_post_gate_visual_foundation_missing_blocks_p6_readiness(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_visual_foundation_missing_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            payload = audit.build_audit(
                generated_at="2026-06-21T07:20:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=root / "missing_visual_row.json",
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                handoff_root=root / "missing_handoffs",
            )

        self.assertFalse(payload["post_gate_visual_foundation"]["post_gate_visual_foundation_ready"])
        self.assertIn(
            "post_gate_visual_foundation:not_ready",
            payload["readiness_gates"]["p6_integrated_demo_blockers"],
        )
        self.assertIn(
            "p6:post_gate_visual_foundation:not_ready",
            payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"],
        )

    def test_post_gate_visual_foundation_failed_mesh_row_blocks_p6_readiness(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_visual_foundation_failed_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            visual_path = root / "failed_visual_row.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            failed_visual_row = _post_gate_visual_foundation_row()
            failed_visual_row["actual_eoat_mesh_visual_present"] = False
            failed_visual_row["actual_eoat_mesh_visual_uri"] = ""
            failed_visual_row["primitive_proxy_not_main_visual_cue"] = False
            failed_visual_row["observer_visual_failure_reasons"] = ["eoat primary visual is primitive proxy"]
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path.write_text(json.dumps(failed_visual_row, indent=2), encoding="utf-8")
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            payload = audit.build_audit(
                generated_at="2026-06-21T07:20:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                handoff_root=root / "missing_handoffs",
            )

        self.assertFalse(payload["post_gate_visual_foundation"]["post_gate_visual_foundation_ready"])
        self.assertIn(
            "actual_eoat_mesh_visual_present:not_true",
            payload["post_gate_visual_foundation"]["validation_issues"],
        )
        self.assertIn(
            "actual_eoat_mesh_visual_uri_mesh_like:not_true",
            payload["post_gate_visual_foundation"]["validation_issues"],
        )
        self.assertIn(
            "primitive_proxy_not_main_visual_cue:not_true",
            payload["post_gate_visual_foundation"]["validation_issues"],
        )
        self.assertIn(
            "p6:post_gate_visual_foundation:not_ready",
            payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"],
        )

    def test_timed_audit_coverage_summarizes_prompt_only_triplet(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_timed_prompt_only_fixture_") as tmp:
            root = Path(tmp)
            for lens in ["visual-observer", "geometry-frame", "report-claim"]:
                (root / f"ur10e-gazebo-hour7-{lens}-subagent-prompt-20260621-075742.md").write_text(
                    "prompt\n",
                    encoding="utf-8",
                )
            summary = audit.timed_audit_coverage_summary(root)

        self.assertFalse(summary["full_acceptance_timed_audit_ready"])
        self.assertEqual(summary["latest_opus_record"]["status"], "missing")
        self.assertEqual(summary["prompt_only_subagent_triplet_hours"], [7])
        self.assertEqual(summary["incomplete_subagent_triplet_hours"], [7])
        self.assertEqual(summary["hourly_subagent_triplets"][0]["missing_result_lenses"], [
            "geometry-frame",
            "report-claim",
            "visual-observer",
        ])

    def test_timed_audit_coverage_accepts_complete_triplet_and_opus_zero(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_timed_complete_fixture_") as tmp:
            root = Path(tmp)
            for lens in ["visual-observer", "geometry-frame", "report-claim"]:
                (root / f"ur10e-gazebo-hour7-{lens}-subagent-prompt-20260621-075742.md").write_text(
                    "prompt\n",
                    encoding="utf-8",
                )
                (root / f"ur10e-gazebo-hour7-{lens}-subagent-result-20260621-075742.md").write_text(
                    "result\n",
                    encoding="utf-8",
                )
            base = root / "ur10e-gazebo-hour7-opus-advisory-response-20260621-075742"
            base.with_suffix(".stdout.txt").write_text("advice\n", encoding="utf-8")
            base.with_suffix(".stderr.txt").write_text("", encoding="utf-8")
            base.with_suffix(".exitcode.txt").write_text("0\n", encoding="utf-8")
            summary = audit.timed_audit_coverage_summary(root)

        self.assertTrue(summary["full_acceptance_timed_audit_ready"])
        self.assertEqual(summary["latest_opus_record"]["status"], "complete")
        self.assertEqual(summary["complete_subagent_triplet_count"], 1)
        self.assertEqual(summary["incomplete_subagent_triplet_hours"], [])
        self.assertEqual(summary["missing_subagent_triplet_sequence_hours"], [])

    def test_timed_audit_coverage_blocks_numbered_hour_gaps(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_timed_gap_fixture_") as tmp:
            root = Path(tmp)
            for hour in [4, 7]:
                for lens in ["visual-observer", "geometry-frame", "report-claim"]:
                    (root / f"ur10e-gazebo-hour{hour}-{lens}-subagent-prompt-20260621-075742.md").write_text(
                        "prompt\n",
                        encoding="utf-8",
                    )
                    (root / f"ur10e-gazebo-hour{hour}-{lens}-subagent-result-20260621-075742.md").write_text(
                        "result\n",
                        encoding="utf-8",
                    )
            base = root / "ur10e-gazebo-hour7-opus-advisory-response-20260621-075742"
            base.with_suffix(".stdout.txt").write_text("advice\n", encoding="utf-8")
            base.with_suffix(".stderr.txt").write_text("", encoding="utf-8")
            base.with_suffix(".exitcode.txt").write_text("0\n", encoding="utf-8")
            summary = audit.timed_audit_coverage_summary(root)

        self.assertFalse(summary["full_acceptance_timed_audit_ready"])
        self.assertEqual(summary["missing_subagent_triplet_sequence_hours"], [5, 6])
        self.assertIn("hourly subagent triplet sequence has gaps", summary["unresolved_p0_p1_findings"])

    def test_readiness_can_bind_external_timed_audit_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_external_timed_audit_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            timed_path = root / "timed_audit_coverage_audit.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            timed_path.write_text(
                json.dumps(
                    {
                        "schema": "ur10e_timed_audit_coverage_audit_v1",
                        "goal_lineage": GOAL_LINEAGE,
                        "generated_at": "2026-06-21T02:20:00+08:00",
                        "timed_audit_coverage": {
                            "full_acceptance_timed_audit_ready": True,
                            "claim_tier": "visual_only",
                            "expected_subagent_triplet_hours": [1],
                            "expected_opus_checkpoint_hours": [0],
                            "latest_opus_record": {"status": "complete"},
                            "hourly_subagent_triplets": [],
                            "complete_subagent_triplet_count": 2,
                            "incomplete_subagent_triplet_hours": [],
                            "missing_subagent_triplet_sequence_hours": [],
                            "prompt_only_subagent_triplet_hours": [],
                            "unresolved_p0_p1_findings": [],
                            "blocker": "",
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T02:20:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                timed_audit_coverage_path=timed_path,
                handoff_root=root / "ignored_handoffs",
            )

        self.assertTrue(payload["timed_audit_coverage"]["full_acceptance_timed_audit_ready"])
        self.assertEqual(payload["source_artifacts"]["timed_audit_coverage"], str(timed_path))
        self.assertNotIn("timed_audit_coverage:not_verified", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])

    def test_readiness_rejects_stale_external_timed_audit_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_stale_external_timed_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            timed_path = root / "timed_audit_coverage_audit.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            timed_path.write_text(
                json.dumps(
                    {
                        "schema": "ur10e_timed_audit_coverage_audit_v1",
                        "goal_lineage": GOAL_LINEAGE,
                        "generated_at": "2026-06-21T01:20:00+08:00",
                        "timed_audit_coverage": {
                            "full_acceptance_timed_audit_ready": True,
                            "claim_tier": "visual_only",
                            "expected_subagent_triplet_hours": [1],
                            "expected_opus_checkpoint_hours": [0],
                            "latest_opus_record": {"status": "complete"},
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T07:20:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                timed_audit_coverage_path=timed_path,
                handoff_root=root / "ignored_handoffs",
            )

        issues = payload["timed_audit_coverage"]["external_artifact_validation_issues"]
        self.assertFalse(payload["timed_audit_coverage"]["full_acceptance_timed_audit_ready"])
        self.assertIn("timed_audit_coverage.expected_subagent_triplet_hours:stale_or_mismatch", issues)
        self.assertIn("timed_audit_coverage.expected_opus_checkpoint_hours:stale_or_mismatch", issues)
        self.assertIn("timed_audit_coverage:not_verified", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])

    def test_readiness_rejects_external_timed_audit_without_lineage(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_invalid_external_timed_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            timed_path = root / "timed_audit_coverage_audit.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            timed_path.write_text(
                json.dumps(
                    {
                        "schema": "ur10e_timed_audit_coverage_audit_v1",
                        "timed_audit_coverage": {
                            "full_acceptance_timed_audit_ready": True,
                            "claim_tier": "visual_only",
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T07:20:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                timed_audit_coverage_path=timed_path,
                handoff_root=root / "ignored_handoffs",
            )

        self.assertFalse(payload["timed_audit_coverage"]["full_acceptance_timed_audit_ready"])
        self.assertIn("goal_lineage:mismatch_or_missing", payload["timed_audit_coverage"]["external_artifact_validation_issues"])
        self.assertIn("timed_audit_coverage:not_verified", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])

    def test_readiness_can_bind_external_same_run_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_external_same_run_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            same_run_path = root / "same_run_integrated_binding_audit.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            same_run_path.write_text(
                json.dumps(
                    {
                        "schema": "ur10e_same_run_integrated_binding_audit_v1",
                        "goal_lineage": GOAL_LINEAGE,
                        "generated_at": "2026-06-21T07:21:00+08:00",
                        "same_run_integrated_demo_proven": True,
                        "binding_status": "same_run_integrated_demo_proven",
                        "target_run_id": "fixture",
                        "missing_surfaces": [],
                        "cross_run_surfaces": [],
                        "validation_issues": [],
                        "blockers": [],
                        "artifact_rows": [
                            _artifact_row(root, "p6_manifest"),
                            _artifact_row(root, "p3_visual_rviz_audit"),
                            _artifact_row(root, "stage_simulated_ft_manifest"),
                            _artifact_row(root, "step_status_rnn_audit"),
                            _artifact_row(root, "p2_contact_correlation_audit"),
                            _artifact_row(root, "tcp_distance_evidence"),
                        ],
                        "manifest_same_run_binding": {
                            "visual_rviz_simulated_ft_same_run": True,
                            "visual_rviz_physical_gazebo_contact_same_run": True,
                            "step_rnn_physical_gazebo_contact_same_run": True,
                        },
                        "concurrent_observation": _concurrent_observation(),
                        "blocker": None,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T07:21:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                same_run_binding_path=same_run_path,
                handoff_root=root / "missing_handoffs",
            )

        self.assertTrue(payload["same_run_binding"]["same_run_integrated_demo_proven"])
        self.assertEqual(payload["source_artifacts"]["same_run_integrated_binding"], str(same_run_path))
        self.assertNotIn("same_run_integrated_binding:not_proven", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])

    def test_readiness_rejects_external_same_run_artifact_without_concurrent_observation(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_missing_external_same_run_observation_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            same_run_path = root / "same_run_integrated_binding_audit.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            same_run_path.write_text(
                json.dumps(
                    {
                        "schema": "ur10e_same_run_integrated_binding_audit_v1",
                        "goal_lineage": GOAL_LINEAGE,
                        "generated_at": "2026-06-21T07:21:00+08:00",
                        "same_run_integrated_demo_proven": True,
                        "binding_status": "same_run_integrated_demo_proven",
                        "target_run_id": "fixture",
                        "missing_surfaces": [],
                        "cross_run_surfaces": [],
                        "validation_issues": [],
                        "blockers": [],
                        "artifact_rows": [
                            _artifact_row(root, "p6_manifest"),
                            _artifact_row(root, "p3_visual_rviz_audit"),
                            _artifact_row(root, "stage_simulated_ft_manifest"),
                            _artifact_row(root, "step_status_rnn_audit"),
                            _artifact_row(root, "p2_contact_correlation_audit"),
                            _artifact_row(root, "tcp_distance_evidence"),
                        ],
                        "manifest_same_run_binding": {
                            "visual_rviz_simulated_ft_same_run": True,
                            "visual_rviz_physical_gazebo_contact_same_run": True,
                            "step_rnn_physical_gazebo_contact_same_run": True,
                        },
                        "blocker": None,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T07:21:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                same_run_binding_path=same_run_path,
                handoff_root=root / "missing_handoffs",
            )

        self.assertFalse(payload["same_run_binding"]["same_run_integrated_demo_proven"])
        self.assertIn("concurrent_observation:missing", payload["same_run_binding"]["validation_issues"])
        self.assertIn("same_run_integrated_binding:not_proven", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])

    def test_readiness_rejects_external_same_run_artifact_without_lineage(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_invalid_external_same_run_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            same_run_path = root / "same_run_integrated_binding_audit.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            same_run_path.write_text(
                json.dumps(
                    {
                        "schema": "ur10e_same_run_integrated_binding_audit_v1",
                        "same_run_integrated_demo_proven": True,
                        "manifest_same_run_binding": {
                            "visual_rviz_simulated_ft_same_run": True,
                            "visual_rviz_physical_gazebo_contact_same_run": True,
                            "step_rnn_physical_gazebo_contact_same_run": True,
                        },
                        "concurrent_observation": _concurrent_observation(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T07:21:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                same_run_binding_path=same_run_path,
                handoff_root=root / "missing_handoffs",
            )

        self.assertFalse(payload["same_run_binding"]["same_run_integrated_demo_proven"])
        self.assertIn("goal_lineage:mismatch_or_missing", payload["same_run_binding"]["validation_issues"])
        self.assertIn("same_run_integrated_binding:not_proven", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])

    def test_readiness_rejects_external_same_run_artifact_with_unverified_row_hash(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_bad_external_same_run_hash_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            same_run_path = root / "same_run_integrated_binding_audit.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            rows = [
                _artifact_row(root, "p6_manifest"),
                _artifact_row(root, "p3_visual_rviz_audit"),
                _artifact_row(root, "stage_simulated_ft_manifest"),
                _artifact_row(root, "step_status_rnn_audit"),
                _artifact_row(root, "p2_contact_correlation_audit"),
                _artifact_row(root, "tcp_distance_evidence"),
            ]
            rows[0]["sha256"] = "0" * 64
            same_run_path.write_text(
                json.dumps(
                    {
                        "schema": "ur10e_same_run_integrated_binding_audit_v1",
                        "goal_lineage": GOAL_LINEAGE,
                        "generated_at": "2026-06-21T07:21:00+08:00",
                        "same_run_integrated_demo_proven": True,
                        "binding_status": "same_run_integrated_demo_proven",
                        "target_run_id": "fixture",
                        "missing_surfaces": [],
                        "cross_run_surfaces": [],
                        "validation_issues": [],
                        "blockers": [],
                        "artifact_rows": rows,
                        "manifest_same_run_binding": {
                            "visual_rviz_simulated_ft_same_run": True,
                            "visual_rviz_physical_gazebo_contact_same_run": True,
                            "step_rnn_physical_gazebo_contact_same_run": True,
                        },
                        "concurrent_observation": _concurrent_observation(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T07:21:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                same_run_binding_path=same_run_path,
                handoff_root=root / "missing_handoffs",
            )

        self.assertFalse(payload["same_run_binding"]["same_run_integrated_demo_proven"])
        self.assertIn("artifact_rows.p6_manifest.sha256:mismatch", payload["same_run_binding"]["validation_issues"])
        self.assertIn("same_run_integrated_binding:not_proven", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])

    def test_readiness_can_bind_external_dual_sensor_total_wrench_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_external_dual_sensor_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            dual_path = root / "dual_sensor_total_wrench_audit.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            dual_path.write_text(
                json.dumps(
                    {
                        "schema": "ur10e_dual_sensor_total_wrench_audit_v1",
                        "goal_lineage": GOAL_LINEAGE,
                        "generated_at": "2026-06-21T07:22:00+08:00",
                        "claim_tier": "visual_only",
                        "total_contact_wrench_proven": True,
                        "same_run_dual_sensor_observation_proven": True,
                        "missing_surfaces": [],
                        "cross_run_surfaces": [],
                        "validation_issues": [],
                        "blockers": [],
                        "artifact_rows": [
                            _artifact_row(root, "stage_simulated_ft_manifest"),
                            _artifact_row(root, "p2_contact_correlation_audit"),
                            _artifact_row(root, "step_status_rnn_audit"),
                        ],
                        "same_run_dual_sensor_observation": _dual_sensor_observation(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T07:22:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                dual_sensor_total_wrench_path=dual_path,
                handoff_root=root / "missing_handoffs",
            )

        self.assertTrue(payload["dual_sensor_total_wrench"]["total_contact_wrench_proven"])
        self.assertTrue(payload["dual_sensor_total_wrench"]["same_run_dual_sensor_observation_proven"])
        self.assertEqual(payload["source_artifacts"]["dual_sensor_total_wrench"], str(dual_path))
        self.assertNotIn("total_contact_wrench:not_proven", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])
        self.assertNotIn("same_run_dual_sensor_observation:not_proven", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])

    def test_readiness_rejects_external_dual_sensor_artifact_without_concurrent_observation(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_missing_external_dual_sensor_observation_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            dual_path = root / "dual_sensor_total_wrench_audit.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            dual_path.write_text(
                json.dumps(
                    {
                        "schema": "ur10e_dual_sensor_total_wrench_audit_v1",
                        "goal_lineage": GOAL_LINEAGE,
                        "generated_at": "2026-06-21T07:22:00+08:00",
                        "claim_tier": "visual_only",
                        "total_contact_wrench_proven": True,
                        "same_run_dual_sensor_observation_proven": True,
                        "missing_surfaces": [],
                        "cross_run_surfaces": [],
                        "validation_issues": [],
                        "blockers": [],
                        "artifact_rows": [
                            _artifact_row(root, "stage_simulated_ft_manifest"),
                            _artifact_row(root, "p2_contact_correlation_audit"),
                            _artifact_row(root, "step_status_rnn_audit"),
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T07:22:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                dual_sensor_total_wrench_path=dual_path,
                handoff_root=root / "missing_handoffs",
            )

        self.assertFalse(payload["dual_sensor_total_wrench"]["same_run_dual_sensor_observation_proven"])
        self.assertIn(
            "same_run_dual_sensor_observation:missing",
            payload["dual_sensor_total_wrench"]["validation_issues"],
        )
        self.assertIn("same_run_dual_sensor_observation:not_proven", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])

    def test_readiness_rejects_external_dual_sensor_artifact_without_internal_evidence_rows(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_invalid_external_dual_sensor_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            dual_path = root / "dual_sensor_total_wrench_audit.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(root), indent=2), encoding="utf-8")
            dual_path.write_text(
                json.dumps(
                    {
                        "schema": "ur10e_dual_sensor_total_wrench_audit_v1",
                        "goal_lineage": GOAL_LINEAGE,
                        "generated_at": "2026-06-21T07:22:00+08:00",
                        "claim_tier": "visual_only",
                        "total_contact_wrench_proven": True,
                        "same_run_dual_sensor_observation_proven": True,
                        "missing_surfaces": [],
                        "cross_run_surfaces": [],
                        "validation_issues": [],
                        "blockers": [],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T07:22:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
                dual_sensor_total_wrench_path=dual_path,
                handoff_root=root / "missing_handoffs",
            )

        self.assertFalse(payload["dual_sensor_total_wrench"]["total_contact_wrench_proven"])
        self.assertIn("artifact_rows:missing", payload["dual_sensor_total_wrench"]["validation_issues"])
        self.assertIn("total_contact_wrench:not_proven", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])
        self.assertIn("same_run_dual_sensor_observation:not_proven", payload["full_goal_acceptance_gate"]["full_goal_acceptance_blockers"])

    def test_demo_manifest_requires_all_plots_with_units_frame_and_claim_labels(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_plot_gate_fixture_") as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            manifest = _demo_manifest_payload(root)
            manifest["plots"]["gravity_residual"].pop("frame_label")
            manifest["plots"].pop("latency_staleness")
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            result = audit.validate_demo_manifest(manifest_path)

        self.assertFalse(result["valid"])
        self.assertEqual(result["claim_tier"], "visual_only")
        self.assertIn("plots.gravity_residual.frame_label:missing", result["validation_issues"])
        self.assertIn("plots.latency_staleness:missing", result["validation_issues"])

    def test_demo_manifest_rejects_unsupported_required_plot_but_allows_gravity_gap(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_unsupported_plot_fixture_") as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            manifest = _demo_manifest_payload(root, tcp_distance_supported=False)
            manifest["plots"]["tcp_distance_to_surface_vs_time"]["supported"] = False
            manifest["plots"]["tcp_distance_to_surface_vs_time"]["unsupported_reason"] = "no same-run TCP distance samples"
            manifest["plots"]["gravity_residual"]["supported"] = False
            manifest["plots"]["gravity_residual"]["unsupported_reason"] = "no gravity residual source"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            result = audit.validate_demo_manifest(manifest_path)

        self.assertFalse(result["valid"])
        self.assertIn("plots.tcp_distance_to_surface_vs_time:unsupported", result["validation_issues"])
        self.assertNotIn("plots.gravity_residual:unsupported", result["validation_issues"])
        self.assertEqual(result["plot_status"]["gravity_residual"], "unsupported")

    def test_manifest_requires_current_goal_lineage(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_lineage_gate_fixture_") as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            manifest = _demo_manifest_payload(root)
            manifest["goal_lineage"] = "/tmp/other-goal.md"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            result = audit.validate_demo_manifest(manifest_path)

        self.assertFalse(result["valid"])
        self.assertIn("goal_lineage:mismatch_or_missing", result["validation_issues"])

    def test_supported_tcp_distance_plot_requires_supporting_evidence(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_tcp_evidence_gate_fixture_") as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            manifest = _demo_manifest_payload(root, tcp_distance_supported=False)
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            result = audit.validate_demo_manifest(manifest_path)

        self.assertFalse(result["valid"])
        self.assertIn("tcp_distance_evidence:not_supporting_supported_plot", result["validation_issues"])
        self.assertEqual(result["tcp_distance_evidence"]["claim_tier"], "visual_only")

    def test_valid_manifest_keeps_declared_visual_only_tier(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_visual_tier_fixture_") as tmp:
            root = Path(tmp)
            manifest_path = root / "manifest.json"
            manifest = _demo_manifest_payload(root)
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            result = audit.validate_demo_manifest(manifest_path)

        self.assertTrue(result["valid"])
        self.assertEqual(result["claim_tier"], "visual_only")

    def test_claim_tier_table_never_upgrades_stage_specific_contact_physics(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_claim_table_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step_path.write_text(json.dumps(_step_payload(), indent=2), encoding="utf-8")
            payload = audit.build_audit(
                generated_at="2026-06-21T07:25:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
            )

        rows = {row["evidence_surface"]: row for row in payload["current_claim_tier_table"]}
        self.assertEqual(
            rows["0708 standalone P2 Gazebo contact witness"]["claim_tier"],
            "physical Gazebo collision/contact physics",
        )
        self.assertIn("EOAT collision evidence", rows["0708 standalone P2 Gazebo contact witness"]["current_status"])
        self.assertEqual(
            rows["Step5b/Step5d/Step6b/Step7/Step8 per-stage Gazebo contact"]["claim_tier"],
            "simulated_ft",
        )
        self.assertIn("standalone P2 is not stage-specific", rows["Step5b/Step5d/Step6b/Step7/Step8 per-stage Gazebo contact"]["current_status"])
        for row in payload["current_claim_tier_table"]:
            self.assertIn(row["claim_tier"], EXPECTED_TIERS)

    def test_claim_tier_table_downgrades_blocked_standalone_p2_witness(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_claim_table_blocked_p2_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step = _step_payload()
            step["audit_coverage"]["p2_claim_tier"] = "visual_only"
            step["p2_physical_gazebo_contact"].update(
                {
                    "claim_tier": "visual_only",
                    "adapter_verified_gazebo_contact_wrench": False,
                    "wrench_contact_correlation": False,
                    "force_contact_physics_proven": False,
                }
            )
            step_path.write_text(json.dumps(step, indent=2), encoding="utf-8")
            payload = audit.build_audit(
                generated_at="2026-06-21T14:58:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
            )

        rows = {row["evidence_surface"]: row for row in payload["current_claim_tier_table"]}
        p2_row = rows["0708 standalone P2 Gazebo contact witness"]
        self.assertEqual(p2_row["claim_tier"], "visual_only")
        self.assertIn("native wrench/contact correlation is not proven", p2_row["current_status"])
        self.assertNotIn("correlation all exist", p2_row["current_status"])

    def test_stage_set_must_be_exact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_stage_set_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            visual_path = _write_post_gate_visual_foundation_row(root)
            step = _step_payload()
            step["step_status_matrix"] = [row for row in step["step_status_matrix"] if row["stage_id"] != "step8"]
            step["step_status_matrix"].append({"stage_id": "step9", "claim_tier": "visual_only"})
            step_path.write_text(json.dumps(step, indent=2), encoding="utf-8")
            payload = audit.build_audit(
                generated_at="2026-06-21T07:28:00+08:00",
                p3_audit_path=p3_path,
                post_gate_visual_foundation_row_path=visual_path,
                step_status_audit_path=step_path,
            )

        self.assertFalse(payload["step_status_rnn"]["stage_set_exact"])
        self.assertEqual(payload["step_status_rnn"]["missing_stage_ids"], ["step8"])
        self.assertEqual(payload["step_status_rnn"]["extra_stage_ids"], ["step9"])
        self.assertIn("step_status_matrix:stage_set_not_exact", payload["readiness_gates"]["p6_integrated_demo_blockers"])

    def test_write_audit_creates_machine_readable_json_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_integrated_demo_audit_test_") as tmp:
            path = audit.write_audit(Path(tmp), generated_at="2026-06-21T07:30:00+08:00")
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(path.name, "p6_integrated_demo_readiness_audit.json")
        self.assertEqual(payload["artifact_path"], str(path))
        self.assertEqual(payload["schema"], "ur10e_p6_integrated_demo_readiness_audit_v1")


if __name__ == "__main__":
    unittest.main()
