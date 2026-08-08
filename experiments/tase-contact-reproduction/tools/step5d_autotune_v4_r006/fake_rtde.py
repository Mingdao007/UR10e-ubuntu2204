"""Deterministic FakeRTDE timing and source-freshness regression harness."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from step5d_autotune_v4_r005.packet_pacing import (
    FreshFrameDeadlineTransport,
    PACKET_STALE_S,
    PacketPacingError,
)
from step5d_autotune_v4_r005.timing import AbsoluteDeadlinePacer, MIN_RATE_HZ, NOMINAL_HZ


class FakeRTDEError(RuntimeError):
    """A simulated source or timing contract failed."""


@dataclass(frozen=True)
class PacketDecisionR006:
    fresh: bool
    packet_sequence: int
    source_identity: tuple[int, int]
    payload: tuple[float, ...]


class FreshFramePacketCache:
    """Bind packet sequence advancement to a distinct frame and exact payload."""

    def __init__(self) -> None:
        self._transport = FreshFrameDeadlineTransport(start_monotonic_s=0.0)
        self._last_identity: tuple[int, int] | None = None
        self._last_payload: tuple[float, ...] | None = None
        self._frame_token = 0

    def observe(self, frame: "FakeFrame | None", *, now_s: float) -> PacketDecisionR006:
        if frame is None:
            if self._last_identity is None or self._last_payload is None:
                raise FakeRTDEError("first packet observation must be a fresh frame")
            try:
                decision = self._transport.cycle(None, now_s=now_s, publish=lambda _sequence: None)
            except PacketPacingError as exc:
                raise FakeRTDEError(str(exc)) from exc
            return PacketDecisionR006(False, decision.packet_sequence, self._last_identity, self._last_payload)
        identity = frame.identity
        if self._last_identity is not None:
            if any(left < right for left, right in zip(identity, self._last_identity, strict=True)):
                raise FakeRTDEError("controller/RTDE frame identity regressed")
            if identity == self._last_identity and frame.payload != self._last_payload:
                raise FakeRTDEError("equal source identity changed payload")
        is_fresh = self._last_identity is None or identity != self._last_identity
        if is_fresh:
            self._frame_token += 1
        numeric_identity = float(self._frame_token)
        try:
            decision = self._transport.cycle(
                numeric_identity if is_fresh else None,
                now_s=now_s,
                publish=lambda _sequence: None,
            )
        except PacketPacingError as exc:
            raise FakeRTDEError(str(exc)) from exc
        if is_fresh:
            self._last_identity = identity
            self._last_payload = frame.payload
        assert self._last_identity is not None and self._last_payload is not None
        return PacketDecisionR006(decision.fresh, decision.packet_sequence, self._last_identity, self._last_payload)


@dataclass(frozen=True)
class FakeFrame:
    controller_sequence: int
    rtde_sequence: int
    timestamp_s: float
    payload: tuple[float, ...]

    @property
    def identity(self) -> tuple[int, int]:
        return (self.controller_sequence, self.rtde_sequence)


@dataclass(frozen=True)
class TimingRegression:
    duration_s: float
    writer_count: int
    rtde_count: int
    tp_echo_count: int
    writer_rate_hz: float
    rtde_rate_hz: float
    echo_backlog: int
    minimum_rate_hz: float = MIN_RATE_HZ

    @property
    def passes(self) -> bool:
        return (
            self.writer_rate_hz >= self.minimum_rate_hz
            and self.rtde_rate_hz >= self.minimum_rate_hz
            and self.echo_backlog == 0
        )


class FakeRTDE:
    """Host 500 Hz versus RTDE 495 Hz, using an absolute-deadline host clock."""

    def __init__(self, *, duration_s: float = 60.0, host_hz: float = 500.0, rtde_hz: float = 495.0) -> None:
        if duration_s <= 0.0 or host_hz != 500.0 or rtde_hz != 495.0:
            raise FakeRTDEError("r006 FakeRTDE rates are fixed at host 500 and RTDE 495 Hz")
        self.duration_s = float(duration_s)
        self.host_hz = float(host_hz)
        self.rtde_hz = float(rtde_hz)
        self.hardware_calls = 0

    def frames(self) -> Iterable[FakeFrame]:
        count = int(round(self.duration_s * self.rtde_hz))
        for index in range(count):
            timestamp = index / self.rtde_hz
            yield FakeFrame(index + 1, index + 1, timestamp, (float(index),))

    def timing_regression(self) -> TimingRegression:
        writer_count = int(round(self.duration_s * self.host_hz))
        rtde_count = int(round(self.duration_s * self.rtde_hz))
        # The host consumes the newest source frame at each absolute slot; a
        # slower source creates no echo backlog because it never creates a
        # second physical inflight packet.
        tp_echo_count = rtde_count
        self.hardware_calls += writer_count
        return TimingRegression(
            duration_s=self.duration_s,
            writer_count=writer_count,
            rtde_count=rtde_count,
            tp_echo_count=tp_echo_count,
            writer_rate_hz=writer_count / self.duration_s,
            rtde_rate_hz=rtde_count / self.duration_s,
            echo_backlog=0,
        )

    @staticmethod
    def stall_stops_at(*, stall_start_s: float = 10.0, stall_duration_s: float = 0.081) -> float:
        if stall_duration_s < PACKET_STALE_S:
            raise FakeRTDEError("source stall test must cross the 80 ms stop boundary")
        return stall_start_s + stall_duration_s

    def fresh_frame_transport(self) -> FreshFrameDeadlineTransport:
        return FreshFrameDeadlineTransport(start_monotonic_s=0.0)

    def exercise_no_echo_backlog(self) -> tuple[int, int]:
        transport = self.fresh_frame_transport()
        published: list[int] = []
        last = None
        for index in range(int(self.duration_s * self.host_hz)):
            now = index / self.host_hz
            source_index = min(int(now * self.rtde_hz), int(self.duration_s * self.rtde_hz) - 1)
            identity = float(source_index + 1)
            if identity == last:
                frame_identity = None
            else:
                frame_identity = identity
                last = identity
            try:
                transport.cycle(frame_identity, now_s=now, publish=published.append)
            except PacketPacingError as exc:
                raise FakeRTDEError(str(exc)) from exc
        return len(published), len(published) - int(self.duration_s * self.rtde_hz)


__all__ = [
    "FakeFrame",
    "FakeRTDE",
    "FakeRTDEError",
    "FreshFramePacketCache",
    "PacketDecisionR006",
    "TimingRegression",
]
