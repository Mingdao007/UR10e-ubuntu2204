#!/usr/bin/env python3
"""Focused tests for the intentionally narrow Review v3 policy."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_review_v3 import canonical_composite, resolve  # noqa: E402
from run_step5d_review_v3 import run_lane  # noqa: E402


BINDING = {
    "package_triplet": "1" * 64,
    "controller_readback": "2" * 64,
    "timing_raw": "3" * 64,
    "timing_summary": "4" * 64,
    "source_fingerprint": "5" * 64,
    "effective_operator_config": "6" * 64,
}
FINGERPRINT = canonical_composite(BINDING)


def lane(provider: str, status: str = "pass") -> dict:
    payload = {
        "provider": provider,
        "requested_model": "gpt-5.6-sol" if provider == "codex" else "claude-fable-5",
        "actual_model": "gpt-5.6-sol" if provider == "codex" else "claude-fable-5",
        "effort": "xhigh",
        "requested_effort": "xhigh",
        "actual_effort": "xhigh",
        "reviewed_composite_fingerprint": FINGERPRINT,
        "reviewed_binding_sha256": "b" * 64,
        "runtime_evidence_sha256": "7" * 64,
        "started_at": "2026-07-14T00:00:00Z",
        "ended_at": "2026-07-14T00:00:01Z",
        "status": status,
        "findings": [],
        "exact_model_verified": status == "pass",
    }
    if provider == "fable5":
        if status != "pass":
            payload["degraded_transcript"] = {
                "path": "runs/fable.txt", "sha256": "8" * 64, "status": status,
            }
    return payload


def index() -> dict:
    return {
        "full_review_count_by_composite_fingerprint": {FINGERPRINT: 1},
        "review_records": [{"review_mode": "full", "composite_fingerprint": FINGERPRINT}],
    }


def manifest(fable_status: str = "pass") -> dict:
    return {
        "schema_version": "ur10e_review_manifest_v3",
        "review_mode": "full",
        "composite_fingerprint": FINGERPRINT,
        "composite_binding": BINDING,
        "binding_document_sha256": "b" * 64,
        "lanes": {
            "control_timing_claim": lane("codex"),
            "physical_operator_safety": lane("fable5", fable_status),
        },
    }


class Step5dReviewPolicyV3Test(unittest.TestCase):
    def test_runner_rejects_unverified_codex_and_degrades_unverified_fable(self) -> None:
        command = [sys.executable, "-c", "print('{}')"]
        with tempfile.TemporaryDirectory() as directory:
            codex = run_lane("control_timing_claim", command, Path(directory) / "c.txt", 1, "gpt-5.6-sol", "xhigh", FINGERPRINT, "b" * 64)
            fable = run_lane("physical_operator_safety", command, Path(directory) / "f.txt", 1, "claude-fable-5", "xhigh", FINGERPRINT, "b" * 64)
        self.assertEqual(codex["status"], "fail")
        self.assertFalse(codex["exact_model_verified"])
        self.assertEqual(fable["status"], "model_unverified")
        self.assertEqual(fable["degraded_transcript"]["status"], "model_unverified")

    def test_targeted_closer_binds_parent_index_scope_and_closed_finding(self) -> None:
        closer = {
            "schema_version": "ur10e_review_manifest_v3",
            "review_mode": "targeted_closer",
            "composite_fingerprint": FINGERPRINT,
            "composite_binding": BINDING,
            "binding_document_sha256": "b" * 64,
            "closer_binding": {
                "parent_manifest_sha256": "9" * 64,
                "finding_ids": ["P1-fixture"],
                "repaired_composite_fingerprint": FINGERPRINT,
                "allowed_lane_scope": ["control_timing_claim"],
            },
            "lanes": {"control_timing_claim": lane("codex")},
        }
        closer["lanes"]["control_timing_claim"]["findings"] = [
            {"id": "P1-fixture", "severity": "P1", "status": "closed"}
        ]
        review_index = {
            "review_records": [
                {"review_mode": "full", "manifest_sha256": "9" * 64,
                 "composite_fingerprint": "8" * 64,
                 "open_blocking_finding_ids": ["P1-fixture"],
                 "open_blocking_findings": {
                     "P1-fixture": {"lane": "control_timing_claim", "severity": "P1"}
                 }},
                {"review_mode": "targeted_closer", "composite_fingerprint": FINGERPRINT,
                 "manifest_sha256": "a" * 64},
            ]
        }
        result = resolve(
            workflow="v30", milestone="contact_pre_live",
            gate={"evidence_frozen": True, "composite_fingerprint": FINGERPRINT,
                  "manifest_sha256": "a" * 64},
            manifest=closer, index=review_index,
        )
        self.assertTrue(result["accepted"], result["blockers"])
        self.assertEqual(result["effective_stack"], "1+0")

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
            index=index(),
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
            index=index(),
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
            index=index(),
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

    def test_adversarial_manifest_contracts_fail_closed(self) -> None:
        cases = []
        count_zero = index()
        count_zero["full_review_count_by_composite_fingerprint"][FINGERPRINT] = 0
        cases.append((manifest(), count_zero, "full_review_count_must_equal_one"))
        fake_degraded = manifest("timeout")
        fake_degraded["lanes"]["physical_operator_safety"].pop("degraded_transcript")
        cases.append((fake_degraded, index(), "fable_degraded_transcript_missing_or_invalid"))
        missing_model = manifest()
        missing_model["lanes"]["control_timing_claim"].pop("actual_model")
        cases.append((missing_model, index(), "codex_lane_runtime_contract_invalid"))
        mismatch = manifest()
        mismatch["composite_binding"] = {**BINDING, "package_triplet": "9" * 64}
        cases.append((mismatch, index(), "review_v3_composite_not_recomputed_from_binding"))
        for candidate, review_index, blocker in cases:
            with self.subTest(blocker=blocker):
                result = resolve(
                    workflow="v30", milestone="contact_pre_live",
                    gate={"evidence_frozen": True, "composite_fingerprint": FINGERPRINT},
                    manifest=candidate, index=review_index,
                )
                self.assertFalse(result["accepted"])
                self.assertIn(blocker, result["blockers"])

    def test_non_hex_fingerprint_is_rejected(self) -> None:
        result = resolve(
            workflow="v30", milestone="contact_pre_live",
            gate={"evidence_frozen": True, "composite_fingerprint": "z" * 64},
            manifest=manifest(), index=index(),
        )
        self.assertIn("composite_fingerprint_invalid", result["blockers"])


if __name__ == "__main__":
    unittest.main()
