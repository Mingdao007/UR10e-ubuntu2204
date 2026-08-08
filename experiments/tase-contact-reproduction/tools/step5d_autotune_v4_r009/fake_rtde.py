"""Deterministic R009 FakeRTDE cases with no network or socket access."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .freshness import (
    PACKET_STALE_S,
    PacketFreshnessGuard,
    R009FreshnessDecision,
    R009PacketPayload,
    Reason43Subtype,
)
from .identity import R009_RUNTIME_PROTOCOL


class FakeRTDEError(RuntimeError):
    """A deterministic R009 fixture was used incorrectly."""


@dataclass(frozen=True)
class FakeFrame:
    sequence: int
    payload: R009PacketPayload
    timestamp_s: float


R009FakeFrame = FakeFrame


@dataclass(frozen=True)
class R009TPPropagation:
    """Pure model of the R009 packet/guard-to-terminal publication seam."""

    state: int
    terminal_reason: int
    reason43_subtype: Reason43Subtype

    @classmethod
    def from_decision(
        cls,
        decision: R009FreshnessDecision,
        *,
        stationary: bool = False,
        stationary_fallback_reason: int = 11,
    ) -> "R009TPPropagation":
        if not isinstance(decision, R009FreshnessDecision):
            raise FakeRTDEError("R009 TP propagation requires a freshness decision")
        if (
            isinstance(stationary_fallback_reason, bool)
            or not isinstance(stationary_fallback_reason, int)
            or stationary_fallback_reason <= 0
        ):
            raise FakeRTDEError("stationary fallback reason must be a positive int")
        if decision.reason_code != 0:
            subtype = decision.reason43_subtype
            if decision.reason_code != 43:
                subtype = Reason43Subtype.NONE
            return cls(90, decision.reason_code, subtype)
        if stationary:
            return cls(90, stationary_fallback_reason, Reason43Subtype.NONE)
        return cls(78, 0, Reason43Subtype.NONE)


@dataclass
class R009TerminalSubtypeLatch:
    """Pure first-fault-wins model for the resident terminal subtype latch."""

    terminal_subtype: Reason43Subtype = Reason43Subtype.NONE
    instantaneous_subtype: Reason43Subtype = Reason43Subtype.NONE

    @staticmethod
    def _coerce_subtype(value: Reason43Subtype | int) -> Reason43Subtype:
        if isinstance(value, bool) or not isinstance(value, (int, Reason43Subtype)):
            raise FakeRTDEError("R009 terminal subtype must be a typed integer")
        try:
            return Reason43Subtype(value)
        except ValueError as exc:
            raise FakeRTDEError("R009 terminal subtype is unknown") from exc

    def observe(
        self,
        terminal_reason: int,
        instantaneous_subtype: Reason43Subtype | int,
    ) -> Reason43Subtype:
        if isinstance(terminal_reason, bool) or not isinstance(terminal_reason, int):
            raise FakeRTDEError("R009 terminal reason must be a typed integer")
        subtype = self._coerce_subtype(instantaneous_subtype)
        if terminal_reason == 43:
            if subtype is Reason43Subtype.NONE:
                raise FakeRTDEError("reason 43 requires a non-zero subtype")
            self.instantaneous_subtype = subtype
            if self.terminal_subtype is Reason43Subtype.NONE:
                self.terminal_subtype = subtype
            return self.terminal_subtype
        if subtype is not Reason43Subtype.NONE:
            raise FakeRTDEError("non-43 reason requires subtype zero")
        self.instantaneous_subtype = Reason43Subtype.NONE
        self.terminal_subtype = Reason43Subtype.NONE
        return self.terminal_subtype


def _payload(value: float, *, variant: int = 0) -> R009PacketPayload:
    return R009PacketPayload((float(value), float(variant)), (variant,))


class FakeRTDE:
    """A small fixture that can trigger each freshness branch independently."""

    def __init__(self, *, max_equal_sequence_age_s: float = PACKET_STALE_S) -> None:
        self.max_equal_sequence_age_s = float(max_equal_sequence_age_s)
        self.guard = PacketFreshnessGuard(
            max_equal_sequence_age_s=self.max_equal_sequence_age_s
        )
        self.last_decision: R009FreshnessDecision | None = None

    def observe(self, frame: FakeFrame) -> R009FreshnessDecision:
        if not isinstance(frame, FakeFrame):
            raise FakeRTDEError("R009 FakeRTDE requires a typed FakeFrame")
        self.last_decision = self.guard.observe(
            frame.sequence, frame.payload, frame.timestamp_s
        )
        return self.last_decision

    @staticmethod
    def _case(name: str) -> R009FreshnessDecision:
        guard = PacketFreshnessGuard()
        first = _payload(1.0)
        if name == "newer":
            guard.observe(7, first, 0.000)
            return guard.observe(8, _payload(2.0), 0.002)
        if name == "exact_equal":
            guard.observe(7, first, 0.000)
            return guard.observe(7, first, 0.002)
        if name == "sequence_regression":
            guard.observe(8, first, 0.000)
            return guard.observe(7, _payload(0.5), 0.002)
        if name == "equal_sequence_payload_changed":
            guard.observe(7, first, 0.000)
            return guard.observe(7, _payload(1.0, variant=1), 0.002)
        if name == "held_age_timeout":
            guard.observe(7, first, 0.000)
            return guard.observe(7, first, PACKET_STALE_S)
        raise FakeRTDEError(f"unknown R009 FakeRTDE case: {name}")

    @classmethod
    def run_case(cls, name: str) -> R009FreshnessDecision:
        return cls._case(name)

    @classmethod
    def deterministic_cases(cls) -> Mapping[str, R009FreshnessDecision]:
        return {
            name: cls.run_case(name)
            for name in (
                "newer",
                "exact_equal",
                "sequence_regression",
                "equal_sequence_payload_changed",
                "held_age_timeout",
            )
        }

    @staticmethod
    def output_mapping(
        decision: R009FreshnessDecision,
        *,
        state: int | None = None,
        terminal_reason: int | None = None,
        timestamp_s: float = 100.0,
    ) -> dict[str, Any]:
        if not isinstance(decision, R009FreshnessDecision):
            raise FakeRTDEError("output_mapping requires a freshness decision")
        reason = decision.reason_code if terminal_reason is None else int(terminal_reason)
        resolved_state = 90 if decision.stopped else (78 if state is None else int(state))
        observed = -1 if decision.observed_packet_sequence is None else decision.observed_packet_sequence
        cached = -1 if decision.cached_packet_sequence is None else decision.cached_packet_sequence
        return {
            "timestamp": float(timestamp_s),
            "payload": 0.413,
            "payload_cog": [0.0011, 0.0031, 0.0163],
            "tcp_offset": [0.0, 0.0, 0.0874, 0.0, 0.0, 0.0],
            "actual_TCP_speed": [0.0] * 6,
            "actual_TCP_pose": [0.487834547, 0.129337053, 0.033, 3.120752062, 0.0, 0.068626833],
            "actual_q": [0.0] * 6,
            "actual_qd": [0.0] * 6,
            "safety_mode": "NORMAL",
            "robot_mode": "RUNNING",
            "runtime_state": "RUNNING",
            "output_double_register_24": float(cached),
            "output_double_register_25": float(observed),
            "output_double_register_26": float(cached),
            "output_double_register_27": float(decision.cache_age_s),
            "output_int_register_24": 1,
            "output_int_register_25": 1,
            "output_int_register_26": resolved_state,
            "output_int_register_27": 0,
            "output_int_register_28": reason,
            "output_int_register_29": cached,
            "output_int_register_30": 1,
            "output_int_register_31": 0,
            "output_int_register_32": R009_RUNTIME_PROTOCOL,
            "output_int_register_33": 11,
            "output_int_register_34": 12,
            "output_int_register_35": int(decision.reason43_subtype),
        }


__all__ = [
    "FakeFrame",
    "FakeRTDE",
    "FakeRTDEError",
    "R009FakeFrame",
    "R009TerminalSubtypeLatch",
    "R009TPPropagation",
]
