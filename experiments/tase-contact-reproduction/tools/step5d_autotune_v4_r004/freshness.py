"""500 Hz TP/writer packet freshness primitive for r004."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import PACKET_STALE_S
from .wire import PacketPayload


PACKET_FRESHNESS_FAILURE_REASON = 43


@dataclass(frozen=True)
class PacketCache:
    sequence: int
    payload: PacketPayload
    last_new_timestamp_s: float


@dataclass(frozen=True)
class PacketFreshnessDecision:
    accepted: bool
    stopped: bool
    reason_code: int
    reason: str
    cache_updated: bool
    reused_cached_payload: bool
    equal_sequence_age_s: float
    cache: PacketCache | None


class PacketFreshnessGuard:
    """Accept a new packet or an exact short-lived repeat, fail closed otherwise."""

    def __init__(self, *, max_equal_sequence_age_s: float = PACKET_STALE_S) -> None:
        if not math.isfinite(max_equal_sequence_age_s) or max_equal_sequence_age_s <= 0.0:
            raise ValueError("max_equal_sequence_age_s must be positive and finite")
        self.max_equal_sequence_age_s = float(max_equal_sequence_age_s)
        self._cache: PacketCache | None = None
        self._last_observation_timestamp_s: float | None = None
        self._stopped = False

    @property
    def cache(self) -> PacketCache | None:
        return self._cache

    @property
    def stopped(self) -> bool:
        return self._stopped

    def _stop(self, reason: str, *, age_s: float = 0.0) -> PacketFreshnessDecision:
        self._stopped = True
        return PacketFreshnessDecision(
            accepted=False,
            stopped=True,
            reason_code=PACKET_FRESHNESS_FAILURE_REASON,
            reason=reason,
            cache_updated=False,
            reused_cached_payload=False,
            equal_sequence_age_s=age_s,
            cache=self._cache,
        )

    def observe(
        self, sequence: int, payload: PacketPayload, monotonic_timestamp_s: float
    ) -> PacketFreshnessDecision:
        if self._stopped:
            return self._stop("freshness_guard_already_stopped")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            return self._stop("malformed_packet_sequence")
        if not isinstance(payload, PacketPayload):
            return self._stop("malformed_packet_payload")
        if not math.isfinite(monotonic_timestamp_s):
            return self._stop("nonfinite_packet_timestamp")
        if (
            self._last_observation_timestamp_s is not None
            and monotonic_timestamp_s < self._last_observation_timestamp_s
        ):
            return self._stop("packet_timestamp_regression")
        self._last_observation_timestamp_s = float(monotonic_timestamp_s)
        if self._cache is None:
            self._cache = PacketCache(sequence, payload, float(monotonic_timestamp_s))
            return PacketFreshnessDecision(
                accepted=True,
                stopped=False,
                reason_code=0,
                reason="new_packet",
                cache_updated=True,
                reused_cached_payload=False,
                equal_sequence_age_s=0.0,
                cache=self._cache,
            )
        if sequence > self._cache.sequence:
            self._cache = PacketCache(sequence, payload, float(monotonic_timestamp_s))
            return PacketFreshnessDecision(
                accepted=True,
                stopped=False,
                reason_code=0,
                reason="newer_packet",
                cache_updated=True,
                reused_cached_payload=False,
                equal_sequence_age_s=0.0,
                cache=self._cache,
            )
        age_s = float(monotonic_timestamp_s) - self._cache.last_new_timestamp_s
        if sequence < self._cache.sequence:
            return self._stop("packet_sequence_regression", age_s=age_s)
        if payload != self._cache.payload:
            return self._stop("equal_sequence_payload_changed", age_s=age_s)
        if age_s >= self.max_equal_sequence_age_s:
            return self._stop("equal_sequence_held_for_80ms", age_s=age_s)
        return PacketFreshnessDecision(
            accepted=True,
            stopped=False,
            reason_code=0,
            reason="exact_cached_packet_reuse",
            cache_updated=False,
            reused_cached_payload=True,
            equal_sequence_age_s=age_s,
            cache=self._cache,
        )


__all__ = [
    "PACKET_FRESHNESS_FAILURE_REASON",
    "PacketCache",
    "PacketFreshnessDecision",
    "PacketFreshnessGuard",
]
