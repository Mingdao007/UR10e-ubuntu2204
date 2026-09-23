"""Native input allocation that leaves OnRobot's status register 24 alone.

The logical layout-606 integer slot 24 is carried on physical input 36.
Input/output and integer/double register banks are independent. The OnRobot
installation continues to own input integer 24 and its own watchdog.
"""
import math
from typing import Sequence

from step5d_autotune_v4_r004.transport import (
    INPUT_DOUBLE_FIELDS, INPUT_INTEGER_FIELDS, OUTPUT_FIELDS, TransportError,
    _integer_recipe_type, _validate_input_recipe, _validate_output_recipe,
)
from step5d_autotune_v4_live_writer import WritableRTDEClient
from step5d_autotune_v4_r012.register_transport import (
    R012LiveRTDETransport, R012_INPUT_FIELD,
)

NATIVE_INPUT_INTEGER_FIELDS = tuple(
    'input_int_register_36' if name == 'input_int_register_24' else name
    for name in INPUT_INTEGER_FIELDS
)
NATIVE_INPUT_FIELDS = (*INPUT_DOUBLE_FIELDS, *NATIVE_INPUT_INTEGER_FIELDS, R012_INPUT_FIELD)
CONTACT_RAMP_DURATION_INPUT_FIELD = "input_int_register_34"
CONTACT_RAMP_PROBE_INPUT_FIELDS = (
    *INPUT_DOUBLE_FIELDS,
    *NATIVE_INPUT_INTEGER_FIELDS,
    CONTACT_RAMP_DURATION_INPUT_FIELD,
    R012_INPUT_FIELD,
)


class NativeYieldRTDETransport(R012LiveRTDETransport):
    """Reuse mature polling and serialization with a non-overlapping recipe."""

    def open(self):
        if self.client is not None:
            raise TransportError('native RTDE transport is already open')
        client = WritableRTDEClient(self.host, port=self.port, timeout=self.timeout_s)
        client.__enter__()
        try:
            client.negotiate()
            self.output_recipe, self.output_types = client.setup_outputs(500., OUTPUT_FIELDS)
            _validate_output_recipe(self.output_types)
            self.input_recipe, self.input_types = client.setup_inputs(NATIVE_INPUT_FIELDS)
            _validate_input_recipe(self.input_types[:-1])
            _integer_recipe_type(self.input_types[-1], R012_INPUT_FIELD)
            client.start()
        except BaseException:
            client.__exit__(None, None, None)
            raise
        self.client = client


class ContactRampProbeRTDETransport(NativeYieldRTDETransport):
    """Probe-only register-34 duration binding; production recipe is unchanged."""

    def __init__(self, host: str, *, ramp_duration_s: int, port: int = 30004,
                 timeout_s: float = 3.0) -> None:
        if isinstance(ramp_duration_s, bool) or ramp_duration_s not in (1, 2, 3, 4, 8):
            raise TransportError("contact-ramp probe duration must be one of 1, 2, 3, 4, 8 seconds")
        super().__init__(host, port=port, timeout_s=timeout_s)
        self.ramp_duration_s = int(ramp_duration_s)

    def open(self):
        if self.client is not None:
            raise TransportError("contact-ramp probe RTDE transport is already open")
        client = WritableRTDEClient(self.host, port=self.port, timeout=self.timeout_s)
        client.__enter__()
        try:
            client.negotiate()
            self.output_recipe, self.output_types = client.setup_outputs(500.0, OUTPUT_FIELDS)
            _validate_output_recipe(self.output_types)
            self.input_recipe, self.input_types = client.setup_inputs(CONTACT_RAMP_PROBE_INPUT_FIELDS)
            _validate_input_recipe(self.input_types[: len(INPUT_DOUBLE_FIELDS) + len(NATIVE_INPUT_INTEGER_FIELDS)])
            _integer_recipe_type(
                self.input_types[len(INPUT_DOUBLE_FIELDS) + len(NATIVE_INPUT_INTEGER_FIELDS)],
                CONTACT_RAMP_DURATION_INPUT_FIELD,
            )
            _integer_recipe_type(self.input_types[-1], R012_INPUT_FIELD)
            client.start()
        except BaseException:
            client.__exit__(None, None, None)
            raise
        self.client = client

    def send_packet(self, double_values: Sequence[float], integer_values: Sequence[int]) -> None:
        client = self.client
        if client is None:
            raise TransportError("contact-ramp probe RTDE transport is not open")
        if len(double_values) != 24 or len(integer_values) != 9:
            raise TransportError("contact-ramp probe base packet shape differs from layout-606")
        doubles = tuple(float(value) for value in double_values)
        integers = tuple(integer_values)
        sequence = int(self._r012_request_sequence)
        if not all(math.isfinite(value) for value in doubles):
            raise TransportError("contact-ramp probe packet contains a nonfinite double")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (*integers, sequence)):
            raise TransportError("contact-ramp probe packet contains an untyped integer")
        values = [*doubles, *integers, self.ramp_duration_s, sequence]
        try:
            client.send_input_sample(self.input_recipe, self.input_types, values)
        except Exception as exc:
            raise TransportError(f"contact-ramp probe RTDE input write failed: {exc}") from exc


def install_native_yield_transport(writer, *, transport_class=None, transport_kwargs=None):
    current = writer._controller_transport
    if current is not None:
        if isinstance(current, R012LiveRTDETransport) and not isinstance(current, NativeYieldRTDETransport):
            raise TransportError('legacy input24 transport conflicts with the OnRobot installation')
        if transport_class is not None and not isinstance(current, transport_class):
            raise TransportError('injected controller transport differs from requested diagnostic recipe')
        return current  # Explicit injected test endpoints remain supported.
    selected = NativeYieldRTDETransport if transport_class is None else transport_class
    if not isinstance(selected, type) or not issubclass(selected, NativeYieldRTDETransport):
        raise TransportError("native diagnostic transport must extend NativeYieldRTDETransport")
    writer._controller_transport = selected(writer.controller_host, **dict(transport_kwargs or {}))
    return writer._controller_transport
