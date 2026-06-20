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


def _demo_manifest_payload() -> dict[str, object]:
    return {
        "schema": "ur10e_p6_integrated_demo_manifest_v1",
        "goal_lineage": GOAL_LINEAGE,
        "fail_closed": True,
        "platform_trajectory_evidence": "platform_trajectory.json",
        "eoat_tooling_evidence": "eoat_tooling.json",
        "contact_surface_evidence": "contact_surface.json",
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
        payload = audit.build_audit(generated_at="2026-06-21T07:15:00+08:00")

        self.assertEqual(payload["schema"], "ur10e_p6_integrated_demo_readiness_audit_v1")
        self.assertEqual(payload["claim_boundary_gate"]["tiers"], EXPECTED_TIERS)
        self.assertFalse(payload["live_authorization"]["robot_motion_authorized"])
        self.assertFalse(payload["live_authorization"]["real_bench_live_contact_authorized"])

        gates = payload["readiness_gates"]
        self.assertTrue(gates["p3_visual_rviz_ready"])
        self.assertTrue(gates["stage_matrix_present"])
        self.assertTrue(gates["contact_stage_simulated_ft_ready"])
        self.assertFalse(gates["integrated_demo_manifest_valid"])
        self.assertFalse(gates["p6_integrated_demo_readiness_allowed"])
        self.assertIn("integrated_demo_manifest:not_valid", gates["p6_integrated_demo_blockers"])
        self.assertIn("step_status_full_acceptance:not_allowed", gates["p6_integrated_demo_blockers"])
        self.assertIn("strict_rnn_final_acceptance:not_proven", gates["p6_integrated_demo_blockers"])

        final_gate = payload["full_goal_acceptance_gate"]
        self.assertFalse(final_gate["full_goal_acceptance_allowed"])
        self.assertIn("p6:integrated_demo_manifest:not_valid", final_gate["full_goal_acceptance_blockers"])
        self.assertIn("per_stage_physical_gazebo_contact:not_proven", final_gate["full_goal_acceptance_blockers"])
        self.assertIn("strict_rnn_final_acceptance:not_proven", final_gate["full_goal_acceptance_blockers"])
        self.assertIn("same_run_integrated_binding:not_proven", final_gate["full_goal_acceptance_blockers"])
        self.assertIn("timed_audit_coverage:not_verified", final_gate["full_goal_acceptance_blockers"])
        self.assertIn("real_bench_live_contact:not_authorized", final_gate["full_goal_acceptance_blockers"])
        self.assertFalse(payload["same_run_binding"]["same_run_integrated_demo_proven"])
        self.assertFalse(payload["timed_audit_coverage"]["full_acceptance_timed_audit_ready"])
        self.assertEqual(len(payload["stage_status_matrix"]), 8)

    def test_fixture_with_valid_manifest_still_requires_upstream_step_acceptance(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_integrated_demo_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            manifest_path = root / "manifest.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            step_path.write_text(json.dumps(_step_payload(strict_ready=True), indent=2), encoding="utf-8")
            manifest_path.write_text(json.dumps(_demo_manifest_payload(), indent=2), encoding="utf-8")
            payload = audit.build_audit(
                generated_at="2026-06-21T07:20:00+08:00",
                p3_audit_path=p3_path,
                step_status_audit_path=step_path,
                integrated_demo_manifest_path=manifest_path,
            )

        self.assertTrue(payload["integrated_demo_manifest"]["valid"])
        self.assertEqual(payload["integrated_demo_manifest"]["claim_tier"], "simulated_ft")
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

    def test_demo_manifest_requires_all_plots_with_units_frame_and_claim_labels(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_plot_gate_fixture_") as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest = _demo_manifest_payload()
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
            manifest_path = Path(tmp) / "manifest.json"
            manifest = _demo_manifest_payload()
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
            manifest_path = Path(tmp) / "manifest.json"
            manifest = _demo_manifest_payload()
            manifest["goal_lineage"] = "/tmp/other-goal.md"
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            result = audit.validate_demo_manifest(manifest_path)

        self.assertFalse(result["valid"])
        self.assertIn("goal_lineage:mismatch_or_missing", result["validation_issues"])

    def test_claim_tier_table_never_upgrades_stage_specific_contact_physics(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_claim_table_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            step_path.write_text(json.dumps(_step_payload(), indent=2), encoding="utf-8")
            payload = audit.build_audit(
                generated_at="2026-06-21T07:25:00+08:00",
                p3_audit_path=p3_path,
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

    def test_stage_set_must_be_exact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p6_stage_set_fixture_") as tmp:
            root = Path(tmp)
            p3_path = root / "p3.json"
            step_path = root / "step.json"
            p3_path.write_text(json.dumps(_p3_payload(), indent=2), encoding="utf-8")
            step = _step_payload()
            step["step_status_matrix"] = [row for row in step["step_status_matrix"] if row["stage_id"] != "step8"]
            step["step_status_matrix"].append({"stage_id": "step9", "claim_tier": "visual_only"})
            step_path.write_text(json.dumps(step, indent=2), encoding="utf-8")
            payload = audit.build_audit(
                generated_at="2026-06-21T07:28:00+08:00",
                p3_audit_path=p3_path,
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
