"""Faithful offline RTDE model for r004 cadence and integer session registers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from collections import deque
from typing import Any, Mapping, Sequence

from step5d_eoat_profiles import load_new_eoat_profile

from .freshness import PacketFreshnessDecision, PacketFreshnessGuard
from .wire import INPUT_INTEGER_REGISTERS, DOUBLE_REGISTERS, PacketPayload, SessionInput, WirePacket
from .contracts import R004Contract, runtime_identity_limbs, RUNTIME_PROTOCOL
from .transport import R004OutputSnapshot


@dataclass(frozen=True)
class FakeRegisterSnapshot:
    doubles: tuple[float, ...]
    integers: tuple[int, ...]


class FakeRTDE:
    """No write-count stage advancement; registers and cadence are explicit."""

    def __init__(self, *, tp_hz: float = 500.0, writer_hz: float = 125.0) -> None:
        if tp_hz <= 0.0 or writer_hz <= 0.0 or tp_hz < writer_hz:
            raise ValueError("FakeRTDE cadence must be positive with TP >= writer")
        self.tp_period_s = 1.0 / tp_hz
        self.writer_period_s = 1.0 / writer_hz
        self.now_s = 0.0
        self.doubles = {register: 0.0 for register in DOUBLE_REGISTERS}
        self.integers = {register: 0 for register in INPUT_INTEGER_REGISTERS}
        self.freshness = PacketFreshnessGuard()
        self.write_count = 0
        self.stage = "READY_HOME_NEXT"

    def write_packet(self, packet: WirePacket) -> None:
        for register, value in packet.doubles_by_register.items():
            self.doubles[register] = value
        for register, value in packet.integers_by_register.items():
            self.integers[register] = value
        self.write_count += 1

    def write_integer_register(self, register: int, value: int) -> None:
        if register not in self.integers:
            raise KeyError(f"integer input register {register} is not in r004 wire")
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("FakeRTDE integer register requires int")
        self.integers[register] = value

    def read_integer_register(self, register: int) -> int:
        return self.integers[register]

    def snapshot(self) -> FakeRegisterSnapshot:
        return FakeRegisterSnapshot(
            doubles=tuple(self.doubles[index] for index in DOUBLE_REGISTERS),
            integers=tuple(self.integers[index] for index in INPUT_INTEGER_REGISTERS),
        )

    def tp_tick(self, *, timestamp_s: float | None = None) -> PacketFreshnessDecision:
        if timestamp_s is None:
            self.now_s += self.tp_period_s
        else:
            self.now_s = float(timestamp_s)
        payload = PacketPayload(
            doubles=tuple(self.doubles[index] for index in DOUBLE_REGISTERS),
            integers=tuple(self.integers[index] for index in INPUT_INTEGER_REGISTERS),
        )
        sequence = int(round(self.doubles[46]))
        decision = self.freshness.observe(sequence, payload, self.now_s)
        # Stage is intentionally not advanced by write count; session commands
        # are the only stage-changing inputs in this model.
        return decision

    def send_session(self, session: SessionInput) -> None:
        for register, value in session.by_register.items():
            self.write_integer_register(register, value)


class FakeLiveRTDETransport:
    """Faithful live-boundary double used by tests; it never opens a socket."""

    hardware_calls = 0

    def __init__(
        self,
        contract: R004Contract,
        *,
        output_queue: Sequence[Mapping[str, Any]] = (),
        events: list[str] | None = None,
    ) -> None:
        self.contract = contract
        self.opened = False
        self.closed = False
        self.sent_packets: list[tuple[tuple[float, ...], tuple[int, ...]]] = []
        self._queue = deque(dict(item) for item in output_queue)
        self._input_doubles = [0.0] * 24
        self._input_integers = [0] * 9
        self._state = 78
        self._reason = 0
        self._return_guard = 127
        self._latest: R004OutputSnapshot | None = None
        self.events = events

    def open(self) -> None:
        if self.opened:
            raise RuntimeError("Fake r004 RTDE is already open")
        self.opened = True
        if self.events is not None:
            self.events.append("rtde.open")

    def close(self) -> None:
        self.closed = True
        self.opened = False
        if self.events is not None:
            self.events.append("rtde.close")

    def queue_output(self, value: Mapping[str, Any]) -> None:
        self._queue.append(dict(value))

    def _mapping(self) -> dict[str, Any]:
        profile = load_new_eoat_profile()
        hi, lo = runtime_identity_limbs(
            self.contract.raw["program"], self.contract.sha256, self.contract.campaign_fingerprint
        )
        return {
            "timestamp": time.time(),
            "payload": profile.payload_kg,
            "payload_cog": list(profile.cog_m),
            "tcp_offset": list(profile.controller_tcp_m_rad),
            "actual_TCP_speed": [0.0] * 6,
            "actual_TCP_pose": [0.487834547, 0.129337053, 0.033, 3.120752062, 0.0, 0.068626833],
            "actual_q": [0.0] * 6,
            "actual_qd": [0.0] * 6,
            "safety_mode": "NORMAL",
            "robot_mode": "RUNNING",
            "runtime_state": "RUNNING",
            "output_int_register_24": self._input_integers[5],
            "output_int_register_25": self._input_integers[6],
            "output_int_register_26": self._state,
            "output_int_register_27": self._input_integers[8],
            "output_int_register_28": self._reason,
            "output_int_register_29": self._input_integers[4],
            "output_int_register_30": self._input_integers[7],
            "output_int_register_31": self._return_guard,
            "output_int_register_32": RUNTIME_PROTOCOL,
            "output_int_register_33": hi,
            "output_int_register_34": lo,
        }

    def send_packet(self, double_values: Sequence[float], integer_values: Sequence[int]) -> None:
        if not self.opened:
            raise RuntimeError("Fake r004 RTDE is not open")
        if len(double_values) != 24 or len(integer_values) != 9:
            raise ValueError("Fake r004 packet shape differs")
        self._input_doubles = [float(value) for value in double_values]
        self._input_integers = [int(value) for value in integer_values]
        self.sent_packets.append((tuple(self._input_doubles), tuple(self._input_integers)))
        if self.events is not None:
            self.events.append("rtde.send")
        command = self._input_integers[3]
        if command == 1:
            self._state = 25
            self._return_guard = 0
        elif command == 2:
            self._state = 80
        elif command == 3 or self._input_doubles[4] >= 0.5:
            self._state = 90
            self._reason = 4
        elif command == 0 and self._state == 25:
            self._state = 78
            self._return_guard = 127

    def poll_output(self) -> R004OutputSnapshot | None:
        if not self.opened:
            raise RuntimeError("Fake r004 RTDE is not open")
        value = self._queue.popleft() if self._queue else self._mapping()
        self._latest = R004OutputSnapshot.from_mapping(time.time(), value)
        return self._latest


class FakeLiveKunweiTransport:
    """Deterministic SI wrench source with explicit cleanup instrumentation."""

    hardware_calls = 0

    def __init__(
        self,
        *,
        wrench_n_nm: Sequence[float] = (0.0, 0.0, -5.0, 0.0, 0.0, 0.0),
        events: list[str] | None = None,
    ) -> None:
        if len(wrench_n_nm) != 6:
            raise ValueError("Fake Kunwei wrench must have six values")
        self.wrench_n_nm = tuple(float(value) for value in wrench_n_nm)
        self.opened = False
        self.closed = False
        self.stop_stream_sent = False
        self.now_s = 0.0
        self.events = events

    def open(self) -> None:
        self.opened = True
        if self.events is not None:
            self.events.append("kunwei.open")

    def close(self) -> None:
        self.stop_stream_sent = True
        self.closed = True
        self.opened = False
        if self.events is not None:
            self.events.extend(("kunwei.stop_stream", "kunwei.close"))

    def poll(self) -> tuple[tuple[float, float, float, float, float, float], float]:
        if not self.opened:
            raise RuntimeError("Fake Kunwei is not open")
        self.now_s += 0.008
        return self.wrench_n_nm, self.now_s


__all__ = [
    "FakeLiveKunweiTransport",
    "FakeLiveRTDETransport",
    "FakeRTDE",
    "FakeRegisterSnapshot",
]
