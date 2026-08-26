"""Offline-testable V5 RTDE recipe and register transport adapter.

The adapter reuses the mature single-connection R004 transport seams while
owning only layout-607 input integers and V5 output interpretation.  It does
not create a second connection, request output registers 35..39, or perform
controller, motion, or sensor operations.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import time
from typing import Any, Mapping, Sequence

try:  # Tests put ``tools`` on sys.path; the fallback supports repo-root imports.
    from step5d_autotune_v4_r004.transport import (
        INPUT_DOUBLE_FIELDS,
        OUTPUT_FIELDS,
        R004OutputSnapshot,
        LiveR004RTDETransport,
        TransportError,
        _canonical_v4,
        _integer_recipe_type,
        _validate_output_recipe,
        _wait_for_rtde_readable,
    )
    from step6_figure8_autotune_v1.v5_composition_contract import (
        AutotunerV5CompositionContractV2,
        INPUT_DOUBLE_REGISTERS,
        INPUT_INTEGER_REGISTERS,
        OUTPUT_DOUBLE_REGISTERS,
        OUTPUT_INTEGER_REGISTERS,
        OutputOverlayV2,
        V5AttemptKind,
        V5RolloverInput,
        V5TPState,
        decode_output_overlay,
    )
    from step5d_autotune_v4_r004.wire import AttemptKind
except ModuleNotFoundError:  # pragma: no cover - package import from repository root
    from tools.step5d_autotune_v4_r004.transport import (
        INPUT_DOUBLE_FIELDS,
        OUTPUT_FIELDS,
        R004OutputSnapshot,
        LiveR004RTDETransport,
        TransportError,
        _canonical_v4,
        _integer_recipe_type,
        _validate_output_recipe,
        _wait_for_rtde_readable,
    )
    from tools.step6_figure8_autotune_v1.v5_composition_contract import (
        AutotunerV5CompositionContractV2,
        INPUT_DOUBLE_REGISTERS,
        INPUT_INTEGER_REGISTERS,
        OUTPUT_DOUBLE_REGISTERS,
        OUTPUT_INTEGER_REGISTERS,
        OutputOverlayV2,
        V5AttemptKind,
        V5RolloverInput,
        V5TPState,
        decode_output_overlay,
    )
    from tools.step5d_autotune_v4_r004.wire import AttemptKind


V5_REGISTER_TRANSPORT_SCHEMA = "step6.autotune/figure8-v5-register-transport-v1"
V5_REGISTER_TRANSPORT_VERSION = 1
V5_SETUP_RATE_HZ = 500.0
V5_INPUT_INTEGER_FIELDS = tuple(
    f"input_int_register_{register}" for register in INPUT_INTEGER_REGISTERS
)
V5_INPUT_FIELDS = (*INPUT_DOUBLE_FIELDS, *V5_INPUT_INTEGER_FIELDS)


def _validate_v5_input_recipe(type_names: Sequence[str]) -> None:
    if len(type_names) != len(V5_INPUT_FIELDS):
        raise TransportError("V5 input recipe field count differs from layout-607")
    if tuple(type_names[: len(INPUT_DOUBLE_FIELDS)]) != ("DOUBLE",) * len(INPUT_DOUBLE_FIELDS):
        raise TransportError("V5 double input recipe is not DOUBLE-only")
    for register, type_name in zip(
        INPUT_INTEGER_REGISTERS,
        type_names[len(INPUT_DOUBLE_FIELDS) :],
        strict=True,
    ):
        _integer_recipe_type(type_name, f"V5 input integer register {register}")


@dataclass(frozen=True)
class V5RecipeContractReceiptV1:
    """A no-motion recipe contract and offline falsifier input."""

    schema: str = V5_REGISTER_TRANSPORT_SCHEMA
    version: int = V5_REGISTER_TRANSPORT_VERSION
    layout_tag: float = 607.0
    setup_rate_hz: float = V5_SETUP_RATE_HZ
    input_double_fields: tuple[str, ...] = INPUT_DOUBLE_FIELDS
    input_integer_fields: tuple[str, ...] = V5_INPUT_INTEGER_FIELDS
    output_fields: tuple[str, ...] = OUTPUT_FIELDS
    input_double_registers: tuple[int, ...] = INPUT_DOUBLE_REGISTERS
    input_integer_registers: tuple[int, ...] = INPUT_INTEGER_REGISTERS
    output_double_registers: tuple[int, ...] = OUTPUT_DOUBLE_REGISTERS
    output_integer_registers: tuple[int, ...] = OUTPUT_INTEGER_REGISTERS
    rtde_connection_count: int = 1
    single_rtde_connection: bool = True
    no_motion: bool = True
    controller_program_state_change: bool = False
    sensor_reader: bool = False
    output_35_to_39_requested: bool = False
    live_acceptance_claim: bool = False
    purpose: str = "contract_falsifier_input"

    def __post_init__(self) -> None:
        contract = AutotunerV5CompositionContractV2()
        contract.assert_register_recipes(
            input_double_registers=self.input_double_registers,
            input_integer_registers=self.input_integer_registers,
            output_double_registers=self.output_double_registers,
            output_integer_registers=self.output_integer_registers,
        )
        if self.schema != V5_REGISTER_TRANSPORT_SCHEMA or self.version != V5_REGISTER_TRANSPORT_VERSION:
            raise TransportError("V5 recipe receipt schema/version differs")
        if self.layout_tag != 607.0 or self.setup_rate_hz != V5_SETUP_RATE_HZ:
            raise TransportError("V5 recipe layout or rate differs")
        if self.input_double_fields != INPUT_DOUBLE_FIELDS:
            raise TransportError("V5 input double field order differs")
        if self.input_integer_fields != V5_INPUT_INTEGER_FIELDS:
            raise TransportError("V5 input integer field order differs")
        if self.output_fields != OUTPUT_FIELDS:
            raise TransportError("V5 output field order differs from mature output recipe")
        if self.rtde_connection_count != 1 or self.single_rtde_connection is not True:
            raise TransportError("V5 recipe requires one RTDE connection")
        if self.no_motion is not True or self.controller_program_state_change is not False:
            raise TransportError("V5 recipe receipt is not no-motion")
        if self.sensor_reader is not False or self.output_35_to_39_requested is not False:
            raise TransportError("V5 recipe receipt requests a forbidden reader or output range")
        if self.live_acceptance_claim is not False or self.purpose != "contract_falsifier_input":
            raise TransportError("V5 recipe receipt is a live acceptance claim")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "layout_tag": self.layout_tag,
            "setup_rate_hz": self.setup_rate_hz,
            "input_recipe": {
                "double_fields": list(self.input_double_fields),
                "double_registers": list(self.input_double_registers),
                "integer_fields": list(self.input_integer_fields),
                "integer_registers": list(self.input_integer_registers),
            },
            "output_recipe": {
                "fields": list(self.output_fields),
                "double_registers": list(self.output_double_registers),
                "integer_registers": list(self.output_integer_registers),
            },
            "rtde_connection_count": self.rtde_connection_count,
            "single_rtde_connection": self.single_rtde_connection,
            "no_motion": self.no_motion,
            "controller_program_state_change": self.controller_program_state_change,
            "sensor_reader": self.sensor_reader,
            "output_35_to_39_requested": self.output_35_to_39_requested,
            "live_acceptance_claim": self.live_acceptance_claim,
            "purpose": self.purpose,
        }


def v5_recipe_contract_receipt() -> V5RecipeContractReceiptV1:
    return V5RecipeContractReceiptV1()


@dataclass(frozen=True)
class V5OutputSnapshotV1:
    r004_snapshot: R004OutputSnapshot
    state: V5TPState
    overlay: OutputOverlayV2
    schema: str = V5_REGISTER_TRANSPORT_SCHEMA
    version: int = V5_REGISTER_TRANSPORT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.r004_snapshot, R004OutputSnapshot):
            raise TypeError("V5 output view requires the mature R004OutputSnapshot")
        if not isinstance(self.state, V5TPState):
            raise TypeError("V5 output state must be typed")
        if self.schema != V5_REGISTER_TRANSPORT_SCHEMA or self.version != V5_REGISTER_TRANSPORT_VERSION:
            raise TransportError("V5 output view schema/version differs")

    @property
    def timestamp(self) -> float:
        return self.r004_snapshot.timestamp

    @property
    def integer_echoes(self) -> Mapping[int, int]:
        return self.r004_snapshot.integer_echoes

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "timestamp": self.timestamp,
            "state": int(self.state),
            "overlay": dict(self.overlay.by_register),
        }


def _decode_v5_output(snapshot: R004OutputSnapshot) -> V5OutputSnapshotV1:
    try:
        state = V5TPState(snapshot.integer_echoes[26])
    except (KeyError, TypeError, ValueError) as exc:
        raise TransportError("V5 output26 is not a known V5TPState") from exc
    try:
        overlay = decode_output_overlay(state, snapshot.integer_echoes)
    except (TypeError, ValueError, KeyError) as exc:
        raise TransportError("V5 output29..31 overlay is invalid for output26 state") from exc
    return V5OutputSnapshotV1(r004_snapshot=snapshot, state=state, overlay=overlay)


class LiveV5RTDETransport(LiveR004RTDETransport):
    """One writable RTDE connection using the V5 layout-607 recipe."""

    def __init__(self, host: str, *, port: int = 30004, timeout_s: float = 3.0) -> None:
        super().__init__(host, port=port, timeout_s=timeout_s)
        self.recipe_contract = v5_recipe_contract_receipt()
        self.latest_v5: V5OutputSnapshotV1 | None = None

    def open(self) -> None:
        if self.client is not None:
            raise TransportError("V5 RTDE transport is already open")
        canonical_v4 = _canonical_v4()
        client = canonical_v4.WritableRTDEClient(self.host, port=self.port, timeout=self.timeout_s)
        client.__enter__()
        try:
            client.negotiate()
            self.output_recipe, self.output_types = client.setup_outputs(
                V5_SETUP_RATE_HZ,
                OUTPUT_FIELDS,
            )
            _validate_output_recipe(self.output_types)
            self.input_recipe, self.input_types = client.setup_inputs(V5_INPUT_FIELDS)
            _validate_v5_input_recipe(self.input_types)
            client.start()
        except Exception as exc:
            client.__exit__(None, None, None)
            raise TransportError(f"canonical V5 RTDE setup failed: {exc}") from exc
        self.client = client

    def close(self) -> None:
        super().close()
        self.latest_v5 = None

    def send_packet(self, double_values: Sequence[float], integer_values: Sequence[int]) -> None:
        client = self.client
        if client is None:
            raise TransportError("V5 RTDE transport is not open")
        if (
            isinstance(double_values, (str, bytes))
            or isinstance(integer_values, (str, bytes))
            or len(double_values) != len(INPUT_DOUBLE_REGISTERS)
            or len(integer_values) != len(INPUT_INTEGER_REGISTERS)
        ):
            raise TransportError("V5 RTDE packet shape differs from layout-607")
        doubles: tuple[float, ...] = tuple(
            float(value) if not isinstance(value, bool) else math.nan for value in double_values
        )
        if not all(math.isfinite(value) for value in doubles):
            raise TransportError("V5 RTDE packet contains a nonfinite double")
        integers = tuple(integer_values)
        if any(type(value) is not int for value in integers):
            raise TransportError("V5 RTDE packet contains an untyped integer")
        for register, type_name, value in zip(
            INPUT_INTEGER_REGISTERS,
            self.input_types[len(INPUT_DOUBLE_FIELDS) :],
            integers,
            strict=True,
        ):
            resolved = _integer_recipe_type(type_name, f"V5 input integer register {register}")
            if resolved == "UINT32" and not 0 <= value <= 0xFFFFFFFF:
                raise TransportError(f"V5 UINT32 input register {register} is out of range")
            if resolved == "INT32" and not -(2**31) <= value <= 2**31 - 1:
                raise TransportError(f"V5 INT32 input register {register} is out of range")
        try:
            client.send_input_sample(
                self.input_recipe,
                self.input_types,
                [*doubles, *integers],
            )
        except Exception as exc:
            raise TransportError(f"canonical V5 RTDE input write failed: {exc}") from exc

    def poll_output(self, *, wait_s: float = 0.0) -> V5OutputSnapshotV1 | None:
        client = self.client
        if client is None:
            raise TransportError("V5 RTDE transport is not open")
        try:
            if not _wait_for_rtde_readable(client, wait_s):
                return None
            raw = client.recv_latest_sample(self.output_recipe, self.output_types, OUTPUT_FIELDS)
            if raw is None:
                return None
            candidate = R004OutputSnapshot.from_mapping(time.time(), raw)
            if (
                self._last_controller_timestamp is not None
                and candidate.timestamp <= self._last_controller_timestamp
            ):
                return None
            view = _decode_v5_output(candidate)
            self._last_controller_timestamp = candidate.timestamp
            self.latest = candidate
            self.latest_v5 = view
            return view
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError(f"canonical V5 RTDE output read failed: {exc}") from exc


class V5MatureTransportAdapter:
    """Present layout-607 through the mature writer's one-transport surface.

    The inherited writer still constructs the reviewed 24-double/9-integer
    safety packet.  This adapter changes only the V5 wire envelope: layout
    607, the V5 attempt kind, and input integers 33..39.  It never opens a
    second RTDE connection and it keeps the unmodified typed V5 output view
    available as :attr:`latest_v5`.

    ``poll_output`` returns an R004-shaped compatibility view because the
    mature session object uses the older four-value AttemptKind enum during
    its local ARM bookkeeping.  Only output30 is translated in that view;
    the actual controller bytes and the V5 view are retained unchanged.
    """

    def __init__(
        self,
        host: str | None = None,
        *,
        transport: LiveV5RTDETransport | None = None,
        port: int = 30004,
        timeout_s: float = 3.0,
    ) -> None:
        if transport is None:
            if not isinstance(host, str) or not host:
                raise TransportError("V5 mature adapter requires a controller host")
            transport = LiveV5RTDETransport(host, port=port, timeout_s=timeout_s)
        if not isinstance(transport, LiveV5RTDETransport):
            raise TypeError("V5 mature adapter requires LiveV5RTDETransport")
        self.transport = transport
        self.recipe_contract = transport.recipe_contract
        self.active_attempt_kind: V5AttemptKind | None = None
        self.mature_attempt_kind: AttemptKind = AttemptKind.BATCH_B
        self.rollover_input = V5RolloverInput()
        self.latest_v5: V5OutputSnapshotV1 | None = None
        self.last_v5_double_values: tuple[float, ...] | None = None
        self.last_v5_integer_values: tuple[int, ...] | None = None

    @property
    def client(self) -> Any:
        return self.transport.client

    @property
    def latest(self) -> R004OutputSnapshot | None:
        return self.transport.latest

    def bind_attempt_kind(
        self,
        v5_kind: V5AttemptKind,
        *,
        mature_kind: AttemptKind = AttemptKind.BATCH_B,
    ) -> None:
        if not isinstance(v5_kind, V5AttemptKind):
            raise TypeError("V5 active attempt kind must be typed")
        if not isinstance(mature_kind, AttemptKind):
            raise TypeError("mature compatibility attempt kind must be typed")
        self.active_attempt_kind = v5_kind
        self.mature_attempt_kind = mature_kind

    def set_rollover_input(self, value: V5RolloverInput) -> None:
        if not isinstance(value, V5RolloverInput):
            raise TypeError("V5 rollover input must be typed")
        self.rollover_input = value

    def open(self) -> None:
        self.transport.open()

    def close(self) -> None:
        self.transport.close()
        self.latest_v5 = None

    def send_packet(
        self,
        double_values: Sequence[float],
        integer_values: Sequence[int],
    ) -> None:
        if len(double_values) != len(INPUT_DOUBLE_REGISTERS):
            raise TransportError("mature V5 packet double shape differs")
        if len(integer_values) != 9:
            raise TransportError("mature V5 compatibility packet must contain input24..32")
        doubles = tuple(float(value) for value in double_values)
        if not all(math.isfinite(value) for value in doubles):
            raise TransportError("mature V5 packet contains a nonfinite double")
        doubles = (*doubles[:-1], 607.0)
        base = tuple(integer_values)
        if any(type(value) is not int for value in base):
            raise TransportError("mature V5 packet contains an untyped integer")
        session_command = base[3]
        active_kind = self.active_attempt_kind
        if session_command != 0 and active_kind is None:
            raise TransportError("V5 ARM/STOP packet lacks an active attempt kind")
        kind_value = 0 if active_kind is None else int(active_kind)
        integers = (
            *base[:7],
            kind_value,
            base[8],
            *tuple(self.rollover_input.by_register[index] for index in range(33, 40)),
        )
        self.transport.send_packet(doubles, integers)
        self.last_v5_double_values = doubles
        self.last_v5_integer_values = integers

    def poll_output(self, *, wait_s: float = 0.0) -> R004OutputSnapshot | None:
        view = self.transport.poll_output(wait_s=wait_s)
        if view is None:
            return None
        self.latest_v5 = view
        snapshot = view.r004_snapshot
        echoes = dict(snapshot.integer_echoes)
        # R004's local SessionPhase bookkeeping compares output30 against its
        # own legacy enum only during ARM.  The actual V5 overlay remains in
        # ``latest_v5`` and is what the V5 coordinator validates and seals.
        if view.state not in (
            V5TPState.CLOSURE_TAIL,
            V5TPState.ROLLOVER_PREPARED,
            V5TPState.ROLLOVER_COMMITTED,
            V5TPState.ROLLOVER_REJECTED,
        ):
            echoes[30] = int(self.mature_attempt_kind)
        return replace(snapshot, integer_echoes=echoes)


__all__ = [
    "LiveV5RTDETransport",
    "OUTPUT_FIELDS",
    "R004OutputSnapshot",
    "TransportError",
    "V5_INPUT_FIELDS",
    "V5_INPUT_INTEGER_FIELDS",
    "V5_OUTPUT_FIELDS",
    "V5OutputSnapshotV1",
    "V5MatureTransportAdapter",
    "V5RolloverInput",
    "V5RecipeContractReceiptV1",
    "V5_REGISTER_TRANSPORT_SCHEMA",
    "V5_REGISTER_TRANSPORT_VERSION",
    "V5_SETUP_RATE_HZ",
    "v5_recipe_contract_receipt",
]


V5_OUTPUT_FIELDS = OUTPUT_FIELDS
