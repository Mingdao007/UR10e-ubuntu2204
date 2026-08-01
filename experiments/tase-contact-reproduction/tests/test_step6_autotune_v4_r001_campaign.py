"""Behavior tests for the offline Step6 Figure-Eight r001 campaign contracts."""

from __future__ import annotations

import math
from dataclasses import replace
from decimal import Decimal

import pytest

from step6_autotune_v4_r001 import (
    AnchorAuditReceipt,
    AnchorAuditStatus,
    AttemptDisposition,
    BOOTSTRAP_SLOT_ORDER,
    CampaignContractError,
    CampaignGenesis,
    CampaignIdentityError,
    CampaignPausedError,
    CampaignPhase,
    CampaignResumeError,
    CampaignState,
    CampaignStateError,
    DomainViolationError,
    FORCE_MAE_V2_SPEC_FINGERPRINT,
    FORMAL_BIN_COUNT,
    ForceMetrics,
    ForceObjectiveReceipt,
    ForceSample,
    ForceSampleIdentity,
    IncumbentSeedReceipt,
    Named7dCandidate,
    Named7dDomain,
    SeedReceiptError,
    STEP6_R001_PROFILE,
    assess_attempt,
    build_force_objective_receipt,
    compute_force_metrics,
    plan_bootstrap_pd,
)


_SOURCE_CANDIDATE_UID = "1" * 64
_SOURCE_CLOSURE = "2" * 64
_HANDOFF = "3" * 64
_PACKAGE_ROOT = "4" * 64
_RETURN_RECEIPT = "5" * 64


def _candidate() -> Named7dCandidate:
    return Named7dCandidate(
        P=0.0,
        D=0.0,
        tau=0.5,
        I_on_log2=1.0,
        I_off=0,
        Ko=0.25,
        Kp=2.0,
    )


def _receipt(candidate: Named7dCandidate | None = None) -> IncumbentSeedReceipt:
    return IncumbentSeedReceipt(
        candidate=_candidate() if candidate is None else candidate,
        source_candidate_uid=_SOURCE_CANDIDATE_UID,
        source_identity="step5d.final.named_7d.incumbent",
        source_closure_sha256=_SOURCE_CLOSURE,
        handoff_sha256=_HANDOFF,
    )


def _domain(
    *,
    include_p_lower: bool = True,
    include_p_upper: bool = True,
    include_d_lower: bool = True,
    include_d_upper: bool = True,
) -> Named7dDomain:
    anchor = _candidate()
    p_lower = anchor.P - 0.25
    p_upper = anchor.P + 0.25
    d_lower = anchor.D - 0.25
    d_upper = anchor.D + 0.25
    return Named7dDomain(
        P=tuple(value for value in (p_lower, anchor.P, p_upper) if value == anchor.P or (value == p_lower and include_p_lower) or (value == p_upper and include_p_upper)),
        D=tuple(value for value in (d_lower, anchor.D, d_upper) if value == anchor.D or (value == d_lower and include_d_lower) or (value == d_upper and include_d_upper)),
        tau=(anchor.tau,),
        I_on_log2=(anchor.I_on_log2,),
        I_off=(anchor.I_off,),
        Ko=(anchor.Ko,),
        Kp=(anchor.Kp,),
        source_identity="frozen.parent.domain.handoff",
        source_domain_sha256="6" * 64,
    )


def _bin_time(index: int) -> float:
    return float(Decimal("5.05") + Decimal("0.1") * index)


def _complete_metrics() -> ForceMetrics:
    return compute_force_metrics(_complete_samples())


def _complete_samples(force_n: float = 5.0) -> list[ForceSample]:
    return [
        ForceSample(
            path_time_s=_bin_time(index),
            filtered_normal_n=force_n,
            sample_identity=ForceSampleIdentity(source_id="campaign-golden", sequence=index),
        )
        for index in range(FORMAL_BIN_COUNT)
    ]


def _objective_receipt(force_n: float = 5.0) -> ForceObjectiveReceipt:
    return build_force_objective_receipt(_complete_samples(force_n))


def _genesis(domain: Named7dDomain | None = None) -> CampaignGenesis:
    domain = _domain() if domain is None else domain
    receipt = _receipt()
    return CampaignGenesis.create(
        campaign_id="step6-r001-campaign-golden",
        epoch=1,
        source_closure_sha256=_SOURCE_CLOSURE,
        seed_receipt=receipt,
        domain=domain,
    )


def _state() -> CampaignState:
    receipt = _receipt()
    domain = _domain()
    genesis = _genesis(domain)
    plan = plan_bootstrap_pd(receipt, domain)
    return CampaignState.create(
        genesis,
        plan,
        package_root_sha256=_PACKAGE_ROOT,
    )


def test_seed_receipt_is_exactly_typed_content_addressed_and_has_no_parent_state() -> None:
    receipt = _receipt()
    mapping = receipt.to_mapping()
    assert receipt.candidate.coordinates == (0.0, 0.0, 0.5, 1.0, 0, 0.25, 2.0)
    assert receipt.receipt_digest == IncumbentSeedReceipt.from_mapping(mapping).receipt_digest
    assert receipt.receipt_digest.islower() and len(receipt.receipt_digest) == 64

    forbidden = {
        "gp_state": {},
        "objective": 0.0,
        "observations": [],
        "qualification": True,
        "ledger": {},
        "epoch": 7,
    }
    for field, value in forbidden.items():
        malformed = dict(mapping)
        malformed[field] = value
        with pytest.raises(SeedReceiptError):
            IncumbentSeedReceipt.from_mapping(malformed)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda candidate: {key: value for key, value in candidate.items() if key != "Kp"},
        lambda candidate: {**candidate, "extra_coordinate": 0.0},
        lambda candidate: {**candidate, "P": float("nan")},
        lambda candidate: {**candidate, "I_off": 2},
        lambda candidate: {**candidate, "I_off": 1, "I_on_log2": -0.0},
    ],
)
def test_seed_rejects_malformed_dimensions_nonfinite_categorical_and_noncanonical_values(mutator) -> None:
    mapping = _receipt().to_mapping()
    mapping["candidate"] = mutator(dict(mapping["candidate"]))
    with pytest.raises(SeedReceiptError):
        IncumbentSeedReceipt.from_mapping(mapping)

    drifted = _receipt().to_mapping()
    drifted["receipt_digest"] = "0" * 64
    with pytest.raises(SeedReceiptError):
        IncumbentSeedReceipt.from_mapping(drifted)


def test_domain_fingerprint_is_stable_and_off_domain_candidates_fail_closed() -> None:
    first = _domain()
    second = Named7dDomain.from_mapping(first.to_mapping())
    assert first.semantic_fingerprint == second.semantic_fingerprint
    assert first.domain_fingerprint == first.semantic_fingerprint
    drifted = first.to_mapping()
    drifted["semantic_fingerprint"] = "0" * 64
    with pytest.raises(CampaignContractError):
        # The public parser rejects the fingerprint drift before any campaign can bind it.
        Named7dDomain.from_mapping(drifted)

    off_domain = replace(_candidate(), P=0.125)
    with pytest.raises(DomainViolationError):
        first.validate_candidate(off_domain)


def test_bootstrap_plan_has_exact_order_and_named_coordinate_probe_geometry() -> None:
    receipt = _receipt()
    domain = _domain()
    plan = plan_bootstrap_pd(receipt, domain)
    assert tuple(row.slot for row in plan.rows) == BOOTSTRAP_SLOT_ORDER
    assert tuple(row.index for row in plan.rows) == tuple(range(10))
    assert all(not row.replaced_by_anchor for row in plan.rows)
    assert plan.rows[0].candidate == plan.anchor
    assert plan.rows[3].candidate.P == pytest.approx(-0.25)
    assert plan.rows[5].candidate.P == pytest.approx(0.25)
    assert plan.rows[7].candidate.D == pytest.approx(-0.25)
    assert plan.rows[9].candidate.D == pytest.approx(0.25)
    for index in (3, 5):
        assert plan.rows[index].candidate.D == plan.anchor.D
    for index in (7, 9):
        assert plan.rows[index].candidate.P == plan.anchor.P


def test_bootstrap_replaces_only_off_domain_p_and_d_probe_slots_with_anchor() -> None:
    receipt = _receipt()
    plan = plan_bootstrap_pd(
        receipt,
        _domain(
            include_p_lower=False,
            include_p_upper=False,
            include_d_lower=False,
            include_d_upper=False,
        ),
    )
    for index in (3, 5, 7, 9):
        row = plan.rows[index]
        assert row.replaced_by_anchor is True
        assert row.replacement_reason == "outside_injected_domain"
        assert row.candidate == plan.anchor
    for index in (0, 1, 2, 4, 6, 8):
        assert plan.rows[index].candidate == plan.anchor
        assert plan.rows[index].replaced_by_anchor is False


def test_genesis_binds_locked_identity_and_rejects_old_optimizer_state_payloads() -> None:
    genesis = _genesis()
    restored = CampaignGenesis.from_mapping(genesis.to_mapping())
    assert restored.campaign_fingerprint == genesis.campaign_fingerprint
    assert restored.lineage == STEP6_R001_PROFILE.lineage
    assert restored.profile == STEP6_R001_PROFILE.profile
    assert restored.revision == STEP6_R001_PROFILE.revision
    assert restored.force_mae_spec_fingerprint == FORCE_MAE_V2_SPEC_FINGERPRINT

    for field in ("ledger", "gp", "observations", "qualification", "old_epoch_payload"):
        malformed = genesis.to_mapping()
        malformed[field] = {}
        with pytest.raises(CampaignIdentityError):
            CampaignGenesis.from_mapping(malformed)
    with pytest.raises(CampaignIdentityError):
        replace(genesis, profile="step5d.other")


def test_qualification_is_not_optimizer_eligibility_and_objective_needs_exact_metrics() -> None:
    anchor = _candidate()
    baseline = assess_attempt(anchor, AttemptDisposition.BASELINE_ONLY, qualification_passed=True)
    assert baseline.qualification_passed is True
    assert baseline.optimizer_eligible is False
    assert baseline.objective_value_n is None
    assert baseline.force_metrics is None

    receipt = _objective_receipt()
    objective = assess_attempt(anchor, AttemptDisposition.OBJECTIVE, qualification_passed=True, objective_receipt=receipt)
    assert objective.optimizer_eligible is True
    assert objective.objective_value_n == receipt.metrics.force_mae_v2_n
    assert objective.objective_receipt is receipt
    assert objective.force_metrics.semantic_fingerprint == FORCE_MAE_V2_SPEC_FINGERPRINT
    assert objective.evidence_binding_sha256 is not None

    with pytest.raises(CampaignContractError):
        # A scalar is intentionally not a substitute for complete typed evidence.
        assess_attempt(anchor, AttemptDisposition.OBJECTIVE, qualification_passed=True, force_metrics=1.0)  # type: ignore[arg-type]

    not_qualified = assess_attempt(
        anchor,
        AttemptDisposition.OBJECTIVE,
        qualification_passed=False,
        objective_receipt=receipt,
    )
    assert not_qualified.qualification_passed is False
    assert not_qualified.optimizer_eligible is False
    assert not_qualified.objective_value_n is None


def test_objective_receipt_is_builder_only_and_rejects_forged_metrics() -> None:
    receipt = _objective_receipt(force_n=6.0)
    assert receipt.metrics.force_mae_v2_n == pytest.approx(1.0)
    assert receipt.metrics.force_mae_v1_compat_shadow_n == pytest.approx(1.0)
    assert len(receipt.distinct_samples) == FORMAL_BIN_COUNT
    assert len(receipt.evidence_sha256) == 64

    forged = replace(receipt.metrics, force_mae_v2_n=0.0)
    with pytest.raises(CampaignContractError):
        assess_attempt(
            _candidate(),
            AttemptDisposition.OBJECTIVE,
            qualification_passed=True,
            objective_receipt=forged,  # type: ignore[arg-type]
        )
    with pytest.raises(CampaignContractError):
        assess_attempt(
            _candidate(),
            AttemptDisposition.OBJECTIVE,
            qualification_passed=True,
            force_metrics=forged,
        )
    with pytest.raises(CampaignContractError):
        replace(receipt, metrics=forged)
    with pytest.raises(CampaignContractError):
        ForceObjectiveReceipt(raw_samples=receipt.raw_samples, metrics=receipt.metrics)


@pytest.mark.parametrize(
    "disposition",
    [
        AttemptDisposition.SAFE_NONTRAINABLE,
        AttemptDisposition.HARD_STOP_SAFETY,
        AttemptDisposition.HARD_STOP_RETURN,
        AttemptDisposition.HARD_STOP_CODE_EVIDENCE,
        AttemptDisposition.HARD_STOP_OPTIMIZER,
    ],
)
def test_safe_and_each_hard_stop_are_never_optimizer_eligible(disposition: AttemptDisposition) -> None:
    assessment = assess_attempt(
        _candidate(),
        disposition,
        qualification_passed=True,
    )
    assert assessment.optimizer_eligible is False
    assert assessment.objective_value_n is None


def _paused_state() -> CampaignState:
    state = _state()
    ticket = state.dispatch_next()
    assert ticket is not None and ticket.bootstrap_index == 0
    objective = assess_attempt(
        ticket.candidate,
        AttemptDisposition.OBJECTIVE,
        qualification_passed=True,
        objective_receipt=_objective_receipt(),
    )
    return state.record_attempt(ticket, objective, return_receipt_sha256=_RETURN_RECEIPT)


def test_objective_missing_post_return_receipt_is_rejected_before_recording() -> None:
    state = _state()
    ticket = state.dispatch_next()
    assert ticket is not None
    assessment = assess_attempt(
        ticket.candidate,
        AttemptDisposition.OBJECTIVE,
        qualification_passed=True,
        objective_receipt=_objective_receipt(),
    )
    with pytest.raises(CampaignStateError):
        state.record_attempt(ticket, assessment)
    assert state.phase is CampaignPhase.BOOTSTRAP_PD
    assert state.attempts == ()


def test_first_safe_nontrainable_restarts_epoch_and_forbids_dispatch() -> None:
    state = _state()
    ticket = state.dispatch_next()
    assert ticket is not None
    assessment = assess_attempt(ticket.candidate, AttemptDisposition.SAFE_NONTRAINABLE, qualification_passed=True)
    with pytest.raises(CampaignStateError):
        state.record_attempt(ticket, assessment)
    restarted = state.record_attempt(
        ticket,
        assessment,
        return_receipt_sha256=_RETURN_RECEIPT,
    )
    assert restarted.phase is CampaignPhase.RESTART_REQUIRED
    assert len(restarted.attempts) == 1
    with pytest.raises(CampaignStateError):
        restarted.dispatch_next()


@pytest.mark.parametrize(
    "disposition,return_receipt",
    [
        (AttemptDisposition.HARD_STOP_SAFETY, _RETURN_RECEIPT),
        (AttemptDisposition.HARD_STOP_RETURN, None),
        (AttemptDisposition.HARD_STOP_CODE_EVIDENCE, _RETURN_RECEIPT),
        (AttemptDisposition.HARD_STOP_OPTIMIZER, _RETURN_RECEIPT),
    ],
)
def test_every_first_hard_stop_enters_non_dispatchable_hard_stopped(
    disposition: AttemptDisposition,
    return_receipt: str | None,
) -> None:
    state = _state()
    ticket = state.dispatch_next()
    assert ticket is not None
    stopped = state.record_attempt(
        ticket,
        assess_attempt(ticket.candidate, disposition, qualification_passed=False),
        return_receipt_sha256=return_receipt,
    )
    assert stopped.phase is CampaignPhase.HARD_STOPPED
    assert stopped.next_bootstrap_index == 1
    with pytest.raises(CampaignStateError):
        stopped.dispatch_next()


def test_later_hard_stop_also_halts_after_exact_anchor_resume() -> None:
    paused = _paused_state()
    assert paused.pause_context is not None
    resumed = paused.resume_from_anchor_audit(
        AnchorAuditReceipt.from_context(paused.pause_context, AnchorAuditStatus.PASS)
    )
    ticket = resumed.dispatch_next()
    assert ticket is not None and ticket.bootstrap_index == 1
    stopped = resumed.record_attempt(
        ticket,
        assess_attempt(ticket.candidate, AttemptDisposition.HARD_STOP_SAFETY, qualification_passed=False),
        return_receipt_sha256=_RETURN_RECEIPT,
    )
    assert stopped.phase is CampaignPhase.HARD_STOPPED
    with pytest.raises(CampaignStateError):
        stopped.dispatch_next()


def test_ten_consumed_bootstrap_rows_end_at_ready_for_bo_not_complete() -> None:
    paused = _paused_state()
    assert paused.pause_context is not None
    resumed = paused.resume_from_anchor_audit(AnchorAuditReceipt.from_context(paused.pause_context, AnchorAuditStatus.PASS))
    for index in range(1, 10):
        ticket = resumed.dispatch_next()
        assert ticket is not None and ticket.bootstrap_index == index
        resumed = resumed.record_attempt(
            ticket,
            assess_attempt(ticket.candidate, AttemptDisposition.SAFE_NONTRAINABLE, qualification_passed=True),
            return_receipt_sha256=f"{index + 10:064x}",
        )
    assert resumed.phase is CampaignPhase.READY_FOR_BO
    assert resumed.next_bootstrap_index == 10
    assert resumed.dispatch_next() is None


def test_first_bootstrap_objective_pauses_and_prevents_next_dispatch_or_prefill() -> None:
    paused = _paused_state()
    assert paused.phase is CampaignPhase.PAUSED_FOR_ANCHOR_AUDIT
    assert paused.next_bootstrap_index == 1
    assert paused.pause_context is not None
    assert len(paused.attempts) == 1
    assert paused.pause_context.first_attempt_identity == paused.attempts[0].ticket.attempt_identity
    assert paused.pause_context.candidate_uid == paused.attempts[0].ticket.candidate.candidate_uid
    assert paused.pause_context.objective_receipt_digest == paused.attempts[0].assessment.objective_receipt.receipt_digest  # type: ignore[union-attr]
    assert paused.pause_context.force_metrics_fingerprint == FORCE_MAE_V2_SPEC_FINGERPRINT
    assert paused.pause_context.metrics_binding_sha256 == paused.attempts[0].assessment.objective_receipt.metrics_binding_sha256  # type: ignore[union-attr]
    assert paused.pause_context.return_receipt_sha256 == _RETURN_RECEIPT
    with pytest.raises(CampaignPausedError):
        paused.dispatch_next()


@pytest.mark.parametrize(
    "field",
    [
        "pause_context_fingerprint",
        "campaign_fingerprint",
        "epoch",
        "first_attempt_identity",
        "candidate_uid",
        "objective_receipt_digest",
        "force_metrics_fingerprint",
        "metrics_binding_sha256",
        "evidence_binding_sha256",
        "ledger_head_sha256",
        "source_closure_sha256",
        "package_root_sha256",
        "return_receipt_sha256",
    ],
)
def test_anchor_audit_resume_rejects_every_identity_and_evidence_binding_drift(field: str) -> None:
    paused = _paused_state()
    assert paused.pause_context is not None
    good = AnchorAuditReceipt.from_context(paused.pause_context, AnchorAuditStatus.PASS)
    changed = 2 if field == "epoch" else "f" * 64
    drifted = replace(good, **{field: changed})
    with pytest.raises(CampaignResumeError):
        paused.resume_from_anchor_audit(drifted)


def test_anchor_audit_requires_pass_and_exact_receipt_then_resumes_same_epoch() -> None:
    paused = _paused_state()
    assert paused.pause_context is not None
    with pytest.raises(CampaignResumeError):
        paused.resume_from_anchor_audit(None)
    failed = AnchorAuditReceipt.from_context(paused.pause_context, AnchorAuditStatus.FAIL)
    with pytest.raises(CampaignResumeError):
        paused.resume_from_anchor_audit(failed)

    passed = AnchorAuditReceipt.from_context(paused.pause_context, AnchorAuditStatus.PASS)
    resumed = paused.resume_from_anchor_audit(passed)
    assert resumed.phase == "BOOTSTRAP_PD"
    assert resumed.genesis.epoch == paused.genesis.epoch == 1
    assert resumed.next_bootstrap_index == 1
    next_ticket = resumed.dispatch_next()
    assert next_ticket is not None
    assert next_ticket.bootstrap_index == 1
    assert next_ticket.slot.value == "anchor"
    assert len(resumed.attempts) == 1
    assert resumed.last_audit_receipt == passed


def test_pause_context_change_is_not_accepted_even_when_receipt_is_self_consistent() -> None:
    paused = _paused_state()
    assert paused.pause_context is not None
    drifted_context = replace(paused.pause_context, package_root_sha256="f" * 64)
    drifted_receipt = AnchorAuditReceipt.from_context(drifted_context, AnchorAuditStatus.PASS)
    with pytest.raises(CampaignResumeError):
        paused.resume_from_anchor_audit(drifted_receipt)


def test_state_rejects_arbitrary_replaced_pause_context_and_cross_bound_stored_pass() -> None:
    paused = _paused_state()
    assert paused.pause_context is not None
    arbitrary = replace(paused.pause_context, ledger_head_sha256="f" * 64)
    with pytest.raises(CampaignStateError):
        replace(paused, pause_context=arbitrary)

    passed = AnchorAuditReceipt.from_context(paused.pause_context, AnchorAuditStatus.PASS)
    resumed = paused.resume_from_anchor_audit(passed)
    cross_bound = replace(passed, source_closure_sha256="f" * 64)
    with pytest.raises(CampaignResumeError):
        replace(resumed, last_audit_receipt=cross_bound)


def test_campaign_state_does_not_accept_cross_epoch_or_foreign_attempts() -> None:
    state = _state()
    ticket = state.dispatch_next()
    assert ticket is not None
    foreign_ticket = replace(ticket, campaign_fingerprint="f" * 64)
    with pytest.raises(CampaignIdentityError):
        state.record_attempt(foreign_ticket, assess_attempt(ticket.candidate, AttemptDisposition.BASELINE_ONLY, True))

    with pytest.raises(CampaignStateError):
        CampaignState.create(
            state.genesis,
            state.bootstrap_plan,
            package_root_sha256="not-a-sha",
        )

    with pytest.raises(TypeError):
        CampaignState.create(
            state.genesis,
            state.bootstrap_plan,
            package_root_sha256=_PACKAGE_ROOT,
            return_receipt_sha256=_RETURN_RECEIPT,
        )
