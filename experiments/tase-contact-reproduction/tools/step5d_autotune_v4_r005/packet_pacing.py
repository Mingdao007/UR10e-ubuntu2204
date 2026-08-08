"""V3-style fresh-frame packet gate for the r005 host adapter.

This primitive is deliberately transport-neutral.  It makes the two
properties that are easy to lose in a host loop explicit: the resident TP
echo is reconciled before the first packet, and a cached controller frame can
never mint a new packet sequence.  The r004 writer composes the same rules at
its canonical RTDE owner; this module provides the deterministic r005 proof
surface without opening a socket.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from .timing import AbsoluteDeadlinePacer, NOMINAL_HZ


PACKET_STALE_S = 0.080


class PacketPacingError(RuntimeError):
    """A sequence, clock, or freshness invariant failed closed."""


@dataclass(frozen=True)
class PacketDecision:
    fresh: bool
    packet_sequence: int
    frame_identity: float
    stale_age_s: float


class FreshFramePacketGate:
    """Reconcile TP state and publish only on a distinct controller frame."""

    def __init__(self, *, initial_packet_sequence: int = 0) -> None:
        if (
            isinstance(initial_packet_sequence, bool)
            or not isinstance(initial_packet_sequence, int)
            or initial_packet_sequence < 0
        ):
            raise PacketPacingError("initial packet sequence must be non-negative")
        self._next_packet_sequence = initial_packet_sequence
        self._last_frame_identity: float | None = None
        self._last_fresh_at_s: float | None = None
        self._last_packet_sequence: int | None = None

    @property
    def next_packet_sequence(self) -> int:
        return self._next_packet_sequence

    @property
    def last_packet_sequence(self) -> int | None:
        return self._last_packet_sequence

    def reconcile_consumed_echo(self, consumed_packet_sequence: int) -> int:
        """Set the next sequence strictly above the resident TP echo."""

        if (
            isinstance(consumed_packet_sequence, bool)
            or not isinstance(consumed_packet_sequence, int)
            or consumed_packet_sequence < -1
        ):
            raise PacketPacingError("resident packet sequence echo is invalid")
        self._next_packet_sequence = max(
            self._next_packet_sequence, consumed_packet_sequence + 1
        )
        return self._next_packet_sequence

    @staticmethod
    def _clock(value: float) -> float:
        current = float(value)
        if not math.isfinite(current):
            raise PacketPacingError("fresh-frame clock is not finite")
        return current

    @staticmethod
    def _identity(value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PacketPacingError("controller frame identity is not numeric")
        identity = float(value)
        if not math.isfinite(identity):
            raise PacketPacingError("controller frame identity is not finite")
        return identity

    def observe(
        self,
        frame_identity: object | None,
        *,
        observed_at_s: float,
    ) -> PacketDecision:
        """Classify one host poll; ``None`` means no fresh controller frame."""

        now = self._clock(observed_at_s)
        if self._last_fresh_at_s is not None and now < self._last_fresh_at_s:
            raise PacketPacingError("fresh-frame clock regressed")
        if frame_identity is not None:
            identity = self._identity(frame_identity)
            if (
                self._last_frame_identity is not None
                and identity < self._last_frame_identity
            ):
                raise PacketPacingError("controller frame identity regressed")
        elif self._last_frame_identity is None:
            raise PacketPacingError("first controller poll has no fresh frame")
        else:
            identity = self._last_frame_identity

        distinct = (
            self._last_frame_identity is None
            or identity > self._last_frame_identity
        )
        if distinct:
            packet_sequence = self._next_packet_sequence
            self._next_packet_sequence += 1
            self._last_packet_sequence = packet_sequence
            self._last_frame_identity = identity
            self._last_fresh_at_s = now
            return PacketDecision(True, packet_sequence, identity, 0.0)

        assert self._last_fresh_at_s is not None
        age = now - self._last_fresh_at_s
        if age >= PACKET_STALE_S:
            raise PacketPacingError("controller output is stale at the 80 ms hard limit")
        assert self._last_packet_sequence is not None
        return PacketDecision(False, self._last_packet_sequence, identity, age)


@dataclass
class FreshFrameDeadlineTransport:
    """Compose the gate with V3's absolute-deadline 500 Hz pacer."""

    start_monotonic_s: float = 0.0
    gate: FreshFramePacketGate | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.start_monotonic_s)):
            raise PacketPacingError("transport start time is not finite")
        if self.gate is None:
            self.gate = FreshFramePacketGate()
        self.pacer = AbsoluteDeadlinePacer(
            frequency_hz=NOMINAL_HZ,
            start_monotonic=float(self.start_monotonic_s),
        )

    def reconcile_consumed_echo(self, consumed_packet_sequence: int) -> int:
        assert self.gate is not None
        return self.gate.reconcile_consumed_echo(consumed_packet_sequence)

    def cycle(
        self,
        frame_identity: object | None,
        *,
        now_s: float,
        publish: Callable[[int], None],
    ) -> PacketDecision:
        """Advance one absolute host slot and publish only distinct frames."""

        if not callable(publish):
            raise PacketPacingError("publish callback is not callable")
        assert self.gate is not None
        self.pacer.tick(now=float(now_s))
        decision = self.gate.observe(frame_identity, observed_at_s=float(now_s))
        if decision.fresh:
            publish(decision.packet_sequence)
        return decision


__all__ = [
    "FreshFrameDeadlineTransport",
    "FreshFramePacketGate",
    "PACKET_STALE_S",
    "PacketDecision",
    "PacketPacingError",
]
