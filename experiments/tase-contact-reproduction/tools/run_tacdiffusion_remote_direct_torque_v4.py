#!/usr/bin/env python3
"""Validate or run the bounded Remote-Control Direct Torque v4 canary.

``status`` and ``validate`` are read-only/offline.  ``run`` is fail-closed and
requires separate command-line and signed-artifact gates for URScript send,
RTDE input writes, Direct Torque, physical motion, and the no-contact scope.
This runner never starts the Kunwei stream.
"""

from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
VIC_ROOT = ROOT.parent / "ur10e-variable-impedance"
UR_HELPERS = Path(
    "/home/andy/codex-private-skills-shared-main/skills/ur10e-realsetup/scripts"
)
for import_root in (VIC_ROOT, UR_HELPERS):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from _ur_common import RTDEClient, dashboard_exchange, read_rtde_once  # noqa: E402
from ur10e_parallel import ResourceProfile, writer_lease  # noqa: E402
from ur10e_vic.tacdiffusion.direct_torque_live_v4 import (  # noqa: E402
    COMPILE_PROBE_PROTOCOL_TOKEN,
    LIVE_PROTOCOL_TOKEN,
    WRENCH_FRAME_TOKEN,
    NO_CONTACT_RELEASE_TOLERANCE_M,
    LiveTubeContract,
    build_compile_probe_source,
    parse_compile_probe_source,
    parse_live_receiver_source,
)


AUTHORIZATION_SCHEMA = "ur10e_tacdiffusion_direct_torque_authorization/v1"
COMPILE_PROBE_AUTHORIZATION_SCHEMA = (
    "ur10e_tacdiffusion_direct_torque_compile_probe_authorization/v1"
)
BUNDLE_SCHEMA = "ur10e_tacdiffusion_direct_torque_live_bundle/v1"
REFERENCE_SCHEMA = "ur10e_tacdiffusion_unknown_surface_episode_artifact/v1"
MODE_IDLE = 0
MODE_RUN = 1
MODE_END = 2
MODE_ABORT = 3
STATE_WAITING = 0
STATE_STARTUP = 1
STATE_TORQUE = 2
STATE_SAFE_EXIT = 3
STATE_FAULT = 4
STATE_COMPLETE = 5
COMPILE_PROBE_STATE_ACTIVE = 77
COMPILE_PROBE_STATE_COMPLETE = 78
RUNTIME_STOPPED = 1
RUNTIME_PLAYING = 2
ROBOT_MODE_RUNNING = 7
SAFETY_MODE_NORMAL = 1
FIXED_STIFFNESS = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
ZERO6 = (0.0,) * 6
LIVE_WRITER_TASK = "tacdiffusion-remote-direct-torque-v4"
CANARY_STAGE_HOLD = "hold_100ms"
CANARY_STAGE_RAMP = "ramp_0_2mm_500ms"
CANARY_STAGE_REFERENCE = "reference_2s"
CANARY_STAGE_ORDER = (
    CANARY_STAGE_HOLD,
    CANARY_STAGE_RAMP,
    CANARY_STAGE_REFERENCE,
)
CANARY_STAGE_DURATIONS_S = {
    CANARY_STAGE_HOLD: 0.1,
    CANARY_STAGE_RAMP: 0.5,
    CANARY_STAGE_REFERENCE: 2.0,
}

_LIVE_WRITER_PATTERNS = (
    "run_tacdiffusion_remote_direct_torque_v4.py",
    "run_step5d_tacdiffusion_bridge.py",
    "run_tacdiffusion_bridge.py",
    "run_step5d_tacdiffusion_simulation_probe.py",
    "kunwei_rtde_bridge.py",
    "step5d_tacdiffusion_bridge",
    "ros2 run",
    "roslaunch",
)
_LIVE_WRITER_IGNORED_PATTERNS = ("codex", "pgrep", "pytest")
_PGREP_LIVE_WRITER_PATTERN = (
    "run_tacdiffusion_remote_direct_torque_v4.py|"
    "run_step5d_tacdiffusion_bridge.py|"
    "run_tacdiffusion_bridge.py|"
    "run_step5d_tacdiffusion_simulation_probe.py|"
    "kunwei_rtde_bridge.py|"
    "step5d_tacdiffusion_bridge|"
    "ros2 run|"
    "roslaunch"
)

DOUBLE_INPUT_FIELDS = [f"input_double_register_{index}" for index in range(24, 48)]
INTEGER_INPUT_FIELDS = [f"input_int_register_{index}" for index in range(24, 36)]
INPUT_FIELDS = DOUBLE_INPUT_FIELDS + INTEGER_INPUT_FIELDS
OUTPUT_FIELDS = [
    "timestamp",
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_TCP_force",
    "actual_q",
    "actual_qd",
    "target_moment",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    *[f"output_double_register_{index}" for index in range(24, 44)],
    *[f"output_int_register_{index}" for index in range(24, 34)],
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite6(values: Sequence[float], name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != 6 or not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain six finite values")
    return result


def _parse_time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _sample_translation_error_sqm3(a_pose: Sequence[float], b_pose: Sequence[float]) -> float:
    return math.sqrt(
        sum((float(a_pose[index]) - float(b_pose[index])) ** 2 for index in range(3))
    )


def _next_available_run_dir(output_dir: Path) -> Path:
    output_dir = output_dir.resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise RuntimeError(f"output_dir_is_file:{output_dir}")
    if not output_dir.exists():
        return output_dir
    suffix = 1
    while True:
        candidate = output_dir.with_name(f"{output_dir.name}_{suffix}")
        if not candidate.exists():
            return candidate
        suffix += 1


def _detect_live_writer_processes() -> list[str]:
    try:
        probe = subprocess.run(
            ["pgrep", "-af", _PGREP_LIVE_WRITER_PATTERN],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"active_writer_probe_failed:{type(exc).__name__}") from exc
    if probe.returncode not in {0, 1}:
        raise RuntimeError("active_writer_probe_failed:pgrep_error")
    own_pid = os.getpid()
    matches: list[str] = []
    for line in probe.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split(maxsplit=1)
        if len(fields) < 2:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if pid == own_pid:
            continue
        command = fields[1].lower()
        if any(pattern in command for pattern in _LIVE_WRITER_IGNORED_PATTERNS):
            continue
        if any(pattern in command for pattern in _LIVE_WRITER_PATTERNS):
            matches.append(f"{pid}:{fields[1]}")
    matches.sort()
    return matches


def _enforce_no_live_writer_conflict() -> None:
    matches = _detect_live_writer_processes()
    if matches:
        raise RuntimeError("active_live_writer_detected:" + ",".join(matches))


def _rtde_format(type_name: str) -> str:
    formats = {
        "DOUBLE": "d",
        "VECTOR6D": "6d",
        "INT32": "i",
        "UINT32": "I",
        "UINT64": "Q",
        "BOOL": "?",
    }
    try:
        return formats[type_name]
    except KeyError as exc:
        raise RuntimeError(f"unsupported_rtde_type:{type_name}") from exc


class LiveRTDE(RTDEClient):
    def setup_inputs(self, fields: list[str]) -> tuple[int, list[str]]:
        self._send_packet("I", ",".join(fields).encode())
        packet_type, payload = self._recv_packet()
        if packet_type != ord("I"):
            raise RuntimeError(f"rtde_input_setup_response:{packet_type}")
        recipe = payload[0]
        types = payload[1:].decode("ascii", errors="replace").split(",")
        if recipe == 0 or any(value == "NOT_FOUND" for value in types):
            raise RuntimeError(f"rtde_input_recipe_invalid:{types}")
        return recipe, types

    def send_inputs(self, recipe: int, types: list[str], values: Sequence[Any]) -> None:
        if len(types) != len(values):
            raise RuntimeError("rtde_input_value_count_mismatch")
        payload = bytearray([recipe])
        for type_name, value in zip(types, values):
            payload.extend(struct.pack("!" + _rtde_format(type_name), value))
        self._send_packet("U", bytes(payload))

    def _decode_output_packet(
        self,
        recipe: int,
        types: list[str],
        fields: list[str],
        payload: bytes,
    ) -> dict[str, Any] | None:
        if not payload or payload[0] != recipe:
            return None
        cursor = 1
        values: list[Any] = []
        for type_name in types:
            fmt = _rtde_format(type_name)
            width = struct.calcsize("!" + fmt)
            if len(payload) < cursor + width:
                raise RuntimeError("rtde_output_payload_truncated")
            unpacked = struct.unpack("!" + fmt, payload[cursor : cursor + width])
            cursor += width
            values.append(unpacked[0] if len(unpacked) == 1 else tuple(unpacked))
        if cursor != len(payload):
            raise RuntimeError("rtde_output_payload_trailing_bytes")
        return dict(zip(fields, values))

    def receive_available(
        self,
        recipe: int,
        types: list[str],
        fields: list[str],
        timeout_s: float,
    ) -> list[dict[str, Any]]:
        assert self.sock is not None
        ready, _, _ = select.select([self.sock], [], [], timeout_s)
        if not ready:
            return []
        samples: list[dict[str, Any]] = []
        while True:
            packet_type, payload = self._recv_packet()
            if packet_type == ord("U"):
                sample = self._decode_output_packet(recipe, types, fields, payload)
                if sample is not None:
                    samples.append(sample)
            ready, _, _ = select.select([self.sock], [], [], 0.0)
            if not ready:
                return samples

    def receive_latest(
        self,
        recipe: int,
        types: list[str],
        fields: list[str],
        timeout_s: float,
    ) -> dict[str, Any] | None:
        samples = self.receive_available(recipe, types, fields, timeout_s)
        return samples[-1] if samples else None


def _receive_available(
    rtde: Any,
    recipe: int,
    types: list[str],
    fields: list[str],
    timeout_s: float,
) -> list[dict[str, Any]]:
    receiver = getattr(rtde, "receive_available", None)
    if callable(receiver):
        return list(receiver(recipe, types, fields, timeout_s))
    sample = rtde.receive_latest(recipe, types, fields, timeout_s)
    return [] if sample is None else [sample]


@dataclass(frozen=True)
class ReferenceTimeline:
    rows: tuple[Mapping[str, Any], ...]
    duration_s: float

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ReferenceTimeline":
        if payload.get("schema") != REFERENCE_SCHEMA:
            raise ValueError("reference schema mismatch")
        raw_rows = payload.get("references")
        trajectory = payload.get("trajectory")
        if not isinstance(raw_rows, list) or len(raw_rows) < 2:
            raise ValueError("reference rows are missing")
        if not isinstance(trajectory, Mapping):
            raise ValueError("reference trajectory is missing")
        duration = float(trajectory.get("capture_duration_s", 0.0))
        if not 0.0 < duration <= 2.0:
            raise ValueError("reference duration exceeds the no-contact canary")
        rows = tuple(row for row in raw_rows if isinstance(row, Mapping))
        if len(rows) != len(raw_rows):
            raise ValueError("reference row type mismatch")
        progress = [float(row["progress_s"]) for row in rows]
        if progress[0] != 0.0 or progress[-1] < duration:
            raise ValueError("reference timeline does not cover capture duration")
        if any(right <= left for left, right in zip(progress, progress[1:])):
            raise ValueError("reference progress must be strictly increasing")
        for row in rows:
            _finite6(row["desired_pose_base"], "desired_pose_base")
        return cls(rows=rows, duration_s=duration)

    def row_at(self, elapsed_s: float) -> Mapping[str, Any]:
        bounded = min(max(float(elapsed_s), 0.0), self.duration_s)
        low = 0
        high = len(self.rows)
        while low < high:
            middle = (low + high) // 2
            if float(self.rows[middle]["progress_s"]) <= bounded:
                low = middle + 1
            else:
                high = middle
        return self.rows[max(0, low - 1)]


@dataclass(frozen=True)
class CanaryTimeline:
    """Bounded stage trajectory, anchored to the fresh actual TCP pose."""

    stage: str
    start_pose: tuple[float, ...]
    duration_s: float
    bundle_timeline: ReferenceTimeline
    ramp_axis: tuple[float, ...]

    @classmethod
    def from_stage(
        cls,
        stage: str,
        *,
        actual_pose: Sequence[float],
        bundle: "ValidatedBundle",
    ) -> "CanaryTimeline":
        if stage not in CANARY_STAGE_ORDER:
            raise ValueError(f"unknown_canary_stage:{stage}")
        start_pose = _finite6(actual_pose, "canary_start_pose")
        duration_s = CANARY_STAGE_DURATIONS_S[stage]
        if stage == CANARY_STAGE_REFERENCE:
            duration_s = bundle.timeline.duration_s
        timeline = cls(
            stage=stage,
            start_pose=start_pose,
            duration_s=duration_s,
            bundle_timeline=bundle.timeline,
            ramp_axis=tuple(float(value) for value in bundle.tube.v_axis_base),
        )
        bundle.tube.assert_contains_pose(
            timeline.row_at(0.0)["desired_pose_base"], role="desired"
        )
        bundle.tube.assert_contains_pose(
            timeline.row_at(duration_s)["desired_pose_base"], role="desired"
        )
        if stage == CANARY_STAGE_REFERENCE:
            for row in bundle.timeline.rows:
                bundle.tube.assert_contains_pose(
                    timeline.row_at(float(row["progress_s"]))["desired_pose_base"],
                    role="desired",
                )
        return timeline

    @property
    def rows(self) -> tuple[Mapping[str, Any], ...]:
        return (self.row_at(0.0), self.row_at(self.duration_s))

    def row_at(self, elapsed_s: float) -> Mapping[str, Any]:
        bounded = min(max(float(elapsed_s), 0.0), self.duration_s)
        if self.stage == CANARY_STAGE_REFERENCE:
            reference_row = self.bundle_timeline.row_at(bounded)
            reference_origin = _finite6(
                self.bundle_timeline.rows[0]["desired_pose_base"],
                "reference_origin_pose",
            )
            reference_pose = _finite6(
                reference_row["desired_pose_base"],
                "reference_desired_pose",
            )
            pose = list(self.start_pose)
            for index in range(3):
                pose[index] += reference_pose[index] - reference_origin[index]
            return {
                "progress_s": float(reference_row["progress_s"]),
                "desired_pose_base": tuple(pose),
            }
        pose = list(self.start_pose)
        if self.stage == CANARY_STAGE_RAMP:
            phase = bounded / self.duration_s
            smooth = phase * phase * (3.0 - 2.0 * phase)
            for index in range(3):
                pose[index] += 0.0002 * smooth * self.ramp_axis[index]
        return {"progress_s": bounded, "desired_pose_base": tuple(pose)}


@dataclass
class AckPacedScheduler:
    """Issue exactly one new command only after the prior sequence is ACKed."""

    last_sent_sequence: int = 0

    def next_sequence(self, acknowledged_sequence: int) -> int | None:
        acknowledged = int(acknowledged_sequence)
        if acknowledged < 0 or acknowledged > self.last_sent_sequence:
            raise RuntimeError("controller_ack_out_of_range")
        if acknowledged != self.last_sent_sequence:
            return None
        self.last_sent_sequence += 1
        return self.last_sent_sequence


@dataclass(frozen=True)
class CommandLineage:
    """Immutable command/reference identity attached to one command sequence."""

    command_mode: int
    command_sequence: int
    progress_s: float
    desired_pose: tuple[float, ...]
    commanded_k: tuple[float, ...]
    commanded_raw_f_ff: tuple[float, ...]
    lease: int
    episode: int
    model_mode: int
    frame_token: int


@dataclass(frozen=True)
class CommandPacket:
    lineage: CommandLineage
    values: tuple[Any, ...]


def _command_packet(
    *,
    command: int,
    sequence: int,
    progress_s: float,
    pose: Sequence[float],
    lease_id: int,
    episode_identity: int,
) -> CommandPacket:
    desired_pose = _finite6(pose, "desired_pose")
    packet_values = tuple(
        command_values(
            command=command,
            sequence=sequence,
            pose=desired_pose,
            lease_id=lease_id,
            episode_identity=episode_identity,
        )
    )
    lineage = CommandLineage(
        command_mode=int(command),
        command_sequence=int(sequence),
        progress_s=float(progress_s),
        desired_pose=desired_pose,
        commanded_k=tuple(float(value) for value in packet_values[6:12]),
        commanded_raw_f_ff=tuple(float(value) for value in packet_values[18:24]),
        lease=int(packet_values[27]),
        episode=int(packet_values[35]),
        model_mode=int(packet_values[30]),
        frame_token=int(packet_values[31]),
    )
    return CommandPacket(lineage=lineage, values=packet_values)


def _register_command_lineage(
    lineages: dict[int, CommandLineage], packet: CommandPacket
) -> None:
    sequence = packet.lineage.command_sequence
    if sequence in lineages and lineages[sequence] != packet.lineage:
        raise RuntimeError(f"command_sequence_lineage_rebound:{sequence}")
    lineages[sequence] = packet.lineage


@dataclass(frozen=True)
class ValidatedBundle:
    source_path: Path
    manifest_path: Path
    reference_path: Path
    source_sha256: str
    manifest_sha256: str
    reference_sha256: str
    source: str
    reference: Mapping[str, Any]
    timeline: ReferenceTimeline
    tube: LiveTubeContract


def validate_bundle(manifest_path: Path) -> ValidatedBundle:
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, Mapping) or manifest.get("schema") != BUNDLE_SCHEMA:
        raise ValueError("bundle manifest schema mismatch")
    source_path = manifest_path.parent / str(manifest["receiver_source"])
    reference_path = Path(str(manifest["reference_artifact"])).resolve()
    source_sha = _sha256(source_path)
    reference_sha = _sha256(reference_path)
    if source_sha != manifest.get("receiver_source_sha256"):
        raise ValueError("receiver source SHA-256 mismatch")
    if reference_sha != manifest.get("reference_artifact_sha256"):
        raise ValueError("reference artifact SHA-256 mismatch")
    source = source_path.read_text(encoding="utf-8")
    contract = parse_live_receiver_source(source)
    if contract.protocol_token != LIVE_PROTOCOL_TOKEN:
        raise ValueError("receiver protocol token mismatch")
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    if not isinstance(reference, Mapping):
        raise ValueError("reference artifact type mismatch")
    timeline = ReferenceTimeline.from_payload(reference)
    tube = LiveTubeContract.from_reference_artifact(reference_path)
    for row in timeline.rows:
        tube.assert_contains_pose(row["desired_pose_base"], role="desired")
    return ValidatedBundle(
        source_path=source_path,
        manifest_path=manifest_path,
        reference_path=reference_path,
        source_sha256=source_sha,
        manifest_sha256=_sha256(manifest_path),
        reference_sha256=reference_sha,
        source=source,
        reference=reference,
        timeline=timeline,
        tube=tube,
    )


def validate_authorization(
    path: Path,
    bundle: ValidatedBundle,
    *,
    robot_host: str,
    canary_stage: str = CANARY_STAGE_REFERENCE,
    now: datetime | None = None,
) -> Mapping[str, Any]:
    if canary_stage not in CANARY_STAGE_ORDER:
        raise ValueError(f"unknown_canary_stage:{canary_stage}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema") != AUTHORIZATION_SCHEMA:
        raise ValueError("authorization schema mismatch")
    checks = {
        "robot_host": robot_host,
        "receiver_source_sha256": bundle.source_sha256,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "reference_artifact_sha256": bundle.reference_sha256,
        "canary_stage": canary_stage,
        "allow_urscript_send": True,
        "allow_rtde_input_write": True,
        "allow_direct_torque": True,
        "allow_motion": True,
        "allow_contact": False,
        "allow_kunwei_stream": False,
    }
    failures = [
        key for key, expected in checks.items() if payload.get(key) != expected
    ]
    if int(payload.get("episode_identity", 0)) <= 0:
        failures.append("episode_identity")
    required_duration_s = (
        bundle.timeline.duration_s
        if canary_stage == CANARY_STAGE_REFERENCE
        else CANARY_STAGE_DURATIONS_S[canary_stage]
    )
    if float(payload.get("max_duration_s", 0.0)) < required_duration_s:
        failures.append("max_duration_s")
    if float(payload.get("normal_half_width_m", math.inf)) != 0.002:
        failures.append("normal_half_width_m")
    current = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    if not (_parse_time(payload.get("authorized_at"), "authorized_at") <= current):
        failures.append("authorized_at")
    if not (current < _parse_time(payload.get("expires_at"), "expires_at")):
        failures.append("expires_at")
    if failures:
        raise RuntimeError("authorization_mismatch:" + ",".join(sorted(set(failures))))
    return payload


def validate_compile_probe_evidence(
    path: Path,
    *,
    robot_host: str,
) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_source_sha = hashlib.sha256(
        build_compile_probe_source().encode("utf-8")
    ).hexdigest()
    checks = {
        "schema": "ur10e_tacdiffusion_compile_probe_evidence/v1",
        "claim_class": "live_controller_compile_probe_no_motion",
        "ok": True,
        "robot_host": robot_host,
        "compile_probe_source_sha256": expected_source_sha,
        "motion_performed": False,
        "direct_torque_called": False,
        "rtde_inputs_written": False,
        "kunwei_stream_started": False,
    }
    failures = [
        key for key, expected in checks.items() if payload.get(key) != expected
    ]
    strict = payload.get("strict_success_gate")
    if not isinstance(strict, Mapping) or strict.get("ok") is not True:
        failures.append("strict_success_gate")
    if failures:
        raise RuntimeError(
            "compile_probe_evidence_mismatch:" + ",".join(sorted(set(failures)))
        )
    return payload


def validate_prior_stage_evidence(
    path: Path | None,
    *,
    required_stage: str | None,
    bundle: ValidatedBundle,
    robot_host: str,
) -> Mapping[str, Any] | None:
    if required_stage is None:
        if path is not None:
            raise RuntimeError("prior_stage_evidence_not_allowed_for_first_stage")
        return None
    if path is None:
        raise RuntimeError(f"prior_stage_evidence_required:{required_stage}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    checks = {
        "schema": "ur10e_tacdiffusion_direct_torque_canary_evidence/v1",
        "ok": True,
        "robot_host": robot_host,
        "receiver_source_sha256": bundle.source_sha256,
        "bundle_manifest_sha256": bundle.manifest_sha256,
        "reference_artifact_sha256": bundle.reference_sha256,
        "canary_stage": required_stage,
    }
    failures = [
        key for key, expected in checks.items() if payload.get(key) != expected
    ]
    strict = payload.get("strict_success_gate")
    if not isinstance(strict, Mapping) or strict.get("ok") is not True:
        failures.append("strict_success_gate")
    if failures:
        raise RuntimeError(
            "prior_stage_evidence_mismatch:" + ",".join(sorted(set(failures)))
        )
    return payload


def validate_compile_probe_authorization(
    path: Path,
    source_sha256: str,
    *,
    robot_host: str,
    now: datetime | None = None,
) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != COMPILE_PROBE_AUTHORIZATION_SCHEMA
    ):
        raise ValueError("compile probe authorization schema mismatch")
    checks = {
        "robot_host": robot_host,
        "compile_probe_source_sha256": source_sha256,
        "allow_urscript_send": True,
        "allow_rtde_output_read": True,
        "allow_rtde_input_write": False,
        "allow_direct_torque": False,
        "allow_motion": False,
        "allow_contact": False,
        "allow_kunwei_stream": False,
    }
    failures = [
        key for key, expected in checks.items() if payload.get(key) != expected
    ]
    current = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    if not (_parse_time(payload.get("authorized_at"), "authorized_at") <= current):
        failures.append("authorized_at")
    if not (current < _parse_time(payload.get("expires_at"), "expires_at")):
        failures.append("expires_at")
    if failures:
        raise RuntimeError(
            "compile_probe_authorization_mismatch:"
            + ",".join(sorted(set(failures)))
        )
    return payload


def command_values(
    *,
    command: int,
    sequence: int,
    pose: Sequence[float],
    lease_id: int,
    episode_identity: int,
) -> list[Any]:
    desired_pose = _finite6(pose, "desired_pose")
    doubles = [
        *desired_pose,
        *FIXED_STIFFNESS,
        *ZERO6,
        *ZERO6,
    ]
    integers = [
        int(command),
        int(sequence),
        int(sequence),
        int(lease_id),
        0,
        0,
        0,
        WRENCH_FRAME_TOKEN,
        0,
        0,
        0,
        int(episode_identity),
    ]
    return doubles + integers


def readonly_status(robot_host: str) -> dict[str, Any]:
    dashboard = dashboard_exchange(
        robot_host,
        [
            "PolyscopeVersion",
            "get loaded program",
            "programState",
            "robotmode",
            "safetystatus",
            "is in remote control",
        ],
    )
    rtde = read_rtde_once(
        robot_host,
        [
            "actual_TCP_pose",
            "actual_TCP_speed",
            "actual_TCP_force",
            "actual_qd",
            "runtime_state",
            "robot_mode",
            "safety_mode",
        ],
        frequency_hz=10.0,
    )
    stationary = max(
        abs(float(value)) for value in (*rtde["actual_TCP_speed"], *rtde["actual_qd"])
    ) <= 1.0e-6
    return {
        "dashboard": dashboard,
        "rtde": rtde,
        "remote_control": dashboard.get("is in remote control", "").lower() == "true",
        "stopped": int(rtde["runtime_state"]) == RUNTIME_STOPPED,
        "stationary": stationary,
        "motion_performed": False,
        "urscript_sent": False,
        "rtde_inputs_written": False,
        "kunwei_stream_started": False,
    }


def validate_compile_probe_preflight(status: Mapping[str, Any]) -> None:
    dashboard = status["dashboard"]
    rtde = status["rtde"]
    failures = []
    if not status["remote_control"]:
        failures.append("controller_not_remote_control")
    if not status["stopped"]:
        failures.append("controller_program_not_stopped")
    if not status["stationary"]:
        failures.append("robot_not_stationary")
    if "URSoftware 5.26." not in dashboard.get("PolyscopeVersion", ""):
        failures.append("controller_not_5_26")
    if dashboard.get("safetystatus") != "Safetystatus: NORMAL":
        failures.append("dashboard_safety_not_normal")
    if dashboard.get("robotmode") != "Robotmode: RUNNING":
        failures.append("dashboard_robotmode_not_running")
    if int(rtde["safety_mode"]) != SAFETY_MODE_NORMAL:
        failures.append("rtde_safety_not_normal")
    if int(rtde["robot_mode"]) != ROBOT_MODE_RUNNING:
        failures.append("rtde_robotmode_not_running")
    if failures:
        raise RuntimeError(
            "compile_probe_preflight_failed:" + ",".join(failures)
        )


def validate_live_preflight(
    status: Mapping[str, Any],
    bundle: ValidatedBundle,
    *,
    timeline: ReferenceTimeline | CanaryTimeline,
) -> None:
    dashboard = status["dashboard"]
    rtde = status["rtde"]
    first_desired_pose = timeline.rows[0]["desired_pose_base"]
    failures = []
    if not status["remote_control"]:
        failures.append("controller_not_remote_control")
    if not status["stopped"]:
        failures.append("controller_program_not_stopped")
    if not status["stationary"]:
        failures.append("robot_not_stationary")
    if "URSoftware 5.26." not in dashboard.get("PolyscopeVersion", ""):
        failures.append("controller_not_5_26")
    if dashboard.get("safetystatus") != "Safetystatus: NORMAL":
        failures.append("dashboard_safety_not_normal")
    if dashboard.get("robotmode") != "Robotmode: RUNNING":
        failures.append("dashboard_robotmode_not_running")
    if int(rtde["safety_mode"]) != SAFETY_MODE_NORMAL:
        failures.append("rtde_safety_not_normal")
    if int(rtde["robot_mode"]) != ROBOT_MODE_RUNNING:
        failures.append("rtde_robotmode_not_running")
    force = _finite6(rtde["actual_TCP_force"], "actual_TCP_force")
    if math.sqrt(sum(value * value for value in force[:3])) > 10.0:
        failures.append("release_force_over_10n")
    if math.sqrt(sum(value * value for value in force[3:])) > 1.0:
        failures.append("release_torque_over_1nm")
    if (
        _sample_translation_error_sqm3(rtde["actual_TCP_pose"], first_desired_pose)
        > NO_CONTACT_RELEASE_TOLERANCE_M
    ):
        failures.append("release_translation_error_exceeds_1mm")
    try:
        bundle.tube.assert_contains_pose(rtde["actual_TCP_pose"], role="actual")
    except RuntimeError as exc:
        failures.append(str(exc))
    if failures:
        raise RuntimeError("live_preflight_failed:" + ",".join(failures))


def _wait_for_fresh_receiver_waiting(
    rtde: LiveRTDE,
    output_recipe: int,
    output_types: list[str],
    output_fields: list[str],
    *,
    receiver_wait_s: float,
    samples_out: list[dict[str, Any]] | None = None,
) -> tuple[float, Mapping[str, Any]]:
    deadline = time.monotonic() + receiver_wait_s
    waiting_stale = False
    while time.monotonic() < deadline:
        batch = _receive_available(
            rtde, output_recipe, output_types, output_fields, 0.01
        )
        if not batch:
            continue
        if samples_out is not None:
            samples_out.extend(batch)
        for sample in batch:
            state = int(sample["output_int_register_24"])
            runtime_state = int(sample["runtime_state"])
            protocol = int(sample["output_int_register_32"])
            if protocol != LIVE_PROTOCOL_TOKEN:
                continue
            if state != STATE_WAITING:
                continue
            if runtime_state != RUNTIME_PLAYING:
                waiting_stale = True
                continue
            return time.monotonic(), sample
    if waiting_stale:
        raise RuntimeError("receiver_waiting_stale_after_send")
    raise RuntimeError("receiver_protocol_wait_timeout")


def _prime_idle_inputs(
    rtde: LiveRTDE,
    input_recipe: int,
    input_types: list[str],
    idle_values: Sequence[Any],
    output_recipe: int,
    output_types: list[str],
    output_fields: list[str],
    *,
    timeout_s: float = 0.25,
    minimum_fresh_ticks: int = 5,
) -> None:
    """Overwrite stale terminal commands before starting the URScript receiver."""
    deadline = time.monotonic() + timeout_s
    baseline_timestamp: float | None = None
    while time.monotonic() < deadline and baseline_timestamp is None:
        baseline_batch = _receive_available(
            rtde, output_recipe, output_types, output_fields, 0.01
        )
        for sample in baseline_batch:
            if int(sample["robot_mode"]) != ROBOT_MODE_RUNNING:
                raise RuntimeError("idle_prime_robotmode_changed")
            if int(sample["safety_mode"]) != SAFETY_MODE_NORMAL:
                raise RuntimeError("idle_prime_safety_changed")
            if int(sample["runtime_state"]) != RUNTIME_STOPPED:
                raise RuntimeError("idle_prime_runtime_not_stopped")
            timestamp = float(sample["timestamp"])
            if baseline_timestamp is None or timestamp > baseline_timestamp:
                baseline_timestamp = timestamp
    if baseline_timestamp is None:
        raise RuntimeError("idle_prime_baseline_timeout")

    timestamps: list[float] = []
    while time.monotonic() < deadline:
        rtde.send_inputs(input_recipe, input_types, idle_values)
        batch = _receive_available(
            rtde, output_recipe, output_types, output_fields, 0.01
        )
        for sample in batch:
            if int(sample["robot_mode"]) != ROBOT_MODE_RUNNING:
                raise RuntimeError("idle_prime_robotmode_changed")
            if int(sample["safety_mode"]) != SAFETY_MODE_NORMAL:
                raise RuntimeError("idle_prime_safety_changed")
            if int(sample["runtime_state"]) != RUNTIME_STOPPED:
                raise RuntimeError("idle_prime_runtime_not_stopped")
            timestamp = float(sample["timestamp"])
            if timestamp <= baseline_timestamp:
                continue
            if not timestamps or timestamp > timestamps[-1]:
                timestamps.append(timestamp)
            if (
                len(timestamps) >= minimum_fresh_ticks
                and timestamps[-1] - timestamps[0]
                >= (minimum_fresh_ticks - 1) / 500.0
            ):
                return
    raise RuntimeError("idle_prime_timeout")


def _send_urscript(host: str, source: str, timeout_s: float) -> None:
    payload = source if source.endswith("\n") else source + "\n"
    with socket.create_connection((host, 30002), timeout=timeout_s) as connection:
        connection.sendall(payload.encode("utf-8"))


def _output_row(
    sample: Mapping[str, Any],
    host_elapsed_s: float,
    *,
    outgoing: CommandLineage,
    acked: CommandLineage | None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "host_elapsed_s": host_elapsed_s,
        "controller_timestamp_s": float(sample["timestamp"]),
        "runtime_state": int(sample["runtime_state"]),
        "robot_mode": int(sample["robot_mode"]),
        "safety_mode": int(sample["safety_mode"]),
        "receiver_state": int(sample["output_int_register_24"]),
        "ack_sequence": int(sample["output_int_register_25"]),
        "fault": int(sample["output_int_register_26"]),
        "lease_echo": int(sample["output_int_register_27"]),
        "model_sequence_echo": int(sample["output_int_register_28"]),
        "frame_echo": int(sample["output_int_register_30"]),
        "episode_echo": int(sample["output_int_register_31"]),
        "protocol_echo": int(sample["output_int_register_32"]),
        "exit_reason": int(sample["output_int_register_33"]),
        "max_abs_tau_nm": float(sample["output_double_register_24"]),
        "steptime_s": float(sample["output_double_register_25"]),
        "outgoing_command_mode": outgoing.command_mode,
        "outgoing_command_sequence": outgoing.command_sequence,
        "acked_command_mode": "" if acked is None else acked.command_mode,
        "acked_command_sequence": "" if acked is None else acked.command_sequence,
        "ack_command_lineage_missing": int(acked is None),
    }
    associated = acked
    row["command_progress_s"] = "" if associated is None else associated.progress_s
    for index in range(6):
        row[f"command_desired_pose_{index}"] = (
            "" if associated is None else associated.desired_pose[index]
        )
        row[f"commanded_k_{index}"] = (
            "" if associated is None else associated.commanded_k[index]
        )
        row[f"commanded_raw_f_ff_{index}"] = (
            "" if associated is None else associated.commanded_raw_f_ff[index]
        )
    row["command_lease"] = "" if associated is None else associated.lease
    row["command_episode"] = "" if associated is None else associated.episode
    row["command_model_mode"] = "" if associated is None else associated.model_mode
    row["command_frame_token"] = "" if associated is None else associated.frame_token
    for prefix, lineage in (
        ("outgoing_command", outgoing),
        ("acked_command", acked),
    ):
        row[f"{prefix}_progress_s"] = (
            "" if lineage is None else lineage.progress_s
        )
        for index in range(6):
            row[f"{prefix}_desired_pose_{index}"] = (
                "" if lineage is None else lineage.desired_pose[index]
            )
            row[f"{prefix}ed_k_{index}"] = (
                "" if lineage is None else lineage.commanded_k[index]
            )
            row[f"{prefix}ed_raw_f_ff_{index}"] = (
                "" if lineage is None else lineage.commanded_raw_f_ff[index]
            )
        row[f"{prefix}_lease"] = "" if lineage is None else lineage.lease
        row[f"{prefix}_episode"] = "" if lineage is None else lineage.episode
        row[f"{prefix}_model_mode"] = "" if lineage is None else lineage.model_mode
        row[f"{prefix}_frame_token"] = "" if lineage is None else lineage.frame_token
    for name in ("actual_TCP_pose", "actual_TCP_speed", "actual_TCP_force", "actual_q", "actual_qd", "target_moment"):
        for index, value in enumerate(sample[name]):
            row[f"{name}_{index}"] = float(value)
    for index in range(6):
        row[f"applied_f_ff_{index}"] = float(
            sample[f"output_double_register_{26 + index}"]
        )
        row[f"applied_k_{index}"] = float(
            sample[f"output_double_register_{32 + index}"]
        )
        row[f"commanded_joint_torque_nm_{index}"] = float(
            sample[f"output_double_register_{38 + index}"]
        )
    return row


def _sample_safety_errors(
    sample: Mapping[str, Any],
    *,
    lease_id: int,
    episode_identity: int,
    bundle: ValidatedBundle,
    desired_pose: Sequence[float],
) -> list[str]:
    errors: list[str] = []
    if int(sample["robot_mode"]) != ROBOT_MODE_RUNNING or int(
        sample["safety_mode"]
    ) != SAFETY_MODE_NORMAL:
        errors.append("runtime_safety_changed")
    if int(sample["output_int_register_32"]) != LIVE_PROTOCOL_TOKEN:
        errors.append("receiver_protocol_identity_changed")
    state = int(sample["output_int_register_24"])
    fault = int(sample["output_int_register_26"])
    if fault != 0 or state == STATE_FAULT:
        errors.append(f"receiver_fault:{fault}")
    if state in {STATE_STARTUP, STATE_TORQUE}:
        if int(sample["output_int_register_27"]) != lease_id:
            errors.append("lease_echo_mismatch")
        if int(sample["output_int_register_31"]) != episode_identity:
            errors.append("episode_echo_mismatch")
        if int(sample["output_int_register_30"]) != WRENCH_FRAME_TOKEN:
            errors.append("frame_echo_mismatch")
    try:
        bundle.tube.assert_contains_pose(sample["actual_TCP_pose"], role="actual")
    except RuntimeError as exc:
        errors.append(str(exc))
    try:
        bundle.tube.assert_contains_pose(desired_pose, role="desired")
    except RuntimeError as exc:
        errors.append(str(exc))
    return errors


def _append_output_batch(
    batch: Sequence[Mapping[str, Any]],
    *,
    start: float,
    outgoing: CommandPacket,
    lineages: Mapping[int, CommandLineage],
    lease_id: int,
    episode_identity: int,
    bundle: ValidatedBundle,
    samples: list[dict[str, Any]],
    timeline: ReferenceTimeline | CanaryTimeline | None = None,
) -> tuple[Mapping[str, Any], bool, list[str]]:
    if not batch:
        raise RuntimeError("empty_output_batch")
    errors: list[str] = []
    observed_torque = False
    for sample in batch:
        elapsed = time.monotonic() - start
        ack_sequence = int(sample["output_int_register_25"])
        acked = lineages.get(ack_sequence)
        active_timeline = bundle.timeline if timeline is None else timeline
        desired_row = active_timeline.row_at(elapsed)
        errors.extend(
            _sample_safety_errors(
                sample,
                lease_id=lease_id,
                episode_identity=episode_identity,
                bundle=bundle,
                desired_pose=desired_row["desired_pose_base"],
            )
        )
        samples.append(
            _output_row(
                sample,
                elapsed,
                outgoing=outgoing.lineage,
                acked=acked,
            )
        )
        if acked is None:
            errors.append(f"ack_command_lineage_missing:{ack_sequence}")
        observed_torque = observed_torque or int(
            sample["output_int_register_24"]
        ) == STATE_TORQUE
    return batch[-1], observed_torque, errors


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        raise RuntimeError("no_live_samples_collected")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_json_new(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def _compile_probe_row(
    sample: Mapping[str, Any],
    *,
    host_elapsed_s: float,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "host_elapsed_s": host_elapsed_s,
        "controller_timestamp_s": float(sample["timestamp"]),
        "runtime_state": int(sample["runtime_state"]),
        "robot_mode": int(sample["robot_mode"]),
        "safety_mode": int(sample["safety_mode"]),
        "probe_state": int(sample["output_int_register_24"]),
        "probe_tick": int(sample["output_int_register_25"]),
        "probe_fault": int(sample["output_int_register_26"]),
        "probe_protocol": int(sample["output_int_register_32"]),
    }
    for name in (
        "actual_TCP_pose",
        "actual_TCP_speed",
        "actual_TCP_force",
        "actual_q",
        "actual_qd",
        "target_moment",
    ):
        for index, value in enumerate(sample[name]):
            row[f"{name}_{index}"] = float(value)
    return row


def run_compile_probe(args: argparse.Namespace) -> dict[str, Any]:
    if not (args.live and args.send_urscript and args.no_motion):
        raise RuntimeError("compile_probe_live_send_and_no_motion_gates_required")
    source = build_compile_probe_source()
    parse_compile_probe_source(source)
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    validate_compile_probe_authorization(
        args.authorization.resolve(),
        source_sha256,
        robot_host=args.robot_host,
    )
    status = readonly_status(args.robot_host)
    validate_compile_probe_preflight(status)
    output_dir = _next_available_run_dir(args.output_dir.resolve())
    rows: list[dict[str, Any]] = []
    failure: str | None = None
    observed_active = False
    observed_complete = False
    with _live_writer_lease():
        _enforce_no_live_writer_conflict()
        start = time.monotonic()
        with LiveRTDE(args.robot_host, timeout=args.connect_timeout_s) as rtde:
            rtde.negotiate()
            output_recipe, output_types = rtde.setup_outputs(500.0, OUTPUT_FIELDS)
            rtde.start()
            try:
                _send_urscript(args.robot_host, source, args.connect_timeout_s)
                deadline = time.monotonic() + args.probe_timeout_s
                while time.monotonic() < deadline:
                    batch = _receive_available(
                        rtde,
                        output_recipe,
                        output_types,
                        OUTPUT_FIELDS,
                        0.01,
                    )
                    if not batch:
                        continue
                    for sample in batch:
                        rows.append(
                            _compile_probe_row(
                                sample,
                                host_elapsed_s=time.monotonic() - start,
                            )
                        )
                        token = int(sample["output_int_register_32"])
                        state = int(sample["output_int_register_24"])
                        if token == COMPILE_PROBE_PROTOCOL_TOKEN:
                            observed_active = observed_active or (
                                state == COMPILE_PROBE_STATE_ACTIVE
                                and int(sample["runtime_state"]) == RUNTIME_PLAYING
                            )
                            observed_complete = observed_complete or (
                                state == COMPILE_PROBE_STATE_COMPLETE
                            )
                    if observed_active and observed_complete:
                        break
                if not observed_active:
                    raise RuntimeError("compile_probe_active_marker_missing")
                if not observed_complete:
                    raise RuntimeError("compile_probe_complete_marker_missing")
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                csv_path = output_dir / "compile_probe_rtde.csv"
                evidence_path = output_dir / "evidence.json"
                if rows:
                    _write_csv(csv_path, rows)
                baseline_pose = tuple(
                    float(value) for value in status["rtde"]["actual_TCP_pose"]
                )
                maximum_translation_m = max(
                    (
                        _sample_translation_error_sqm3(
                            [row[f"actual_TCP_pose_{axis}"] for axis in range(6)],
                            baseline_pose,
                        )
                        for row in rows
                    ),
                    default=0.0,
                )
                maximum_speed_m_s = max(
                    (
                        math.sqrt(
                            sum(
                                float(row[f"actual_TCP_speed_{axis}"]) ** 2
                                for axis in range(3)
                            )
                        )
                        for row in rows
                    ),
                    default=0.0,
                )
                active_rows = sum(
                    row["probe_protocol"] == COMPILE_PROBE_PROTOCOL_TOKEN
                    and row["probe_state"] == COMPILE_PROBE_STATE_ACTIVE
                    for row in rows
                )
                strict_gate = {
                    "source_contains_no_torque_or_motion_api": True,
                    "observed_active_runtime_marker": observed_active,
                    "observed_complete_marker": observed_complete,
                    "at_least_45_active_500hz_rows": active_rows >= 45,
                    "maximum_tcp_translation_le_0_1mm": maximum_translation_m
                    <= 0.0001,
                    "maximum_tcp_speed_le_1mm_s": maximum_speed_m_s <= 0.001,
                }
                strict_gate["ok"] = all(strict_gate.values()) and failure is None
                evidence = {
                    "schema": "ur10e_tacdiffusion_compile_probe_evidence/v1",
                    "claim_class": "live_controller_compile_probe_no_motion",
                    "ok": strict_gate["ok"],
                    "failure": failure,
                    "robot_host": args.robot_host,
                    "compile_probe_source_sha256": source_sha256,
                    "sample_count": len(rows),
                    "active_500hz_rows": active_rows,
                    "maximum_tcp_translation_m": maximum_translation_m,
                    "maximum_tcp_speed_m_s": maximum_speed_m_s,
                    "observed_active_marker": observed_active,
                    "observed_complete_marker": observed_complete,
                    "strict_success_gate": strict_gate,
                    "motion_performed": False,
                    "direct_torque_called": False,
                    "rtde_inputs_written": False,
                    "kunwei_stream_started": False,
                    "training_dataset": False,
                    "data_csv": str(csv_path) if rows else None,
                }
                _write_json_new(evidence_path, evidence)
    return json.loads((output_dir / "evidence.json").read_text())


@contextmanager
def _live_writer_lease():
    profile = ResourceProfile.from_env()
    try:
        lease_context = writer_lease(profile, LIVE_WRITER_TASK, blocking=False)
        lease_context.__enter__()
    except BlockingIOError as exc:
        raise RuntimeError("live_writer_lock_unavailable") from exc
    try:
        yield
    except BaseException:
        lease_context.__exit__(*sys.exc_info())
        raise
    else:
        lease_context.__exit__(None, None, None)


def run_live(args: argparse.Namespace, bundle: ValidatedBundle) -> dict[str, Any]:
    if not (
        args.live
        and args.send_urscript
        and args.write_rtde_inputs
        and args.allow_direct_torque
        and args.allow_motion
        and args.no_contact
    ):
        raise RuntimeError("all_independent_live_cli_gates_are_required")
    with _live_writer_lease():
        return _run_live_locked(args, bundle)


def _run_live_locked(args: argparse.Namespace, bundle: ValidatedBundle) -> dict[str, Any]:
    canary_stage = str(args.canary_stage)
    if canary_stage not in CANARY_STAGE_ORDER:
        raise ValueError(f"unknown_canary_stage:{canary_stage}")
    compile_probe_evidence = validate_compile_probe_evidence(
        args.compile_probe_evidence.resolve(),
        robot_host=args.robot_host,
    )
    stage_index = CANARY_STAGE_ORDER.index(canary_stage)
    required_prior_stage = (
        None if stage_index == 0 else CANARY_STAGE_ORDER[stage_index - 1]
    )
    prior_stage_evidence = validate_prior_stage_evidence(
        (
            None
            if args.prior_stage_evidence is None
            else args.prior_stage_evidence.resolve()
        ),
        required_stage=required_prior_stage,
        bundle=bundle,
        robot_host=args.robot_host,
    )
    authorization = validate_authorization(
        args.authorization.resolve(),
        bundle,
        robot_host=args.robot_host,
        canary_stage=canary_stage,
    )
    status = readonly_status(args.robot_host)
    timeline = CanaryTimeline.from_stage(
        canary_stage,
        actual_pose=status["rtde"]["actual_TCP_pose"],
        bundle=bundle,
    )
    validate_live_preflight(status, bundle, timeline=timeline)
    _enforce_no_live_writer_conflict()
    lease_id = int(authorization["lease_id"])
    episode_identity = int(authorization["episode_identity"])
    if lease_id <= 0:
        raise RuntimeError("authorization_lease_id_invalid")
    initial_pose = timeline.rows[0]["desired_pose_base"]
    scheduler = AckPacedScheduler()
    samples: list[dict[str, Any]] = []
    sent_sequences = 0
    observed_torque = False
    complete = False
    start: float | None = None
    initial_command = _command_packet(
        command=MODE_IDLE,
        sequence=0,
        progress_s=0.0,
        pose=initial_pose,
        lease_id=lease_id,
        episode_identity=episode_identity,
    )
    lineages: dict[int, CommandLineage] = {0: initial_command.lineage}
    outgoing = initial_command
    failure: str | None = None
    output_dir = _next_available_run_dir(args.output_dir.resolve())
    with LiveRTDE(args.robot_host, timeout=args.connect_timeout_s) as rtde:
        rtde.negotiate()
        output_recipe, output_types = rtde.setup_outputs(500.0, OUTPUT_FIELDS)
        input_recipe, input_types = rtde.setup_inputs(INPUT_FIELDS)
        rtde.start()
        _prime_idle_inputs(
            rtde,
            input_recipe,
            input_types,
            outgoing.values,
            output_recipe,
            output_types,
            OUTPUT_FIELDS,
        )
        _send_urscript(args.robot_host, bundle.source, args.connect_timeout_s)
        try:
            handshake_samples: list[dict[str, Any]] = []
            start, sample = _wait_for_fresh_receiver_waiting(
                rtde,
                output_recipe,
                output_types,
                OUTPUT_FIELDS,
                receiver_wait_s=args.receiver_wait_s,
                samples_out=handshake_samples,
            )
            pending = handshake_samples or [dict(sample)]
            while True:
                if not pending:
                    pending = _receive_available(
                        rtde,
                        output_recipe,
                        output_types,
                        OUTPUT_FIELDS,
                        0.01,
                    )
                    if not pending:
                        raise RuntimeError("rtde_output_stale")

                last_sample, batch_torque, errors = _append_output_batch(
                    pending,
                    start=start,
                    outgoing=outgoing,
                    lineages=lineages,
                    lease_id=lease_id,
                    episode_identity=episode_identity,
                    bundle=bundle,
                    samples=samples,
                    timeline=timeline,
                )
                observed_torque = observed_torque or batch_torque
                if errors:
                    raise RuntimeError("output_batch_safety_failed:" + ";".join(errors))

                elapsed = time.monotonic() - start
                if elapsed >= timeline.duration_s and observed_torque:
                    end_row = timeline.row_at(elapsed)
                    end_command = _command_packet(
                        command=MODE_END,
                        sequence=outgoing.lineage.command_sequence,
                        progress_s=float(end_row["progress_s"]),
                        pose=end_row["desired_pose_base"],
                        lease_id=lease_id,
                        episode_identity=episode_identity,
                    )
                    rtde.send_inputs(input_recipe, input_types, end_command.values)
                    outgoing = end_command
                    end_deadline = time.monotonic() + 1.0
                    while time.monotonic() < end_deadline:
                        final_batch = _receive_available(
                            rtde,
                            output_recipe,
                            output_types,
                            OUTPUT_FIELDS,
                            0.01,
                        )
                        if not final_batch:
                            continue
                        _, batch_torque, errors = _append_output_batch(
                            final_batch,
                            start=start,
                            outgoing=outgoing,
                            lineages=lineages,
                            lease_id=lease_id,
                            episode_identity=episode_identity,
                            bundle=bundle,
                            samples=samples,
                            timeline=timeline,
                        )
                        observed_torque = observed_torque or batch_torque
                        if errors:
                            raise RuntimeError(
                                "completion_batch_safety_failed:" + ";".join(errors)
                            )
                        if any(
                            int(final["output_int_register_24"]) == STATE_COMPLETE
                            for final in final_batch
                        ):
                            complete = True
                            break
                    if not complete:
                        raise RuntimeError("receiver_completion_timeout")
                    break
                next_sequence = scheduler.next_sequence(
                    int(last_sample["output_int_register_25"])
                )
                if next_sequence is not None:
                    command_row = timeline.row_at(elapsed)
                    next_command = _command_packet(
                        command=MODE_RUN,
                        sequence=next_sequence,
                        progress_s=float(command_row["progress_s"]),
                        pose=command_row["desired_pose_base"],
                        lease_id=lease_id,
                        episode_identity=episode_identity,
                    )
                    rtde.send_inputs(input_recipe, input_types, next_command.values)
                    _register_command_lineage(lineages, next_command)
                    outgoing = next_command
                    sent_sequences += 1
                pending = []
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            try:
                abort_command = _command_packet(
                    command=MODE_ABORT,
                    sequence=outgoing.lineage.command_sequence,
                    progress_s=outgoing.lineage.progress_s,
                    pose=outgoing.lineage.desired_pose,
                    lease_id=lease_id,
                    episode_identity=episode_identity,
                )
                rtde.send_inputs(input_recipe, input_types, abort_command.values)
                outgoing = abort_command
                abort_batch = _receive_available(
                    rtde, output_recipe, output_types, OUTPUT_FIELDS, 0.05
                )
                if abort_batch and start is not None:
                    _append_output_batch(
                        abort_batch,
                        start=start,
                        outgoing=outgoing,
                        lineages=lineages,
                        lease_id=lease_id,
                        episode_identity=episode_identity,
                        bundle=bundle,
                        samples=samples,
                        timeline=timeline,
                    )
            except Exception:
                pass
            raise
        finally:
            csv_path = output_dir / "direct_torque_rtde.csv"
            evidence_path = output_dir / "evidence.json"
            if samples:
                _write_csv(csv_path, samples)
            evidence = {
                "schema": "ur10e_tacdiffusion_direct_torque_canary_evidence/v1",
                "claim_class": "live_no_contact_direct_torque_canary",
                "ok": False,
                "failure": failure,
                "robot_host": args.robot_host,
                "receiver_source_sha256": bundle.source_sha256,
                "bundle_manifest_sha256": bundle.manifest_sha256,
                "reference_artifact_sha256": bundle.reference_sha256,
                "canary_stage": canary_stage,
                "required_prior_stage": required_prior_stage,
                "compile_probe_evidence_sha256": _sha256(
                    args.compile_probe_evidence.resolve()
                ),
                "prior_stage_evidence_sha256": (
                    None
                    if args.prior_stage_evidence is None
                    else _sha256(args.prior_stage_evidence.resolve())
                ),
                "compile_probe_evidence_ok": bool(compile_probe_evidence["ok"]),
                "prior_stage_evidence_ok": (
                    None
                    if prior_stage_evidence is None
                    else bool(prior_stage_evidence["ok"])
                ),
                "lease_id": lease_id,
                "episode_identity": episode_identity,
                "duration_s": time.monotonic() - start if start is not None else 0.0,
                "sample_count": len(samples),
                "total_rows": len(samples),
                "sent_sequences": sent_sequences,
                "observed_direct_torque_state": observed_torque,
                "observed_complete_state": complete,
                "applied_action_echo_captured": bool(samples),
                "kunwei_stream_started": False,
                "contact_authorized": False,
                "training_dataset": False,
                "data_csv": str(csv_path) if samples else None,
            }
            timestamps = [
                float(row["controller_timestamp_s"])
                for row in samples
                if row.get("controller_timestamp_s") is not None
            ]
            unique_timestamps = len(set(timestamps))
            duplicate_count = len(timestamps) - unique_timestamps
            nonmonotonic_count = sum(
                right < left for left, right in zip(timestamps, timestamps[1:])
            )
            if timestamps:
                controller_span = max(0.0, timestamps[-1] - timestamps[0])
            else:
                controller_span = 0.0
            expected_rows = controller_span * 500.0 + (1.0 if timestamps else 0.0)
            achieved_rate = (
                (len(timestamps) - 1) / controller_span
                if controller_span > 0.0 and len(timestamps) > 1
                else 0.0
            )
            lineage_misses = sum(
                int(row["ack_command_lineage_missing"]) for row in samples
            )
            rate_gate = bool(
                timestamps
                and len(timestamps) >= 0.9 * expected_rows
            )
            strict_gate = {
                "no_lineage_misses": lineage_misses == 0,
                "no_nonmonotonic_timestamps": nonmonotonic_count == 0,
                "observed_direct_torque": observed_torque,
                "observed_complete": complete,
                "at_least_90_percent_expected_500hz_rows": rate_gate,
            }
            strict_gate["ok"] = all(strict_gate.values()) and failure is None
            evidence.update(
                {
                    "unique_controller_timestamps": unique_timestamps,
                    "observed_controller_span_s": controller_span,
                    "expected_500hz_rows": expected_rows,
                    "achieved_output_row_rate_hz": achieved_rate,
                    "duplicate_timestamp_count": duplicate_count,
                    "nonmonotonic_timestamp_count": nonmonotonic_count,
                    "ack_command_lineage_misses": lineage_misses,
                    "strict_success_gate": strict_gate,
                }
            )
            evidence["ok"] = bool(strict_gate["ok"])
            _write_json_new(evidence_path, evidence)
    return json.loads((output_dir / "evidence.json").read_text())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="read-only controller snapshot")
    status.add_argument("--robot-host", default="192.168.1.18")
    probe = subparsers.add_parser(
        "compile-probe",
        help="explicitly authorized controller parser/connectivity probe without motion",
    )
    probe.add_argument("--robot-host", default="192.168.1.18")
    probe.add_argument("--authorization", type=Path, required=True)
    probe.add_argument("--output-dir", type=Path, required=True)
    probe.add_argument("--connect-timeout-s", type=float, default=3.0)
    probe.add_argument("--probe-timeout-s", type=float, default=1.0)
    probe.add_argument("--live", action="store_true")
    probe.add_argument("--send-urscript", action="store_true")
    probe.add_argument("--no-motion", action="store_true")
    validate = subparsers.add_parser("validate", help="offline bundle validation")
    validate.add_argument("--bundle-manifest", type=Path, required=True)
    run = subparsers.add_parser("run", help="explicitly authorized live canary")
    run.add_argument("--robot-host", default="192.168.1.18")
    run.add_argument("--bundle-manifest", type=Path, required=True)
    run.add_argument("--authorization", type=Path, required=True)
    run.add_argument(
        "--canary-stage",
        choices=CANARY_STAGE_ORDER,
        required=True,
        help="staged live sequence; later stages require the immediately prior evidence",
    )
    run.add_argument("--compile-probe-evidence", type=Path, required=True)
    run.add_argument("--prior-stage-evidence", type=Path)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--connect-timeout-s", type=float, default=3.0)
    run.add_argument("--receiver-wait-s", type=float, default=2.0)
    run.add_argument("--live", action="store_true")
    run.add_argument("--send-urscript", action="store_true")
    run.add_argument("--write-rtde-inputs", action="store_true")
    run.add_argument("--allow-direct-torque", action="store_true")
    run.add_argument("--allow-motion", action="store_true")
    run.add_argument("--no-contact", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            result = readonly_status(args.robot_host)
        elif args.command == "compile-probe":
            result = run_compile_probe(args)
        else:
            bundle = validate_bundle(args.bundle_manifest)
            if args.command == "validate":
                result = {
                    "ok": True,
                    "claim_class": "offline_validated_no_live_actions",
                    "receiver_source_sha256": bundle.source_sha256,
                    "bundle_manifest_sha256": bundle.manifest_sha256,
                    "reference_artifact_sha256": bundle.reference_sha256,
                    "reference_rows": len(bundle.timeline.rows),
                    "capture_duration_s": bundle.timeline.duration_s,
                    "motion_performed": False,
                    "urscript_sent": False,
                    "rtde_inputs_written": False,
                    "kunwei_stream_started": False,
                }
            else:
                result = run_live(args, bundle)
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
