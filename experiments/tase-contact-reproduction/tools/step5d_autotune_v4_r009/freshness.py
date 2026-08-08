"""Typed, transport-neutral R009 reason-43 freshness protocol.

R009 keeps terminal reason ``43`` for controller compatibility, while the
typed subtype makes the three freshness causes independently observable.  The
primitive is deliberately in-memory and has no socket, file, or controller
side effects.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum
from types import MappingProxyType
from typing import Any, Mapping


PACKET_FRESHNESS_FAILURE_REASON = 43
PACKET_STALE_S = 0.080
R009_REASON43_PROTOCOL_SCHEMA = (
    "step5d.autotune-v4/r009-reason43-runtime-protocol-v1"
)
R009_MALFORMED_PACKET_REASON = 41
R009_TIMESTAMP_REGRESSION_REASON = 40
R009_STOPPED_GUARD_REASON = 42


class Reason43Subtype(IntEnum):
    """The closed R009 subtype namespace for terminal reason 43."""

    NONE = 0
    SEQUENCE_REGRESSION = 1
    EQUAL_SEQUENCE_PAYLOAD_CHANGED = 2
    HELD_AGE_TIMEOUT = 3


Reason43Cause = Reason43Subtype


class R009FreshnessError(ValueError):
    """A malformed or inconsistent R009 freshness observation."""


def _integer_like(value: Any, role: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R009FreshnessError(f"{role} must be finite and integer-like")
    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        raise R009FreshnessError(f"{role} must be finite and integer-like")
    result = int(number)
    if result < minimum:
        raise R009FreshnessError(f"{role} is below its minimum")
    return result


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R009FreshnessError(f"{role} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise R009FreshnessError(f"{role} must be finite")
    return number


def _finite_vector(value: Any, role: str) -> tuple[float, ...]:
    if not isinstance(value, (tuple, list)):
        raise R009FreshnessError(f"{role} must be a finite numeric vector")
    result = tuple(_finite(item, f"{role}[{index}]") for index, item in enumerate(value))
    if not result:
        raise R009FreshnessError(f"{role} must not be empty")
    return result


@dataclass(frozen=True)
class R009PacketPayload:
    """Immutable FakeRTDE payload used for exact equality checks."""

    doubles: tuple[float, ...]
    integers: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        doubles = _finite_vector(self.doubles, "payload doubles")
        integers = tuple(
            _integer_like(value, f"payload integers[{index}]")
            for index, value in enumerate(self.integers)
        )
        object.__setattr__(self, "doubles", doubles)
        object.__setattr__(self, "integers", integers)

    @classmethod
    def from_values(
        cls, doubles: tuple[float, ...] | list[float], integers: tuple[int, ...] = ()
    ) -> "R009PacketPayload":
        return cls(tuple(doubles), tuple(integers))


def _coerce_payload(value: Any) -> R009PacketPayload:
    if isinstance(value, R009PacketPayload):
        return value
    # Structural compatibility lets an offline test use the mature r004
    # PacketPayload without importing or changing the r004 protocol.
    if hasattr(value, "doubles") and hasattr(value, "integers"):
        try:
            return R009PacketPayload(tuple(value.doubles), tuple(value.integers))
        except (TypeError, R009FreshnessError) as exc:
            raise R009FreshnessError("packet payload is malformed") from exc
    if isinstance(value, (tuple, list)):
        try:
            return R009PacketPayload(tuple(value))
        except (TypeError, R009FreshnessError) as exc:
            raise R009FreshnessError("packet payload is malformed") from exc
    if isinstance(value, Mapping):
        try:
            doubles = value["doubles"]
            integers = value.get("integers", ())
            return R009PacketPayload(tuple(doubles), tuple(integers))
        except (KeyError, TypeError, R009FreshnessError) as exc:
            raise R009FreshnessError("packet payload is malformed") from exc
    raise R009FreshnessError("packet payload is malformed")


@dataclass(frozen=True)
class R009PacketCache:
    sequence: int
    payload: R009PacketPayload
    last_new_timestamp_s: float

    def __post_init__(self) -> None:
        if self.sequence < 0 or not isinstance(self.sequence, int):
            raise R009FreshnessError("cached packet sequence is invalid")
        if not isinstance(self.payload, R009PacketPayload):
            raise R009FreshnessError("cached packet payload is not typed")
        timestamp = _finite(self.last_new_timestamp_s, "cached packet timestamp")
        if timestamp < 0.0:
            raise R009FreshnessError("cached packet timestamp is negative")
        object.__setattr__(self, "last_new_timestamp_s", timestamp)


@dataclass(frozen=True)
class R009FreshnessDecision:
    """One complete freshness result, including diagnostic status fields."""

    accepted: bool
    stopped: bool
    reason_code: int
    reason: str
    observed_packet_sequence: int | None
    cached_packet_sequence: int | None
    cache_age_s: float
    reason43_subtype: Reason43Subtype
    cache_updated: bool
    reused_cached_payload: bool
    cache: R009PacketCache | None

    @property
    def observed_sequence(self) -> int | None:
        return self.observed_packet_sequence

    @property
    def cached_sequence(self) -> int | None:
        return self.cached_packet_sequence

    @property
    def cache_age_seconds(self) -> float:
        return self.cache_age_s

    @property
    def equal_sequence_age_s(self) -> float:
        return self.cache_age_s

    @property
    def subtype(self) -> Reason43Subtype:
        return self.reason43_subtype

    @property
    def reason_43_subtype(self) -> Reason43Subtype:
        return self.reason43_subtype

    @property
    def diagnostic(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "observed_packet_sequence": self.observed_packet_sequence,
                "cached_packet_sequence": self.cached_packet_sequence,
                "cache_age_s": self.cache_age_s,
                "reason43_subtype": int(self.reason43_subtype),
                "accepted": self.accepted,
                "stopped": self.stopped,
                "cache_updated": self.cache_updated,
                "reused_cached_payload": self.reused_cached_payload,
            }
        )


def _coerce_subtype(value: Any) -> Reason43Subtype:
    try:
        return Reason43Subtype(_integer_like(value, "reason-43 subtype"))
    except (ValueError, R009FreshnessError) as exc:
        raise R009FreshnessError("unknown reason-43 subtype") from exc


def validate_reason43_diagnostic(
    *,
    terminal_reason: Any,
    tp_state: Any,
    reason43_subtype: Any,
    observed_packet_sequence: Any,
    cached_packet_sequence: Any,
    consumed_packet_sequence: Any,
    cache_age_s: Any,
) -> Reason43Subtype:
    """Validate cross-field reason-43 semantics shared by parser and host.

    A subtype is never inferred from the fields.  The declared subtype must
    agree with the fields or the row is rejected fail-closed.
    """

    reason = _integer_like(terminal_reason, "terminal reason")
    state = _integer_like(tp_state, "TP state")
    subtype = _coerce_subtype(reason43_subtype)
    observed = _integer_like(observed_packet_sequence, "observed packet sequence", minimum=-1)
    cached = _integer_like(cached_packet_sequence, "cached packet sequence", minimum=-1)
    consumed = _integer_like(consumed_packet_sequence, "consumed packet sequence", minimum=-1)
    age = _finite(cache_age_s, "cache age")
    if age < 0.0:
        raise R009FreshnessError("cache age must be non-negative")
    if consumed != cached:
        raise R009FreshnessError("consumed packet sequence differs from cached sequence")

    if reason == PACKET_FRESHNESS_FAILURE_REASON:
        if state != 90:
            raise R009FreshnessError("reason 43 must be published with TP state 90")
        if subtype is Reason43Subtype.NONE:
            raise R009FreshnessError("reason 43 cannot use subtype 0")
        if observed < 0 or cached < 0:
            raise R009FreshnessError("reason 43 requires a valid observed and cached sequence")
        if subtype is Reason43Subtype.SEQUENCE_REGRESSION:
            if not observed < cached:
                raise R009FreshnessError("sequence regression subtype is inconsistent")
        elif subtype is Reason43Subtype.EQUAL_SEQUENCE_PAYLOAD_CHANGED:
            if observed != cached:
                raise R009FreshnessError("equal-payload-change subtype is inconsistent")
        elif subtype is Reason43Subtype.HELD_AGE_TIMEOUT:
            if observed != cached or age < PACKET_STALE_S:
                raise R009FreshnessError("held-age-timeout subtype is inconsistent")
        else:  # pragma: no cover - IntEnum construction already closes this set.
            raise R009FreshnessError("unknown reason-43 subtype")
        return subtype

    if subtype is not Reason43Subtype.NONE:
        raise R009FreshnessError("non-43 terminal reason must use subtype 0")
    if cached == -1:
        if observed != -1:
            raise R009FreshnessError("missing cached sequence requires missing observed sequence")
    elif observed < cached:
        raise R009FreshnessError("non-43 row cannot report a regressed sequence")
    return subtype


class PacketFreshnessGuard:
    """Accept new packets and exact short-lived repeats, then stop fail-closed."""

    def __init__(self, *, max_equal_sequence_age_s: float = PACKET_STALE_S) -> None:
        age = _finite(max_equal_sequence_age_s, "max equal-sequence age")
        if age <= 0.0:
            raise R009FreshnessError("max equal-sequence age must be positive")
        self.max_equal_sequence_age_s = age
        self._cache: R009PacketCache | None = None
        self._last_observation_timestamp_s: float | None = None
        self._stopped = False
        self._stop_reason_code = R009_STOPPED_GUARD_REASON
        self._stop_reason = "freshness_guard_already_stopped"
        self._stop_subtype = Reason43Subtype.NONE

    @property
    def cache(self) -> R009PacketCache | None:
        return self._cache

    @property
    def stopped(self) -> bool:
        return self._stopped

    def _decision(
        self,
        *,
        accepted: bool,
        stopped: bool,
        reason_code: int,
        reason: str,
        observed: int | None,
        age_s: float,
        subtype: Reason43Subtype,
        cache_updated: bool,
        reused: bool,
    ) -> R009FreshnessDecision:
        cached = None if self._cache is None else self._cache.sequence
        return R009FreshnessDecision(
            accepted=accepted,
            stopped=stopped,
            reason_code=reason_code,
            reason=reason,
            observed_packet_sequence=observed,
            cached_packet_sequence=cached,
            cache_age_s=max(0.0, float(age_s)),
            reason43_subtype=subtype,
            cache_updated=cache_updated,
            reused_cached_payload=reused,
            cache=self._cache,
        )

    def _stop(
        self,
        reason: str,
        *,
        reason_code: int = PACKET_FRESHNESS_FAILURE_REASON,
        observed: int | None = None,
        age_s: float = 0.0,
        subtype: Reason43Subtype = Reason43Subtype.NONE,
    ) -> R009FreshnessDecision:
        if not self._stopped:
            self._stopped = True
            self._stop_reason_code = reason_code
            self._stop_reason = reason
            self._stop_subtype = subtype
        return self._decision(
            accepted=False,
            stopped=True,
            reason_code=self._stop_reason_code,
            reason=self._stop_reason,
            observed=observed,
            age_s=age_s,
            subtype=self._stop_subtype,
            cache_updated=False,
            reused=False,
        )

    def observe(
        self, sequence: Any, payload: Any, monotonic_timestamp_s: Any
    ) -> R009FreshnessDecision:
        if self._stopped:
            observed: int | None = None
            try:
                observed = _integer_like(sequence, "observed packet sequence")
            except R009FreshnessError:
                pass
            return self._stop(self._stop_reason, observed=observed, subtype=self._stop_subtype)

        try:
            observed = _integer_like(sequence, "observed packet sequence")
            normalized_payload = _coerce_payload(payload)
            now = _finite(monotonic_timestamp_s, "packet timestamp")
            if now < 0.0:
                raise R009FreshnessError("packet timestamp is negative")
        except R009FreshnessError as exc:
            return self._stop(
                str(exc),
                reason_code=R009_MALFORMED_PACKET_REASON,
                observed=None,
            )

        if (
            self._last_observation_timestamp_s is not None
            and now < self._last_observation_timestamp_s
        ):
            return self._stop(
                "packet_timestamp_regression",
                reason_code=R009_TIMESTAMP_REGRESSION_REASON,
                observed=observed,
            )
        self._last_observation_timestamp_s = now

        if self._cache is None:
            self._cache = R009PacketCache(observed, normalized_payload, now)
            return self._decision(
                accepted=True,
                stopped=False,
                reason_code=0,
                reason="new_packet",
                observed=observed,
                age_s=0.0,
                subtype=Reason43Subtype.NONE,
                cache_updated=True,
                reused=False,
            )
        if observed > self._cache.sequence:
            self._cache = R009PacketCache(observed, normalized_payload, now)
            return self._decision(
                accepted=True,
                stopped=False,
                reason_code=0,
                reason="newer_packet",
                observed=observed,
                age_s=0.0,
                subtype=Reason43Subtype.NONE,
                cache_updated=True,
                reused=False,
            )

        age = now - self._cache.last_new_timestamp_s
        if observed < self._cache.sequence:
            return self._stop(
                "sequence_regression",
                observed=observed,
                age_s=age,
                subtype=Reason43Subtype.SEQUENCE_REGRESSION,
            )
        if normalized_payload != self._cache.payload:
            return self._stop(
                "equal_sequence_payload_changed",
                observed=observed,
                age_s=age,
                subtype=Reason43Subtype.EQUAL_SEQUENCE_PAYLOAD_CHANGED,
            )
        if age >= self.max_equal_sequence_age_s:
            return self._stop(
                "held_age_timeout",
                observed=observed,
                age_s=age,
                subtype=Reason43Subtype.HELD_AGE_TIMEOUT,
            )
        return self._decision(
            accepted=True,
            stopped=False,
            reason_code=0,
            reason="exact_cached_packet_reuse",
            observed=observed,
            age_s=age,
            subtype=Reason43Subtype.NONE,
            cache_updated=False,
            reused=True,
        )


PacketFreshnessDecision = R009FreshnessDecision
PacketCache = R009PacketCache


__all__ = [
    "PACKET_FRESHNESS_FAILURE_REASON",
    "PACKET_STALE_S",
    "PacketCache",
    "PacketFreshnessDecision",
    "PacketFreshnessGuard",
    "R009FreshnessDecision",
    "R009FreshnessError",
    "R009PacketCache",
    "R009PacketPayload",
    "R009_REASON43_PROTOCOL_SCHEMA",
    "R009_MALFORMED_PACKET_REASON",
    "R009_STOPPED_GUARD_REASON",
    "R009_TIMESTAMP_REGRESSION_REASON",
    "Reason43Cause",
    "Reason43Subtype",
    "validate_reason43_diagnostic",
]
