"""Shared, profile-driven force-search decision primitive.

r005, immutable r006, and V4 provide profiles to this engine.  Their transport
and URScript presentation may differ, but guard ordering and state semantics
are accepted through this one behavior contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class ContactNormalMode(str, Enum):
    ABS_DELTA = "abs_delta"
    POSITIVE_RAW = "positive_raw"


@dataclass(frozen=True)
class ForceSearchProfile:
    profile_id: str
    heartbeat_limit_s: float
    heartbeat_limit_inclusive: bool
    max_travel_m: float
    runtime_limit_s: float
    contact_normal_n: float
    contact_force_norm_n: float
    hard_abs_normal_n: float
    hard_force_norm_n: float
    hard_torque_norm_nm: float
    contact_normal_mode: ContactNormalMode
    hard_guard_includes_delta: bool

    def __post_init__(self) -> None:
        if not self.profile_id:
            raise ValueError("force-search profile id is required")
        numeric = (
            self.heartbeat_limit_s,
            self.max_travel_m,
            self.runtime_limit_s,
            self.contact_normal_n,
            self.contact_force_norm_n,
            self.hard_abs_normal_n,
            self.hard_force_norm_n,
            self.hard_torque_norm_nm,
        )
        if not all(math.isfinite(float(value)) for value in numeric):
            raise ValueError("force-search profile contains nonfinite limits")
        if any(float(value) <= 0.0 for value in numeric):
            raise ValueError("force-search profile limits must be positive")
        if not (
            self.contact_normal_n < self.hard_abs_normal_n
            and self.contact_force_norm_n < self.hard_force_norm_n
        ):
            raise ValueError("force-search contact limits must be below hard limits")
        if not isinstance(self.heartbeat_limit_inclusive, bool) or not isinstance(
            self.hard_guard_includes_delta, bool
        ):
            raise ValueError("force-search profile flags must be typed booleans")
        if not isinstance(self.contact_normal_mode, ContactNormalMode):
            raise ValueError("force-search contact normal mode is untyped")


@dataclass(frozen=True)
class ForceSearchObservation:
    normal_n: float
    force_norm_n: float
    torque_norm_nm: float
    delta_normal_n: float = 0.0
    delta_force_norm_n: float = 0.0
    delta_torque_norm_nm: float = 0.0
    sensor_fresh: bool = True
    stop_requested: bool = False
    heartbeat_gap_s: float = 0.0
    travel_m: float = 0.0
    elapsed_s: float = 0.0


@dataclass(frozen=True)
class ForceSearchState:
    terminal_reason: int = 0


@dataclass(frozen=True)
class ForceSearchDecision:
    reason: int
    contact_latched: bool
    stop: bool
    search_allowed: bool
    retract_allowed: bool


class ForceSearchEngine:
    def __init__(self, profile: ForceSearchProfile) -> None:
        self.profile = profile

    def step(
        self, state: ForceSearchState, observation: ForceSearchObservation
    ) -> tuple[ForceSearchState, ForceSearchDecision]:
        if state.terminal_reason:
            return state, self._decision(state.terminal_reason)
        reason = self._reason(observation)
        next_state = (
            ForceSearchState(reason)
            if reason != 0
            else state
        )
        return next_state, self._decision(reason)

    def _reason(self, value: ForceSearchObservation) -> int:
        numeric = (
            value.normal_n,
            value.force_norm_n,
            value.torque_norm_nm,
            value.delta_normal_n,
            value.delta_force_norm_n,
            value.delta_torque_norm_nm,
            value.heartbeat_gap_s,
            value.travel_m,
            value.elapsed_s,
        )
        if not all(math.isfinite(item) for item in numeric):
            return 3
        if any(
            item < 0.0
            for item in (
                value.force_norm_n,
                value.torque_norm_nm,
                value.delta_force_norm_n,
                value.delta_torque_norm_nm,
            )
        ):
            return 3
        heartbeat_failed = (
            value.heartbeat_gap_s >= self.profile.heartbeat_limit_s
            if self.profile.heartbeat_limit_inclusive
            else value.heartbeat_gap_s > self.profile.heartbeat_limit_s
        )
        if heartbeat_failed:
            return 2
        if not value.sensor_fresh:
            return 3
        if value.stop_requested:
            return 4
        hard_normals = [abs(value.normal_n)]
        hard_forces = [value.force_norm_n]
        hard_torques = [value.torque_norm_nm]
        if self.profile.hard_guard_includes_delta:
            hard_normals.append(abs(value.delta_normal_n))
            hard_forces.append(value.delta_force_norm_n)
            hard_torques.append(value.delta_torque_norm_nm)
        if max(hard_normals) >= self.profile.hard_abs_normal_n:
            return 5
        if max(hard_forces) >= self.profile.hard_force_norm_n:
            return 6
        if max(hard_torques) >= self.profile.hard_torque_norm_nm:
            return 7
        contact_normal = (
            abs(value.delta_normal_n)
            if self.profile.contact_normal_mode is ContactNormalMode.ABS_DELTA
            else value.normal_n
        )
        contact_force = (
            value.delta_force_norm_n
            if self.profile.contact_normal_mode is ContactNormalMode.ABS_DELTA
            else value.force_norm_n
        )
        if (
            contact_normal >= self.profile.contact_normal_n
            or contact_force >= self.profile.contact_force_norm_n
        ):
            return 11
        if value.travel_m >= self.profile.max_travel_m:
            return 8
        if value.elapsed_s >= self.profile.runtime_limit_s:
            return 10
        return 0

    @staticmethod
    def _decision(reason: int) -> ForceSearchDecision:
        return ForceSearchDecision(
            reason=reason,
            contact_latched=reason == 11,
            stop=reason != 0,
            search_allowed=reason == 0,
            retract_allowed=reason == 11,
        )


__all__ = [
    "ContactNormalMode",
    "ForceSearchDecision",
    "ForceSearchEngine",
    "ForceSearchObservation",
    "ForceSearchProfile",
    "ForceSearchState",
]
