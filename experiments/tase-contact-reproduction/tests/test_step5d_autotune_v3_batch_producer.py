from __future__ import annotations

import hashlib
import json
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = ROOT.parents[1] / "src/ur10e_experiment_runtime"
sys.path.insert(0, str(RUNTIME_SRC))
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_batch_plan import (  # noqa: E402
    PlanLifecycle,
    append_r008_batch,
    initialize_rolling_plan,
    load_plan,
    mark_rolling_plan_open_empty,
)
from step5d_autotune_r008_policy import (  # noqa: E402
    PlannedOccurrence,
    initialization_batch,
    recovery_batch,
)
from step5d_autotune_contract import (  # noqa: E402
    Evaluation,
    ForceCandidate,
    TrialDisposition,
)
from step5d_autotune_optimizer import Observation  # noqa: E402
import step5d_autotune_v3.batch_producer as batch_producer  # noqa: E402
from step5d_autotune_v3.batch_producer import (  # noqa: E402
    PRODUCTION_BATCH_A_SOURCE,
    PRODUCTION_BATCH_B_SOURCE,
    PRODUCTION_RECOVERY_SOURCE,
    _ProductionTruth,
    _VerifiedRuntimeBatch,
    _json_sha256,
    _observations_from_history,
    BatchProducerError,
    BatchProposal,
    ProductionProposalProvider,
    RollingBatchProducer,
)
from step5d_autotune_v3.runtime_profile import (  # noqa: E402
    DEFAULT_OVERLAY,
    ORIENTATION_KO_LATTICE,
    OVERLAY_FIELDS,
    LaunchProfile,
    normalize_trial_overlay,
    normalized_overlay_sha256,
)
from step5d_autotune_v3.state import CampaignPaths, atomic_json, read_strict_json  # noqa: E402
from ur10e_experiment_runtime import ControlCandidateUid  # noqa: E402


CAMPAIGN_ID = "batch-producer-test-campaign"
BINDING = "a" * 64


def _fingerprint(launch_fingerprint: str, batches: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "launch_profile_fingerprint": launch_fingerprint,
                "batches": batches,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _test_launch_profile() -> LaunchProfile:
    policy = {}
    for field in OVERLAY_FIELDS:
        if field == "control_candidate_uid":
            policy[field] = {"derived": "sha256"}
        elif field == "execution_profile_id":
            policy[field] = {"allowed": [DEFAULT_OVERLAY[field]]}
        elif field == "orientation_ko":
            policy[field] = {"allowed": list(ORIENTATION_KO_LATTICE)}
        else:
            policy[field] = {"min": 0.0, "max": 100.0}
    return LaunchProfile(
        document={"test_only": True},
        launch_overrides={},
        trial_overlay_policy=policy,
        fingerprint="c" * 64,
    )


def _catalog() -> tuple[ForceCandidate, ...]:
    return tuple(
        ForceCandidate.from_log2(p=p, damping=damping, i=0.0)
        for p, damping in (
            (-1.0, -1.0),
            (-1.0, 1.0),
            (-0.75, -0.5),
            (0.5, 0.75),
            (0.75, -0.75),
            (1.0, 1.0),
        )
    )


def _evaluation(index: int, objective: float, *, eligible: bool = True) -> Evaluation:
    return Evaluation(
        trial_uid=f"{index:064x}",
        backend_id="production-provider-test",
        eligible=eligible,
        disposition=(
            TrialDisposition.OBJECTIVE
            if eligible
            else TrialDisposition.FAIL_CLOSED
        ),
        objective_mae_n=objective if eligible else None,
        force_bias_n=0.0 if eligible else None,
        force_std_n=0.05 if eligible else None,
        coverage_12_plus_minus_1_ratio=1.0 if eligible else None,
        complete_bins=550 if eligible else 0,
        safe_closure=eligible,
        structural_failures=() if eligible else ("failed_closed",),
    )


def _history_and_runtime_rows():
    candidates = (
        ForceCandidate(),
        ForceCandidate(),
        ForceCandidate(),
        ForceCandidate.from_log2(p=-0.25, damping=0.0, i=0.0),
        ForceCandidate.from_log2(p=0.25, damping=0.0, i=0.0),
        ForceCandidate.from_log2(p=0.0, damping=0.25, i=0.0),
    )
    history = []
    runtime_rows = {}
    for index, candidate in enumerate(candidates, start=1):
        evaluation = _evaluation(index, 0.3 - index * 0.01)
        control_uid = str(
            ControlCandidateUid.from_overlay(
                {
                    "force_p_gain": candidate.force_p_gain,
                    "force_i_gain": candidate.force_i_gain,
                    "force_damping": candidate.force_damping,
                    "orientation_ko": 0.4,
                }
            )
        )
        trial_uid = evaluation.trial_uid
        history.append(
            {
                "history_identity": f"{index + 100:064x}",
                "trial_uid": trial_uid,
                "campaign_id": CAMPAIGN_ID,
                "candidate_uid": candidate.candidate_uid,
                "candidate": candidate.payload(),
                "execution_profile": {"profile_id": "nf100-slew050-a050"},
                "plant_epoch": 1,
                "artifact_provenance": {"csv": {"sha256": f"{index + 200:064x}"}},
                "evaluation": evaluation.history_payload(),
            }
        )
        runtime_rows[trial_uid] = (control_uid, f"{index + 300:064x}", True)
    return history, runtime_rows


def _batch_a_rows(sequence: int) -> tuple[PlannedOccurrence, ...]:
    source = recovery_batch(sequence)
    roles = ("supercycle_anchor", "qlognei_a", "qlognei_a", "qlognei_a", "qlognei_a")
    return tuple(
        PlannedOccurrence(
            logical_batch_sequence=sequence,
            row_index=row.row_index,
            candidate=row.candidate,
            plan_revision=sequence,
            selection_role=role,
            replicate_ordinal=row.replicate_ordinal,
        )
        for row, role in zip(source, roles, strict=True)
    )


def _append_bound_batch(paths: CampaignPaths, profile, rows, source: str):
    bound = []
    overlays = []
    for row in rows:
        raw = {
            **DEFAULT_OVERLAY,
            "force_p_gain": row.candidate.force_p_gain,
            "force_i_gain": row.candidate.force_i_gain,
            "force_damping": row.candidate.force_damping,
        }
        raw.pop("control_candidate_uid")
        overlay = normalize_trial_overlay(raw, profile=profile)
        bound_row = row.bind_control_candidate_uid(overlay["control_candidate_uid"])
        bound.append(bound_row)
        overlays.append(overlay)
    plan = append_r008_batch(paths.candidate_plan, occurrences=bound, source=source)
    batches = []
    if paths.trial_overlays.exists():
        batches = list(
            read_strict_json(paths.trial_overlays, role="test overlay plan")["batches"]
        )
    batches.append(
        {
            "batch_id": plan.revision,
            "source": source,
            "trials": [
                {
                    "occurrence_uid": str(row.occurrence_uid),
                    "transport_candidate_uid": str(row.transport_candidate_uid),
                    "control_candidate_uid": str(row.control_candidate_uid),
                    "normalized_overlay_sha256": normalized_overlay_sha256(
                        profile, overlay
                    ),
                    "overlay": overlay,
                }
                for row, overlay in zip(bound, overlays, strict=True)
            ],
        }
    )
    payload = {
        "schema": "step5d.autotune-v3/trial-overlay-plan-v2",
        "revision": plan.revision,
        "candidate_count": sum(len(batch["trials"]) for batch in batches),
        "launch_profile_fingerprint": profile.fingerprint,
        "fingerprint": _fingerprint(profile.fingerprint, batches),
        "batches": batches,
    }
    atomic_json(paths.trial_overlays, payload)
    return plan, payload


@pytest.fixture
def producer_campaign(tmp_path: Path):
    paths = CampaignPaths(tmp_path / "campaign")
    profile = _test_launch_profile()
    initialize_rolling_plan(paths.candidate_plan, campaign_id=CAMPAIGN_ID)
    _append_bound_batch(
        paths,
        profile,
        initialization_batch(1),
        "canonical_live.initialization_v1",
    )
    mark_rolling_plan_open_empty(paths.candidate_plan)
    return paths, profile


def _producer(paths: CampaignPaths, profile, **kwargs) -> RollingBatchProducer:
    return RollingBatchProducer(
        campaign_root=paths.root,
        campaign_id=CAMPAIGN_ID,
        binding_fingerprint=BINDING,
        launch_profile=profile,
        instance_id="1" * 32,
        **kwargs,
    )


def _assert_full_coherence(paths: CampaignPaths, profile) -> None:
    plan = load_plan(paths.candidate_plan, campaign_id=CAMPAIGN_ID)
    overlays = read_strict_json(paths.trial_overlays, role="test overlay plan")
    assert overlays["revision"] == plan.revision
    assert overlays["candidate_count"] == plan.revision * 5
    assert overlays["fingerprint"] == _fingerprint(
        profile.fingerprint, overlays["batches"]
    )
    for occurrence_batch, overlay_batch in zip(
        plan.occurrences, overlays["batches"], strict=True
    ):
        assert len(occurrence_batch) == len(overlay_batch["trials"]) == 5
        for occurrence, trial in zip(
            occurrence_batch, overlay_batch["trials"], strict=True
        ):
            assert str(occurrence.occurrence_uid) == trial["occurrence_uid"]
            assert str(occurrence.transport_candidate_uid) == trial["transport_candidate_uid"]
            assert str(occurrence.control_candidate_uid) == trial["control_candidate_uid"]
            assert normalized_overlay_sha256(profile, trial["overlay"]) == trial[
                "normalized_overlay_sha256"
            ]
            assert (
                occurrence.candidate.force_p_gain,
                occurrence.candidate.force_i_gain,
                occurrence.candidate.force_damping,
            ) == (
                trial["overlay"]["force_p_gain"],
                trial["overlay"]["force_i_gain"],
                trial["overlay"]["force_damping"],
            )


def test_replenishes_revision_two_and_later_without_closing(
    producer_campaign,
) -> None:
    paths, profile = producer_campaign
    producer = _producer(paths, profile)

    second = producer.poll_once()
    assert second.appended is True
    assert second.recovered is False
    assert second.plan_revision == second.overlay_revision == 2
    assert second.policy == "initialization_batch_2"
    plan = load_plan(paths.candidate_plan, campaign_id=CAMPAIGN_ID)
    assert plan.lifecycle is PlanLifecycle.OPEN_READY
    assert plan.closed is False
    assert plan.closure is None
    _assert_full_coherence(paths, profile)

    mark_rolling_plan_open_empty(paths.candidate_plan)
    third = producer.poll_once()
    assert third.appended is True
    assert third.plan_revision == third.overlay_revision == 3
    assert third.policy == "recovery_batch"
    plan = load_plan(paths.candidate_plan, campaign_id=CAMPAIGN_ID)
    assert plan.lifecycle is PlanLifecycle.OPEN_READY
    assert plan.closed is False
    assert plan.closure is None
    _assert_full_coherence(paths, profile)

    waiting = producer.poll_once()
    assert waiting.phase == "waiting_open_empty"
    assert waiting.appended is False
    assert load_plan(paths.candidate_plan).revision == 3


def test_revision_three_accepts_existing_optimizer_policy_proposal(
    producer_campaign,
) -> None:
    paths, profile = producer_campaign
    producer = _producer(paths, profile)
    producer.poll_once()
    mark_rolling_plan_open_empty(paths.candidate_plan)
    calls = []

    def provider(target_revision: int) -> BatchProposal:
        calls.append(target_revision)
        return BatchProposal(
            recovery_batch(target_revision),
            "existing_optimizer.supercycle_a",
            "supercycle_batch_a",
            {"optimizer_evidence_sha256": "b" * 64},
        )

    result = producer.poll_once(proposal_provider=provider)
    assert calls == [3]
    assert result.policy == "supercycle_batch_a"
    plan = load_plan(paths.candidate_plan)
    assert plan.payload["batches"][-1]["source"] == "existing_optimizer.supercycle_a"
    _assert_full_coherence(paths, profile)


class _InjectedCrash(BaseException):
    pass


def test_exact_candidate_overlay_torn_revision_recovers_once(
    producer_campaign,
) -> None:
    paths, profile = producer_campaign

    def crash(stage: str) -> None:
        if stage == "candidate_appended":
            raise _InjectedCrash(stage)

    with pytest.raises(_InjectedCrash):
        _producer(paths, profile, crash_hook=crash).poll_once()
    assert load_plan(paths.candidate_plan).revision == 2
    assert read_strict_json(paths.trial_overlays, role="test overlays")["revision"] == 1
    assert (paths.control / "batch_producer_intent.json").is_file()

    recovered = _producer(paths, profile).poll_once()
    assert recovered.recovered is True
    assert recovered.plan_revision == recovered.overlay_revision == 2
    assert not (paths.control / "batch_producer_intent.json").exists()
    evidence = read_strict_json(
        paths.control / "batch_producer_evidence.json", role="producer evidence"
    )
    assert evidence["action"] == "recovered_exact_revision"
    _assert_full_coherence(paths, profile)


@pytest.mark.parametrize("corruption", ["fingerprint", "identity"])
def test_overlay_corruption_fails_closed_without_appending(
    producer_campaign,
    corruption: str,
) -> None:
    paths, profile = producer_campaign
    payload = read_strict_json(paths.trial_overlays, role="test overlays")
    if corruption == "fingerprint":
        payload["fingerprint"] = "0" * 64
    else:
        payload["batches"][0]["trials"][0]["occurrence_uid"] = "occ:" + "f" * 64
        payload["fingerprint"] = _fingerprint(profile.fingerprint, payload["batches"])
    atomic_json(paths.trial_overlays, payload)

    with pytest.raises(BatchProducerError) as caught:
        _producer(paths, profile).poll_once()
    assert caught.value.reason_code in {
        "OVERLAY_FINGERPRINT_MISMATCH",
        "OVERLAY_COHERENCE_MISMATCH",
    }
    plan = load_plan(paths.candidate_plan)
    assert plan.revision == 1
    assert plan.lifecycle is PlanLifecycle.OPEN_EMPTY
    heartbeat = read_strict_json(
        paths.control / "batch_producer_heartbeat.json", role="producer heartbeat"
    )
    assert heartbeat["phase"] == "failed_closed"


def test_intent_binding_change_and_unprovable_recovery_fail_closed(
    producer_campaign,
) -> None:
    paths, profile = producer_campaign

    def crash_after_intent(stage: str) -> None:
        if stage == "intent_persisted":
            raise _InjectedCrash(stage)

    with pytest.raises(_InjectedCrash):
        _producer(paths, profile, crash_hook=crash_after_intent).poll_once()
    with pytest.raises(BatchProducerError, match="BINDING_MISMATCH"):
        RollingBatchProducer(
            campaign_root=paths.root,
            campaign_id=CAMPAIGN_ID,
            binding_fingerprint="b" * 64,
            launch_profile=profile,
            instance_id="2" * 32,
        ).poll_once()
    assert load_plan(paths.candidate_plan).revision == 1

    intent_path = paths.control / "batch_producer_intent.json"
    intent = read_strict_json(intent_path, role="producer intent")
    intent["candidate_batch"]["source"] = "tampered-source"
    atomic_json(intent_path, intent)
    with pytest.raises(BatchProducerError) as caught:
        _producer(paths, profile).poll_once()
    assert caught.value.reason_code in {
        "INTENT_FINGERPRINT_MISMATCH",
        "INTENT_INVALID",
    }
    assert load_plan(paths.candidate_plan).revision == 1


def test_production_history_rebuilds_exact_observations_and_control_groups() -> None:
    history, runtime_rows = _history_and_runtime_rows()
    observations, material = _observations_from_history(
        history,
        campaign_id=CAMPAIGN_ID,
        runtime_rows=runtime_rows,
    )
    assert len(observations) == len(material) == 6
    assert all(isinstance(row, Observation) for row in observations)
    assert sum(row.eligible for row in observations) == 6
    assert sum(row.candidate == ForceCandidate() for row in observations) == 3
    assert [row.control_candidate_uid for row in observations] == [
        ControlCandidateUid.parse(runtime_rows[row["trial_uid"]][0])
        for row in history
    ]
    assert material[0]["evaluation"] == history[0]["evaluation"]

    mismatched = dict(runtime_rows)
    first_uid = history[0]["trial_uid"]
    control_uid, bundle_uid, _ = mismatched[first_uid]
    mismatched[first_uid] = (control_uid, bundle_uid, False)
    with pytest.raises(BatchProducerError, match="PRODUCTION_HISTORY_INVALID"):
        _observations_from_history(
            history,
            campaign_id=CAMPAIGN_ID,
            runtime_rows=mismatched,
        )


def test_production_provider_uses_recovery_when_bo_gate_is_false(
    producer_campaign,
    monkeypatch,
) -> None:
    paths, profile = producer_campaign
    _append_bound_batch(
        paths,
        profile,
        initialization_batch(2),
        "canonical_live.initialization_v2",
    )
    mark_rolling_plan_open_empty(paths.candidate_plan)
    provider = ProductionProposalProvider(
        campaign_root=paths.root,
        campaign_id=CAMPAIGN_ID,
        catalog=_catalog(),
    )
    truth = _ProductionTruth((), (), _json_sha256([]), {}, {})
    monkeypatch.setattr(provider, "_load_truth", lambda plan: truth)

    proposal = provider(3)
    assert proposal.policy == "recovery_batch"
    assert proposal.source == PRODUCTION_RECOVERY_SOURCE
    assert tuple(proposal.occurrences) == recovery_batch(3)
    assert proposal.evidence["bo_gate"] is False


def test_production_truth_calls_campaign_store_cold_read(
    producer_campaign,
    monkeypatch,
) -> None:
    paths, profile = producer_campaign
    _append_bound_batch(
        paths,
        profile,
        initialization_batch(2),
        "canonical_live.initialization_v2",
    )
    mark_rolling_plan_open_empty(paths.candidate_plan)
    provider = ProductionProposalProvider(
        campaign_root=paths.root,
        campaign_id=CAMPAIGN_ID,
        catalog=_catalog(),
    )
    history, runtime_rows = _history_and_runtime_rows()
    store_root = paths.root / "store"
    atomic_json(store_root / "campaign.json", {"test": "manifest-presence-only"})
    called = []

    def cold_read(store):
        called.append(store.root)
        return history

    monkeypatch.setattr(batch_producer.CampaignStore, "read_resume_history", cold_read)
    monkeypatch.setattr(
        provider,
        "_verified_runtime_batches",
        lambda **kwargs: (
            {},
            runtime_rows,
            tuple(row["trial_uid"] for row in history),
        ),
    )
    truth = provider._load_truth(load_plan(paths.candidate_plan))
    assert called == [store_root.resolve()]
    assert len(truth.observations) == 6
    assert truth.observation_material_sha256 == _json_sha256(
        list(truth.observation_material)
    )


def test_production_provider_odd_revision_uses_supercycle_a(
    producer_campaign,
    monkeypatch,
) -> None:
    paths, profile = producer_campaign
    _append_bound_batch(
        paths,
        profile,
        initialization_batch(2),
        "canonical_live.initialization_v2",
    )
    mark_rolling_plan_open_empty(paths.candidate_plan)
    provider = ProductionProposalProvider(
        campaign_root=paths.root,
        campaign_id=CAMPAIGN_ID,
        catalog=_catalog(),
    )
    history, runtime_rows = _history_and_runtime_rows()
    observations, material = _observations_from_history(
        history,
        campaign_id=CAMPAIGN_ID,
        runtime_rows=runtime_rows,
    )
    truth = _ProductionTruth(
        observations,
        material,
        _json_sha256(list(material)),
        {row["trial_uid"]: row for row in history},
        {},
    )
    monkeypatch.setattr(provider, "_load_truth", lambda plan: truth)
    calls = []

    def select_a(observed, catalog, *, sequence):
        calls.append((observed, catalog, sequence))
        return _batch_a_rows(sequence), {"selection": "focused-test-a"}

    monkeypatch.setattr(batch_producer, "supercycle_batch_a", select_a)
    proposal = provider(3)
    assert calls == [(observations, _catalog(), 3)]
    assert proposal.source == PRODUCTION_BATCH_A_SOURCE
    assert proposal.policy == "supercycle_batch_a"
    assert proposal.evidence["observation_material_sha256"] == truth.observation_material_sha256


def test_production_provider_even_revision_requires_real_batch_a_closure(
    producer_campaign,
    monkeypatch,
) -> None:
    paths, profile = producer_campaign
    _append_bound_batch(
        paths,
        profile,
        initialization_batch(2),
        "canonical_live.initialization_v2",
    )
    mark_rolling_plan_open_empty(paths.candidate_plan)
    _append_bound_batch(
        paths,
        profile,
        _batch_a_rows(3),
        PRODUCTION_BATCH_A_SOURCE,
    )
    mark_rolling_plan_open_empty(paths.candidate_plan)
    provider = ProductionProposalProvider(
        campaign_root=paths.root,
        campaign_id=CAMPAIGN_ID,
        catalog=_catalog(),
    )
    history, runtime_rows = _history_and_runtime_rows()
    observations, material = _observations_from_history(
        history,
        campaign_id=CAMPAIGN_ID,
        runtime_rows=runtime_rows,
    )
    batch_trial_uids = [row["trial_uid"] for row in history[:5]]
    result = {
        "batch_uid": "d" * 64,
        "rows": [
            {
                "row_index": index,
                "trial_uid": trial_uid,
                "immutable_bundle_sha256": runtime_rows[trial_uid][1],
            }
            for index, trial_uid in enumerate(batch_trial_uids, start=1)
        ],
    }
    result_path = paths.root / "runtime_batches" / ("d" * 64) / "batch_result.json"
    atomic_json(result_path, result)
    result_sha = hashlib.sha256(result_path.read_bytes()).hexdigest()
    verified_batch = _VerifiedRuntimeBatch(
        3,
        SimpleNamespace(batch_uid="d" * 64),
        SimpleNamespace(),
        result,
        result_path,
        result_sha,
    )
    truth = _ProductionTruth(
        observations,
        material,
        _json_sha256(list(material)),
        {row["trial_uid"]: row for row in history},
        {3: verified_batch},
    )
    monkeypatch.setattr(provider, "_load_truth", lambda plan: truth)
    received = []

    def select_b(observed, catalog, *, sequence, batch_a_closure):
        received.append(batch_a_closure)
        return recovery_batch(sequence), {"selection": "focused-test-b"}

    monkeypatch.setattr(
        batch_producer, "supercycle_batch_b_after_gp_update", select_b
    )
    proposal = provider(4)
    assert proposal.source == PRODUCTION_BATCH_B_SOURCE
    assert proposal.policy == "supercycle_batch_b_after_gp_update"
    assert received[0]["batch_result_sha256"] == result_sha
    assert received[0]["cold_read_verified"] is True
    assert proposal.evidence["batch_a_closure"]["batch_result_path"] == str(
        result_path
    )

    atomic_json(result_path, {**result, "tampered": True})
    with pytest.raises(BatchProducerError, match="BATCH_A_CLOSURE_NOT_PROVABLE"):
        provider(4)


def test_module_has_no_public_or_network_entrypoint() -> None:
    source = (
        ROOT / "tools/step5d_autotune_v3/batch_producer.py"
    ).read_text(encoding="utf-8")
    assert "if __name__" not in source
    assert "argparse" not in source
    assert "subprocess" not in source
    assert "socket" not in source
    assert "requests" not in source
