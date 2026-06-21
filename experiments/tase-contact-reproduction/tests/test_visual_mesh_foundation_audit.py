#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
MODULE_PATH = TOOLS / "build_visual_mesh_foundation_audit.py"


def import_module():
    spec = importlib.util.spec_from_file_location("build_visual_mesh_foundation_audit", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VisualMeshFoundationAuditTest(unittest.TestCase):
    def test_build_audit_verifies_real_mesh_sources_without_upgrading_claim_tier(self) -> None:
        module = import_module()
        payload = module.build_audit(generated_at="2026-06-21T15:30:00+08:00")

        self.assertEqual(payload["schema"], "ur10e_visual_mesh_foundation_audit_v1")
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertFalse(payload["live_robot_command_authorized"])
        self.assertFalse(payload["real_bench_live_contact_authorized"])
        self.assertEqual(payload["marker_default_style"], "minimal_tcp_dot")
        self.assertIn("not primary visual cues", payload["primitive_proxy_policy"])
        self.assertIn("post-integration visible Gazebo GUI screenshot", payload["forbidden_claim"])

        eoat = payload["meshes"]["eoat"]
        self.assertTrue(eoat["exists"])
        self.assertTrue(eoat["source_exists"])
        self.assertTrue(eoat["install_exists"])
        self.assertFalse(eoat["source_matches_repo_copy"])
        self.assertTrue(eoat["source_geometry_matches_repo_copy"])
        self.assertTrue(eoat["install_matches_repo_copy"])
        self.assertTrue(eoat["install_geometry_matches_repo_copy"])
        self.assertTrue(eoat["raw_bbox_size_matches_expected"])
        self.assertEqual(eoat["scale_to_m"], [0.001, 0.001, 0.001])
        self.assertEqual(eoat["claim_tier"], "visual_only")
        self.assertIn("physical Gazebo collision/contact physics", eoat["forbidden_claim"])

        surface = payload["meshes"]["contact_surface"]
        self.assertTrue(surface["exists"])
        self.assertTrue(surface["source_matches_repo_copy"])
        self.assertTrue(surface["source_geometry_matches_repo_copy"])
        self.assertTrue(surface["install_matches_repo_copy"])
        self.assertTrue(surface["install_geometry_matches_repo_copy"])
        self.assertTrue(surface["raw_bbox_size_matches_expected"])
        self.assertEqual(surface["scale_to_m"], [0.001, 0.001, 0.001])
        self.assertEqual(surface["claim_tier"], "visual_only")
        self.assertIn("SDF pose rotates STL", surface["axis_convention"])

    def test_write_audit_creates_json_artifact(self) -> None:
        module = import_module()
        with tempfile.TemporaryDirectory(prefix="visual_mesh_foundation_audit_test_") as tmp:
            path = module.write_audit(Path(tmp), generated_at="2026-06-21T15:31:00+08:00")
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(path.name, "visual_mesh_foundation_audit.json")
        self.assertEqual(payload["schema"], "ur10e_visual_mesh_foundation_audit_v1")


if __name__ == "__main__":
    unittest.main()
