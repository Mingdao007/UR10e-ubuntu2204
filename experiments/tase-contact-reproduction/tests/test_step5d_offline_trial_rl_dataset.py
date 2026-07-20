from __future__ import annotations

import sys
import unittest
from pathlib import Path

from jsonschema import ValidationError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_offline_trial_rl.dataset import _outcome  # noqa: E402
from step5d_offline_trial_rl.schema import validate_record  # noqa: E402


def valid_record() -> dict:
    return {
        "schema": "step5d.offline-trial-rl/dataset-record-v1",
        "record_uid": "0" * 64,
        "source_kind": "v3_diagnostic_bundle",
        "source_artifact_sha256": "1" * 64,
        "campaign_uid": "campaign", "trial_uid": "trial", "legacy_trial_number": 21,
        "source_git_sha": "abc", "plant_epoch": "plant",
        "controller_identity": {}, "tp_identity": {}, "trajectory_identity": {},
        "objective_identity": {}, "parameter_semantics_fingerprint": "fp",
        "action": {"force_p_gain": 0.001, "force_i_gain": 0.00001, "force_damping": 7.0, "orientation_ko": 0.4},
        "action_complete": True, "sequence_artifact": None,
        "outcome_class": "observer_gap", "metric_role": "unavailable",
        "objective": None, "reward": None, "reward_eligible": False,
        "constraint_eligible": False, "constraint_unsafe": None,
        "structural_failures": ["cadence_failed"], "features": {"force_mae_n": None},
        "missing_reasons": ["objective_metric_unavailable"], "split_group": "plant",
    }


class OfflineTrialDatasetTest(unittest.TestCase):
    def test_schema_rejects_unknown_fields(self) -> None:
        record = valid_record()
        validate_record(record)
        record["optimizer_eligible"] = True
        with self.assertRaises(ValidationError):
            validate_record(record)

    def test_outcome_boundaries_do_not_mix_failure_causes(self) -> None:
        self.assertEqual(_outcome("parameter_event", [], False), "parameter_constraint_violation")
        self.assertEqual(_outcome("wait_infra_ready", [], False), "infrastructure_failure")
        self.assertEqual(_outcome("fail_closed", ["cadence_failed"], False), "observer_gap")
        self.assertEqual(_outcome("fail_closed", ["orientation_profile_unqualified"], False), "model_mismatch")
        self.assertEqual(_outcome("new_unknown_disposition", [], False), "unknown")


if __name__ == "__main__":
    unittest.main()
