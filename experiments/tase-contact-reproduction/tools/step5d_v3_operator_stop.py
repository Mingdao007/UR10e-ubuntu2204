#!/usr/bin/env python3
"""Fail-closed, receipt-producing V3 Remote operator-stop primitives.

This module deliberately owns only the operator-stop boundary.  It does not
change the bridge lifecycle or ``stop_after_current`` contract.  The live
side-effect is dependency injected, and the public helper defaults to
validation-only; callers must pass ``execute=True`` to construct a writer.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import stat
import sys
import time
from typing import Any, Callable, Mapping, NoReturn, Sequence

# ``step5d_parameter_queue`` imports the local runtime package directly.  Keep
# this adapter self-contained when invoked as a script or collected alone.
_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_SRC = _ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
if str(_RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_SRC))

from step5d_parameter_queue import (  # type: ignore
    _lock,
    _load_terminal_receipts,
    _pending,
    _strict_json,
    _visible_requests,
    load_state,
)
from step5d_autotune_v3.release_identity import load_current_release
from step5d_autotune_v3.runtime_gate import (
    load_campaign_lease,
    process_starttime,
)
from step5d_remote_startup import RemoteDashboardWriter


RECEIPT_SCHEMA = "step5d.autotune-v3/operator-stop-receipt-v1"
REQUEST_SCHEMA = "step5d.parameter-receiver/request-v1"
TERMINAL_RECEIPT_SCHEMA = "step5d.parameter-receiver/governance-terminal-receipt-v1"
DISPATCH_SCHEMA = "step5d.parameter-receiver/dispatch-v1"
EXPECTED_TASK = "step5d-autotune-v3-production-bridge"
EXPECTED_PROGRAM_ID = "step5d_strict_rnn_autotune_v3_r034"
EXPECTED_CONTROLLER_TARGET = (
    "/programs/andyl/kunwei/step5/"
    "step5d_strict_rnn_autotune_v3_r034.urp"
)
FULL_DASHBOARD_GET = (
    "is in remote control",
    "safetymode",
    "robotmode",
    "running",
    "programState",
    "get loaded program",
)
REQUEST_FIELDS = frozenset(
    {
        "schema",
        "request_uid",
        "enqueue_sequence",
        "occurrence_nonce",
        "control_candidate_uid",
        "normalized_overlay_sha256",
        "overlay",
        "source",
        "position",
    }
)
PACKET_IDENTITY_FIELDS = (
    "campaign_epoch",
    "trial_id",
    "candidate_token",
    "execution_profile_id",
    "command_seq",
)
TERMINAL_HOST_PACKET_FIELDS = PACKET_IDENTITY_FIELDS + ("command",)
TERMINAL_RTDE_PACKET_FIELDS = (
    ("ur_output_int_register_24", "campaign_epoch"),
    ("ur_output_int_register_25", "trial_id"),
    ("ur_output_int_register_27", "candidate_token"),
    ("ur_output_int_register_29", "execution_profile_id"),
    ("ur_output_int_register_30", "command_seq"),
    ("ur_output_int_register_34", "logical_batch_sequence"),
    ("ur_output_int_register_31", "batch_row_index"),
)
TERMINAL_PACKET_FIELDS = {
    "campaign_epoch": "campaign_epoch",
    "trial_id": "trial_id",
    "candidate_token": "candidate_token",
    "execution_profile_id": "execution_profile_id",
    "consumed_command_seq": "command_seq",
    "logical_batch_sequence": "logical_batch_sequence",
    "batch_row_index": "batch_row_index",
}


class OperatorStopError(RuntimeError):
    """A stop precondition, observation, or receipt boundary failed."""


def _fail(message: str) -> NoReturn:
    raise OperatorStopError(message)


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _positive_int(value: Any, role: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(f"{role} must be a positive integer")
    return value


def _strict_float(value: Any, role: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise OperatorStopError(f"{role} must be numeric") from exc
    if not math.isfinite(parsed):
        _fail(f"{role} must be finite")
    return parsed


def _strict_csv_int(value: Any, role: str) -> int:
    if isinstance(value, bool):
        _fail(f"{role} must be an integer")
    text = str(value).strip()
    if not text or text.startswith(("+", "-")) and not text[1:].isdigit():
        _fail(f"{role} must be an integer")
    try:
        return int(text, 10)
    except ValueError as exc:
        raise OperatorStopError(f"{role} must be an integer") from exc


def _real_directory(path: Path, role: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        _fail(f"{role} must be an existing real directory")
    resolved = candidate.resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_dir():
        _fail(f"{role} must resolve to a real directory")
    return resolved


def _real_file(path: Path, role: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        _fail(f"{role} must be a real regular file")
    try:
        mode = candidate.stat().st_mode
    except OSError as exc:
        raise OperatorStopError(f"{role} cannot be stat'ed") from exc
    if not stat.S_ISREG(mode):
        _fail(f"{role} must be a regular file")
    return candidate.resolve(strict=True)


def _check_fresh(path: Path, *, now_ns: int, max_age_s: float, role: str) -> None:
    try:
        modified_ns = path.stat().st_mtime_ns
    except OSError as exc:
        raise OperatorStopError(f"{role} freshness cannot be read") from exc
    age_s = (now_ns - modified_ns) / 1_000_000_000.0
    if age_s < -0.25 or age_s > max_age_s:
        _fail(f"{role} is stale or from the future: age_s={age_s:.6f}")


def _json_files(directory: Path, role: str, *, expected_count: int) -> tuple[Path, ...]:
    if directory.is_symlink() or not directory.is_dir():
        _fail(f"{role} must be a real directory")
    entries = tuple(sorted(directory.iterdir(), key=lambda item: item.name))
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            _fail(f"{role} contains an unsafe entry: {entry.name}")
    files = tuple(entry for entry in entries if entry.suffix == ".json")
    if len(files) != expected_count:
        _fail(f"{role} must contain exactly {expected_count} JSON record(s)")
    return files


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class StopPolicy:
    """Typed, bounded operator handoff policy."""

    min_stationary_dwell_s: float = 0.5
    max_gap_s: float = 0.2
    max_linear_tcp_speed_m_s: float = 0.001
    max_angular_tcp_speed_rad_s: float = 0.01
    freshness_s: float = 5.0
    dashboard_timeout_s: float = 2.0
    post_poll_timeout_s: float = 2.0
    post_poll_interval_s: float = 0.05
    dashboard_port: int = 29999

    def __post_init__(self) -> None:
        bounds = {
            "min_stationary_dwell_s": (0.0, 60.0),
            "max_gap_s": (0.0, 10.0),
            "max_linear_tcp_speed_m_s": (0.0, 1.0),
            "max_angular_tcp_speed_rad_s": (0.0, 5.0),
            "freshness_s": (0.0, 300.0),
            "dashboard_timeout_s": (0.01, 30.0),
            "post_poll_timeout_s": (0.01, 30.0),
            "post_poll_interval_s": (0.001, 1.0),
        }
        for name, (lower, upper) in bounds.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            value = float(value)
            if not math.isfinite(value) or not lower < value <= upper:
                raise ValueError(f"{name} must be in ({lower}, {upper}]")
        if self.post_poll_interval_s > self.post_poll_timeout_s:
            raise ValueError("post_poll_interval_s cannot exceed post_poll_timeout_s")
        if (
            isinstance(self.dashboard_port, bool)
            or not isinstance(self.dashboard_port, int)
            or not 1 <= self.dashboard_port <= 65535
        ):
            raise ValueError("dashboard_port must be in [1, 65535]")


@dataclass(frozen=True)
class QueueGate:
    root: Path
    state: Mapping[str, Any]
    request: Mapping[str, Any]
    dispatch: Mapping[str, Any]
    terminal_receipt: Mapping[str, Any]

    @property
    def packet(self) -> Mapping[str, Any]:
        packet = self.dispatch.get("packet")
        if not isinstance(packet, Mapping):
            _fail("dispatch packet is missing")
        return packet


@dataclass(frozen=True)
class StationaryDwell:
    csv_path: str
    row_count: int
    terminal_row_count: int
    first_t_wall_ns: int
    last_t_wall_ns: int
    first_ur_timestamp: float
    last_ur_timestamp: float
    duration_s: float
    max_gap_s: float
    max_linear_tcp_speed_m_s: float
    max_angular_tcp_speed_rad_s: float
    freshness_age_s: float
    identity: Mapping[str, Any]


@dataclass(frozen=True)
class DashboardGate:
    raw: Mapping[str, Any]
    remote_control: bool
    safety: str
    robotmode: str
    running: bool
    program_state: str
    loaded_program: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": dict(self.raw),
            "remote_control": self.remote_control,
            "safety": self.safety,
            "robotmode": self.robotmode,
            "running": self.running,
            "program_state": self.program_state,
            "loaded_program": self.loaded_program,
        }


def _validate_queue_gate_locked(
    root: Path,
    *,
    expected_overlay_sha256: str,
    expected_control_candidate_uid: str,
    expected_source: str,
    now_ns: int,
    freshness_s: float,
) -> QueueGate:
    if not _is_sha256(expected_overlay_sha256):
        _fail("expected normalized overlay SHA-256 is invalid")
    if not isinstance(expected_control_candidate_uid, str) or not expected_control_candidate_uid:
        _fail("expected control candidate UID is invalid")
    if not isinstance(expected_source, str) or not expected_source:
        _fail("expected source is invalid")

    state = load_state(root)
    if state.get("revision") != 1:
        _fail(f"queue revision must be 1, got {state.get('revision')!r}")
    if state.get("dispatch_sequence") != 1:
        _fail("queue dispatch_sequence must be 1")
    if state.get("inflight") is not None:
        _fail("queue inflight must be null")

    request_paths = _json_files(root / "requests", "queue requests", expected_count=1)
    dispatch_paths = _json_files(root / "dispatches", "queue dispatches", expected_count=1)
    terminal_paths = _json_files(
        root / "governance" / "terminal_receipts",
        "governance terminal receipts",
        expected_count=1,
    )
    for path, role in (
        (root / "state.json", "queue state"),
        (terminal_paths[0], "governance terminal receipt"),
    ):
        _check_fresh(path, now_ns=now_ns, max_age_s=freshness_s, role=role)

    requests = tuple(_visible_requests(root, state))
    pending = tuple(_pending(root, state))
    if len(requests) != 1:
        _fail("queue must expose exactly one request")
    if pending:
        _fail("queue authoritative pending requests must be empty")
    request = requests[0]
    if set(request) != REQUEST_FIELDS:
        _fail("P01 request fields differ")
    if request.get("schema") != REQUEST_SCHEMA:
        _fail("P01 request schema differs")
    if not isinstance(request.get("request_uid"), str) or not request["request_uid"]:
        _fail("P01 request_uid is invalid")
    if not isinstance(request.get("occurrence_nonce"), str) or not request["occurrence_nonce"]:
        _fail("P01 occurrence_nonce is invalid")
    if request.get("normalized_overlay_sha256") != expected_overlay_sha256:
        _fail("P01 normalized overlay SHA-256 differs")
    if request.get("control_candidate_uid") != expected_control_candidate_uid:
        _fail("P01 control candidate UID differs")
    if request.get("source") != expected_source:
        _fail("P01 source differs")
    if request.get("enqueue_sequence") != 1:
        _fail("P01 enqueue_sequence must be 1")
    if not isinstance(request.get("overlay"), Mapping):
        _fail("P01 overlay is missing")
    if request.get("position") not in {"tail", "next"}:
        _fail("P01 position is invalid")
    stored_request = _strict_json(request_paths[0], "P01 request")
    if stored_request != request:
        _fail("visible P01 request differs from its stored bytes")

    dispatch = _strict_json(dispatch_paths[0], "P01 dispatch")
    if dispatch.get("schema") != DISPATCH_SCHEMA:
        _fail("P01 dispatch schema differs")
    if dispatch.get("dispatch_sequence") != 1:
        _fail("P01 dispatch sequence must be 1")
    dispatch_identity = dispatch.get("dispatch_identity")
    if (
        not isinstance(dispatch_identity, str)
        or not dispatch_identity.startswith("dispatch:v1:")
    ):
        _fail("P01 dispatch identity is invalid")
    if isinstance(dispatch.get("request"), Mapping) and dispatch["request"] != request:
        _fail("P01 dispatch request differs")
    if dispatch.get("request_uid") is not None and dispatch.get("request_uid") != request["request_uid"]:
        _fail("P01 dispatch request_uid differs")
    packet = dispatch.get("packet")
    if not isinstance(packet, Mapping):
        _fail("P01 dispatch packet is missing")
    for field in PACKET_IDENTITY_FIELDS:
        _positive_int(packet.get(field), f"P01 packet {field}")
    for field in ("logical_batch_sequence", "batch_row_index"):
        _positive_int(packet.get(field), f"P01 packet {field}")
    if packet.get("command") is not None and packet.get("command") != 1:
        _fail("P01 packet command is not ARM")

    terminal_receipts = tuple(_load_terminal_receipts(root))
    if len(terminal_receipts) != 1:
        _fail("queue must contain exactly one terminal governance receipt")
    terminal_receipt = terminal_receipts[0]
    if terminal_receipt.get("schema") != TERMINAL_RECEIPT_SCHEMA:
        _fail("terminal governance receipt schema differs")
    if terminal_receipt.get("dispatch_sequence") != 1:
        _fail("terminal governance receipt sequence differs")
    if terminal_receipt.get("dispatch_identity") != dispatch_identity:
        _fail("terminal governance receipt dispatch identity differs")
    terminal_state = terminal_receipt.get("terminal_state")
    if not isinstance(terminal_state, Mapping):
        _fail("terminal governance state is missing")
    if terminal_state.get("state") != 78:
        _fail("terminal governance state must be 78")
    for terminal_field, packet_field in TERMINAL_PACKET_FIELDS.items():
        if terminal_state.get(terminal_field) != packet.get(packet_field):
            _fail(f"terminal {terminal_field} differs from packet {packet_field}")

    return QueueGate(
        root=root,
        state=dict(state),
        request=dict(request),
        dispatch=dict(dispatch),
        terminal_receipt=dict(terminal_receipt),
    )


def validate_queue_gate(
    queue_root: Path,
    *,
    expected_overlay_sha256: str,
    expected_control_candidate_uid: str,
    expected_source: str,
    now_ns: int | None = None,
    freshness_s: float = 5.0,
) -> QueueGate:
    """Validate the queue snapshot while holding its one canonical lock."""

    root = _real_directory(queue_root, "queue root")
    observed_now_ns = time.time_ns() if now_ns is None else _positive_int(now_ns, "queue now_ns")
    if not math.isfinite(float(freshness_s)) or not 0.0 < float(freshness_s) <= 300.0:
        _fail("queue freshness_s is outside its strict bound")
    with _lock(root):
        return _validate_queue_gate_locked(
            root,
            expected_overlay_sha256=expected_overlay_sha256,
            expected_control_candidate_uid=expected_control_candidate_uid,
            expected_source=expected_source,
            now_ns=observed_now_ns,
            freshness_s=float(freshness_s),
        )


def _csv_identity_value(value: Any, expected: Any, role: str) -> None:
    if isinstance(expected, bool):
        if str(value).strip().lower() != str(expected).lower():
            _fail(f"{role} differs")
        return
    if isinstance(expected, int):
        if _strict_csv_int(value, role) != expected:
            _fail(f"{role} differs")
        return
    if str(value).strip() != str(expected):
        _fail(f"{role} differs")


def _row_terminal(row: Mapping[str, Any], index: int) -> bool:
    runtime = _strict_csv_int(row.get("ur_runtime_state"), f"CSV row {index} ur_runtime_state")
    safety = _strict_csv_int(row.get("ur_safety_mode"), f"CSV row {index} ur_safety_mode")
    state = _strict_csv_int(row.get("ur_output_int_register_26"), f"CSV row {index} state")
    return runtime == 2 and safety == 1 and state == 78


def validate_stationary_dwell(
    csv_path: Path,
    *,
    queue_gate: QueueGate,
    policy: StopPolicy = StopPolicy(),
    now_ns: int | None = None,
) -> StationaryDwell:
    """Validate the fresh terminal tail of one P01 bridge CSV."""

    path = _real_file(csv_path, "bridge CSV")
    observed_now_ns = time.time_ns() if now_ns is None else _positive_int(now_ns, "CSV now_ns")
    _check_fresh(path, now_ns=observed_now_ns, max_age_s=policy.freshness_s, role="bridge CSV")
    try:
        with path.open("r", newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or any(not field for field in reader.fieldnames):
                _fail("bridge CSV header is missing or contains an empty field")
            rows = []
            for index, row in enumerate(reader, start=1):
                if None in row or any(value is None for value in row.values()):
                    _fail(f"bridge CSV row {index} is malformed")
                rows.append(dict(row))
    except UnicodeError as exc:
        raise OperatorStopError("bridge CSV is not strict UTF-8") from exc
    except OSError as exc:
        raise OperatorStopError("bridge CSV cannot be read") from exc
    if not rows:
        _fail("bridge CSV has no rows")

    terminal_start = len(rows) - 1
    while terminal_start >= 0 and _row_terminal(rows[terminal_start], terminal_start + 1):
        terminal_start -= 1
    terminal_start += 1
    if terminal_start >= len(rows):
        _fail("bridge CSV tail is not a terminal state-78 dwell")
    terminal_rows = rows[terminal_start:]
    if len(terminal_rows) < 2:
        _fail("terminal state-78 dwell needs at least two rows")

    packet = queue_gate.packet
    request = queue_gate.request
    if "command" not in packet:
        _fail("P01 dispatch packet command is missing")
    identity: dict[str, Any] = {key: packet[key] for key in PACKET_IDENTITY_FIELDS}
    identity["command"] = packet["command"]
    identity["dispatch_sequence"] = queue_gate.dispatch.get("dispatch_sequence")
    identity["dispatch_identity"] = queue_gate.dispatch.get("dispatch_identity")
    identity["request_uid"] = request.get("request_uid")
    identity["control_candidate_uid"] = request.get("control_candidate_uid")
    for index, row in enumerate(terminal_rows, start=terminal_start + 1):
        for key in TERMINAL_HOST_PACKET_FIELDS:
            if key not in row:
                _fail(f"CSV terminal row {index} lacks host field {key}")
            _csv_identity_value(row[key], packet[key], f"CSV terminal row {index} {key}")
        for register, packet_field in TERMINAL_RTDE_PACKET_FIELDS:
            if register not in row:
                _fail(f"CSV terminal row {index} lacks RTDE echo register {register}")
            _csv_identity_value(
                row[register],
                packet[packet_field],
                f"CSV terminal row {index} {register}",
            )
        if _strict_csv_int(row.get("ur_output_int_register_26"), f"CSV terminal row {index} state") != 78:
            _fail(f"CSV terminal row {index} state must be 78")

    wall_times: list[int] = []
    ur_times: list[float] = []
    linear_speeds: list[float] = []
    angular_speeds: list[float] = []
    for index, row in enumerate(terminal_rows, start=terminal_start + 1):
        wall = _strict_csv_int(row.get("t_wall_ns"), f"CSV terminal row {index} t_wall_ns")
        if wall < 1:
            _fail(f"CSV terminal row {index} t_wall_ns is not positive")
        ur_timestamp = _strict_float(
            row.get("ur_timestamp"), f"CSV terminal row {index} ur_timestamp"
        )
        wall_times.append(wall)
        ur_times.append(ur_timestamp)
        linear_components = [
            _strict_float(
                row.get(f"ur_actual_TCP_speed_{axis}"),
                f"CSV terminal row {index} TCP linear speed {axis}",
            )
            for axis in range(3)
        ]
        angular_components = [
            _strict_float(
                row.get(f"ur_actual_TCP_speed_{axis}"),
                f"CSV terminal row {index} TCP angular speed {axis}",
            )
            for axis in range(3, 6)
        ]
        linear_speeds.append(math.sqrt(sum(value * value for value in linear_components)))
        angular_speeds.append(math.sqrt(sum(value * value for value in angular_components)))
        if index > terminal_start + 1 and (
            wall <= wall_times[-2] or ur_timestamp <= ur_times[-2]
        ):
            _fail("terminal bridge CSV timestamps must be strictly monotonic")

    first_wall = wall_times[0]
    last_wall = wall_times[-1]
    duration_s = (last_wall - first_wall) / 1_000_000_000.0
    gaps = [
        (wall_times[index] - wall_times[index - 1]) / 1_000_000_000.0
        for index in range(1, len(wall_times))
    ]
    max_gap_s = max(gaps, default=0.0)
    max_linear = max(linear_speeds)
    max_angular = max(angular_speeds)
    freshness_age_s = (observed_now_ns - last_wall) / 1_000_000_000.0
    if freshness_age_s < -0.25 or freshness_age_s > policy.freshness_s:
        _fail("bridge CSV terminal timestamp is stale or from the future")
    if duration_s < policy.min_stationary_dwell_s:
        _fail(f"terminal dwell duration {duration_s:.6f}s is below policy")
    if max_gap_s > policy.max_gap_s:
        _fail(f"terminal dwell max gap {max_gap_s:.6f}s exceeds policy")
    if max_linear > policy.max_linear_tcp_speed_m_s:
        _fail(f"terminal linear TCP speed {max_linear:.9f} exceeds policy")
    if max_angular > policy.max_angular_tcp_speed_rad_s:
        _fail(f"terminal angular TCP speed {max_angular:.9f} exceeds policy")

    return StationaryDwell(
        csv_path=str(path),
        row_count=len(rows),
        terminal_row_count=len(terminal_rows),
        first_t_wall_ns=first_wall,
        last_t_wall_ns=last_wall,
        first_ur_timestamp=ur_times[0],
        last_ur_timestamp=ur_times[-1],
        duration_s=duration_s,
        max_gap_s=max_gap_s,
        max_linear_tcp_speed_m_s=max_linear,
        max_angular_tcp_speed_rad_s=max_angular,
        freshness_age_s=freshness_age_s,
        identity=identity,
    )


def _dashboard_value(
    observation: Mapping[str, Any],
    names: Sequence[str],
    role: str,
) -> Any:
    for name in names:
        if name in observation:
            return observation[name]
    _fail(f"Dashboard full GET lacks {role}")


def _dashboard_bool(value: Any, role: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if ":" in text:
        text = text.rsplit(":", 1)[1].strip()
    if text == "true":
        return True
    if text == "false":
        return False
    _fail(f"Dashboard {role} is not boolean")


def _dashboard_state(value: Any, role: str) -> str:
    text = str(value).strip()
    if ":" in text:
        text = text.rsplit(":", 1)[1].strip()
    state = text.split()[0].upper() if text else ""
    if state not in {"PLAYING", "STOPPED", "PAUSED"}:
        _fail(f"Dashboard {role} is invalid")
    return state


def _dashboard_mode(value: Any, role: str) -> str:
    text = str(value).strip()
    if ":" in text:
        text = text.rsplit(":", 1)[1].strip()
    if not text:
        _fail(f"Dashboard {role} is empty")
    return text.upper().split()[0]


def _dashboard_program(value: Any) -> str:
    text = str(value).strip()
    if text.startswith("Loaded program:"):
        text = text[len("Loaded program:") :].strip()
    if not text.startswith("/") or "\n" in text or "\r" in text:
        _fail("Dashboard loaded program is not an absolute path")
    return text


def validate_dashboard(
    observation: Mapping[str, Any],
    *,
    expected_target: str,
    expected_running: bool,
    expected_program_state: str,
) -> DashboardGate:
    """Validate one complete Dashboard GET without performing I/O."""

    if not isinstance(observation, Mapping):
        _fail("Dashboard observation must be a mapping")
    if not isinstance(expected_target, str) or expected_target != EXPECTED_CONTROLLER_TARGET:
        _fail("Dashboard target is not the exact r034 controller path")
    remote = _dashboard_bool(
        _dashboard_value(observation, ("is in remote control", "remote_control"), "remote control"),
        "remote control",
    )
    safety = _dashboard_mode(
        _dashboard_value(observation, ("safetymode", "safety_mode", "Safety"), "Safety"),
        "Safety",
    )
    robotmode = _dashboard_mode(
        _dashboard_value(observation, ("robotmode", "robot_mode"), "robotmode"),
        "robotmode",
    )
    running = _dashboard_bool(
        _dashboard_value(observation, ("running", "program_running"), "running"),
        "running",
    )
    state = _dashboard_state(
        _dashboard_value(observation, ("programState", "program_state"), "programState"),
        "programState",
    )
    loaded = _dashboard_program(
        _dashboard_value(observation, ("get loaded program", "loaded_program"), "loaded program")
    )
    expected_state = str(expected_program_state).upper()
    if expected_state not in {"PLAYING", "STOPPED"}:
        _fail("expected Dashboard programState is invalid")
    if remote is not True:
        _fail("Dashboard Remote control is not true")
    if safety != "NORMAL":
        _fail("Dashboard Safety is not NORMAL")
    if robotmode != "RUNNING":
        _fail("Dashboard robotmode is not RUNNING")
    if running is not bool(expected_running):
        _fail(f"Dashboard running expected {expected_running!r}, got {running!r}")
    if state != expected_state:
        _fail(f"Dashboard programState expected {expected_state}, got {state}")
    if loaded != expected_target:
        _fail("Dashboard loaded program is not the exact r034 target")
    return DashboardGate(
        raw=dict(observation),
        remote_control=remote,
        safety=safety,
        robotmode=robotmode,
        running=running,
        program_state=state,
        loaded_program=loaded,
    )


def _default_dashboard_reader(
    robot_host: str,
    commands: Sequence[str],
    *,
    timeout_s: float,
) -> Mapping[str, Any]:
    from step5d_autotune_v3.dashboard import dashboard_exchange

    return dashboard_exchange(
        robot_host,
        list(commands),
        port=29999,
        timeout=timeout_s,
    )


def _default_writer_owner_reader() -> Mapping[str, Any] | None:
    from ur10e_parallel import ResourceProfile, writer_lease_owner

    return writer_lease_owner(ResourceProfile.from_env())


def _discover_experiment_root(queue_root: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        return _real_directory(explicit, "experiment root")
    for candidate in (queue_root, *queue_root.parents):
        pointer = candidate / "config/step5d/current.json"
        if not pointer.is_symlink() and pointer.is_file():
            return _real_directory(candidate, "experiment root")
    return queue_root


def _lease_value(lease: Any, name: str, *aliases: str) -> Any:
    names = (name, *aliases)
    for candidate in names:
        if isinstance(lease, Mapping) and candidate in lease:
            return lease[candidate]
        if hasattr(lease, candidate):
            return getattr(lease, candidate)
    return None


def _lease_record(lease: Any) -> dict[str, Any]:
    document = getattr(lease, "document", None)
    if isinstance(document, Mapping):
        return dict(document)
    if isinstance(lease, Mapping):
        return dict(lease)
    return {"repr": repr(lease)}


def _release_value(release: Any, name: str) -> Any:
    if isinstance(release, Mapping):
        return release.get(name)
    return getattr(release, name, None)


def _validate_lease_and_release(
    *,
    root: Path,
    queue_gate: QueueGate,
    lease_path: Path,
    process_starttime_reader: Callable[[int], int | None],
    writer_owner_reader: Callable[[], Mapping[str, Any] | None],
    lease_loader: Callable[[Path], Any],
    release_loader: Callable[[Path], Any],
) -> tuple[dict[str, Any], dict[str, Any], Any, Any]:
    lease_file = _real_file(lease_path, "campaign lease")
    lease = lease_loader(lease_file)
    campaign_id = _lease_value(lease, "campaign_id")
    manifest_sha = _lease_value(lease, "manifest_sha256")
    campaign_fingerprint = _lease_value(lease, "campaign_fingerprint")
    supervisor_pid = _lease_value(lease, "supervisor_pid")
    supervisor_starttime = _lease_value(
        lease,
        "supervisor_starttime",
        "supervisor_starttime_ticks",
    )
    if campaign_id != queue_gate.state.get("campaign_id"):
        _fail("campaign lease campaign_id differs from queue")
    _positive_int(supervisor_pid, "campaign lease supervisor PID")
    _positive_int(supervisor_starttime, "campaign lease supervisor starttime")
    observed_starttime = process_starttime_reader(supervisor_pid)
    if observed_starttime != supervisor_starttime:
        _fail("campaign lease supervisor PID/starttime is not alive and exact")
    if not _is_sha256(str(manifest_sha)) or not _is_sha256(str(campaign_fingerprint)):
        _fail("campaign lease release/fingerprint is invalid")

    release = release_loader(root)
    program_id = _release_value(release, "program_id")
    target = _release_value(release, "controller_target")
    release_manifest_sha = _release_value(release, "manifest_sha256")
    if program_id != EXPECTED_PROGRAM_ID or target != EXPECTED_CONTROLLER_TARGET:
        _fail("current release is not the exact r034 target")
    if release_manifest_sha != manifest_sha:
        _fail("campaign lease manifest SHA differs from current release")
    source_fingerprints = _release_value(release, "source_fingerprints")
    if not isinstance(source_fingerprints, Mapping):
        _fail("current release source closure is missing")
    launcher_sha = source_fingerprints.get("scripts/step5d-autotune-v3.sh")
    if not _is_sha256(launcher_sha):
        _fail("current r034 launcher source fingerprint is missing")
    release_record = {
        "program_id": program_id,
        "controller_target": target,
        "manifest_sha256": release_manifest_sha,
        "release_stage_id": _release_value(release, "release_stage_id"),
        "launcher_sha256": launcher_sha,
        "source_fingerprint": _canonical_sha256(dict(source_fingerprints)),
    }

    owner = writer_owner_reader()
    if not isinstance(owner, Mapping):
        _fail("writer_lease_owner is unavailable")
    if owner.get("schema") != "ur10e/live-writer-lease-owner-v1":
        _fail("writer_lease_owner schema differs")
    owner_pid = owner.get("pid")
    owner_starttime = owner.get("starttime_ticks")
    if owner_pid != supervisor_pid or owner_starttime != supervisor_starttime:
        _fail("writer_lease_owner does not match campaign supervisor identity")
    if owner.get("task") != EXPECTED_TASK:
        _fail("writer_lease_owner task differs")
    if process_starttime_reader(owner_pid) != owner_starttime:
        _fail("writer_lease_owner PID/starttime is not alive and exact")
    return (
        _lease_record(lease),
        dict(owner),
        lease,
        release,
    )


def _writer_binding(writer: Any, target: str) -> dict[str, Any]:
    binding = getattr(writer, "binding", None)
    if callable(binding):
        binding = binding()
    if isinstance(writer, RemoteDashboardWriter):
        binding = {
            "writer": type(writer).__name__,
            "host": writer.host,
            "load_target": writer.load_target,
            "port": writer.port,
            "timeout": writer.timeout_s,
            "command": "stop",
        }
    if binding is None:
        binding = {"writer": type(writer).__name__, "target_path": target}
    if not isinstance(binding, Mapping):
        _fail("RemoteDashboardWriter binding is not a mapping")
    result = dict(binding)
    observed_target = result.get("load_target", result.get("target_path"))
    if observed_target != target:
        _fail("RemoteDashboardWriter binding target differs")
    result.setdefault("command", "stop")
    return result


def _writer_result(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return dict(result)
    if hasattr(result, "__dict__"):
        return dict(vars(result))
    return {"result": str(result)}


def _post_dashboard_poll(
    *,
    robot_host: str,
    target: str,
    policy: StopPolicy,
    dashboard_reader: Callable[..., Mapping[str, Any]],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> tuple[DashboardGate, int, Mapping[str, Any]]:
    started = monotonic()
    deadline = started + policy.post_poll_timeout_s
    attempts = 0
    max_attempts = max(1, math.ceil(policy.post_poll_timeout_s / policy.post_poll_interval_s) + 1)
    last_error: Exception | None = None
    while attempts < max_attempts:
        attempts += 1
        try:
            raw = dashboard_reader(
                robot_host,
                FULL_DASHBOARD_GET,
                timeout_s=policy.dashboard_timeout_s,
            )
            gate = validate_dashboard(
                raw,
                expected_target=target,
                expected_running=False,
                expected_program_state="STOPPED",
            )
            return gate, attempts, raw
        except Exception as exc:  # bounded observer retry; no write is retried
            last_error = exc
        now = monotonic()
        if now >= deadline:
            break
        sleep(min(policy.post_poll_interval_s, max(0.0, deadline - now)))
    detail = "post Dashboard did not observe running=false/programState=STOPPED"
    if last_error is not None:
        detail = f"{detail}: {last_error}"
    raise OperatorStopError(detail)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    target = Path(path).expanduser()
    if target.exists() and (target.is_symlink() or not target.is_file()):
        _fail("operator-stop receipt target is unsafe")
    encoded = (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, target)
    directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _operator_identity(
    process_starttime_reader: Callable[[int], int | None],
) -> dict[str, int]:
    pid = os.getpid()
    starttime = process_starttime_reader(pid)
    if starttime is None:
        _fail("operator PID/starttime is unavailable")
    return {"pid": _positive_int(pid, "operator PID"), "starttime": _positive_int(starttime, "operator starttime")}


def governed_operator_stop(
    *,
    queue_root: Path,
    experiment_root: Path | None = None,
    bridge_csv: Path,
    campaign_lease: Path,
    robot_host: str,
    receipt_path: Path,
    expected_overlay_sha256: str,
    expected_control_candidate_uid: str,
    expected_source: str,
    policy: StopPolicy = StopPolicy(),
    execute: bool = False,
    dashboard_reader: Callable[..., Mapping[str, Any]] | None = None,
    writer_factory: Callable[[str], Any] | None = None,
    process_starttime_reader: Callable[[int], int | None] | None = None,
    writer_owner_reader: Callable[[], Mapping[str, Any] | None] | None = None,
    lease_loader: Callable[[Path], Any] | None = None,
    release_loader: Callable[[Path], Any] | None = None,
    time_ns: Callable[[], int] = time.time_ns,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run validation and, only with ``execute=True``, one governed stop."""

    root = _real_directory(queue_root, "queue root")
    release_root = _discover_experiment_root(root, experiment_root)
    if not isinstance(robot_host, str) or not robot_host:
        _fail("robot_host is empty")
    if not isinstance(policy, StopPolicy):
        _fail("policy must be StopPolicy")
    process_reader = process_starttime if process_starttime_reader is None else process_starttime_reader
    owner_reader = _default_writer_owner_reader if writer_owner_reader is None else writer_owner_reader
    load_lease = load_campaign_lease if lease_loader is None else lease_loader
    load_release = load_current_release if release_loader is None else release_loader
    observed_now_ns = _positive_int(time_ns(), "operator-stop now_ns")
    operator: dict[str, int] | None = None
    try:
        operator = _operator_identity(process_reader)
    except Exception:
        operator = None
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "mode": "execute" if execute else "validate",
        "timestamps_ns": {"started_at_ns": observed_now_ns},
        "operator": operator,
        "queue_root": str(root),
        "experiment_root": str(release_root),
        "request": None,
        "dispatch": None,
        "terminal": None,
        "campaign": None,
        "release": None,
        "lease": None,
        "writer_lease_owner": None,
        "csv_dwell": None,
        "dashboard": {"pre": None, "post": None, "post_poll_attempts": 0},
        "writer": {"constructed": False, "binding": None, "outcome": None},
        "outcome": {"status": "NOT_STARTED", "command": "stop", "command_sent": False},
    }
    command_sent = False

    def finish_receipt(*, status: str, detail: str | None = None) -> dict[str, Any]:
        receipt["timestamps_ns"]["finished_at_ns"] = _positive_int(time_ns(), "receipt finished_at_ns")
        result = dict(receipt["outcome"])
        result["status"] = status
        result["command_sent"] = command_sent
        if detail:
            result["detail"] = detail
        receipt["outcome"] = result
        _atomic_json(Path(receipt_path), receipt)
        return receipt

    try:
        if operator is None:
            _fail("operator PID/starttime is unavailable")
        with _lock(root):
            queue_gate = _validate_queue_gate_locked(
                root,
                expected_overlay_sha256=expected_overlay_sha256,
                expected_control_candidate_uid=expected_control_candidate_uid,
                expected_source=expected_source,
                now_ns=observed_now_ns,
                freshness_s=policy.freshness_s,
            )
            receipt["request"] = {
                "request_uid": queue_gate.request.get("request_uid"),
                "control_candidate_uid": queue_gate.request.get("control_candidate_uid"),
                "normalized_overlay_sha256": queue_gate.request.get("normalized_overlay_sha256"),
                "source": queue_gate.request.get("source"),
            }
            receipt["dispatch"] = {
                "dispatch_sequence": queue_gate.dispatch.get("dispatch_sequence"),
                "dispatch_identity": queue_gate.dispatch.get("dispatch_identity"),
                "packet": dict(queue_gate.packet),
            }
            receipt["terminal"] = {
                "dispatch_sequence": queue_gate.terminal_receipt.get("dispatch_sequence"),
                "dispatch_identity": queue_gate.terminal_receipt.get("dispatch_identity"),
                "terminal_state": dict(queue_gate.terminal_receipt.get("terminal_state", {})),
            }
            lease_record, owner_record, lease, release = _validate_lease_and_release(
                    root=release_root,
                    queue_gate=queue_gate,
                    lease_path=campaign_lease,
                    process_starttime_reader=process_reader,
                    writer_owner_reader=owner_reader,
                    lease_loader=load_lease,
                    release_loader=load_release,
            )
            receipt["lease"] = lease_record
            receipt["writer_lease_owner"] = owner_record
            receipt["campaign"] = {
                "campaign_id": _lease_value(lease, "campaign_id"),
                "campaign_fingerprint": _lease_value(lease, "campaign_fingerprint"),
                "manifest_sha256": _lease_value(lease, "manifest_sha256"),
            }
            receipt["release"] = {
                "program_id": _release_value(release, "program_id"),
                "release_stage_id": _release_value(release, "release_stage_id"),
                "controller_target": _release_value(release, "controller_target"),
                "manifest_sha256": _release_value(release, "manifest_sha256"),
                "launcher_sha256": _release_value(release, "source_fingerprints").get(
                    "scripts/step5d-autotune-v3.sh"
                ),
                "source_fingerprint": _canonical_sha256(
                    dict(_release_value(release, "source_fingerprints"))
                ),
            }
            dwell = validate_stationary_dwell(
                bridge_csv,
                queue_gate=queue_gate,
                policy=policy,
                now_ns=observed_now_ns,
            )
            receipt["csv_dwell"] = asdict(dwell)

            reader = _default_dashboard_reader if dashboard_reader is None else dashboard_reader
            pre_raw = reader(
                robot_host,
                FULL_DASHBOARD_GET,
                timeout_s=policy.dashboard_timeout_s,
            )
            pre = validate_dashboard(
                pre_raw,
                expected_target=EXPECTED_CONTROLLER_TARGET,
                expected_running=True,
                expected_program_state="PLAYING",
            )
            receipt["dashboard"]["pre"] = pre.as_dict()
            if not execute:
                receipt["writer"] = {"constructed": False, "binding": None, "outcome": None}
                return finish_receipt(status="VALIDATED_NO_EXECUTE")

            if writer_factory is None:
                factory = lambda target: RemoteDashboardWriter(
                    robot_host,
                    load_target=target,
                    port=policy.dashboard_port,
                    timeout_s=policy.dashboard_timeout_s,
                    monotonic=monotonic,
                )
            else:
                factory = writer_factory
            writer = factory(EXPECTED_CONTROLLER_TARGET)
            binding = _writer_binding(writer, EXPECTED_CONTROLLER_TARGET)
            receipt["writer"] = {
                "constructed": True,
                "binding": binding,
                "outcome": None,
            }
            try:
                write_result = writer.write("stop")
                write_record = _writer_result(write_result)
                command_sent = bool(write_record.get("command_sent", False))
                receipt["writer"]["outcome"] = write_record
            except Exception as exc:
                command_sent = bool(getattr(exc, "command_sent", False))
                response = getattr(exc, "response", None)
                receipt["writer"]["outcome"] = {
                    "command": "stop",
                    "command_sent": command_sent,
                    "response": response,
                    "error": str(exc),
                }
                raise
            if not command_sent:
                _fail("RemoteDashboardWriter did not report command_sent")
            response = receipt["writer"]["outcome"].get("response")
            if not isinstance(response, str) or not response.startswith("Stopped"):
                _fail("RemoteDashboardWriter response does not start with Stopped")
            post, attempts, post_raw = _post_dashboard_poll(
                robot_host=robot_host,
                target=EXPECTED_CONTROLLER_TARGET,
                policy=policy,
                dashboard_reader=reader,
                monotonic=monotonic,
                sleep=sleep,
            )
            receipt["dashboard"]["post"] = post.as_dict()
            receipt["dashboard"]["post_poll_attempts"] = attempts
            return finish_receipt(status="STOPPED")
    except Exception as exc:
        status = "AMBIGUOUS" if command_sent else "FAILED"
        try:
            finish_receipt(status=status, detail=str(exc))
        except Exception as receipt_error:
            raise OperatorStopError(
                f"operator-stop failed and receipt write failed: {receipt_error}"
            ) from exc
        if isinstance(exc, OperatorStopError):
            raise
        raise OperatorStopError(str(exc)) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-root", type=Path, required=True)
    parser.add_argument("--experiment-root", type=Path)
    parser.add_argument("--bridge-csv", type=Path, required=True)
    parser.add_argument("--campaign-lease", type=Path, required=True)
    parser.add_argument("--robot-host", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--expected-normalized-overlay-sha256",
        "--expected-overlay-sha256",
        dest="expected_overlay_sha256",
        required=True,
    )
    parser.add_argument(
        "--expected-control-candidate-uid",
        "--expected-control-uid",
        dest="expected_control_candidate_uid",
        required=True,
    )
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--execute", action="store_true", help="perform the one Dashboard stop")
    parser.add_argument("--min-stationary-dwell-s", type=float, default=StopPolicy.min_stationary_dwell_s)
    parser.add_argument("--max-gap-s", type=float, default=StopPolicy.max_gap_s)
    parser.add_argument("--max-linear-tcp-speed-m-s", type=float, default=StopPolicy.max_linear_tcp_speed_m_s)
    parser.add_argument("--max-angular-tcp-speed-rad-s", type=float, default=StopPolicy.max_angular_tcp_speed_rad_s)
    parser.add_argument("--freshness-s", type=float, default=StopPolicy.freshness_s)
    parser.add_argument("--dashboard-timeout-s", type=float, default=StopPolicy.dashboard_timeout_s)
    parser.add_argument("--post-poll-timeout-s", type=float, default=StopPolicy.post_poll_timeout_s)
    parser.add_argument("--post-poll-interval-s", type=float, default=StopPolicy.post_poll_interval_s)
    parser.add_argument("--dashboard-port", type=int, default=StopPolicy.dashboard_port)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        policy = StopPolicy(
            min_stationary_dwell_s=args.min_stationary_dwell_s,
            max_gap_s=args.max_gap_s,
            max_linear_tcp_speed_m_s=args.max_linear_tcp_speed_m_s,
            max_angular_tcp_speed_rad_s=args.max_angular_tcp_speed_rad_s,
            freshness_s=args.freshness_s,
            dashboard_timeout_s=args.dashboard_timeout_s,
            post_poll_timeout_s=args.post_poll_timeout_s,
            post_poll_interval_s=args.post_poll_interval_s,
            dashboard_port=args.dashboard_port,
        )
        result = governed_operator_stop(
            queue_root=args.queue_root,
            experiment_root=args.experiment_root,
            bridge_csv=args.bridge_csv,
            campaign_lease=args.campaign_lease,
            robot_host=args.robot_host,
            receipt_path=args.receipt,
            expected_overlay_sha256=args.expected_overlay_sha256,
            expected_control_candidate_uid=args.expected_control_candidate_uid,
            expected_source=args.expected_source,
            policy=policy,
            execute=args.execute,
        )
    except (OperatorStopError, OSError, ValueError) as exc:
        print(f"operator-stop blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
