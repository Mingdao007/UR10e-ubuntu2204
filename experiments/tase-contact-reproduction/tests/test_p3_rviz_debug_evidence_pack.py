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
MODULE_PATH = TOOLS / "build_p3_rviz_debug_evidence_pack.py"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PACKAGE))

REQUIRED_ITEMS = [
    "TF tree",
    "robot model",
    "EOAT/tool frames",
    "TCP/contact_tip/contact_surface frames",
    "wrench/contact vectors or markers",
    "trajectory/path markers",
    "frame and claim-tier labels",
]


def import_pack_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing generator: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_p3_rviz_debug_evidence_pack", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class P3RvizDebugEvidencePackTest(unittest.TestCase):
    def test_write_pack_creates_config_and_manifest_with_required_items(self) -> None:
        pack = import_pack_module()
        with tempfile.TemporaryDirectory(prefix="p3_rviz_pack_") as tmp:
            manifest_path = pack.write_pack(Path(tmp), generated_at="2026-06-21T04:20:00+08:00")
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            rviz_config = Path(tmp) / "ur10e_p3_debug.rviz"
            config_text = rviz_config.read_text(encoding="utf-8")

        self.assertEqual(payload["schema"], "ur10e_p3_rviz_debug_evidence_pack_v1")
        self.assertEqual(payload["claim_tier"], "visual_only")
        self.assertEqual(payload["evidence_mode"], "rviz_config_manifest_only_not_rendered")
        self.assertTrue(payload["all_required_items_evidenced"])
        self.assertFalse(payload["rendered_screenshot_evidence"]["present"])
        self.assertFalse(payload["full_rviz_render_acceptance_allowed"])
        self.assertEqual(payload["rviz_config_path"], "ur10e_p3_debug.rviz")
        self.assertEqual(payload["required_items"], REQUIRED_ITEMS)
        self.assertEqual(set(payload["evidenced_items"]), set(REQUIRED_ITEMS))
        self.assertTrue(all(row["evidenced"] for row in payload["evidenced_items"].values()))

        for frame in ["base", "tool0", "flange", "ft_sensor", "tcp", "contact_tip", "contact_surface", "surface_normal"]:
            self.assertIn(frame, payload["frames"])
            self.assertIn(frame, config_text)

        for topic in [
            "/tf",
            "/tf_static",
            "/joint_states",
            "/ur10e/contact/canonical_wrench",
            "/ur10e/contact/simulated_ft/wrench",
            "/ur10e/rviz/wrench_vector",
            "/ur10e/rviz/trajectory_path",
            "/ur10e/rviz/claim_tier_labels",
        ]:
            self.assertIn(topic, payload["topics"])
            self.assertIn(topic, config_text)

        self.assertEqual(
            payload["forbidden_claim"],
            "physical Gazebo collision/contact physics; simulated_ft; real bench/live contact",
        )

    def test_manifest_fails_closed_if_required_item_is_missing(self) -> None:
        pack = import_pack_module()
        payload = pack.build_manifest(generated_at="2026-06-21T04:20:00+08:00")
        payload["evidenced_items"]["TF tree"]["evidenced"] = False

        missing = pack.missing_required_items(payload)
        self.assertEqual(missing, ["TF tree"])


if __name__ == "__main__":
    unittest.main()
