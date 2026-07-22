"""Loopback-only controller endpoints for Step5d production qualification.

The simulator owns only the Dashboard, RTDE, and Kunwei TCP endpoints.  It
never writes bridge readiness or campaign evidence files; those remain outputs
of the production launcher and bridge process tree.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
import copy
import hashlib
import ipaddress
import json
import math
import select
import socket
import struct
import threading
import time
from typing import Any, Callable, Mapping, Sequence


R010_PROGRAM_ID = "step5d_strict_rnn_autotune_v3_r010"
ROLLING_PROTOCOL_ID = "v3_full_home_rolling_arm_v1"
ENDPOINT_CONTENT_SCHEMA = "step5d.autotune-v3/qualification-endpoint-content-v1"
ENDPOINT_EVIDENCE_SCHEMA = "step5d.autotune-v3/qualification-endpoint-evidence-v1"

START_STREAM = bytes.fromhex("48 AA 0D 0A")
STOP_STREAM = bytes.fromhex("43 AA 0D 0A")

READY_HOME = 10
RUN = 20
READY_HOME_CLOSED = 77
READY_HOME_NEXT = 78
FAULT = 90

PRECONTACT_TCP_POSE = (
    0.487834547,
    0.129337053,
    0.022863519,
    3.120752062,
    0.0,
    0.068626833,
)
STAGE25_CONTACT_Z_M = 0.008044839
STAGE25_PATH_ORIGIN_XY_M = (0.487795411149049, 0.12932679270060748)
STAGE25_PATH_U_ALONG_XY = (-0.010785642631908187, 0.9999418332648238)
STAGE25_PATH_P_LATERAL_XY = (-0.9999418332648239, -0.010785642631908406)
STAGE25_PATH_AMPLITUDE_M = 0.015
STAGE25_PATH_OMEGA_RAD_S = 0.1
STAGE25_PATH_DURATION_S = 60.0

_TP_STAGE_SEQUENCE = (20.0, 22.0, 23.0, 24.0, 24.2, 25.05, 25.15, 25.3, 25.95, 25.0)
_CONTACT_FORCE_STAGES = frozenset({24.2, 25.05, 25.15, 25.3, 25.95, 25.0})
_TP_STAGE_MINIMUM_DWELL_S = {
    20.0: 0.020,
    22.0: 0.020,
    23.0: 0.220,
    24.0: 0.020,
    24.2: 0.020,
    25.05: 0.010,
    25.15: 0.004,
}

PRODUCTION_RTDE_OUTPUT_FIELDS = (
    "timestamp",
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_q",
    "actual_qd",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    "speed_scaling",
    *(f"output_double_register_{index}" for index in range(24, 48)),
    *(f"output_int_register_{index}" for index in range(24, 38)),
)
PRODUCTION_RTDE_INPUT_FIELDS = (
    *(f"input_double_register_{index}" for index in range(24, 48)),
    *(f"input_int_register_{index}" for index in range(24, 32)),
)

_VECTOR6D_FIELDS = {
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_TCP_force",
    "actual_q",
    "actual_qd",
    "tcp_offset",
}
_VECTOR3D_FIELDS = {"payload_cog"}
_RTDE_FORMATS = {
    "BOOL": "?",
    "DOUBLE": "d",
    "INT32": "i",
    "UINT32": "I",
    "UINT64": "Q",
    "VECTOR3D": "3d",
    "VECTOR6D": "6d",
}
_TP_STATE_NAMES = {
    READY_HOME: "READY_HOME",
    RUN: "RUN",
    READY_HOME_CLOSED: "READY_HOME_CLOSED",
    READY_HOME_NEXT: "READY_HOME_NEXT",
    FAULT: "FAULT",
}


class EndpointSimulatorError(RuntimeError):
    """The loopback endpoint configuration or protocol exchange is invalid."""


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool):
        raise EndpointSimulatorError(f"{role} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise EndpointSimulatorError(f"{role} must be finite") from exc
    if not math.isfinite(result):
        raise EndpointSimulatorError(f"{role} must be finite")
    return result


def _positive(value: Any, role: str) -> float:
    result = _finite(value, role)
    if result <= 0.0:
        raise EndpointSimulatorError(f"{role} must be positive")
    return result


def _port(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 65535:
        raise EndpointSimulatorError(f"{role} must be an integer in [0, 65535]")
    return value


def stage25_reference(progress_s: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Return the production cycloid center pose and tangent velocity."""

    progress = min(max(_finite(progress_s, "Stage25 progress"), 0.0), STAGE25_PATH_DURATION_S)
    phase = STAGE25_PATH_OMEGA_RAD_S * progress
    local_x = STAGE25_PATH_AMPLITUDE_M * (phase - math.sin(phase))
    local_y = STAGE25_PATH_AMPLITUDE_M * (1.0 - math.cos(phase))
    local_vx = (
        STAGE25_PATH_AMPLITUDE_M
        * STAGE25_PATH_OMEGA_RAD_S
        * (1.0 - math.cos(phase))
    )
    local_vy = (
        STAGE25_PATH_AMPLITUDE_M
        * STAGE25_PATH_OMEGA_RAD_S
        * math.sin(phase)
    )
    x = (
        STAGE25_PATH_ORIGIN_XY_M[0]
        + local_x * STAGE25_PATH_U_ALONG_XY[0]
        + local_y * STAGE25_PATH_P_LATERAL_XY[0]
    )
    y = (
        STAGE25_PATH_ORIGIN_XY_M[1]
        + local_x * STAGE25_PATH_U_ALONG_XY[1]
        + local_y * STAGE25_PATH_P_LATERAL_XY[1]
    )
    vx = (
        local_vx * STAGE25_PATH_U_ALONG_XY[0]
        + local_vy * STAGE25_PATH_P_LATERAL_XY[0]
    )
    vy = (
        local_vx * STAGE25_PATH_U_ALONG_XY[1]
        + local_vy * STAGE25_PATH_P_LATERAL_XY[1]
    )
    return (
        (x, y, STAGE25_CONTACT_Z_M, *PRECONTACT_TCP_POSE[3:]),
        (vx, vy, 0.0, 0.0, 0.0, 0.0),
    )


def rtde_type_for_field(field: str) -> str:
    """Return the PolyScope RTDE type for every production qualification field."""

    if field in _VECTOR6D_FIELDS:
        return "VECTOR6D"
    if field in _VECTOR3D_FIELDS:
        return "VECTOR3D"
    if field == "runtime_state":
        return "UINT32"
    if field in {"robot_mode", "safety_mode"}:
        return "INT32"
    if field in {"timestamp", "speed_scaling", "payload"}:
        return "DOUBLE"
    if field.startswith(("input_double_register_", "output_double_register_")):
        suffix = field.rsplit("_", 1)[-1]
        if suffix.isdigit() and 0 <= int(suffix) <= 47:
            return "DOUBLE"
    if field.startswith(("input_int_register_", "output_int_register_")):
        suffix = field.rsplit("_", 1)[-1]
        if suffix.isdigit() and 0 <= int(suffix) <= 47:
            return "INT32"
    return "NOT_FOUND"


def _pack_rtde_value(type_name: str, value: Any) -> bytes:
    fmt = _RTDE_FORMATS.get(type_name)
    if fmt is None:
        raise EndpointSimulatorError(f"unsupported RTDE type {type_name!r}")
    if type_name in {"VECTOR3D", "VECTOR6D"}:
        return struct.pack("!" + fmt, *value)
    return struct.pack("!" + fmt, value)


def _decode_rtde_values(type_names: Sequence[str], payload: bytes) -> list[Any]:
    values: list[Any] = []
    cursor = 0
    for type_name in type_names:
        fmt = _RTDE_FORMATS.get(type_name)
        if fmt is None:
            raise EndpointSimulatorError(f"unsupported RTDE type {type_name!r}")
        width = struct.calcsize("!" + fmt)
        if cursor + width > len(payload):
            raise EndpointSimulatorError("RTDE input packet is truncated")
        unpacked = struct.unpack("!" + fmt, payload[cursor : cursor + width])
        cursor += width
        values.append(unpacked[0] if len(unpacked) == 1 else list(unpacked))
    if cursor != len(payload):
        raise EndpointSimulatorError("RTDE input packet has trailing bytes")
    return values


@dataclass(frozen=True)
class R010RuntimeIdentity:
    protocol_version: int
    digest_hi: int
    digest_lo: int
    program_id: str = R010_PROGRAM_ID
    protocol_id: str = ROLLING_PROTOCOL_ID

    def __post_init__(self) -> None:
        if self.program_id != R010_PROGRAM_ID or self.protocol_id != ROLLING_PROTOCOL_ID:
            raise EndpointSimulatorError("TP runtime identity is not the r010 rolling release")
        if self.protocol_version != 1:
            raise EndpointSimulatorError("r010 runtime protocol version must be 1")
        for role, value in (("digest_hi", self.digest_hi), ("digest_lo", self.digest_lo)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**31:
                raise EndpointSimulatorError(f"{role} must be an unsigned 31-bit integer")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "R010RuntimeIdentity":
        return cls(
            protocol_version=payload["protocol_version"],
            digest_hi=payload["digest_hi"],
            digest_lo=payload["digest_lo"],
            program_id=payload.get("program_id", R010_PROGRAM_ID),
            protocol_id=payload.get("protocol_id", ROLLING_PROTOCOL_ID),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "protocol_id": self.protocol_id,
            "protocol_version": self.protocol_version,
            "digest_hi": self.digest_hi,
            "digest_lo": self.digest_lo,
            "registers": {"protocol_version": 35, "digest_hi": 36, "digest_lo": 37},
        }


@lru_cache(maxsize=1)
def default_r010_runtime_identity() -> R010RuntimeIdentity:
    """Derive the exact current r010 literals from the canonical TP generator."""

    import build_step5d_autotune_tp_v3 as builder  # noqa: PLC0415
    from step5d_autotune_v3.runtime_identity import bind_final_script  # noqa: PLC0415

    _, payload = bind_final_script(
        builder.render_script(),
        program_id=builder.PROGRAM_NAME,
        protocol_id=builder.PROTOCOL_ID,
    )
    return R010RuntimeIdentity.from_mapping(payload)


class _TcpService:
    def __init__(
        self,
        role: str,
        host: str,
        port: int,
        stop_event: threading.Event,
        handler: Callable[[socket.socket, tuple[str, int]], None],
        error_sink: Callable[[str, BaseException], None],
    ) -> None:
        self.role = role
        self.host = host
        self.requested_port = port
        self.stop_event = stop_event
        self.handler = handler
        self.error_sink = error_sink
        self.listener: socket.socket | None = None
        self.port = 0
        self.accept_thread: threading.Thread | None = None
        self._connections: set[socket.socket] = set()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.requested_port))
        listener.listen(16)
        listener.settimeout(0.1)
        self.listener = listener
        self.port = int(listener.getsockname()[1])
        self.accept_thread = threading.Thread(
            target=self._accept_loop,
            name=f"step5d-qualification-{self.role}",
            daemon=True,
        )
        self.accept_thread.start()

    def _accept_loop(self) -> None:
        assert self.listener is not None
        while not self.stop_event.is_set():
            try:
                connection, address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                if not self.stop_event.is_set():
                    self.error_sink(self.role, exc)
                return
            with self._lock:
                self._connections.add(connection)
            thread = threading.Thread(
                target=self._run_handler,
                args=(connection, address),
                name=f"step5d-qualification-{self.role}-client",
                daemon=True,
            )
            with self._lock:
                self._threads.append(thread)
            thread.start()

    def _run_handler(self, connection: socket.socket, address: tuple[str, int]) -> None:
        try:
            self.handler(connection, address)
        except ConnectionError:
            pass
        except OSError as exc:
            if not self.stop_event.is_set():
                self.error_sink(self.role, exc)
        except BaseException as exc:  # protocol faults must remain observable
            if not self.stop_event.is_set():
                self.error_sink(self.role, exc)
        finally:
            with self._lock:
                self._connections.discard(connection)
            try:
                connection.close()
            except OSError:
                pass

    def close(self) -> None:
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
        with self._lock:
            connections = tuple(self._connections)
            threads = tuple(self._threads)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        if self.accept_thread is not None:
            self.accept_thread.join(timeout=1.0)
        for thread in threads:
            thread.join(timeout=1.0)


class QualificationEndpointSimulator:
    """No-motion localhost substitute for production qualification endpoints."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        dashboard_port: int = 0,
        secondary_port: int = 0,
        rtde_port: int = 0,
        kunwei_port: int = 0,
        runtime_identity: R010RuntimeIdentity | Mapping[str, Any] | None = None,
        expected_program_id: str = R010_PROGRAM_ID,
        loaded_program: str = (
            "/programs/andyl/kunwei/step5/"
            "step5d_strict_rnn_autotune_v3_r010.urp"
        ),
        trial_duration_s: float = 60.1,
        rtde_frequency_limit_hz: float = 1000.0,
        kunwei_frequency_hz: float = 1000.0,
        kunwei_values: Sequence[float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        kunwei_run_values: Sequence[float] = (
            0.0,
            0.0,
            1.223659773,
            0.0,
            0.0,
            0.0,
        ),
        home_pose: Sequence[float] = (
            0.45,
            0.10,
            0.055,
            PRECONTACT_TCP_POSE[3],
            PRECONTACT_TCP_POSE[4],
            PRECONTACT_TCP_POSE[5],
        ),
        home_q: Sequence[float] = (0.62, -1.65, -2.55, -0.49, 1.55, -0.95),
    ) -> None:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise EndpointSimulatorError("endpoint host must be a loopback IPv4 literal") from exc
        if address.version != 4 or not address.is_loopback:
            raise EndpointSimulatorError("qualification endpoints are loopback-only")
        self.host = str(address)
        self.requested_ports = {
            "dashboard": _port(dashboard_port, "Dashboard port"),
            "secondary": _port(secondary_port, "secondary interface port"),
            "rtde": _port(rtde_port, "RTDE port"),
            "kunwei": _port(kunwei_port, "Kunwei port"),
        }
        if runtime_identity is None:
            self.runtime_identity = default_r010_runtime_identity()
        elif isinstance(runtime_identity, R010RuntimeIdentity):
            self.runtime_identity = runtime_identity
        else:
            self.runtime_identity = R010RuntimeIdentity.from_mapping(runtime_identity)
        if (
            not isinstance(expected_program_id, str)
            or not expected_program_id
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in expected_program_id)
        ):
            raise EndpointSimulatorError("expected program ID is invalid")
        if not isinstance(loaded_program, str) or not loaded_program.endswith(
            f"/{expected_program_id}.urp"
        ):
            raise EndpointSimulatorError(
                "loaded program must match the exact expected controller program"
            )
        self.expected_program_id = expected_program_id
        self.loaded_program = loaded_program
        self.trial_duration_s = _positive(trial_duration_s, "trial duration")
        self.rtde_frequency_limit_hz = _positive(
            rtde_frequency_limit_hz, "RTDE frequency limit"
        )
        self.kunwei_frequency_hz = _positive(kunwei_frequency_hz, "Kunwei frequency")
        if (
            len(kunwei_values) != 6
            or len(kunwei_run_values) != 6
            or len(home_pose) != 6
            or len(home_q) != 6
        ):
            raise EndpointSimulatorError(
                "Kunwei baseline/run, home pose, and home joint vectors must have six values"
            )
        self.kunwei_values = tuple(
            _finite(value, f"Kunwei value {index}") for index, value in enumerate(kunwei_values)
        )
        self.kunwei_run_values = tuple(
            _finite(value, f"Kunwei RUN value {index}")
            for index, value in enumerate(kunwei_run_values)
        )
        self.home_pose = tuple(
            _finite(value, f"home pose {index}") for index, value in enumerate(home_pose)
        )
        self.home_q = tuple(
            _finite(value, f"home joint {index}") for index, value in enumerate(home_q)
        )

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._services: dict[str, _TcpService] = {}
        self._started_at: float | None = None
        self._closed = False
        self._dashboard_state = "STOPPED"
        self._tp_state = READY_HOME
        self._tp_stage = 0.0
        self._terminal_reason = 0
        self._consumed_command_seq = 0
        self._active_identity = {
            "campaign_epoch": 0,
            "trial_id": 0,
            "candidate_token": 0,
            "execution_profile_id": 0,
            "batch_row_index": 0,
            "logical_batch_sequence": 0,
        }
        self._stage_started_at: float | None = None
        self._stage_gate_started_at: float | None = None
        self._stage_initial_heartbeat = 0.0
        self._stage_sensor_not_ready_seen = False
        self._run_started_at: float | None = None
        self._rtde_controller_tick = 0
        self._last_double_inputs = {index: 0.0 for index in range(24, 48)}
        self._events: list[dict[str, Any]] = []
        self._errors: list[dict[str, str]] = []
        self._evidence: dict[str, Any] = {
            "dashboard": {
                "connections": 0,
                "commands": Counter(),
                "play_transitions": 0,
                "stop_transitions": 0,
                "unknown_commands": 0,
            },
            "secondary": {"connections": 0, "banner_bytes_sent": 0},
            "kunwei": {
                "connections": 0,
                "start_commands": 0,
                "stop_commands": 0,
                "unknown_commands": 0,
                "frames_sent": 0,
                "bytes_sent": 0,
            },
            "rtde": {
                "connections": 0,
                "received_packets": Counter(),
                "sent_packets": Counter(),
                "output_setups": 0,
                "input_setups": 0,
                "starts": 0,
                "pauses": 0,
                "input_samples": 0,
                "input_decode_errors": 0,
                "output_samples": 0,
                "arm_acknowledgements": 0,
                "rejected_arm_packets": 0,
                "host_stop_requests": 0,
                "trials_completed": 0,
                "last_output_frequency_hz": None,
                "stage_samples": Counter(),
                "last_actual_tcp_pose": [],
                "last_actual_tcp_speed": [],
                "stage25_max_center_error_m": 0.0,
                "last_output_fields": [],
                "last_output_types": [],
                "last_input_fields": [],
                "last_input_types": [],
                "last_input_values": {},
            },
        }

    @property
    def content_binding(self) -> dict[str, Any]:
        return {
            "schema": ENDPOINT_CONTENT_SCHEMA,
            "transport": "localhost_tcp_endpoint_substitution_only",
            "host": self.host,
            "requested_ports": dict(self.requested_ports),
            "expected_program_id": self.expected_program_id,
            "loaded_program": self.loaded_program,
            "runtime_identity": self.runtime_identity.as_dict(),
            "trial_duration_s": self.trial_duration_s,
            "second_arm_policy": "acknowledge_run_and_hold",
            "rtde_frequency_limit_hz": self.rtde_frequency_limit_hz,
            "kunwei_frequency_hz": self.kunwei_frequency_hz,
            "kunwei_values": list(self.kunwei_values),
            "kunwei_run_values": list(self.kunwei_run_values),
            "kunwei_run_stages": sorted(_CONTACT_FORCE_STAGES),
            "home_pose": list(self.home_pose),
            "home_q": list(self.home_q),
            "precontact_tcp_pose": list(PRECONTACT_TCP_POSE),
            "stage25_contact_z_m": STAGE25_CONTACT_Z_M,
            "stage25_path": {
                "origin_xy_m": list(STAGE25_PATH_ORIGIN_XY_M),
                "u_along_xy": list(STAGE25_PATH_U_ALONG_XY),
                "p_lateral_xy": list(STAGE25_PATH_P_LATERAL_XY),
                "amplitude_m": STAGE25_PATH_AMPLITUDE_M,
                "omega_rad_s": STAGE25_PATH_OMEGA_RAD_S,
                "duration_s": STAGE25_PATH_DURATION_S,
            },
            "tp_stage_sequence": list(_TP_STAGE_SEQUENCE),
            "rtde_output_fields": list(PRODUCTION_RTDE_OUTPUT_FIELDS),
            "rtde_input_fields": list(PRODUCTION_RTDE_INPUT_FIELDS),
            "writes_bridge_readiness": False,
            "motion_capable": False,
        }

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.content_binding)).hexdigest()

    @property
    def addresses(self) -> dict[str, dict[str, Any]]:
        if not self._services:
            raise EndpointSimulatorError("qualification endpoints are not started")
        return {
            role: {"host": self.host, "port": service.port}
            for role, service in self._services.items()
        }

    @property
    def dashboard_port(self) -> int:
        return self.addresses["dashboard"]["port"]

    @property
    def rtde_port(self) -> int:
        return self.addresses["rtde"]["port"]

    @property
    def secondary_port(self) -> int:
        return self.addresses["secondary"]["port"]

    @property
    def kunwei_port(self) -> int:
        return self.addresses["kunwei"]["port"]

    def __enter__(self) -> "QualificationEndpointSimulator":
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.close()

    def start(self) -> "QualificationEndpointSimulator":
        if self._services or self._closed:
            raise EndpointSimulatorError("qualification endpoint simulator is one-shot")
        handlers = {
            "dashboard": self._handle_dashboard,
            "secondary": self._handle_secondary,
            "rtde": self._handle_rtde,
            "kunwei": self._handle_kunwei,
        }
        try:
            for role, handler in handlers.items():
                service = _TcpService(
                    role,
                    self.host,
                    self.requested_ports[role],
                    self._stop_event,
                    handler,
                    self._record_error,
                )
                service.start()
                self._services[role] = service
        except BaseException:
            self.close()
            raise
        self._started_at = time.monotonic()
        return self

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        for service in tuple(self._services.values()):
            service.close()

    def simulate_play(self) -> None:
        with self._lock:
            if self._started_at is None or self._closed:
                raise EndpointSimulatorError("qualification endpoints are not running")
            if self._dashboard_state != "PLAYING":
                self._dashboard_state = "PLAYING"
                self._evidence["dashboard"]["play_transitions"] += 1
                self._append_event("dashboard_playing")

    def simulate_stop(self) -> None:
        with self._lock:
            if self._started_at is None or self._closed:
                raise EndpointSimulatorError("qualification endpoints are not running")
            if self._dashboard_state != "STOPPED":
                self._dashboard_state = "STOPPED"
                self._evidence["dashboard"]["stop_transitions"] += 1
                self._append_event("dashboard_stopped")

    def wait_for_tp_state(
        self,
        state: int,
        *,
        timeout_s: float = 5.0,
        minimum_arm_count: int = 0,
    ) -> bool:
        deadline = time.monotonic() + _positive(timeout_s, "state wait timeout")
        while time.monotonic() < deadline:
            with self._lock:
                self._advance_tp(time.monotonic())
                if (
                    self._tp_state == state
                    and self._evidence["rtde"]["arm_acknowledgements"]
                    >= minimum_arm_count
                ):
                    return True
            time.sleep(0.002)
        return False

    def evidence(self) -> dict[str, Any]:
        with self._lock:
            self._advance_tp(time.monotonic())
            counters = copy.deepcopy(self._evidence)
            for role in ("dashboard", "rtde"):
                key = "commands" if role == "dashboard" else "received_packets"
                counters[role][key] = dict(counters[role][key])
            counters["rtde"]["sent_packets"] = dict(counters["rtde"]["sent_packets"])
            counters["rtde"]["stage_samples"] = dict(
                counters["rtde"]["stage_samples"]
            )
            return {
                "schema": ENDPOINT_EVIDENCE_SCHEMA,
                "content_sha256": self.content_sha256,
                "content": self.content_binding,
                "addresses": self.addresses if self._services else {},
                "alive": bool(self._services and not self._closed),
                "dashboard_state": self._dashboard_state,
                "tp": {
                    "state": self._tp_state,
                    "state_name": _TP_STATE_NAMES.get(self._tp_state, "UNKNOWN"),
                    "stage": self._tp_stage,
                    "terminal_reason": self._terminal_reason,
                    "consumed_command_seq": self._consumed_command_seq,
                    "active_identity": dict(self._active_identity),
                    "runtime_identity_registers": {
                        "35": self.runtime_identity.protocol_version,
                        "36": self.runtime_identity.digest_hi,
                        "37": self.runtime_identity.digest_lo,
                    },
                },
                "counters": counters,
                "events": copy.deepcopy(self._events),
                "errors": copy.deepcopy(self._errors),
            }

    def _append_event(self, event: str, **values: Any) -> None:
        if len(self._events) >= 64:
            self._events.pop(0)
        self._events.append(
            {
                "event": event,
                "monotonic_s": time.monotonic(),
                **values,
            }
        )

    def _record_error(self, role: str, error: BaseException) -> None:
        with self._lock:
            if len(self._errors) >= 32:
                self._errors.pop(0)
            self._errors.append(
                {"role": role, "error": f"{type(error).__name__}:{error}"}
            )

    def _handle_dashboard(self, connection: socket.socket, _address: tuple[str, int]) -> None:
        connection.settimeout(0.2)
        connection.sendall(b"Connected: Universal Robots Dashboard Server\n")
        with self._lock:
            self._evidence["dashboard"]["connections"] += 1
        buffered = bytearray()
        while not self._stop_event.is_set():
            try:
                chunk = connection.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                return
            buffered.extend(chunk)
            while b"\n" in buffered:
                raw, _, remainder = buffered.partition(b"\n")
                buffered[:] = remainder
                command = raw.decode("ascii", errors="replace").strip()
                if not command:
                    continue
                response = self._dashboard_response(command)
                connection.sendall((response + "\n").encode("ascii"))

    def _dashboard_response(self, command: str) -> str:
        with self._lock:
            commands: Counter[str] = self._evidence["dashboard"]["commands"]
            commands[command] += 1
            responses = {
                "PolyscopeVersion": "URSoftware 5.25.2.0",
                "get loaded program": f"Loaded program: {self.loaded_program}",
                "is in remote control": "true",
                "programState": self._dashboard_state,
                "robotmode": "Robotmode: RUNNING",
                "running": f"Program running: {str(self._dashboard_state == 'PLAYING').lower()}",
                "safetymode": "Safetymode: NORMAL",
            }
            if command not in responses:
                self._evidence["dashboard"]["unknown_commands"] += 1
                return f"Unknown command: {command}"
            return responses[command]

    def _handle_secondary(self, connection: socket.socket, _address: tuple[str, int]) -> None:
        banner = b"UR secondary client interface\n"
        connection.sendall(banner)
        with self._lock:
            self._evidence["secondary"]["connections"] += 1
            self._evidence["secondary"]["banner_bytes_sent"] += len(banner)

    def _handle_kunwei(self, connection: socket.socket, _address: tuple[str, int]) -> None:
        connection.settimeout(0.5)
        with self._lock:
            self._evidence["kunwei"]["connections"] += 1
        buffered = bytearray()
        streaming = False
        period = 1.0 / self.kunwei_frequency_hz
        next_frame = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            wait_s = min(0.05, max(0.0, next_frame - now)) if streaming else 0.05
            readable, _, _ = select.select([connection], [], [], wait_s)
            if readable:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                buffered.extend(chunk)
                while len(buffered) >= 4:
                    command = bytes(buffered[:4])
                    del buffered[:4]
                    with self._lock:
                        if command == START_STREAM:
                            self._evidence["kunwei"]["start_commands"] += 1
                            streaming = True
                            next_frame = time.monotonic()
                        elif command == STOP_STREAM:
                            self._evidence["kunwei"]["stop_commands"] += 1
                            streaming = False
                        else:
                            self._evidence["kunwei"]["unknown_commands"] += 1
            if streaming and time.monotonic() >= next_frame:
                with self._lock:
                    values = (
                        self.kunwei_run_values
                        if self._tp_state == RUN
                        and self._tp_stage in _CONTACT_FORCE_STAGES
                        else self.kunwei_values
                    )
                frame = b"\x48\xaa" + struct.pack("<6f", *values) + b"\r\n"
                connection.sendall(frame)
                with self._lock:
                    self._evidence["kunwei"]["frames_sent"] += 1
                    self._evidence["kunwei"]["bytes_sent"] += len(frame)
                next_frame = max(next_frame + period, time.monotonic())

    @staticmethod
    def _send_rtde_packet(connection: socket.socket, kind: str, payload: bytes = b"") -> None:
        connection.sendall(struct.pack("!HB", len(payload) + 3, ord(kind)) + payload)

    def _handle_rtde(self, connection: socket.socket, _address: tuple[str, int]) -> None:
        connection.settimeout(0.5)
        with self._lock:
            self._evidence["rtde"]["connections"] += 1
        buffered = bytearray()
        output_fields: tuple[str, ...] = ()
        output_types: tuple[str, ...] = ()
        input_fields: tuple[str, ...] = ()
        input_types: tuple[str, ...] = ()
        output_recipe_id = 1
        input_recipe_id = 2
        frequency_hz = 125.0
        started = False
        next_sample = time.monotonic()

        def sent(kind: str, payload: bytes = b"") -> None:
            self._send_rtde_packet(connection, kind, payload)
            with self._lock:
                packets: Counter[str] = self._evidence["rtde"]["sent_packets"]
                packets[kind] += 1

        while not self._stop_event.is_set():
            wait_s = 0.05
            if started:
                wait_s = min(wait_s, max(0.0, next_sample - time.monotonic()))
            readable, _, _ = select.select([connection], [], [], wait_s)
            if readable:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                buffered.extend(chunk)
                while len(buffered) >= 3:
                    size, kind_value = struct.unpack("!HB", buffered[:3])
                    if size < 3:
                        raise EndpointSimulatorError("RTDE frame size is smaller than its header")
                    if len(buffered) < size:
                        break
                    payload = bytes(buffered[3:size])
                    del buffered[:size]
                    kind = chr(kind_value)
                    with self._lock:
                        packets = self._evidence["rtde"]["received_packets"]
                        packets[kind] += 1
                    if kind == "V":
                        accepted = len(payload) == 2 and struct.unpack("!H", payload)[0] == 2
                        sent("V", bytes([int(accepted)]))
                    elif kind == "O":
                        if len(payload) < 8:
                            raise EndpointSimulatorError("RTDE output setup lacks frequency")
                        requested_hz = struct.unpack("!d", payload[:8])[0]
                        requested = tuple(
                            name for name in payload[8:].decode("ascii").split(",") if name
                        )
                        types = tuple(rtde_type_for_field(name) for name in requested)
                        valid = (
                            bool(requested)
                            and len(set(requested)) == len(requested)
                            and all(name != "NOT_FOUND" for name in types)
                            and math.isfinite(requested_hz)
                            and 0.0 < requested_hz <= self.rtde_frequency_limit_hz
                        )
                        if valid:
                            frequency_hz = requested_hz
                            output_fields, output_types = requested, types
                            recipe = output_recipe_id
                        else:
                            output_fields, output_types = (), ()
                            recipe = 0
                        sent("O", bytes([recipe]) + ",".join(types).encode("ascii"))
                        with self._lock:
                            self._evidence["rtde"]["output_setups"] += 1
                            self._evidence["rtde"]["last_output_frequency_hz"] = (
                                requested_hz
                            )
                            self._evidence["rtde"]["last_output_fields"] = list(requested)
                            self._evidence["rtde"]["last_output_types"] = list(types)
                    elif kind == "I":
                        requested = tuple(
                            name for name in payload.decode("ascii").split(",") if name
                        )
                        types = tuple(rtde_type_for_field(name) for name in requested)
                        valid = (
                            bool(requested)
                            and len(set(requested)) == len(requested)
                            and all(name != "NOT_FOUND" for name in types)
                        )
                        if valid:
                            input_fields, input_types = requested, types
                            recipe = input_recipe_id
                        else:
                            input_fields, input_types = (), ()
                            recipe = 0
                        sent("I", bytes([recipe]) + ",".join(types).encode("ascii"))
                        with self._lock:
                            self._evidence["rtde"]["input_setups"] += 1
                            self._evidence["rtde"]["last_input_fields"] = list(requested)
                            self._evidence["rtde"]["last_input_types"] = list(types)
                    elif kind == "S":
                        accepted = bool(output_fields)
                        sent("S", bytes([int(accepted)]))
                        started = accepted
                        next_sample = time.monotonic()
                        if accepted:
                            with self._lock:
                                self._evidence["rtde"]["starts"] += 1
                    elif kind == "U":
                        try:
                            if not input_fields or not payload or payload[0] != input_recipe_id:
                                raise EndpointSimulatorError("RTDE input recipe identity differs")
                            values = _decode_rtde_values(input_types, payload[1:])
                            self._consume_rtde_input(input_fields, values)
                        except EndpointSimulatorError:
                            with self._lock:
                                self._evidence["rtde"]["input_decode_errors"] += 1
                            raise
                    elif kind == "P":
                        started = False
                        sent("P", b"\x01")
                        with self._lock:
                            self._evidence["rtde"]["pauses"] += 1
                    else:
                        raise EndpointSimulatorError(f"unsupported RTDE packet {kind!r}")
            if started and time.monotonic() >= next_sample:
                values = self._rtde_output_values(output_fields, frequency_hz)
                payload = bytes([output_recipe_id]) + b"".join(
                    _pack_rtde_value(type_name, value)
                    for type_name, value in zip(output_types, values, strict=True)
                )
                sent("U", payload)
                with self._lock:
                    self._evidence["rtde"]["output_samples"] += 1
                next_sample = max(next_sample + 1.0 / frequency_hz, time.monotonic())

    def _consume_rtde_input(self, fields: Sequence[str], values: Sequence[Any]) -> None:
        row = dict(zip(fields, values, strict=True))
        with self._lock:
            self._evidence["rtde"]["input_samples"] += 1
            self._evidence["rtde"]["last_input_values"] = copy.deepcopy(row)
            for index in range(24, 48):
                name = f"input_double_register_{index}"
                if name in row:
                    self._last_double_inputs[index] = _finite(row[name], name)
            required = tuple(f"input_int_register_{index}" for index in range(24, 32))
            if not all(name in row for name in required):
                return
            integers: dict[int, int] = {}
            for index, name in zip(range(24, 32), required, strict=True):
                value = row[name]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise EndpointSimulatorError(f"{name} is not an INT32")
                integers[index] = value
            command = integers[26]
            sequence = integers[29]
            if command == 1 and sequence > self._consumed_command_seq:
                valid_identity = all(
                    integers[index] > 0 for index in (24, 25, 27, 28, 29, 30, 31)
                ) and integers[28] == 633 and 1 <= integers[30] <= 5
                ready = self._tp_state in {READY_HOME, READY_HOME_NEXT}
                if self._dashboard_state != "PLAYING" or not valid_identity or not ready:
                    self._evidence["rtde"]["rejected_arm_packets"] += 1
                    return
                self._active_identity = {
                    "campaign_epoch": integers[24],
                    "trial_id": integers[25],
                    "candidate_token": integers[27],
                    "execution_profile_id": integers[28],
                    "batch_row_index": integers[30],
                    "logical_batch_sequence": integers[31],
                }
                self._consumed_command_seq = sequence
                self._terminal_reason = 0
                self._tp_state = RUN
                self._run_started_at = None
                self._evidence["rtde"]["arm_acknowledgements"] += 1
                self._set_tp_stage(20.0, time.monotonic())
                self._append_event(
                    "tp_arm_acknowledged",
                    command_seq=sequence,
                    trial_id=integers[25],
                    arm_count=self._evidence["rtde"]["arm_acknowledgements"],
                )
            elif command == 3 and sequence > self._consumed_command_seq:
                self._consumed_command_seq = sequence
                self._terminal_reason = 4
                self._tp_state = FAULT
                self._tp_stage = 0.0
                self._run_started_at = None
                self._stage_started_at = None
                self._stage_gate_started_at = None
                self._append_event("tp_stop_fault", command_seq=sequence)
            elif command == 4 and sequence > self._consumed_command_seq:
                if self._tp_state != READY_HOME_NEXT:
                    return
                self._consumed_command_seq = sequence
                self._tp_state = READY_HOME_CLOSED
                self._tp_stage = 40.3
                self._append_event("tp_campaign_complete", command_seq=sequence)
            if self._tp_state == RUN and self._last_double_inputs[28] > 0.5:
                self._tp_state = READY_HOME_NEXT
                self._tp_stage = 40.3
                self._terminal_reason = 4
                self._run_started_at = None
                self._stage_started_at = None
                self._stage_gate_started_at = None
                self._evidence["rtde"]["host_stop_requests"] += 1
                self._append_event(
                    "tp_host_stop_request",
                    command_seq=self._consumed_command_seq,
                )
                return
            self._advance_tp(time.monotonic())

    def _set_tp_stage(self, stage: float, now: float) -> None:
        self._tp_stage = stage
        self._stage_started_at = now
        self._stage_gate_started_at = None
        self._stage_initial_heartbeat = self._last_double_inputs[26]
        self._stage_sensor_not_ready_seen = False
        if stage == 25.0:
            self._run_started_at = now
        self._append_event("tp_stage", stage=stage)

    def _advance_tp(self, now: float) -> None:
        if self._tp_state != RUN:
            return
        if self._tp_stage == 25.0:
            if (
                self._run_started_at is None
                or self._evidence["rtde"]["arm_acknowledgements"] != 1
                or now - self._run_started_at < self.trial_duration_s
            ):
                return
            self._tp_state = READY_HOME_NEXT
            self._tp_stage = 40.3
            self._terminal_reason = 1
            self._run_started_at = None
            self._stage_started_at = None
            self._stage_gate_started_at = None
            self._evidence["rtde"]["trials_completed"] += 1
            self._append_event(
                "tp_trial_completed",
                trial_id=self._active_identity["trial_id"],
                command_seq=self._consumed_command_seq,
            )
            return
        if self._stage_started_at is None or self._tp_stage not in _TP_STAGE_SEQUENCE:
            return
        if self._tp_stage == 23.0 and self._last_double_inputs[27] < 0.5:
            self._stage_sensor_not_ready_seen = True
        elapsed = now - self._stage_started_at
        minimum_dwell = _TP_STAGE_MINIMUM_DWELL_S.get(self._tp_stage, 0.0)
        if elapsed < minimum_dwell:
            return
        if self._tp_stage == 23.0:
            if self._last_double_inputs[27] < 0.5:
                return
            if (
                not self._stage_sensor_not_ready_seen
                or self._last_double_inputs[26] == self._stage_initial_heartbeat
            ):
                return
        if self._tp_stage == 24.2 and self._last_double_inputs[27] < 0.5:
            return
        if self._tp_stage == 25.05 and self._last_double_inputs[43] < 0.5:
            return
        if self._tp_stage == 25.3:
            valid = (
                self._last_double_inputs[27] >= 0.5
                and self._last_double_inputs[43] >= 0.5
                and abs(self._last_double_inputs[47] - 521.0) < 0.001
            )
            required_s = min(max(self._last_double_inputs[44], 0.0), 0.250)
            if not valid:
                self._stage_gate_started_at = None
                return
            if self._stage_gate_started_at is None:
                self._stage_gate_started_at = now
                return
            if now - self._stage_gate_started_at < required_s:
                return
        if self._tp_stage == 25.95:
            clear = (
                self._last_double_inputs[43] < 0.5
                and all(abs(self._last_double_inputs[index]) <= 0.0005 for index in range(37, 43))
                and all(
                    abs(self._last_double_inputs[47] - marker) >= 0.001
                    for marker in (521.0, 523.0, 524.0)
                )
            )
            if not clear:
                self._stage_gate_started_at = None
                return
            if self._stage_gate_started_at is None:
                self._stage_gate_started_at = now
                return
            if now - self._stage_gate_started_at < 0.006:
                return
        index = _TP_STAGE_SEQUENCE.index(self._tp_stage)
        if index + 1 < len(_TP_STAGE_SEQUENCE):
            self._set_tp_stage(_TP_STAGE_SEQUENCE[index + 1], now)

    def _tcp_observation(self, progress_s: float) -> tuple[tuple[float, ...], tuple[float, ...]]:
        if self._tp_state != RUN:
            return self.home_pose, (0.0,) * 6
        if self._tp_stage in {20.0}:
            return self.home_pose, (0.0,) * 6
        if self._tp_stage == 22.0:
            return (
                PRECONTACT_TCP_POSE[0],
                PRECONTACT_TCP_POSE[1],
                self.home_pose[2],
                *PRECONTACT_TCP_POSE[3:],
            ), (0.0,) * 6
        if self._tp_stage in {23.0, 24.0}:
            return PRECONTACT_TCP_POSE, (0.0,) * 6
        if self._tp_stage == 25.0:
            return stage25_reference(progress_s)
        return (
            STAGE25_PATH_ORIGIN_XY_M[0],
            STAGE25_PATH_ORIGIN_XY_M[1],
            STAGE25_CONTACT_Z_M,
            *PRECONTACT_TCP_POSE[3:],
        ), (0.0,) * 6

    def _rtde_output_values(
        self, fields: Sequence[str], frequency_hz: float
    ) -> list[Any]:
        with self._lock:
            now = time.monotonic()
            self._advance_tp(now)
            tick_step = max(1, round(500.0 / frequency_hz))
            self._rtde_controller_tick += tick_step
            timestamp = (
                now
                if self._started_at is None
                else max(0.0, now - self._started_at)
            )
            output_doubles = {index: 0.0 for index in range(24, 48)}
            for index in range(24, 30):
                output_doubles[index] = self._last_double_inputs[index]
            output_doubles[30] = float(self._terminal_reason)
            output_doubles[31] = self._last_double_inputs[44]
            output_doubles[32] = self._last_double_inputs[45]
            output_doubles[33] = self._last_double_inputs[39]
            output_doubles[34] = (
                1.0
                if self._tp_state == RUN and self._tp_stage not in {20.0, 22.0}
                else 0.0
            )
            if self._tp_state == RUN:
                output_doubles[35] = self._tp_stage
                for output_index, input_index in zip(range(36, 47), range(37, 48), strict=True):
                    output_doubles[output_index] = self._last_double_inputs[input_index]
                output_doubles[47] = (
                    1.0
                    if self._tp_stage == 25.0
                    and self._last_double_inputs[43] >= 0.5
                    and abs(self._last_double_inputs[47] - 524.0) < 0.001
                    else 0.0
                )
            elif self._tp_state in {READY_HOME_NEXT, READY_HOME_CLOSED}:
                output_doubles.update(
                    {
                        35: 40.3,
                        36: 0.0,
                        37: 0.0,
                        38: 0.0,
                        39: 3.0,
                        40: 0.0,
                        41: 0.0,
                        42: 0.0,
                        43: 0.0,
                        44: 0.002,
                    }
                )
            output_ints = {
                24: self._active_identity["campaign_epoch"],
                25: self._active_identity["trial_id"],
                26: self._tp_state,
                27: self._active_identity["candidate_token"],
                28: self._terminal_reason,
                29: self._active_identity["execution_profile_id"],
                30: self._consumed_command_seq,
                31: self._active_identity["batch_row_index"],
                32: 2 if self._active_identity["trial_id"] else 0,
                33: 0x7F if self._tp_state in {READY_HOME_NEXT, READY_HOME_CLOSED} else 0,
                34: self._active_identity["logical_batch_sequence"],
                35: self.runtime_identity.protocol_version,
                36: self.runtime_identity.digest_hi,
                37: self.runtime_identity.digest_lo,
            }
            pose, speed = self._tcp_observation(output_doubles[31])
            stage_key = format(output_doubles[35], "g")
            self._evidence["rtde"]["stage_samples"][stage_key] += 1
            self._evidence["rtde"]["last_actual_tcp_pose"] = list(pose)
            self._evidence["rtde"]["last_actual_tcp_speed"] = list(speed)
            if self._tp_state == RUN and self._tp_stage == 25.0:
                center, _ = stage25_reference(output_doubles[31])
                center_error = math.sqrt(
                    sum((pose[index] - center[index]) ** 2 for index in range(3))
                )
                self._evidence["rtde"]["stage25_max_center_error_m"] = max(
                    self._evidence["rtde"]["stage25_max_center_error_m"],
                    center_error,
                )
            base = {
                "timestamp": timestamp,
                "actual_TCP_pose": list(pose),
                "actual_TCP_speed": list(speed),
                "actual_TCP_force": [0.0] * 6,
                "actual_q": list(self.home_q),
                "actual_qd": [0.0] * 6,
                "runtime_state": 2 if self._dashboard_state == "PLAYING" else 1,
                "robot_mode": 7,
                "safety_mode": 1,
                "speed_scaling": 1.0,
                "payload": 0.0,
                "payload_cog": [0.0] * 3,
                "tcp_offset": [0.0] * 6,
            }
            return [
                base[field]
                if field in base
                else output_doubles[int(field.rsplit("_", 1)[-1])]
                if field.startswith("output_double_register_")
                else output_ints[int(field.rsplit("_", 1)[-1])]
                for field in fields
            ]


__all__ = [
    "ENDPOINT_CONTENT_SCHEMA",
    "ENDPOINT_EVIDENCE_SCHEMA",
    "EndpointSimulatorError",
    "PRODUCTION_RTDE_INPUT_FIELDS",
    "PRODUCTION_RTDE_OUTPUT_FIELDS",
    "PRECONTACT_TCP_POSE",
    "QualificationEndpointSimulator",
    "R010RuntimeIdentity",
    "READY_HOME",
    "READY_HOME_CLOSED",
    "READY_HOME_NEXT",
    "ROLLING_PROTOCOL_ID",
    "R010_PROGRAM_ID",
    "RUN",
    "START_STREAM",
    "STAGE25_CONTACT_Z_M",
    "STAGE25_PATH_DURATION_S",
    "STAGE25_PATH_ORIGIN_XY_M",
    "STOP_STREAM",
    "default_r010_runtime_identity",
    "rtde_type_for_field",
    "stage25_reference",
]
