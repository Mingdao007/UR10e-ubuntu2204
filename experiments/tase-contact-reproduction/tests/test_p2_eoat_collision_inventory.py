#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import build_p2_eoat_collision_inventory as p2_inventory  # noqa: E402


class P2EoatCollisionInventoryTest(unittest.TestCase):
    def test_inventory_schema_is_fail_closed_visual_only(self) -> None:
        artifact = p2_inventory.build_inventory(generated_at="2026-06-21T02:00:00+08:00")

        self.assertEqual(artifact["schema"], "ur10e_gazebo_p2_eoat_collision_inventory_v1")
        self.assertEqual(artifact["claim_tier"], "visual_only")
        self.assertEqual(artifact["evidence_scope"]["scope"], "collision_inventory_only")
        self.assertTrue(artifact["evidence_scope"]["collision_inventory_evaluated"])
        self.assertFalse(artifact["evidence_scope"]["runtime_contact_pair_log_evidence_evaluated"])
        self.assertFalse(artifact["evidence_scope"]["wrench_contact_correlation_evaluated"])
        self.assertEqual(
            artifact["evidence_scope"]["contact_pair_log_evidence_authority"],
            "build_p2_contact_correlation_audit.py",
        )
        self.assertFalse(artifact["live_robot_command_authorized"])
        self.assertFalse(artifact["bridge_start_authorized"])
        self.assertFalse(artifact["payload_tcp_safety_writes_authorized"])
        self.assertGreater(artifact["current_eoat_collision_count"], 0)
        self.assertFalse(artifact["force_contact_physics_proven"])
        self.assertIn("visual_only", artifact["allowed_claim"])
        self.assertIn("physical Gazebo collision/contact physics", artifact["forbidden_claim"])
        self.assertIn("real bench/live contact", artifact["forbidden_claim"])
        self.assertNotIn("eoat_collision_count=0", artifact["known_blockers"])
        self.assertIn("no_eoat_contact_pair_log_evidence", artifact["known_blockers"])
        self.assertIn("force_contact_physics_proven=false", artifact["known_blockers"])

        for key in (
            "generated_at",
            "eoat_parts",
            "collision_candidates",
            "inertial_provenance",
            "contact_surface_candidates",
            "known_blockers",
            "allowed_claim",
            "forbidden_claim",
        ):
            self.assertIn(key, artifact)

    def test_current_eoat_mesh_visual_is_not_physics_claim(self) -> None:
        artifact = p2_inventory.build_inventory(generated_at="2026-06-21T02:00:00+08:00")
        parts_by_id = {part["id"]: part for part in artifact["eoat_parts"]}

        mesh_part = parts_by_id[p2_inventory.gazebo.EOAT_REAL_MESH_VISUAL_NAME]
        self.assertEqual(mesh_part["source_type"], "local_stl_mesh_installed_in_urdf")
        self.assertEqual(mesh_part["unit"], "m")
        self.assertEqual(mesh_part["scale_to_m"], 0.001)
        self.assertEqual(mesh_part["claim_tier"], "visual_only")
        self.assertEqual(mesh_part["geometry"]["kind"], "mesh")
        self.assertEqual(mesh_part["geometry"]["filename"], p2_inventory.gazebo.EOAT_REAL_MESH_URI)
        self.assertEqual(mesh_part["geometry"]["scale"], [0.001, 0.001, 0.001])
        self.assertFalse(mesh_part["collision_body_instantiated"])

        summary = parts_by_id["current_eoat_visual_stack_summary"]
        self.assertEqual(summary["source_type"], "local_stl_mesh_installed_in_urdf")
        self.assertTrue(summary["collision_body_instantiated"])
        self.assertGreater(summary["collision_count"], 0)

    def test_v13_cad_candidate_records_unit_scale_axis_and_source_files(self) -> None:
        artifact = p2_inventory.build_inventory(generated_at="2026-06-21T02:00:00+08:00")
        cad_parts = [part for part in artifact["eoat_parts"] if part["source_type"] == "local_cad_archive"]
        self.assertTrue(cad_parts)

        body = next(part for part in cad_parts if part["id"] == "v13_ksm8n_receiver_body")
        self.assertEqual(body["unit"], "mm")
        self.assertEqual(body["scale_to_m"], 0.001)
        self.assertEqual(body["axis_convention"], "CAD +Z from flange face toward KSM-8N contact point")
        self.assertEqual(body["contact_point_from_flange_face_mm"], 85)
        self.assertEqual(body["approximation_status"], "local CAD candidate not yet installed into Gazebo URDF")
        self.assertTrue(Path(body["source_files"]["step"]).is_file())
        self.assertTrue(Path(body["source_files"]["stl"]).is_file())
        self.assertIn("body", body["bounding_box_mm"])

    def test_contact_surface_candidates_include_sdf_collision_and_material_provenance(self) -> None:
        artifact = p2_inventory.build_inventory(generated_at="2026-06-21T02:00:00+08:00")
        surfaces_by_id = {surface["id"]: surface for surface in artifact["contact_surface_candidates"]}

        for required in (
            "step5_contact_surface",
            "step6_contact_surface",
            "step7_large_platform_contact_surface",
            "step8_large_platform_contact_surface",
        ):
            self.assertIn(required, surfaces_by_id)
            surface = surfaces_by_id[required]
            self.assertTrue(surface["static"])
            self.assertEqual(surface["claim_tier"], "visual_only")
            self.assertTrue(surface["collision_body_instantiated"])
            self.assertEqual(surface["collision"]["geometry"]["kind"], "box")
            self.assertTrue(surface["visual_mesh"]["actual_mesh_visual_present"])
            self.assertEqual(surface["visual_mesh"]["uri"], p2_inventory.gazebo.CONTACT_SURFACE_REAL_MESH_URI)
            self.assertEqual(surface["visual_mesh"]["scale"], "0.001 0.001 0.001")
            self.assertGreater(surface["collision"]["geometry"]["size_m"][0], 0.0)
            self.assertAlmostEqual(
                surface["top_z_m"],
                surface["pose_m_rpy"][2] + 0.5 * surface["collision"]["geometry"]["size_m"][2],
            )
            self.assertEqual(surface["material"]["contact"]["kp"], 1000000.0)
            self.assertEqual(surface["material"]["contact"]["kd"], 100.0)
            self.assertEqual(surface["material"]["friction"]["mu"], 1.0)
            self.assertEqual(surface["material"]["friction"]["mu2"], 1.0)

    def test_collision_candidates_fail_closed_until_contact_pair_logs_exist(self) -> None:
        artifact = p2_inventory.build_inventory(generated_at="2026-06-21T02:00:00+08:00")
        candidates = {candidate["id"]: candidate for candidate in artifact["collision_candidates"]}

        self.assertIn("current_eoat_simplified_collision_stack", candidates)
        self.assertTrue(candidates["current_eoat_simplified_collision_stack"]["collision_body_instantiated"])
        self.assertGreater(candidates["current_eoat_simplified_collision_stack"]["collision_count"], 0)
        self.assertEqual(candidates["current_eoat_simplified_collision_stack"]["claim_tier"], "visual_only")
        self.assertIn("step5_contact_surface_collision", candidates)
        self.assertTrue(candidates["step5_contact_surface_collision"]["collision_body_instantiated"])
        self.assertEqual(candidates["step5_contact_surface_collision"]["claim_tier"], "visual_only")
        self.assertTrue(artifact["physical_gazebo_contact_gate"]["eoat_collision_body_audit_passed"])
        self.assertEqual(
            artifact["physical_gazebo_contact_gate"]["gate_scope"],
            "inventory_only_no_runtime_contact_log",
        )
        self.assertEqual(
            artifact["physical_gazebo_contact_gate"]["collision_count_proven_scope"],
            "model_inventory_only_not_runtime_contact",
        )
        self.assertEqual(
            artifact["physical_gazebo_contact_gate"]["contact_pair_log_evidence_scope"],
            "not_evaluated_by_inventory",
        )
        self.assertEqual(
            artifact["physical_gazebo_contact_gate"]["wrench_contact_correlation_scope"],
            "not_evaluated_by_inventory",
        )
        self.assertFalse(artifact["physical_gazebo_contact_gate"]["collision_count_proven"])
        self.assertFalse(artifact["physical_gazebo_contact_gate"]["contact_pair_log_evidence"])
        self.assertFalse(artifact["physical_gazebo_contact_gate"]["wrench_contact_correlation"])

    def test_write_inventory_creates_machine_readable_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2_eoat_inventory_test_") as tmp:
            path = p2_inventory.write_inventory(Path(tmp), generated_at="2026-06-21T02:00:00+08:00")
            self.assertEqual(path.name, "p2_eoat_collision_inventory.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "ur10e_gazebo_p2_eoat_collision_inventory_v1")
            self.assertEqual(payload["artifact_path"], str(path))


if __name__ == "__main__":
    unittest.main()
