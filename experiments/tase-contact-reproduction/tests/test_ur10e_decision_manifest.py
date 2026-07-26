from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from ur10e_decision_manifest import build_snapshot, canonical_digest, freeze, verify  # noqa: E402


class Ur10eDecisionManifestTest(unittest.TestCase):
    def test_snapshot_resolves_direct_duration_and_no_live_authority(self) -> None:
        snapshot = build_snapshot()
        self.assertEqual(snapshot["resolved"]["p0_v8_direct_duration_s"], 60.0)
        self.assertFalse(snapshot["resolved"]["onrobot_mainline_enabled"])
        self.assertFalse(snapshot["resolved"]["live_actions_authorized"])
        decisions = json.loads(
            (ROOT / "config/ur10e_user_decisions_v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot["decision_source_digest"], canonical_digest(decisions))
        candidate = json.loads(
            (ROOT / "config/current_stage.json").read_text(encoding="utf-8")
        )["p0_v8_candidate"]
        self.assertEqual(
            candidate["composite_binding"]["decision_source_digest"],
            snapshot["decision_source_digest"],
        )

    def test_frozen_manifest_fails_closed_after_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "decision.json"
            freeze(target)
            self.assertEqual(verify(target)["decision_digest"], json.loads(target.read_text())["decision_digest"])
            payload = json.loads(target.read_text())
            payload["resolved"]["current_stage_id"] = "stale"
            target.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "stale"):
                verify(target)


if __name__ == "__main__":
    unittest.main()
