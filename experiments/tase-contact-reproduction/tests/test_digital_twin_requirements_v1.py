#!/usr/bin/env python3
"""Keep the tracked digital-twin coverage matrix honest and hash-bound."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class DigitalTwinRequirementsV1Test(unittest.TestCase):
    def test_p0_offline_result_is_bound_without_live_promotion(self) -> None:
        matrix = load(ROOT / "config" / "digital_twin_requirements_v1.json")
        summary_path = ROOT / "config" / "step5d_p0_v8_offline_simulation_diagnostic.json"
        summary = load(summary_path)
        rows = {row["id"]: row for row in matrix["requirements"]}
        p0 = rows["p0_v8_2_10_60"]

        self.assertEqual(matrix["workflow_state"], "liveprep_blocked")
        self.assertEqual(matrix["current_program"], "step5d_strict_rnn_ablation_v29")
        self.assertFalse(matrix["v30_active"])
        self.assertFalse(matrix["live_motion_authorized"])
        self.assertEqual(
            p0["status"],
            "offline_control_diagnostic_complete_controller_canaries_pending",
        )
        self.assertEqual(
            p0["summary_sha256"],
            hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        )
        self.assertTrue(p0["control_and_fault_path_pass"])
        self.assertTrue(p0["offline_control_timing_pass"])
        self.assertEqual(p0["final_60s_control_deadline_miss_count"], 0)
        self.assertFalse(p0["simulator_cycle_500hz_diagnostic_pass"])
        self.assertFalse(p0["p0_sim_physics_pass"])
        self.assertEqual(p0["controller_canaries_completed"], [])
        self.assertTrue(summary["diagnostic"]["all_control_paths_diagnostic_pass"])
        self.assertTrue(summary["diagnostic"]["all_required_faults_exact_zero"])
        self.assertTrue(summary["diagnostic"]["offline_control_timing_pass"])
        self.assertEqual(
            summary["diagnostic"]["timing_scope_status"],
            "current_control_hard_500hz_measurement_scope",
        )
        self.assertTrue(all(value is False for value in summary["claims"].values()))

    def test_engine_coverage_does_not_overstate_runtime_equivalence(self) -> None:
        matrix = load(ROOT / "config" / "digital_twin_requirements_v1.json")
        rows = {row["id"]: row for row in matrix["requirements"]}

        self.assertEqual(
            rows["gazebo_v2_native_contact"]["status"],
            "runtime_tooling_ready_execution_failed",
        )
        self.assertTrue(
            rows["gazebo_v2_native_contact"]["runtime_tooling_tests_pass"]
        )
        self.assertEqual(
            rows["gazebo_v2_native_contact"]["runtime_attempt_blocker"],
            "velocity_controller_not_ready_or_not_exclusive",
        )
        self.assertFalse(rows["gazebo_v2_native_contact"]["runtime_claim"])
        self.assertEqual(
            rows["cross_engine_and_domain_randomization"]["status"],
            "static_falsification_plan_ready_runtime_blocked",
        )
        self.assertEqual(
            rows["cross_engine_and_domain_randomization"]["scenario_count"],
            37,
        )
        self.assertEqual(
            rows["vic_simulation_active_dbil_shadow"]["status"],
            "offline_contracts_pass_dbil_shadow_only",
        )
        self.assertTrue(
            rows["vic_simulation_active_dbil_shadow"][
                "dbil_shadow_command_bitwise_inert"
            ]
        )
        self.assertFalse(rows["vic_simulation_active_dbil_shadow"]["dbil_active"])

    def test_v30_formal_timing_is_bound_but_not_a_live_claim(self) -> None:
        matrix = load(ROOT / "config" / "digital_twin_requirements_v1.json")
        rows = {row["id"]: row for row in matrix["requirements"]}
        timing = rows["v30_formal_host_timing"]
        raw_path = ROOT / timing["raw_artifact"]
        summary_path = ROOT / timing["summary_artifact"]
        summary = load(summary_path)

        self.assertEqual(
            timing["raw_sha256"], hashlib.sha256(raw_path.read_bytes()).hexdigest()
        )
        self.assertEqual(
            timing["summary_sha256"],
            hashlib.sha256(summary_path.read_bytes()).hexdigest(),
        )
        self.assertTrue(summary["overall_pass"])
        self.assertEqual(timing["compute_deadline_miss_count"], 0)
        self.assertEqual(timing["schedule_deadline_miss_count"], 0)
        self.assertIn(
            "live_runtime_prewarm_not_integrated_or_verified",
            timing["blockers"],
        )
        self.assertFalse(matrix["v30_active"])
        self.assertFalse(matrix["live_motion_authorized"])


if __name__ == "__main__":
    unittest.main()
