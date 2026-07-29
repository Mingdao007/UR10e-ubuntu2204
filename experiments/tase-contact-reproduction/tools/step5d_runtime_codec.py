#!/usr/bin/env python3
"""Small runtime codecs shared without importing optimizer policy."""

from __future__ import annotations

from dataclasses import dataclass

from step5d_autotune_contract import ExecutionProfile


@dataclass(frozen=True)
class CadenceEvidence:
    profile_id: str
    sent_packets: int
    consumed_packets: int | None
    fresh_feedback_packets: int
    row_gap_over_20ms_count: int


def cadence_eligible(evidence: CadenceEvidence) -> tuple[bool, tuple[str, ...]]:
    failures: list[str] = []
    if evidence.sent_packets <= 0:
        failures.append("no_sent_packets")
    if evidence.consumed_packets is None:
        failures.append("missing_tp_consumption_echo")
    elif evidence.consumed_packets / max(evidence.sent_packets, 1) < 0.98:
        failures.append("tp_consumption_ratio_below_0p98")
    if evidence.fresh_feedback_packets / max(evidence.sent_packets, 1) < 0.98:
        failures.append("fresh_feedback_ratio_below_0p98")
    if evidence.row_gap_over_20ms_count != 0:
        failures.append("row_gap_over_20ms")
    return not failures, tuple(failures)


def execution_profile_integer_id(profile: ExecutionProfile) -> int:
    normal_levels = {
        0.010: 1,
        0.015: 2,
        0.020: 3,
        0.030: 4,
        0.050: 5,
        0.100: 6,
        0.500: 7,
        1.000: 8,
        2.000: 9,
        5.000: 10,
        15.000: 11,
    }
    actuator_levels = {0.1: 1, 0.2: 2, 0.5: 3, 2.5: 4}
    try:
        normal = normal_levels[profile.normal_max_rate_rad_s]
        host_slew = actuator_levels[profile.host_qdot_slew_rad_s2]
        tp_accel = actuator_levels[profile.tp_speedj_accel_rad_s2]
    except KeyError as exc:
        raise ValueError(
            "execution profile is outside the frozen profile lattice"
        ) from exc
    return 100 * normal + 10 * host_slew + tp_accel
