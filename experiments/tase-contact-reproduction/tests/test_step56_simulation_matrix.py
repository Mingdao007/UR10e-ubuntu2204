#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers import step56_simulation_matrix as sim  # noqa: E402


class Step56SimulationMatrixTest(unittest.TestCase):
    def test_registry_contains_exact_full_matrix(self) -> None:
        self.assertEqual(
            list(sim.STAGE_REGISTRY),
            ["step5a", "step5b", "step5c", "step5d", "step6a", "step6b", "step7", "step8"],
        )
        for stage_id, spec in sim.STAGE_REGISTRY.items():
            self.assertEqual(spec.stage_id, stage_id)
            self.assertEqual(spec.mode, "offline_no_motion")
            self.assertFalse(spec.live_robot_command_authorized)

    def test_stage_all_writes_eight_artifacts_and_matrix_summary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step56_matrix_test_") as tmp:
            out = Path(tmp)
            result = sim.run_matrix("all", out)
            matrix_path = out / "matrix_summary.json"
            self.assertEqual(result, matrix_path)
            payload = json.loads(matrix_path.read_text(encoding="utf-8"))

            self.assertEqual(payload["schema"], "ur10e_step56_simulation_matrix_v1")
            self.assertEqual(payload["mode"], "offline_no_motion")
            self.assertFalse(payload["live_robot_command_authorized"])
            self.assertEqual(payload["stage_count"], 8)
            self.assertEqual([stage["stage_id"] for stage in payload["stages"]], list(sim.STAGE_REGISTRY))
            for stage_id in sim.STAGE_REGISTRY:
                summary = out / stage_id / "summary.json"
                self.assertTrue(summary.is_file(), stage_id)
                artifact = json.loads(summary.read_text(encoding="utf-8"))
                self.assertEqual(artifact["stage_id"], stage_id)
                self.assertEqual(artifact["mode"], "offline_no_motion")
                self.assertFalse(artifact["live_robot_command_authorized"])
                self.assertIn("source_paths", artifact)
                self.assertIn("frames", artifact)
                self.assertIn("units", artifact)

    def test_stage_step7_8_writes_only_large_platform_artifacts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step78_matrix_test_") as tmp:
            out = Path(tmp)
            result = sim.run_matrix("step7_8", out)
            payload = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(payload["stage_count"], 2)
            self.assertEqual([stage["stage_id"] for stage in payload["stages"]], ["step7", "step8"])
            self.assertTrue((out / "step7" / "summary.json").is_file())
            self.assertTrue((out / "step8" / "summary.json").is_file())
            self.assertFalse((out / "step5a" / "summary.json").exists())

    def test_step5a_uses_textbook_affine_frame_map_not_raw_offsets(self) -> None:
        artifact = sim.build_stage_artifact("step5a")
        frame_map = artifact["safe_frame"]["frame_map"]
        self.assertEqual(frame_map["mode"], "step5a_tp_v3_affine_map")
        self.assertEqual(frame_map["alignment_label"], "preserved")
        self.assertNotEqual(
            artifact["trajectory"]["rows"][-1]["base_xy_m"],
            artifact["trajectory"]["rows"][-1]["local_xy_m"],
        )
        self.assertAlmostEqual(artifact["stage"]["velocity_cap_m_s"], 0.009, places=12)
        self.assertEqual(
            artifact["stage"]["velocity_cap_semantics"],
            "TP v3 command-vector clamp provenance, not achieved-speed truth",
        )

    def test_step5b_preserves_single_continuous_preposition(self) -> None:
        artifact = sim.build_stage_artifact("step5b")
        self.assertEqual(artifact["stage"]["source_stage_id"], "step5_contact_cycloid_baseline_v1")
        self.assertTrue(artifact["step5b_live_locked"])
        self.assertEqual(artifact["preposition"]["goal_count"], 1)
        self.assertTrue(artifact["preposition"]["legacy_repeated_short_goals_rejected"])

    def test_step6a_uses_step6_safe_frame_mapping(self) -> None:
        artifact = sim.build_stage_artifact("step6a")
        self.assertEqual(artifact["safe_frame"]["source"], str(sim.STEP6_SAFE_FRAME))
        self.assertEqual(artifact["safe_frame"]["frame_map"]["mode"], "step6_eight_safe_frame_rotation")
        self.assertEqual(artifact["safe_frame"]["waypoint_count"], 5)
        self.assertEqual(artifact["trajectory"]["shape"], "eight")
        self.assertAlmostEqual(artifact["trajectory"]["max_reference_speed_m_s"], 0.00894427190999916)

    def test_step7_uses_step4f_cycloid_paper_reference(self) -> None:
        artifact = sim.build_stage_artifact("step7")
        self.assertEqual(artifact["stage"]["source_stage_id"], "step4f_cycloid_seed_normal_v1")
        self.assertEqual(artifact["trajectory"]["shape"], "cycloid")
        self.assertEqual(artifact["safe_frame"]["frame_map"]["mode"], "step4f_safe_frame_rotation")
        self.assertTrue(artifact["stage"]["contact"])
        self.assertAlmostEqual(artifact["trajectory"]["max_reference_speed_m_s"], 0.003, places=6)
        self.assertAlmostEqual(artifact["trajectory"]["rows"][-1]["local_xy_m"][0], 0.015 * (6.0 - math.sin(6.0)), places=9)

    def test_step8_uses_step4g_eight_paper_reference(self) -> None:
        artifact = sim.build_stage_artifact("step8")
        self.assertEqual(artifact["stage"]["source_stage_id"], "step4g_eight_seed_normal_v1")
        self.assertEqual(artifact["trajectory"]["shape"], "eight")
        self.assertEqual(artifact["safe_frame"]["frame_map"]["mode"], "step4g_line_mid_basis")
        self.assertTrue(artifact["stage"]["contact"])
        self.assertAlmostEqual(
            artifact["trajectory"]["max_reference_speed_m_s"],
            math.hypot(0.004, 0.002),
            places=9,
        )

    def test_contact_stages_record_reaction_and_approach_contract(self) -> None:
        for stage_id in ["step5b", "step5d", "step6b", "step7", "step8"]:
            with self.subTest(stage_id=stage_id):
                artifact = sim.build_stage_artifact(stage_id)
                force = artifact["simulated_force_evidence"]
                self.assertEqual(force["reaction_normal"], [0.0, 0.0, 1.0])
                self.assertEqual(force["approach_normal"], [0.0, 0.0, -1.0])
                self.assertEqual(force["normal_load_definition"], "dot(force_base, reaction_normal)")
                self.assertEqual(artifact["frames"]["reaction_normal"], "base")
                self.assertEqual(artifact["frames"]["approach_normal"], "base")

    def test_step5c_and_step5d_do_not_claim_full_physics_completion(self) -> None:
        step5c = sim.build_stage_artifact("step5c")
        self.assertEqual(step5c["runner_status"], "quarantined_offline_only")
        self.assertIn("wrong XY/Z direction", step5c["known_blocker"])

        step5d = sim.build_stage_artifact("step5d")
        self.assertEqual(step5d["runner_status"], "retained_evidence_offline_shadow_only")
        self.assertIn("hold_duty_limit", step5d["known_blocker"])
        self.assertFalse(step5d["acceptance"]["physics_closed_loop_claimed"])

    def test_world_sdf_expresses_step5_and_step6_markers(self) -> None:
        tree = ET.parse(sim.WORLD_PATH)
        models = {element.attrib["name"] for element in tree.findall(".//model")}
        self.assertIn("step5_contact_surface", models)
        self.assertIn("step5_safe_frame_origin_marker", models)
        self.assertIn("step6_contact_surface", models)
        self.assertIn("step6_safe_frame_origin_marker", models)
        self.assertIn("step7_large_platform_contact_surface", models)
        self.assertIn("step7_large_platform_origin_marker", models)
        self.assertIn("step8_large_platform_contact_surface", models)
        self.assertIn("step8_large_platform_origin_marker", models)

    def test_calibrated_urdf_strips_real_driver_for_simulation(self) -> None:
        source_urdf = sim.CALIBRATED_URDF.read_text(encoding="utf-8")
        sanitized = sim.strip_ros2_control_blocks(source_urdf)
        self.assertIn("ur_robot_driver/URPositionHardwareInterface", source_urdf)
        self.assertNotIn("ur_robot_driver/URPositionHardwareInterface", sanitized)
        self.assertNotIn("ros2_control", sanitized)


if __name__ == "__main__":
    unittest.main()
