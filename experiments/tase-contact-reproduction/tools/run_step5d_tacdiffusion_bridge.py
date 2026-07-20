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


def validate_calibration(path: Path) -> tuple[dict[str, Any], str]:
    payload = load_json(path, "sensor_frame_calibration")
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
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or not isinstance(evidence.get("sha256"), str):
        raise RuntimeError("sensor_frame_evidence_hash_missing")
    if len(evidence["sha256"]) != 64:
        raise RuntimeError("sensor_frame_evidence_hash_invalid")
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


def packet_values(packet: Any) -> list[Any]:
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
    )
    last_packet = None
    rebaseline_requested = False
    output: dict[str, Any] | None = None
    with KunweiStream(args.sensor_ip, args.sensor_port, args.connect_timeout_s) as sensor:
        with BridgeRTDE(args.robot_host, timeout=args.connect_timeout_s) as rtde:
            rtde.negotiate()
            output_recipe, output_types = rtde.setup_outputs(500.0, OUTPUT_FIELDS)
            input_recipe, input_types = rtde.setup_inputs(INPUT_FIELDS)
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
                    rtde.send_inputs(input_recipe, input_types, doubles + [0] * 8)
                    continue
                if last_packet is None:
                    direct_started = now
                elapsed = now - direct_started
                if elapsed >= 60.0 and last_packet is not None:
                    completion = replace(
                        last_packet,
                        sequence_before=last_packet.sequence_after + 1,
                        sequence_after=last_packet.sequence_after + 1,
                        heartbeat=last_packet.sequence_after + 1,
                        mode=2,
                    )
                    rtde.send_inputs(input_recipe, input_types, packet_values(completion))
                    return 0
                sample = RuntimeSample(
                    tick=direct_tick,
                    elapsed_s=elapsed,
                    tcp_pose_base=output["actual_TCP_pose"],
                    tcp_speed_base=output["actual_TCP_speed"],
                    joint_position_rad=output["actual_q"],
                    joint_speed_rad_s=output["actual_qd"],
                    joint_torque_nm=output["target_moment"],
                    wrench_tcp_si=wrench_tcp,
                    control_reaction_normal_base=(0.0, 0.0, -1.0),
                )
                result = direct_core.tick(sample)
                last_packet = result.packet
                direct_tick += 1
                rtde.send_inputs(input_recipe, input_types, packet_values(result.packet))


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
