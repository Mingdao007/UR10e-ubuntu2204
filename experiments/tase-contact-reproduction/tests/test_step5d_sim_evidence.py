#!/usr/bin/env python3
"""Tests for simulation evidence determinism and claim non-promotion."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from verify_step5d_sim_evidence import (  # noqa: E402
    P0_REQUIRED_FAULTS,
    source_composite_sha256,
    validate_evidence,
)


def payload() -> dict[str, object]:
    source: dict[str, object] = {
        "base_commit": "19f7dca",
        "head_commit": "19f7dca",
        "calibration_hash": "calib_7367377276742883610",
        "model_sha256": "1" * 64,
        "frame_lineage_sha256": "2" * 64,
    }
    source["composite_sha256"] = source_composite_sha256(source)
    return {
        "schema": "ur10e_simulation_evidence_v1",
        "generated_at": "2026-07-11T13:00:00+08:00",
        "profile": {
            "id": "step5d_strict_rnn_no_contact_p0_v8",
            "backend": "cupy",
            "inner_iterations": 1_024,
            "epsilon": 0.010,
            "sigr_exponent_r": 0.8,
            "qdot_cap_rad_s": 0.05,
            "effective_ko": 0.01,
            "dls_runtime_fallback_allowed": False,
        },
        "engine": {
            "name": "deterministic_stub",
            "version": "1",
            "lane": "unit",
            "physics_provenance": "deterministic_unit",
            "command_jacobian_source": "calibrated_pinocchio",
            "engine_oracle_is_command_source": False,
        },
        "source_binding": source,
        "schedule": {
            "physics_hz": 2_000,
            "control_hz": 500,
            "dbil_hz": 200,
            "integer_schedule": True,
            "sim_tick_miss_count": 0,
        },
        "phase": {
            "duration_s": 2.0,
            "sequence_index": 0,
            "same_fingerprint_as_previous": False,
        },
        "nominal": {
            "tick_count": 1_000,
            "accepted_tick_count": 1_000,
            "safe_hold_count": 0,
            "stop_count": 0,
            "missed_sequence_count": 0,
            "nonfinite_output_count": 0,
            "qdot_bound_violation_count": 0,
            "unexpected_contact_count": 0,
            "cage_collision_count": 0,
            "deadline_miss_count": 0,
            "max_qdot_abs_rad_s": 0.0004,
            "exact_zero_rejection_count": 0,
        },
        "faults": [
            {
                "id": fault,
                "passed": True,
                "exact_zero_command": True,
                "observed_reason": f"synthetic:{fault}",
            }
            for fault in sorted(P0_REQUIRED_FAULTS)
        ],
        "control_contract": {
            "path": "SimulatorState->Step5dObservation->StrictRnnControlPolicy->step5d_v30_contract_pipeline->SafetyEnvelope->RegisterCommand->SimulationCommand",
            "dls_shadow_only": True,
            "exact_zero_rejection": True,
            "same_production_code": True,
        },
        "claims": {
            "p0_sim_physics_pass": False,
            "p0_ursim_protocol_pass": False,
            "contact_sim_pass": False,
            "direct_torque_ursim_software_pass": False,
            "v30_offline_ready": False,
        },
        "claim_boundary": {
            "workflow_state": "liveprep_blocked",
            "current_program": "step5d_strict_rnn_ablation_v29",
            "v30_active": False,
            "live_motion_authorized": False,
            "package_accepted": False,
            "live_accepted": False,
            "reproduction_complete": False,
            "sim_pass_cannot_promote_live_state": True,
        },
        "artifacts": [],
        "blockers": [],
    }


class Step5dSimEvidenceTest(unittest.TestCase):
    def test_dependency_light_schema_file_is_valid_json(self) -> None:
        schema = json.loads(
            (ROOT / "config" / "schemas" / "ur10e_simulation_evidence_v1.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["claim_boundary"]["properties"]["live_accepted"]["const"], False)

    def test_valid_deterministic_packet_has_no_schema_or_claim_blockers(self) -> None:
        self.assertEqual(validate_evidence(payload()), [])

    def test_source_fingerprint_is_deterministic_and_invalidates_change(self) -> None:
        first = payload()
        second = payload()
        self.assertEqual(first["source_binding"], second["source_binding"])

        source = second["source_binding"]
        assert isinstance(source, dict)
        source["model_sha256"] = "3" * 64
        blockers = validate_evidence(second)

        self.assertIn("source_binding.composite_sha256:mismatch", blockers)

    def test_profile_drift_and_nominal_counter_block(self) -> None:
        changed = payload()
        profile = changed["profile"]
        nominal = changed["nominal"]
        assert isinstance(profile, dict) and isinstance(nominal, dict)
        profile["inner_iterations"] = 512
        nominal["missed_sequence_count"] = 1

        blockers = validate_evidence(changed)

        self.assertTrue(any("inner_iterations" in blocker for blocker in blockers))
        self.assertIn("nominal.missed_sequence_count:must_be_zero", blockers)

    def test_sim_pass_cannot_promote_package_live_or_reproduction(self) -> None:
        changed = payload()
        boundary = changed["claim_boundary"]
        assert isinstance(boundary, dict)
        boundary["package_accepted"] = True
        boundary["live_accepted"] = True
        boundary["reproduction_complete"] = True

        blockers = validate_evidence(changed)

        self.assertIn("claim_boundary.package_accepted:expected_False", blockers)
        self.assertIn("claim_boundary.live_accepted:expected_False", blockers)
        self.assertIn("claim_boundary.reproduction_complete:expected_False", blockers)

    def test_p0_physics_claim_requires_60s_calibrated_physics_engine(self) -> None:
        changed = payload()
        claims = changed["claims"]
        assert isinstance(claims, dict)
        claims["p0_sim_physics_pass"] = True

        blockers = validate_evidence(changed)

        self.assertIn("claims.p0_sim_physics_pass:requires_60s", blockers)
        self.assertIn("claims.p0_sim_physics_pass:wrong_engine", blockers)
        self.assertIn("claims.p0_sim_physics_pass:requires_calibrated_physics", blockers)

    def test_fault_matrix_must_be_complete_and_exact_zero(self) -> None:
        changed = payload()
        faults = changed["faults"]
        assert isinstance(faults, list)
        removed = faults.pop()
        faults[0]["exact_zero_command"] = False

        blockers = validate_evidence(changed)

        self.assertTrue(any(str(removed["id"]) in blocker and "missing" in blocker for blocker in blockers))
        self.assertTrue(any("not_exact_zero" in blocker for blocker in blockers))


if __name__ == "__main__":
    unittest.main()
