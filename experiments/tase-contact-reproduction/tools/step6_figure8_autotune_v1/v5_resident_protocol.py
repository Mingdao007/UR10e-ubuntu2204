"""Pure tick-level resident protocol for the isolated Autotuner V5 path.

The protocol is an offline specification for a later URScript generator.  It
only composes the typed V5 composition and rollover primitives; it has no
transport, sensor, controller, filesystem, or motion effects.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
import math
from typing import Any

try:  # Tests may put ``tools`` on sys.path directly.
    from step6_figure8_autotune_v1.v5_composition_contract import (
        BaseOutputOverlayV2,
        OutputOverlayV2,
        RolloverCommand,
        RolloverOutputOverlayV2,
        V5AttemptKind,
        V5RolloverInput,
        V5TPState,
        decode_output_overlay,
    )
    from step6_figure8_autotune_v1.v5_rollover import (
        MAX_COMMITTED_ROLLOVERS,
        PATH_END_S,
        TAIL_END_S,
        CandidateIdentityV1,
        RolloverReason,
        RolloverStateV1,
        SwitchGateFamiliesV1,
        V5RolloverError,
        cancel_rollover,
        commit_rollover,
        initial_rollover_state,
        prepare_rollover,
        validate_commit_request,
    )
except ModuleNotFoundError:  # pragma: no cover - repository-root imports
    from tools.step6_figure8_autotune_v1.v5_composition_contract import (
        BaseOutputOverlayV2,
        OutputOverlayV2,
        RolloverCommand,
        RolloverOutputOverlayV2,
        V5AttemptKind,
        V5RolloverInput,
        V5TPState,
        decode_output_overlay,
    )
    from tools.step6_figure8_autotune_v1.v5_rollover import (
        MAX_COMMITTED_ROLLOVERS,
        PATH_END_S,
        TAIL_END_S,
        CandidateIdentityV1,
        RolloverReason,
        RolloverStateV1,
        SwitchGateFamiliesV1,
        V5RolloverError,
        cancel_rollover,
        commit_rollover,
        initial_rollover_state,
        prepare_rollover,
        validate_commit_request,
    )


RESIDENT_PROTOCOL_SCHEMA = "step6.autotune/figure8-v5-resident-protocol-v1"
RESIDENT_RECEIPT_SCHEMA = "step6.autotune/figure8-v5-resident-receipt-v1"
RESIDENT_PROTOCOL_VERSION = 1
MAX_TICK_DT_S = 0.08


class ResidentStatus(str, Enum):
    RUNNING = "running"
    HOME_READY = "home_ready"
    HOME_FALLBACK_REQUIRED = "home_fallback_required"
    STOPPED = "stopped"


class ResidentAction(str, Enum):
    HOLD = "hold"
    ENTER_PATH = "enter_path"
    ADVANCE_PATH = "advance_path"
    ADVANCE_TAIL = "advance_tail"
    PREPARE = "prepare"
    CANCEL = "cancel"
    ARM_COMMIT = "arm_commit"
    COMMIT = "commit"
    RETURN_HOME = "return_home"
    HOME_FALLBACK_REQUIRED = "home_fallback_required"
    STOPPED = "stopped"


class ResidentReason(str, Enum):
    ENTRY_LATCH_WAITING = "entry_latch_waiting"
    ENTRY_LATCHED_SAME_TICK = "entry_latched_same_tick"
    PATH_ADVANCED = "path_advanced"
    TAIL_ADVANCED = "tail_advanced"
    EARLY_CENSOR_NONMATCHING = "early_censor_nonmatching"
    EARLY_CENSOR_MATCH = "early_censor_match"
    PREPARE_ACCEPTED = "prepare_accepted"
    CANCEL_ACCEPTED = "cancel_accepted"
    COMMIT_ARMED = "commit_armed"
    COMMIT_ACCEPTED = "commit_accepted"
    CHAIN_COMPLETE_HOME = "chain_complete_home"
    PERIODIC_SEAM_REQUIRES_COMMIT = "periodic_seam_requires_commit"
    CHAIN_CAP_REACHED = "chain_cap_reached"
    ROLLOVER_REJECTED = "rollover_rejected"
    STOP_INPUT_DOMINANT = "stop_input_dominant"
    TERMINAL_STATE = "terminal_state"


@dataclass(frozen=True)
class StopReasonCodeV1:
    """A caller-supplied, already-typed nonzero STOP reason."""

    code: int
    schema: str = "step6.autotune/figure8-v5-stop-reason-v1"
    version: int = RESIDENT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if type(self.code) is not int or self.code <= 0:
            raise ValueError("STOP reason code must be a typed nonzero integer")
        if self.code > 2**31 - 1:
            raise ValueError("STOP reason code exceeds the signed RTDE integer range")
        if self.schema != "step6.autotune/figure8-v5-stop-reason-v1" or self.version != RESIDENT_PROTOCOL_VERSION:
            raise ValueError("STOP reason schema/version differs")


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{role} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{role} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{role} must be finite")
    return result


def _nonnegative_integer(value: Any, role: str) -> int:
    if type(value) is not int or value < 0 or value > 2**31 - 1:
        raise ValueError(f"{role} must be a non-negative typed integer")
    return value


@dataclass(frozen=True)
class ResidentTickInputV1:
    """The complete typed input to one resident tick.

    ``switch_gates`` is an already-evaluated boolean receipt.  No raw
    performance, readiness, force, or safety measurement is an input here.
    """

    dt_s: float
    stable_one_newton_latched: bool = False
    rollover_input: V5RolloverInput | None = None
    switch_gates: SwitchGateFamiliesV1 | Mapping[str, Any] | None = None
    path_early_end_request: int = 0
    stop_reason: StopReasonCodeV1 | None = None
    schema: str = RESIDENT_PROTOCOL_SCHEMA
    version: int = RESIDENT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        dt_s = _finite(self.dt_s, "tick dt")
        if not 0.0 < dt_s < MAX_TICK_DT_S:
            raise ValueError("tick dt must satisfy 0 < dt < 0.08")
        if type(self.stable_one_newton_latched) is not bool:
            raise TypeError("stable one-newton latch must be boolean")
        if self.rollover_input is not None and not isinstance(self.rollover_input, V5RolloverInput):
            raise TypeError("rollover input must be a typed V5RolloverInput")
        if self.switch_gates is not None and not isinstance(self.switch_gates, (SwitchGateFamiliesV1, Mapping)):
            raise TypeError("switch gates must be typed booleans or a typed mapping")
        _nonnegative_integer(self.path_early_end_request, "path early-end request")
        if self.stop_reason is not None and not isinstance(self.stop_reason, StopReasonCodeV1):
            raise TypeError("STOP input must be a typed StopReasonCodeV1")
        if self.schema != RESIDENT_PROTOCOL_SCHEMA or self.version != RESIDENT_PROTOCOL_VERSION:
            raise ValueError("resident tick schema/version differs")
        object.__setattr__(self, "dt_s", dt_s)


@dataclass(frozen=True)
class ResidentProtocolStateV1:
    rollover_state: RolloverStateV1
    tp_state: V5TPState = V5TPState.ONE_NEWTON_ENTRY
    status: ResidentStatus = ResidentStatus.RUNNING
    path_time_s: float = 0.0
    armed_commit: V5RolloverInput | None = None
    schema: str = RESIDENT_PROTOCOL_SCHEMA
    version: int = RESIDENT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.rollover_state, RolloverStateV1):
            raise TypeError("resident state requires a typed rollover state")
        if not isinstance(self.tp_state, V5TPState):
            raise TypeError("resident TP state must be typed")
        if not isinstance(self.status, ResidentStatus):
            raise TypeError("resident status must be typed")
        path_time_s = _finite(self.path_time_s, "resident path time")
        if not 0.0 <= path_time_s <= TAIL_END_S:
            raise ValueError("resident path time is outside the V5 analytic interval")
        if self.tp_state is V5TPState.ONE_NEWTON_ENTRY and path_time_s != 0.0:
            raise ValueError("one-newton entry must start at path time zero")
        if self.tp_state is V5TPState.PATH and not path_time_s < PATH_END_S:
            raise ValueError("PATH state must be before the periodic path boundary")
        if self.tp_state is V5TPState.CLOSURE_TAIL and not PATH_END_S <= path_time_s <= TAIL_END_S:
            raise ValueError("CLOSURE_TAIL state must be within the closure interval")
        if self.armed_commit is not None:
            if (
                not isinstance(self.armed_commit, V5RolloverInput)
                or self.armed_commit.command is not RolloverCommand.COMMIT
            ):
                raise TypeError("resident armed COMMIT must be a typed COMMIT request")
            prepared = self.rollover_state.prepared_slot
            if prepared is None or self.armed_commit.generation != prepared.generation:
                raise ValueError("resident armed COMMIT differs from the prepared slot")
        if self.schema != RESIDENT_PROTOCOL_SCHEMA or self.version != RESIDENT_PROTOCOL_VERSION:
            raise ValueError("resident state schema/version differs")
        object.__setattr__(self, "path_time_s", path_time_s)

    @property
    def active_identity(self) -> CandidateIdentityV1:
        return self.rollover_state.active_identity

    def as_dict(self) -> dict[str, Any]:
        active = self.active_identity
        return {
            "schema": self.schema,
            "version": self.version,
            "tp_state": int(self.tp_state),
            "status": self.status.value,
            "path_time_s": self.path_time_s,
            "active_epoch": active.epoch,
            "active_ordinal": active.ordinal,
            "active_attempt_kind": int(active.attempt_kind),
            "active_candidate_token": active.candidate_token,
            "committed_rollovers": self.rollover_state.committed_rollovers,
            "armed_commit_generation": (
                0 if self.armed_commit is None else self.armed_commit.generation
            ),
        }


def initial_resident_state(
    active_identity: CandidateIdentityV1,
    *,
    qdot_generation: int = 0,
) -> ResidentProtocolStateV1:
    """Start one typed active candidate at ONE_NEWTON_ENTRY."""

    return ResidentProtocolStateV1(
        rollover_state=initial_rollover_state(
            active_identity,
            qdot_generation=qdot_generation,
        ),
    )


@dataclass(frozen=True)
class ResidentTickReceiptV1:
    tp_state: V5TPState
    status: ResidentStatus
    active_epoch: int
    active_ordinal: int
    active_attempt_kind: V5AttemptKind
    active_candidate_token: int
    path_time_s: float
    committed_rollovers: int
    action: ResidentAction
    reason: ResidentReason
    overlay: OutputOverlayV2
    rollover_reason: RolloverReason | None = None
    stop_reason_code: int | None = None
    schema: str = RESIDENT_RECEIPT_SCHEMA
    version: int = RESIDENT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.tp_state, V5TPState):
            raise TypeError("receipt TP state must be typed")
        if not isinstance(self.status, ResidentStatus):
            raise TypeError("receipt status must be typed")
        _nonnegative_integer(self.active_epoch, "receipt active epoch")
        _nonnegative_integer(self.active_ordinal, "receipt active ordinal")
        if not isinstance(self.active_attempt_kind, V5AttemptKind):
            raise TypeError("receipt attempt kind must be typed")
        if type(self.active_candidate_token) is not int or self.active_candidate_token <= 0:
            raise ValueError("receipt candidate token must be a positive typed integer")
        path_time_s = _finite(self.path_time_s, "receipt path time")
        if not 0.0 <= path_time_s <= TAIL_END_S:
            raise ValueError("receipt path time is outside the V5 analytic interval")
        _nonnegative_integer(self.committed_rollovers, "receipt committed rollovers")
        if not isinstance(self.action, ResidentAction) or not isinstance(self.reason, ResidentReason):
            raise TypeError("receipt action/reason must be typed")
        if not isinstance(self.overlay, (BaseOutputOverlayV2, RolloverOutputOverlayV2)):
            raise TypeError("receipt overlay must be typed")
        if self.rollover_reason is not None and not isinstance(self.rollover_reason, RolloverReason):
            raise TypeError("receipt rollover rejection reason must be typed")
        if self.stop_reason_code is not None:
            _nonnegative_integer(self.stop_reason_code, "receipt STOP reason")
            if self.stop_reason_code == 0:
                raise ValueError("receipt STOP reason must be nonzero")
        if self.schema != RESIDENT_RECEIPT_SCHEMA or self.version != RESIDENT_PROTOCOL_VERSION:
            raise ValueError("resident receipt schema/version differs")
        # This is the state-dependent wire assertion for output29..31.
        decoded = decode_output_overlay(self.tp_state, self.overlay.by_register)
        if decoded != self.overlay:
            raise ValueError("receipt overlay does not match its typed TP state")
        object.__setattr__(self, "path_time_s", path_time_s)

    @property
    def overlay_registers(self) -> dict[int, int]:
        return self.overlay.by_register

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "tp_state": int(self.tp_state),
            "status": self.status.value,
            "active_epoch": self.active_epoch,
            "active_ordinal": self.active_ordinal,
            "active_attempt_kind": int(self.active_attempt_kind),
            "active_candidate_token": self.active_candidate_token,
            "path_time_s": self.path_time_s,
            "committed_rollovers": self.committed_rollovers,
            "action": self.action.value,
            "reason": self.reason.value,
            "overlay_registers": self.overlay_registers,
            "rollover_reason": None if self.rollover_reason is None else self.rollover_reason.value,
            "stop_reason_code": self.stop_reason_code,
        }


def _phase_state(path_time_s: float, *, prepared: bool) -> V5TPState:
    if path_time_s < PATH_END_S:
        return V5TPState.ROLLOVER_PREPARED if prepared else V5TPState.PATH
    return V5TPState.ROLLOVER_PREPARED if prepared else V5TPState.CLOSURE_TAIL


def _before_boundary(value: float, boundary: float) -> bool:
    """Treat round-off at an analytic boundary as the exact boundary."""

    return value < boundary and not math.isclose(value, boundary, rel_tol=0.0, abs_tol=1e-12)


def _overlay_for_state(
    state: ResidentProtocolStateV1,
    *,
    tp_state: V5TPState | None = None,
    ack_generation: int | None = None,
    active_qdot_generation: int | None = None,
    prepared_candidate_token: int | None = None,
) -> OutputOverlayV2:
    output_state = state.tp_state if tp_state is None else tp_state
    if output_state in (
        V5TPState.CLOSURE_TAIL,
        V5TPState.ROLLOVER_PREPARED,
        V5TPState.ROLLOVER_COMMITTED,
        V5TPState.ROLLOVER_REJECTED,
    ):
        prepared = state.rollover_state.prepared_slot
        return RolloverOutputOverlayV2(
            rollover_ack_generation=(
                state.rollover_state.rollover_generation_fence
                if ack_generation is None
                else ack_generation
            ),
            active_qdot_generation=(
                state.rollover_state.qdot_generation
                if active_qdot_generation is None
                else active_qdot_generation
            ),
            prepared_candidate_token=(
                0 if prepared is None else prepared.identity.candidate_token
                if prepared_candidate_token is None
                else prepared_candidate_token
            ),
        )
    return BaseOutputOverlayV2(
        consumed_session_command_sequence=0,
        attempt_kind=state.active_identity.attempt_kind,
        return_guard=0,
    )


def _receipt(
    state: ResidentProtocolStateV1,
    *,
    action: ResidentAction,
    reason: ResidentReason,
    tp_state: V5TPState | None = None,
    overlay: OutputOverlayV2 | None = None,
    rollover_reason: RolloverReason | None = None,
    stop_reason_code: int | None = None,
) -> ResidentTickReceiptV1:
    identity = state.active_identity
    output_state = state.tp_state if tp_state is None else tp_state
    return ResidentTickReceiptV1(
        tp_state=output_state,
        status=state.status,
        active_epoch=identity.epoch,
        active_ordinal=identity.ordinal,
        active_attempt_kind=identity.attempt_kind,
        active_candidate_token=identity.candidate_token,
        path_time_s=state.path_time_s,
        committed_rollovers=state.rollover_state.committed_rollovers,
        action=action,
        reason=reason,
        overlay=(
            _overlay_for_state(state, tp_state=output_state)
            if overlay is None
            else overlay
        ),
        rollover_reason=rollover_reason,
        stop_reason_code=stop_reason_code,
    )


def _fallback(
    state: ResidentProtocolStateV1,
    *,
    reason: ResidentReason,
    rollover_reason: RolloverReason | None = None,
) -> tuple[ResidentProtocolStateV1, ResidentTickReceiptV1]:
    next_state = replace(
        state,
        status=ResidentStatus.HOME_FALLBACK_REQUIRED,
        tp_state=V5TPState.ROLLOVER_REJECTED,
    )
    return next_state, _receipt(
        next_state,
        action=ResidentAction.HOME_FALLBACK_REQUIRED,
        reason=reason,
        rollover_reason=rollover_reason,
    )


def _home_ready(
    state: ResidentProtocolStateV1,
) -> tuple[ResidentProtocolStateV1, ResidentTickReceiptV1]:
    """Close a complete chain normally when no successor was prepared."""

    next_state = replace(
        state,
        status=ResidentStatus.HOME_READY,
        tp_state=V5TPState.READY_HOME_NEXT,
        path_time_s=TAIL_END_S,
        armed_commit=None,
    )
    return next_state, _receipt(
        next_state,
        action=ResidentAction.RETURN_HOME,
        reason=ResidentReason.CHAIN_COMPLETE_HOME,
    )


def _rollover_failure(
    state: ResidentProtocolStateV1,
    error: V5RolloverError,
) -> tuple[ResidentProtocolStateV1, ResidentTickReceiptV1]:
    return _fallback(
        state,
        reason=ResidentReason.ROLLOVER_REJECTED,
        rollover_reason=error.reason,
    )


def _command(tick: ResidentTickInputV1) -> RolloverCommand:
    if tick.rollover_input is None:
        return RolloverCommand.NONE
    return tick.rollover_input.command


def _early_end_matches(state: ResidentProtocolStateV1, request: int) -> bool:
    return (
        0.0 <= state.path_time_s < PATH_END_S
        and request > 0
        and request == state.active_identity.ordinal
    )


def _early_end_nonmatching(state: ResidentProtocolStateV1, request: int) -> bool:
    return (
        0.0 <= state.path_time_s < PATH_END_S
        and request > 0
        and request != state.active_identity.ordinal
    )


def resident_tick(
    state: ResidentProtocolStateV1,
    tick: ResidentTickInputV1,
) -> tuple[ResidentProtocolStateV1, ResidentTickReceiptV1]:
    """Apply one deterministic resident tick and return state plus receipt."""

    if not isinstance(state, ResidentProtocolStateV1):
        raise TypeError("resident state must be a typed ResidentProtocolStateV1")
    if not isinstance(tick, ResidentTickInputV1):
        raise TypeError("resident tick must be a typed ResidentTickInputV1")

    # STOP is checked before every other input and also dominates a prior
    # Home-required terminal result.
    if tick.stop_reason is not None:
        next_state = replace(
            state,
            status=ResidentStatus.STOPPED,
            tp_state=V5TPState.STOPPED,
        )
        return next_state, _receipt(
            next_state,
            action=ResidentAction.STOPPED,
            reason=ResidentReason.STOP_INPUT_DOMINANT,
            stop_reason_code=tick.stop_reason.code,
        )

    if state.status is not ResidentStatus.RUNNING:
        return state, _receipt(
            state,
            action=ResidentAction.HOLD,
            reason=ResidentReason.TERMINAL_STATE,
        )

    command = _command(tick)
    current = state

    # A stable latch is consumed on this exact tick.  No dt is added to the
    # new PATH clock, so its observable time is exactly zero.
    if current.tp_state is V5TPState.ONE_NEWTON_ENTRY:
        if not tick.stable_one_newton_latched:
            # Entry owns the candidate slots and clock until the latch is
            # true.  Every rollover command is held without invoking the
            # rollover state machine.
            return current, _receipt(
                current,
                action=ResidentAction.HOLD,
                reason=ResidentReason.ENTRY_LATCH_WAITING,
            )
        else:
            current = replace(current, tp_state=V5TPState.PATH, path_time_s=0.0)
            if _early_end_matches(current, tick.path_early_end_request):
                return _fallback(current, reason=ResidentReason.EARLY_CENSOR_MATCH)
            if command is RolloverCommand.NONE:
                return current, _receipt(
                    current,
                    action=ResidentAction.ENTER_PATH,
                    reason=(
                        ResidentReason.EARLY_CENSOR_NONMATCHING
                        if _early_end_nonmatching(current, tick.path_early_end_request)
                        else ResidentReason.ENTRY_LATCHED_SAME_TICK
                    ),
                )

    # Early censor is a trial-local Home result and is evaluated before any
    # rollover command.  A request for another ordinal has no state effect.
    if _early_end_matches(current, tick.path_early_end_request):
        return _fallback(current, reason=ResidentReason.EARLY_CENSOR_MATCH)
    nonmatching_early_end = _early_end_nonmatching(current, tick.path_early_end_request)

    if command is RolloverCommand.PREPARE:
        try:
            prepared_rollover, _ = prepare_rollover(
                current.rollover_state,
                tick.rollover_input,
                epoch=current.rollover_state.epoch,
            )
        except V5RolloverError as error:
            return _rollover_failure(current, error)
        next_state = replace(
            current,
            rollover_state=prepared_rollover,
            tp_state=V5TPState.ROLLOVER_PREPARED,
            armed_commit=None,
        )
        # Entry has a same-tick zero-time contract; preparation does not
        # advance that first PATH tick.  Subsequent prepared ticks advance.
        if state.tp_state is V5TPState.ONE_NEWTON_ENTRY:
            return next_state, _receipt(
                next_state,
                action=ResidentAction.PREPARE,
                reason=ResidentReason.PREPARE_ACCEPTED,
            )
        return _advance_after_operation(
            next_state,
            tick,
            action=ResidentAction.PREPARE,
            reason=ResidentReason.PREPARE_ACCEPTED,
            nonmatching_early_end=nonmatching_early_end,
        )

    if command is RolloverCommand.CANCEL:
        try:
            cancelled_rollover, _ = cancel_rollover(
                current.rollover_state,
                tick.rollover_input,
                epoch=current.rollover_state.epoch,
            )
        except V5RolloverError as error:
            return _rollover_failure(current, error)
        next_state = replace(
            current,
            rollover_state=cancelled_rollover,
            tp_state=_phase_state(
                current.path_time_s,
                prepared=cancelled_rollover.prepared_slot is not None,
            ),
            armed_commit=None,
        )
        if state.tp_state is V5TPState.ONE_NEWTON_ENTRY:
            return next_state, _receipt(
                next_state,
                action=ResidentAction.CANCEL,
                reason=ResidentReason.CANCEL_ACCEPTED,
            )
        return _advance_after_operation(
            next_state,
            tick,
            action=ResidentAction.CANCEL,
            reason=ResidentReason.CANCEL_ACCEPTED,
            nonmatching_early_end=nonmatching_early_end,
        )

    if command is RolloverCommand.COMMIT:
        next_time = current.path_time_s + tick.dt_s
        if _before_boundary(next_time, TAIL_END_S):
            try:
                validate_commit_request(
                    current.rollover_state,
                    tick.rollover_input,
                    epoch=current.rollover_state.epoch,
                )
            except V5RolloverError as error:
                return _rollover_failure(current, error)
            armed = replace(current, armed_commit=tick.rollover_input)
            return _advance_after_operation(
                armed,
                tick,
                action=ResidentAction.ARM_COMMIT,
                reason=ResidentReason.COMMIT_ARMED,
                nonmatching_early_end=nonmatching_early_end,
            )
        return _commit_at_seam(current, tick)

    return _advance_after_operation(
        current,
        tick,
        action=ResidentAction.ADVANCE_PATH,
        reason=(
            ResidentReason.EARLY_CENSOR_NONMATCHING
            if nonmatching_early_end
            else ResidentReason.PATH_ADVANCED
        ),
        nonmatching_early_end=nonmatching_early_end,
    )


def _advance_after_operation(
    state: ResidentProtocolStateV1,
    tick: ResidentTickInputV1,
    *,
    action: ResidentAction,
    reason: ResidentReason,
    nonmatching_early_end: bool,
) -> tuple[ResidentProtocolStateV1, ResidentTickReceiptV1]:
    next_time = state.path_time_s + tick.dt_s
    if _before_boundary(next_time, PATH_END_S):
        next_state = replace(
            state,
            tp_state=(
                V5TPState.ROLLOVER_PREPARED
                if state.rollover_state.prepared_slot is not None
                else V5TPState.PATH
            ),
            path_time_s=next_time,
        )
        if action is ResidentAction.ADVANCE_PATH:
            action = ResidentAction.ADVANCE_PATH
            reason = ResidentReason.EARLY_CENSOR_NONMATCHING if nonmatching_early_end else ResidentReason.PATH_ADVANCED
        return next_state, _receipt(next_state, action=action, reason=reason)
    if _before_boundary(next_time, TAIL_END_S):
        next_state = replace(
            state,
            tp_state=(
                V5TPState.ROLLOVER_PREPARED
                if state.rollover_state.prepared_slot is not None
                else V5TPState.CLOSURE_TAIL
            ),
            path_time_s=next_time,
        )
        if action is ResidentAction.ADVANCE_PATH:
            action = ResidentAction.ADVANCE_TAIL
            reason = ResidentReason.TAIL_ADVANCED
        return next_state, _receipt(next_state, action=action, reason=reason)

    # The exact periodic endpoint is represented as a typed Home result.  No
    # interpolation is performed when a dt crosses the endpoint.
    seam_state = replace(
        state,
        tp_state=(
            V5TPState.ROLLOVER_PREPARED
            if state.rollover_state.prepared_slot is not None
            else V5TPState.CLOSURE_TAIL
        ),
        path_time_s=TAIL_END_S,
    )
    if seam_state.armed_commit is not None:
        return _commit_at_seam(seam_state, tick)
    if seam_state.rollover_state.prepared_slot is None:
        return _home_ready(seam_state)
    seam_reason = (
        ResidentReason.CHAIN_CAP_REACHED
        if seam_state.rollover_state.committed_rollovers >= MAX_COMMITTED_ROLLOVERS
        else ResidentReason.PERIODIC_SEAM_REQUIRES_COMMIT
    )
    return _fallback(seam_state, reason=seam_reason)


def _commit_at_seam(
    state: ResidentProtocolStateV1,
    tick: ResidentTickInputV1,
) -> tuple[ResidentProtocolStateV1, ResidentTickReceiptV1]:
    request = (
        tick.rollover_input
        if tick.rollover_input is not None
        and tick.rollover_input.command is RolloverCommand.COMMIT
        else state.armed_commit
    )
    try:
        committed_rollover, _ = commit_rollover(
            state.rollover_state,
            request,  # type: ignore[arg-type]
            tick.switch_gates,
            epoch=state.rollover_state.epoch,
            at_periodic_seam=True,
        )
    except V5RolloverError as error:
        seam_state = replace(
            state,
            tp_state=(
                V5TPState.ROLLOVER_PREPARED
                if state.rollover_state.prepared_slot is not None
                else V5TPState.CLOSURE_TAIL
            ),
            path_time_s=TAIL_END_S,
        )
        return _rollover_failure(seam_state, error)

    next_state = ResidentProtocolStateV1(
        rollover_state=committed_rollover,
        tp_state=V5TPState.PATH,
        status=ResidentStatus.RUNNING,
        path_time_s=0.0,
        armed_commit=None,
    )
    assert request is not None
    request_generation = request.generation
    overlay = RolloverOutputOverlayV2(
        rollover_ack_generation=request_generation,
        active_qdot_generation=next_state.rollover_state.qdot_generation,
        prepared_candidate_token=0,
    )
    return next_state, _receipt(
        next_state,
        action=ResidentAction.COMMIT,
        reason=ResidentReason.COMMIT_ACCEPTED,
        tp_state=V5TPState.ROLLOVER_COMMITTED,
        overlay=overlay,
    )


__all__ = [
    "MAX_TICK_DT_S",
    "RESIDENT_PROTOCOL_SCHEMA",
    "RESIDENT_PROTOCOL_VERSION",
    "RESIDENT_RECEIPT_SCHEMA",
    "ResidentAction",
    "ResidentProtocolStateV1",
    "ResidentReason",
    "ResidentStatus",
    "ResidentTickInputV1",
    "ResidentTickReceiptV1",
    "StopReasonCodeV1",
    "initial_resident_state",
    "resident_tick",
]
