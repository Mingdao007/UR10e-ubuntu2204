from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step6_figure8_autotune_v1 import v5_register_transport as v5  # noqa: E402
from step6_figure8_autotune_v1.v5_composition_contract import (  # noqa: E402
    BaseOutputOverlayV2,
    RolloverCommand,
    RolloverOutputOverlayV2,
    V5AttemptKind,
    V5RolloverInput,
    V5TPState,
)
from step5d_autotune_v4_r004.wire import AttemptKind  # noqa: E402


def _output_types() -> list[str]:
    return [
        "DOUBLE",
        "DOUBLE",
        "VECTOR3D",
        "VECTOR6D",
        "VECTOR6D",
        "VECTOR6D",
        "VECTOR6D",
        "VECTOR6D",
        "UINT32",
        "UINT32",
        "UINT32",
        "DOUBLE",
        *("INT32" for _ in range(11)),
    ]


class FakeWritableRTDEClient:
    instances = 0
    last: "FakeWritableRTDEClient | None" = None

    def __init__(self, host: str, *, port: int, timeout: float) -> None:
        type(self).instances += 1
        type(self).last = self
        self.init = (host, port, timeout)
        self.sock = object()
        self.samples: list[dict[str, object]] = []
        self.calls: list[tuple[str, object]] = []

    def __enter__(self) -> "FakeWritableRTDEClient":
        self.calls.append(("enter", None))
        return self

    def __exit__(self, *_args: object) -> None:
        self.calls.append(("exit", None))

    def negotiate(self) -> None:
        self.calls.append(("negotiate", None))

    def setup_outputs(self, rate_hz: float, fields: tuple[str, ...]) -> tuple[int, list[str]]:
        self.calls.append(("setup_outputs", (rate_hz, fields)))
        return 7, _output_types()

    def setup_inputs(self, fields: tuple[str, ...]) -> tuple[int, list[str]]:
        self.calls.append(("setup_inputs", fields))
        return 8, ["DOUBLE"] * 24 + ["UINT32", "INT32"] * 8

    def start(self) -> None:
        self.calls.append(("start", None))

    def send_input_sample(self, recipe: int, types: list[str], values: list[object]) -> None:
        self.calls.append(("send", (recipe, types, values)))

    def recv_latest_sample(
        self,
        _recipe: int,
        _types: list[str],
        _fields: tuple[str, ...],
    ) -> dict[str, object] | None:
        if not self.samples:
            return None
        return self.samples.pop(0)


def _raw_output(
    *,
    timestamp: float,
    state: int,
    overlay: tuple[int, int, int],
) -> dict[str, object]:
    raw: dict[str, object] = {
        "timestamp": timestamp,
        "payload": 1.0,
        "payload_cog": [0.0, 0.0, 0.0],
        "tcp_offset": [0.0] * 6,
        "actual_TCP_speed": [0.0] * 6,
        "actual_TCP_pose": [0.0] * 6,
        "actual_q": [0.0] * 6,
        "actual_qd": [0.0] * 6,
        "safety_mode": "NORMAL",
        "robot_mode": "RUNNING",
        "runtime_state": 2,
        "output_double_register_24": 0.0,
    }
    for register in range(24, 35):
        raw[f"output_int_register_{register}"] = 0
    raw["output_int_register_26"] = state
    raw["output_int_register_29"] = overlay[0]
    raw["output_int_register_30"] = overlay[1]
    raw["output_int_register_31"] = overlay[2]
    return raw


def _open(monkeypatch: pytest.MonkeyPatch) -> tuple[v5.LiveV5RTDETransport, FakeWritableRTDEClient]:
    FakeWritableRTDEClient.instances = 0
    FakeWritableRTDEClient.last = None
    monkeypatch.setattr(
        v5,
        "_canonical_v4",
        lambda: SimpleNamespace(WritableRTDEClient=FakeWritableRTDEClient),
    )
    transport = v5.LiveV5RTDETransport("offline-test", port=30004)
    transport.open()
    client = FakeWritableRTDEClient.last
    assert client is not None
    return transport, client


def test_recipe_receipt_and_setup_are_exact_layout607_without_output35_to_39(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = v5.v5_recipe_contract_receipt()
    payload = receipt.as_dict()
    assert payload["layout_tag"] == 607.0
    assert payload["setup_rate_hz"] == 500.0
    assert payload["input_recipe"]["double_registers"] == list(range(24, 48))
    assert payload["input_recipe"]["integer_registers"] == list(range(24, 40))
    assert payload["output_recipe"]["double_registers"] == [24]
    assert payload["output_recipe"]["integer_registers"] == list(range(24, 35))
    assert payload["rtde_connection_count"] == 1
    assert payload["single_rtde_connection"] is True
    assert payload["no_motion"] is True
    assert payload["controller_program_state_change"] is False
    assert payload["sensor_reader"] is False
    assert payload["output_35_to_39_requested"] is False
    assert payload["live_acceptance_claim"] is False

    transport, client = _open(monkeypatch)
    output_setup = next(value for name, value in client.calls if name == "setup_outputs")
    input_setup = next(value for name, value in client.calls if name == "setup_inputs")
    assert output_setup == (500.0, v5.OUTPUT_FIELDS)
    assert input_setup == v5.V5_INPUT_FIELDS
    assert not any("output_int_register_35" in field for field in output_setup[1])
    assert not any("output_int_register_39" in field for field in output_setup[1])
    transport.close()


def test_packet_shape_type_and_recipe_range_checks_use_one_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, client = _open(monkeypatch)
    transport.send_packet((0.0,) * 24, tuple(range(16)))
    sent = next(value for name, value in client.calls if name == "send")
    assert sent[0] == 8
    assert len(sent[2]) == 40
    assert sent[2][:24] == [0.0] * 24
    assert sent[2][24:] == list(range(16))

    with pytest.raises(v5.TransportError, match="shape"):
        transport.send_packet((0.0,) * 23, tuple(range(16)))
    with pytest.raises(v5.TransportError, match="nonfinite"):
        transport.send_packet((math.nan,) + (0.0,) * 23, tuple(range(16)))
    with pytest.raises(v5.TransportError, match="untyped"):
        transport.send_packet((0.0,) * 24, (True,) + tuple(range(1, 16)))
    with pytest.raises(v5.TransportError, match="out of range"):
        transport.send_packet((0.0,) * 24, (2**32,) + tuple(range(1, 16)))
    with pytest.raises(v5.TransportError, match="out of range"):
        transport.send_packet((0.0,) * 24, (0, 2**31) + tuple(range(2, 16)))

    with pytest.raises(v5.TransportError, match="already open"):
        transport.open()
    assert FakeWritableRTDEClient.instances == 1
    transport.close()


def test_output_view_preserves_r004_snapshot_and_decodes_state_dependent_overlays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, client = _open(monkeypatch)
    client.samples.extend(
        [
            _raw_output(
                timestamp=1.0,
                state=int(V5TPState.PATH),
                overlay=(12, int(V5AttemptKind.PRIMARY_NOVEL), 0),
            ),
            _raw_output(
                timestamp=2.0,
                state=int(V5TPState.CLOSURE_TAIL),
                overlay=(7, 6, 9001),
            ),
        ]
    )
    base = transport.poll_output()
    tail = transport.poll_output()
    assert base is not None and isinstance(base.r004_snapshot, v5.R004OutputSnapshot)
    assert base.state is V5TPState.PATH
    assert isinstance(base.overlay, BaseOutputOverlayV2)
    assert base.overlay.by_register == {29: 12, 30: int(V5AttemptKind.PRIMARY_NOVEL), 31: 0}
    assert tail is not None and tail.state is V5TPState.CLOSURE_TAIL
    assert isinstance(tail.overlay, RolloverOutputOverlayV2)
    assert tail.overlay.by_register == {29: 7, 30: 6, 31: 9001}
    transport.close()


def test_stale_timestamp_is_suppressed_and_unknown_state_or_overlay_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport, client = _open(monkeypatch)
    client.samples.extend(
        [
            _raw_output(timestamp=3.0, state=int(V5TPState.PATH), overlay=(1, 10, 0)),
            _raw_output(timestamp=3.0, state=int(V5TPState.PATH), overlay=(2, 10, 0)),
            _raw_output(timestamp=4.0, state=int(V5TPState.PATH), overlay=(3, 10, 0)),
        ]
    )
    first = transport.poll_output()
    stale = transport.poll_output()
    fresh = transport.poll_output()
    assert first is not None and first.timestamp == 3.0
    assert stale is None
    assert fresh is not None and fresh.timestamp == 4.0
    transport.close()

    unknown_transport, unknown_client = _open(monkeypatch)
    unknown_client.samples.append(_raw_output(timestamp=5.0, state=999, overlay=(1, 10, 0)))
    with pytest.raises(v5.TransportError, match="known V5TPState"):
        unknown_transport.poll_output()
    unknown_transport.close()

    invalid_transport, invalid_client = _open(monkeypatch)
    invalid_client.samples.append(_raw_output(timestamp=6.0, state=int(V5TPState.PATH), overlay=(1, 999, 0)))
    with pytest.raises(v5.TransportError, match="overlay"):
        invalid_transport.poll_output()
    invalid_transport.close()


class FakeLiveV5Transport(v5.LiveV5RTDETransport):
    def __init__(self) -> None:
        self.recipe_contract = v5.v5_recipe_contract_receipt()
        self.client: object | None = None
        self.latest: v5.R004OutputSnapshot | None = None
        self.latest_v5: v5.V5OutputSnapshotV1 | None = None
        self.sent: list[tuple[tuple[float, ...], tuple[int, ...]]] = []
        self.queued: list[v5.V5OutputSnapshotV1] = []

    def open(self) -> None:
        self.client = object()

    def close(self) -> None:
        self.client = None

    def send_packet(self, double_values: object, integer_values: object) -> None:
        self.sent.append((tuple(double_values), tuple(integer_values)))  # type: ignore[arg-type]

    def poll_output(self, *, wait_s: float = 0.0) -> v5.V5OutputSnapshotV1 | None:
        del wait_s
        if not self.queued:
            return None
        view = self.queued.pop(0)
        self.latest = view.r004_snapshot
        self.latest_v5 = view
        return view


def _v5_view(*, timestamp: float, state: V5TPState, overlay: tuple[int, int, int]) -> v5.V5OutputSnapshotV1:
    snapshot = v5.R004OutputSnapshot.from_mapping(
        timestamp,
        _raw_output(timestamp=timestamp, state=int(state), overlay=overlay),
    )
    return v5._decode_v5_output(snapshot)


def test_mature_adapter_fans_in_layout607_without_a_second_connection() -> None:
    transport = FakeLiveV5Transport()
    adapter = v5.V5MatureTransportAdapter(transport=transport)
    adapter.bind_attempt_kind(
        V5AttemptKind.CORRECTION_NOVEL,
        mature_kind=AttemptKind.RETEST,
    )
    rollover = V5RolloverInput(
        command=RolloverCommand.PREPARE,
        generation=4,
        path_early_end_request=1,
        next_attempt_ordinal=72,
        next_attempt_kind=V5AttemptKind.CORRECTION_NOVEL,
        next_candidate_token=9001,
        qdot_generation=0,
    )
    adapter.set_rollover_input(rollover)

    adapter.open()
    assert adapter.client is transport.client
    mature_doubles = tuple(float(index) for index in range(24))
    mature_integers = (11, 12, 13, 1, 15, 16, 17, int(AttemptKind.BATCH_A), 19)
    adapter.send_packet(mature_doubles, mature_integers)

    assert len(transport.sent) == 1
    sent_doubles, sent_integers = transport.sent[0]
    assert sent_doubles[:-1] == mature_doubles[:-1]
    assert sent_doubles[-1] == 607.0
    assert sent_integers[:7] == mature_integers[:7]
    assert sent_integers[7] == int(V5AttemptKind.CORRECTION_NOVEL)
    assert sent_integers[8] == mature_integers[8]
    assert sent_integers[9:] == tuple(rollover.by_register[index] for index in range(33, 40))
    assert len(sent_integers) == 16
    assert adapter.last_v5_integer_values == sent_integers
    adapter.close()


def test_mature_adapter_preserves_raw_v5_overlay_and_only_translates_arm_bookkeeping() -> None:
    transport = FakeLiveV5Transport()
    adapter = v5.V5MatureTransportAdapter(transport=transport)
    adapter.bind_attempt_kind(
        V5AttemptKind.PRIMARY_NOVEL,
        mature_kind=AttemptKind.BATCH_B,
    )
    path = _v5_view(
        timestamp=10.0,
        state=V5TPState.PATH,
        overlay=(8, int(V5AttemptKind.PRIMARY_NOVEL), 0),
    )
    tail = _v5_view(
        timestamp=11.0,
        state=V5TPState.CLOSURE_TAIL,
        overlay=(5, 4, 9001),
    )
    transport.queued.extend((path, tail))

    path_compat = adapter.poll_output()
    assert path_compat is not None
    assert path_compat.integer_echoes[30] == int(AttemptKind.BATCH_B)
    assert adapter.latest_v5 is path
    assert adapter.latest_v5.integer_echoes[30] == int(V5AttemptKind.PRIMARY_NOVEL)

    tail_compat = adapter.poll_output()
    assert tail_compat is not None
    assert tail_compat.integer_echoes[30] == 4
    assert adapter.latest_v5 is tail


def test_mature_adapter_requires_typed_kind_for_non_hold_packets() -> None:
    adapter = v5.V5MatureTransportAdapter(transport=FakeLiveV5Transport())
    with pytest.raises(v5.TransportError, match="active attempt kind"):
        adapter.send_packet((0.0,) * 24, (0, 0, 0, 1, 0, 0, 0, 0, 0))
    with pytest.raises(TypeError, match="typed"):
        adapter.bind_attempt_kind(10)  # type: ignore[arg-type]
    with pytest.raises(v5.TransportError, match="must contain input24..32"):
        adapter.send_packet((0.0,) * 24, (0,) * 8)
