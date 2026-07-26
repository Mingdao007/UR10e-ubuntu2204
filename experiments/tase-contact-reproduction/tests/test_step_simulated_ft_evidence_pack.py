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
MODULE_PATH = TOOLS / "build_step_simulated_ft_evidence_pack.py"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PACKAGE))


def import_pack_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing generator: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("build_step_simulated_ft_evidence_pack", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StepSimulatedFtEvidencePackTest(unittest.TestCase):
    def test_write_pack_creates_valid_per_stage_logs_and_manifest(self) -> None:
        pack = import_pack_module()
        with tempfile.TemporaryDirectory(prefix="step_sim_ft_pack_test_") as tmp:
            manifest_path = pack.write_pack(Path(tmp), generated_at="2026-06-21T05:00:00+08:00")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["schema"], "ur10e_step_simulated_ft_evidence_pack_v1")
            self.assertEqual(manifest["claim_tier"], "simulated_ft")
            self.assertTrue(manifest["all_contact_stages_valid"])
            self.assertEqual(manifest["valid_stage_count"], 5)
            self.assertFalse(manifest["live_authorization"]["real_bench_live_contact_authorized"])
            self.assertEqual(
                manifest["contact_stage_ids"],
                ["step5b", "step5d", "step6b", "step7", "step8"],
            )

            for stage_id, summary in manifest["stages"].items():
                with self.subTest(stage_id=stage_id):
                    log_path = WORKSPACE / summary["log_path"]
                    payload = json.loads(log_path.read_text(encoding="utf-8"))
                    self.assertEqual(payload["schema"], "ur10e_stage_canonical_simulated_ft_log_v1")
                    self.assertEqual(payload["stage_id"], stage_id)
                    self.assertTrue(all(payload["evidence_fields_present"].values()))
                    self.assertEqual(payload["trace"]["force_source"], "simulated_ft")
                    self.assertGreater(payload["trace"]["sample_count"], 0)
                    first = payload["trace"]["rows"][0]
                    self.assertIn("stamp_s", first["header"])
                    self.assertEqual(first["header"]["frame_id"], "base")
                    self.assertEqual(first["source"], "simulated_ft")
                    self.assertEqual(first["status"], "valid")
                    self.assertEqual(first["baseline_policy"], "simulated_zero_no_contact_baseline")
                    self.assertTrue(payload["freshness"]["freshness_ok"])
                    self.assertEqual(payload["claim_tier"], "simulated_ft")
                    self.assertEqual(payload["trace"]["claim_tier"], "simulated_ft")
                    self.assertFalse(payload["trace_validation_issues"])
                    self.assertTrue(summary["valid"])
                    self.assertTrue(payload["contact_semantics"]["has_contact_state"])
                    self.assertTrue(payload["contact_semantics"]["has_nonzero_load"])


if __name__ == "__main__":
    unittest.main()
