#!/usr/bin/env python3
"""Single-owner Kunwei-to-RTDE V4 register live writer for Autotune r003."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import select
import signal
import socket
import statistics
import struct
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
KUNWEI_TOOLS = (
    REPOSITORY_ROOT / "experiments/sensor-integration/kunwei-kwr75b/tools"
)
sys.path.insert(0, str(KUNWEI_TOOLS))
sys.path.insert(0, str(ROOT / "tools"))

from capture_kunwei_kwr75_1khz import (  # noqa: E402
    FORCE_KG_TO_N,
    MOMENT_KG_M_TO_NM,
    START_STREAM,
    STOP_STREAM,
    parse_frame,
    pop_frames,
)
from step5d_autotune_v3.governance import read_proc_starttime_ticks  # noqa: E402
from step5d_autotune_v3.rtde_client import (  # noqa: E402
    RTDEClient,
    dashboard_exchange,
)
from step5d_autotune_v4.adapter import (  # noqa: E402
    AdapterTick,
    V4RuntimeAdapter,
    seal_attempt_ledger,
)
from step5d_autotune_v4.baseline_ledger import BaselineQualificationLedger  # noqa: E402
from step5d_autotune_v4.calibrated_runtime import (  # noqa: E402
    CalibratedRuntimeError,
    V4CalibratedRuntime,
)
from step5d_autotune_v4.contracts import (  # noqa: E402
    TARGET_FORCE_N,
    V4Candidate,
    V4Contract,
    assert_runtime_target,
    load_contract as load_v4_contract,
)
from step5d_autotune_v4.control import RuntimeObservation  # noqa: E402
from step5d_autotune_v4.path_controller import (  # noqa: E402
    CandidateTickLog,
    V4PathController,
    derive_force_terms,
)
from step5d_autotune_v4.replay import ReplayRow, replay as replay_attempt  # noqa: E402
from step5d_autotune_v4.wire import (  # noqa: E402
    DOUBLE_FIELDS,
    INTEGER_FIELDS,
    SensorPacket,
)
import step5d_bridge_authority as authority  # noqa: E402

try:
    from step5d_autotune_v4.live_attempt import (  # noqa: E402
        AttemptKind,
        V4LiveAttemptResult,
        V4LiveAttemptSpec,
        load_attempt_spec as _load_attempt_spec,
        restore_baseline_ledger_from_seal,
    )
except ImportError:
    # Expected import path: step5d_autotune_v4.live_attempt
    class AttemptKind(str, Enum):
        QUALIFICATION = "QUALIFICATION"
        PD_TRIAL = "PD_TRIAL"
        RETEST = "RETEST"

    @dataclass(frozen=True)
    class V4LiveAttemptSpec:
        attempt_id: str
        kind: AttemptKind
        candidate: V4Candidate
        v4_contract_path: Path
        v4_contract_sha256: str
        campaign_fingerprint: str
        eoat_sha256: str
        baseline_ledger_sha256: str | None = None

    @dataclass(frozen=True)
    class V4LiveAttemptResult:
        attempt_id: str
        ready: bool
        terminal: Mapping[str, Any] | None
        summary_path: Path
        tick_log_path: Path
        attempt_ledger_path: Path | None
        completion_path: Path | None

    def _load_attempt_spec(path: Path) -> V4LiveAttemptSpec:
        raise LiveWriterError(
            "step5d_autotune_v4.live_attempt is unavailable; use parse_attempt_spec"
        )


DEFAULT_CONTRACT = ROOT / "config/step5d/autotune_v4_live_writer_r003.json"
WRITER_SCHEMA = "step5d.autotune-v4/register-live-writer-v1"
ARTIFACT_ID = "autotune-v4-kunwei-register-live-writer-r003"
RUN_METADATA_SCHEMA = "step5d.autotune-v4/register-writer-run-v1"
SUMMARY_SCHEMA = "step5d.autotune-v4/register-writer-summary-v1"
COMPLETION_SCHEMA = "step5d.autotune-v4/completion-closure-v1"
READY_SCHEMA = "step5d.autotune-v4/register-writer-ready-v1"
TICK_LOG_SCHEMA = "step5d.autotune-v4/register-writer-tick-v1"
ATTEMPT_SPEC_SCHEMA = "step5d.autotune-v4/live-attempt-spec-v1"
INPUT_FIELDS = [*DOUBLE_FIELDS, *INTEGER_FIELDS]
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
    "output_double_register_26",
    "output_double_register_30",
    "output_double_register_35",
    "output_double_register_36",
)
RTDE_TYPES = {
    "DOUBLE": "d",
    "VECTOR3D": "3d",
    "VECTOR6D": "6d",
    "UINT32": "I",
    "UINT64": "Q",
    "INT32": "i",
    "BOOL": "?",
}
NORMAL_BASE = (0.0, 0.0, 1.0)
SEARCH_STAGES = frozenset({10, 20})
BASELINE_PATH_STAGES = frozenset({21, 25})


class LiveWriterError(RuntimeError):
    """The V4 register-live-writer contract or runtime is invalid."""


@dataclass(frozen=True)
class GuardLimits:
    max_abs_normal_n: float
    max_force_norm_n: float
    max_torque_norm_nm: float


@dataclass(frozen=True)
class WriterContract:
    path: Path
    sha256: str
    robot_host: str
    sensor_host: str
    sensor_port: int
    rtde_hz: float
    period_s: float
    baseline_s: float
    stale_s: float
    maximum_runtime_s: float
    terminal_exit_timeout_s: float
    search_guards: GuardLimits
    baseline_path_guards: GuardLimits
    payload_kg: float
    cog_m: tuple[float, float, float]
    tcp_offset: tuple[float, float, float, float, float, float]
    payload_tolerance: float
    cog_tolerance: float
    tcp_tolerance: float
    authority_root: Path
    resource_id: str
    active_stages: tuple[int, ...]
    terminal_stages: tuple[int, ...]
    v4_release_default_path: Path
    program_basename: str
    controller_target: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LiveWriterError(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise LiveWriterError(f"{role} must be finite")
    return result


def _vector(value: Any, size: int, role: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise LiveWriterError(f"{role} must contain exactly {size} values")
    return tuple(_finite(item, role) for item in value)


def _integer_tuple(value: Any, role: str) -> tuple[int, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
    ):
        raise LiveWriterError(f"{role} must be a non-empty integer list")
    return tuple(value)


def _guard_limits(value: Any, role: str) -> GuardLimits:
    if not isinstance(value, dict):
        raise LiveWriterError(f"{role} must be an object")
    return GuardLimits(
        max_abs_normal_n=_finite(value.get("absolute_normal_load_n"), f"{role}.normal"),
        max_force_norm_n=_finite(value.get("force_norm_n"), f"{role}.force_norm"),
        max_torque_norm_nm=_finite(value.get("torque_norm_nm"), f"{role}.torque"),
    )


def load_contract(path: Path = DEFAULT_CONTRACT) -> WriterContract:
    if path.is_symlink() or not path.is_file():
        raise LiveWriterError(f"writer contract must be a regular file: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LiveWriterError(f"writer contract is unreadable: {exc}") from exc
    if document.get("schema") != WRITER_SCHEMA or document.get("artifact_id") != ARTIFACT_ID:
        raise LiveWriterError("writer contract identity differs")
    endpoints = document.get("endpoints")
    timing = document.get("timing")
    mapping = document.get("force_mapping")
    guards = document.get("hard_guards")
    eoat = document.get("eoat_controller_get")
    lease = document.get("lease")
    terminal = document.get("terminal")
    release = document.get("v4_release")
    program = document.get("program")
    if not all(
        isinstance(item, dict)
        for item in (
            endpoints,
            timing,
            mapping,
            guards,
            eoat,
            lease,
            terminal,
            release,
            program,
        )
    ):
        raise LiveWriterError("writer contract sections are missing")
    if mapping != {
        "normal_axis": "fz",
        "normal_sign": -1.0,
        "software_baseline_only": True,
        "hardware_zero_or_tare": False,
    }:
        raise LiveWriterError("Kunwei force mapping differs")
    if eoat.get("ack_input_register") != 29:
        raise LiveWriterError("EOAT acknowledgement register differs")
    sensor_port = endpoints.get("kunwei_port")
    if not isinstance(sensor_port, int) or isinstance(sensor_port, bool):
        raise LiveWriterError("Kunwei port must be an integer")
    contract = WriterContract(
        path=path,
        sha256=_sha256(path),
        robot_host=str(endpoints.get("robot_host")),
        sensor_host=str(endpoints.get("kunwei_host")),
        sensor_port=sensor_port,
        rtde_hz=_finite(timing.get("rtde_hz"), "RTDE rate"),
        period_s=_finite(timing.get("publish_period_s"), "publish period"),
        baseline_s=_finite(timing.get("baseline_s"), "baseline duration"),
        stale_s=_finite(timing.get("sensor_stale_s"), "sensor stale limit"),
        maximum_runtime_s=_finite(timing.get("maximum_runtime_s"), "maximum runtime"),
        terminal_exit_timeout_s=_finite(
            timing.get("terminal_exit_timeout_s"), "terminal exit timeout"
        ),
        search_guards=_guard_limits(guards.get("search"), "search guards"),
        baseline_path_guards=_guard_limits(
            guards.get("baseline_path"), "baseline/path guards"
        ),
        payload_kg=_finite(eoat.get("payload_kg"), "payload"),
        cog_m=tuple(_vector(eoat.get("payload_cog_m"), 3, "CoG")),  # type: ignore[arg-type]
        tcp_offset=tuple(_vector(eoat.get("tcp_offset_m_rad"), 6, "TCP")),  # type: ignore[arg-type]
        payload_tolerance=_finite(eoat.get("payload_tolerance_kg"), "payload tolerance"),
        cog_tolerance=_finite(eoat.get("cog_tolerance_m"), "CoG tolerance"),
        tcp_tolerance=_finite(eoat.get("tcp_tolerance_m_rad"), "TCP tolerance"),
        authority_root=Path(str(lease.get("authority_root"))),
        resource_id=str(lease.get("resource_id")),
        active_stages=_integer_tuple(terminal.get("active_stages"), "active stages"),
        terminal_stages=_integer_tuple(terminal.get("terminal_stages"), "terminal stages"),
        v4_release_default_path=ROOT / str(release.get("default_path")),
        program_basename=str(program.get("basename")),
        controller_target=str(program.get("controller_target")),
    )
    expected = {
        "robot_host": "192.168.1.18",
        "sensor_host": "192.168.50.25",
        "sensor_port": 5152,
        "rtde_hz": 125.0,
        "period_s": 0.008,
        "baseline_s": 5.0,
        "stale_s": 0.08,
        "maximum_runtime_s": 240.0,
        "terminal_exit_timeout_s": 2.0,
        "payload_kg": 0.413,
        "cog_m": (0.0011, 0.0031, 0.0163),
        "tcp_offset": (0.0, 0.0, 0.0874, 0.0, 0.0, 0.0),
        "resource_id": "step5d-bridge-writer",
        "active_stages": (10, 20, 21, 25),
        "terminal_stages": (80, 90),
        "program_basename": "step5d_strict_rnn_autotune_v4_r003",
        "controller_target": (
            "/programs/andyl/kunwei/step5/"
            "step5d_strict_rnn_autotune_v4_r003.urp"
        ),
    }
    mismatches = {
        name: {"expected": expected_value, "actual": getattr(contract, name)}
        for name, expected_value in expected.items()
        if getattr(contract, name) != expected_value
    }
    if mismatches:
        raise LiveWriterError(f"writer fixed invariants differ: {mismatches}")
    return contract


def _candidate_from_mapping(value: Mapping[str, Any]) -> V4Candidate:
    if not isinstance(value, Mapping):
        raise LiveWriterError("candidate must be an object")
    return V4Candidate(
        force_p_gain=_finite(value.get("force_p_gain"), "force_p_gain"),
        force_i_gain=_finite(value.get("force_i_gain"), "force_i_gain"),
        force_damping=_finite(value.get("force_damping"), "force_damping"),
        normal_filter_tau_s=_finite(
            value.get("normal_filter_tau_s"), "normal_filter_tau_s"
        ),
        orientation_ko=_finite(value.get("orientation_ko"), "orientation_ko"),
        motion_kp=_finite(value.get("motion_kp"), "motion_kp"),
        target_force_n=_finite(value.get("target_force_n", TARGET_FORCE_N), "target_force_n"),
    )


def parse_attempt_spec(path: Path) -> V4LiveAttemptSpec:
    if path.is_symlink() or not path.is_file():
        raise LiveWriterError(f"attempt spec must be a regular file: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LiveWriterError(f"attempt spec is unreadable: {exc}") from exc
    if document.get("schema") != ATTEMPT_SPEC_SCHEMA:
        raise LiveWriterError("attempt spec schema differs")
    attempt_id = document.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id:
        raise LiveWriterError("attempt_id is required")
    kind_raw = document.get("kind")
    try:
        kind = AttemptKind(str(kind_raw))
    except ValueError as exc:
        raise LiveWriterError(f"attempt kind is invalid: {kind_raw}") from exc
    candidate = _candidate_from_mapping(document.get("candidate", {}))
    assert_runtime_target(candidate, TARGET_FORCE_N)
    v4_path_text = document.get("v4_contract_path")
    if not isinstance(v4_path_text, str) or not v4_path_text:
        v4_contract_path = load_v4_contract().path
    else:
        v4_contract_path = Path(v4_path_text)
    if not v4_contract_path.is_absolute():
        v4_contract_path = ROOT / v4_contract_path
    v4_contract_sha256 = document.get(
        "v4_contract_sha256", document.get("contract_sha256")
    )
    if (
        not isinstance(v4_contract_sha256, str)
        or len(v4_contract_sha256) != 64
    ):
        raise LiveWriterError("v4_contract_sha256 is required")
    if _sha256(v4_contract_path) != v4_contract_sha256:
        raise LiveWriterError("v4 contract SHA-256 binding differs")
    campaign_fingerprint = document.get("campaign_fingerprint")
    eoat_sha256 = document.get("eoat_sha256")
    for role, value in (
        ("campaign_fingerprint", campaign_fingerprint),
        ("eoat_sha256", eoat_sha256),
    ):
        if not isinstance(value, str) or len(value) != 64:
            raise LiveWriterError(f"{role} is required")
    input_baseline_ledger_sha256 = document.get(
        "input_baseline_ledger_sha256", document.get("baseline_ledger_sha256")
    )
    if (
        not isinstance(input_baseline_ledger_sha256, str)
        or len(input_baseline_ledger_sha256) != 64
    ):
        raise LiveWriterError("input_baseline_ledger_sha256 is required")
    return V4LiveAttemptSpec(
        attempt_id=attempt_id,
        kind=kind,
        candidate=candidate,
        contract_sha256=v4_contract_sha256,
        campaign_fingerprint=campaign_fingerprint,
        eoat_sha256=eoat_sha256,
        model_hashes=dict(document.get("model_hashes", {})),
        triplet_sha256=dict(document.get("triplet_sha256", {})),
        input_baseline_ledger_sha256=input_baseline_ledger_sha256,
        path_requested=document.get("path_requested", False),
        v4_contract_path=v4_contract_path,
        baseline_ledger_seal=document.get("baseline_ledger_seal"),
    )


def load_attempt_spec(path: Path) -> V4LiveAttemptSpec:
    return _load_attempt_spec(path)


def controller_get_matches(contract: WriterContract, output: dict[str, Any]) -> bool:
    try:
        payload = float(output["payload"])
        cog = tuple(float(value) for value in output["payload_cog"])
        tcp = tuple(float(value) for value in output["tcp_offset"])
    except (KeyError, TypeError, ValueError):
        return False
    if not all(math.isfinite(value) for value in (payload, *cog, *tcp)):
        return False
    return (
        abs(payload - contract.payload_kg) <= contract.payload_tolerance
        and len(cog) == 3
        and all(
            abs(actual - expected) <= contract.cog_tolerance
            for actual, expected in zip(cog, contract.cog_m, strict=True)
        )
        and len(tcp) == 6
        and all(
            abs(actual - expected) <= contract.tcp_tolerance
            for actual, expected in zip(tcp, contract.tcp_offset, strict=True)
        )
    )


def zeroed_wrench(
    raw_values: Sequence[float], baseline: Sequence[float]
) -> tuple[float, float, float, float, float, float]:
    if len(raw_values) != 6 or len(baseline) != 6:
        raise LiveWriterError("Kunwei wrench and baseline must be 6D")
    converted = (
        float(raw_values[0]) * FORCE_KG_TO_N,
        float(raw_values[1]) * FORCE_KG_TO_N,
        float(raw_values[2]) * FORCE_KG_TO_N,
        float(raw_values[3]) * MOMENT_KG_M_TO_NM,
        float(raw_values[4]) * MOMENT_KG_M_TO_NM,
        float(raw_values[5]) * MOMENT_KG_M_TO_NM,
    )
    result = tuple(
        actual - float(offset)
        for actual, offset in zip(converted, baseline, strict=True)
    )
    if not all(math.isfinite(value) for value in result):
        raise LiveWriterError("Kunwei zeroed wrench is nonfinite")
    return result  # type: ignore[return-value]


def guard_limits_for_stage(contract: WriterContract, stage: int) -> GuardLimits:
    if stage in BASELINE_PATH_STAGES:
        return contract.baseline_path_guards
    return contract.search_guards


def evaluate_hard_guard(
    limits: GuardLimits,
    *,
    normal_load: float,
    force_norm: float,
    torque_norm: float,
    sensor_fresh: bool,
) -> str | None:
    if not sensor_fresh:
        return "sensor_stale_or_unready"
    if abs(normal_load) >= limits.max_abs_normal_n:
        return "hard_abs_normal"
    if force_norm >= limits.max_force_norm_n:
        return "hard_force_norm"
    if torque_norm >= limits.max_torque_norm_nm:
        return "hard_torque_norm"
    return None


def path_mode_for_stage(stage: int) -> str:
    if stage in {21}:
        return "baseline"
    if stage in {25}:
        return "path"
    return "hold"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


class WritableRTDEClient(RTDEClient):
    """Minimal RTDE v2 input writer with nonblocking latest-output reads."""

    def setup_inputs(self, fields: Sequence[str]) -> tuple[int, list[str]]:
        self._send_packet("I", ",".join(fields).encode())
        packet_type, payload = self._recv_packet()
        if packet_type != ord("I") or not payload:
            raise LiveWriterError("RTDE input setup failed")
        recipe_id = payload[0]
        types = payload[1:].decode("ascii", errors="replace").split(",")
        if (
            recipe_id == 0
            or len(types) != len(fields)
            or any(value == "NOT_FOUND" for value in types)
        ):
            raise LiveWriterError("RTDE input recipe is invalid")
        return recipe_id, types

    def send_input_sample(
        self,
        recipe_id: int,
        type_names: Sequence[str],
        values: Sequence[Any],
    ) -> None:
        if len(type_names) != len(values):
            raise LiveWriterError("RTDE input type/value count differs")
        payload = bytearray([recipe_id])
        for type_name, value in zip(type_names, values, strict=True):
            fmt = RTDE_TYPES.get(type_name)
            if fmt is None:
                raise LiveWriterError(f"unsupported RTDE input type {type_name}")
            if fmt.endswith("d") and fmt != "d":
                payload.extend(struct.pack("!" + fmt, *value))
            else:
                payload.extend(struct.pack("!" + fmt, value))
        self._send_packet("U", bytes(payload))

    def recv_latest_sample(
        self,
        recipe_id: int,
        type_names: Sequence[str],
        fields: Sequence[str],
    ) -> dict[str, Any] | None:
        if self.sock is None:
            raise LiveWriterError("RTDE socket is unavailable")
        latest: dict[str, Any] | None = None
        while select.select([self.sock], [], [], 0.0)[0]:
            packet_type, payload = self._recv_packet()
            if packet_type != ord("U") or not payload or payload[0] != recipe_id:
                continue
            cursor = 1
            values: list[Any] = []
            for type_name in type_names:
                fmt = RTDE_TYPES.get(type_name)
                if fmt is None:
                    raise LiveWriterError(f"unsupported RTDE output type {type_name}")
                width = struct.calcsize("!" + fmt)
                unpacked = struct.unpack("!" + fmt, payload[cursor : cursor + width])
                cursor += width
                values.append(unpacked[0] if len(unpacked) == 1 else list(unpacked))
            if cursor != len(payload):
                raise LiveWriterError("RTDE output sample has trailing bytes")
            latest = dict(zip(fields, values, strict=True))
        return latest


class KunweiTransport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def poll(self) -> tuple[list[tuple[float, float, float, float, float, float]], int]: ...


class RTDETransport(Protocol):
    def open(self) -> tuple[int, list[str], int, list[str]]: ...
    def close(self) -> None: ...
    def poll_output(self) -> dict[str, Any]: ...
    def send_packet(
        self,
        double_values: Sequence[float],
        integer_values: Sequence[int],
    ) -> None: ...


class LiveKunweiTransport:
    def __init__(self, contract: WriterContract) -> None:
        self.contract = contract
        self.sock: socket.socket | None = None
        self.buffer = bytearray()
        self.dropped_bytes = 0
        self.parse_errors = 0
        self.sensor_samples = 0
        self.latest_raw: tuple[float, float, float, float, float, float] | None = None
        self.latest_mono: float | None = None
        self.raw_handle: Any = None

    def open(self) -> None:
        self.sock = socket.create_connection(
            (self.contract.sensor_host, self.contract.sensor_port), timeout=3.0
        )
        self.sock.setblocking(False)
        self.sock.sendall(START_STREAM)

    def attach_raw_sink(self, handle: Any) -> None:
        self.raw_handle = handle

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.sendall(STOP_STREAM)
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def poll(self) -> tuple[list[tuple[float, float, float, float, float, float]], int]:
        frames: list[tuple[float, float, float, float, float, float]] = []
        if self.sock is None:
            return frames, 0
        ready = select.select([self.sock], [], [], 0.0)[0]
        if self.sock not in ready:
            return frames, 0
        try:
            chunk = self.sock.recv(8192)
        except BlockingIOError:
            chunk = b""
        if not chunk:
            return frames, 0
        self.buffer.extend(chunk)
        parsed_frames, dropped = pop_frames(self.buffer, 0x48)
        self.dropped_bytes += dropped
        for frame in parsed_frames:
            try:
                parsed = parse_frame(frame)
            except ValueError:
                self.parse_errors += 1
                continue
            if not all(math.isfinite(value) for value in parsed):
                self.parse_errors += 1
                continue
            self.latest_raw = parsed
            self.latest_mono = time.monotonic()
            self.sensor_samples += 1
            frames.append(parsed)
            if self.raw_handle is not None:
                self.raw_handle.write(frame)
        return frames, len(frames)


class LiveRTDETransport:
    def __init__(self, contract: WriterContract) -> None:
        self.contract = contract
        self.client: WritableRTDEClient | None = None
        self.input_recipe = 0
        self.input_types: list[str] = []
        self.output_recipe = 0
        self.output_types: list[str] = []
        self.latest_output: dict[str, Any] = {}

    def open(self) -> tuple[int, list[str], int, list[str]]:
        self.client = WritableRTDEClient(self.contract.robot_host, timeout=3.0)
        self.client.__enter__()
        try:
            self.client.negotiate()
            self.output_recipe, self.output_types = self.client.setup_outputs(
                self.contract.rtde_hz, OUTPUT_FIELDS
            )
            self.input_recipe, self.input_types = self.client.setup_inputs(INPUT_FIELDS)
            self.client.start()
        except Exception:
            self.client.__exit__(None, None, None)
            self.client = None
            raise
        return (
            self.input_recipe,
            self.input_types,
            self.output_recipe,
            self.output_types,
        )

    def close(self) -> None:
        if self.client is not None:
            self.client.__exit__(None, None, None)
            self.client = None

    def poll_output(self) -> dict[str, Any]:
        if self.client is None or self.client.sock is None:
            return self.latest_output
        ready = select.select([self.client.sock], [], [], 0.0)[0]
        if self.client.sock in ready:
            observed = self.client.recv_latest_sample(
                self.output_recipe, self.output_types, OUTPUT_FIELDS
            )
            if observed is not None:
                self.latest_output = observed
        return self.latest_output

    def send_packet(
        self,
        double_values: Sequence[float],
        integer_values: Sequence[int],
    ) -> None:
        if self.client is None:
            raise LiveWriterError("RTDE transport was lost")
        values: list[Any] = [float(value) for value in double_values]
        values.extend((int(integer_values[0]), int(integer_values[1])))
        self.client.send_input_sample(self.input_recipe, self.input_types, values)


class FakeKunweiTransport:
    """Deterministic Kunwei transport for dry-run and unit tests."""

    def __init__(
        self,
        *,
        wrench: Sequence[float] = (0.0, 0.0, -1.0, 0.0, 0.0, 0.0),
        frames_per_poll: int = 1,
    ) -> None:
        self.wrench = tuple(float(value) for value in wrench)
        self.frames_per_poll = frames_per_poll
        self.opened = False
        self.closed = False
        self.sensor_samples = 0
        self.parse_errors = 0
        self.dropped_bytes = 0
        self.latest_raw = self.wrench
        self.latest_mono: float | None = None

    def open(self) -> None:
        self.opened = True
        self.latest_mono = time.monotonic()

    def close(self) -> None:
        self.closed = True

    def poll(self) -> tuple[list[tuple[float, float, float, float, float, float]], int]:
        if not self.opened:
            return [], 0
        self.latest_mono = time.monotonic()
        frames = [self.wrench for _ in range(self.frames_per_poll)]
        self.sensor_samples += len(frames)
        self.latest_raw = self.wrench
        return frames, len(frames)


class FakeRTDETransport:
    """Simulates TP echo stages and EOAT GET without network."""

    def __init__(self, contract: WriterContract) -> None:
        self.contract = contract
        self.opened = False
        self.closed = False
        self.stage = 10.0
        self.reason = 0.0
        self.sent_heartbeat = 0.0
        self.write_count = 0
        self.latest_output: dict[str, Any] = self._output()

    def _output(self) -> dict[str, Any]:
        return {
            "timestamp": time.time(),
            "payload": self.contract.payload_kg,
            "payload_cog": list(self.contract.cog_m),
            "tcp_offset": list(self.contract.tcp_offset),
            "actual_TCP_speed": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "actual_TCP_pose": [
                0.487834547,
                0.129337053,
                0.008044839,
                3.120752062,
                0.0,
                0.068626833,
            ],
            "actual_q": [0.0, -1.2, 1.8, -2.1, -1.57, 0.0],
            "actual_qd": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "safety_mode": 1,
            "robot_mode": 7,
            "runtime_state": 2,
            "output_double_register_26": self.sent_heartbeat,
            "output_double_register_30": self.reason,
            "output_double_register_35": self.stage,
            "output_double_register_36": 0.0,
        }

    def open(self) -> tuple[int, list[str], int, list[str]]:
        self.opened = True
        return 1, ["DOUBLE"] * len(INPUT_FIELDS), 2, ["DOUBLE"] * len(OUTPUT_FIELDS)

    def close(self) -> None:
        self.closed = True

    def poll_output(self) -> dict[str, Any]:
        return self.latest_output

    def send_packet(
        self,
        double_values: Sequence[float],
        integer_values: Sequence[int],
    ) -> None:
        del integer_values
        self.write_count += 1
        heartbeat = float(double_values[2])
        if math.isfinite(heartbeat):
            self.sent_heartbeat = heartbeat
        if self.stage == 10.0 and self.write_count >= 40:
            self.stage = 20.0
        elif self.stage == 20.0 and self.write_count >= 80:
            self.stage = 21.0
        elif self.stage == 21.0 and self.write_count >= 160:
            self.stage = 25.0
        elif self.stage == 25.0 and self.write_count >= 240:
            self.stage = 80.0
            self.reason = 32.0
        self.latest_output = self._output()


def _dashboard_preflight(contract: WriterContract) -> dict[str, str]:
    dashboard = dashboard_exchange(
        contract.robot_host,
        (
            "is in remote control",
            "safetymode",
            "robotmode",
            "running",
            "programState",
            "get loaded program",
        ),
        timeout=3.0,
    )
    if dashboard.get("is in remote control", "").strip().lower() != "true":
        raise LiveWriterError("Dashboard Remote Control gate failed")
    if "NORMAL" not in dashboard.get("safetymode", ""):
        raise LiveWriterError("Dashboard Safety NORMAL gate failed")
    if "RUNNING" not in dashboard.get("robotmode", ""):
        raise LiveWriterError("Dashboard robot RUNNING gate failed")
    loaded_response = dashboard.get("get loaded program", "").strip()
    loaded_target = loaded_response.split(":", 1)[-1].strip()
    if loaded_target != contract.controller_target:
        raise LiveWriterError("Dashboard loaded program identity differs")
    return dashboard


def _stationary_from_output(output: Mapping[str, Any]) -> bool:
    speed = output.get("actual_TCP_speed")
    if not isinstance(speed, list) or len(speed) != 6:
        return False
    try:
        linear = math.sqrt(sum(float(value) ** 2 for value in speed[:3]))
        angular = math.sqrt(sum(float(value) ** 2 for value in speed[3:]))
    except (TypeError, ValueError):
        return False
    return linear <= 0.0005 and angular <= 0.005


def _restore_baseline_ledger(
    v4_contract: V4Contract,
    attempt_spec: V4LiveAttemptSpec,
) -> BaselineQualificationLedger:
    ledger = BaselineQualificationLedger(v4_contract)
    sealed = getattr(attempt_spec, "baseline_ledger_seal", None)
    if not isinstance(sealed, Mapping):
        return ledger
    return restore_baseline_ledger_from_seal(v4_contract, sealed)


def _tick_log_row(
    *,
    write_index: int,
    now: float,
    sensor_age: float,
    tick_log: CandidateTickLog,
    packet: Mapping[str, Any],
    guard_reason: str,
    latest_output: Mapping[str, Any],
    stage: float,
    reason: float,
    echo: float,
    command_sequence: int,
    raw_normal_n: float,
    force_norm_n: float,
    torque_norm_nm: float,
    path_time_s: float,
    sensor_fresh: bool,
) -> dict[str, Any]:
    return {
        "schema": TICK_LOG_SCHEMA,
        "write_index": write_index,
        "t_monotonic_s": now,
        "sensor_age_s": sensor_age if math.isfinite(sensor_age) else None,
        "command_sequence": command_sequence,
        "force_p_gain": tick_log.force_p_gain,
        "force_i_gain": tick_log.force_i_gain,
        "force_damping": tick_log.force_damping,
        "normal_filter_tau_s": tick_log.normal_filter_tau_s,
        "orientation_ko": tick_log.orientation_ko,
        "motion_kp": tick_log.motion_kp,
        "target_force_n": tick_log.target_force_n,
        "Md": tick_log.Md,
        "kf": tick_log.kf,
        "Bd": tick_log.Bd,
        "filter_alpha": tick_log.filter_alpha,
        "actual_dt_s": tick_log.actual_dt_s,
        "filtered_normal_n": tick_log.filtered_normal_n,
        "raw_normal_n": raw_normal_n,
        "force_norm_n": force_norm_n,
        "torque_norm_nm": torque_norm_nm,
        "sensor_fresh": sensor_fresh,
        "path_time_s": path_time_s,
        "proposed_qdot": list(tick_log.proposed_qdot),
        "mode": tick_log.mode,
        "packet_stop_dominant": packet.get("stop_dominant"),
        "packet_reason": packet.get("reason"),
        "guard_reason": guard_reason,
        "ur_payload": latest_output.get("payload"),
        "ur_safety_mode": latest_output.get("safety_mode"),
        "ur_robot_mode": latest_output.get("robot_mode"),
        "ur_runtime_state": latest_output.get("runtime_state"),
        "ur_echo_heartbeat": latest_output.get("output_double_register_26"),
        "ur_stage": stage,
        "ur_reason": reason,
        "ur_echo_delta": (
            None
            if not math.isfinite(echo)
            else abs(echo - float(packet.get("heartbeat", 0.0)))
        ),
    }


def run_writer(
    contract: WriterContract,
    attempt_spec: V4LiveAttemptSpec,
    *,
    output_dir: Path,
    kunwei_transport: KunweiTransport | None = None,
    rtde_transport: RTDETransport | None = None,
    skip_dashboard: bool = False,
    skip_lease: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    contract_copy = output_dir / contract.path.name
    contract_copy.write_bytes(contract.path.read_bytes())
    v4_contract = load_v4_contract(attempt_spec.v4_contract_path)
    if v4_contract.sha256 != attempt_spec.v4_contract_sha256:
        raise LiveWriterError("attempt spec V4 contract SHA differs")
    if v4_contract.campaign_fingerprint != attempt_spec.campaign_fingerprint:
        raise LiveWriterError("attempt spec campaign fingerprint differs")
    if v4_contract.eoat_sha256 != attempt_spec.eoat_sha256:
        raise LiveWriterError("attempt spec EOAT SHA differs")
    if dict(attempt_spec.model_hashes) != dict(v4_contract.model_hashes):
        raise LiveWriterError("attempt spec robot-model binding differs")
    expected_triplet = {
        suffix: _sha256(
            ROOT
            / "programs/step5/step5d"
            / f"{contract.program_basename}.{suffix}"
        )
        for suffix in ("urp", "script", "txt")
    }
    if dict(attempt_spec.triplet_sha256) != expected_triplet:
        raise LiveWriterError("attempt spec triplet binding differs")
    dashboard: dict[str, str] = {}
    if not skip_dashboard:
        dashboard = _dashboard_preflight(contract)
    parent_pid = os.getppid()
    parent_starttime = read_proc_starttime_ticks(parent_pid)
    if parent_starttime is None and not skip_lease:
        raise LiveWriterError("writer parent process identity is unavailable")
    lease: Mapping[str, Any] = {}
    lease_active = False
    if not skip_lease:
        lease = authority.begin(
            contract.authority_root,
            attempt_spec.attempt_id,
            parent_pid,
            parent_starttime,
            worktree_root=str(REPOSITORY_ROOT),
            launch_basis_path=str(contract.path),
            launch_basis_sha256=contract.sha256,
            resource_id=contract.resource_id,
        )
        lease_active = True
    kunwei = kunwei_transport or LiveKunweiTransport(contract)
    rtde = rtde_transport or LiveRTDETransport(contract)
    path_controller = V4PathController(attempt_spec.candidate)
    calibrated_runtime = V4CalibratedRuntime(
        v4_contract, attempt_spec.candidate
    )
    baseline_ledger = _restore_baseline_ledger(v4_contract, attempt_spec)
    adapter = V4RuntimeAdapter(
        v4_contract,
        attempt_spec.candidate,
        baseline_ledger=baseline_ledger,
        attempt_id=attempt_spec.attempt_id,
        path_requested=attempt_spec.path_requested,
    )
    completion_reason = "failed"
    terminal: dict[str, Any] | None = None
    rows = 0
    heartbeat = 0.0
    sent_heartbeat = 0.0
    maximum_publish_gap_s = 0.0
    last_publish_mono: float | None = None
    command_sequence = 0
    outer_setpoint_n = 1.0
    one_newton_latched = False
    stop_signal: dict[str, str | None] = {"value": None}
    ledger_rows: list[Mapping[str, object]] = []
    tick_lines: list[str] = []
    replay_rows: list[ReplayRow] = []
    ready_written = False
    ready_payload: dict[str, Any] = {}

    def signal_handler(signum: int, _frame: Any) -> None:
        stop_signal["value"] = signal.Signals(signum).name

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    metadata = {
        "schema": RUN_METADATA_SCHEMA,
        "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "attempt_id": attempt_spec.attempt_id,
        "attempt_kind": attempt_spec.kind.value,
        "candidate_uid": attempt_spec.candidate.candidate_uid,
        "contract_sha256": contract.sha256,
        "v4_contract_sha256": attempt_spec.v4_contract_sha256,
        "campaign_fingerprint": attempt_spec.campaign_fingerprint,
        "dashboard_preflight": dashboard,
        "lease": lease,
        "safety_boundary": [
            "sole RTDE input-register writer under canonical lease",
            "Kunwei START_STREAM and STOP_STREAM only",
            "software baseline only; no zero/tare/configuration write",
            "search publishes sensor only; TP owns contact acquisition",
            "baseline/path hard guards 15/20/1; search guards 3/3/0.2",
            "no URScript, Dashboard writes, robot motion, TCP, or payload writes",
        ],
    }
    _atomic_json(output_dir / "metadata.json", metadata)
    csv_path = output_dir / "register_writer_125hz.csv"
    raw_path = output_dir / "kunwei_raw_frames.bin"
    tick_log_path = output_dir / "tick_log.jsonl"
    attempt_ledger_path = output_dir / "attempt_ledger.jsonl"
    baseline_rows: list[tuple[float, float, float, float, float, float]] = []
    baseline: tuple[float, float, float, float, float, float] | None = None
    latest_output: dict[str, Any] = {}
    saw_active = False
    start_mono = time.monotonic()
    last_tick_mono = start_mono
    next_publish = start_mono
    stop_latched_mono: float | None = None
    last_rtde_timestamp: float | None = None
    last_rtde_advance_mono: float | None = None
    path_origin_mono: float | None = None
    fieldnames = (
        "write_index",
        "t_monotonic_s",
        "sensor_age_s",
        "command_sequence",
        "force_p_gain",
        "force_i_gain",
        "force_damping",
        "normal_filter_tau_s",
        "orientation_ko",
        "motion_kp",
        "target_force_n",
        "Md",
        "kf",
        "Bd",
        "filter_alpha",
        "filtered_normal_n",
        "mode",
        "guard_reason",
        "packet_stop_dominant",
        "packet_reason",
        "ur_stage",
        "ur_reason",
        "ur_echo_heartbeat",
    )
    try:
        kunwei.open()
        rtde.open()
        with (
            csv_path.open("w", newline="", encoding="utf-8") as csv_handle,
            raw_path.open("wb") as raw_handle,
        ):
            if isinstance(kunwei, LiveKunweiTransport):
                kunwei.attach_raw_sink(raw_handle)
            writer = csv.DictWriter(csv_handle, fieldnames=fieldnames)
            writer.writeheader()
            while True:
                now = time.monotonic()
                if now - start_mono >= contract.maximum_runtime_s:
                    stop_signal["value"] = "maximum_runtime"
                new_frames, _ = kunwei.poll()
                latest_output = rtde.poll_output()
                now = time.monotonic()
                if latest_output:
                    try:
                        controller_timestamp = float(latest_output["timestamp"])
                    except (KeyError, TypeError, ValueError):
                        stop_signal["value"] = "invalid_rtde_timestamp"
                    else:
                        if not math.isfinite(controller_timestamp):
                            stop_signal["value"] = "nonfinite_rtde_timestamp"
                        elif (
                            last_rtde_timestamp is not None
                            and controller_timestamp < last_rtde_timestamp
                        ):
                            stop_signal["value"] = "rtde_timestamp_decreased"
                        elif (
                            last_rtde_timestamp is None
                            or controller_timestamp > last_rtde_timestamp
                        ):
                            last_rtde_timestamp = controller_timestamp
                            last_rtde_advance_mono = now
                        elif (
                            last_rtde_advance_mono is not None
                            and now - last_rtde_advance_mono >= contract.stale_s
                        ):
                            stop_signal["value"] = "rtde_output_stale"
                if baseline is None:
                    for parsed in new_frames:
                        converted = (
                            parsed[0] * FORCE_KG_TO_N,
                            parsed[1] * FORCE_KG_TO_N,
                            parsed[2] * FORCE_KG_TO_N,
                            parsed[3] * MOMENT_KG_M_TO_NM,
                            parsed[4] * MOMENT_KG_M_TO_NM,
                            parsed[5] * MOMENT_KG_M_TO_NM,
                        )
                        baseline_rows.append(converted)
                if baseline is None and now - start_mono >= contract.baseline_s:
                    if len(baseline_rows) < 1000:
                        stop_signal["value"] = "insufficient_baseline_samples"
                    else:
                        baseline = tuple(
                            statistics.fmean(axis)
                            for axis in zip(*baseline_rows, strict=True)
                        )
                if now < next_publish:
                    timeout = max(0.0, min(next_publish - now, 0.005))
                    select.select([], [], [], timeout)
                    continue
                gap_s = (
                    0.0
                    if last_publish_mono is None
                    else max(0.0, now - last_publish_mono)
                )
                maximum_publish_gap_s = max(maximum_publish_gap_s, gap_s)
                last_publish_mono = now
                actual_dt = now - last_tick_mono
                last_tick_mono = now
                if actual_dt <= 0.0:
                    stop_signal["value"] = "nonpositive_actual_dt"
                    actual_dt = math.nextafter(0.0, 1.0)
                elif actual_dt >= 0.08:
                    stop_signal["value"] = "actual_gap_at_least_80ms"
                while next_publish <= now:
                    next_publish += contract.period_s
                sensor_age = (
                    math.inf
                    if getattr(kunwei, "latest_mono", None) is None
                    else max(0.0, now - float(kunwei.latest_mono))
                )
                sensor_fresh = (
                    baseline is not None
                    and getattr(kunwei, "latest_raw", None) is not None
                    and sensor_age < contract.stale_s
                )
                latest_raw = getattr(kunwei, "latest_raw", None)
                wrench: tuple[float, float, float, float, float, float] | None
                try:
                    wrench = (
                        zeroed_wrench(latest_raw, baseline)
                        if sensor_fresh
                        and latest_raw is not None
                        and baseline is not None
                        else None
                    )
                except LiveWriterError:
                    wrench = None
                    sensor_fresh = False
                    stop_signal["value"] = "nonfinite_wrench"
                stage = float(latest_output.get("output_double_register_35", 0.0))
                stage_int = int(round(stage))
                limits = guard_limits_for_stage(contract, stage_int)
                eoat_ack = controller_get_matches(contract, latest_output)
                safety_ok = (
                    latest_output.get("safety_mode") == 1
                    and latest_output.get("robot_mode") == 7
                )
                if latest_output and not safety_ok and stop_signal["value"] is None:
                    stop_signal["value"] = "rtde_safety_or_robot_mode"
                external_stop = stop_signal["value"] is not None
                if external_stop and stop_latched_mono is None:
                    stop_latched_mono = now
                values = [0.0] * 6 if wrench is None else list(wrench)
                finite = len(values) == 6 and all(math.isfinite(value) for value in values)
                if not finite:
                    values = [0.0] * 6
                    sensor_fresh = False
                    external_stop = True
                fx, fy, fz, mx, my, mz = values
                normal_load = -fz if finite else 0.0
                force_norm = math.sqrt(fx * fx + fy * fy + fz * fz) if finite else 0.0
                torque_norm = math.sqrt(mx * mx + my * my + mz * mz) if finite else 0.0
                if (
                    not one_newton_latched
                    and finite
                    and (normal_load >= 0.8 or force_norm >= 1.0)
                ):
                    one_newton_latched = True
                guard_reason = (
                    None
                    if baseline is None
                    else evaluate_hard_guard(
                        limits,
                        normal_load=normal_load,
                        force_norm=force_norm,
                        torque_norm=torque_norm,
                        sensor_fresh=sensor_fresh,
                    )
                )
                if guard_reason is not None:
                    external_stop = True
                mode = path_mode_for_stage(stage_int)
                if stage_int in SEARCH_STAGES:
                    mode = "hold"
                path_time_s = float(
                    latest_output.get("output_double_register_36", 0.0)
                )
                if stage_int == 25 and path_origin_mono is None:
                    path_origin_mono = now
                replay_time_s = (
                    0.0
                    if path_origin_mono is None
                    else now - path_origin_mono
                )
                tangential_error = (0.0, 0.0)
                orientation_error = (0.0, 0.0, 0.0)
                try:
                    actual_q = latest_output["actual_q"]
                    actual_qd = latest_output["actual_qd"]
                    actual_tcp_pose = latest_output["actual_TCP_pose"]
                    actual_tcp_speed = latest_output["actual_TCP_speed"]
                    if mode == "path":
                        tangential_error, orientation_error = (
                            calibrated_runtime.path_errors(
                                actual_tcp_pose=actual_tcp_pose,
                                path_time_s=path_time_s,
                                motion_kp=attempt_spec.candidate.motion_kp,
                            )
                        )
                except (KeyError, TypeError, ValueError, CalibratedRuntimeError):
                    actual_q = (0.0,) * 6
                    actual_qd = (0.0,) * 6
                    actual_tcp_pose = (0.0,) * 6
                    actual_tcp_speed = (0.0,) * 6
                    stop_signal["value"] = "invalid_rtde_kinematics"
                    external_stop = True
                tick_log = path_controller.step(
                    actual_dt_s=actual_dt,
                    raw_normal_n=normal_load,
                    setpoint_n=outer_setpoint_n,
                    mode=mode,
                    orientation_error_rad=orientation_error,
                    tangential_error_m=tangential_error,
                )
                try:
                    desired_twist = calibrated_runtime.desired_twist(
                        actual_tcp_pose=actual_tcp_pose,
                        actual_tcp_speed=actual_tcp_speed,
                        force_tcp_n=(fx, fy, fz),
                        filtered_normal_n=tick_log.filtered_normal_n,
                        internal_setpoint_n=outer_setpoint_n,
                        actual_dt_s=actual_dt,
                        mode=mode,
                        path_time_s=path_time_s,
                    )
                    tick_log = replace(
                        tick_log,
                        proposed_qdot=desired_twist,
                    )
                    calibrated_command = calibrated_runtime.command(
                        actual_q=actual_q,
                        actual_qd=actual_qd,
                        actual_tcp_pose=actual_tcp_pose,
                        desired_twist=desired_twist,
                        actual_dt_s=actual_dt,
                        mode=mode,
                        path_time_s=path_time_s,
                    )
                except (TypeError, ValueError, CalibratedRuntimeError):
                    stop_signal["value"] = "calibrated_runtime_failure"
                    external_stop = True
                    calibrated_command = calibrated_runtime.command(
                        actual_q=actual_q,
                        actual_qd=actual_qd,
                        actual_tcp_pose=actual_tcp_pose,
                        desired_twist=(0.0,) * 6,
                        actual_dt_s=actual_dt,
                        mode="stop",
                        path_time_s=path_time_s,
                    )
                derive_force_terms(attempt_spec.candidate)
                if sensor_fresh and eoat_ack and not external_stop:
                    heartbeat += 1.0
                sent_heartbeat = heartbeat
                stop_request = external_stop or guard_reason is not None
                sensor = SensorPacket(
                    normal_load_n=normal_load,
                    force_norm_n=force_norm,
                    heartbeat=sent_heartbeat,
                    sensor_fresh=sensor_fresh,
                    stop_request=stop_request,
                    eoat_get_ack=eoat_ack,
                    torque_norm_nm=torque_norm,
                    wrench=(fx, fy, fz, mx, my, mz),
                    filtered_normal_load_n=tick_log.filtered_normal_n,
                )
                observation = RuntimeObservation(
                    monotonic_s=now - start_mono,
                    heartbeat=sent_heartbeat,
                    one_newton_latched=one_newton_latched,
                    filtered_normal_n=tick_log.filtered_normal_n,
                    raw_normal_n=normal_load,
                    force_norm_n=force_norm,
                    torque_norm_nm=torque_norm,
                    sensor_fresh=sensor_fresh,
                    stationary=_stationary_from_output(latest_output),
                    safety_failure=latest_output.get("safety_mode") not in (None, 1),
                    structural_failure=stop_signal["value"] == "nonfinite_wrench",
                )
                adapter_result = adapter.tick(
                    AdapterTick(
                        observation=observation,
                        sensor=sensor,
                        proposed_qdot=calibrated_command.qdot,
                        jacobian_6x6=calibrated_command.jacobian_6x6,
                        normal_base=NORMAL_BASE,
                        observed_model_hashes=calibrated_command.observed_model_hashes,
                        command_sequence=command_sequence,
                    )
                )
                outer_setpoint_n = adapter_result.decision.internal_setpoint_n
                command_sequence += 1
                packet = adapter_result.packet
                ledger_rows.append(adapter_result.ledger_row)
                rtde.send_packet(packet.double_values, packet.integer_values)
                rows += 1
                reason = float(latest_output.get("output_double_register_30", 0.0))
                echo = float(
                    latest_output.get("output_double_register_26", math.nan)
                )
                echo_fresh = math.isfinite(echo) and abs(echo - sent_heartbeat) <= 2.0
                if stage_int in contract.active_stages and echo_fresh:
                    saw_active = True
                if (
                    saw_active
                    and stage_int in contract.terminal_stages
                    and reason != 0.0
                    and echo_fresh
                ):
                    terminal = {
                        "stage": stage,
                        "reason": reason,
                        "echo_heartbeat": echo,
                        "sent_heartbeat": sent_heartbeat,
                        "observed_at_monotonic_s": now,
                    }
                tick_row = _tick_log_row(
                    write_index=rows,
                    now=now,
                    sensor_age=sensor_age,
                    tick_log=tick_log,
                    packet={
                        "stop_dominant": packet.stop_dominant,
                        "reason": packet.reason,
                        "heartbeat": sent_heartbeat,
                    },
                    guard_reason=guard_reason or stop_signal["value"] or "",
                    latest_output=latest_output,
                    stage=stage,
                    reason=reason,
                    echo=echo,
                    command_sequence=command_sequence - 1,
                    raw_normal_n=normal_load,
                    force_norm_n=force_norm,
                    torque_norm_nm=torque_norm,
                    path_time_s=path_time_s,
                    sensor_fresh=sensor_fresh,
                )
                if stage_int == 25:
                    replay_rows.append(
                        ReplayRow(
                            monotonic_s=replay_time_s,
                            filtered_normal_n=tick_log.filtered_normal_n,
                            raw_normal_n=normal_load,
                            force_norm_n=force_norm,
                            torque_norm_nm=torque_norm,
                            sensor_fresh=sensor_fresh,
                        )
                    )
                tick_lines.append(
                    json.dumps(tick_row, sort_keys=True, separators=(",", ":"), allow_nan=False)
                )
                writer.writerow(
                    {
                        "write_index": rows,
                        "t_monotonic_s": f"{now:.9f}",
                        "sensor_age_s": (
                            "" if not math.isfinite(sensor_age) else f"{sensor_age:.9f}"
                        ),
                        "command_sequence": command_sequence - 1,
                        "force_p_gain": tick_log.force_p_gain,
                        "force_i_gain": tick_log.force_i_gain,
                        "force_damping": tick_log.force_damping,
                        "normal_filter_tau_s": tick_log.normal_filter_tau_s,
                        "orientation_ko": tick_log.orientation_ko,
                        "motion_kp": tick_log.motion_kp,
                        "target_force_n": tick_log.target_force_n,
                        "Md": tick_log.Md,
                        "kf": tick_log.kf,
                        "Bd": tick_log.Bd,
                        "filter_alpha": tick_log.filter_alpha,
                        "filtered_normal_n": tick_log.filtered_normal_n,
                        "mode": tick_log.mode,
                        "guard_reason": guard_reason or stop_signal["value"] or "",
                        "packet_stop_dominant": int(packet.stop_dominant),
                        "packet_reason": packet.reason,
                        "ur_stage": stage,
                        "ur_reason": reason,
                        "ur_echo_heartbeat": latest_output.get(
                            "output_double_register_26", ""
                        ),
                    }
                )
                if (
                    not ready_written
                    and baseline is not None
                    and eoat_ack
                    and sensor_fresh
                    and rows >= 2
                ):
                    writer_document = json.loads(
                        contract.path.read_text(encoding="utf-8")
                    )
                    ready_payload = {
                        "schema": READY_SCHEMA,
                        "attempt_id": attempt_spec.attempt_id,
                        "ready_at_monotonic_s": now,
                        "controller_target": writer_document.get("program", {}).get(
                            "controller_target"
                        ),
                        "eoat_payload_kg": contract.payload_kg,
                        "eoat_cog_m": list(contract.cog_m),
                        "eoat_tcp_offset_m_rad": list(contract.tcp_offset),
                        "lease_resource_id": contract.resource_id,
                        "register_writes": rows,
                    }
                    _atomic_json(output_dir / "ready.json", ready_payload)
                    ready_written = True
                if rows % 125 == 0 or terminal is not None:
                    csv_handle.flush()
                    raw_handle.flush()
                if terminal is not None:
                    completion_reason = "completed"
                    break
                if stop_signal["value"] is not None and not saw_active:
                    completion_reason = "failed"
                    break
                if (
                    stop_latched_mono is not None
                    and now - stop_latched_mono >= contract.terminal_exit_timeout_s
                ):
                    completion_reason = "failed"
                    break
    finally:
        kunwei.close()
        rtde.close()
        if lease_active:
            authority.revoke(
                contract.authority_root,
                attempt_spec.attempt_id,
                parent_pid,
                parent_starttime,
                reason=completion_reason,
                resource_id=contract.resource_id,
            )
            lease_active = False
    _atomic_text(tick_log_path, "\n".join(tick_lines) + ("\n" if tick_lines else ""))
    terminal_closure = terminal or {"stage": None, "reason": stop_signal["value"]}
    completion_closure = {
        "attempt_id": attempt_spec.attempt_id,
        "terminal": terminal_closure,
        "register_writes": rows,
    }
    replay_result = replay_attempt(replay_rows)
    replay_closure = {
        "tick_log_sha256": hashlib.sha256(tick_log_path.read_bytes()).hexdigest(),
        "rows": rows,
        "path_rows": len(replay_rows),
        "complete_bins": replay_result.complete_bins,
        "required_bins": replay_result.required_bins,
        "mae_n": replay_result.mae_n,
        "bias_n": replay_result.bias_n,
        "std_n": replay_result.std_n,
        "p99_normal_n": replay_result.p99_normal_n,
        "max_force_norm_n": replay_result.max_force_norm_n,
        "max_torque_norm_nm": replay_result.max_torque_norm_nm,
        "timing": replay_result.timing,
        "eligible_shape": replay_result.eligible_shape,
    }
    seal_info = seal_attempt_ledger(
        ledger_rows,
        attempt_ledger_path,
        terminal_closure=terminal_closure,
        completion_closure=completion_closure,
        replay_closure=replay_closure,
    )
    output_baseline_ledger = dict(adapter.control.baseline_ledger.seal())
    _atomic_json(output_dir / "baseline_ledger_seal.json", output_baseline_ledger)
    timing_acceptance = dict(adapter.control.policies.timing.acceptance())
    summary = {
        "schema": SUMMARY_SCHEMA,
        "attempt_id": attempt_spec.attempt_id,
        "attempt_kind": attempt_spec.kind.value,
        "candidate_uid": attempt_spec.candidate.candidate_uid,
        "contract_sha256": contract.sha256,
        "v4_contract_sha256": attempt_spec.v4_contract_sha256,
        "status": "tp_terminal_observed" if terminal is not None else "writer_failed",
        "terminal": terminal,
        "metrics": {
            "elapsed_s": time.monotonic() - start_mono,
            "register_writes": rows,
            "sensor_samples": getattr(kunwei, "sensor_samples", 0),
            "parse_errors": getattr(kunwei, "parse_errors", 0),
            "dropped_sync_bytes": getattr(kunwei, "dropped_bytes", 0),
            "maximum_publish_gap_s": maximum_publish_gap_s,
            "baseline_samples": len(baseline_rows),
            "ready_written": ready_written,
            "objective": replay_result.mae_n,
            "mae_n": replay_result.mae_n,
            "bias_n": replay_result.bias_n,
            "std_n": replay_result.std_n,
            "complete_bins": replay_result.complete_bins,
            "p99_normal_n": replay_result.p99_normal_n,
            "max_force_norm_n": replay_result.max_force_norm_n,
            "max_torque_norm_nm": replay_result.max_torque_norm_nm,
        },
        "stop_signal": stop_signal["value"],
        "timing_acceptance": timing_acceptance,
        "replay_eligible_shape": replay_result.eligible_shape,
        "completion_sha256": seal_info["ledger_sha256"],
        "output_baseline_ledger": output_baseline_ledger,
        "attempt_ledger": seal_info,
        "paths": {
            "metadata": str(output_dir / "metadata.json"),
            "ready": str(output_dir / "ready.json"),
            "csv": str(csv_path),
            "raw": str(raw_path),
            "tick_log": str(tick_log_path),
            "attempt_ledger": str(attempt_ledger_path),
            "baseline_ledger_seal": str(output_dir / "baseline_ledger_seal.json"),
        },
    }
    _atomic_json(output_dir / "summary.json", summary)
    completion_path: Path | None = None
    completion: dict[str, Any] = {}
    if terminal is not None:
        completion_path = output_dir / "completion.json"
        completion = {
            "schema": COMPLETION_SCHEMA,
            "attempt_id": attempt_spec.attempt_id,
            "writer_summary_sha256": _sha256(output_dir / "summary.json"),
            "tick_log_sha256": _sha256(tick_log_path),
            "attempt_ledger_sha256": seal_info["ledger_sha256"],
            "csv_sha256": _sha256(csv_path),
            "raw_sha256": _sha256(raw_path),
            "terminal": terminal,
            "register_live_writer_exited_after_terminal": True,
        }
        _atomic_json(completion_path, completion)
    return {
        "summary": summary,
        "result": asdict(
            V4LiveAttemptResult(
                attempt_id=attempt_spec.attempt_id,
                kind=attempt_spec.kind,
                ready=ready_payload,
                terminal=terminal_closure,
                completion=completion,
                replay={
                    **replay_closure,
                    "attempt_ledger_sha256": seal_info["ledger_sha256"],
                },
                eligibility={
                    "eligible": False,
                    "reason": "campaign_prior-eligible-count_owner_pending",
                    "timing_acceptance": timing_acceptance,
                },
                output_baseline_ledger_sha256=str(
                    output_baseline_ledger["ledger_sha256"]
                ),
                applied_candidate_uid=attempt_spec.candidate.candidate_uid,
                applied_candidate=attempt_spec.candidate.canonical_physical,
            )
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--attempt-spec-json", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use fake transports and skip dashboard/network I/O",
    )
    parser.add_argument(
        "--fake-transport",
        action="store_true",
        help="Alias for --dry-run fake Kunwei/RTDE transports",
    )
    args = parser.parse_args(argv)
    contract = load_contract(args.contract)
    attempt_spec = load_attempt_spec(args.attempt_spec_json)
    use_fake = args.dry_run or args.fake_transport
    payload = run_writer(
        contract,
        attempt_spec,
        output_dir=args.output_dir,
        kunwei_transport=FakeKunweiTransport() if use_fake else None,
        rtde_transport=FakeRTDETransport(contract) if use_fake else None,
        skip_dashboard=use_fake,
        skip_lease=use_fake,
    )
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    return 0 if payload["summary"]["status"] == "tp_terminal_observed" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARTIFACT_ID",
    "AttemptKind",
    "DEFAULT_CONTRACT",
    "FakeKunweiTransport",
    "FakeRTDETransport",
    "LiveKunweiTransport",
    "LiveRTDETransport",
    "LiveWriterError",
    "V4LiveAttemptResult",
    "V4LiveAttemptSpec",
    "WRITER_SCHEMA",
    "WritableRTDEClient",
    "WriterContract",
    "controller_get_matches",
    "derive_force_terms",
    "evaluate_hard_guard",
    "guard_limits_for_stage",
    "load_attempt_spec",
    "load_contract",
    "main",
    "parse_attempt_spec",
    "path_mode_for_stage",
    "run_writer",
    "zeroed_wrench",
]
