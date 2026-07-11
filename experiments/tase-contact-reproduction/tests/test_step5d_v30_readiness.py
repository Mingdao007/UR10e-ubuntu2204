#!/usr/bin/env python3
"""Canonical v30 offline-readiness status and evidence tests."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_v30_offline_readiness as readiness_builder  # noqa: E402


class Step5dV30ReadinessTest(unittest.TestCase):
    def test_tracked_readiness_is_deterministically_rebuildable(self) -> None:
        path = ROOT / "config" / "step5d_v30_offline_readiness.json"
        tracked = json.loads(path.read_text(encoding="utf-8"))

        rebuilt = readiness_builder.build(generated_at=tracked["generated_at"])

        self.assertEqual(rebuilt, tracked)
        self.assertEqual(
            tracked["schema_version"], "step5d_v30_offline_readiness_v2"
        )
        self.assertEqual(tracked["status"], readiness_builder.STATUS_BLOCKED)
        self.assertNotEqual(tracked["status"], readiness_builder.STATUS_READY)

    def test_readiness_preserves_timing_history_and_hard_outlier_blocker(self) -> None:
        payload = json.loads(
            (ROOT / "config" / "step5d_v30_offline_readiness.json").read_text(
                encoding="utf-8"
            )
        )
        history = {item["role"]: item for item in payload["timing"]["history"]}

        optimized = history["six_lane_optimized_solver_10k_with_20_tick_smoke"]
        self.assertEqual(optimized["solver"]["samples"], 10_000)
        self.assertEqual(optimized["solver"]["compute_deadline_miss_count"], 3)
        self.assertGreater(optimized["solver"]["max_ms"], 2.0)
        self.assertFalse(optimized["acceptance_eligible"])
        current_source = history[
            "current_source_solver_10k_with_20_tick_runtime_smoke"
        ]
        self.assertEqual(current_source["solver"]["samples"], 10_000)
        self.assertEqual(current_source["solver"]["compute_deadline_miss_count"], 3)
        self.assertGreater(current_source["solver"]["max_ms"], 2.0)
        self.assertEqual(
            current_source["classification"], "failed_hard_solver_deadline"
        )
        self.assertFalse(current_source["acceptance_eligible"])
        evaluation = current_source["acceptance_evaluation"]
        self.assertTrue(
            evaluation["recomputed_from_single_hash_bound_raw_artifact"]
        )
        self.assertEqual(evaluation["raw_sha256"], current_source["sha256"])
        self.assertIn(
            "safe_hold_wall_duration_not_60s", evaluation["blockers"]
        )
        self.assertEqual(
            current_source["path"],
            "config/step5d_v30_current_source_solver_10k_raw.json",
        )
        raw_path = ROOT / current_source["path"]
        self.assertEqual(
            current_source["sha256"], hashlib.sha256(raw_path.read_bytes()).hexdigest()
        )
        self.assertFalse(history["runtime_shaped_smoke_not_acceptance"]["acceptance_eligible"])
        self.assertFalse(history["component_diagnostic_not_acceptance"]["acceptance_eligible"])
        self.assertIn("system_level_host_driver_timing_outliers_unresolved", payload["blockers"])
        self.assertIn("runtime_shaped_60s_500hz_acceptance_not_run", payload["blockers"])
        self.assertIn(
            "current_source_solver_10k_ran_but_60s_paced_runtime_shaped_not_run",
            payload["blockers"],
        )
        self.assertNotIn(
            "current_runtime_source_bound_full_timing_not_run", payload["blockers"]
        )

    def test_readiness_denies_live_claims_and_binds_current_sources(self) -> None:
        payload = json.loads(
            (ROOT / "config" / "step5d_v30_offline_readiness.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertFalse(payload["authorization"]["live_motion_authorized"])
        self.assertFalse(payload["authorization"]["bridge_start_authorized"])
        self.assertFalse(payload["authorization"]["tp_play_authorized"])
        self.assertFalse(payload["authorization"]["controller_upload_authorized"])
        self.assertTrue(payload["authorization"]["delivery_preparation_allowed"])
        self.assertFalse(payload["current_pointer"]["v30_is_current"])
        self.assertTrue(all(
            payload["claim_boundary"][field] is False
            for field in ("package_accepted", "live_accepted", "reproduction_complete")
        ))
        self.assertFalse(payload["control_pipeline"]["dls_shadow_runtime_fallback_allowed"])
        deadline = payload["deadline_overrun_policy"]
        self.assertTrue(deadline["hard_realtime_claim_requires_zero_deadline_miss"])
        self.assertTrue(deadline["package_static_prepared"])
        self.assertEqual(deadline["continuous_stale_stop_s"], 0.006)
        self.assertFalse(deadline["controller_or_ursim_execution_verified"])
        self.assertFalse(deadline["degraded_fail_closed_claim_allowed"])
        self.assertEqual(
            payload["timing"]["acceptance_decision_source"],
            "per-artifact recomputation from one hash-bound raw artifact; "
            "the aggregate summary is diagnostic only",
        )
        self.assertIsNone(payload["timing"]["acceptance_raw_evidence"])
        self.assertTrue(payload["package"]["binding_valid"])
        self.assertFalse(payload["package"]["controller_readback_verified"])
        self.assertFalse(payload["p0_v8_gate"]["passed"])
        self.assertIn(
            "p0_v8_final_60s_not_passed", payload["p0_v8_gate"]["blockers"]
        )
        self.assertEqual(
            payload["historical_review"]["status"],
            "historical_superseded_by_review_policy_v2",
        )
        for key in ("policy_path", "policy_sha256", "index_path", "index_sha256"):
            self.assertIn(key, payload["review_v2"])
        for field in (
            "bundler_sha256",
            "aggregator_sha256",
            "readiness_builder_sha256",
        ):
            self.assertIn(field, payload["source_contract"])
        for item in payload["source_contract"].values():
            path = ROOT / item["path"]
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), item["sha256"])

    def test_raw_claim_cannot_bypass_recomputed_acceptance(self) -> None:
        malicious = {
            "schema_version": "step5d_v30_remote_timing_raw_v1",
            "acceptance_eligible": True,
            "classification": "acceptance_eligible",
            "solver": {
                "samples": 10_000,
                "nonfinite_count": 0,
                "p99_ms": 1.0,
                "max_ms": 1.1,
                "compute_deadline_miss_count": 0,
            },
            "full_tick": {
                "samples": 30_000,
                "nonfinite_count": 0,
                "p99_ms": 1.0,
                "max_ms": 1.1,
                "compute_deadline_miss_count": 0,
            },
            "safe_hold": {
                "samples": 30_000,
                "nonfinite_count": 0,
                "p99_ms": 0.1,
                "max_ms": 0.2,
                "compute_deadline_miss_count": 0,
            },
        }
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = Path(tmp) / "raw.json"
            path.write_text(json.dumps(malicious), encoding="utf-8")
            expected_sources = {
                field: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                for field, relative in readiness_builder.SOURCE_BINDING_FILES.items()
            }
            entry = readiness_builder.timing_history_entry(
                path,
                role="malicious_claim",
                expected_source_binding=expected_sources,
                expected_replay_sha256="1" * 64,
                expected_paper_truth_sha256="2" * 64,
            )

        self.assertFalse(entry["acceptance_eligible"])
        self.assertEqual(entry["classification"], "diagnostic_only_not_acceptance")
        self.assertIn(
            "remote_timing_source_bundle_unbound",
            entry["acceptance_evaluation"]["blockers"],
        )


if __name__ == "__main__":
    unittest.main()
