from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_r006_identity_namespace_incident_is_fail_closed() -> None:
    fixture = json.loads(
        (
            ROOT
            / "tests/fixtures/step5d_r006_batch_identity_namespace_incident.json"
        ).read_text(encoding="utf-8")
    )

    assert fixture["disposition"] == "known_incompatible_do_not_retry"
    assert fixture["bundle_committed"] is True
    assert fixture["trial_brief_published"] is True
    assert fixture["terminal_ready_committed"] is True
    assert fixture["arm2_dispatched"] is False
    assert fixture["all_ten_rows_mismatch"] is True
    assert fixture["optimizer_eligible"] is False
    assert fixture["sha256"] == {
        "bridge_csv": "a42036730a30b6479b226c83219225b1ffe017d5d62fee5eee9f10c16496ba53",
        "candidate_plan": "bed54b7482fa596fcc6bf34fa4c4aabbeecfe9c86903b123aa68f4edeaec5935",
        "trial_overlay_plan": "2691a3faa60e68df5b86d12c0a37bfa0807e7ae6902c5bfb05ba16c1a928a689",
        "batch_identity": "135dfa1a0518e7dfccdb0b9ca7f48382c93d4aed0bfb6075093289b7c79fdd5d",
        "immutable_trial_bundle": "dd912bd66f18d649b387c77cca38d270c4fb4db15e4b7a1315a19ffbae041726",
    }
