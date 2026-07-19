#!/usr/bin/env python3
"""Run the exact V3 bounded-return controller in isolated digest-pinned URSim."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import socket
import struct
import subprocess
import tempfile
import time
from typing import Any, Mapping, Sequence

import build_step5d_autotune_v3_return_route_evidence as return_evidence
from step5d_autotune_v3.profile import control_fingerprint, load_contract
from step5d_autotune_v3.state import orchestration_fingerprint
from ur10e_experiment_runtime.return_route import (
    URSIM_RETURN_TRACE_SCHEMA,
    return_orientation_distance_rad,
    validate_motion_capable_ursim_return_trace,
)


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_IMAGE = (
    "universalrobots/ursim_e-series:5.25.2@sha256:"
    "a4c4365207d54d1a1a4ead87526ff3781e2e98ae703c72f362060a46688fa7a4"
)
PROGRAM_PATH = (
    ROOT / "programs/step5/step5d/step5d_strict_rnn_autotune_v3.script"
)
OUTPUT_FIELDS = (
    "timestamp",
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_qd",
    "output_double_register_35",
    "output_double_register_39",
    "output_int_register_24",
    "robot_mode",
    "safety_mode",
)


class URSimReturnGateError(RuntimeError):
    pass


def _docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = shlex.join(["docker", *arguments])
    completed = subprocess.run(
        ["sg", "docker", "-c", command],
        capture_output=True,
        text=True,
        timeout=60.0,
        check=False,
    )
    if check and completed.returncode != 0:
        raise URSimReturnGateError(
            (completed.stderr or completed.stdout).strip()
            or f"Docker exited {completed.returncode}"
        )
    return completed


def _dashboard(host: str, command: str, timeout_s: float = 2.0) -> str:
    with socket.create_connection((host, 29999), timeout=timeout_s) as connection:
        connection.settimeout(timeout_s)
        connection.recv(1024)
        connection.sendall((command + "\n").encode("ascii"))
        return connection.recv(1024).decode(errors="replace").strip()


def _wait_dashboard(host: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    error = "not attempted"
    while time.monotonic() < deadline:
        try:
            return _dashboard(host, "PolyscopeVersion")
        except OSError as exc:
            error = str(exc)
            time.sleep(0.25)
    raise URSimReturnGateError(f"URSim Dashboard did not start: {error}")


def _wait_robot_mode(host: str, token: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    observed = ""
    while time.monotonic() < deadline:
        observed = _dashboard(host, "robotmode")
        if token in observed.upper():
            return observed
        time.sleep(0.25)
    raise URSimReturnGateError(
        f"URSim robot mode did not reach {token}: {observed}"
    )


def _extract_between(source: str, start: str, end: str) -> str:
    start_index = source.index(start)
    end_index = source.index(end, start_index)
    return source[start_index:end_index].rstrip() + "\n"


def build_ursim_return_program(source: str) -> tuple[str, str]:
    """Embed the production bounded segment function byte-for-byte."""

    norm = _extract_between(
        source,
        "def codex_autotune_norm3(x, y, z):",
        "\nthread codex_autotune_return_guard_thread():",
    )
    controller = _extract_between(
        source,
        "def codex_autotune_bounded_return_segment(",
        "\ndef codex_autotune_typed_target_verified(",
    )
    controller_sha256 = hashlib.sha256(controller.encode("utf-8")).hexdigest()
    embedded_norm = "\n".join(f"  {line}" for line in norm.rstrip().splitlines())
    embedded_controller = "\n".join(
        f"  {line}" for line in controller.rstrip().splitlines()
    )
    wrapper = f"""def codex_step5d_v3_ursim_return_gate():
  global codex_autotune_return_guard_reason = 0.0
  global codex_autotune_return_segment_id = 0
{embedded_norm}
{embedded_controller}
  write_output_integer_register(24, 100)
  local start_pose = get_actual_tcp_pose()
  local rise_pose = p[start_pose[0], start_pose[1], start_pose[2] + 0.005, start_pose[3], start_pose[4], start_pose[5]]
  local transfer_pose = pose_trans(rise_pose, p[0.005, 0.0, 0.0, 0.0, 0.0, 0.010])
  local final_pose = p[transfer_pose[0], transfer_pose[1], start_pose[2], transfer_pose[3], transfer_pose[4], transfer_pose[5]]
  write_output_float_register(39, 1.0)
  local rise_ok = codex_autotune_bounded_return_segment(rise_pose, 0.060, 0.040, 1.0)
  if rise_ok:
    write_output_integer_register(24, 101)
    write_output_float_register(39, 2.0)
    local transfer_ok = codex_autotune_bounded_return_segment(transfer_pose, 0.135, 0.090, 2.0)
    if transfer_ok:
      write_output_integer_register(24, 102)
      write_output_float_register(39, 3.0)
      local final_ok = codex_autotune_bounded_return_segment(final_pose, 0.060, 0.040, 3.0)
      if final_ok:
        write_output_integer_register(24, 104)
      else:
        write_output_integer_register(24, -103)
      end
    else:
      write_output_integer_register(24, -102)
    end
  else:
    write_output_integer_register(24, -101)
  end
  sleep(0.25)
end
codex_step5d_v3_ursim_return_gate()
"""
    normalized = "\n".join(
        line[2:] for line in embedded_controller.splitlines()
    ) + "\n"
    if normalized != controller:
        raise URSimReturnGateError("production return controller embedding differs")
    return wrapper, controller_sha256


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise URSimReturnGateError("URSim closed the RTDE stream")
        data.extend(chunk)
    return bytes(data)


def _receive(connection: socket.socket) -> tuple[int, bytes]:
    size, kind = struct.unpack("!HB", _recv_exact(connection, 3))
    return kind, _recv_exact(connection, size - 3) if size > 3 else b""


def _send(connection: socket.socket, kind: str, payload: bytes = b"") -> None:
    if kind not in {"V", "O", "S"}:
        raise URSimReturnGateError("RTDE gate attempted a forbidden packet type")
    connection.sendall(struct.pack("!HB", len(payload) + 3, ord(kind)) + payload)


def _receive_expected(connection: socket.socket, expected: str) -> bytes:
    for _ in range(32):
        kind, payload = _receive(connection)
        if kind == ord("M"):
            continue
        if kind != ord(expected):
            raise URSimReturnGateError(
                f"unexpected RTDE packet {kind}, expected {ord(expected)}"
            )
        return payload
    raise URSimReturnGateError(f"RTDE packet {expected} was not received")


def _decode_sample(payload: bytes, recipe_id: int, types: Sequence[str]) -> dict[str, Any]:
    formats = {"DOUBLE": "d", "VECTOR6D": "6d", "UINT32": "I", "INT32": "i"}
    if not payload or payload[0] != recipe_id:
        raise URSimReturnGateError("RTDE output recipe identity differs")
    cursor = 1
    values: list[Any] = []
    for type_name in types:
        if type_name not in formats:
            raise URSimReturnGateError(f"unsupported RTDE type: {type_name}")
        fmt = formats[type_name]
        width = struct.calcsize("!" + fmt)
        unpacked = struct.unpack("!" + fmt, payload[cursor : cursor + width])
        cursor += width
        values.append(unpacked[0] if len(unpacked) == 1 else list(unpacked))
    if cursor != len(payload):
        raise URSimReturnGateError("RTDE sample has trailing bytes")
    return dict(zip(OUTPUT_FIELDS, values, strict=True))


def _capture_motion(host: str, program: str, timeout_s: float) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    with socket.create_connection((host, 30004), timeout=2.0) as rtde:
        rtde.settimeout(2.0)
        _send(rtde, "V", struct.pack("!H", 2))
        if _receive_expected(rtde, "V") != b"\x01":
            raise URSimReturnGateError("RTDE v2 negotiation failed")
        _send(rtde, "O", struct.pack("!d", 500.0) + ",".join(OUTPUT_FIELDS).encode("ascii"))
        setup = _receive_expected(rtde, "O")
        if not setup or setup[0] == 0:
            raise URSimReturnGateError("RTDE output recipe setup failed")
        recipe_id = setup[0]
        types = setup[1:].decode("ascii").split(",")
        if len(types) != len(OUTPUT_FIELDS):
            raise URSimReturnGateError("RTDE output recipe types differ")
        _send(rtde, "S")
        if _receive_expected(rtde, "S") != b"\x01":
            raise URSimReturnGateError("RTDE stream start failed")
        with socket.create_connection((host, 30002), timeout=2.0) as secondary:
            secondary.sendall(program.encode("utf-8"))
        deadline = time.monotonic() + timeout_s
        completed_samples = 0
        while time.monotonic() < deadline:
            kind, payload = _receive(rtde)
            if kind == ord("M"):
                continue
            if kind != ord("U"):
                raise URSimReturnGateError(f"unexpected RTDE stream packet: {kind}")
            row = _decode_sample(payload, recipe_id, types)
            if not all(
                math.isfinite(float(value))
                for name in ("timestamp", "output_double_register_35", "output_double_register_39")
                for value in (row[name],)
            ):
                raise URSimReturnGateError("URSim RTDE sample is nonfinite")
            samples.append(row)
            state = int(row["output_int_register_24"])
            if state < 0:
                raise URSimReturnGateError(f"URSim return program failed state={state}")
            completed_samples = completed_samples + 1 if state == 104 else 0
            if completed_samples >= 10:
                return samples
    last = samples[-1] if samples else {}
    diagnostic = {
        "sample_count": len(samples),
        "last_state": last.get("output_int_register_24"),
        "last_phase": last.get("output_double_register_35"),
        "last_segment": last.get("output_double_register_39"),
        "last_pose": last.get("actual_TCP_pose"),
        "last_speed": last.get("actual_TCP_speed"),
        "robot_mode": last.get("robot_mode"),
        "safety_mode": last.get("safety_mode"),
    }
    raise URSimReturnGateError(
        "URSim return program did not complete before timeout: "
        + json.dumps(diagnostic, sort_keys=True)
    )


def _summary(samples: Sequence[Mapping[str, Any]]) -> dict[str, object]:
    first_pose = tuple(float(value) for value in samples[0]["actual_TCP_pose"])
    segment_order: list[int] = []
    max_position = 0.0
    max_angle = 0.0
    max_speed = 0.0
    max_acceleration = 0.0
    max_qd = 0.0
    previous: Mapping[str, Any] | None = None
    for row in samples:
        pose = tuple(float(value) for value in row["actual_TCP_pose"])
        speed = tuple(float(value) for value in row["actual_TCP_speed"])
        qd = tuple(float(value) for value in row["actual_qd"])
        segment = int(round(float(row["output_double_register_39"])))
        if segment in (1, 2, 3) and (not segment_order or segment_order[-1] != segment):
            segment_order.append(segment)
        max_position = max(
            max_position,
            math.sqrt(sum((pose[index] - first_pose[index]) ** 2 for index in range(3))),
        )
        max_angle = max(
            max_angle,
            return_orientation_distance_rad(first_pose[3:], pose[3:]),
        )
        max_speed = max(max_speed, math.sqrt(sum(value * value for value in speed[3:])))
        max_qd = max(max_qd, *(abs(value) for value in qd))
        if previous is not None:
            dt = float(row["timestamp"]) - float(previous["timestamp"])
            previous_speed = tuple(float(value) for value in previous["actual_TCP_speed"])
            if dt > 0.0:
                acceleration = math.sqrt(
                    sum((speed[index] - previous_speed[index]) ** 2 for index in range(3, 6))
                ) / dt
                max_acceleration = max(max_acceleration, acceleration)
        previous = row
    return {
        "sample_count": len(samples),
        "motion_observed": max_position > 0.001,
        "max_position_excursion_m": max_position,
        "max_angular_excursion_rad": max_angle,
        "max_angular_speed_rad_s": max_speed,
        "max_angular_acceleration_rad_s2": max_acceleration,
        "max_abs_qd_rad_s": max_qd,
        "segment_order_observed": segment_order,
        "completion_state": int(samples[-1]["output_int_register_24"]),
        "forbidden_action_count": 0,
    }


def _atomic_new(path: Path, payload: Mapping[str, object]) -> None:
    output = path.absolute()
    if output.exists() or output.is_symlink():
        raise URSimReturnGateError("URSim return evidence output must be fresh")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(*, output_path: Path, timeout_s: float = 45.0) -> dict[str, object]:
    token = f"{os.getpid()}-{time.time_ns() & 0xFFFFFFFF:x}"
    network_name = f"step5d-v3-return-{token}"
    container_name = f"step5d-v3-return-{token}"
    container_created = False
    network_created = False
    program_stopped = False
    version = ""
    image_id = ""
    samples: list[dict[str, Any]] = []
    source = PROGRAM_PATH.read_text(encoding="utf-8")
    program, controller_sha256 = build_ursim_return_program(source)
    base = return_evidence.build_document(include_ursim_trace=False)
    control = control_fingerprint(load_contract())
    orchestration = orchestration_fingerprint(ROOT)
    try:
        _docker("network", "create", "--internal", network_name)
        network_created = True
        _docker(
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "--network",
            network_name,
            "-e",
            "ROBOT_MODEL=UR10",
            EXPECTED_IMAGE,
        )
        container_created = True
        inspect = json.loads(
            _docker("inspect", "--type", "container", container_name).stdout
        )[0]
        if (
            inspect["Config"]["Image"] != EXPECTED_IMAGE
            or inspect["HostConfig"]["Privileged"] is True
            or inspect["HostConfig"].get("PortBindings")
            or inspect.get("Mounts")
            or set(inspect["NetworkSettings"]["Networks"]) != {network_name}
        ):
            raise URSimReturnGateError("isolated URSim container contract differs")
        image_id = str(inspect["Image"])
        host = str(inspect["NetworkSettings"]["Networks"][network_name]["IPAddress"])
        if not re.fullmatch(r"172\.[0-9]+\.[0-9]+\.[0-9]+", host):
            raise URSimReturnGateError("isolated URSim address differs")
        version = _wait_dashboard(host, timeout_s)
        if "5.25.2" not in version:
            raise URSimReturnGateError("URSim Polyscope version differs")
        _wait_robot_mode(host, "POWER_OFF", timeout_s)
        if "POWERING ON" not in _dashboard(host, "power on").upper():
            raise URSimReturnGateError("URSim rejected the power-on command")
        _wait_robot_mode(host, "IDLE", timeout_s)
        if "BRAKE RELEASING" not in _dashboard(host, "brake release").upper():
            raise URSimReturnGateError("URSim rejected the brake-release command")
        _wait_robot_mode(host, "RUNNING", timeout_s)
        if "NORMAL" not in _dashboard(host, "safetymode").upper():
            raise URSimReturnGateError("URSim safety mode is not NORMAL")
        samples = _capture_motion(host, program, timeout_s)
        _dashboard(host, "stop")
        program_stopped = True
        _dashboard(host, "power off")
    finally:
        if container_created:
            _docker("rm", "-f", container_name, check=False)
        if network_created:
            _docker("network", "rm", network_name, check=False)
    cleanup = {
        "container_removed": _docker(
            "inspect", "--type", "container", container_name, check=False
        ).returncode != 0,
        "network_removed": _docker("network", "inspect", network_name, check=False).returncode
        != 0,
        "program_stopped": program_stopped,
    }
    converted = [
        {
            "controller_timestamp_s": float(row["timestamp"]),
            "actual_tcp_pose": [float(value) for value in row["actual_TCP_pose"]],
            "actual_tcp_speed": [float(value) for value in row["actual_TCP_speed"]],
            "actual_qd": [float(value) for value in row["actual_qd"]],
            "return_phase_echo": float(row["output_double_register_35"]),
            "return_segment_id": int(round(float(row["output_double_register_39"]))),
            "completion_state": int(row["output_int_register_24"]),
            "safety_mode": int(row["safety_mode"]),
        }
        for row in samples
    ]
    document = {
        "schema": URSIM_RETURN_TRACE_SCHEMA,
        "status": "pass",
        "claim_boundary": "isolated_ursim_motion_only_not_live_certification",
        "identity": {
            "control_fingerprint": control,
            "orchestration_fingerprint": orchestration,
        },
        "source_binding_sha256": base["source_binding_sha256"],
        "triplet_sha256": base["local_triplet_sha256"],
        "image": {
            "reference": EXPECTED_IMAGE,
            "image_id": image_id,
            "polyscope_version": re.search(r"\b5\.25\.2\b", version).group(0),
            "robot_model": "UR10",
        },
        "network": {
            "internal": True,
            "host_ports_published": False,
            "real_robot_network_connected": False,
        },
        "route": {
            "controller_function": "codex_autotune_bounded_return_segment",
            "controller_function_sha256": controller_sha256,
            "segment_order": [1, 2, 3],
            "vertical_transfer_vertical": True,
            "relative_test_motion_only": True,
            "no_contact": True,
        },
        "samples": converted,
        "summary": _summary(samples),
        "cleanup": cleanup,
    }
    try:
        validate_motion_capable_ursim_return_trace(
            document,
            expected_control_fingerprint=control,
            expected_orchestration_fingerprint=orchestration,
            expected_source_binding_sha256=str(base["source_binding_sha256"]),
            expected_triplet_sha256=dict(base["local_triplet_sha256"]),
        )
    except ValueError as exc:
        raise URSimReturnGateError(
            f"URSim return evidence rejected: {exc}; "
            + json.dumps(
                {"summary": document["summary"], "cleanup": cleanup},
                sort_keys=True,
            )
        ) from exc
    _atomic_new(output_path, document)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=45.0)
    args = parser.parse_args(argv)
    run(output_path=args.output, timeout_s=args.timeout_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
