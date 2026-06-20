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
MODULE_PATH = TOOLS / "build_p3_visual_rviz_evidence_audit.py"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PACKAGE))

EXPECTED_TIERS = [
    "visual_only",
    "virtual/software force-loop",
    "simulated_ft",
    "physical Gazebo collision/contact physics",
    "real bench/live contact",
]


def import_audit_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing generator: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_p3_visual_rviz_evidence_audit", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class P3VisualRvizEvidenceAuditTest(unittest.TestCase):
    def test_build_audit_keeps_gazebo_observer_visual_only(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="rviz_missing_evidence_") as tmp:
            payload = audit.build_audit(generated_at="2026-06-21T04:05:00+08:00", rviz_search_root=Path(tmp))

        self.assertEqual(payload["schema"], "ur10e_p3_visual_rviz_evidence_audit_v1")
        self.assertEqual(payload["claim_boundary_gate"]["tiers"], EXPECTED_TIERS)
        self.assertFalse(payload["live_authorization"]["robot_motion_authorized"])
        self.assertFalse(payload["live_authorization"]["real_bench_live_contact_authorized"])

        gazebo = payload["gazebo_observer_evidence"]
        self.assertEqual(gazebo["claim_tier"], "visual_only")
        self.assertEqual(gazebo["row_count"], 24)
        self.assertEqual(gazebo["observer_visual_pass_count"], 24)
        self.assertEqual(gazebo["observer_visual_fail_count"], 0)
        self.assertEqual(gazebo["reviewed_row_count"], 24)
        self.assertEqual(gazebo["stages"], ["step5a", "step5b", "step5c", "step5d", "step6a", "step6b", "step7", "step8"])
        self.assertEqual(gazebo["views"], ["close_detail", "context_overview", "interaction_view"])
        self.assertEqual(gazebo["force_contact_source"], "gazebo_joint_state_fk_virtual_surface_model")
        self.assertFalse(gazebo["force_contact_physics_proven"])
        self.assertIn("force_contact_physics_proven=false", gazebo["blocker_tokens"])
        self.assertEqual(gazebo["source_boundary"], "visual observer evidence only; not Gazebo contact physics")

        required = payload["p3_requirement_status"]
        self.assertEqual(required["gazebo_gui_observer"]["status"], "visual_observer_pass_with_claim_boundary")
        self.assertEqual(required["gazebo_gui_observer"]["claim_tier"], "visual_only")
        self.assertEqual(required["gazebo_gui_observer"]["explicit_side_view_status"], "missing_explicit_side_view_label")
        self.assertEqual(required["rviz_debug_evidence"]["status"], "blocked_missing_current_rviz_evidence")
        self.assertEqual(required["rviz_debug_evidence"]["claim_tier"], "visual_only")
        self.assertFalse(required["rviz_debug_evidence"]["all_required_items_evidenced"])

    def test_current_claim_table_has_exact_tiers_and_no_physical_upgrade(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="rviz_missing_evidence_") as tmp:
            payload = audit.build_audit(generated_at="2026-06-21T04:05:00+08:00", rviz_search_root=Path(tmp))

        rows = payload["current_claim_tier_table"]
        self.assertGreaterEqual(len(rows), 4)
        for row in rows:
            self.assertIn(row["claim_tier"], EXPECTED_TIERS)
            self.assertNotIn("blocked/not_proven", row["claim_tier"])

        gazebo_row = next(row for row in rows if row["evidence_surface"] == "Gazebo observer matrix")
        self.assertEqual(gazebo_row["claim_tier"], "visual_only")
        self.assertIn("24/24", gazebo_row["current_status"])

        rviz_row = next(row for row in rows if row["evidence_surface"] == "RViz debug evidence")
        self.assertEqual(rviz_row["claim_tier"], "visual_only")
        self.assertIn("missing", rviz_row["current_status"])

        physical_row = next(row for row in rows if row["evidence_surface"] == "P2 physical Gazebo contact")
        self.assertEqual(physical_row["claim_tier"], "visual_only")
        self.assertIn("force_contact_physics_proven=false", physical_row["current_status"])

        self.assertIn("p1_simulated_ft", payload["source_artifacts"])
        p1_reference = payload["p1_simulated_ft_reference"]
        self.assertEqual(p1_reference["claim_tier"], "simulated_ft")
        self.assertEqual(p1_reference["artifact"], payload["source_artifacts"]["p1_simulated_ft"])
        self.assertTrue(all(p1_reference["evidence_fields_present"].values()))
        p1_row = next(row for row in rows if row["evidence_surface"] == "P1 simulated FT")
        self.assertEqual(p1_row["claim_tier"], "simulated_ft")
        self.assertEqual(p1_row["evidence_artifact"], payload["source_artifacts"]["p1_simulated_ft"])
        self.assertEqual(
            p1_row["required_evidence_fields"],
            ["stamp", "frame_id", "source", "status", "baseline", "log_evidence"],
        )

    def test_shallow_rviz_files_do_not_unlock_debug_acceptance(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="rviz_shallow_evidence_") as tmp:
            root = Path(tmp)
            (root / "debug.rviz").write_text("Panels: []\n", encoding="utf-8")
            (root / "rviz_view.png").write_bytes(b"not-a-real-image")
            rviz = audit.find_rviz_artifacts(root)

        self.assertEqual(rviz["claim_tier"], "visual_only")
        self.assertEqual(rviz["status"], "blocked_incomplete_rviz_evidence")
        self.assertFalse(rviz["all_required_items_evidenced"])
        self.assertTrue(rviz["config_paths"])
        self.assertTrue(rviz["screenshot_paths"])
        self.assertTrue(all(value is False for value in rviz["evidenced_items"].values()))

    def test_valid_rviz_manifest_unlocks_config_manifest_evidence_only(self) -> None:
        audit = import_audit_module()
        rviz_pack_path = TOOLS / "build_p3_rviz_debug_evidence_pack.py"
        spec = importlib.util.spec_from_file_location("build_p3_rviz_debug_evidence_pack", rviz_pack_path)
        if spec is None or spec.loader is None:
            raise AssertionError(f"cannot load spec for {rviz_pack_path}")
        rviz_pack = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rviz_pack)

        with tempfile.TemporaryDirectory(prefix="rviz_valid_evidence_") as tmp:
            root = Path(tmp)
            manifest = rviz_pack.write_pack(root, generated_at="2026-06-21T04:20:00+08:00")
            rviz = audit.find_rviz_artifacts(root)

        self.assertEqual(rviz["claim_tier"], "visual_only")
        self.assertEqual(rviz["status"], "rviz_config_manifest_evidence_present_not_rendered")
        self.assertTrue(rviz["all_required_items_evidenced"])
        self.assertFalse(rviz["rendered_screenshot_evidence_present"])
        self.assertFalse(rviz["full_rviz_render_acceptance_allowed"])
        self.assertEqual(rviz["manifest_paths"], [audit.rel(manifest)])
        self.assertTrue(all(rviz["evidenced_items"].values()))

    def test_write_audit_creates_json_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p3_visual_rviz_audit_test_") as tmp:
            path = audit.write_audit(Path(tmp), generated_at="2026-06-21T04:05:00+08:00")
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(path.name, "p3_visual_rviz_evidence_audit.json")
        self.assertEqual(payload["artifact_path"], str(path))
        self.assertEqual(payload["schema"], "ur10e_p3_visual_rviz_evidence_audit_v1")


if __name__ == "__main__":
    unittest.main()
