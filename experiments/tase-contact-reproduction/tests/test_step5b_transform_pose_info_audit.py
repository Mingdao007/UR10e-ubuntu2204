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
MODULE_PATH = TOOLS / "build_step5b_transform_pose_info_audit.py"
sys.path.insert(0, str(TOOLS))


def import_audit_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing audit module: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_step5b_transform_pose_info_audit", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Step5bTransformPoseInfoAuditTest(unittest.TestCase):
    def test_extract_named_poses_treats_empty_position_as_identity(self) -> None:
        audit = import_audit_module()
        payload = {
            "pose": [
                {"name": "ur10e_base_frame", "id": 10, "position": {}, "orientation": {"w": 1}},
                {"name": "base_link", "id": 11, "position": {}, "orientation": {"w": 1}},
            ]
        }
        named = audit.extract_named_poses(payload, ["ur10e_base_frame", "base_link"])

        self.assertTrue(named["ur10e_base_frame"]["world_identity"])
        self.assertEqual(named["ur10e_base_frame"]["position_xyz_m"], [0.0, 0.0, 0.0])
        self.assertEqual(named["base_link"]["orientation_xyzw"], [0.0, 0.0, 0.0, 1.0])

    def test_build_audit_payload_records_independent_runtime_evidence(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="step5b_pose_info_audit_") as tmp:
            root = Path(tmp)
            world = root / "world.sdf"
            world.write_text("<sdf version='1.9'><world name='w'/></sdf>\n", encoding="utf-8")
            pose_info = root / "pose_info.json"
            pose_info.write_text("{}\n", encoding="utf-8")
            stderr = root / "stderr.log"
            stderr.write_text("", encoding="utf-8")
            payload = audit.build_audit_payload(
                generated_at="2026-06-22T12:00:00+08:00",
                run_dir=root,
                world_path=world,
                pose_info_path=pose_info,
                pose_info_payload={
                    "pose": [
                        {"name": "ur10e_base_frame", "id": 10, "position": {}, "orientation": {"w": 1}},
                        {"name": "base_link", "id": 11, "position": {}, "orientation": {"w": 1}},
                        {
                            "name": "step5b_nonformal_forced_eoat_probe",
                            "id": 12,
                            "position": {"x": -0.4, "y": -0.2, "z": 0.08},
                            "orientation": {"w": 1},
                        },
                    ]
                },
                gz_returncode=0,
                gz_stderr_path=stderr,
                server_stderr_path=stderr,
            )

        self.assertEqual(payload["schema"], "ur10e_step5b_gazebo_pose_info_transform_audit_v1")
        self.assertEqual(payload["source"], "gazebo_runtime_pose_info_topic")
        self.assertTrue(payload["independent_gazebo_runtime_transform_evidence"])
        self.assertFalse(payload["formal_step5b_transform_proof"])
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["transform"]["translation_xyz_m"], [0.0, 0.0, 0.0])
        self.assertIn("no robot motion", payload["safety_boundary"])
        self.assertIn("formal Step5b acceptance", payload["forbidden_claim"])

    def test_build_audit_payload_blocks_nonidentity_base_pose(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="step5b_pose_info_audit_bad_") as tmp:
            root = Path(tmp)
            world = root / "world.sdf"
            world.write_text("<sdf version='1.9'><world name='w'/></sdf>\n", encoding="utf-8")
            pose_info = root / "pose_info.json"
            pose_info.write_text("{}\n", encoding="utf-8")
            stderr = root / "stderr.log"
            stderr.write_text("", encoding="utf-8")
            payload = audit.build_audit_payload(
                generated_at="2026-06-22T12:00:00+08:00",
                run_dir=root,
                world_path=world,
                pose_info_path=pose_info,
                pose_info_payload={
                    "pose": [
                        {"name": "ur10e_base_frame", "id": 10, "position": {"x": 0.01}, "orientation": {"w": 1}},
                        {"name": "base_link", "id": 11, "position": {}, "orientation": {"w": 1}},
                    ]
                },
                gz_returncode=0,
                gz_stderr_path=stderr,
                server_stderr_path=stderr,
            )

        self.assertFalse(payload["independent_gazebo_runtime_transform_evidence"])
        self.assertIn("pose_info_entity_not_world_identity:ur10e_base_frame", payload["blockers"])
        self.assertEqual(payload["allowed_claim"], "visual_only blocked/not_proven")


if __name__ == "__main__":
    unittest.main()
