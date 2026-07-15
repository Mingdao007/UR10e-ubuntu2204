from __future__ import annotations

import json
import hashlib
from copy import deepcopy
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_step5d_autotune_tp import render_script  # noqa: E402


class Step5dAutotuneConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config_path = ROOT / "config/step5d_autotune_campaign_v1.json"
        self.schema_path = (
            ROOT / "config/schemas/step5d_autotune_campaign_v1.schema.json"
        )
        self.payload = json.loads(self.config_path.read_text())
        self.schema = json.loads(self.schema_path.read_text())

    def test_source_contract_validates_and_records_live_activation(self) -> None:
        Draft202012Validator(self.schema).validate(self.payload)
        self.assertTrue(self.payload["activation"]["active"])
        self.assertTrue(self.payload["activation"]["live_authorized"])
        self.assertNotIn(
            "controller_delivery_authorized", self.payload["activation"]
        )

    def test_candidate_schema_has_no_alpha_or_qdot_dimension(self) -> None:
        candidate = self.payload["baseline"]["force_candidate"]
        self.assertEqual(
            set(candidate), {"force_p_gain", "force_i_gain", "force_damping"}
        )
        self.assertNotIn("normal_filter_alpha", self.config_path.read_text())
        self.assertFalse(self.payload["governor"]["qdot_cap_optimizer_dimension"])

    def test_profiles_and_handshake_are_exact(self) -> None:
        self.assertEqual(
            self.payload["baseline"]["execution_profile_id"],
            "nf050-slew050-a050",
        )
        self.assertEqual(
            self.payload["baseline"]["execution_profile_integer_id"], 533
        )
        profiles = self.payload["execution_profiles"]
        self.assertEqual(
            [row["normal_max_rate_rad_s"] for row in profiles],
            [0.01, 0.015, 0.02, 0.03, 0.05],
        )
        self.assertFalse(profiles[3]["live_eligible"])
        self.assertTrue(profiles[4]["live_eligible"])
        self.assertEqual(profiles[2]["tp_speedj_accel_rad_s2"], 0.2)
        self.assertEqual(profiles[2]["host_qdot_slew_rad_s2"], 0.2)
        self.assertEqual(
            set(self.payload["integer_handshake"]["host_to_tp"].values()),
            set(range(24, 30)),
        )
        self.assertEqual(
            set(self.payload["integer_handshake"]["tp_to_host"].values()),
            set(range(24, 31)),
        )

    def test_stage_delivery_review_and_live_binding_are_closed(self) -> None:
        table = json.loads((ROOT / "config/step5_stage_table.json").read_text())
        rows = [
            row
            for row in table["stages"]
            if row["id"] == "step5d_strict_rnn_autotune_v1"
        ]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            row["acceptance"]["review_v3_pre_live_stack"],
            "Fable5/high_or_automatic_degraded_0+0_when_quota_unavailable",
        )
        self.assertEqual(
            row["review_v3"]["status"],
            "accepted_degraded_0+0_fable_unavailable",
        )
        self.assertTrue(row["current_binding"]["is_current"])
        self.assertTrue(row["current_binding"]["live_authorized"])
        self.assertEqual(
            row["package_delivery"]["controller_readback_manifest"],
            "config/step5d_autotune_controller_readback_v1.json",
        )
        self.assertTrue(row["package_delivery"]["controller_readback_verified"])
        manifest_path = ROOT / row["package_delivery"]["controller_readback_manifest"]
        self.assertEqual(
            row["package_delivery"]["controller_readback_manifest_sha256"],
            hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        )
        builder_bytes = (ROOT / "tools/build_step5d_autotune_tp.py").read_bytes()
        rendered_bytes = render_script().encode("utf-8")
        self.assertEqual(
            row["source_binding"]["autotune_tp_builder_sha256"],
            hashlib.sha256(builder_bytes).hexdigest(),
        )
        self.assertEqual(
            row["source_binding"]["autotune_rendered_script_sha256"],
            hashlib.sha256(rendered_bytes).hexdigest(),
        )
        self.assertEqual(
            row["source_binding"]["autotune_rendered_script_bytes"],
            len(rendered_bytes),
        )

    def test_search_never_stops_for_budget_or_low_ei(self) -> None:
        self.assertFalse(self.payload["search"]["trial_budget_stop_enabled"])
        self.assertFalse(self.payload["search"]["low_ei_stop_enabled"])
        self.assertFalse(
            self.payload["failure_policy"]["exact_parameter_set_reuse_allowed"]
        )

    def test_unlock_governor_and_epoch_policies_are_machine_closed(self) -> None:
        search = self.payload["search"]
        self.assertEqual(
            search["tiers"]["T2"]["unlock"]["minimum_eligible_trials"], 6
        )
        self.assertEqual(
            search["tiers"]["T3"]["unlock"]["candidate_bound_latest_trace_replay"],
            ["exact_rnn", "oracle", "slew"],
        )
        self.assertFalse(
            search["plant_epoch_policy"]["observations_cross_epoch_training_allowed"]
        )
        self.assertEqual(
            self.payload["governor"]["probe_protocol"]["sequence"],
            ["A", "B"],
        )
        self.assertTrue(
            self.payload["failure_policy"]
            ["evidence_failure_records_outcome_then_advances"]
        )

    def test_schema_rejects_safety_search_and_handshake_drift(self) -> None:
        validator = Draft202012Validator(self.schema)
        mutations = []
        safety = deepcopy(self.payload)
        safety["safe_closure"]["host"]["continuous_dwell_s"] = 0.1
        mutations.append(safety)
        search = deepcopy(self.payload)
        search["search"]["one_coordinate_per_live_trial"] = False
        mutations.append(search)
        handshake = deepcopy(self.payload)
        handshake["integer_handshake"]["tp_to_host"]["terminal_reason"] = 30
        mutations.append(handshake)
        profile = deepcopy(self.payload)
        profile["execution_profiles"][3]["live_eligible"] = True
        mutations.append(profile)
        for payload in mutations:
            with self.subTest(payload=payload):
                self.assertTrue(list(validator.iter_errors(payload)))


if __name__ == "__main__":
    unittest.main()
