"""Bounded in-memory R009 diagnostic recorder.

The recorder is a host-side evidence primitive, not a launcher.  It performs
no socket or file I/O.  Every accepted row is timestamp-ordered, typed, and
serialized from an immutable event snapshot.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .freshness import (
    R009FreshnessError,
    Reason43Subtype,
    _integer_like,
    validate_reason43_diagnostic,
)
from .transport import R009OutputSnapshot


class R009DiagnosticError(RuntimeError):
    """A diagnostic row would violate ordering, bounds, or typed consistency."""


def _time(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R009DiagnosticError(f"{role} must be finite")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise R009DiagnosticError(f"{role} must be finite and non-negative")
    return result


def _sequence(value: Any, role: str, *, minimum: int = 0) -> int:
    try:
        return _integer_like(value, role, minimum=minimum)
    except R009FreshnessError as exc:
        raise R009DiagnosticError(str(exc)) from exc


def _frame_identity(value: Any) -> tuple[int, ...]:
    if isinstance(value, bool):
        raise R009DiagnosticError("RTDE frame identity is invalid")
    if isinstance(value, int):
        return (_sequence(value, "RTDE frame identity"),)
    if not isinstance(value, (tuple, list)) or not value:
        raise R009DiagnosticError("RTDE frame identity must be a non-empty integer tuple")
    return tuple(
        _sequence(item, f"RTDE frame identity[{index}]")
        for index, item in enumerate(value)
    )


@dataclass(frozen=True)
class PacketPublishRecord:
    timestamp_s: float
    sequence: int


@dataclass(frozen=True)
class RTDEPollRecord:
    timestamp_s: float
    frame_identity: tuple[int, ...]


@dataclass(frozen=True)
class TPDiagnosticRecord:
    timestamp_s: float
    state: int
    terminal_reason: int
    reason43_subtype: Reason43Subtype
    observed_packet_sequence: int
    cached_packet_sequence: int
    cache_age_s: float
    consumed_packet_sequence: int

    @property
    def reason(self) -> int:
        return self.terminal_reason


@dataclass(frozen=True)
class CloseRecord:
    timestamp_s: float
    layer: str
    resource: str


@dataclass(frozen=True)
class DiagnosticEvent:
    ordinal: int
    timestamp_s: float
    kind: str
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "timestamp_s": self.timestamp_s,
            "kind": self.kind,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True)
class R009DiagnosticSnapshot:
    events: tuple[DiagnosticEvent, ...]
    last_packet_publish: PacketPublishRecord | None
    last_rtde_poll: RTDEPollRecord | None
    last_tp_output: TPDiagnosticRecord | None
    close_order: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        def publish(value: PacketPublishRecord | None) -> dict[str, Any] | None:
            return None if value is None else {
                "timestamp_s": value.timestamp_s,
                "sequence": value.sequence,
            }

        def poll(value: RTDEPollRecord | None) -> dict[str, Any] | None:
            return None if value is None else {
                "timestamp_s": value.timestamp_s,
                "frame_identity": list(value.frame_identity),
            }

        def tp(value: TPDiagnosticRecord | None) -> dict[str, Any] | None:
            return None if value is None else {
                "timestamp_s": value.timestamp_s,
                "state": value.state,
                "terminal_reason": value.terminal_reason,
                "reason43_subtype": int(value.reason43_subtype),
                "observed_packet_sequence": value.observed_packet_sequence,
                "cached_packet_sequence": value.cached_packet_sequence,
                "cache_age_s": value.cache_age_s,
                "consumed_packet_sequence": value.consumed_packet_sequence,
            }

        return {
            "events": [event.as_dict() for event in self.events],
            "last_packet_publish": publish(self.last_packet_publish),
            "last_rtde_poll": poll(self.last_rtde_poll),
            "last_tp_output": tp(self.last_tp_output),
            "close_order": list(self.close_order),
        }

    def serialize(self) -> bytes:
        return json.dumps(
            self.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")

    def to_json(self) -> str:
        return self.serialize().decode("utf-8")


class R009DiagnosticRecorder:
    """Bounded append-only event recorder with deterministic snapshots."""

    def __init__(self, *, max_events: int = 256) -> None:
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events <= 0:
            raise ValueError("max_events must be a positive integer")
        self.max_events = max_events
        self._events: list[DiagnosticEvent] = []
        self._last_event_timestamp_s: float | None = None
        self._last_packet_publish: PacketPublishRecord | None = None
        self._last_rtde_poll: RTDEPollRecord | None = None
        self._last_tp_output: TPDiagnosticRecord | None = None
        self._close_records: list[CloseRecord] = []
        self._socket_close_started = False

    def _append(self, timestamp_s: Any, kind: str, payload: Mapping[str, Any]) -> None:
        timestamp = _time(timestamp_s, f"{kind} timestamp")
        if self._last_event_timestamp_s is not None and timestamp < self._last_event_timestamp_s:
            raise R009DiagnosticError("diagnostic event timestamp regressed")
        if len(self._events) >= self.max_events:
            raise R009DiagnosticError("diagnostic recorder event bound exceeded")
        self._last_event_timestamp_s = timestamp
        self._events.append(
            DiagnosticEvent(
                ordinal=len(self._events) + 1,
                timestamp_s=timestamp,
                kind=kind,
                payload=MappingProxyType(dict(payload)),
            )
        )

    def _ensure_event_open(self) -> None:
        if self._close_records:
            raise R009DiagnosticError("diagnostic event occurred after transport close")

    def _ensure_close_open(self) -> None:
        if self._socket_close_started:
            raise R009DiagnosticError("diagnostic close occurred after socket close")

    def record_packet_publish(self, timestamp_s: Any, sequence: Any) -> PacketPublishRecord:
        self._ensure_event_open()
        normalized = _sequence(sequence, "published packet sequence")
        if self._last_packet_publish is not None and normalized <= self._last_packet_publish.sequence:
            raise R009DiagnosticError("packet publish sequence regressed or repeated")
        timestamp = _time(timestamp_s, "packet publish timestamp")
        record = PacketPublishRecord(timestamp, normalized)
        self._append(timestamp, "packet_publish", {"sequence": normalized})
        self._last_packet_publish = record
        return record

    def record_rtde_poll(self, timestamp_s: Any, frame_identity: Any) -> RTDEPollRecord:
        self._ensure_event_open()
        identity = _frame_identity(frame_identity)
        if self._last_rtde_poll is not None and identity <= self._last_rtde_poll.frame_identity:
            raise R009DiagnosticError("RTDE frame identity regressed or repeated")
        timestamp = _time(timestamp_s, "RTDE poll timestamp")
        record = RTDEPollRecord(timestamp, identity)
        self._append(timestamp, "rtde_poll", {"frame_identity": list(identity)})
        self._last_rtde_poll = record
        return record

    def record_tp_output(
        self,
        timestamp_s: Any,
        *,
        state: Any,
        terminal_reason: Any,
        reason43_subtype: Any,
        observed_packet_sequence: Any,
        cached_packet_sequence: Any,
        cache_age_s: Any,
        consumed_packet_sequence: Any,
    ) -> TPDiagnosticRecord:
        self._ensure_event_open()
        normalized_state = _sequence(state, "TP state")
        normalized_reason = _sequence(terminal_reason, "terminal reason")
        normalized_subtype = _sequence(reason43_subtype, "reason-43 subtype")
        observed = _sequence(observed_packet_sequence, "observed packet sequence", minimum=-1)
        cached = _sequence(cached_packet_sequence, "cached packet sequence", minimum=-1)
        consumed = _sequence(consumed_packet_sequence, "consumed packet sequence", minimum=-1)
        try:
            subtype = validate_reason43_diagnostic(
                terminal_reason=normalized_reason,
                tp_state=normalized_state,
                reason43_subtype=normalized_subtype,
                observed_packet_sequence=observed,
                cached_packet_sequence=cached,
                consumed_packet_sequence=consumed,
                cache_age_s=cache_age_s,
            )
        except R009FreshnessError as exc:
            raise R009DiagnosticError(str(exc)) from exc
        age = _time(cache_age_s, "cache age")
        record = TPDiagnosticRecord(
            timestamp_s=_time(timestamp_s, "TP output timestamp"),
            state=normalized_state,
            terminal_reason=normalized_reason,
            reason43_subtype=subtype,
            observed_packet_sequence=observed,
            cached_packet_sequence=cached,
            cache_age_s=age,
            consumed_packet_sequence=consumed,
        )
        self._append(
            record.timestamp_s,
            "tp_output",
            {
                "state": record.state,
                "terminal_reason": record.terminal_reason,
                "reason43_subtype": int(record.reason43_subtype),
                "observed_packet_sequence": record.observed_packet_sequence,
                "cached_packet_sequence": record.cached_packet_sequence,
                "cache_age_s": record.cache_age_s,
                "consumed_packet_sequence": record.consumed_packet_sequence,
            },
        )
        self._last_tp_output = record
        return record

    def record_output(
        self, snapshot: R009OutputSnapshot, *, timestamp_s: Any | None = None
    ) -> TPDiagnosticRecord:
        if not isinstance(snapshot, R009OutputSnapshot):
            raise R009DiagnosticError("record_output requires an R009OutputSnapshot")
        timestamp = snapshot.observed_at_s if timestamp_s is None else timestamp_s
        return self.record_tp_output(
            timestamp,
            state=snapshot.state,
            terminal_reason=snapshot.terminal_reason,
            reason43_subtype=snapshot.reason43_subtype,
            observed_packet_sequence=snapshot.observed_packet_sequence,
            cached_packet_sequence=snapshot.cached_packet_sequence,
            cache_age_s=snapshot.cache_age_s,
            consumed_packet_sequence=snapshot.consumed_packet_sequence,
        )

    def record_transport_close(self, timestamp_s: Any, transport: str = "rtde") -> CloseRecord:
        self._ensure_close_open()
        name = str(transport)
        if not name:
            raise R009DiagnosticError("transport name is empty")
        if self._socket_close_started:
            raise R009DiagnosticError("transport close occurred after socket close")
        if any(item.layer == "transport" and item.resource == name for item in self._close_records):
            raise R009DiagnosticError("transport close is duplicated")
        record = CloseRecord(_time(timestamp_s, "transport close timestamp"), "transport", name)
        self._append(record.timestamp_s, "transport_close", {"resource": name})
        self._close_records.append(record)
        return record

    def record_socket_close(self, timestamp_s: Any, socket: str = "rtde") -> CloseRecord:
        self._ensure_close_open()
        name = str(socket)
        if not name:
            raise R009DiagnosticError("socket name is empty")
        if not self._close_records or not any(item.layer == "transport" for item in self._close_records):
            raise R009DiagnosticError("socket close occurred before transport close")
        if any(item.layer == "socket" and item.resource == name for item in self._close_records):
            raise R009DiagnosticError("socket close is duplicated")
        record = CloseRecord(_time(timestamp_s, "socket close timestamp"), "socket", name)
        self._append(record.timestamp_s, "socket_close", {"resource": name})
        self._socket_close_started = True
        self._close_records.append(record)
        return record

    @property
    def events(self) -> tuple[DiagnosticEvent, ...]:
        return tuple(self._events)

    @property
    def close_order(self) -> tuple[str, ...]:
        return tuple(f"{item.layer}:{item.resource}" for item in self._close_records)

    def snapshot(self) -> R009DiagnosticSnapshot:
        return R009DiagnosticSnapshot(
            events=tuple(self._events),
            last_packet_publish=self._last_packet_publish,
            last_rtde_poll=self._last_rtde_poll,
            last_tp_output=self._last_tp_output,
            close_order=self.close_order,
        )

    def as_dict(self) -> dict[str, Any]:
        return self.snapshot().as_dict()

    def serialize(self) -> bytes:
        return self.snapshot().serialize()


__all__ = [
    "CloseRecord",
    "DiagnosticEvent",
    "PacketPublishRecord",
    "R009DiagnosticError",
    "R009DiagnosticRecorder",
    "R009DiagnosticSnapshot",
    "RTDEPollRecord",
    "TPDiagnosticRecord",
]
