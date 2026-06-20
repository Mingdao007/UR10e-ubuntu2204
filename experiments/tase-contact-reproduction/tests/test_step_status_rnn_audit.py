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
        p1_fields = payload["p1_simulated_ft"]["evidence_fields_present"]
        self.assertTrue(all(p1_fields[field] for field in ("stamp", "frame_id", "source", "status", "baseline", "log_evidence")))

        p2_gate = payload["p2_physical_gazebo_contact"]
        self.assertEqual(p2_gate["claim_tier"], "visual_only")
        self.assertFalse(p2_gate["force_contact_physics_proven"])
        self.assertIn("force_contact_physics_proven=false", p2_gate["blocker_tokens"])

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
                self.assertEqual(row["claim_tier"], "virtual/software force-loop")
                self.assertEqual(row["simulated_ft_status"], "not_per_stage_canonical_log_evidence")
                self.assertFalse(row["per_stage_simulated_ft_log_evidence"])
                self.assertEqual(row["gazebo_contact_physics_status"], "blocked_not_proven")
                self.assertIn("force_contact_physics_proven=false", row["current_blocker"])
                self.assertNotIn("physical Gazebo collision/contact physics", row["claim_tier"])

        self.assertEqual(by_stage["step5a"]["claim_tier"], "visual_only")
        self.assertEqual(by_stage["step6a"]["claim_tier"], "visual_only")

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
        self.assertIn("no live bridge", interfaces["carrier_registers_ros2_runner"]["forbidden_claim"])

        lineage = {row["source_name"]: row for row in payload["force_source_lineage_current_goal"]}
        self.assertEqual(lineage["virtual/software force-loop"]["current_report_claim_tier"], "virtual/software force-loop")
        self.assertEqual(lineage["simulated_ft"]["current_report_claim_tier"], "simulated_ft")
        self.assertEqual(lineage["gazebo_contact"]["current_report_claim_tier"], "visual_only")
        self.assertEqual(lineage["gazebo_contact"]["target_claim_tier"], "physical Gazebo collision/contact physics")
        self.assertEqual(lineage["real_kunwei_read_only"]["current_report_claim_tier"], "visual_only")
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
