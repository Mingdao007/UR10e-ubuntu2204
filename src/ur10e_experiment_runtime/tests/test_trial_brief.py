from __future__ import annotations

import json

import pytest

from ur10e_experiment_runtime.batch import (
    BatchIdentity,
    BatchJournal,
    BatchRow,
    ExactAckReceipt,
    SafeClosureReceipt,
    return_reference_for_row,
)
from ur10e_experiment_runtime.evidence import EvidenceSink, build_trial_brief
from ur10e_experiment_runtime.failure_to_guard import (
    MetricRole,
    ObserverStatus,
    OracleStatus,
    TrialOutcomeClass,
)
from ur10e_experiment_runtime.stage_adapters import control_candidate_uid


def identity() -> BatchIdentity:
    rows = []
    for index in range(1, 11):
        candidate = {"force_p_gain": 0.001 + index * 1e-6, "force_i_gain": 1e-5, "force_damping": 7.0, "orientation_ko": 0.4}
        overlay = {
            **candidate,
            "control_candidate_uid": control_candidate_uid(candidate),
            "execution_profile_id": "111",
            "step5d_preload_filtered_min_n": 5.0,
            "step5d_preload_filtered_max_n": 22.0,
            "step5d_preload_raw_min_n": 3.0,
            "step5d_preload_raw_max_n": 25.0,
            "step5d_preload_force_norm_max_n": 100.0,
            "step5d_preload_hold_s": 0.0,
            "step5d_preload_timeout_s": 60.0,
        }
        rows.append(BatchRow(index, candidate, overlay))
    return BatchIdentity(
        campaign_uid="campaign-uid",
        experiment_fingerprint="a" * 64,
        launch_fingerprint="b" * 64,
        adapter_fingerprint="c" * 64,
        physical_prior_fingerprint="d" * 64,
        safety_envelope_fingerprint="e" * 64,
        return_policy_fingerprint="f" * 64,
        controller_readback_fingerprint="1" * 64,
        authorization_ref_sha256="2" * 64,
        plant_epoch=1,
        rows=tuple(rows),
    )


def receipts(batch: BatchIdentity, row_index: int = 1):
    trial_uid = "c" * 64
    ack = ExactAckReceipt(
        batch.batch_uid,
        row_index,
        trial_uid,
        batch.rows[row_index - 1].control_candidate_uid,
        "d" * 64,
        "e" * 64,
        "f" * 64,
        1,
        2,
        2,
    )
    closure = SafeClosureReceipt(
        batch.batch_uid,
        row_index,
        trial_uid,
        ack.ack_uid,
        "e" * 64,
        "f" * 64,
        return_reference_for_row(row_index),
    )
    return trial_uid, ack, closure


def ready_brief(tmp_path):
    batch = identity()
    trial_uid, ack, closure = receipts(batch)
    brief = build_trial_brief(
        batch=batch,
        row_index=1,
        trial_uid=trial_uid,
        immutable_bundle_sha256="d" * 64,
        ack=ack,
        closure=closure,
        outcome_class=TrialOutcomeClass.VALID_PARAMETER_OBSERVATION,
        metric_role=MetricRole.TRAINABLE_OBJECTIVE,
        objective=1.25,
        oracle_status=OracleStatus.CREDIBLE,
        observer_status=ObserverStatus.COMPLETE,
        artifact_digests={"bundle": "d" * 64},
        fingerprint_verified=True,
    )
    journal = BatchJournal.create((tmp_path / "batch").resolve(), batch)
    journal.start_attempt(1, trial_uid)
    journal.record_bundle(1, trial_uid, "d" * 64)
    return batch, trial_uid, ack, closure, brief, journal


def test_trial_brief_uses_actual_overlay_and_publishes_once(tmp_path) -> None:
    batch, trial_uid, ack, closure, brief, journal = ready_brief(tmp_path)
    assert brief.document["trial_overlay"] == batch.rows[0].trial_overlay
    assert brief.document["optimizer_eligible"] is False
    assert brief.document["publication_unique"] is False
    journal.record_ack_consumed(ack)
    journal.record_safe_closure(closure)
    sink = EvidenceSink((tmp_path / "briefs").resolve(), journal)
    first = sink.publish_trial_brief(brief)
    assert sink.publish_trial_brief(brief) == first
    published = json.loads(first.read_text())
    assert published["publication_uid"] == brief.publication_uid
    assert published["publication_unique"] is True
    assert published["optimizer_eligible"] is True
    durable = journal.state().rows[0]
    assert durable.trial_brief_publication_uid == brief.publication_uid
    assert durable.optimizer_eligible is True


def test_evidence_sink_rejects_publication_before_exact_ack_and_closure(tmp_path) -> None:
    _, _, ack, closure, brief, journal = ready_brief(tmp_path)
    sink = EvidenceSink((tmp_path / "briefs").resolve(), journal)
    with pytest.raises(ValueError, match="ACK-completed"):
        sink.publish_trial_brief(brief)
    journal.record_ack_consumed(ack)
    with pytest.raises(ValueError, match="ACK-completed"):
        sink.publish_trial_brief(brief)
    journal.record_safe_closure(closure)
    assert sink.publish_trial_brief(brief).is_file()


def test_evidence_sink_recovers_file_before_journal_crash_window(
    tmp_path,
    monkeypatch,
) -> None:
    _, _, ack, closure, brief, journal = ready_brief(tmp_path)
    journal.record_ack_consumed(ack)
    journal.record_safe_closure(closure)
    sink = EvidenceSink((tmp_path / "briefs").resolve(), journal)

    original = journal.record_trial_brief_published

    def simulated_crash(**_kwargs):
        raise RuntimeError("simulated crash after immutable publication")

    monkeypatch.setattr(journal, "record_trial_brief_published", simulated_crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        sink.publish_trial_brief(brief)
    published_path = sink.root / f"{brief.publication_uid}.trial-brief.json"
    assert published_path.is_file()
    assert journal.state().rows[0].trial_brief_document_sha256 is None

    monkeypatch.setattr(journal, "record_trial_brief_published", original)
    assert sink.publish_trial_brief(brief) == published_path
    assert journal.state().rows[0].trial_brief_publication_uid == brief.publication_uid


@pytest.mark.parametrize("row_index", [0, 11, True])
def test_trial_brief_rejects_out_of_range_row(row_index, tmp_path) -> None:
    batch = identity()
    trial_uid, ack, closure = receipts(batch)
    with pytest.raises(ValueError, match="row index"):
        build_trial_brief(
            batch=batch,
            row_index=row_index,
            trial_uid=trial_uid,
            immutable_bundle_sha256="d" * 64,
            ack=ack,
            closure=closure,
            outcome_class=TrialOutcomeClass.VALID_PARAMETER_OBSERVATION,
            metric_role=MetricRole.TRAINABLE_OBJECTIVE,
            objective=1.0,
            oracle_status=OracleStatus.CREDIBLE,
            observer_status=ObserverStatus.COMPLETE,
            artifact_digests={"bundle": "d" * 64},
            fingerprint_verified=True,
        )


def test_legacy_trial_21_is_unavailable_null_and_not_optimizer_eligible() -> None:
    batch = identity()
    trial_uid, ack, closure = receipts(batch)
    brief = build_trial_brief(
        batch=batch,
        row_index=1,
        trial_uid=trial_uid,
        immutable_bundle_sha256="d" * 64,
        ack=ack,
        closure=closure,
        outcome_class=TrialOutcomeClass.OBSERVER_GAP,
        metric_role=MetricRole.UNAVAILABLE,
        objective=None,
        oracle_status=OracleStatus.UNAVAILABLE,
        observer_status=ObserverStatus.INCOMPLETE,
        artifact_digests={"bundle": "d" * 64},
        fingerprint_verified=True,
        legacy_trial_number=21,
    )
    assert brief.document["objective"] is None
    assert brief.document["metric_role"] == "unavailable"
    assert brief.document["optimizer_eligible"] is False
    with pytest.raises(ValueError, match="trial 21"):
        build_trial_brief(
            batch=batch,
            row_index=1,
            trial_uid=trial_uid,
            immutable_bundle_sha256="d" * 64,
            ack=ack,
            closure=closure,
            outcome_class=TrialOutcomeClass.VALID_PARAMETER_OBSERVATION,
            metric_role=MetricRole.TRAINABLE_OBJECTIVE,
            objective=0.0,
            oracle_status=OracleStatus.CREDIBLE,
            observer_status=ObserverStatus.COMPLETE,
            artifact_digests={"bundle": "d" * 64},
            fingerprint_verified=True,
            legacy_trial_number=21,
        )


@pytest.mark.parametrize("legacy_trial_number", [13, 14, 15, 16, 17, 18, 19, 20, 22])
def test_legacy_trials_remain_diagnostic_only(legacy_trial_number: int) -> None:
    batch = identity()
    trial_uid, ack, closure = receipts(batch)
    brief = build_trial_brief(
        batch=batch,
        row_index=1,
        trial_uid=trial_uid,
        immutable_bundle_sha256="d" * 64,
        ack=ack,
        closure=closure,
        outcome_class=TrialOutcomeClass.VALID_PARAMETER_OBSERVATION,
        metric_role=MetricRole.DIAGNOSTIC_ONLY,
        objective=1.0,
        oracle_status=OracleStatus.CREDIBLE,
        observer_status=ObserverStatus.COMPLETE,
        artifact_digests={"bundle": "d" * 64},
        fingerprint_verified=True,
        legacy_trial_number=legacy_trial_number,
    )
    assert brief.document["optimizer_eligible"] is False
    with pytest.raises(ValueError, match="diagnostic_only"):
        build_trial_brief(
            batch=batch,
            row_index=1,
            trial_uid=trial_uid,
            immutable_bundle_sha256="d" * 64,
            ack=ack,
            closure=closure,
            outcome_class=TrialOutcomeClass.VALID_PARAMETER_OBSERVATION,
            metric_role=MetricRole.TRAINABLE_OBJECTIVE,
            objective=1.0,
            oracle_status=OracleStatus.CREDIBLE,
            observer_status=ObserverStatus.COMPLETE,
            artifact_digests={"bundle": "d" * 64},
            fingerprint_verified=True,
            legacy_trial_number=legacy_trial_number,
        )


@pytest.mark.parametrize(
    ("bundle_sha256", "artifact_digests", "message"),
    [
        ("not-a-sha", {"bundle": "d" * 64}, "immutable bundle"),
        ("d" * 64, {}, "non-empty"),
        ("d" * 64, {"other": "d" * 64}, "bundle artifact"),
    ],
)
def test_trial_brief_rejects_unbound_or_empty_evidence(
    bundle_sha256: str,
    artifact_digests: dict[str, str],
    message: str,
) -> None:
    batch = identity()
    trial_uid, ack, closure = receipts(batch)
    with pytest.raises(ValueError, match=message):
        build_trial_brief(
            batch=batch,
            row_index=1,
            trial_uid=trial_uid,
            immutable_bundle_sha256=bundle_sha256,
            ack=ack,
            closure=closure,
            outcome_class=TrialOutcomeClass.VALID_PARAMETER_OBSERVATION,
            metric_role=MetricRole.TRAINABLE_OBJECTIVE,
            objective=1.0,
            oracle_status=OracleStatus.CREDIBLE,
            observer_status=ObserverStatus.COMPLETE,
            artifact_digests=artifact_digests,
            fingerprint_verified=True,
        )
