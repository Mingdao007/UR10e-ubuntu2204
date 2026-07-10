from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from ur10e_vic.evidence import (
    validate_compact_evidence_index,
    validate_offline_claim_state,
    verify_external_artifacts,
)


ROOT = Path(__file__).resolve().parents[1]


class EvidenceTests(unittest.TestCase):
    def test_offline_claim_state_rejects_internally_consistent_live_promotion(self) -> None:
        promoted = {
            "live_motion_authorized": False,
            "claim_state": {
                "package_status": "package_accepted",
                "authorization_status": "live_authorized",
                "run_status": "live_completed",
                "acceptance_status": "live_accepted",
                "reproduction_status": "reproduction_complete",
            },
        }
        with self.assertRaisesRegex(ValueError, "live authorization"):
            validate_offline_claim_state(promoted)

    def test_compact_index_validates_without_external_cache(self) -> None:
        payload = validate_compact_evidence_index(
            ROOT / "evidence/offline_evidence_index.json"
        )
        self.assertFalse(payload["live_motion_authorized"])
        self.assertFalse(payload["dbil_active_enabled"])
        self.assertEqual(
            payload["claim_state"]["reproduction_status"], "not_claimed"
        )
        self.assertFalse(payload["trace_scope"]["claim_evidence_valid"])
        self.assertEqual(
            payload["paced_timing"]["result"],
            "all_paced_rates_failed_provenance_ineligible_shadow_only",
        )
        self.assertFalse(payload["paced_timing"]["selection_eligible"])
        self.assertIsNone(payload["paced_timing"]["selected_rate_hz"])
        absent = verify_external_artifacts(payload, None)
        self.assertFalse(absent["external_rehash_performed"])
        self.assertFalse(absent["artifact_integrity_verified"])
        self.assertFalse(absent["claim_validation_ready"])
        with tempfile.TemporaryDirectory() as directory:
            missing = verify_external_artifacts(payload, Path(directory))
        self.assertTrue(missing["external_rehash_performed"])
        self.assertFalse(missing["artifact_integrity_verified"])
        self.assertFalse(missing["claim_validation_ready"])
        self.assertEqual(
            len(missing["missing_roles"]), len(payload["artifacts"])
        )

    def test_external_rehash_snapshot_binds_compact_index(self) -> None:
        index_path = ROOT / "evidence/offline_evidence_index.json"
        snapshot = json.loads(
            (ROOT / "evidence/external_rehash_verification.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            snapshot["source_index_sha256"],
            hashlib.sha256(index_path.read_bytes()).hexdigest(),
        )
        self.assertTrue(snapshot["external_rehash_performed"])
        self.assertTrue(snapshot["artifact_integrity_verified"])
        self.assertFalse(snapshot["claim_validation_ready"])
        self.assertEqual(snapshot["missing_roles"], [])
        self.assertEqual(snapshot["mismatched_roles"], [])


if __name__ == "__main__":
    unittest.main()
