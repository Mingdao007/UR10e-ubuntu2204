#!/usr/bin/env python3
"""Focused tests for the fail-closed Astra High Review v3 policy."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from run_step5d_review_v3 import (  # noqa: E402
    ASTRA_EFFORT,
    ASTRA_MODEL,
    ASTRA_PROVIDER,
    reserve_full_review,
    run_lane,
)
from step5d_review_v3 import canonical_composite, resolve  # noqa: E402


BINDING = {
    "package_triplet": "1" * 64,
    "controller_readback": "2" * 64,
    "timing_raw": "3" * 64,
    "timing_summary": "4" * 64,
    "source_fingerprint": "5" * 64,
    "effective_operator_config": "6" * 64,
}
FINGERPRINT = canonical_composite(BINDING)
WORK_ITEM_ID = "step5d-v30-contact-20260920"


def lane(status: str = "pass", *, provider: str = ASTRA_PROVIDER,
         actual_provider: str = ASTRA_PROVIDER, model: str = ASTRA_MODEL,
         actual_model: str = ASTRA_MODEL, effort: str = ASTRA_EFFORT,
         actual_effort: str = ASTRA_EFFORT) -> dict:
    return {
        "provider": provider,
        "actual_provider": actual_provider,
        "requested_model": model,
        "actual_model": actual_model,
        "effort": actual_effort,
        "requested_effort": effort,
        "actual_effort": actual_effort,
        "reviewed_composite_fingerprint": FINGERPRINT,
        "reviewed_binding_sha256": "b" * 64,
        "runtime_evidence_sha256": "7" * 64,
        "started_at": "2026-09-20T00:00:00Z",
        "ended_at": "2026-09-20T00:00:01Z",
        "status": status,
        "findings": [],
        "exact_model_verified": status == "pass" and provider == ASTRA_PROVIDER
        and actual_provider == ASTRA_PROVIDER and model == ASTRA_MODEL
        and actual_model == ASTRA_MODEL and effort == ASTRA_EFFORT
        and actual_effort == ASTRA_EFFORT,
    }


def index() -> dict:
    return {
        "full_review_count_by_composite_fingerprint": {FINGERPRINT: 1},
        "full_review_count_by_work_item_id": {WORK_ITEM_ID: 1},
        "review_records": [{
            "review_mode": "full",
            "work_item_id": WORK_ITEM_ID,
            "composite_fingerprint": FINGERPRINT,
        }],
    }


def manifest(*, control_status: str = "pass", physical_status: str = "pass") -> dict:
    return {
        "schema_version": "ur10e_review_manifest_v3",
        "review_mode": "full",
        "work_item_id": WORK_ITEM_ID,
        "composite_fingerprint": FINGERPRINT,
        "composite_binding": BINDING,
        "binding_document_sha256": "b" * 64,
        "formal_review": {
            "provider": ASTRA_PROVIDER,
            "model": ASTRA_MODEL,
            "effort": ASTRA_EFFORT,
            "read_only": True,
            "fail_closed": True,
        },
        "lanes": {
            "control_timing_claim": lane(control_status),
            "physical_operator_safety": lane(physical_status),
        },
    }


def gate() -> dict:
    return {
        "evidence_frozen": True,
        "work_item_id": WORK_ITEM_ID,
        "composite_fingerprint": FINGERPRINT,
    }


class Step5dReviewPolicyV3Test(unittest.TestCase):
    def test_runner_requires_exact_astra_provenance_for_each_role(self) -> None:
        command = [
            sys.executable,
            "-c",
            (
                "import json; print(json.dumps({"
                "'provider':'hp-astra','actual_provider':'hp-astra',"
                "'actual_model':'gpt-6-astra','actual_effort':'high',"
                "'reviewed_composite_fingerprint':__import__('os').environ['UR10E_REVIEW_COMPOSITE_FINGERPRINT'],"
                "'reviewed_binding_sha256':__import__('os').environ['UR10E_REVIEW_BINDING_SHA256'],"
                "'findings':[]}))"
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = run_lane(
                "control_timing_claim", command, Path(directory) / "c.txt",
                ASTRA_PROVIDER, ASTRA_MODEL, ASTRA_EFFORT, FINGERPRINT, "b" * 64,
            )
        self.assertEqual(result["status"], "pass")
        self.assertTrue(result["exact_model_verified"])
        self.assertEqual(result["actual_provider"], ASTRA_PROVIDER)

        with tempfile.TemporaryDirectory() as directory:
            unverified = run_lane(
                "physical_operator_safety", [sys.executable, "-c", "print('{}')"],
                Path(directory) / "p.txt", ASTRA_PROVIDER, ASTRA_MODEL,
                ASTRA_EFFORT, FINGERPRINT, "b" * 64,
            )
        self.assertEqual(unverified["status"], "model_unverified")
        self.assertFalse(unverified["exact_model_verified"])

    def test_nonzero_astra_lane_is_unavailable_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            unavailable = run_lane(
                "physical_operator_safety",
                [sys.executable, "-c", "import sys; sys.exit(7)"],
                Path(directory) / "p.txt", ASTRA_PROVIDER, ASTRA_MODEL,
                ASTRA_EFFORT, FINGERPRINT, "b" * 64,
            )
        self.assertEqual(unavailable["status"], "unavailable_error")
        candidate = manifest()
        candidate["lanes"]["physical_operator_safety"] = unavailable
        result = resolve(
            workflow="v30", milestone="contact_pre_live", gate=gate(),
            manifest=candidate, index=index(),
        )
        self.assertFalse(result["accepted"])
        self.assertIn("physical_operator_safety_not_passed", result["blockers"])
        self.assertEqual(result["effective_stack"], "1+1")
        self.assertFalse(result["degraded_review"])

    def test_resolver_accepts_two_exact_astra_high_lanes(self) -> None:
        result = resolve(
            workflow="v30", milestone="contact_pre_live", gate=gate(),
            manifest=manifest(), index=index(),
        )
        self.assertTrue(result["accepted"], result["blockers"])
        self.assertEqual(result["effective_stack"], "1+1")
        self.assertFalse(result["degraded_review"])

    def test_provider_model_or_effort_mismatch_blocks(self) -> None:
        cases = [
            ("provider", {"provider": "other"}, "control_timing_claim_not_passed"),
            ("model", {"actual_model": "other"}, "control_timing_claim_not_passed"),
            ("effort", {"actual_effort": "xhigh"}, "control_timing_claim_not_passed"),
        ]
        for label, updates, blocker in cases:
            with self.subTest(label=label):
                candidate = manifest()
                candidate["lanes"]["control_timing_claim"].update(updates)
                result = resolve(
                    workflow="v30", milestone="contact_pre_live", gate=gate(),
                    manifest=candidate, index=index(),
                )
                self.assertFalse(result["accepted"])
                self.assertIn(blocker, result["blockers"])

    def test_missing_or_extra_lane_fails_closed(self) -> None:
        missing = manifest()
        missing["lanes"].pop("physical_operator_safety")
        result = resolve(
            workflow="v30", milestone="contact_pre_live", gate=gate(),
            manifest=missing, index=index(),
        )
        self.assertFalse(result["accepted"])
        self.assertIn("review_v3_lane_set_invalid", result["blockers"])

        extra = manifest()
        extra["lanes"]["retired_lane"] = lane()
        result = resolve(
            workflow="v30", milestone="contact_pre_live", gate=gate(),
            manifest=extra, index=index(),
        )
        self.assertFalse(result["accepted"])
        self.assertIn("review_v3_lane_set_invalid", result["blockers"])

    def test_runner_and_lanes_have_no_wall_clock_timeout(self) -> None:
        completed = type("Completed", (), {
            "stdout": "{}\n", "stderr": "", "returncode": 0,
        })()
        with tempfile.TemporaryDirectory() as directory, patch(
            "run_step5d_review_v3.subprocess.run", return_value=completed
        ) as mocked:
            run_lane(
                "control_timing_claim", ["astra"], Path(directory) / "c.txt",
                ASTRA_PROVIDER, ASTRA_MODEL, ASTRA_EFFORT, FINGERPRINT, "b" * 64,
            )
        self.assertEqual(mocked.call_count, 1)
        self.assertNotIn("timeout", mocked.call_args.kwargs)

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
                **gate(),
                "composite_fingerprint": repaired,
                "manifest_sha256": "9" * 64,
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
            workflow="v30", milestone="contact_pre_live", gate=gate(),
            manifest=candidate, index=index(),
        )
        self.assertIn("review_mode_must_be_single_full_review", result["blockers"])

    def test_ordinary_routes_remain_zero_plus_zero(self) -> None:
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

    def test_policy_has_only_astra_high_and_no_degraded_route(self) -> None:
        policy = json.loads(
            (ROOT / "config/step5d_review_policy_v3.json").read_text(encoding="utf-8")
        )
        execution = policy["execution"]
        self.assertEqual(execution["formal_review_provider"], ASTRA_PROVIDER)
        self.assertEqual(execution["formal_review_model"], ASTRA_MODEL)
        self.assertEqual(execution["formal_review_effort"], ASTRA_EFFORT)
        self.assertTrue(execution["formal_review_provenance_required"])
        self.assertEqual(
            execution["formal_review_provenance_fields"],
            ["provider", "model", "effort", "runtime"],
        )
        self.assertTrue(execution["formal_review_fail_closed"])
        self.assertNotIn("degraded_effective_stack", execution)
        for lane_config in policy["lanes"].values():
            self.assertEqual(lane_config, {
                "provider": ASTRA_PROVIDER,
                "model": ASTRA_MODEL,
                "effort": ASTRA_EFFORT,
                "required": True,
            })

    def test_adversarial_manifest_contracts_fail_closed(self) -> None:
        cases = []
        count_zero = index()
        count_zero["full_review_count_by_composite_fingerprint"][FINGERPRINT] = 0
        cases.append((manifest(), count_zero, "full_review_count_must_equal_one"))
        missing_model = manifest()
        missing_model["lanes"]["control_timing_claim"].pop("actual_model")
        cases.append((missing_model, index(), "control_timing_claim_runtime_contract_invalid"))
        mismatch = manifest()
        mismatch["composite_binding"] = {**BINDING, "package_triplet": "9" * 64}
        cases.append((mismatch, index(), "review_v3_composite_not_recomputed_from_binding"))
        for candidate, review_index, blocker in cases:
            with self.subTest(blocker=blocker):
                result = resolve(
                    workflow="v30", milestone="contact_pre_live", gate=gate(),
                    manifest=candidate, index=review_index,
                )
                self.assertFalse(result["accepted"])
                self.assertIn(blocker, result["blockers"])

    def test_non_hex_fingerprint_is_rejected(self) -> None:
        candidate = manifest()
        result = resolve(
            workflow="v30", milestone="contact_pre_live",
            gate={**gate(), "composite_fingerprint": "z" * 64},
            manifest=candidate, index=index(),
        )
        self.assertIn("composite_fingerprint_invalid", result["blockers"])


if __name__ == "__main__":
    unittest.main()
