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

import numpy as np
import pinocchio as pin


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
STEP4F_SAFE_FRAME_PATH = EXPERIMENT_ROOT / "config" / "step4f_safe_frame.json"
KUNWEI_TOOLS = Path("/home/andy/ur10e_ros2_ws/experiments/sensor-integration/kunwei-kwr75b/tools")
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
from contact_semantics import semantic_boundary_is_consistent  # noqa: E402
import step5c_calibrated_kinematics_audit as step5d_kin  # noqa: E402
from step5_table import step5_path_reference  # noqa: E402
from step5c_dls_joint_solver import JointSolverConfig, STATUS_INVALID, Step5cDlsJointSolver  # noqa: E402
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver  # noqa: E402
from step5d_paper_outer_loop import (  # noqa: E402
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
    rnn_target_state_from_outer_loop,
)
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
BRIDGE_INPUT_FIELDS = [
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
BRIDGE_INPUT_NAMES = [
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
STEP4E_INPUT_FIELDS = BRIDGE_INPUT_FIELDS
STEP4E_INPUT_NAMES = BRIDGE_INPUT_NAMES
STEP5C_JOINT_REGISTER_CONTRACT = [
    ("input_double_register_37", "step4e_cmd_vx_m_s", "_step5c_cmd_qd0_rad_s", "qd0_rad_s"),
    ("input_double_register_38", "step4e_cmd_vy_m_s", "_step5c_cmd_qd1_rad_s", "qd1_rad_s"),
    ("input_double_register_39", "step4e_cmd_vz_m_s", "_step5c_cmd_qd2_rad_s", "qd2_rad_s"),
    ("input_double_register_40", "step4e_cmd_wx_rad_s", "_step5c_cmd_qd3_rad_s", "qd3_rad_s"),
    ("input_double_register_41", "step4e_cmd_wy_rad_s", "_step5c_cmd_qd4_rad_s", "qd4_rad_s"),
    ("input_double_register_42", "step4e_cmd_wz_rad_s", "_step5c_cmd_qd5_rad_s", "qd5_rad_s"),
]
STEP5C_INPUT_REGISTER_SEMANTICS = {
    "input_double_register_37": "qd0_rad_s",
    "input_double_register_38": "qd1_rad_s",
    "input_double_register_39": "qd2_rad_s",
    "input_double_register_40": "qd3_rad_s",
    "input_double_register_41": "qd4_rad_s",
    "input_double_register_42": "qd5_rad_s",
    "input_double_register_43": "cmd_valid",
    "input_double_register_44": "path_time_s",
    "input_double_register_45": "force_error_n",
    "input_double_register_46": "pose_or_orientation_error",
    "input_double_register_47": "solver_status",
}
STEP5C_DIAG_FIELDS = [
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
STEP5D_DIAG_FIELDS = [
    "_step5d_solver_status",
    "_step5d_qdot_max_abs_rad_s",
    "_step5d_constraint_residual_norm",
    "_step5d_outer_xdot_norm",
    "_step5d_outer_xdot_limited_norm",
    "_step5d_outer_xdot_limiter_active",
    "_step5d_qdot_slew_limiter_active",
    "_step5d_engage_gate_ok",
    "_step5d_line_guard_ok",
    "_step5d_line_guard_loss_s",
    "_step5d_line_guard_reason",
    "_step5d_line_tcp_speed_m_s",
    "_step5d_contact_safety_state",
    "_step5d_contact_safety_action",
    "_step5d_contact_hold_s",
    "_step5d_contact_high_window_s",
    "_step5d_actual_speed_violation_s",
    "_step5d_actual_speed_violation_count",
    "_step5d_actual_tcp_speed_m_s",
    "_step5d_predicted_tcp_speed_m_s",
    "_step5d_tcp_cage_distance_m",
    "_step5d_tcp_cage_braking_margin_m",
    "_step5d_tcp_cage_signed_distance_m",
    "_step5d_tcp_cage_cell_index",
    "_step5d_tcp_cage_reason",
    "_step5d_hold_event_count",
    "_step5d_consecutive_hold_s",
    "_step5d_total_hold_s",
    "_step5d_hold_duty",
    "_step5d_repeated_hold_count",
    "_step5d_contact_safety_reason",
    "_step5d_control_normal_vs_world_z_angle_rad",
    "_step5d_control_normal_vs_tcp_z_angle_rad",
    "_step5d_approach_normal_vs_tcp_z_angle_rad",
    "_step5d_force_settle_ready",
    "_step5d_force_settle_filtered_normal_load_n",
    "_step5d_force_settle_velocity_m_s",
    "_step5d_force_sign_convention",
    "_step5d_proj_input_form",
    "_step5d_active_bounds_count",
    "_step5d_contact_orientation_error_rad",
    "_step5d_outer_orientation_error_rad",
    "_step5d_R_d_z_dot_R_cur_z",
    "_step5d_semantic_gate_ok",
    "_step5d_solver_error",
]
INPUT_FIELDS = BASE_INPUT_FIELDS + BRIDGE_INPUT_FIELDS
INPUT_NAMES = BASE_INPUT_NAMES + BRIDGE_INPUT_NAMES
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
STEP5B_15N_TRIAL_PROFILE = "guarded_15n_sentinel"
STEP5B_RAMP_5_TO_15_TRIAL_PROFILE = "ramp_5_to_15_sentinel"
STEP5B_TRIAL_PROFILES = ("none", STEP5B_15N_TRIAL_PROFILE, STEP5B_RAMP_5_TO_15_TRIAL_PROFILE)
STEP5B_15N_TRIAL_TARGET_N = 15.0
STEP5B_15N_TRIAL_TARGET_TOL_N = 0.1
STEP5B_15N_TRIAL_ACQUIRE_N = 12.0
STEP5B_15N_TRIAL_ACQUIRE_TIMEOUT_S = 2.0
STEP5B_15N_TRIAL_DISCARD_S = 0.5
STEP5B_15N_TRIAL_SCORE_S = 2.0
STEP5B_15N_TRIAL_NORMAL_STOP_N = 20.0
STEP5B_15N_TRIAL_NORMAL_DWELL_N = 18.0
STEP5B_15N_TRIAL_NORMAL_DWELL_S = 0.050
STEP5B_15N_TRIAL_FORCE_STOP_N = 25.0
STEP5B_15N_TRIAL_FORCE_DWELL_N = 22.0
STEP5B_15N_TRIAL_FORCE_DWELL_S = 0.050
STEP5B_15N_TRIAL_TORQUE_STOP_NM = 1.5
STEP5B_15N_TRIAL_TORQUE_DWELL_NM = 0.6
STEP5B_15N_TRIAL_TORQUE_DWELL_S = 0.100
STEP5B_15N_TRIAL_LOW_LOAD_N = 2.0
STEP5B_15N_TRIAL_LOW_LOAD_S = 0.500
STEP5B_15N_TRIAL_RELATIVE_LOW_LOAD_N = 7.5
STEP5B_15N_TRIAL_RELATIVE_LOW_LOAD_S = 0.500
STEP5B_15N_TRIAL_SATURATION_S = 0.200
STEP5B_15N_TRIAL_NORMAL_VELOCITY_LIMIT_MAX_M_S = 0.0011
STEP5B_RAMP_NORMAL_VELOCITY_LIMIT_MAX_M_S = 0.0101
STEP5B_RAMP_START_TARGET_N = 5.0
STEP5B_RAMP_FINAL_TARGET_N = 15.0
STEP5B_RAMP_PRELOAD_MIN_N = 2.0
STEP5B_RAMP_PRELOAD_MAX_N = 8.0
STEP5B_RAMP_PRELOAD_FORCE_NORM_MAX_N = 12.0
STEP5B_RAMP_PRELOAD_HOLD_S = 0.150
STEP5B_RAMP_PRELOAD_TIMEOUT_S = 10.0
STEP5B_RAMP_DURATION_S = 5.0
STEP5B_RAMP_FINAL_ACQUIRE_N = 12.0
STEP5B_RAMP_FINAL_DISCARD_S = 0.5
STEP5B_RAMP_FINAL_SCORE_S = 2.0
STEP5B_RAMP_MOVE_SCORE_S = 2.0
STEP5B_RAMP_PRELOAD_NORMAL_STOP_N = 35.0
STEP5B_RAMP_PRELOAD_FORCE_STOP_N = 35.0
STEP5B_RAMP_NORMAL_STOP_MARGIN_N = 20.0
STEP5B_RAMP_NORMAL_DWELL_MARGIN_N = 18.0
STEP5B_RAMP_FORCE_STOP_MARGIN_N = 20.0
STEP5B_RAMP_FORCE_DWELL_MARGIN_N = 18.0
STEP5B_RAMP_PHASE_CODES = {
    "inactive": 0.0,
    "pre_unload_to_5": 1.0,
    "ramp_5_to_15": 2.0,
    "hold_15": 3.0,
    "move_xy": 4.0,
    "complete": 5.0,
}
STEP5C_DRYRUN_STAGE_ID = "step5c_speedj_dryrun_v1"
STEP5C_CONTACT_STAGE_ID = "step5c_joint_rnn_cycloid_v1"
STEP5D_REPRODUCTION_STAGE_ID = "step5d_strict_rnn_reproduction_v1"
STEP5D_LIVEPREP_V1_STAGE_ID = "step5d_strict_rnn_liveprep_v1"
STEP5D_LIVEPREP_V2_STAGE_ID = "step5d_strict_rnn_liveprep_v2"
STEP5D_LIVEPREP_V3_STAGE_ID = "step5d_strict_rnn_liveprep_v3"
STEP5D_LIVEPREP_V4_STAGE_ID = "step5d_strict_rnn_liveprep_v4"
STEP5D_LIVEPREP_V5_STAGE_ID = "step5d_strict_rnn_liveprep_v5"
STEP5D_LIVEPREP_V6_STAGE_ID = "step5d_strict_rnn_liveprep_v6"
STEP5D_LIVEPREP_V7_STAGE_ID = "step5d_strict_rnn_liveprep_v7"
STEP5D_LIVEPREP_V8_STAGE_ID = "step5d_strict_rnn_liveprep_v8"
STEP5D_LIVEPREP_V9_STAGE_ID = "step5d_strict_rnn_liveprep_v9"
STEP5D_LIVEPREP_V10_STAGE_ID = "step5d_strict_rnn_liveprep_v10"
STEP5D_LIVEPREP_V11_STAGE_ID = "step5d_strict_rnn_liveprep_v11"
STEP5D_LIVEPREP_V12_STAGE_ID = "step5d_strict_rnn_liveprep_v12"
STEP5D_LIVEPREP_V13_STAGE_ID = "step5d_strict_rnn_liveprep_v13"
STEP5D_LIVEPREP_STAGE_ID = "step5d_strict_rnn_liveprep_v14"
STEP5D_LIVEPREP_V15_STAGE_ID = "step5d_strict_rnn_liveprep_v15"
STEP5D_LIVEPREP_V15A_STAGE_ID = "step5d_strict_rnn_liveprep_v15a"
STEP5D_LIVEPREP_STAGE_IDS = {
    STEP5D_LIVEPREP_V1_STAGE_ID,
    STEP5D_LIVEPREP_V2_STAGE_ID,
    STEP5D_LIVEPREP_V3_STAGE_ID,
    STEP5D_LIVEPREP_V4_STAGE_ID,
    STEP5D_LIVEPREP_V5_STAGE_ID,
    STEP5D_LIVEPREP_V6_STAGE_ID,
    STEP5D_LIVEPREP_V7_STAGE_ID,
    STEP5D_LIVEPREP_V8_STAGE_ID,
    STEP5D_LIVEPREP_V9_STAGE_ID,
    STEP5D_LIVEPREP_V10_STAGE_ID,
    STEP5D_LIVEPREP_V11_STAGE_ID,
    STEP5D_LIVEPREP_V12_STAGE_ID,
    STEP5D_LIVEPREP_V13_STAGE_ID,
    STEP5D_LIVEPREP_STAGE_ID,
    STEP5D_LIVEPREP_V15_STAGE_ID,
    STEP5D_LIVEPREP_V15A_STAGE_ID,
}
STEP5D_SEMANTIC_ORIENTATION_TOLERANCE_RAD = math.radians(5.0)
STEP5D_LIVEPREP_TRUTH_PATH = EXPERIMENT_ROOT / "config" / "step5d_liveprep_solver_gate.json"
STEP5D_V3_FORCE_SETTLE_TOLERANCE_N = 3.0
STEP5D_V3_FORCE_SETTLE_MAX_N = 12.0
STEP5D_V4_NORMAL_LOAD_MIN_N = 2.0
STEP5D_V4_NORMAL_LOAD_MAX_N = 15.0
STEP5D_V4_FORCE_NORM_MAX_N = 25.0
STEP5D_V6_NORMAL_LOAD_MIN_N = 2.0
STEP5D_V6_NORMAL_LOAD_MAX_N = 40.0
STEP5D_V6_FORCE_NORM_MAX_N = 45.0
STEP5D_V8_SETTLED_NORMAL_LOAD_MIN_N = 3.0
STEP5D_V8_SETTLED_NORMAL_LOAD_MAX_N = 8.0
STEP5D_V8_FORCE_NORM_MAX_N = 25.0
STEP5D_V8_RECOVERY_NORMAL_LOAD_MIN_N = 0.5
STEP5D_V8_RECOVERY_NORMAL_LOAD_MAX_N = 40.0
STEP5D_V8_RECOVERY_FORCE_NORM_STOP_N = 100.0
STEP5D_V10_SETTLE_FILTER_ALPHA = 0.15
STEP5D_V10_ADMITTANCE_MASS = 12.0
STEP5D_V10_ADMITTANCE_DAMPING = 300.0
STEP5D_V10_SETTLE_VELOCITY_READY_M_S = 0.001
STEP5D_V11_ENTRY_NORMAL_LOAD_MIN_N = 2.0
STEP5D_V11_ENTRY_NORMAL_LOAD_MAX_N = 15.0
STEP5D_V11_ENTRY_REQUIRED_S = 0.150
STEP5D_V11_ACQUIRE_FILTER_ALPHA = 0.15
STEP5D_V11_ACQUIRE_V_MAX_M_S = 0.0012
STEP5D_V11_ACQUIRE_SLEW_M_S2 = 0.012
STEP5D_V12_QDOT_LIMIT_RAD_S = 0.05
STEP5D_V12_QDOT_SLEW_RAD_S2 = 0.20
STEP5D_V12_GUARD_DT_MAX_S = 0.010
STEP5D_V12_LINE_CONTACT_LOW_STOP_N = 0.5
STEP5D_V12_LINE_CONTACT_MIN_N = 1.0
STEP5D_V12_LINE_CONTACT_MAX_N = 15.0
STEP5D_V12_LINE_FORCE_NORM_MAX_N = 25.0
STEP5D_V12_LINE_CONTACT_LOSS_LIMIT_S = 0.030
STEP5D_V12_LINE_TCP_SPEED_MAX_M_S = 0.050
STEP5D_V13_HARD_LOW_LOAD_N = 0.25
STEP5D_V13_SOFT_LOW_LOAD_N = 0.50
STEP5D_V13_VALID_CONTACT_MIN_N = 0.50
STEP5D_V13_VALID_CONTACT_MAX_N = 15.0
STEP5D_V13_FORCE_NORM_VALID_MAX_N = 25.0
STEP5D_V13_LOW_LOAD_SPEED_LOAD_N = 1.0
STEP5D_V13_LOW_LOAD_SPEED_STOP_M_S = 0.025
STEP5D_V13_ABSOLUTE_SPEED_STOP_M_S = 0.050
STEP5D_V13_ACTUAL_SPEED_DWELL_STOP_S = 0.004
STEP5D_V13_LOW_LOAD_HOLD_TIMEOUT_S = 0.300
STEP5D_V13_HIGH_WINDOW_DWELL_STOP_S = 0.030
STEP5D_V13_CONTACT_SAFETY_STATES = {
    "inactive": 0.0,
    "valid_contact": 1.0,
    "contact_uncertain_hold": 2.0,
    "danger_stop": 3.0,
}
STEP5D_V13_CONTACT_SAFETY_ACTIONS = {
    "inactive": 0.0,
    "pass_solver": 1.0,
    "hold_zero_qdot": 2.0,
    "stop_zero_qdot": 3.0,
}
STEP5D_V15A_TCP_CAGE_SOURCE_CSVS = [
    EXPERIMENT_ROOT / "runs" / "bridge_step5b_contact_cycloid_baseline_v1_20260612_082352" / "bridge_rtde_500hz.csv",
    EXPERIMENT_ROOT / "runs" / "bridge_step5b_contact_cycloid_baseline_v1_20260614_222309" / "bridge_rtde_500hz.csv",
    EXPERIMENT_ROOT / "runs" / "bridge_step6b_contact_eight_baseline_v1_20260612_223047" / "bridge_rtde_500hz.csv",
    EXPERIMENT_ROOT / "runs" / "bridge_step6b_contact_eight_baseline_v2_20260612_225841" / "bridge_rtde_500hz.csv",
    EXPERIMENT_ROOT / "runs" / "bridge_step6b_contact_eight_baseline_v2_20260614_223106" / "bridge_rtde_500hz.csv",
]
STEP5D_V15A_TCP_CAGE_PADDING_M = 0.020
STEP5D_V15A_TCP_CAGE_TAU_STOP_S = 0.100
STEP5D_V15A_TCP_CAGE_A_STOP_M_S2 = 1.000
STEP5D_V15A_TCP_CAGE_MODEL_MARGIN_M = 0.003
STEP5D_V15A_TCP_CAGE_CONTACT_MARGIN_M = 0.002
STEP5D_V15A_HOLD_CONSECUTIVE_MAX_S = 1.200
STEP5D_V15A_HOLD_EVENT_LIMIT = 360
STEP5D_V15A_HOLD_DUTY_MAX = 0.400
STEP5D_V15A_HOLD_DUTY_MIN_ACTIVE_S = 1.000
STEP5D_V15A_REPEATED_HOLD_LIMIT = 120
STEP5D_V15A_EARLY_ESCAPE_SPEED_HOLD_M_S = 0.0099
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


def _finite_csv_float(row: dict[str, str], key: str) -> float:
    value = row.get(key, "")
    if value == "":
        return math.nan
    try:
        return float(value)
    except ValueError:
        return math.nan


def _csv_stage25_active(row: dict[str, str]) -> bool:
    stage = _finite_csv_float(row, "ur_output_double_register_35")
    if math.isfinite(stage) and abs(stage - 25.0) < 0.05:
        return True
    return _finite_csv_float(row, "step4e_cmd_valid") > 0.5


class Step5dTcpCage:
    def __init__(
        self,
        *,
        min_xyz: tuple[float, float, float],
        max_xyz: tuple[float, float, float],
        source_rows: int,
        source_csvs: list[str],
        padding_m: float,
    ) -> None:
        self.min_xyz = min_xyz
        self.max_xyz = max_xyz
        self.source_rows = int(source_rows)
        self.source_csvs = source_csvs
        self.padding_m = float(padding_m)
        self.mode = "broad_stagewise_aabb_from_success_step5b_step6b"

    def evaluate(
        self,
        pose: list[float] | tuple[float, ...],
        *,
        actual_tcp_speed_m_s: float,
        predicted_tcp_speed_m_s: float | None,
    ) -> dict[str, float | str]:
        if len(pose) < 3:
            return self._invalid("missing_tcp_pose")
        xyz = tuple(float(pose[idx]) for idx in range(3))
        actual_speed = float(actual_tcp_speed_m_s)
        predicted_speed = float(predicted_tcp_speed_m_s) if predicted_tcp_speed_m_s is not None else math.nan
        if not all(math.isfinite(value) for value in xyz) or not math.isfinite(actual_speed):
            return self._invalid("nonfinite_tcp_cage_input")
        speed_for_braking = actual_speed
        if math.isfinite(predicted_speed):
            speed_for_braking = max(speed_for_braking, predicted_speed)
        signed_distance = min(
            xyz[0] - self.min_xyz[0],
            self.max_xyz[0] - xyz[0],
            xyz[1] - self.min_xyz[1],
            self.max_xyz[1] - xyz[1],
            xyz[2] - self.min_xyz[2],
            self.max_xyz[2] - xyz[2],
        )
        braking_distance = (
            speed_for_braking * STEP5D_V15A_TCP_CAGE_TAU_STOP_S
            + speed_for_braking * speed_for_braking / (2.0 * STEP5D_V15A_TCP_CAGE_A_STOP_M_S2)
            + STEP5D_V15A_TCP_CAGE_MODEL_MARGIN_M
            + STEP5D_V15A_TCP_CAGE_CONTACT_MARGIN_M
        )
        braking_margin = signed_distance - braking_distance
        if signed_distance <= 0.0:
            reason = "outside_broad_tcp_cage"
        elif braking_margin <= 0.0:
            reason = "tcp_cage_braking_margin_exhausted"
        else:
            reason = "inside_broad_tcp_cage"
        return {
            "distance_m": max(0.0, signed_distance),
            "signed_distance_m": signed_distance,
            "braking_margin_m": braking_margin,
            "cell_index": 0.0,
            "reason": reason,
        }

    @staticmethod
    def _invalid(reason: str) -> dict[str, float | str]:
        return {
            "distance_m": math.nan,
            "signed_distance_m": math.nan,
            "braking_margin_m": math.nan,
            "cell_index": -1.0,
            "reason": reason,
        }


def build_step5d_v15a_tcp_cage(source_csvs: list[Path] | None = None) -> Step5dTcpCage:
    paths = source_csvs or STEP5D_V15A_TCP_CAGE_SOURCE_CSVS
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    used_paths: list[str] = []
    for path in paths:
        with path.open(newline="", encoding="utf-8") as handle:
            used = False
            for row in csv.DictReader(handle):
                if not _csv_stage25_active(row):
                    continue
                x = _finite_csv_float(row, "ur_actual_TCP_pose_0")
                y = _finite_csv_float(row, "ur_actual_TCP_pose_1")
                z = _finite_csv_float(row, "ur_actual_TCP_pose_2")
                if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
                    continue
                xs.append(x)
                ys.append(y)
                zs.append(z)
                used = True
            if used:
                used_paths.append(str(path))
    if not xs:
        raise RuntimeError("Step5d v15a TCP cage construction found no finite Stage25 source poses")
    padding = STEP5D_V15A_TCP_CAGE_PADDING_M
    return Step5dTcpCage(
        min_xyz=(min(xs) - padding, min(ys) - padding, min(zs) - padding),
        max_xyz=(max(xs) + padding, max(ys) + padding, max(zs) + padding),
        source_rows=len(xs),
        source_csvs=used_paths,
        padding_m=padding,
    )


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


def skew3_np(vector: np.ndarray) -> np.ndarray:
    x, y, z = vector
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=float)


def step5d_tcp_jacobian_base(
    model_bundle: step5d_kin.CalibratedModel,
    q: np.ndarray,
    tcp_offset_tool0: np.ndarray,
) -> np.ndarray:
    model = model_bundle.model
    data = model_bundle.data
    pin.forwardKinematics(model, data, q)
    pin.computeJointJacobians(model, data, q)
    pin.updateFramePlacements(model, data)
    tool_jac_world_aligned = pin.getFrameJacobian(
        model,
        data,
        model_bundle.tool0_frame_id,
        pin.ReferenceFrame.LOCAL_WORLD_ALIGNED,
    )
    base_world_rotation = data.oMf[model_bundle.base_frame_id].rotation.T
    tool_linear_base_jac = base_world_rotation @ tool_jac_world_aligned[:3, :]
    tool_angular_base_jac = base_world_rotation @ tool_jac_world_aligned[3:, :]
    tool0_base = data.oMf[model_bundle.base_frame_id].inverse() * data.oMf[model_bundle.tool0_frame_id]
    tcp_lever_base = tool0_base.rotation @ tcp_offset_tool0
    tcp_linear_base_jac = tool_linear_base_jac - skew3_np(tcp_lever_base) @ tool_angular_base_jac
    return np.vstack((tcp_linear_base_jac, tool_angular_base_jac))


def step5d_omega_bounds(
    q: np.ndarray,
    q_min: np.ndarray,
    q_max: np.ndarray,
    *,
    alpha_s_inv: float,
    qdot_limit_rad_s: float,
) -> tuple[np.ndarray, np.ndarray]:
    lower = np.maximum(alpha_s_inv * (q_min - q), -qdot_limit_rad_s)
    upper = np.minimum(qdot_limit_rad_s, alpha_s_inv * (q_max - q))
    if np.any(lower > upper):
        raise ValueError("Step5d omega bounds are inverted")
    return lower, upper


def step5d_force_settle_ready(
    *,
    normal_load_n: float,
    force_norm_n: float,
    target_force_n: float,
    tolerance_n: float = STEP5D_V3_FORCE_SETTLE_TOLERANCE_N,
    max_force_norm_n: float = STEP5D_V3_FORCE_SETTLE_MAX_N,
) -> bool:
    values = [normal_load_n, force_norm_n, target_force_n, tolerance_n, max_force_norm_n]
    if any(not math.isfinite(float(value)) for value in values):
        return False
    if tolerance_n < 0.0 or max_force_norm_n <= 0.0:
        return False
    return abs(float(normal_load_n) - float(target_force_n)) <= float(tolerance_n) and float(force_norm_n) <= float(max_force_norm_n)


def step5d_contact_window_ready(
    *,
    normal_load_n: float,
    force_norm_n: float,
    min_normal_load_n: float = STEP5D_V4_NORMAL_LOAD_MIN_N,
    max_normal_load_n: float = STEP5D_V4_NORMAL_LOAD_MAX_N,
    max_force_norm_n: float = STEP5D_V4_FORCE_NORM_MAX_N,
) -> bool:
    values = [normal_load_n, force_norm_n, min_normal_load_n, max_normal_load_n, max_force_norm_n]
    if any(not math.isfinite(float(value)) for value in values):
        return False
    if min_normal_load_n < 0.0 or max_normal_load_n < min_normal_load_n or max_force_norm_n <= 0.0:
        return False
    load = float(normal_load_n)
    return float(min_normal_load_n) <= load <= float(max_normal_load_n) and float(force_norm_n) <= float(max_force_norm_n)


def step5d_v8_recovery_window_ok(
    *,
    normal_load_n: float,
    force_norm_n: float,
    min_normal_load_n: float = STEP5D_V8_RECOVERY_NORMAL_LOAD_MIN_N,
    max_normal_load_n: float = STEP5D_V8_RECOVERY_NORMAL_LOAD_MAX_N,
    hard_force_norm_n: float = STEP5D_V8_RECOVERY_FORCE_NORM_STOP_N,
) -> bool:
    values = [normal_load_n, force_norm_n, min_normal_load_n, max_normal_load_n, hard_force_norm_n]
    if any(not math.isfinite(float(value)) for value in values):
        return False
    if min_normal_load_n < 0.0 or max_normal_load_n < min_normal_load_n or hard_force_norm_n <= 0.0:
        return False
    load = float(normal_load_n)
    return (
        float(min_normal_load_n) <= load <= float(max_normal_load_n)
        and float(force_norm_n) <= float(hard_force_norm_n)
    )


def step5d_v9_recovery_window_ok(
    *,
    normal_load_n: float,
    force_norm_n: float,
    max_normal_load_n: float = STEP5D_V8_RECOVERY_NORMAL_LOAD_MAX_N,
    hard_force_norm_n: float = STEP5D_V8_RECOVERY_FORCE_NORM_STOP_N,
) -> bool:
    values = [normal_load_n, force_norm_n, max_normal_load_n, hard_force_norm_n]
    if any(not math.isfinite(float(value)) for value in values):
        return False
    if max_normal_load_n <= 0.0 or hard_force_norm_n <= 0.0:
        return False
    return float(normal_load_n) <= float(max_normal_load_n) and float(force_norm_n) <= float(hard_force_norm_n)


def step5d_v8_force_pid_settle_velocity(
    *,
    normal_load_n: float,
    target_force_n: float,
    integral_error_n_s: float,
    dt_s: float,
    reaction_normal_b: tuple[float, float, float],
    normal_velocity_m_s: float,
    kp_m_s_per_n: float,
    ki_m_s_per_n_s: float,
    damping: float,
    integral_limit_n_s: float,
    v_press_max_m_s: float,
    v_unload_max_m_s: float,
) -> tuple[tuple[float, float, float], float, float]:
    values = [
        normal_load_n,
        target_force_n,
        integral_error_n_s,
        dt_s,
        normal_velocity_m_s,
        kp_m_s_per_n,
        ki_m_s_per_n_s,
        damping,
        integral_limit_n_s,
        v_press_max_m_s,
        v_unload_max_m_s,
        *reaction_normal_b,
    ]
    if any(not math.isfinite(float(value)) for value in values):
        raise ValueError("Step5d v8 PID settle inputs must be finite")
    if dt_s < 0.0 or integral_limit_n_s < 0.0 or v_press_max_m_s <= 0.0 or v_unload_max_m_s <= 0.0:
        raise ValueError("Step5d v8 PID settle limits must be positive")
    reaction = normalize3(reaction_normal_b)
    approach = (-reaction[0], -reaction[1], -reaction[2])
    error_n = float(target_force_n) - float(normal_load_n)
    integral = clamp(
        float(integral_error_n_s) + error_n * max(0.0, float(dt_s)),
        -float(integral_limit_n_s),
        float(integral_limit_n_s),
    )
    v_normal = (
        float(kp_m_s_per_n) * error_n
        + float(ki_m_s_per_n_s) * integral
        - float(damping) * float(normal_velocity_m_s)
    )
    v_normal = clamp(v_normal, -float(v_unload_max_m_s), float(v_press_max_m_s))
    cmd = tuple(approach[idx] * v_normal for idx in range(3))
    return cmd, integral, v_normal


def step5d_v10_admittance_settle_velocity(
    *,
    normal_load_n: float,
    filtered_normal_load_n: float | None,
    target_force_n: float,
    settle_velocity_m_s: float,
    dt_s: float,
    reaction_normal_b: tuple[float, float, float],
    alpha: float = STEP5D_V10_SETTLE_FILTER_ALPHA,
    mass: float = STEP5D_V10_ADMITTANCE_MASS,
    damping: float = STEP5D_V10_ADMITTANCE_DAMPING,
    v_max_m_s: float = 0.003,
) -> tuple[tuple[float, float, float], float, float]:
    values = [
        normal_load_n,
        target_force_n,
        settle_velocity_m_s,
        dt_s,
        alpha,
        mass,
        damping,
        v_max_m_s,
        *reaction_normal_b,
    ]
    if filtered_normal_load_n is not None:
        values.append(filtered_normal_load_n)
    if any(not math.isfinite(float(value)) for value in values):
        raise ValueError("Step5d v10 admittance settle inputs must be finite")
    if dt_s < 0.0 or mass <= 0.0 or damping < 0.0 or v_max_m_s <= 0.0:
        raise ValueError("Step5d v10 admittance settle limits must be positive")
    reaction = normalize3(reaction_normal_b)
    approach = (-reaction[0], -reaction[1], -reaction[2])
    alpha = clamp(float(alpha), 0.0, 1.0)
    load = float(normal_load_n)
    filtered_load = load if filtered_normal_load_n is None else (1.0 - alpha) * float(filtered_normal_load_n) + alpha * load
    error_n = float(target_force_n) - filtered_load
    accel_m_s2 = (error_n - float(damping) * float(settle_velocity_m_s)) / float(mass)
    v_next = clamp(
        float(settle_velocity_m_s) + accel_m_s2 * max(0.0, float(dt_s)),
        -float(v_max_m_s),
        float(v_max_m_s),
    )
    cmd = tuple(approach[idx] * v_next for idx in range(3))
    return cmd, filtered_load, v_next


def step5d_v11_deadband_acquire_velocity(
    *,
    normal_load_n: float,
    filtered_normal_load_n: float | None,
    settle_velocity_m_s: float,
    dt_s: float,
    reaction_normal_b: tuple[float, float, float],
    min_normal_load_n: float = STEP5D_V11_ENTRY_NORMAL_LOAD_MIN_N,
    max_normal_load_n: float = STEP5D_V11_ENTRY_NORMAL_LOAD_MAX_N,
    alpha: float = STEP5D_V11_ACQUIRE_FILTER_ALPHA,
    v_max_m_s: float = STEP5D_V11_ACQUIRE_V_MAX_M_S,
    slew_m_s2: float = STEP5D_V11_ACQUIRE_SLEW_M_S2,
) -> tuple[tuple[float, float, float], float, float]:
    values = [
        normal_load_n,
        settle_velocity_m_s,
        dt_s,
        min_normal_load_n,
        max_normal_load_n,
        alpha,
        v_max_m_s,
        slew_m_s2,
        *reaction_normal_b,
    ]
    if filtered_normal_load_n is not None:
        values.append(filtered_normal_load_n)
    if any(not math.isfinite(float(value)) for value in values):
        raise ValueError("Step5d v11 deadband acquire inputs must be finite")
    if (
        dt_s < 0.0
        or min_normal_load_n < 0.0
        or max_normal_load_n < min_normal_load_n
        or v_max_m_s <= 0.0
        or slew_m_s2 <= 0.0
    ):
        raise ValueError("Step5d v11 deadband acquire limits must be positive")
    reaction = normalize3(reaction_normal_b)
    approach = (-reaction[0], -reaction[1], -reaction[2])
    alpha = clamp(float(alpha), 0.0, 1.0)
    load = float(normal_load_n)
    filtered_load = load if filtered_normal_load_n is None else (1.0 - alpha) * float(filtered_normal_load_n) + alpha * load
    if filtered_load < float(min_normal_load_n):
        target_v_m_s = float(v_max_m_s)
    elif filtered_load > float(max_normal_load_n):
        target_v_m_s = -float(v_max_m_s)
    else:
        target_v_m_s = 0.0
    dv_limit = float(slew_m_s2) * max(0.0, float(dt_s))
    v_next = clamp(
        float(settle_velocity_m_s) + clamp(target_v_m_s - float(settle_velocity_m_s), -dv_limit, dv_limit),
        -float(v_max_m_s),
        float(v_max_m_s),
    )
    cmd = tuple(approach[idx] * v_next for idx in range(3))
    return cmd, filtered_load, v_next


def step5d_liveprep_contact_window_limits(bridge_profile: str) -> tuple[float, float, float]:
    if bridge_profile == STEP5D_LIVEPREP_V6_STAGE_ID:
        return (STEP5D_V6_NORMAL_LOAD_MIN_N, STEP5D_V6_NORMAL_LOAD_MAX_N, STEP5D_V6_FORCE_NORM_MAX_N)
    if bridge_profile in {
        STEP5D_LIVEPREP_V12_STAGE_ID,
        STEP5D_LIVEPREP_V13_STAGE_ID,
        STEP5D_LIVEPREP_STAGE_ID,
        STEP5D_LIVEPREP_V15_STAGE_ID,
        STEP5D_LIVEPREP_V15A_STAGE_ID,
    }:
        return (
            STEP5D_V11_ENTRY_NORMAL_LOAD_MIN_N,
            STEP5D_V11_ENTRY_NORMAL_LOAD_MAX_N,
            STEP5D_V8_FORCE_NORM_MAX_N,
        )
    if bridge_profile == STEP5D_LIVEPREP_V11_STAGE_ID:
        return (
            STEP5D_V11_ENTRY_NORMAL_LOAD_MIN_N,
            STEP5D_V11_ENTRY_NORMAL_LOAD_MAX_N,
            STEP5D_V8_FORCE_NORM_MAX_N,
        )
    if bridge_profile in {STEP5D_LIVEPREP_V8_STAGE_ID, STEP5D_LIVEPREP_V9_STAGE_ID, STEP5D_LIVEPREP_V10_STAGE_ID}:
        return (
            STEP5D_V8_SETTLED_NORMAL_LOAD_MIN_N,
            STEP5D_V8_SETTLED_NORMAL_LOAD_MAX_N,
            STEP5D_V8_FORCE_NORM_MAX_N,
        )
    return (STEP5D_V4_NORMAL_LOAD_MIN_N, STEP5D_V4_NORMAL_LOAD_MAX_N, STEP5D_V4_FORCE_NORM_MAX_N)


def step5d_v12_line_guard(
    *,
    normal_load_n: float,
    force_norm_n: float,
    tcp_linear_speed_m_s: float,
    prior_loss_s: float,
    dt_s: float,
    low_stop_n: float = STEP5D_V12_LINE_CONTACT_LOW_STOP_N,
    min_normal_load_n: float = STEP5D_V12_LINE_CONTACT_MIN_N,
    max_normal_load_n: float = STEP5D_V12_LINE_CONTACT_MAX_N,
    max_force_norm_n: float = STEP5D_V12_LINE_FORCE_NORM_MAX_N,
    loss_limit_s: float = STEP5D_V12_LINE_CONTACT_LOSS_LIMIT_S,
    max_tcp_speed_m_s: float = STEP5D_V12_LINE_TCP_SPEED_MAX_M_S,
    dt_max_s: float = STEP5D_V12_GUARD_DT_MAX_S,
) -> tuple[bool, float, str]:
    values = [
        normal_load_n,
        force_norm_n,
        tcp_linear_speed_m_s,
        prior_loss_s,
        dt_s,
        low_stop_n,
        min_normal_load_n,
        max_normal_load_n,
        max_force_norm_n,
        loss_limit_s,
        max_tcp_speed_m_s,
        dt_max_s,
    ]
    if any(not math.isfinite(float(value)) for value in values):
        return False, float(prior_loss_s), "nonfinite_line_guard_input"
    if (
        low_stop_n < 0.0
        or min_normal_load_n < low_stop_n
        or max_normal_load_n < min_normal_load_n
        or max_force_norm_n <= 0.0
        or loss_limit_s < 0.0
        or max_tcp_speed_m_s <= 0.0
        or dt_max_s <= 0.0
    ):
        raise ValueError("Step5d v12 line guard limits are inconsistent")
    if float(tcp_linear_speed_m_s) > float(max_tcp_speed_m_s):
        return False, float(prior_loss_s), "tcp_speed_watchdog"
    if float(normal_load_n) < float(low_stop_n):
        return False, float(prior_loss_s), "lost_contact_low_load"
    in_window = (
        float(min_normal_load_n) <= float(normal_load_n) <= float(max_normal_load_n)
        and float(force_norm_n) <= float(max_force_norm_n)
    )
    if in_window:
        return True, 0.0, "ok"
    safe_dt_s = min(max(0.0, float(dt_s)), float(dt_max_s))
    loss_s = max(0.0, float(prior_loss_s)) + safe_dt_s
    if loss_s >= float(loss_limit_s):
        return False, loss_s, "contact_window_timeout"
    return True, loss_s, "contact_window_dwell"


def step5d_v13_contact_safety_guard(
    *,
    normal_load_n: float,
    force_norm_n: float,
    actual_tcp_speed_m_s: float,
    predicted_tcp_speed_m_s: float | None,
    prior_hold_s: float,
    prior_high_window_s: float,
    prior_actual_speed_violation_s: float,
    prior_actual_speed_violation_count: int = 0,
    dt_s: float,
    hard_low_load_n: float = STEP5D_V13_HARD_LOW_LOAD_N,
    soft_low_load_n: float = STEP5D_V13_SOFT_LOW_LOAD_N,
    valid_contact_min_n: float = STEP5D_V13_VALID_CONTACT_MIN_N,
    valid_contact_max_n: float = STEP5D_V13_VALID_CONTACT_MAX_N,
    force_norm_valid_max_n: float = STEP5D_V13_FORCE_NORM_VALID_MAX_N,
    low_load_speed_load_n: float = STEP5D_V13_LOW_LOAD_SPEED_LOAD_N,
    low_load_speed_stop_m_s: float = STEP5D_V13_LOW_LOAD_SPEED_STOP_M_S,
    absolute_speed_stop_m_s: float = STEP5D_V13_ABSOLUTE_SPEED_STOP_M_S,
    actual_speed_dwell_stop_s: float = STEP5D_V13_ACTUAL_SPEED_DWELL_STOP_S,
    hold_timeout_s: float = STEP5D_V13_LOW_LOAD_HOLD_TIMEOUT_S,
    high_window_dwell_stop_s: float = STEP5D_V13_HIGH_WINDOW_DWELL_STOP_S,
    dt_max_s: float = STEP5D_V12_GUARD_DT_MAX_S,
    advance_actual_speed_dwell: bool = True,
) -> dict[str, float | str]:
    values = [
        normal_load_n,
        force_norm_n,
        actual_tcp_speed_m_s,
        prior_hold_s,
        prior_high_window_s,
        prior_actual_speed_violation_s,
        dt_s,
        hard_low_load_n,
        soft_low_load_n,
        valid_contact_min_n,
        valid_contact_max_n,
        force_norm_valid_max_n,
        low_load_speed_load_n,
        low_load_speed_stop_m_s,
        absolute_speed_stop_m_s,
        actual_speed_dwell_stop_s,
        hold_timeout_s,
        high_window_dwell_stop_s,
        dt_max_s,
    ]
    if any(not math.isfinite(float(value)) for value in values):
        return {
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "nonfinite_contact_safety_input",
            "hold_s": float(prior_hold_s),
            "high_window_s": float(prior_high_window_s),
            "actual_speed_violation_s": float(prior_actual_speed_violation_s),
            "actual_speed_violation_count": int(prior_actual_speed_violation_count),
        }
    if predicted_tcp_speed_m_s is not None and not math.isfinite(float(predicted_tcp_speed_m_s)):
        return {
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "nonfinite_predicted_tcp_speed",
            "hold_s": float(prior_hold_s),
            "high_window_s": float(prior_high_window_s),
            "actual_speed_violation_s": float(prior_actual_speed_violation_s),
            "actual_speed_violation_count": int(prior_actual_speed_violation_count),
        }
    if (
        hard_low_load_n < 0.0
        or soft_low_load_n < hard_low_load_n
        or valid_contact_min_n < soft_low_load_n
        or valid_contact_max_n < valid_contact_min_n
        or force_norm_valid_max_n <= 0.0
        or low_load_speed_load_n < valid_contact_min_n
        or low_load_speed_stop_m_s <= 0.0
        or absolute_speed_stop_m_s < low_load_speed_stop_m_s
        or actual_speed_dwell_stop_s <= 0.0
        or hold_timeout_s < 0.0
        or high_window_dwell_stop_s < 0.0
        or dt_max_s <= 0.0
    ):
        raise ValueError("Step5d v13 contact-safety limits are inconsistent")

    safe_dt_s = min(max(0.0, float(dt_s)), float(dt_max_s))
    actual_violation = (
        float(actual_tcp_speed_m_s) > float(absolute_speed_stop_m_s)
        or (
            float(normal_load_n) < float(low_load_speed_load_n)
            and float(actual_tcp_speed_m_s) > float(low_load_speed_stop_m_s)
        )
    )
    actual_speed_violation_s = (
        max(0.0, float(prior_actual_speed_violation_s)) + safe_dt_s
        if actual_violation and advance_actual_speed_dwell
        else max(0.0, float(prior_actual_speed_violation_s))
        if actual_violation
        else 0.0
    )
    actual_speed_violation_count = (
        max(0, int(prior_actual_speed_violation_count)) + 1
        if actual_violation and advance_actual_speed_dwell
        else max(0, int(prior_actual_speed_violation_count))
        if actual_violation
        else 0
    )
    if actual_violation and actual_speed_violation_s >= float(actual_speed_dwell_stop_s):
        reason = (
            "actual_tcp_speed_watchdog_dwell"
            if float(actual_tcp_speed_m_s) > float(absolute_speed_stop_m_s)
            else "low_load_actual_tcp_speed_watchdog_dwell"
        )
        return {
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": reason,
            "hold_s": float(prior_hold_s),
            "high_window_s": float(prior_high_window_s),
            "actual_speed_violation_s": actual_speed_violation_s,
            "actual_speed_violation_count": actual_speed_violation_count,
        }

    if predicted_tcp_speed_m_s is not None:
        predicted_speed_m_s = float(predicted_tcp_speed_m_s)
        if predicted_speed_m_s > float(absolute_speed_stop_m_s):
            return {
                "state": "danger_stop",
                "action": "stop_zero_qdot",
                "reason": "predicted_tcp_speed_watchdog",
                "hold_s": float(prior_hold_s),
                "high_window_s": float(prior_high_window_s),
                "actual_speed_violation_s": actual_speed_violation_s,
                "actual_speed_violation_count": actual_speed_violation_count,
            }
        if float(normal_load_n) < float(low_load_speed_load_n) and predicted_speed_m_s > float(low_load_speed_stop_m_s):
            return {
                "state": "danger_stop",
                "action": "stop_zero_qdot",
                "reason": "low_load_predicted_tcp_speed_watchdog",
                "hold_s": float(prior_hold_s),
                "high_window_s": float(prior_high_window_s),
                "actual_speed_violation_s": actual_speed_violation_s,
                "actual_speed_violation_count": actual_speed_violation_count,
            }

    if actual_violation:
        reason = (
            "actual_tcp_speed_watchdog_dwell_hold"
            if float(actual_tcp_speed_m_s) > float(absolute_speed_stop_m_s)
            else "low_load_actual_tcp_speed_watchdog_dwell_hold"
        )
        return {
            "state": "contact_uncertain_hold",
            "action": "hold_zero_qdot",
            "reason": reason,
            "hold_s": float(prior_hold_s),
            "high_window_s": float(prior_high_window_s),
            "actual_speed_violation_s": actual_speed_violation_s,
            "actual_speed_violation_count": actual_speed_violation_count,
        }

    if float(normal_load_n) < float(soft_low_load_n):
        hold_s = max(0.0, float(prior_hold_s)) + safe_dt_s
        if hold_s >= float(hold_timeout_s):
            return {
                "state": "danger_stop",
                "action": "stop_zero_qdot",
                "reason": "low_load_hold_timeout",
                "hold_s": hold_s,
                "high_window_s": 0.0,
                "actual_speed_violation_s": actual_speed_violation_s,
                "actual_speed_violation_count": actual_speed_violation_count,
            }
        reason = "hard_lost_contact_hold" if float(normal_load_n) < float(hard_low_load_n) else "soft_low_contact_hold"
        return {
            "state": "contact_uncertain_hold",
            "action": "hold_zero_qdot",
            "reason": reason,
            "hold_s": hold_s,
            "high_window_s": 0.0,
            "actual_speed_violation_s": actual_speed_violation_s,
            "actual_speed_violation_count": actual_speed_violation_count,
        }

    if float(normal_load_n) > float(valid_contact_max_n) or float(force_norm_n) > float(force_norm_valid_max_n):
        high_window_s = max(0.0, float(prior_high_window_s)) + safe_dt_s
        if high_window_s >= float(high_window_dwell_stop_s):
            return {
                "state": "danger_stop",
                "action": "stop_zero_qdot",
                "reason": "high_contact_window_dwell_stop",
                "hold_s": 0.0,
                "high_window_s": high_window_s,
                "actual_speed_violation_s": actual_speed_violation_s,
                "actual_speed_violation_count": actual_speed_violation_count,
            }
        return {
            "state": "contact_uncertain_hold",
            "action": "hold_zero_qdot",
            "reason": "high_contact_window_dwell",
            "hold_s": 0.0,
            "high_window_s": high_window_s,
            "actual_speed_violation_s": actual_speed_violation_s,
            "actual_speed_violation_count": actual_speed_violation_count,
        }

    return {
        "state": "valid_contact",
        "action": "pass_solver",
        "reason": "ok",
        "hold_s": 0.0,
        "high_window_s": 0.0,
        "actual_speed_violation_s": actual_speed_violation_s,
        "actual_speed_violation_count": actual_speed_violation_count,
    }


def step5d_v15_permissive_recovery_guard(
    *,
    braking_margin_m: float | None = None,
    require_braking_margin: bool = False,
    semantic_gate_ok: bool = True,
    repeated_hold_count: int = 0,
    repeated_hold_limit: int = STEP5D_V15A_REPEATED_HOLD_LIMIT,
    prior_consecutive_hold_s: float = 0.0,
    prior_total_hold_s: float = 0.0,
    prior_hold_event_count: int = 0,
    prior_last_hold_reason: str = "",
    prior_hold_actual_tcp_speed_m_s: float | None = None,
    active_stage25_s: float = 0.0,
    hold_consecutive_max_s: float = STEP5D_V15A_HOLD_CONSECUTIVE_MAX_S,
    hold_event_limit: int = STEP5D_V15A_HOLD_EVENT_LIMIT,
    hold_duty_max: float = STEP5D_V15A_HOLD_DUTY_MAX,
    hold_duty_min_active_s: float = STEP5D_V15A_HOLD_DUTY_MIN_ACTIVE_S,
    force_norm_hard_stop_n: float = 60.0,
    **kwargs: Any,
) -> dict[str, float | str]:
    safe_dt_s = min(
        max(0.0, float(kwargs.get("dt_s", 0.0))),
        float(kwargs.get("dt_max_s", STEP5D_V12_GUARD_DT_MAX_S)),
    )

    def finalize(result: dict[str, float | str]) -> dict[str, float | str]:
        action = str(result["action"])
        reason = str(result["reason"])
        total_hold_s = max(0.0, float(prior_total_hold_s))
        consecutive_hold_s = max(0.0, float(prior_consecutive_hold_s))
        hold_event_count = max(0, int(prior_hold_event_count))
        updated_repeated_hold_count = max(0, int(repeated_hold_count))
        last_hold_reason = str(prior_last_hold_reason)
        hold_actual_tcp_speed_m_s = float(kwargs.get("actual_tcp_speed_m_s", math.nan))
        if action == "hold_zero_qdot":
            same_reason = consecutive_hold_s > 0.0 and last_hold_reason == reason
            if same_reason:
                consecutive_hold_s += safe_dt_s
                prior_speed = (
                    float(prior_hold_actual_tcp_speed_m_s)
                    if prior_hold_actual_tcp_speed_m_s is not None
                    else math.nan
                )
                speed_not_improving = (
                    math.isfinite(prior_speed)
                    and math.isfinite(hold_actual_tcp_speed_m_s)
                    and hold_actual_tcp_speed_m_s >= prior_speed - 1e-4
                )
                updated_repeated_hold_count = updated_repeated_hold_count + 1 if speed_not_improving else 0
            else:
                consecutive_hold_s = safe_dt_s
                hold_event_count += 1
                updated_repeated_hold_count = 0
            total_hold_s += safe_dt_s
            last_hold_reason = reason
        elif action == "pass_solver":
            consecutive_hold_s = 0.0
            updated_repeated_hold_count = 0
            last_hold_reason = ""
        active_s = max(float(active_stage25_s), safe_dt_s)
        hold_duty = total_hold_s / active_s if active_s > 0.0 else 0.0
        result.update(
            {
                "consecutive_hold_s": consecutive_hold_s,
                "total_hold_s": total_hold_s,
                "hold_event_count": float(hold_event_count),
                "hold_duty": hold_duty,
                "repeated_hold_count": float(updated_repeated_hold_count),
                "last_hold_reason": last_hold_reason,
                "hold_actual_tcp_speed_m_s": hold_actual_tcp_speed_m_s,
            }
        )
        if action == "hold_zero_qdot":
            bounded_stop_reason = ""
            if consecutive_hold_s > float(hold_consecutive_max_s):
                bounded_stop_reason = "hold_consecutive_limit"
            elif hold_event_count > int(hold_event_limit):
                bounded_stop_reason = "hold_event_limit"
            elif active_s >= float(hold_duty_min_active_s) and hold_duty > float(hold_duty_max):
                bounded_stop_reason = "hold_duty_limit"
            elif updated_repeated_hold_count > int(repeated_hold_limit):
                bounded_stop_reason = "repeated_hold_limit"
            if bounded_stop_reason:
                result.update(
                    {
                        "state": "danger_stop",
                        "action": "stop_zero_qdot",
                        "reason": bounded_stop_reason,
                    }
                )
        return result

    if require_braking_margin and braking_margin_m is None:
        return finalize(
            {
                "state": "danger_stop",
                "action": "stop_zero_qdot",
                "reason": "missing_tcp_cage_braking_margin",
                "hold_s": float(kwargs.get("prior_hold_s", 0.0)),
                "high_window_s": float(kwargs.get("prior_high_window_s", 0.0)),
                "actual_speed_violation_s": float(kwargs.get("prior_actual_speed_violation_s", 0.0)),
                "actual_speed_violation_count": int(kwargs.get("prior_actual_speed_violation_count", 0)),
            }
        )
    if braking_margin_m is not None:
        if not math.isfinite(float(braking_margin_m)):
            return finalize({
                "state": "danger_stop",
                "action": "stop_zero_qdot",
                "reason": "nonfinite_tcp_cage_braking_margin",
                "hold_s": float(kwargs.get("prior_hold_s", 0.0)),
                "high_window_s": float(kwargs.get("prior_high_window_s", 0.0)),
                "actual_speed_violation_s": float(kwargs.get("prior_actual_speed_violation_s", 0.0)),
                "actual_speed_violation_count": int(kwargs.get("prior_actual_speed_violation_count", 0)),
            })
        if float(braking_margin_m) <= 0.0:
            return finalize({
                "state": "danger_stop",
                "action": "stop_zero_qdot",
                "reason": "tcp_cage_braking_margin_exhausted",
                "hold_s": float(kwargs.get("prior_hold_s", 0.0)),
                "high_window_s": float(kwargs.get("prior_high_window_s", 0.0)),
                "actual_speed_violation_s": float(kwargs.get("prior_actual_speed_violation_s", 0.0)),
                "actual_speed_violation_count": int(kwargs.get("prior_actual_speed_violation_count", 0)),
            })
    if not semantic_gate_ok:
        return finalize({
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "semantic_gate_failed",
            "hold_s": float(kwargs.get("prior_hold_s", 0.0)),
            "high_window_s": float(kwargs.get("prior_high_window_s", 0.0)),
            "actual_speed_violation_s": float(kwargs.get("prior_actual_speed_violation_s", 0.0)),
            "actual_speed_violation_count": int(kwargs.get("prior_actual_speed_violation_count", 0)),
        })
    if int(repeated_hold_count) > int(repeated_hold_limit):
        return finalize({
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "repeated_hold_limit",
            "hold_s": float(kwargs.get("prior_hold_s", 0.0)),
            "high_window_s": float(kwargs.get("prior_high_window_s", 0.0)),
            "actual_speed_violation_s": float(kwargs.get("prior_actual_speed_violation_s", 0.0)),
            "actual_speed_violation_count": int(kwargs.get("prior_actual_speed_violation_count", 0)),
        })
    force_norm_n = float(kwargs.get("force_norm_n", 0.0))
    if not math.isfinite(force_norm_n) or force_norm_n >= float(force_norm_hard_stop_n):
        return finalize({
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "force_norm_hard_stop",
            "hold_s": float(kwargs.get("prior_hold_s", 0.0)),
            "high_window_s": float(kwargs.get("prior_high_window_s", 0.0)),
            "actual_speed_violation_s": float(kwargs.get("prior_actual_speed_violation_s", 0.0)),
                "actual_speed_violation_count": int(kwargs.get("prior_actual_speed_violation_count", 0)),
        })
    actual_tcp_speed_m_s = float(kwargs.get("actual_tcp_speed_m_s", math.nan))
    early_escape_speed_hold_m_s = float(
        kwargs.get("early_escape_speed_hold_m_s", STEP5D_V15A_EARLY_ESCAPE_SPEED_HOLD_M_S)
    )
    if (
        math.isfinite(actual_tcp_speed_m_s)
        and math.isfinite(early_escape_speed_hold_m_s)
        and actual_tcp_speed_m_s > early_escape_speed_hold_m_s
    ):
        hold_s = max(0.0, float(kwargs.get("prior_hold_s", 0.0))) + safe_dt_s
        return finalize({
            "state": "contact_uncertain_hold",
            "action": "hold_zero_qdot",
            "reason": "early_tcp_escape_recoverable_hold",
            "hold_s": hold_s,
            "high_window_s": float(kwargs.get("prior_high_window_s", 0.0)),
            "actual_speed_violation_s": float(kwargs.get("prior_actual_speed_violation_s", 0.0)),
            "actual_speed_violation_count": int(kwargs.get("prior_actual_speed_violation_count", 0)),
        })
    predicted_tcp_speed_m_s = kwargs.get("predicted_tcp_speed_m_s")
    if predicted_tcp_speed_m_s is not None and math.isfinite(float(predicted_tcp_speed_m_s)):
        absolute_speed_stop_m_s = float(kwargs.get("absolute_speed_stop_m_s", STEP5D_V13_ABSOLUTE_SPEED_STOP_M_S))
        low_load_speed_stop_m_s = float(kwargs.get("low_load_speed_stop_m_s", STEP5D_V13_LOW_LOAD_SPEED_STOP_M_S))
        low_load_speed_load_n = float(kwargs.get("low_load_speed_load_n", STEP5D_V13_LOW_LOAD_SPEED_LOAD_N))
        normal_load_n = float(kwargs.get("normal_load_n", 0.0))
        safe_dt_s = min(
            max(0.0, float(kwargs.get("dt_s", 0.0))),
            float(kwargs.get("dt_max_s", STEP5D_V12_GUARD_DT_MAX_S)),
        )
        predicted_high = float(predicted_tcp_speed_m_s) > absolute_speed_stop_m_s
        low_load_predicted_high = normal_load_n < low_load_speed_load_n and float(predicted_tcp_speed_m_s) > low_load_speed_stop_m_s
        if predicted_high or low_load_predicted_high:
            hold_s = max(0.0, float(kwargs.get("prior_hold_s", 0.0))) + safe_dt_s
            hold_timeout_s = float(kwargs.get("hold_timeout_s", STEP5D_V13_LOW_LOAD_HOLD_TIMEOUT_S))
            if hold_s >= hold_timeout_s:
                return finalize({
                    "state": "danger_stop",
                    "action": "stop_zero_qdot",
                    "reason": "predicted_tcp_speed_hold_timeout",
                    "hold_s": hold_s,
                    "high_window_s": float(kwargs.get("prior_high_window_s", 0.0)),
                    "actual_speed_violation_s": float(kwargs.get("prior_actual_speed_violation_s", 0.0)),
                    "actual_speed_violation_count": int(kwargs.get("prior_actual_speed_violation_count", 0)),
                })
            return finalize({
                "state": "contact_uncertain_hold",
                "action": "hold_zero_qdot",
                "reason": "predicted_tcp_speed_recoverable_hold"
                if predicted_high
                else "low_load_predicted_tcp_speed_recoverable_hold",
                "hold_s": hold_s,
                "high_window_s": float(kwargs.get("prior_high_window_s", 0.0)),
                "actual_speed_violation_s": float(kwargs.get("prior_actual_speed_violation_s", 0.0)),
                "actual_speed_violation_count": int(kwargs.get("prior_actual_speed_violation_count", 0)),
            })
    base_result = step5d_v13_contact_safety_guard(**kwargs)
    if base_result["reason"] in {"high_contact_window_dwell", "high_contact_window_dwell_stop"}:
        return finalize({
            **base_result,
            "state": "valid_contact",
            "action": "pass_solver",
            "reason": "ok_high_contact_below_hard_force",
            "high_window_s": 0.0,
        })
    if base_result["reason"] == "low_load_hold_timeout":
        actual_tcp_speed_m_s = float(kwargs.get("actual_tcp_speed_m_s", math.inf))
        low_load_speed_stop_m_s = float(kwargs.get("low_load_speed_stop_m_s", STEP5D_V13_LOW_LOAD_SPEED_STOP_M_S))
        if math.isfinite(actual_tcp_speed_m_s) and actual_tcp_speed_m_s <= low_load_speed_stop_m_s:
            return finalize({
                **base_result,
                "state": "contact_uncertain_hold",
                "action": "hold_zero_qdot",
                "reason": "low_load_reacquire_hold_timeout_deferred",
            })
    return finalize(base_result)


def step5d_v15a_guard(**kwargs: Any) -> dict[str, float | str]:
    return step5d_v15_permissive_recovery_guard(require_braking_margin=True, **kwargs)


def limit_step5d_live_xdot(
    xdot_c: Any,
    *,
    max_linear_m_s: float,
    max_angular_rad_s: float,
) -> tuple[np.ndarray, bool]:
    xdot = np.asarray(xdot_c, dtype=float)
    if xdot.shape != (6,) or not np.all(np.isfinite(xdot)):
        raise ValueError("Step5d xdot_c must be a finite 6-vector")
    max_linear = float(max_linear_m_s)
    max_angular = float(max_angular_rad_s)
    if not math.isfinite(max_linear) or max_linear <= 0.0:
        raise ValueError("max_linear_m_s must be finite and positive")
    if not math.isfinite(max_angular) or max_angular <= 0.0:
        raise ValueError("max_angular_rad_s must be finite and positive")
    limited = xdot.copy()
    limiter_active = False
    linear_norm = float(np.linalg.norm(limited[:3]))
    if linear_norm > max_linear:
        limited[:3] *= max_linear / linear_norm
        limiter_active = True
    angular_norm = float(np.linalg.norm(limited[3:]))
    if angular_norm > max_angular:
        limited[3:] *= max_angular / angular_norm
        limiter_active = True
    return limited, limiter_active


def limit_step5d_qdot_slew(
    qdot: Any,
    previous_qdot: np.ndarray | None,
    *,
    dt_s: float,
    max_slew_rad_s2: float = STEP5D_V12_QDOT_SLEW_RAD_S2,
    dt_max_s: float = STEP5D_V12_GUARD_DT_MAX_S,
) -> tuple[np.ndarray, bool]:
    qdot_values = np.asarray(qdot, dtype=float)
    if qdot_values.shape != (6,) or not np.all(np.isfinite(qdot_values)):
        raise ValueError("Step5d qdot must be a finite 6-vector")
    if previous_qdot is None:
        previous = np.zeros(6, dtype=float)
    else:
        previous = np.asarray(previous_qdot, dtype=float)
    if previous.shape != (6,) or not np.all(np.isfinite(previous)):
        raise ValueError("previous Step5d qdot must be a finite 6-vector")
    if not math.isfinite(float(dt_s)) or dt_s < 0.0 or max_slew_rad_s2 <= 0.0 or dt_max_s <= 0.0:
        raise ValueError("Step5d qdot slew limits must be positive")
    delta_limit = float(max_slew_rad_s2) * min(max(0.0, float(dt_s)), float(dt_max_s))
    limited = previous + np.clip(qdot_values - previous, -delta_limit, delta_limit)
    return limited, bool(np.any(np.abs(limited - qdot_values) > 1e-12))


def ensure_step5d_liveprep_runtime(state: "BridgeState", args: argparse.Namespace) -> None:
    if state.step5d_model_bundle is None:
        state.step5d_model_bundle = step5d_kin.build_calibrated_model()
        audit_rows = step5d_kin.finite_run_rows(step5d_kin.DEFAULT_BRIDGE_CSV)
        state.step5d_tcp_offset_tool0 = step5d_kin.infer_tcp_offset(state.step5d_model_bundle, audit_rows)["mean"]
    if args.bridge_profile == STEP5D_LIVEPREP_V15A_STAGE_ID and state.step5d_tcp_cage is None:
        state.step5d_tcp_cage = build_step5d_v15a_tcp_cage()
    if state.step5d_solver is None:
        state.step5d_solver = StrictTaseRnnSolver(
            StrictRnnConfig(
                paper_truth_path=STEP5D_LIVEPREP_TRUTH_PATH,
                qdot_limit_rad_s=float(args.step5d_qdot_limit_rad_s),
                epsilon=float(args.step5d_epsilon),
                sigr_exponent_r=float(args.step5d_sigr_exponent_r),
            )
        )


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


def bridge_zero_values() -> dict[str, float]:
    return {name: 0.0 for name in BRIDGE_INPUT_NAMES}


step4e_zero_values = bridge_zero_values


def step5c_register_metadata() -> dict[str, Any]:
    return {
        "contract": STEP5C_INPUT_REGISTER_SEMANTICS,
        "carrier_warning": (
            "In Step5c joint mode the existing Step4e RTDE recipe field names are "
            "carriers only; registers 37..42 are qd0..qd5 rad/s, not Cartesian "
            "TCP velocity or angular speed."
        ),
        "carriers": {
            register: {
                "step4e_carrier_name": carrier_name,
                "step5c_debug_column": debug_name,
                "step5c_semantic": semantic,
            }
            for register, carrier_name, debug_name, semantic in STEP5C_JOINT_REGISTER_CONTRACT
        },
        "register_43": {
            "step4e_carrier_name": "step4e_cmd_valid",
            "step5c_semantic": "cmd_valid",
        },
        "register_44": {
            "step4e_carrier_name": "step4e_progress_m",
            "step5c_semantic": "path_time_s",
        },
        "register_47": {
            "step4e_carrier_name": "step4e_controller_state",
            "step5c_semantic": "solver_status",
        },
        "legacy_mujoco_model_role": "failure_contrast_only_not_live_authorized",
    }


def step5c_joint_register_values(
    qdot: tuple[float, float, float, float, float, float] | list[float],
    *,
    cmd_valid: float,
    path_time_s: float,
    force_error_n: float,
    pose_or_orientation_error: float,
    solver_status: float,
) -> dict[str, float]:
    if len(qdot) != 6:
        raise ValueError(f"Step5c qdot must have 6 joints, got {len(qdot)}")
    qdot_values = tuple(float(value) for value in qdot)
    scalar_values = {
        "cmd_valid": float(cmd_valid),
        "path_time_s": float(path_time_s),
        "force_error_n": float(force_error_n),
        "pose_or_orientation_error": float(pose_or_orientation_error),
        "solver_status": float(solver_status),
    }
    for label, value in [*zip([f"qd{idx}_rad_s" for idx in range(6)], qdot_values), *scalar_values.items()]:
        if not math.isfinite(value):
            raise ValueError(f"Step5c register value must be finite: {label}={value!r}")

    values = {
        "step4e_cmd_valid": scalar_values["cmd_valid"],
        "step4e_progress_m": scalar_values["path_time_s"],
        "step4e_force_error_n": scalar_values["force_error_n"],
        "step4e_orientation_error_rad": scalar_values["pose_or_orientation_error"],
        "step4e_controller_state": scalar_values["solver_status"],
        "_step5c_solver_status": scalar_values["solver_status"],
    }
    for idx, (_register, carrier_name, debug_name, _semantic) in enumerate(STEP5C_JOINT_REGISTER_CONTRACT):
        values[carrier_name] = qdot_values[idx]
        values[debug_name] = qdot_values[idx]
    return values


class BridgeState:
    def __init__(self) -> None:
        self.integral_error_n_s = 0.0
        self.normal_velocity_m_s = 0.0
        self.latched_normal_b: tuple[float, float, float] | None = None
        self.filtered_normal_b: tuple[float, float, float] | None = None
        self.latched_normal_locked = False
        self.normal_acquired = False
        self.line_stage_s = 0.0
        self.last_robot_stage: float | None = None
        self.step5d_model_bundle: step5d_kin.CalibratedModel | None = None
        self.step5d_tcp_offset_tool0: np.ndarray | None = None
        self.step5d_tcp_cage: Step5dTcpCage | None = None
        self.step5d_solver: StrictTaseRnnSolver | None = None
        self.step5d_outer_state = Step5dOuterLoopState()
        self.step5d_settle_filtered_normal_load_n: float | None = None
        self.step5d_line_guard_loss_s = 0.0
        self.step5d_last_qdot: np.ndarray | None = None
        self.step5d_contact_hold_s = 0.0
        self.step5d_contact_high_window_s = 0.0
        self.step5d_actual_speed_violation_s = 0.0
        self.step5d_actual_speed_violation_count = 0
        self.step5d_contact_hold_path_time_s: float | None = None
        self.step5d_active_stage25_s = 0.0
        self.step5d_hold_event_count = 0
        self.step5d_consecutive_hold_s = 0.0
        self.step5d_total_hold_s = 0.0
        self.step5d_repeated_hold_count = 0
        self.step5d_last_hold_reason = ""
        self.step5d_hold_actual_tcp_speed_m_s: float | None = None
        self.step5b_15n_anchor_xy: tuple[float, float] | None = None
        self.step5b_15n_acquired = False
        self.step5b_15n_after_acquire_s = 0.0
        self.step5b_15n_scored_s = 0.0
        self.step5b_15n_high_normal_s = 0.0
        self.step5b_15n_high_force_s = 0.0
        self.step5b_15n_high_torque_s = 0.0
        self.step5b_15n_low_load_s = 0.0
        self.step5b_15n_relative_low_load_s = 0.0
        self.step5b_15n_saturation_s = 0.0
        self.step5b_ramp_anchor_xy: tuple[float, float] | None = None
        self.step5b_ramp_phase = "inactive"
        self.step5b_ramp_phase_s = 0.0
        self.step5b_ramp_preload_ready_s = 0.0
        self.step5b_ramp_after_initial_acquire_s = 0.0
        self.step5b_ramp_terminal_discard_s = 0.0
        self.step5b_ramp_scored_s = 0.0
        self.step5b_ramp_move_s = 0.0
        self.step5b_ramp_high_normal_s = 0.0
        self.step5b_ramp_high_force_s = 0.0
        self.step5b_ramp_high_torque_s = 0.0
        self.step5b_ramp_low_load_s = 0.0
        self.step5b_ramp_relative_low_load_s = 0.0
        self.step5b_ramp_saturation_s = 0.0
        self.step5b_ramp_active_target_force_n = STEP5B_RAMP_START_TARGET_N
        self.step5b_ramp_alpha = 0.0
        self.step5b_ramp_xy_enabled = False

    def reset_line_contact(self) -> None:
        self.integral_error_n_s = 0.0
        self.normal_velocity_m_s = 0.0
        self.latched_normal_b = None
        self.filtered_normal_b = None
        self.latched_normal_locked = False
        self.normal_acquired = False
        self.line_stage_s = 0.0
        self.last_robot_stage = None
        self.step5d_outer_state = Step5dOuterLoopState()
        self.step5d_settle_filtered_normal_load_n = None
        self.step5d_line_guard_loss_s = 0.0
        self.step5d_last_qdot = None
        self.step5d_contact_hold_s = 0.0
        self.step5d_contact_high_window_s = 0.0
        self.step5d_actual_speed_violation_s = 0.0
        self.step5d_actual_speed_violation_count = 0
        self.step5d_contact_hold_path_time_s = None
        self.step5d_active_stage25_s = 0.0
        self.step5d_hold_event_count = 0
        self.step5d_consecutive_hold_s = 0.0
        self.step5d_total_hold_s = 0.0
        self.step5d_repeated_hold_count = 0
        self.step5d_last_hold_reason = ""
        self.step5d_hold_actual_tcp_speed_m_s = None
        self.reset_step5b_15n_trial()

    def reset_step5b_15n_trial(self) -> None:
        self.step5b_15n_anchor_xy = None
        self.step5b_15n_acquired = False
        self.step5b_15n_after_acquire_s = 0.0
        self.step5b_15n_scored_s = 0.0
        self.step5b_15n_high_normal_s = 0.0
        self.step5b_15n_high_force_s = 0.0
        self.step5b_15n_high_torque_s = 0.0
        self.step5b_15n_low_load_s = 0.0
        self.step5b_15n_relative_low_load_s = 0.0
        self.step5b_15n_saturation_s = 0.0
        self.reset_step5b_ramp_trial()

    def reset_step5b_ramp_trial(self) -> None:
        self.step5b_ramp_anchor_xy = None
        self.step5b_ramp_phase = "inactive"
        self.step5b_ramp_phase_s = 0.0
        self.step5b_ramp_preload_ready_s = 0.0
        self.step5b_ramp_after_initial_acquire_s = 0.0
        self.step5b_ramp_terminal_discard_s = 0.0
        self.step5b_ramp_scored_s = 0.0
        self.step5b_ramp_move_s = 0.0
        self.step5b_ramp_high_normal_s = 0.0
        self.step5b_ramp_high_force_s = 0.0
        self.step5b_ramp_high_torque_s = 0.0
        self.step5b_ramp_low_load_s = 0.0
        self.step5b_ramp_relative_low_load_s = 0.0
        self.step5b_ramp_saturation_s = 0.0
        self.step5b_ramp_active_target_force_n = STEP5B_RAMP_START_TARGET_N
        self.step5b_ramp_alpha = 0.0
        self.step5b_ramp_xy_enabled = False


Step4EState = BridgeState


def compute_bridge_values(
    args: argparse.Namespace,
    latest_zeroed: list[float],
    latest_output: dict[str, Any] | None,
    sensor_ok: float,
    state: BridgeState,
    dt_s: float,
) -> dict[str, float]:
    values = bridge_zero_values()
    if args.bridge_mode == "off" or latest_output is None:
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
    v20_profile = args.bridge_profile == "v20"
    v21_profile = args.bridge_profile == "v21"
    v22_profile = args.bridge_profile == "v22"
    v23_profile = args.bridge_profile == "v23"
    v24_profile = args.bridge_profile == "v24"
    v25_profile = args.bridge_profile == "v25"
    v26_profile = args.bridge_profile == "v26"
    v27_profile = args.bridge_profile == "v27"
    v28_profile = args.bridge_profile == "v28"
    v29_profile = args.bridge_profile == "v29"
    v30_profile = args.bridge_profile == "v30"
    v31_profile = args.bridge_profile == "v31"
    step4f_profile = args.bridge_profile == "step4f_v1"
    step4g_profile = args.bridge_profile == "step4g_v1"
    step5b_profile = args.bridge_profile == "step5b_v1"
    step5b_15n_trial_profile = step5b_15n_trial_enabled(args)
    step5b_ramp_trial_profile = step5b_ramp_trial_enabled(args)
    step5b_any_trial_profile = step5b_15n_trial_profile or step5b_ramp_trial_profile
    step5c_dryrun_profile = args.bridge_profile == STEP5C_DRYRUN_STAGE_ID
    step5c_contact_profile = args.bridge_profile == STEP5C_CONTACT_STAGE_ID
    step5c_joint_profile = step5c_dryrun_profile or step5c_contact_profile
    step5d_liveprep_profile = args.bridge_profile in STEP5D_LIVEPREP_STAGE_IDS
    step5d_liveprep_v3_profile = args.bridge_profile == STEP5D_LIVEPREP_V3_STAGE_ID
    step5d_liveprep_v4_profile = args.bridge_profile == STEP5D_LIVEPREP_V4_STAGE_ID
    step5d_liveprep_v5_profile = args.bridge_profile == STEP5D_LIVEPREP_V5_STAGE_ID
    step5d_liveprep_v6_profile = args.bridge_profile == STEP5D_LIVEPREP_V6_STAGE_ID
    step5d_liveprep_v7_profile = args.bridge_profile == STEP5D_LIVEPREP_V7_STAGE_ID
    step5d_liveprep_v8_profile = args.bridge_profile == STEP5D_LIVEPREP_V8_STAGE_ID
    step5d_liveprep_v9_profile = args.bridge_profile == STEP5D_LIVEPREP_V9_STAGE_ID
    step5d_liveprep_v10_profile = args.bridge_profile == STEP5D_LIVEPREP_V10_STAGE_ID
    step5d_liveprep_v11_profile = args.bridge_profile == STEP5D_LIVEPREP_V11_STAGE_ID
    step5d_liveprep_v12_profile = args.bridge_profile == STEP5D_LIVEPREP_V12_STAGE_ID
    step5d_liveprep_v13_profile = args.bridge_profile == STEP5D_LIVEPREP_V13_STAGE_ID
    step5d_liveprep_v14_profile = args.bridge_profile == STEP5D_LIVEPREP_STAGE_ID
    step5d_liveprep_v15_profile = args.bridge_profile == STEP5D_LIVEPREP_V15_STAGE_ID
    step5d_liveprep_v15a_profile = args.bridge_profile == STEP5D_LIVEPREP_V15A_STAGE_ID
    step5d_liveprep_guarded_profile = (
        step5d_liveprep_v3_profile
        or step5d_liveprep_v4_profile
        or step5d_liveprep_v5_profile
        or step5d_liveprep_v6_profile
        or step5d_liveprep_v7_profile
        or step5d_liveprep_v8_profile
        or step5d_liveprep_v9_profile
        or step5d_liveprep_v10_profile
        or step5d_liveprep_v11_profile
        or step5d_liveprep_v12_profile
        or step5d_liveprep_v13_profile
        or step5d_liveprep_v14_profile
        or step5d_liveprep_v15_profile
        or step5d_liveprep_v15a_profile
    )
    if step5d_liveprep_profile:
        try:
            ensure_step5d_liveprep_runtime(state, args)
        except (ValueError, RuntimeError) as exc:
            values["_step5d_solver_error"] = f"warmup: {exc}"
    step6b_profile = args.bridge_profile in {"step6b_v1", "step6b_v2"}
    step6_stage_id = STEP6_CONTACT_EIGHT_STAGE_ID_V2 if args.bridge_profile == "step6b_v2" else STEP6_CONTACT_EIGHT_STAGE_ID
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
        # Step5d must stay in this group so contact-search orientation uses
        # the approach axis (-n_control_b), matching the Step5d outer loop.
        or step5d_liveprep_profile
        or step6b_profile
    )
    detached_profile = v20_profile or v21_profile or v22_profile or angular_speedl_profile
    axis_iso_active = args.bridge_mode == "axis_iso" and 25.18 <= robot_stage <= 25.27
    first_search_stage_active = (
        args.bridge_mode == "line"
        and angular_speedl_profile
        and (abs(robot_stage - 24.0) < 0.05 or abs(robot_stage - 24.2) < 0.05)
    )
    latch_stage_active = args.bridge_mode == "line" and detached_profile and abs(robot_stage - 25.05) < 0.03
    detach_stage_active = args.bridge_mode == "line" and detached_profile and abs(robot_stage - 25.1) < 0.03
    orient_stage_active = args.bridge_mode == "line" and (
        (detached_profile and abs(robot_stage - 25.2) < 0.05)
        or (not detached_profile and abs(robot_stage - 25.1) < 0.05)
    )
    acquire_stage_active = (
        args.bridge_mode == "line"
        and (v20_profile or v22_profile or angular_speedl_profile)
        and abs(robot_stage - 25.3) < 0.05
    )
    line_entry_gate_active = (
        v29_profile or v30_profile or v31_profile or step5b_profile or step5c_contact_profile or step5d_liveprep_profile or step6b_profile
    ) and acquire_stage_active
    line_stage_active = args.bridge_mode == "line" and abs(robot_stage - 25.0) < 0.05
    step5d_joint_line_profile = step5d_liveprep_profile and line_stage_active
    if not step5d_joint_line_profile:
        state.step5d_line_guard_loss_s = 0.0
        state.step5d_last_qdot = None
        state.step5d_contact_hold_s = 0.0
        state.step5d_contact_high_window_s = 0.0
        state.step5d_actual_speed_violation_s = 0.0
        state.step5d_actual_speed_violation_count = 0
        state.step5d_contact_hold_path_time_s = None
        state.step5d_active_stage25_s = 0.0
        state.step5d_hold_event_count = 0
        state.step5d_consecutive_hold_s = 0.0
        state.step5d_total_hold_s = 0.0
        state.step5d_repeated_hold_count = 0
        state.step5d_last_hold_reason = ""
        state.step5d_hold_actual_tcp_speed_m_s = None
    control_stage_active = (
        latch_stage_active
        or detach_stage_active
        or orient_stage_active
        or acquire_stage_active
        or line_stage_active
        or axis_iso_active
    )
    if args.bridge_mode not in {"line", "axis_iso"} or (
        math.isfinite(robot_stage) and (robot_stage < 24.0 or robot_stage >= 26.0)
    ):
        state.reset_line_contact()
    if math.isfinite(robot_stage):
        if state.last_robot_stage is None or abs(robot_stage - state.last_robot_stage) >= 0.03:
            state.line_stage_s = 0.0
            state.last_robot_stage = robot_stage
    if control_stage_active:
        state.line_stage_s += dt_s
    if step5d_joint_line_profile:
        state.step5d_active_stage25_s += max(0.0, dt_s)
    if not (step5b_any_trial_profile and line_stage_active):
        state.reset_step5b_15n_trial()

    force_t, torque_t = kunwei_to_tcp_wrench(latest_zeroed)
    force_abs = norm3(force_t)
    torque_abs = norm3(torque_t)
    contact_offset_x = ""
    contact_offset_y = ""
    if abs(force_t[2]) > args.bridge_contact_offset_min_fz_n:
        contact_offset_x = -torque_t[1] / force_t[2]
        contact_offset_y = torque_t[0] / force_t[2]

    rotation = rotvec_to_matrix(float(pose[3]), float(pose[4]), float(pose[5]))
    synthetic_normal = (
        synthetic_axis_iso_normal(robot_stage, math.radians(args.bridge_axis_iso_tilt_deg))
        if axis_iso_active
        else None
    )
    force_b = mat_vec3(rotation, force_t)
    n_reaction_b = synthetic_normal if synthetic_normal is not None else normalize3(force_b)
    raw_live_normal_b, live_candidate_b, live_candidate_force_n = live_normal_candidate(
        force_b,
        friction_projection=args.bridge_normal_friction_projection == "on",
    )
    if (
        detached_profile
        and (latch_stage_active or first_search_stage_active)
        and sensor_ok > 0.5
        and force_abs >= args.bridge_min_force_for_control_n
        and not state.latched_normal_locked
    ):
        state.latched_normal_b = n_reaction_b
        state.latched_normal_locked = True
        state.normal_acquired = True
    elif (
        not detached_profile
        and sensor_ok > 0.5
        and force_abs >= args.bridge_min_force_for_control_n
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
        (v30_profile or v31_profile or step4f_profile or step4g_profile or step5b_profile or step5c_contact_profile or step5d_liveprep_profile or step6b_profile)
        and args.bridge_normal_follow_mode == "filtered_live"
        and line_stage_active
        and state.normal_acquired
        and state.latched_normal_b is not None
    )
    if normal_follow_active:
        filtered_current = state.filtered_normal_b if state.filtered_normal_b is not None else state.latched_normal_b
        live_candidate_angle_rad = angle_between_unit(filtered_current, live_candidate_b)
        live_candidate_angle_from_latch_rad = angle_between_unit(state.latched_normal_b, live_candidate_b)
        if v31_profile or step4f_profile or step4g_profile or step5b_profile or step5c_contact_profile or step5d_liveprep_profile or step6b_profile:
            state.filtered_normal_b, normal_filter_source = v31_filtered_live_normal(
                filtered_current,
                live_candidate_b,
                live_candidate_force_n,
                sensor_ok=sensor_ok,
                alpha=args.bridge_normal_filter_alpha,
                min_force_n=args.bridge_normal_min_force_n,
            )
            n_control_b = state.filtered_normal_b
        else:
            max_candidate_angle_rad = math.radians(args.bridge_normal_max_angle_from_latch_deg)
            if sensor_ok <= 0.5:
                normal_filter_source = "locked_fallback_stale"
                n_control_b = state.latched_normal_b
            elif live_candidate_force_n < args.bridge_normal_min_force_n:
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
                if args.bridge_normal_filter_tau_s > 0.0:
                    alpha = 1.0 - math.exp(-max(0.0, dt_s) / args.bridge_normal_filter_tau_s)
                ema_normal_b = slerp_unit(filtered_current, live_candidate_b, alpha)
                max_step_rad = max(0.0, args.bridge_normal_max_rate_rad_s) * max(0.0, dt_s)
                state.filtered_normal_b = rotate_toward_unit(filtered_current, ema_normal_b, max_step_rad)
                n_control_b = state.filtered_normal_b
                normal_filter_source = "filtered_live"
    elif (
        v30_profile or v31_profile or step4f_profile or step4g_profile or step5b_profile or step5c_contact_profile or step5d_liveprep_profile or step6b_profile
    ) and args.bridge_normal_follow_mode == "filtered_live":
        if line_stage_active:
            normal_filter_source = "locked_no_latch"
        else:
            normal_filter_source = "locked_pre_line"
    normal_load_n = max(0.0, dot3(force_b, n_control_b)) if state.normal_acquired else 0.0
    if step5b_ramp_trial_profile and line_stage_active:
        step5b_ramp_trial_phase_update(
            normal_load_n=normal_load_n,
            force_norm_n=force_abs,
            dt_s=dt_s,
            state=state,
        )
    tcp_z_axis_b = (rotation[0][2], rotation[1][2], rotation[2][2])
    step5d_line_tcp_speed_m_s = (
        norm3([float(speed[0]), float(speed[1]), float(speed[2])])
        if speed and len(speed) >= 3
        else 0.0
    )
    step5d_contact_safety = {
        "state": "inactive",
        "action": "inactive",
        "reason": "not_active",
        "hold_s": state.step5d_contact_hold_s,
        "high_window_s": state.step5d_contact_high_window_s,
    }
    step5d_contact_safety_stop = False
    step5d_predicted_tcp_speed_m_s = math.nan
    step5d_tcp_cage = {
        "distance_m": math.nan,
        "braking_margin_m": math.nan,
        "signed_distance_m": math.nan,
        "cell_index": -1.0,
        "reason": "not_active",
    }
    step5d_contact_safety_profile = (
        step5d_liveprep_v13_profile or step5d_liveprep_v14_profile or step5d_liveprep_v15_profile or step5d_liveprep_v15a_profile
    )
    if step5d_contact_safety_profile and step5d_joint_line_profile:
        step5d_contact_safety_fn = (
            step5d_v15a_guard
            if step5d_liveprep_v15a_profile
            else step5d_v15_permissive_recovery_guard
            if step5d_liveprep_v15_profile
            else step5d_v13_contact_safety_guard
        )
        if step5d_liveprep_v15a_profile:
            if state.step5d_tcp_cage is None:
                try:
                    state.step5d_tcp_cage = build_step5d_v15a_tcp_cage()
                except RuntimeError as exc:
                    step5d_tcp_cage = {
                        "distance_m": math.nan,
                        "braking_margin_m": math.nan,
                        "signed_distance_m": math.nan,
                        "cell_index": -1.0,
                        "reason": str(exc),
                    }
            if state.step5d_tcp_cage is not None:
                step5d_tcp_cage = state.step5d_tcp_cage.evaluate(
                    pose,
                    actual_tcp_speed_m_s=step5d_line_tcp_speed_m_s,
                    predicted_tcp_speed_m_s=None,
                )
        step5d_contact_safety = step5d_contact_safety_fn(
            normal_load_n=normal_load_n,
            force_norm_n=force_abs,
            actual_tcp_speed_m_s=step5d_line_tcp_speed_m_s,
            predicted_tcp_speed_m_s=None,
            braking_margin_m=(
                float(step5d_tcp_cage["braking_margin_m"])
                if step5d_liveprep_v15a_profile
                else None
            ),
            prior_hold_s=state.step5d_contact_hold_s,
            prior_high_window_s=state.step5d_contact_high_window_s,
            prior_actual_speed_violation_s=state.step5d_actual_speed_violation_s,
            prior_actual_speed_violation_count=state.step5d_actual_speed_violation_count,
            prior_consecutive_hold_s=state.step5d_consecutive_hold_s,
            prior_total_hold_s=state.step5d_total_hold_s,
            prior_hold_event_count=state.step5d_hold_event_count,
            prior_last_hold_reason=state.step5d_last_hold_reason,
            prior_hold_actual_tcp_speed_m_s=state.step5d_hold_actual_tcp_speed_m_s,
            active_stage25_s=state.step5d_active_stage25_s,
            dt_s=dt_s,
        )
        state.step5d_contact_hold_s = float(step5d_contact_safety["hold_s"])
        state.step5d_contact_high_window_s = float(step5d_contact_safety["high_window_s"])
        state.step5d_actual_speed_violation_s = float(step5d_contact_safety["actual_speed_violation_s"])
        state.step5d_actual_speed_violation_count = int(step5d_contact_safety["actual_speed_violation_count"])
        state.step5d_consecutive_hold_s = float(step5d_contact_safety.get("consecutive_hold_s", 0.0))
        state.step5d_total_hold_s = float(step5d_contact_safety.get("total_hold_s", state.step5d_total_hold_s))
        state.step5d_hold_event_count = int(float(step5d_contact_safety.get("hold_event_count", state.step5d_hold_event_count)))
        state.step5d_repeated_hold_count = int(float(step5d_contact_safety.get("repeated_hold_count", state.step5d_repeated_hold_count)))
        state.step5d_last_hold_reason = str(step5d_contact_safety.get("last_hold_reason", state.step5d_last_hold_reason))
        hold_speed = float(step5d_contact_safety.get("hold_actual_tcp_speed_m_s", math.nan))
        state.step5d_hold_actual_tcp_speed_m_s = hold_speed if math.isfinite(hold_speed) else None
        if step5d_contact_safety["action"] == "hold_zero_qdot":
            if state.step5d_contact_hold_path_time_s is None:
                state.step5d_contact_hold_path_time_s = max(0.0, state.line_stage_s - max(0.0, dt_s))
            state.line_stage_s = state.step5d_contact_hold_path_time_s
        elif step5d_contact_safety["action"] == "pass_solver":
            if state.step5d_contact_hold_path_time_s is not None:
                state.step5d_outer_state = Step5dOuterLoopState()
                state.step5d_last_qdot = None
            state.step5d_contact_hold_path_time_s = None
        elif step5d_contact_safety["action"] == "stop_zero_qdot":
            step5d_contact_safety_stop = True
            state.step5d_outer_state = Step5dOuterLoopState()
            state.step5d_last_qdot = None
    orientation_target_axis_b = (
        # For contact routes in angular_speedl_profile, n_control_b is the
        # reaction normal. TCP z must target the approach axis -n_control_b.
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
        args.bridge_orientation_gain * args.bridge_orientation_wx_sign * orientation_axis[0],
        args.bridge_orientation_gain * args.bridge_orientation_wy_sign * orientation_axis[1],
        args.bridge_orientation_gain * orientation_axis[2],
    )
    orientation_norm = norm3(orientation_cmd)
    if orientation_norm > args.bridge_angular_limit_rad_s:
        scale = args.bridge_angular_limit_rad_s / orientation_norm
        orientation_cmd = tuple(value * scale for value in orientation_cmd)

    path_time_s = state.line_stage_s
    if step5b_ramp_trial_profile and line_stage_active:
        path_time_s = state.step5b_ramp_move_s if state.step5b_ramp_xy_enabled else 0.0

    if step5b_profile:
        path_ref = step5_contact_path_reference(
            (float(pose[0]), float(pose[1])),
            path_time_s,
        )
    elif step5c_dryrun_profile:
        path_ref = step5_contact_path_reference(
            (float(pose[0]), float(pose[1])),
            path_time_s,
            stage_id=STEP5C_DRYRUN_STAGE_ID,
        )
    elif step5c_contact_profile:
        path_ref = step5_contact_path_reference(
            (float(pose[0]), float(pose[1])),
            path_time_s,
            stage_id=STEP5C_CONTACT_STAGE_ID,
        )
    elif step5d_liveprep_profile:
        path_ref = step5_contact_path_reference(
            (float(pose[0]), float(pose[1])),
            path_time_s,
            stage_id=args.bridge_profile,
        )
    elif step6b_profile:
        path_ref = step6_contact_path_reference(
            (float(pose[0]), float(pose[1])),
            state.line_stage_s,
            stage_id=step6_stage_id,
        )
    else:
        path_ref = step4e_path_reference(
            args.bridge_path_shape,
            (float(pose[0]), float(pose[1])),
            state.line_stage_s,
        )
    progress = float(path_ref["progress"])
    desired_x, desired_y = path_ref["desired_xy"]
    path_error = (path_ref["path_error_xy"][0], path_ref["path_error_xy"][1], 0.0)
    step5b_freeze_xy = line_stage_active and (
        step5b_15n_trial_profile or (step5b_ramp_trial_profile and not state.step5b_ramp_xy_enabled)
    )
    if step5b_freeze_xy:
        if step5b_ramp_trial_profile:
            if state.step5b_ramp_anchor_xy is None:
                state.step5b_ramp_anchor_xy = (float(pose[0]), float(pose[1]))
            desired_x, desired_y = state.step5b_ramp_anchor_xy
        else:
            if state.step5b_15n_anchor_xy is None:
                state.step5b_15n_anchor_xy = (float(pose[0]), float(pose[1]))
            desired_x, desired_y = state.step5b_15n_anchor_xy
        path_error = (0.0, 0.0, 0.0)
    if args.bridge_path_shape == "line":
        tangent_speed = args.bridge_line_speed_m_s if args.bridge_mode == "line" and line_stage_active else 0.0
        desired_velocity_xy = (
            tangent_speed * STEP4E_LINE_UNIT_XY[0],
            tangent_speed * STEP4E_LINE_UNIT_XY[1],
        )
    else:
        desired_velocity_xy = path_ref["desired_velocity_xy"]
        if args.bridge_mode != "line" or not line_stage_active:
            desired_velocity_xy = (0.0, 0.0)
    if step5b_freeze_xy:
        desired_velocity_xy = (0.0, 0.0)
    if line_stage_active and path_time_s <= args.bridge_line_settle_s:
        desired_velocity_xy = (0.0, 0.0)
    if detached_profile and not line_stage_active:
        base_motion = (0.0, 0.0, 0.0)
    else:
        base_motion = (
            desired_velocity_xy[0] + args.bridge_path_p_gain * path_error[0],
            desired_velocity_xy[1] + args.bridge_path_p_gain * path_error[1],
            0.0,
        )
    if step5c_dryrun_profile:
        motion_cmd = base_motion
    else:
        normal_projection = dot3(base_motion, n_control_b)
        motion_cmd = tuple(base_motion[idx] - normal_projection * n_control_b[idx] for idx in range(3))
    motion_norm = norm3(motion_cmd)
    if motion_norm > args.bridge_motion_limit_m_s:
        scale = args.bridge_motion_limit_m_s / motion_norm
        motion_cmd = tuple(value * scale for value in motion_cmd)

    controlled_force_n = 0.0 if step5c_dryrun_profile else normal_load_n if args.bridge_mode == "line" else force_abs
    effective_target_force_n = (
        state.step5b_ramp_active_target_force_n
        if step5b_ramp_trial_profile and line_stage_active
        else float(args.target_force_n)
    )
    force_error = effective_target_force_n - controlled_force_n
    line_grace_valid = (
        args.bridge_mode == "line"
        and not detached_profile
        and control_stage_active
        and not state.normal_acquired
        and state.line_stage_s <= args.bridge_acquire_grace_s
    )
    if step5c_dryrun_profile:
        control_allowed = args.bridge_mode == "line" and line_stage_active
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
            force_abs >= args.bridge_min_force_for_control_n
            or (args.bridge_mode == "line" and state.normal_acquired)
            or line_grace_valid
        )
    if args.bridge_integrate_stage25_only and args.bridge_mode == "line" and not control_stage_active:
        control_allowed = False
    step5d_v8_pid_recovery_ok = True
    step5b_15n_trial_stop_reason = None
    if control_allowed:
        if args.bridge_mode == "line" and not step5c_dryrun_profile and not state.normal_acquired:
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
            if step5d_liveprep_v10_profile or step5d_liveprep_v11_profile or step5d_liveprep_v12_profile or step5d_liveprep_v13_profile or step5d_liveprep_v14_profile or step5d_liveprep_v15_profile or step5d_liveprep_v15a_profile:
                recovery_window_ok = step5d_v9_recovery_window_ok(normal_load_n=normal_load_n, force_norm_n=force_abs)
                if recovery_window_ok:
                    if step5d_liveprep_v11_profile or step5d_liveprep_v12_profile or step5d_liveprep_v13_profile or step5d_liveprep_v14_profile or step5d_liveprep_v15_profile or step5d_liveprep_v15a_profile:
                        cmd, state.step5d_settle_filtered_normal_load_n, state.normal_velocity_m_s = (
                            step5d_v11_deadband_acquire_velocity(
                                normal_load_n=normal_load_n,
                                filtered_normal_load_n=state.step5d_settle_filtered_normal_load_n,
                                settle_velocity_m_s=state.normal_velocity_m_s,
                                dt_s=dt_s,
                                reaction_normal_b=tuple(float(value) for value in n_control_b),  # type: ignore[arg-type]
                            )
                        )
                    else:
                        cmd, state.step5d_settle_filtered_normal_load_n, state.normal_velocity_m_s = (
                            step5d_v10_admittance_settle_velocity(
                            normal_load_n=normal_load_n,
                            filtered_normal_load_n=state.step5d_settle_filtered_normal_load_n,
                            target_force_n=float(args.target_force_n),
                            settle_velocity_m_s=state.normal_velocity_m_s,
                            dt_s=dt_s,
                            reaction_normal_b=tuple(float(value) for value in n_control_b),  # type: ignore[arg-type]
                            v_max_m_s=float(args.bridge_normal_velocity_limit_m_s),
                            )
                        )
                    orientation_cmd = (0.0, 0.0, 0.0)
                else:
                    step5d_v8_pid_recovery_ok = False
                    state.step5d_settle_filtered_normal_load_n = None
                    state.normal_velocity_m_s = 0.0
                    cmd = (0.0, 0.0, 0.0)
                    orientation_cmd = (0.0, 0.0, 0.0)
            elif step5d_liveprep_v8_profile or step5d_liveprep_v9_profile:
                recovery_window_ok = (
                    step5d_v9_recovery_window_ok(normal_load_n=normal_load_n, force_norm_n=force_abs)
                    if step5d_liveprep_v9_profile
                    else step5d_v8_recovery_window_ok(normal_load_n=normal_load_n, force_norm_n=force_abs)
                )
                if recovery_window_ok:
                    approach_normal_b = (-n_control_b[0], -n_control_b[1], -n_control_b[2])
                    measured_normal_velocity_m_s = (
                        dot3((float(speed[0]), float(speed[1]), float(speed[2])), approach_normal_b)
                        if speed and len(speed) >= 3
                        else state.normal_velocity_m_s
                    )
                    v_press_limit_m_s = (
                        min(0.0029, float(args.bridge_normal_velocity_limit_m_s))
                        if step5d_liveprep_v9_profile
                        else float(args.bridge_normal_velocity_limit_m_s)
                    )
                    cmd, state.integral_error_n_s, state.normal_velocity_m_s = step5d_v8_force_pid_settle_velocity(
                        normal_load_n=normal_load_n,
                        target_force_n=float(args.target_force_n),
                        integral_error_n_s=state.integral_error_n_s,
                        dt_s=dt_s,
                        reaction_normal_b=tuple(float(value) for value in n_control_b),  # type: ignore[arg-type]
                        normal_velocity_m_s=measured_normal_velocity_m_s,
                        kp_m_s_per_n=float(args.bridge_force_p_gain),
                        ki_m_s_per_n_s=float(args.bridge_force_i_gain),
                        damping=float(args.bridge_force_damping),
                        integral_limit_n_s=float(args.bridge_integral_limit_n_s),
                        v_press_max_m_s=v_press_limit_m_s,
                        v_unload_max_m_s=v_press_limit_m_s,
                    )
                    orientation_cmd = (0.0, 0.0, 0.0)
                else:
                    step5d_v8_pid_recovery_ok = False
                    if not step5d_liveprep_v9_profile:
                        state.integral_error_n_s = 0.0
                        state.normal_velocity_m_s = 0.0
                    cmd = (0.0, 0.0, 0.0)
                    orientation_cmd = (0.0, 0.0, 0.0)
            elif step5d_liveprep_guarded_profile:
                state.integral_error_n_s = clamp(
                    state.integral_error_n_s + force_error * dt_s,
                    -args.bridge_integral_limit_n_s,
                    args.bridge_integral_limit_n_s,
                )
                accel_like = (
                    args.bridge_force_p_gain * force_error
                    + args.bridge_force_i_gain * state.integral_error_n_s
                    - args.bridge_force_damping * state.normal_velocity_m_s
                )
                state.normal_velocity_m_s = clamp(
                    state.normal_velocity_m_s + accel_like * dt_s,
                    -args.bridge_normal_velocity_limit_m_s,
                    args.bridge_normal_velocity_limit_m_s,
                )
                cmd = tuple(
                    -args.bridge_normal_command_sign * n_control_b[idx] * state.normal_velocity_m_s
                    for idx in range(3)
                )
                orientation_cmd = (0.0, 0.0, 0.0)
            else:
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
                if step5b_any_trial_profile and (
                    normal_load_n < STEP5B_15N_TRIAL_LOW_LOAD_N
                    or normal_filter_source == "hold_low_force"
                ):
                    bleed = min(1.0, max(0.0, dt_s) / 0.050)
                    state.integral_error_n_s *= 1.0 - bleed
                else:
                    state.integral_error_n_s = clamp(
                        state.integral_error_n_s + force_error * dt_s,
                        -args.bridge_integral_limit_n_s,
                        args.bridge_integral_limit_n_s,
                    )
                accel_like = (
                    args.bridge_force_p_gain * force_error
                    + args.bridge_force_i_gain * state.integral_error_n_s
                    - args.bridge_force_damping * state.normal_velocity_m_s
                )
                state.normal_velocity_m_s = clamp(
                    state.normal_velocity_m_s + accel_like * dt_s,
                    -args.bridge_normal_velocity_limit_m_s,
                    args.bridge_normal_velocity_limit_m_s,
                )
                if v20_profile and acquire_stage_active and force_error < -0.25:
                    unload_speed = min(
                        args.bridge_normal_velocity_limit_m_s,
                        max(args.bridge_reacquire_velocity_m_s, 0.003),
                    )
                    state.normal_velocity_m_s = min(state.normal_velocity_m_s, -unload_speed)
                if (
                    args.bridge_mode == "line"
                    and state.normal_acquired
                    and normal_load_n < args.bridge_min_force_for_control_n
                    and force_error > 0.0
                ):
                    state.normal_velocity_m_s = max(
                        state.normal_velocity_m_s,
                        min(args.bridge_reacquire_velocity_m_s, args.bridge_normal_velocity_limit_m_s),
                    )
                force_cmd = tuple(
                    -args.bridge_normal_command_sign * n_control_b[idx] * state.normal_velocity_m_s
                    for idx in range(3)
                )
                cmd = tuple(motion_cmd[idx] + force_cmd[idx] for idx in range(3))
                if detached_profile and acquire_stage_active:
                    orientation_cmd = (0.0, 0.0, 0.0)
        cmd_norm = norm3(cmd)
        if cmd_norm > args.bridge_total_linear_limit_m_s:
            scale = args.bridge_total_linear_limit_m_s / cmd_norm
            cmd = tuple(value * scale for value in cmd)
        if angular_speedl_profile and orient_stage_active and (
            abs(cmd[0]) > 1e-12 or abs(cmd[1]) > 1e-12 or abs(cmd[2]) > 1e-12
        ):
            raise RuntimeError("Step4e v23..v31 stage 25.2 contract violation: linear command registers must be zero")
        step5b_15n_trial_stop_reason = None
        if step5b_15n_trial_profile and line_stage_active:
            step5b_15n_trial_stop_reason = step5b_15n_trial_guard_reason(
                normal_load_n=normal_load_n,
                force_norm_n=force_abs,
                torque_norm_nm=torque_abs,
                sensor_ok=sensor_ok,
                normal_velocity_m_s=state.normal_velocity_m_s,
                normal_velocity_limit_m_s=float(args.bridge_normal_velocity_limit_m_s),
                dt_s=dt_s,
                state=state,
            )
        elif step5b_ramp_trial_profile and line_stage_active:
            step5b_15n_trial_stop_reason = step5b_ramp_trial_guard_reason(
                normal_load_n=normal_load_n,
                force_norm_n=force_abs,
                torque_norm_nm=torque_abs,
                sensor_ok=sensor_ok,
                normal_velocity_m_s=state.normal_velocity_m_s,
                normal_velocity_limit_m_s=float(args.bridge_normal_velocity_limit_m_s),
                dt_s=dt_s,
                state=state,
            )
        joint_result = None
        step5d_result = None
        step5d_qdot_command = None
        step5d_outer_output = None
        step5d_outer_xdot_limited: np.ndarray | None = None
        step5d_outer_xdot_limiter_active = False
        step5d_qdot_slew_limiter_active = False
        step5d_engage_gate_ok = True
        step5d_line_guard_ok = True
        step5d_line_guard_reason = "not_active"
        if step5d_joint_line_profile:
            try:
                actual_q = latest_output.get("actual_q")
                actual_qd = latest_output.get("actual_qd")
                if not actual_q or len(actual_q) < 6:
                    raise ValueError("missing RTDE actual_q")
                if not actual_qd or len(actual_qd) < 6:
                    raise ValueError("missing RTDE actual_qd")
                if not speed or len(speed) < 6:
                    raise ValueError("missing RTDE actual_TCP_speed")
                ensure_step5d_liveprep_runtime(state, args)
                if (
                    state.step5d_model_bundle is None
                    or state.step5d_tcp_offset_tool0 is None
                    or state.step5d_solver is None
                ):
                    raise RuntimeError("Step5d liveprep runtime did not initialize")
                q = np.asarray(actual_q[:6], dtype=float)
                qd = np.asarray(actual_qd[:6], dtype=float)
                if step5d_contact_safety_profile and step5d_contact_safety["action"] in {"hold_zero_qdot", "stop_zero_qdot"}:
                    if step5d_contact_safety["action"] == "stop_zero_qdot":
                        step5d_engage_gate_ok = False
                    raise RuntimeError(
                        "Step5d contact safety "
                        f"{step5d_contact_safety['action']}: {step5d_contact_safety['reason']}"
                    )
                if step5d_liveprep_v3_profile and not step5d_force_settle_ready(
                    normal_load_n=normal_load_n,
                    force_norm_n=force_abs,
                    target_force_n=float(args.target_force_n),
                ):
                    step5d_engage_gate_ok = False
                    raise ValueError("Step5d v3 engage gate blocked: force/load outside 5N entry window")
                if (
                    step5d_liveprep_v4_profile
                    or step5d_liveprep_v5_profile
                    or step5d_liveprep_v6_profile
                    or step5d_liveprep_v7_profile
                    or step5d_liveprep_v8_profile
                    or step5d_liveprep_v9_profile
                    or step5d_liveprep_v11_profile
                ):
                    contact_min_n, contact_max_n, contact_force_norm_max_n = step5d_liveprep_contact_window_limits(args.bridge_profile)
                    if not step5d_contact_window_ready(
                        normal_load_n=normal_load_n,
                        force_norm_n=force_abs,
                        min_normal_load_n=contact_min_n,
                        max_normal_load_n=contact_max_n,
                        max_force_norm_n=contact_force_norm_max_n,
                    ):
                        step5d_engage_gate_ok = False
                        raise ValueError(
                            "Step5d contact-window engage gate blocked: "
                            f"force/load outside {contact_min_n:g}-{contact_max_n:g}N load "
                            f"and <= {contact_force_norm_max_n:g}N force_norm window"
                        )
                if step5d_liveprep_v12_profile:
                    step5d_line_guard_ok, state.step5d_line_guard_loss_s, step5d_line_guard_reason = step5d_v12_line_guard(
                        normal_load_n=normal_load_n,
                        force_norm_n=force_abs,
                        tcp_linear_speed_m_s=step5d_line_tcp_speed_m_s,
                        prior_loss_s=state.step5d_line_guard_loss_s,
                        dt_s=dt_s,
                    )
                    if not step5d_line_guard_ok:
                        step5d_engage_gate_ok = False
                        raise ValueError(f"Step5d v12 Stage25 line guard blocked: {step5d_line_guard_reason}")
                jacobian = step5d_tcp_jacobian_base(state.step5d_model_bundle, q, state.step5d_tcp_offset_tool0)
                omega_minus, omega_plus = step5d_omega_bounds(
                    q,
                    state.step5d_model_bundle.model.lowerPositionLimit,
                    state.step5d_model_bundle.model.upperPositionLimit,
                    alpha_s_inv=float(args.step5d_alpha_s_inv),
                    qdot_limit_rad_s=float(args.step5d_qdot_limit_rad_s),
                )
                step5d_outer_output = compute_step5d_outer_loop(
                    Step5dOuterLoopConfig(
                        kp=4.0,
                        ko=5.0,
                        kf=1.0,
                        Md_scalar=12.0,
                        Bd_scalar=550.0,
                        force_target_n=float(args.target_force_n),
                        delay_T_s=dt_s,
                        force_sign_convention="step5_step6_positive_normal_load",
                    ),
                    state.step5d_outer_state,
                    Step5dOuterLoopInputs(
                        tcp_pose_base=tuple(float(value) for value in pose[:6]),  # type: ignore[arg-type]
                        tcp_speed_base=tuple(float(value) for value in speed[:6]),  # type: ignore[arg-type]
                        force_tcp_n=force_t,
                        control_reaction_normal_base=tuple(float(value) for value in n_control_b),  # type: ignore[arg-type]
                        x_pd_base=(float(desired_x), float(desired_y), float(pose[2])),
                        xdot_pd_base=(float(desired_velocity_xy[0]), float(desired_velocity_xy[1]), 0.0),
                        dt_s=dt_s,
                        cmd_valid=True,
                    ),
                )
                outer_orientation_error_rad = float(step5d_outer_output.diagnostics["outer_orientation_angle_rad"])
                if not semantic_boundary_is_consistent(
                    contact_orientation_error_rad=orientation_error,
                    outer_orientation_error_rad=outer_orientation_error_rad,
                    tolerance_rad=STEP5D_SEMANTIC_ORIENTATION_TOLERANCE_RAD,
                ):
                    step5d_engage_gate_ok = False
                    raise ValueError(
                        "Step5d semantic gate blocked: contact orientation "
                        f"{orientation_error:.6f} rad vs outer orientation {outer_orientation_error_rad:.6f} rad"
                    )
                state.step5d_outer_state = step5d_outer_output.next_state
                target_state = rnn_target_state_from_outer_loop(
                    step5d_outer_output,
                    J=jacobian,
                    omega_minus=omega_minus,
                    omega_plus=omega_plus,
                    dt_s=dt_s,
                    epsilon=float(args.step5d_epsilon),
                    r=float(args.step5d_sigr_exponent_r),
                )
                step5d_outer_xdot_limited = np.asarray(step5d_outer_output.xdot_c, dtype=float)
                if step5d_liveprep_guarded_profile:
                    step5d_outer_xdot_limited, step5d_outer_xdot_limiter_active = limit_step5d_live_xdot(
                        step5d_outer_xdot_limited,
                        max_linear_m_s=float(args.bridge_total_linear_limit_m_s),
                        max_angular_rad_s=float(args.bridge_angular_limit_rad_s),
                    )
                    target_state["xdot_c"] = step5d_outer_xdot_limited
                step5d_result = state.step5d_solver.solve(actual_q=q, actual_qd=qd, target_state=target_state)
                step5d_qdot_command = step5d_result.qdot
                if step5d_liveprep_v12_profile or step5d_liveprep_v13_profile or step5d_liveprep_v14_profile or step5d_liveprep_v15_profile or step5d_liveprep_v15a_profile:
                    qdot_limited, step5d_qdot_slew_limiter_active = limit_step5d_qdot_slew(
                        step5d_result.qdot,
                        state.step5d_last_qdot,
                        dt_s=dt_s,
                    )
                    step5d_qdot_command = tuple(float(value) for value in qdot_limited.tolist())
                    state.step5d_last_qdot = qdot_limited
                if step5d_contact_safety_profile:
                    predicted_twist = jacobian @ np.asarray(step5d_qdot_command, dtype=float)
                    step5d_predicted_tcp_speed_m_s = float(np.linalg.norm(predicted_twist[:3]))
                    if step5d_liveprep_v15a_profile:
                        if state.step5d_tcp_cage is None:
                            step5d_tcp_cage = {
                                "distance_m": math.nan,
                                "braking_margin_m": math.nan,
                                "signed_distance_m": math.nan,
                                "cell_index": -1.0,
                                "reason": "missing_tcp_cage",
                            }
                        else:
                            step5d_tcp_cage = state.step5d_tcp_cage.evaluate(
                                pose,
                                actual_tcp_speed_m_s=step5d_line_tcp_speed_m_s,
                                predicted_tcp_speed_m_s=step5d_predicted_tcp_speed_m_s,
                            )
                    step5d_contact_safety = step5d_contact_safety_fn(
                        normal_load_n=normal_load_n,
                        force_norm_n=force_abs,
                        actual_tcp_speed_m_s=step5d_line_tcp_speed_m_s,
                        predicted_tcp_speed_m_s=step5d_predicted_tcp_speed_m_s,
                        braking_margin_m=(
                            float(step5d_tcp_cage["braking_margin_m"])
                            if step5d_liveprep_v15a_profile
                            else None
                        ),
                        prior_hold_s=state.step5d_contact_hold_s,
                        prior_high_window_s=state.step5d_contact_high_window_s,
                        prior_actual_speed_violation_s=state.step5d_actual_speed_violation_s,
                        prior_actual_speed_violation_count=state.step5d_actual_speed_violation_count,
                        prior_consecutive_hold_s=state.step5d_consecutive_hold_s,
                        prior_total_hold_s=state.step5d_total_hold_s,
                        prior_hold_event_count=state.step5d_hold_event_count,
                        prior_last_hold_reason=state.step5d_last_hold_reason,
                        prior_hold_actual_tcp_speed_m_s=state.step5d_hold_actual_tcp_speed_m_s,
                        active_stage25_s=state.step5d_active_stage25_s,
                        dt_s=dt_s,
                        advance_actual_speed_dwell=False,
                    )
                    state.step5d_contact_hold_s = float(step5d_contact_safety["hold_s"])
                    state.step5d_contact_high_window_s = float(step5d_contact_safety["high_window_s"])
                    state.step5d_actual_speed_violation_s = float(step5d_contact_safety["actual_speed_violation_s"])
                    state.step5d_actual_speed_violation_count = int(step5d_contact_safety["actual_speed_violation_count"])
                    state.step5d_consecutive_hold_s = float(step5d_contact_safety.get("consecutive_hold_s", 0.0))
                    state.step5d_total_hold_s = float(step5d_contact_safety.get("total_hold_s", state.step5d_total_hold_s))
                    state.step5d_hold_event_count = int(float(step5d_contact_safety.get("hold_event_count", state.step5d_hold_event_count)))
                    state.step5d_repeated_hold_count = int(float(step5d_contact_safety.get("repeated_hold_count", state.step5d_repeated_hold_count)))
                    state.step5d_last_hold_reason = str(step5d_contact_safety.get("last_hold_reason", state.step5d_last_hold_reason))
                    hold_speed = float(step5d_contact_safety.get("hold_actual_tcp_speed_m_s", math.nan))
                    state.step5d_hold_actual_tcp_speed_m_s = hold_speed if math.isfinite(hold_speed) else None
                    if step5d_contact_safety["action"] in {"hold_zero_qdot", "stop_zero_qdot"}:
                        if step5d_contact_safety["action"] == "stop_zero_qdot":
                            step5d_contact_safety_stop = True
                            step5d_engage_gate_ok = False
                        if step5d_contact_safety["action"] == "hold_zero_qdot" and state.step5d_contact_hold_path_time_s is None:
                            state.step5d_contact_hold_path_time_s = max(0.0, state.line_stage_s - max(0.0, dt_s))
                        if step5d_contact_safety["action"] == "hold_zero_qdot":
                            state.line_stage_s = state.step5d_contact_hold_path_time_s
                        zero_qdot = np.zeros(6, dtype=float)
                        step5d_qdot_command = tuple(float(value) for value in zero_qdot.tolist())
                        state.step5d_outer_state = Step5dOuterLoopState()
                        state.step5d_last_qdot = None
                cmd = (step5d_qdot_command[0], step5d_qdot_command[1], step5d_qdot_command[2])
                orientation_cmd = (step5d_qdot_command[3], step5d_qdot_command[4], step5d_qdot_command[5])
            except (ValueError, RuntimeError) as exc:
                if not str(exc).startswith("Step5d contact safety"):
                    values["_step5d_solver_error"] = str(exc)
                state.step5d_outer_state = Step5dOuterLoopState()
                state.step5d_last_qdot = None
                cmd = (0.0, 0.0, 0.0)
                orientation_cmd = (0.0, 0.0, 0.0)
                step5d_result = None
                step5d_outer_output = None
        elif step5c_joint_profile:
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
        if step5c_joint_profile or step5d_joint_line_profile:
            if step5d_joint_line_profile and step5d_outer_output is not None:
                register_force_error = float(step5d_outer_output.diagnostics.get("e_f", force_error))
                register_pose_error = float(step5d_outer_output.diagnostics.get("outer_orientation_angle_rad", orientation_error))
                register_status = step5d_result.solver_status if step5d_result is not None else STATUS_INVALID
            else:
                register_force_error = force_error
                register_pose_error = orientation_error
                register_status = joint_result.solver_status if joint_result is not None else STATUS_INVALID
            values.update(
                step5c_joint_register_values(
                    (
                        step5d_result.qdot
                        if step5d_joint_line_profile and step5d_result is not None and step5d_qdot_command is None
                        else step5d_qdot_command
                        if step5d_joint_line_profile and step5d_qdot_command is not None
                        else joint_result.qdot
                        if joint_result is not None
                        else (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
                    ),
                    cmd_valid=0.0
                    if args.bridge_mode == "preview"
                    or (
                        joint_result is None
                        and step5d_result is None
                        and not (
                            step5d_contact_safety_profile
                            and step5d_contact_safety["action"] in {"hold_zero_qdot", "stop_zero_qdot"}
                        )
                    )
                    else 1.0,
                    path_time_s=progress,
                    force_error_n=register_force_error,
                    pose_or_orientation_error=register_pose_error,
                    solver_status=register_status,
                )
            )
        else:
            register_force_error = force_error
            if (
                (step5d_liveprep_v10_profile or step5d_liveprep_v11_profile or step5d_liveprep_v12_profile or step5d_liveprep_v13_profile or step5d_liveprep_v14_profile or step5d_liveprep_v15_profile or step5d_liveprep_v15a_profile)
                and line_entry_gate_active
                and state.step5d_settle_filtered_normal_load_n is not None
            ):
                register_force_error = float(args.target_force_n) - state.step5d_settle_filtered_normal_load_n
            values.update(
                {
                    "step4e_cmd_vx_m_s": n_control_b[0] if (v21_profile and detach_stage_active) else cmd[0],
                    "step4e_cmd_vy_m_s": n_control_b[1] if (v21_profile and detach_stage_active) else cmd[1],
                    "step4e_cmd_vz_m_s": n_control_b[2] if (v21_profile and detach_stage_active) else cmd[2],
                    "step4e_cmd_wx_rad_s": orientation_cmd[0],
                    "step4e_cmd_wy_rad_s": orientation_cmd[1],
                    "step4e_cmd_wz_rad_s": orientation_cmd[2] if (axis_iso_active or v21_profile or v22_profile or angular_speedl_profile) else 0.0,
                    "step4e_cmd_valid": 0.0
                    if args.bridge_mode == "preview"
                    or (
                        (step5d_liveprep_v8_profile or step5d_liveprep_v9_profile or step5d_liveprep_v10_profile or step5d_liveprep_v11_profile)
                        and line_entry_gate_active
                        and not step5d_v8_pid_recovery_ok
                    )
                    else 1.0,
                    "step4e_progress_m": progress,
                    "step4e_force_error_n": register_force_error,
                    "step4e_orientation_error_rad": orientation_error,
                    "step4e_controller_state": (
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
                        else {"preview": 10.0, "hold": 20.0, "line": 30.0, "axis_iso": 34.0}[args.bridge_mode]
                    ),
                }
            )
        if step5d_joint_line_profile and step5d_result is not None and step5d_outer_output is not None:
            qdot_abs = [abs(float(value)) for value in (step5d_qdot_command or step5d_result.qdot)]
            values["_step5d_solver_status"] = step5d_result.solver_status
            values["_step5d_qdot_max_abs_rad_s"] = max(qdot_abs)
            values["_step5d_constraint_residual_norm"] = step5d_result.residual_norm
            values["_step5d_outer_xdot_norm"] = float(np.linalg.norm(np.asarray(step5d_outer_output.xdot_c, dtype=float)))
            values["_step5d_outer_xdot_limited_norm"] = (
                float(np.linalg.norm(step5d_outer_xdot_limited)) if step5d_outer_xdot_limited is not None else values["_step5d_outer_xdot_norm"]
            )
            values["_step5d_outer_xdot_limiter_active"] = 1.0 if step5d_outer_xdot_limiter_active else 0.0
            values["_step5d_qdot_slew_limiter_active"] = 1.0 if step5d_qdot_slew_limiter_active else 0.0
            values["_step5d_engage_gate_ok"] = 1.0 if step5d_engage_gate_ok else 0.0
            values["_step5d_line_guard_ok"] = 1.0 if step5d_line_guard_ok else 0.0
            values["_step5d_line_guard_loss_s"] = state.step5d_line_guard_loss_s
            values["_step5d_line_guard_reason"] = step5d_line_guard_reason
            values["_step5d_line_tcp_speed_m_s"] = step5d_line_tcp_speed_m_s
            values["_step5d_force_sign_convention"] = step5d_outer_output.diagnostics["force_sign_convention"]
            values["_step5d_proj_input_form"] = step5d_result.diagnostics["proj_input_form"]
            values["_step5d_active_bounds_count"] = float(sum(bool(value) for value in step5d_result.diagnostics["active_bounds_mask"]))
            values["_step5d_contact_orientation_error_rad"] = orientation_error
            values["_step5d_outer_orientation_error_rad"] = float(step5d_outer_output.diagnostics["outer_orientation_angle_rad"])
            values["_step5d_R_d_z_dot_R_cur_z"] = float(step5d_outer_output.diagnostics["R_d_z_dot_R_cur_z"])
            values["_step5d_semantic_gate_ok"] = 1.0
        elif step5d_joint_line_profile:
            values["_step5d_engage_gate_ok"] = 1.0 if step5d_engage_gate_ok else 0.0
            values["_step5d_line_guard_ok"] = 1.0 if step5d_line_guard_ok else 0.0
            values["_step5d_line_guard_loss_s"] = state.step5d_line_guard_loss_s
            values["_step5d_line_guard_reason"] = step5d_line_guard_reason
            values["_step5d_line_tcp_speed_m_s"] = step5d_line_tcp_speed_m_s
            values["_step5d_contact_orientation_error_rad"] = orientation_error
            values["_step5d_semantic_gate_ok"] = 1.0 if step5d_engage_gate_ok else 0.0
        if step5d_joint_line_profile:
            contact_state = str(step5d_contact_safety["state"])
            contact_action = str(step5d_contact_safety["action"])
            values["_step5d_contact_safety_state"] = STEP5D_V13_CONTACT_SAFETY_STATES.get(contact_state, 0.0)
            values["_step5d_contact_safety_action"] = STEP5D_V13_CONTACT_SAFETY_ACTIONS.get(contact_action, 0.0)
            values["_step5d_contact_hold_s"] = state.step5d_contact_hold_s
            values["_step5d_contact_high_window_s"] = state.step5d_contact_high_window_s
            values["_step5d_actual_speed_violation_s"] = state.step5d_actual_speed_violation_s
            values["_step5d_actual_speed_violation_count"] = float(state.step5d_actual_speed_violation_count)
            values["_step5d_actual_tcp_speed_m_s"] = step5d_line_tcp_speed_m_s
            values["_step5d_predicted_tcp_speed_m_s"] = step5d_predicted_tcp_speed_m_s
            values["_step5d_tcp_cage_distance_m"] = step5d_tcp_cage["distance_m"]
            values["_step5d_tcp_cage_braking_margin_m"] = step5d_tcp_cage["braking_margin_m"]
            values["_step5d_tcp_cage_signed_distance_m"] = step5d_tcp_cage["signed_distance_m"]
            values["_step5d_tcp_cage_cell_index"] = step5d_tcp_cage["cell_index"]
            values["_step5d_tcp_cage_reason"] = step5d_tcp_cage["reason"]
            values["_step5d_hold_event_count"] = float(state.step5d_hold_event_count)
            values["_step5d_consecutive_hold_s"] = state.step5d_consecutive_hold_s
            values["_step5d_total_hold_s"] = state.step5d_total_hold_s
            values["_step5d_hold_duty"] = (
                state.step5d_total_hold_s / state.step5d_active_stage25_s
                if state.step5d_active_stage25_s > 0.0
                else 0.0
            )
            values["_step5d_repeated_hold_count"] = float(state.step5d_repeated_hold_count)
            values["_step5d_contact_safety_reason"] = step5d_contact_safety["reason"]
            values["_step5d_control_normal_vs_world_z_angle_rad"] = angle_between_unit(n_control_b, (0.0, 0.0, 1.0))
            values["_step5d_control_normal_vs_tcp_z_angle_rad"] = angle_between_unit(n_control_b, tcp_z_axis_b)
            values["_step5d_approach_normal_vs_tcp_z_angle_rad"] = angle_between_unit(
                (-n_control_b[0], -n_control_b[1], -n_control_b[2]),
                tcp_z_axis_b,
            )
            if step5d_contact_safety_stop:
                values["stop_request"] = 1.0
        if step5c_joint_profile and joint_result is not None:
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

    if step5b_15n_trial_profile:
        values["_step5b_15n_trial_active"] = 1.0 if line_stage_active else 0.0
        values["_step5b_15n_trial_acquired"] = 1.0 if state.step5b_15n_acquired else 0.0
        values["_step5b_15n_trial_after_acquire_s"] = state.step5b_15n_after_acquire_s
        values["_step5b_15n_trial_scored_s"] = state.step5b_15n_scored_s
        values["_step5b_15n_trial_anchor_x_m"] = "" if state.step5b_15n_anchor_xy is None else state.step5b_15n_anchor_xy[0]
        values["_step5b_15n_trial_anchor_y_m"] = "" if state.step5b_15n_anchor_xy is None else state.step5b_15n_anchor_xy[1]
        values["_step5b_15n_trial_low_load_s"] = state.step5b_15n_low_load_s
        values["_step5b_15n_trial_relative_low_load_s"] = state.step5b_15n_relative_low_load_s
        values["_step5b_15n_trial_saturation_s"] = state.step5b_15n_saturation_s
        values["_step5b_15n_trial_normal_velocity_saturated"] = (
            1.0
            if args.bridge_normal_velocity_limit_m_s > 0.0
            and abs(state.normal_velocity_m_s) >= 0.98 * args.bridge_normal_velocity_limit_m_s
            else 0.0
        )
        values["_step5b_15n_trial_stop_reason"] = step5b_15n_trial_stop_reason or ""
        if step5b_15n_trial_stop_reason:
            values["stop_request"] = 1.0
    if step5b_ramp_trial_profile:
        values["_step5b_ramp_active"] = 1.0 if line_stage_active else 0.0
        values["_step5b_ramp_phase_code"] = STEP5B_RAMP_PHASE_CODES.get(state.step5b_ramp_phase, -1.0)
        values["_step5b_ramp_phase"] = state.step5b_ramp_phase
        values["_step5b_ramp_phase_s"] = state.step5b_ramp_phase_s
        values["_step5b_ramp_active_target_force_n"] = state.step5b_ramp_active_target_force_n
        values["_step5b_ramp_alpha"] = state.step5b_ramp_alpha
        values["_step5b_ramp_xy_enabled"] = 1.0 if state.step5b_ramp_xy_enabled else 0.0
        values["_step5b_ramp_preload_ready_s"] = state.step5b_ramp_preload_ready_s
        values["_step5b_ramp_terminal_discard_s"] = state.step5b_ramp_terminal_discard_s
        values["_step5b_ramp_scored_s"] = state.step5b_ramp_scored_s
        values["_step5b_ramp_move_s"] = state.step5b_ramp_move_s
        values["_step5b_ramp_low_load_s"] = state.step5b_ramp_low_load_s
        values["_step5b_ramp_relative_low_load_s"] = state.step5b_ramp_relative_low_load_s
        values["_step5b_ramp_saturation_s"] = state.step5b_ramp_saturation_s
        values["_step5b_ramp_normal_velocity_saturated"] = (
            1.0
            if args.bridge_normal_velocity_limit_m_s > 0.0
            and abs(state.normal_velocity_m_s) >= 0.98 * args.bridge_normal_velocity_limit_m_s
            else 0.0
        )
        values["_step5b_ramp_stop_reason"] = step5b_15n_trial_stop_reason or ""
        if step5b_15n_trial_stop_reason:
            values["stop_request"] = 1.0
    elif not step5b_15n_trial_profile:
        values["_step5b_ramp_active"] = 0.0

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
    values["_step4e_normal_follow_mode"] = args.bridge_normal_follow_mode
    values["_step4e_normal_load_n"] = normal_load_n
    values["_step4e_normal_force_error_n"] = force_error
    values["_step4e_normal_acquired"] = 1.0 if state.normal_acquired else 0.0
    if step5d_liveprep_profile:
        values["_step5d_force_settle_filtered_normal_load_n"] = (
            state.step5d_settle_filtered_normal_load_n
            if state.step5d_settle_filtered_normal_load_n is not None
            else float("nan")
        )
        values["_step5d_force_settle_velocity_m_s"] = state.normal_velocity_m_s
        if step5d_liveprep_v10_profile or step5d_liveprep_v11_profile or step5d_liveprep_v12_profile:
            filtered_load = state.step5d_settle_filtered_normal_load_n
            min_load, max_load, max_force = step5d_liveprep_contact_window_limits(args.bridge_profile)
            values["_step5d_force_settle_ready"] = 1.0 if (
                filtered_load is not None
                and step5d_contact_window_ready(
                    normal_load_n=filtered_load,
                    force_norm_n=force_abs,
                    min_normal_load_n=min_load,
                    max_normal_load_n=max_load,
                    max_force_norm_n=max_force,
                )
                and (
                    step5d_liveprep_v11_profile
                    or step5d_liveprep_v12_profile
                    or step5d_liveprep_v13_profile
                    or step5d_liveprep_v14_profile
                    or step5d_liveprep_v15_profile
                    or step5d_liveprep_v15a_profile
                    or abs(state.normal_velocity_m_s) <= STEP5D_V10_SETTLE_VELOCITY_READY_M_S
                )
            ) else 0.0
        elif args.bridge_profile in {
            STEP5D_LIVEPREP_V4_STAGE_ID,
            STEP5D_LIVEPREP_V5_STAGE_ID,
            STEP5D_LIVEPREP_V6_STAGE_ID,
            STEP5D_LIVEPREP_V7_STAGE_ID,
            STEP5D_LIVEPREP_V8_STAGE_ID,
            STEP5D_LIVEPREP_V9_STAGE_ID,
            STEP5D_LIVEPREP_V12_STAGE_ID,
            STEP5D_LIVEPREP_V13_STAGE_ID,
            STEP5D_LIVEPREP_STAGE_ID,
            STEP5D_LIVEPREP_V15_STAGE_ID,
            STEP5D_LIVEPREP_V15A_STAGE_ID,
        }:
            contact_min_n, contact_max_n, contact_force_norm_max_n = step5d_liveprep_contact_window_limits(args.bridge_profile)
            values["_step5d_force_settle_ready"] = 1.0 if step5d_contact_window_ready(
                normal_load_n=normal_load_n,
                force_norm_n=force_abs,
                min_normal_load_n=contact_min_n,
                max_normal_load_n=contact_max_n,
                max_force_norm_n=contact_force_norm_max_n,
            ) else 0.0
        else:
            values["_step5d_force_settle_ready"] = 1.0 if step5d_force_settle_ready(
                normal_load_n=normal_load_n,
                force_norm_n=force_abs,
                target_force_n=float(args.target_force_n),
            ) else 0.0
    values["_step4e_line_stage_s"] = state.line_stage_s
    values["_step4e_path_shape"] = args.bridge_path_shape
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


compute_step4e_values = compute_bridge_values


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


def finite_csv_float(row: dict[str, str], field: str, default: float = math.nan) -> float:
    try:
        value = float(row.get(field, ""))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def step5b_15n_trial_verdict(payload: dict[str, Any]) -> str:
    stop_reason = str(payload.get("stop_reason") or "")
    if stop_reason in {"step5b_15n_trial:normal_load_stop", "step5b_15n_trial:normal_load_dwell", "step5b_15n_trial:force_norm_stop", "step5b_15n_trial:force_norm_dwell", "step5b_15n_trial:torque_norm_stop", "step5b_15n_trial:torque_norm_dwell"}:
        return "fail_high_force"
    if stop_reason in {"step5b_15n_trial:low_load_dropout", "step5b_15n_trial:relative_low_load_dropout", "step5b_15n_trial:acquisition_timeout"}:
        return "fail_dropout_not_improved"
    if stop_reason == "step5b_15n_trial:normal_velocity_saturation":
        return "fail_saturation"
    if stop_reason not in {"step5b_15n_trial:complete", "duration"}:
        return "invalid_run"
    metrics = payload.get("metrics", {})
    normal = metrics.get("normal_load_n", {})
    if not metrics.get("scored_rows"):
        return "invalid_run"
    if normal.get("p50") is None or normal.get("p95") is None or normal.get("max") is None:
        return "invalid_run"
    if not (12.0 <= float(normal["p50"]) <= 18.0):
        return "fail_dropout_not_improved"
    if float(normal["p95"]) > 20.0 or float(normal["max"]) > 22.0:
        return "fail_high_force"
    if float(metrics.get("dropout_lt2_duty", 1.0)) > 0.0:
        return "fail_dropout_not_improved"
    if float(metrics.get("normal_velocity_saturation_duty", 1.0)) > 0.02:
        return "fail_saturation"
    return "pass_to_repeat_static"


def write_step5b_15n_trial_summary(
    *,
    output_dir: Path,
    bridge_csv_path: Path,
    metadata: dict[str, Any],
    stop_reason: str,
) -> dict[str, Any]:
    active_rows = 0
    scored_rows = 0
    normal_loads: list[float] = []
    force_norms: list[float] = []
    torque_norms: list[float] = []
    dropout_lt2 = 0
    dropout_lt75 = 0
    saturation_rows = 0
    first_trial_stop = ""
    with bridge_csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if finite_csv_float(row, "_step5b_15n_trial_active", 0.0) <= 0.5:
                continue
            active_rows += 1
            reason = str(row.get("_step5b_15n_trial_stop_reason") or "")
            if reason and not first_trial_stop:
                first_trial_stop = reason
            scored = finite_csv_float(row, "_step5b_15n_trial_scored_s", 0.0) > 0.0
            if not scored:
                continue
            normal = finite_csv_float(row, "_step4e_normal_load_n")
            force_norm = finite_csv_float(row, "force_norm_n")
            torque_norm = finite_csv_float(row, "torque_norm_nm")
            if not all(math.isfinite(value) for value in (normal, force_norm, torque_norm)):
                continue
            scored_rows += 1
            normal_loads.append(normal)
            force_norms.append(force_norm)
            torque_norms.append(torque_norm)
            dropout_lt2 += int(normal < STEP5B_15N_TRIAL_LOW_LOAD_N)
            dropout_lt75 += int(normal < STEP5B_15N_TRIAL_RELATIVE_LOW_LOAD_N)
            saturation_rows += int(finite_csv_float(row, "_step5b_15n_trial_normal_velocity_saturated", 0.0) > 0.5)
    payload: dict[str, Any] = {
        "profile": STEP5B_15N_TRIAL_PROFILE,
        "target_force_n": metadata["args"].get("target_force_n"),
        "stop_reason": first_trial_stop or stop_reason,
        "active_rows": active_rows,
        "metrics": {
            "scored_rows": scored_rows,
            "normal_load_n": {
                "p50": percentile(normal_loads, 0.50),
                "p95": percentile(normal_loads, 0.95),
                "max": max(normal_loads) if normal_loads else None,
            },
            "force_norm_n": {
                "max": max(force_norms) if force_norms else None,
            },
            "torque_norm_nm": {
                "max": max(torque_norms) if torque_norms else None,
            },
            "dropout_lt2_duty": dropout_lt2 / scored_rows if scored_rows else None,
            "dropout_lt7p5_duty": dropout_lt75 / scored_rows if scored_rows else None,
            "normal_velocity_saturation_duty": saturation_rows / scored_rows if scored_rows else None,
        },
        "paths": {
            "bridge_csv": str(bridge_csv_path),
            "metadata": str(output_dir / "metadata.json"),
        },
    }
    payload["verdict"] = step5b_15n_trial_verdict(payload)
    summary_path = output_dir / "step5b_15n_guarded_trial_summary.json"
    write_json(summary_path, payload)
    md_path = output_dir / "step5b_15n_guarded_trial_summary.md"
    md_path.write_text(
        "\n".join(
            [
                "# Step5b 15N guarded trial summary",
                "",
                f"- profile: `{payload['profile']}`",
                f"- target_force_n: `{payload['target_force_n']}`",
                f"- stop_reason: `{payload['stop_reason']}`",
                f"- verdict: `{payload['verdict']}`",
                f"- active_rows: `{payload['active_rows']}`",
                f"- scored_rows: `{payload['metrics']['scored_rows']}`",
                f"- normal_load_p50_n: `{payload['metrics']['normal_load_n']['p50']}`",
                f"- normal_load_p95_n: `{payload['metrics']['normal_load_n']['p95']}`",
                f"- normal_load_max_n: `{payload['metrics']['normal_load_n']['max']}`",
                f"- dropout_lt2_duty: `{payload['metrics']['dropout_lt2_duty']}`",
                f"- normal_velocity_saturation_duty: `{payload['metrics']['normal_velocity_saturation_duty']}`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    payload["paths"]["trial_summary_json"] = str(summary_path)
    payload["paths"]["trial_summary_md"] = str(md_path)
    return payload


def step5b_ramp_trial_verdict(payload: dict[str, Any]) -> str:
    stop_reason = str(payload.get("stop_reason") or "")
    if stop_reason in {
        "step5b_ramp_5_to_15:preload_normal_stop",
        "step5b_ramp_5_to_15:preload_force_norm_stop",
        "step5b_ramp_5_to_15:normal_load_stop",
        "step5b_ramp_5_to_15:normal_load_dwell",
        "step5b_ramp_5_to_15:force_norm_stop",
        "step5b_ramp_5_to_15:force_norm_dwell",
        "step5b_ramp_5_to_15:torque_norm_stop",
        "step5b_ramp_5_to_15:torque_norm_dwell",
    }:
        return "fail_high_force"
    if stop_reason in {
        "step5b_ramp_5_to_15:low_load_dropout",
        "step5b_ramp_5_to_15:relative_low_load_dropout",
        "step5b_ramp_5_to_15:preload_acquisition_timeout",
    }:
        return "fail_dropout_not_improved"
    if stop_reason == "step5b_ramp_5_to_15:normal_velocity_saturation":
        return "fail_saturation"
    if stop_reason != "step5b_ramp_5_to_15:complete":
        return "invalid_run"

    metrics = payload.get("metrics", {})
    normal = metrics.get("move_normal_load_n", {})
    if not payload.get("active_rows") or not metrics.get("move_rows"):
        return "invalid_run"
    if not metrics.get("target_monotonic") or not metrics.get("target_reached_final"):
        return "invalid_run"
    if metrics.get("xy_motion_before_move_rows", 1) != 0:
        return "invalid_run"
    if normal.get("p50") is None or normal.get("p95") is None or normal.get("max") is None:
        return "invalid_run"
    if not (12.0 <= float(normal["p50"]) <= 18.0):
        return "fail_dropout_not_improved"
    if float(normal["p95"]) > 20.0 or float(normal["max"]) > 22.0:
        return "fail_high_force"
    if float(metrics.get("move_dropout_lt2_duty", 1.0)) > 0.0:
        return "fail_dropout_not_improved"
    if float(metrics.get("move_normal_velocity_saturation_duty", 1.0)) > 0.02:
        return "fail_saturation"
    return "pass_ramp_and_short_move"


def write_step5b_ramp_trial_summary(
    *,
    output_dir: Path,
    bridge_csv_path: Path,
    metadata: dict[str, Any],
    stop_reason: str,
) -> dict[str, Any]:
    active_rows = 0
    move_rows = 0
    target_values: list[float] = []
    move_normal_loads: list[float] = []
    move_force_norms: list[float] = []
    move_torque_norms: list[float] = []
    phase_counts: dict[str, int] = {}
    xy_motion_before_move_rows = 0
    dropout_lt2 = 0
    saturation_rows = 0
    first_trial_stop = ""
    with bridge_csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if finite_csv_float(row, "_step5b_ramp_active", 0.0) <= 0.5:
                continue
            active_rows += 1
            phase = str(row.get("_step5b_ramp_phase") or "")
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
            reason = str(row.get("_step5b_ramp_stop_reason") or "")
            if reason and not first_trial_stop:
                first_trial_stop = reason
            target = finite_csv_float(row, "_step5b_ramp_active_target_force_n")
            if math.isfinite(target):
                target_values.append(target)
            xy_enabled = finite_csv_float(row, "_step5b_ramp_xy_enabled", 0.0) > 0.5
            desired_vx = finite_csv_float(row, "_step4e_desired_vx_m_s", 0.0)
            desired_vy = finite_csv_float(row, "_step4e_desired_vy_m_s", 0.0)
            if phase != "move_xy" and (xy_enabled or abs(desired_vx) > 1e-9 or abs(desired_vy) > 1e-9):
                xy_motion_before_move_rows += 1
            if phase != "move_xy" or not xy_enabled:
                continue
            normal = finite_csv_float(row, "_step4e_normal_load_n")
            force_norm = finite_csv_float(row, "force_norm_n")
            torque_norm = finite_csv_float(row, "torque_norm_nm")
            if not all(math.isfinite(value) for value in (normal, force_norm, torque_norm)):
                continue
            move_rows += 1
            move_normal_loads.append(normal)
            move_force_norms.append(force_norm)
            move_torque_norms.append(torque_norm)
            dropout_lt2 += int(normal < STEP5B_15N_TRIAL_LOW_LOAD_N)
            saturation_rows += int(finite_csv_float(row, "_step5b_ramp_normal_velocity_saturated", 0.0) > 0.5)

    target_monotonic = all(
        nxt + 1e-6 >= prev
        for prev, nxt in zip(target_values, target_values[1:])
    )
    payload: dict[str, Any] = {
        "profile": STEP5B_RAMP_5_TO_15_TRIAL_PROFILE,
        "target_force_n": metadata["args"].get("target_force_n"),
        "ramp_start_force_n": STEP5B_RAMP_START_TARGET_N,
        "ramp_final_force_n": STEP5B_RAMP_FINAL_TARGET_N,
        "ramp_duration_s": STEP5B_RAMP_DURATION_S,
        "stop_reason": first_trial_stop or stop_reason,
        "active_rows": active_rows,
        "phase_counts": phase_counts,
        "metrics": {
            "move_rows": move_rows,
            "target_monotonic": target_monotonic,
            "target_reached_final": any(value >= STEP5B_RAMP_FINAL_TARGET_N - 1e-6 for value in target_values),
            "target_min_n": min(target_values) if target_values else None,
            "target_max_n": max(target_values) if target_values else None,
            "xy_motion_before_move_rows": xy_motion_before_move_rows,
            "move_normal_load_n": {
                "p50": percentile(move_normal_loads, 0.50),
                "p95": percentile(move_normal_loads, 0.95),
                "max": max(move_normal_loads) if move_normal_loads else None,
            },
            "move_force_norm_n": {
                "max": max(move_force_norms) if move_force_norms else None,
            },
            "move_torque_norm_nm": {
                "max": max(move_torque_norms) if move_torque_norms else None,
            },
            "move_dropout_lt2_duty": dropout_lt2 / move_rows if move_rows else None,
            "move_normal_velocity_saturation_duty": saturation_rows / move_rows if move_rows else None,
        },
        "paths": {
            "bridge_csv": str(bridge_csv_path),
            "metadata": str(output_dir / "metadata.json"),
        },
    }
    payload["verdict"] = step5b_ramp_trial_verdict(payload)
    summary_path = output_dir / "step5b_ramp_5_to_15_sentinel_summary.json"
    write_json(summary_path, payload)
    md_path = output_dir / "step5b_ramp_5_to_15_sentinel_summary.md"
    md_path.write_text(
        "\n".join(
            [
                "# Step5b ramp 5N to 15N sentinel summary",
                "",
                f"- profile: `{payload['profile']}`",
                f"- target_force_n: `{payload['target_force_n']}`",
                f"- stop_reason: `{payload['stop_reason']}`",
                f"- verdict: `{payload['verdict']}`",
                f"- active_rows: `{payload['active_rows']}`",
                f"- move_rows: `{payload['metrics']['move_rows']}`",
                f"- target_monotonic: `{payload['metrics']['target_monotonic']}`",
                f"- target_reached_final: `{payload['metrics']['target_reached_final']}`",
                f"- move_normal_load_p50_n: `{payload['metrics']['move_normal_load_n']['p50']}`",
                f"- move_normal_load_p95_n: `{payload['metrics']['move_normal_load_n']['p95']}`",
                f"- move_normal_load_max_n: `{payload['metrics']['move_normal_load_n']['max']}`",
                f"- move_dropout_lt2_duty: `{payload['metrics']['move_dropout_lt2_duty']}`",
                f"- move_normal_velocity_saturation_duty: `{payload['metrics']['move_normal_velocity_saturation_duty']}`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    payload["paths"]["trial_summary_json"] = str(summary_path)
    payload["paths"]["trial_summary_md"] = str(md_path)
    return payload


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


def env_choice_alias(primary: str, legacy: str, default: str, choices: tuple[str, ...]) -> str:
    value = os.getenv(primary, os.getenv(legacy, default))
    if value not in choices:
        raise SystemExit(f"{primary}/{legacy} must be one of {choices}; got {value!r}")
    return value


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a float; got {value!r}") from exc


def env_float_alias(primary: str, legacy: str, default: float) -> float:
    value = os.getenv(primary, os.getenv(legacy))
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise SystemExit(f"{primary}/{legacy} must be a float; got {value!r}") from exc


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


def step5b_15n_trial_enabled(args: argparse.Namespace) -> bool:
    return (
        getattr(args, "bridge_profile", "") == "step5b_v1"
        and getattr(args, "step5b_trial_profile", "none") == STEP5B_15N_TRIAL_PROFILE
    )


def step5b_ramp_trial_enabled(args: argparse.Namespace) -> bool:
    return (
        getattr(args, "bridge_profile", "") == "step5b_v1"
        and getattr(args, "step5b_trial_profile", "none") == STEP5B_RAMP_5_TO_15_TRIAL_PROFILE
    )


def step5b_trial_enabled(args: argparse.Namespace) -> bool:
    return step5b_15n_trial_enabled(args) or step5b_ramp_trial_enabled(args)


def validate_common_target_force(args: argparse.Namespace) -> None:
    target = float(args.target_force_n)
    if not math.isfinite(target) or target <= 0.0:
        raise SystemExit("--target-force-n must be finite and positive")


def validate_step5b_15n_trial_args(args: argparse.Namespace) -> None:
    if getattr(args, "step5b_trial_profile", "none") == "none":
        return
    if getattr(args, "step5b_trial_profile", "none") not in STEP5B_TRIAL_PROFILES:
        raise SystemExit(f"Unknown --step5b-trial-profile {args.step5b_trial_profile!r}")
    if args.bridge_profile != "step5b_v1" or args.bridge_mode != "line":
        raise SystemExit(f"--step5b-trial-profile {args.step5b_trial_profile} requires step5b_v1 line mode")
    if abs(float(args.target_force_n) - STEP5B_15N_TRIAL_TARGET_N) > STEP5B_15N_TRIAL_TARGET_TOL_N:
        raise SystemExit(f"{args.step5b_trial_profile} requires --target-force-n 15.0")
    normal_velocity_limit_max_m_s = (
        STEP5B_RAMP_NORMAL_VELOCITY_LIMIT_MAX_M_S
        if args.step5b_trial_profile == STEP5B_RAMP_5_TO_15_TRIAL_PROFILE
        else STEP5B_15N_TRIAL_NORMAL_VELOCITY_LIMIT_MAX_M_S
    )
    if args.bridge_normal_velocity_limit_m_s > normal_velocity_limit_max_m_s:
        raise SystemExit(
            f"{args.step5b_trial_profile} requires normal velocity limit <= "
            f"{normal_velocity_limit_max_m_s:.4f} m/s"
        )
    if args.bridge_total_linear_limit_m_s > 0.0041:
        raise SystemExit(f"{args.step5b_trial_profile} requires total linear limit <= 0.0041 m/s")
    if args.bridge_integral_limit_n_s > 1.01:
        raise SystemExit(f"{args.step5b_trial_profile} requires integral limit <= 1.0 N*s")


def set_step5b_ramp_phase(state: BridgeState, phase: str) -> None:
    if state.step5b_ramp_phase == phase:
        return
    state.step5b_ramp_phase = phase
    state.step5b_ramp_phase_s = 0.0
    state.integral_error_n_s = 0.0
    state.normal_velocity_m_s = 0.0
    if phase == "pre_unload_to_5":
        state.step5b_ramp_preload_ready_s = 0.0
        state.step5b_ramp_after_initial_acquire_s = 0.0
        state.step5b_ramp_terminal_discard_s = 0.0
        state.step5b_ramp_scored_s = 0.0
        state.step5b_ramp_move_s = 0.0
        state.step5b_ramp_active_target_force_n = STEP5B_RAMP_START_TARGET_N
        state.step5b_ramp_alpha = 0.0
        state.step5b_ramp_xy_enabled = False
    elif phase == "ramp_5_to_15":
        state.step5b_ramp_alpha = 0.0
        state.step5b_ramp_active_target_force_n = STEP5B_RAMP_START_TARGET_N
        state.step5b_ramp_xy_enabled = False
    elif phase == "hold_15":
        state.step5b_ramp_terminal_discard_s = 0.0
        state.step5b_ramp_scored_s = 0.0
        state.step5b_ramp_active_target_force_n = STEP5B_RAMP_FINAL_TARGET_N
        state.step5b_ramp_alpha = 1.0
        state.step5b_ramp_xy_enabled = False
    elif phase == "move_xy":
        state.step5b_ramp_move_s = 0.0
        state.step5b_ramp_active_target_force_n = STEP5B_RAMP_FINAL_TARGET_N
        state.step5b_ramp_alpha = 1.0
        state.step5b_ramp_xy_enabled = True
    elif phase == "complete":
        state.step5b_ramp_active_target_force_n = STEP5B_RAMP_FINAL_TARGET_N
        state.step5b_ramp_alpha = 1.0
        state.step5b_ramp_xy_enabled = False


def step5b_ramp_trial_phase_update(
    *,
    normal_load_n: float,
    force_norm_n: float,
    dt_s: float,
    state: BridgeState,
) -> None:
    safe_dt_s = max(0.0, float(dt_s))
    if state.step5b_ramp_phase == "inactive":
        set_step5b_ramp_phase(state, "pre_unload_to_5")
    else:
        state.step5b_ramp_phase_s += safe_dt_s

    phase = state.step5b_ramp_phase
    if phase == "pre_unload_to_5":
        state.step5b_ramp_active_target_force_n = STEP5B_RAMP_START_TARGET_N
        state.step5b_ramp_alpha = 0.0
        state.step5b_ramp_xy_enabled = False
        preload_ready = (
            STEP5B_RAMP_PRELOAD_MIN_N <= float(normal_load_n) <= STEP5B_RAMP_PRELOAD_MAX_N
            and float(force_norm_n) <= STEP5B_RAMP_PRELOAD_FORCE_NORM_MAX_N
        )
        state.step5b_ramp_preload_ready_s = (
            state.step5b_ramp_preload_ready_s + safe_dt_s if preload_ready else 0.0
        )
        if state.step5b_ramp_preload_ready_s >= STEP5B_RAMP_PRELOAD_HOLD_S:
            state.step5b_ramp_after_initial_acquire_s = 0.0
            set_step5b_ramp_phase(state, "ramp_5_to_15")
    elif phase == "ramp_5_to_15":
        ramp_s = min(STEP5B_RAMP_DURATION_S, max(0.0, state.step5b_ramp_phase_s))
        alpha = 1.0 if STEP5B_RAMP_DURATION_S <= 0.0 else clamp(ramp_s / STEP5B_RAMP_DURATION_S, 0.0, 1.0)
        state.step5b_ramp_alpha = alpha
        state.step5b_ramp_active_target_force_n = (
            STEP5B_RAMP_START_TARGET_N
            + (STEP5B_RAMP_FINAL_TARGET_N - STEP5B_RAMP_START_TARGET_N) * alpha
        )
        state.step5b_ramp_xy_enabled = False
        state.step5b_ramp_after_initial_acquire_s += safe_dt_s
        if alpha >= 1.0:
            set_step5b_ramp_phase(state, "hold_15")
    elif phase == "hold_15":
        state.step5b_ramp_active_target_force_n = STEP5B_RAMP_FINAL_TARGET_N
        state.step5b_ramp_alpha = 1.0
        state.step5b_ramp_xy_enabled = False
        if float(normal_load_n) >= STEP5B_RAMP_FINAL_ACQUIRE_N:
            state.step5b_ramp_terminal_discard_s += safe_dt_s
            if state.step5b_ramp_terminal_discard_s > STEP5B_RAMP_FINAL_DISCARD_S:
                state.step5b_ramp_scored_s += safe_dt_s
        else:
            state.step5b_ramp_terminal_discard_s = 0.0
            state.step5b_ramp_scored_s = 0.0
        if state.step5b_ramp_scored_s >= STEP5B_RAMP_FINAL_SCORE_S:
            set_step5b_ramp_phase(state, "move_xy")
    elif phase == "move_xy":
        state.step5b_ramp_active_target_force_n = STEP5B_RAMP_FINAL_TARGET_N
        state.step5b_ramp_alpha = 1.0
        state.step5b_ramp_xy_enabled = True
        state.step5b_ramp_move_s += safe_dt_s
        if state.step5b_ramp_move_s >= STEP5B_RAMP_MOVE_SCORE_S:
            set_step5b_ramp_phase(state, "complete")
    elif phase == "complete":
        state.step5b_ramp_active_target_force_n = STEP5B_RAMP_FINAL_TARGET_N
        state.step5b_ramp_alpha = 1.0
        state.step5b_ramp_xy_enabled = False


def step5b_ramp_trial_guard_reason(
    *,
    normal_load_n: float,
    force_norm_n: float,
    torque_norm_nm: float,
    sensor_ok: float,
    normal_velocity_m_s: float,
    normal_velocity_limit_m_s: float,
    dt_s: float,
    state: BridgeState,
) -> str | None:
    safe_dt_s = max(0.0, float(dt_s))
    phase = state.step5b_ramp_phase
    target = float(state.step5b_ramp_active_target_force_n)

    if phase == "complete":
        return "step5b_ramp_5_to_15:complete"
    if sensor_ok <= 0.5 and phase not in {"inactive", "pre_unload_to_5"}:
        return "step5b_ramp_5_to_15:sensor_not_ok"

    if phase == "pre_unload_to_5":
        if normal_load_n > STEP5B_RAMP_PRELOAD_NORMAL_STOP_N:
            return "step5b_ramp_5_to_15:preload_normal_stop"
        if force_norm_n > STEP5B_RAMP_PRELOAD_FORCE_STOP_N:
            return "step5b_ramp_5_to_15:preload_force_norm_stop"
        if state.step5b_ramp_phase_s >= STEP5B_RAMP_PRELOAD_TIMEOUT_S:
            return "step5b_ramp_5_to_15:preload_acquisition_timeout"
        return None

    normal_stop_n = min(STEP5B_RAMP_PRELOAD_NORMAL_STOP_N, target + STEP5B_RAMP_NORMAL_STOP_MARGIN_N)
    normal_dwell_n = min(STEP5B_RAMP_PRELOAD_NORMAL_STOP_N - 2.0, target + STEP5B_RAMP_NORMAL_DWELL_MARGIN_N)
    force_stop_n = min(STEP5B_RAMP_PRELOAD_FORCE_STOP_N, target + STEP5B_RAMP_FORCE_STOP_MARGIN_N)
    force_dwell_n = min(STEP5B_RAMP_PRELOAD_FORCE_STOP_N - 2.0, target + STEP5B_RAMP_FORCE_DWELL_MARGIN_N)
    if normal_load_n > normal_stop_n:
        return "step5b_ramp_5_to_15:normal_load_stop"
    state.step5b_ramp_high_normal_s = (
        state.step5b_ramp_high_normal_s + safe_dt_s
        if normal_load_n > normal_dwell_n
        else 0.0
    )
    if state.step5b_ramp_high_normal_s >= STEP5B_15N_TRIAL_NORMAL_DWELL_S:
        return "step5b_ramp_5_to_15:normal_load_dwell"

    if force_norm_n > force_stop_n:
        return "step5b_ramp_5_to_15:force_norm_stop"
    state.step5b_ramp_high_force_s = (
        state.step5b_ramp_high_force_s + safe_dt_s
        if force_norm_n > force_dwell_n
        else 0.0
    )
    if state.step5b_ramp_high_force_s >= STEP5B_15N_TRIAL_FORCE_DWELL_S:
        return "step5b_ramp_5_to_15:force_norm_dwell"

    if torque_norm_nm > STEP5B_15N_TRIAL_TORQUE_STOP_NM:
        return "step5b_ramp_5_to_15:torque_norm_stop"
    state.step5b_ramp_high_torque_s = (
        state.step5b_ramp_high_torque_s + safe_dt_s
        if torque_norm_nm > STEP5B_15N_TRIAL_TORQUE_DWELL_NM
        else 0.0
    )
    if state.step5b_ramp_high_torque_s >= STEP5B_15N_TRIAL_TORQUE_DWELL_S:
        return "step5b_ramp_5_to_15:torque_norm_dwell"

    state.step5b_ramp_low_load_s = (
        state.step5b_ramp_low_load_s + safe_dt_s
        if normal_load_n < STEP5B_15N_TRIAL_LOW_LOAD_N
        else 0.0
    )
    if state.step5b_ramp_low_load_s >= STEP5B_15N_TRIAL_LOW_LOAD_S:
        return "step5b_ramp_5_to_15:low_load_dropout"
    relative_low_load_n = max(STEP5B_15N_TRIAL_LOW_LOAD_N, 0.5 * target)
    state.step5b_ramp_relative_low_load_s = (
        state.step5b_ramp_relative_low_load_s + safe_dt_s
        if normal_load_n < relative_low_load_n
        else 0.0
    )
    if state.step5b_ramp_relative_low_load_s >= STEP5B_15N_TRIAL_RELATIVE_LOW_LOAD_S:
        return "step5b_ramp_5_to_15:relative_low_load_dropout"

    if normal_velocity_limit_m_s > 0.0 and abs(normal_velocity_m_s) >= 0.98 * normal_velocity_limit_m_s:
        state.step5b_ramp_saturation_s += safe_dt_s
    else:
        state.step5b_ramp_saturation_s = 0.0
    if state.step5b_ramp_saturation_s >= STEP5B_15N_TRIAL_SATURATION_S:
        return "step5b_ramp_5_to_15:normal_velocity_saturation"
    return None


def step5b_15n_trial_guard_reason(
    *,
    normal_load_n: float,
    force_norm_n: float,
    torque_norm_nm: float,
    sensor_ok: float,
    normal_velocity_m_s: float,
    normal_velocity_limit_m_s: float,
    dt_s: float,
    state: BridgeState,
) -> str | None:
    safe_dt_s = max(0.0, float(dt_s))
    if sensor_ok <= 0.5 and state.step5b_15n_acquired:
        return "step5b_15n_trial:sensor_not_ok"

    if normal_load_n >= STEP5B_15N_TRIAL_ACQUIRE_N:
        state.step5b_15n_acquired = True
    if state.step5b_15n_acquired:
        state.step5b_15n_after_acquire_s += safe_dt_s
        if state.step5b_15n_after_acquire_s > STEP5B_15N_TRIAL_DISCARD_S:
            state.step5b_15n_scored_s += safe_dt_s

    if normal_load_n > STEP5B_15N_TRIAL_NORMAL_STOP_N:
        return "step5b_15n_trial:normal_load_stop"
    state.step5b_15n_high_normal_s = (
        state.step5b_15n_high_normal_s + safe_dt_s
        if normal_load_n > STEP5B_15N_TRIAL_NORMAL_DWELL_N
        else 0.0
    )
    if state.step5b_15n_high_normal_s >= STEP5B_15N_TRIAL_NORMAL_DWELL_S:
        return "step5b_15n_trial:normal_load_dwell"

    if force_norm_n > STEP5B_15N_TRIAL_FORCE_STOP_N:
        return "step5b_15n_trial:force_norm_stop"
    state.step5b_15n_high_force_s = (
        state.step5b_15n_high_force_s + safe_dt_s
        if force_norm_n > STEP5B_15N_TRIAL_FORCE_DWELL_N
        else 0.0
    )
    if state.step5b_15n_high_force_s >= STEP5B_15N_TRIAL_FORCE_DWELL_S:
        return "step5b_15n_trial:force_norm_dwell"

    if torque_norm_nm > STEP5B_15N_TRIAL_TORQUE_STOP_NM:
        return "step5b_15n_trial:torque_norm_stop"
    state.step5b_15n_high_torque_s = (
        state.step5b_15n_high_torque_s + safe_dt_s
        if torque_norm_nm > STEP5B_15N_TRIAL_TORQUE_DWELL_NM
        else 0.0
    )
    if state.step5b_15n_high_torque_s >= STEP5B_15N_TRIAL_TORQUE_DWELL_S:
        return "step5b_15n_trial:torque_norm_dwell"

    if state.step5b_15n_acquired:
        state.step5b_15n_low_load_s = (
            state.step5b_15n_low_load_s + safe_dt_s
            if normal_load_n < STEP5B_15N_TRIAL_LOW_LOAD_N
            else 0.0
        )
        if state.step5b_15n_low_load_s >= STEP5B_15N_TRIAL_LOW_LOAD_S:
            return "step5b_15n_trial:low_load_dropout"
        state.step5b_15n_relative_low_load_s = (
            state.step5b_15n_relative_low_load_s + safe_dt_s
            if normal_load_n < STEP5B_15N_TRIAL_RELATIVE_LOW_LOAD_N
            else 0.0
        )
        if state.step5b_15n_relative_low_load_s >= STEP5B_15N_TRIAL_RELATIVE_LOW_LOAD_S:
            return "step5b_15n_trial:relative_low_load_dropout"

    if normal_velocity_limit_m_s > 0.0 and abs(normal_velocity_m_s) >= 0.98 * normal_velocity_limit_m_s:
        state.step5b_15n_saturation_s += safe_dt_s
    else:
        state.step5b_15n_saturation_s = 0.0
    if state.step5b_15n_saturation_s >= STEP5B_15N_TRIAL_SATURATION_S:
        return "step5b_15n_trial:normal_velocity_saturation"

    if not state.step5b_15n_acquired and state.line_stage_s >= STEP5B_15N_TRIAL_ACQUIRE_TIMEOUT_S:
        return "step5b_15n_trial:acquisition_timeout"
    if state.step5b_15n_scored_s >= STEP5B_15N_TRIAL_SCORE_S:
        return "step5b_15n_trial:complete"
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
    parser.add_argument("--step5b-trial-profile", choices=STEP5B_TRIAL_PROFILES, default="none")
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
        default=env_choice_alias("BRIDGE_NORMAL_FOLLOW_MODE", "STEP4E_NORMAL_FOLLOW_MODE", "locked", ("locked", "filtered_live")),
    )
    parser.add_argument(
        "--step4e-normal-filter-tau-s",
        type=float,
        default=env_float_alias("BRIDGE_NORMAL_FILTER_TAU_S", "STEP4E_NORMAL_FILTER_TAU_S", 0.35),
    )
    parser.add_argument(
        "--step4e-normal-filter-alpha",
        type=float,
        default=env_float_alias("BRIDGE_NORMAL_FILTER_ALPHA", "STEP4E_NORMAL_FILTER_ALPHA", 0.35),
    )
    parser.add_argument(
        "--step4e-normal-max-rate-rad-s",
        type=float,
        default=env_float_alias("BRIDGE_NORMAL_MAX_RATE_RAD_S", "STEP4E_NORMAL_MAX_RATE_RAD_S", 0.010),
    )
    parser.add_argument(
        "--step4e-normal-min-force-n",
        type=float,
        default=env_float_alias("BRIDGE_NORMAL_MIN_FORCE_N", "STEP4E_NORMAL_MIN_FORCE_N", 2.0),
    )
    parser.add_argument(
        "--step4e-normal-max-angle-from-latch-deg",
        type=float,
        default=env_float_alias("BRIDGE_NORMAL_MAX_ANGLE_FROM_LATCH_DEG", "STEP4E_NORMAL_MAX_ANGLE_FROM_LATCH_DEG", 20.0),
    )
    parser.add_argument(
        "--step4e-normal-friction-projection",
        choices=("on", "off"),
        default=env_choice_alias("BRIDGE_NORMAL_FRICTION_PROJECTION", "STEP4E_NORMAL_FRICTION_PROJECTION", "on", ("on", "off")),
    )
    parser.add_argument("--bridge-mode", dest="step4e_mode", choices=("off", "preview", "hold", "line", "axis_iso"), default=argparse.SUPPRESS)
    parser.add_argument("--bridge-profile", dest="step4e_version", default=argparse.SUPPRESS)
    parser.add_argument("--bridge-path-shape", dest="step4e_path_shape", choices=("line", "cycloid", "eight"), default=argparse.SUPPRESS)
    parser.add_argument("--bridge-line-speed-m-s", dest="step4e_line_speed_m_s", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-line-settle-s", dest="step4e_line_settle_s", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-integrate-stage25-only", dest="step4e_integrate_stage25_only", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--bridge-path-p-gain", dest="step4e_path_p_gain", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-motion-limit-m-s", dest="step4e_motion_limit_m_s", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-total-linear-limit-m-s", dest="step4e_total_linear_limit_m_s", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-normal-velocity-limit-m-s", dest="step4e_normal_velocity_limit_m_s", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-force-p-gain", dest="step4e_force_p_gain", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-force-i-gain", dest="step4e_force_i_gain", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-force-damping", dest="step4e_force_damping", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-normal-command-sign", dest="step4e_normal_command_sign", type=float, choices=(-1.0, 1.0), default=argparse.SUPPRESS)
    parser.add_argument("--bridge-integral-limit-n-s", dest="step4e_integral_limit_n_s", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-min-force-for-control-n", dest="step4e_min_force_for_control_n", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-acquire-grace-s", dest="step4e_acquire_grace_s", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-reacquire-velocity-m-s", dest="step4e_reacquire_velocity_m_s", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-orientation-gain", dest="step4e_orientation_gain", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-orientation-wx-sign", dest="step4e_orientation_wx_sign", type=float, choices=(-1.0, 1.0), default=argparse.SUPPRESS)
    parser.add_argument("--bridge-orientation-wy-sign", dest="step4e_orientation_wy_sign", type=float, choices=(-1.0, 1.0), default=argparse.SUPPRESS)
    parser.add_argument("--bridge-angular-limit-rad-s", dest="step4e_angular_limit_rad_s", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-axis-iso-tilt-deg", dest="step4e_axis_iso_tilt_deg", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-contact-offset-min-fz-n", dest="step4e_contact_offset_min_fz_n", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-normal-follow-mode", dest="step4e_normal_follow_mode", choices=("locked", "filtered_live"), default=argparse.SUPPRESS)
    parser.add_argument("--bridge-normal-filter-tau-s", dest="step4e_normal_filter_tau_s", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-normal-filter-alpha", dest="step4e_normal_filter_alpha", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-normal-max-rate-rad-s", dest="step4e_normal_max_rate_rad_s", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-normal-min-force-n", dest="step4e_normal_min_force_n", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-normal-max-angle-from-latch-deg", dest="step4e_normal_max_angle_from_latch_deg", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--bridge-normal-friction-projection", dest="step4e_normal_friction_projection", choices=("on", "off"), default=argparse.SUPPRESS)
    parser.add_argument("--step5c-qdot-limit-rad-s", type=float, default=0.15)
    parser.add_argument("--step5c-joint-damping", type=float, default=1e-4)
    parser.add_argument(
        "--step5c-joint-model",
        type=Path,
        default=Path(
            "/home/andy/ur10e_ros2_ws/experiments/archive/legacy/tase-mujoco-reproduction-2026-05-23/assets/mjcf/ur10e_nominal.xml"
        ),
    )
    parser.add_argument("--step5c-joint-site", default="tcp_site_unverified_85mm")
    parser.add_argument("--step5d-qdot-limit-rad-s", type=float, default=None)
    parser.add_argument("--step5d-alpha-s-inv", type=float, default=1.0)
    parser.add_argument("--step5d-epsilon", type=float, default=0.022)
    parser.add_argument("--step5d-sigr-exponent-r", type=float, default=0.2)
    parser.add_argument(
        "--disable-dashboard-program-watch",
        action="store_true",
        help="disable Step5d live-prep Dashboard watchdog that exits after TP stop/play timeout",
    )
    parser.add_argument("--dashboard-program-watch-timeout-s", type=float, default=45.0)
    args = parser.parse_args(argv)
    for key, value in list(vars(args).items()):
        if key.startswith("step4e_"):
            setattr(args, f"bridge_{key.removeprefix('step4e_')}", value)
    args.bridge_profile = args.step4e_version
    if args.step5d_qdot_limit_rad_s is None:
        args.step5d_qdot_limit_rad_s = (
            STEP5D_V12_QDOT_LIMIT_RAD_S
            if args.bridge_profile
            in {STEP5D_LIVEPREP_V12_STAGE_ID, STEP5D_LIVEPREP_V13_STAGE_ID, STEP5D_LIVEPREP_STAGE_ID, STEP5D_LIVEPREP_V15_STAGE_ID, STEP5D_LIVEPREP_V15A_STAGE_ID}
            else 0.30
        )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.duration_s <= 0 or args.baseline_s < 0 or args.rtde_hz <= 0:
        raise SystemExit("duration, baseline, and RTDE rate must be positive")
    validate_common_target_force(args)
    if not args.no_start_command and not args.allow_kunwei_stream_command:
        raise SystemExit("Refusing to send Kunwei stream command without --allow-kunwei-stream-command")
    known_bridge_profiles = {
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
        *STEP5D_LIVEPREP_STAGE_IDS,
        STEP5D_REPRODUCTION_STAGE_ID,
        "step6b_v1",
        "step6b_v2",
    }
    if args.bridge_profile not in known_bridge_profiles:
        raise SystemExit(
            f"Unknown --step4e-version {args.bridge_profile!r}; bridge profiles only cover "
            f"{sorted(v for v in known_bridge_profiles if v)}. Add the new version to the "
            "profile definitions before running, otherwise cmd_valid is never asserted."
        )
    if args.bridge_profile == STEP5C_DRYRUN_STAGE_ID:
        raise SystemExit(
            "Blocked Step5c dry-run: 2026-06-13 live run showed wrong XY/Z motion from "
            "the DLS/MuJoCo Jacobian mapping. Do not run until offline mapping validation passes."
        )
    if args.bridge_profile == STEP5C_CONTACT_STAGE_ID:
        raise SystemExit(
            "Blocked Step5c contact: step5c_joint_rnn_cycloid_v1 was a misnamed DLS route, "
            "not strict TASE RNN. No Step5c joint-space bridge profile is runnable."
        )
    if args.bridge_profile == STEP5D_REPRODUCTION_STAGE_ID:
        raise SystemExit(
            "Blocked Step5d reproduction: offline paper outer-loop, strict RNN, calibrated "
            "Jacobian audit, numeric sanity, TP package read-back, and a separately accepted "
            "live plan must all pass before bridge start."
        )
    if args.bridge_normal_filter_tau_s < 0.0:
        raise SystemExit("--step4e-normal-filter-tau-s must be non-negative")
    if not 0.0 <= args.bridge_normal_filter_alpha <= 1.0:
        raise SystemExit("--step4e-normal-filter-alpha must be in [0, 1]")
    if args.bridge_normal_max_rate_rad_s < 0.0:
        raise SystemExit("--step4e-normal-max-rate-rad-s must be non-negative")
    if args.bridge_normal_min_force_n < 0.0:
        raise SystemExit("--step4e-normal-min-force-n must be non-negative")
    if args.bridge_normal_max_angle_from_latch_deg <= 0.0:
        raise SystemExit("--step4e-normal-max-angle-from-latch-deg must be positive")
    if args.step5d_qdot_limit_rad_s <= 0.0:
        raise SystemExit("--step5d-qdot-limit-rad-s must be positive")
    if args.step5d_alpha_s_inv <= 0.0:
        raise SystemExit("--step5d-alpha-s-inv must be positive")
    if args.step5d_epsilon <= 0.0:
        raise SystemExit("--step5d-epsilon must be positive")
    if not 0.0 < args.step5d_sigr_exponent_r <= 1.0:
        raise SystemExit("--step5d-sigr-exponent-r must be in (0, 1]")
    if args.dashboard_program_watch_timeout_s <= 0.0:
        raise SystemExit("--dashboard-program-watch-timeout-s must be positive")
    validate_step5b_15n_trial_args(args)

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
        "step5b_trial_contract": {
            "profile": args.step5b_trial_profile,
            "target_force_n": args.target_force_n if step5b_trial_enabled(args) else None,
            "score_after_acquire_n": STEP5B_15N_TRIAL_ACQUIRE_N if step5b_15n_trial_enabled(args) else None,
            "discard_s": STEP5B_15N_TRIAL_DISCARD_S if step5b_15n_trial_enabled(args) else None,
            "score_s": STEP5B_15N_TRIAL_SCORE_S if step5b_15n_trial_enabled(args) else None,
            "normal_load_stop_n": STEP5B_15N_TRIAL_NORMAL_STOP_N if step5b_15n_trial_enabled(args) else None,
            "force_norm_stop_n": STEP5B_15N_TRIAL_FORCE_STOP_N if step5b_15n_trial_enabled(args) else None,
            "torque_norm_stop_nm": STEP5B_15N_TRIAL_TORQUE_STOP_NM if step5b_15n_trial_enabled(args) else None,
            "ramp_start_force_n": STEP5B_RAMP_START_TARGET_N if step5b_ramp_trial_enabled(args) else None,
            "ramp_final_force_n": STEP5B_RAMP_FINAL_TARGET_N if step5b_ramp_trial_enabled(args) else None,
            "ramp_duration_s": STEP5B_RAMP_DURATION_S if step5b_ramp_trial_enabled(args) else None,
            "ramp_final_score_s": STEP5B_RAMP_FINAL_SCORE_S if step5b_ramp_trial_enabled(args) else None,
            "ramp_move_score_s": STEP5B_RAMP_MOVE_SCORE_S if step5b_ramp_trial_enabled(args) else None,
        },
        "dashboard_program_watch": {
            "enabled": args.bridge_profile in STEP5D_LIVEPREP_STAGE_IDS
            and not args.skip_dashboard_preflight
            and not args.disable_dashboard_program_watch,
            "timeout_s": args.dashboard_program_watch_timeout_s,
            "scope": "Step5d live-prep bridge exits after TP program stop or Play timeout",
        },
        "register_map": dict(zip(INPUT_FIELDS, INPUT_NAMES)),
        "bridge_register_contract": {
            "physical_fields": BRIDGE_INPUT_FIELDS,
            "legacy_step4e_carrier_names": BRIDGE_INPUT_NAMES,
            "note": "Step4e carrier names are retained for RTDE recipe and CSV compatibility; bridge profiles own the active semantics.",
            "step5_joint_semantics": STEP5C_INPUT_REGISTER_SEMANTICS,
        },
        "step5c_joint_register_contract": step5c_register_metadata(),
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
            "step5c_speedj_dryrun_v1": "No-contact Step5c joint-space dry-run: bridge reads actual_q and writes qd0..qd5 in registers 37..42 through the explicit Step5c qdot helper; TP executes speedj only in the archived Stage25 dry-run fixture. Live profile remains blocked.",
            "step5c_joint_rnn_cycloid_v1": "Blocked/quarantined Step5c contact route: previous implementation was DLS, not strict TASE RNN.",
            "step5d_strict_rnn_liveprep_v1": "Live-prep strict RNN qdot route: TP reuses Step5b contact scaffold, then Stage 25.0 consumes registers 37..42 as qd0..qd5 rad/s and executes speedj. Not a completed reproduction claim.",
            "step5d_strict_rnn_liveprep_v2": "Evidence live-prep strict RNN qdot route: warmed calibrated Pinocchio/RNN before Stage 25.0 and skipped lift/25.2 when first-contact orientation error was small; archived after v2 showed over-contact from high 25.3 preload plus unbounded Step5d task velocity.",
            "step5d_strict_rnn_liveprep_v3": "Previous live-prep strict RNN qdot route: raises raw/force-norm hard guards to 100 N, uses a strict 5N-centered 25.3 force-settle entry gate before Stage 25.0, limits live Step5d xdot_c before the RNN, and executes speedj qdot registers 37..42.",
            "step5d_strict_rnn_liveprep_v4": "Archived live-prep strict RNN route: entered Stage 25.0 through the tolerant contact window but exposed the Step5d force/frame semantic bug where the outer loop inverted the orientation target.",
            "step5d_strict_rnn_liveprep_v5": "Retained semantic-fix live-prep route: Step5d outer loop consumes the Step5b/Step6b reaction-normal contract and hard-fails 25.0 if contact-search and outer-loop orientation semantics disagree. Live v5 entered 25.0 but the 2-15N engage window blocked qdot at about 17-21N.",
            "step5d_strict_rnn_liveprep_v6": "Retained live-prep strict RNN route: widened the contact/recovery window to 2-40N normal_load with force_norm <=45N, proving the v5 semantic gate could enter 25.0 but exposing the 24.3 re-contact overpressure failure.",
            "step5d_strict_rnn_liveprep_v7": "Retained live-prep strict RNN route: inherits the v5/v6 semantic gate and speedj qdot path, restores the 2-15N contact window, and changes 24.3/24.4 re-contact to a slow-only search before Stage 25.0.",
            "step5d_strict_rnn_liveprep_v8": "Retained failure evidence: Stage 25.3 consumed registers 37..39 as locked-normal Cartesian force-PID settle velocity, but low-load dropout below 0.5N cleared cmd_valid and TP stopped with reason 17 before Stage 25.0.",
            "step5d_strict_rnn_liveprep_v9": "Retained failure evidence: Stage 25.3 direct force-PID settle fixed low-load dropout but hunted under point contact and timed out before Stage 25.0.",
            "step5d_strict_rnn_liveprep_v10": "Retained failure evidence: Stage 25.3 scalar admittance settle softened v9 direct PID but still saturated and flipped sign under point contact, never satisfying the 3-8N plus settle-speed window before Stage 25.0.",
            "step5d_strict_rnn_liveprep_v11": "Retained incomplete evidence: Stage 25.3 deadband acquire released into Stage 25.0, but Stage 25.0 lost contact and the strict RNN speedj path accelerated until operator E-stop.",
            "step5d_strict_rnn_liveprep_v12": "Retained read-back guarded live-prep strict RNN route: keeps v11 Stage 25.3 deadband acquire, then Stage 25.0 adds low-load/contact-retention, TCP speed watchdog, 0.05 rad/s qdot cap, and qdot slew limiting before TP speedj.",
            "step5d_strict_rnn_liveprep_v13": "Retained read-back evidence with known P1 gap: actual TCP speed dwell first sample could pass solver before v14.",
            "step5d_strict_rnn_liveprep_v14": "Retained contact-safety live-prep strict RNN evidence: actual TCP speed dwell first sample holds zero qdot and freezes path time; predicted TCP speed stops immediately; 0.004s actual dwell or other danger sets stop_request with zero qdot. Not current after the 2026-06-15 predicted TCP speed watchdog stop.",
            "step5d_strict_rnn_liveprep_v15": "Retained controller-readback evidence with audit gaps: cage hook was not online and bounded hold metrics were incomplete. Superseded by v15a planning before any bridge run.",
            "step5d_strict_rnn_liveprep_v15a": "Current v15a live-prep package route: computes online broad AABB TCP cage distance/braking margin from Step5b/Step6b success traces, routes recoverable predicted-speed/contact uncertainty to bounded zero-qdot hold/reacquire, and logs cage plus hold-burden diagnostics. No bridge run or live authorization yet.",
            "step6b_contact_eight_baseline_v1": "Same TP contact-search/latch/25.2/25.3 scaffold as Step5b/v31, but stage 25.0 uses the active Step6 five-point safe-frame 8-shaped reference for 30 s and v31 filtered-live normal policy.",
            "step6b_contact_eight_baseline_v2": "Same TP contact-search/latch/25.2/25.3 scaffold and Step6 reference as v1, but intended bridge caps are 15 mm/s path, 15 mm/s total linear, 3 mm/s normal reserve, and 0.060 rad/s attitude.",
        },
        "step4e_path": {
            "type": args.bridge_path_shape,
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
                if args.bridge_path_shape == "cycloid" and args.bridge_profile != "step5b_v1"
                else None
            ),
            "step5_stage_id": (
                STEP5_CONTACT_CYCLOID_STAGE_ID
                if args.bridge_profile == "step5b_v1"
                else args.bridge_profile
                if args.bridge_profile in STEP5D_LIVEPREP_STAGE_IDS
                else None
            ),
            "step5c_stage_id": args.bridge_profile
            if args.bridge_profile in {STEP5C_DRYRUN_STAGE_ID, STEP5C_CONTACT_STAGE_ID}
            else None,
            "step5c_register_contract": "Step5c/Step5d Stage 25.0: 37..42=qd0..qd5 rad/s, 43=cmd_valid, 44=path_time, 45=force_error, 46=pose/orientation_error, 47=solver_status. Step5d v8/v9 Stage 25.3: 37..39=Cartesian force-PID settle vx/vy/vz only. Step5d v10 Stage 25.3: 37..39=Cartesian admittance settle vx/vy/vz only and 45 carries filtered force_error. Step5d v11/v12 Stage 25.3: 37..39=Cartesian deadband-acquire vx/vy/vz only and 45 carries filtered force_error. Step5d v12 Stage 25.0 additionally gates loss-of-contact and TCP speed before cmd_valid. Step4e field names are carrier names only in joint mode."
            if args.bridge_profile in {STEP5C_DRYRUN_STAGE_ID, STEP5C_CONTACT_STAGE_ID, *STEP5D_LIVEPREP_STAGE_IDS}
            else None,
            "step5c_joint_model": str(args.step5c_joint_model)
            if args.bridge_profile in {STEP5C_DRYRUN_STAGE_ID, STEP5C_CONTACT_STAGE_ID}
            else None,
            "step5c_qdot_limit_rad_s": args.step5c_qdot_limit_rad_s
            if args.bridge_profile in {STEP5C_DRYRUN_STAGE_ID, STEP5C_CONTACT_STAGE_ID}
            else None,
            "step6_stage_id": (
                STEP6_CONTACT_EIGHT_STAGE_ID_V2
                if args.bridge_profile == "step6b_v2"
                else STEP6_CONTACT_EIGHT_STAGE_ID
                if args.bridge_profile == "step6b_v1"
                else None
            ),
            "step6_curve_duration_s": STEP6_PATH_DURATION_S if args.bridge_profile in {"step6b_v1", "step6b_v2"} else None,
            "step6_table_source": str(STEP6_TABLE_PATH.relative_to(EXPERIMENT_ROOT))
            if args.bridge_profile in {"step6b_v1", "step6b_v2"}
            else None,
            "step6_safe_frame_source": str(STEP6_SAFE_FRAME_PATH.relative_to(EXPERIMENT_ROOT))
            if args.bridge_profile in {"step6b_v1", "step6b_v2"}
            else None,
            "step6_safe_frame": load_step6_safe_frame() if args.bridge_profile in {"step6b_v1", "step6b_v2"} else None,
            "step4e_motion_limit_m_s": args.bridge_motion_limit_m_s,
            "step4e_total_linear_limit_m_s": args.bridge_total_linear_limit_m_s,
            "step4e_normal_velocity_limit_m_s": args.bridge_normal_velocity_limit_m_s,
            "step4e_angular_limit_rad_s": args.bridge_angular_limit_rad_s,
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
    step4e_state = BridgeState()

    next_write = start_mono
    write_period = 1.0 / args.rtde_hz
    dashboard_watch_enabled = (
        args.bridge_profile in STEP5D_LIVEPREP_STAGE_IDS
        and not args.skip_dashboard_preflight
        and not args.disable_dashboard_program_watch
    )
    dashboard_watch_saw_running = False
    next_dashboard_watch = start_mono

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
            "_step5b_15n_trial_active",
            "_step5b_15n_trial_acquired",
            "_step5b_15n_trial_after_acquire_s",
            "_step5b_15n_trial_scored_s",
            "_step5b_15n_trial_anchor_x_m",
            "_step5b_15n_trial_anchor_y_m",
            "_step5b_15n_trial_low_load_s",
            "_step5b_15n_trial_relative_low_load_s",
            "_step5b_15n_trial_saturation_s",
            "_step5b_15n_trial_normal_velocity_saturated",
            "_step5b_15n_trial_stop_reason",
            "_step5b_ramp_active",
            "_step5b_ramp_phase_code",
            "_step5b_ramp_phase",
            "_step5b_ramp_phase_s",
            "_step5b_ramp_active_target_force_n",
            "_step5b_ramp_alpha",
            "_step5b_ramp_xy_enabled",
            "_step5b_ramp_preload_ready_s",
            "_step5b_ramp_terminal_discard_s",
            "_step5b_ramp_scored_s",
            "_step5b_ramp_move_s",
            "_step5b_ramp_low_load_s",
            "_step5b_ramp_relative_low_load_s",
            "_step5b_ramp_saturation_s",
            "_step5b_ramp_normal_velocity_saturated",
            "_step5b_ramp_stop_reason",
            *STEP5C_DIAG_FIELDS,
            *STEP5D_DIAG_FIELDS,
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
                if dashboard_watch_enabled and now >= next_dashboard_watch:
                    dash = dashboard_exchange(args.robot_host, ["running", "programState", "safetymode"])
                    if "NORMAL" not in dash.get("safetymode", ""):
                        stop_reason = "dashboard_safety_not_normal"
                        break
                    running = "true" in dash.get("running", "").lower()
                    stopped = "STOPPED" in dash.get("programState", "").upper()
                    if running:
                        dashboard_watch_saw_running = True
                    elif dashboard_watch_saw_running and stopped:
                        stop_reason = "dashboard_program_stopped"
                        break
                    elif not dashboard_watch_saw_running and stopped and now - start_mono >= args.dashboard_program_watch_timeout_s:
                        stop_reason = "dashboard_play_timeout"
                        break
                    next_dashboard_watch = now + 0.25

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
                                args.bridge_mode in {"hold", "line"}
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
                    step4e_values = compute_bridge_values(
                        args,
                        latest_zeroed,
                        latest_output,
                        sensor_ok,
                        step4e_state,
                        write_period,
                    )
                    for name in BRIDGE_INPUT_NAMES:
                        bridge_values[name] = float(step4e_values.get(name, 0.0))
                    guard_reason = None
                    step4e_stop_request = float(step4e_values.get("stop_request", 0.0)) > 0.5
                    if step4e_stop_request:
                        bridge_values["stop_request"] = 1.0
                        stop_request = 1.0
                        step5b_trial_reason = str(step4e_values.get("_step5b_15n_trial_stop_reason", ""))
                        step5b_ramp_reason = str(step4e_values.get("_step5b_ramp_stop_reason", ""))
                        if step5b_trial_reason.startswith("step5b_15n_trial:"):
                            guard_reason = step5b_trial_reason
                        elif step5b_ramp_reason.startswith("step5b_ramp_5_to_15:"):
                            guard_reason = step5b_ramp_reason
                        else:
                            guard_reason = "step5d_contact_safety:" + str(
                                step4e_values.get("_step5d_contact_safety_reason", "stop_request")
                            )
                        stop_reason = guard_reason
                    if sensor_ok:
                        hard_guard_reason = guard_stop_reason(args, bridge_values)
                        if hard_guard_reason is not None:
                            bridge_values["stop_request"] = 1.0
                            stop_request = 1.0
                            guard_reason = hard_guard_reason
                            stop_reason = hard_guard_reason
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
    if step5b_15n_trial_enabled(args):
        trial_summary = write_step5b_15n_trial_summary(
            output_dir=args.output_dir,
            bridge_csv_path=bridge_csv_path,
            metadata=metadata,
            stop_reason=stop_reason,
        )
        summary["step5b_15n_guarded_trial"] = trial_summary
        summary["paths"]["step5b_15n_guarded_trial_summary_json"] = trial_summary["paths"]["trial_summary_json"]
        summary["paths"]["step5b_15n_guarded_trial_summary_md"] = trial_summary["paths"]["trial_summary_md"]
    if step5b_ramp_trial_enabled(args):
        trial_summary = write_step5b_ramp_trial_summary(
            output_dir=args.output_dir,
            bridge_csv_path=bridge_csv_path,
            metadata=metadata,
            stop_reason=stop_reason,
        )
        summary["step5b_ramp_5_to_15_sentinel"] = trial_summary
        summary["paths"]["step5b_ramp_5_to_15_sentinel_summary_json"] = trial_summary["paths"]["trial_summary_json"]
        summary["paths"]["step5b_ramp_5_to_15_sentinel_summary_md"] = trial_summary["paths"]["trial_summary_md"]
    write_json(summary_path, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if samples > 0 and parse_errors == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
