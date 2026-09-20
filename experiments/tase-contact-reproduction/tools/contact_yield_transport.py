"""Native input allocation that leaves OnRobot's status register 24 alone.

The logical layout-606 integer slot 24 is carried on physical input 36.
Input/output and integer/double register banks are independent. The OnRobot
installation continues to own input integer 24 and its own watchdog.
"""
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


def install_native_yield_transport(writer):
    current = writer._controller_transport
    if current is not None:
        if isinstance(current, R012LiveRTDETransport) and not isinstance(current, NativeYieldRTDETransport):
            raise TransportError('legacy input24 transport conflicts with the OnRobot installation')
        return current  # Explicit injected test endpoints remain supported.
    writer._controller_transport = NativeYieldRTDETransport(writer.controller_host)
    return writer._controller_transport
