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
BUNDLE_MODULE_PATH = TOOLS / "build_p6_integrated_demo_bundle.py"
READINESS_MODULE_PATH = TOOLS / "build_p6_integrated_demo_readiness_audit.py"
sys.path.insert(0, str(TOOLS))


def import_module(path: Path, name: str):
    if not path.is_file():
        raise AssertionError(f"missing module: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class P6IntegratedDemoBundleTest(unittest.TestCase):
    def test_write_bundle_creates_manifest_csv_and_plots_without_full_acceptance(self) -> None:
        bundle = import_module(BUNDLE_MODULE_PATH, "build_p6_integrated_demo_bundle")
        with tempfile.TemporaryDirectory(prefix="p6_integrated_demo_bundle_test_") as tmp:
            manifest_path = bundle.write_bundle(Path(tmp), generated_at="2026-06-21T07:45:00+08:00")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], "ur10e_p6_integrated_demo_manifest_v1")
            self.assertTrue(manifest["fail_closed"])
            self.assertFalse(manifest["same_run_integrated_demo_proven"])
            self.assertEqual(manifest["claim_tier"], "visual_only")
            self.assertIn("not same-run integrated demo", manifest["current_claim_tier_table"][0]["current_status"])
            self.assertTrue(manifest["simulated_ft_artifacts"])
            self.assertTrue(manifest["gazebo_gui_evidence_paths"])
            self.assertTrue(manifest["rviz_evidence_paths"])
            self.assertIn("tcp_distance_evidence", manifest)

            combined_csv = WORKSPACE / manifest["source_artifacts"]["combined_simulated_ft_csv"]
            self.assertTrue(combined_csv.is_file())
            self.assertIn("stage_id,t_s,frame_id,source,status,claim_tier", combined_csv.read_text(encoding="utf-8").splitlines()[0])
            planned_csv = WORKSPACE / manifest["source_artifacts"]["planned_tcp_distance_csv"]
            self.assertTrue(planned_csv.is_file())
            self.assertIn("distance_to_surface_m", planned_csv.read_text(encoding="utf-8").splitlines()[0])
            tcp_distance_evidence = WORKSPACE / manifest["source_artifacts"]["tcp_distance_evidence"]
            self.assertTrue(tcp_distance_evidence.is_file())
            tcp_payload = json.loads(tcp_distance_evidence.read_text(encoding="utf-8"))
            self.assertEqual(tcp_payload["schema"], "ur10e_p6_tcp_distance_evidence_audit_v1")
            self.assertTrue(tcp_payload["tcp_distance_time_series_supported"])
            self.assertEqual(tcp_payload["claim_tier"], "visual_only")
            self.assertIn("planned path geometry", tcp_payload["candidate_source_audit"][0]["reason"])
            self.assertIn("planned path geometry", tcp_payload["downgrade_rule"])

            plots = manifest["plots"]
            for plot_name in ["wrench_vs_time", "contact_state_vs_time", "force_threshold_crossing", "latency_staleness"]:
                with self.subTest(plot_name=plot_name):
                    plot = plots[plot_name]
                    self.assertTrue(plot["supported"])
                    self.assertEqual(plot["claim_tier"], "simulated_ft")
                    self.assertEqual(plot["frame_label"], "base")
                    self.assertTrue((WORKSPACE / plot["path"]).is_file())

            tcp_plot = plots["tcp_distance_to_surface_vs_time"]
            self.assertTrue(tcp_plot["supported"])
            self.assertEqual(tcp_plot["claim_tier"], "visual_only")
            self.assertTrue((WORKSPACE / tcp_plot["path"]).is_file())

            gravity_plot = plots["gravity_residual"]
            self.assertFalse(gravity_plot["supported"])
            self.assertIn("no gravity residual source", gravity_plot["unsupported_reason"])

    def test_bundle_manifest_is_present_but_readiness_gate_stays_fail_closed(self) -> None:
        bundle = import_module(BUNDLE_MODULE_PATH, "build_p6_integrated_demo_bundle")
        readiness = import_module(READINESS_MODULE_PATH, "build_p6_integrated_demo_readiness_audit")
        with tempfile.TemporaryDirectory(prefix="p6_integrated_demo_bundle_readiness_test_") as tmp:
            manifest_path = bundle.write_bundle(Path(tmp), generated_at="2026-06-21T07:45:00+08:00")
            payload = readiness.build_audit(
                generated_at="2026-06-21T07:46:00+08:00",
                integrated_demo_manifest_path=manifest_path,
            )

        manifest_summary = payload["integrated_demo_manifest"]
        self.assertEqual(manifest_summary["manifest_path"], readiness.rel(manifest_path))
        self.assertTrue(manifest_summary["valid"])
        self.assertEqual(manifest_summary["validation_issues"], [])
        self.assertNotIn("tcp_distance_evidence:missing_or_unreadable", manifest_summary["validation_issues"])
        self.assertTrue(manifest_summary["tcp_distance_evidence"]["tcp_distance_time_series_supported"])
        self.assertEqual(manifest_summary["plot_status"]["gravity_residual"], "unsupported")
        self.assertFalse(payload["readiness_gates"]["p6_integrated_demo_readiness_allowed"])
        self.assertNotIn("integrated_demo_manifest:not_valid", payload["readiness_gates"]["p6_integrated_demo_blockers"])
        self.assertIn("strict_rnn_final_acceptance:not_proven", payload["readiness_gates"]["p6_integrated_demo_blockers"])


if __name__ == "__main__":
    unittest.main()
