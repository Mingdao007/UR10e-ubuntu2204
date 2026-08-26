from __future__ import annotations

import inspect
import math
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step6_figure8_autotune_v1.v5_composition_contract import (  # noqa: E402
    RolloverCommand,
    V5AttemptKind,
    V5RolloverInput,
)
from step6_figure8_autotune_v1.v5_rollover import (  # noqa: E402
    CandidateIdentityV1,
    CurrentObservationV1,
    FORMAL_METRIC_START_S,
    LogicalRuntimeStateV1,
    MAX_COMMITTED_ROLLOVERS,
    PATH_END_S,
    PhysicalContinuitySeedsV1,
    RolloverReason,
    SwitchGateFamiliesV1,
    TAIL_END_S,
    V5Phase,
    V5RolloverStateV2,
    V5RolloverError,
    commit_rollover,
    entry_tick,
    entry_target_n,
    figure8_analytic_sample,
    initial_rollover_state,
    phase_at,
    phase_for_time,
    prepare_rollover,
    cancel_rollover,
)


def _identity(ordinal: int = 10, token: int = 100) -> CandidateIdentityV1:
    return CandidateIdentityV1(0, ordinal, V5AttemptKind.PRIMARY_NOVEL, token)


def _prepare_request(generation: int, ordinal: int = 11, token: int = 101) -> V5RolloverInput:
    return V5RolloverInput(
        command=RolloverCommand.PREPARE,
        generation=generation,
        next_attempt_ordinal=ordinal,
        next_attempt_kind=V5AttemptKind.PRIMARY_NOVEL,
        next_candidate_token=token,
    )


def _commit_request(generation: int, ordinal: int = 11, token: int = 101) -> V5RolloverInput:
    return V5RolloverInput(
        command=RolloverCommand.COMMIT,
        generation=generation,
        next_attempt_ordinal=ordinal,
        next_attempt_kind=V5AttemptKind.PRIMARY_NOVEL,
        next_candidate_token=token,
        qdot_generation=generation,
    )


def _gates() -> SwitchGateFamiliesV1:
    return SwitchGateFamiliesV1(True, True, True, True, True)


def test_entry_latch_and_same_tick_ramp_boundaries() -> None:
    assert entry_target_n(0.0) == pytest.approx(5.0)
    assert entry_tick(0.0).correction_enabled is False
    assert entry_target_n(4.0) == pytest.approx(5.0)
    assert entry_target_n(4.0 + 1e-9) == pytest.approx(5.0)
    assert entry_tick(FORMAL_METRIC_START_S - 1e-9).formal_metric_enabled is False
    assert entry_tick(FORMAL_METRIC_START_S).formal_metric_enabled is True
    assert entry_tick(FORMAL_METRIC_START_S).correction_enabled is True
    assert entry_tick(PATH_END_S).formal_metric_enabled is False
    assert entry_tick(PATH_END_S).correction_enabled is True
    assert entry_tick(TAIL_END_S - 1e-9).correction_enabled is True
    assert entry_tick(TAIL_END_S).correction_enabled is False


def test_exact_tail_endpoint_and_boundary_continuity() -> None:
    at_boundary = figure8_analytic_sample(PATH_END_S)
    just_tail = figure8_analytic_sample(PATH_END_S + 1e-9)
    endpoint = figure8_analytic_sample(TAIL_END_S)
    assert at_boundary.along_m == just_tail.along_m or math.isclose(
        at_boundary.along_m, just_tail.along_m, rel_tol=0.0, abs_tol=1e-10
    )
    assert math.isclose(endpoint.along_m, 0.0, rel_tol=0.0, abs_tol=1e-12)
    assert math.isclose(endpoint.lateral_m, 0.0, rel_tol=0.0, abs_tol=1e-12)
    assert phase_at(PATH_END_S) is V5Phase.ANALYTIC_CLOSURE_TAIL
    assert phase_at(TAIL_END_S) is V5Phase.COMPLETE_ENDPOINT


def test_tail_is_old_candidate_authority_and_excluded_from_all_training_paths() -> None:
    path = phase_for_time(PATH_END_S)
    assert path.tail_old_candidate_authority is True
    assert path.metric_evidence_path is False
    assert path.formal_metric is False
    assert path.included_in_mae is False
    assert path.included_in_censor is False
    assert path.included_in_gp is False
    assert path.included_in_tell_exact is False
    assert path.counts_exact_novel_budget is False


def test_prepare_keeps_active_identity_and_allocates_only_prepared_slot() -> None:
    state = initial_rollover_state(_identity())
    next_state, receipt = prepare_rollover(state, _prepare_request(1), epoch=0)
    assert next_state.active_identity == state.active_identity
    assert next_state.qdot_generation == state.qdot_generation
    assert next_state.prepared_slot is not None
    assert next_state.prepared_slot.identity.ordinal == 11
    assert receipt.active_before == receipt.active_after == state.active_identity
    assert receipt.reason is RolloverReason.ACCEPTED


def test_commit_is_atomic_and_resets_logical_but_carries_only_continuity_seeds() -> None:
    observations = CurrentObservationV1(
        pose=(3.0,) * 6,
        q=(4.0,) * 6,
        qd=(5.0,) * 6,
        tcp_velocity=(6.0,) * 6,
        controller_timestamp=7.5,
        reaction_normal=8.0,
    )
    seeds = PhysicalContinuitySeedsV1((1.0,) * 6, (2.0,) * 6, observations)
    state = initial_rollover_state(
        _identity(),
        logical_state=LogicalRuntimeStateV1(5, 6, 17.0, 3, 0.4, 2.0),
        continuity_seeds=seeds,
    )
    prepared, _ = prepare_rollover(state, _prepare_request(1), epoch=0)
    with pytest.raises(V5RolloverError) as seam_error:
        commit_rollover(
            prepared,
            _commit_request(1),
            _gates(),
            epoch=0,
            at_periodic_seam=False,
        )
    assert seam_error.value.reason is RolloverReason.PERIODIC_SEAM_REQUIRED
    committed, receipt = commit_rollover(
        prepared,
        _commit_request(1),
        _gates(),
        epoch=0,
        at_periodic_seam=True,
    )
    assert committed.active_identity == prepared.prepared_slot.identity
    assert committed.prepared_slot is None
    assert committed.qdot_generation == 1
    assert committed.committed_rollovers == 1
    assert committed.logical_state.is_fresh
    assert committed.continuity_seeds == seeds
    assert committed.continuity_seeds.current_observations is observations
    assert receipt.active_before == state.active_identity
    assert receipt.active_after == committed.active_identity
    assert receipt.qdot_generation_before == 0
    assert receipt.qdot_generation_after == 1
    assert receipt.reason is RolloverReason.ACCEPTED


def test_stale_generation_identity_seam_and_epoch_are_rejected_without_mutation() -> None:
    state = initial_rollover_state(_identity())
    with pytest.raises(V5RolloverError) as epoch_error:
        prepare_rollover(state, _prepare_request(1), epoch=1)
    assert epoch_error.value.reason is RolloverReason.SAME_EPOCH_REQUIRED
    prepared, _ = prepare_rollover(state, _prepare_request(1), epoch=0)
    with pytest.raises(V5RolloverError) as generation_error:
        commit_rollover(
            prepared,
            _commit_request(2),
            _gates(),
            epoch=0,
            at_periodic_seam=True,
        )
    assert generation_error.value.reason is RolloverReason.PREPARED_GENERATION_MISMATCH
    assert prepared.prepared_slot is not None
    with pytest.raises(V5RolloverError) as identity_error:
        commit_rollover(
            prepared,
            _commit_request(1, token=999),
            _gates(),
            epoch=0,
            at_periodic_seam=True,
        )
    assert identity_error.value.reason is RolloverReason.PREPARED_IDENTITY_MISMATCH
    assert prepared.active_identity == state.active_identity


def test_cancel_clears_only_matching_prepared_slot() -> None:
    state = initial_rollover_state(_identity())
    prepared, _ = prepare_rollover(state, _prepare_request(1), epoch=0)
    wrong = V5RolloverInput(command=RolloverCommand.CANCEL, generation=2)
    with pytest.raises(V5RolloverError) as error:
        cancel_rollover(prepared, wrong, epoch=0)
    assert error.value.reason is RolloverReason.PREPARED_GENERATION_MISMATCH
    cancelled, _ = cancel_rollover(
        prepared,
        V5RolloverInput(command=RolloverCommand.CANCEL, generation=1),
        epoch=0,
    )
    assert cancelled.prepared_slot is None
    assert cancelled.active_identity == prepared.active_identity
    assert cancelled.qdot_generation == prepared.qdot_generation
    assert cancelled.rollover_generation_fence == prepared.rollover_generation_fence == 1


def test_cancelled_generation_cannot_be_prepared_again() -> None:
    state = initial_rollover_state(_identity())
    prepared, _ = prepare_rollover(state, _prepare_request(1), epoch=0)
    cancelled, _ = cancel_rollover(
        prepared,
        V5RolloverInput(command=RolloverCommand.CANCEL, generation=1),
        epoch=0,
    )
    assert cancelled.qdot_generation == 0
    assert cancelled.rollover_generation_fence == 1
    with pytest.raises(V5RolloverError) as error:
        prepare_rollover(cancelled, _prepare_request(1, ordinal=12, token=102), epoch=0)
    assert error.value.reason is RolloverReason.STALE_GENERATION


def test_chain_cap_rejects_the_fifth_rollover_for_home_fallback() -> None:
    state = initial_rollover_state(_identity())
    for generation in range(1, MAX_COMMITTED_ROLLOVERS + 1):
        ordinal = state.active_identity.ordinal + 1
        token = state.active_identity.candidate_token + 1
        prepared, _ = prepare_rollover(state, _prepare_request(generation, ordinal, token), epoch=0)
        state, _ = commit_rollover(
            prepared,
            _commit_request(generation, ordinal, token),
            _gates(),
            epoch=0,
            at_periodic_seam=True,
        )
    with pytest.raises(V5RolloverError) as error:
        prepare_rollover(state, _prepare_request(5, state.active_identity.ordinal + 1, 999), epoch=0)
    assert error.value.reason is RolloverReason.CHAIN_CAP_REACHED
    assert "home_fallback" in str(error.value)


def test_missing_boolean_gate_is_rejected_and_no_force_window_is_a_switch_input() -> None:
    with pytest.raises(V5RolloverError) as missing:
        SwitchGateFamiliesV1.from_mapping({"hard_safety": True})
    assert missing.value.reason is RolloverReason.MISSING_BOOLEAN_GATE
    parameters = inspect.signature(commit_rollover).parameters
    assert "force_window" not in parameters
    assert "torque_window" not in parameters
    assert set(SwitchGateFamiliesV1.__dataclass_fields__) >= {
        "hard_safety",
        "timing_freshness",
        "tube_cbf",
        "identity_metric_closure",
        "command_envelope",
    }


def test_rollover_state_v2_carries_filter_outer_solver_and_physical_state() -> None:
    state = V5RolloverStateV2(
        candidate={"force_p_gain": 1.0},
        filtered_force_n=5.0,
        filter_initialized=True,
        force_integral_n_s=0.2,
        xdot_p_prev_m_s=(0.0, 0.0, -0.001),
        theta_dot_state=(0.0,) * 6,
        lambda_state=(0.0,) * 6,
        last_cartesian_twist=(0.0,) * 6,
        previous_qdot=(0.0,) * 6,
        pose=(0.0,) * 6,
        q=(0.0,) * 6,
        qd=(0.0,) * 6,
        jacobian_6x6=(0.0,) * 36,
        reaction_normal=(0.0, 0.0, 1.0),
        approach_normal=(0.0, 0.0, -1.0),
        target_force_n=5.0,
        actual_dt_s=0.002,
        controller_timestamp=12.0,
        generation=3,
    )
    payload = state.as_dict()
    assert payload["schema"] == "step6.autotune/figure8-v5-rollover-state-v2"
    assert payload["filter_initialized"] is True
    assert len(payload["jacobian_6x6"]) == 36

    with pytest.raises(ValueError, match="target force"):
        V5RolloverStateV2(
            **{**payload, "schema": "step6.autotune/figure8-v5-rollover-state-v2", "target_force_n": 4.0}
        )
