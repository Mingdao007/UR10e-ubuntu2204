from __future__ import annotations

from dataclasses import fields, replace
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
    V5TPState,
)
from step6_figure8_autotune_v1.v5_resident_protocol import (  # noqa: E402
    ResidentAction,
    ResidentReason,
    ResidentStatus,
    ResidentTickInputV1,
    StopReasonCodeV1,
    initial_resident_state,
    resident_tick,
)
from step6_figure8_autotune_v1.v5_rollover import (  # noqa: E402
    PATH_END_S,
    TAIL_END_S,
    CandidateIdentityV1,
    SwitchGateFamiliesV1,
)


DT = 0.01
GATES = SwitchGateFamiliesV1(True, True, True, True, True)


def identity(ordinal: int, *, token: int | None = None) -> CandidateIdentityV1:
    return CandidateIdentityV1(
        epoch=7,
        ordinal=ordinal,
        attempt_kind=V5AttemptKind.PRIMARY_NOVEL,
        candidate_token=ordinal if token is None else token,
    )


def tick(**kwargs) -> ResidentTickInputV1:
    return ResidentTickInputV1(dt_s=DT, **kwargs)


def prepare_input(generation: int, ordinal: int, *, token: int | None = None) -> V5RolloverInput:
    return V5RolloverInput(
        command=RolloverCommand.PREPARE,
        generation=generation,
        next_attempt_ordinal=ordinal,
        next_attempt_kind=V5AttemptKind.PRIMARY_NOVEL,
        next_candidate_token=ordinal if token is None else token,
    )


def commit_input(generation: int, ordinal: int, *, token: int | None = None) -> V5RolloverInput:
    return V5RolloverInput(
        command=RolloverCommand.COMMIT,
        generation=generation,
        next_attempt_ordinal=ordinal,
        next_attempt_kind=V5AttemptKind.PRIMARY_NOVEL,
        next_candidate_token=ordinal if token is None else token,
        qdot_generation=generation,
    )


def cancel_input(generation: int) -> V5RolloverInput:
    return V5RolloverInput(command=RolloverCommand.CANCEL, generation=generation)


def at_tail(state, *, prepared=False):
    return replace(
        state,
        tp_state=V5TPState.ROLLOVER_PREPARED if prepared else V5TPState.CLOSURE_TAIL,
        path_time_s=TAIL_END_S - DT,
    )


def test_same_tick_latch_enters_path_at_zero_without_a_counter_or_force_window():
    state = initial_resident_state(identity(1))
    next_state, receipt = resident_tick(
        state,
        tick(stable_one_newton_latched=True),
    )

    assert next_state.tp_state is V5TPState.PATH
    assert next_state.path_time_s == 0.0
    assert receipt.action is ResidentAction.ENTER_PATH
    assert receipt.reason is ResidentReason.ENTRY_LATCHED_SAME_TICK
    assert receipt.overlay_registers[30] == int(V5AttemptKind.PRIMARY_NOVEL)
    names = {field.name for field in fields(ResidentTickInputV1)}
    assert not names.intersection({"force", "force_n", "torque", "readiness_window", "performance_window"})


def test_prepare_before_entry_latch_is_held_without_any_rollover_mutation():
    state = initial_resident_state(identity(1))
    next_state, receipt = resident_tick(
        state,
        tick(rollover_input=prepare_input(1, 2)),
    )

    assert next_state == state
    assert next_state.tp_state is V5TPState.ONE_NEWTON_ENTRY
    assert next_state.path_time_s == 0.0
    assert next_state.rollover_state.prepared_slot is None
    assert next_state.rollover_state.rollover_generation_fence == 0
    assert receipt.action is ResidentAction.HOLD
    assert receipt.reason is ResidentReason.ENTRY_LATCH_WAITING


def test_exact_phase_boundaries_and_tail_overlay():
    state = replace(
        initial_resident_state(identity(1)),
        tp_state=V5TPState.PATH,
        path_time_s=PATH_END_S - DT,
    )
    state, path_receipt = resident_tick(state, tick())
    assert state.tp_state is V5TPState.CLOSURE_TAIL
    assert state.path_time_s == PATH_END_S
    assert path_receipt.overlay_registers == {29: 0, 30: 0, 31: 0}

    state = replace(state, path_time_s=TAIL_END_S - DT)
    state, tail_receipt = resident_tick(state, tick())
    assert state.status is ResidentStatus.HOME_READY
    assert state.path_time_s == TAIL_END_S
    assert tail_receipt.action is ResidentAction.RETURN_HOME
    assert tail_receipt.reason is ResidentReason.CHAIN_COMPLETE_HOME
    assert tail_receipt.tp_state is V5TPState.READY_HOME_NEXT
    assert tail_receipt.overlay_registers == {29: 0, 30: int(V5AttemptKind.PRIMARY_NOVEL), 31: 0}


def test_prepare_keeps_identity_stable_and_cancelled_generation_is_not_reusable():
    state = replace(
        initial_resident_state(identity(1)),
        tp_state=V5TPState.CLOSURE_TAIL,
        path_time_s=60.0,
    )
    active_before = state.active_identity
    state, receipt = resident_tick(
        state,
        tick(rollover_input=prepare_input(1, 2)),
    )
    assert receipt.action is ResidentAction.PREPARE
    assert state.active_identity == active_before
    assert state.rollover_state.rollover_generation_fence == 1

    state, receipt = resident_tick(
        state,
        tick(rollover_input=cancel_input(1)),
    )
    assert receipt.action is ResidentAction.CANCEL
    assert state.active_identity == active_before
    assert state.rollover_state.rollover_generation_fence == 1

    rejected, receipt = resident_tick(
        state,
        tick(rollover_input=prepare_input(1, 2)),
    )
    assert rejected.status is ResidentStatus.HOME_FALLBACK_REQUIRED
    assert receipt.rollover_reason is not None
    assert receipt.rollover_reason.value == "stale_generation"


def test_atomic_seam_commit_starts_fresh_path_and_preserves_tail_overlay_then_base():
    state = replace(
        initial_resident_state(identity(1), qdot_generation=0),
        tp_state=V5TPState.CLOSURE_TAIL,
        path_time_s=TAIL_END_S - 2 * DT,
    )
    state, prepare_receipt = resident_tick(
        state,
        tick(rollover_input=prepare_input(1, 2)),
    )
    # PREPARE tick leaves one dt before the exact endpoint; COMMIT owns the seam.
    assert state.active_identity == identity(1)
    assert state.rollover_state.prepared_slot is not None
    assert prepare_receipt.overlay_registers == {29: 1, 30: 0, 31: 2}

    state, receipt = resident_tick(
        state,
        tick(rollover_input=commit_input(1, 2), switch_gates=GATES),
    )
    assert receipt.tp_state is V5TPState.ROLLOVER_COMMITTED
    assert receipt.overlay_registers == {29: 1, 30: 1, 31: 0}
    assert state.tp_state is V5TPState.PATH
    assert state.path_time_s == 0.0
    assert state.active_identity == identity(2)
    assert state.rollover_state.qdot_generation == 1
    assert state.rollover_state.logical_state.is_fresh
    assert state.rollover_state.prepared_slot is None
    assert receipt.active_ordinal == 2

    state, path_receipt = resident_tick(state, tick())
    assert state.path_time_s == DT
    assert path_receipt.overlay_registers[30] == int(V5AttemptKind.PRIMARY_NOVEL)


def test_commit_can_be_armed_before_seam_while_old_identity_remains_authoritative():
    state = replace(
        initial_resident_state(identity(1)),
        tp_state=V5TPState.CLOSURE_TAIL,
        path_time_s=TAIL_END_S - 3 * DT,
    )
    state, _ = resident_tick(
        state,
        tick(rollover_input=prepare_input(1, 2)),
    )
    state, armed = resident_tick(
        state,
        tick(rollover_input=commit_input(1, 2), switch_gates=GATES),
    )
    assert armed.action is ResidentAction.ARM_COMMIT
    assert armed.reason is ResidentReason.COMMIT_ARMED
    assert state.active_identity == identity(1)
    assert state.armed_commit == commit_input(1, 2)
    assert state.path_time_s == pytest.approx(TAIL_END_S - DT)

    state, committed = resident_tick(state, tick(switch_gates=GATES))
    assert committed.action is ResidentAction.COMMIT
    assert state.active_identity == identity(2)
    assert state.path_time_s == 0.0
    assert state.armed_commit is None


def test_stale_commit_and_missing_gate_are_home_fallbacks():
    state = replace(
        initial_resident_state(identity(1)),
        tp_state=V5TPState.CLOSURE_TAIL,
        path_time_s=TAIL_END_S,
    )
    state, receipt = resident_tick(
        state,
        tick(rollover_input=commit_input(1, 2), switch_gates=GATES),
    )
    assert state.status is ResidentStatus.HOME_FALLBACK_REQUIRED
    assert receipt.rollover_reason is not None
    assert receipt.rollover_reason.value == "prepared_slot_missing"


def test_matching_early_end_is_trial_home_and_other_ordinal_is_ignored():
    state = replace(
        initial_resident_state(identity(1)),
        tp_state=V5TPState.PATH,
        path_time_s=1.0,
    )
    state, receipt = resident_tick(
        state,
        tick(path_early_end_request=2),
    )
    assert state.status is ResidentStatus.RUNNING
    assert state.path_time_s == pytest.approx(1.0 + DT)
    assert receipt.reason is ResidentReason.EARLY_CENSOR_NONMATCHING
    assert receipt.action is ResidentAction.ADVANCE_PATH

    state, receipt = resident_tick(
        state,
        tick(path_early_end_request=1),
    )
    assert state.status is ResidentStatus.HOME_FALLBACK_REQUIRED
    assert receipt.action is ResidentAction.HOME_FALLBACK_REQUIRED
    assert receipt.reason is ResidentReason.EARLY_CENSOR_MATCH


def test_stop_dominates_latch_rollover_and_seam_without_auto_home_or_retry():
    state = replace(
        initial_resident_state(identity(1)),
        tp_state=V5TPState.CLOSURE_TAIL,
        path_time_s=TAIL_END_S - 2 * DT,
    )
    stopped, receipt = resident_tick(
        state,
        tick(
            stable_one_newton_latched=True,
            rollover_input=commit_input(1, 2),
            switch_gates=GATES,
            stop_reason=StopReasonCodeV1(17),
        ),
    )
    assert stopped.status is ResidentStatus.STOPPED
    assert stopped.tp_state is V5TPState.STOPPED
    assert receipt.action is ResidentAction.STOPPED
    assert receipt.stop_reason_code == 17
    held, held_receipt = resident_tick(stopped, tick())
    assert held == stopped
    assert held_receipt.action is ResidentAction.HOLD


def test_four_commits_are_allowed_and_the_fifth_candidate_closes_home():
    state = replace(
        initial_resident_state(identity(1)),
        tp_state=V5TPState.CLOSURE_TAIL,
        path_time_s=TAIL_END_S - 2 * DT,
    )
    for generation in range(1, 5):
        ordinal = generation + 1
        state, _ = resident_tick(
            state,
            tick(rollover_input=prepare_input(generation, ordinal)),
        )
        assert state.path_time_s == pytest.approx(TAIL_END_S - DT)
        state, receipt = resident_tick(
            state,
            tick(rollover_input=commit_input(generation, ordinal), switch_gates=GATES),
        )
        assert state.status is ResidentStatus.RUNNING
        assert state.tp_state is V5TPState.PATH
        assert state.path_time_s == 0.0
        assert state.rollover_state.committed_rollovers == generation
        assert receipt.action is ResidentAction.COMMIT
        state = replace(state, tp_state=V5TPState.CLOSURE_TAIL, path_time_s=TAIL_END_S - 2 * DT)

    state = replace(state, path_time_s=TAIL_END_S - DT)
    state, _ = resident_tick(state, tick())
    assert state.status is ResidentStatus.HOME_READY
    assert state.tp_state is V5TPState.READY_HOME_NEXT
    assert state.rollover_state.committed_rollovers == 4
    assert state.path_time_s == TAIL_END_S


def test_dt_is_finite_and_strictly_below_tick_period():
    with pytest.raises(ValueError):
        ResidentTickInputV1(dt_s=0.0)
    with pytest.raises(ValueError):
        ResidentTickInputV1(dt_s=0.08)
    with pytest.raises(ValueError):
        ResidentTickInputV1(dt_s=math.inf)
