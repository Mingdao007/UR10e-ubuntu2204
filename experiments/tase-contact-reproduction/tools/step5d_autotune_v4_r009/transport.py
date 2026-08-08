"""R009 extended output recipe and fail-closed transport parser.

This is an offline schema/parser layer only.  It intentionally has no socket
client and does not alter the r004 layout-606 constants.  R009 owns output
double registers 24--27 and output integer register 35; output integer 24--34
retain the mature state/reason/runtime identity echoes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from .freshness import R009FreshnessError, Reason43Subtype, validate_reason43_diagnostic
from .identity import R009_RUNTIME_PROTOCOL


R009_OUTPUT_RECIPE_SCHEMA = "step5d.autotune-v4/r009-output-recipe-v1"
OUTPUT_DOUBLE_FIELDS: Mapping[int, str] = {
    24: "consumed_or_cached_packet_sequence",
    25: "observed_packet_sequence",
    26: "cached_packet_sequence",
    27: "cache_age_seconds",
}
OUTPUT_INTEGER_FIELDS: Mapping[int, str] = {
    24: "epoch",
    25: "ordinal",
    26: "state",
    27: "token",
    28: "reason",
    29: "consumed_session_command_sequence",
    30: "attempt_kind",
    31: "return_guard",
    32: "runtime_protocol",
    33: "runtime_digest_hi",
    34: "runtime_digest_lo",
    35: "reason43_subtype",
}
OUTPUT_DOUBLE_REGISTERS = tuple(OUTPUT_DOUBLE_FIELDS)
OUTPUT_INTEGER_REGISTERS = tuple(OUTPUT_INTEGER_FIELDS)
OUTPUT_DOUBLE_FIELDS_ORDERED = tuple(
    f"output_double_register_{register}" for register in OUTPUT_DOUBLE_REGISTERS
)
OUTPUT_INTEGER_FIELDS_ORDERED = tuple(
    f"output_int_register_{register}" for register in OUTPUT_INTEGER_REGISTERS
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


class R009TransportError(RuntimeError):
    """An R009 recipe, output, or cross-field binding failed closed."""


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R009TransportError(f"RTDE {role} must be finite")
    result = float(value)
    if not math.isfinite(result):
        raise R009TransportError(f"RTDE {role} must be finite")
    return result


def _integer_like(value: Any, role: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R009TransportError(f"RTDE {role} must be finite and integer-like")
    result = float(value)
    if not math.isfinite(result) or not result.is_integer() or int(result) < minimum:
        raise R009TransportError(f"RTDE {role} must be finite and integer-like")
    return int(result)


def _vector(value: Any, size: int, role: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise R009TransportError(f"RTDE {role} must have {size} values")
    result = tuple(_finite(item, f"{role}[{index}]") for index, item in enumerate(value))
    return result


def validate_output_recipe(type_names: Sequence[str]) -> None:
    """Validate the exact R009 output field ownership and primitive types."""

    if len(type_names) != len(OUTPUT_FIELDS):
        raise R009TransportError("R009 output recipe field count differs")
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
        raise R009TransportError("R009 output pose/EOAT recipe types differ")
    for index, name in enumerate(type_names[8:11], start=8):
        if name not in {"INT32", "UINT32"}:
            raise R009TransportError(f"R009 output runtime field {index} is not integer")
    double_start = 11
    double_end = double_start + len(OUTPUT_DOUBLE_FIELDS)
    if any(name != "DOUBLE" for name in type_names[double_start:double_end]):
        raise R009TransportError("R009 diagnostic output doubles must be DOUBLE")
    for register, name in zip(
        OUTPUT_INTEGER_FIELDS,
        type_names[double_end:],
        strict=True,
    ):
        if name not in {"INT32", "UINT32"}:
            raise R009TransportError(
                f"R009 output integer register {register} is not INT32/UINT32"
            )


@dataclass(frozen=True)
class R009OutputSnapshot:
    """Typed output row including all reason-43 diagnostics."""

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
    observed_packet_sequence: int
    cached_packet_sequence: int
    cache_age_s: float
    integer_echoes: Mapping[int, int]

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.observed_at_s)):
            raise R009TransportError("R009 output observation time is nonfinite")
        if not math.isfinite(float(self.timestamp)):
            raise R009TransportError("R009 controller timestamp is nonfinite")
        if not math.isfinite(float(self.payload_kg)) or self.payload_kg <= 0.0:
            raise R009TransportError("R009 payload is invalid")
        if not math.isfinite(float(self.cache_age_s)) or self.cache_age_s < 0.0:
            raise R009TransportError("R009 cache age is invalid")
        object.__setattr__(self, "integer_echoes", MappingProxyType(dict(self.integer_echoes)))

    @classmethod
    def from_mapping(
        cls, observed_at_s: Any, value: Mapping[str, Any]
    ) -> "R009OutputSnapshot":
        if not isinstance(value, Mapping):
            raise R009TransportError("R009 output must be a mapping")
        try:
            observed = _finite(observed_at_s, "output observation time")
            integer_echoes: dict[int, int] = {}
            for register in OUTPUT_INTEGER_FIELDS:
                raw = value[f"output_int_register_{register}"]
                if isinstance(raw, bool) or not isinstance(raw, int):
                    raise R009TransportError(
                        f"R009 output integer {register} is not typed"
                    )
                if raw < 0:
                    raise R009TransportError(
                        f"R009 output integer {register} is negative"
                    )
                integer_echoes[register] = raw
            timestamp = _finite(value["timestamp"], "controller timestamp")
            payload = _finite(value["payload"], "payload")
            consumed = _integer_like(
                value["output_double_register_24"],
                "consumed/cached packet sequence",
                minimum=-1,
            )
            observed_sequence = _integer_like(
                value["output_double_register_25"],
                "observed packet sequence",
                minimum=-1,
            )
            cached_sequence = _integer_like(
                value["output_double_register_26"],
                "cached packet sequence",
                minimum=-1,
            )
            age = _finite(value["output_double_register_27"], "cache age")
        except R009TransportError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise R009TransportError("R009 output omits a required field") from exc

        if payload <= 0.0:
            raise R009TransportError("R009 payload is invalid")
        if integer_echoes[32] != R009_RUNTIME_PROTOCOL:
            raise R009TransportError("R009 runtime protocol echo differs")
        try:
            validate_reason43_diagnostic(
                terminal_reason=integer_echoes[28],
                tp_state=integer_echoes[26],
                reason43_subtype=integer_echoes[35],
                observed_packet_sequence=observed_sequence,
                cached_packet_sequence=cached_sequence,
                consumed_packet_sequence=consumed,
                cache_age_s=age,
            )
        except R009FreshnessError as exc:
            raise R009TransportError(str(exc)) from exc

        return cls(
            observed_at_s=observed,
            timestamp=timestamp,
            payload_kg=payload,
            payload_cog_m=_vector(value["payload_cog"], 3, "CoG"),
            tcp_offset_m_rad=_vector(value["tcp_offset"], 6, "TCP"),
            tcp_speed_m_s_rad_s=_vector(value["actual_TCP_speed"], 6, "TCP speed"),
            tcp_pose_m_rad=_vector(value["actual_TCP_pose"], 6, "TCP pose"),
            q_rad=_vector(value["actual_q"], 6, "q"),
            qd_rad_s=_vector(value["actual_qd"], 6, "qd"),
            safety_mode=value["safety_mode"],
            robot_mode=value["robot_mode"],
            runtime_state=value["runtime_state"],
            consumed_packet_sequence=consumed,
            observed_packet_sequence=observed_sequence,
            cached_packet_sequence=cached_sequence,
            cache_age_s=age,
            integer_echoes=integer_echoes,
        )

    @property
    def state(self) -> int:
        return self.integer_echoes[26]

    @property
    def terminal_reason(self) -> int:
        return self.integer_echoes[28]

    @property
    def reason(self) -> int:
        return self.terminal_reason

    @property
    def reason43_subtype(self) -> Reason43Subtype:
        return Reason43Subtype(self.integer_echoes[35])

    @property
    def stopped(self) -> bool:
        return self.state == 90

    @property
    def accepted(self) -> bool:
        return not self.stopped and self.terminal_reason == 0

    @property
    def safety_normal(self) -> bool:
        return self.safety_mode == "NORMAL" or self.safety_mode == 1

    @property
    def stationary(self) -> bool:
        return (
            math.sqrt(sum(value * value for value in self.tcp_speed_m_s_rad_s[:3]))
            <= 0.0005
            and math.sqrt(sum(value * value for value in self.tcp_speed_m_s_rad_s[3:]))
            <= 0.005
        )

    @property
    def program_running(self) -> bool:
        return self.runtime_state == 2 or str(self.runtime_state).upper() in {
            "PLAYING",
            "RUNNING",
        }

    def diagnostic_fields(self) -> dict[str, Any]:
        return {
            "consumed_packet_sequence": self.consumed_packet_sequence,
            "observed_packet_sequence": self.observed_packet_sequence,
            "cached_packet_sequence": self.cached_packet_sequence,
            "cache_age_s": self.cache_age_s,
            "reason43_subtype": int(self.reason43_subtype),
            "state": self.state,
            "reason": self.terminal_reason,
            "runtime_protocol": self.integer_echoes[32],
        }


def parse_output(value: Mapping[str, Any], *, observed_at_s: Any = 0.0) -> R009OutputSnapshot:
    return R009OutputSnapshot.from_mapping(observed_at_s, value)


class R009RTDETransport(Protocol):
    """Offline protocol shape; implementations remain outside this scope."""

    def poll_output(self) -> R009OutputSnapshot | None: ...


R009_OUTPUT_DOUBLE_FIELDS = OUTPUT_DOUBLE_FIELDS
R009_OUTPUT_INTEGER_FIELDS = OUTPUT_INTEGER_FIELDS
R009_OUTPUT_FIELDS = OUTPUT_FIELDS
R009_OUTPUT_DOUBLE_REGISTERS = OUTPUT_DOUBLE_REGISTERS
R009_OUTPUT_INTEGER_REGISTERS = OUTPUT_INTEGER_REGISTERS
parse_r009_output = parse_output


__all__ = [
    "OUTPUT_DOUBLE_FIELDS",
    "OUTPUT_DOUBLE_FIELDS_ORDERED",
    "OUTPUT_DOUBLE_REGISTERS",
    "OUTPUT_FIELDS",
    "OUTPUT_INTEGER_FIELDS",
    "OUTPUT_INTEGER_FIELDS_ORDERED",
    "OUTPUT_INTEGER_REGISTERS",
    "R009_OUTPUT_RECIPE_SCHEMA",
    "R009_OUTPUT_DOUBLE_FIELDS",
    "R009_OUTPUT_INTEGER_FIELDS",
    "R009_OUTPUT_FIELDS",
    "R009_OUTPUT_DOUBLE_REGISTERS",
    "R009_OUTPUT_INTEGER_REGISTERS",
    "R009OutputSnapshot",
    "R009RTDETransport",
    "R009TransportError",
    "parse_output",
    "parse_r009_output",
    "validate_output_recipe",
]
