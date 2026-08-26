from __future__ import annotations

import copy
from dataclasses import replace
import json
import math
from pathlib import Path
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(REPO / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r013.domain import candidate_to_log_features  # noqa: E402
from step5d_autotune_v4_r013.figure8_transfer import (  # noqa: E402
    FigureEightLaunchPackageV1,
    FigureEightTransferError,
    FigureEightTransferSeedManifestV1,
    load_figure8_transfer_template,
    materialize_figure8_launch_package,
    materialize_figure8_transfer_seed_manifest,
)
from step5d_autotune_v4_r013.checkpoint import build_checkpoint_receipt  # noqa: E402
from step5d_autotune_v4_r013.floor_coordinator import (  # noqa: E402
    CORE_BO_NOVEL,
    CORE_PROPOSAL_CONTRACT,
    CORE_SOBOL_NOVEL,
    CORRECTION_PROPOSAL_CONTRACT,
    POLISH_CORE_NOVEL,
    POLISH_CORRECTION_NOVEL,
    SENTINEL,
    ControllerCoreBlockV1,
    CorrectionBlockV1,
    FloorCandidateProposal,
    FloorCoordinatorError,
    FloorDiscoveryCoordinator,
    FloorDiscoveryPolicyV1,
    RuntimePrimitiveNotInstalled,
    HandoffABPlanV1,
)
from step5d_autotune_v4_r013.gp import (  # noqa: E402
    CORRECTION_MODEL_DIMENSION_COUNT,
    CorrectionObservationGroupV1,
    CorrectionQLogNEIProposalProviderV1,
    R013GPError,
    ask_correction_qlognei,
    fit_correction_production_gp,
    validate_correction_observation_groups,
)
from step5d_autotune_v4_r013.identity import CampaignFingerprint  # noqa: E402
from step5d_autotune_v4_r013.campaign_config import (  # noqa: E402
    R013CampaignConfigError,
    R013RuntimeInstallationReceiptV1,
    controller_source_identity_sha256,
    home_tare_procedure_identity,
    load_r013_budgeted_floor_config,
    materialize_campaign_fingerprint,
    materialize_r013_handoff_selection,
    materialize_r013_live_ready_config,
)
from step5d_autotune_v4_r013.metrics import (  # noqa: E402
    GapPreservingMetricAccumulatorV1,
    MetricGapError,
    MetricFingerprintV1,
)
from step5d_autotune_v4_r013.path_context import (  # noqa: E402
    CycloidPathProviderV1,
    PathContextError,
    PathIdentityReceiptV1,
    PlanarBasisReceiptV1,
)


def _fingerprint(suffix: str = "a") -> CampaignFingerprint:
    return CampaignFingerprint(
        path_id="r013_cycloid_v1",
        metric_fingerprint="force-mae-v2-sealed|formal=[5,60)|bin=0.1s|target=5N",
        handoff_policy="freeze_carry_v1",
        correction_runtime_strategy_identity="offline-correction-v2",
        source_identity=f"source-{suffix}",
        eoat_identity="new_eoat_v4_profile",
        home_tare_identity="fixed-home-tare-v1",
        controller_lineage="step5d-r013-offline",
    )


def _correction_groups(fingerprint: CampaignFingerprint) -> tuple[CorrectionObservationGroupV1, ...]:
    return tuple(
        CorrectionObservationGroupV1(
            campaign_fingerprint=fingerprint,
            weights=tuple(-0.42 + 0.07 * ((index + offset) % 6) for offset in range(6)),
            n=3 + index,
            mean_n=0.5 + 0.03 * index,
            yvar_n2=0.001 + 0.0001 * index,
        )
        for index in range(6)
    )


def _core_provider(pool: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> FloorCandidateProposal:
    candidate = pool[0]
    return FloorCandidateProposal(
        candidate=candidate,
        block_input=tuple(candidate_to_log_features(candidate)[index] for index in (0, 1, 2, 5)),
        contract=CORE_PROPOSAL_CONTRACT,
        acquisition_value=0.125,
        posterior_beating_probability=0.0,
        fit_receipt={"backend": "test-provider", "model_dimensions": 4},
    )


def _correction_provider(pool: tuple[tuple[float, ...], ...]) -> FloorCandidateProposal:
    return FloorCandidateProposal(
        candidate={},
        block_input=tuple(pool[0]),
        contract=CORRECTION_PROPOSAL_CONTRACT,
        acquisition_value=0.25,
        posterior_beating_probability=0.0,
        fit_receipt={"backend": "test-provider", "model_dimensions": 6},
    )


def _coordinator(*, fingerprint: str = "repair", qualified: bool = True) -> FloorDiscoveryCoordinator:
    coordinator = FloorDiscoveryCoordinator(
        FloorDiscoveryPolicyV1(),
        fingerprint=fingerprint,
        core_proposal_provider=_core_provider,
        correction_proposal_provider=_correction_provider,
    )
    qualified_token: str | None = None
    while coordinator.novel_count < 100:
        request = coordinator.next_request()
        if request.role == CORE_SOBOL_NOVEL and qualified_token is None:
            qualified_token = request.canonical_token
        is_qualified_group = qualified and request.canonical_token == qualified_token
        coordinator.record_result(
            admitted=True,
            sealed_mae_n=0.5 if is_qualified_group else 1.0,
            posterior_beating_probability=0.3 if is_qualified_group else 0.0,
            guardrails_passed=True,
            receipt_id=f"receipt-{request.request_id}",
        )
    return coordinator


def _record_success(coordinator: FloorDiscoveryCoordinator, request: Any) -> None:
    coordinator.record_result(
        admitted=True,
        sealed_mae_n=0.9,
        guardrails_passed=True,
        receipt_id=f"receipt-{request.request_id}",
    )


def _advance_to_160(coordinator: FloorDiscoveryCoordinator) -> None:
    while coordinator.novel_count < 160:
        request = coordinator.next_request()
        _record_success(coordinator, request)
    if coordinator.sentinel_due:
        request = coordinator.next_request()
        assert request.role == SENTINEL
        _record_success(coordinator, request)


def _complete_coordinator(*, fingerprint: str = "repair-complete") -> FloorDiscoveryCoordinator:
    coordinator = _coordinator(fingerprint=fingerprint)
    while not coordinator.complete:
        _record_success(coordinator, coordinator.next_request())
    return coordinator


def test_nonidentity_planar_basis_transforms_pose_twist_acceleration_and_binds_identity() -> None:
    with pytest.raises(PathContextError):
        PlanarBasisReceiptV1(along_base=(2.0, 0.0, 0.0), lateral_base=(0.0, 1.0, 0.0))
    with pytest.raises(PathContextError):
        PlanarBasisReceiptV1(along_base=(1.0, 0.0, 0.0), lateral_base=(1.0, 0.0, 0.0))
    with pytest.raises(PathContextError):
        PlanarBasisReceiptV1(
            along_base=(1.0, 0.0, 0.0),
            lateral_base=(0.0, 1.0, 0.0),
            signed_curvature_orientation="negative",
        )
    basis = PlanarBasisReceiptV1(
        along_base=(0.0, 1.0, 0.0),
        lateral_base=(-1.0, 0.0, 0.0),
    )
    anchor = (1.2, -0.4, 0.7, 0.1, -0.2, 0.3)
    provider = CycloidPathProviderV1(anchor_pose_base=anchor, basis=basis)
    sample = provider.sample(10.0)
    theta = 1.0
    along = 0.015 * (theta - math.sin(theta))
    lateral = 0.015 * (1.0 - math.cos(theta))
    along_v = 0.0015 * (1.0 - math.cos(theta))
    lateral_v = 0.0015 * math.sin(theta)
    along_a = 0.00015 * math.sin(theta)
    lateral_a = 0.00015 * math.cos(theta)
    assert sample.desired_pose_base_m[:3] == pytest.approx(
        (anchor[0] - lateral, anchor[1] + along, anchor[2])
    )
    assert sample.desired_twist_base_m_s[:3] == pytest.approx(
        (-lateral_v, along_v, 0.0)
    )
    assert sample.desired_acceleration_base_m_s2[:3] == pytest.approx(
        (-lateral_a, along_a, 0.0)
    )
    assert sample.desired_pose_base_m[3:] == anchor[3:]
    assert sample.identity_receipt.basis_receipt.as_dict() == basis.as_dict()
    assert provider.identity_receipt.source_identity
    assert provider.identity_receipt.sha256 != CycloidPathProviderV1().identity_receipt.sha256
    assert provider.identity_receipt.sha256 != CycloidPathProviderV1(anchor_pose_base=anchor).identity_receipt.sha256
    round_tripped = PathIdentityReceiptV1.from_mapping(provider.identity_receipt.as_dict())
    assert round_tripped.sha256 == provider.identity_receipt.sha256
    tampered = copy.deepcopy(provider.identity_receipt.as_dict())
    tampered["basis_receipt"]["along_base"][0] = 1.0
    with pytest.raises(PathContextError):
        PathIdentityReceiptV1.from_mapping(tampered)
    tampered = copy.deepcopy(provider.identity_receipt.as_dict())
    tampered["path_identity_sha256"] = "0" * 64
    with pytest.raises(PathContextError):
        PathIdentityReceiptV1.from_mapping(tampered)


def test_formal_seal_keeps_auxiliary_coverage_explicit_and_rejects_formal_gap() -> None:
    metric = MetricFingerprintV1.cycloid()

    accumulator = GapPreservingMetricAccumulatorV1(metric)
    for index in range(600):
        if index == 0:
            continue
        accumulator.add_sample(time_s=(index + 0.5) * 0.1, normal_load_n=5.2)
    result = accumulator.seal()
    assert result.exact is True and result.sealed is True
    assert result.formal_mae_n == pytest.approx(0.2)
    assert result.full_curve_mae_n is None
    assert result.transient_mae_n is None
    assert result.observed_formal_bin_count == 550
    assert result.required_full_bin_count == 600
    assert result.observed_transient_bin_count == 49
    assert result.full_curve_coverage == pytest.approx(599 / 600)
    assert result.transient_coverage == pytest.approx(49 / 50)

    formal_gap = GapPreservingMetricAccumulatorV1(metric)
    for index in range(600):
        if index == 50:
            continue
        formal_gap.add_sample(time_s=(index + 0.5) * 0.1, normal_load_n=5.2)
    with pytest.raises(MetricGapError, match="formal gaps"):
        formal_gap.seal()


def test_figure8_convergence_uses_exact_fresh_posterior_rule_and_distinct_budget_stop() -> None:
    package = load_figure8_transfer_template()
    common = {
        "fresh_valid_proposal_count": 25,
        "max_posterior_probability_of_improvement": 0.049,
        "improvement_threshold_n": 0.01,
        "top3_repeat_evidence": (5, 5, 5),
    }
    assert package.convergence_met(novel_count=80, **common) is True
    assert package.convergence_met(novel_count=79, **common) is False
    assert package.convergence_met(novel_count=160, **common) is True
    assert package.convergence_met(novel_count=80, **{**common, "fresh_valid_proposal_count": 24}) is False
    assert package.convergence_met(novel_count=80, **{**common, "max_posterior_probability_of_improvement": 0.05}) is False
    assert package.convergence_met(novel_count=80, **{**common, "top3_repeat_evidence": (5, 5, 4)}) is False
    assert package.budget_stop(novel_count=159) is False
    assert package.budget_stop(novel_count=160) is True
    assert package.completion_status(novel_count=160) == {
        "posterior_converged": False,
        "budget_stop": True,
        "stop": True,
    }
    assert package.completion_status(
        novel_count=160,
        **{**common, "max_posterior_probability_of_improvement": 0.2},
    ) == {"posterior_converged": False, "budget_stop": True, "stop": True}
    with pytest.raises(FigureEightTransferError, match="threshold must equal 0.01"):
        package.convergence_met(novel_count=80, **{**common, "improvement_threshold_n": 0.011})


def test_figure8_seed_manifest_is_final_receipt_gated_and_round_trips() -> None:
    coordinator = _complete_coordinator()
    final = build_checkpoint_receipt(coordinator, "final")
    manifest = materialize_figure8_transfer_seed_manifest(coordinator, final)
    assert manifest.launch_ready is False
    assert manifest.warm_start_count == 24
    assert manifest.controller_core_center == coordinator.robust_core_freeze.controller_core.coordinates
    assert manifest.correction_weak_prior_weights[4:] == (0.0, 0.0)
    assert FigureEightTransferSeedManifestV1.from_mapping(manifest.as_dict()) == manifest

    launch_package = materialize_figure8_launch_package(manifest)
    assert launch_package.launch_ready is False
    assert launch_package.campaign_entrypoint == "tools/run_step5d_autotune_v4_r013_live.py"
    assert launch_package.required_receipts[-1] == "figure8_component_freeze_receipt"
    assert FigureEightLaunchPackageV1.from_mapping(launch_package.as_dict()) == launch_package

    with pytest.raises(FigureEightTransferError, match="placeholder"):
        materialize_figure8_launch_package(FigureEightTransferSeedManifestV1.default())
    partial_placeholder = {**manifest.as_dict(), "source_correction_receipt_sha256": "0" * 64}
    with pytest.raises(FigureEightTransferError, match="placeholder"):
        materialize_figure8_launch_package(partial_placeholder)

    tampered = {**final.as_dict(), "observed_novel_count": 199}
    with pytest.raises(FigureEightTransferError):
        materialize_figure8_transfer_seed_manifest(coordinator, tampered)

    partial = _coordinator(fingerprint="repair-partial")
    with pytest.raises(FigureEightTransferError, match="cycloid state is incomplete"):
        materialize_figure8_transfer_seed_manifest(partial, final)


def test_default_floor_config_is_pending_and_handoff_materialization_stays_offline(
    tmp_path: Path,
) -> None:
    config = load_r013_budgeted_floor_config()
    assert config.status == "offline_prepared_awaiting_live_prerequisites"
    assert config.launch_ready is False
    assert config.handoff_policy is None
    assert config.selected_handoff_policy is None
    assert config.handoff_plan.selected_policy is None
    assert config.campaign_fingerprint.handoff_policy == "pending_handoff_selection_v1"
    assert set(config.blockers) >= {
        "live_handoff_ab_not_executed",
        "outward_boundary_runtime_primitive_not_installed",
        "context_correction_runtime_primitive_not_installed",
        "cycloid_200_novel_not_executed",
    }

    plan = HandoffABPlanV1()
    evidence = {
        "sealed": True,
        "gaps": [],
        "fmin_n": 0.0,
        "fmax_n": 5.0,
        "pose_error_m": 0.0,
        "orientation_error_rad": 0.0,
        "carry_reset_receipt_ids": ["carry-offline"],
        "tmae5_n": 1.0,
        "settling_time_s": 1.0,
        "raw_evidence_refs": ["raw-offline"],
    }
    for _ in range(8):
        arm = plan.next_arm()
        assert arm is not None
        row = dict(evidence)
        row["sealed_mae_n"] = 1.0 if arm == "A" else 1.03
        plan.record_evidence(arm, row)
    assert plan.complete is True
    materialized = materialize_r013_handoff_selection(config, plan)
    assert materialized.launch_ready is False
    assert materialized.blockers == config.blockers
    assert materialized.selected_handoff_policy is not None
    assert materialized.handoff_selection_receipt is not None
    assert materialized.handoff_selection_receipt.receipt_sha256 in (
        materialized.campaign_fingerprint.handoff_policy
    )
    roundtrip_path = tmp_path / "materialized-offline-config.json"
    roundtrip_path.write_text(
        json.dumps(materialized.as_dict(), sort_keys=True), encoding="utf-8"
    )
    assert load_r013_budgeted_floor_config(roundtrip_path) == materialized
    with pytest.raises(R013CampaignConfigError, match="launch_ready=false"):
        materialize_campaign_fingerprint(
            materialized,
            runtime_strategy_sha256_value="a" * 64,
            controller_triplet_sha256={role: "b" * 64 for role in ("script", "txt", "urp")},
            eoat_identity_sha256="c" * 64,
            script1_source_sha256="d" * 64,
        )
    with pytest.raises(R013CampaignConfigError, match="offline-only"):
        replace(materialized, launch_ready=True, blockers=())


def test_live_ready_transition_requires_complete_receipts_and_binds_fingerprint(
    tmp_path: Path,
) -> None:
    config = load_r013_budgeted_floor_config()
    plan = HandoffABPlanV1()
    evidence = {
        "sealed": True,
        "gaps": [],
        "fmin_n": 0.0,
        "fmax_n": 5.0,
        "pose_error_m": 0.0,
        "orientation_error_rad": 0.0,
        "carry_reset_receipt_ids": ["carry-live-ready"],
        "tmae5_n": 1.0,
        "settling_time_s": 1.0,
        "raw_evidence_refs": ["raw-live-ready"],
    }
    for _ in range(8):
        arm = plan.next_arm()
        assert arm is not None
        row = dict(evidence)
        row["sealed_mae_n"] = 1.0 if arm == "A" else 1.03
        plan.record_evidence(arm, row)
    handoff_receipt = plan.selection_receipt()
    assert handoff_receipt is not None
    triplet = {role: "b" * 64 for role in ("script", "txt", "urp")}
    runtime_identity = "a" * 64
    eoat_identity = "c" * 64
    script1_identity = "d" * 64
    selected = materialize_r013_handoff_selection(config, plan)
    target_fingerprint = replace(
        selected.campaign_fingerprint,
        correction_runtime_strategy_identity=runtime_identity,
        source_identity=controller_source_identity_sha256(triplet),
        eoat_identity=eoat_identity,
        home_tare_identity=home_tare_procedure_identity(script1_identity),
    )
    source_identity = target_fingerprint.source_identity
    outward = R013RuntimeInstallationReceiptV1(
        primitive="outward_boundary",
        source_identity=source_identity,
        runtime_identity=source_identity,
        campaign_fingerprint_sha256=target_fingerprint.sha256,
    )
    correction = R013RuntimeInstallationReceiptV1(
        primitive="six_context_correction",
        source_identity=source_identity,
        runtime_identity=runtime_identity,
        campaign_fingerprint_sha256=target_fingerprint.sha256,
    )

    with pytest.raises(R013CampaignConfigError, match="complete hash-bound handoff"):
        materialize_r013_live_ready_config(
            config,
            handoff_plan=plan,
            handoff_selection_receipt=None,  # type: ignore[arg-type]
            outward_boundary_runtime_receipt=outward,
            six_context_correction_runtime_receipt=correction,
            runtime_strategy_sha256_value=runtime_identity,
            controller_triplet_sha256=triplet,
            eoat_identity_sha256=eoat_identity,
            script1_source_sha256=script1_identity,
        )
    with pytest.raises(R013CampaignConfigError, match="handoff receipt is malformed"):
        materialize_r013_live_ready_config(
            config,
            handoff_plan=plan,
            handoff_selection_receipt={"schema": handoff_receipt.schema},
            outward_boundary_runtime_receipt=outward,
            six_context_correction_runtime_receipt=correction,
            runtime_strategy_sha256_value=runtime_identity,
            controller_triplet_sha256=triplet,
            eoat_identity_sha256=eoat_identity,
            script1_source_sha256=script1_identity,
        )
    with pytest.raises(R013CampaignConfigError, match="stale or mismatched"):
        materialize_r013_live_ready_config(
            config,
            handoff_plan=plan,
            handoff_selection_receipt=handoff_receipt,
            outward_boundary_runtime_receipt=R013RuntimeInstallationReceiptV1(
                primitive="outward_boundary",
                source_identity=source_identity,
                runtime_identity=source_identity,
                campaign_fingerprint_sha256="e" * 64,
            ),
            six_context_correction_runtime_receipt=correction,
            runtime_strategy_sha256_value=runtime_identity,
            controller_triplet_sha256=triplet,
            eoat_identity_sha256=eoat_identity,
            script1_source_sha256=script1_identity,
        )
    with pytest.raises(R013CampaignConfigError, match="current installation status"):
        R013RuntimeInstallationReceiptV1(
            primitive="outward_boundary",
            source_identity=source_identity,
            runtime_identity=source_identity,
            campaign_fingerprint_sha256=target_fingerprint.sha256,
            installation_status="stale",
        )

    live_ready = materialize_r013_live_ready_config(
        config,
        handoff_plan=plan,
        handoff_selection_receipt=handoff_receipt,
        outward_boundary_runtime_receipt=outward,
        six_context_correction_runtime_receipt=correction,
        runtime_strategy_sha256_value=runtime_identity,
        controller_triplet_sha256=triplet,
        eoat_identity_sha256=eoat_identity,
        script1_source_sha256=script1_identity,
    )
    assert live_ready.launch_ready is True
    assert live_ready.status == "live_ready_awaiting_campaign_start"
    assert live_ready.blockers == ()
    live_ready.require_launch_ready()
    assert live_ready.live_ready_transition_receipt is not None
    assert live_ready.live_ready_transition_receipt.receipt_sha256
    live_ready_path = tmp_path / "live-ready-r013-config.json"
    live_ready_path.write_text(json.dumps(live_ready.as_dict()), encoding="utf-8")
    loaded_live_ready = load_r013_budgeted_floor_config(live_ready_path)
    loaded_live_ready.require_launch_ready()


def test_robust_core_freeze_is_fail_closed_typed_and_replay_bound() -> None:
    coordinator = _coordinator(fingerprint="freeze-replay", qualified=True)
    sentinel = coordinator.next_request()
    assert sentinel.role == SENTINEL
    _record_success(coordinator, sentinel)
    correction = coordinator.next_request()
    assert correction.trial.active_block == "correction"
    freeze = coordinator.robust_core_freeze
    assert freeze is not None
    assert freeze.n >= 3
    assert freeze.canonical_token
    assert freeze.fingerprint == "freeze-replay"
    assert freeze.controller_core == correction.trial.controller_core
    assert freeze.orientation_ko == pytest.approx(0.05)
    replayed = FloorDiscoveryCoordinator.from_records(
        FloorDiscoveryPolicyV1(),
        coordinator.event_log,
        fingerprint="freeze-replay",
        core_proposal_provider=_core_provider,
        correction_proposal_provider=_correction_provider,
    )
    assert replayed.snapshot() == coordinator.snapshot()

    tampered = copy.deepcopy(list(coordinator.event_log))
    freeze_event = next(item for item in tampered if item.get("event") == "robust_core_freeze")
    freeze_event["receipt"]["mean_n"] += 0.01
    with pytest.raises(FloorCoordinatorError, match="receipt hash|mean differs"):
        FloorDiscoveryCoordinator.from_records(
            FloorDiscoveryPolicyV1(),
            tampered,
            fingerprint="freeze-replay",
            core_proposal_provider=_core_provider,
            correction_proposal_provider=_correction_provider,
        )
    with pytest.raises(FloorCoordinatorError, match="fingerprint"):
        FloorDiscoveryCoordinator.from_records(
            FloorDiscoveryPolicyV1(),
            coordinator.event_log,
            fingerprint="wrong-fingerprint",
            core_proposal_provider=_core_provider,
            correction_proposal_provider=_correction_provider,
        )

    unqualified = _coordinator(fingerprint="no-qualified-group", qualified=False)
    sentinel = unqualified.next_request()
    _record_success(unqualified, sentinel)
    with pytest.raises(FloorCoordinatorError, match="robust_core_not_qualified"):
        unqualified.next_request()
    assert unqualified.state == "robust_core_not_qualified"
    assert unqualified.robust_core_not_qualified_receipt is not None


def test_local_polish_is_fresh_128_point_one_block_qlognei_and_rejects_outside_provider() -> None:
    coordinator = _coordinator(fingerprint="local-valid", qualified=True)
    _advance_to_160(coordinator)
    freeze = coordinator.robust_core_freeze
    assert freeze is not None
    combined_incumbent = coordinator._best_compatible_correction_receipt()
    core_polish = coordinator.next_request()
    assert core_polish.role == POLISH_CORE_NOVEL
    core_receipt = core_polish.proposal_receipt["local_pool_receipt"]
    assert core_receipt["fresh_candidate_count"] == 128
    assert core_receipt["model_dimensions"] == 4
    assert core_receipt["q"] == 1
    assert all(
        low <= value <= high
        for value, (low, high) in zip(
            core_polish.trial.controller_core.coordinates,
            core_receipt["local_bounds"],
            strict=True,
        )
    )
    assert core_polish.trial.controller_core != freeze.controller_core
    assert core_polish.trial.correction == combined_incumbent.correction
    assert core_polish.runtime_candidate is None
    assert core_polish.executable is False
    _record_success(coordinator, core_polish)
    correction_polish = coordinator.next_request()
    assert correction_polish.role == POLISH_CORRECTION_NOVEL
    correction_receipt = correction_polish.proposal_receipt["local_pool_receipt"]
    assert correction_receipt["fresh_candidate_count"] == 128
    assert correction_receipt["model_dimensions"] == 6
    assert correction_receipt["q"] == 1
    assert all(
        low <= value <= high
        for value, (low, high) in zip(
            correction_polish.trial.correction.weights,
            correction_receipt["local_bounds"],
            strict=True,
        )
    )
    assert correction_polish.trial.controller_core == freeze.controller_core
    assert correction_polish.trial.correction != combined_incumbent.correction
    assert tuple(correction_receipt["local_center"]) == combined_incumbent.correction.weights
    assert tuple(correction_polish.trial.correction.weights) != tuple(correction_receipt["local_center"])
    assert correction_polish.runtime_candidate is None
    assert correction_polish.executable is False
    assert core_receipt["combined_incumbent"]["correction_incumbent_receipt"] == (
        combined_incumbent.as_dict()
    )

    outside = _coordinator(fingerprint="local-outside", qualified=True)
    _advance_to_160(outside)

    def outside_provider(pool: tuple[dict[str, Any], ...]) -> FloorCandidateProposal:
        candidate = dict(pool[0])
        candidate["motion_kp"] = 3.0
        return FloorCandidateProposal(
            candidate=candidate,
            block_input=tuple(candidate_to_log_features(candidate)[index] for index in (0, 1, 2, 5)),
            contract=CORE_PROPOSAL_CONTRACT,
        )

    outside.core_proposal_provider = outside_provider
    with pytest.raises(FloorCoordinatorError, match="outside the supplied pool"):
        outside.next_request()

    replayed = FloorDiscoveryCoordinator.from_records(
        FloorDiscoveryPolicyV1(),
        coordinator.event_log,
        fingerprint="local-valid",
        core_proposal_provider=_core_provider,
        correction_proposal_provider=_correction_provider,
    )
    assert replayed.compatible_correction_incumbent == combined_incumbent
    assert replayed.snapshot() == coordinator.snapshot()


def test_failed_guardrail_correction_group_cannot_become_compatible_incumbent() -> None:
    coordinator = _coordinator(fingerprint="guardrail-incumbent", qualified=True)
    _advance_to_160(coordinator)
    groups = [
        stat
        for stat in coordinator.stats.values()
        if stat["trial"].get("active_block") in {"correction", "polish_correction"}
    ]
    assert len(groups) >= 2
    failed = groups[0]
    failed_correction = CorrectionBlockV1.from_mapping(failed["trial"]["correction"])
    failed["values"] = [0.001] * len(failed["values"])
    failed["guardrail_evidence"] = [
        {"passed": False, "guardrails_passed": False, "receipt_id": "failed-guardrail"}
        for _ in failed["values"]
    ]
    incumbent = coordinator._best_compatible_correction_receipt()
    assert incumbent.correction != failed_correction
    assert all(row["passed"] is True for row in incumbent.guardrail_evidence)
    assert coordinator._best_compatible_correction() == incumbent.correction


def test_correction_observation_groups_are_real_v2_fingerprint_bound_and_cuda_only() -> None:
    fingerprint = _fingerprint()
    groups = _correction_groups(fingerprint)
    assert validate_correction_observation_groups(groups) == groups
    assert CorrectionObservationGroupV1.from_mapping(groups[0].as_dict()) == groups[0]

    with pytest.raises(R013GPError, match="campaign fingerprints"):
        validate_correction_observation_groups(groups + _correction_groups(_fingerprint("b"))[:1])
    with pytest.raises(R013GPError, match="duplicate"):
        validate_correction_observation_groups((groups[0], groups[0]))
    with pytest.raises(R013GPError, match="duplicate incompatible"):
        validate_correction_observation_groups((groups[0], groups[0].__class__(
            campaign_fingerprint=fingerprint,
            weights=groups[0].weights,
            n=groups[0].n,
            mean_n=groups[0].mean_n + 0.1,
            yvar_n2=groups[0].yvar_n2,
        )))
    with pytest.raises(R013GPError, match="n must be at least"):
        CorrectionObservationGroupV1(fingerprint, groups[0].weights, 0, 0.5, 0.001)
    with pytest.raises(R013GPError, match="finite"):
        CorrectionObservationGroupV1(fingerprint, groups[0].weights, 1, math.nan, 0.001)
    with pytest.raises(R013GPError, match="weight bounds"):
        CorrectionObservationGroupV1(fingerprint, (0.6,) * 6, 1, 0.5, 0.001)
    with pytest.raises(R013GPError, match="Yvar"):
        CorrectionObservationGroupV1(fingerprint, groups[0].weights, 1, 0.5, 0.00009)
    legacy = CorrectionBlockV1.from_mapping({
        "schema": "step5d.autotune-v4/r013-correction-block-placeholder-v1",
        "version": 1,
        "coordinates": [0.1] * 6,
    })
    with pytest.raises(R013GPError, match="real v2 weights"):
        CorrectionObservationGroupV1(fingerprint, legacy, 1, 0.5, 0.001)

    try:
        import torch
    except ImportError:
        torch = None
    if torch is None or not torch.cuda.is_available():
        with pytest.raises(R013GPError, match="(runtime is unavailable|requires CUDA)"):
            fit_correction_production_gp(groups, robust_incumbent_n=0.49)
        return

    fit = fit_correction_production_gp(groups, robust_incumbent_n=0.49)
    assert fit.backend == "botorch.SingleTaskGP"
    assert fit.device == "cuda"
    assert len(fit.lengthscales_unit_fitted) == CORRECTION_MODEL_DIMENSION_COUNT
    assert fit.fit_receipt["training_rows"] == len(groups)
    candidates = tuple(
        tuple(-0.49 + 0.98 * ((index + 1 + 3 * offset) % 129) / 129 for offset in range(6))
        for index in range(128)
    )
    proposal = ask_correction_qlognei(fit, candidates)
    assert proposal.weights in candidates
    assert math.isfinite(proposal.acquisition_value)
    assert 0.0 <= proposal.posterior_probability_of_improvement <= 1.0
    assert proposal.fit_receipt["fresh_candidate_count"] == 128
    adapter = CorrectionQLogNEIProposalProviderV1(fit)
    assert adapter(candidates).weights in candidates
