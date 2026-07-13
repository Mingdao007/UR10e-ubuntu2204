#!/usr/bin/env python3
"""Focused tests for the intentionally narrow Review v3 policy."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_review_v3 import resolve  # noqa: E402


FINGERPRINT = "a" * 64


def manifest(fable_status: str = "pass") -> dict:
    return {
        "schema_version": "ur10e_review_manifest_v3",
        "review_mode": "full",
        "composite_fingerprint": FINGERPRINT,
        "lanes": {
            "control_timing_claim": {
                "provider": "codex",
                "status": "pass",
                "findings": [],
            },
            "physical_operator_safety": {
                "provider": "fable5",
                "status": fable_status,
                "exact_model_verified": fable_status == "pass",
                "findings": [],
            },
        },
    }


class Step5dReviewPolicyV3Test(unittest.TestCase):
    def test_ordinary_direction_p0_postrun_package_and_push_are_zero_plus_zero(self) -> None:
        routes = [
            ("ordinary", "development"),
            ("ordinary", "handoff"),
            ("ordinary", "package"),
            ("ordinary", "commit_push"),
            ("direction_change", "evidence_freeze"),
            ("p0_v8", "pre_live"),
            ("p0_v8", "post_run"),
            ("v30", "post_run"),
        ]
        for workflow, milestone in routes:
            with self.subTest(workflow=workflow, milestone=milestone):
                result = resolve(workflow=workflow, milestone=milestone)
                self.assertTrue(result["accepted"])
                self.assertEqual(result["effective_stack"], "0+0")
                self.assertFalse(result["review_invocation_required"])

    def test_contact_pre_live_accepts_one_plus_one(self) -> None:
        result = resolve(
            workflow="v30",
            milestone="contact_pre_live",
            gate={"evidence_frozen": True, "composite_fingerprint": FINGERPRINT},
            manifest=manifest(),
            index={"full_review_count_by_composite_fingerprint": {FINGERPRINT: 1}},
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["effective_stack"], "1+1")
        self.assertFalse(result["degraded_review"])

    def test_fable_timeout_degrades_to_valid_one_plus_zero(self) -> None:
        result = resolve(
            workflow="v30",
            milestone="contact_pre_live",
            gate={"evidence_frozen": True, "composite_fingerprint": FINGERPRINT},
            manifest=manifest("timeout"),
            index={"full_review_count_by_composite_fingerprint": {FINGERPRINT: 1}},
        )
        self.assertTrue(result["accepted"])
        self.assertTrue(result["degraded_review"])
        self.assertEqual(result["effective_stack"], "1+0")

    def test_codex_lane_never_degrades_and_p1_blocks(self) -> None:
        candidate = manifest("timeout")
        candidate["lanes"]["control_timing_claim"]["status"] = "timeout"
        candidate["lanes"]["control_timing_claim"]["findings"] = [
            {"severity": "P1", "status": "open", "summary": "fixture"}
        ]
        result = resolve(
            workflow="v29",
            milestone="contact_pre_live",
            gate={"evidence_frozen": True, "composite_fingerprint": FINGERPRINT},
            manifest=candidate,
        )
        self.assertFalse(result["accepted"])
        self.assertIn("codex_control_timing_claim_not_passed", result["blockers"])
        self.assertIn("open_p0_or_p1_finding", result["blockers"])

    def test_timeout_contract_is_20_300_330(self) -> None:
        policy = json.loads(
            (ROOT / "config/step5d_review_policy_v3.json").read_text(encoding="utf-8")
        )
        execution = policy["execution"]
        self.assertEqual(execution["fable5_preflight_timeout_seconds"], 20)
        self.assertEqual(execution["review_lane_timeout_seconds"], 300)
        self.assertEqual(execution["total_gate_timeout_seconds"], 330)


if __name__ == "__main__":
    unittest.main()
