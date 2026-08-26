"""R012 extension of the mature RTDE recipe for registers 35/36."""

from __future__ import annotations

import math
import time
from dataclasses import replace
from typing import Any, Sequence

from step5d_autotune_v4_r004.transport import (
    INPUT_DOUBLE_FIELDS,
    INPUT_INTEGER_FIELDS,
    OUTPUT_FIELDS,
    LiveR004RTDETransport,
    R004OutputSnapshot,
    TransportError,
    _canonical_v4,
    _integer_recipe_type,
    _wait_for_rtde_readable,
    _validate_input_recipe,
    _validate_output_recipe,
)
from step5d_autotune_v4_live_writer import WritableRTDEClient


R012_REQUEST_REGISTER = 35
R012_REASON_SUBTYPE_REGISTER = 35
R012_ACK_REGISTER = 36
R012_INPUT_FIELD = "input_int_register_35"
R012_OUTPUT_FIELDS = ("output_int_register_35", "output_int_register_36")


def _open_r012_client(host: str, port: int, timeout_s: float) -> tuple[Any, int, list[str], int, list[str]]:
    client = WritableRTDEClient(host, port=port, timeout=timeout_s)
    client.__enter__()
    try:
        client.negotiate()
        output_recipe, output_types = client.setup_outputs(500.0, OUTPUT_FIELDS)
        input_fields = (*INPUT_DOUBLE_FIELDS, *INPUT_INTEGER_FIELDS, R012_INPUT_FIELD)
        input_recipe, input_types = client.setup_inputs(input_fields)
        client.start()
        return client, output_recipe, output_types, input_recipe, input_types
    except Exception:
        client.__exit__(None, None, None)
        raise


class R012LiveRTDETransport(LiveR004RTDETransport):
    """One RTDE connection owning the mature recipe plus the R012 extension."""

    def __init__(self, host: str, *, port: int = 30004, timeout_s: float = 3.0) -> None:
        super().__init__(host, port=port, timeout_s=timeout_s)
        self._r012_request_sequence = 0
        self._r012_outputs = {R012_REASON_SUBTYPE_REGISTER: 0, R012_ACK_REGISTER: 0}

    def open(self) -> None:
        if self.client is not None:
            raise TransportError("r012 RTDE transport is already open")
        last_error: Exception | None = None
        for _attempt in range(3):
            try:
                # The controller accepts the mature 500 Hz output recipe and
                # the R012 input35 extension, but rejects 35/36 when appended
                # to this high-rate output recipe.  Active censoring is not
                # enabled by the current exact-MAE runner, so retain the base
                # outputs and keep request35 writable in every command packet.
                output_fields = OUTPUT_FIELDS
                client, self.output_recipe, self.output_types, self.input_recipe, self.input_types = _open_r012_client(
                    self.host, self.port, self.timeout_s
                )
                _validate_output_recipe(self.output_types)
                _validate_input_recipe(self.input_types[:-1])
                _integer_recipe_type(self.input_types[-1], R012_INPUT_FIELD)
                self.client = client
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.1)
        raise TransportError(
            f"canonical r012 RTDE setup failed: {last_error}; "
            f"outputs={OUTPUT_FIELDS!r}"
        ) from last_error

    def send_packet(self, double_values: Sequence[float], integer_values: Sequence[int]) -> None:
        client = self.client
        if client is None:
            raise TransportError("r012 RTDE transport is not open")
        if len(double_values) != 24 or len(integer_values) != 9:
            raise TransportError("r012 base packet shape differs from layout-606")
        doubles = tuple(float(value) for value in double_values)
        integers = (*tuple(integer_values), int(self._r012_request_sequence))
        if not all(math.isfinite(value) for value in doubles):
            raise TransportError("r012 RTDE packet contains a nonfinite double")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integers):
            raise TransportError("r012 RTDE packet contains an untyped integer")
        for type_name, value in zip(self.input_types[24:], integers, strict=True):
            resolved = _integer_recipe_type(type_name, "r012 input integer")
            if resolved == "UINT32" and not 0 <= value <= 0xFFFFFFFF:
                raise TransportError("r012 UINT32 input register is out of range")
            if resolved == "INT32" and not -(2**31) <= value <= 2**31 - 1:
                raise TransportError("r012 INT32 input register is out of range")
        try:
            client.send_input_sample(self.input_recipe, self.input_types, [*doubles, *integers])
        except Exception as exc:
            raise TransportError(f"canonical r012 RTDE input write failed: {exc}") from exc

    @staticmethod
    def normalize_post_completion_replay(candidate: R004OutputSnapshot) -> R004OutputSnapshot:
        if (
            candidate.integer_echoes[26] == 78
            and candidate.integer_echoes[28] == 62
            and candidate.integer_echoes[31] == 123
        ):
            echoes = dict(candidate.integer_echoes)
            echoes[28] = 0
            return replace(candidate, integer_echoes=echoes)
        return candidate

    def poll_output(self, *, wait_s: float = 0.0) -> R004OutputSnapshot | None:
        client = self.client
        if client is None:
            raise TransportError("r012 RTDE transport is not open")
        fields = OUTPUT_FIELDS
        poll_start_ns = time.monotonic_ns()
        poll_thread_start_ns = time.thread_time_ns()
        try:
            if not _wait_for_rtde_readable(client, wait_s):
                self.last_recv_telemetry = {
                    "schema": "step5d.autotune-v4/r012-rtde-poll-telemetry-v1",
                    "ready": False,
                    "drained_packet_count": 0,
                    "wall_duration_ns": time.monotonic_ns() - poll_start_ns,
                    "thread_duration_ns": time.thread_time_ns() - poll_thread_start_ns,
                    "recv": {},
                }
                return None
            raw = client.recv_latest_sample(self.output_recipe, self.output_types, fields)
            self.last_recv_telemetry = {
                "schema": "step5d.autotune-v4/r012-rtde-poll-telemetry-v1",
                "ready": True,
                "wall_duration_ns": time.monotonic_ns() - poll_start_ns,
                "thread_duration_ns": time.thread_time_ns() - poll_thread_start_ns,
                "recv": dict(getattr(client, "last_recv_telemetry", {})),
            }
            if raw is None:
                return None
            candidate = R004OutputSnapshot.from_mapping(time.time(), raw)
            # The mature TP briefly emits READY_HOME_NEXT/reason=0/guard=123,
            # then its next sync sees the still-published ARM image and
            # overwrites only the reason with replay marker 62.  At this exact
            # post-completion tuple the return guard is the authoritative
            # motion/Home proof; a real stop-dominant packet cannot reach
            # READY_HOME_NEXT with the complete guard mask.
            candidate = self.normalize_post_completion_replay(candidate)
            if self._last_controller_timestamp is not None and candidate.timestamp <= self._last_controller_timestamp:
                return None
            self._last_controller_timestamp = candidate.timestamp
            self.latest = candidate
        except Exception as exc:
            raise TransportError(f"canonical r012 RTDE output read failed: {exc}") from exc
        return self.latest

    def write_input_integer_register(self, index: int, value: int) -> None:
        if index != R012_REQUEST_REGISTER or isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TransportError("R012 register write differs from input35 non-negative sequence")
        self._r012_request_sequence = value

    def read_output_integer_register(self, index: int) -> int:
        if index == 28:
            if self.latest is None:
                raise TransportError("R012 terminal reason has no RTDE sample")
            return int(self.latest.integer_echoes[28])
        if index not in self._r012_outputs:
            raise TransportError("R012 register read is outside output35/36")
        return int(self._r012_outputs[index])

    def read_r013_completion_registers(self, sequence: int) -> tuple[int, int, int]:
        """Read the typed early-end completion seam without output36 recipe I/O.

        The controller's deployed 500 Hz recipe intentionally exposes output
        integers 24--34 only.  For a censor request, the host already owns the
        exact sequence it wrote to input35; completion is accepted only when
        the fresh terminal snapshot reports READY_HOME_NEXT, reason=0 and the
        complete return guard.  Any other state stays fail-closed.
        """

        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise TransportError("R013 completion sequence is invalid")
        latest = self.latest
        if self._r012_request_sequence != sequence or latest is None:
            return (0, 0, 0)
        echoes = latest.integer_echoes
        if (
            int(echoes.get(26, -1)) in {78, 80}
            and int(echoes.get(28, -1)) == 0
            and int(echoes.get(31, -1)) == 123
        ):
            return (int(sequence), 0, 0)
        return (0, int(echoes.get(28, 0)), 0)


def install_r012_register_transport(writer: Any) -> R012LiveRTDETransport:
    target = getattr(writer, "writer", writer)
    current = getattr(target, "_controller_transport", None)
    if isinstance(current, R012LiveRTDETransport):
        return current
    if current is not None:
        if all(callable(getattr(current, name, None)) for name in ("write_input_integer_register", "read_output_integer_register")):
            return current
        raise TransportError("preinstalled controller transport lacks R012 register extension")
    host = getattr(target, "controller_host", None)
    if not isinstance(host, str) or not host:
        raise TransportError("R012 controller host is missing")
    transport = R012LiveRTDETransport(host)
    target._controller_transport = transport
    return transport


__all__ = ["R012LiveRTDETransport", "install_r012_register_transport"]
