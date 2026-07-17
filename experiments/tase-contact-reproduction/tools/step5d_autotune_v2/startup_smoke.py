"""No-motion RTDE startup smoke shared by URSim and target-controller gates."""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable

from step5d_autotune_live_driver import TpFeedbackDecoder, TpFeedbackPhase
from step5d_autotune_state_machine import TpLoopState


OUTPUT_FIELDS = ["runtime_state", *(f"output_int_register_{index}" for index in range(24, 31))]
INPUT_FIELDS = [*(f"input_int_register_{index}" for index in range(24, 30)), "input_double_register_26"]
TYPE_FORMATS = {"UINT32": "I", "INT32": "i", "DOUBLE": "d", "BOOL": "?"}


class StartupSmokeError(RuntimeError):
    pass


@dataclass(frozen=True)
class StartupSmokeResult:
    phases: tuple[str, ...]
    startup_duration_s: float
    first_active_state: int
    consumed_command_seq: int
    samples: int


class RtdeHoldSession:
    """Minimal RTDE client that writes heartbeat plus an all-zero HOLD command."""

    def __init__(self, host: str, *, port: int = 30004, timeout_s: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self.socket: socket.socket | None = None
        self.output_recipe = 0
        self.output_types: list[str] = []
        self.input_recipe = 0
        self.input_types: list[str] = []

    def __enter__(self) -> "RtdeHoldSession":
        try:
            self.socket = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
            self.socket.settimeout(self.timeout_s)
            self._send("V", struct.pack("!H", 2))
            kind, payload = self._receive()
            if kind != ord("V") or payload != b"\x01":
                raise StartupSmokeError("RTDE protocol v2 negotiation failed")
            self.output_recipe, self.output_types = self._setup_output(125.0, OUTPUT_FIELDS)
            self.input_recipe, self.input_types = self._setup_input(INPUT_FIELDS)
            self._send("S")
            kind, payload = self._receive()
            if kind != ord("S") or payload != b"\x01":
                raise StartupSmokeError("RTDE synchronization start failed")
        except Exception:
            if self.socket is not None:
                self.socket.close()
                self.socket = None
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.socket is not None:
            try:
                self._send("P")
            except OSError:
                pass
            self.socket.close()
            self.socket = None

    def _read_exact(self, size: int) -> bytes:
        if self.socket is None:
            raise StartupSmokeError("RTDE socket is closed")
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.socket.recv(size - len(chunks))
            if not chunk:
                raise StartupSmokeError("RTDE socket closed during frame")
            chunks.extend(chunk)
        return bytes(chunks)

    def _receive(self) -> tuple[int, bytes]:
        size, kind = struct.unpack("!HB", self._read_exact(3))
        return kind, self._read_exact(size - 3) if size > 3 else b""

    def _send(self, kind: str, payload: bytes = b"") -> None:
        if self.socket is None:
            raise StartupSmokeError("RTDE socket is closed")
        self.socket.sendall(struct.pack("!HB", len(payload) + 3, ord(kind)) + payload)

    def _setup_output(self, frequency_hz: float, fields: list[str]) -> tuple[int, list[str]]:
        self._send("O", struct.pack("!d", frequency_hz) + ",".join(fields).encode("ascii"))
        kind, payload = self._receive_recipe("output", ord("O"))
        return self._parse_recipe("output", kind, ord("O"), payload)

    def _setup_input(self, fields: list[str]) -> tuple[int, list[str]]:
        self._send("I", ",".join(fields).encode("ascii"))
        kind, payload = self._receive_recipe("input", ord("I"))
        return self._parse_recipe("input", kind, ord("I"), payload)

    def _receive_recipe(self, label: str, expected: int) -> tuple[int, bytes]:
        messages: list[bytes] = []
        while True:
            try:
                kind, payload = self._receive()
            except socket.timeout as exc:
                raise StartupSmokeError(
                    f"RTDE {label} recipe timed out after text messages: {messages!r}"
                ) from exc
            if kind == expected:
                return kind, payload
            if kind == ord("M"):
                messages.append(payload)
                continue
            raise StartupSmokeError(
                f"unexpected RTDE {label} recipe response: "
                f"kind={kind} expected={expected} payload={payload!r}"
            )

    @staticmethod
    def _parse_recipe(label: str, kind: int, expected: int, payload: bytes) -> tuple[int, list[str]]:
        if kind != expected or not payload:
            raise StartupSmokeError(
                f"unexpected RTDE {label} recipe response: "
                f"kind={kind} expected={expected} payload={payload!r}"
            )
        recipe = payload[0]
        types = payload[1:].decode("ascii").split(",")
        if recipe == 0 or len(types) == 0 or any(value == "NOT_FOUND" for value in types):
            raise StartupSmokeError(f"invalid RTDE {label} recipe: {types}")
        return recipe, types

    def send_hold(self, heartbeat: float) -> None:
        values: list[int | float] = [0, 0, 0, 0, 0, 0, heartbeat]
        if len(self.input_types) != len(values):
            raise StartupSmokeError("RTDE HOLD input recipe width differs")
        payload = bytearray([self.input_recipe])
        for type_name, value in zip(self.input_types, values):
            try:
                payload.extend(struct.pack("!" + TYPE_FORMATS[type_name], value))
            except KeyError as exc:
                raise StartupSmokeError(f"unsupported RTDE input type {type_name}") from exc
        self._send("U", bytes(payload))

    def receive_output(self) -> dict[str, Any]:
        while True:
            kind, payload = self._receive()
            if kind != ord("U") or not payload or payload[0] != self.output_recipe:
                continue
            cursor = 1
            values: list[Any] = []
            for type_name in self.output_types:
                try:
                    format_name = TYPE_FORMATS[type_name]
                except KeyError as exc:
                    raise StartupSmokeError(f"unsupported RTDE output type {type_name}") from exc
                width = struct.calcsize("!" + format_name)
                values.append(struct.unpack("!" + format_name, payload[cursor : cursor + width])[0])
                cursor += width
            return dict(zip(OUTPUT_FIELDS, values))


def startup_baseline_phase(
    output: dict[str, Any],
    observation: Any,
    *,
    allow_latched_ready: bool,
) -> str:
    if output.get("runtime_state") != 1:
        raise StartupSmokeError("startup baseline must be STOPPED")
    if observation.phase is not TpFeedbackPhase.PREPLAY:
        raise StartupSmokeError("startup decoder did not establish PREPLAY")
    if observation.baseline_kind == "cold_zero":
        return observation.phase.value
    if allow_latched_ready and observation.baseline_kind == "latched_ready":
        return "preplay_ready_latched"
    raise StartupSmokeError(
        "startup baseline must be cold-zero or zero-identity latched READY_HOME"
    )


def verify_play_startup(
    session: RtdeHoldSession,
    *,
    trigger_play: Callable[[], None] | None,
    timeout_s: float,
    allow_latched_ready_baseline: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> StartupSmokeResult:
    decoder = TpFeedbackDecoder()
    started_at = clock()
    play_at: float | None = None
    phases: list[str] = []
    samples = 0
    heartbeat = 1.0
    while clock() - started_at < timeout_s:
        heartbeat += 1.0
        session.send_hold(heartbeat)
        output = session.receive_output()
        observed_at = clock()
        observation = decoder.observe(output, observed_at_s=observed_at)
        samples += 1
        if play_at is None:
            phase = startup_baseline_phase(
                output,
                observation,
                allow_latched_ready=allow_latched_ready_baseline,
            )
            phases.append(phase)
            play_at = observed_at
            if trigger_play is not None:
                trigger_play()
            continue
        if observation.phase is TpFeedbackPhase.PREPLAY:
            continue
        if not phases or phases[-1] != observation.phase.value:
            phases.append(observation.phase.value)
        if observation.phase is TpFeedbackPhase.ACTIVE:
            if output.get("runtime_state") != 2:
                continue
            packet = observation.packet
            if packet is None or packet.state is not TpLoopState.READY_HOME:
                raise StartupSmokeError("first active TP state is not READY_HOME")
            if packet.consumed_command_seq != 0:
                raise StartupSmokeError("TP consumed a command during HOLD-only startup smoke")
            return StartupSmokeResult(
                tuple(phases),
                observed_at - play_at,
                int(packet.state),
                packet.consumed_command_seq,
                samples,
            )
    raise StartupSmokeError("TP startup smoke timed out before READY_HOME")
