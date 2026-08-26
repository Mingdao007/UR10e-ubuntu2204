from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step6_figure8_autotune_v1.v5_campaign import (  # noqa: E402
    CampaignIdentityV2,
    CampaignRoleV2,
    V5CampaignV2,
)
from step6_figure8_autotune_v1.v5_lifecycle_ledger import (  # noqa: E402
    LedgerRole,
    V5PhysicalAdmissionLedgerV2,
)
from step6_figure8_autotune_v1.v5_optimizer_journal import (  # noqa: E402
    V5OptimizerJournalError,
    V5OptimizerTellJournalV1,
)
from test_step6_autotuner_v5_campaign import (  # noqa: E402
    FP,
    RELEASE,
    _real_g3_record,
)


def _campaign(tmp_path: Path) -> V5CampaignV2:
    identity = CampaignIdentityV2(
        CampaignRoleV2.PRIMARY,
        FP,
        RELEASE,
        tmp_path / "campaign",
        f"PRIMARY:{FP}",
    )
    return V5CampaignV2.create(identity)


def test_optimizer_journal_is_exactly_once_across_crash_replay(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    plan = campaign.plan_next()
    assert plan is not None
    record = _real_g3_record(tmp_path / "artifact", plan, role=LedgerRole.PRIMARY)
    ledger = V5PhysicalAdmissionLedgerV2(
        tmp_path / "physical.jsonl",
        campaign_fingerprint=FP,
        release_identity_sha256=RELEASE,
        role=LedgerRole.PRIMARY,
    )
    ledger.append_record(record)
    ledger.prepare_tell(record)
    authorization = ledger.authorize_tell(record.trial_id)

    journal_path = tmp_path / "optimizer.jsonl"
    journal = V5OptimizerTellJournalV1(
        journal_path,
        campaign_fingerprint=FP,
        release_identity_sha256=RELEASE,
        role=LedgerRole.PRIMARY,
    )
    first = journal.tell_exact(
        authorization,
        record,
        candidate=plan.candidate.as_dict(),
    )
    # Simulate a crash after optimizer receipt durability but before the
    # physical ledger reconciles it.  Reopen and replay must return the same
    # receipt without appending another tell row.
    reopened = V5OptimizerTellJournalV1(
        journal_path,
        campaign_fingerprint=FP,
        release_identity_sha256=RELEASE,
        role=LedgerRole.PRIMARY,
    )
    replay = reopened.tell_exact(
        authorization,
        record,
        candidate=plan.candidate.as_dict(),
    )
    assert replay == first
    assert reopened.tell_count == 1
    assert len(journal_path.read_text(encoding="utf-8").splitlines()) == 2

    ledger.reconcile_tell(record.trial_id, replay)
    ledger.commit_tell(record.trial_id, replay)
    outcome = campaign.record_physical_result(plan, record, ledger)
    assert outcome.counted_exact is True


def test_optimizer_journal_rejects_different_replay_and_hash_tamper(
    tmp_path: Path,
) -> None:
    campaign = _campaign(tmp_path)
    plan = campaign.plan_next()
    assert plan is not None
    record = _real_g3_record(tmp_path / "artifact", plan, role=LedgerRole.PRIMARY)
    ledger = V5PhysicalAdmissionLedgerV2(
        tmp_path / "physical.jsonl",
        campaign_fingerprint=FP,
        release_identity_sha256=RELEASE,
        role=LedgerRole.PRIMARY,
    )
    ledger.append_record(record)
    ledger.prepare_tell(record)
    authorization = ledger.authorize_tell(record.trial_id)
    path = tmp_path / "optimizer.jsonl"
    journal = V5OptimizerTellJournalV1(
        path,
        campaign_fingerprint=FP,
        release_identity_sha256=RELEASE,
        role=LedgerRole.PRIMARY,
    )
    journal.tell_exact(authorization, record, candidate=plan.candidate.as_dict())
    changed = plan.candidate.as_dict()
    changed["motion_kp"] = float(changed["motion_kp"]) + 0.1
    with pytest.raises(V5OptimizerJournalError, match="replay differs"):
        journal.tell_exact(authorization, record, candidate=changed)

    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["formal_mae_n"] += 0.1
    lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(V5OptimizerJournalError, match="hash"):
        V5OptimizerTellJournalV1(
            path,
            campaign_fingerprint=FP,
            release_identity_sha256=RELEASE,
            role=LedgerRole.PRIMARY,
        )

