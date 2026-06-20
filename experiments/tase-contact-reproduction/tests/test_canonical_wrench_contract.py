#!/usr/bin/env python3
from __future__ import annotations

import csv
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
sys.path.insert(0, str(WORKSPACE / "src" / "ur10e_example_controllers"))

from ur10e_example_controllers import canonical_wrench_contract as contract  # noqa: E402
from ur10e_example_controllers import step5b_contact_control_core as core  # noqa: E402
from ur10e_example_controllers import step5b_simulation_mvp as step5b_mvp  # noqa: E402
from ur10e_example_controllers import step56_simulation_matrix as step56  # noqa: E402


class CanonicalWrenchContractTest(unittest.TestCase):
    def test_contract_spec_has_required_topics_frames_and_sources(self) -> None:
        spec = contract.canonical_contract_spec()
        self.assertEqual(spec["canonical_wrench_topic"], "/ur10e/contact/canonical_wrench")
        self.assertIn("/joint_states", spec["required_input_topics"])
        self.assertIn("/tf", spec["required_input_topics"])
        self.assertIn("/tf_static", spec["required_input_topics"])
        self.assertIn("/clock", spec["required_input_topics"])
        for frame in [
            "world",
            "base",
            "base_link",
            "tool0",
            "flange",
            "ft_sensor",
            "tcp",
            "contact_tip",
            "contact_surface",
            "surface_normal",
        ]:
            self.assertIn(frame, spec["required_frames"])
        self.assertEqual(set(spec["source_classes"]), set(contract.SOURCE_CLASSES))
        self.assertEqual(spec["consumer_interface"]["consumer_frame_id"], "base")
        self.assertFalse(spec["consumer_interface"]["controller_private_gazebo_topic_dependency_allowed"])

    def test_schema_validator_rejects_missing_required_fields(self) -> None:
        sample = contract.simulated_ft_sample(
            stamp_s=1.0,
            sequence=0,
            force_n=(0.0, 0.0, 5.0),
        )
        row = sample.to_row()
        self.assertEqual(contract.validate_canonical_sample_row(row), [])
        row.pop("header")
        issues = contract.validate_canonical_sample_row(row)
        self.assertIn("missing:header", issues)
        self.assertIn("header:not_object", issues)

    def test_simulated_ft_trace_rows_carry_required_envelope(self) -> None:
        artifact = step5b_mvp.build_artifact()
        trace = artifact["simulated_force_evidence"]
        self.assertEqual(trace["schema"], contract.TRACE_SCHEMA)
        self.assertEqual(trace["force_source"], contract.SOURCE_SIMULATED_FT)
        self.assertFalse(trace["schema_issues"])
        first = trace["rows"][0]
        for field in contract.REQUIRED_SAMPLE_FIELDS:
            self.assertIn(field, first)
        self.assertEqual(first["header"]["frame_id"], "base")
        self.assertEqual(first["source"], contract.SOURCE_SIMULATED_FT)
        self.assertEqual(first["baseline_policy"], "simulated_zero_no_contact_baseline")
        self.assertEqual(first["claim_tier"], "simulated_ft")

    def test_no_contact_static_trace_has_no_phantom_load(self) -> None:
        rows = [
            {"t_s": 0.0, "tcp_z_m": step5b_mvp.CONTACT_SURFACE_Z_M + 0.05},
            {"t_s": 0.1, "tcp_z_m": step5b_mvp.CONTACT_SURFACE_Z_M + 0.05},
        ]
        trace = contract.simulated_ft_trace_from_rows(
            rows,
            contact_surface_z_m=step5b_mvp.CONTACT_SURFACE_Z_M,
            nominal_contact_load_n=0.0,
        )
        self.assertEqual(trace["max_force_norm_n"], 0.0)
        self.assertEqual(trace["max_normal_load_n"], 0.0)
        self.assertEqual({row["contact_state"] for row in trace["rows"]}, {"no_contact"})
        for row in trace["rows"]:
            self.assertIn("no_contact_static_baseline", row["diagnostic_flags"])

    def test_contact_trace_uses_reaction_and_approach_semantics(self) -> None:
        artifact = step56.build_stage_artifact("step6b")
        trace = artifact["simulated_force_evidence"]
        self.assertEqual(trace["force_source"], contract.SOURCE_SIMULATED_FT)
        self.assertEqual(trace["reaction_normal"], [0.0, 0.0, 1.0])
        self.assertEqual(trace["approach_normal"], [0.0, 0.0, -1.0])
        self.assertGreater(trace["max_normal_load_n"], 0.0)
        self.assertTrue(artifact["acceptance"]["canonical_wrench_schema_ok"])

    def test_stale_wrench_produces_hold_status_not_silent_ready(self) -> None:
        fresh = contract.simulated_ft_sample(
            stamp_s=10.0,
            sequence=1,
            force_n=(0.0, 0.0, 5.0),
        )
        status = contract.controller_status_from_canonical(fresh, now_s=10.5)
        self.assertEqual(status["status"], "hold")
        self.assertEqual(status["reason"], "stale")
        self.assertIn("stale_wrench", status["diagnostic_flags"])
        stale = fresh.with_freshness(now_s=10.5)
        sample = contract.step5b_sample_from_canonical_wrench(
            stale,
            tcp_pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            robot_stage=25.0,
            dt_s=0.002,
        )
        self.assertEqual(sample.sensor_ok, 0.0)

    def test_retained_kunwei_log_drives_same_canonical_step5b_sample_path(self) -> None:
        csv_path = (
            ROOT
            / "runs"
            / "bridge_step4e_line_outerloop_v9_autowatch_20260609_134652"
            / "kunwei_sensor_1khz.csv"
        )
        with csv_path.open(newline="", encoding="utf-8") as handle:
            row = next(csv.DictReader(handle))
        canonical = contract.kunwei_csv_row_to_canonical(row)
        sample = contract.step5b_sample_from_canonical_wrench(
            canonical,
            tcp_pose=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            robot_stage=25.0,
            dt_s=0.002,
        )
        self.assertEqual(canonical.source, contract.SOURCE_REAL_KUNWEI_READ_ONLY)
        self.assertEqual(sample.sensor_ok, 1.0)
        self.assertAlmostEqual(sample.tcp_wrench[2], float(row["fz_n_zeroed"]))

        params = core.Step5bContactParams()
        basis = core.Step5bPathBasis(
            origin_xy_m=(0.0, 0.0),
            u_along_xy=(1.0, 0.0),
            p_lateral_xy=(0.0, 1.0),
        )
        result, _ = core.compute_step5b_contact_sample(
            sample,
            core.Step5bContactState(normal_acquired=True, latched_normal_b=(0.0, 0.0, 1.0)),
            params,
            basis,
        )
        self.assertGreaterEqual(result.force_norm_n, 0.0)

    def test_step5b_controller_core_has_no_gazebo_private_force_topic_dependency(self) -> None:
        source = Path(core.__file__).read_text(encoding="utf-8")
        self.assertNotIn("gazebo_joint_state_fk_virtual_surface_model", source)
        self.assertNotIn("/gazebo/", source)
        self.assertNotIn("/world/", source)

    def test_force_source_lineage_covers_all_required_sources(self) -> None:
        lineage = contract.force_source_lineage_table()
        self.assertEqual({row["source_name"] for row in lineage}, set(contract.SOURCE_CLASSES))
        for row in lineage:
            self.assertIn("allowed_claim_tier", row)
            self.assertIn("zero_baseline_policy", row)
            self.assertIn("real_transfer_readiness", row)


if __name__ == "__main__":
    unittest.main()
