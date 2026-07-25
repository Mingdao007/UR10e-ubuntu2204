#!/usr/bin/env python3
"""Lightweight Step5d Direct Torque bridge; never loads or plays a TP program."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import select
import socket
import struct
import sys
import time
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
KUNWEI_TOOLS = Path("/home/andy/ur10e_ros2_ws/experiments/sensor-integration/kunwei-kwr75b/tools")
UR_HELPERS = Path("/home/andy/codex-private-skills-shared-main/skills/ur10e-realsetup/scripts")
for candidate in (ROOT / "tools", KUNWEI_TOOLS, UR_HELPERS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from _ur_common import RTDEClient  # noqa: E402
from capture_kunwei_kwr75_1khz import (  # noqa: E402
    FORCE_KG_TO_N,
    MOMENT_KG_M_TO_NM,
    START_STREAM,
    STOP_STREAM,
    parse_frame,
    pop_frames,
)
from step5d_tacdiffusion_direct_torque import (  # noqa: E402
    FORCE_NORM_LIMIT_N,
    TORQUE_NORM_LIMIT_NM,
    FixtureShadowRunner,
    RuntimeSample,
    SoftwareWrenchBaseline,
    Step5dDirectTorqueCore,
)
from ur10e_vic.tacdiffusion.expert import DeterministicExpert  # noqa: E402
from ur10e_vic.tacdiffusion.queue import (  # noqa: E402
    EpisodeRequest,
    PersistentRollingQueue,
    QueueDecision,
)


PROGRAM = "step5d_tacdiffusion_direct_torque_fixture_shadow_v1"
PROGRAM_DIR = ROOT / "programs/step5/step5d_tacdiffusion"
CALIBRATION_DEFAULT = ROOT / "config/step5d_tacdiffusion_sensor_frame_v1.json"
READINESS_DEFAULT = ROOT / "config/step5d_tacdiffusion_liveprep_v1.json"
DIRECT_STAGE = 25.0
DIRECT_STATE_WAITING = 0
DIRECT_STATE_TORQUE = 2
RUNTIME_PLAYING = 2

DOUBLE_INPUT_FIELDS = [f"input_double_register_{index}" for index in range(24, 48)]
INTEGER_INPUT_FIELDS = [f"input_int_register_{index}" for index in range(24, 32)]
INPUT_FIELDS = DOUBLE_INPUT_FIELDS + INTEGER_INPUT_FIELDS
MAINLINE_INTEGER_INPUT_FIELDS = [f"input_int_register_{index}" for index in range(24, 36)]
MAINLINE_INPUT_FIELDS = DOUBLE_INPUT_FIELDS + MAINLINE_INTEGER_INPUT_FIELDS
OUTPUT_FIELDS = [
    "timestamp",
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_q",
    "actual_qd",
    "target_moment",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    "output_double_register_34",
    "output_double_register_35",
] + [f"output_int_register_{index}" for index in range(24, 31)]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path, role: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"{role}_missing:{path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{role}_not_object:{path}")
    return payload


def package_hashes() -> dict[str, str]:
    result: dict[str, str] = {}
    for suffix in (".script", ".txt", ".urp"):
        path = PROGRAM_DIR / f"{PROGRAM}{suffix}"
        if not path.is_file():
            raise RuntimeError(f"package_missing:{path}")
        result[suffix] = sha256(path)
    return result


def wrench_transform_from_geometry(
    rotation_tcp_from_sensor: Sequence[Sequence[float]],
    sensor_origin_to_tcp_sensor_m: Sequence[float],
) -> tuple[tuple[float, ...], ...]:
    try:
        rotation = tuple(tuple(float(value) for value in row) for row in rotation_tcp_from_sensor)
        lever = tuple(float(value) for value in sensor_origin_to_tcp_sensor_m)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("sensor_frame_geometry_shape") from exc
    if len(rotation) != 3 or any(len(row) != 3 for row in rotation) or len(lever) != 3:
        raise RuntimeError("sensor_frame_geometry_shape")
    if not all(math.isfinite(value) for row in rotation for value in row) or not all(
        math.isfinite(value) for value in lever
    ):
        raise RuntimeError("sensor_frame_geometry_nonfinite")
    for row in range(3):
        for column in range(3):
            dot = sum(rotation[row][index] * rotation[column][index] for index in range(3))
            if abs(dot - (1.0 if row == column else 0.0)) > 1.0e-9:
                raise RuntimeError("sensor_frame_rotation_not_orthonormal")
    determinant = (
        rotation[0][0] * (rotation[1][1] * rotation[2][2] - rotation[1][2] * rotation[2][1])
        - rotation[0][1] * (rotation[1][0] * rotation[2][2] - rotation[1][2] * rotation[2][0])
        + rotation[0][2] * (rotation[1][0] * rotation[2][1] - rotation[1][1] * rotation[2][0])
    )
    if abs(determinant - 1.0) > 1.0e-9:
        raise RuntimeError("sensor_frame_rotation_not_proper")
    x, y, z = lever
    skew = ((0.0, -z, y), (z, 0.0, -x), (-y, x, 0.0))
    matrix = [[0.0] * 6 for _ in range(6)]
    for row in range(3):
        for column in range(3):
            matrix[row][column] = rotation[row][column]
            matrix[row + 3][column + 3] = rotation[row][column]
            matrix[row + 3][column] = -sum(
                rotation[row][index] * skew[index][column] for index in range(3)
            )
    return tuple(tuple(row) for row in matrix)


def validate_calibration(path: Path) -> tuple[dict[str, Any], str]:
    payload = load_json(path, "sensor_frame_calibration")
    if payload.get("schema") != "step5d_tacdiffusion_sensor_frame_v1":
        raise RuntimeError("sensor_frame_calibration_schema_mismatch")
    if payload.get("status") != "verified":
        raise RuntimeError("sensor_frame_calibration_not_verified")
    if payload.get("frame_token") != 5_252_001:
        raise RuntimeError("sensor_frame_token_mismatch")
    matrix = payload.get("wrench_transform_sensor_to_tcp_6x6")
    if not isinstance(matrix, list) or len(matrix) != 6:
        raise RuntimeError("sensor_frame_transform_missing")
    for row in matrix:
        if not isinstance(row, list) or len(row) != 6:
            raise RuntimeError("sensor_frame_transform_shape")
        if not all(math.isfinite(float(value)) for value in row):
            raise RuntimeError("sensor_frame_transform_nonfinite")
    expected_matrix = wrench_transform_from_geometry(
        payload.get("rotation_tcp_from_sensor_3x3", ()),
        payload.get("sensor_origin_to_tcp_sensor_m", ()),
    )
    if any(
        abs(float(matrix[row][column]) - expected_matrix[row][column]) > 1.0e-12
        for row in range(6)
        for column in range(6)
    ):
        raise RuntimeError("sensor_frame_transform_geometry_mismatch")
    evidence = payload.get("evidence")
    if (
        not isinstance(evidence, dict)
        or not isinstance(evidence.get("path"), str)
        or not isinstance(evidence.get("sha256"), str)
    ):
        raise RuntimeError("sensor_frame_evidence_hash_missing")
    if len(evidence["sha256"]) != 64 or any(
        character not in "0123456789abcdef" for character in evidence["sha256"]
    ):
        raise RuntimeError("sensor_frame_evidence_hash_invalid")
    evidence_path = (path.parent / evidence["path"]).resolve()
    calibration_root = path.parent.parent.resolve()
    try:
        evidence_path.relative_to(calibration_root)
    except ValueError as exc:
        raise RuntimeError("sensor_frame_evidence_path_escape") from exc
    if not evidence_path.is_file():
        raise RuntimeError("sensor_frame_evidence_missing")
    if sha256(evidence_path) != evidence["sha256"]:
        raise RuntimeError("sensor_frame_evidence_hash_mismatch")
    evidence_payload = load_json(evidence_path, "sensor_frame_evidence")
    if evidence_payload.get("schema") != "step5d_tacdiffusion_sensor_frame_evidence_v1":
        raise RuntimeError("sensor_frame_evidence_schema_mismatch")
    derived_geometry = evidence_payload.get("derived_geometry")
    evidence_transform = evidence_payload.get("wrench_transform")
    normal_binding = evidence_payload.get("normal_load_binding")
    if (
        not isinstance(derived_geometry, dict)
        or derived_geometry.get("rotation_tcp_from_sensor_3x3")
        != payload.get("rotation_tcp_from_sensor_3x3")
        or derived_geometry.get("sensor_origin_to_tcp_sensor_m")
        != payload.get("sensor_origin_to_tcp_sensor_m")
        or not isinstance(evidence_transform, dict)
        or evidence_transform.get("matrix_6x6") != matrix
        or not isinstance(normal_binding, dict)
        or normal_binding.get("axis") != payload.get("normal_force_axis")
        or float(normal_binding.get("sign", 0.0))
        != float(payload.get("normal_force_sign", 0.0))
    ):
        raise RuntimeError("sensor_frame_evidence_content_mismatch")
    if payload.get("normal_force_axis") != "fz":
        raise RuntimeError("normal_force_axis_must_be_tcp_fz")
    if float(payload.get("normal_force_sign", 0.0)) not in {-1.0, 1.0}:
        raise RuntimeError("normal_force_sign_invalid")
    return payload, sha256(path)


def validate_package_readiness(
    path: Path, local_package_sha256: dict[str, str]
) -> dict[str, Any]:
    payload = load_json(path, "live_readiness")
    if payload.get("schema") != "step5d_tacdiffusion_liveprep_v1":
        raise RuntimeError("readiness_schema_mismatch")
    if payload.get("program") != PROGRAM:
        raise RuntimeError("readiness_program_mismatch")
    package = payload.get("package")
    if not isinstance(package, dict):
        raise RuntimeError("readiness_package_missing")
    if package.get("sha256") != local_package_sha256:
        raise RuntimeError("readiness_package_hash_mismatch")
    if package.get("controller_readback_verified") is not True:
        raise RuntimeError("readiness_controller_readback_not_verified")
    relative = package.get("controller_readback_manifest")
    expected_manifest_sha = package.get("controller_readback_manifest_sha256")
    if not isinstance(relative, str) or not isinstance(expected_manifest_sha, str):
        raise RuntimeError("readiness_controller_readback_binding_missing")
    manifest_path = (ROOT / relative).resolve()
    try:
        manifest_path.relative_to(ROOT)
    except ValueError as exc:
        raise RuntimeError("readiness_controller_readback_path_escape") from exc
    if not manifest_path.is_file() or sha256(manifest_path) != expected_manifest_sha:
        raise RuntimeError("readiness_controller_readback_manifest_mismatch")
    manifest = load_json(manifest_path, "controller_readback_manifest")
    shas = manifest.get("sha256")
    if (
        manifest.get("status") != "controller read-back verified"
        or manifest.get("fresh_controller_sha_verified") is not True
        or not isinstance(shas, dict)
        or shas.get("local") != local_package_sha256
        or shas.get("controller") != local_package_sha256
        or shas.get("readback") != local_package_sha256
    ):
        raise RuntimeError("readiness_controller_readback_content_mismatch")
    return payload


def validate_authorization(
    path: Path,
    *,
    calibration_sha256: str,
    local_package_sha256: dict[str, str],
) -> dict[str, Any]:
    payload = load_json(path, "live_authorization")
    if payload.get("schema") != "step5d_tacdiffusion_live_authorization_v1":
        raise RuntimeError("authorization_schema_mismatch")
    if payload.get("program") != PROGRAM:
        raise RuntimeError("authorization_program_mismatch")
    if payload.get("motion_scope") not in {"no_contact", "contact"}:
        raise RuntimeError("authorization_motion_scope_invalid")
    if payload.get("explicit_user_authorization") is not True:
        raise RuntimeError("explicit_user_authorization_missing")
    if payload.get("controller_verified") is not True:
        raise RuntimeError("controller_5_26_not_verified")
    if not str(payload.get("polyscope_version", "")).startswith("5.26."):
        raise RuntimeError("controller_version_not_5_26")
    if payload.get("package_readback_verified") is not True:
        raise RuntimeError("package_readback_not_verified")
    if payload.get("calibration_sha256") != calibration_sha256:
        raise RuntimeError("authorization_calibration_hash_mismatch")
    if payload.get("package_sha256") != local_package_sha256:
        raise RuntimeError("authorization_package_hash_mismatch")
    shadow = payload.get("shadow")
    if shadow != {
        "kind": "fixture_shadow",
        "diagnostic_only": True,
        "command_invariant": True,
        "runtime_fallback_allowed": False,
        "checkpoint_bound": False,
        "model_active": False,
    }:
        raise RuntimeError("authorization_shadow_contract_mismatch")
    expires = payload.get("expires_at")
    if not isinstance(expires, str):
        raise RuntimeError("authorization_expiry_missing")
    try:
        expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("authorization_expiry_invalid") from exc
    if expiry.tzinfo is None or expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise RuntimeError("authorization_expired")
    reviews = payload.get("review_gate")
    if not isinstance(reviews, dict) or reviews.get("passed") is not True:
        raise RuntimeError("review_gate_not_passed")
    return payload


def apply_wrench_transform(values: Sequence[float], matrix: Sequence[Sequence[float]]) -> tuple[float, ...]:
    vector = tuple(float(value) for value in values)
    if len(vector) != 6 or not all(math.isfinite(value) for value in vector):
        raise RuntimeError("sensor_wrench_nonfinite")
    return tuple(sum(float(row[column]) * vector[column] for column in range(6)) for row in matrix)


def rtde_format(type_name: str) -> str:
    formats = {
        "DOUBLE": "d",
        "VECTOR6D": "6d",
        "INT32": "i",
        "UINT32": "I",
        "UINT64": "Q",
        "BOOL": "?",
    }
    if type_name not in formats:
        raise RuntimeError(f"unsupported_rtde_type:{type_name}")
    return formats[type_name]


class BridgeRTDE(RTDEClient):
    def setup_inputs(self, fields: list[str]) -> tuple[int, list[str]]:
        self._send_packet("I", ",".join(fields).encode())
        packet_type, data = self._recv_packet()
        if packet_type != ord("I"):
            raise RuntimeError(f"rtde_input_setup_response:{packet_type}")
        recipe = data[0]
        types = data[1:].decode("ascii", errors="replace").split(",")
        if recipe == 0 or any(value == "NOT_FOUND" for value in types):
            raise RuntimeError(f"rtde_input_recipe_invalid:{types}")
        return recipe, types

    def send_inputs(self, recipe: int, types: list[str], values: Sequence[Any]) -> None:
        if len(types) != len(values):
            raise RuntimeError("rtde_input_value_count_mismatch")
        payload = bytearray([recipe])
        for type_name, value in zip(types, values):
            payload.extend(struct.pack("!" + rtde_format(type_name), value))
        self._send_packet("U", bytes(payload))

    def receive_latest(
        self, recipe: int, types: list[str], timeout_s: float
    ) -> dict[str, Any] | None:
        assert self.sock is not None
        ready, _, _ = select.select([self.sock], [], [], timeout_s)
        if not ready:
            return None
        latest = None
        while True:
            packet_type, payload = self._recv_packet()
            if packet_type == ord("U") and payload and payload[0] == recipe:
                cursor = 1
                values: list[Any] = []
                for type_name in types:
                    fmt = rtde_format(type_name)
                    width = struct.calcsize("!" + fmt)
                    unpacked = struct.unpack("!" + fmt, payload[cursor : cursor + width])
                    cursor += width
                    values.append(unpacked[0] if len(unpacked) == 1 else tuple(unpacked))
                latest = dict(zip(OUTPUT_FIELDS, values))
            ready, _, _ = select.select([self.sock], [], [], 0.0)
            if not ready:
                return latest


class KunweiStream:
    def __init__(self, sensor_ip: str, port: int, timeout_s: float) -> None:
        self.sensor_ip = sensor_ip
        self.port = port
        self.timeout_s = timeout_s
        self.socket: socket.socket | None = None
        self.buffer = bytearray()

    def __enter__(self) -> "KunweiStream":
        self.socket = socket.create_connection(
            (self.sensor_ip, self.port), timeout=self.timeout_s
        )
        self.socket.setblocking(False)
        self.socket.sendall(START_STREAM)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.socket is not None:
            try:
                self.socket.sendall(STOP_STREAM)
            except OSError:
                pass
            self.socket.close()

    def receive_frames(self, timeout_s: float) -> list[tuple[float, ...]]:
        assert self.socket is not None
        ready, _, _ = select.select([self.socket], [], [], timeout_s)
        if ready:
            chunk = self.socket.recv(8192)
            if not chunk:
                raise RuntimeError("kunwei_stream_closed")
            self.buffer.extend(chunk)
        frames, _ = pop_frames(self.buffer, None)
        return [parse_frame(frame) for frame in frames]


def precontact_doubles(
    wrench_tcp: Sequence[float], *, heartbeat: int, stage: float, sensor_ok: bool
) -> list[float]:
    force_norm = math.sqrt(sum(float(value) ** 2 for value in wrench_tcp[:3]))
    torque_norm = math.sqrt(sum(float(value) ** 2 for value in wrench_tcp[3:]))
    normal_load = float(wrench_tcp[2])
    values = [0.0] * 24
    values[0:13] = [
        normal_load,
        force_norm,
        float(heartbeat),
        1.0 if sensor_ok else 0.0,
        0.0,
        12.0,
        torque_norm,
        *[float(value) for value in wrench_tcp],
    ]
    if abs(stage - 25.95) < 0.001:
        return values
    values[13:24] = [
        0.0,
        0.0,
        0.0,
        5.0,
        22.0,
        50.0,
        1.0,
        0.0,
        12.0 - normal_load,
        60.0,
        521.0 if abs(stage - 25.3) < 0.01 else 0.0,
    ]
    return values


def run_offline_campaign(
    queue: PersistentRollingQueue,
    requests: Sequence[EpisodeRequest],
    dispatch: Any,
) -> tuple[str, ...]:
    """Drain a queue with an injected fake transport and no wall-clock exit."""

    for request in requests:
        queue.append(request)
    queue.request_drain()
    results: list[str] = []
    while True:
        read = queue.next()
        if read.decision == QueueDecision.COMPLETE:
            return tuple(results)
        if read.decision != QueueDecision.ITEM or read.item is None:
            raise RuntimeError(f"offline campaign cannot progress: {read.decision.value}")
        result_identity = str(dispatch(read.item))
        queue.complete(result_identity=result_identity)
        results.append(read.item.dispatch_id)


def packet_values(packet: Any, *, mainline: bool = False) -> list[Any]:
    doubles = [
        *packet.equilibrium_pose,
        *packet.stiffness,
        *packet.damping,
        *packet.raw_feedforward_wrench,
    ]
    integers = [
        packet.mode,
        packet.sequence_after,
        packet.heartbeat,
        packet.lease_id,
        packet.model_sequence_after,
        packet.model_period_us,
        packet.model_mode,
        packet.wrench_frame_token,
    ]
    if mainline:
        integers.extend((packet.model_timestamp_us, packet.home_ack_identity, packet.home_consume_identity, packet.episode_identity))
    return doubles + integers


def status(
    calibration_path: Path, readiness_path: Path = READINESS_DEFAULT
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "program": PROGRAM,
        "package": None,
        "calibration": "blocked",
        "live_ready": False,
        "motion_performed": False,
    }
    try:
        result["package"] = package_hashes()
    except RuntimeError as exc:
        result["package_error"] = str(exc)
    if result["package"] is not None:
        try:
            readiness = validate_package_readiness(readiness_path, result["package"])
            result["package_readback"] = "verified"
            result["readiness_status"] = readiness.get("status")
            result["blockers"] = readiness.get("blockers", [])
        except RuntimeError as exc:
            result["package_readback"] = "blocked"
            result["readiness_error"] = str(exc)
    try:
        _, calibration_hash = validate_calibration(calibration_path)
        result["calibration"] = "verified"
        result["calibration_sha256"] = calibration_hash
    except RuntimeError as exc:
        result["calibration_error"] = str(exc)
    return result


def run_live(args: argparse.Namespace) -> int:
    if not (args.live and args.allow_kunwei_stream_command and args.write_rtde_inputs):
        raise RuntimeError("live_start_requires_all_three_explicit_flags")
    package = package_hashes()
    validate_package_readiness(args.readiness, package)
    calibration, calibration_hash = validate_calibration(args.calibration)
    validate_authorization(
        args.authorization,
        calibration_sha256=calibration_hash,
        local_package_sha256=package,
    )
    matrix = calibration["wrench_transform_sensor_to_tcp_6x6"]
    if args.episode_reference is None:
        raise RuntimeError("mainline_episode_reference_injection_required")
    reference_payload = load_json(args.episode_reference, "episode_reference")
    if isinstance(reference_payload, dict) and isinstance(reference_payload.get("references"), list):
        reference_rows = reference_payload["references"]
    elif isinstance(reference_payload, list):
        reference_rows = reference_payload
    else:
        raise RuntimeError("episode_reference_rows_missing")
    if not reference_rows or not all(isinstance(row, dict) for row in reference_rows):
        raise RuntimeError("episode_reference_rows_invalid")

    def episode_reference_provider(sample: RuntimeSample) -> dict[str, Any]:
        if sample.tick >= len(reference_rows):
            raise RuntimeError("episode_reference_exhausted")
        return reference_rows[sample.tick]

    normal_axis = {"fx": 0, "fy": 1, "fz": 2}[calibration["normal_force_axis"]]
    normal_sign = float(calibration["normal_force_sign"])
    baseline = SoftwareWrenchBaseline()
    latest_raw_si: tuple[float, ...] | None = None
    latest_sensor_time = 0.0
    heartbeat = 0
    direct_tick = 0
    direct_started = 0.0
    direct_core = Step5dDirectTorqueCore(
        lease_id=int(time.time()) & 0x7FFFFFFF or 1,
        shadow=FixtureShadowRunner(lambda sample: (sample.elapsed_s, *sample.wrench_tcp_si[:5])),
        mainline=True,
        expert=DeterministicExpert(),
        episode_reference_provider=episode_reference_provider,
    )
    last_packet = None
    rebaseline_requested = False
    output: dict[str, Any] | None = None
    with KunweiStream(args.sensor_ip, args.sensor_port, args.connect_timeout_s) as sensor:
        with BridgeRTDE(args.robot_host, timeout=args.connect_timeout_s) as rtde:
            rtde.negotiate()
            output_recipe, output_types = rtde.setup_outputs(500.0, OUTPUT_FIELDS)
            input_recipe, input_types = rtde.setup_inputs(MAINLINE_INPUT_FIELDS)
            rtde.start()
            next_release = time.monotonic()
            while True:
                for raw in sensor.receive_frames(0.0):
                    raw_si = tuple(
                        float(raw[index]) * (FORCE_KG_TO_N if index < 3 else MOMENT_KG_M_TO_NM)
                        for index in range(6)
                    )
                    if not all(math.isfinite(value) for value in raw_si):
                        raise RuntimeError("kunwei_raw_nonfinite")
                    latest_raw_si = raw_si
                    latest_sensor_time = time.monotonic()
                    if not baseline.ready:
                        baseline.add(raw_si)
                    else:
                        zeroed = tuple(value - bias for value, bias in zip(raw_si, baseline.bias()))
                        wrench = apply_wrench_transform(zeroed, matrix)
                        if math.sqrt(sum(value * value for value in wrench[:3])) > FORCE_NORM_LIMIT_N:
                            raise RuntimeError("kunwei_1khz_force_guard")
                        if math.sqrt(sum(value * value for value in wrench[3:])) > TORQUE_NORM_LIMIT_NM:
                            raise RuntimeError("kunwei_1khz_torque_guard")
                now = time.monotonic()
                if now < next_release:
                    time.sleep(min(next_release - now, 0.0005))
                    continue
                next_release += 0.002
                current = rtde.receive_latest(output_recipe, output_types, 0.0)
                if current is not None:
                    output = current
                if output is None or latest_raw_si is None:
                    continue
                if now - latest_sensor_time > 0.010:
                    raise RuntimeError("kunwei_sensor_stale")
                zero_request = float(output.get("output_double_register_34", 0.0)) > 0.5
                if zero_request and not rebaseline_requested:
                    baseline = SoftwareWrenchBaseline()
                    rebaseline_requested = True
                elif not zero_request:
                    rebaseline_requested = False
                if not baseline.ready:
                    wrench_tcp = (0.0,) * 6
                else:
                    zeroed = tuple(value - bias for value, bias in zip(latest_raw_si, baseline.bias()))
                    wrench_tcp_list = list(apply_wrench_transform(zeroed, matrix))
                    wrench_tcp_list[normal_axis] *= normal_sign
                    wrench_tcp = tuple(wrench_tcp_list)
                heartbeat += 1
                stage = float(output.get("output_double_register_35", 0.0))
                runtime_state = int(output.get("runtime_state", 0))
                direct_state = int(output.get("output_int_register_24", -1))
                direct_waiting = (
                    runtime_state == RUNTIME_PLAYING
                    and abs(stage - DIRECT_STAGE) < 0.01
                    and direct_state == DIRECT_STATE_WAITING
                    and baseline.ready
                )
                if last_packet is None and not direct_waiting:
                    doubles = precontact_doubles(
                        wrench_tcp,
                        heartbeat=heartbeat,
                        stage=stage,
                        sensor_ok=baseline.ready,
                    )
                    # Mainline recipe includes timestamp, home ACK/consume,
                    # and episode identity: disabled precontact is exactly 12
                    # integer fields, not the legacy 8-field packet.
                    rtde.send_inputs(input_recipe, input_types, doubles + [0] * 12)
                    continue
                if last_packet is None:
                    direct_started = now
                current_reference = reference_rows[direct_tick] if direct_tick < len(reference_rows) else None
                if not isinstance(current_reference, dict) or "reaction_normal_base" not in current_reference:
                    raise RuntimeError("episode_reference_reaction_normal_missing")
                # Completion is an explicit campaign END, never a fixed
                # duration outcome.  The live owner may set this flag only
                # after its queue/lifecycle has issued END.
                if getattr(args, "end_campaign", False) and last_packet is not None:
                    completion = replace(
                        last_packet,
                        sequence_before=last_packet.sequence_after + 1,
                        sequence_after=last_packet.sequence_after + 1,
                        heartbeat=last_packet.sequence_after + 1,
                        mode=2,
                    )
                    rtde.send_inputs(input_recipe, input_types, packet_values(completion, mainline=True))
                    return 0
                sample = RuntimeSample(
                    tick=direct_tick,
                    elapsed_s=direct_tick * 0.002,
                    tcp_pose_base=output["actual_TCP_pose"],
                    tcp_speed_base=output["actual_TCP_speed"],
                    joint_position_rad=output["actual_q"],
                    joint_speed_rad_s=output["actual_qd"],
                    joint_torque_nm=output["target_moment"],
                    wrench_tcp_si=wrench_tcp,
                    control_reaction_normal_base=tuple(current_reference["reaction_normal_base"]),
                )
                result = direct_core.tick(sample)
                last_packet = result.packet
                direct_tick += 1
                rtde.send_inputs(input_recipe, input_types, packet_values(result.packet, mainline=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?", choices=("status", "start"), default="status")
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--connect-timeout-s", type=float, default=120.0)
    parser.add_argument("--calibration", type=Path, default=CALIBRATION_DEFAULT)
    parser.add_argument("--readiness", type=Path, default=READINESS_DEFAULT)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-kunwei-stream-command", action="store_true")
    parser.add_argument("--write-rtde-inputs", action="store_true")
    parser.add_argument("--end-campaign", action="store_true", help="explicit END; never implied by elapsed time")
    parser.add_argument("--episode-reference", type=Path, help="precomputed SurfaceCalibration/BoundedTrajectory reference rows")
    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(status(args.calibration, args.readiness), indent=2, sort_keys=True))
        return 0
    if args.authorization is None:
        raise SystemExit("start requires --authorization <fresh artifact>")
    return run_live(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2)
