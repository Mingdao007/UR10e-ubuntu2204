"""Common packet/RTDE sequence and timestamp binding for qdot and actual_qd."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


class AlignmentError(RuntimeError):
    """qdot and actual_qd were not captured from one common packet identity."""


@dataclass(frozen=True)
class JointVelocityPacket:
    packet_sequence: int
    rtde_sequence: int
    timestamp_s: float
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        for name in ("packet_sequence", "rtde_sequence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AlignmentError(f"{name} must be a positive integer")
        if not math.isfinite(float(self.timestamp_s)):
            raise AlignmentError("joint packet timestamp is not finite")
        if not self.values or any(not math.isfinite(float(value)) for value in self.values):
            raise AlignmentError("joint packet values are invalid")


@dataclass(frozen=True)
class AlignedJointVelocity:
    packet_sequence: int
    rtde_sequence: int
    timestamp_s: float
    qdot: tuple[float, ...]
    actual_qd: tuple[float, ...]

    def __post_init__(self) -> None:
        for name in ("packet_sequence", "rtde_sequence"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AlignmentError(f"aligned {name} must be a positive integer")
        if not math.isfinite(float(self.timestamp_s)):
            raise AlignmentError("aligned timestamp is not finite")
        if (
            not self.qdot
            or len(self.qdot) != len(self.actual_qd)
            or any(not math.isfinite(float(value)) for value in (*self.qdot, *self.actual_qd))
        ):
            raise AlignmentError("aligned qdot/actual_qd values are invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "packet_sequence": self.packet_sequence,
            "rtde_sequence": self.rtde_sequence,
            "timestamp_s": self.timestamp_s,
            "commanded_qdot": list(self.qdot),
            "actual_qd": list(self.actual_qd),
        }


def align_qdot_actual_qd(
    qdot: JointVelocityPacket,
    actual_qd: JointVelocityPacket,
    *,
    max_time_delta_s: float = 0.001,
) -> AlignedJointVelocity:
    if qdot.packet_sequence != actual_qd.packet_sequence:
        raise AlignmentError("qdot and actual_qd packet sequence differs")
    if qdot.rtde_sequence != actual_qd.rtde_sequence:
        raise AlignmentError("qdot and actual_qd RTDE sequence differs")
    if len(qdot.values) != len(actual_qd.values):
        raise AlignmentError("qdot and actual_qd joint dimensions differ")
    delta = abs(qdot.timestamp_s - actual_qd.timestamp_s)
    if delta > max_time_delta_s:
        raise AlignmentError("qdot and actual_qd time binding exceeds the packet tolerance")
    return AlignedJointVelocity(
        packet_sequence=qdot.packet_sequence,
        rtde_sequence=qdot.rtde_sequence,
        timestamp_s=(qdot.timestamp_s + actual_qd.timestamp_s) / 2.0,
        qdot=qdot.values,
        actual_qd=actual_qd.values,
    )


def align_streams(
    qdot_packets: Sequence[JointVelocityPacket],
    actual_qd_packets: Sequence[JointVelocityPacket],
) -> tuple[AlignedJointVelocity, ...]:
    if len(qdot_packets) != len(actual_qd_packets):
        raise AlignmentError("qdot and actual_qd packet counts differ")
    return tuple(
        align_qdot_actual_qd(qdot, actual)
        for qdot, actual in zip(qdot_packets, actual_qd_packets)
    )


__all__ = [
    "AlignedJointVelocity",
    "AlignmentError",
    "JointVelocityPacket",
    "align_qdot_actual_qd",
    "align_streams",
]
