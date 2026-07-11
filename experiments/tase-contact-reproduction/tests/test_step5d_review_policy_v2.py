#!/usr/bin/env python3
"""Focused acceptance tests for UR10e Review v2 policy artifacts."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_review_index_v2 as index_builder  # noqa: E402
import build_step5d_review_packet as packet_builder  # noqa: E402
import validate_step5d_review_v2 as validator  # noqa: E402
from step5d_review_v2 import canonical_sha256, file_sha256  # noqa: E402


CODE_FILE = "tools/step5d_control_contract.py"
PACKAGE_FILE = "programs/step5/step5d/step5d_strict_rnn_ablation_v30.script"
EVIDENCE_FILE = "config/step5d_v30_replay_summary.json"
BASE = "1" * 40
HEAD = "2" * 40


def base_spec(
    *,
    workflow: str = "p0_v8",
    milestone: str = "pre_live",
    fable_status: str = "available",
) -> dict:
    return {
        "workflow": workflow,
        "milestone": milestone,
        "base_commit": BASE,
        "head_commit": HEAD,
        "evidence_frozen": True,
        "state_resolver": {
            "ok": False,
            "workflow_state": "liveprep_blocked",
            "experiment_root": str(ROOT),
            "blockers": ["review_v2_manifest_missing"],
        },
        "changed_symbols": [
            {"path": CODE_FILE, "symbol": "SafetyEnvelope.evaluate"}
        ],
        "tests": [
            {
                "id": "control_contract",
                "path": "tests/test_step5d_v30_control_contract.py",
                "status": "pass",
                "lanes": [
                    "control_timing_claim"
                    if workflow == "p0_v8"
                    else "control_claim"
                ],
                "command": "python3 -m unittest tests.test_step5d_v30_control_contract",
            }
        ],
        "files": {
            "code": [CODE_FILE],
            "package": [PACKAGE_FILE],
            "evidence": [EVIDENCE_FILE],
        },
        "package_identity": {
            "program": "step5d_strict_rnn_ablation_v30",
            "manifest_sha256": "3" * 64,
        },
        "evidence_roles": {
            "package_readback": EVIDENCE_FILE,
            "safe_hold": EVIDENCE_FILE,
            "timing": EVIDENCE_FILE,
        },
        "claims": {
            "package_accepted": False,
            "live_accepted": False,
            "reproduction_complete": False,
        },
        "fable5_preflight": {
            "status": fable_status,
            "command_surface": "Claude CLI",
        },
    }


def contact_spec() -> dict:
    spec = base_spec(workflow="v30", milestone="contact_pre_live")
    spec["evidence_roles"].update(
        {
            "p0_v8_pass": EVIDENCE_FILE,
            "readiness": EVIDENCE_FILE,
        }
    )
    return spec


def passing_lane(invocation: dict, *, status: str = "pass", findings=None) -> dict:
    lane = {
        "status": status,
        "verdict": status,
        "requested_model": invocation["requested_model"],
        "requested_effort": invocation["requested_effort"],
        "timeout_seconds": invocation["timeout_seconds"],
        "elapsed_seconds": 12.0 if status == "pass" else 0.0,
        "findings": findings or [],
    }
    if status == "pass":
        lane["actual_model"] = invocation["requested_model"]
        lane["actual_effort"] = invocation["requested_effort"]
    else:
        lane["actual_model"] = None
        lane["actual_effort"] = None
    return lane


def full_manifest(packet: dict) -> dict:
    lanes = {
        item["lane"]: passing_lane(item)
        for item in packet["invocation_plan"]
    }
    return {
        "schema_version": "ur10e_review_manifest_v2",
        "policy_id": packet["policy_id"],
        "workflow": packet["workflow"],
        "milestone": packet["milestone"],
        "required_stack": packet["required_stack"],
        "review_mode": "full",
        "composite_fingerprint": packet["fingerprints"]["composite"],
        "lanes": lanes,
        "invalidation_reason": None,
    }


class Step5dReviewPolicyV2Test(unittest.TestCase):
    def test_policy_matrix_covers_p0_v8_v29_and_v30(self) -> None:
        policy = json.loads(
            (ROOT / "config" / "step5d_review_policy_v2.json").read_text(
                encoding="utf-8"
            )
        )
        mapping = policy["workflow_milestone_map"]
        classes = policy["review_classes"]
        expected = {
            ("ordinary", "development"): "0+0",
            ("ordinary", "handoff"): "0+0",
            ("direction_change", "evidence_freeze"): "1+0",
            ("p0_v8", "pre_live"): "1+1",
            ("p0_v8", "post_run"): "1+0",
            ("v29", "baseline_re_review"): "1+0",
            ("v29", "contact_pre_live"): "2+1",
            ("v30", "contact_pre_live"): "2+1",
            ("v29", "post_run"): "1+1",
            ("v30", "post_run"): "1+1",
        }
        for (workflow, milestone), stack in expected.items():
            with self.subTest(workflow=workflow, milestone=milestone):
                review_class = mapping[workflow][milestone]
                self.assertEqual(classes[review_class]["stack"], stack)
        self.assertNotIn(
            "p0_v8_pass",
            classes[mapping["v29"]["contact_pre_live"]]["required_evidence_roles"],
        )
        self.assertIn(
            "p0_v8_pass",
            classes[mapping["v30"]["contact_pre_live"]]["required_evidence_roles"],
        )

    def test_packet_is_deterministic_and_has_four_component_fingerprints(self) -> None:
        spec = base_spec()
        first = packet_builder.build_packet(spec)
        second = packet_builder.build_packet(copy.deepcopy(spec))

        self.assertEqual(first, second)
        self.assertEqual(
            set(first["fingerprints"]),
            {"code", "package", "evidence", "policy", "composite"},
        )
        self.assertEqual(len(first["fingerprints"]["composite"]), 64)
        self.assertEqual(first["state_resolver"]["experiment_root"], ".")
        self.assertNotIn(str(ROOT), json.dumps(first, sort_keys=True))
        self.assertTrue(validator.validate_packet(first)["ok"])

    def test_ordinary_development_is_zero_plus_zero_without_invocation(self) -> None:
        spec = base_spec(workflow="ordinary", milestone="development")
        spec["tests"] = []
        spec["evidence_roles"] = {}
        packet = packet_builder.build_packet(spec)

        self.assertEqual(packet["required_stack"], "0+0")
        self.assertFalse(packet["full_review_required"])
        self.assertFalse(packet["review_due"])
        self.assertEqual(packet["invocation_plan"], [])
        self.assertTrue(validator.validate_packet(packet)["ok"])

    def test_high_is_default_and_max_is_risk_escalation_only(self) -> None:
        normal = packet_builder.build_packet(
            contact_spec()
        )
        normal_codex = [
            lane for lane in normal["invocation_plan"] if lane["provider"] == "codex"
        ]
        self.assertEqual(len(normal_codex), 2)
        self.assertTrue(all(lane["requested_effort"] == "high" for lane in normal_codex))

        risky_spec = contact_spec()
        risky_spec["risk_flags"] = ["major_force_frame_change"]
        risky = packet_builder.build_packet(risky_spec)
        risky_codex = [
            lane for lane in risky["invocation_plan"] if lane["provider"] == "codex"
        ]
        risky_fable = [
            lane for lane in risky["invocation_plan"] if lane["provider"] == "fable5"
        ]
        self.assertTrue(all(lane["requested_effort"] == "max" for lane in risky_codex))
        self.assertEqual(risky_fable[0]["requested_effort"], "high")

    def test_fable_preflight_unavailable_does_not_block_codex_lanes(self) -> None:
        packet = packet_builder.build_packet(base_spec(fable_status="unavailable_not_logged_in"))
        by_provider = {item["provider"]: item for item in packet["invocation_plan"]}

        self.assertTrue(packet["review_due"])
        self.assertTrue(by_provider["codex"]["start_allowed"])
        self.assertFalse(by_provider["fable5"]["start_allowed"])
        self.assertIn(
            "fable5_review_requires_supplement_or_fingerprint_bound_user_waiver",
            packet["live_authorization_blockers"],
        )

    def test_missing_fable_preflight_stops_all_lane_start(self) -> None:
        spec = base_spec()
        spec.pop("fable5_preflight")
        packet = packet_builder.build_packet(spec)

        self.assertTrue(packet["review_due"])
        self.assertFalse(packet["review_start_allowed"])
        self.assertIn("fable5_preflight_not_run", packet["review_start_blockers"])

    def test_same_fingerprint_index_prevents_second_full_review(self) -> None:
        spec = base_spec()
        first = packet_builder.build_packet(spec)
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            index_path = Path(temp) / "index.json"
            index_path.write_text(
                json.dumps(
                    {
                        "schema_version": "ur10e_review_index_v2",
                        "v2_reviews": [
                            {
                                "review_mode": "full",
                                "composite_fingerprint": first["fingerprints"]["composite"],
                                "sha256": "a" * 64,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            spec["review_index_path"] = str(index_path.relative_to(ROOT))
            second = packet_builder.build_packet(spec)

        self.assertEqual(first["fingerprints"], second["fingerprints"])
        self.assertFalse(second["review_due"])
        self.assertIn(
            "full_review_already_recorded_for_fingerprint",
            second["freeze_blockers"],
        )

    def test_p2_backlog_is_nonblocking(self) -> None:
        packet = packet_builder.build_packet(base_spec())
        manifest = full_manifest(packet)
        control_lane = next(
            item["lane"]
            for item in packet["invocation_plan"]
            if item["provider"] == "codex"
        )
        manifest["lanes"][control_lane]["findings"] = [
            {
                "id": "P2-LOG-DOC",
                "severity": "P2",
                "status": "backlog",
                "backlog_ref": "backlog://ur10e/P2-LOG-DOC",
            }
        ]

        result = validator.validate_manifest(manifest, packet)

        self.assertTrue(result["accepted"], result["blockers"])
        self.assertEqual(result["nonblocking_p2_backlog"], ["P2-LOG-DOC"])

    def test_model_effort_mismatch_and_timeout_block(self) -> None:
        packet = packet_builder.build_packet(base_spec())
        manifest = full_manifest(packet)
        control_lane = next(
            item["lane"]
            for item in packet["invocation_plan"]
            if item["provider"] == "codex"
        )
        manifest["lanes"][control_lane]["actual_effort"] = "max"
        manifest["lanes"][control_lane]["elapsed_seconds"] = 721
        manifest["lanes"][control_lane]["status"] = "timeout"

        result = validator.validate_manifest(manifest, packet)

        self.assertFalse(result["accepted"])
        self.assertIn(f"lane_timeout_exceeded:{control_lane}", result["blockers"])
        self.assertIn(f"lane_timed_out:{control_lane}", result["blockers"])

    def test_fingerprint_bound_user_waiver_only_covers_missing_fable_lane(self) -> None:
        packet = packet_builder.build_packet(base_spec(fable_status="unavailable_not_logged_in"))
        manifest = full_manifest(packet)
        fable_lane = next(
            item["lane"]
            for item in packet["invocation_plan"]
            if item["provider"] == "fable5"
        )
        invocation = next(
            item for item in packet["invocation_plan"] if item["lane"] == fable_lane
        )
        manifest["lanes"][fable_lane] = passing_lane(invocation, status="unavailable")
        manifest["waiver"] = {
            "waiver_id": "user-waiver-test",
            "provider": "fable5",
            "lane": "physical_operator_safety",
            "composite_fingerprint": packet["fingerprints"]["composite"],
            "authorized_by": "user",
            "explicit": True,
            "issued_at": "2026-07-11T00:00:00+08:00",
            "authorization_evidence": "explicit test fixture",
            "reason": "Fable5 unavailable after preflight",
        }

        accepted = validator.validate_manifest(manifest, packet)
        self.assertTrue(accepted["accepted"], accepted["blockers"])

        manifest["waiver"]["composite_fingerprint"] = "0" * 64
        rejected = validator.validate_manifest(manifest, packet)
        self.assertFalse(rejected["accepted"])
        self.assertIn("waiver_fingerprint_mismatch", rejected["blockers"])

        manifest["waiver"]["composite_fingerprint"] = packet["fingerprints"][
            "composite"
        ]
        manifest["lanes"][fable_lane]["status"] = "block"
        manifest["lanes"][fable_lane]["verdict"] = "block"
        adverse = validator.validate_manifest(manifest, packet)
        self.assertFalse(adverse["accepted"])
        self.assertIn(f"lane_not_pass:{fable_lane}:block", adverse["blockers"])

    def test_p1_fix_uses_targeted_lane_closer_not_full_review(self) -> None:
        packet = packet_builder.build_packet(base_spec())
        source = full_manifest(packet)
        control_lane = next(
            item["lane"]
            for item in packet["invocation_plan"]
            if item["provider"] == "codex"
        )
        source["lanes"][control_lane]["status"] = "block"
        source["lanes"][control_lane]["verdict"] = "block"
        source["lanes"][control_lane]["findings"] = [
            {
                "id": "P1-NORMAL-FRAME",
                "severity": "P1",
                "status": "open",
            }
        ]
        invocation = next(
            item for item in packet["invocation_plan"] if item["lane"] == control_lane
        )
        closer = {
            "schema_version": "ur10e_review_manifest_v2",
            "policy_id": packet["policy_id"],
            "workflow": packet["workflow"],
            "milestone": packet["milestone"],
            "required_stack": packet["required_stack"],
            "review_mode": "targeted_closer",
            "composite_fingerprint": packet["fingerprints"]["composite"],
            "targeted_closer": {
                "source_manifest_sha256": canonical_sha256(source),
                "finding_ids": ["P1-NORMAL-FRAME"],
                "supplemental_lanes": [],
            },
            "lanes": {
                control_lane: passing_lane(
                    invocation,
                    findings=[
                        {
                            "id": "P1-NORMAL-FRAME",
                            "severity": "P1",
                            "status": "closed",
                            "resolution": "canonical frame test and guard now pass",
                        }
                    ],
                )
            },
            "invalidation_reason": None,
        }

        result = validator.validate_manifest(
            closer, packet, source_manifest=source
        )

        self.assertTrue(result["accepted"], result["blockers"])
        self.assertEqual(result["review_mode"], "targeted_closer")

    def test_unavailable_fable_lane_can_be_supplemented_without_full_rerun(self) -> None:
        packet = packet_builder.build_packet(
            base_spec(fable_status="unavailable_not_logged_in")
        )
        source = full_manifest(packet)
        fable_invocation = next(
            item for item in packet["invocation_plan"] if item["provider"] == "fable5"
        )
        lane_id = fable_invocation["lane"]
        source["lanes"][lane_id] = passing_lane(
            fable_invocation, status="unavailable"
        )
        supplement = {
            "schema_version": "ur10e_review_manifest_v2",
            "policy_id": packet["policy_id"],
            "workflow": packet["workflow"],
            "milestone": packet["milestone"],
            "required_stack": packet["required_stack"],
            "review_mode": "targeted_closer",
            "composite_fingerprint": packet["fingerprints"]["composite"],
            "targeted_closer": {
                "source_manifest_sha256": canonical_sha256(source),
                "finding_ids": [],
                "supplemental_lanes": [lane_id],
            },
            "lanes": {lane_id: passing_lane(fable_invocation)},
            "invalidation_reason": None,
        }

        result = validator.validate_manifest(
            supplement, packet, source_manifest=source
        )

        self.assertTrue(result["accepted"], result["blockers"])
        self.assertEqual(result["review_mode"], "targeted_closer")

    def test_duplicate_full_review_is_blocked_by_index(self) -> None:
        packet = packet_builder.build_packet(base_spec())
        manifest = full_manifest(packet)
        index = {
            "schema_version": "ur10e_review_index_v2",
            "v2_reviews": [
                {
                    "review_mode": "full",
                    "composite_fingerprint": packet["fingerprints"]["composite"],
                    "sha256": "a" * 64,
                }
            ],
        }

        result = validator.validate_manifest(
            manifest,
            packet,
            review_index=index,
            manifest_sha256="b" * 64,
        )

        self.assertFalse(result["accepted"])
        self.assertIn(
            "duplicate_full_review_for_composite_fingerprint", result["blockers"]
        )

    def test_index_builder_flags_duplicate_full_review_records(self) -> None:
        packet = packet_builder.build_packet(base_spec())
        manifest = full_manifest(packet)
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            first = Path(temp) / "first.json"
            second = Path(temp) / "second.json"
            rendered = json.dumps(manifest, sort_keys=True)
            first.write_text(rendered, encoding="utf-8")
            second.write_text(rendered, encoding="utf-8")
            index = index_builder.build(
                review_manifests=[
                    first.relative_to(ROOT),
                    second.relative_to(ROOT),
                ]
            )

        self.assertEqual(
            index["blockers"], ["duplicate_full_review_for_composite_fingerprint"]
        )
        self.assertEqual(
            len(index["duplicate_full_review_fingerprints"]), 1
        )

    def test_historical_index_is_rebuildable_and_old_artifacts_are_hash_bound(self) -> None:
        tracked = json.loads(
            (ROOT / "config" / "step5d_review_index_v2.json").read_text(
                encoding="utf-8"
            )
        )
        rebuilt = index_builder.build()

        self.assertEqual(rebuilt, tracked)
        self.assertEqual(tracked["v2_reviews"], [])
        for entry in tracked["historical_artifacts"]:
            path = ROOT / entry["path"]
            self.assertEqual(file_sha256(path), entry["sha256"])
            self.assertEqual(
                entry["status"], "historical_superseded_by_review_policy_v2"
            )
            self.assertFalse(entry["counts_as_review_v2"])

    def test_lane_test_limit_and_fingerprint_invalidation_fail_closed(self) -> None:
        spec = base_spec()
        spec["tests"] = [
            {
                "id": f"focused-{index}",
                "status": "pass",
                "lanes": ["control_timing_claim"],
            }
            for index in range(4)
        ]
        packet = packet_builder.build_packet(spec)
        self.assertFalse(packet["review_due"])
        self.assertIn(
            "lane_focused_test_limit_exceeded:control_timing_claim",
            packet["blockers"],
        )

        valid_packet = packet_builder.build_packet(base_spec())
        manifest = full_manifest(valid_packet)
        manifest["composite_fingerprint"] = "f" * 64
        result = validator.validate_manifest(manifest, valid_packet)
        self.assertFalse(result["accepted"])
        self.assertIn("manifest_composite_fingerprint_mismatch", result["blockers"])
        self.assertIn(
            "fingerprint_mismatch_without_invalidation_reason", result["blockers"]
        )


if __name__ == "__main__":
    unittest.main()
