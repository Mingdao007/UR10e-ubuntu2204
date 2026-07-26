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
MODULE_PATH = TOOLS / "build_p1_simulated_ft_hard_floor_audit.py"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PACKAGE))


def import_audit_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing generator: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_p1_simulated_ft_hard_floor_audit", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class P1SimulatedFtHardFloorAuditTest(unittest.TestCase):
    def test_write_audit_creates_fail_closed_ready_artifact(self) -> None:
        audit = import_audit_module()
        with tempfile.TemporaryDirectory(prefix="p1_sim_ft_hard_floor_test_") as tmp:
            path = audit.write_audit(
                Path(tmp),
                generated_at="2026-06-21T08:30:00+08:00",
                semantic_max_rows=20,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(payload["schema"], "ur10e_p1_simulated_ft_hard_floor_audit_v1")
            self.assertEqual(payload["claim_tier"], "simulated_ft")
            self.assertTrue(payload["p1_simulated_ft_hard_floor_ready"])
            self.assertFalse(payload["p1_blockers"])
            self.assertFalse(payload["live_authorization"]["real_bench_live_contact_authorized"])
            self.assertEqual(
                payload["forbidden_claim"],
                "physical Gazebo collision/contact physics; real bench/live contact; live bridge/TP/URScript/motion",
            )

            expected_checks = {
                "contract_schema",
                "source_isolation",
                "runtime_dry_run",
                "runtime_observation",
                "no_contact_static",
                "sign_frame",
                "staleness_dropout",
                "replay_parity",
                "per_stage_pack",
                "step5d_semantic_gate",
            }
            self.assertEqual(set(payload["checks"]), expected_checks)
            for name, check in payload["checks"].items():
                with self.subTest(check=name):
                    self.assertTrue(check["pass"])
                    self.assertIsNone(check["blocker"])

            no_contact = payload["checks"]["no_contact_static"]["evidence"]
            self.assertEqual(no_contact["max_force_norm_n"], 0.0)
            self.assertEqual(no_contact["max_normal_load_n"], 0.0)
            self.assertEqual(no_contact["contact_state_values"], ["no_contact"])

            sign = payload["checks"]["sign_frame"]["evidence"]
            self.assertEqual(sign["reaction_normal"], [0.0, 0.0, 1.0])
            self.assertEqual(sign["approach_normal"], [0.0, 0.0, -1.0])
            self.assertGreater(sign["max_normal_load_n"], 0.0)
            self.assertLessEqual(sign["max_recomputed_normal_load_error_n"], 1e-9)

            runtime = payload["checks"]["runtime_dry_run"]["evidence"]
            self.assertTrue(all(runtime["evidence_fields_present"].values()))
            self.assertEqual(runtime["force_source"], "simulated_ft")
            self.assertFalse(runtime["live_authorization"]["bridge_start_authorized"])

            observed = payload["checks"]["runtime_observation"]["evidence"]
            self.assertEqual(observed["schema"], "ur10e_canonical_simulated_ft_runtime_observation_v1")
            self.assertEqual(observed["mode"], "offline_ros2_runtime_observation")
            self.assertEqual(observed["force_source"], "simulated_ft")
            self.assertTrue(observed["observed_complete"])
            self.assertTrue(observed["discovery_ready"])
            self.assertEqual(observed["first_wrench_frame_id"], "base")
            self.assertEqual(observed["first_canonical_source"], "simulated_ft")
            self.assertEqual(observed["first_canonical_status"], "valid")
            self.assertEqual(observed["first_canonical_baseline_policy"], "simulated_zero_no_contact_baseline")
            self.assertTrue(all(observed["evidence_fields_present"].values()))
            for topic_key, expected_count in observed["expected_counts"].items():
                self.assertGreaterEqual(observed["observed_counts"][topic_key], expected_count)
            observation_path = WORKSPACE / observed["observation_summary"]
            self.assertTrue(observation_path.is_file())

            pack = payload["checks"]["per_stage_pack"]["evidence"]
            pack_path = WORKSPACE / pack["manifest_path"]
            self.assertTrue(pack_path.is_file())
            self.assertTrue(pack["all_contact_stages_valid"])
            self.assertEqual(pack["claim_tier"], "simulated_ft")

            semantic = payload["checks"]["step5d_semantic_gate"]["evidence"]
            semantic_path = WORKSPACE / semantic["summary_path"]
            self.assertTrue(semantic_path.is_file())
            self.assertTrue(semantic["overall_pass"])
            self.assertTrue(semantic["static_scan_pass"])
            self.assertTrue(all(item["failure_contrast_pass"] for item in semantic["failure_contrast_passes"]))


if __name__ == "__main__":
    unittest.main()
