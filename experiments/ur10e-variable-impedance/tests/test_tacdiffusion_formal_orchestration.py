from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ur10e_vic.tacdiffusion.formal_campaign import (
    build_formal_campaign_source_contract,
)
from ur10e_vic.tacdiffusion.formal_orchestration import (
    ContactAcquisitionContractV1,
    ConsecutiveContactLatchV1,
    FormalAttemptOutcome,
    FormalAttemptPhase,
    FormalAttemptReceiptV1,
    FormalCampaignLedgerV1,
    classify_fault,
)


def _write(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_contact_acquisition_is_frozen_signed_and_bounded() -> None:
    contract = ContactAcquisitionContractV1()
    assert contract.baseline_samples == 1000
    assert contract.latch_samples == 50
    assert contract.sensor_delivery_watchdog_s == pytest.approx(0.080)
    assert contract.as_json()["sensor_delivery_watchdog_semantics"] == (
        "latest_native_batch_delivery_age_only_not_per_frame_host_arrival"
    )
    assert contract.search_displacement_m(2.0) == pytest.approx(0.001)
    assert contract.search_displacement_m(1000.0) == pytest.approx(0.025)
    entry = (0.48, 0.13, 0.033, 3.12, 0.0, 0.068)
    searched = contract.search_pose(entry, 2.0)
    assert searched[2] == pytest.approx(0.032)
    retracted = contract.retract_pose(searched, 1.0)
    assert retracted[2] == pytest.approx(0.042)
    assert contract.permits_automatic_return(
        outcome=FormalAttemptOutcome.RECOVERABLE_FAILURE.value,
        fault_class="recoverable_runtime",
    )
    assert not contract.permits_automatic_return(
        outcome=FormalAttemptOutcome.HARD_FAULT.value,
        fault_class="sensor_fault",
    )


def test_contact_latch_requires_consecutive_native_samples() -> None:
    latch = ConsecutiveContactLatchV1(ContactAcquisitionContractV1())
    for index in range(49):
        assert not latch.observe(sample_index=index, normal_load_n=1.01)
    assert latch.observe(sample_index=49, normal_load_n=1.01)
    latch.reset()
    for index in range(30):
        assert not latch.observe(sample_index=index, normal_load_n=1.1)
    assert not latch.observe(sample_index=31, normal_load_n=1.1)
    assert latch.consecutive == 1
    assert not latch.observe(sample_index=32, normal_load_n=0.9)
    assert latch.consecutive == 0


def test_campaign_ledger_retries_without_consuming_eligible_ordinal(tmp_path: Path) -> None:
    contract = build_formal_campaign_source_contract().fixed_campaign
    ledger = FormalCampaignLedgerV1(tmp_path / "fixed", contract)
    ledger.initialize(source_bindings={"source": "a" * 64})
    attempt_ordinal, episode, attempt_dir = ledger.next_attempt()
    assert (attempt_ordinal, episode.episode_index) == (0, 0)
    evidence_rel = str((attempt_dir / "evidence.json").relative_to(ledger.root))
    evidence_sha = _write(ledger.root / evidence_rel, {"ok": False})
    first = FormalAttemptReceiptV1(
        campaign_id=contract.campaign_id,
        attempt_ordinal=0,
        eligible_ordinal=0,
        episode=episode,
        attempt_id=attempt_dir.name,
        outcome=FormalAttemptOutcome.RECOVERABLE_FAILURE.value,
        phase=FormalAttemptPhase.FAULT.value,
        fault_class="recoverable_runtime",
        auto_return_performed=True,
        evidence_path=evidence_rel,
        evidence_sha256=evidence_sha,
    )
    ledger.append_terminal(first)
    attempt_ordinal, retry_episode, retry_dir = ledger.next_attempt()
    assert attempt_ordinal == 1
    assert retry_episode == episode

    artifact_rel = str((retry_dir / "episode_v4.jsonl").relative_to(ledger.root))
    recorder_manifest_rel = str(
        (retry_dir / "episode_v4.manifest.json").relative_to(ledger.root)
    )
    eligibility_rel = str((retry_dir / "formal_eligibility.json").relative_to(ledger.root))
    evidence_rel = str((retry_dir / "evidence.json").relative_to(ledger.root))
    artifact_sha = _write(ledger.root / artifact_rel, {"sealed": True})
    recorder_manifest_sha = _write(
        ledger.root / recorder_manifest_rel, {"artifact": "episode_v4.jsonl"}
    )
    eligibility_sha = _write(ledger.root / eligibility_rel, {"formal_eligible": True})
    evidence_sha = _write(ledger.root / evidence_rel, {"ok": True})
    second = FormalAttemptReceiptV1(
        campaign_id=contract.campaign_id,
        attempt_ordinal=1,
        eligible_ordinal=0,
        episode=retry_episode,
        attempt_id=retry_dir.name,
        outcome=FormalAttemptOutcome.ELIGIBLE.value,
        phase=FormalAttemptPhase.COMPLETE.value,
        fault_class=None,
        auto_return_performed=True,
        evidence_path=evidence_rel,
        evidence_sha256=evidence_sha,
        artifact_path=artifact_rel,
        artifact_sha256=artifact_sha,
        recorder_manifest_path=recorder_manifest_rel,
        recorder_manifest_sha256=recorder_manifest_sha,
        eligibility_path=eligibility_rel,
        eligibility_sha256=eligibility_sha,
        previous_record_sha256=str(first.record_sha256),
    )
    ledger.append_terminal(second)
    progress = ledger.progress()
    assert progress.attempts == 2
    assert progress.eligible == 1
    assert progress.recoverable_failures == 1
    assert ledger.next_attempt()[1].episode_index == 1


def test_campaign_ledger_rejects_tampering_and_hard_fault_latches(tmp_path: Path) -> None:
    contract = build_formal_campaign_source_contract().fixed_campaign
    ledger = FormalCampaignLedgerV1(tmp_path / "fixed", contract)
    ledger.initialize(source_bindings={"source": "b" * 64})
    _, episode, attempt_dir = ledger.next_attempt()
    evidence_rel = str((attempt_dir / "evidence.json").relative_to(ledger.root))
    evidence_path = ledger.root / evidence_rel
    evidence_sha = _write(evidence_path, {"fault": "protective_stop"})
    receipt = FormalAttemptReceiptV1(
        campaign_id=contract.campaign_id,
        attempt_ordinal=0,
        eligible_ordinal=0,
        episode=episode,
        attempt_id=attempt_dir.name,
        outcome=FormalAttemptOutcome.HARD_FAULT.value,
        phase=FormalAttemptPhase.FAULT.value,
        fault_class="protective_stop",
        auto_return_performed=False,
        evidence_path=evidence_rel,
        evidence_sha256=evidence_sha,
    )
    ledger.append_terminal(receipt)
    with pytest.raises(RuntimeError, match="hard fault"):
        ledger.next_attempt()
    evidence_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        ledger.progress()


def test_fault_classification_denies_unsafe_auto_return() -> None:
    assert classify_fault("kunwei delivery watchdog expired") == ("sensor_fault", False)
    assert classify_fault("protective stop") == ("protective_stop", False)
    assert classify_fault("ordinary eligibility mismatch") == (
        "recoverable_runtime",
        True,
    )
