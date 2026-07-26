#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
TOOLS = ROOT / "tools"
PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PACKAGE))

import build_p2_eoat_collision_inventory as p2_inventory  # noqa: E402
import build_p2_contact_correlation_audit as contact_audit  # noqa: E402
from ur10e_example_controllers import canonical_wrench_contract as contract  # noqa: E402


def _simulated_ft_observation() -> dict[str, object]:
    return {
        "schema": "ur10e_canonical_simulated_ft_runtime_observation_v1",
        "mode": "offline_ros2_runtime_observation",
        "claim_tier": "simulated_ft",
        "force_source": contract.SOURCE_SIMULATED_FT,
        "observed_counts": {
            "canonical_wrench": 5,
            "simulated_ft_wrench": 5,
            "simulated_ft_status": 5,
            "contact_state": 5,
            "controller_status": 5,
            "run_metadata": 1,
        },
        "evidence_fields_present": {
            "stamp": True,
            "frame_id": True,
            "source": True,
            "status": True,
            "baseline": True,
            "log_evidence": True,
        },
    }


def _contact_pair_log() -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_contact_pair_log_v1",
        "source": "gazebo_contact_plugin",
        "rows": [
            {
                "stamp_s": 1.0,
                "collision1": "real_aligned_eoat_visual_stack::eoat_contact_pad_collision",
                "collision2": "step5_contact_surface::surface::collision",
                "position_m": [0.0, 0.0, 0.008044839],
                "normal": [0.0, 0.0, 1.0],
                "contact_count": 2,
            }
        ],
    }


def _gazebo_contact_wrench_trace() -> dict[str, object]:
    samples = [
        contract.CanonicalWrenchSample(
            stamp_s=1.0,
            frame_id="base",
            force_n=(0.0, 0.0, 4.8),
            torque_nm=(0.0, 0.0, 0.0),
            source=contract.SOURCE_GAZEBO_CONTACT,
            valid=True,
            quality="nominal",
            status="valid",
            baseline_policy="simulated_zero_no_contact_baseline",
            latency_s=0.0,
            stale_after_s=0.1,
            diagnostic_flags=("gazebo_contact_wrench_adapter",),
            sequence=0,
            contact_state="contact",
        )
    ]
    return contract.trace_payload(samples, source_topic="/ur10e/contact/gazebo_contact/wrench")


def _adapter_verified_gazebo_contact_wrench_report() -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_contact_wrench_adapter_report_v1",
        "claim_tier": "physical Gazebo collision/contact physics",
        "target_claim_tier": "physical Gazebo collision/contact physics",
        "trace_written": True,
        "force_source": contract.SOURCE_GAZEBO_CONTACT,
        "source_contact_pair_row_count": 1,
        "native_wrench_row_count": 1,
        "verified_native_wrench_row_count": 1,
        "wrench_aggregation_policy": "single_native_contact_point_wrench_sample_no_total_contact_wrench_claim",
        "total_contact_wrench_proven": False,
        "blockers": [],
        "claim_boundary_gate": {
            "contact_pair_only_does_not_prove_wrench": True,
            "simulated_ft_is_not_physical_gazebo_contact": True,
            "real_bench_live_contact_authorized": False,
        },
        "wrench_trace": _gazebo_contact_wrench_trace(),
    }


def _adapter_unverified_gazebo_contact_wrench_report() -> dict[str, object]:
    return {
        "schema": "ur10e_gazebo_contact_wrench_adapter_report_v1",
        "claim_tier": "visual_only",
        "target_claim_tier": "physical Gazebo collision/contact physics",
        "trace_written": False,
        "force_source": None,
        "native_wrench_source_class": contract.SOURCE_GAZEBO_CONTACT,
        "source_contact_pair_row_count": 1,
        "native_wrench_row_count": 1,
        "verified_native_wrench_row_count": 0,
        "blockers": [
            "missing_base_frame_transform_evidence",
            "native_wrench_status_not_valid",
        ],
        "wrench_trace": None,
    }


class P2ContactCorrelationAuditTest(unittest.TestCase):
    def test_current_p2_collision_and_p1_simulated_ft_stay_blocked_without_contact_log(self) -> None:
        audit = contact_audit.build_audit(
            p2_inventory_payload=p2_inventory.build_inventory(generated_at="2026-06-21T02:30:00+08:00"),
            wrench_payload=_simulated_ft_observation(),
            contact_pair_payload=None,
            generated_at="2026-06-21T02:30:00+08:00",
        )

        self.assertEqual(audit["schema"], "ur10e_gazebo_p2_contact_correlation_audit_v1")
        self.assertEqual(audit["claim_tier"], "visual_only")
        self.assertEqual(audit["referenced_wrench_claim_tier"], "simulated_ft")
        self.assertTrue(audit["physical_gazebo_contact_gate"]["eoat_collision_body_audit_passed"])
        self.assertEqual(audit["physical_gazebo_contact_gate"]["eoat_collision_count"], 7)
        self.assertEqual(audit["p2_collision_inventory"]["evidence_scope"]["scope"], "collision_inventory_only")
        self.assertFalse(audit["p2_collision_inventory"]["runtime_contact_pair_log_evidence_evaluated_by_inventory"])
        self.assertFalse(audit["p2_collision_inventory"]["wrench_contact_correlation_evaluated_by_inventory"])
        self.assertFalse(audit["physical_gazebo_contact_gate"]["contact_pair_log_evidence"])
        self.assertFalse(audit["physical_gazebo_contact_gate"]["wrench_contact_correlation"])
        self.assertFalse(audit["physical_gazebo_contact_gate"]["force_contact_physics_proven"])
        self.assertEqual(audit["physical_gazebo_contact_gate"]["status"], "blocked_not_proven")
        self.assertIn("no_eoat_contact_pair_log_evidence", audit["known_blockers"])
        self.assertIn("wrench_source_not_gazebo_contact", audit["known_blockers"])

    def test_contact_pair_log_plus_simulated_ft_wrench_does_not_prove_physical_contact(self) -> None:
        audit = contact_audit.build_audit(
            p2_inventory_payload=p2_inventory.build_inventory(generated_at="2026-06-21T02:30:00+08:00"),
            wrench_payload=_simulated_ft_observation(),
            contact_pair_payload=_contact_pair_log(),
            generated_at="2026-06-21T02:30:00+08:00",
        )

        self.assertTrue(audit["physical_gazebo_contact_gate"]["contact_pair_log_evidence"])
        self.assertFalse(audit["physical_gazebo_contact_gate"]["wrench_contact_correlation"])
        self.assertFalse(audit["physical_gazebo_contact_gate"]["force_contact_physics_proven"])
        self.assertIn("wrench_source_not_gazebo_contact", audit["known_blockers"])

    def test_raw_gazebo_contact_wrench_trace_without_adapter_provenance_stays_blocked(self) -> None:
        audit = contact_audit.build_audit(
            p2_inventory_payload=p2_inventory.build_inventory(generated_at="2026-06-21T02:30:00+08:00"),
            wrench_payload=_gazebo_contact_wrench_trace(),
            contact_pair_payload=_contact_pair_log(),
            generated_at="2026-06-21T02:30:00+08:00",
        )

        self.assertEqual(audit["claim_tier"], "visual_only")
        self.assertFalse(audit["physical_gazebo_contact_gate"]["wrench_contact_correlation"])
        self.assertFalse(audit["physical_gazebo_contact_gate"]["force_contact_physics_proven"])
        self.assertIn("wrench_not_adapter_verified_gazebo_contact", audit["known_blockers"])

    def test_unverified_adapter_report_keeps_gazebo_source_but_stays_blocked(self) -> None:
        audit = contact_audit.build_audit(
            p2_inventory_payload=p2_inventory.build_inventory(generated_at="2026-06-21T02:30:00+08:00"),
            wrench_payload=_adapter_unverified_gazebo_contact_wrench_report(),
            contact_pair_payload=_contact_pair_log(),
            generated_at="2026-06-21T02:30:00+08:00",
        )

        self.assertEqual(audit["claim_tier"], "visual_only")
        self.assertTrue(audit["physical_gazebo_contact_gate"]["wrench_source_is_gazebo_contact"])
        self.assertFalse(audit["physical_gazebo_contact_gate"]["adapter_verified_gazebo_contact_wrench"])
        self.assertFalse(audit["physical_gazebo_contact_gate"]["force_contact_physics_proven"])
        self.assertFalse(audit["wrench_evidence"]["trace_written"])
        self.assertEqual(audit["wrench_evidence"]["native_wrench_row_count"], 1)
        self.assertEqual(audit["wrench_evidence"]["verified_native_wrench_row_count"], 0)
        self.assertIn("native_wrench_status_not_valid", audit["wrench_evidence"]["adapter_report_blockers"])
        self.assertIn("wrench_not_adapter_verified_gazebo_contact", audit["known_blockers"])
        self.assertNotIn("wrench_source_not_gazebo_contact", audit["known_blockers"])

    def test_adapter_verified_gazebo_contact_wrench_with_contact_pair_log_can_close_physical_gate(self) -> None:
        audit = contact_audit.build_audit(
            p2_inventory_payload=p2_inventory.build_inventory(generated_at="2026-06-21T02:30:00+08:00"),
            wrench_payload=_adapter_verified_gazebo_contact_wrench_report(),
            contact_pair_payload=_contact_pair_log(),
            generated_at="2026-06-21T02:30:00+08:00",
        )

        self.assertEqual(audit["claim_tier"], "physical Gazebo collision/contact physics")
        self.assertTrue(audit["physical_gazebo_contact_gate"]["contact_pair_log_evidence"])
        self.assertTrue(audit["physical_gazebo_contact_gate"]["wrench_contact_correlation"])
        self.assertTrue(audit["physical_gazebo_contact_gate"]["force_contact_physics_proven"])
        self.assertEqual(audit["physical_gazebo_contact_gate"]["status"], "proven")
        self.assertEqual(audit["known_blockers"], [])
        self.assertTrue(audit["wrench_evidence"]["trace_written"])
        self.assertEqual(audit["wrench_evidence"]["native_wrench_row_count"], 1)
        self.assertEqual(audit["wrench_evidence"]["verified_native_wrench_row_count"], 1)
        self.assertEqual(audit["wrench_evidence"]["adapter_report_blockers"], [])
        self.assertIn("single contact-point wrench", audit["allowed_claim"])
        self.assertFalse(audit["claim_boundary_gate"]["total_contact_wrench_proven"])

    def test_write_audit_creates_machine_readable_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p2_contact_correlation_test_") as tmp:
            tmp_path = Path(tmp)
            p2_path = p2_inventory.write_inventory(tmp_path / "p2", generated_at="2026-06-21T02:30:00+08:00")
            wrench_path = tmp_path / "simulated_ft_observation.json"
            wrench_path.write_text(json.dumps(_simulated_ft_observation(), indent=2), encoding="utf-8")

            path = contact_audit.write_audit(
                tmp_path / "audit",
                p2_inventory_path=p2_path,
                wrench_path=wrench_path,
                contact_pair_path=None,
                generated_at="2026-06-21T02:30:00+08:00",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(path.name, "p2_contact_correlation_audit.json")
        self.assertEqual(payload["artifact_path"], str(path))
        self.assertFalse(payload["physical_gazebo_contact_gate"]["force_contact_physics_proven"])


if __name__ == "__main__":
    unittest.main()
