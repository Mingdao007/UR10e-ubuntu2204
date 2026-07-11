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
    CONTROL_SCOPE_V2,
    CURRENT_TIMING_SCOPE_STATUS,
    EVIDENCE_SCHEMA_V1,
    EVIDENCE_SCHEMA_V2,
    EVIDENCE_SCHEMA_V3,
    EVIDENCE_SCHEMA_V4,
    FALSE_CLAIMS,
    HISTORICAL_TIMING_SCOPE_STATUS,
    NUMERIC_THREAD_ENV_CONTRACT_V2,
    P0_REQUIRED_FAULTS,
    PREFAULT_STRATEGY_V2,
    RUN_SCHEMA_V1,
    RUN_SCHEMA_V2,
    RUN_SCHEMA_V3,
    RUN_SCHEMA_V4,
    SIMULATOR_SCOPE_V2,
    TIMING_SCOPE_VERSION_V2,
    bound_state_binding,
    build_diagnostic,
    canonical_sha256,
    unbound_state_binding,
    validate_diagnostic,
    validate_state_binding,
)
from verify_step5d_p0_v8_mujoco import (  # noqa: E402
    expected_deadline_command_contract,
    expected_prewarm_contract,
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
        "schema": EVIDENCE_SCHEMA_V1,
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
        "schema": RUN_SCHEMA_V1,
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


def split_phase_evidence(
    index: int,
    duration_s: float,
    *,
    control_pass: bool = True,
    simulator_fast: bool = False,
    prewarm_binding: dict | None = None,
) -> dict:
    evidence = phase_evidence(index, duration_s)
    evidence["schema"] = EVIDENCE_SCHEMA_V2
    evidence.pop("wall_timing")
    evidence["source_binding"]["runtime_timing_environment"] = {
        "thread_environment": dict(NUMERIC_THREAD_ENV_CONTRACT_V2),
        "trace_prefault": {
            "required": True,
            "completed": True,
            "strategy": PREFAULT_STRATEGY_V2,
        },
        "timing_scope_contract": {
            "version": TIMING_SCOPE_VERSION_V2,
            "control_hard_500hz": CONTROL_SCOPE_V2,
            "simulator_cycle_diagnostic": SIMULATOR_SCOPE_V2,
        },
    }
    evidence["control_contract"] = {
        "timing_scope_version": TIMING_SCOPE_VERSION_V2,
        "trace_buffers_prefaulted": True,
    }
    ticks = int(duration_s * 500)
    control_misses = 0 if control_pass else 1
    evidence["nominal"].update(
        {
            "control_deadline_miss_count": control_misses,
            "cycle_compute_deadline_miss_count": 0 if simulator_fast else index + 1,
            "absolute_deadline_miss_count": 0 if simulator_fast else index + 2,
        }
    )
    evidence["control_hard_500hz"] = {
        "scope": CONTROL_SCOPE_V2,
        "paced": True,
        "samples": ticks,
        "deadline_ms": 2.0,
        "p99_limit_ms": 1.8,
        "p50_ms": 0.4,
        "p95_ms": 0.6,
        "p99_ms": 0.7 if control_pass else 1.9,
        "max_ms": 0.9 if control_pass else 2.1,
        "deadline_miss_count": control_misses,
        "p99_within_limit": control_pass,
        "max_within_deadline": control_pass,
        "prefault_required": True,
        "prefault_verified": True,
        "pass": control_pass,
    }
    slow = 0.8 if simulator_fast else 2.4 + index
    distribution = {
        "p50_ms": 0.2,
        "p95_ms": 0.4,
        "p99_ms": 0.6 if simulator_fast else slow - 0.1,
        "max_ms": 0.7 if simulator_fast else slow,
    }
    evidence["simulator_cycle_diagnostic"] = {
        "scope": SIMULATOR_SCOPE_V2,
        "diagnostic_only": True,
        "samples": ticks,
        "physics_substeps_per_control_tick": 4,
        "oracle_snapshot_ms": dict(distribution),
        "command_apply_and_physics_ms": dict(distribution),
        "cycle_wall_ms": dict(distribution),
        "release_lateness_ms": dict(distribution),
        "absolute_finish_lateness_ms": dict(distribution),
        "cycle_compute_deadline_miss_count": 0 if simulator_fast else index + 1,
        "absolute_deadline_miss_count": 0 if simulator_fast else index + 2,
        "meets_500hz_diagnostic": simulator_fast,
    }
    if prewarm_binding is not None:
        evidence["schema"] = EVIDENCE_SCHEMA_V3
        evidence["prewarm_binding"] = copy.deepcopy(prewarm_binding)
        evidence["source_binding"]["runtime_timing_environment"][
            "production_path_prewarm_contract"
        ] = expected_prewarm_contract()
        evidence["control_contract"].update(
            {
                "measured_samples_excluded": 0,
                "prewarm_samples_in_control_trace": 0,
                "measured_sequence_restarts_at_zero": True,
            }
        )
    return evidence


def split_summary(
    *,
    final_control_pass: bool = True,
    with_prewarm: bool = False,
) -> dict:
    prewarm_binding = (
        {
            "schema": "step5d_p0_v8_production_path_prewarm_v1",
            "path": "production_path_prewarm.json",
            "sha256": "9" * 64,
            "size_bytes": 2_000,
            "source_composite_sha256": SOURCE_SHA,
            "execute_tick_count": 1_000,
            "pacing_hz": 500,
            "paced": True,
            "pass": True,
        }
        if with_prewarm
        else None
    )
    evidence = [
        split_phase_evidence(
            index,
            duration,
            control_pass=final_control_pass or index < 2,
            prewarm_binding=prewarm_binding,
        )
        for index, duration in enumerate((2.0, 10.0, 60.0))
    ]
    control_results = [
        {
            "duration_s": duration,
            "deadline_miss_count": row["control_hard_500hz"]["deadline_miss_count"],
            "pass": row["control_hard_500hz"]["pass"],
        }
        for duration, row in zip((2.0, 10.0, 60.0), evidence)
    ]
    simulator_results = [
        {
            "duration_s": duration,
            "cycle_compute_deadline_miss_count": row["simulator_cycle_diagnostic"]["cycle_compute_deadline_miss_count"],
            "absolute_deadline_miss_count": row["simulator_cycle_diagnostic"]["absolute_deadline_miss_count"],
            "meets_500hz_diagnostic": False,
        }
        for duration, row in zip((2.0, 10.0, 60.0), evidence)
    ]
    rows = [
        {
            "duration_s": duration,
            "sequence_index": index,
            "evidence_path": f"phase_{index}/evidence.json",
            "evidence_sha256": str(index + 6) * 64,
            "evidence_size_bytes": 1000 + index,
            "structurally_valid": True,
            "validation_blockers": [],
            "control_path_diagnostic_pass": True,
        }
        for index, duration in enumerate((2.0, 10.0, 60.0))
    ]
    manifest_v2 = {
        "schema": RUN_SCHEMA_V3 if with_prewarm else RUN_SCHEMA_V2,
        "generated_at": "2026-07-12T08:00:00+00:00",
        "source_composite_sha256": SOURCE_SHA,
        "canonical_phase_sequence_complete": True,
        "phases": rows,
        "result": (
            "diagnostic_pass"
            if final_control_pass
            else "control_diagnostic_pass_control_hard_500hz_blocked"
        ),
        "control_hard_500hz_gate": {
            "scope": CONTROL_SCOPE_V2,
            "required_phase_duration_s": 60.0,
            "deadline_ms": 2.0,
            "p99_limit_ms": 1.8,
            "requires_zero_deadline_misses": True,
            "requires_prefault": True,
            "phase_results": control_results,
            "complete_sequence_evaluated": True,
            "pass": final_control_pass,
        },
        "simulator_cycle_diagnostic": {
            "scope": SIMULATOR_SCOPE_V2,
            "diagnostic_only": True,
            "phase_results": simulator_results,
        },
        "blockers": ["geometry_provisional_no_p0_physics_claim"],
    }
    if prewarm_binding is not None:
        manifest_v2["production_path_prewarm"] = prewarm_binding
    return build_diagnostic(
        manifest_v2,
        evidence,
        run_manifest_binding={"path": "runs/v2/run_manifest.json", "sha256": RUN_SHA, "size_bytes": 3700},
        source_host="andy7",
        model_manifest_binding={"path": "runs/models/model_manifest.json", "sha256": MODEL_MANIFEST_SHA, "size_bytes": 17000},
    )


def v4_summary(*, misses: tuple[int, int, int] = (40, 369, 198)) -> dict:
    prewarm_binding = {
        "schema": "step5d_p0_v8_production_path_prewarm_v1",
        "path": "production_path_prewarm.json",
        "sha256": "9" * 64,
        "size_bytes": 2_000,
        "source_composite_sha256": SOURCE_SHA,
        "execute_tick_count": 1_000,
        "pacing_hz": 500,
        "paced": True,
        "pass": True,
    }
    evidence = []
    for index, (duration, miss_count) in enumerate(
        zip((2.0, 10.0, 60.0), misses)
    ):
        row = split_phase_evidence(
            index,
            duration,
            control_pass=False,
            prewarm_binding=prewarm_binding,
        )
        row["schema"] = EVIDENCE_SCHEMA_V4
        runtime = row["source_binding"]["runtime_timing_environment"]
        runtime["deadline_command_contract"] = expected_deadline_command_contract()
        row["control_contract"].update(
            {
                "deadline_command_contract_version": "pre_write_exact_zero_stop_v1",
                "deadline_classification_point": (
                    "after_adapter_step_before_plant_write"
                ),
                "deadline_comparison": "control_elapsed_ms_gte_deadline",
                "deadline_ms": 2.0,
                "deadline_rejection_action": "stop",
                "deadline_rejection_reason": "control_deadline_miss_ge_2ms",
                "deadline_rejection_qdot": [0.0] * 6,
                "deadline_rejection_cmd_valid": False,
                "deadline_rejection_stop_request": True,
                "deadline_rejection_may_satisfy_timing_gate": False,
            }
        )
        tick_count = int(duration * 500)
        row["nominal"].update(
            {
                "accepted_tick_count": tick_count - miss_count,
                "stop_count": miss_count,
                "control_deadline_miss_count": miss_count,
                "exact_zero_rejection_count": miss_count,
                "deadline_rejection_count": miss_count,
                "deadline_zero_rejection_count": miss_count,
                "nonzero_rejection_count": 0,
                "control_path_diagnostic_pass": True,
            }
        )
        row["control_hard_500hz"].update(
            {
                "p50_ms": 0.5,
                "p95_ms": 0.7,
                "p99_ms": 0.755,
                "max_ms": 4.0 + index,
                "deadline_miss_count": miss_count,
                "p99_within_limit": True,
                "max_within_deadline": False,
                "pass": False,
            }
        )
        evidence.append(row)
    phase_results = [
        {
            "duration_s": duration,
            "deadline_miss_count": miss_count,
            "pass": False,
        }
        for duration, miss_count in zip((2.0, 10.0, 60.0), misses)
    ]
    simulator_results = [
        {
            "duration_s": duration,
            "cycle_compute_deadline_miss_count": row[
                "simulator_cycle_diagnostic"
            ]["cycle_compute_deadline_miss_count"],
            "absolute_deadline_miss_count": row[
                "simulator_cycle_diagnostic"
            ]["absolute_deadline_miss_count"],
            "meets_500hz_diagnostic": False,
        }
        for duration, row in zip((2.0, 10.0, 60.0), evidence)
    ]
    rows = [
        {
            "duration_s": duration,
            "sequence_index": index,
            "evidence_path": f"phase_{index}/evidence.json",
            "evidence_sha256": str(index + 6) * 64,
            "evidence_size_bytes": 1000 + index,
            "structurally_valid": True,
            "validation_blockers": [],
            "control_path_diagnostic_pass": True,
        }
        for index, duration in enumerate((2.0, 10.0, 60.0))
    ]
    manifest_v4 = {
        "schema": RUN_SCHEMA_V4,
        "generated_at": "2026-07-12T08:00:00+00:00",
        "source_composite_sha256": SOURCE_SHA,
        "canonical_phase_sequence_complete": True,
        "production_path_prewarm": prewarm_binding,
        "phases": rows,
        "result": "control_diagnostic_pass_control_hard_500hz_blocked",
        "control_hard_500hz_gate": {
            "scope": CONTROL_SCOPE_V2,
            "required_phase_duration_s": 60.0,
            "deadline_ms": 2.0,
            "p99_limit_ms": 1.8,
            "requires_zero_deadline_misses": True,
            "requires_prefault": True,
            "phase_results": phase_results,
            "complete_sequence_evaluated": True,
            "pass": False,
        },
        "simulator_cycle_diagnostic": {
            "scope": SIMULATOR_SCOPE_V2,
            "diagnostic_only": True,
            "phase_results": simulator_results,
        },
        "blockers": [
            "geometry_provisional_no_p0_physics_claim",
            "control_hard_500hz_gate_failed_60s",
        ],
    }
    return build_diagnostic(
        manifest_v4,
        evidence,
        run_manifest_binding={
            "path": "runs/v4/run_manifest.json",
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
        self.assertFalse(payload["diagnostic"]["offline_control_timing_pass"])
        self.assertEqual(
            payload["diagnostic"]["timing_scope_status"],
            HISTORICAL_TIMING_SCOPE_STATUS,
        )
        self.assertEqual(payload["claims"], FALSE_CLAIMS)
        self.assertEqual(
            payload["state_projection"]["controller_canaries"],
            {"completed": [], "p0_v8_passed": False},
        )
        self.assertIn("current_control_timing_evidence_missing", payload["blockers"])

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
        self.assertTrue(payload["diagnostic"]["historical_combined_scope_timing_pass"])
        self.assertFalse(payload["diagnostic"]["offline_control_timing_pass"])
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
        self.assertEqual(binding["status"], "bound_timing_scope_superseded")
        self.assertEqual(binding["controller_canaries"]["completed"], [])
        self.assertFalse(binding["claims"]["p0_v8_passed"])
        self.assertIn("current_control_timing_evidence_missing", binding["blockers"])

    def test_v2_control_pass_is_current_even_when_simulator_cycle_is_slow(self) -> None:
        payload = split_summary()

        self.assertEqual(validate_diagnostic(payload), [])
        self.assertEqual(
            payload["diagnostic"]["timing_scope_status"],
            CURRENT_TIMING_SCOPE_STATUS,
        )
        self.assertTrue(payload["diagnostic"]["offline_control_timing_pass"])
        self.assertFalse(
            payload["phases"][2]["simulator_cycle_diagnostic"][
                "meets_500hz_diagnostic"
            ]
        )
        self.assertNotIn("offline_control_timing_failed", payload["blockers"])

    def test_v2_control_failure_blocks_offline_control_timing(self) -> None:
        payload = split_summary(final_control_pass=False)

        self.assertEqual(validate_diagnostic(payload), [])
        self.assertFalse(payload["diagnostic"]["offline_control_timing_pass"])
        self.assertIn("offline_control_timing_failed", payload["blockers"])

    def test_v3_prewarm_binding_survives_offline_projection(self) -> None:
        payload = split_summary(with_prewarm=True)

        self.assertEqual(validate_diagnostic(payload), [])
        self.assertEqual(
            payload["timing_evidence"]["run_manifest_schema"],
            RUN_SCHEMA_V3,
        )
        self.assertTrue(
            payload["timing_evidence"]["production_path_prewarm"]["pass"]
        )
        self.assertTrue(
            all(
                row["prewarm_binding"]
                == payload["timing_evidence"]["production_path_prewarm"]
                for row in payload["phases"]
            )
        )

        payload["phases"][0]["timing_scope_binding"][
            "measured_samples_excluded"
        ] = 1
        rehash(payload)
        self.assertIn(
            "phases[0].prewarm_trace_boundary:invalid",
            validate_diagnostic(payload),
        )

    def test_v4_deadline_absorption_is_control_pass_but_timing_blocked(self) -> None:
        payload = v4_summary()

        self.assertEqual(validate_diagnostic(payload), [])
        self.assertEqual(
            payload["diagnostic"]["source_result"],
            "control_diagnostic_pass_control_hard_500hz_blocked",
        )
        self.assertTrue(payload["diagnostic"]["all_control_paths_diagnostic_pass"])
        self.assertFalse(payload["diagnostic"]["offline_control_timing_pass"])
        self.assertIn("offline_control_timing_failed", payload["blockers"])
        self.assertEqual(
            bound_state_binding(
                payload,
                summary_artifact="config/p0-v8-v4.json",
                summary_sha256="a" * 64,
            )["status"],
            "bound_timing_blocked",
        )
        for row, misses in zip(payload["phases"], (40, 369, 198)):
            counters = row["control_counters"]
            self.assertEqual(
                counters["accepted_tick_count"],
                counters["tick_count"] - misses,
            )
            self.assertEqual(counters["stop_count"], misses)
            self.assertEqual(counters["exact_zero_rejection_count"], misses)
            self.assertEqual(counters["deadline_rejection_count"], misses)
            self.assertEqual(counters["deadline_zero_rejection_count"], misses)
            self.assertEqual(counters["nonzero_rejection_count"], 0)
            self.assertTrue(row["control_path_diagnostic_pass"])
            self.assertFalse(row["control_hard_500hz"]["pass"])

    def test_v4_rejects_nonzero_or_mismatched_deadline_projection(self) -> None:
        payload = v4_summary()
        payload["phases"][2]["control_counters"]["nonzero_rejection_count"] = 1
        payload["phases"][2]["control_counters"]["accepted_tick_count"] += 1
        rehash(payload)

        blockers = validate_diagnostic(payload)

        self.assertIn(
            "phases[2].deadline_fail_closed_counters:mismatch",
            blockers,
        )

    def test_v4_fail_closed_absorption_cannot_promote_timing_pass(self) -> None:
        payload = v4_summary()
        payload["diagnostic"]["offline_control_timing_pass"] = True
        rehash(payload)

        self.assertIn("diagnostic:projection_mismatch", validate_diagnostic(payload))

    def test_v1_pass_never_satisfies_current_control_timing(self) -> None:
        payload = summary(final_timing_pass=True)

        self.assertEqual(validate_diagnostic(payload), [])
        self.assertEqual(
            payload["diagnostic"]["timing_scope_status"],
            HISTORICAL_TIMING_SCOPE_STATUS,
        )
        self.assertFalse(payload["diagnostic"]["offline_control_timing_pass"])

    def test_v2_prefault_or_source_tamper_fails_closed(self) -> None:
        payload = split_summary()
        payload["phases"][1]["timing_scope_binding"]["trace_prefault"][
            "completed"
        ] = False
        payload["phases"][1]["timing_scope_binding"][
            "numeric_thread_environment"
        ]["OMP_NUM_THREADS"] = "2"
        payload["phases"][2]["source_composite_sha256"] = "a" * 64
        rehash(payload)

        blockers = validate_diagnostic(payload)

        self.assertIn("phases[1].timing_scope_binding:invalid", blockers)
        self.assertIn("phases[2].source_composite_sha256:mismatch", blockers)

    def test_v2_simulator_realtime_claim_must_match_each_miss_counter(self) -> None:
        for cycle_misses, absolute_misses in ((1, 0), (0, 1)):
            with self.subTest(
                cycle_misses=cycle_misses,
                absolute_misses=absolute_misses,
            ):
                payload = split_summary()
                phase = payload["phases"][2]
                simulator = phase["simulator_cycle_diagnostic"]
                counters = phase["control_counters"]
                manifest_projection = payload["timing_evidence"][
                    "simulator_cycle_diagnostic"
                ]["phase_results"][2]
                for target in (simulator, counters, manifest_projection):
                    target["cycle_compute_deadline_miss_count"] = cycle_misses
                    target["absolute_deadline_miss_count"] = absolute_misses
                simulator["meets_500hz_diagnostic"] = True
                manifest_projection["meets_500hz_diagnostic"] = True
                rehash(payload)

                blockers = validate_diagnostic(payload)

                self.assertIn(
                    "phases[2].simulator_cycle_diagnostic:inconsistent",
                    blockers,
                )

    def test_unbound_state_rejects_hidden_simulator_promotion(self) -> None:
        binding = copy.deepcopy(unbound_state_binding())
        binding["controller_canaries"]["completed"] = [{"phase_s": 2.0}]
        binding["claims"]["p0_sim_physics_pass"] = True

        blockers = validate_state_binding(binding)

        self.assertIn("state_binding.controller_canaries:non_promotion_mismatch", blockers)
        self.assertIn("state_binding.claims:non_promotion_mismatch", blockers)


if __name__ == "__main__":
    unittest.main()
