"""Typed V4 register packet primitive after the Jacobian safety gate."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import Mapping, Sequence

from .baseline_ledger import BaselineQualificationSnapshot
from .contracts import TARGET_FORCE_N, V4Candidate, V4Contract, assert_runtime_target
from .policies import V4InvariantEnvelope
from .runtime import KinematicGateResult, RuntimeGuardError, gate_qdot


LAYOUT_CODE = 605.0
WRENCH_AUTHORITY = "kunwei_only"
DOUBLE_FIELDS = tuple(f"input_double_register_{index}" for index in range(24, 48))
INTEGER_FIELDS = ("input_integer_register_24", "input_integer_register_25")


class CommandMode(IntEnum):
    HOLD = 0
    BASELINE = 1
    PATH = 2
    RETRACT = 3
    STOP = 4


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
    filtered_normal_load_n: float
    wrench_authority: str = WRENCH_AUTHORITY


@dataclass(frozen=True)
class WirePacket:
    double_values: tuple[float, ...]
    integer_values: tuple[int, ...]
    gate: KinematicGateResult
    stop_dominant: bool
    reason: str

    @property
    def doubles_by_register(self) -> dict[int, float]:
        return {
            register: value
            for register, value in zip(range(24, 48), self.double_values, strict=True)
        }

    @property
    def integers_by_register(self) -> dict[int, int]:
        return {
            register: value
            for register, value in zip(
                range(24, 26), self.integer_values, strict=True
            )
        }


def build_wire_packet(
    contract: V4Contract,
    candidate: V4Candidate,
    *,
    sensor: SensorPacket,
    proposed_qdot: Sequence[float],
    jacobian_6x6: Sequence[Sequence[float]],
    normal_base: Sequence[float],
    observed_model_hashes: Mapping[str, str],
    internal_setpoint_n: float,
    command_sequence: int,
    baseline_qualification: BaselineQualificationSnapshot,
    command_mode: CommandMode = CommandMode.HOLD,
    structural_stop: bool = False,
) -> WirePacket:
    assert_runtime_target(candidate, TARGET_FORCE_N)
    if not isinstance(sensor, SensorPacket):
        raise TypeError("sensor must be a typed SensorPacket")
    if not all(
        isinstance(value, bool)
        for value in (sensor.sensor_fresh, sensor.stop_request, sensor.eoat_get_ack)
    ):
        raise TypeError("sensor flags must be typed booleans")
    if sensor.wrench_authority != WRENCH_AUTHORITY:
        raise ValueError("wrench authority must remain Kunwei-only")
    fingerprint = contract.raw.get("fingerprint")
    if not isinstance(fingerprint, Mapping) or (
        fingerprint.get("wrench_authority") != WRENCH_AUTHORITY
        or fingerprint.get("ur_builtin_force_allowed") is not False
    ):
        raise ValueError("V4 wrench authority envelope differs")
    if len(sensor.wrench) != 6:
        raise ValueError("sensor wrench must be six-dimensional")
    values = (
        sensor.normal_load_n,
        sensor.force_norm_n,
        sensor.heartbeat,
        sensor.torque_norm_nm,
        *sensor.wrench,
        sensor.filtered_normal_load_n,
        internal_setpoint_n,
    )
    try:
        finite = all(math.isfinite(float(value)) for value in values)
        nonnegative_sensor_norms = all(
            float(value) >= 0.0
            for value in (
                sensor.normal_load_n,
                sensor.force_norm_n,
                sensor.torque_norm_nm,
                sensor.filtered_normal_load_n,
                internal_setpoint_n,
            )
        )
    except (TypeError, ValueError, OverflowError):
        finite = False
        nonnegative_sensor_norms = False
    setpoint_valid = (
        finite
        and not isinstance(internal_setpoint_n, bool)
        and isinstance(internal_setpoint_n, (int, float))
        and 1.0 <= float(internal_setpoint_n) <= TARGET_FORCE_N
    )
    if (
        not isinstance(command_sequence, int)
        or isinstance(command_sequence, bool)
        or command_sequence < 0
    ):
        raise ValueError("command_sequence must be a non-negative integer")
    if not isinstance(baseline_qualification, BaselineQualificationSnapshot):
        raise TypeError("baseline qualification must come from its ledger snapshot")
    if not isinstance(command_mode, CommandMode):
        raise TypeError("command_mode must be a typed CommandMode")
    try:
        gate = gate_qdot(
            contract,
            qdot=proposed_qdot,
            jacobian_6x6=jacobian_6x6,
            normal_base=normal_base,
            observed_model_hashes=observed_model_hashes,
        )
    except RuntimeGuardError as exc:
        gate = KinematicGateResult(
            allowed=False,
            qdot=(0.0,) * 6,
            twist=(0.0,) * 6,
            total_linear_m_s=0.0,
            normal_m_s=0.0,
            tangential_m_s=0.0,
            angular_rad_s=0.0,
            reason=f"structural_gate:{exc}",
        )
    try:
        gate = V4InvariantEnvelope().enforce_gate(gate)
    except RuntimeGuardError as exc:
        gate = KinematicGateResult(
            allowed=False,
            qdot=(0.0,) * 6,
            twist=(0.0,) * 6,
            total_linear_m_s=0.0,
            normal_m_s=0.0,
            tangential_m_s=0.0,
            angular_rad_s=0.0,
            reason=f"invariant_envelope:{exc}",
        )
    stop_reason = ""
    if not finite:
        stop_reason = "nonfinite_sensor_or_setpoint"
    elif not setpoint_valid:
        stop_reason = "internal_setpoint_outside_1_to_5n"
    elif not nonnegative_sensor_norms:
        stop_reason = "negative_sensor_norm_or_setpoint"
    elif not sensor.sensor_fresh:
        stop_reason = "sensor_stale"
    elif not sensor.eoat_get_ack:
        stop_reason = "eoat_get_ack_missing"
    elif sensor.stop_request:
        stop_reason = "external_stop"
    elif structural_stop:
        stop_reason = "structural_stop"
    elif baseline_qualification.frozen:
        stop_reason = "baseline_qualification_frozen"
    elif command_mode is CommandMode.STOP:
        stop_reason = "typed_stop_mode"
    elif command_mode is CommandMode.HOLD and any(
        abs(float(value)) > 0.0 for value in proposed_qdot
    ):
        stop_reason = "hold_mode_requires_zero_qdot"
    elif command_mode is CommandMode.PATH and not baseline_qualification.full_path_allowed:
        stop_reason = "baseline_qualification_missing"
    elif not gate.allowed:
        stop_reason = gate.reason or "kinematic_gate_blocked"
    elif command_mode is CommandMode.BASELINE and (
        gate.tangential_m_s > 1e-9 or gate.angular_rad_s > 1e-9
    ):
        stop_reason = "baseline_forbids_xy_or_angular_motion"
    stop_dominant = bool(stop_reason)
    if stop_dominant:
        command_mode = CommandMode.STOP
    qdot = (0.0,) * 6 if stop_dominant else gate.qdot
    if stop_dominant:
        gate = KinematicGateResult(
            allowed=False,
            qdot=(0.0,) * 6,
            twist=gate.twist,
            total_linear_m_s=gate.total_linear_m_s,
            normal_m_s=gate.normal_m_s,
            tangential_m_s=gate.tangential_m_s,
            angular_rad_s=gate.angular_rad_s,
            reason=stop_reason,
        )
    wrench = sensor.wrench if finite else (0.0,) * 6
    packet_by_register = {
        24: sensor.normal_load_n if finite else 0.0,
        25: sensor.force_norm_n if finite else 0.0,
        26: sensor.heartbeat if finite else 0.0,
        27: 1.0 if sensor.sensor_fresh and finite else 0.0,
        28: 1.0 if stop_dominant else 0.0,
        29: 1.0 if sensor.eoat_get_ack else 0.0,
        30: sensor.torque_norm_nm if finite else 0.0,
        31: wrench[0],
        32: wrench[1],
        33: wrench[2],
        34: wrench[3],
        35: wrench[4],
        36: wrench[5],
        37: qdot[0],
        38: qdot[1],
        39: qdot[2],
        40: qdot[3],
        41: qdot[4],
        42: qdot[5],
        43: 0.0 if stop_dominant else 1.0,
        44: internal_setpoint_n if finite else 0.0,
        45: sensor.filtered_normal_load_n if finite else 0.0,
        46: float(command_sequence),
        47: LAYOUT_CODE,
    }
    return WirePacket(
        double_values=tuple(packet_by_register[index] for index in range(24, 48)),
        integer_values=(
            baseline_qualification.consecutive_successes,
            int(command_mode),
        ),
        gate=gate,
        stop_dominant=stop_dominant,
        reason=stop_reason,
    )


__all__ = [
    "DOUBLE_FIELDS",
    "CommandMode",
    "INTEGER_FIELDS",
    "LAYOUT_CODE",
    "SensorPacket",
    "WRENCH_AUTHORITY",
    "WirePacket",
    "build_wire_packet",
]
