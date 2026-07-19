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
        self.assertFalse(payload["historical_trace_scope"]["claim_evidence_valid"])
        self.assertEqual(
            payload["historical_paced_timing"]["result"],
            "all_paced_rates_failed_provenance_ineligible_shadow_only",
        )
        self.assertFalse(payload["historical_paced_timing"]["selection_eligible"])
        current_timing = payload["evidence_tracks"]["paced_timing"][
            "current_selected"
        ]
        self.assertTrue(current_timing["selection_eligible"])
        self.assertIsNone(current_timing["selected_rate_hz"])
        self.assertEqual(
            current_timing["status"], "validated_all_rates_rejected"
        )
        self.assertTrue(current_timing["shadow_only"])
        self.assertFalse(current_timing["active_enabled"])
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

    def test_current_selection_pointer_is_not_hardcoded_to_historical_failure(self) -> None:
        index_path = ROOT / "evidence/offline_evidence_index.json"
        payload = json.loads(index_path.read_text(encoding="utf-8"))
        payload["artifacts"].extend(
            (
                {
                    "bundle_relative_path": "current/timing-candidate.json",
                    "claim_level": "throughput_rejection_evidence",
                    "role": "current_timing_candidate",
                    "sha256": "a" * 64,
                },
                {
                    "bundle_relative_path": "current/timing-selection.json",
                    "claim_level": "throughput_rejection_evidence",
                    "role": "current_timing_selection",
                    "sha256": "b" * 64,
                },
            )
        )
        payload["external_artifact_verification"]["artifact_count"] = len(
            payload["artifacts"]
        )
        payload["evidence_tracks"]["paced_timing"]["current_selected"] = {
            "candidate_role": "current_timing_candidate",
            "selection_manifest_role": "current_timing_selection",
            "selection_eligible": True,
            "selected_rate_hz": 50,
            "shadow_only": True,
            "active_enabled": False,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", dir=ROOT / "evidence", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream)
        try:
            selected = validate_compact_evidence_index(temporary)
        finally:
            temporary.unlink(missing_ok=True)
        self.assertEqual(
            selected["evidence_tracks"]["paced_timing"]["current_selected"][
                "selected_rate_hz"
            ],
            50,
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
