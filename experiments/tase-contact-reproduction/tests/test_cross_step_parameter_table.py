import hashlib
import json
import sys
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import validate_cross_step_parameter_table as validator  # noqa: E402


def add_no_contact_p0_capture_fixture(root: Path) -> None:
    table = validator.load_json(root / "config" / "step5_stage_table.json")
    current = validator.load_json(root / "config" / "current_stage.json")
    row = next(row for row in table["stages"] if row.get("id") == "step5d_strict_rnn_no_contact_p0_v7")
    delivery = row["package_delivery"]
    current["bridge_trigger"]["no_contact_p0_capture"] = {
        "profile": "step5d_strict_rnn_no_contact_p0_v7",
        "controller_target": delivery["controller_target"],
        "controller_readback_manifest": delivery["controller_readback_manifest"],
        "sha256": delivery["sha256"],
    }
    (root / "config" / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")


class CrossStepParameterTableTest(unittest.TestCase):
    def test_cross_step_parameter_table_contract_passes(self) -> None:
        self.assertEqual([], validator.validate(ROOT))

    def test_step5_flow_current_summary_must_match_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            current = validator.load_json(ROOT / "config" / "current_stage.json")
            flow = (ROOT / "STEP5_FLOW.md").read_text(encoding="utf-8")
            (tmp_root / "STEP5_FLOW.md").write_text(
                flow.replace(current["current_stage_id"], "step5d_strict_rnn_ablation_v99", 1),
                encoding="utf-8",
            )

            failures = validator.validate(tmp_root)

        self.assertTrue(
            any("STEP5_FLOW current summary does not match current_stage.json" in failure for failure in failures),
            failures,
        )

    def test_current_v29_readback_flags_and_claim_states_are_consistent(self) -> None:
        current = validator.load_json(ROOT / "config" / "current_stage.json")
        table = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        row = next(row for row in table["stages"] if row.get("id") == current["current_stage_id"])

        self.assertTrue(row["acceptance"]["controller_readback_verified"])
        self.assertTrue(current["v29_contact_candidate"]["controller_readback_verified"])
        self.assertEqual(current["liveprep_status"]["state"], "blocked")
        self.assertTrue(row["blocked"])
        self.assertEqual(row["blocked"], current["liveprep_status"]["state"] == "blocked")
        self.assertEqual(current["live_run_status"]["state"], "not_started")
        self.assertEqual(current["reproduction_status"]["state"], "incomplete")

    def test_canonical_protocol_current_program_and_mode_are_not_stale(self) -> None:
        current = validator.load_json(ROOT / "config" / "current_stage.json")
        table = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        protocol = validator.load_json(ROOT / "config" / "tase_protocol_table.json")
        row = next(item for item in table["stages"] if item.get("id") == current["current_stage_id"])
        profile = protocol["experiment_profiles"]["Step5.step5d_rnn"]

        self.assertEqual(profile["current_program"], current["program"])
        self.assertEqual(profile["stage25_default_control_mode"], row["guard"]["stage25_default_control_mode"])

    def test_validator_detects_stale_canonical_protocol_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            shutil.copy2(ROOT / "STEP5_FLOW.md", tmp_root / "STEP5_FLOW.md")
            protocol_path = tmp_root / "config" / "tase_protocol_table.json"
            protocol = validator.load_json(protocol_path)
            protocol["experiment_profiles"]["Step5.step5d_rnn"]["current_program"] = "step5d_strict_rnn_ablation_v27"
            protocol_path.write_text(json.dumps(protocol), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertIn("canonical Step5d current_program does not match current_stage.json", failures)

    def test_v30_offline_candidate_cannot_claim_controller_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            shutil.copytree(ROOT / "programs", tmp_root / "programs")
            shutil.copy2(ROOT / "STEP5_FLOW.md", tmp_root / "STEP5_FLOW.md")
            table_path = tmp_root / "config" / "step5_stage_table.json"
            table = validator.load_json(table_path)
            v30 = next(item for item in table["stages"] if item.get("id") == "step5d_strict_rnn_ablation_v30")
            v30["package_delivery"]["controller_target"] = "/programs/forbidden.urp"
            table_path.write_text(json.dumps(table), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertIn("v30 offline candidate must have no controller delivery claim", failures)

    def test_v30_stage_binds_current_source_solver_10k_hard_failure(self) -> None:
        table = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        v30 = next(
            item
            for item in table["stages"]
            if item.get("id") == "step5d_strict_rnn_ablation_v30"
        )
        evidence = v30["local_analysis_evidence"]
        raw_path = ROOT / evidence["current_source_solver_10k_raw"]
        raw = validator.load_json(raw_path)

        self.assertEqual(
            hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            evidence["current_source_solver_10k_raw_sha256"],
        )
        self.assertEqual(raw["classification"], "failed_hard_solver_deadline")
        self.assertFalse(raw["acceptance_eligible"])
        self.assertEqual(raw["solver"]["samples"], 10_000)
        self.assertGreater(raw["solver"]["compute_deadline_miss_count"], 0)

    def test_awaiting_v29_requires_matching_readiness_pointer_and_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            shutil.copy2(ROOT / "STEP5_FLOW.md", tmp_root / "STEP5_FLOW.md")
            current_path = tmp_root / "config" / "current_stage.json"
            table_path = tmp_root / "config" / "step5_stage_table.json"
            current = validator.load_json(current_path)
            table = validator.load_json(table_path)
            row = next(item for item in table["stages"] if item.get("id") == current["current_stage_id"])
            current["liveprep_status"] = {
                "state": "awaiting_live_authorization",
                "readiness_artifact": "runs/final/liveprep_readiness.json",
                "blockers": [],
            }
            row["blocked"] = False
            row["liveprep_status"] = {
                "state": "awaiting_live_authorization",
                "readiness_artifact": "runs/final/liveprep_readiness.json",
            }
            current_path.write_text(json.dumps(current), encoding="utf-8")
            table_path.write_text(json.dumps(table), encoding="utf-8")

            failures = validator.validate(tmp_root)
            self.assertTrue(any("readiness sha" in failure for failure in failures), failures)

            current["liveprep_status"]["readiness_sha256"] = "a" * 64
            row["liveprep_status"]["readiness_sha256"] = "a" * 64
            current_path.write_text(json.dumps(current), encoding="utf-8")
            table_path.write_text(json.dumps(table), encoding="utf-8")
            failures = validator.validate(tmp_root)
            self.assertFalse(any("readiness sha" in failure for failure in failures), failures)

    def test_live_startup_gates_are_not_cacheable(self) -> None:
        table = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        profile = validator.dotted_get(table, "startup_gate_profiles.prepared_fast_bridge_v1")
        live_gates = [gate for gate in profile["gates"] if gate.get("liveness_required") is True]

        self.assertGreaterEqual(len(live_gates), 5)
        self.assertTrue(all(gate.get("cacheable") is False for gate in live_gates))

    def test_bridge_startup_policy_stage_ids_are_derived_from_policy_refs(self) -> None:
        step5 = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        step6 = validator.load_json(ROOT / "config" / "step6_stage_table.json")

        derived = set(validator.derived_bridge_startup_policy_stage_ids(step5, step6))
        redundant = set(step5["bridge_startup_policy"]["applies_to_stage_ids"])

        self.assertEqual(derived, redundant)
        self.assertEqual(len(derived), 11)
        self.assertIn("step5d_strict_rnn_ablation_v29", derived)
        self.assertIn("step5d_strict_rnn_no_contact_p0_v4", derived)
        self.assertIn("step5d_strict_rnn_no_contact_p0_v7", derived)
        self.assertIn("step6_contact_eight_baseline_v2", derived)

    def test_triggerable_bridge_row_without_startup_policy_ref_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            table_path = tmp_root / "config" / "step5_stage_table.json"
            table = validator.load_json(table_path)
            table["stages"].append(
                {
                    "id": "step5d_strict_rnn_ablation_v99_test",
                    "stage": "Step5d-test",
                    "owner": "bridge+TP",
                    "active": True,
                    "shape": "cycloid",
                    "frame": "config/step5_safe_frame.json",
                    "contact": True,
                    "bridge": True,
                    "success_condition": "test-only triggerable bridge row",
                    "operator_lifecycle": {
                        "entrypoint": "scripts/step5d-liveprep-operator.sh",
                        "base_operator": "scripts/bridge-line-operator.sh",
                        "mode": "line-bridge-fast",
                    },
                }
            )
            table_path.write_text(json.dumps(table), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertTrue(
            any("step5:step5d_strict_rnn_ablation_v99_test triggerable bridge row missing bridge_startup_policy ref" in failure for failure in failures),
            failures,
        )

    def test_no_contact_p0_delivery_must_match_current_capture_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            add_no_contact_p0_capture_fixture(tmp_root)
            table_path = tmp_root / "config" / "step5_stage_table.json"
            table = validator.load_json(table_path)
            row = next(row for row in table["stages"] if row.get("id") == "step5d_strict_rnn_no_contact_p0_v7")
            row["package_delivery"]["controller_dir"] = "/programs/andyl/kunwei/step5/step5d"
            row["package_delivery"][
                "controller_target"
            ] = "/programs/andyl/kunwei/step5/step5d/step5d_strict_rnn_no_contact_p0_v7.urp"
            table_path.write_text(json.dumps(table), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertTrue(
            any("strict RNN no-contact P0 package_delivery.controller_target" in failure for failure in failures),
            failures,
        )

    def test_no_contact_p0_capture_profile_must_not_be_stale_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            add_no_contact_p0_capture_fixture(tmp_root)
            current = validator.load_json(tmp_root / "config" / "current_stage.json")
            current["bridge_trigger"]["no_contact_p0_capture"]["profile"] = "step5d_strict_rnn_no_contact_p0_v2"
            (tmp_root / "config" / "current_stage.json").write_text(
                json.dumps(current),
                encoding="utf-8",
            )
            failures = validator.validate(tmp_root)

        self.assertTrue(
            any("strict RNN no-contact P0 row is missing from Step5 table: step5d_strict_rnn_no_contact_p0_v2" in failure for failure in failures),
            failures,
        )

    def test_no_contact_p0_delivery_and_capture_manifest_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            add_no_contact_p0_capture_fixture(tmp_root)
            table = validator.load_json(tmp_root / "config" / "step5_stage_table.json")
            row = next(row for row in table["stages"] if row.get("id") == "step5d_strict_rnn_no_contact_p0_v7")
            row["package_delivery"]["controller_readback_manifest"] = "runs/controller_readback_step5d_strict_rnn_no_contact_p0_v7_LOCAL_PENDING_READBACK/manifest.json"
            (tmp_root / "config" / "step5_stage_table.json").write_text(json.dumps(table), encoding="utf-8")
            current = validator.load_json(tmp_root / "config" / "current_stage.json")
            current["bridge_trigger"]["no_contact_p0_capture"]["controller_readback_manifest"] = (
                "runs/controller_readback_step5d_strict_rnn_no_contact_p0_v7_LOCAL_PENDING_READBACK/bad_manifest.json"
            )
            (tmp_root / "config" / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
            failures = validator.validate(tmp_root)

        self.assertTrue(
            any(
                "strict RNN no-contact P0 package_delivery.controller_readback_manifest does not match current capture pointer"
                in failure
                for failure in failures
            ),
            failures,
        )

    def test_v27_failed_evidence_separates_stop_reason_from_control_classification(self) -> None:
        table = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        stage = next(stage for stage in table["stages"] if stage.get("id") == "step5d_strict_rnn_ablation_v27")
        timing = stage["local_analysis_evidence"]["v27_20260706_024815_live_timing"]
        shadow_experiment = stage["local_analysis_evidence"]["v27_20260706_033032_orientation_shadow_experiment"]
        force_overshoot = stage["local_analysis_evidence"]["v27_20260706_040900_step5d_outer_linear_live_fix_validation"]
        fix_validation = stage["local_analysis_evidence"][
            "v27_20260706_045513_step5b_live_step5d_shadow_fix_validation"
        ]
        evidence_refs = stage["evidence_refs"]

        self.assertEqual(timing["terminal_stop_reason"], "step5d_contact_safety:force_norm_hard_stop")
        self.assertEqual(timing["analysis_classification"], "stage25_control_force_oscillation/low_load_timeout")
        self.assertEqual(timing["control_oscillation_trigger"], "low_load_repress_window")
        self.assertEqual(shadow_experiment["terminal_stop_reason"], "v25_speedl_hard_low_load_timeout")
        self.assertEqual(
            shadow_experiment["evidence_classification"],
            "stage25_orientation_shadow_experiment_failed_low_load_timeout",
        )
        self.assertEqual(force_overshoot["summary_stop_reason"], "step5d_contact_safety:force_norm_hard_stop")
        self.assertEqual(force_overshoot["analysis_classification"], "stage25_control_force_oscillation/force_norm_hard_stop")
        self.assertEqual(
            force_overshoot["evidence_classification"],
            "old_v27_paper_outer_linear_live_gain_mismatch_force_norm_hard_stop",
        )
        self.assertEqual(fix_validation["analysis_classification"], "stage25_fix_validation_success")
        self.assertEqual(fix_validation["result"], "successful_10s_fix_validation_not_60s_reproduction")
        self.assertEqual(fix_validation["reproduction_status"], "pending_60s_step5b_equivalent_run")
        self.assertEqual(evidence_refs["latest_terminal_stop_reason"], "tp_normal_stop_reason_1")
        self.assertEqual(evidence_refs["latest_analysis_classification"], "stage25_fix_validation_success")
        self.assertEqual(
            evidence_refs["latest_evidence_classification"],
            "v27_10s_step5b_live_step5d_shadow_fix_validation_success",
        )
        self.assertEqual(
            evidence_refs["previous_failed_live_run_analysis"],
            "runs/bridge_step5d_strict_rnn_ablation_v27_20260706_040900/step5d_bridge_analysis.json",
        )


if __name__ == "__main__":
    unittest.main()
