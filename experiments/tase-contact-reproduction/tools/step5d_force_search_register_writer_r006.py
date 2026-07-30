#!/usr/bin/env python3
"""Single-owner Kunwei-to-RTDE register live writer for canary r006."""

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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


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
import step5d_bridge_authority as authority  # noqa: E402


DEFAULT_CONTRACT = ROOT / "config/step5d/force_search_register_writer_r006.json"
SCHEMA = "step5d.force-search-canary/register-live-writer-v1"
ARTIFACT_ID = "new-eoat-kunwei-register-live-writer-r006"
INPUT_FIELDS = [f"input_double_register_{index}" for index in range(24, 37)]
INPUT_NAMES = (
    "normal_load_n",
    "force_norm_n",
    "heartbeat",
    "sensor_fresh",
    "stop_request",
    "eoat_get_ack",
    "torque_norm_nm",
    "fx_n",
    "fy_n",
    "fz_n",
    "mx_nm",
    "my_nm",
    "mz_nm",
)
OUTPUT_FIELDS = (
    "timestamp",
    "payload",
    "payload_cog",
    "tcp_offset",
    "actual_TCP_speed",
    "safety_mode",
    "robot_mode",
    "runtime_state",
    "output_double_register_26",
    "output_double_register_30",
    "output_double_register_35",
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


class RegisterWriterError(RuntimeError):
    """The r006 register-live-writer contract or runtime is invalid."""


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
    max_abs_normal_n: float
    max_force_norm_n: float
    max_torque_norm_nm: float
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RegisterWriterError(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise RegisterWriterError(f"{role} must be finite")
    return result


def _vector(value: Any, size: int, role: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != size:
        raise RegisterWriterError(f"{role} must contain exactly {size} values")
    return tuple(_finite(item, role) for item in value)


def _integer_tuple(value: Any, role: str) -> tuple[int, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
    ):
        raise RegisterWriterError(f"{role} must be a non-empty integer list")
    return tuple(value)


def load_contract(path: Path = DEFAULT_CONTRACT) -> WriterContract:
    if path.is_symlink() or not path.is_file():
        raise RegisterWriterError(f"writer contract must be a regular file: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RegisterWriterError(f"writer contract is unreadable: {exc}") from exc
    if document.get("schema") != SCHEMA or document.get("artifact_id") != ARTIFACT_ID:
        raise RegisterWriterError("writer contract identity differs")
    endpoints = document.get("endpoints")
    timing = document.get("timing")
    mapping = document.get("force_mapping")
    guards = document.get("hard_guards")
    eoat = document.get("eoat_controller_get")
    lease = document.get("lease")
    terminal = document.get("terminal")
    if not all(
        isinstance(item, dict)
        for item in (endpoints, timing, mapping, guards, eoat, lease, terminal)
    ):
        raise RegisterWriterError("writer contract sections are missing")
    if mapping != {
        "normal_axis": "fz",
        "normal_sign": -1.0,
        "software_baseline_only": True,
        "hardware_zero_or_tare": False,
    }:
        raise RegisterWriterError("Kunwei force mapping differs")
    if eoat.get("ack_input_register") != 29:
        raise RegisterWriterError("EOAT acknowledgement register differs")
    sensor_port = endpoints.get("kunwei_port")
    if not isinstance(sensor_port, int) or isinstance(sensor_port, bool):
        raise RegisterWriterError("Kunwei port must be an integer")
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
        maximum_runtime_s=_finite(
            timing.get("maximum_runtime_s"), "maximum runtime"
        ),
        terminal_exit_timeout_s=_finite(
            timing.get("terminal_exit_timeout_s"), "terminal exit timeout"
        ),
        max_abs_normal_n=_finite(
            guards.get("absolute_normal_load_n"), "absolute normal guard"
        ),
        max_force_norm_n=_finite(guards.get("force_norm_n"), "force norm guard"),
        max_torque_norm_nm=_finite(
            guards.get("torque_norm_nm"), "torque norm guard"
        ),
        payload_kg=_finite(eoat.get("payload_kg"), "payload"),
        cog_m=tuple(_vector(eoat.get("payload_cog_m"), 3, "CoG")),  # type: ignore[arg-type]
        tcp_offset=tuple(
            _vector(eoat.get("tcp_offset_m_rad"), 6, "TCP")
        ),  # type: ignore[arg-type]
        payload_tolerance=_finite(
            eoat.get("payload_tolerance_kg"), "payload tolerance"
        ),
        cog_tolerance=_finite(eoat.get("cog_tolerance_m"), "CoG tolerance"),
        tcp_tolerance=_finite(
            eoat.get("tcp_tolerance_m_rad"), "TCP tolerance"
        ),
        authority_root=Path(str(lease.get("authority_root"))),
        resource_id=str(lease.get("resource_id")),
        active_stages=_integer_tuple(terminal.get("active_stages"), "active stages"),
        terminal_stages=_integer_tuple(
            terminal.get("terminal_stages"), "terminal stages"
        ),
    )
    expected = {
        "robot_host": "192.168.1.18",
        "sensor_host": "192.168.50.25",
        "sensor_port": 5152,
        "rtde_hz": 125.0,
        "period_s": 0.008,
        "baseline_s": 5.0,
        "stale_s": 0.08,
        "maximum_runtime_s": 120.0,
        "terminal_exit_timeout_s": 2.0,
        "max_abs_normal_n": 3.0,
        "max_force_norm_n": 3.0,
        "max_torque_norm_nm": 0.2,
        "payload_kg": 0.413,
        "cog_m": (0.0011, 0.0031, 0.0163),
        "tcp_offset": (0.0, 0.0, 0.0874, 0.0, 0.0, 0.0),
        "resource_id": "step5d-bridge-writer",
        "active_stages": (10, 11, 12, 13, 14),
        "terminal_stages": (15, 90),
    }
    mismatches = {
        name: {"expected": expected_value, "actual": getattr(contract, name)}
        for name, expected_value in expected.items()
        if getattr(contract, name) != expected_value
    }
    if mismatches:
        raise RegisterWriterError(f"writer fixed invariants differ: {mismatches}")
    return contract


def controller_get_matches(
    contract: WriterContract, output: dict[str, Any]
) -> bool:
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
        raise RegisterWriterError("Kunwei wrench and baseline must be 6D")
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
        raise RegisterWriterError("Kunwei zeroed wrench is non-finite")
    return result  # type: ignore[return-value]


def register_packet(
    contract: WriterContract,
    *,
    wrench: Sequence[float] | None,
    sensor_fresh: bool,
    heartbeat: float,
    eoat_get_ack: bool,
    external_stop: bool = False,
) -> tuple[dict[str, float], str | None]:
    values = [0.0] * 6 if wrench is None else [float(value) for value in wrench]
    finite = len(values) == 6 and all(math.isfinite(value) for value in values)
    if not finite:
        values = [0.0] * 6
        sensor_fresh = False
        external_stop = True
    fx, fy, fz, mx, my, mz = values
    normal_load = -fz
    force_norm = math.sqrt(fx * fx + fy * fy + fz * fz)
    torque_norm = math.sqrt(mx * mx + my * my + mz * mz)
    guard_reason: str | None = None
    if not sensor_fresh:
        guard_reason = "sensor_stale_or_unready"
    elif abs(normal_load) >= contract.max_abs_normal_n:
        guard_reason = "hard_abs_normal"
    elif force_norm >= contract.max_force_norm_n:
        guard_reason = "hard_force_norm"
    elif torque_norm >= contract.max_torque_norm_nm:
        guard_reason = "hard_torque_norm"
    stop_request = external_stop or guard_reason is not None
    packet = {
        "normal_load_n": normal_load if finite else 0.0,
        "force_norm_n": force_norm if finite else 0.0,
        "heartbeat": heartbeat if math.isfinite(heartbeat) else 0.0,
        "sensor_fresh": 1.0 if sensor_fresh else 0.0,
        "stop_request": 1.0 if stop_request else 0.0,
        "eoat_get_ack": 1.0 if eoat_get_ack else 0.0,
        "torque_norm_nm": torque_norm if finite else 0.0,
        "fx_n": fx,
        "fy_n": fy,
        "fz_n": fz,
        "mx_nm": mx,
        "my_nm": my,
        "mz_nm": mz,
    }
    return packet, guard_reason


class WritableRTDEClient(RTDEClient):
    """Minimal RTDE v2 input writer with nonblocking latest-output reads."""

    def setup_inputs(self, fields: Sequence[str]) -> tuple[int, list[str]]:
        self._send_packet("I", ",".join(fields).encode())
        packet_type, payload = self._recv_packet()
        if packet_type != ord("I") or not payload:
            raise RegisterWriterError("RTDE input setup failed")
        recipe_id = payload[0]
        types = payload[1:].decode("ascii", errors="replace").split(",")
        if (
            recipe_id == 0
            or len(types) != len(fields)
            or any(value == "NOT_FOUND" for value in types)
        ):
            raise RegisterWriterError("RTDE input recipe is invalid")
        return recipe_id, types

    def send_input_sample(
        self, recipe_id: int, type_names: Sequence[str], values: Sequence[Any]
    ) -> None:
        if len(type_names) != len(values):
            raise RegisterWriterError("RTDE input type/value count differs")
        payload = bytearray([recipe_id])
        for type_name, value in zip(type_names, values, strict=True):
            fmt = RTDE_TYPES.get(type_name)
            if fmt is None:
                raise RegisterWriterError(f"unsupported RTDE input type {type_name}")
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
            raise RegisterWriterError("RTDE socket is unavailable")
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
                    raise RegisterWriterError(
                        f"unsupported RTDE output type {type_name}"
                    )
                width = struct.calcsize("!" + fmt)
                unpacked = struct.unpack("!" + fmt, payload[cursor : cursor + width])
                cursor += width
                values.append(unpacked[0] if len(unpacked) == 1 else list(unpacked))
            if cursor != len(payload):
                raise RegisterWriterError("RTDE output sample has trailing bytes")
            latest = dict(zip(fields, values, strict=True))
        return latest


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


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
        raise RegisterWriterError("Dashboard Remote Control gate failed")
    if "NORMAL" not in dashboard.get("safetymode", ""):
        raise RegisterWriterError("Dashboard Safety NORMAL gate failed")
    if "RUNNING" not in dashboard.get("robotmode", ""):
        raise RegisterWriterError("Dashboard robot RUNNING gate failed")
    return dashboard


def _open_rtde(
    contract: WriterContract,
) -> tuple[WritableRTDEClient, int, list[str], int, list[str]]:
    client = WritableRTDEClient(contract.robot_host, timeout=3.0)
    client.__enter__()
    try:
        client.negotiate()
        output_recipe, output_types = client.setup_outputs(
            contract.rtde_hz, OUTPUT_FIELDS
        )
        input_recipe, input_types = client.setup_inputs(INPUT_FIELDS)
        client.start()
    except Exception:
        client.__exit__(None, None, None)
        raise
    return client, input_recipe, input_types, output_recipe, output_types


def run_writer(
    contract: WriterContract,
    *,
    output_dir: Path,
    attempt_id: str,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=False)
    contract_copy = output_dir / contract.path.name
    contract_copy.write_bytes(contract.path.read_bytes())
    dashboard = _dashboard_preflight(contract)
    parent_pid = os.getppid()
    parent_starttime = read_proc_starttime_ticks(parent_pid)
    if parent_starttime is None:
        raise RegisterWriterError("writer parent process identity is unavailable")
    lease = authority.begin(
        contract.authority_root,
        attempt_id,
        parent_pid,
        parent_starttime,
        worktree_root=str(REPOSITORY_ROOT),
        launch_basis_path=str(contract.path),
        launch_basis_sha256=contract.sha256,
        resource_id=contract.resource_id,
    )
    lease_active = True
    sensor: socket.socket | None = None
    rtde: WritableRTDEClient | None = None
    completion_reason = "failed"
    terminal: dict[str, Any] | None = None
    rows = 0
    sensor_samples = 0
    parse_errors = 0
    dropped_bytes = 0
    heartbeat = 0.0
    sent_heartbeat = 0.0
    maximum_publish_gap_s = 0.0
    last_publish_mono: float | None = None
    stop_signal: dict[str, str | None] = {"value": None}

    def signal_handler(signum: int, _frame: Any) -> None:
        stop_signal["value"] = signal.Signals(signum).name

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    metadata = {
        "schema": "step5d.force-search-canary/register-writer-run-v1",
        "created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "attempt_id": attempt_id,
        "contract_sha256": contract.sha256,
        "dashboard_preflight": dashboard,
        "lease": lease,
        "safety_boundary": [
            "sole RTDE input-register writer under canonical lease",
            "Kunwei START_STREAM and STOP_STREAM only",
            "software baseline only; no zero/tare/configuration write",
            "no URScript, Dashboard writes, robot motion, TCP, or payload writes",
        ],
    }
    _atomic_json(output_dir / "metadata.json", metadata)
    csv_path = output_dir / "register_writer_125hz.csv"
    raw_path = output_dir / "kunwei_raw_frames.bin"
    buffer = bytearray()
    baseline_rows: list[tuple[float, float, float, float, float, float]] = []
    baseline: tuple[float, float, float, float, float, float] | None = None
    latest_raw: tuple[float, float, float, float, float, float] | None = None
    latest_sensor_mono: float | None = None
    latest_output: dict[str, Any] = {}
    saw_active = False
    start_mono = time.monotonic()
    next_publish = start_mono
    stop_latched_mono: float | None = None
    fieldnames = (
        "write_index",
        "t_monotonic_s",
        "sensor_age_s",
        *INPUT_NAMES,
        "guard_reason",
        "ur_payload",
        "ur_tcp_z_m",
        "ur_safety_mode",
        "ur_robot_mode",
        "ur_runtime_state",
        "ur_echo_heartbeat",
        "ur_stage",
        "ur_reason",
    )
    try:
        sensor = socket.create_connection(
            (contract.sensor_host, contract.sensor_port), timeout=3.0
        )
        sensor.setblocking(False)
        sensor.sendall(START_STREAM)
        rtde, input_recipe, input_types, output_recipe, output_types = _open_rtde(
            contract
        )
        with (
            csv_path.open("w", newline="", encoding="utf-8") as csv_handle,
            raw_path.open("wb") as raw_handle,
        ):
            writer = csv.DictWriter(csv_handle, fieldnames=fieldnames)
            writer.writeheader()
            while True:
                now = time.monotonic()
                if now - start_mono >= contract.maximum_runtime_s:
                    stop_signal["value"] = "maximum_runtime"
                readable: list[socket.socket] = []
                if sensor is not None:
                    readable.append(sensor)
                if rtde is not None and rtde.sock is not None:
                    readable.append(rtde.sock)
                timeout = max(0.0, min(next_publish - now, 0.005))
                ready, _, _ = select.select(readable, [], [], timeout)
                if sensor in ready:
                    try:
                        chunk = sensor.recv(8192)
                    except BlockingIOError:
                        chunk = b""
                    if chunk:
                        buffer.extend(chunk)
                        frames, dropped = pop_frames(buffer, 0x48)
                        dropped_bytes += dropped
                        for frame in frames:
                            try:
                                parsed = parse_frame(frame)
                            except ValueError:
                                parse_errors += 1
                                continue
                            if not all(math.isfinite(value) for value in parsed):
                                parse_errors += 1
                                continue
                            latest_raw = parsed
                            latest_sensor_mono = time.monotonic()
                            sensor_samples += 1
                            raw_handle.write(frame)
                            if baseline is None:
                                converted = (
                                    parsed[0] * FORCE_KG_TO_N,
                                    parsed[1] * FORCE_KG_TO_N,
                                    parsed[2] * FORCE_KG_TO_N,
                                    parsed[3] * MOMENT_KG_M_TO_NM,
                                    parsed[4] * MOMENT_KG_M_TO_NM,
                                    parsed[5] * MOMENT_KG_M_TO_NM,
                                )
                                baseline_rows.append(converted)
                if rtde is not None and rtde.sock in ready:
                    observed = rtde.recv_latest_sample(
                        output_recipe, output_types, OUTPUT_FIELDS
                    )
                    if observed is not None:
                        latest_output = observed
                now = time.monotonic()
                if baseline is None and now - start_mono >= contract.baseline_s:
                    if len(baseline_rows) < 1000:
                        stop_signal["value"] = "insufficient_baseline_samples"
                    else:
                        baseline = tuple(
                            statistics.fmean(axis)
                            for axis in zip(*baseline_rows, strict=True)
                        )
                if now < next_publish:
                    continue
                gap_s = (
                    0.0
                    if last_publish_mono is None
                    else max(0.0, now - last_publish_mono)
                )
                maximum_publish_gap_s = max(maximum_publish_gap_s, gap_s)
                last_publish_mono = now
                while next_publish <= now:
                    next_publish += contract.period_s
                sensor_age = (
                    math.inf
                    if latest_sensor_mono is None
                    else max(0.0, now - latest_sensor_mono)
                )
                sensor_fresh = (
                    baseline is not None
                    and latest_raw is not None
                    and sensor_age < contract.stale_s
                )
                wrench: tuple[float, float, float, float, float, float] | None
                try:
                    wrench = (
                        zeroed_wrench(latest_raw, baseline)
                        if sensor_fresh
                        and latest_raw is not None
                        and baseline is not None
                        else None
                    )
                except RegisterWriterError:
                    wrench = None
                    sensor_fresh = False
                    stop_signal["value"] = "nonfinite_wrench"
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
                packet, guard_reason = register_packet(
                    contract,
                    wrench=wrench,
                    sensor_fresh=sensor_fresh,
                    heartbeat=heartbeat,
                    eoat_get_ack=eoat_ack,
                    external_stop=external_stop,
                )
                if sensor_fresh and eoat_ack and not external_stop:
                    heartbeat += 1.0
                    packet["heartbeat"] = heartbeat
                sent_heartbeat = packet["heartbeat"]
                if rtde is None:
                    raise RegisterWriterError("RTDE transport was lost")
                rtde.send_input_sample(
                    input_recipe,
                    input_types,
                    [packet[name] for name in INPUT_NAMES],
                )
                rows += 1
                stage = float(latest_output.get("output_double_register_35", 0.0))
                reason = float(latest_output.get("output_double_register_30", 0.0))
                echo = float(
                    latest_output.get("output_double_register_26", math.nan)
                )
                echo_fresh = math.isfinite(echo) and abs(echo - sent_heartbeat) <= 2.0
                if int(round(stage)) in contract.active_stages and echo_fresh:
                    saw_active = True
                if (
                    saw_active
                    and int(round(stage)) in contract.terminal_stages
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
                writer.writerow(
                    {
                        "write_index": rows,
                        "t_monotonic_s": f"{now:.9f}",
                        "sensor_age_s": (
                            "" if not math.isfinite(sensor_age) else f"{sensor_age:.9f}"
                        ),
                        **packet,
                        "guard_reason": guard_reason or stop_signal["value"] or "",
                        "ur_payload": latest_output.get("payload", ""),
                        "ur_tcp_z_m": (
                            latest_output.get("tcp_offset", ["", "", ""])[2]
                            if isinstance(latest_output.get("tcp_offset"), list)
                            else ""
                        ),
                        "ur_safety_mode": latest_output.get("safety_mode", ""),
                        "ur_robot_mode": latest_output.get("robot_mode", ""),
                        "ur_runtime_state": latest_output.get("runtime_state", ""),
                        "ur_echo_heartbeat": latest_output.get(
                            "output_double_register_26", ""
                        ),
                        "ur_stage": stage,
                        "ur_reason": reason,
                    }
                )
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
        if sensor is not None:
            try:
                sensor.sendall(STOP_STREAM)
            except OSError:
                pass
            sensor.close()
        if rtde is not None:
            rtde.__exit__(None, None, None)
        if lease_active:
            authority.revoke(
                contract.authority_root,
                attempt_id,
                parent_pid,
                parent_starttime,
                reason=completion_reason,
                resource_id=contract.resource_id,
            )
            lease_active = False
    summary = {
        "schema": "step5d.force-search-canary/register-writer-summary-v1",
        "attempt_id": attempt_id,
        "contract_sha256": contract.sha256,
        "status": "tp_terminal_observed" if terminal is not None else "writer_failed",
        "terminal": terminal,
        "metrics": {
            "elapsed_s": time.monotonic() - start_mono,
            "register_writes": rows,
            "sensor_samples": sensor_samples,
            "parse_errors": parse_errors,
            "dropped_sync_bytes": dropped_bytes,
            "maximum_publish_gap_s": maximum_publish_gap_s,
            "baseline_samples": len(baseline_rows),
        },
        "stop_signal": stop_signal["value"],
        "paths": {
            "metadata": str(output_dir / "metadata.json"),
            "csv": str(csv_path),
            "raw": str(raw_path),
        },
    }
    _atomic_json(output_dir / "summary.json", summary)
    if terminal is not None:
        completion = {
            "schema": "step5d.force-search-canary/completion-closure-v1",
            "attempt_id": attempt_id,
            "writer_summary_sha256": _sha256(output_dir / "summary.json"),
            "csv_sha256": _sha256(csv_path),
            "raw_sha256": _sha256(raw_path),
            "terminal": terminal,
            "register_live_writer_exited_after_terminal": True,
        }
        _atomic_json(output_dir / "completion.json", completion)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--attempt-id", required=True)
    args = parser.parse_args(argv)
    contract = load_contract(args.contract)
    summary = run_writer(
        contract,
        output_dir=args.output_dir,
        attempt_id=args.attempt_id,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["status"] == "tp_terminal_observed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
