"""Typed composition contract for the isolated Autotuner V5 wire.

This module is deliberately pure: it describes layout-607 and its typed
state-dependent output overlay without opening RTDE, reading a sensor, or
granting motion authority.  Layout-606 remains owned by the V4 modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Mapping, Sequence


AUTOTUNER_V5_COMPOSITION_SCHEMA = (
    "step6.autotune/autotuner-v5-composition-contract-v2"
)
AUTOTUNER_V5_COMPOSITION_VERSION = 2
LAYOUT_TAG = 607.0

INPUT_DOUBLE_REGISTERS = tuple(range(24, 48))
INPUT_INTEGER_REGISTERS = tuple(range(24, 40))
OUTPUT_DOUBLE_REGISTERS = (24,)
OUTPUT_INTEGER_REGISTERS = tuple(range(24, 35))

ROLLOVER_INPUT_FIELDS: Mapping[int, str] = {
    33: "rollover_command",
    34: "rollover_generation",
    35: "path_early_end_request",
    36: "next_attempt_ordinal",
    37: "next_attempt_kind",
    38: "next_candidate_token",
    39: "qdot_generation",
}

BASE_OUTPUT_OVERLAY_FIELDS: Mapping[int, str] = {
    29: "consumed_session_command_sequence",
    30: "attempt_kind",
    31: "return_guard",
}

ROLLOVER_OUTPUT_OVERLAY_FIELDS: Mapping[int, str] = {
    29: "rollover_ack_generation",
    30: "active_qdot_generation",
    31: "prepared_candidate_token",
}


class RolloverCommand(IntEnum):
    NONE = 0
    PREPARE = 1
    COMMIT = 2
    CANCEL = 3


class V5AttemptKind(IntEnum):
    """Attempt kinds admitted by V5; campaign qualification is absent."""

    PRIMARY_NOVEL = 10
    PRIMARY_CONFIRM = 11
    CORRECTION_NOVEL = 12
    MATCHED_ZERO = 13
    MATCHED_CORRECTED = 14
    CAPABILITY_ACCEPTANCE = 15


class V5TPState(IntEnum):
    WAITING_FOR_PLAY = 10
    ARMING = 11
    CONTACT_ACQUISITION = 20
    ONE_NEWTON_ENTRY = 21
    PATH = 25
    CLOSURE_TAIL = 26
    ROLLOVER_PREPARED = 27
    ROLLOVER_COMMITTED = 28
    ROLLOVER_REJECTED = 29
    RETURNING_HOME = 40
    READY_HOME_NEXT = 78
    COMPLETE = 80
    STOPPED = 90


ROLLOVER_OVERLAY_STATES = frozenset(
    {
        V5TPState.CLOSURE_TAIL,
        V5TPState.ROLLOVER_PREPARED,
        V5TPState.ROLLOVER_COMMITTED,
        V5TPState.ROLLOVER_REJECTED,
    }
)


def _typed_nonnegative(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{role} must be a non-negative typed integer")
    if value > 2**31 - 1:
        raise ValueError(f"{role} exceeds the signed RTDE integer range")
    return value


@dataclass(frozen=True)
class V5RolloverInput:
    command: RolloverCommand = RolloverCommand.NONE
    generation: int = 0
    path_early_end_request: int = 0
    next_attempt_ordinal: int = 0
    next_attempt_kind: V5AttemptKind | None = None
    next_candidate_token: int = 0
    qdot_generation: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.command, RolloverCommand):
            raise TypeError("command must be a typed RolloverCommand")
        for role, value in (
            ("generation", self.generation),
            ("path_early_end_request", self.path_early_end_request),
            ("next_attempt_ordinal", self.next_attempt_ordinal),
            ("next_candidate_token", self.next_candidate_token),
            ("qdot_generation", self.qdot_generation),
        ):
            _typed_nonnegative(value, role)
        if self.next_attempt_kind is not None and not isinstance(
            self.next_attempt_kind, V5AttemptKind
        ):
            raise TypeError("next_attempt_kind must be a typed V5AttemptKind or None")

        identity_present = (
            self.next_attempt_ordinal > 0
            and self.next_attempt_kind is not None
            and self.next_candidate_token > 0
        )
        identity_empty = (
            self.next_attempt_ordinal == 0
            and self.next_attempt_kind is None
            and self.next_candidate_token == 0
        )
        if self.command is RolloverCommand.NONE:
            if self.generation != 0 or not identity_empty:
                raise ValueError("NONE must not carry a rollover generation or next identity")
        elif self.command is RolloverCommand.PREPARE:
            if self.generation <= 0 or not identity_present:
                raise ValueError("PREPARE requires a positive generation and complete next identity")
        elif self.command is RolloverCommand.COMMIT:
            if self.generation <= 0 or not identity_present:
                raise ValueError("COMMIT requires a positive generation and complete next identity")
            if self.qdot_generation != self.generation:
                raise ValueError("COMMIT requires qdot_generation to match rollover generation")
        elif self.command is RolloverCommand.CANCEL:
            if self.generation <= 0 or not identity_empty:
                raise ValueError("CANCEL carries only the positive generation being cancelled")

    @property
    def by_register(self) -> dict[int, int]:
        return {
            33: int(self.command),
            34: self.generation,
            35: self.path_early_end_request,
            36: self.next_attempt_ordinal,
            37: 0 if self.next_attempt_kind is None else int(self.next_attempt_kind),
            38: self.next_candidate_token,
            39: self.qdot_generation,
        }


@dataclass(frozen=True)
class BaseOutputOverlayV2:
    consumed_session_command_sequence: int
    attempt_kind: V5AttemptKind | None
    return_guard: int

    def __post_init__(self) -> None:
        _typed_nonnegative(
            self.consumed_session_command_sequence,
            "consumed_session_command_sequence",
        )
        if self.attempt_kind is not None and not isinstance(
            self.attempt_kind, V5AttemptKind
        ):
            raise TypeError("attempt_kind must be a typed V5AttemptKind or None")
        _typed_nonnegative(self.return_guard, "return_guard")

    @property
    def by_register(self) -> dict[int, int]:
        return {
            29: self.consumed_session_command_sequence,
            30: 0 if self.attempt_kind is None else int(self.attempt_kind),
            31: self.return_guard,
        }


@dataclass(frozen=True)
class RolloverOutputOverlayV2:
    rollover_ack_generation: int
    active_qdot_generation: int
    prepared_candidate_token: int

    def __post_init__(self) -> None:
        for role, value in (
            ("rollover_ack_generation", self.rollover_ack_generation),
            ("active_qdot_generation", self.active_qdot_generation),
            ("prepared_candidate_token", self.prepared_candidate_token),
        ):
            _typed_nonnegative(value, role)

    @property
    def by_register(self) -> dict[int, int]:
        return {
            29: self.rollover_ack_generation,
            30: self.active_qdot_generation,
            31: self.prepared_candidate_token,
        }


OutputOverlayV2 = BaseOutputOverlayV2 | RolloverOutputOverlayV2


def decode_output_overlay(
    state: V5TPState,
    integer_echoes: Mapping[int, int],
) -> OutputOverlayV2:
    """Decode registers 29..31 according to the already-typed TP state."""

    if not isinstance(state, V5TPState):
        raise TypeError("state must be a typed V5TPState")
    try:
        values = {register: _typed_nonnegative(integer_echoes[register], f"output{register}") for register in (29, 30, 31)}
    except KeyError as exc:
        raise ValueError("output overlay omits register 29, 30, or 31") from exc
    if state in ROLLOVER_OVERLAY_STATES:
        return RolloverOutputOverlayV2(
            rollover_ack_generation=values[29],
            active_qdot_generation=values[30],
            prepared_candidate_token=values[31],
        )
    try:
        attempt_kind = None if values[30] == 0 else V5AttemptKind(values[30])
    except ValueError as exc:
        raise ValueError("base output overlay has an unknown V5 attempt kind") from exc
    return BaseOutputOverlayV2(
        consumed_session_command_sequence=values[29],
        attempt_kind=attempt_kind,
        return_guard=values[31],
    )


@dataclass(frozen=True)
class AutotunerV5CompositionContractV2:
    schema: str = AUTOTUNER_V5_COMPOSITION_SCHEMA
    version: int = AUTOTUNER_V5_COMPOSITION_VERSION
    layout_tag: float = LAYOUT_TAG
    maximum_rollovers_per_chain: int = 4
    early_censor_register: int = 35
    single_live_writer: bool = True
    performance_force_windows_blocking: bool = False
    safety_policy_ref: str = "existing-v5-hard-safety-authority"

    def __post_init__(self) -> None:
        if self.schema != AUTOTUNER_V5_COMPOSITION_SCHEMA:
            raise ValueError("Autotuner V5 composition schema differs")
        if self.version != AUTOTUNER_V5_COMPOSITION_VERSION:
            raise ValueError("Autotuner V5 composition version differs")
        if self.layout_tag != LAYOUT_TAG:
            raise ValueError("Autotuner V5 layout tag differs from 607")
        if self.maximum_rollovers_per_chain != 4:
            raise ValueError("V5 rollover chain cap differs from four")
        if self.early_censor_register != 35:
            raise ValueError("V5 must preserve the early-censor request on input35")
        if self.single_live_writer is not True:
            raise ValueError("V5 requires exactly one live writer")
        if self.performance_force_windows_blocking is not False:
            raise ValueError("performance/readiness force windows cannot block V5")
        if not isinstance(self.safety_policy_ref, str) or not self.safety_policy_ref:
            raise ValueError("V5 must reference the existing hard-safety authority")

    def assert_register_recipes(
        self,
        *,
        input_double_registers: Sequence[int],
        input_integer_registers: Sequence[int],
        output_double_registers: Sequence[int],
        output_integer_registers: Sequence[int],
    ) -> None:
        if tuple(input_double_registers) != INPUT_DOUBLE_REGISTERS:
            raise ValueError("V5 input-double recipe differs from registers 24..47")
        if tuple(input_integer_registers) != INPUT_INTEGER_REGISTERS:
            raise ValueError("V5 input-integer recipe differs from registers 24..39")
        if tuple(output_double_registers) != OUTPUT_DOUBLE_REGISTERS:
            raise ValueError("V5 output-double recipe differs from register 24")
        if tuple(output_integer_registers) != OUTPUT_INTEGER_REGISTERS:
            raise ValueError("V5 output-integer recipe differs from registers 24..34")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "layout_tag": self.layout_tag,
            "register_recipe": {
                "input_double": list(INPUT_DOUBLE_REGISTERS),
                "input_integer": list(INPUT_INTEGER_REGISTERS),
                "output_double": list(OUTPUT_DOUBLE_REGISTERS),
                "output_integer": list(OUTPUT_INTEGER_REGISTERS),
            },
            "rollover_input_fields": {
                str(register): role for register, role in ROLLOVER_INPUT_FIELDS.items()
            },
            "base_output_overlay_fields": {
                str(register): role
                for register, role in BASE_OUTPUT_OVERLAY_FIELDS.items()
            },
            "rollover_output_overlay_fields": {
                str(register): role
                for register, role in ROLLOVER_OUTPUT_OVERLAY_FIELDS.items()
            },
            "maximum_rollovers_per_chain": self.maximum_rollovers_per_chain,
            "early_censor_register": self.early_censor_register,
            "single_live_writer": self.single_live_writer,
            "performance_force_windows_blocking": self.performance_force_windows_blocking,
            "safety_policy_ref": self.safety_policy_ref,
            "blocking_gate_families": [
                "existing_hard_safety",
                "timing_and_freshness",
                "tube_and_cbf",
                "identity_and_metric_closure",
                "command_envelope",
            ],
            "observation_only_sidecars": [
                "filter_shadow",
                "camera_observer",
                "ros2_observation_mirror",
            ],
        }


__all__ = [
    "AUTOTUNER_V5_COMPOSITION_SCHEMA",
    "AUTOTUNER_V5_COMPOSITION_VERSION",
    "BASE_OUTPUT_OVERLAY_FIELDS",
    "INPUT_DOUBLE_REGISTERS",
    "INPUT_INTEGER_REGISTERS",
    "LAYOUT_TAG",
    "OUTPUT_DOUBLE_REGISTERS",
    "OUTPUT_INTEGER_REGISTERS",
    "ROLLOVER_INPUT_FIELDS",
    "ROLLOVER_OUTPUT_OVERLAY_FIELDS",
    "AutotunerV5CompositionContractV2",
    "BaseOutputOverlayV2",
    "OutputOverlayV2",
    "RolloverCommand",
    "RolloverOutputOverlayV2",
    "V5AttemptKind",
    "V5RolloverInput",
    "V5TPState",
    "decode_output_overlay",
]
