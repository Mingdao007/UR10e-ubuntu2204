"""Pure TP heartbeat/watchdog policy; no path or control-law implementation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class TpPhase(str, Enum):
    READY_HOME = "ready_home"
    ARMED = "armed"
    RUN = "run"
    RETRACT = "retract"
    RETURN = "return"
    HOME_VERIFY = "home_verify"
    WAIT_ACK = "wait_ack"


class WatchdogAction(str, Enum):
    CONTINUE = "continue"
    HALT_AT_VERIFIED_HOME = "halt_at_verified_home"
    CONTROLLED_STOP_AND_HALT = "controlled_stop_and_halt"
    SAFETY_HALT_UNKNOWN_HOME = "safety_halt_unknown_home"


@dataclass(frozen=True)
class WatchdogDecision:
    action: WatchdogAction
    terminal_reason: str | None
    auto_home: bool
    home_known: bool


def decide_watchdog(
    *,
    phase: TpPhase,
    heartbeat_age_s: float,
    timeout_s: float,
    safety_normal: bool,
    measured_home_verified: bool,
) -> WatchdogDecision:
    if not math.isfinite(heartbeat_age_s) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("watchdog ages must be finite and timeout positive")
    if heartbeat_age_s <= timeout_s and safety_normal:
        return WatchdogDecision(WatchdogAction.CONTINUE, None, False, measured_home_verified)
    if not safety_normal:
        return WatchdogDecision(
            WatchdogAction.SAFETY_HALT_UNKNOWN_HOME,
            "safety_not_normal",
            False,
            False,
        )
    if phase in {TpPhase.WAIT_ACK, TpPhase.READY_HOME} and measured_home_verified:
        return WatchdogDecision(
            WatchdogAction.HALT_AT_VERIFIED_HOME,
            "host_heartbeat_timeout_at_home",
            False,
            True,
        )
    return WatchdogDecision(
        WatchdogAction.CONTROLLED_STOP_AND_HALT,
        "host_heartbeat_timeout_unknown_home",
        False,
        False,
    )


@dataclass(frozen=True)
class HomeComparison:
    matches: bool
    position_error_m: float
    orientation_error_rad: float
    joint_error_rad: float
    blocker: str | None


def compare_measured_home(
    *,
    actual_tcp_pose: Sequence[float],
    saved_tcp_pose: Sequence[float],
    actual_q: Sequence[float],
    saved_q: Sequence[float],
    position_tolerance_m: float = 0.003,
    orientation_tolerance_rad: float = 0.05,
    joint_tolerance_rad: float = 0.01,
) -> HomeComparison:
    if any(len(row) != 6 for row in (actual_tcp_pose, saved_tcp_pose, actual_q, saved_q)):
        raise ValueError("Home comparison requires four six-element vectors")
    values = tuple(float(value) for row in (actual_tcp_pose, saved_tcp_pose, actual_q, saved_q) for value in row)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Home vectors must be finite")
    position = math.dist(actual_tcp_pose[:3], saved_tcp_pose[:3])
    orientation = math.dist(actual_tcp_pose[3:], saved_tcp_pose[3:])
    joint = max(abs(float(a) - float(b)) for a, b in zip(actual_q, saved_q))
    blockers = []
    if position > position_tolerance_m:
        blockers.append("home_position_mismatch")
    if orientation > orientation_tolerance_rad:
        blockers.append("home_orientation_mismatch")
    if joint > joint_tolerance_rad:
        blockers.append("home_joint_mismatch")
    return HomeComparison(
        matches=not blockers,
        position_error_m=position,
        orientation_error_rad=orientation,
        joint_error_rad=joint,
        blocker="+".join(blockers) if blockers else None,
    )
