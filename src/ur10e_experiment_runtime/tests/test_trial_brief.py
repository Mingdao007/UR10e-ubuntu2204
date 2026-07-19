from __future__ import annotations

import json

import pytest

from ur10e_experiment_runtime.batch import (
    BatchIdentity,
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
    return BatchIdentity("a" * 64, "b" * 64, 1, tuple(rows))


def receipts(batch: BatchIdentity, row_index: int = 1):
    trial_uid = "c" * 64
    ack = ExactAckReceipt(batch.batch_uid, row_index, trial_uid, batch.rows[row_index - 1].control_candidate_uid, 1, 2, 2)
    closure = SafeClosureReceipt(batch.batch_uid, row_index, trial_uid, ack.ack_uid, return_reference_for_row(row_index))
    return trial_uid, ack, closure


def test_trial_brief_uses_actual_overlay_and_publishes_once(tmp_path) -> None:
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
    assert brief.document["trial_overlay"] == batch.rows[0].trial_overlay
    assert brief.document["optimizer_eligible"] is True
    sink = EvidenceSink(tmp_path)
    first = sink.publish_trial_brief(brief)
    assert sink.publish_trial_brief(brief) == first
    assert json.loads(first.read_text())["publication_uid"] == brief.publication_uid


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
