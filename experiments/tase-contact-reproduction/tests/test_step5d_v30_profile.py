#!/usr/bin/env python3
"""Offline package/status checks for the inactive v30 candidate."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_liveprep as build  # noqa: E402
import step5d_runtime_interface as interface  # noqa: E402
import upload_ur_tp_package as upload  # noqa: E402


class Step5dV30ProfileTest(unittest.TestCase):
    def test_v30_is_inactive_and_does_not_replace_current_v29(self) -> None:
        current = json.loads((ROOT / "config" / "current_stage.json").read_text(encoding="utf-8"))
        table = json.loads((ROOT / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))
        v30 = next(row for row in table["stages"] if row.get("id") == interface.STEP5D_ABLATION_V30_STAGE_ID)

        self.assertEqual(current["program"], interface.STEP5D_ABLATION_V29_STAGE_ID)
        self.assertFalse(v30["active"])
        self.assertTrue(v30["blocked"])
        self.assertFalse(v30["current_binding"]["is_current"])
        self.assertIsNone(v30["package_delivery"]["controller_target"])
        self.assertFalse(v30["package_delivery"]["controller_uploaded"])
        self.assertFalse(v30["package_delivery"]["controller_readback_verified"])
        self.assertFalse(v30["canary_stop_register"]["enabled"])
        self.assertFalse(v30["canary_stop_register"]["armed"])
        self.assertEqual(v30["canary_stop_register"]["phases_s"], [2.0, 10.0, 60.0])
        self.assertEqual(v30["offline_acceptance"]["status"], "v30_offline_blocked")
        self.assertEqual(
            v30["local_analysis_evidence"]["offline_readiness"],
            "config/step5d_v30_offline_readiness.json",
        )

    def test_v30_local_package_is_generated_with_no_delivery_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = build.write_outputs(
                "2026-07-11T0100HKT_STEP5D_STRICT_RNN_ABLATION_V30",
                "2026-07-11T01:00:00+08:00",
                output_dir=Path(tmp),
                program=interface.STEP5D_ABLATION_V30_STAGE_ID,
            )
            script = Path(result["script"]).read_text(encoding="utf-8")
            txt = Path(result["txt"]).read_text(encoding="utf-8")
            marker = json.loads(Path(result["local_candidate_marker"]).read_text(encoding="utf-8"))

        self.assertTrue(result["local_only"])
        self.assertIn("STEP5D_STRICT_RNN_ABLATION_V30", script)
        self.assertIn("STAGE25_V30_SCAFFOLD", script)
        self.assertIn("v30 is an inactive local-only strict RNN candidate", txt)
        self.assertIn("DLS shadow diagnostics only", script + txt)
        self.assertIn("PURPOSE: v30 contact search", script)
        self.assertNotIn("PURPOSE: v31 contact search", script)
        self.assertIn("forbids DLS runtime fallback", txt)
        self.assertNotIn("remain explicit debug/fallback modes", script + txt)
        self.assertNotIn("explicit fallback/debug mode for v30", script + txt)
        self.assertTrue(marker["local_only"])
        self.assertTrue(marker["not_delivered"])
        self.assertIn("no live bridge", marker["safety_boundary"])

    def test_v30_runtime_interface_never_reports_live_bridge_or_upload(self) -> None:
        runtime = interface.resolve_runtime_interface(
            program=interface.STEP5D_ABLATION_V30_STAGE_ID,
            root=ROOT,
            env={},
        )

        lines = interface.live_ready_lines(runtime, {"state": "MISS", "fingerprint_ok": False})
        rendered = "\n".join(lines)

        self.assertEqual(runtime.controller_target, "LOCAL_ONLY_NOT_DELIVERED")
        self.assertTrue(runtime.hard_contract["offline_candidate"])
        self.assertIn("phase=v30-offline-candidate", rendered)
        self.assertIn("upload=no", rendered)
        self.assertNotIn("phase=live-bridge", rendered)

    def test_v30_delivery_block_cannot_be_bypassed_by_cli_override_flags(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "inactive offline candidate"):
            upload.enforce_offline_candidate_delivery_block(
                interface.STEP5D_ABLATION_V30_STAGE_ID,
                root=ROOT,
            )

    def test_v30_bridge_source_uses_contract_pipeline_but_refuses_live_start(self) -> None:
        source = (ROOT / "tools" / "kunwei_rtde_bridge.py").read_text(encoding="utf-8")

        self.assertIn("from step5d_control_contract import", source)
        self.assertIn("observation_v30 = Step5dObservation(", source)
        self.assertIn("raw_candidate_v30 = state.step5d_v30_policy.compute(", source)
        self.assertIn("step5d_v30_contract_pipeline(", source)
        self.assertIn("dls_shadow = compute_dls_shadow(", source)
        self.assertIn("command = decision_to_register_command(", source)
        self.assertIn("deferred_diagnostics.record(", source)
        self.assertIn("v30 raw bridge is an inactive offline candidate", source)


if __name__ == "__main__":
    unittest.main()
