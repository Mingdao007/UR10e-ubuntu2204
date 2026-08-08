"""Small r004 register-schema adapters over the canonical live transports.

The r004 layer owns only the layout-606 field order and the typed output
interpretation.  RTDE framing/recipe packing and Kunwei stream parsing remain
in the production V4/V3 transport modules.  This module has no Dashboard
path and never performs Load, Play, zero, tare, or sensor configuration.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Protocol, Sequence

from step5d_eoat_profiles import load_new_eoat_profile

from .contracts import PROGRAM, RUNTIME_PROTOCOL, R004Contract, runtime_identity_limbs
from .wire import (
    DOUBLE_REGISTERS,
    INPUT_INTEGER_REGISTERS,
    OUTPUT_DOUBLE_FIELDS,
    OUTPUT_INTEGER_FIELDS,
)


INPUT_DOUBLE_FIELDS = tuple(
    f"input_double_register_{register}" for register in DOUBLE_REGISTERS
)
INPUT_INTEGER_FIELDS = tuple(
    f"input_int_register_{register}" for register in INPUT_INTEGER_REGISTERS
)
OUTPUT_INTEGER_FIELDS_ORDERED = tuple(
    f"output_int_register_{register}" for register in OUTPUT_INTEGER_FIELDS
)
OUTPUT_DOUBLE_FIELDS_ORDERED = tuple(
    f"output_double_register_{register}" for register in OUTPUT_DOUBLE_FIELDS
)
OUTPUT_FIELDS = (
    "timestamp",
    "payload",
    "payload_cog",
    "tcp_offset",
    "actual_TCP_speed",
    "actual_TCP_pose",
    "actual_q",
    "actual_qd",
    "safety_mode",
    "robot_mode",
    "runtime_state",
    *OUTPUT_DOUBLE_FIELDS_ORDERED,
    *OUTPUT_INTEGER_FIELDS_ORDERED,
)


class TransportError(RuntimeError):
    """A canonical transport or r004 recipe failed closed."""


@dataclass(frozen=True)
class R004OutputSnapshot:
    observed_at_s: float
    timestamp: float
    payload_kg: float
    payload_cog_m: tuple[float, float, float]
    tcp_offset_m_rad: tuple[float, float, float, float, float, float]
    tcp_speed_m_s_rad_s: tuple[float, float, float, float, float, float]
    tcp_pose_m_rad: tuple[float, float, float, float, float, float]
    q_rad: tuple[float, float, float, float, float, float]
    qd_rad_s: tuple[float, float, float, float, float, float]
    safety_mode: Any
    robot_mode: Any
    runtime_state: Any
    consumed_packet_sequence: int
    integer_echoes: Mapping[int, int]

    @staticmethod
    def _vector(value: Any, size: int, role: str) -> tuple[float, ...]:
        if not isinstance(value, (list, tuple)) or len(value) != size:
            raise TransportError(f"RTDE {role} must have {size} values")
        result = tuple(float(item) for item in value)
        if not all(math.isfinite(item) for item in result):
            raise TransportError(f"RTDE {role} contains a nonfinite value")
        return result

    @classmethod
    def from_mapping(cls, observed_at_s: float, value: Mapping[str, Any]) -> "R004OutputSnapshot":
        observed = float(observed_at_s)
        if not math.isfinite(observed):
            raise TransportError("RTDE output observation time is nonfinite")
        try:
            echoes: dict[int, int] = {}
            for register in OUTPUT_INTEGER_FIELDS:
                raw = value[f"output_int_register_{register}"]
                if isinstance(raw, bool) or not isinstance(raw, int):
                    raise TransportError(f"RTDE output integer {register} is not typed")
                if raw < 0:
                    raise TransportError(f"RTDE output integer {register} is negative")
                echoes[register] = raw
            timestamp = float(value["timestamp"])
            payload = float(value["payload"])
            consumed_packet_sequence_raw = float(value["output_double_register_24"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise TransportError("RTDE r004 output omits a required field") from exc
        if (
            not math.isfinite(timestamp)
            or not math.isfinite(payload)
            or payload <= 0.0
            or not math.isfinite(consumed_packet_sequence_raw)
            or consumed_packet_sequence_raw < -1.0
            or not consumed_packet_sequence_raw.is_integer()
        ):
            raise TransportError("RTDE payload or sample timestamp is invalid")
        return cls(
            observed_at_s=observed,
            timestamp=timestamp,
            payload_kg=payload,
            payload_cog_m=cls._vector(value["payload_cog"], 3, "CoG"),
            tcp_offset_m_rad=cls._vector(value["tcp_offset"], 6, "TCP"),
            tcp_speed_m_s_rad_s=cls._vector(value["actual_TCP_speed"], 6, "TCP speed"),
            tcp_pose_m_rad=cls._vector(value["actual_TCP_pose"], 6, "TCP pose"),
            q_rad=cls._vector(value["actual_q"], 6, "q"),
            qd_rad_s=cls._vector(value["actual_qd"], 6, "qd"),
            safety_mode=value["safety_mode"],
            robot_mode=value["robot_mode"],
            runtime_state=value["runtime_state"],
            consumed_packet_sequence=int(consumed_packet_sequence_raw),
            integer_echoes=echoes,
        )

    @property
    def safety_normal(self) -> bool:
        return self.safety_mode == "NORMAL" or self.safety_mode == 1

    @property
    def stationary(self) -> bool:
        return (
            math.sqrt(sum(value * value for value in self.tcp_speed_m_s_rad_s[:3])) <= 0.0005
            and math.sqrt(sum(value * value for value in self.tcp_speed_m_s_rad_s[3:])) <= 0.005
        )

    @property
    def program_running(self) -> bool:
        return self.runtime_state == 2 or str(self.runtime_state).upper() in {
            "PLAYING",
            "RUNNING",
        }


class R004RTDETransport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def send_packet(self, double_values: Sequence[float], integer_values: Sequence[int]) -> None: ...
    def poll_output(self) -> R004OutputSnapshot | None: ...


class R004KunweiTransport(Protocol):
    distinct_frame_sequence: int
    latest_batch_count: int

    def open(self) -> None: ...
    def close(self) -> None: ...
    def poll(self) -> tuple[tuple[float, float, float, float, float, float] | None, float | None]: ...


def _canonical_v4() -> Any:
    """Load the production transport only when an explicit live route opens."""

    try:
        import step5d_autotune_v4_live_writer as module
    except Exception as exc:
        raise TransportError(f"canonical V4 transport is unavailable: {exc}") from exc
    return module


def _integer_recipe_type(type_name: object, role: str) -> str:
    if type_name not in {"INT32", "UINT32"}:
        raise TransportError(f"{role} must use INT32 or UINT32, got {type_name!r}")
    return str(type_name)


def _validate_input_recipe(type_names: Sequence[str]) -> None:
    if len(type_names) != len(INPUT_DOUBLE_FIELDS) + len(INPUT_INTEGER_FIELDS):
        raise TransportError("r004 input recipe field count differs from layout-606")
    if any(name != "DOUBLE" for name in type_names[: len(INPUT_DOUBLE_FIELDS)]):
        raise TransportError("r004 double input recipe is not DOUBLE-only")
    for index, name in enumerate(type_names[len(INPUT_DOUBLE_FIELDS) :], start=24):
        _integer_recipe_type(name, f"r004 input integer register {index}")


def _validate_output_recipe(type_names: Sequence[str]) -> None:
    if len(type_names) != len(OUTPUT_FIELDS):
        raise TransportError("r004 output recipe field count differs from layout-606")
    expected_prefix = (
        "DOUBLE",
        "DOUBLE",
        "VECTOR3D",
        "VECTOR6D",
        "VECTOR6D",
        "VECTOR6D",
        "VECTOR6D",
        "VECTOR6D",
    )
    if tuple(type_names[: len(expected_prefix)]) != expected_prefix:
        raise TransportError("r004 output pose/EOAT recipe types differ")
    for index, name in enumerate(type_names[8:11], start=8):
        _integer_recipe_type(name, f"r004 output runtime field {index}")
    if type_names[11] != "DOUBLE":
        raise TransportError("r004 consumed packet echo is not DOUBLE")
    for register, name in zip(OUTPUT_INTEGER_FIELDS, type_names[12:], strict=True):
        _integer_recipe_type(name, f"r004 output integer register {register}")


class LiveR004RTDETransport:
    """Canonical RTDE client plus the r004 layout-606 recipe checks."""

    def __init__(self, host: str, *, port: int = 30004, timeout_s: float = 3.0) -> None:
        if not isinstance(host, str) or not host:
            raise TransportError("r004 controller host must be explicit")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise TransportError("r004 RTDE port is invalid")
        if not math.isfinite(float(timeout_s)) or timeout_s <= 0.0:
            raise TransportError("r004 RTDE timeout is invalid")
        self.host = host
        self.port = port
        self.timeout_s = float(timeout_s)
        self.client: Any = None
        self.input_recipe = 0
        self.input_types: list[str] = []
        self.output_recipe = 0
        self.output_types: list[str] = []
        self.latest: R004OutputSnapshot | None = None
        self._last_controller_timestamp: float | None = None

    def open(self) -> None:
        if self.client is not None:
            raise TransportError("r004 RTDE transport is already open")
        canonical_v4 = _canonical_v4()
        client = canonical_v4.WritableRTDEClient(self.host, port=self.port, timeout=self.timeout_s)
        client.__enter__()
        try:
            client.negotiate()
            self.output_recipe, self.output_types = client.setup_outputs(500.0, OUTPUT_FIELDS)
            _validate_output_recipe(self.output_types)
            self.input_recipe, self.input_types = client.setup_inputs(
                (*INPUT_DOUBLE_FIELDS, *INPUT_INTEGER_FIELDS)
            )
            _validate_input_recipe(self.input_types)
            client.start()
        except Exception as exc:
            client.__exit__(None, None, None)
            raise TransportError(f"canonical r004 RTDE setup failed: {exc}") from exc
        self.client = client

    def close(self) -> None:
        client = self.client
        self.client = None
        if client is not None:
            client.__exit__(None, None, None)
        self.latest = None
        self._last_controller_timestamp = None

    def send_packet(self, double_values: Sequence[float], integer_values: Sequence[int]) -> None:
        client = self.client
        if client is None:
            raise TransportError("r004 RTDE transport is not open")
        if len(double_values) != 24 or len(integer_values) != 9:
            raise TransportError("r004 RTDE packet shape differs from layout-606")
        doubles = tuple(float(value) for value in double_values)
        if not all(math.isfinite(value) for value in doubles):
            raise TransportError("r004 RTDE packet contains a nonfinite double")
        integers = tuple(integer_values)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integers):
            raise TransportError("r004 RTDE packet contains an untyped integer")
        for offset, (type_name, value) in enumerate(
            zip(self.input_types[24:], integers, strict=True), start=24
        ):
            resolved = _integer_recipe_type(type_name, f"r004 input integer register {offset}")
            if resolved == "UINT32" and not 0 <= value <= 0xFFFFFFFF:
                raise TransportError(f"r004 UINT32 input register {offset} is out of range")
            if resolved == "INT32" and not -(2**31) <= value <= 2**31 - 1:
                raise TransportError(f"r004 INT32 input register {offset} is out of range")
        try:
            client.send_input_sample(
                self.input_recipe,
                self.input_types,
                [*doubles, *integers],
            )
        except Exception as exc:
            raise TransportError(f"canonical r004 RTDE input write failed: {exc}") from exc

    def poll_output(self) -> R004OutputSnapshot | None:
        client = self.client
        if client is None:
            raise TransportError("r004 RTDE transport is not open")
        try:
            raw = client.recv_latest_sample(self.output_recipe, self.output_types, OUTPUT_FIELDS)
            if raw is None:
                return None
            candidate = R004OutputSnapshot.from_mapping(time.time(), raw)
            if (
                self._last_controller_timestamp is not None
                and candidate.timestamp <= self._last_controller_timestamp
            ):
                return None
            self._last_controller_timestamp = candidate.timestamp
            self.latest = candidate
        except Exception as exc:
            raise TransportError(f"canonical r004 RTDE output read failed: {exc}") from exc
        return self.latest


@dataclass(frozen=True)
class KunweiSample:
    wrench_n_nm: tuple[float, float, float, float, float, float]
    observed_at_s: float


class LiveR004KunweiTransport:
    """Canonical Kunwei stream with an SI wrench view for r004."""

    def __init__(self, host: str, *, port: int, timeout_s: float = 3.0) -> None:
        if not isinstance(host, str) or not host:
            raise TransportError("r004 Kunwei host must be explicit")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise TransportError("r004 Kunwei port is invalid")
        if not math.isfinite(float(timeout_s)) or timeout_s <= 0.0:
            raise TransportError("r004 Kunwei timeout is invalid")
        self.host = host
        self.port = port
        self.timeout_s = float(timeout_s)
        self._delegate: Any = None
        self.latest: KunweiSample | None = None
        # The canonical socket parser can return many physical sensor frames in
        # one nonblocking TCP poll.  Keep the real frame count instead of
        # collapsing a batch into one host-loop observation.
        self.distinct_frame_sequence = 0
        self.latest_batch_count = 0

    def open(self) -> None:
        if self._delegate is None:
            canonical_v4 = _canonical_v4()
            self._delegate = canonical_v4.LiveKunweiTransport(
                SimpleNamespace(sensor_host=self.host, sensor_port=self.port)
            )
        try:
            self._delegate.open()
        except Exception as exc:
            raise TransportError(f"canonical Kunwei START_STREAM failed: {exc}") from exc

    def close(self) -> None:
        if self._delegate is None:
            return
        try:
            self._delegate.close()
        except Exception as exc:
            raise TransportError(f"canonical Kunwei STOP_STREAM failed: {exc}") from exc

    def poll(self) -> tuple[tuple[float, float, float, float, float, float] | None, float | None]:
        if self._delegate is None:
            raise TransportError("r004 Kunwei transport is not open")
        try:
            frames, count = self._delegate.poll()
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise TransportError("canonical Kunwei frame count is invalid")
            if count != len(frames):
                raise TransportError("canonical Kunwei frame count disagrees with parsed frames")
            self.latest_batch_count = count
            if frames:
                self.distinct_frame_sequence += count
                canonical_v4 = _canonical_v4()
                converted = canonical_v4.zeroed_wrench(frames[-1], (0.0,) * 6)
                observed = self._delegate.latest_mono
                if observed is None or not math.isfinite(float(observed)):
                    raise TransportError("canonical Kunwei sample timestamp is invalid")
                self.latest = KunweiSample(converted, float(observed))
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError(f"canonical Kunwei sample read failed: {exc}") from exc
        if self.latest is None:
            return None, None
        return self.latest.wrench_n_nm, self.latest.observed_at_s


def expected_runtime_identity(contract: R004Contract) -> tuple[int, int, int]:
    hi, lo = runtime_identity_limbs(PROGRAM, contract.sha256, contract.campaign_fingerprint)
    return RUNTIME_PROTOCOL, hi, lo


def expected_eoat() -> tuple[float, tuple[float, float, float], tuple[float, float, float, float, float, float]]:
    profile = load_new_eoat_profile()
    return profile.payload_kg, profile.cog_m, profile.controller_tcp_m_rad


__all__ = [
    "INPUT_DOUBLE_FIELDS",
    "INPUT_INTEGER_FIELDS",
    "OUTPUT_FIELDS",
    "R004KunweiTransport",
    "R004OutputSnapshot",
    "R004RTDETransport",
    "LiveR004KunweiTransport",
    "LiveR004RTDETransport",
    "KunweiSample",
    "TransportError",
    "expected_eoat",
    "expected_runtime_identity",
]
