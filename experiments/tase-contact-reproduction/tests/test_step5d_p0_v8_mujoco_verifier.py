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
from verify_step5d_p0_v8_mujoco import (  # noqa: E402
    _trace_blockers,
    validate_run_manifest,
)


class Step5dP0V8MujocoVerifierTest(unittest.TestCase):
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
            "nominal": {"tick_count": count, "deadline_miss_count": 0},
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

        self.assertIn("trace:deadline_count_mismatch", blockers)
        self.assertIn("trace:qdot_over_cap", blockers)

    def test_trace_enforces_full_tick_p99_budget(self) -> None:
        with np.load(self.trace, allow_pickle=False) as values:
            payload = {name: values[name] for name in values.files}
        payload["compute_ms"][:] = 1.81
        np.savez_compressed(self.trace, **payload)

        blockers = _trace_blockers(self.evidence, self.root)

        self.assertIn("trace:compute_p99_over_1p80ms", blockers)

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _build_full_bundle(self) -> tuple[Path, dict[str, object]]:
        phases: list[dict[str, object]] = []
        seed = base_evidence()
        seed_source = seed["source_binding"]
        assert isinstance(seed_source, dict)
        fingerprint = str(seed_source["composite_sha256"])
        for index, duration in enumerate((2.0, 10.0, 60.0)):
            phase_dir = self.root / f"phase_{index}_{int(duration)}s"
            phase_dir.mkdir()
            count = int(duration * 500)
            trace = phase_dir / "control_trace.npz"
            np.savez_compressed(
                trace,
                sequence=np.arange(count, dtype=np.int64),
                sim_time_s=np.arange(count, dtype=float) * 0.002,
                compute_ms=np.full(count, 0.5),
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
            self.assertEqual(source["composite_sha256"], fingerprint)
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
                }
            )
        manifest: dict[str, object] = {
            "schema": "step5d_p0_v8_mujoco_run_v1",
            "generated_at": "2026-07-11T00:00:00+00:00",
            "profile": base_evidence()["profile"],
            "source_composite_sha256": fingerprint,
            "canonical_phase_sequence_complete": True,
            "phases": phases,
            "result": "diagnostic_pass",
            "claims": {
                "p0_sim_physics_pass": False,
                "live_accepted": False,
                "reproduction_complete": False,
            },
            "claim_boundary": simulation_claim_boundary(),
            "blockers": ["geometry_provisional_no_p0_physics_claim"],
        }
        return self.root / "run_manifest.json", manifest

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


if __name__ == "__main__":
    unittest.main()
