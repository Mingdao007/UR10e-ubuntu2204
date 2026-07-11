#!/usr/bin/env python3
"""Focused trace and chain tests for the MuJoCo P0 v8 verifier."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from step5d_simulator_adapter import simulation_claim_boundary  # noqa: E402
from step5d_control_contract import V30_DEFERRED_NUMERIC_FIELDS  # noqa: E402
from test_step5d_sim_evidence import payload as base_evidence  # noqa: E402
from verify_step5d_sim_evidence import source_composite_sha256  # noqa: E402
from verify_step5d_p0_v8_mujoco import (  # noqa: E402
    CONTROL_HARD_SCOPE,
    EVIDENCE_SCHEMA_V3,
    PREWARM_CONTROL_HZ,
    PREWARM_EXECUTE_TICKS,
    PREWARM_SCHEMA,
    RUN_SCHEMA_V3,
    SIMULATOR_CYCLE_SCOPE,
    TIMING_SCOPE_VERSION,
    TRACE_PREFAULT_STRATEGY,
    _trace_blockers,
    expected_prewarm_contract,
    expected_prewarm_profile,
    validate_run_manifest,
)


class Step5dP0V8MujocoVerifierTest(unittest.TestCase):
    @staticmethod
    def _bind_runtime_environment(evidence: dict[str, object]) -> str:
        source = evidence["source_binding"]
        assert isinstance(source, dict)
        source["runtime_timing_environment"] = {
            "paced_wall_clock": True,
            "process_affinity": {"available": True, "cpu_ids": [0]},
            "process_scheduler": {
                "available": True,
                "policy": 0,
                "policy_name": "SCHED_OTHER",
                "priority": 0,
            },
            "thread_environment": {
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
            },
            "versions": {
                "python": "3.10.0",
                "python_implementation": "CPython",
                "numpy": "1.24.4",
                "cupy": "13.6.0",
                "mujoco": "3.8.1",
            },
            "capabilities": {
                "busy_poll_completion": True,
                "pinned_host_staging": True,
                "dedicated_nonblocking_stream": True,
            },
        }
        source["composite_sha256"] = source_composite_sha256(source)
        return str(source["composite_sha256"])

    @staticmethod
    def _wall_timing(
        count: int, value_ms: float, *, paced: bool = True
    ) -> dict[str, object]:
        compute_misses = count if value_ms >= 2.0 else 0
        absolute_lateness_ms = max(0.0, value_ms - 2.0)
        absolute_misses = count if absolute_lateness_ms > 0.0 else 0
        p99_ok = value_ms <= 1.8
        max_ok = value_ms < 2.0
        return {
            "scope": "read_state_to_shared_control_to_four_physics_substeps",
            "deadline_accounting": "compute_elapsed_and_absolute_release_deadline_v2",
            "paced": paced,
            "samples": count,
            "deadline_ms": 2.0,
            "p99_limit_ms": 1.8,
            "p50_ms": value_ms,
            "p95_ms": value_ms,
            "p99_ms": value_ms,
            "max_ms": value_ms,
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
                "p50_ms": absolute_lateness_ms,
                "p95_ms": absolute_lateness_ms,
                "p99_ms": absolute_lateness_ms,
                "max_ms": absolute_lateness_ms,
            },
            "p99_within_limit": p99_ok,
            "max_within_deadline": max_ok,
            "absolute_finish_within_deadline": absolute_misses == 0,
            "pass": (
                paced
                and compute_misses == 0
                and absolute_misses == 0
                and p99_ok
                and max_ok
            ),
        }

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.trace = self.root / "control_trace.npz"
        count = 4
        np.savez_compressed(
            self.trace,
            sequence=np.arange(count, dtype=np.int64),
            sim_time_s=np.arange(count, dtype=float) * 0.002,
            compute_ms=np.full(count, 0.5),
            release_lateness_ms=np.zeros(count),
            absolute_finish_lateness_ms=np.zeros(count),
            qdot=np.zeros((count, 6)),
            command_jacobian=np.repeat(np.eye(6)[None, :, :], count, axis=0),
            desired_twist=np.repeat(
                np.asarray(((0.0, 0.0, 0.0001, 0.0, 0.0, 0.0),)),
                count,
                axis=0,
            ),
            reaction_normal=np.repeat(
                np.asarray(((0.0, 0.0, -1.0),)), count, axis=0
            ),
            approach_normal=np.repeat(
                np.asarray(((0.0, 0.0, 1.0),)), count, axis=0
            ),
            wrench=np.zeros((count, 6)),
            native_contact_count=np.zeros(count, dtype=np.int32),
            cage_collision_count=np.zeros(count, dtype=np.int32),
            tcp_inside_cage=np.ones(count, dtype=np.uint8),
            accepted=np.ones(count, dtype=np.uint8),
            action=np.asarray(["execute"] * count, dtype="<U96"),
            reason=np.asarray(["ok"] * count, dtype="<U160"),
            deferred_numeric=np.zeros((count, len(V30_DEFERRED_NUMERIC_FIELDS))),
            deferred_reason=np.asarray(["ok"] * count, dtype="<U160"),
            deferred_action=np.asarray(["execute"] * count, dtype="<U96"),
        )
        self.evidence = {
            "nominal": {
                "tick_count": count,
                "deadline_miss_count": 0,
                "compute_deadline_miss_count": 0,
                "absolute_deadline_miss_count": 0,
            },
            "wall_timing": self._wall_timing(count, 0.5),
            "artifacts": [
                {"role": "control_trace_npz", "path": self.trace.name}
            ],
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_trace_accepts_contiguous_exact_500hz_execution(self) -> None:
        self.assertEqual(_trace_blockers(self.evidence, self.root), [])

    def test_trace_detects_sequence_and_exact_execution_tamper(self) -> None:
        with np.load(self.trace, allow_pickle=False) as values:
            payload = {name: values[name] for name in values.files}
        payload["sequence"] = np.asarray((0, 1, 3, 4), dtype=np.int64)
        payload["accepted"] = np.asarray((1, 1, 0, 1), dtype=np.uint8)
        payload["reason"] = np.asarray(("ok", "ok", "hold", "ok"), dtype="<U160")
        np.savez_compressed(self.trace, **payload)

        blockers = _trace_blockers(self.evidence, self.root)

        self.assertIn("trace:sequence_not_contiguous", blockers)
        self.assertIn("trace:not_all_accepted", blockers)
        self.assertIn("trace:reason_not_all_ok", blockers)

    def test_trace_detects_deadline_and_qdot_tamper(self) -> None:
        with np.load(self.trace, allow_pickle=False) as values:
            payload = {name: values[name] for name in values.files}
        payload["compute_ms"][2] = 2.0
        payload["qdot"][1, 0] = 0.051
        np.savez_compressed(self.trace, **payload)

        blockers = _trace_blockers(self.evidence, self.root)

        self.assertIn(
            "trace:compute_deadline_miss_count:nominal_mismatch",
            blockers,
        )
        self.assertIn("trace:qdot_over_cap", blockers)

    def test_trace_detects_absolute_release_deadline_tamper(self) -> None:
        with np.load(self.trace, allow_pickle=False) as values:
            payload = {name: values[name] for name in values.files}
        payload["release_lateness_ms"][2] = 1.75
        payload["absolute_finish_lateness_ms"][2] = 0.25
        np.savez_compressed(self.trace, **payload)

        blockers = _trace_blockers(self.evidence, self.root)

        self.assertIn(
            "trace:absolute_deadline_miss_count:nominal_mismatch",
            blockers,
        )
        self.assertIn(
            "trace:wall_timing.absolute_finish_lateness_ms.max_ms:trace_mismatch",
            blockers,
        )

    def test_trace_enforces_full_tick_p99_budget(self) -> None:
        with np.load(self.trace, allow_pickle=False) as values:
            payload = {name: values[name] for name in values.files}
        payload["compute_ms"][:] = 1.81
        np.savez_compressed(self.trace, **payload)
        self.evidence["wall_timing"] = self._wall_timing(4, 1.81)

        blockers = _trace_blockers(
            self.evidence,
            self.root,
            require_timing_threshold=False,
        )

        self.assertEqual(blockers, [])
        claimed = _trace_blockers(
            self.evidence,
            self.root,
            require_timing_threshold=True,
        )
        self.assertIn(
            "trace:claimed_wall_timing_pass_but_threshold_failed",
            claimed,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _build_full_bundle(
        self, *, final_timing_pass: bool = True
    ) -> tuple[Path, dict[str, object]]:
        phases: list[dict[str, object]] = []
        seed = base_evidence()
        fingerprint = self._bind_runtime_environment(seed)
        for index, duration in enumerate((2.0, 10.0, 60.0)):
            phase_dir = self.root / f"phase_{index}_{int(duration)}s"
            phase_dir.mkdir()
            count = int(duration * 500)
            timing_value_ms = 0.5 if final_timing_pass or duration != 60.0 else 2.5
            timing = self._wall_timing(count, timing_value_ms)
            trace = phase_dir / "control_trace.npz"
            np.savez_compressed(
                trace,
                sequence=np.arange(count, dtype=np.int64),
                sim_time_s=np.arange(count, dtype=float) * 0.002,
                compute_ms=np.full(count, timing_value_ms),
                release_lateness_ms=np.zeros(count),
                absolute_finish_lateness_ms=np.full(
                    count,
                    max(0.0, timing_value_ms - 2.0),
                ),
                qdot=np.zeros((count, 6)),
                command_jacobian=np.repeat(np.eye(6)[None, :, :], count, axis=0),
                desired_twist=np.repeat(
                    np.asarray(((0.0, 0.0, 0.0001, 0.0, 0.0, 0.0),)),
                    count,
                    axis=0,
                ),
                reaction_normal=np.repeat(
                    np.asarray(((0.0, 0.0, -1.0),)), count, axis=0
                ),
                approach_normal=np.repeat(
                    np.asarray(((0.0, 0.0, 1.0),)), count, axis=0
                ),
                wrench=np.zeros((count, 6)),
                native_contact_count=np.zeros(count, dtype=np.int32),
                cage_collision_count=np.zeros(count, dtype=np.int32),
                tcp_inside_cage=np.ones(count, dtype=np.uint8),
                accepted=np.ones(count, dtype=np.uint8),
                action=np.asarray(["execute"] * count, dtype="<U96"),
                reason=np.asarray(["ok"] * count, dtype="<U160"),
                deferred_numeric=np.zeros((count, len(V30_DEFERRED_NUMERIC_FIELDS))),
                deferred_reason=np.asarray(["ok"] * count, dtype="<U160"),
                deferred_action=np.asarray(["execute"] * count, dtype="<U96"),
            )
            evidence = base_evidence()
            evidence["engine"] = {
                "name": "mujoco",
                "version": "fake",
                "lane": "p0_v8_no_contact_air_motion",
                "physics_provenance": "geometry_provisional",
                "command_jacobian_source": "calibrated_pinocchio",
                "engine_oracle_is_command_source": False,
            }
            source = evidence["source_binding"]
            assert isinstance(source, dict)
            self.assertEqual(
                self._bind_runtime_environment(evidence),
                fingerprint,
            )
            evidence["phase"] = {
                "duration_s": duration,
                "sequence_index": index,
                "same_fingerprint_as_previous": index > 0,
            }
            nominal = evidence["nominal"]
            assert isinstance(nominal, dict)
            nominal.update(
                {
                    "tick_count": count,
                    "accepted_tick_count": count,
                    "deadline_miss_count": timing["deadline_miss_count"],
                    "compute_deadline_miss_count": timing[
                        "compute_deadline_miss_count"
                    ],
                    "absolute_deadline_miss_count": timing[
                        "absolute_deadline_miss_count"
                    ],
                    "control_path_diagnostic_pass": True,
                    "sim_clock": {
                        "physics_tick_count": int(duration * 2_000),
                        "control_tick_count": count,
                        "dbil_tick_count": int(duration * 200),
                        "start_s": 0.0,
                        "end_s": duration,
                        "drift_s": 0.0,
                        "first_sequence": 0,
                        "last_sequence": count - 1,
                    },
                }
            )
            evidence["wall_timing"] = timing
            evidence["artifacts"] = [
                {
                    "role": "control_trace_npz",
                    "path": trace.name,
                    "sha256": self._sha256(trace),
                    "size_bytes": trace.stat().st_size,
                }
            ]
            faults = evidence["faults"]
            assert isinstance(faults, list)
            for fault in faults:
                fault["command_qdot"] = [0.0] * 6
            evidence_path = phase_dir / "evidence.json"
            evidence_path.write_text(
                json.dumps(evidence, sort_keys=True), encoding="utf-8"
            )
            phases.append(
                {
                    "duration_s": duration,
                    "sequence_index": index,
                    "evidence_path": str(evidence_path.relative_to(self.root)),
                    "evidence_sha256": self._sha256(evidence_path),
                    "evidence_size_bytes": evidence_path.stat().st_size,
                    "structurally_valid": True,
                    "validation_blockers": [],
                    "control_path_diagnostic_pass": True,
                    "compute_deadline_miss_count": timing[
                        "compute_deadline_miss_count"
                    ],
                    "absolute_deadline_miss_count": timing[
                        "absolute_deadline_miss_count"
                    ],
                    "wall_timing_pass": timing["pass"],
                }
            )
        phase_timing = [
            {
                "duration_s": row["duration_s"],
                "compute_deadline_miss_count": row[
                    "compute_deadline_miss_count"
                ],
                "absolute_deadline_miss_count": row[
                    "absolute_deadline_miss_count"
                ],
                "pass": row["wall_timing_pass"],
            }
            for row in phases
        ]
        final_pass = bool(phase_timing[-1]["pass"])
        manifest: dict[str, object] = {
            "schema": "step5d_p0_v8_mujoco_run_v1",
            "generated_at": "2026-07-11T00:00:00+00:00",
            "profile": base_evidence()["profile"],
            "source_composite_sha256": fingerprint,
            "canonical_phase_sequence_complete": True,
            "phases": phases,
            "timing_gate": {
                "scope": "separate_wall_timing_acceptance",
                "deadline_accounting": "compute_elapsed_and_absolute_release_deadline_v2",
                "required_phase_duration_s": 60.0,
                "deadline_ms": 2.0,
                "p99_limit_ms": 1.8,
                "requires_zero_compute_deadline_misses": True,
                "requires_zero_absolute_deadline_misses": True,
                "phase_results": phase_timing,
                "complete_sequence_evaluated": True,
                "pass": final_pass,
            },
            "result": (
                "diagnostic_pass"
                if final_pass
                else "control_diagnostic_pass_timing_blocked"
            ),
            "claims": {
                "p0_sim_physics_pass": False,
                "live_accepted": False,
                "reproduction_complete": False,
            },
            "claim_boundary": simulation_claim_boundary(),
            "blockers": [
                "geometry_provisional_no_p0_physics_claim",
                *([] if final_pass else ["wall_timing_gate_failed_60s"]),
            ],
        }
        return self.root / "run_manifest.json", manifest

    @staticmethod
    def _distribution(values: np.ndarray) -> dict[str, float]:
        return {
            "p50_ms": float(np.percentile(values, 50)),
            "p95_ms": float(np.percentile(values, 95)),
            "p99_ms": float(np.percentile(values, 99)),
            "max_ms": float(np.max(values)),
        }

    def _build_full_bundle_v2(self) -> tuple[Path, dict[str, object]]:
        manifest_path, manifest = self._build_full_bundle()
        phases = manifest["phases"]
        assert isinstance(phases, list)
        fingerprint: str | None = None
        control_rows: list[dict[str, object]] = []
        cycle_rows: list[dict[str, object]] = []
        for phase in phases:
            assert isinstance(phase, dict)
            evidence_path = self.root / str(phase["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            trace_path = evidence_path.parent / "control_trace.npz"
            with np.load(trace_path, allow_pickle=False) as trace:
                arrays = {name: trace[name] for name in trace.files}
            control = np.asarray(arrays.pop("compute_ms"), dtype=float)
            count = control.shape[0]
            oracle = np.full(count, 0.1)
            physics = np.full(count, 0.2)
            cycle = oracle + control + physics
            arrays.update(
                {
                    "control_compute_ms": control,
                    "oracle_snapshot_ms": oracle,
                    "command_apply_and_physics_ms": physics,
                    "cycle_wall_ms": cycle,
                }
            )
            np.savez_compressed(trace_path, **arrays)

            source = evidence["source_binding"]
            assert isinstance(source, dict)
            runtime = source["runtime_timing_environment"]
            assert isinstance(runtime, dict)
            runtime["timing_scope_contract"] = {
                "version": TIMING_SCOPE_VERSION,
                "control_hard_500hz": CONTROL_HARD_SCOPE,
                "simulator_cycle_diagnostic": SIMULATOR_CYCLE_SCOPE,
            }
            runtime["trace_prefault"] = {
                "required": True,
                "completed": True,
                "strategy": TRACE_PREFAULT_STRATEGY,
            }
            source["composite_sha256"] = source_composite_sha256(source)
            if fingerprint is None:
                fingerprint = str(source["composite_sha256"])
            self.assertEqual(source["composite_sha256"], fingerprint)

            control_timing = {
                "scope": CONTROL_HARD_SCOPE,
                "paced": True,
                "samples": count,
                "deadline_ms": 2.0,
                "p99_limit_ms": 1.8,
                **self._distribution(control),
                "deadline_miss_count": 0,
                "p99_within_limit": True,
                "max_within_deadline": True,
                "prefault_required": True,
                "prefault_verified": True,
                "pass": True,
            }
            cycle_timing = {
                "scope": SIMULATOR_CYCLE_SCOPE,
                "diagnostic_only": True,
                "samples": count,
                "physics_substeps_per_control_tick": 4,
                "oracle_snapshot_ms": self._distribution(oracle),
                "command_apply_and_physics_ms": self._distribution(physics),
                "cycle_wall_ms": self._distribution(cycle),
                "release_lateness_ms": self._distribution(
                    np.asarray(arrays["release_lateness_ms"], dtype=float)
                ),
                "absolute_finish_lateness_ms": self._distribution(
                    np.asarray(arrays["absolute_finish_lateness_ms"], dtype=float)
                ),
                "cycle_compute_deadline_miss_count": 0,
                "absolute_deadline_miss_count": 0,
                "meets_500hz_diagnostic": True,
            }
            evidence["schema"] = "ur10e_simulation_evidence_v2"
            evidence.pop("wall_timing")
            evidence["control_hard_500hz"] = control_timing
            evidence["simulator_cycle_diagnostic"] = cycle_timing
            nominal = evidence["nominal"]
            assert isinstance(nominal, dict)
            nominal.pop("deadline_miss_count")
            nominal.pop("compute_deadline_miss_count")
            nominal["control_deadline_miss_count"] = 0
            nominal["cycle_compute_deadline_miss_count"] = 0
            nominal["absolute_deadline_miss_count"] = 0
            contract = evidence["control_contract"]
            assert isinstance(contract, dict)
            contract["timing_scope_version"] = TIMING_SCOPE_VERSION
            contract["trace_buffers_prefaulted"] = True
            artifact = evidence["artifacts"][0]
            artifact["sha256"] = self._sha256(trace_path)
            artifact["size_bytes"] = trace_path.stat().st_size
            evidence_path.write_text(
                json.dumps(evidence, sort_keys=True), encoding="utf-8"
            )
            phase["evidence_sha256"] = self._sha256(evidence_path)
            phase["evidence_size_bytes"] = evidence_path.stat().st_size
            phase.pop("compute_deadline_miss_count")
            phase.pop("wall_timing_pass")
            phase["control_hard_500hz_pass"] = True
            phase["control_deadline_miss_count"] = 0
            phase["simulator_cycle_meets_500hz_diagnostic"] = True
            phase["cycle_compute_deadline_miss_count"] = 0
            phase["absolute_deadline_miss_count"] = 0
            duration = phase["duration_s"]
            control_rows.append(
                {"duration_s": duration, "deadline_miss_count": 0, "pass": True}
            )
            cycle_rows.append(
                {
                    "duration_s": duration,
                    "cycle_compute_deadline_miss_count": 0,
                    "absolute_deadline_miss_count": 0,
                    "meets_500hz_diagnostic": True,
                }
            )
        assert fingerprint is not None
        manifest["schema"] = "step5d_p0_v8_mujoco_run_v2"
        manifest["source_composite_sha256"] = fingerprint
        manifest.pop("timing_gate")
        manifest["control_hard_500hz_gate"] = {
            "scope": CONTROL_HARD_SCOPE,
            "required_phase_duration_s": 60.0,
            "deadline_ms": 2.0,
            "p99_limit_ms": 1.8,
            "requires_zero_deadline_misses": True,
            "requires_prefault": True,
            "phase_results": control_rows,
            "complete_sequence_evaluated": True,
            "pass": True,
        }
        manifest["simulator_cycle_diagnostic"] = {
            "scope": SIMULATOR_CYCLE_SCOPE,
            "diagnostic_only": True,
            "phase_results": cycle_rows,
        }
        manifest["result"] = "diagnostic_pass"
        manifest["blockers"] = ["geometry_provisional_no_p0_physics_claim"]
        return manifest_path, manifest

    def _build_full_bundle_v3(self) -> tuple[Path, dict[str, object]]:
        manifest_path, manifest = self._build_full_bundle_v2()
        phases = manifest["phases"]
        assert isinstance(phases, list)
        fingerprint: str | None = None
        evidence_rows: list[tuple[Path, dict[str, object], dict[str, object]]] = []
        for phase in phases:
            assert isinstance(phase, dict)
            evidence_path = self.root / str(phase["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            source = evidence["source_binding"]
            assert isinstance(source, dict)
            runtime = source["runtime_timing_environment"]
            assert isinstance(runtime, dict)
            runtime["production_path_prewarm_contract"] = (
                expected_prewarm_contract()
            )
            source["composite_sha256"] = source_composite_sha256(source)
            if fingerprint is None:
                fingerprint = str(source["composite_sha256"])
            self.assertEqual(source["composite_sha256"], fingerprint)
            evidence["schema"] = EVIDENCE_SCHEMA_V3
            control_contract = evidence["control_contract"]
            assert isinstance(control_contract, dict)
            control_contract.update(
                {
                    "measured_samples_excluded": 0,
                    "prewarm_samples_in_control_trace": 0,
                    "measured_sequence_restarts_at_zero": True,
                }
            )
            evidence_rows.append((evidence_path, evidence, phase))
        assert fingerprint is not None
        prewarm = {
            "schema": PREWARM_SCHEMA,
            "generated_at": "2026-07-12T00:00:00+00:00",
            "source_composite_sha256": fingerprint,
            "profile": expected_prewarm_profile(),
            "contract": expected_prewarm_contract(),
            "result": {
                "execute_tick_count": PREWARM_EXECUTE_TICKS,
                "accepted_tick_count": PREWARM_EXECUTE_TICKS,
                "safe_hold_count": 0,
                "stop_count": 0,
                "nonfinite_output_count": 0,
                "qdot_bound_violation_count": 0,
                "dls_shadow_count": PREWARM_EXECUTE_TICKS,
                "dls_runtime_fallback_count": 0,
                "register_command_generation_count": PREWARM_EXECUTE_TICKS,
                "command_sink_write_count": 0,
                "first_sequence": 0,
                "last_sequence": PREWARM_EXECUTE_TICKS - 1,
                "release_wait_count": PREWARM_EXECUTE_TICKS - 1,
                "deferred_diagnostic_count": PREWARM_EXECUTE_TICKS,
                "first_release_elapsed_s": 0.0,
                "last_release_elapsed_s": 1.998,
                "elapsed_release_span_s": 1.998,
                "min_inter_release_s": 0.002,
                "max_inter_release_s": 0.002,
                "burst_interval_count": 0,
                "pacing_hz": PREWARM_CONTROL_HZ,
                "paced": True,
                "unmeasured": True,
                "no_output": True,
                "timing_acceptance_eligible": False,
                "measured_sample_count": 0,
                "pass": True,
            },
            "reset": {
                "simulator_state_reset_after_prewarm": True,
                "solver_state_reset_after_prewarm": True,
                "control_adapter_discarded_after_prewarm": True,
                "measured_phase_first_sequence": 0,
                "post_reset_unmeasured_execute_tick_count": 0,
                "next_action": "measured_canonical_2_10_60_sequence",
            },
            "claim_boundary": {
                "prewarm_is_not_measured_timing_evidence": True,
                "prewarm_is_not_p0_pass": True,
                "prewarm_is_not_live_acceptance": True,
            },
        }
        prewarm_path = self.root / "production_path_prewarm.json"
        prewarm_path.write_text(
            json.dumps(prewarm, sort_keys=True),
            encoding="utf-8",
        )
        binding = {
            "schema": PREWARM_SCHEMA,
            "path": prewarm_path.name,
            "sha256": self._sha256(prewarm_path),
            "size_bytes": prewarm_path.stat().st_size,
            "source_composite_sha256": fingerprint,
            "execute_tick_count": PREWARM_EXECUTE_TICKS,
            "pacing_hz": PREWARM_CONTROL_HZ,
            "paced": True,
            "pass": True,
        }
        for evidence_path, evidence, phase in evidence_rows:
            evidence["prewarm_binding"] = dict(binding)
            evidence_path.write_text(
                json.dumps(evidence, sort_keys=True),
                encoding="utf-8",
            )
            phase["evidence_sha256"] = self._sha256(evidence_path)
            phase["evidence_size_bytes"] = evidence_path.stat().st_size
        manifest["schema"] = RUN_SCHEMA_V3
        manifest["source_composite_sha256"] = fingerprint
        manifest["production_path_prewarm"] = binding
        return manifest_path, manifest

    def _rebind_prewarm(
        self,
        manifest: dict[str, object],
        prewarm: dict[str, object],
    ) -> None:
        prewarm_path = self.root / "production_path_prewarm.json"
        prewarm_path.write_text(
            json.dumps(prewarm, sort_keys=True),
            encoding="utf-8",
        )
        binding = manifest["production_path_prewarm"]
        assert isinstance(binding, dict)
        binding["sha256"] = self._sha256(prewarm_path)
        binding["size_bytes"] = prewarm_path.stat().st_size
        phases = manifest["phases"]
        assert isinstance(phases, list)
        for index, phase in enumerate(phases):
            assert isinstance(phase, dict)
            evidence_path = self.root / str(phase["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["prewarm_binding"] = dict(binding)
            self._rebind_phase_evidence(manifest, index, evidence)

    def _rebind_phase_evidence(
        self,
        manifest: dict[str, object],
        index: int,
        evidence: dict[str, object],
    ) -> Path:
        phases = manifest["phases"]
        assert isinstance(phases, list) and isinstance(phases[index], dict)
        phase = phases[index]
        evidence_path = self.root / str(phase["evidence_path"])
        evidence_path.write_text(
            json.dumps(evidence, sort_keys=True), encoding="utf-8"
        )
        phase["evidence_sha256"] = self._sha256(evidence_path)
        phase["evidence_size_bytes"] = evidence_path.stat().st_size
        return evidence_path

    def test_full_chain_requires_one_fingerprint_and_claim_non_promotion(self) -> None:
        _path, manifest = self._build_full_bundle()

        self.assertEqual(
            validate_run_manifest(manifest, root=self.root, require_complete=True),
            [],
        )

    def test_full_chain_detects_phase_fingerprint_drift(self) -> None:
        _path, manifest = self._build_full_bundle()
        phases = manifest["phases"]
        assert isinstance(phases, list) and isinstance(phases[1], dict)
        evidence_path = self.root / str(phases[1]["evidence_path"])
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["source_binding"]["composite_sha256"] = "4" * 64
        evidence_path.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
        phases[1]["evidence_sha256"] = self._sha256(evidence_path)
        phases[1]["evidence_size_bytes"] = evidence_path.stat().st_size

        blockers = validate_run_manifest(manifest, root=self.root, require_complete=True)

        self.assertIn("phases[1].fingerprint:mismatch", blockers)

    def test_full_chain_control_pass_remains_valid_when_timing_is_blocked(self) -> None:
        _path, manifest = self._build_full_bundle(final_timing_pass=False)

        blockers = validate_run_manifest(
            manifest,
            root=self.root,
            require_complete=True,
        )

        self.assertEqual(blockers, [])
        self.assertEqual(
            manifest["result"],
            "control_diagnostic_pass_timing_blocked",
        )
        self.assertIn("wall_timing_gate_failed_60s", manifest["blockers"])

    def test_v2_full_chain_clean_fixture_passes(self) -> None:
        _path, manifest = self._build_full_bundle_v2()

        self.assertEqual(
            validate_run_manifest(manifest, root=self.root, require_complete=True),
            [],
        )

    def test_v3_full_chain_requires_bound_prewarm(self) -> None:
        _path, manifest = self._build_full_bundle_v3()

        self.assertEqual(
            validate_run_manifest(manifest, root=self.root, require_complete=True),
            [],
        )

        manifest.pop("production_path_prewarm")
        blockers = validate_run_manifest(
            manifest,
            root=self.root,
            require_complete=True,
        )
        self.assertIn(
            "production_path_prewarm:missing_or_not_object",
            blockers,
        )

    def test_v3_rejects_rehashed_prewarm_count_and_pacing_tamper(self) -> None:
        _path, manifest = self._build_full_bundle_v3()
        prewarm_path = self.root / "production_path_prewarm.json"
        prewarm = json.loads(prewarm_path.read_text(encoding="utf-8"))
        result = prewarm["result"]
        assert isinstance(result, dict)
        result["execute_tick_count"] = PREWARM_EXECUTE_TICKS - 1
        result["min_inter_release_s"] = 0.0
        result["burst_interval_count"] = 1
        self._rebind_prewarm(manifest, prewarm)

        blockers = validate_run_manifest(
            manifest,
            root=self.root,
            require_complete=True,
        )

        self.assertIn(
            "production_path_prewarm.result.execute_tick_count:invalid",
            blockers,
        )
        self.assertIn(
            "production_path_prewarm.result.burst_interval_count:invalid",
            blockers,
        )
        self.assertIn(
            "production_path_prewarm.result.inter_release_pacing:invalid",
            blockers,
        )

    def test_v3_rejects_measured_sample_discard_claim(self) -> None:
        _path, manifest = self._build_full_bundle_v3()
        phases = manifest["phases"]
        assert isinstance(phases, list) and isinstance(phases[0], dict)
        evidence_path = self.root / str(phases[0]["evidence_path"])
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["control_contract"]["measured_samples_excluded"] = 1
        self._rebind_phase_evidence(manifest, 0, evidence)

        blockers = validate_run_manifest(
            manifest,
            root=self.root,
            require_complete=True,
        )

        self.assertTrue(
            any(
                "control_contract.measured_samples_excluded:invalid" in item
                for item in blockers
            ),
            blockers,
        )

    def test_historical_cold_diagnostic_bytes_and_run_binding_are_immutable(self) -> None:
        path = (
            ROOT
            / "config"
            / "step5d_p0_v8_rnn512_91de37b_cold_start_v2_diagnostic.json"
        )
        payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(
            self._sha256(path),
            "9ee7d9995f630689f73a24f965a852a6528b55dfbf700dbfaf0f0dff1cc96093",
        )
        self.assertEqual(
            payload["run_binding"]["sha256"],
            "b4aa84309a699cd2d4543f0b6239d22a1777746e14ecdcb28afbd79b70aaaf89",
        )
        self.assertEqual(payload["phases"][0]["duration_s"], 2.0)
        self.assertEqual(
            payload["phases"][0]["control_hard_500hz"][
                "deadline_miss_count"
            ],
            25,
        )

    def test_v2_rejects_raw_control_timing_tamper_after_rebinding(self) -> None:
        _path, manifest = self._build_full_bundle_v2()
        phases = manifest["phases"]
        assert isinstance(phases, list) and isinstance(phases[0], dict)
        evidence_path = self.root / str(phases[0]["evidence_path"])
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        trace_path = evidence_path.parent / "control_trace.npz"
        with np.load(trace_path, allow_pickle=False) as trace:
            arrays = {name: trace[name].copy() for name in trace.files}
        arrays["control_compute_ms"][0] = 2.1
        np.savez_compressed(trace_path, **arrays)
        artifact = evidence["artifacts"][0]
        artifact["sha256"] = self._sha256(trace_path)
        artifact["size_bytes"] = trace_path.stat().st_size
        self._rebind_phase_evidence(manifest, 0, evidence)

        blockers = validate_run_manifest(
            manifest, root=self.root, require_complete=True
        )

        self.assertTrue(
            any("control_hard_500hz" in item and "trace_mismatch" in item for item in blockers),
            blockers,
        )

    def test_v2_rejects_scope_and_prefault_binding_tamper(self) -> None:
        _path, manifest = self._build_full_bundle_v2()
        phases = manifest["phases"]
        assert isinstance(phases, list) and isinstance(phases[0], dict)
        evidence_path = self.root / str(phases[0]["evidence_path"])
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        source = evidence["source_binding"]
        runtime = source["runtime_timing_environment"]
        runtime["timing_scope_contract"]["control_hard_500hz"] = "wrong_scope"
        runtime["trace_prefault"]["completed"] = False
        runtime["thread_environment"]["OPENBLAS_NUM_THREADS"] = "2"
        source["composite_sha256"] = source_composite_sha256(source)
        self._rebind_phase_evidence(manifest, 0, evidence)

        blockers = validate_run_manifest(
            manifest, root=self.root, require_complete=True
        )

        self.assertTrue(any("timing_scope_contract:invalid" in item for item in blockers), blockers)
        self.assertTrue(any("trace_prefault:invalid" in item for item in blockers), blockers)
        self.assertTrue(any("thread_environment:invalid" in item for item in blockers), blockers)

    def test_v2_run_rejects_mixed_v1_phase_evidence(self) -> None:
        _path, manifest = self._build_full_bundle_v2()
        phases = manifest["phases"]
        assert isinstance(phases, list) and isinstance(phases[0], dict)
        evidence_path = self.root / str(phases[0]["evidence_path"])
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        control = evidence.pop("control_hard_500hz")
        evidence.pop("simulator_cycle_diagnostic")
        evidence["schema"] = "ur10e_simulation_evidence_v1"
        evidence["wall_timing"] = self._wall_timing(
            int(control["samples"]), float(control["p50_ms"])
        )
        nominal = evidence["nominal"]
        nominal["deadline_miss_count"] = 0
        nominal["compute_deadline_miss_count"] = 0
        nominal["absolute_deadline_miss_count"] = 0
        trace_path = evidence_path.parent / "control_trace.npz"
        with np.load(trace_path, allow_pickle=False) as trace:
            arrays = {name: trace[name].copy() for name in trace.files}
        arrays["compute_ms"] = arrays.pop("control_compute_ms")
        arrays.pop("oracle_snapshot_ms")
        arrays.pop("command_apply_and_physics_ms")
        arrays.pop("cycle_wall_ms")
        np.savez_compressed(trace_path, **arrays)
        artifact = evidence["artifacts"][0]
        artifact["sha256"] = self._sha256(trace_path)
        artifact["size_bytes"] = trace_path.stat().st_size
        self._rebind_phase_evidence(manifest, 0, evidence)

        blockers = validate_run_manifest(
            manifest, root=self.root, require_complete=True
        )

        self.assertTrue(any("trace:missing_arrays" in item for item in blockers), blockers)


if __name__ == "__main__":
    unittest.main()
