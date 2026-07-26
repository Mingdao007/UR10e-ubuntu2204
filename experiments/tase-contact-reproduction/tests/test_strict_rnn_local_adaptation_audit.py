#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_strict_rnn_local_adaptation_audit as audit  # noqa: E402


class StrictRnnLocalAdaptationAuditTest(unittest.TestCase):
    def test_build_audit_is_fail_closed_and_post_checkpoint_bound(self) -> None:
        payload = audit.build_audit(generated_at="2026-06-21T10:42:00+08:00")

        self.assertEqual(payload["schema"], "ur10e_strict_rnn_local_adaptation_audit_v1")
        self.assertEqual(payload["status"], "blocked_local_adaptation_fields_not_final_acceptance")
        self.assertEqual(payload["claim_tier"], "virtual/software force-loop")
        self.assertEqual(payload["target_claim_tier"], "virtual/software force-loop")
        self.assertTrue(payload["audit_ok"])
        self.assertFalse(payload["strict_rnn_final_acceptance_allowed"])
        self.assertEqual(
            payload["evidence_window"],
            "post-checkpoint gated audit of pre-gate source artifacts",
        )
        self.assertEqual(payload["checkpoint_boundary"]["freeze_at"], "2026-06-21T10:33:50+08:00")
        self.assertEqual(payload["checkpoint_boundary"]["source_artifacts_evidence_window"], "pre-gate evidence")
        self.assertEqual(payload["checkpoint_boundary"]["audit_artifact_evidence_window"], "post-checkpoint gated evidence")
        self.assertEqual(
            payload["checkpoint_boundary"]["final_acceptance_effect"],
            "does_not_clear_strict_rnn_final_acceptance",
        )
        self.assertIn("simulated_ft", payload["forbidden_claim"])
        self.assertIn("physical Gazebo collision/contact physics", payload["forbidden_claim"])
        self.assertIn("real bench/live contact", payload["forbidden_claim"])

    def test_all_pending_fields_are_accounted_without_supporting_final_acceptance(self) -> None:
        payload = audit.build_audit(generated_at="2026-06-21T10:42:00+08:00")
        rows = payload["field_rows"]

        self.assertEqual(len(rows), 9)
        self.assertFalse(payload["missing_pending_rows"])
        self.assertFalse(payload["unexpected_rows"])
        self.assertEqual(payload["supports_final_acceptance_count"], 0)
        self.assertEqual(payload["blocked_or_local_only_count"], 9)
        self.assertEqual(set(payload["paper_truth_pending_fields"]), set(audit.PENDING_FIELDS))
        for row in rows:
            with self.subTest(field=row["field"]):
                self.assertEqual(row["claim_tier"], "virtual/software force-loop")
                self.assertFalse(row["supports_strict_rnn_final_acceptance"])
                self.assertIn("blocker", row)

    def test_nonzero_command_probe_passes_local_discrete_sign_gate(self) -> None:
        payload = audit.build_audit(generated_at="2026-06-21T10:42:00+08:00")
        row = next(row for row in payload["field_rows"] if row["field"] == "Eq23_nonzero_command_stability")
        evidence = row["evidence"]

        self.assertEqual(row["status"], "local_discrete_sign_gate_passed_current_variant")
        self.assertEqual(evidence["claim_tier"], "virtual/software force-loop")
        self.assertTrue(evidence["stable_for_final_acceptance"])
        self.assertFalse(evidence["hit_velocity_bound"])
        self.assertLess(evidence["final_residual_norm"], evidence["initial_residual_norm"])
        self.assertLess(evidence["final_residual_norm"], 1e-3)
        self.assertEqual(evidence["projection_input_form"], "J.T @ lambda_state")
        self.assertEqual(
            evidence["lambda_update_form"],
            "lambda_state -= (dt / epsilon) * (J @ theta_dot_state - xdot_c)",
        )
        self.assertEqual(len(evidence["final_theta_dot_state"]), 6)
        self.assertEqual(len(evidence["final_lambda_state"]), 6)
        pdf_sign = evidence["paper_pdf_sign_consistency"]
        self.assertEqual(pdf_sign["claim_tier"], "virtual/software force-loop")
        self.assertEqual(pdf_sign["status"], "blocked_pdf_printed_sign_requires_local_discrete_gate")
        self.assertTrue(pdf_sign["printed_eq23_sign_anchors_present"])
        self.assertTrue(pdf_sign["kkt_sign_tension_from_eq21_and_printed_eq23"])
        self.assertTrue(pdf_sign["local_discrete_gate_required"])
        sign = evidence["sign_sensitivity"]
        self.assertEqual(sign["claim_tier"], "virtual/software force-loop")
        self.assertEqual(sign["status"], "local_discrete_sign_gate_passed_current_variant")
        self.assertTrue(sign["current_variant"]["stable_for_final_acceptance"])
        self.assertEqual(sign["current_variant"]["variant"], "current_positive_projection_negative_lambda_update")
        self.assertNotIn(
            "shadow_positive_projection_positive_lambda_update",
            sign["shadow_stable_variants"],
        )
        self.assertIn(
            "shadow_negative_projection_positive_lambda_update",
            sign["shadow_stable_variants"],
        )
        self.assertIn("does_not_clear_strict_rnn_final_acceptance", sign["acceptance_effect"])
        self.assertNotIn("eq23_nonzero_command_stability:not_proven", payload["blockers"])
        self.assertIn("local_adaptation:not_final_acceptance", payload["blockers"])

    def test_local_qdot_and_no_contact_rows_explain_remaining_blockers(self) -> None:
        payload = audit.build_audit(generated_at="2026-06-21T10:42:00+08:00")
        rows = {row["field"]: row for row in payload["field_rows"]}

        qdot = rows["local_qdot_bound_rad_s"]
        self.assertEqual(qdot["status"], "blocked_local_bound_differs_from_pdf_anchor")
        self.assertEqual(qdot["evidence"]["pdf_section_vi_bound_rad_s"], 0.15)
        self.assertEqual(qdot["evidence"]["local_numeric_sanity_bound_rad_s"], 0.3)
        self.assertEqual(qdot["evidence"]["numeric_qdot_max_abs_rad_s"], 0.3)

        dryrun = rows["step5c_strict_dryrun.which paper equations remain active in no-contact dry-run"]
        self.assertEqual(dryrun["status"], "blocked_no_contact_dryrun_is_repo_adaptation")
        self.assertEqual(dryrun["evidence"]["contact_evidence"], "not_claimed")
        self.assertEqual(dryrun["evidence"]["numeric_sanity_force_input"], "synthetic_no_contact_unit_normal")

    def test_pdf_anchor_qdot_sanity_reduces_bound_mismatch_without_final_acceptance(self) -> None:
        with tempfile.TemporaryDirectory(prefix="strict_rnn_qdot_anchor_test_") as tmp:
            numeric_path = Path(tmp) / "step5d_numeric_sanity.json"
            numeric_path.write_text(
                json.dumps(
                    {
                        "overall_pass": True,
                        "assumptions": {
                            "alpha_s_inv": 1.0,
                            "T_s": 0.002,
                            "qdot_limit_rad_s": 0.15,
                            "sigr_exponent_r": 1.0,
                            "force_input": "synthetic_environment_on_tool_force_along_reaction_normal",
                            "contact_evidence": "not_claimed",
                        },
                        "gates": {"qdot_within_nominal_limit_pass": True},
                        "metrics": {"qdot_max_abs_rad_s": 0.15},
                        "outputs": {"summary_json": str(numeric_path)},
                    }
                ),
                encoding="utf-8",
            )
            payload = audit.build_audit(
                generated_at="2026-06-21T14:20:00+08:00",
                numeric_sanity_path=numeric_path,
            )

        rows = {row["field"]: row for row in payload["field_rows"]}
        qdot = rows["local_qdot_bound_rad_s"]
        self.assertEqual(qdot["status"], "local_pdf_anchor_bound_sanity_passed_not_final_acceptance")
        self.assertEqual(qdot["evidence"]["local_numeric_sanity_bound_rad_s"], 0.15)
        self.assertEqual(qdot["evidence"]["numeric_qdot_max_abs_rad_s"], 0.15)
        self.assertTrue(qdot["evidence"]["qdot_within_nominal_limit_pass"])
        self.assertFalse(qdot["supports_strict_rnn_final_acceptance"])
        self.assertFalse(payload["strict_rnn_final_acceptance_allowed"])

    def test_write_audit_creates_json_artifact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="strict_rnn_local_adaptation_test_") as tmp:
            path = audit.write_audit(Path(tmp), generated_at="2026-06-21T10:42:00+08:00")
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(path.name, "strict_rnn_local_adaptation_audit.json")
        self.assertEqual(payload["artifact_path"], str(path))
        self.assertEqual(payload["schema"], "ur10e_strict_rnn_local_adaptation_audit_v1")
        self.assertFalse(payload["strict_rnn_final_acceptance_allowed"])


if __name__ == "__main__":
    unittest.main()
