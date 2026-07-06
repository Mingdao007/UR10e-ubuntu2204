import json
import sys
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import validate_cross_step_parameter_table as validator  # noqa: E402


class CrossStepParameterTableTest(unittest.TestCase):
    def test_cross_step_parameter_table_contract_passes(self) -> None:
        self.assertEqual([], validator.validate(ROOT))

    def test_live_startup_gates_are_not_cacheable(self) -> None:
        table = validator.load_json(ROOT / "config" / "step5_stage_table.json")
        profile = validator.dotted_get(table, "startup_gate_profiles.prepared_fast_bridge_v1")
        live_gates = [gate for gate in profile["gates"] if gate.get("liveness_required") is True]

        self.assertGreaterEqual(len(live_gates), 5)
        self.assertTrue(all(gate.get("cacheable") is False for gate in live_gates))

    def test_no_contact_p0_delivery_must_match_current_capture_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            shutil.copytree(ROOT / "config", tmp_root / "config")
            table_path = tmp_root / "config" / "step5_stage_table.json"
            table = validator.load_json(table_path)
            row = next(row for row in table["stages"] if row.get("id") == "step5d_strict_rnn_no_contact_p0_v2")
            row["package_delivery"]["controller_dir"] = "/programs/andyl/kunwei/step5/step5d"
            row["package_delivery"][
                "controller_target"
            ] = "/programs/andyl/kunwei/step5/step5d/step5d_strict_rnn_no_contact_p0_v2.urp"
            table_path.write_text(json.dumps(table), encoding="utf-8")

            failures = validator.validate(tmp_root)

        self.assertTrue(
            any("strict RNN no-contact P0 package_delivery.controller_target" in failure for failure in failures),
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
