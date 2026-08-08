"""Typed layout-606 register wire for the V4 r004 resident TP loop."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import TYPE_CHECKING, Mapping, Sequence

from .contracts import (
    Candidate,
    R004Contract,
    R004ContractError,
    TARGET_FORCE_N,
    V3_R034_SAFETY_ENVELOPE,
    assert_target,
)

if TYPE_CHECKING:
    from .motion_profile import V4MotionProfile


LAYOUT_TAG = 606.0
WIRE_SCHEMA = "step5d.autotune-v4/register-wire-v3"
WRENCH_AUTHORITY = "kunwei_only"
DOUBLE_REGISTERS = tuple(range(24, 48))
INPUT_INTEGER_REGISTERS = tuple(range(24, 33))
OUTPUT_INTEGER_REGISTERS = tuple(range(24, 35))
OUTPUT_DOUBLE_REGISTERS = (24,)

DOUBLE_FIELDS: Mapping[int, str] = {
    24: "normal_load_n",
    25: "force_norm_n",
    26: "heartbeat",
    27: "sensor_fresh",
    28: "stop_request",
    29: "eoat_get_ack",
    30: "torque_norm_nm",
    31: "wrench_fx_n",
    32: "wrench_fy_n",
    33: "wrench_fz_n",
    34: "wrench_mx_nm",
    35: "wrench_my_nm",
    36: "wrench_mz_nm",
    37: "qdot_0_rad_s",
    38: "qdot_1_rad_s",
    39: "qdot_2_rad_s",
    40: "qdot_3_rad_s",
    41: "qdot_4_rad_s",
    42: "qdot_5_rad_s",
    43: "cmd_valid",
    44: "internal_setpoint_n",
    45: "filtered_normal_n",
    46: "packet_sequence",
    47: "layout_tag",
}
INTEGER_FIELDS: Mapping[int, str] = {
    24: "baseline_consecutive_successes",
    25: "command_mode",
    26: "sticky_one_newton_latched",
    27: "session_command",
    28: "session_command_sequence",
    29: "session_epoch",
    30: "logical_attempt_ordinal",
    31: "attempt_kind",
    32: "candidate_token",
}
OUTPUT_INTEGER_FIELDS: Mapping[int, str] = {
    24: "epoch",
    25: "ordinal",
    26: "state",
    27: "token",
    28: "reason",
    29: "consumed_session_command_sequence",
    30: "attempt_kind",
    31: "return_guard",
    32: "runtime_protocol",
    33: "runtime_digest_hi",
    34: "runtime_digest_lo",
}
OUTPUT_DOUBLE_FIELDS: Mapping[int, str] = {24: "consumed_packet_sequence"}


class CommandMode(IntEnum):
    HOLD = 0
    BASELINE = 1
    PATH = 2
    RETRACT = 3
    STOP = 4


class SessionCommand(IntEnum):
    HOLD = 0
    ARM = 1
    COMPLETE = 2
    STOP = 3


class AttemptKind(IntEnum):
    QUALIFICATION = 1
    BATCH_A = 2
    BATCH_B = 3
    RETEST = 4


class TPState(IntEnum):
    WAITING_FOR_PLAY = 10
    ARMING = 11
    CONTACT_ACQUISITION = 20
    BASELINE = 21
    PATH = 25
    RETURNING_HOME = 40
    READY_HOME_NEXT = 78
    COMPLETE = 80
    STOPPED = 90


class ReturnGuard(IntFlag):
    NONE = 0
    STATIONARY = 1
    RETRACT_5MM = 2
    FIXED_HOME_ROUTE = 4
    ENTRY_ENVELOPE = 8
    RETURN_ENVELOPE = 16
    HOME_POSE = 32
    HOME_Q = 64


@dataclass(frozen=True)
class SensorPacket:
    normal_load_n: float
    force_norm_n: float
    heartbeat: float
    sensor_fresh: bool
    stop_request: bool
    eoat_get_ack: bool
    torque_norm_nm: float
    wrench: tuple[float, float, float, float, float, float]
    filtered_normal_n: float

    def __post_init__(self) -> None:
        if not all(isinstance(value, bool) for value in (self.sensor_fresh, self.stop_request, self.eoat_get_ack)):
            raise TypeError("sensor flags must be bool")
        if len(self.wrench) != 6 or not all(math.isfinite(float(value)) for value in self.wrench):
            raise ValueError("wrench must be six finite values")
        values = (
            self.normal_load_n,
            self.force_norm_n,
            self.heartbeat,
            self.torque_norm_nm,
            self.filtered_normal_n,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise ValueError("sensor packet values must be finite")


@dataclass(frozen=True)
class SessionInput:
    baseline_consecutive_successes: int
    command_mode: CommandMode
    sticky_one_newton_latched: int
    session_command: SessionCommand
    session_command_sequence: int
    session_epoch: int
    logical_attempt_ordinal: int
    attempt_kind: AttemptKind
    candidate_token: int

    def __post_init__(self) -> None:
        if not isinstance(self.command_mode, CommandMode):
            raise TypeError("command_mode must be typed CommandMode")
        if not isinstance(self.session_command, SessionCommand):
            raise TypeError("session_command must be typed SessionCommand")
        if not isinstance(self.attempt_kind, AttemptKind):
            raise TypeError("attempt_kind must be typed AttemptKind")
        if isinstance(self.sticky_one_newton_latched, bool) or self.sticky_one_newton_latched not in (0, 1):
            raise ValueError("sticky_one_newton_latched must be integer 0 or 1")
        for role, value in (
            ("baseline_consecutive_successes", self.baseline_consecutive_successes),
            ("session_command_sequence", self.session_command_sequence),
            ("session_epoch", self.session_epoch),
            ("logical_attempt_ordinal", self.logical_attempt_ordinal),
            ("candidate_token", self.candidate_token),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{role} must be a non-negative integer")
        if self.baseline_consecutive_successes > 3:
            raise ValueError("baseline consecutive-success counter is out of bounds")

    @property
    def by_register(self) -> dict[int, int]:
        return {
            24: self.baseline_consecutive_successes,
            25: int(self.command_mode),
            26: self.sticky_one_newton_latched,
            27: int(self.session_command),
            28: self.session_command_sequence,
            29: self.session_epoch,
            30: self.logical_attempt_ordinal,
            31: int(self.attempt_kind),
            32: self.candidate_token,
        }


@dataclass(frozen=True)
class PacketPayload:
    """Immutable field image used by the TP freshness guard."""

    doubles: tuple[float, ...]
    integers: tuple[int, ...]

    def __post_init__(self) -> None:
        if len(self.doubles) != 24 or len(self.integers) != 9:
            raise ValueError("layout-606 payload has 24 doubles and 9 integers")
        if not all(math.isfinite(float(value)) for value in self.doubles):
            raise ValueError("packet doubles must be finite")
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in self.integers):
            raise ValueError("packet integers must be typed integers")


@dataclass(frozen=True)
class WirePacket:
    double_values: tuple[float, ...]
    integer_values: tuple[int, ...]
    sequence: int
    command_mode: CommandMode
    stop_dominant: bool
    reason_code: int
    reason: str

    def __post_init__(self) -> None:
        if len(self.double_values) != 24 or len(self.integer_values) != 9:
            raise ValueError("wire packet shape differs from layout 606")
        if self.double_values[23] != LAYOUT_TAG:
            raise ValueError("wire layout tag differs")
        if self.double_values[23] != self.double_values[23]:
            raise ValueError("wire layout is nonfinite")
        if self.double_values[23] != 606.0:
            raise ValueError("wire layout is not 606")
        if not isinstance(self.command_mode, CommandMode) or not isinstance(self.stop_dominant, bool):
            raise TypeError("wire packet typed fields differ")

    @property
    def doubles_by_register(self) -> dict[int, float]:
        return dict(zip(DOUBLE_REGISTERS, self.double_values, strict=True))

    @property
    def integers_by_register(self) -> dict[int, int]:
        return dict(zip(INPUT_INTEGER_REGISTERS, self.integer_values, strict=True))

    @property
    def payload(self) -> PacketPayload:
        return PacketPayload(self.double_values, self.integer_values)


def _finite_qdot(
    values: Sequence[float],
    *,
    max_abs_rad_s: float | None = None,
    motion_profile: V4MotionProfile | None = None,
) -> tuple[float, ...]:
    if len(values) != 6:
        raise ValueError("qdot must have six values")
    result = tuple(float(value) for value in values)
    if motion_profile is not None:
        from .motion_profile import V4MotionProfile

        if not isinstance(motion_profile, V4MotionProfile):
            raise TypeError("motion_profile must be a typed V4MotionProfile")
        cap = motion_profile.qdot_cap_rad_s
    elif max_abs_rad_s is not None:
        cap = float(max_abs_rad_s)
    else:
        from .motion_profile import R004_MOTION_PROFILE

        cap = R004_MOTION_PROFILE.qdot_cap_rad_s
    if not math.isfinite(cap) or cap <= 0.0:
        raise ValueError("qdot envelope must be finite and positive")
    if not all(math.isfinite(value) and abs(value) <= cap + 1e-12 for value in result):
        raise ValueError(f"qdot exceeds the typed {cap:g} rad/s envelope")
    return result


def build_wire_packet(
    contract: R004Contract,
    candidate: Candidate,
    *,
    sensor: SensorPacket,
    proposed_qdot: Sequence[float],
    internal_setpoint_n: float,
    packet_sequence: int,
    session: SessionInput,
    structural_stop: bool = False,
    motion_profile: V4MotionProfile | None = None,
) -> WirePacket:
    """Build one bounded packet; the TP and FakeRTDE consume the same image."""

    del contract
    assert_target(candidate, TARGET_FORCE_N)
    if not isinstance(sensor, SensorPacket):
        raise TypeError("sensor must be a typed SensorPacket")
    if isinstance(packet_sequence, bool) or not isinstance(packet_sequence, int) or packet_sequence < 0:
        raise ValueError("packet_sequence must be a non-negative integer")
    if not math.isfinite(float(internal_setpoint_n)) or not 1.0 <= float(internal_setpoint_n) <= TARGET_FORCE_N:
        raise ValueError("internal setpoint must remain in [1, 5] N")
    qdot = _finite_qdot(proposed_qdot, motion_profile=motion_profile)
    stop_reason = 0
    reason = ""
    if not sensor.sensor_fresh:
        stop_reason, reason = 3, "sensor_stale"
    elif abs(sensor.normal_load_n) >= V3_R034_SAFETY_ENVELOPE.max_abs_normal_n:
        stop_reason, reason = 61, "hard_abs_normal_60n"
    elif sensor.force_norm_n >= V3_R034_SAFETY_ENVELOPE.max_force_norm_n:
        stop_reason, reason = 62, "hard_force_norm_100n"
    elif sensor.torque_norm_nm >= V3_R034_SAFETY_ENVELOPE.max_torque_norm_nm:
        stop_reason, reason = 63, "hard_torque_norm_3nm"
    elif sensor.stop_request:
        stop_reason, reason = 4, "external_stop"
    elif structural_stop:
        stop_reason, reason = 41, "structural_stop"
    elif session.command_mode is CommandMode.STOP:
        stop_reason, reason = 4, "typed_stop_mode"
    elif session.command_mode is CommandMode.HOLD and any(abs(value) > 0.0 for value in qdot):
        stop_reason, reason = 42, "hold_mode_requires_zero_qdot"
    elif session.command_mode is CommandMode.PATH and session.baseline_consecutive_successes < 3:
        stop_reason, reason = 50, "baseline_qualification_missing"
    stop = bool(stop_reason)
    mode = CommandMode.STOP if stop else session.command_mode
    values = {
        24: sensor.normal_load_n,
        25: sensor.force_norm_n,
        26: sensor.heartbeat,
        27: 1.0 if sensor.sensor_fresh else 0.0,
        28: 1.0 if stop else 0.0,
        29: 1.0 if sensor.eoat_get_ack else 0.0,
        30: sensor.torque_norm_nm,
        31: sensor.wrench[0],
        32: sensor.wrench[1],
        33: sensor.wrench[2],
        34: sensor.wrench[3],
        35: sensor.wrench[4],
        36: sensor.wrench[5],
        37: 0.0 if stop else qdot[0],
        38: 0.0 if stop else qdot[1],
        39: 0.0 if stop else qdot[2],
        40: 0.0 if stop else qdot[3],
        41: 0.0 if stop else qdot[4],
        42: 0.0 if stop else qdot[5],
        43: 0.0 if stop else 1.0,
        44: float(internal_setpoint_n),
        45: sensor.filtered_normal_n,
        46: float(packet_sequence),
        47: LAYOUT_TAG,
    }
    return WirePacket(
        double_values=tuple(values[index] for index in DOUBLE_REGISTERS),
        integer_values=tuple(session.by_register[index] for index in INPUT_INTEGER_REGISTERS),
        sequence=packet_sequence,
        command_mode=mode,
        stop_dominant=stop,
        reason_code=stop_reason,
        reason=reason,
    )


def validate_register_mappings() -> None:
    if set(DOUBLE_FIELDS) != set(DOUBLE_REGISTERS):
        raise R004ContractError("double register mapping collision or omission")
    if set(INTEGER_FIELDS) != set(INPUT_INTEGER_REGISTERS):
        raise R004ContractError("input integer register mapping collision or omission")
    if set(OUTPUT_INTEGER_FIELDS) != set(OUTPUT_INTEGER_REGISTERS):
        raise R004ContractError("output integer register mapping collision or omission")
    if set(OUTPUT_DOUBLE_FIELDS) != set(OUTPUT_DOUBLE_REGISTERS):
        raise R004ContractError("output double register mapping collision or omission")
    if len(set(DOUBLE_FIELDS.values())) != len(DOUBLE_FIELDS):
        raise R004ContractError("double field name collision")
    if len(set(INTEGER_FIELDS.values())) != len(INTEGER_FIELDS):
        raise R004ContractError("input integer field name collision")
    if len(set(OUTPUT_INTEGER_FIELDS.values())) != len(OUTPUT_INTEGER_FIELDS):
        raise R004ContractError("output integer field name collision")
    # RTDE directions are separate namespaces too: input ``attempt_kind`` and
    # output ``attempt_kind`` intentionally describe the echoed pair.


validate_register_mappings()

__all__ = [
    "AttemptKind",
    "CommandMode",
    "DOUBLE_FIELDS",
    "DOUBLE_REGISTERS",
    "INPUT_INTEGER_REGISTERS",
    "INTEGER_FIELDS",
    "LAYOUT_TAG",
    "OUTPUT_INTEGER_FIELDS",
    "OUTPUT_INTEGER_REGISTERS",
    "OUTPUT_DOUBLE_FIELDS",
    "OUTPUT_DOUBLE_REGISTERS",
    "PacketPayload",
    "ReturnGuard",
    "SensorPacket",
    "SessionCommand",
    "SessionInput",
    "TPState",
    "WIRE_SCHEMA",
    "WRENCH_AUTHORITY",
    "WirePacket",
    "build_wire_packet",
    "validate_register_mappings",
]
