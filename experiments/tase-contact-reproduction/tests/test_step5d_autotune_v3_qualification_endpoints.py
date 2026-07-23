from __future__ import annotations

import inspect
import socket
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SOURCE = ROOT.parents[1] / "src/ur10e_experiment_runtime"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(RUNTIME_SOURCE))

from step5d_autotune_v3.dashboard import dashboard_exchange  # noqa: E402
from step5d_autotune_v3.rtde_client import RTDEClient  # noqa: E402
from step5d_autotune_v3.qualification_endpoints import (  # noqa: E402
    ENDPOINT_EVIDENCE_SCHEMA,
    EndpointSimulatorError,
    PRECONTACT_TCP_POSE,
    PRODUCTION_RTDE_INPUT_FIELDS,
    PRODUCTION_RTDE_OUTPUT_FIELDS,
    QualificationEndpointSimulator,
    R012RuntimeIdentity,
    READY_HOME,
    READY_HOME_NEXT,
    RUN,
    START_STREAM,
    STAGE25_PATH_ORIGIN_XY_M,
    STOP_STREAM,
    _TcpService,
    default_r012_runtime_identity,
    rtde_type_for_field,
    stage25_reference,
)


IDENTITY = R012RuntimeIdentity(protocol_version=1, digest_hi=1234, digest_lo=5678)


def _recvn(connection: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = connection.recv(count - len(result))
        if not chunk:
            raise RuntimeError("socket closed")
        result.extend(chunk)
    return bytes(result)


def _send_rtde(connection: socket.socket, kind: str, payload: bytes = b"") -> None:
    connection.sendall(struct.pack("!HB", 3 + len(payload), ord(kind)) + payload)


def _recv_rtde(connection: socket.socket) -> tuple[str, bytes]:
    size, kind = struct.unpack("!HB", _recvn(connection, 3))
    return chr(kind), _recvn(connection, size - 3)


def _recv_kind(connection: socket.socket, expected: str) -> bytes:
    for _ in range(10000):
        kind, payload = _recv_rtde(connection)
        if kind == expected:
            return payload
    raise AssertionError(f"RTDE packet {expected!r} was not observed")


def _format(type_name: str) -> str:
    return {
        "BOOL": "?",
        "DOUBLE": "d",
        "INT32": "i",
        "UINT32": "I",
        "UINT64": "Q",
        "VECTOR3D": "3d",
        "VECTOR6D": "6d",
    }[type_name]


def _decode(type_names: Sequence[str], payload: bytes) -> list[Any]:
    values: list[Any] = []
    cursor = 0
    for type_name in type_names:
        fmt = _format(type_name)
        width = struct.calcsize("!" + fmt)
        unpacked = struct.unpack("!" + fmt, payload[cursor : cursor + width])
        cursor += width
        values.append(unpacked[0] if len(unpacked) == 1 else list(unpacked))
    assert cursor == len(payload)
    return values


def _pack(type_names: Sequence[str], values: Sequence[Any]) -> bytes:
    encoded = bytearray()
    for type_name, value in zip(type_names, values, strict=True):
        fmt = _format(type_name)
        if type_name.startswith("VECTOR"):
            encoded.extend(struct.pack("!" + fmt, *value))
        else:
            encoded.extend(struct.pack("!" + fmt, value))
    return bytes(encoded)


def _setup_rtde(
    simulator: QualificationEndpointSimulator,
    *,
    frequency_hz: float = 500.0,
) -> tuple[socket.socket, int, tuple[str, ...], int, tuple[str, ...]]:
    connection = socket.create_connection((simulator.host, simulator.rtde_port), timeout=2.0)
    connection.settimeout(2.0)
    _send_rtde(connection, "V", struct.pack("!H", 2))
    assert _recv_rtde(connection) == ("V", b"\x01")
    _send_rtde(
        connection,
        "O",
        struct.pack("!d", frequency_hz)
        + ",".join(PRODUCTION_RTDE_OUTPUT_FIELDS).encode("ascii"),
    )
    kind, payload = _recv_rtde(connection)
    assert kind == "O"
    output_recipe = payload[0]
    output_types = tuple(payload[1:].decode("ascii").split(","))
    assert output_types == tuple(map(rtde_type_for_field, PRODUCTION_RTDE_OUTPUT_FIELDS))
    _send_rtde(
        connection,
        "I",
        ",".join(PRODUCTION_RTDE_INPUT_FIELDS).encode("ascii"),
    )
    kind, payload = _recv_rtde(connection)
    assert kind == "I"
    input_recipe = payload[0]
    input_types = tuple(payload[1:].decode("ascii").split(","))
    assert input_types == tuple(map(rtde_type_for_field, PRODUCTION_RTDE_INPUT_FIELDS))
    _send_rtde(connection, "S")
    assert _recv_rtde(connection) == ("S", b"\x01")
    return connection, output_recipe, output_types, input_recipe, input_types


def _output_row(
    connection: socket.socket,
    output_recipe: int,
    output_types: Sequence[str],
) -> dict[str, Any]:
    payload = _recv_kind(connection, "U")
    assert payload[0] == output_recipe
    return dict(
        zip(
            PRODUCTION_RTDE_OUTPUT_FIELDS,
            _decode(output_types, payload[1:]),
            strict=True,
        )
    )


def _wait_output_state(
    connection: socket.socket,
    output_recipe: int,
    output_types: Sequence[str],
    state: int,
    *,
    timeout_s: float = 2.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = _output_row(connection, output_recipe, output_types)
        if row["output_int_register_26"] == state:
            return row
    raise AssertionError(f"TP state {state} was not observed")


def _wait_output_stage(
    connection: socket.socket,
    output_recipe: int,
    output_types: Sequence[str],
    stage: float,
    *,
    timeout_s: float = 2.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = _output_row(connection, output_recipe, output_types)
        if abs(row["output_double_register_35"] - stage) < 0.001:
            return row
    raise AssertionError(f"TP stage {stage} was not observed")


def _host_row(integer_overrides: Mapping[int, int] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        **{f"input_double_register_{index}": 0.0 for index in range(24, 48)},
        **{f"input_int_register_{index}": 0 for index in range(24, 32)},
    }
    row.update(
        {
            "input_double_register_26": 7.0,
            "input_double_register_27": 1.0,
            "input_double_register_29": 8.0,
            "input_double_register_37": 0.001,
            "input_double_register_43": 1.0,
            "input_double_register_44": 0.25,
            "input_double_register_45": -0.2,
            "input_double_register_47": 631.0,
            "input_int_register_24": 7,
            "input_int_register_25": 11,
            "input_int_register_26": 1,
            "input_int_register_27": 13,
            "input_int_register_28": 633,
            "input_int_register_29": 17,
            "input_int_register_30": 1,
            "input_int_register_31": 2,
        }
    )
    row.update(
        {
            f"input_int_register_{index}": value
            for index, value in (integer_overrides or {}).items()
        }
    )
    return row


def _send_host_row(
    connection: socket.socket,
    input_recipe: int,
    input_types: Sequence[str],
    row: dict[str, Any],
) -> None:
    values = [row[field] for field in PRODUCTION_RTDE_INPUT_FIELDS]
    _send_rtde(connection, "U", bytes([input_recipe]) + _pack(input_types, values))


def test_defaults_are_content_bound_r010_and_loopback_only() -> None:
    default = inspect.signature(QualificationEndpointSimulator.__init__).parameters[
        "trial_duration_s"
    ].default
    assert default == 60.1
    identity = default_r012_runtime_identity()
    assert identity.protocol_version == 1
    assert identity.digest_hi > 0 and identity.digest_lo > 0
    first = QualificationEndpointSimulator(runtime_identity=IDENTITY)
    second = QualificationEndpointSimulator(runtime_identity=IDENTITY)
    assert first.content_sha256 == second.content_sha256
    assert first.content_binding["motion_capable"] is False
    assert first.content_binding["writes_bridge_readiness"] is False
    assert first.home_pose[2] >= PRECONTACT_TCP_POSE[2] + 0.01
    assert first.home_pose[3:] == PRECONTACT_TCP_POSE[3:]
    assert first.content_binding["tp_stage_sequence"] == [
        20.0,
        22.0,
        23.0,
        24.0,
        24.2,
        25.05,
        25.15,
        25.3,
        25.95,
        25.0,
    ]
    assert first.content_binding["kunwei_run_stages"] == [
        24.2,
        25.0,
        25.05,
        25.15,
        25.3,
        25.95,
    ]
    for host in ("0.0.0.0", "192.168.1.18", "localhost"):
        with pytest.raises(EndpointSimulatorError, match="loopback"):
            QualificationEndpointSimulator(host=host, runtime_identity=IDENTITY)


def test_rtde_controller_timestamp_tracks_elapsed_monotonic_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulator = QualificationEndpointSimulator(runtime_identity=IDENTITY)
    simulator._started_at = 100.0
    clock = [100.125]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    first = simulator._rtde_output_values(("timestamp",), 500.0)[0]
    clock[0] = 100.375
    second = simulator._rtde_output_values(("timestamp",), 500.0)[0]

    assert first == pytest.approx(0.125)
    assert second - first == pytest.approx(0.250)


def test_dashboard_secondary_and_kunwei_are_real_local_tcp_endpoints() -> None:
    values = (1.0, -2.0, 3.0, -4.0, 5.0, -6.0)
    with QualificationEndpointSimulator(
        runtime_identity=IDENTITY,
        kunwei_values=values,
        kunwei_frequency_hz=400.0,
        trial_duration_s=0.05,
    ) as simulator:
        observed = dashboard_exchange(
            simulator.host,
            ["PolyscopeVersion", "get loaded program", "programState", "safetymode"],
            port=simulator.dashboard_port,
            timeout=0.5,
        )
        assert observed["programState"] == "STOPPED"
        assert observed["get loaded program"].endswith(
            "/step5d_strict_rnn_autotune_v3_r012.urp"
        )
        simulator.simulate_play()
        assert dashboard_exchange(
            simulator.host,
            ["programState", "running"],
            port=simulator.dashboard_port,
            timeout=0.5,
        ) == {"programState": "PLAYING", "running": "Program running: true"}

        with socket.create_connection(
            (simulator.host, simulator.secondary_port), timeout=0.5
        ) as secondary:
            assert secondary.recv(128).startswith(b"UR secondary client interface")

        with socket.create_connection(
            (simulator.host, simulator.kunwei_port), timeout=0.5
        ) as sensor:
            sensor.settimeout(0.5)
            sensor.sendall(START_STREAM)
            encoded = _recvn(sensor, 28 * 3)
            for offset in range(0, len(encoded), 28):
                frame = encoded[offset : offset + 28]
                assert frame[:2] == b"\x48\xaa" and frame[-2:] == b"\r\n"
                assert struct.unpack("<6f", frame[2:26]) == pytest.approx(values)
            sensor.sendall(STOP_STREAM)
        deadline = time.monotonic() + 0.5
        while simulator.evidence()["counters"]["kunwei"]["stop_commands"] < 1:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        evidence = simulator.evidence()
        assert evidence["schema"] == ENDPOINT_EVIDENCE_SCHEMA
        assert evidence["counters"]["dashboard"]["play_transitions"] == 1
        assert evidence["counters"]["secondary"]["connections"] == 1
        assert evidence["counters"]["kunwei"]["frames_sent"] >= 3
        assert evidence["errors"] == []


def test_expected_peer_disconnect_is_not_a_protocol_error() -> None:
    errors: list[tuple[str, BaseException]] = []

    def run_handler(error: BaseException) -> None:
        left, right = socket.socketpair()
        try:
            service = _TcpService(
                "rtde",
                "127.0.0.1",
                0,
                threading.Event(),
                lambda _connection, _address: (_ for _ in ()).throw(error),
                lambda role, observed: errors.append((role, observed)),
            )
            service._connections.add(left)
            service._run_handler(left, ("127.0.0.1", 1))
        finally:
            right.close()

    run_handler(ConnectionResetError("peer closed"))
    assert errors == []
    run_handler(ValueError("protocol fault"))
    assert len(errors) == 1
    assert errors[0][0] == "rtde"
    assert isinstance(errors[0][1], ValueError)


def test_rtde_v2_production_recipe_first_trial_and_next_arm_stay_alive() -> None:
    with QualificationEndpointSimulator(
        runtime_identity=IDENTITY,
        trial_duration_s=0.10,
    ) as simulator:
        connection, output_recipe, output_types, input_recipe, input_types = _setup_rtde(
            simulator
        )
        with connection:
            initial = _wait_output_state(
                connection, output_recipe, output_types, READY_HOME
            )
            assert initial["runtime_state"] == 1
            assert [initial[f"output_int_register_{index}"] for index in (35, 36, 37)] == [
                1,
                1234,
                5678,
            ]

            arm1 = _host_row()
            _send_host_row(connection, input_recipe, input_types, arm1)
            deadline = time.monotonic() + 0.5
            while (
                simulator.evidence()["counters"]["rtde"]["rejected_arm_packets"] < 1
            ):
                assert time.monotonic() < deadline
                time.sleep(0.002)
            assert _wait_output_state(
                connection, output_recipe, output_types, READY_HOME
            )["output_int_register_26"] == READY_HOME

            simulator.simulate_play()
            _send_host_row(connection, input_recipe, input_types, arm1)
            running1 = _wait_output_state(connection, output_recipe, output_types, RUN)
            assert running1["runtime_state"] == 2
            assert [running1[f"output_int_register_{index}"] for index in range(24, 35)] == [
                7,
                11,
                RUN,
                13,
                0,
                633,
                17,
                1,
                2,
                0,
                2,
            ]
            with socket.create_connection(
                (simulator.host, simulator.kunwei_port), timeout=0.5
            ) as sensor:
                sensor.sendall(START_STREAM)
                run_frame = _recvn(sensor, 28)
                run_values = struct.unpack("<6f", run_frame[2:26])
                assert run_values == pytest.approx((0.0,) * 6)
                sensor.sendall(STOP_STREAM)

            stage23 = _wait_output_stage(
                connection, output_recipe, output_types, 23.0
            )
            assert stage23["actual_TCP_pose"] == pytest.approx(PRECONTACT_TCP_POSE)
            rezero_pending = dict(arm1)
            rezero_pending["input_double_register_26"] = 8.0
            rezero_pending["input_double_register_27"] = 0.0
            _send_host_row(connection, input_recipe, input_types, rezero_pending)
            time.sleep(0.23)
            assert _wait_output_stage(
                connection, output_recipe, output_types, 23.0
            )["output_double_register_34"] == 1.0
            rezero_complete = dict(arm1)
            rezero_complete["input_double_register_26"] = 9.0
            _send_host_row(connection, input_recipe, input_types, rezero_complete)

            _wait_output_stage(connection, output_recipe, output_types, 24.2)
            with socket.create_connection(
                (simulator.host, simulator.kunwei_port), timeout=0.5
            ) as sensor:
                sensor.sendall(START_STREAM)
                contact_frame = _recvn(sensor, 28)
                contact_values = struct.unpack("<6f", contact_frame[2:26])
                assert contact_values[2] * 9.80665 == pytest.approx(12.0, abs=1e-5)
                sensor.sendall(STOP_STREAM)

            _wait_output_stage(connection, output_recipe, output_types, 25.3)
            line_entry = dict(arm1)
            line_entry.update(
                {
                    "input_double_register_40": 7.5,
                    "input_double_register_41": 14.0,
                    "input_double_register_42": 25.0,
                    "input_double_register_43": 1.0,
                    "input_double_register_44": 0.01,
                    "input_double_register_46": 1.0,
                    "input_double_register_47": 521.0,
                }
            )
            _send_host_row(connection, input_recipe, input_types, line_entry)

            _wait_output_stage(connection, output_recipe, output_types, 25.95)
            clear = dict(arm1)
            clear.update(
                {
                    f"input_double_register_{index}": 0.0
                    for index in range(37, 48)
                }
            )
            _send_host_row(connection, input_recipe, input_types, clear)
            _wait_output_stage(connection, output_recipe, output_types, 25.0)

            stage25_command = dict(arm1)
            stage25_command["input_double_register_44"] = 0.25
            stage25_command["input_double_register_47"] = 524.0
            _send_host_row(connection, input_recipe, input_types, stage25_command)
            deadline = time.monotonic() + 0.5
            while True:
                running1 = _wait_output_stage(
                    connection, output_recipe, output_types, 25.0
                )
                if running1["output_double_register_47"] == 1.0:
                    break
                assert time.monotonic() < deadline
            expected_pose, expected_speed = stage25_reference(0.25)
            assert running1["actual_TCP_pose"] == pytest.approx(expected_pose)
            assert running1["actual_TCP_speed"] == pytest.approx(expected_speed)
            assert running1["actual_TCP_pose"][:2] != pytest.approx(
                STAGE25_PATH_ORIGIN_XY_M
            )

            terminal = _wait_output_state(
                connection, output_recipe, output_types, READY_HOME_NEXT
            )
            assert terminal["output_int_register_28"] == 1
            assert terminal["output_int_register_33"] == 0x7F
            assert [terminal[f"output_double_register_{index}"] for index in range(35, 45)] == [
                40.3,
                0.0,
                0.0,
                0.0,
                3.0,
                0.0,
                0.0,
                0.0,
                0.0,
                0.002,
            ]

            arm2 = _host_row({25: 12, 27: 14, 29: 18, 30: 2, 31: 3})
            _send_host_row(connection, input_recipe, input_types, arm2)
            running2 = _wait_output_state(connection, output_recipe, output_types, RUN)
            assert running2["output_int_register_25"] == 12
            assert running2["output_int_register_30"] == 18
            assert running2["output_int_register_34"] == 3
            assert simulator.wait_for_tp_state(RUN, timeout_s=0.2, minimum_arm_count=2)
            _send_rtde(connection, "P")
            assert _recv_kind(connection, "P") == b"\x01"

        evidence = simulator.evidence()
        rtde = evidence["counters"]["rtde"]
        assert rtde["arm_acknowledgements"] == 2
        assert rtde["rejected_arm_packets"] == 1
        assert rtde["trials_completed"] == 1
        assert rtde["last_output_frequency_hz"] == 500.0
        assert rtde["host_stop_requests"] == 0
        assert rtde["stage25_max_center_error_m"] == 0.0
        assert all(
            rtde["stage_samples"].get(format(stage, "g"), 0) > 0
            for stage in (23.0, 24.0, 24.2, 25.0)
        )
        assert rtde["last_input_fields"] == list(PRODUCTION_RTDE_INPUT_FIELDS)
        assert rtde["last_input_values"]["input_int_register_25"] == 12
        assert rtde["last_input_values"]["input_double_register_47"] == 631.0
        assert rtde["received_packets"]["U"] == 8
        assert evidence["alive"] is True
        assert evidence["errors"] == []


@pytest.mark.parametrize(
    ("batch_row_index", "accepted"),
    ((0, False), (1, True), (5, True), (6, False)),
)
def test_rtde_arm_batch_row_domain_matches_tp_one_through_five(
    batch_row_index: int,
    accepted: bool,
) -> None:
    with QualificationEndpointSimulator(
        runtime_identity=IDENTITY,
        trial_duration_s=1.0,
    ) as simulator:
        connection, output_recipe, output_types, input_recipe, input_types = _setup_rtde(
            simulator
        )
        with connection:
            _wait_output_state(connection, output_recipe, output_types, READY_HOME)
            simulator.simulate_play()
            _send_host_row(
                connection,
                input_recipe,
                input_types,
                _host_row({30: batch_row_index}),
            )
            if accepted:
                _wait_output_state(connection, output_recipe, output_types, RUN)
            else:
                deadline = time.monotonic() + 0.5
                while (
                    simulator.evidence()["counters"]["rtde"]["rejected_arm_packets"]
                    < 1
                ):
                    assert time.monotonic() < deadline
                    time.sleep(0.002)
                assert _wait_output_state(
                    connection, output_recipe, output_types, READY_HOME
                )["output_int_register_26"] == READY_HOME
        counters = simulator.evidence()["counters"]["rtde"]
        assert counters["arm_acknowledgements"] == int(accepted)
        assert counters["rejected_arm_packets"] == int(not accepted)


def test_rtde_repository_client_rejects_unknown_recipe_field() -> None:
    fields = [
        "timestamp",
        "actual_TCP_pose",
        "actual_TCP_force",
        "payload",
        "payload_cog",
        "tcp_offset",
        "runtime_state",
        "safety_mode",
        "output_int_register_35",
        "output_int_register_36",
        "output_int_register_37",
    ]
    with QualificationEndpointSimulator(
        runtime_identity=IDENTITY, trial_duration_s=0.05
    ) as simulator:
        with RTDEClient(
            simulator.host, port=simulator.rtde_port, timeout=1.0
        ) as client:
            client.negotiate()
            recipe, types = client.setup_outputs(500.0, fields)
            client.start()
            values = client.recv_recipe_sample(recipe, types)
        row = dict(zip(fields, values, strict=True))
        assert row["safety_mode"] == 1
        assert row["actual_TCP_force"] == [0.0] * 6
        assert row["payload"] == 0.0
        assert row["payload_cog"] == [0.0] * 3
        assert row["tcp_offset"] == [0.0] * 6
        assert [row[f"output_int_register_{index}"] for index in (35, 36, 37)] == [
            1,
            1234,
            5678,
        ]

        with socket.create_connection(
            (simulator.host, simulator.rtde_port), timeout=1.0
        ) as connection:
            connection.settimeout(1.0)
            _send_rtde(connection, "V", struct.pack("!H", 2))
            assert _recv_rtde(connection) == ("V", b"\x01")
            _send_rtde(
                connection,
                "O",
                struct.pack("!d", 500.0) + b"timestamp,invented_register",
            )
            kind, payload = _recv_rtde(connection)
            assert kind == "O"
            assert payload == b"\x00DOUBLE,NOT_FOUND"
