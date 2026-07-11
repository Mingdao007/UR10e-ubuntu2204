#!/usr/bin/env python3
"""Focused tests for the P0 v8 offline/controller state separation."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_step5d_p0_v8_offline_diagnostic import (  # noqa: E402
    FALSE_CLAIMS,
    P0_REQUIRED_FAULTS,
    bound_state_binding,
    build_diagnostic,
    canonical_sha256,
    unbound_state_binding,
    validate_diagnostic,
    validate_state_binding,
)


SOURCE_SHA = "1" * 64
MODEL_SHA = "2" * 64
FRAME_SHA = "3" * 64
RUN_SHA = "4" * 64
MODEL_MANIFEST_SHA = "5" * 64


def phase_evidence(index: int, duration_s: float, *, timing_pass: bool = False) -> dict:
    tick_count = int(duration_s * 500)
    compute_misses = 0 if timing_pass else index + 1
    absolute_misses = 0 if timing_pass else index + 2
    p99_ms = 1.2 if timing_pass else 2.2 + index
    max_ms = 1.4 if timing_pass else 2.5 + index
    return {
        "source_binding": {
            "base_commit": "abcdef0123456789",
            "head_commit": "abcdef0123456789",
            "composite_sha256": SOURCE_SHA,
            "model_sha256": MODEL_SHA,
            "frame_lineage_sha256": FRAME_SHA,
        },
        "engine": {
            "name": "mujoco",
            "version": "3.8.1",
            "physics_provenance": "geometry_provisional",
        },
        "phase": {
            "duration_s": duration_s,
            "sequence_index": index,
        },
        "nominal": {
            "tick_count": tick_count,
            "accepted_tick_count": tick_count,
            "safe_hold_count": 0,
            "stop_count": 0,
            "missed_sequence_count": 0,
            "nonfinite_output_count": 0,
            "qdot_bound_violation_count": 0,
            "unexpected_contact_count": 0,
            "cage_collision_count": 0,
            "max_qdot_abs_rad_s": 0.0004,
            "control_path_diagnostic_pass": True,
        },
        "wall_timing": {
            "scope": "read_state_to_shared_control_to_four_physics_substeps",
            "deadline_accounting": "compute_elapsed_and_absolute_release_deadline_v2",
            "paced": True,
            "samples": tick_count,
            "deadline_ms": 2.0,
            "p99_limit_ms": 1.8,
            "p50_ms": 1.0,
            "p95_ms": 1.1 if timing_pass else 1.9,
            "p99_ms": p99_ms,
            "max_ms": max_ms,
            "deadline_miss_count": absolute_misses,
            "compute_deadline_miss_count": compute_misses,
            "absolute_deadline_miss_count": absolute_misses,
            "release_lateness_ms": {
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
                "max_ms": 0.0,
            },
            "absolute_finish_lateness_ms": {
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0 if timing_pass else 0.1,
                "max_ms": 0.0 if timing_pass else 0.2,
            },
            "p99_within_limit": timing_pass,
            "max_within_deadline": timing_pass,
            "absolute_finish_within_deadline": timing_pass,
            "pass": timing_pass,
        },
        "faults": [
            {
                "id": fault,
                "passed": True,
                "exact_zero_command": True,
                "command_qdot": [0.0] * 6,
            }
            for fault in sorted(P0_REQUIRED_FAULTS)
        ],
    }


def manifest(*, final_timing_pass: bool = False) -> dict:
    rows = []
    for index, duration_s in enumerate((2.0, 10.0, 60.0)):
        rows.append(
            {
                "duration_s": duration_s,
                "sequence_index": index,
                "evidence_path": f"phase_{index}/evidence.json",
                "evidence_sha256": str(index + 6) * 64,
                "evidence_size_bytes": 1000 + index,
                "structurally_valid": True,
                "validation_blockers": [],
                "control_path_diagnostic_pass": True,
                "wall_timing_pass": final_timing_pass and index == 2,
            }
        )
    return {
        "schema": "step5d_p0_v8_mujoco_run_v1",
        "generated_at": "2026-07-11T08:00:00+00:00",
        "source_composite_sha256": SOURCE_SHA,
        "canonical_phase_sequence_complete": True,
        "phases": rows,
        "result": (
            "diagnostic_pass"
            if final_timing_pass
            else "control_diagnostic_pass_timing_blocked"
        ),
        "timing_gate": {"pass": final_timing_pass},
        "blockers": (
            ["geometry_provisional_no_p0_physics_claim"]
            if final_timing_pass
            else [
                "geometry_provisional_no_p0_physics_claim",
                "wall_timing_gate_failed_60s",
            ]
        ),
    }


def summary(*, final_timing_pass: bool = False) -> dict:
    evidence = [
        phase_evidence(index, duration, timing_pass=final_timing_pass and index == 2)
        for index, duration in enumerate((2.0, 10.0, 60.0))
    ]
    return build_diagnostic(
        manifest(final_timing_pass=final_timing_pass),
        evidence,
        run_manifest_binding={
            "path": "runs/final/run_manifest.json",
            "sha256": RUN_SHA,
            "size_bytes": 3700,
        },
        source_host="andy7",
        model_manifest_binding={
            "path": "runs/models/model_manifest.json",
            "sha256": MODEL_MANIFEST_SHA,
            "size_bytes": 17000,
        },
    )


def rehash(payload: dict) -> None:
    payload["diagnostic_sha256"] = canonical_sha256(
        {key: value for key, value in payload.items() if key != "diagnostic_sha256"}
    )


class Step5dP0V8OfflineDiagnosticTest(unittest.TestCase):
    def test_timing_failure_is_valid_diagnostic_not_p0_or_controller_pass(self) -> None:
        payload = summary()

        self.assertEqual(validate_diagnostic(payload), [])
        self.assertEqual(
            payload["diagnostic"]["source_result"],
            "control_diagnostic_pass_timing_blocked",
        )
        self.assertTrue(payload["diagnostic"]["all_control_paths_diagnostic_pass"])
        self.assertTrue(payload["diagnostic"]["all_required_faults_exact_zero"])
        self.assertFalse(payload["diagnostic"]["wall_timing_gate_pass"])
        self.assertEqual(payload["claims"], FALSE_CLAIMS)
        self.assertEqual(
            payload["state_projection"]["controller_canaries"],
            {"completed": [], "p0_v8_passed": False},
        )
        self.assertIn("offline_simulation_wall_timing_failed", payload["blockers"])

    def test_hashes_and_each_phase_metrics_are_bound(self) -> None:
        payload = summary()

        self.assertEqual(payload["source_binding"]["source_composite_sha256"], SOURCE_SHA)
        self.assertEqual(payload["source_binding"]["frame_lineage_sha256"], FRAME_SHA)
        self.assertEqual(payload["model_binding"]["model_sha256"], MODEL_SHA)
        self.assertEqual(payload["run_binding"]["sha256"], RUN_SHA)
        self.assertEqual(
            payload["model_binding"]["model_manifest"]["sha256"],
            MODEL_MANIFEST_SHA,
        )
        self.assertEqual([row["duration_s"] for row in payload["phases"]], [2.0, 10.0, 60.0])
        for row in payload["phases"]:
            self.assertTrue(row["control_path_diagnostic_pass"])
            self.assertGreater(row["wall_timing"]["compute_deadline_miss_count"], 0)
            self.assertGreater(row["wall_timing"]["absolute_deadline_miss_count"], 0)
            self.assertTrue(row["fault_injection"]["all_required_faults_exact_zero"])

    def test_simulator_summary_cannot_promote_claims_or_controller_canaries(self) -> None:
        payload = summary()
        payload["claims"]["p0_v8_passed"] = True
        payload["state_projection"]["controller_canaries"]["completed"] = [
            {"phase_s": 60.0}
        ]
        rehash(payload)

        blockers = validate_diagnostic(payload)

        self.assertIn("claims:must_all_remain_false", blockers)
        self.assertIn("state_projection:non_promotion_boundary_mismatch", blockers)

    def test_timing_pass_still_cannot_make_geometry_or_live_claims_true(self) -> None:
        payload = summary(final_timing_pass=True)

        self.assertEqual(validate_diagnostic(payload), [])
        self.assertTrue(payload["diagnostic"]["wall_timing_gate_pass"])
        self.assertEqual(payload["claims"], FALSE_CLAIMS)
        self.assertIn("geometry_provisional_no_p0_physics_claim", payload["blockers"])
        self.assertIn("controller_canaries_not_run", payload["blockers"])

    def test_unbound_canonical_state_is_fail_closed(self) -> None:
        binding = unbound_state_binding()

        self.assertEqual(validate_state_binding(binding), [])
        self.assertIsNone(binding["summary_artifact"])
        self.assertIsNone(binding["summary_sha256"])
        self.assertFalse(binding["controller_readback_verified"])
        self.assertFalse(binding["evidence_frozen"])
        self.assertEqual(binding["controller_canaries"]["completed"], [])

    def test_bound_state_keeps_simulator_and_controller_canaries_separate(self) -> None:
        payload = summary()
        binding = bound_state_binding(
            payload,
            summary_artifact="config/step5d_p0_v8_offline_simulation_diagnostic.json",
            summary_sha256="9" * 64,
        )

        self.assertEqual(validate_state_binding(binding, summary=payload), [])
        self.assertEqual(binding["status"], "bound_timing_blocked")
        self.assertEqual(binding["controller_canaries"]["completed"], [])
        self.assertFalse(binding["claims"]["p0_v8_passed"])
        self.assertIn("offline_simulation_wall_timing_failed", binding["blockers"])

    def test_unbound_state_rejects_hidden_simulator_promotion(self) -> None:
        binding = copy.deepcopy(unbound_state_binding())
        binding["controller_canaries"]["completed"] = [{"phase_s": 2.0}]
        binding["claims"]["p0_sim_physics_pass"] = True

        blockers = validate_state_binding(binding)

        self.assertIn("state_binding.controller_canaries:non_promotion_mismatch", blockers)
        self.assertIn("state_binding.claims:non_promotion_mismatch", blockers)


if __name__ == "__main__":
    unittest.main()
