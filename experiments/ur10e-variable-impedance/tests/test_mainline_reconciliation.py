import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MainlineReconciliationTests(unittest.TestCase):
    def test_dirty_ubuntu_lane_is_quarantined_without_borrowing_evidence(self) -> None:
        record = json.loads(
            (ROOT / "config" / "mainline_reconciliation_20260713.json").read_text()
        )
        self.assertEqual(record["schema"], "ur10e_mainline_reconciliation_v1")
        self.assertEqual(record["source_head"], record["merge_base"])
        self.assertEqual(record["source_behind_commits"], 9)
        self.assertEqual(record["source_ahead_commits"], 0)
        self.assertFalse(record["source_worktree_mutated"])
        self.assertEqual(record["safe_start_commit"], record["frozen_baseline"])
        self.assertEqual(
            sum(record["classification_counts"].values()),
            record["tracked_changed_paths"] + record["untracked_paths"],
        )
        self.assertEqual(
            record["decisions"]["p0_runtime_evidence"],
            "preserve_external_historical_only_do_not_replace_frozen_v4",
        )
        evidence = record["external_evidence"]
        self.assertRegex(evidence["bundle_sha256_manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(evidence["contains_binary_patch"])
        self.assertTrue(evidence["contains_untracked_tar"])
        self.assertTrue(evidence["contains_per_path_sha256"])


if __name__ == "__main__":
    unittest.main()
