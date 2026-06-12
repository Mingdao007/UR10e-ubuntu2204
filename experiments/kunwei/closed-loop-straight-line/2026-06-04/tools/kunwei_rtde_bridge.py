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
import os
import select
import signal
import socket
import statistics
import struct
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
STEP4F_SAFE_FRAME_PATH = EXPERIMENT_ROOT / "config" / "step4f_safe_frame.json"
KUNWEI_TOOLS = Path("/home/andy/ur10e_ros2_ws/ft_sensor/kunwei/kwr75b/tools")
UR_REALSETUP_SCRIPTS = Path("/home/andy/codex-private-skills-shared-main/skills/ur10e-realsetup/scripts")
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
from step5_table import step5_path_reference  # noqa: E402
from step5c_dls_joint_solver import JointSolverConfig, STATUS_INVALID, Step5cDlsJointSolver  # noqa: E402
from step6_eight import (  # noqa: E402
    PATH_DURATION_S as STEP6_PATH_DURATION_S,
    STEP6_SAFE_FRAME_PATH,
    STEP6_TABLE_PATH,
    eight_local,
    load_safe_frame as load_step6_safe_frame,
    step6_stage,
    transform_local as step6_transform_local,
    transform_velocity as step6_transform_velocity,
)


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
    "actual_q",
    "actual_qd",
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
STEP4E_LINE_PERP_XY = (-STEP4E_LINE_UNIT_XY[1], STEP4E_LINE_UNIT_XY[0])
STEP4E_LINE_MID_XY = (
    0.5 * (STEP4E_START_XY[0] + STEP4E_END_XY[0]),
    0.5 * (STEP4E_START_XY[1] + STEP4E_END_XY[1]),
)
STEP4FG_PATH_DURATION_S = 60.0
STEP5_CONTACT_CYCLOID_STAGE_ID = "step5_contact_cycloid_baseline_v1"
STEP5C_DRYRUN_STAGE_ID = "step5c_speedj_dryrun_v1"
STEP5C_CONTACT_STAGE_ID = "step5c_joint_rnn_cycloid_v1"
STEP6_CONTACT_EIGHT_STAGE_ID = "step6_contact_eight_baseline_v1"
STEP6_CONTACT_EIGHT_STAGE_ID_V2 = "step6_contact_eight_baseline_v2"
_STEP5C_SOLVERS: dict[tuple[str, str, float, float], Step5cDlsJointSolver] = {}


def load_step4f_safe_frame() -> dict[str, Any]:
    fallback = {
        "basis": {
            "origin_xy_m": STEP4E_START_XY,
            "u_along_xy": STEP4E_LINE_UNIT_XY,
            "p_lateral_xy": STEP4E_LINE_PERP_XY,
            "x_minus_shift_m": 0.0,
        },
        "guard": {
            "passed": False,
            "guard_line_x_m": None,
            "path_max_x_m": None,
        },
        "policy": {
            "no_scale": True,
            "fallback": True,
        },
    }
    if not STEP4F_SAFE_FRAME_PATH.is_file():
        return fallback
    try:
        return json.loads(STEP4F_SAFE_FRAME_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


STEP4F_SAFE_FRAME = load_step4f_safe_frame()
STEP4F_ORIGIN_XY = tuple(float(v) for v in STEP4F_SAFE_FRAME["basis"]["origin_xy_m"])
STEP4F_ALONG_UNIT_XY = tuple(float(v) for v in STEP4F_SAFE_FRAME["basis"]["u_along_xy"])
STEP4F_LATERAL_UNIT_XY = tuple(float(v) for v in STEP4F_SAFE_FRAME["basis"]["p_lateral_xy"])


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


def xy_from_line_basis(anchor_xy: tuple[float, float], along_m: float, lateral_m: float) -> tuple[float, float]:
    return xy_from_basis(anchor_xy, STEP4E_LINE_UNIT_XY, STEP4E_LINE_PERP_XY, along_m, lateral_m)


def xy_from_basis(
    anchor_xy: tuple[float, float],
    u_along_xy: tuple[float, float],
    p_lateral_xy: tuple[float, float],
    along_m: float,
    lateral_m: float,
) -> tuple[float, float]:
    return (
        anchor_xy[0] + along_m * u_along_xy[0] + lateral_m * p_lateral_xy[0],
        anchor_xy[1] + along_m * u_along_xy[1] + lateral_m * p_lateral_xy[1],
    )


def step4e_path_reference(
    shape: str,
    pose_xy: tuple[float, float],
    elapsed_s: float,
) -> dict[str, Any]:
    if shape == "line":
        path_x = pose_xy[0] - STEP4E_START_XY[0]
        path_y = pose_xy[1] - STEP4E_START_XY[1]
        progress = clamp(
            path_x * STEP4E_LINE_UNIT_XY[0] + path_y * STEP4E_LINE_UNIT_XY[1],
            0.0,
            STEP4E_LINE_LENGTH_M,
        )
        desired_x, desired_y = xy_from_line_basis(STEP4E_START_XY, progress, 0.0)
        return {
            "progress": progress,
            "desired_xy": (desired_x, desired_y),
            "desired_velocity_xy": (0.0, 0.0),
            "path_error_xy": (desired_x - pose_xy[0], desired_y - pose_xy[1]),
            "path_time_s": "",
        }

    path_time_s = clamp(elapsed_s, 0.0, STEP4FG_PATH_DURATION_S)
    if shape == "cycloid":
        phase = 0.1 * path_time_s
        along_m = 0.015 * (phase - math.sin(phase))
        lateral_m = 0.015 * (1.0 - math.cos(phase))
        along_v = 0.0015 * (1.0 - math.cos(phase))
        lateral_v = 0.0015 * math.sin(phase)
        anchor_xy = STEP4F_ORIGIN_XY
        u_along_xy = STEP4F_ALONG_UNIT_XY
        p_lateral_xy = STEP4F_LATERAL_UNIT_XY
        desired_x, desired_y = xy_from_basis(anchor_xy, u_along_xy, p_lateral_xy, along_m, lateral_m)
    elif shape == "eight":
        phase = 0.1 * path_time_s
        along_m = 0.04 * math.sin(phase)
        lateral_m = 0.01 * math.sin(2.0 * phase)
        along_v = 0.004 * math.cos(phase)
        lateral_v = 0.002 * math.cos(2.0 * phase)
        anchor_xy = STEP4E_LINE_MID_XY
        u_along_xy = STEP4E_LINE_UNIT_XY
        p_lateral_xy = STEP4E_LINE_PERP_XY
        desired_x, desired_y = xy_from_basis(anchor_xy, u_along_xy, p_lateral_xy, along_m, lateral_m)
    else:
        raise ValueError(f"unknown Step4e path shape: {shape}")

    desired_vx = along_v * u_along_xy[0] + lateral_v * p_lateral_xy[0]
    desired_vy = along_v * u_along_xy[1] + lateral_v * p_lateral_xy[1]
    return {
        "progress": path_time_s,
        "desired_xy": (desired_x, desired_y),
        "desired_velocity_xy": (desired_vx, desired_vy),
        "path_error_xy": (desired_x - pose_xy[0], desired_y - pose_xy[1]),
        "path_time_s": path_time_s,
    }


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


def mat_mul3(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [
        [sum(a[row][idx] * b[idx][col] for idx in range(3)) for col in range(3)]
        for row in range(3)
    ]


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


def matrix_to_rotvec(matrix: list[list[float]]) -> tuple[float, float, float]:
    trace = matrix[0][0] + matrix[1][1] + matrix[2][2]
    theta = math.acos(clamp((trace - 1.0) * 0.5, -1.0, 1.0))
    if theta < 1e-12:
        return (0.0, 0.0, 0.0)
    if math.pi - theta < 1e-6:
        xx = max(0.0, (matrix[0][0] + 1.0) * 0.5)
        yy = max(0.0, (matrix[1][1] + 1.0) * 0.5)
        zz = max(0.0, (matrix[2][2] + 1.0) * 0.5)
        axis = [math.sqrt(xx), math.sqrt(yy), math.sqrt(zz)]
        if matrix[0][1] < 0.0:
            axis[1] = -axis[1]
        if matrix[0][2] < 0.0:
            axis[2] = -axis[2]
        axis_t = normalize3(axis, (1.0, 0.0, 0.0))
        return (axis_t[0] * theta, axis_t[1] * theta, axis_t[2] * theta)
    scale = theta / (2.0 * math.sin(theta))
    return (
        (matrix[2][1] - matrix[1][2]) * scale,
        (matrix[0][2] - matrix[2][0]) * scale,
        (matrix[1][0] - matrix[0][1]) * scale,
    )


def minimal_rotation_between(
    source: tuple[float, float, float],
    target: tuple[float, float, float],
) -> list[list[float]]:
    src = normalize3(source)
    dst = normalize3(target)
    axis = cross3(src, dst)
    axis_norm = norm3(axis)
    c = clamp(dot3(src, dst), -1.0, 1.0)
    if axis_norm < 1e-9:
        if c > 0.0:
            return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        helper = (1.0, 0.0, 0.0) if abs(src[0]) < 0.9 else (0.0, 1.0, 0.0)
        axis = normalize3(cross3(src, helper), (0.0, 0.0, 1.0))
        return rotvec_to_matrix(axis[0] * math.pi, axis[1] * math.pi, axis[2] * math.pi)
    axis = (axis[0] / axis_norm, axis[1] / axis_norm, axis[2] / axis_norm)
    theta = math.atan2(axis_norm, c)
    return rotvec_to_matrix(axis[0] * theta, axis[1] * theta, axis[2] * theta)


def angle_between_unit(
    source: tuple[float, float, float],
    target: tuple[float, float, float],
) -> float:
    src = normalize3(source)
    dst = normalize3(target)
    return math.acos(clamp(dot3(src, dst), -1.0, 1.0))


def slerp_unit(
    source: tuple[float, float, float],
    target: tuple[float, float, float],
    fraction: float,
) -> tuple[float, float, float]:
    src = normalize3(source)
    dst = normalize3(target, src)
    fraction = clamp(fraction, 0.0, 1.0)
    angle = angle_between_unit(src, dst)
    if angle < 1e-9:
        return dst
    sin_angle = math.sin(angle)
    if abs(sin_angle) < 1e-9:
        blended = tuple((1.0 - fraction) * src[idx] + fraction * dst[idx] for idx in range(3))
        return normalize3(blended, src)
    src_weight = math.sin((1.0 - fraction) * angle) / sin_angle
    dst_weight = math.sin(fraction * angle) / sin_angle
    blended = tuple(src_weight * src[idx] + dst_weight * dst[idx] for idx in range(3))
    return normalize3(blended, src)


def rotate_toward_unit(
    source: tuple[float, float, float],
    target: tuple[float, float, float],
    max_angle_rad: float,
) -> tuple[float, float, float]:
    src = normalize3(source)
    dst = normalize3(target, src)
    angle = angle_between_unit(src, dst)
    if angle <= max(0.0, max_angle_rad):
        return dst
    if max_angle_rad <= 0.0:
        return src
    return slerp_unit(src, dst, max_angle_rad / angle)


def live_normal_candidate(
    force_b: tuple[float, float, float],
    *,
    friction_projection: bool,
) -> tuple[tuple[float, float, float], tuple[float, float, float], float]:
    raw_normal_b = normalize3(force_b)
    candidate_force_b = force_b
    if friction_projection:
        tangent_b = (STEP4E_LINE_UNIT_XY[0], STEP4E_LINE_UNIT_XY[1], 0.0)
        tangent_load = dot3(force_b, tangent_b)
        candidate_force_b = tuple(force_b[idx] - tangent_load * tangent_b[idx] for idx in range(3))
    candidate_force_n = norm3(candidate_force_b)
    candidate_b = normalize3(candidate_force_b, raw_normal_b)
    return raw_normal_b, candidate_b, candidate_force_n


def v31_filtered_live_normal(
    filtered_current_b: tuple[float, float, float],
    live_candidate_b: tuple[float, float, float],
    live_candidate_force_n: float,
    *,
    sensor_ok: float,
    alpha: float = 0.35,
    min_force_n: float = 2.0,
) -> tuple[tuple[float, float, float], str]:
    if sensor_ok <= 0.5:
        return filtered_current_b, "hold_stale"
    if live_candidate_force_n < min_force_n:
        return filtered_current_b, "hold_low_force"
    if dot3(filtered_current_b, live_candidate_b) < 0.0:
        return filtered_current_b, "hold_reverse"
    alpha = clamp(alpha, 0.0, 1.0)
    return (
        normalize3(
            tuple(
                (1.0 - alpha) * filtered_current_b[idx] + alpha * live_candidate_b[idx]
                for idx in range(3)
            ),
            filtered_current_b,
        ),
        "filtered_live_alpha",
    )


def step5_contact_path_reference(
    pose_xy: tuple[float, float],
    elapsed_s: float,
    stage_id: str = STEP5_CONTACT_CYCLOID_STAGE_ID,
) -> dict[str, Any]:
    return step5_path_reference(stage_id, pose_xy, elapsed_s)


def step5c_solver(args: argparse.Namespace) -> Step5cDlsJointSolver:
    key = (
        str(args.step5c_joint_model),
        args.step5c_joint_site,
        float(args.step5c_qdot_limit_rad_s),
        float(args.step5c_joint_damping),
    )
    solver = _STEP5C_SOLVERS.get(key)
    if solver is None:
        solver = Step5cDlsJointSolver(
            JointSolverConfig(
                model_path=Path(args.step5c_joint_model),
                site_name=args.step5c_joint_site,
                qdot_limit_rad_s=args.step5c_qdot_limit_rad_s,
                damping=args.step5c_joint_damping,
            )
        )
        _STEP5C_SOLVERS[key] = solver
    return solver


def step6_contact_path_reference(
    pose_xy: tuple[float, float],
    elapsed_s: float,
    stage_id: str = STEP6_CONTACT_EIGHT_STAGE_ID,
) -> dict[str, Any]:
    stage = step6_stage(stage_id)
    if stage.get("shape") != "eight":
        raise ValueError(f"unsupported Step6 shape: {stage.get('shape')}")
    frame = load_step6_safe_frame()
    local = eight_local(elapsed_s)
    desired_xy = step6_transform_local(frame, local["local_x_m"], local["local_y_m"])
    desired_vxy = step6_transform_velocity(frame, local["local_vx_m_s"], local["local_vy_m_s"])
    return {
        "stage_id": stage_id,
        "progress": local["t_s"],
        "path_time_s": local["t_s"],
        "phase_rad": local["phase_rad"],
        "desired_xy": desired_xy,
        "desired_velocity_xy": desired_vxy,
        "path_error_xy": (desired_xy[0] - pose_xy[0], desired_xy[1] - pose_xy[1]),
        "local": local,
    }


def synthetic_axis_iso_normal(stage: float, tilt_rad: float) -> tuple[float, float, float] | None:
    component = math.sin(tilt_rad) / math.sqrt(2.0)
    z = -math.cos(tilt_rad)
    if abs(stage - 25.21) < 0.03:
        return normalize3((component, component, z))
    if abs(stage - 25.22) < 0.03:
        return normalize3((component, -component, z))
    if abs(stage - 25.23) < 0.03:
        return normalize3((-component, component, z))
    if abs(stage - 25.24) < 0.03:
        return normalize3((-component, -component, z))
    return None


def kunwei_to_tcp_wrench(values_si_zeroed: list[float]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    force_t = (values_si_zeroed[0], values_si_zeroed[1], values_si_zeroed[2])
    torque_t = (values_si_zeroed[3], values_si_zeroed[4], values_si_zeroed[5])
    return force_t, torque_t


def step4e_zero_values() -> dict[str, float]:
    return {name: 0.0 for name in STEP4E_INPUT_NAMES}


class Step4EState:
    def __init__(self) -> None:
        self.integral_error_n_s = 0.0
        self.normal_velocity_m_s = 0.0
        self.latched_normal_b: tuple[float, float, float] | None = None
        self.filtered_normal_b: tuple[float, float, float] | None = None
        self.latched_normal_locked = False
        self.normal_acquired = False
        self.line_stage_s = 0.0
        self.last_robot_stage: float | None = None

    def reset_line_contact(self) -> None:
        self.integral_error_n_s = 0.0
        self.normal_velocity_m_s = 0.0
        self.latched_normal_b = None
        self.filtered_normal_b = None
        self.latched_normal_locked = False
        self.normal_acquired = False
        self.line_stage_s = 0.0
        self.last_robot_stage = None


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
    try:
        robot_stage = float(latest_output.get("output_double_register_35", math.nan))
    except (TypeError, ValueError):
        robot_stage = math.nan
    v20_profile = args.step4e_version == "v20"
    v21_profile = args.step4e_version == "v21"
    v22_profile = args.step4e_version == "v22"
    v23_profile = args.step4e_version == "v23"
    v24_profile = args.step4e_version == "v24"
    v25_profile = args.step4e_version == "v25"
    v26_profile = args.step4e_version == "v26"
    v27_profile = args.step4e_version == "v27"
    v28_profile = args.step4e_version == "v28"
    v29_profile = args.step4e_version == "v29"
    v30_profile = args.step4e_version == "v30"
    v31_profile = args.step4e_version == "v31"
    step4f_profile = args.step4e_version == "step4f_v1"
    step4g_profile = args.step4e_version == "step4g_v1"
    step5b_profile = args.step4e_version == "step5b_v1"
    step5c_dryrun_profile = args.step4e_version == STEP5C_DRYRUN_STAGE_ID
    step5c_contact_profile = args.step4e_version == STEP5C_CONTACT_STAGE_ID
    step5c_joint_profile = step5c_dryrun_profile or step5c_contact_profile
    step6b_profile = args.step4e_version in {"step6b_v1", "step6b_v2"}
    step6_stage_id = STEP6_CONTACT_EIGHT_STAGE_ID_V2 if args.step4e_version == "step6b_v2" else STEP6_CONTACT_EIGHT_STAGE_ID
    angular_speedl_profile = (
        v23_profile
        or v24_profile
        or v25_profile
        or v26_profile
        or v27_profile
        or v28_profile
        or v29_profile
        or v30_profile
        or v31_profile
        or step4f_profile
        or step4g_profile
        or step5b_profile
        or step5c_contact_profile
        or step6b_profile
    )
    detached_profile = v20_profile or v21_profile or v22_profile or angular_speedl_profile
    axis_iso_active = args.step4e_mode == "axis_iso" and 25.18 <= robot_stage <= 25.27
    first_search_stage_active = (
        args.step4e_mode == "line"
        and angular_speedl_profile
        and (abs(robot_stage - 24.0) < 0.05 or abs(robot_stage - 24.2) < 0.05)
    )
    latch_stage_active = args.step4e_mode == "line" and detached_profile and abs(robot_stage - 25.05) < 0.03
    detach_stage_active = args.step4e_mode == "line" and detached_profile and abs(robot_stage - 25.1) < 0.03
    orient_stage_active = args.step4e_mode == "line" and (
        (detached_profile and abs(robot_stage - 25.2) < 0.05)
        or (not detached_profile and abs(robot_stage - 25.1) < 0.05)
    )
    acquire_stage_active = (
        args.step4e_mode == "line"
        and (v20_profile or v22_profile or angular_speedl_profile)
        and abs(robot_stage - 25.3) < 0.05
    )
    line_entry_gate_active = (
        v29_profile or v30_profile or v31_profile or step5b_profile or step5c_contact_profile or step6b_profile
    ) and acquire_stage_active
    line_stage_active = args.step4e_mode == "line" and abs(robot_stage - 25.0) < 0.05
    control_stage_active = (
        latch_stage_active
        or detach_stage_active
        or orient_stage_active
        or acquire_stage_active
        or line_stage_active
        or axis_iso_active
    )
    if args.step4e_mode not in {"line", "axis_iso"} or (
        math.isfinite(robot_stage) and (robot_stage < 24.0 or robot_stage >= 26.0)
    ):
        state.reset_line_contact()
    if math.isfinite(robot_stage):
        if state.last_robot_stage is None or abs(robot_stage - state.last_robot_stage) >= 0.03:
            state.line_stage_s = 0.0
            state.last_robot_stage = robot_stage
    if control_stage_active:
        state.line_stage_s += dt_s

    force_t, torque_t = kunwei_to_tcp_wrench(latest_zeroed)
    force_abs = norm3(force_t)
    contact_offset_x = ""
    contact_offset_y = ""
    if abs(force_t[2]) > args.step4e_contact_offset_min_fz_n:
        contact_offset_x = -torque_t[1] / force_t[2]
        contact_offset_y = torque_t[0] / force_t[2]

    rotation = rotvec_to_matrix(float(pose[3]), float(pose[4]), float(pose[5]))
    synthetic_normal = (
        synthetic_axis_iso_normal(robot_stage, math.radians(args.step4e_axis_iso_tilt_deg))
        if axis_iso_active
        else None
    )
    force_b = mat_vec3(rotation, force_t)
    n_reaction_b = synthetic_normal if synthetic_normal is not None else normalize3(force_b)
    raw_live_normal_b, live_candidate_b, live_candidate_force_n = live_normal_candidate(
        force_b,
        friction_projection=args.step4e_normal_friction_projection == "on",
    )
    if (
        detached_profile
        and (latch_stage_active or first_search_stage_active)
        and sensor_ok > 0.5
        and force_abs >= args.step4e_min_force_for_control_n
        and not state.latched_normal_locked
    ):
        state.latched_normal_b = n_reaction_b
        state.latched_normal_locked = True
        state.normal_acquired = True
    elif (
        not detached_profile
        and sensor_ok > 0.5
        and force_abs >= args.step4e_min_force_for_control_n
    ):
        if state.latched_normal_b is None:
            state.latched_normal_b = n_reaction_b
        else:
            blended = tuple(
                0.95 * state.latched_normal_b[idx] + 0.05 * n_reaction_b[idx]
                for idx in range(3)
            )
            state.latched_normal_b = normalize3(blended, state.latched_normal_b)
        state.normal_acquired = True
    elif synthetic_normal is not None:
        state.latched_normal_b = synthetic_normal
        state.latched_normal_locked = True
        state.normal_acquired = True
    if state.latched_normal_b is not None and state.filtered_normal_b is None:
        state.filtered_normal_b = state.latched_normal_b

    n_control_b = state.latched_normal_b if state.latched_normal_b is not None else n_reaction_b
    normal_filter_source = "locked"
    live_candidate_angle_rad: float | str = ""
    live_candidate_angle_from_latch_rad: float | str = ""
    normal_follow_active = (
        (v30_profile or v31_profile or step4f_profile or step4g_profile or step5b_profile or step5c_contact_profile or step6b_profile)
        and args.step4e_normal_follow_mode == "filtered_live"
        and line_stage_active
        and state.normal_acquired
        and state.latched_normal_b is not None
    )
    if normal_follow_active:
        filtered_current = state.filtered_normal_b if state.filtered_normal_b is not None else state.latched_normal_b
        live_candidate_angle_rad = angle_between_unit(filtered_current, live_candidate_b)
        live_candidate_angle_from_latch_rad = angle_between_unit(state.latched_normal_b, live_candidate_b)
        if v31_profile or step4f_profile or step4g_profile or step5b_profile or step5c_contact_profile or step6b_profile:
            state.filtered_normal_b, normal_filter_source = v31_filtered_live_normal(
                filtered_current,
                live_candidate_b,
                live_candidate_force_n,
                sensor_ok=sensor_ok,
                alpha=args.step4e_normal_filter_alpha,
                min_force_n=args.step4e_normal_min_force_n,
            )
            n_control_b = state.filtered_normal_b
        else:
            max_candidate_angle_rad = math.radians(args.step4e_normal_max_angle_from_latch_deg)
            if sensor_ok <= 0.5:
                normal_filter_source = "locked_fallback_stale"
                n_control_b = state.latched_normal_b
            elif live_candidate_force_n < args.step4e_normal_min_force_n:
                normal_filter_source = "freeze_low_force"
                n_control_b = filtered_current
            elif dot3(state.latched_normal_b, live_candidate_b) <= 0.0:
                normal_filter_source = "freeze_opposite_latch"
                n_control_b = filtered_current
            elif live_candidate_angle_from_latch_rad > max_candidate_angle_rad:
                normal_filter_source = "freeze_latch_angle_gate"
                n_control_b = filtered_current
            elif live_candidate_angle_rad > max_candidate_angle_rad:
                normal_filter_source = "freeze_candidate_angle_gate"
                n_control_b = filtered_current
            else:
                alpha = 1.0
                if args.step4e_normal_filter_tau_s > 0.0:
                    alpha = 1.0 - math.exp(-max(0.0, dt_s) / args.step4e_normal_filter_tau_s)
                ema_normal_b = slerp_unit(filtered_current, live_candidate_b, alpha)
                max_step_rad = max(0.0, args.step4e_normal_max_rate_rad_s) * max(0.0, dt_s)
                state.filtered_normal_b = rotate_toward_unit(filtered_current, ema_normal_b, max_step_rad)
                n_control_b = state.filtered_normal_b
                normal_filter_source = "filtered_live"
    elif (
        v30_profile or v31_profile or step4f_profile or step4g_profile or step5b_profile or step5c_contact_profile or step6b_profile
    ) and args.step4e_normal_follow_mode == "filtered_live":
        if line_stage_active:
            normal_filter_source = "locked_no_latch"
        else:
            normal_filter_source = "locked_pre_line"
    normal_load_n = max(0.0, dot3(force_b, n_control_b)) if state.normal_acquired else 0.0
    tcp_z_axis_b = (rotation[0][2], rotation[1][2], rotation[2][2])
    orientation_target_axis_b = (
        (-n_control_b[0], -n_control_b[1], -n_control_b[2])
        if (v21_profile or v22_profile or angular_speedl_profile)
        else n_control_b
    )
    orientation_axis = cross3(tcp_z_axis_b, orientation_target_axis_b)
    orientation_error = math.atan2(
        norm3(orientation_axis),
        clamp(dot3(tcp_z_axis_b, orientation_target_axis_b), -1.0, 1.0),
    )
    target_rotvec = (0.0, 0.0, 0.0)
    if (v21_profile or v22_profile) and orient_stage_active and state.normal_acquired:
        delta_r = minimal_rotation_between(tcp_z_axis_b, orientation_target_axis_b)
        target_rotvec = matrix_to_rotvec(mat_mul3(delta_r, rotation))
    orientation_cmd = (
        args.step4e_orientation_gain * args.step4e_orientation_wx_sign * orientation_axis[0],
        args.step4e_orientation_gain * args.step4e_orientation_wy_sign * orientation_axis[1],
        args.step4e_orientation_gain * orientation_axis[2],
    )
    orientation_norm = norm3(orientation_cmd)
    if orientation_norm > args.step4e_angular_limit_rad_s:
        scale = args.step4e_angular_limit_rad_s / orientation_norm
        orientation_cmd = tuple(value * scale for value in orientation_cmd)

    if step5b_profile:
        path_ref = step5_contact_path_reference(
            (float(pose[0]), float(pose[1])),
            state.line_stage_s,
        )
    elif step5c_dryrun_profile:
        path_ref = step5_contact_path_reference(
            (float(pose[0]), float(pose[1])),
            state.line_stage_s,
            stage_id=STEP5C_DRYRUN_STAGE_ID,
        )
    elif step5c_contact_profile:
        path_ref = step5_contact_path_reference(
            (float(pose[0]), float(pose[1])),
            state.line_stage_s,
            stage_id=STEP5C_CONTACT_STAGE_ID,
        )
    elif step6b_profile:
        path_ref = step6_contact_path_reference(
            (float(pose[0]), float(pose[1])),
            state.line_stage_s,
            stage_id=step6_stage_id,
        )
    else:
        path_ref = step4e_path_reference(
            args.step4e_path_shape,
            (float(pose[0]), float(pose[1])),
            state.line_stage_s,
        )
    progress = float(path_ref["progress"])
    desired_x, desired_y = path_ref["desired_xy"]
    path_error = (path_ref["path_error_xy"][0], path_ref["path_error_xy"][1], 0.0)
    if args.step4e_path_shape == "line":
        tangent_speed = args.step4e_line_speed_m_s if args.step4e_mode == "line" and line_stage_active else 0.0
        desired_velocity_xy = (
            tangent_speed * STEP4E_LINE_UNIT_XY[0],
            tangent_speed * STEP4E_LINE_UNIT_XY[1],
        )
    else:
        desired_velocity_xy = path_ref["desired_velocity_xy"]
        if args.step4e_mode != "line" or not line_stage_active:
            desired_velocity_xy = (0.0, 0.0)
    if line_stage_active and state.line_stage_s <= args.step4e_line_settle_s:
        desired_velocity_xy = (0.0, 0.0)
    if detached_profile and not line_stage_active:
        base_motion = (0.0, 0.0, 0.0)
    else:
        base_motion = (
            desired_velocity_xy[0] + args.step4e_path_p_gain * path_error[0],
            desired_velocity_xy[1] + args.step4e_path_p_gain * path_error[1],
            0.0,
        )
    if step5c_dryrun_profile:
        motion_cmd = base_motion
    else:
        normal_projection = dot3(base_motion, n_control_b)
        motion_cmd = tuple(base_motion[idx] - normal_projection * n_control_b[idx] for idx in range(3))
    motion_norm = norm3(motion_cmd)
    if motion_norm > args.step4e_motion_limit_m_s:
        scale = args.step4e_motion_limit_m_s / motion_norm
        motion_cmd = tuple(value * scale for value in motion_cmd)

    controlled_force_n = 0.0 if step5c_dryrun_profile else normal_load_n if args.step4e_mode == "line" else force_abs
    force_error = args.target_force_n - controlled_force_n
    line_grace_valid = (
        args.step4e_mode == "line"
        and not detached_profile
        and control_stage_active
        and not state.normal_acquired
        and state.line_stage_s <= args.step4e_acquire_grace_s
    )
    if step5c_dryrun_profile:
        control_allowed = args.step4e_mode == "line" and line_stage_active
    elif axis_iso_active:
        control_allowed = sensor_ok > 0.5 and state.normal_acquired
    elif detached_profile:
        control_allowed = sensor_ok > 0.5 and (
            (latch_stage_active and state.normal_acquired)
            or ((detach_stage_active or orient_stage_active or acquire_stage_active or line_stage_active) and state.normal_acquired)
            or line_grace_valid
        )
    else:
        control_allowed = sensor_ok > 0.5 and (
            force_abs >= args.step4e_min_force_for_control_n
            or (args.step4e_mode == "line" and state.normal_acquired)
            or line_grace_valid
        )
    if args.step4e_integrate_stage25_only and args.step4e_mode == "line" and not control_stage_active:
        control_allowed = False
    if control_allowed:
        if args.step4e_mode == "line" and not step5c_dryrun_profile and not state.normal_acquired:
            cmd = (0.0, 0.0, 0.0)
            orientation_cmd = (0.0, 0.0, 0.0)
        elif detached_profile and latch_stage_active:
            cmd = (0.0, 0.0, 0.0)
            orientation_cmd = (0.0, 0.0, 0.0)
        elif v21_profile and detach_stage_active:
            cmd = (0.0, 0.0, 0.0)
            orientation_cmd = (0.0, 0.0, 0.0)
        elif (v21_profile or v22_profile) and orient_stage_active:
            cmd = (0.0, 0.0, 0.0)
            orientation_cmd = target_rotvec
        elif angular_speedl_profile and orient_stage_active:
            cmd = (0.0, 0.0, 0.0)
        elif detached_profile and orient_stage_active:
            cmd = (0.0, 0.0, 0.0)
        elif axis_iso_active:
            cmd = (0.0, 0.0, 0.0)
        elif line_entry_gate_active:
            state.integral_error_n_s = 0.0
            state.normal_velocity_m_s = 0.0
            cmd = (0.0, 0.0, 0.0)
            orientation_cmd = (0.0, 0.0, 0.0)
        else:
            if step5c_dryrun_profile:
                cmd = motion_cmd
                orientation_cmd = (0.0, 0.0, 0.0)
                state.normal_velocity_m_s = 0.0
            else:
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
                if v20_profile and acquire_stage_active and force_error < -0.25:
                    unload_speed = min(
                        args.step4e_normal_velocity_limit_m_s,
                        max(args.step4e_reacquire_velocity_m_s, 0.003),
                    )
                    state.normal_velocity_m_s = min(state.normal_velocity_m_s, -unload_speed)
                if (
                    args.step4e_mode == "line"
                    and state.normal_acquired
                    and normal_load_n < args.step4e_min_force_for_control_n
                    and force_error > 0.0
                ):
                    state.normal_velocity_m_s = max(
                        state.normal_velocity_m_s,
                        min(args.step4e_reacquire_velocity_m_s, args.step4e_normal_velocity_limit_m_s),
                    )
                force_cmd = tuple(
                    -args.step4e_normal_command_sign * n_control_b[idx] * state.normal_velocity_m_s
                    for idx in range(3)
                )
                cmd = tuple(motion_cmd[idx] + force_cmd[idx] for idx in range(3))
                if detached_profile and acquire_stage_active:
                    orientation_cmd = (0.0, 0.0, 0.0)
        cmd_norm = norm3(cmd)
        if cmd_norm > args.step4e_total_linear_limit_m_s:
            scale = args.step4e_total_linear_limit_m_s / cmd_norm
            cmd = tuple(value * scale for value in cmd)
        if angular_speedl_profile and orient_stage_active and (
            abs(cmd[0]) > 1e-12 or abs(cmd[1]) > 1e-12 or abs(cmd[2]) > 1e-12
        ):
            raise RuntimeError("Step4e v23..v31 stage 25.2 contract violation: linear command registers must be zero")
        joint_result = None
        if step5c_joint_profile:
            try:
                actual_q = latest_output.get("actual_q")
                if not actual_q or len(actual_q) < 6:
                    raise ValueError("missing RTDE actual_q")
                joint_result = step5c_solver(args).solve(
                    actual_q[:6],
                    (cmd[0], cmd[1], cmd[2], orientation_cmd[0], orientation_cmd[1], orientation_cmd[2]),
                )
                cmd = (joint_result.qdot[0], joint_result.qdot[1], joint_result.qdot[2])
                orientation_cmd = (joint_result.qdot[3], joint_result.qdot[4], joint_result.qdot[5])
            except (ValueError, RuntimeError) as exc:
                values["_step5c_solver_error"] = str(exc)
                cmd = (0.0, 0.0, 0.0)
                orientation_cmd = (0.0, 0.0, 0.0)
                joint_result = None
        values.update(
            {
                "step4e_cmd_vx_m_s": n_control_b[0] if (v21_profile and detach_stage_active) else cmd[0],
                "step4e_cmd_vy_m_s": n_control_b[1] if (v21_profile and detach_stage_active) else cmd[1],
                "step4e_cmd_vz_m_s": n_control_b[2] if (v21_profile and detach_stage_active) else cmd[2],
                "step4e_cmd_wx_rad_s": orientation_cmd[0],
                "step4e_cmd_wy_rad_s": orientation_cmd[1],
                "step4e_cmd_wz_rad_s": orientation_cmd[2] if (axis_iso_active or v21_profile or v22_profile or angular_speedl_profile) else 0.0,
                "step4e_cmd_valid": 0.0 if args.step4e_mode == "preview" or (step5c_joint_profile and joint_result is None) else 1.0,
                "step4e_progress_m": progress,
                "step4e_force_error_n": force_error,
                "step4e_orientation_error_rad": orientation_error,
                "step4e_controller_state": (
                    joint_result.solver_status
                    if step5c_joint_profile and joint_result is not None
                    else STATUS_INVALID
                    if step5c_joint_profile and joint_result is None
                    else (
                    33.0
                    if latch_stage_active
                    else 35.0
                    if detach_stage_active
                    else 31.0
                    if orient_stage_active
                    else 32.0
                    if acquire_stage_active and not line_entry_gate_active
                    else 36.0
                    if line_entry_gate_active
                    else 34.0
                    if axis_iso_active
                    else {"preview": 10.0, "hold": 20.0, "line": 30.0, "axis_iso": 34.0}[args.step4e_mode]
                    )
                ),
            }
        )
        if step5c_joint_profile and joint_result is not None:
            values["_step5c_cmd_qd0_rad_s"] = joint_result.qdot[0]
            values["_step5c_cmd_qd1_rad_s"] = joint_result.qdot[1]
            values["_step5c_cmd_qd2_rad_s"] = joint_result.qdot[2]
            values["_step5c_cmd_qd3_rad_s"] = joint_result.qdot[3]
            values["_step5c_cmd_qd4_rad_s"] = joint_result.qdot[4]
            values["_step5c_cmd_qd5_rad_s"] = joint_result.qdot[5]
            values["_step5c_solver_status"] = joint_result.solver_status
            values["_step5c_qdot_max_abs_rad_s"] = joint_result.max_abs_qdot_rad_s
            values["_step5c_qdot_clipped"] = 1.0 if joint_result.clipped else 0.0
            values["_step5c_qdot_projected"] = 1.0 if joint_result.projected else 0.0
            values["_step5c_solver_residual_norm"] = joint_result.residual_norm
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
    values["_step4e_latched_normal_b_x"] = "" if state.latched_normal_b is None else state.latched_normal_b[0]
    values["_step4e_latched_normal_b_y"] = "" if state.latched_normal_b is None else state.latched_normal_b[1]
    values["_step4e_latched_normal_b_z"] = "" if state.latched_normal_b is None else state.latched_normal_b[2]
    values["_step4e_live_normal_raw_b_x"] = raw_live_normal_b[0]
    values["_step4e_live_normal_raw_b_y"] = raw_live_normal_b[1]
    values["_step4e_live_normal_raw_b_z"] = raw_live_normal_b[2]
    values["_step4e_live_normal_candidate_b_x"] = live_candidate_b[0]
    values["_step4e_live_normal_candidate_b_y"] = live_candidate_b[1]
    values["_step4e_live_normal_candidate_b_z"] = live_candidate_b[2]
    values["_step4e_filtered_normal_b_x"] = "" if state.filtered_normal_b is None else state.filtered_normal_b[0]
    values["_step4e_filtered_normal_b_y"] = "" if state.filtered_normal_b is None else state.filtered_normal_b[1]
    values["_step4e_filtered_normal_b_z"] = "" if state.filtered_normal_b is None else state.filtered_normal_b[2]
    values["_step4e_control_normal_b_x"] = n_control_b[0]
    values["_step4e_control_normal_b_y"] = n_control_b[1]
    values["_step4e_control_normal_b_z"] = n_control_b[2]
    values["_step4e_live_normal_candidate_force_n"] = live_candidate_force_n
    values["_step4e_live_normal_candidate_angle_rad"] = live_candidate_angle_rad
    values["_step4e_live_normal_angle_from_latch_rad"] = live_candidate_angle_from_latch_rad
    values["_step4e_normal_filter_source"] = normal_filter_source
    values["_step4e_normal_follow_mode"] = args.step4e_normal_follow_mode
    values["_step4e_normal_load_n"] = normal_load_n
    values["_step4e_normal_force_error_n"] = force_error
    values["_step4e_normal_acquired"] = 1.0 if state.normal_acquired else 0.0
    values["_step4e_line_stage_s"] = state.line_stage_s
    values["_step4e_path_shape"] = args.step4e_path_shape
    values["_step4e_path_time_s"] = path_ref["path_time_s"]
    values["_step4e_desired_x_m"] = desired_x
    values["_step4e_desired_y_m"] = desired_y
    values["_step4e_desired_vx_m_s"] = desired_velocity_xy[0]
    values["_step4e_desired_vy_m_s"] = desired_velocity_xy[1]
    values["_step4e_path_error_x_m"] = path_error[0]
    values["_step4e_path_error_y_m"] = path_error[1]
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


def env_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = os.getenv(name, default)
    if value not in choices:
        raise SystemExit(f"{name} must be one of {choices}; got {value!r}")
    return value


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a float; got {value!r}") from exc


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
    parser.add_argument("--target-force-n", type=float, default=5.0)
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
    parser.add_argument("--max-force-norm-n", type=float, default=60.0)
    parser.add_argument("--max-torque-norm-nm", type=float, default=3.0)
    parser.add_argument("--sensor-stale-s", type=float, default=0.08)
    parser.add_argument("--rezero-s", type=float, default=1.0)
    parser.add_argument("--step4e-mode", choices=("off", "preview", "hold", "line", "axis_iso"), default="off")
    parser.add_argument("--step4e-version", default="")
    parser.add_argument("--step4e-path-shape", choices=("line", "cycloid", "eight"), default="line")
    parser.add_argument("--step4e-line-speed-m-s", type=float, default=0.003)
    parser.add_argument("--step4e-line-settle-s", type=float, default=0.0)
    parser.add_argument("--step4e-integrate-stage25-only", action="store_true")
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
    parser.add_argument("--step4e-acquire-grace-s", type=float, default=0.25)
    parser.add_argument("--step4e-reacquire-velocity-m-s", type=float, default=0.001)
    parser.add_argument("--step4e-orientation-gain", type=float, default=0.20)
    parser.add_argument("--step4e-orientation-wx-sign", type=float, choices=(-1.0, 1.0), default=1.0)
    parser.add_argument("--step4e-orientation-wy-sign", type=float, choices=(-1.0, 1.0), default=1.0)
    parser.add_argument("--step4e-angular-limit-rad-s", type=float, default=0.015)
    parser.add_argument("--step4e-axis-iso-tilt-deg", type=float, default=10.0)
    parser.add_argument("--step4e-contact-offset-min-fz-n", type=float, default=1.0)
    parser.add_argument(
        "--step4e-normal-follow-mode",
        choices=("locked", "filtered_live"),
        default=env_choice("STEP4E_NORMAL_FOLLOW_MODE", "locked", ("locked", "filtered_live")),
    )
    parser.add_argument(
        "--step4e-normal-filter-tau-s",
        type=float,
        default=env_float("STEP4E_NORMAL_FILTER_TAU_S", 0.35),
    )
    parser.add_argument(
        "--step4e-normal-filter-alpha",
        type=float,
        default=env_float("STEP4E_NORMAL_FILTER_ALPHA", 0.35),
    )
    parser.add_argument(
        "--step4e-normal-max-rate-rad-s",
        type=float,
        default=env_float("STEP4E_NORMAL_MAX_RATE_RAD_S", 0.010),
    )
    parser.add_argument(
        "--step4e-normal-min-force-n",
        type=float,
        default=env_float("STEP4E_NORMAL_MIN_FORCE_N", 2.0),
    )
    parser.add_argument(
        "--step4e-normal-max-angle-from-latch-deg",
        type=float,
        default=env_float("STEP4E_NORMAL_MAX_ANGLE_FROM_LATCH_DEG", 20.0),
    )
    parser.add_argument(
        "--step4e-normal-friction-projection",
        choices=("on", "off"),
        default=env_choice("STEP4E_NORMAL_FRICTION_PROJECTION", "on", ("on", "off")),
    )
    parser.add_argument("--step5c-qdot-limit-rad-s", type=float, default=0.15)
    parser.add_argument("--step5c-joint-damping", type=float, default=1e-4)
    parser.add_argument(
        "--step5c-joint-model",
        type=Path,
        default=Path(
            "/home/andy/ur10e_ros2_ws/experiments/20260523_tase_finite_time_ur10e_mujoco_reproduction/assets/mjcf/ur10e_nominal.xml"
        ),
    )
    parser.add_argument("--step5c-joint-site", default="tcp_site_unverified_85mm")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.duration_s <= 0 or args.baseline_s < 0 or args.rtde_hz <= 0:
        raise SystemExit("duration, baseline, and RTDE rate must be positive")
    if not args.no_start_command and not args.allow_kunwei_stream_command:
        raise SystemExit("Refusing to send Kunwei stream command without --allow-kunwei-stream-command")
    known_step4e_versions = {
        "",
        "v20",
        "v21",
        "v22",
        "v23",
        "v24",
        "v25",
        "v26",
        "v27",
        "v28",
        "v29",
        "v30",
        "v31",
        "step4f_v1",
        "step4g_v1",
        "step5b_v1",
        STEP5C_DRYRUN_STAGE_ID,
        STEP5C_CONTACT_STAGE_ID,
        "step6b_v1",
        "step6b_v2",
    }
    if args.step4e_version not in known_step4e_versions:
        raise SystemExit(
            f"Unknown --step4e-version {args.step4e_version!r}; bridge profiles only cover "
            f"{sorted(v for v in known_step4e_versions if v)}. Add the new version to the "
            "profile definitions before running, otherwise cmd_valid is never asserted."
        )
    if args.step4e_version == STEP5C_DRYRUN_STAGE_ID:
        raise SystemExit(
            "Blocked Step5c dry-run: 2026-06-13 live run showed wrong XY/Z motion from "
            "the DLS/MuJoCo Jacobian mapping. Do not run until offline mapping validation passes."
        )
    if args.step4e_version == STEP5C_CONTACT_STAGE_ID:
        raise SystemExit(
            "Blocked Step5c contact: step5c_joint_rnn_cycloid_v1 was a misnamed DLS route, "
            "not strict TASE RNN. No Step5c joint-space bridge profile is runnable."
        )
    if args.step4e_normal_filter_tau_s < 0.0:
        raise SystemExit("--step4e-normal-filter-tau-s must be non-negative")
    if not 0.0 <= args.step4e_normal_filter_alpha <= 1.0:
        raise SystemExit("--step4e-normal-filter-alpha must be in [0, 1]")
    if args.step4e_normal_max_rate_rad_s < 0.0:
        raise SystemExit("--step4e-normal-max-rate-rad-s must be non-negative")
    if args.step4e_normal_min_force_n < 0.0:
        raise SystemExit("--step4e-normal-min-force-n must be non-negative")
    if args.step4e_normal_max_angle_from_latch_deg <= 0.0:
        raise SystemExit("--step4e-normal-max-angle-from-latch-deg must be positive")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    sensor_csv_path = args.output_dir / "kunwei_sensor_1khz.csv"
    rtde_hz_label = f"{args.rtde_hz:g}".replace(".", "p")
    bridge_csv_path = args.output_dir / f"bridge_rtde_{rtde_hz_label}hz.csv"
    raw_path = args.output_dir / "raw_frames.bin"
    metadata_path = args.output_dir / "metadata.json"
    summary_path = args.output_dir / "summary.json"
    stop_signal: dict[str, str | None] = {"name": None}

    def request_stop(signum: int, _frame: Any) -> None:
        stop_signal["name"] = signal.Signals(signum).name

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

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
        "guard_contract": {
            "max_normal_force_n": args.max_normal_force_n,
            "max_force_norm_n": args.max_force_norm_n,
            "max_torque_norm_nm": args.max_torque_norm_nm,
        },
        "register_map": dict(zip(INPUT_FIELDS, INPUT_NAMES)),
        "stage_aware_register_notes": {
            "p0_geo_ball_first_contact": "step4e-mode=off; only base force/heartbeat/guard registers are used. No Step4E motion or attitude command registers are consumed.",
            "p0_ball_vs_cyl_contact": "step4e-mode=off; one TP program runs ball-contact and housing/cylindrical-face witness searches. It uses low-threshold contact only, not 5N force acquisition.",
            "axis_iso_25.21_to_25.24": "input_double_register_40..42 are angular speedl wx/wy/wz; input_double_register_37..39 must remain zero.",
            "v21_line_25.1": "input_double_register_37..39 are the +locked-normal unit detach direction, not Cartesian velocity.",
            "v21_line_25.2": "input_double_register_40..42 are target TCP rotvec rx/ry/rz for a single detached movel; target is z_tcp_B ~= -locked_normal_B.",
            "v22_seed_normal_loop": "25.05 latches the first contact normal; 25.2 outputs target TCP rotvec for optional lifted posture correction; 25.3 reacquires 5 N before 25.0 line control.",
            "v23_seed_normal_loop_failed_archive": "24.0/24.2 latch the first contact normal; 25.2 outputs angular speedl wx/wy/wz for lifted posture correction; 25.3 reacquires 5 N before 25.0 line control. Archived after 2026-06-12 stop_reason=13 at 25.2.",
            "v24_seed_normal_loop_evidence": "One-step entry scaffold evidence; first search envelope still used a fixed 80 mm far-search transition.",
            "v25_seed_normal_loop_failed_archive": "Dynamic first search formula but wrong target initial Z datum; archived after 2026-06-12 no-contact depth stop.",
            "v26_seed_normal_loop_failed_archive": "Reference-Z search used the reference path contact-start datum; archived after 2026-06-12 no-contact depth stop.",
            "v27_seed_normal_loop_failed_archive": "Used v13/v16 force-jump first-contact Z evidence; archived after stage 25.05 cmd_valid timeout with the older bridge profile set.",
            "v28_seed_normal_loop_failed_archive": "Reached first contact and stage 25.05, but the runtime bridge did not recognize v28 in the active profile set, so cmd_valid stayed 0.",
            "v29_seed_normal_loop_previous": "Canonical locked-normal flow follows STEP4E_FLOW.md: v28 motion with bridge-profile recognition fixed; one-step entry XY plus target attitude at current Z, first far/near search using v13/v16 first-contact Z evidence with a 20 mm near window and 2.5 mm/s near descent, first-contact latch, 20 mm lift, 25.2 angular speedl after input_double_register_37..39 settle to zero, second search, 25.3 zero-linear line-entry gate, then line control.",
            "v30_seed_normal_loop_previous": "Same TP flow as v29. Bridge defaults to locked normal, but --step4e-normal-follow-mode filtered_live changes only stage 25.0 line control to use a friction-projected, gated, slew-limited live normal estimate; 25.2 and 25.3 continue to use the first locked normal.",
            "v31_seed_normal_loop_current": "Same TP flow as v30. Bridge --step4e-normal-follow-mode filtered_live changes only stage 25.0 line control to use a friction-projected live normal with direct alpha EMA, min-force hold, and same-hemisphere hold against the previous filtered normal; no slew-rate, latch-angle, or candidate-angle gate.",
            "step4f_cycloid_seed_normal_v1": "Same TP flow and force/normal/orientation loop as v31, but stage 25.0 uses the paper Experiment #1 cycloid XY reference for 60 s.",
            "step4g_eight_seed_normal_v1": "Same TP flow and force/normal/orientation loop as v31, but stage 25.0 uses the paper Experiment #2 8-shaped XY reference for 60 s.",
            "step5b_contact_cycloid_baseline_v1": "Same TP contact-search/latch/25.2/25.3 scaffold as v31, but stage 25.0 uses the active Step5 table contact cycloid reference and v31 filtered-live normal policy.",
            "step5c_speedj_dryrun_v1": "No-contact Step5c joint-space dry-run: bridge reads actual_q and writes qd0..qd5 in registers 37..42; TP executes speedj only in the Stage25 dry-run window.",
            "step5c_joint_rnn_cycloid_v1": "Blocked/quarantined Step5c contact route: previous implementation was DLS, not strict TASE RNN.",
            "step6b_contact_eight_baseline_v1": "Same TP contact-search/latch/25.2/25.3 scaffold as Step5b/v31, but stage 25.0 uses the active Step6 five-point safe-frame 8-shaped reference for 30 s and v31 filtered-live normal policy.",
            "step6b_contact_eight_baseline_v2": "Same TP contact-search/latch/25.2/25.3 scaffold and Step6 reference as v1, but intended bridge caps are 15 mm/s path, 15 mm/s total linear, 3 mm/s normal reserve, and 0.060 rad/s attitude.",
        },
        "step4e_path": {
            "type": args.step4e_path_shape,
            "start_xy_m": STEP4E_START_XY,
            "end_xy_m": STEP4E_END_XY,
            "mid_xy_m": STEP4E_LINE_MID_XY,
            "line_length_m": STEP4E_LINE_LENGTH_M,
            "line_unit_xy": STEP4E_LINE_UNIT_XY,
            "line_perp_xy": STEP4E_LINE_PERP_XY,
            "curve_duration_s": STEP4FG_PATH_DURATION_S,
            "curve_source": "TASE paper Section VI-A; Step4f Experiment #1 cycloid and Step4g Experiment #2 8-shaped.",
            "kunwei_to_tcp": "F_T=[Fx_K,Fy_K,Fz_K], M_T=[Mx_K,My_K,Mz_K]",
            "tcp_contact_length_m": 0.1221,
            "line_control_target": "latched contact normal load, not total force norm",
            "step4f_safe_frame": (
                STEP4F_SAFE_FRAME
                if args.step4e_path_shape == "cycloid" and args.step4e_version != "step5b_v1"
                else None
            ),
            "step5_stage_id": STEP5_CONTACT_CYCLOID_STAGE_ID if args.step4e_version == "step5b_v1" else None,
            "step5c_stage_id": args.step4e_version
            if args.step4e_version in {STEP5C_DRYRUN_STAGE_ID, STEP5C_CONTACT_STAGE_ID}
            else None,
            "step5c_register_contract": "37..42=qd0..qd5 rad/s, 43=cmd_valid, 44=path_time, 45=force_error, 46=pose/orientation_error, 47=solver_status"
            if args.step4e_version in {STEP5C_DRYRUN_STAGE_ID, STEP5C_CONTACT_STAGE_ID}
            else None,
            "step5c_joint_model": str(args.step5c_joint_model)
            if args.step4e_version in {STEP5C_DRYRUN_STAGE_ID, STEP5C_CONTACT_STAGE_ID}
            else None,
            "step5c_qdot_limit_rad_s": args.step5c_qdot_limit_rad_s
            if args.step4e_version in {STEP5C_DRYRUN_STAGE_ID, STEP5C_CONTACT_STAGE_ID}
            else None,
            "step6_stage_id": (
                STEP6_CONTACT_EIGHT_STAGE_ID_V2
                if args.step4e_version == "step6b_v2"
                else STEP6_CONTACT_EIGHT_STAGE_ID
                if args.step4e_version == "step6b_v1"
                else None
            ),
            "step6_curve_duration_s": STEP6_PATH_DURATION_S if args.step4e_version in {"step6b_v1", "step6b_v2"} else None,
            "step6_table_source": str(STEP6_TABLE_PATH.relative_to(EXPERIMENT_ROOT))
            if args.step4e_version in {"step6b_v1", "step6b_v2"}
            else None,
            "step6_safe_frame_source": str(STEP6_SAFE_FRAME_PATH.relative_to(EXPERIMENT_ROOT))
            if args.step4e_version in {"step6b_v1", "step6b_v2"}
            else None,
            "step6_safe_frame": load_step6_safe_frame() if args.step4e_version in {"step6b_v1", "step6b_v2"} else None,
            "step4e_motion_limit_m_s": args.step4e_motion_limit_m_s,
            "step4e_total_linear_limit_m_s": args.step4e_total_linear_limit_m_s,
            "step4e_normal_velocity_limit_m_s": args.step4e_normal_velocity_limit_m_s,
            "step4e_angular_limit_rad_s": args.step4e_angular_limit_rad_s,
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
            "_step4e_latched_normal_b_x",
            "_step4e_latched_normal_b_y",
            "_step4e_latched_normal_b_z",
            "_step4e_live_normal_raw_b_x",
            "_step4e_live_normal_raw_b_y",
            "_step4e_live_normal_raw_b_z",
            "_step4e_live_normal_candidate_b_x",
            "_step4e_live_normal_candidate_b_y",
            "_step4e_live_normal_candidate_b_z",
            "_step4e_filtered_normal_b_x",
            "_step4e_filtered_normal_b_y",
            "_step4e_filtered_normal_b_z",
            "_step4e_control_normal_b_x",
            "_step4e_control_normal_b_y",
            "_step4e_control_normal_b_z",
            "_step4e_live_normal_candidate_force_n",
            "_step4e_live_normal_candidate_angle_rad",
            "_step4e_live_normal_angle_from_latch_rad",
            "_step4e_normal_filter_source",
            "_step4e_normal_follow_mode",
            "_step4e_normal_load_n",
            "_step4e_normal_force_error_n",
            "_step4e_normal_acquired",
            "_step4e_line_stage_s",
            "_step4e_path_shape",
            "_step4e_path_time_s",
            "_step4e_desired_x_m",
            "_step4e_desired_y_m",
            "_step4e_desired_vx_m_s",
            "_step4e_desired_vy_m_s",
            "_step4e_path_error_x_m",
            "_step4e_path_error_y_m",
            "_step4e_contact_offset_x_m",
            "_step4e_contact_offset_y_m",
            "_step4e_actual_speed_norm_m_s",
            "_step5c_cmd_qd0_rad_s",
            "_step5c_cmd_qd1_rad_s",
            "_step5c_cmd_qd2_rad_s",
            "_step5c_cmd_qd3_rad_s",
            "_step5c_cmd_qd4_rad_s",
            "_step5c_cmd_qd5_rad_s",
            "_step5c_solver_status",
            "_step5c_qdot_max_abs_rad_s",
            "_step5c_qdot_clipped",
            "_step5c_qdot_projected",
            "_step5c_solver_residual_norm",
            "_step5c_solver_error",
        ]
        bridge_output_fields = [
            f"ur_{field}_{idx}"
            for field in ["actual_TCP_pose", "actual_TCP_speed", "actual_q", "actual_qd"]
            for idx in range(6)
        ]
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
                if stop_signal["name"] is not None:
                    stop_reason = f"signal_{stop_signal['name'].lower()}"
                    break
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
