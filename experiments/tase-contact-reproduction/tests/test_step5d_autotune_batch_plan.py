from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_batch_plan import (  # noqa: E402
    append_batch,
    assert_append_only,
    candidate_from_log2_payload,
    initialize_plan,
    load_plan,
)
from step5d_autotune_contract import ForceCandidate  # noqa: E402


def candidate(p: float, i: float, damping: float) -> ForceCandidate:
    return ForceCandidate.from_log2(p=p, i=i, damping=damping)


class CandidateBatchPlanTest(unittest.TestCase):
    def test_five_point_batches_are_atomic_append_only_and_log2_native(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "control" / "candidate_plan.json"
            initial = initialize_plan(path, campaign_id="campaign-1")
            points = (
                candidate(-0.25, 0.0, 0.0),
                candidate(0.25, 0.0, 0.0),
                candidate(0.0, 0.0, 0.25),
                candidate(-0.25, 0.0, -0.25),
                candidate(0.25, 0.0, -0.25),
            )
            updated = append_batch(path, candidates=points, source="Codex batch 1")
            self.assertEqual(updated.revision, 1)
            self.assertEqual(updated.candidates, points)
            assert_append_only(initial, updated)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["batch_size"], 5)
            self.assertEqual(payload["batches"][0]["candidates"][0]["log2_p"], -0.25)

    def test_plan_accepts_only_approved_coarse_i_scale_multipliers(self) -> None:
        approved = (10.0, 50.0, 100.0, 500.0, 1000.0)
        parsed = tuple(
            candidate_from_log2_payload(
                {
                    "log2_p": 0.75,
                    "i_multiplier": multiplier,
                    "log2_damping": 0.25,
                }
            )
            for multiplier in approved
        )
        self.assertEqual(
            tuple(row.approved_i_scale_multiplier for row in parsed),
            approved,
        )
        with self.assertRaisesRegex(ValueError, "approved I scale probe"):
            candidate_from_log2_payload(
                {"log2_p": 0.75, "i_multiplier": 25, "log2_damping": 0.25}
            )

    def test_plan_rejects_non_lattice_out_of_envelope_and_duplicate_points(self) -> None:
        for row in (
            {"log2_p": 0.1, "log2_i": 0.0, "log2_damping": 0.0},
            {"log2_p": 1.25, "log2_i": 0.0, "log2_damping": 0.0},
        ):
            with self.subTest(row=row), self.assertRaises(ValueError):
                candidate_from_log2_payload(row)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "candidate_plan.json"
            initialize_plan(path, campaign_id="campaign-1")
            repeated = candidate(0.25, 0.0, 0.0)
            with self.assertRaisesRegex(ValueError, "repeats"):
                append_batch(
                    path,
                    candidates=(repeated,) * 5,
                    source="invalid duplicate batch",
                )

    def test_prior_batch_rewrite_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "candidate_plan.json"
            initial = initialize_plan(path, campaign_id="campaign-1")
            points = tuple(candidate(p, 0.0, 0.0) for p in (-1.0, -0.75, -0.5, -0.25, 0.0))
            updated = append_batch(path, candidates=points, source="Codex batch 1")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["batches"][0]["candidates"][0]["log2_p"] = 0.25
            path.write_text(json.dumps(payload), encoding="utf-8")
            rewritten = load_plan(path, campaign_id="campaign-1")
            with self.assertRaisesRegex(ValueError, "rewrote"):
                assert_append_only(updated, rewritten)
            self.assertEqual(initial.revision, 0)


if __name__ == "__main__":
    unittest.main()
