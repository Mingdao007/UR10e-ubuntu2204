#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
TOOLS = ROOT / "tools"
PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
MODULE_PATH = TOOLS / "build_step_status_rnn_audit.py"
PACK_MODULE_PATH = TOOLS / "build_step_simulated_ft_evidence_pack.py"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PACKAGE))

EXPECTED_TIERS = [
    "visual_only",
    "virtual/software force-loop",
    "simulated_ft",
    "physical Gazebo collision/contact physics",
    "real bench/live contact",
]

EXPECTED_STAGE_COLUMNS = {
    "stage_id",
    "path_shape",
    "source_spec",
    "safe_frame",
    "contact_or_no_contact",
    "control_route",
    "inner_rnn_status",
    "outer_loop_status",
    "simulated_ft_status",
    "gazebo_contact_physics_status",
    "evidence_artifact",
    "current_blocker",
    "allowed_claim",
    "forbidden_claim",
    "claim_tier",
}


def import_audit_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing generator: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_step_status_rnn_audit", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def import_pack_module():
    if not PACK_MODULE_PATH.is_file():
        raise AssertionError(f"missing generator: {PACK_MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_step_simulated_ft_evidence_pack", PACK_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {PACK_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _p2_physical_gate_payload() -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_p2_contact_correlation_audit_v1",
        "claim_tier": "physical Gazebo collision/contact physics",
        "allowed_claim": "physical Gazebo collision/contact physics with adapter-verified single contact-point wrench correlation",
        "forbidden_claim": "real bench/live contact; total contact wrench unless total_contact_wrench_proven=true",
        "known_blockers": [],
        "inputs": {
            "p2_inventory_path": "/tmp/p2_inventory.json",
            "wrench_path": "/tmp/wrench.json",
            "contact_pair_path": "/tmp/contact_pair.json",
        },
        "claim_boundary_gate": {
            "surface_eoat_cross_check_may_be_cross_run_repeatability_not_concurrent_observation": True,
            "total_contact_wrench_proven": False,
            "real_bench_live_contact_authorized": False,
        },
        "physical_gazebo_contact_gate": {
            "adapter_verified_gazebo_contact_wrench": True,
            "collision_count_proven": True,
            "contact_pair_log_evidence": True,
            "eoat_collision_body_audit_passed": True,
            "eoat_collision_count": 7,
            "force_contact_physics_proven": True,
            "status": "proven",
            "wrench_contact_correlation": True,
            "wrench_source_is_gazebo_contact": True,
        },
    }


class StepStatusRnnAuditTest(unittest.TestCase):
    def test_build_audit_labels_claim_boundaries_and_stage_matrix(self) -> None:
        audit = import_audit_module()
        payload = audit.build_audit(generated_at="2026-06-21T04:00:00+08:00")

        self.assertEqual(payload["schema"], "ur10e_step_status_rnn_audit_v1")
        self.assertEqual(payload["claim_boundary_gate"]["tiers"], EXPECTED_TIERS)
        self.assertTrue(payload["steering_note"]["acceptance_gate_changed"])
        self.assertFalse(payload["live_authorization"]["real_bench_live_contact_authorized"])
        self.assertFalse(payload["live_authorization"]["robot_motion_authorized"])

        self.assertEqual(payload["p1_simulated_ft"]["claim_tier"], "simulated_ft")
        self.assertEqual(payload["p1_simulated_ft"]["schema"], "ur10e_p1_simulated_ft_hard_floor_audit_v1")
        self.assertTrue(payload["p1_simulated_ft"]["hard_floor_ready"])
        self.assertIn("step_simulated_ft_evidence_manifest.json", payload["p1_simulated_ft"]["per_stage_simulated_ft_manifest"])
        p1_fields = payload["p1_simulated_ft"]["evidence_fields_present"]
        self.assertTrue(all(p1_fields[field] for field in ("stamp", "frame_id", "source", "status", "baseline", "log_evidence")))
        self.assertEqual(payload["stage_simulated_ft_evidence"]["status"], "valid")
        self.assertEqual(payload["audit_coverage"]["per_stage_simulated_ft_attached_count"], 5)

        p2_gate = payload["p2_physical_gazebo_contact"]
        self.assertEqual(p2_gate["claim_tier"], "physical Gazebo collision/contact physics")
        self.assertTrue(p2_gate["force_contact_physics_proven"])
        self.assertEqual(p2_gate["scope"], "standalone_p2_witness_single_contact_point_wrench")
        self.assertFalse(p2_gate["stage_specific_contact_physics_proven"])
        self.assertFalse(p2_gate["total_contact_wrench_proven"])
        self.assertIn("0708_p2_gz_sim8_physical_contact_gate", payload["source_artifacts"]["p2_contact_pair_log"])
        self.assertIn("0708_p2_gz_sim8_physical_contact_gate", payload["source_artifacts"]["p2_wrench_adapter_report"])

        rows = payload["step_status_matrix"]
        self.assertEqual(
            [row["stage_id"] for row in rows],
            ["step5a", "step5b", "step5c", "step5d", "step6a", "step6b", "step7", "step8"],
        )
        for row in rows:
            self.assertTrue(EXPECTED_STAGE_COLUMNS.issubset(row.keys()), row)
            self.assertIn(row["claim_tier"], EXPECTED_TIERS)
            self.assertIn("real bench/live contact", row["forbidden_claim"])

        by_stage = {row["stage_id"]: row for row in rows}
        for stage_id in ["step5b", "step5d", "step6b", "step7", "step8"]:
            with self.subTest(stage_id=stage_id):
                row = by_stage[stage_id]
                self.assertEqual(row["claim_tier"], "simulated_ft")
                self.assertEqual(row["simulated_ft_status"], "per_stage_canonical_log_evidence_attached")
                self.assertTrue(row["per_stage_simulated_ft_log_evidence"])
                self.assertGreater(row["per_stage_simulated_ft_sample_count"], 0)
                self.assertTrue(all(row["per_stage_simulated_ft_evidence_fields_present"].values()))
                self.assertEqual(row["gazebo_contact_physics_status"], "standalone_p2_witness_proven_not_stage_specific")
                self.assertIn("per-stage Gazebo contact physics not proven", row["current_blocker"])
                self.assertNotIn("physical Gazebo collision/contact physics", row["claim_tier"])

        self.assertEqual(by_stage["step5a"]["claim_tier"], "visual_only")
        self.assertEqual(by_stage["step6a"]["claim_tier"], "visual_only")

    def test_explicit_per_stage_simulated_ft_manifest_upgrades_only_simulated_ft_claims(self) -> None:
        audit = import_audit_module()
        pack = import_pack_module()
        with tempfile.TemporaryDirectory(prefix="step_status_sim_ft_manifest_test_") as tmp:
            manifest_path = pack.write_pack(Path(tmp), generated_at="2026-06-21T05:00:00+08:00")
            payload = audit.build_audit(
                generated_at="2026-06-21T05:00:00+08:00",
                stage_sim_ft_manifest_path=manifest_path,
            )

        self.assertEqual(payload["stage_simulated_ft_evidence"]["status"], "valid")
        self.assertEqual(payload["stage_simulated_ft_evidence"]["claim_tier"], "simulated_ft")
        self.assertFalse(payload["stage_simulated_ft_evidence"]["validation_issues"])
        self.assertEqual(payload["audit_coverage"]["per_stage_simulated_ft_attached_count"], 5)
        self.assertEqual(payload["p2_physical_gazebo_contact"]["claim_tier"], "physical Gazebo collision/contact physics")
        self.assertTrue(payload["p2_physical_gazebo_contact"]["force_contact_physics_proven"])
        self.assertFalse(payload["p2_physical_gazebo_contact"]["stage_specific_contact_physics_proven"])

        by_stage = {row["stage_id"]: row for row in payload["step_status_matrix"]}
        for stage_id in ["step5b", "step5d", "step6b", "step7", "step8"]:
            with self.subTest(stage_id=stage_id):
                row = by_stage[stage_id]
                self.assertEqual(row["claim_tier"], "simulated_ft")
                self.assertEqual(row["simulated_ft_status"], "per_stage_canonical_log_evidence_attached")
                self.assertTrue(row["per_stage_simulated_ft_log_evidence"])
                self.assertGreater(row["per_stage_simulated_ft_sample_count"], 0)
                self.assertTrue(all(row["per_stage_simulated_ft_evidence_fields_present"].values()))
                self.assertTrue(row["per_stage_simulated_ft_contact_semantics"]["has_contact_state"])
                self.assertTrue(row["per_stage_simulated_ft_contact_semantics"]["has_nonzero_load"])
                self.assertTrue(row["per_stage_simulated_ft_freshness"]["freshness_ok"])
                self.assertFalse(row["per_stage_simulated_ft_validation_issues"])
                self.assertEqual(row["gazebo_contact_physics_status"], "standalone_p2_witness_proven_not_stage_specific")
                self.assertIn("per-stage Gazebo contact physics not proven", row["current_blocker"])
                self.assertIn("not physical Gazebo", row["allowed_claim"])
                self.assertIn("real bench/live contact", row["forbidden_claim"])

        self.assertEqual(by_stage["step5a"]["claim_tier"], "visual_only")
        self.assertEqual(by_stage["step6a"]["claim_tier"], "visual_only")

    def test_p2_physical_gate_updates_global_lineage_without_stage_overclaim(self) -> None:
        audit = import_audit_module()
        pack = import_pack_module()
        with tempfile.TemporaryDirectory(prefix="step_status_p2_physical_test_") as tmp:
            tmp_path = Path(tmp)
            manifest_path = pack.write_pack(tmp_path / "sim_ft", generated_at="2026-06-21T07:30:00+08:00")
            p2_path = tmp_path / "p2_contact_correlation_audit.json"
            p2_path.write_text(json.dumps(_p2_physical_gate_payload(), indent=2), encoding="utf-8")
            payload = audit.build_audit(
                generated_at="2026-06-21T07:30:00+08:00",
                stage_sim_ft_manifest_path=manifest_path,
                p2_correlation_path=p2_path,
            )

        p2_gate = payload["p2_physical_gazebo_contact"]
        self.assertEqual(p2_gate["claim_tier"], "physical Gazebo collision/contact physics")
        self.assertTrue(p2_gate["force_contact_physics_proven"])
        self.assertEqual(p2_gate["scope"], "standalone_p2_witness_single_contact_point_wrench")
        self.assertFalse(p2_gate["stage_specific_contact_physics_proven"])
        self.assertFalse(p2_gate["total_contact_wrench_proven"])

        by_stage = {row["stage_id"]: row for row in payload["step_status_matrix"]}
        for stage_id in ["step5b", "step5d", "step6b", "step7", "step8"]:
            with self.subTest(stage_id=stage_id):
                row = by_stage[stage_id]
                self.assertEqual(row["claim_tier"], "simulated_ft")
                self.assertEqual(row["gazebo_contact_physics_status"], "standalone_p2_witness_proven_not_stage_specific")
                self.assertIn("per-stage Gazebo contact physics not proven", row["current_blocker"])
                self.assertIn("for this stage", row["forbidden_claim"])

        lineage = {row["source_name"]: row for row in payload["force_source_lineage_current_goal"]}
        self.assertEqual(lineage["gazebo_contact"]["current_report_claim_tier"], "physical Gazebo collision/contact physics")
        self.assertIn("standalone P2 witness", lineage["gazebo_contact"]["current_goal_status"])
        self.assertIn("per-stage Gazebo contact physics", lineage["gazebo_contact"]["current_goal_status"])
        self.assertEqual(payload["audit_coverage"]["p2_scope"], "standalone_p2_witness_single_contact_point_wrench")
        self.assertFalse(payload["audit_coverage"]["stage_specific_contact_physics_proven"])

    def test_rnn_interface_and_force_source_lineage_fail_closed_for_current_goal(self) -> None:
        audit = import_audit_module()
        payload = audit.build_audit(generated_at="2026-06-21T04:00:00+08:00")

        interfaces = {row["interface"]: row for row in payload["rnn_interface_table"]}
        self.assertEqual(
            list(interfaces),
            ["path_provider", "outer_loop", "inner_strict_rnn_solver", "carrier_registers_ros2_runner"],
        )
        self.assertEqual(interfaces["inner_strict_rnn_solver"]["status"], "blocked_pending_pdf_truth_extraction")
        self.assertEqual(interfaces["inner_strict_rnn_solver"]["claim_tier"], "virtual/software force-loop")
        self.assertGreater(
            interfaces["inner_strict_rnn_solver"]["evidence"]["pending_pdf_verify_count"],
            0,
        )
        self.assertIn("no live bridge", interfaces["carrier_registers_ros2_runner"]["forbidden_claim"])

        strict_gate = payload["strict_rnn_final_acceptance_gate"]
        self.assertTrue(strict_gate["fail_closed"])
        self.assertFalse(strict_gate["strict_rnn_final_acceptance_allowed"])
        self.assertEqual(strict_gate["claim_tier"], "virtual/software force-loop")
        self.assertIn("paper_truth:strict_rnn_disabled", strict_gate["blockers"])
        self.assertIn("paper_truth:pending_pdf_verify", strict_gate["blockers"])
        self.assertTrue(strict_gate["evidence"]["paper_truth_pdf_audit_ok"])
        self.assertIn(
            "finite_time_rnn_state_equation",
            strict_gate["evidence"]["paper_truth_pdf_verified_fields"],
        )
        self.assertEqual(strict_gate["evidence"]["paper_truth_pdf_remaining_pending_count"], 9)
        self.assertTrue(strict_gate["evidence"]["numeric_sanity_overall_pass"])
        self.assertEqual(strict_gate["evidence"]["numeric_sanity_contact_evidence"], "not_claimed")
        self.assertIn("simulated_ft", strict_gate["forbidden_claim"])

        lineage = {row["source_name"]: row for row in payload["force_source_lineage_current_goal"]}
        self.assertEqual(lineage["virtual/software force-loop"]["current_report_claim_tier"], "virtual/software force-loop")
        self.assertEqual(lineage["simulated_ft"]["current_report_claim_tier"], "simulated_ft")
        self.assertEqual(lineage["gazebo_contact"]["current_report_claim_tier"], "physical Gazebo collision/contact physics")
        self.assertEqual(lineage["gazebo_contact"]["target_claim_tier"], "physical Gazebo collision/contact physics")
        self.assertIn("standalone P2 witness", lineage["gazebo_contact"]["current_goal_status"])
        self.assertIn("per-stage Gazebo contact physics", lineage["gazebo_contact"]["current_goal_status"])
        self.assertEqual(lineage["real_kunwei_read_only"]["current_report_claim_tier"], "visual_only")
        self.assertEqual(lineage["real_kunwei_read_only"]["target_claim_tier"], "not authorized in current goal")
        self.assertIn("not authorized", lineage["real_kunwei_read_only"]["current_goal_status"])

    def test_write_audit_creates_machine_readable_json_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="step_status_rnn_audit_test_") as tmp:
            path = audit.write_audit(Path(tmp), generated_at="2026-06-21T04:00:00+08:00")
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(path.name, "step_status_rnn_audit.json")
        self.assertEqual(payload["artifact_path"], str(path))
        self.assertEqual(payload["schema"], "ur10e_step_status_rnn_audit_v1")


if __name__ == "__main__":
    unittest.main()
