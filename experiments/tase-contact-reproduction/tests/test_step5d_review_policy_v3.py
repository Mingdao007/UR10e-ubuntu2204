#!/usr/bin/env python3
"""Focused tests for the intentionally narrow Review v3 policy."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_review_v3 import canonical_composite, resolve  # noqa: E402
from run_step5d_review_v3 import (  # noqa: E402
    fable_limit_returned,
    fable_preflight,
    reserve_full_review,
    run_lane,
)


BINDING = {
    "package_triplet": "1" * 64,
    "controller_readback": "2" * 64,
    "timing_raw": "3" * 64,
    "timing_summary": "4" * 64,
    "source_fingerprint": "5" * 64,
    "effective_operator_config": "6" * 64,
}
FINGERPRINT = canonical_composite(BINDING)
WORK_ITEM_ID = "step5d-v30-contact-20260714"


def lane(provider: str, status: str = "pass") -> dict:
    payload = {
        "provider": provider,
        "requested_model": "gpt-5.6-sol" if provider == "codex" else "claude-fable-5",
        "actual_model": "gpt-5.6-sol" if provider == "codex" else "claude-fable-5",
        "effort": "xhigh" if provider == "codex" else "high",
        "requested_effort": "xhigh" if provider == "codex" else "high",
        "actual_effort": "xhigh" if provider == "codex" else "high",
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
        "full_review_count_by_work_item_id": {WORK_ITEM_ID: 1},
        "review_records": [{"review_mode": "full", "work_item_id": WORK_ITEM_ID,
                            "composite_fingerprint": FINGERPRINT}],
    }


def manifest(fable_status: str = "pass") -> dict:
    return {
        "schema_version": "ur10e_review_manifest_v3",
        "review_mode": "full",
        "work_item_id": WORK_ITEM_ID,
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
            codex = run_lane("control_timing_claim", command, Path(directory) / "c.txt", "gpt-5.6-sol", "xhigh", FINGERPRINT, "b" * 64)
            fable = run_lane("physical_operator_safety", command, Path(directory) / "f.txt", "claude-fable-5", "high", FINGERPRINT, "b" * 64)
        self.assertEqual(codex["status"], "fail")
        self.assertFalse(codex["exact_model_verified"])
        self.assertEqual(fable["status"], "model_unverified")
        self.assertEqual(fable["degraded_transcript"]["status"], "model_unverified")

    def test_fable_quota_result_is_skipped_without_wait_or_retry(self) -> None:
        self.assertTrue(fable_limit_returned("session limit reached; reset tomorrow"))
        command = [sys.executable, "-c", "import sys; print('quota limit reached'); sys.exit(1)"]
        with tempfile.TemporaryDirectory() as directory:
            lane_result = run_lane(
                "physical_operator_safety", command, Path(directory) / "f.txt",
                "claude-fable-5", "high", FINGERPRINT, "b" * 64,
            )
        self.assertEqual(lane_result["status"], "skipped_unavailable")

    def test_successful_fable_transcript_mentioning_rate_limit_is_not_skipped(self) -> None:
        command = [sys.executable, "-c", "print('finding discusses an RTDE rate limit')"]
        with tempfile.TemporaryDirectory() as directory:
            lane_result = run_lane(
                "physical_operator_safety", command, Path(directory) / "f.txt",
                "claude-fable-5", "high", FINGERPRINT, "b" * 64,
            )
        self.assertEqual(lane_result["status"], "model_unverified")

    def test_failed_review_finding_that_mentions_rate_limit_is_not_quota(self) -> None:
        command = [
            sys.executable, "-c",
            "import sys; print('finding: RTDE rate limit contract is wrong'); sys.exit(1)",
        ]
        with tempfile.TemporaryDirectory() as directory:
            lane_result = run_lane(
                "physical_operator_safety", command, Path(directory) / "f.txt",
                "claude-fable-5", "high", FINGERPRINT, "b" * 64,
            )
        self.assertEqual(lane_result["status"], "unavailable_error")

    def test_runner_and_preflight_never_pass_a_subprocess_timeout(self) -> None:
        completed = type("Completed", (), {"stdout": "{}\n", "stderr": "", "returncode": 0})()
        with tempfile.TemporaryDirectory() as directory, patch(
            "run_step5d_review_v3.subprocess.run", return_value=completed
        ) as mocked:
            run_lane(
                "control_timing_claim", ["codex"], Path(directory) / "c.txt",
                "gpt-5.6-sol", "xhigh", FINGERPRINT, "b" * 64,
            )
            fable_preflight(["claude"], ["claude", "probe"])
        self.assertEqual(mocked.call_count, 2)
        self.assertTrue(all("timeout" not in call.kwargs for call in mocked.call_args_list))

    def test_work_item_can_have_only_one_full_review_across_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            review_index = Path(directory) / "index.json"
            review_index.write_text(json.dumps({
                "full_review_count_by_composite_fingerprint": {},
                "full_review_count_by_work_item_id": {},
                "review_records": [],
            }))
            reserve_full_review(review_index, FINGERPRINT, WORK_ITEM_ID)
            with self.assertRaisesRegex(SystemExit, "work item"):
                reserve_full_review(review_index, "a" * 64, WORK_ITEM_ID)

    def test_finding_fix_uses_deterministic_owner_validation_without_reviewer(self) -> None:
        reviewed = manifest()
        reviewed["lanes"]["control_timing_claim"]["findings"] = [
            {"id": "P1-fixture", "severity": "P1", "status": "open"}
        ]
        repaired = "a" * 64
        result = resolve(
            workflow="v30", milestone="contact_pre_live",
            gate={
                "evidence_frozen": True,
                "work_item_id": WORK_ITEM_ID,
                "composite_fingerprint": repaired,
                "manifest_sha256": "9" * 64,
                "decision_digest": "d" * 64,
                "decision_digest": "d" * 64,
                "deterministic_finding_closure": {
                    "no_reviewer_invoked": True,
                    "parent_review_manifest_sha256": "9" * 64,
                    "reviewed_composite_fingerprint": FINGERPRINT,
                    "repaired_composite_fingerprint": repaired,
                    "finding_ids": ["P1-fixture"],
                    "decision_digest": "d" * 64,
                    "owner_validation": {
                        "path": "runs/owner-validation.json",
                        "sha256": "e" * 64,
                        "status": "pass",
                    },
                },
            },
            manifest=reviewed, index=index(),
        )
        self.assertTrue(result["accepted"], result["blockers"])
        self.assertTrue(result["deterministic_finding_closure_accepted"])

    def test_targeted_closer_is_rejected(self) -> None:
        candidate = manifest()
        candidate["review_mode"] = "targeted_closer"
        result = resolve(
            workflow="v30", milestone="contact_pre_live",
            gate={"evidence_frozen": True, "work_item_id": WORK_ITEM_ID,
                  "composite_fingerprint": FINGERPRINT},
            manifest=candidate, index=index(),
        )
        self.assertIn("review_mode_must_be_single_full_review", result["blockers"])

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
            gate={"evidence_frozen": True, "work_item_id": WORK_ITEM_ID,
                  "composite_fingerprint": FINGERPRINT},
            manifest=manifest(),
            index=index(),
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(result["effective_stack"], "1+1")
        self.assertFalse(result["degraded_review"])

    def test_explicit_fable_limit_degrades_to_valid_one_plus_zero(self) -> None:
        result = resolve(
            workflow="v30",
            milestone="contact_pre_live",
            gate={"evidence_frozen": True, "work_item_id": WORK_ITEM_ID,
                  "composite_fingerprint": FINGERPRINT},
            manifest=manifest("skipped_unavailable"),
            index=index(),
        )
        self.assertTrue(result["accepted"])
        self.assertTrue(result["degraded_review"])
        self.assertEqual(result["effective_stack"], "1+0")

    def test_authentication_or_other_non_limit_fable_failure_does_not_degrade(self) -> None:
        result = resolve(
            workflow="v30",
            milestone="contact_pre_live",
            gate={"evidence_frozen": True, "work_item_id": WORK_ITEM_ID,
                  "composite_fingerprint": FINGERPRINT},
            manifest=manifest("unavailable_not_logged_in"),
            index=index(),
        )
        self.assertFalse(result["accepted"])
        self.assertFalse(result["degraded_review"])
        self.assertIn("fable5_lane_neither_passed_nor_degradable", result["blockers"])

    def test_codex_lane_never_degrades_and_p1_blocks(self) -> None:
        candidate = manifest("timeout")
        candidate["lanes"]["control_timing_claim"]["status"] = "timeout"
        candidate["lanes"]["control_timing_claim"]["findings"] = [
            {"severity": "P1", "status": "open", "summary": "fixture"}
        ]
        result = resolve(
            workflow="v29",
            milestone="contact_pre_live",
            gate={"evidence_frozen": True, "work_item_id": WORK_ITEM_ID,
                  "composite_fingerprint": FINGERPRINT},
            manifest=candidate,
            index=index(),
        )
        self.assertFalse(result["accepted"])
        self.assertIn("codex_control_timing_claim_not_passed", result["blockers"])
        self.assertIn("open_p0_or_p1_finding", result["blockers"])

    def test_review_lanes_have_no_wall_clock_timeout(self) -> None:
        policy = json.loads(
            (ROOT / "config/step5d_review_policy_v3.json").read_text(encoding="utf-8")
        )
        execution = policy["execution"]
        self.assertIs(execution["review_lanes_have_wall_clock_timeout"], False)
        self.assertNotIn("fable5_preflight_timeout_seconds", execution)
        self.assertNotIn("review_lane_timeout_seconds", execution)
        self.assertNotIn("total_gate_timeout_seconds", execution)

    def test_adversarial_manifest_contracts_fail_closed(self) -> None:
        cases = []
        count_zero = index()
        count_zero["full_review_count_by_composite_fingerprint"][FINGERPRINT] = 0
        cases.append((manifest(), count_zero, "full_review_count_must_equal_one"))
        fake_degraded = manifest("skipped_unavailable")
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
                    gate={"evidence_frozen": True, "work_item_id": WORK_ITEM_ID,
                          "composite_fingerprint": FINGERPRINT},
                    manifest=candidate, index=review_index,
                )
                self.assertFalse(result["accepted"])
                self.assertIn(blocker, result["blockers"])

    def test_non_hex_fingerprint_is_rejected(self) -> None:
        result = resolve(
            workflow="v30", milestone="contact_pre_live",
            gate={"evidence_frozen": True, "work_item_id": WORK_ITEM_ID,
                  "composite_fingerprint": "z" * 64},
            manifest=manifest(), index=index(),
        )
        self.assertIn("composite_fingerprint_invalid", result["blockers"])


if __name__ == "__main__":
    unittest.main()
