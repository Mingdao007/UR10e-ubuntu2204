from __future__ import annotations

from dataclasses import replace
import ast
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from step6_figure8_autotune_v1 import v5_campaign as campaign_module  # noqa: E402
from step6_figure8_autotune_v1.v5_campaign import (  # noqa: E402
    CampaignConfigV2,
    CampaignIdentityV2,
    CampaignOutcomeV2,
    CampaignPaused,
    CampaignRoleV2,
    OutcomeV2,
    ProposalMethodV2,
    TrialStageV2,
    V5TrialPlan,
    V5_WIRE_EPOCH,
    V5CampaignError,
    V5CampaignV2,
    build_v5_observation_groups,
)
from step6_figure8_autotune_v1.v5_composition_contract import V5AttemptKind, V5TPState  # noqa: E402
from step6_figure8_autotune_v1.v5_lifecycle_ledger import (  # noqa: E402
    BoundaryMode,
    FigureEightPhysicalRecordV2,
    GateFamiliesV2,
    HomeBoundaryV2,
    LedgerRole,
    LifecycleEventKind,
    MetricSnapshotV1,
    OptimizerReceiptV2,
    PathTailClosureV2,
    TellState,
    TrialBoundaryReceiptV2,
    TrialSliceV2,
    V5_EVENT_BUNDLE_SCHEMA,
    V5PhysicalAdmissionLedgerV2,
    canonical_sha256,
    index_sealed_r013_artifact,
)
from step6_figure8_autotune_v1.v5_rollover import CandidateIdentityV1  # noqa: E402

from test_step6_autotuner_v5_lifecycle_ledger import (  # noqa: E402
    _gate_closure,
    _write_artifact,
)


FP = "a" * 64
RELEASE = "b" * 64


def _identity(tmp_path: Path, role: CampaignRoleV2 = CampaignRoleV2.PRIMARY, **kwargs) -> CampaignIdentityV2:
    return CampaignIdentityV2(
        role,
        FP if role is CampaignRoleV2.PRIMARY else "c" * 64,
        RELEASE,
        tmp_path / role.value,
        f"{role.value}:{FP if role is CampaignRoleV2.PRIMARY else 'c' * 64}",
        **kwargs,
    )


def _fixed_controller() -> dict[str, object]:
    return {"force_p_gain": 0.02, "force_damping": 100.0, "force_i_gain": 0.01, "i_off": False, "normal_filter_tau_s": 0.04, "orientation_ko": 0.04, "motion_kp": 2.0}


def _correction_identity(tmp_path: Path) -> CampaignIdentityV2:
    fixed = _fixed_controller()
    return _identity(
        tmp_path,
        CampaignRoleV2.CORRECTION,
        fixed_controller_path=fixed,
        primary_winner_controller_sha256=campaign_module._controller_hash(fixed),
        primary_closeout_sha256="c" * 64,
        primary_ledger_head_sha256="d" * 64,
    )


def _install_fast_mature_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this focused suite offline when scipy is intentionally unavailable."""

    def fresh_pool(self, *, evaluated_keys=(), pending_keys=(), domain="controller_path", count=128, fixed_other_block=None, probe_bounds_open=None, local=False):
        if count != 128:
            raise AssertionError("G4 must request exactly 128 candidates")
        excluded = set(evaluated_keys) | set(pending_keys) | set(self.issued_keys)
        result = []
        while len(result) < 128:
            index = self.cursor_index
            self.cursor_index += 1
            unit = tuple(((index * 1000003 + axis * 9176 + self.seed) % 2147483647) / 2147483646.0 for axis in range(6))
            candidate = self._candidate(unit, domain, fixed_other_block=fixed_other_block, probe_bounds_open=probe_bounds_open, local=local)
            key = campaign_module._candidate(candidate).candidate_key
            if key in excluded:
                continue
            self.issued_keys.add(key)
            excluded.add(key)
            result.append(candidate)
        self._save()
        return tuple(result)

    monkeypatch.setattr(campaign_module.PersistedSobolCursorV1, "fresh_pool", fresh_pool)


def _provider(pool, **kwargs):
    return {
        "candidate": pool[0],
        "acquisition": "qLogNEI",
        "production_provider": "test-qlognei",
        "provider_receipt": {"provider_call": len(pool), "pending": list(kwargs.get("pending_keys", ()))},
    }


def test_v5_observation_groups_emit_worker_schema_and_within_candidate_noise() -> None:
    candidate = campaign_module.CompleteCandidateV1(
        controller_path=_fixed_controller(),
        correction_weights=(0.0,) * 6,
    )
    other = campaign_module.CompleteCandidateV1(
        controller_path={**_fixed_controller(), "motion_kp": 2.1},
        correction_weights=(0.0,) * 6,
    )
    groups = build_v5_observation_groups(
        [
            {"candidate": candidate.as_dict(), "mae_n": 0.5},
            {"candidate": candidate.as_dict(), "mae_n": 0.5},
            {"candidate": other.as_dict(), "mae_n": 1.5},
            {"candidate": other.as_dict(), "mae_n": 1.5},
        ],
        fingerprint_sha256=FP,
    )
    assert len(groups) == 2
    for group in groups:
        payload = group.as_dict()
        assert payload["fingerprint_sha256"] == FP
        assert payload["mae_n"] == payload["mean_n"]
        assert payload["pooled_same_fingerprint_variance_n2"] == payload["pooled_within_fingerprint_variance_n2"]
        assert 1e-4 <= payload["yvar_n2"] <= 2e-2
    assert all(group.sample_variance_n2 == 0.0 for group in groups)
    assert all(group.pooled_within_fingerprint_variance_n2 == 1e-4 for group in groups)


def _real_g3_record(tmp_path: Path, plan, *, role: LedgerRole) -> FigureEightPhysicalRecordV2:
    """Create one cold artifact whose external typed events use this plan identity."""
    path = tmp_path / f"{plan.trial_id}.r013life"
    identity = CandidateIdentityV1(V5_WIRE_EPOCH, plan.candidate_ordinal, plan.attempt_kind, plan.candidate_token)
    receipt, event_bundle = _write_artifact(
        path,
        attempt_count=1,
        identities=(identity,),
    )
    artifact = index_sealed_r013_artifact(path, receipt, event_bundle)
    start = 3
    end = len(artifact.rows)
    metric = MetricSnapshotV1.from_artifact_slice(artifact, candidate_identity=identity, sample_start_index=start, sample_end_index=end, path_clock_start_s=artifact.rows[start]["monotonic_s"])
    home_index = len(artifact.rows) - 1
    home = artifact.rows[home_index]
    boundary = TrialBoundaryReceiptV2(HomeBoundaryV2(V5TPState.READY_HOME_NEXT, home_index, home["monotonic_s"], event_bundle["events"][-1]["event_evidence_sha256"], home["rtde_timestamp_s"]))
    trial_slice = TrialSliceV2(plan.attempt_id, plan.trial_id, identity, start, end, artifact.rows[start]["monotonic_s"], home["monotonic_s"], metric, boundary)
    closure = _gate_closure(artifact, trial_slice)
    return FigureEightPhysicalRecordV2(FP, RELEASE, role, plan.trial_id + "-chain", plan.attempt_id, plan.trial_id, identity, metric, boundary, trial_slice, artifact, closure)
def _append_and_commit(ledger: V5PhysicalAdmissionLedgerV2, record: FigureEightPhysicalRecordV2) -> None:
    if not any(item.trial_id == record.trial_id for item in ledger.records):
        ledger.append_record(record)
    ledger.prepare_tell(record)
    auth = ledger.authorize_tell(record.trial_id)
    receipt = OptimizerReceiptV2(record.trial_id, auth.tell_token, record.record_sha256, "optimizer-" + record.trial_id)
    ledger.reconcile_tell(record.trial_id, receipt)
    ledger.commit_tell(record.trial_id, receipt)


def _append_accounting_outcome(campaign: V5CampaignV2, plan, mae: float, *, home: bool = False) -> CampaignOutcomeV2:
    """Test-only use of the production decision-ledger append seam.

    The real G3 admission is exercised separately above. These accelerated
    rows still use CampaignOutcomeV2 and the exact production JSONL authority;
    they do not create a public admission bypass.
    """
    record_sha = hashlib.sha256(plan.trial_id.encode()).hexdigest()
    physical_path = str((Path(campaign.identity.state_root) / "physical-ledger.jsonl").resolve())
    campaign._append_consume(plan, record_sha)
    boundary = BoundaryMode.HOME if home else BoundaryMode.CONTACT_ROLLOVER
    return campaign._append_outcome(CampaignOutcomeV2(plan.trial_id, plan.candidate_key, plan.stage, OutcomeV2.PHYSICALLY_ELIGIBLE, mae, boundary, record_sha, home_verified=home, tell_state=TellState.COMMITTED if plan.stage in (TrialStageV2.PRIMARY_NOVEL, TrialStageV2.CORRECTION_NOVEL) else None, physical_ledger_path=physical_path))


def test_config_schema_roles_tokens_and_isolated_namespaces(tmp_path: Path):
    config = CampaignConfigV2.from_path(ROOT / "config" / "step6" / "autotuner_v5_campaign_v2.json")
    assert config.version == 2
    raw = json.loads((ROOT / "config" / "step6" / "autotuner_v5_campaign_v2.json").read_text(encoding="utf-8"))
    raw["wire"]["output_integer_registers"] = list(range(24, 36))
    with pytest.raises(V5CampaignError):
        CampaignConfigV2(raw)
    primary = V5CampaignV2.create(_identity(tmp_path, CampaignRoleV2.PRIMARY))
    assert primary.snapshot()["identity"]["role"] == "PRIMARY"
    with pytest.raises(V5CampaignError):
        V5CampaignV2.resume(_identity(tmp_path, CampaignRoleV2.CORRECTION, fixed_controller_path={"force_p_gain": 1.0}, primary_winner_controller_sha256="d" * 64, primary_closeout_sha256="e" * 64, primary_ledger_head_sha256="f" * 64))
    with pytest.raises(V5CampaignError):
        V5CampaignV2.resume(CampaignIdentityV2(CampaignRoleV2.PRIMARY, "d" * 64, RELEASE, primary.identity.state_root, "PRIMARY:" + "d" * 64))
    assert V5_EVENT_BUNDLE_SCHEMA == "step6.autotune/figure8-v5-event-bundle-v2"


def test_schedule_forced_sobol_qlognei_no_fallback_and_prefetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    campaign = V5CampaignV2.create(_identity(tmp_path), qlognei_provider=_provider)
    first = campaign.plan_next()
    assert first is not None and first.proposal_receipt.acquisition is ProposalMethodV2.DETERMINISTIC_SOBOL
    assert first.candidate.correction_weights == (0.0,) * 6
    campaign.record_censor(first, reason="typed-censor")
    second = campaign.plan_next()
    assert second is not None and second.budget_ordinal == 1
    campaign.record_failure(second, signature="not-ready", next_not_ready=True)
    third = campaign.plan_next()
    assert third is not None and third.budget_ordinal == 1
    campaign.record_censor(third, reason="typed-censor")
    plan = campaign.plan_next()
    assert plan is not None and plan.budget_ordinal == 1
    campaign.record_censor(plan, reason="typed-censor")
    # Advance the exact budget slot with non-physical typed outcomes only to
    # inspect the schedule boundary; physical admission is tested separately.
    for _ in range(23):
        current = campaign.plan_next()
        assert current is not None
        campaign.record_censor(current, reason="refill")
    current = campaign.plan_next()
    assert current is not None and current.budget_ordinal == 1
    no_provider = V5CampaignV2.create(_identity(tmp_path / "no-provider"))
    with pytest.raises(V5CampaignError):
        no_provider._proposal(TrialStageV2.PRIMARY_NOVEL, 26)
    assert campaign._method(1) is ProposalMethodV2.DETERMINISTIC_SOBOL
    assert campaign._method(4) is ProposalMethodV2.GLOBAL_SOBOL
    assert campaign._method(25) is ProposalMethodV2.GLOBAL_SOBOL
    assert campaign._method(26) is ProposalMethodV2.QLOGNEI
    campaign.record_failure(current, signature="same")
    for _ in range(2):
        p = campaign.plan_next()
        assert p is not None
        campaign.record_failure(p, signature="same")
    assert campaign.paused
    with pytest.raises(CampaignPaused):
        campaign.plan_next()


def test_ordinal_26_whole_flow_delivers_grouped_observation_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _install_fast_mature_cursor(monkeypatch)
    captured: list[tuple[dict[str, object], ...]] = []

    def provider(pool, **kwargs):
        observations = tuple(dict(row) for row in kwargs["observations"])
        captured.append(observations)
        assert observations
        assert all(
            {
                "candidate",
                "candidate_key",
                "fingerprint_sha256",
                "n",
                "mean_n",
                "mae_n",
                "sample_variance_n2",
                "pooled_within_fingerprint_variance_n2",
                "pooled_same_fingerprint_variance_n2",
                "yvar_n2",
                "schema",
                "version",
            }
            <= set(row)
            for row in observations
        )
        assert all(row["fingerprint_sha256"] == FP for row in observations)
        assert all(1e-4 <= float(row["yvar_n2"]) <= 2e-2 for row in observations)
        return {
            "candidate": pool[0],
            "acquisition": "qLogNEI",
            "production_provider": "ordinal-26-fixture",
            "provider_receipt": {"ordinal": 26},
        }

    campaign = V5CampaignV2.create(
        _identity(tmp_path / "ordinal-26"), qlognei_provider=provider
    )
    for ordinal in range(1, 26):
        plan = campaign.plan_next()
        assert plan is not None and plan.budget_ordinal == ordinal
        _append_accounting_outcome(campaign, plan, 0.5 + ordinal * 1e-3)
    plan26 = campaign.plan_next()
    assert plan26 is not None and plan26.budget_ordinal == 26
    assert plan26.proposal_receipt.acquisition is ProposalMethodV2.QLOGNEI
    assert captured
    assert len(captured[-1]) == 25


def test_real_g3_record_and_exactly_once_commit_are_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    campaign = V5CampaignV2.create(_identity(tmp_path / "campaign"))
    plan = campaign.plan_next()
    assert plan is not None
    record = _real_g3_record(tmp_path / "source", plan, role=LedgerRole.PRIMARY)
    physical = V5PhysicalAdmissionLedgerV2(tmp_path / "physical.jsonl", campaign_fingerprint=FP, release_identity_sha256=RELEASE, role=LedgerRole.PRIMARY)
    physical.append_record(record)
    with pytest.raises(V5CampaignError):
        campaign.record_physical_result(plan, record, physical)
    _append_and_commit(physical, record)
    outcome = campaign.record_physical_result(plan, record, physical)
    assert outcome.disposition is OutcomeV2.PHYSICALLY_ELIGIBLE
    assert outcome.tell_state is TellState.COMMITTED
    assert campaign.exact_novel_count == 1
    resumed = V5CampaignV2.resume(_identity(tmp_path / "campaign"), qlognei_provider=_provider)
    assert resumed.exact_novel_count == 1
    assert resumed.record_physical_result(plan, record, physical) == outcome


def test_consume_record_hash_is_crash_safe_before_and_after_cold_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    identity = _identity(tmp_path)
    campaign = V5CampaignV2.create(identity)
    plan = campaign.plan_next()
    assert plan is not None
    record_a = "a" * 64
    record_b = "b" * 64
    physical_path = str((Path(identity.state_root) / "physical-ledger.jsonl").resolve())
    campaign._append_consume(plan, record_a)
    with pytest.raises(V5CampaignError):
        campaign._append_consume(plan, record_b)
    forged_b = CampaignOutcomeV2(plan.trial_id, plan.candidate_key, plan.stage, OutcomeV2.PHYSICALLY_ELIGIBLE, 1.0, BoundaryMode.CONTACT_ROLLOVER, record_b, physical_ledger_path=physical_path, tell_state=TellState.COMMITTED)
    with pytest.raises(V5CampaignError):
        campaign._append_outcome(forged_b)
    wrong_stage = CampaignOutcomeV2(plan.trial_id, plan.candidate_key, TrialStageV2.PRIMARY_CONFIRM, OutcomeV2.PHYSICALLY_ELIGIBLE, 1.0, BoundaryMode.HOME, record_a, home_verified=True, physical_ledger_path=physical_path)
    with pytest.raises(V5CampaignError):
        campaign._append_outcome(wrong_stage)
    cold = V5CampaignV2.resume(identity)
    with pytest.raises(V5CampaignError):
        cold._append_consume(plan, record_b)
    with pytest.raises(V5CampaignError):
        cold._append_outcome(forged_b)
    with pytest.raises(V5CampaignError):
        cold._append_outcome(wrong_stage)
    assert len(cold._ledger.rows) == 3


def test_wire_session_epoch_is_transport_only_and_campaign_state_has_no_epoch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    campaign = V5CampaignV2.create(_identity(tmp_path))
    plan = campaign.plan_next()
    assert plan is not None
    assert plan.as_dict()["epoch"] == V5_WIRE_EPOCH == 1
    assert plan.candidate_identity["epoch"] == 1
    assert "epoch" not in campaign.snapshot()["identity"]
    assert "epoch" not in campaign._marker()["identity"]
    mapping = plan.as_dict()
    mapping["epoch"] = 0
    with pytest.raises(V5CampaignError):
        V5TrialPlan.from_mapping(mapping)


def test_home_only_primary_plans_are_unpacked_and_home_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fast_mature_cursor(monkeypatch)
    from step6_figure8_autotune_v1.v5_campaign import ENTRY_MODE_HOME_ONLY_V1

    campaign = V5CampaignV2.create(
        _identity(tmp_path, entry_mode=ENTRY_MODE_HOME_ONLY_V1)
    )
    plan = campaign.plan_next()
    assert plan is not None
    assert plan.entry_mode == ENTRY_MODE_HOME_ONLY_V1
    assert plan.requires_home is True
    assert plan.packable is False
    assert campaign.build_pending_chain(max_attempts=5) == (plan,)
    assert campaign.prefetch_next() is None


def test_wire_ordinal_never_reuses_budget_ordinal_and_epoch_rebind_requires_safe_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fast_mature_cursor(monkeypatch)
    identity = _identity(tmp_path)
    campaign = V5CampaignV2.create(identity, wire_session_epoch=77)
    first = campaign.plan_next()
    assert first is not None
    assert first.budget_ordinal == 1
    assert first.candidate_ordinal == first.dispatch_index == 1
    assert first.wire_epoch == 77
    with pytest.raises(V5CampaignError, match="safe checkpoint"):
        V5CampaignV2.resume(identity, wire_session_epoch=78)

    campaign.record_censor(first, reason="typed-refill")
    rebound = V5CampaignV2.resume(identity, wire_session_epoch=78)
    second = rebound.plan_next()
    assert second is not None
    assert second.budget_ordinal == 1
    assert second.candidate_ordinal == second.dispatch_index == 2
    assert second.wire_epoch == 78


def test_prefetch_success_use_and_censor_burn_are_durable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    success = V5CampaignV2.create(_identity(tmp_path / "success"))
    current = success.plan_next()
    assert current is not None
    prefetched = success.prefetch_next()
    assert prefetched is not None and prefetched.prefetched
    with pytest.raises(V5CampaignError):
        success.record_censor(prefetched, reason="current-unresolved")
    _append_accounting_outcome(success, current, 1.0)
    usable = success.plan_next()
    assert usable == prefetched and usable.budget_ordinal == success.exact_novel_count + 1
    assert any(row["record_type"] == "prefetch_activate" and row["trial_id"] == prefetched.trial_id for row in success._ledger.rows)
    next_prefetched = success.prefetch_next()
    assert next_prefetched is not None and next_prefetched.budget_ordinal == 3
    resumed = V5CampaignV2.resume(_identity(tmp_path / "success"))
    assert resumed._active_plans() == (prefetched,)
    assert resumed._prefetched() == (next_prefetched,)
    with pytest.raises(V5CampaignError):
        resumed.plan_next()
    success._ledger.append("prefetch_activate", {"trial_id": prefetched.trial_id, "candidate_key": prefetched.candidate_key, "budget_ordinal": prefetched.budget_ordinal})
    with pytest.raises(V5CampaignError):
        V5CampaignV2.resume(_identity(tmp_path / "success"))

    burned = V5CampaignV2.create(_identity(tmp_path / "burned"))
    old = burned.plan_next()
    assert old is not None
    burned_prefetch = burned.prefetch_next()
    assert burned_prefetch is not None
    burned.record_censor(old, reason="typed-censor")
    replacement = burned.plan_next()
    assert replacement is not None and replacement.budget_ordinal == 1 and replacement.candidate_key != burned_prefetch.candidate_key
    assert any(row["record_type"] == "prefetch_discard" for row in burned._ledger.rows)
    cold = V5CampaignV2.resume(_identity(tmp_path / "burned"))
    assert next(plan for plan in cold.plans if plan.trial_id == replacement.trial_id) == replacement

    failed = V5CampaignV2.create(_identity(tmp_path / "failed"))
    failed_current = failed.plan_next()
    assert failed_current is not None
    failed_prefetch = failed.prefetch_next()
    assert failed_prefetch is not None
    failed.record_failure(failed_current, signature="ordinary-failure")
    failed_replacement = failed.plan_next()
    assert failed_replacement is not None and failed_replacement.candidate_key != failed_prefetch.candidate_key
    assert any(row["record_type"] == "prefetch_discard" for row in failed._ledger.rows)


def test_bounded_five_attempt_pending_chain_cold_resumes_and_activates_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fast_mature_cursor(monkeypatch)
    identity = _identity(tmp_path)
    campaign = V5CampaignV2.create(identity, qlognei_provider=_provider)
    current = campaign.plan_next()
    assert current is not None
    chain = campaign.build_pending_chain(max_attempts=5)
    assert len(chain) == 5 and chain[0] == current
    for index, plan in enumerate(chain[1:], 1):
        assert plan.prefetched
        assert plan.proposal_receipt.pending_candidate_keys == tuple(
            item.candidate_key for item in chain[:index]
        )
    resumed = V5CampaignV2.resume(identity, qlognei_provider=_provider)
    assert resumed.build_pending_chain(max_attempts=5) == chain
    for index, plan in enumerate(chain):
        if index:
            assert resumed.plan_next() == plan
        _append_accounting_outcome(resumed, plan, 1.0 + index / 10.0)
    assert resumed.exact_novel_count == 5
    assert resumed._active_plans() == ()
    assert resumed._prefetched() == ()


def test_correction_qlognei_has_only_admitted_correction_observations_and_pending_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    captures: list[dict[str, object]] = []

    def provider(pool, **kwargs):
        captures.append(dict(kwargs))
        return _provider(pool, **kwargs)

    identity = _correction_identity(tmp_path)
    campaign = V5CampaignV2.create(identity, qlognei_provider=provider)
    for ordinal in range(1, 13):
        plan = campaign.plan_next()
        assert plan is not None and plan.stage is TrialStageV2.CORRECTION_NOVEL
        _append_accounting_outcome(campaign, plan, float(ordinal))
    current = campaign.plan_next()
    assert current is not None and current.budget_ordinal == 13
    prefetch = campaign.prefetch_next()
    assert prefetch is not None and prefetch.budget_ordinal == 14
    assert captures
    assert all(item["candidate_key"] in {plan.candidate_key for plan in campaign.plans if plan.stage is TrialStageV2.CORRECTION_NOVEL} for item in captures[-1]["observations"])
    assert captures[-1]["pending_keys"] == (current.candidate_key,)
    assert captures[-1]["pending_candidates"] == (current.candidate.as_dict(),)
    assert prefetch.proposal_receipt.provider_receipt["pending_candidate_keys"] == [current.candidate_key]


def test_failure_streak_is_only_consecutive_ordinary_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    campaign = V5CampaignV2.create(_identity(tmp_path))
    first = campaign.plan_next()
    assert first is not None
    campaign.record_failure(first, signature="same")
    next_not_ready = campaign.plan_next()
    assert next_not_ready is not None
    campaign.record_failure(next_not_ready, signature="same", next_not_ready=True)
    third = campaign.plan_next()
    assert third is not None
    campaign.record_failure(third, signature="same")
    campaign.record_censor(campaign.plan_next(), reason="censor-resets")
    for _ in range(2):
        campaign.record_failure(campaign.plan_next(), signature="same")
    assert not campaign.paused
    campaign.record_failure(campaign.plan_next(), signature="same")
    assert campaign.paused

    forged = V5CampaignV2.create(_identity(tmp_path / "forged-pause"))
    for _ in range(2):
        forged.record_failure(forged.plan_next(), signature="same")
    forged._ledger.append("pause", {"reason": "three_identical_failure_signatures", "failure_signature": "same"})
    with pytest.raises(V5CampaignError):
        V5CampaignV2.resume(_identity(tmp_path / "forged-pause"))


def test_strict_nested_plan_outcome_and_checkpoint_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    campaign = V5CampaignV2.create(_identity(tmp_path))
    plan = campaign.plan_next()
    assert plan is not None
    plan_mapping = plan.as_dict()
    plan_mapping["unexpected"] = True
    with pytest.raises(V5CampaignError):
        V5TrialPlan.from_mapping(plan_mapping)
    outcome_mapping = CampaignOutcomeV2(plan.trial_id, plan.candidate_key, plan.stage, OutcomeV2.CENSORED, None, None, failure_signature="typed").as_dict()
    outcome_mapping["unexpected"] = True
    with pytest.raises(V5CampaignError):
        CampaignOutcomeV2.from_mapping(outcome_mapping)
    campaign.write_checkpoint()
    checkpoint = json.loads(campaign.checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["unexpected"] = True
    campaign.checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    with pytest.raises(V5CampaignError):
        V5CampaignV2.resume(_identity(tmp_path))
    with pytest.raises(V5CampaignError):
        CampaignOutcomeV2(plan.trial_id, plan.candidate_key, TrialStageV2.PRIMARY_CONFIRM, OutcomeV2.CENSORED, None, None, failure_signature="wrong-stage")
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    (legacy_root / "namespace.json").write_text(json.dumps({"schema": "step6.autotune/figure8-campaign-v1", "version": 1}), encoding="utf-8")
    with pytest.raises(V5CampaignError):
        V5CampaignV2.resume(_identity(legacy_root))


def test_candidate_token_collision_is_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    campaign = V5CampaignV2.create(_identity(tmp_path))
    monkeypatch.setattr(campaign_module, "_candidate_token", lambda _key: 17)
    first = campaign.plan_next()
    assert first is not None
    with pytest.raises(V5CampaignError):
        campaign.plan_next()


def test_correction_identity_hash_binds_the_fixed_controller_on_create_and_resume(tmp_path: Path):
    fixed = _fixed_controller()
    with pytest.raises(V5CampaignError):
        CampaignIdentityV2(CampaignRoleV2.CORRECTION, "c" * 64, RELEASE, tmp_path / "bad", "CORRECTION:" + "c" * 64, fixed_controller_path=fixed, primary_winner_controller_sha256="e" * 64, primary_closeout_sha256="c" * 64, primary_ledger_head_sha256="d" * 64)
    identity = _correction_identity(tmp_path / "good")
    campaign = V5CampaignV2.create(identity)
    marker = json.loads((Path(identity.state_root) / "namespace.json").read_text(encoding="utf-8"))
    marker["identity"]["primary_winner_controller_sha256"] = "e" * 64
    (Path(identity.state_root) / "namespace.json").write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(V5CampaignError):
        V5CampaignV2.resume(identity)


def test_primary_exact_budget_and_home_confirmations_are_accounted_separately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    campaign = V5CampaignV2.create(_identity(tmp_path / "campaign"), qlognei_provider=_provider)
    novel_maes: dict[str, float] = {}
    for ordinal in range(1, 201):
        plan = campaign.plan_next()
        assert plan is not None and plan.stage is TrialStageV2.PRIMARY_NOVEL
        novel_maes[plan.candidate_key] = {1: 0.0, 2: 0.1, 3: 0.2}.get(ordinal, 5.0)
        _append_accounting_outcome(campaign, plan, novel_maes[plan.candidate_key])
    assert campaign.exact_novel_count == 200
    confirmation_maes = (1.0, 0.99, 5.0)
    confirmation_index = 0
    for _ in range(12):
        plan = campaign.plan_next()
        assert plan is not None and plan.stage is TrialStageV2.PRIMARY_CONFIRM and plan.requires_home
        _append_accounting_outcome(campaign, plan, confirmation_maes[confirmation_index % 3], home=True)
        confirmation_index += 1
    report = campaign.report()
    assert report.exact_novel_count == 200
    assert report.winner_candidate_key == report.top_candidates[0]["candidate_key"]
    assert len([item for item in campaign.outcomes if item.stage is TrialStageV2.PRIMARY_CONFIRM]) == 12
    assert all(item.home_verified for item in campaign.outcomes if item.stage is TrialStageV2.PRIMARY_CONFIRM)
    assert all(row["total_n"] == 5 and len(row["confirmation_maes_n"]) == 4 for row in report.top_candidates)
    assert report.top_candidates[0]["original_mae_n"] == 0.0
    assert report.top_candidates[0]["repeated_mean_n"] == pytest.approx(0.8)
    assert report.top_candidates[1]["repeated_mean_n"] == pytest.approx(0.812)
    assert report.winner_candidate_key != report.top_candidates[1]["candidate_key"]


def test_tamper_checkpoint_nested_identity_and_optional_telemetry_never_admit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    campaign = V5CampaignV2.create(_identity(tmp_path), qlognei_provider=_provider)
    plan = campaign.plan_next()
    assert plan is not None
    campaign.write_checkpoint()
    lines = campaign.decision_ledger_path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["plan"]["candidate_token"] += 1
    row["row_sha256"] = campaign_module._sha({key: value for key, value in row.items() if key != "row_sha256"})
    lines[1] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    campaign.decision_ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(V5CampaignError):
        V5CampaignV2.resume(_identity(tmp_path), qlognei_provider=_provider)


def test_correction_is_independent_and_fixed_controller_is_not_primary_observation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    fixed = {"force_p_gain": 0.02, "force_damping": 100.0, "force_i_gain": 0.01, "i_off": False, "normal_filter_tau_s": 0.04, "orientation_ko": 0.04, "motion_kp": 2.0}
    correction_id = _identity(tmp_path, CampaignRoleV2.CORRECTION, fixed_controller_path=fixed, primary_winner_controller_sha256=hashlib.sha256(json.dumps(fixed, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), primary_closeout_sha256="c" * 64, primary_ledger_head_sha256="d" * 64)
    campaign = V5CampaignV2.create(correction_id, qlognei_provider=_provider)
    plan = campaign.plan_next()
    assert plan is not None and plan.stage is TrialStageV2.CORRECTION_NOVEL
    assert plan.proposal_receipt.acquisition is ProposalMethodV2.GLOBAL_SOBOL
    assert campaign._method(13) is ProposalMethodV2.QLOGNEI
    assert campaign._method(15) is ProposalMethodV2.GLOBAL_SOBOL
    assert plan.candidate.controller_path == fixed
    campaign.record_censor(plan, reason="fixture-refill")
    assert campaign.exact_novel_count == 0
    with pytest.raises(V5CampaignError):
        V5CampaignV2.resume(CampaignIdentityV2(CampaignRoleV2.CORRECTION, correction_id.campaign_fingerprint, RELEASE, correction_id.state_root, correction_id.observation_namespace, fixed_controller_path={**fixed, "motion_kp": 3.0}, primary_winner_controller_sha256=correction_id.primary_winner_controller_sha256, primary_closeout_sha256=correction_id.primary_closeout_sha256, primary_ledger_head_sha256=correction_id.primary_ledger_head_sha256), qlognei_provider=_provider)


def test_correction_60_then_strict_matched_closeout_cold_resumes_without_promotion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fast_mature_cursor(monkeypatch)
    captures: list[dict[str, object]] = []

    def provider(pool, **kwargs):
        captures.append(dict(kwargs))
        return _provider(pool, **kwargs)

    identity = _correction_identity(tmp_path)
    campaign = V5CampaignV2.create(identity, qlognei_provider=provider)
    for ordinal in range(1, 61):
        plan = campaign.plan_next()
        assert plan is not None and plan.stage is TrialStageV2.CORRECTION_NOVEL and plan.budget_ordinal == ordinal
        _append_accounting_outcome(campaign, plan, 0.1 if ordinal == 1 else 0.2 + ordinal / 1000.0)
    assert campaign.exact_novel_count == 60
    assert captures and captures[-1]["observations"]
    assert all(item["candidate_key"] in {outcome.candidate_key for outcome in campaign.outcomes if outcome.stage is TrialStageV2.CORRECTION_NOVEL} for item in captures[-1]["observations"])
    assert all(
        item["fingerprint_sha256"] == identity.campaign_fingerprint
        and 1e-4 <= item["yvar_n2"] <= 2e-2
        and item["n"] >= 1
        for item in captures[-1]["observations"]
    )
    matched_order = (
        TrialStageV2.MATCHED_ZERO,
        TrialStageV2.MATCHED_CORRECTED,
        TrialStageV2.MATCHED_CORRECTED,
        TrialStageV2.MATCHED_ZERO,
        TrialStageV2.MATCHED_ZERO,
        TrialStageV2.MATCHED_CORRECTED,
        TrialStageV2.MATCHED_CORRECTED,
        TrialStageV2.MATCHED_ZERO,
        TrialStageV2.MATCHED_ZERO,
        TrialStageV2.MATCHED_CORRECTED,
    )
    for index, expected_stage in enumerate(matched_order):
        plan = campaign.plan_next()
        assert plan is not None
        assert plan.stage is expected_stage
        _append_accounting_outcome(
            campaign,
            plan,
            1.0 if expected_stage is TrialStageV2.MATCHED_ZERO else 0.25,
            home=True,
        )
    report = campaign.report()
    assert report.exact_novel_count == 60
    assert report.corrected_best_candidate_key is not None
    assert report.winner_candidate_key is None and report.primary_winner_controller_sha256 == identity.primary_winner_controller_sha256
    assert report.matched_zero_maes_n == (1.0,) * 5
    assert report.matched_corrected_maes_n == (0.25,) * 5
    assert report.paired_differences == (-0.75,) * 5
    assert report.paired_df == 4 and report.paired_ci90_n == pytest.approx((-0.75, -0.75))
    assert report.automatic_promotion is False and report.decision == "improvement"
    resumed = V5CampaignV2.resume(identity, qlognei_provider=provider)
    assert resumed.report() == report
    assert all(plan.candidate.controller_path == identity.fixed_controller_path for plan in campaign.plans if plan.stage is TrialStageV2.CORRECTION_NOVEL)
    lines = campaign.decision_ledger_path.read_text(encoding="utf-8").splitlines()
    ci_tampered = json.loads(lines[-1])
    ci_tampered["report"]["paired_ci90_n"][0] += 1.0
    ci_tampered["row_sha256"] = campaign_module._sha({key: value for key, value in ci_tampered.items() if key != "row_sha256"})
    ci_lines = [*lines]
    ci_lines[-1] = json.dumps(ci_tampered, sort_keys=True, separators=(",", ":"))
    campaign.decision_ledger_path.write_text("\n".join(ci_lines) + "\n", encoding="utf-8")
    with pytest.raises(V5CampaignError):
        V5CampaignV2.resume(identity, qlognei_provider=provider)
    campaign.decision_ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    closeout = json.loads(lines[-1])
    closeout["report"]["corrected_best_candidate"]["motion_kp"] += 1.0
    closeout["row_sha256"] = campaign_module._sha({key: value for key, value in closeout.items() if key != "row_sha256"})
    lines[-1] = json.dumps(closeout, sort_keys=True, separators=(",", ":"))
    campaign.decision_ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(V5CampaignError):
        V5CampaignV2.resume(identity, qlognei_provider=provider)


def test_no_live_transport_or_force_window_dependency():
    source_path = ROOT / "tools" / "step6_figure8_autotune_v1" / "v5_campaign.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(source_path))
    live_names = ("rtde", "kunwei", "sensor", "transport", "controller", "motion")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any(any(part in name.lower() for part in live_names) for name in names)
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tell_exact" for node in ast.walk(tree))
    assert "4-6" not in source and "3-7" not in source and "0.30" not in source and "7N" not in source
