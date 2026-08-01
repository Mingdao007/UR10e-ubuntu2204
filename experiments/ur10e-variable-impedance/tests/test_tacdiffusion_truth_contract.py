from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest

from ur10e_vic.tacdiffusion.truth import (
    CLAIM_CLASSES,
    TruthContractError,
    build_current_validation,
    build_offline_readiness,
    check_current_views,
    load_truth_source,
    render_json,
    validate_truth_document,
)


ROOT = Path(__file__).resolve().parents[1]
TRUTH_PATH = ROOT / "config" / "tacdiffusion_component_truth_v1.json"


class TacDiffusionTruthContractTests(unittest.TestCase):
    def truth(self) -> dict:
        return load_truth_source(TRUTH_PATH)

    def test_locked_overall_claims_and_claim_vocabulary_are_fail_closed(self) -> None:
        document = self.truth()
        self.assertEqual(document["claim_classes"], list(CLAIM_CLASSES))
        self.assertEqual(
            document["overall"],
            {
                "active": False,
                "adaptation_scope": "UR10e force-domain TacDiffusion adaptation preparation",
                "deterministic_expert_dataset": False,
                "formal_checkpoint": False,
                "internal_wrench_production_conformance": False,
                "model_active": False,
                "model_rate_selected_hz": None,
                "reproduction_boundary": "not Panda 1 kHz paper-exact reproduction",
                "reproduction_status": "not_claimed",
                "tracking_noise_model": None,
                "tracking_supported": False,
            },
        )
        self.assertEqual(document["components"]["tracking"]["status"], "unsupported")
        direct = document["components"]["direct_torque_no_contact_diagnostic"]
        self.assertEqual(direct["status"], "accepted")
        self.assertTrue(direct["accepted"])
        self.assertEqual(direct["claim_classes"], ["live_no_contact_diagnostic"])
        self.assertEqual(direct["promotion_targets"], [])
        self.assertTrue(all(item["state"] == "bound" for item in direct["evidence_bindings"]))
        for component in (
            "internal_wrench_production_conformance",
            "deterministic_expert_dataset",
            "formal_checkpoint",
            "model_rate_selection",
            "model_active",
        ):
            self.assertFalse(document["components"][component]["accepted"])
        self.assertIsNone(document["components"]["model_rate_selection"]["selected_rate_hz"])

    def test_contradictory_overall_and_component_state_is_rejected(self) -> None:
        document = self.truth()
        document["overall"]["active"] = True
        with self.assertRaisesRegex(TruthContractError, "active"):
            validate_truth_document(document)

        document = self.truth()
        document["components"]["formal_checkpoint"]["accepted"] = True
        with self.assertRaisesRegex(TruthContractError, "status and accepted"):
            validate_truth_document(document)

    def test_source_binding_is_rehashed_and_malformed_digest_is_rejected(self) -> None:
        document = self.truth()
        validation = validate_truth_document(document, repo_root=ROOT)
        self.assertEqual(validation.schema, "ur10e_tacdiffusion_component_truth/v1")
        self.assertEqual(
            validation.accepted_components,
            ("offline_fixture", "direct_torque_no_contact_diagnostic"),
        )

        document = self.truth()
        document["components"]["formal_checkpoint"]["source_bindings"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(TruthContractError, "does not match"):
            validate_truth_document(document, repo_root=ROOT)

    def test_unknown_claim_class_and_missing_binding_fail_closed(self) -> None:
        document = self.truth()
        document["components"]["model_active"]["claim_class"] = "unknown_claim"
        document["components"]["model_active"]["claim_classes"] = ["unknown_claim"]
        with self.assertRaisesRegex(TruthContractError, "unknown claim class"):
            validate_truth_document(document)

        document = self.truth()
        document["components"]["deterministic_expert_dataset"]["evidence_bindings"][0][
            "sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(TruthContractError, "cannot carry path or sha256"):
            validate_truth_document(document)

        document = self.truth()
        document["components"]["formal_checkpoint"]["source_bindings"][0]["path"] = (
            "config/missing-checkpoint-schema.json"
        )
        document["components"]["formal_checkpoint"]["source_bindings"][0]["identity"] = (
            "config/missing-checkpoint-schema.json"
        )
        with self.assertRaisesRegex(TruthContractError, "does not exist"):
            validate_truth_document(document)

    def test_direct_torque_cannot_promote_and_fixture_cannot_be_production(self) -> None:
        document = self.truth()
        direct = document["components"]["direct_torque_no_contact_diagnostic"]
        self.assertEqual(direct["claim_classes"], ["live_no_contact_diagnostic"])

        document["components"]["direct_torque_no_contact_diagnostic"]["evidence_bindings"][0][
            "sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(TruthContractError, "does not match"):
            validate_truth_document(document)

        document = self.truth()
        direct = document["components"]["direct_torque_no_contact_diagnostic"]
        direct["evidence_bindings"][0]["path"] = (
            "experiments/tase-contact-reproduction/config/missing_step5_stage_table.json"
        )
        direct["evidence_bindings"][0]["identity"] = (
            "experiments/tase-contact-reproduction/config/missing_step5_stage_table.json#step5d_direct_torque_remote_live_v4"
        )
        with self.assertRaisesRegex(TruthContractError, "does not exist"):
            validate_truth_document(document)

        document = self.truth()
        direct = document["components"]["direct_torque_no_contact_diagnostic"]
        direct["evidence_bindings"][0]["record_id"] = "wrong_stage_record"
        direct["evidence_bindings"][0]["identity"] = (
            "experiments/tase-contact-reproduction/config/step5_stage_table.json#wrong_stage_record"
        )
        with self.assertRaisesRegex(TruthContractError, "record_id"):
            validate_truth_document(document)

        document = self.truth()
        direct = document["components"]["direct_torque_no_contact_diagnostic"]
        direct["evidence_bindings"][0]["root"] = "experiment"
        with self.assertRaisesRegex(TruthContractError, "does not exist"):
            validate_truth_document(document)

        document = self.truth()
        direct = document["components"]["direct_torque_no_contact_diagnostic"]
        direct["evidence_bindings"][0]["root"] = "unknown"
        with self.assertRaisesRegex(TruthContractError, "root must be experiment or repository"):
            validate_truth_document(document)

        document = self.truth()
        direct = document["components"]["direct_torque_no_contact_diagnostic"]
        direct["evidence_bindings"][0]["path"] = (
            "experiments/../tase-contact-reproduction/config/step5_stage_table.json"
        )
        with self.assertRaisesRegex(TruthContractError, "repository-relative"):
            validate_truth_document(document)

        document = self.truth()
        direct = document["components"]["direct_torque_no_contact_diagnostic"]
        direct["evidence_bindings"][0]["path"] = (
            "experiments/./tase-contact-reproduction/config/step5_stage_table.json"
        )
        with self.assertRaisesRegex(TruthContractError, "normalized repository-relative"):
            validate_truth_document(document)

        document = self.truth()
        direct = document["components"]["direct_torque_no_contact_diagnostic"]
        direct["promotion_targets"] = ["formal_expert_data"]
        with self.assertRaisesRegex(TruthContractError, "Direct Torque"):
            validate_truth_document(document)

        document = self.truth()
        document["components"]["direct_torque_no_contact_diagnostic"]["truth"][
            "promotes_model_active"
        ] = True
        with self.assertRaisesRegex(TruthContractError, "Direct Torque truth"):
            validate_truth_document(document)

        document = self.truth()
        fixture = document["components"]["offline_fixture"]
        fixture["promotion_targets"] = ["model_active"]
        with self.assertRaisesRegex(TruthContractError, "offline fixtures"):
            validate_truth_document(document)

        document = self.truth()
        formal = document["components"]["deterministic_expert_dataset"]
        formal["evidence_bindings"][0] = deepcopy(
            document["components"]["offline_fixture"]["evidence_bindings"][0]
        )
        with self.assertRaisesRegex(TruthContractError, "claim class mismatch"):
            validate_truth_document(document)

    def test_generated_views_are_deterministic_and_use_review_policy_v3(self) -> None:
        document = self.truth()
        readiness_a = build_offline_readiness(document, repo_root=ROOT)
        readiness_b = build_offline_readiness(document, repo_root=ROOT)
        validation_a = build_current_validation(document, repo_root=ROOT)
        validation_b = build_current_validation(document, repo_root=ROOT)
        self.assertEqual(render_json(readiness_a), render_json(readiness_b))
        self.assertEqual(render_json(validation_a), render_json(validation_b))
        self.assertEqual(readiness_a["review_policy"]["schema"], "ur10e_tacdiffusion_review_policy/v3")
        self.assertEqual(validation_a["review_policy"]["schema"], "ur10e_tacdiffusion_review_policy/v3")
        self.assertTrue(validation_a["validation"]["review_policy_v3_effective"])
        self.assertFalse(validation_a["validation"]["legacy_review_policy_v2_effective"])
        check_current_views(repo_root=ROOT)

    def test_generated_views_keep_history_and_do_not_elevate_fixture_claim(self) -> None:
        history_path = ROOT / "evidence" / "tacdiffusion_offline_validation.json"
        history_before = history_path.read_bytes()
        document = self.truth()
        readiness = build_offline_readiness(document, repo_root=ROOT)
        validation = build_current_validation(document, repo_root=ROOT)
        self.assertEqual(history_path.read_bytes(), history_before)
        self.assertEqual(
            readiness["accepted_claims"],
            [
                {
                    "component": "direct_torque_no_contact_diagnostic",
                    "claim_class": "live_no_contact_diagnostic",
                    "status": "accepted",
                },
                {
                    "component": "offline_fixture",
                    "claim_class": "offline_fixture",
                    "status": "accepted",
                },
            ],
        )
        self.assertEqual(
            validation["accepted_claims"],
            [
                {
                    "component": "direct_torque_no_contact_diagnostic",
                    "claim_class": "live_no_contact_diagnostic",
                    "status": "accepted",
                },
                {
                    "component": "offline_fixture",
                    "claim_class": "offline_fixture",
                    "status": "accepted",
                },
            ],
        )
        self.assertFalse(validation["overall"]["formal_checkpoint"])
        self.assertFalse(validation["overall"]["active"])


if __name__ == "__main__":
    unittest.main()
