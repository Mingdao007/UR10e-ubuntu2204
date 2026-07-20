from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_offline_trial_rl.canonical import canonical_sha256  # noqa: E402
from step5d_offline_trial_rl.policy import (  # noqa: E402
    PolicyConfig,
    build_offline_proposal,
    wilson_upper,
)


def synthetic_dataset() -> dict:
    actions = [
        (0.0008, 0.00001, 5.0, 0.3),
        (0.0010, 0.000012, 6.0, 0.4),
        (0.0012, 0.000014, 7.0, 0.5),
        (0.0014, 0.000016, 8.0, 0.6),
        (0.0016, 0.000018, 9.0, 0.7),
        (0.0018, 0.000020, 10.0, 0.8),
    ]
    records = []
    for group in range(4):
        for p_gain, i_gain, damping, orientation in actions:
            reward = 2.0 - 100.0 * p_gain - 0.03 * damping + 0.1 * orientation
            records.append({
                "action": {
                    "force_p_gain": p_gain, "force_i_gain": i_gain,
                    "force_damping": damping, "orientation_ko": orientation,
                },
                "action_complete": True,
                "reward": reward,
                "reward_eligible": True,
                "constraint_eligible": True,
                "constraint_unsafe": False,
                "split_group": f"plant-{group}",
            })
    return {"records": records, "dataset_sha256": canonical_sha256(records)}


class OfflineTrialPolicyTest(unittest.TestCase):
    def test_insufficient_data_fails_closed(self) -> None:
        dataset = {"records": [], "dataset_sha256": "0" * 64}
        artifact = build_offline_proposal(dataset)
        self.assertEqual(artifact["result_status"], "insufficient_valid_trials")
        self.assertIsNone(artifact["proposal"])
        self.assertFalse(artifact["warm_start_eligible"])
        self.assertFalse(artifact["current_v3_optimizer_eligible"])
        self.assertEqual(artifact["fallback"]["kind"], "configuration_fallback_not_policy")

    def test_deterministic_policy_stays_on_exact_observed_support(self) -> None:
        dataset = synthetic_dataset()
        first = build_offline_proposal(dataset, PolicyConfig(minimum_oof_coverage=0.0))
        second = build_offline_proposal(copy.deepcopy(dataset), PolicyConfig(minimum_oof_coverage=0.0))
        self.assertEqual(canonical_sha256(first), canonical_sha256(second))
        self.assertEqual(first["result_status"], "proposal_available")
        self.assertEqual(first["proposal"]["support_distance"], 0.0)
        observed = [row["action"] for row in dataset["records"]]
        self.assertIn(first["proposal"]["action"], observed)

    def test_missing_metric_is_never_reward_imputed(self) -> None:
        dataset = synthetic_dataset()
        dataset["records"].append({
            "action": dict(dataset["records"][0]["action"]),
            "action_complete": True,
            "reward": None,
            "reward_eligible": False,
            "constraint_eligible": False,
            "constraint_unsafe": None,
            "split_group": "trial-21",
        })
        artifact = build_offline_proposal(dataset, PolicyConfig(minimum_oof_coverage=0.0))
        self.assertEqual(artifact["gates"]["action_complete_reward_records"], 24)

    def test_wilson_gate_is_conservative(self) -> None:
        self.assertGreater(wilson_upper(0, 1), 0.20)
        self.assertLess(wilson_upper(0, 24), 0.20)


if __name__ == "__main__":
    unittest.main()
