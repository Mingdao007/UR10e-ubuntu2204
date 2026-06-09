#!/usr/bin/env python3
"""Kunwei KWR75B TCP to UR RTDE input-register bridge.

Live-use boundary:
- sends Kunwei 0x48 stream command only with --allow-kunwei-stream-command
- writes UR RTDE input registers only with --write-rtde-inputs
- does not send URScript, start a UR program, move the robot, write TCP/payload,
  call zero_ftsensor(), or send Kunwei zero/tare/config writes
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import select
import socket
import statistics
import struct
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
KUNWEI_TOOLS = Path("/home/andy/ur10e_ros2_ws/ft_sensor/kunwei/kwr75b/tools")
UR_REALSETUP_SCRIPTS = Path("/home/andy/codex-private-skills/skills/ur10e-realsetup/scripts")
sys.path.insert(0, str(KUNWEI_TOOLS))
sys.path.insert(0, str(UR_REALSETUP_SCRIPTS))

from capture_kunwei_kwr75_1khz import (  # noqa: E402
    FIELDS as KUNWEI_RAW_FIELDS,
    FORCE_KG_TO_N,
    MOMENT_KG_M_TO_NM,
    START_STREAM,
    STOP_STREAM,
    parse_frame,
    pop_frames,
)
from _ur_common import RTDEClient, dashboard_exchange  # noqa: E402


BASE_INPUT_FIELDS = [
    "input_double_register_24",
    "input_double_register_25",
    "input_double_register_26",
    "input_double_register_27",
    "input_double_register_28",
    "input_double_register_29",
    "input_double_register_30",
    "input_double_register_31",
    "input_double_register_32",
    "input_double_register_33",
    "input_double_register_34",
    "input_double_register_35",
    "input_double_register_36",
]
BASE_INPUT_NAMES = [
    "normal_force_n",
    "force_norm_n",
    "heartbeat",
    "sensor_ok",
    "stop_request",
    "target_force_n",
    "torque_norm_nm",
    "fx_n_zeroed",
    "fy_n_zeroed",
    "fz_n_zeroed",
    "mx_nm_zeroed",
    "my_nm_zeroed",
    "mz_nm_zeroed",
]
STEP4E_INPUT_FIELDS = [
    "input_double_register_37",
    "input_double_register_38",
    "input_double_register_39",
    "input_double_register_40",
    "input_double_register_41",
    "input_double_register_42",
    "input_double_register_43",
    "input_double_register_44",
    "input_double_register_45",
    "input_double_register_46",
    "input_double_register_47",
]
STEP4E_INPUT_NAMES = [
    "step4e_cmd_vx_m_s",
    "step4e_cmd_vy_m_s",
    "step4e_cmd_vz_m_s",
    "step4e_cmd_wx_rad_s",
    "step4e_cmd_wy_rad_s",
    "step4e_cmd_wz_rad_s",
    "step4e_cmd_valid",
    "step4e_progress_m",
    "step4e_force_error_n",
    "step4e_orientation_error_rad",
    "step4e_controller_state",
]
INPUT_FIELDS = BASE_INPUT_FIELDS + STEP4E_INPUT_FIELDS
INPUT_NAMES = BASE_INPUT_NAMES + STEP4E_INPUT_NAMES
OUTPUT_FIELDS = [
    "actual_TCP_pose",
    "actual_TCP_speed",
    "runtime_state",
    "robot_mode",
    "safety_mode",
    "speed_scaling",
    "output_double_register_24",
    "output_double_register_25",
    "output_double_register_26",
    "output_double_register_27",
    "output_double_register_28",
    "output_double_register_29",
    "output_double_register_30",
    "output_double_register_31",
    "output_double_register_32",
    "output_double_register_33",
    "output_double_register_34",
    "output_double_register_35",
    "output_double_register_36",
    "output_double_register_37",
    "output_double_register_38",
    "output_double_register_39",
    "output_double_register_40",
    "output_double_register_41",
    "output_double_register_42",
    "output_double_register_43",
    "output_double_register_44",
    "output_double_register_45",
    "output_double_register_46",
    "output_double_register_47",
]


STEP4E_START_XY = (0.43301, 0.10802)
STEP4E_END_XY = (0.49274, 0.23877)
STEP4E_LINE_DX = STEP4E_END_XY[0] - STEP4E_START_XY[0]
STEP4E_LINE_DY = STEP4E_END_XY[1] - STEP4E_START_XY[1]
STEP4E_LINE_LENGTH_M = math.hypot(STEP4E_LINE_DX, STEP4E_LINE_DY)
STEP4E_LINE_UNIT_XY = (
    STEP4E_LINE_DX / STEP4E_LINE_LENGTH_M,
    STEP4E_LINE_DY / STEP4E_LINE_LENGTH_M,
)


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def default_output_dir() -> Path:
    return EXPERIMENT_ROOT / "runs" / f"bridge_{now_stamp()}"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def csv_value(value: Any) -> str:
    if value == "":
        return ""
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.9g}"
    if isinstance(value, int):
        return str(value)
    return str(value)


def vec_norm(values: list[float]) -> float:
    return math.sqrt(sum(value * value for value in values))


def clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


def dot3(a: tuple[float, float, float] | list[float], b: tuple[float, float, float] | list[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross3(
    a: tuple[float, float, float] | list[float],
    b: tuple[float, float, float] | list[float],
) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def norm3(values: tuple[float, float, float] | list[float]) -> float:
    return math.sqrt(dot3(values, values))


def normalize3(
    values: tuple[float, float, float] | list[float],
    fallback: tuple[float, float, float] = (0.0, 0.0, 1.0),
) -> tuple[float, float, float]:
    length = norm3(values)
    if length < 1e-9:
        return fallback
    return (values[0] / length, values[1] / length, values[2] / length)


def mat_vec3(matrix: list[list[float]], vector: tuple[float, float, float] | list[float]) -> tuple[float, float, float]:
    return (
        dot3(matrix[0], vector),
        dot3(matrix[1], vector),
        dot3(matrix[2], vector),
    )


def rotvec_to_matrix(rx: float, ry: float, rz: float) -> list[list[float]]:
    theta = math.sqrt(rx * rx + ry * ry + rz * rz)
    if theta < 1e-12:
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    kx, ky, kz = rx / theta, ry / theta, rz / theta
    c = math.cos(theta)
    s = math.sin(theta)
    v = 1.0 - c
    return [
        [c + kx * kx * v, kx * ky * v - kz * s, kx * kz * v + ky * s],
        [ky * kx * v + kz * s, c + ky * ky * v, ky * kz * v - kx * s],
        [kz * kx * v - ky * s, kz * ky * v + kx * s, c + kz * kz * v],
    ]


def kunwei_to_tcp_wrench(values_si_zeroed: list[float]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    force_t = (values_si_zeroed[0], -values_si_zeroed[1], -values_si_zeroed[2])
    torque_t = (values_si_zeroed[3], -values_si_zeroed[4], -values_si_zeroed[5])
    return force_t, torque_t


def step4e_zero_values() -> dict[str, float]:
    return {name: 0.0 for name in STEP4E_INPUT_NAMES}


class Step4EState:
    def __init__(self) -> None:
        self.integral_error_n_s = 0.0
        self.normal_velocity_m_s = 0.0


def compute_step4e_values(
    args: argparse.Namespace,
    latest_zeroed: list[float],
    latest_output: dict[str, Any] | None,
    sensor_ok: float,
    state: Step4EState,
    dt_s: float,
) -> dict[str, float]:
    values = step4e_zero_values()
    if args.step4e_mode == "off" or latest_output is None:
        return values

    pose = latest_output.get("actual_TCP_pose")
    speed = latest_output.get("actual_TCP_speed")
    if not pose or len(pose) < 6:
        values["step4e_controller_state"] = 2.0
        return values

    force_t, torque_t = kunwei_to_tcp_wrench(latest_zeroed)
    force_abs = norm3(force_t)
    contact_offset_x = ""
    contact_offset_y = ""
    if abs(force_t[2]) > args.step4e_contact_offset_min_fz_n:
        contact_offset_x = -torque_t[1] / force_t[2]
        contact_offset_y = torque_t[0] / force_t[2]

    rotation = rotvec_to_matrix(float(pose[3]), float(pose[4]), float(pose[5]))
    force_b = mat_vec3(rotation, force_t)
    n_reaction_b = normalize3(force_b)
    tcp_z_axis_b = (rotation[0][2], rotation[1][2], rotation[2][2])
    orientation_axis = cross3(tcp_z_axis_b, n_reaction_b)
    orientation_error = math.asin(clamp(norm3(orientation_axis), -1.0, 1.0))
    orientation_cmd = tuple(args.step4e_orientation_gain * value for value in orientation_axis)
    orientation_norm = norm3(orientation_cmd)
    if orientation_norm > args.step4e_angular_limit_rad_s:
        scale = args.step4e_angular_limit_rad_s / orientation_norm
        orientation_cmd = tuple(value * scale for value in orientation_cmd)

    path_x = float(pose[0]) - STEP4E_START_XY[0]
    path_y = float(pose[1]) - STEP4E_START_XY[1]
    progress = clamp(path_x * STEP4E_LINE_UNIT_XY[0] + path_y * STEP4E_LINE_UNIT_XY[1], 0.0, STEP4E_LINE_LENGTH_M)
    desired_x = STEP4E_START_XY[0] + progress * STEP4E_LINE_UNIT_XY[0]
    desired_y = STEP4E_START_XY[1] + progress * STEP4E_LINE_UNIT_XY[1]
    path_error = (desired_x - float(pose[0]), desired_y - float(pose[1]), 0.0)
    tangent_speed = args.step4e_line_speed_m_s if args.step4e_mode == "line" else 0.0
    base_motion = (
        tangent_speed * STEP4E_LINE_UNIT_XY[0] + args.step4e_path_p_gain * path_error[0],
        tangent_speed * STEP4E_LINE_UNIT_XY[1] + args.step4e_path_p_gain * path_error[1],
        0.0,
    )
    normal_projection = dot3(base_motion, n_reaction_b)
    motion_cmd = tuple(base_motion[idx] - normal_projection * n_reaction_b[idx] for idx in range(3))
    motion_norm = norm3(motion_cmd)
    if motion_norm > args.step4e_motion_limit_m_s:
        scale = args.step4e_motion_limit_m_s / motion_norm
        motion_cmd = tuple(value * scale for value in motion_cmd)

    force_error = args.target_force_n - force_abs
    if sensor_ok > 0.5 and force_abs >= args.step4e_min_force_for_control_n:
        state.integral_error_n_s = clamp(
            state.integral_error_n_s + force_error * dt_s,
            -args.step4e_integral_limit_n_s,
            args.step4e_integral_limit_n_s,
        )
        accel_like = (
            args.step4e_force_p_gain * force_error
            + args.step4e_force_i_gain * state.integral_error_n_s
            - args.step4e_force_damping * state.normal_velocity_m_s
        )
        state.normal_velocity_m_s = clamp(
            state.normal_velocity_m_s + accel_like * dt_s,
            -args.step4e_normal_velocity_limit_m_s,
            args.step4e_normal_velocity_limit_m_s,
        )
        force_cmd = tuple(
            -args.step4e_normal_command_sign * n_reaction_b[idx] * state.normal_velocity_m_s
            for idx in range(3)
        )
        cmd = tuple(motion_cmd[idx] + force_cmd[idx] for idx in range(3))
        cmd_norm = norm3(cmd)
        if cmd_norm > args.step4e_total_linear_limit_m_s:
            scale = args.step4e_total_linear_limit_m_s / cmd_norm
            cmd = tuple(value * scale for value in cmd)
        values.update(
            {
                "step4e_cmd_vx_m_s": cmd[0],
                "step4e_cmd_vy_m_s": cmd[1],
                "step4e_cmd_vz_m_s": cmd[2],
                "step4e_cmd_wx_rad_s": orientation_cmd[0],
                "step4e_cmd_wy_rad_s": orientation_cmd[1],
                "step4e_cmd_wz_rad_s": 0.0,
                "step4e_cmd_valid": 0.0 if args.step4e_mode == "preview" else 1.0,
                "step4e_progress_m": progress,
                "step4e_force_error_n": force_error,
                "step4e_orientation_error_rad": orientation_error,
                "step4e_controller_state": {"preview": 10.0, "hold": 20.0, "line": 30.0}[args.step4e_mode],
            }
        )
    else:
        state.integral_error_n_s = 0.0
        state.normal_velocity_m_s = 0.0
        values.update(
            {
                "step4e_progress_m": progress,
                "step4e_force_error_n": force_error,
                "step4e_orientation_error_rad": orientation_error,
                "step4e_controller_state": 1.0,
            }
        )

    values["_step4e_force_t_x"] = force_t[0]
    values["_step4e_force_t_y"] = force_t[1]
    values["_step4e_force_t_z"] = force_t[2]
    values["_step4e_force_b_x"] = force_b[0]
    values["_step4e_force_b_y"] = force_b[1]
    values["_step4e_force_b_z"] = force_b[2]
    values["_step4e_normal_b_x"] = n_reaction_b[0]
    values["_step4e_normal_b_y"] = n_reaction_b[1]
    values["_step4e_normal_b_z"] = n_reaction_b[2]
    values["_step4e_contact_offset_x_m"] = contact_offset_x
    values["_step4e_contact_offset_y_m"] = contact_offset_y
    if speed and len(speed) >= 6:
        values["_step4e_actual_speed_norm_m_s"] = norm3([float(speed[0]), float(speed[1]), float(speed[2])])
    return values


def stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"samples": 0}
    return {
        "samples": len(values),
        "mean": statistics.fmean(values),
        "std": statistics.stdev(values) if len(values) >= 2 else 0.0,
        "min": min(values),
        "max": max(values),
    }


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * pct
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[int(index)]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def interval_stats(times: list[float]) -> dict[str, Any]:
    if len(times) < 2:
        return {"samples": len(times), "rate_hz": 0.0}
    intervals = [b - a for a, b in zip(times, times[1:]) if b > a]
    elapsed = times[-1] - times[0]
    return {
        "samples": len(times),
        "elapsed_s": elapsed,
        "rate_hz": (len(times) - 1) / elapsed if elapsed > 0 else 0.0,
        "dt_mean_s": statistics.fmean(intervals) if intervals else None,
        "dt_p95_s": percentile(intervals, 0.95),
        "dt_p99_s": percentile(intervals, 0.99),
        "dt_max_s": max(intervals) if intervals else None,
    }


class RTDEBridgeClient(RTDEClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._started = False

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.pause(best_effort=True)
        super().__exit__(exc_type, exc, tb)

    def setup_inputs(self, fields: list[str]) -> tuple[int, list[str]]:
        self._send_packet("I", ",".join(fields).encode())
        ptype, data = self._recv_packet()
        if ptype != ord("I"):
            raise RuntimeError(f"unexpected RTDE input setup response type {ptype}")
        recipe_id = data[0]
        type_names = data[1:].decode("ascii", errors="replace").split(",")
        if recipe_id == 0 or any(name == "NOT_FOUND" for name in type_names):
            raise RuntimeError(f"invalid RTDE input recipe: id={recipe_id} types={type_names}")
        return recipe_id, type_names

    def send_input_sample(self, recipe_id: int, type_names: list[str], values: list[Any]) -> None:
        if len(type_names) != len(values):
            raise ValueError("RTDE input type/value length mismatch")
        payload = bytearray([recipe_id])
        for type_name, value in zip(type_names, values):
            payload.extend(pack_rtde_value(type_name, value))
        self._send_packet("U", bytes(payload))

    def start(self) -> None:
        super().start()
        self._started = True

    def pause(self, best_effort: bool = False) -> bool:
        if self.sock is None or not self._started:
            return False
        try:
            self._send_packet("P")
            ptype, payload = self._recv_packet()
            ok = ptype == ord("P") and payload == b"\x01"
            if not ok and not best_effort:
                raise RuntimeError(f"RTDE pause failed: type={ptype} payload={payload!r}")
            self._started = False
            return ok
        except (OSError, RuntimeError, socket.timeout):
            if not best_effort:
                raise
            return False

    def recv_available_sample(
        self, recipe_id: int, type_names: list[str], timeout_s: float = 0.0
    ) -> dict[str, Any] | None:
        assert self.sock is not None
        ready, _, _ = select.select([self.sock], [], [], timeout_s)
        if not ready:
            return None
        ptype, payload = self._recv_packet()
        if ptype != ord("U") or not payload or payload[0] != recipe_id:
            return None
        cursor = 1
        values: list[Any] = []
        for type_name in type_names:
            fmt = rtde_struct_format(type_name)
            width = struct.calcsize("!" + fmt)
            unpacked = struct.unpack("!" + fmt, payload[cursor : cursor + width])
            cursor += width
            values.append(unpacked[0] if len(unpacked) == 1 else list(unpacked))
        return {field: value for field, value in zip(OUTPUT_FIELDS, values)}


def open_rtde_bridge(args: argparse.Namespace) -> tuple[RTDEBridgeClient, int, list[str], int, list[str]]:
    rtde = RTDEBridgeClient(args.robot_host, timeout=args.connect_timeout_s)
    rtde.__enter__()
    try:
        rtde.negotiate()
        output_recipe, output_types = rtde.setup_outputs(args.rtde_hz, OUTPUT_FIELDS)
        input_recipe, input_types = rtde.setup_inputs(INPUT_FIELDS)
        rtde.start()
    except Exception:
        rtde.__exit__(None, None, None)
        raise
    return rtde, input_recipe, input_types, output_recipe, output_types


def close_rtde_bridge(rtde: RTDEBridgeClient | None) -> None:
    if rtde is None:
        return
    try:
        rtde.__exit__(None, None, None)
    except (OSError, RuntimeError, socket.timeout):
        pass


def rtde_error_name(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def rtde_struct_format(type_name: str) -> str:
    mapping = {
        "DOUBLE": "d",
        "VECTOR3D": "3d",
        "VECTOR6D": "6d",
        "UINT32": "I",
        "UINT64": "Q",
        "INT32": "i",
        "BOOL": "?",
    }
    if type_name not in mapping:
        raise RuntimeError(f"unsupported RTDE type: {type_name}")
    return mapping[type_name]


def pack_rtde_value(type_name: str, value: Any) -> bytes:
    fmt = rtde_struct_format(type_name)
    if fmt.endswith("d") and fmt != "d":
        return struct.pack("!" + fmt, *value)
    return struct.pack("!" + fmt, value)


def zeroed_si(raw_values: tuple[float, ...], baseline: list[float]) -> list[float]:
    si = [
        raw_values[0] * FORCE_KG_TO_N,
        raw_values[1] * FORCE_KG_TO_N,
        raw_values[2] * FORCE_KG_TO_N,
        raw_values[3] * MOMENT_KG_M_TO_NM,
        raw_values[4] * MOMENT_KG_M_TO_NM,
        raw_values[5] * MOMENT_KG_M_TO_NM,
    ]
    return [value - offset for value, offset in zip(si, baseline)]


def normal_component(values_si_zeroed: list[float], axis: str, sign: float) -> float:
    index = {"fx": 0, "fy": 1, "fz": 2}[axis]
    return sign * values_si_zeroed[index]


def flatten_output(output: dict[str, Any] | None) -> dict[str, Any]:
    row: dict[str, Any] = {}
    if not output:
        return row
    for key, value in output.items():
        if isinstance(value, list):
            for idx, item in enumerate(value):
                row[f"ur_{key}_{idx}"] = item
        else:
            row[f"ur_{key}"] = value
    return row


def guard_stop_reason(args: argparse.Namespace, bridge_values: dict[str, float]) -> str | None:
    if abs(bridge_values["normal_force_n"]) > args.max_normal_force_n:
        return "normal_force_guard"
    if bridge_values["force_norm_n"] > args.max_force_norm_n:
        return "force_norm_guard"
    if bridge_values["torque_norm_nm"] > args.max_torque_norm_nm:
        return "torque_norm_guard"
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--duration-s", type=float, default=30.0)
    parser.add_argument("--baseline-s", type=float, default=5.0)
    parser.add_argument("--rtde-hz", type=float, default=125.0)
    parser.add_argument("--target-force-n", type=float, default=3.0)
    parser.add_argument("--normal-axis", choices=("fx", "fy", "fz"), default="fz")
    parser.add_argument("--normal-sign", type=float, choices=(-1.0, 1.0), default=1.0)
    parser.add_argument("--output-dir", type=Path, default=default_output_dir())
    parser.add_argument("--connect-timeout-s", type=float, default=3.0)
    parser.add_argument("--socket-timeout-s", type=float, default=0.01)
    parser.add_argument("--allow-kunwei-stream-command", action="store_true")
    parser.add_argument("--no-start-command", action="store_true")
    parser.add_argument("--no-stop-command", action="store_true")
    parser.add_argument("--write-rtde-inputs", action="store_true")
    parser.add_argument("--skip-dashboard-preflight", action="store_true")
    parser.add_argument("--max-normal-force-n", type=float, default=12.0)
    parser.add_argument("--max-force-norm-n", type=float, default=50.0)
    parser.add_argument("--max-torque-norm-nm", type=float, default=0.6)
    parser.add_argument("--sensor-stale-s", type=float, default=0.08)
    parser.add_argument("--rezero-s", type=float, default=1.0)
    parser.add_argument("--step4e-mode", choices=("off", "preview", "hold", "line"), default="off")
    parser.add_argument("--step4e-line-speed-m-s", type=float, default=0.003)
    parser.add_argument("--step4e-path-p-gain", type=float, default=1.5)
    parser.add_argument("--step4e-motion-limit-m-s", type=float, default=0.004)
    parser.add_argument("--step4e-total-linear-limit-m-s", type=float, default=0.006)
    parser.add_argument("--step4e-normal-velocity-limit-m-s", type=float, default=0.003)
    parser.add_argument("--step4e-force-p-gain", type=float, default=0.0007)
    parser.add_argument("--step4e-force-i-gain", type=float, default=0.00008)
    parser.add_argument("--step4e-force-damping", type=float, default=0.35)
    parser.add_argument("--step4e-normal-command-sign", type=float, choices=(-1.0, 1.0), default=1.0)
    parser.add_argument("--step4e-integral-limit-n-s", type=float, default=10.0)
    parser.add_argument("--step4e-min-force-for-control-n", type=float, default=1.0)
    parser.add_argument("--step4e-orientation-gain", type=float, default=0.20)
    parser.add_argument("--step4e-angular-limit-rad-s", type=float, default=0.015)
    parser.add_argument("--step4e-contact-offset-min-fz-n", type=float, default=1.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.duration_s <= 0 or args.baseline_s < 0 or args.rtde_hz <= 0:
        raise SystemExit("duration, baseline, and RTDE rate must be positive")
    if not args.no_start_command and not args.allow_kunwei_stream_command:
        raise SystemExit("Refusing to send Kunwei stream command without --allow-kunwei-stream-command")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    sensor_csv_path = args.output_dir / "kunwei_sensor_1khz.csv"
    rtde_hz_label = f"{args.rtde_hz:g}".replace(".", "p")
    bridge_csv_path = args.output_dir / f"bridge_rtde_{rtde_hz_label}hz.csv"
    raw_path = args.output_dir / "raw_frames.bin"
    metadata_path = args.output_dir / "metadata.json"
    summary_path = args.output_dir / "summary.json"

    dashboard: dict[str, str] | None = None
    if not args.skip_dashboard_preflight:
        dashboard = dashboard_exchange(
            args.robot_host,
            ["is in remote control", "safetymode", "robotmode", "running", "programState"],
        )
        if "NORMAL" not in dashboard.get("safetymode", ""):
            raise SystemExit(f"Dashboard safety not NORMAL: {dashboard}")

    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "args": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "dashboard_preflight": dashboard,
        "safety_boundary": [
            "no URScript upload or program start",
            "no robot motion command from Python",
            "no UR TCP/payload writes",
            "no zero_ftsensor",
            "no Kunwei zero/tare/config write",
        ],
        "register_map": dict(zip(INPUT_FIELDS, INPUT_NAMES)),
        "step4e_path": {
            "type": "line_from_two_tcp_points",
            "start_xy_m": STEP4E_START_XY,
            "end_xy_m": STEP4E_END_XY,
            "line_length_m": STEP4E_LINE_LENGTH_M,
            "line_unit_xy": STEP4E_LINE_UNIT_XY,
            "kunwei_to_tcp": "F_T=[Fx_K,-Fy_K,-Fz_K], M_T=[Mx_K,-My_K,-Mz_K]",
            "tcp_contact_length_m": 0.1221,
        },
    }
    write_json(metadata_path, metadata)

    sock: socket.socket | None = None
    rtde: RTDEBridgeClient | None = None
    rtde_input_recipe = 0
    rtde_input_types: list[str] = []
    rtde_output_recipe = 0
    rtde_output_types: list[str] = []
    start_mono = time.monotonic()
    baseline_raw_si: list[list[float]] = []
    baseline = [0.0] * 6
    baseline_ready = args.baseline_s == 0
    baseline_start_mono = start_mono
    baseline_epoch = 0
    latest_zeroed = [0.0] * 6
    latest_frame_time: float | None = None
    latest_output: dict[str, Any] | None = None
    last_zero_request: float | None = None
    zero_request_epsilon = 1e-6
    heartbeat = 0.0
    stop_request = 0.0
    stop_reason = "duration"
    guard_reason: str | None = None
    parse_errors = 0
    dropped_sync_bytes = 0
    samples = 0
    bridge_writes = 0
    bridge_write_times: list[float] = []
    rtde_output_times: list[float] = []
    echo_transition_times: list[float] = []
    rtde_reconnect_events: list[dict[str, Any]] = []
    next_rtde_reconnect_mono = start_mono
    last_echo_heartbeat: float | None = None
    normals: list[float] = []
    force_norms: list[float] = []
    torque_norms: list[float] = []
    zero_events: list[dict[str, Any]] = []
    buffer = bytearray()
    step4e_state = Step4EState()

    next_write = start_mono
    write_period = 1.0 / args.rtde_hz

    try:
        sock = socket.create_connection((args.sensor_ip, args.sensor_port), timeout=args.connect_timeout_s)
        sock.settimeout(args.socket_timeout_s)
        if not args.no_start_command:
            sock.sendall(START_STREAM)

        if args.write_rtde_inputs:
            rtde, rtde_input_recipe, rtde_input_types, rtde_output_recipe, rtde_output_types = open_rtde_bridge(args)

        sensor_fields = [
            "sample_index",
            "t_wall_ns",
            "t_monotonic_s",
            *KUNWEI_RAW_FIELDS,
            "fx_n_zeroed",
            "fy_n_zeroed",
            "fz_n_zeroed",
            "mx_nm_zeroed",
            "my_nm_zeroed",
            "mz_nm_zeroed",
            "normal_force_n",
            "force_norm_n",
            "torque_norm_nm",
            "frame_hex",
        ]
        bridge_fields = [
            "write_index",
            "t_wall_ns",
            "t_monotonic_s",
            "sensor_age_s",
            *INPUT_NAMES,
            "guard_reason",
            "baseline_ready",
            "baseline_epoch",
            "last_zero_request",
            "rtde_connected",
            "rtde_reconnects",
        ]
        step4e_diag_fields = [
            "_step4e_force_t_x",
            "_step4e_force_t_y",
            "_step4e_force_t_z",
            "_step4e_force_b_x",
            "_step4e_force_b_y",
            "_step4e_force_b_z",
            "_step4e_normal_b_x",
            "_step4e_normal_b_y",
            "_step4e_normal_b_z",
            "_step4e_contact_offset_x_m",
            "_step4e_contact_offset_y_m",
            "_step4e_actual_speed_norm_m_s",
        ]
        bridge_output_fields = [f"ur_{field}_{idx}" for field in ["actual_TCP_pose", "actual_TCP_speed"] for idx in range(6)]
        bridge_output_fields += ["ur_runtime_state", "ur_robot_mode", "ur_safety_mode", "ur_speed_scaling"]
        bridge_output_fields += [f"ur_output_double_register_{idx}" for idx in range(24, 48)]

        with (
            sensor_csv_path.open("w", newline="", encoding="utf-8") as sensor_handle,
            bridge_csv_path.open("w", newline="", encoding="utf-8") as bridge_handle,
            raw_path.open("wb") as raw_handle,
        ):
            sensor_writer = csv.DictWriter(sensor_handle, fieldnames=sensor_fields)
            bridge_writer = csv.DictWriter(
                bridge_handle,
                fieldnames=bridge_fields + step4e_diag_fields + bridge_output_fields,
            )
            sensor_writer.writeheader()
            bridge_writer.writeheader()

            while True:
                now = time.monotonic()
                if now - start_mono >= args.duration_s:
                    stop_reason = "duration"
                    break

                try:
                    chunk = sock.recv(8192)
                except (BlockingIOError, socket.timeout):
                    chunk = b""
                if chunk:
                    buffer.extend(chunk)
                    frames, dropped = pop_frames(buffer, 0x48 if not args.no_start_command else None)
                    dropped_sync_bytes += dropped
                    for frame in frames:
                        try:
                            raw_values = parse_frame(frame)
                        except ValueError:
                            parse_errors += 1
                            continue
                        raw_handle.write(frame)
                        samples += 1
                        latest_frame_time = time.monotonic()
                        raw_si = [
                            raw_values[0] * FORCE_KG_TO_N,
                            raw_values[1] * FORCE_KG_TO_N,
                            raw_values[2] * FORCE_KG_TO_N,
                            raw_values[3] * MOMENT_KG_M_TO_NM,
                            raw_values[4] * MOMENT_KG_M_TO_NM,
                            raw_values[5] * MOMENT_KG_M_TO_NM,
                        ]
                        if not baseline_ready:
                            baseline_raw_si.append(raw_si)
                            target_baseline_s = args.baseline_s if baseline_epoch == 0 else args.rezero_s
                            if latest_frame_time - baseline_start_mono >= target_baseline_s:
                                baseline = [statistics.fmean(axis) for axis in zip(*baseline_raw_si)]
                                baseline_ready = True
                                zero_events.append(
                                    {
                                        "baseline_epoch": baseline_epoch,
                                        "completed_at_sample": samples,
                                        "completed_at_monotonic_s": latest_frame_time,
                                        "samples": len(baseline_raw_si),
                                        "duration_s": latest_frame_time - baseline_start_mono,
                                    }
                                )
                        latest_zeroed = [value - offset for value, offset in zip(raw_si, baseline)]
                        normal = normal_component(latest_zeroed, args.normal_axis, args.normal_sign)
                        force_norm = vec_norm(latest_zeroed[:3])
                        torque_norm = vec_norm(latest_zeroed[3:])
                        normals.append(normal)
                        force_norms.append(force_norm)
                        torque_norms.append(torque_norm)
                        sensor_writer.writerow(
                            {
                                "sample_index": samples,
                                "t_wall_ns": time.time_ns(),
                                "t_monotonic_s": f"{latest_frame_time:.9f}",
                                **{field: f"{value:.9g}" for field, value in zip(KUNWEI_RAW_FIELDS, raw_values)},
                                "fx_n_zeroed": f"{latest_zeroed[0]:.9g}",
                                "fy_n_zeroed": f"{latest_zeroed[1]:.9g}",
                                "fz_n_zeroed": f"{latest_zeroed[2]:.9g}",
                                "mx_nm_zeroed": f"{latest_zeroed[3]:.9g}",
                                "my_nm_zeroed": f"{latest_zeroed[4]:.9g}",
                                "mz_nm_zeroed": f"{latest_zeroed[5]:.9g}",
                                "normal_force_n": f"{normal:.9g}",
                                "force_norm_n": f"{force_norm:.9g}",
                                "torque_norm_nm": f"{torque_norm:.9g}",
                                "frame_hex": frame.hex(),
                            }
                        )
                elif sock.fileno() < 0:
                    stop_reason = "socket_closed"
                    break

                if args.write_rtde_inputs and rtde is None and now >= next_rtde_reconnect_mono:
                    try:
                        rtde, rtde_input_recipe, rtde_input_types, rtde_output_recipe, rtde_output_types = open_rtde_bridge(args)
                        rtde_reconnect_events.append(
                            {
                                "event": "reconnected",
                                "at_monotonic_s": now,
                                "count": len(rtde_reconnect_events) + 1,
                            }
                        )
                    except (OSError, RuntimeError, socket.timeout) as exc:
                        rtde_reconnect_events.append(
                            {
                                "event": "reconnect_failed",
                                "at_monotonic_s": now,
                                "error": rtde_error_name(exc),
                                "count": len(rtde_reconnect_events) + 1,
                            }
                        )
                        close_rtde_bridge(rtde)
                        rtde = None
                        next_rtde_reconnect_mono = now + 0.05

                if rtde is not None:
                    try:
                        sample = rtde.recv_available_sample(rtde_output_recipe, rtde_output_types)
                    except (OSError, RuntimeError, socket.timeout) as exc:
                        rtde_reconnect_events.append(
                            {
                                "event": "recv_failed",
                                "at_monotonic_s": now,
                                "error": rtde_error_name(exc),
                                "count": len(rtde_reconnect_events) + 1,
                            }
                        )
                        close_rtde_bridge(rtde)
                        rtde = None
                        next_rtde_reconnect_mono = now + 0.05
                        sample = None
                    if sample is not None:
                        latest_output = sample
                        rtde_output_time = time.monotonic()
                        rtde_output_times.append(rtde_output_time)
                        echo = sample.get("output_double_register_26")
                        if echo is not None:
                            echo_float = float(echo)
                            if last_echo_heartbeat is None or echo_float != last_echo_heartbeat:
                                echo_transition_times.append(rtde_output_time)
                                last_echo_heartbeat = echo_float
                        zero_request = float(sample.get("output_double_register_34", 0.0))
                        if last_zero_request is None:
                            last_zero_request = zero_request
                        elif abs(zero_request - last_zero_request) > zero_request_epsilon:
                            previous_zero_request = last_zero_request
                            last_zero_request = zero_request
                            if (
                                args.step4e_mode in {"hold", "line"}
                                and zero_request > previous_zero_request
                                and zero_request > 0.5
                            ):
                                baseline_epoch += 1
                                baseline_ready = False
                                baseline_raw_si = []
                                baseline_start_mono = time.monotonic()
                                zero_events.append(
                                    {
                                        "baseline_epoch": baseline_epoch,
                                        "requested_at_monotonic_s": baseline_start_mono,
                                        "zero_request": zero_request,
                                        "previous_zero_request": previous_zero_request,
                                    }
                                )

                now = time.monotonic()
                if now >= next_write:
                    sensor_age = math.inf if latest_frame_time is None else now - latest_frame_time
                    sensor_ok = 1.0 if baseline_ready and sensor_age <= args.sensor_stale_s and parse_errors == 0 else 0.0
                    bridge_values = {
                        "normal_force_n": normal_component(latest_zeroed, args.normal_axis, args.normal_sign),
                        "force_norm_n": vec_norm(latest_zeroed[:3]),
                        "heartbeat": heartbeat,
                        "sensor_ok": sensor_ok,
                        "stop_request": stop_request,
                        "target_force_n": args.target_force_n,
                        "torque_norm_nm": vec_norm(latest_zeroed[3:]),
                        "fx_n_zeroed": latest_zeroed[0],
                        "fy_n_zeroed": latest_zeroed[1],
                        "fz_n_zeroed": latest_zeroed[2],
                        "mx_nm_zeroed": latest_zeroed[3],
                        "my_nm_zeroed": latest_zeroed[4],
                        "mz_nm_zeroed": latest_zeroed[5],
                    }
                    step4e_values = compute_step4e_values(
                        args,
                        latest_zeroed,
                        latest_output,
                        sensor_ok,
                        step4e_state,
                        write_period,
                    )
                    for name in STEP4E_INPUT_NAMES:
                        bridge_values[name] = float(step4e_values.get(name, 0.0))
                    if sensor_ok:
                        guard_reason = guard_stop_reason(args, bridge_values)
                        if guard_reason is not None:
                            bridge_values["stop_request"] = 1.0
                            stop_request = 1.0
                            stop_reason = guard_reason
                    rtde_connected = rtde is not None
                    if rtde is not None:
                        try:
                            rtde.send_input_sample(
                                rtde_input_recipe,
                                rtde_input_types,
                                [bridge_values[name] for name in INPUT_NAMES],
                            )
                        except (OSError, RuntimeError, socket.timeout) as exc:
                            rtde_reconnect_events.append(
                                {
                                    "event": "send_failed",
                                    "at_monotonic_s": now,
                                    "error": rtde_error_name(exc),
                                    "count": len(rtde_reconnect_events) + 1,
                                }
                            )
                            close_rtde_bridge(rtde)
                            rtde = None
                            rtde_connected = False
                            next_rtde_reconnect_mono = now + 0.05
                    row = {
                        "write_index": bridge_writes + 1,
                        "t_wall_ns": time.time_ns(),
                        "t_monotonic_s": f"{now:.9f}",
                        "sensor_age_s": sensor_age if math.isfinite(sensor_age) else "",
                        **{key: csv_value(value) for key, value in bridge_values.items()},
                        **{key: csv_value(step4e_values.get(key, "")) for key in step4e_diag_fields},
                        "guard_reason": guard_reason or "",
                        "baseline_ready": int(baseline_ready),
                        "baseline_epoch": baseline_epoch,
                        "last_zero_request": "" if last_zero_request is None else last_zero_request,
                        "rtde_connected": int(rtde_connected),
                        "rtde_reconnects": len(rtde_reconnect_events),
                    }
                    row.update(flatten_output(latest_output))
                    bridge_writer.writerow(row)
                    bridge_writes += 1
                    bridge_write_times.append(now)
                    heartbeat += 1.0
                    next_write += write_period
                    if guard_reason is not None:
                        break
    finally:
        if sock is not None and not args.no_stop_command:
            try:
                sock.sendall(STOP_STREAM)
            except OSError:
                pass
        if sock is not None:
            sock.close()
        close_rtde_bridge(rtde)

    summary = {
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "stop_reason": stop_reason,
        "samples": samples,
        "bridge_writes": bridge_writes,
        "bridge_write_timing": interval_stats(bridge_write_times),
        "rtde_output_timing": interval_stats(rtde_output_times),
        "echo_heartbeat_transitions": interval_stats(echo_transition_times),
        "last_echo_heartbeat": last_echo_heartbeat,
        "parse_errors": parse_errors,
        "dropped_sync_bytes": dropped_sync_bytes,
        "baseline_ready": baseline_ready,
        "baseline_samples": len(baseline_raw_si),
        "baseline_epoch": baseline_epoch,
        "last_zero_request": last_zero_request,
        "zero_events": zero_events,
        "rtde_reconnect_events": rtde_reconnect_events,
        "rtde_reconnect_event_count": len(rtde_reconnect_events),
        "baseline_si_offsets": dict(zip(["fx_n", "fy_n", "fz_n", "mx_nm", "my_nm", "mz_nm"], baseline)),
        "normal_force_stats_n": stats(normals),
        "force_norm_stats_n": stats(force_norms),
        "torque_norm_stats_nm": stats(torque_norms),
        "paths": {
            "metadata": str(metadata_path),
            "summary": str(summary_path),
            "sensor_csv": str(sensor_csv_path),
            "bridge_csv": str(bridge_csv_path),
            "raw_frames": str(raw_path),
        },
    }
    write_json(summary_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if samples > 0 and parse_errors == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
