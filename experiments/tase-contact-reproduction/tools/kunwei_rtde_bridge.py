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
import hashlib
import json
import math
import os
import re
import select
import signal
import socket
import statistics
import struct
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

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
from contact_semantics import (  # noqa: E402
    semantic_boundary_is_consistent,
    twist_base_to_same_origin,
    twist_same_origin_to_base,
)
from step_pose_contract import PRE_CONTACT_GRAVITY_DOWN_CONTRACT_ID, contract_target_axis_base  # noqa: E402
import step5c_calibrated_kinematics_audit as step5d_kin  # noqa: E402
from step5_table import step5_path_reference  # noqa: E402
from step5c_dls_joint_solver import JointSolverConfig, STATUS_INVALID, Step5cDlsJointSolver  # noqa: E402
from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver  # noqa: E402
from verify_step5d_current_binding import (  # noqa: E402
    verify_binding as verify_step5d_binding,
    verify_live_bridge_authorization as verify_step5d_live_bridge_authorization,
)
from step5d_paper_outer_loop import (  # noqa: E402
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
    rnn_target_state_from_outer_loop,
)
from step5d_p0_v8_control_core import (  # noqa: E402
    P0V8Target,
    build_p0_v8_target as shared_build_p0_v8_target,
    limit_p0_xdot_components as shared_limit_p0_xdot_components,
    low_force_posture_policy as shared_low_force_posture_policy,
    press_only_outer_output as shared_press_only_outer_output,
    scale_xdot_for_joint_feasibility as shared_scale_xdot_for_joint_feasibility,
)
from step5d_control_contract import (  # noqa: E402
    ControlCandidate,
    DeferredV30Diagnostics,
    RegisterCommand,
    SafetyEnvelope,
    SafetyDecision,
    Step5dObservation,
    StrictRnnControlPolicy,
    apply_direction_preserving_slew,
    compute_dls_shadow,
    decision_to_register_command,
    step5d_v30_contract_pipeline as shared_step5d_v30_contract_pipeline,
    step5d_v30_control_step as shared_step5d_v30_control_step,
)
from step5d_p0_v8_gate import (  # noqa: E402
    CANARY_PHASES_S as STEP5D_P0_V8_CANARY_PHASES_S,
    authorize_canary as authorize_p0_v8_canary,
    validate_canary_phase as validate_p0_v8_canary_phase,
)
from step5d_runtime_interface import (  # noqa: E402
    STEP5D_ABLATION_STAGE_IDS,
    STEP5D_ABLATION_V25_STAGE_ID,
    STEP5D_ABLATION_V26_STAGE_ID,
    STEP5D_ABLATION_V27_STAGE_ID,
    STEP5D_ABLATION_V28_STAGE_ID,
    STEP5D_ABLATION_V29_STAGE_ID,
    STEP5D_ABLATION_V30_STAGE_ID,
    STEP5D_NO_CONTACT_P0_STAGE_ID,
    STEP5D_NO_CONTACT_P0_STAGE_IDS,
    STEP5D_NO_CONTACT_P0_V8_STAGE_ID,
    STEP5D_V30_CONTROL_CONTRACT_STAGE_IDS,
    STEP5D_LINE_ENTRY_PARAM_VALID_CODE,
    STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE,
    STEP5D_STAGE25_CONTROL_MODES,
    STEP5D_STAGE25_JOINT_LAYOUT_CODE,
    STEP5D_NO_CONTACT_P0_BASELINE_S,
    STEP5D_NO_CONTACT_P0_DURATION_S,
    STEP5D_NO_CONTACT_P0_FORCE_GUARD_N,
    STEP5D_NO_CONTACT_P0_ANGULAR_LIMIT_RAD_S,
    STEP5D_NO_CONTACT_P0_FORCE_DAMPING,
    STEP5D_NO_CONTACT_P0_FORCE_I_GAIN,
    STEP5D_NO_CONTACT_P0_FORCE_P_GAIN,
    STEP5D_NO_CONTACT_P0_INTEGRAL_LIMIT_N_S,
    STEP5D_NO_CONTACT_P0_MOTION_LIMIT_M_S,
    STEP5D_NO_CONTACT_P0_NORMAL_GUARD_N,
    STEP5D_NO_CONTACT_P0_NORMAL_FILTER_ALPHA,
    STEP5D_NO_CONTACT_P0_NORMAL_MIN_FORCE_N,
    STEP5D_NO_CONTACT_P0_NORMAL_VELOCITY_LIMIT_M_S,
    STEP5D_NO_CONTACT_P0_PRELOAD_TIMEOUT_S,
    STEP5D_NO_CONTACT_P0_EPSILON,
    STEP5D_NO_CONTACT_P0_QDOT_CAP_RAD_S,
    STEP5D_NO_CONTACT_P0_REZERO_S,
    STEP5D_NO_CONTACT_P0_RNN_BACKEND,
    STEP5D_NO_CONTACT_P0_RNN_INNER_ITERATIONS,
    STEP5D_NO_CONTACT_P0_V8_RNN_INNER_ITERATIONS,
    STEP5D_NO_CONTACT_P0_RTDE_HZ,
    STEP5D_NO_CONTACT_P0_SENSOR_STALE_S,
    STEP5D_NO_CONTACT_P0_SIGR_EXPONENT_R,
    STEP5D_NO_CONTACT_P0_SOCKET_TIMEOUT_S,
    STEP5D_NO_CONTACT_P0_TARGET_FORCE_N,
    STEP5D_NO_CONTACT_P0_TORQUE_GUARD_NM,
    STEP5D_NO_CONTACT_P0_TOTAL_LINEAR_LIMIT_M_S,
    STEP5D_V30_RNN_INNER_ITERATIONS,
    is_no_contact_p0_stage,
    uses_v30_control_contract,
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
    "_step5d_stage25_control_mode",
    "_step5d_stage25_echo_layout_tag",
    "_step5d_stage25_echo_cmd_valid",
    "_step5d_stage25_echo_command_norm",
    "_step5d_stage25_echo_consumed",
    "_step5d_stage25_row_gap_s",
    "_step5d_solver_status",
    "_step5d_qdot_max_abs_rad_s",
    "_step5d_constraint_residual_norm",
    "_step5d_outer_xdot_norm",
    "_step5d_outer_xdot_limited_norm",
    "_step5d_outer_xdot_joint_feasible_norm",
    "_step5d_outer_xdot_limiter_active",
    "_step5d_qdot_cap_rad_s",
    "_step5d_jinv_xdot_inf_rad_s",
    "_step5d_jinv_xdot_inf_over_qdot_cap",
    "_step5d_jinv_xdot_solve_status",
    "_step5d_xdot_feasibility_scale",
    "_step5d_xdot_norm_pre_feasibility_scale",
    "_step5d_xdot_norm_post_feasibility_scale",
    "_step5d_xdot_feasibility_scale_active",
    "_step5d_oracle_residual_norm",
    "_step5d_oracle_qdot_max_abs_rad_s",
    "_step5d_cmd_residual_norm",
    "_step5d_rnn_vs_oracle_qdot_norm",
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
    "_step5d_predicted_tcp_vx_m_s",
    "_step5d_predicted_tcp_vy_m_s",
    "_step5d_predicted_tcp_vz_m_s",
    "_step5d_outer_xdot_limited_approach_normal_m_s",
    "_step5d_jqdot_raw_approach_normal_m_s",
    "_step5d_jqdot_post_slew_approach_normal_m_s",
    "_step5d_jqdot_cmd_approach_normal_m_s",
    "_step5d_predicted_press_speed_m_s",
    "_step5d_actual_press_speed_m_s",
    "_step5d_normal_load_rate_n_s",
    "_step5d_post_rnn_normal_guard_state",
    "_step5d_post_rnn_normal_guard_action",
    "_step5d_post_rnn_normal_guard_reason",
    "_step5d_normal_direction_guard_dwell_s",
    "_step5d_normal_direction_guard_zeroed_qdot",
    "_step5d_rnn_qdot_max_abs_raw_rad_s",
    "_step5d_qdot_max_abs_after_guard_rad_s",
    "_step5d_intervention_reason",
    "_step5d_live_control_source",
    "_step5d_stage25_entry_relatch_angle_rad",
    "_step5d_live_orientation_enabled",
    "_step5d_speedl_orientation_shadow_only",
    "_step5d_speedl_shadow_raw_vx_m_s",
    "_step5d_speedl_shadow_raw_vy_m_s",
    "_step5d_speedl_shadow_raw_vz_m_s",
    "_step5d_speedl_shadow_raw_wx_rad_s",
    "_step5d_speedl_shadow_raw_wy_rad_s",
    "_step5d_speedl_shadow_raw_wz_rad_s",
    "_step5d_reacquire_speed_cap_active",
    "_step5d_reacquire_speed_cap_m_s",
    "_step5d_reacquire_speed_cap_original_m_s",
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
    "_step5d_active_reacquire_s",
    "_step5d_no_contact_s",
    "_step5d_contact_safety_reason",
    "_step5d_search_pose_contract_active",
    "_step5d_search_pose_contract_ok",
    "_step5d_search_pose_contract_axis_error_rad",
    "_step5d_search_pose_contract_tcp_z_dot_down",
    "_step5d_control_normal_vs_world_z_angle_rad",
    "_step5d_control_normal_vs_tcp_z_angle_rad",
    "_step5d_approach_normal_vs_tcp_z_angle_rad",
    "_step5d_force_settle_ready",
    "_step5d_force_settle_filtered_normal_load_n",
    "_step5d_force_settle_velocity_m_s",
    "_step5d_force_sign_convention",
    "_step5d_proj_input_form",
    "_step5d_lambda_update_form",
    "_step5d_rnn_inner_iterations",
    "_step5d_rnn_backend",
    "_step5d_rnn_solve_wall_ms",
    "_step5d_rnn_epsilon",
    "_step5d_rnn_sigr_exponent_r",
    "_step5d_active_bounds_count",
    "_step5d_lambda_norm",
    *[f"_step5d_rnn_raw_qd{idx}_rad_s" for idx in range(6)],
    *[f"_step5d_post_slew_qd{idx}_rad_s" for idx in range(6)],
    *[f"_step5d_active_bound_qd{idx}" for idx in range(6)],
    *[f"_step5d_lambda_state_{idx}" for idx in range(6)],
    "_step5d_p0_low_force_posture_policy",
    "_step5d_p0_low_force_posture_active",
    "_step5d_p0_posture_gain_scale",
    "_step5d_p0_effective_ko",
    "_step5d_p0_frame_transform_valid",
    "_step5d_p0_frame_transform_mode",
    "_step5d_p0_frame_transform_reason",
    "_step5d_p0_limited_tcp_vx_m_s",
    "_step5d_p0_limited_tcp_vy_m_s",
    "_step5d_p0_limited_tcp_vz_m_s",
    "_step5d_p0_limited_tcp_wx_rad_s",
    "_step5d_p0_limited_tcp_wy_rad_s",
    "_step5d_p0_limited_tcp_wz_rad_s",
    "_step5d_p0_limited_base_vx_m_s",
    "_step5d_p0_limited_base_vy_m_s",
    "_step5d_p0_limited_base_vz_m_s",
    "_step5d_p0_tcp_press_speed_m_s",
    "_step5d_rnn_accepted",
    "_step5d_rnn_reject_reason",
    "_step5d_safe_hold_active",
    "_step5d_p0_rnn_accepted",
    "_step5d_p0_rnn_reject_reason",
    "_step5d_p0_safe_hold_active",
    "_step5d_p0_v8_contract_active",
    "_step5d_p0_v8_canary_phase_s",
    "_step5d_p0_v8_canary_stop_active",
    "_step5d_dls_shadow_present",
    "_step5d_dls_shadow_runtime_fallback_allowed",
    "_step5d_dls_shadow_normal_sign_difference",
    "_step5d_dls_shadow_residual_norm",
    "_step5d_dls_shadow_saturation_count",
    "_step5d_dls_shadow_qdot_delta_norm",
    "_step5d_dls_shadow_twist_delta_norm",
    "_step5d_cmd_valid_reason",
    "_step5d_contact_orientation_error_rad",
    "_step5d_outer_orientation_error_rad",
    "_step5d_R_d_z_dot_R_cur_z",
    "_step5d_semantic_gate_ok",
    "_step5d_solver_error",
    "_bridge_loop_gap_s",
    "_bridge_loop_deadline_lateness_s",
    "_bridge_loop_missed_slots",
    "_bridge_loop_deadline_miss_total",
    "_bridge_loop_deadline_overrun_hold",
    "_bridge_loop_deadline_overrun_hold_total",
    "_bridge_loop_deadline_overrun_consecutive",
    "_bridge_loop_sensor_recv_s",
    "_bridge_loop_rtde_recv_s",
    "_bridge_loop_compute_s",
    "_bridge_loop_rtde_send_s",
    "_bridge_loop_csv_write_s",
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
BIAS_VECTOR_NAMES = ("fx_n", "fy_n", "fz_n", "mx_nm", "my_nm", "mz_nm")
BIAS_ESTIMATE_FIELDS = [f"bias_est_{name}" for name in BIAS_VECTOR_NAMES]
BIAS_RATE_ESTIMATE_FIELDS = [f"bias_rate_est_{name}_per_s" for name in BIAS_VECTOR_NAMES]
BIAS_LOG_FIELDS = [
    "zero_event_id",
    "contact_mask",
    "bias_estimation_contact_mask",
    "bias_contact_reason",
    *BIAS_ESTIMATE_FIELDS,
    *BIAS_RATE_ESTIMATE_FIELDS,
]
BIAS_BRIDGE_LOG_FIELDS = [
    *BIAS_LOG_FIELDS,
    "control_contact_window",
]
KINEMATIC_DERIVED_FIELDS = [
    *[f"ur_actual_qdd_{idx}" for idx in range(6)],
    *[f"ur_actual_TCP_accel_{idx}" for idx in range(6)],
    "ur_kinematics_dt_s",
]
DEFAULT_BIAS_CONTACT_NORMAL_THRESHOLD_N = 0.75
DEFAULT_BIAS_CONTACT_FORCE_NORM_THRESHOLD_N = 2.0


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
STEP5B_BRIDGE_PROFILES = {"step5b_v1", "step5b_v2", "step5b_v3"}
STEP5B_15N_TRIAL_PROFILE = "guarded_15n_sentinel"
STEP5B_RAMP_5_TO_15_TRIAL_PROFILE = "ramp_5_to_15_sentinel"
STEP5B_TRIAL_PROFILES = ("none", STEP5B_15N_TRIAL_PROFILE, STEP5B_RAMP_5_TO_15_TRIAL_PROFILE)
STEP5B_15N_TRIAL_TARGET_N = 15.0
STEP5B_15N_TRIAL_TARGET_TOL_N = 0.1
STEP5B_15N_TRIAL_ACQUIRE_N = 12.0
STEP5B_15N_TRIAL_ACQUIRE_TIMEOUT_S = 2.0
STEP5B_15N_TRIAL_DISCARD_S = 0.5
STEP5B_15N_TRIAL_SCORE_S = 2.0
STEP5B_FORCE_GUARD_MIN_N = 50.0
STEP5B_15N_TRIAL_NORMAL_STOP_N = 50.0
STEP5B_15N_TRIAL_NORMAL_DWELL_N = 50.0
STEP5B_15N_TRIAL_NORMAL_DWELL_S = 0.050
STEP5B_15N_TRIAL_FORCE_STOP_N = 60.0
STEP5B_15N_TRIAL_FORCE_DWELL_N = 50.0
STEP5B_15N_TRIAL_FORCE_DWELL_S = 0.050
STEP5B_15N_TRIAL_TORQUE_STOP_NM = 3.0
STEP5B_15N_TRIAL_TORQUE_DWELL_NM = 2.5
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
STEP5B_RAMP_PRELOAD_NORMAL_STOP_N = 50.0
STEP5B_RAMP_PRELOAD_FORCE_STOP_N = 60.0
STEP5B_RAMP_NORMAL_STOP_MARGIN_N = 35.0
STEP5B_RAMP_NORMAL_DWELL_MARGIN_N = 35.0
STEP5B_RAMP_FORCE_STOP_MARGIN_N = 45.0
STEP5B_RAMP_FORCE_DWELL_MARGIN_N = 35.0
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
STEP5D_LIVEPREP_V16_STAGE_ID = "step5d_strict_rnn_liveprep_v16"
STEP5D_LIVEPREP_V17_STAGE_ID = "step5d_strict_rnn_liveprep_v17"
STEP5D_LIVEPREP_V18_STAGE_ID = "step5d_strict_rnn_liveprep_v18"
STEP5D_LIVEPREP_V19_STAGE_ID = "step5d_strict_rnn_liveprep_v19"
STEP5D_LIVEPREP_V20_STAGE_ID = "step5d_strict_rnn_liveprep_v20"
STEP5D_LIVEPREP_V21_STAGE_ID = "step5d_strict_rnn_liveprep_v21"
STEP5D_LIVEPREP_V22_STAGE_ID = "step5d_strict_rnn_liveprep_v22"
STEP5D_LIVEPREP_V23_STAGE_ID = "step5d_strict_rnn_liveprep_v23"
STEP5D_LIVEPREP_V24_STAGE_ID = "step5d_strict_rnn_liveprep_v24"
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
    STEP5D_LIVEPREP_V16_STAGE_ID,
    STEP5D_LIVEPREP_V17_STAGE_ID,
    STEP5D_LIVEPREP_V18_STAGE_ID,
    STEP5D_LIVEPREP_V19_STAGE_ID,
    STEP5D_LIVEPREP_V20_STAGE_ID,
    STEP5D_LIVEPREP_V21_STAGE_ID,
    STEP5D_LIVEPREP_V22_STAGE_ID,
    STEP5D_LIVEPREP_V23_STAGE_ID,
    STEP5D_LIVEPREP_V24_STAGE_ID,
    STEP5D_ABLATION_V25_STAGE_ID,
    STEP5D_ABLATION_V26_STAGE_ID,
    STEP5D_ABLATION_V27_STAGE_ID,
    STEP5D_ABLATION_V28_STAGE_ID,
    STEP5D_ABLATION_V29_STAGE_ID,
    STEP5D_ABLATION_V30_STAGE_ID,
    *STEP5D_NO_CONTACT_P0_STAGE_IDS,
}
STEP5D_TCP_CAGE_PROFILES = {
    STEP5D_LIVEPREP_V15A_STAGE_ID,
    STEP5D_LIVEPREP_V16_STAGE_ID,
    STEP5D_LIVEPREP_V17_STAGE_ID,
    STEP5D_LIVEPREP_V18_STAGE_ID,
    STEP5D_LIVEPREP_V19_STAGE_ID,
    STEP5D_LIVEPREP_V20_STAGE_ID,
    STEP5D_LIVEPREP_V21_STAGE_ID,
    STEP5D_LIVEPREP_V22_STAGE_ID,
    STEP5D_LIVEPREP_V23_STAGE_ID,
    STEP5D_LIVEPREP_V24_STAGE_ID,
    STEP5D_ABLATION_V25_STAGE_ID,
    STEP5D_ABLATION_V26_STAGE_ID,
    STEP5D_ABLATION_V27_STAGE_ID,
    STEP5D_ABLATION_V28_STAGE_ID,
    STEP5D_ABLATION_V29_STAGE_ID,
}
STEP5D_V29_FAIL_STOP_DASHBOARD_TIMEOUT_S = 0.04
STEP5D_V29_FAIL_STOP_DASHBOARD_RETRY_S = 0.05
STEP5D_V29_FAIL_STOP_RTDE_RECONNECT_TIMEOUT_S = 0.008
STEP5D_V29_RUNTIME_DASHBOARD_WATCH_TIMEOUT_S = 0.02
STEP5D_V29_LIVE_CONFIRMATION = "LIVE STEP5D STRICT RNN LIVEPREP"
STEP5D_RT_PRIORITY = 20
STEP5D_V29_RT_PRIORITY = STEP5D_RT_PRIORITY
STEP5D_SEMANTIC_ORIENTATION_TOLERANCE_RAD = math.radians(5.0)
STEP5D_SEARCH_POSE_CONTRACT_ID = PRE_CONTACT_GRAVITY_DOWN_CONTRACT_ID
STEP5D_SEARCH_POSE_TARGET_AXIS_B = contract_target_axis_base(STEP5D_SEARCH_POSE_CONTRACT_ID)
STEP5D_SEARCH_POSE_RUNTIME_TOLERANCE_RAD = math.radians(2.0)
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
STEP5D_NO_CONTACT_P0_LINEAR_XY_COMPONENT_LIMIT_M_S = 0.010
STEP5D_NO_CONTACT_P0_LINEAR_Z_COMPONENT_LIMIT_M_S = 0.020
STEP5D_NO_CONTACT_P0_ANGULAR_COMPONENT_LIMIT_RAD_S = STEP5D_NO_CONTACT_P0_ANGULAR_LIMIT_RAD_S
STEP5D_NO_CONTACT_P0_PRESS_ONLY_SPEED_M_S = 0.00015
STEP5D_NO_CONTACT_P0_LOW_FORCE_POSTURE_POLICY = "freeze_until_contact_v1"
STEP5D_NO_CONTACT_P0_LOW_FORCE_POSTURE_LOW_LOAD_N = 1.0
STEP5D_NO_CONTACT_P0_LOW_FORCE_POSTURE_HIGH_LOAD_N = 2.0
STEP5D_NO_CONTACT_P0_LOW_FORCE_POSTURE_KO = 0.0
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
STEP5D_V16_ENTRY_NORMAL_LOAD_MIN_N = 5.0
STEP5D_V16_ENTRY_NORMAL_LOAD_MAX_N = 20.0
STEP5D_V16_VALID_CONTACT_MIN_N = 5.0
STEP5D_V16_VALID_CONTACT_MAX_N = 20.0
STEP5D_V16_SOFT_LOW_LOAD_N = 5.0
STEP5D_V16_LOW_LOAD_SPEED_LOAD_N = 5.0
STEP5D_V16_LOW_LOAD_HOLD_TIMEOUT_S = 0.500
STEP5D_V16_HIGH_WINDOW_DWELL_STOP_S = 0.050
STEP5D_V17_ENTRY_FILTERED_NORMAL_LOAD_MIN_N = 8.0
STEP5D_V17_ENTRY_FILTERED_NORMAL_LOAD_MAX_N = 18.0
STEP5D_V17_ENTRY_RAW_NORMAL_LOAD_MIN_N = 7.5
STEP5D_V17_ENTRY_RAW_NORMAL_LOAD_MAX_N = 19.0
STEP5D_V19_ENTRY_FILTERED_NORMAL_LOAD_MAX_N = 13.0
STEP5D_V19_ENTRY_RAW_NORMAL_LOAD_MAX_N = 14.0
STEP5D_V21_ENTRY_FILTERED_NORMAL_LOAD_MIN_N = 7.5
STEP5D_V21_ENTRY_FILTERED_NORMAL_LOAD_MAX_N = 14.0
STEP5D_V21_ENTRY_RAW_NORMAL_LOAD_MIN_N = 7.0
STEP5D_V21_ENTRY_RAW_NORMAL_LOAD_MAX_N = 15.0
STEP5D_V21_ENTRY_FORCE_NORM_MAX_N = 25.0
STEP5D_V21_ENTRY_REQUIRED_S = 0.100
STEP5D_V21_ENTRY_TIMEOUT_S = 10.0
STEP5D_V21_ENTRY_CMD_LIMIT_M_S = 0.003
STEP5D_QDOT_CLEAR_STAGE = 25.95
STEP5D_QDOT_CLEAR_MODE_CODE = 522.0
STEP5D_V19_REACQUIRE_PREDICTED_TCP_SPEED_CAP_M_S = 0.035
STEP5D_V23_NORMAL_GUARD_HOLD_LOAD_N = 14.0
STEP5D_V23_NORMAL_GUARD_DIRECTIONAL_STOP_LOAD_N = 18.0
STEP5D_V23_NORMAL_GUARD_HARD_STOP_LOAD_N = 25.0
STEP5D_V23_NORMAL_GUARD_HARD_STOP_FORCE_NORM_N = 25.0
STEP5D_V23_NORMAL_GUARD_PREDICTED_PRESS_HOLD_M_S = 0.0005
STEP5D_V23_NORMAL_GUARD_ACTUAL_PRESS_HOLD_M_S = 0.0010
STEP5D_V23_NORMAL_GUARD_LOAD_RATE_HOLD_N_S = 20.0
STEP5D_V23_NORMAL_GUARD_DIRECTIONAL_STOP_DWELL_S = 0.004
STEP5D_V24_SENSOR_FORCE_HARD_STOP_N = 25.0
STEP5D_V24_LOW_LOAD_HOLD_TIMEOUT_S = 0.050
STEP5D_V24_RNN_TRACKING_OPPOSED_DWELL_S = 0.004
STEP5D_V24_RNN_TRACKING_OUTER_PRESS_MIN_M_S = 0.0001
STEP5D_V24_RNN_TRACKING_UNLOAD_MIN_M_S = 0.0005
STEP5D_V25_ENTRY_FILTERED_NORMAL_LOAD_MIN_N = 10.5
STEP5D_V25_ENTRY_FILTERED_NORMAL_LOAD_MAX_N = 12.8
STEP5D_V25_ENTRY_RAW_NORMAL_LOAD_MIN_N = 9.5
STEP5D_V25_ENTRY_RAW_NORMAL_LOAD_MAX_N = 13.5
STEP5D_V26_ENTRY_FILTERED_NORMAL_LOAD_MIN_N = 7.0
STEP5D_V26_ENTRY_FILTERED_NORMAL_LOAD_MAX_N = 18.0
STEP5D_V26_ENTRY_RAW_NORMAL_LOAD_MIN_N = 5.0
STEP5D_V26_ENTRY_RAW_NORMAL_LOAD_MAX_N = 20.0
STEP5D_V26_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N = 24.0
STEP5D_V27_ENTRY_FILTERED_NORMAL_LOAD_MIN_N = 5.0
STEP5D_V27_ENTRY_FILTERED_NORMAL_LOAD_MAX_N = 22.0
STEP5D_V27_ENTRY_RAW_NORMAL_LOAD_MIN_N = 3.0
STEP5D_V27_ENTRY_RAW_NORMAL_LOAD_MAX_N = 25.0
STEP5D_V27_ENTRY_FORCE_NORM_MAX_N = 35.0
STEP5D_V27_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N = 35.0
STEP5D_V27_SENSOR_NORMAL_HARD_STOP_N = 50.0
STEP5D_V27_SENSOR_FORCE_HARD_STOP_N = 60.0
STEP5D_V27_SENSOR_TORQUE_HARD_STOP_NM = 3.0
STEP5D_V27_SPEEDL_LIVE_CONTROL_SOURCE = "step5b_speedl_live_step5d_shadow"
# Bridge-side realizability recalibration of the paper outer loop for the
# v27/v28 shadow lane (paper originals: Md=12, Bd=550, ko=5.0). The paper
# values assume continuous, delay-free feedback; against the measured ~50ms
# force batching and the 0.004 m/s / 0.015 rad/s command box the paper
# admittance's proportional force gain (1/Bd ~= 1.8 mm/s per N; kf scales the
# integral term only, see force_motion_acceleration_base) is ~20x the
# proven-stable Step5b evidence gain (~0.09 mm/s per N), and the orientation
# gain saturates the angular channel (5.0 * 0.117 rad >> 0.015 rad/s).
# Scaling Md and Bd together by 20 sets the proportional gain to 1/11000 ~=
# 0.091 mm/s per N while preserving the paper's admittance pole Bd/Md; ko=0.5
# keeps orientation demand inside the box for errors up to ~0.03 rad while
# still tracking the <=0.01 rad/s normal-follow reference drift. Validated
# offline by tools/replay_step5d_outer_loop.py against the v28 60s run.
STEP5D_V28_SHADOW_ADMITTANCE_SCALE = 20.0
STEP5D_V28_SHADOW_MD = 12.0 * STEP5D_V28_SHADOW_ADMITTANCE_SCALE
STEP5D_V28_SHADOW_BD = 550.0 * STEP5D_V28_SHADOW_ADMITTANCE_SCALE
STEP5D_V28_SHADOW_KO = 0.5
STEP5D_ABLATION_SPEEDL_ORIENTATION_SHADOW_ONLY = True
# v29 step: execute the Step5b/step4e orientation-follow command live again
# (gain 0.2 against the re-latched normal reference, existing 0.015 rad/s
# limit). The Step5b 60s baselines always ran with live orientation follow;
# v27/v28 zeroed it as emergency isolation while the (then miscalibrated)
# paper force channel was suspect. With the entry re-latch the follow demand
# stays inside the box (0.2 * ~0.05 rad ~= 0.011 rad/s). The Step5d paper/RNN
# outputs remain shadow-only regardless of this switch.
STEP5D_V28_LIVE_STEP5B_ORIENTATION = True
STEP5D_V25_HARD_LOW_LOAD_N = 2.0
STEP5D_V25_HARD_LOW_LOAD_TIMEOUT_S = 0.100
STEP5D_V25_SOFT_LOW_LOAD_N = 5.0
STEP5D_V25_SOFT_LOW_LOAD_TIMEOUT_S = 0.500
STEP5D_V18_SENSOR_FORCE_HARD_STOP_N = 100.0
STEP5D_V18_SENSOR_TORQUE_HARD_STOP_NM = 4.0
STEP5D_V18_VALID_CONTACT_MAX_N = 100.0
STEP5D_V18_SOFT_LOW_LOAD_N = 5.0
STEP5D_V18_ACTIVE_REACQUIRE_LOAD_MAX_N = 5.0
STEP5D_V18_NO_CONTACT_LOAD_N = 0.25
STEP5D_V18_NORMAL_FOLLOW_SETTLE_S = 0.150
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
    "active_reacquire": 4.0,
}
STEP5D_V13_CONTACT_SAFETY_ACTIONS = {
    "inactive": 0.0,
    "pass_solver": 1.0,
    "hold_zero_qdot": 2.0,
    "stop_zero_qdot": 3.0,
    "active_reacquire_solver": 4.0,
}
STEP5D_V15A_TCP_CAGE_SOURCE_CSVS = [
    EXPERIMENT_ROOT / "runs" / "bridge_step5b_contact_cycloid_baseline_v1_20260612_082352" / "bridge_rtde_500hz.csv",
    EXPERIMENT_ROOT / "runs" / "bridge_step5b_contact_cycloid_baseline_v1_20260614_222309" / "bridge_rtde_500hz.csv",
    EXPERIMENT_ROOT / "runs" / "bridge_step6b_contact_eight_baseline_v1_20260612_223047" / "bridge_rtde_500hz.csv",
    EXPERIMENT_ROOT / "runs" / "bridge_step6b_contact_eight_baseline_v2_20260612_225841" / "bridge_rtde_500hz.csv",
    EXPERIMENT_ROOT / "runs" / "bridge_step6b_contact_eight_baseline_v2_20260614_223106" / "bridge_rtde_500hz.csv",
]
STEP5D_V15A_TCP_CAGE_CACHE_PATH = EXPERIMENT_ROOT / "config" / "step5d_v15a_tcp_cage_cache.json"
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


def bridge_run_pointer_path() -> Path:
    return EXPERIMENT_ROOT / "runs" / "latest_run_pointer.json"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bridge_run_manifest(
    args: argparse.Namespace,
    *,
    argv: Sequence[str] | None = None,
    pointer_path: Path | None = None,
) -> dict[str, Any]:
    run_dir = Path(args.output_dir).resolve()
    started_at_epoch_s = time.time()
    started_at = datetime.fromtimestamp(started_at_epoch_s).isoformat(timespec="seconds")
    profile = str(getattr(args, "bridge_profile", ""))
    manifest = {
        "schema": "bridge_run_manifest.v1",
        "run_dir": str(run_dir),
        "profile": profile,
        "started_at": started_at,
        "started_at_epoch_s": started_at_epoch_s,
        "pid": os.getpid(),
        "argv": list(argv if argv is not None else sys.argv[1:]),
        "rtde_hz": float(getattr(args, "rtde_hz", 0.0)),
        "bridge_mode": str(getattr(args, "bridge_mode", getattr(args, "step4e_mode", ""))),
    }
    if profile == STEP5D_NO_CONTACT_P0_V8_STAGE_ID:
        current = json.loads(
            (EXPERIMENT_ROOT / "config" / "current_stage.json").read_text(
                encoding="utf-8"
            )
        )
        candidate = current.get("p0_v8_candidate") or {}
        capture = (current.get("bridge_trigger") or {}).get(
            "no_contact_p0_v8_capture"
        ) or {}
        policy_path = EXPERIMENT_ROOT / "config" / "step5d_review_policy_v2.json"
        manifest["p0_v8_canary"] = {
            "phase_s": float(getattr(args, "step5d_stop_register_canary_s", 0.0)),
            "composite_fingerprint": candidate.get("composite_fingerprint"),
            "review_manifest": (candidate.get("review_v2") or {}).get("manifest"),
            "package_sha256": capture.get("sha256"),
            "review_policy_sha256": file_sha256(policy_path),
            "prior_canaries": candidate.get("completed_canaries") or [],
            "terminal": {
                "stop_request_sent": False,
                "tp_stop_acknowledged": False,
            },
        }
    write_json(run_dir / "bridge_run_manifest.json", manifest)
    pointer = {
        "schema": "bridge_latest_run_pointer.v1",
        "run_dir": str(run_dir),
        "profile": profile,
        "started_at": started_at,
        "started_at_epoch_s": started_at_epoch_s,
        "pid": os.getpid(),
        "manifest": str(run_dir / "bridge_run_manifest.json"),
    }
    pointer_target = pointer_path or bridge_run_pointer_path()
    pointer_target.parent.mkdir(parents=True, exist_ok=True)
    write_json(pointer_target, pointer)
    return manifest


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


def finite_vector_derivative(
    current: list[float] | tuple[float, ...],
    previous: list[float] | tuple[float, ...] | None,
    dt_s: float | None,
    *,
    length: int = 6,
) -> list[float]:
    if previous is None or dt_s is None or not math.isfinite(dt_s) or dt_s <= 0.0:
        return [math.nan] * length
    if len(current) < length or len(previous) < length:
        return [math.nan] * length
    values: list[float] = []
    for idx in range(length):
        now_value = float(current[idx])
        previous_value = float(previous[idx])
        if not math.isfinite(now_value) or not math.isfinite(previous_value):
            values.append(math.nan)
        else:
            values.append((now_value - previous_value) / dt_s)
    return values


def bias_estimate_row(bias_estimate: list[float], bias_rate_estimate: list[float]) -> dict[str, float]:
    row: dict[str, float] = {}
    for name, value in zip(BIAS_ESTIMATE_FIELDS, bias_estimate):
        row[name] = float(value)
    for name, value in zip(BIAS_RATE_ESTIMATE_FIELDS, bias_rate_estimate):
        row[name] = float(value)
    return row


def bias_contact_mask(
    *,
    baseline_ready: bool,
    normal_load_n: float,
    force_norm_n: float,
    control_contact_window: bool = False,
    normal_threshold_n: float = DEFAULT_BIAS_CONTACT_NORMAL_THRESHOLD_N,
    force_norm_threshold_n: float = DEFAULT_BIAS_CONTACT_FORCE_NORM_THRESHOLD_N,
) -> tuple[int, str]:
    if not baseline_ready:
        return 0, "baseline_not_ready"
    if control_contact_window:
        return 1, "control_contact_window"
    if not all(math.isfinite(value) for value in (normal_load_n, force_norm_n)):
        return 0, "nonfinite_force"
    if abs(float(normal_load_n)) >= float(normal_threshold_n):
        return 1, "normal_load_threshold"
    if float(force_norm_n) >= float(force_norm_threshold_n):
        return 1, "force_norm_threshold"
    return 0, "no_contact"


def control_contact_window_from_bridge_values(step4e_values: dict[str, Any]) -> bool:
    values = [
        step4e_values.get("_step4e_normal_acquired", 0.0),
        step4e_values.get("_step5d_contact_safety_state", 0.0),
    ]
    for value in values:
        try:
            if float(value) > 0.5:
                return True
        except (TypeError, ValueError):
            continue
    return False


def derived_kinematics_row(
    current_output: dict[str, Any],
    previous_output: dict[str, Any] | None,
    dt_s: float | None,
) -> dict[str, float]:
    current_qd = current_output.get("actual_qd", [])
    current_tcp_speed = current_output.get("actual_TCP_speed", [])
    previous_qd = previous_output.get("actual_qd", []) if previous_output is not None else None
    previous_tcp_speed = previous_output.get("actual_TCP_speed", []) if previous_output is not None else None
    qdd = finite_vector_derivative(current_qd, previous_qd, dt_s)
    tcp_accel = finite_vector_derivative(current_tcp_speed, previous_tcp_speed, dt_s)
    row: dict[str, float] = {}
    for idx, value in enumerate(qdd):
        row[f"ur_actual_qdd_{idx}"] = value
    for idx, value in enumerate(tcp_accel):
        row[f"ur_actual_TCP_accel_{idx}"] = value
    row["ur_kinematics_dt_s"] = math.nan if dt_s is None else float(dt_s)
    return row


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


def _step5d_tcp_cage_source_fingerprint(
    paths: Sequence[Path],
    *,
    include_content_hash: bool = False,
) -> list[dict[str, int | str]]:
    fingerprint: list[dict[str, int | str]] = []
    for path in paths:
        stat = path.stat()
        try:
            # Preserve the experiment-relative locator even when a clean
            # worktree overlays immutable archived runs with symlinks.  Size,
            # mtime, and the optional content hash still bind the target; a
            # resolved absolute path would make the same evidence cache
            # non-portable solely because its storage root changed.
            path_id = str(path.relative_to(EXPERIMENT_ROOT))
        except ValueError:
            path_id = str(path.resolve())
        row = {
            "path": path_id,
            "size_bytes": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }
        if include_content_hash:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            row["sha256"] = digest.hexdigest()
        fingerprint.append(row)
    return fingerprint


def _step5d_tcp_cage_from_payload(payload: dict[str, Any]) -> Step5dTcpCage | None:
    try:
        min_xyz_raw = payload["min_xyz"]
        max_xyz_raw = payload["max_xyz"]
        source_rows = int(payload["source_rows"])
        source_csvs = [str(path) for path in payload["source_csvs"]]
        padding_m = float(payload["padding_m"])
        min_xyz_values = [float(value) for value in min_xyz_raw]
        max_xyz_values = [float(value) for value in max_xyz_raw]
    except (KeyError, TypeError, ValueError):
        return None
    if payload.get("mode") != "broad_stagewise_aabb_from_success_step5b_step6b":
        return None
    if len(min_xyz_values) != 3 or len(max_xyz_values) != 3:
        return None
    if not all(math.isfinite(value) for value in (*min_xyz_values, *max_xyz_values, padding_m)):
        return None
    if source_rows <= 0 or not source_csvs:
        return None
    return Step5dTcpCage(
        min_xyz=(min_xyz_values[0], min_xyz_values[1], min_xyz_values[2]),
        max_xyz=(max_xyz_values[0], max_xyz_values[1], max_xyz_values[2]),
        source_rows=source_rows,
        source_csvs=source_csvs,
        padding_m=padding_m,
    )


def _load_step5d_tcp_cage_cache(
    cache_path: Path,
    paths: Sequence[Path],
    *,
    padding_m: float,
    validate_content_hash: bool,
) -> Step5dTcpCage | None:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        fingerprint = _step5d_tcp_cage_source_fingerprint(paths, include_content_hash=False)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema") != "step5d_v15a_tcp_cage_cache_v1":
        return None
    if payload.get("source_fingerprint") != fingerprint:
        return None
    if validate_content_hash:
        content_fingerprint = _step5d_tcp_cage_source_fingerprint(paths, include_content_hash=True)
        if payload.get("source_content_fingerprint") != content_fingerprint:
            return None
    try:
        cached_padding_m = float(payload.get("padding_m"))
    except (TypeError, ValueError):
        return None
    if not math.isclose(cached_padding_m, float(padding_m), rel_tol=0.0, abs_tol=1e-12):
        return None
    return _step5d_tcp_cage_from_payload(payload)


def _write_step5d_tcp_cage_cache(
    cache_path: Path,
    cage: Step5dTcpCage,
    paths: Sequence[Path],
) -> None:
    payload = {
        "schema": "step5d_v15a_tcp_cage_cache_v1",
        "mode": cage.mode,
        "min_xyz": list(cage.min_xyz),
        "max_xyz": list(cage.max_xyz),
        "padding_m": cage.padding_m,
        "source_rows": cage.source_rows,
        "source_csvs": cage.source_csvs,
        "source_fingerprint": _step5d_tcp_cage_source_fingerprint(paths, include_content_hash=False),
        "source_content_fingerprint": _step5d_tcp_cage_source_fingerprint(paths, include_content_hash=True),
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(cache_path, payload)


def build_step5d_v15a_tcp_cage(
    source_csvs: list[Path] | None = None,
    *,
    cache_path: Path | None = None,
    validate_content_hash: bool = False,
) -> Step5dTcpCage:
    paths = STEP5D_V15A_TCP_CAGE_SOURCE_CSVS if source_csvs is None else source_csvs
    effective_cache_path = cache_path
    if source_csvs is None and effective_cache_path is None:
        effective_cache_path = STEP5D_V15A_TCP_CAGE_CACHE_PATH
    padding = STEP5D_V15A_TCP_CAGE_PADDING_M
    if effective_cache_path is not None:
        cached = _load_step5d_tcp_cage_cache(
            effective_cache_path,
            paths,
            padding_m=padding,
            validate_content_hash=validate_content_hash,
        )
        if cached is not None:
            return cached
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
    cage = Step5dTcpCage(
        min_xyz=(min(xs) - padding, min(ys) - padding, min(zs) - padding),
        max_xyz=(max(xs) + padding, max(ys) + padding, max(zs) + padding),
        source_rows=len(xs),
        source_csvs=used_paths,
        padding_m=padding,
    )
    if effective_cache_path is not None:
        try:
            _write_step5d_tcp_cage_cache(effective_cache_path, cage, paths)
        except OSError:
            pass
    return cage


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
    if bridge_profile in {STEP5D_ABLATION_V27_STAGE_ID, STEP5D_ABLATION_V28_STAGE_ID}:
        return (
            STEP5D_V27_ENTRY_FILTERED_NORMAL_LOAD_MIN_N,
            STEP5D_V27_ENTRY_FILTERED_NORMAL_LOAD_MAX_N,
            STEP5D_V27_ENTRY_FORCE_NORM_MAX_N,
        )
    if bridge_profile == STEP5D_ABLATION_V26_STAGE_ID:
        return (
            STEP5D_V26_ENTRY_FILTERED_NORMAL_LOAD_MIN_N,
            STEP5D_V26_ENTRY_FILTERED_NORMAL_LOAD_MAX_N,
            STEP5D_V21_ENTRY_FORCE_NORM_MAX_N,
        )
    if bridge_profile == STEP5D_ABLATION_V25_STAGE_ID:
        return (
            STEP5D_V25_ENTRY_FILTERED_NORMAL_LOAD_MIN_N,
            STEP5D_V25_ENTRY_FILTERED_NORMAL_LOAD_MAX_N,
            STEP5D_V21_ENTRY_FORCE_NORM_MAX_N,
        )
    if bridge_profile in {
        STEP5D_LIVEPREP_V21_STAGE_ID,
        STEP5D_LIVEPREP_V22_STAGE_ID,
        STEP5D_LIVEPREP_V23_STAGE_ID,
        STEP5D_LIVEPREP_V24_STAGE_ID,
    }:
        return (
            STEP5D_V21_ENTRY_FILTERED_NORMAL_LOAD_MIN_N,
            STEP5D_V21_ENTRY_FILTERED_NORMAL_LOAD_MAX_N,
            STEP5D_V21_ENTRY_FORCE_NORM_MAX_N,
        )
    if bridge_profile in {STEP5D_LIVEPREP_V19_STAGE_ID, STEP5D_LIVEPREP_V20_STAGE_ID}:
        return (
            STEP5D_V17_ENTRY_FILTERED_NORMAL_LOAD_MIN_N,
            STEP5D_V19_ENTRY_FILTERED_NORMAL_LOAD_MAX_N,
            STEP5D_V8_FORCE_NORM_MAX_N,
        )
    if bridge_profile in {STEP5D_LIVEPREP_V17_STAGE_ID, STEP5D_LIVEPREP_V18_STAGE_ID}:
        return (
            STEP5D_V17_ENTRY_FILTERED_NORMAL_LOAD_MIN_N,
            STEP5D_V17_ENTRY_FILTERED_NORMAL_LOAD_MAX_N,
            STEP5D_V8_FORCE_NORM_MAX_N,
        )
    if bridge_profile == STEP5D_LIVEPREP_V16_STAGE_ID:
        return (
            STEP5D_V16_ENTRY_NORMAL_LOAD_MIN_N,
            STEP5D_V16_ENTRY_NORMAL_LOAD_MAX_N,
            STEP5D_V8_FORCE_NORM_MAX_N,
        )
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
    cage_primary_low_load_reacquire: bool = False,
    active_reacquire_load_max_n: float = STEP5D_V18_ACTIVE_REACQUIRE_LOAD_MAX_N,
    no_contact_load_n: float = STEP5D_V18_NO_CONTACT_LOAD_N,
    **kwargs: Any,
) -> dict[str, float | str]:
    allow_high_contact_below_hard_force = bool(kwargs.pop("allow_high_contact_below_hard_force", True))
    defer_low_load_hold_timeout = bool(kwargs.pop("defer_low_load_hold_timeout", True))
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
        elif action in {"pass_solver", "active_reacquire_solver"}:
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
    if cage_primary_low_load_reacquire:
        normal_load_n = float(kwargs.get("normal_load_n", 0.0))
        actual_tcp_speed_m_s = float(kwargs.get("actual_tcp_speed_m_s", math.nan))
        predicted_tcp_speed_m_s = kwargs.get("predicted_tcp_speed_m_s")
        absolute_speed_stop_m_s = float(kwargs.get("absolute_speed_stop_m_s", STEP5D_V13_ABSOLUTE_SPEED_STOP_M_S))
        predicted_speed_finite = (
            predicted_tcp_speed_m_s is not None and math.isfinite(float(predicted_tcp_speed_m_s))
        )
        actual_speed_hard_stop = math.isfinite(actual_tcp_speed_m_s) and actual_tcp_speed_m_s > absolute_speed_stop_m_s
        predicted_speed_hard_stop = predicted_speed_finite and float(predicted_tcp_speed_m_s) > absolute_speed_stop_m_s
        if actual_speed_hard_stop or predicted_speed_hard_stop:
            return finalize({
                "state": "danger_stop",
                "action": "stop_zero_qdot",
                "reason": "cage_primary_tcp_speed_hard_stop",
                "hold_s": float(kwargs.get("prior_hold_s", 0.0)),
                "high_window_s": float(kwargs.get("prior_high_window_s", 0.0)),
                "actual_speed_violation_s": float(kwargs.get("prior_actual_speed_violation_s", 0.0)),
                "actual_speed_violation_count": int(kwargs.get("prior_actual_speed_violation_count", 0)),
            })
        if normal_load_n <= float(active_reacquire_load_max_n):
            return finalize({
                "state": "active_reacquire",
                "action": "active_reacquire_solver",
                "reason": (
                    "cage_primary_no_contact_active_reacquire"
                    if normal_load_n <= float(no_contact_load_n)
                    else "cage_primary_low_load_active_reacquire"
                ),
                "hold_s": 0.0,
                "high_window_s": 0.0,
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
    if (
        allow_high_contact_below_hard_force
        and base_result["reason"] in {"high_contact_window_dwell", "high_contact_window_dwell_stop"}
    ):
        return finalize({
            **base_result,
            "state": "valid_contact",
            "action": "pass_solver",
            "reason": "ok_high_contact_below_hard_force",
            "high_window_s": 0.0,
        })
    if base_result["reason"] == "low_load_hold_timeout" and defer_low_load_hold_timeout:
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
    kwargs.setdefault("prior_high_window_s", 0.0)
    kwargs.setdefault("prior_actual_speed_violation_s", 0.0)
    kwargs.setdefault("prior_actual_speed_violation_count", 0)
    return step5d_v15_permissive_recovery_guard(require_braking_margin=True, **kwargs)


def step5d_v18_guard(**kwargs: Any) -> dict[str, float | str]:
    kwargs.setdefault("prior_high_window_s", 0.0)
    kwargs.setdefault("prior_actual_speed_violation_s", 0.0)
    kwargs.setdefault("prior_actual_speed_violation_count", 0)
    kwargs.setdefault("force_norm_hard_stop_n", STEP5D_V18_SENSOR_FORCE_HARD_STOP_N)
    kwargs.setdefault("cage_primary_low_load_reacquire", True)
    kwargs.setdefault("active_reacquire_load_max_n", STEP5D_V18_ACTIVE_REACQUIRE_LOAD_MAX_N)
    kwargs.setdefault("no_contact_load_n", STEP5D_V18_NO_CONTACT_LOAD_N)
    kwargs.setdefault("valid_contact_max_n", STEP5D_V18_VALID_CONTACT_MAX_N)
    kwargs.setdefault("allow_high_contact_below_hard_force", True)
    return step5d_v15_permissive_recovery_guard(require_braking_margin=True, **kwargs)


def step5d_v25_speedl_cartesian_guard(
    *,
    normal_load_n: float,
    force_norm_n: float,
    actual_tcp_speed_m_s: float,
    braking_margin_m: float | None,
    prior_soft_low_load_s: float,
    prior_hard_low_load_s: float,
    prior_actual_speed_violation_s: float,
    prior_actual_speed_violation_count: int,
    dt_s: float,
    force_norm_hard_stop_n: float = STEP5D_V24_SENSOR_FORCE_HARD_STOP_N,
    hard_low_load_n: float = STEP5D_V25_HARD_LOW_LOAD_N,
    hard_low_load_timeout_s: float = STEP5D_V25_HARD_LOW_LOAD_TIMEOUT_S,
    soft_low_load_n: float = STEP5D_V25_SOFT_LOW_LOAD_N,
    soft_low_load_timeout_s: float = STEP5D_V25_SOFT_LOW_LOAD_TIMEOUT_S,
    low_load_speed_stop_m_s: float = STEP5D_V13_LOW_LOAD_SPEED_STOP_M_S,
    absolute_speed_stop_m_s: float = STEP5D_V13_ABSOLUTE_SPEED_STOP_M_S,
    dt_max_s: float = STEP5D_V12_GUARD_DT_MAX_S,
) -> dict[str, float | str]:
    values = [
        normal_load_n,
        force_norm_n,
        actual_tcp_speed_m_s,
        prior_soft_low_load_s,
        prior_hard_low_load_s,
        prior_actual_speed_violation_s,
        dt_s,
        force_norm_hard_stop_n,
        hard_low_load_n,
        hard_low_load_timeout_s,
        soft_low_load_n,
        soft_low_load_timeout_s,
        low_load_speed_stop_m_s,
        absolute_speed_stop_m_s,
        dt_max_s,
    ]
    if any(not math.isfinite(float(value)) for value in values):
        return {
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "nonfinite_v25_speedl_guard_input",
            "hold_s": float(prior_soft_low_load_s),
            "hard_low_load_s": float(prior_hard_low_load_s),
            "high_window_s": 0.0,
            "actual_speed_violation_s": float(prior_actual_speed_violation_s),
            "actual_speed_violation_count": int(prior_actual_speed_violation_count),
        }
    if braking_margin_m is None or not math.isfinite(float(braking_margin_m)):
        return {
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "missing_or_nonfinite_tcp_cage_braking_margin",
            "hold_s": float(prior_soft_low_load_s),
            "hard_low_load_s": float(prior_hard_low_load_s),
            "high_window_s": 0.0,
            "actual_speed_violation_s": float(prior_actual_speed_violation_s),
            "actual_speed_violation_count": int(prior_actual_speed_violation_count),
        }
    if float(braking_margin_m) <= 0.0:
        return {
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "tcp_cage_braking_margin_exhausted",
            "hold_s": float(prior_soft_low_load_s),
            "hard_low_load_s": float(prior_hard_low_load_s),
            "high_window_s": 0.0,
            "actual_speed_violation_s": float(prior_actual_speed_violation_s),
            "actual_speed_violation_count": int(prior_actual_speed_violation_count),
        }
    safe_dt_s = min(max(0.0, float(dt_s)), float(dt_max_s))
    hard_low_load_s = max(0.0, float(prior_hard_low_load_s)) + safe_dt_s if normal_load_n < hard_low_load_n else 0.0
    soft_low_load_s = max(0.0, float(prior_soft_low_load_s)) + safe_dt_s if normal_load_n < soft_low_load_n else 0.0
    actual_speed_hard_stop = float(actual_tcp_speed_m_s) > float(absolute_speed_stop_m_s) or (
        float(normal_load_n) < float(soft_low_load_n)
        and float(actual_tcp_speed_m_s) > float(low_load_speed_stop_m_s)
    )
    actual_speed_violation_s = (
        max(0.0, float(prior_actual_speed_violation_s)) + safe_dt_s if actual_speed_hard_stop else 0.0
    )
    actual_speed_violation_count = max(0, int(prior_actual_speed_violation_count)) + 1 if actual_speed_hard_stop else 0
    common = {
        "hold_s": soft_low_load_s,
        "hard_low_load_s": hard_low_load_s,
        "high_window_s": 0.0,
        "actual_speed_violation_s": actual_speed_violation_s,
        "actual_speed_violation_count": actual_speed_violation_count,
    }
    if float(force_norm_n) >= float(force_norm_hard_stop_n):
        return {"state": "danger_stop", "action": "stop_zero_qdot", "reason": "force_norm_hard_stop", **common}
    if actual_speed_hard_stop:
        return {
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "v25_speedl_actual_tcp_speed_hard_stop",
            **common,
        }
    if hard_low_load_s >= float(hard_low_load_timeout_s):
        return {
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "v25_speedl_hard_low_load_timeout",
            **common,
        }
    if soft_low_load_s >= float(soft_low_load_timeout_s):
        return {
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "v25_speedl_soft_low_load_timeout",
            **common,
        }
    if soft_low_load_s > 0.0:
        return {
            "state": "active_reacquire",
            "action": "pass_solver",
            "reason": "v25_speedl_low_load_repress_window",
            **common,
        }
    return {"state": "valid_contact", "action": "pass_solver", "reason": "ok", **common}


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


def limit_step5d_no_contact_p0_xdot_components(
    xdot_c: Any,
    *,
    rotation_base_from_tcp: Any | None = None,
    max_xy_m_s: float = STEP5D_NO_CONTACT_P0_LINEAR_XY_COMPONENT_LIMIT_M_S,
    max_z_m_s: float = STEP5D_NO_CONTACT_P0_LINEAR_Z_COMPONENT_LIMIT_M_S,
    max_angular_rad_s: float = STEP5D_NO_CONTACT_P0_ANGULAR_COMPONENT_LIMIT_RAD_S,
    return_diagnostics: bool = False,
) -> tuple[np.ndarray, bool] | tuple[np.ndarray, bool, dict[str, Any]]:
    if rotation_base_from_tcp is None:
        raise ValueError("P0 xdot limiter requires rotation_base_from_tcp")
    return shared_limit_p0_xdot_components(
        xdot_c,
        rotation_base_from_tcp=rotation_base_from_tcp,
        max_xy_m_s=max_xy_m_s,
        max_z_m_s=max_z_m_s,
        max_angular_rad_s=max_angular_rad_s,
        return_diagnostics=return_diagnostics,
    )


def smoothstep01(value: float) -> float:
    x = min(max(float(value), 0.0), 1.0)
    return x * x * (3.0 - 2.0 * x)


def step5d_no_contact_p0_low_force_posture_policy(
    *,
    normal_load_n: float,
    base_ko: float,
    low_load_n: float = STEP5D_NO_CONTACT_P0_LOW_FORCE_POSTURE_LOW_LOAD_N,
    high_load_n: float = STEP5D_NO_CONTACT_P0_LOW_FORCE_POSTURE_HIGH_LOAD_N,
    low_ko: float = STEP5D_NO_CONTACT_P0_LOW_FORCE_POSTURE_KO,
    policy: str = STEP5D_NO_CONTACT_P0_LOW_FORCE_POSTURE_POLICY,
) -> dict[str, Any]:
    return shared_low_force_posture_policy(
        normal_load_n=normal_load_n,
        base_ko=base_ko,
        low_load_n=low_load_n,
        high_load_n=high_load_n,
        low_ko=low_ko,
        policy=policy,
    )


def step5d_no_contact_p0_press_only_outer_output(
    *,
    reaction_normal_b: Sequence[float],
    force_error_n: float,
    press_speed_m_s: float = STEP5D_NO_CONTACT_P0_PRESS_ONLY_SPEED_M_S,
) -> SimpleNamespace:
    return shared_press_only_outer_output(
        reaction_normal_b=reaction_normal_b,
        force_error_n=force_error_n,
        press_speed_m_s=press_speed_m_s,
    )


def scale_step5d_xdot_for_joint_feasibility(
    xdot_c: Any,
    jacobian: Any,
    *,
    qdot_cap_rad_s: float,
    safety: float = 0.9,
) -> tuple[np.ndarray, dict[str, Any]]:
    return shared_scale_xdot_for_joint_feasibility(
        xdot_c,
        jacobian,
        qdot_cap_rad_s=qdot_cap_rad_s,
        safety=safety,
    )


def limit_step5d_no_contact_p0_qdot_command(
    qdot: Sequence[float],
    jacobian: Any,
    *,
    qdot_cap_rad_s: float = STEP5D_NO_CONTACT_P0_QDOT_CAP_RAD_S,
    max_xy_m_s: float = STEP5D_NO_CONTACT_P0_LINEAR_XY_COMPONENT_LIMIT_M_S,
    max_z_m_s: float = STEP5D_NO_CONTACT_P0_LINEAR_Z_COMPONENT_LIMIT_M_S,
    max_angular_rad_s: float = STEP5D_NO_CONTACT_P0_ANGULAR_COMPONENT_LIMIT_RAD_S,
) -> tuple[np.ndarray, dict[str, Any]]:
    qdot_values = np.asarray(qdot, dtype=float)
    J = np.asarray(jacobian, dtype=float)
    if qdot_values.shape != (6,) or not np.all(np.isfinite(qdot_values)):
        raise ValueError("P0 qdot command must be a finite 6-vector")
    if J.ndim != 2 or J.shape[1] != 6 or J.shape[0] < 6 or not np.all(np.isfinite(J)):
        raise ValueError("P0 Jacobian must be finite with six TCP twist rows")
    qdot_cap = float(qdot_cap_rad_s)
    caps = np.asarray(
        [
            float(max_xy_m_s),
            float(max_xy_m_s),
            float(max_z_m_s),
            float(max_angular_rad_s),
            float(max_angular_rad_s),
            float(max_angular_rad_s),
        ],
        dtype=float,
    )
    if not math.isfinite(qdot_cap) or qdot_cap <= 0.0:
        raise ValueError("P0 qdot cap must be finite and positive")
    if np.any(~np.isfinite(caps)) or np.any(caps <= 0.0):
        raise ValueError("P0 component velocity caps must be finite and positive")

    joint_limited = np.clip(qdot_values, -qdot_cap, qdot_cap)
    raw_twist = J @ joint_limited
    component_scale = 1.0
    for value, cap in zip(raw_twist[:6], caps):
        abs_value = abs(float(value))
        if abs_value > float(cap) and abs_value > 0.0:
            component_scale = min(component_scale, float(cap) / abs_value)
    limited = joint_limited * component_scale
    limited_twist = J @ limited
    return limited, {
        "qdot_clip_active": bool(np.any(np.abs(joint_limited - qdot_values) > 1e-12)),
        "component_clamp_active": bool(component_scale < 1.0 - 1e-12),
        "component_clamp_scale": component_scale,
        "raw_predicted_twist": raw_twist,
        "limited_predicted_twist": limited_twist,
        "max_xy_m_s": float(max_xy_m_s),
        "max_z_m_s": float(max_z_m_s),
        "max_angular_rad_s": float(max_angular_rad_s),
        "qdot_cap_rad_s": qdot_cap,
    }


def limit_step5d_qdot_slew(
    qdot: Any,
    previous_qdot: np.ndarray | None,
    *,
    dt_s: float,
    max_slew_rad_s2: float = STEP5D_V12_QDOT_SLEW_RAD_S2,
    dt_max_s: float = STEP5D_V12_GUARD_DT_MAX_S,
    preserve_delta_direction: bool = False,
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
    delta = qdot_values - previous
    if preserve_delta_direction:
        max_delta = float(np.max(np.abs(delta)))
        scale = 1.0 if max_delta <= delta_limit or max_delta <= 0.0 else delta_limit / max_delta
        limited = previous + scale * delta
    else:
        limited = previous + np.clip(delta, -delta_limit, delta_limit)
    return limited, bool(np.any(np.abs(limited - qdot_values) > 1e-12))


def limit_step5d_predicted_tcp_speed(
    qdot: Sequence[float],
    jacobian: np.ndarray,
    *,
    max_tcp_speed_m_s: float,
) -> tuple[np.ndarray, float, bool]:
    qdot_values = np.asarray(qdot, dtype=float)
    jacobian_values = np.asarray(jacobian, dtype=float)
    if qdot_values.shape != (6,) or not np.all(np.isfinite(qdot_values)):
        raise ValueError("Step5d qdot must be a finite 6-vector")
    if jacobian_values.shape[1] != 6 or jacobian_values.shape[0] < 3 or not np.all(np.isfinite(jacobian_values)):
        raise ValueError("Step5d Jacobian must be finite with at least three TCP rows")
    if not math.isfinite(float(max_tcp_speed_m_s)) or max_tcp_speed_m_s <= 0.0:
        raise ValueError("Step5d predicted TCP speed cap must be positive")
    predicted_twist = jacobian_values @ qdot_values
    predicted_speed_m_s = float(np.linalg.norm(predicted_twist[:3]))
    if predicted_speed_m_s <= float(max_tcp_speed_m_s) or predicted_speed_m_s <= 0.0:
        return qdot_values, predicted_speed_m_s, False
    scale = float(max_tcp_speed_m_s) / predicted_speed_m_s
    return qdot_values * scale, predicted_speed_m_s, True


def step5d_no_contact_p0_qdot_acceptance_gate(
    *,
    qdot: Sequence[float],
    jacobian: Any,
    outer_xdot_limited: Sequence[float],
    reaction_normal_b: Sequence[float],
    residual_norm: float,
    active_bounds_count: int | float,
    max_tcp_speed_m_s: float,
    max_normal_tracking_error_m_s: float,
    max_residual_norm: float,
    max_base_upward_m_s: float = 1e-6,
) -> dict[str, Any]:
    qdot_values = np.asarray(qdot, dtype=float)
    J = np.asarray(jacobian, dtype=float)
    outer = np.asarray(outer_xdot_limited, dtype=float)
    reaction_values = np.asarray(reaction_normal_b, dtype=float)
    zero_qdot = np.zeros(6, dtype=float)

    def reject(reason: str, *, keep_qdot: bool = False, **extra: Any) -> dict[str, Any]:
        return {
            "accepted": False,
            "reason": reason,
            "qdot": qdot_values if keep_qdot else zero_qdot,
            "candidate_qdot": qdot_values,
            "motion_action": "evidence_rejected_continue" if keep_qdot else "invalid_zero_qdot",
            **extra,
        }

    if (
        qdot_values.shape != (6,)
        or J.ndim != 2
        or J.shape[1] != 6
        or J.shape[0] < 3
        or outer.shape != (6,)
        or reaction_values.shape != (3,)
        or not np.all(np.isfinite(qdot_values))
        or not np.all(np.isfinite(J))
        or not np.all(np.isfinite(outer))
        or not np.all(np.isfinite(reaction_values))
    ):
        return reject(
            "nonfinite_or_bad_shape",
            predicted_tcp_speed_m_s=math.nan,
            jqdot_approach_normal_m_s=math.nan,
            outer_approach_normal_m_s=math.nan,
            normal_tracking_error_m_s=math.nan,
        )
    reaction = normalize3(tuple(float(value) for value in reaction_values.tolist()))
    if not all(math.isfinite(float(value)) for value in reaction):
        return reject(
            "nonfinite_or_bad_shape",
            predicted_tcp_speed_m_s=math.nan,
            jqdot_approach_normal_m_s=math.nan,
            outer_approach_normal_m_s=math.nan,
            normal_tracking_error_m_s=math.nan,
        )
    approach = np.asarray((-reaction[0], -reaction[1], -reaction[2]), dtype=float)
    predicted_twist = J @ qdot_values
    predicted_speed = float(np.linalg.norm(predicted_twist[:3]))
    jqdot_approach = float(np.dot(predicted_twist[:3], approach))
    outer_approach = float(np.dot(outer[:3], approach))
    normal_tracking_error = abs(jqdot_approach - outer_approach)
    common = {
        "predicted_tcp_speed_m_s": predicted_speed,
        "predicted_tcp_speed_over_legacy_cap": (
            bool(predicted_speed > float(max_tcp_speed_m_s))
            if math.isfinite(float(max_tcp_speed_m_s)) and float(max_tcp_speed_m_s) > 0.0
            else False
        ),
        "jqdot_approach_normal_m_s": jqdot_approach,
        "outer_approach_normal_m_s": outer_approach,
        "normal_tracking_error_m_s": normal_tracking_error,
        "predicted_base_vz_m_s": float(predicted_twist[2]),
    }
    if not math.isfinite(float(residual_norm)):
        return reject("nonfinite_residual_norm", **common)
    if not math.isfinite(float(active_bounds_count)):
        return reject("nonfinite_active_bounds_count", **common)
    if outer_approach <= 0.0:
        return reject("outer_approach_not_pressing", keep_qdot=True, **common)
    if float(predicted_twist[2]) > float(max_base_upward_m_s):
        return reject("base_upward_escape", **common)
    if outer_approach > 0.0 and jqdot_approach <= 0.0:
        return reject("approach_normal_unload_mismatch", keep_qdot=True, **common)
    if normal_tracking_error > float(max_normal_tracking_error_m_s):
        return reject("approach_normal_tracking_error", keep_qdot=True, **common)
    if float(residual_norm) > float(max_residual_norm):
        return reject("constraint_residual_norm_exceeds_p0_limit", keep_qdot=True, **common)
    if float(active_bounds_count) > 0.0:
        return reject("active_bounds_exceeds_p0_limit", keep_qdot=True, **common)
    return {
        "accepted": True,
        "reason": "ok",
        "qdot": qdot_values,
        "candidate_qdot": qdot_values,
        "motion_action": "execute_qdot",
        **common,
    }


def step5d_v29_rnn_qdot_acceptance_gate(
    *,
    qdot: Sequence[float],
    jacobian: Any,
    outer_xdot_limited: Sequence[float],
    reaction_normal_b: Sequence[float],
    residual_norm: float,
    active_bounds_count: int | float,
    max_normal_tracking_error_m_s: float = 5e-4,
    max_residual_norm: float = 1e-3,
) -> dict[str, Any]:
    qdot_values = np.asarray(qdot, dtype=float)
    J = np.asarray(jacobian, dtype=float)
    outer = np.asarray(outer_xdot_limited, dtype=float)
    reaction_values = np.asarray(reaction_normal_b, dtype=float)
    zero_qdot = np.zeros(6, dtype=float)

    def reject(reason: str, *, safe_hold: bool = True, **extra: Any) -> dict[str, Any]:
        return {
            "accepted": False,
            "reason": reason,
            "qdot": zero_qdot,
            "candidate_qdot": qdot_values,
            "motion_action": "evidence_rejected_safe_hold" if safe_hold else "invalid_zero_qdot",
            **extra,
        }

    if (
        qdot_values.shape != (6,)
        or J.ndim != 2
        or J.shape[1] != 6
        or J.shape[0] < 3
        or outer.shape != (6,)
        or reaction_values.shape != (3,)
        or not np.all(np.isfinite(qdot_values))
        or not np.all(np.isfinite(J))
        or not np.all(np.isfinite(outer))
        or not np.all(np.isfinite(reaction_values))
    ):
        return reject(
            "nonfinite_or_bad_shape",
            safe_hold=False,
            predicted_tcp_speed_m_s=math.nan,
            jqdot_approach_normal_m_s=math.nan,
            outer_approach_normal_m_s=math.nan,
            normal_tracking_error_m_s=math.nan,
        )
    reaction = normalize3(tuple(float(value) for value in reaction_values.tolist()))
    if not all(math.isfinite(float(value)) for value in reaction):
        return reject(
            "nonfinite_or_bad_shape",
            safe_hold=False,
            predicted_tcp_speed_m_s=math.nan,
            jqdot_approach_normal_m_s=math.nan,
            outer_approach_normal_m_s=math.nan,
            normal_tracking_error_m_s=math.nan,
        )
    approach = np.asarray((-reaction[0], -reaction[1], -reaction[2]), dtype=float)
    predicted_twist = J @ qdot_values
    predicted_speed = float(np.linalg.norm(predicted_twist[:3]))
    jqdot_approach = float(np.dot(predicted_twist[:3], approach))
    outer_approach = float(np.dot(outer[:3], approach))
    normal_tracking_error = abs(jqdot_approach - outer_approach)
    common = {
        "predicted_tcp_speed_m_s": predicted_speed,
        "jqdot_approach_normal_m_s": jqdot_approach,
        "outer_approach_normal_m_s": outer_approach,
        "normal_tracking_error_m_s": normal_tracking_error,
        "predicted_base_vz_m_s": float(predicted_twist[2]),
    }
    if not math.isfinite(float(residual_norm)):
        return reject("nonfinite_residual_norm", safe_hold=False, **common)
    if not math.isfinite(float(active_bounds_count)):
        return reject("nonfinite_active_bounds_count", safe_hold=False, **common)
    if outer_approach <= 0.0:
        return reject("outer_approach_not_pressing", **common)
    if outer_approach > 0.0 and jqdot_approach <= 0.0:
        return reject("approach_normal_unload_mismatch", **common)
    if normal_tracking_error > float(max_normal_tracking_error_m_s):
        return reject("approach_normal_tracking_error", **common)
    if float(residual_norm) > float(max_residual_norm):
        return reject("constraint_residual_norm_exceeds_v29_limit", **common)
    if float(active_bounds_count) > 0.0:
        return reject("active_bounds_exceeds_v29_limit", **common)
    return {
        "accepted": True,
        "reason": "ok",
        "qdot": qdot_values,
        "candidate_qdot": qdot_values,
        "motion_action": "execute_qdot",
        **common,
    }


def step5d_p0_frame_diagnostic_values(diagnostics: dict[str, Any]) -> dict[str, Any]:
    limited_tcp = np.asarray(diagnostics.get("limited_tcp", np.full(6, math.nan, dtype=float)), dtype=float)
    limited_base = np.asarray(diagnostics.get("limited_base", np.full(6, math.nan, dtype=float)), dtype=float)
    if limited_tcp.shape != (6,):
        limited_tcp = np.full(6, math.nan, dtype=float)
    if limited_base.shape != (6,):
        limited_base = np.full(6, math.nan, dtype=float)
    return {
        "_step5d_p0_frame_transform_valid": 1.0 if bool(diagnostics.get("valid", False)) else 0.0,
        "_step5d_p0_frame_transform_mode": str(diagnostics.get("mode", "")),
        "_step5d_p0_frame_transform_reason": str(diagnostics.get("reason", "")),
        "_step5d_p0_limited_tcp_vx_m_s": float(limited_tcp[0]),
        "_step5d_p0_limited_tcp_vy_m_s": float(limited_tcp[1]),
        "_step5d_p0_limited_tcp_vz_m_s": float(limited_tcp[2]),
        "_step5d_p0_limited_tcp_wx_rad_s": float(limited_tcp[3]),
        "_step5d_p0_limited_tcp_wy_rad_s": float(limited_tcp[4]),
        "_step5d_p0_limited_tcp_wz_rad_s": float(limited_tcp[5]),
        "_step5d_p0_limited_base_vx_m_s": float(limited_base[0]),
        "_step5d_p0_limited_base_vy_m_s": float(limited_base[1]),
        "_step5d_p0_limited_base_vz_m_s": float(limited_base[2]),
        "_step5d_p0_tcp_press_speed_m_s": float(diagnostics.get("tcp_press_speed_m_s", math.nan)),
    }


def step5d_dls_qdot_oracle(
    jacobian: Any,
    xdot_c: Sequence[float],
    omega_minus: Sequence[float],
    omega_plus: Sequence[float],
    *,
    damping: float = 1e-4,
) -> tuple[float, float, float, float, float, float]:
    J = np.asarray(jacobian, dtype=float)
    xdot = np.asarray(xdot_c, dtype=float)
    lower = np.asarray(omega_minus, dtype=float)
    upper = np.asarray(omega_plus, dtype=float)
    if J.shape != (6, 6) or xdot.shape != (6,) or lower.shape != (6,) or upper.shape != (6,):
        raise ValueError("Step5d DLS oracle expects 6x6 J, 6-vector xdot, and 6-vector bounds")
    if not (np.all(np.isfinite(J)) and np.all(np.isfinite(xdot)) and np.all(np.isfinite(lower)) and np.all(np.isfinite(upper))):
        raise ValueError("Step5d DLS oracle inputs must be finite")
    if not math.isfinite(float(damping)) or damping <= 0.0:
        raise ValueError("Step5d DLS damping must be positive")
    lhs = J @ J.T + (float(damping) ** 2) * np.eye(6)
    qdot = J.T @ np.linalg.solve(lhs, xdot)
    qdot = np.clip(qdot, lower, upper)
    return tuple(float(value) for value in qdot.tolist())


def step5d_v30_contract_pipeline(
    observation: Step5dObservation,
    raw_candidate: ControlCandidate,
    *,
    previous_qdot: Sequence[float] | None,
    safety_envelope: SafetyEnvelope,
    deferred_diagnostics: DeferredV30Diagnostics,
) -> tuple[ControlCandidate, Any, Any, Any]:
    """Exact v30 candidate→slew→DLS-shadow→safety→register/log seam.

    Both the inactive bridge profile and the source-bound timing harness call
    this function.  DLS evidence has no command conversion; only the strict-RNN
    candidate reaches ``SafetyEnvelope`` and ``RegisterCommand``.
    """

    shared = shared_step5d_v30_contract_pipeline(
        observation,
        raw_candidate,
        previous_qdot=(
            tuple(float(value) for value in previous_qdot)
            if previous_qdot is not None
            else None
        ),
        safety_envelope=safety_envelope,
        deferred_diagnostics=deferred_diagnostics,
        max_slew_rad_s2=STEP5D_V12_QDOT_SLEW_RAD_S2,
        dt_max_s=STEP5D_V12_GUARD_DT_MAX_S,
    )
    return (
        shared.candidate,
        shared.dls_shadow,
        shared.decision,
        shared.register_command,
    )


def step5d_v30_bridge_control_step(
    observation: Step5dObservation,
    policy: StrictRnnControlPolicy,
    *,
    previous_qdot: Sequence[float] | None,
    safety_envelope: SafetyEnvelope,
    deferred_diagnostics: DeferredV30Diagnostics,
    prepare_policy: Callable[[Step5dObservation], None] | None = None,
) -> Any:
    """Run reference governance, warm-start, policy, and contract fail-closed.

    This is the future v30 bridge's single production seam.  Reference
    governance and strict-RNN exceptions are converted into a layout-524
    exact-zero stop ``RegisterCommand`` before the transport loop can tear
    down.
    """

    return shared_step5d_v30_control_step(
        observation,
        policy,
        previous_qdot=(
            tuple(float(value) for value in previous_qdot)
            if previous_qdot is not None
            else None
        ),
        safety_envelope=safety_envelope,
        deferred_diagnostics=deferred_diagnostics,
        prepare_policy=prepare_policy,
        max_slew_rad_s2=STEP5D_V12_QDOT_SLEW_RAD_S2,
        dt_max_s=STEP5D_V12_GUARD_DT_MAX_S,
    )


def step5d_qdot_diagnostic_values(
    *,
    jacobian: Any,
    raw_qdot: Sequence[float] | None,
    post_slew_qdot: Sequence[float] | None,
    final_qdot: Sequence[float] | None,
    outer_xdot_limited: Sequence[float] | None,
    reaction_normal_b: Sequence[float],
    lambda_state: Sequence[float] | None = None,
    active_bounds_mask: Sequence[bool] | None = None,
    intervention_reason: str = "none",
    feasibility_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    J = np.asarray(jacobian, dtype=float)
    reaction = normalize3((float(reaction_normal_b[0]), float(reaction_normal_b[1]), float(reaction_normal_b[2])))
    approach = np.asarray((-reaction[0], -reaction[1], -reaction[2]), dtype=float)
    diagnostics: dict[str, Any] = {
        "_step5d_outer_xdot_limited_approach_normal_m_s": math.nan,
        "_step5d_jqdot_raw_approach_normal_m_s": math.nan,
        "_step5d_jqdot_post_slew_approach_normal_m_s": math.nan,
        "_step5d_jqdot_cmd_approach_normal_m_s": math.nan,
        "_step5d_lambda_norm": math.nan,
        "_step5d_intervention_reason": intervention_reason,
        "_step5d_qdot_cap_rad_s": math.nan,
        "_step5d_jinv_xdot_inf_rad_s": math.nan,
        "_step5d_jinv_xdot_inf_over_qdot_cap": math.nan,
        "_step5d_jinv_xdot_solve_status": "",
        "_step5d_xdot_feasibility_scale": math.nan,
        "_step5d_xdot_norm_pre_feasibility_scale": math.nan,
        "_step5d_xdot_norm_post_feasibility_scale": math.nan,
        "_step5d_xdot_feasibility_scale_active": math.nan,
        "_step5d_oracle_residual_norm": math.nan,
        "_step5d_oracle_qdot_max_abs_rad_s": math.nan,
        "_step5d_cmd_residual_norm": math.nan,
        "_step5d_rnn_vs_oracle_qdot_norm": math.nan,
    }
    for idx in range(6):
        diagnostics[f"_step5d_rnn_raw_qd{idx}_rad_s"] = math.nan
        diagnostics[f"_step5d_post_slew_qd{idx}_rad_s"] = math.nan
        diagnostics[f"_step5d_active_bound_qd{idx}"] = 0.0
        diagnostics[f"_step5d_lambda_state_{idx}"] = math.nan

    def qdot_array(values: Sequence[float] | None) -> np.ndarray | None:
        if values is None:
            return None
        arr = np.asarray(values, dtype=float)
        if arr.shape != (6,):
            return None
        return arr

    raw_arr = qdot_array(raw_qdot)
    post_slew_arr = qdot_array(post_slew_qdot)
    final_arr = qdot_array(final_qdot)
    outer_arr = None if outer_xdot_limited is None else np.asarray(outer_xdot_limited, dtype=float)
    if outer_arr is not None and outer_arr.shape == (6,) and np.all(np.isfinite(outer_arr)):
        diagnostics["_step5d_outer_xdot_limited_approach_normal_m_s"] = float(np.dot(outer_arr[:3], approach))
        if J.shape == (6, 6) and np.all(np.isfinite(J)):
            try:
                oracle_qdot = np.linalg.solve(J, outer_arr)
            except np.linalg.LinAlgError:
                oracle_qdot = None
            if oracle_qdot is not None and oracle_qdot.shape == (6,) and np.all(np.isfinite(oracle_qdot)):
                diagnostics["_step5d_oracle_residual_norm"] = float(np.linalg.norm(J @ oracle_qdot - outer_arr))
                diagnostics["_step5d_oracle_qdot_max_abs_rad_s"] = float(np.max(np.abs(oracle_qdot)))
                if raw_arr is not None:
                    diagnostics["_step5d_rnn_vs_oracle_qdot_norm"] = float(np.linalg.norm(raw_arr - oracle_qdot))
    if raw_arr is not None:
        for idx, value in enumerate(raw_arr):
            diagnostics[f"_step5d_rnn_raw_qd{idx}_rad_s"] = float(value)
        diagnostics["_step5d_jqdot_raw_approach_normal_m_s"] = float(np.dot((J @ raw_arr)[:3], approach))
    if post_slew_arr is not None:
        for idx, value in enumerate(post_slew_arr):
            diagnostics[f"_step5d_post_slew_qd{idx}_rad_s"] = float(value)
        diagnostics["_step5d_jqdot_post_slew_approach_normal_m_s"] = float(np.dot((J @ post_slew_arr)[:3], approach))
    if final_arr is not None:
        diagnostics["_step5d_jqdot_cmd_approach_normal_m_s"] = float(np.dot((J @ final_arr)[:3], approach))
        if (
            outer_arr is not None
            and outer_arr.shape == (6,)
            and np.all(np.isfinite(outer_arr))
            and J.shape == (6, 6)
            and np.all(np.isfinite(J))
        ):
            diagnostics["_step5d_cmd_residual_norm"] = float(np.linalg.norm(J @ final_arr - outer_arr))
    if lambda_state is not None:
        lam = np.asarray(lambda_state, dtype=float)
        if lam.size > 0 and np.all(np.isfinite(lam)):
            diagnostics["_step5d_lambda_norm"] = float(np.linalg.norm(lam))
            for idx, value in enumerate(lam.reshape(-1)[:6]):
                diagnostics[f"_step5d_lambda_state_{idx}"] = float(value)
    if active_bounds_mask is not None:
        for idx, active in enumerate(list(active_bounds_mask)[:6]):
            diagnostics[f"_step5d_active_bound_qd{idx}"] = 1.0 if bool(active) else 0.0
    if feasibility_diagnostics:
        key_map = {
            "qdot_cap_rad_s": "_step5d_qdot_cap_rad_s",
            "jinv_xdot_inf_rad_s": "_step5d_jinv_xdot_inf_rad_s",
            "jinv_xdot_inf_over_qdot_cap": "_step5d_jinv_xdot_inf_over_qdot_cap",
            "jinv_xdot_solve_status": "_step5d_jinv_xdot_solve_status",
            "xdot_feasibility_scale": "_step5d_xdot_feasibility_scale",
            "xdot_norm_pre_feasibility_scale": "_step5d_xdot_norm_pre_feasibility_scale",
            "xdot_norm_post_feasibility_scale": "_step5d_xdot_norm_post_feasibility_scale",
            "xdot_feasibility_scale_active": "_step5d_xdot_feasibility_scale_active",
        }
        for source, target in key_map.items():
            if source in feasibility_diagnostics:
                value = feasibility_diagnostics[source]
                diagnostics[target] = 1.0 if isinstance(value, bool) and value else 0.0 if isinstance(value, bool) else value
    return diagnostics


def step5d_post_rnn_normal_direction_guard(
    *,
    qdot: Sequence[float],
    jacobian: Any,
    reaction_normal_b: Sequence[float],
    actual_tcp_speed_b: Sequence[float],
    normal_load_n: float,
    force_norm_n: float,
    previous_normal_load_n: float | None,
    prior_dwell_s: float,
    dt_s: float,
    hold_load_n: float = STEP5D_V23_NORMAL_GUARD_HOLD_LOAD_N,
    directional_stop_load_n: float = STEP5D_V23_NORMAL_GUARD_DIRECTIONAL_STOP_LOAD_N,
    hard_stop_load_n: float = STEP5D_V23_NORMAL_GUARD_HARD_STOP_LOAD_N,
    hard_stop_force_norm_n: float = STEP5D_V23_NORMAL_GUARD_HARD_STOP_FORCE_NORM_N,
    predicted_press_hold_m_s: float = STEP5D_V23_NORMAL_GUARD_PREDICTED_PRESS_HOLD_M_S,
    actual_press_hold_m_s: float = STEP5D_V23_NORMAL_GUARD_ACTUAL_PRESS_HOLD_M_S,
    load_rate_hold_n_s: float = STEP5D_V23_NORMAL_GUARD_LOAD_RATE_HOLD_N_S,
    directional_stop_dwell_s: float = STEP5D_V23_NORMAL_GUARD_DIRECTIONAL_STOP_DWELL_S,
) -> dict[str, Any]:
    safe_dt_s = min(max(0.0, float(dt_s)), STEP5D_V12_GUARD_DT_MAX_S)
    prior_dwell = max(0.0, float(prior_dwell_s))
    try:
        qdot_arr = np.asarray(qdot, dtype=float)
        J = np.asarray(jacobian, dtype=float)
        reaction = normalize3(
            (float(reaction_normal_b[0]), float(reaction_normal_b[1]), float(reaction_normal_b[2]))
        )
        approach = np.asarray((-reaction[0], -reaction[1], -reaction[2]), dtype=float)
        actual_speed = np.asarray(actual_tcp_speed_b[:3], dtype=float)
        if qdot_arr.shape != (6,) or J.shape[1] != 6 or actual_speed.shape != (3,):
            raise ValueError("shape")
        predicted_twist = J @ qdot_arr
        values_to_check = np.concatenate([qdot_arr, predicted_twist, approach, actual_speed])
        if not np.all(np.isfinite(values_to_check)):
            raise ValueError("nonfinite")
    except (TypeError, ValueError, IndexError):
        return {
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "post_rnn_normal_direction_nonfinite",
            "predicted_twist": np.full(6, math.nan),
            "predicted_press_speed_m_s": math.nan,
            "actual_press_speed_m_s": math.nan,
            "normal_load_rate_n_s": math.nan,
            "dwell_s": prior_dwell,
            "zeroed_qdot": True,
            "directional_press": True,
        }
    predicted_press_m_s = float(np.dot(predicted_twist[:3], approach))
    actual_press_m_s = float(np.dot(actual_speed, approach))
    load = float(normal_load_n)
    force_norm = float(force_norm_n)
    if previous_normal_load_n is not None and safe_dt_s > 0.0:
        load_rate_n_s = (load - float(previous_normal_load_n)) / safe_dt_s
    else:
        load_rate_n_s = math.nan
    directional_press = (
        predicted_press_m_s >= float(predicted_press_hold_m_s)
        or actual_press_m_s >= float(actual_press_hold_m_s)
        or (math.isfinite(load_rate_n_s) and load_rate_n_s >= float(load_rate_hold_n_s))
    )
    if load >= float(hard_stop_load_n) or force_norm >= float(hard_stop_force_norm_n):
        return {
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "post_rnn_high_load_hard_stop",
            "predicted_twist": predicted_twist,
            "predicted_press_speed_m_s": predicted_press_m_s,
            "actual_press_speed_m_s": actual_press_m_s,
            "normal_load_rate_n_s": load_rate_n_s,
            "dwell_s": 0.0,
            "zeroed_qdot": True,
            "directional_press": directional_press,
        }
    if load >= float(directional_stop_load_n) and directional_press:
        dwell_s = prior_dwell + safe_dt_s
        if dwell_s >= float(directional_stop_dwell_s):
            return {
                "state": "danger_stop",
                "action": "stop_zero_qdot",
                "reason": "post_rnn_high_load_press_dwell_stop",
                "predicted_twist": predicted_twist,
                "predicted_press_speed_m_s": predicted_press_m_s,
                "actual_press_speed_m_s": actual_press_m_s,
                "normal_load_rate_n_s": load_rate_n_s,
                "dwell_s": dwell_s,
                "zeroed_qdot": True,
                "directional_press": directional_press,
            }
        return {
            "state": "contact_uncertain_hold",
            "action": "hold_zero_qdot",
            "reason": "post_rnn_high_load_press_dwell_hold",
            "predicted_twist": predicted_twist,
            "predicted_press_speed_m_s": predicted_press_m_s,
            "actual_press_speed_m_s": actual_press_m_s,
            "normal_load_rate_n_s": load_rate_n_s,
            "dwell_s": dwell_s,
            "zeroed_qdot": True,
            "directional_press": directional_press,
        }
    if load >= float(hold_load_n) and directional_press:
        return {
            "state": "contact_uncertain_hold",
            "action": "hold_zero_qdot",
            "reason": "post_rnn_high_load_press_hold",
            "predicted_twist": predicted_twist,
            "predicted_press_speed_m_s": predicted_press_m_s,
            "actual_press_speed_m_s": actual_press_m_s,
            "normal_load_rate_n_s": load_rate_n_s,
            "dwell_s": 0.0,
            "zeroed_qdot": True,
            "directional_press": directional_press,
        }
    return {
        "state": "valid_contact",
        "action": "pass_solver",
        "reason": "post_rnn_high_load_unload_allowed" if load >= float(hold_load_n) else "post_rnn_normal_direction_ok",
        "predicted_twist": predicted_twist,
        "predicted_press_speed_m_s": predicted_press_m_s,
        "actual_press_speed_m_s": actual_press_m_s,
        "normal_load_rate_n_s": load_rate_n_s,
        "dwell_s": 0.0,
        "zeroed_qdot": False,
        "directional_press": directional_press,
    }


def step5d_post_rnn_tracking_guard(
    *,
    qdot: Sequence[float],
    jacobian: Any,
    outer_xdot_limited: Sequence[float] | None,
    reaction_normal_b: Sequence[float],
    actual_tcp_speed_b: Sequence[float],
    prior_dwell_s: float,
    dt_s: float,
    outer_press_min_m_s: float = STEP5D_V24_RNN_TRACKING_OUTER_PRESS_MIN_M_S,
    unload_min_m_s: float = STEP5D_V24_RNN_TRACKING_UNLOAD_MIN_M_S,
    opposed_dwell_stop_s: float = STEP5D_V24_RNN_TRACKING_OPPOSED_DWELL_S,
) -> dict[str, Any]:
    safe_dt_s = min(max(0.0, float(dt_s)), STEP5D_V12_GUARD_DT_MAX_S)
    prior_dwell = max(0.0, float(prior_dwell_s))
    try:
        qdot_arr = np.asarray(qdot, dtype=float)
        J = np.asarray(jacobian, dtype=float)
        outer = np.zeros(6, dtype=float) if outer_xdot_limited is None else np.asarray(outer_xdot_limited, dtype=float)
        reaction = normalize3(
            (float(reaction_normal_b[0]), float(reaction_normal_b[1]), float(reaction_normal_b[2]))
        )
        approach = np.asarray((-reaction[0], -reaction[1], -reaction[2]), dtype=float)
        actual_speed = np.asarray(actual_tcp_speed_b[:3], dtype=float)
        if qdot_arr.shape != (6,) or J.shape[1] != 6 or outer.shape != (6,) or actual_speed.shape != (3,):
            raise ValueError("shape")
        predicted_twist = J @ qdot_arr
        values_to_check = np.concatenate([qdot_arr, predicted_twist, outer, approach, actual_speed])
        if not np.all(np.isfinite(values_to_check)):
            raise ValueError("nonfinite")
    except (TypeError, ValueError, IndexError):
        return {
            "state": "danger_stop",
            "action": "stop_zero_qdot",
            "reason": "post_rnn_tracking_nonfinite",
            "predicted_twist": np.full(6, math.nan),
            "predicted_press_speed_m_s": math.nan,
            "actual_press_speed_m_s": math.nan,
            "normal_load_rate_n_s": math.nan,
            "dwell_s": prior_dwell,
            "zeroed_qdot": True,
            "directional_press": False,
        }
    outer_press_m_s = float(np.dot(outer[:3], approach))
    predicted_press_m_s = float(np.dot(predicted_twist[:3], approach))
    actual_press_m_s = float(np.dot(actual_speed, approach))
    reversed_unload = (
        outer_press_m_s >= float(outer_press_min_m_s)
        and predicted_press_m_s <= -float(unload_min_m_s)
    )
    if reversed_unload:
        dwell_s = prior_dwell + safe_dt_s
        if dwell_s >= float(opposed_dwell_stop_s):
            return {
                "state": "danger_stop",
                "action": "stop_zero_qdot",
                "reason": "post_rnn_tracking_reversed_unload_stop",
                "predicted_twist": predicted_twist,
                "predicted_press_speed_m_s": predicted_press_m_s,
                "actual_press_speed_m_s": actual_press_m_s,
                "normal_load_rate_n_s": math.nan,
                "dwell_s": dwell_s,
                "zeroed_qdot": True,
                "directional_press": False,
            }
        return {
            "state": "contact_uncertain_hold",
            "action": "hold_zero_qdot",
            "reason": "post_rnn_tracking_reversed_unload_hold",
            "predicted_twist": predicted_twist,
            "predicted_press_speed_m_s": predicted_press_m_s,
            "actual_press_speed_m_s": actual_press_m_s,
            "normal_load_rate_n_s": math.nan,
            "dwell_s": dwell_s,
            "zeroed_qdot": True,
            "directional_press": False,
        }
    return {
        "state": "valid_contact",
        "action": "pass_solver",
        "reason": "post_rnn_tracking_ok",
        "predicted_twist": predicted_twist,
        "predicted_press_speed_m_s": predicted_press_m_s,
        "actual_press_speed_m_s": actual_press_m_s,
        "normal_load_rate_n_s": math.nan,
        "dwell_s": 0.0,
        "zeroed_qdot": False,
        "directional_press": predicted_press_m_s >= 0.0,
    }


def ensure_step5d_liveprep_runtime(state: "BridgeState", args: argparse.Namespace) -> None:
    if state.step5d_model_bundle is None:
        state.step5d_model_bundle = step5d_kin.build_calibrated_model()
        audit_rows = step5d_kin.finite_run_rows(step5d_kin.DEFAULT_BRIDGE_CSV)
        state.step5d_tcp_offset_tool0 = step5d_kin.infer_tcp_offset(state.step5d_model_bundle, audit_rows)["mean"]
    if args.bridge_profile in STEP5D_TCP_CAGE_PROFILES and state.step5d_tcp_cage is None:
        state.step5d_tcp_cage = build_step5d_v15a_tcp_cage()
    if state.step5d_solver is None:
        state.step5d_solver = StrictTaseRnnSolver(
            StrictRnnConfig(
                paper_truth_path=STEP5D_LIVEPREP_TRUTH_PATH,
                qdot_limit_rad_s=float(args.step5d_qdot_limit_rad_s),
                epsilon=float(args.step5d_epsilon),
                sigr_exponent_r=float(args.step5d_sigr_exponent_r),
                inner_iterations=int(args.step5d_rnn_inner_iterations),
                backend=str(args.step5d_rnn_backend),
            )
        )
    if (
        uses_v30_control_contract(args.bridge_profile)
        and state.step5d_v30_deferred_diagnostics is None
    ):
        state.step5d_v30_deferred_diagnostics = DeferredV30Diagnostics(capacity=33_000)
    if (
        uses_v30_control_contract(args.bridge_profile)
        and state.step5d_v30_policy is None
    ):
        if state.step5d_solver is None:
            raise RuntimeError("v30 strict-RNN policy requires a prewarmed solver")
        state.step5d_v30_policy = StrictRnnControlPolicy(state.step5d_solver)


def step5d_liveprep_runtime_missing(state: "BridgeState", args: argparse.Namespace) -> list[str]:
    missing: list[str] = []
    if state.step5d_model_bundle is None:
        missing.append("model_bundle")
    if state.step5d_tcp_offset_tool0 is None:
        missing.append("tcp_offset_tool0")
    if state.step5d_solver is None:
        missing.append("solver")
    if (
        uses_v30_control_contract(args.bridge_profile)
        and state.step5d_v30_deferred_diagnostics is None
    ):
        missing.append("v30_deferred_diagnostics")
    if (
        uses_v30_control_contract(args.bridge_profile)
        and state.step5d_v30_policy is None
    ):
        missing.append("v30_control_policy")
    if args.bridge_profile in STEP5D_TCP_CAGE_PROFILES and state.step5d_tcp_cage is None:
        missing.append("tcp_cage")
    return missing


def require_step5d_liveprep_runtime_prewarmed(state: "BridgeState", args: argparse.Namespace) -> None:
    missing = step5d_liveprep_runtime_missing(state, args)
    if missing:
        raise RuntimeError(
            "Step5d liveprep runtime is not prewarmed before 500Hz control loop: "
            + ",".join(missing)
        )


def reset_step5d_solver_state_for_boundary(state: "BridgeState", boundary_key: str) -> None:
    if state.step5d_solver_lifecycle_key == boundary_key:
        return
    if state.step5d_solver is not None:
        state.step5d_solver.reset_state()
    state.step5d_last_qdot = None
    state.step5d_solver_lifecycle_key = boundary_key
    state.step5d_pending_solver_warm_start = True


def apply_step5d_solver_warm_start_if_pending(
    state: "BridgeState",
    *,
    jacobian: np.ndarray,
    xdot_c: np.ndarray,
    omega_minus: np.ndarray,
    omega_plus: np.ndarray,
) -> bool:
    """Warm-start the strict RNN at the first solve after a lifecycle reset.

    The reset boundary has no J/xdot_c in scope, so the reset only flags the
    warm start and the first subsequent solve supplies the entry command here.
    """
    if not state.step5d_pending_solver_warm_start or state.step5d_solver is None:
        return False
    try:
        state.step5d_solver.warm_start(
            J=jacobian,
            xdot_c=xdot_c,
            omega_minus=omega_minus,
            omega_plus=omega_plus,
        )
    except (ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
        raise RuntimeError(f"Step5d solver warm_start failed: {exc}") from exc
    state.step5d_pending_solver_warm_start = False
    return True


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


def step5d_stage25_register_values(
    command: tuple[float, float, float, float, float, float] | list[float],
    *,
    layout_tag: float,
    cmd_valid: float,
    path_time_s: float,
    force_error_n: float,
    pose_or_orientation_error: float,
) -> dict[str, float]:
    if len(command) != 6:
        raise ValueError(f"Step5d Stage25 command must have 6 values, got {len(command)}")
    command_values = tuple(float(value) for value in command)
    scalar_values = {
        "layout_tag": float(layout_tag),
        "cmd_valid": float(cmd_valid),
        "path_time_s": float(path_time_s),
        "force_error_n": float(force_error_n),
        "pose_or_orientation_error": float(pose_or_orientation_error),
    }
    for label, value in [*zip([f"cmd{idx}" for idx in range(6)], command_values), *scalar_values.items()]:
        if not math.isfinite(value):
            raise ValueError(f"Step5d Stage25 register value must be finite: {label}={value!r}")
    if scalar_values["layout_tag"] not in {STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE, STEP5D_STAGE25_JOINT_LAYOUT_CODE}:
        raise ValueError(f"Step5d Stage25 layout tag is invalid: {scalar_values['layout_tag']!r}")

    values = {
        "step4e_cmd_valid": scalar_values["cmd_valid"],
        "step4e_progress_m": scalar_values["path_time_s"],
        "step4e_force_error_n": scalar_values["force_error_n"],
        "step4e_orientation_error_rad": scalar_values["pose_or_orientation_error"],
        "step4e_controller_state": scalar_values["layout_tag"],
    }
    for idx, value in enumerate(command_values):
        values[BRIDGE_INPUT_NAMES[idx]] = value
    return values


def build_step5d_v30_exception_stop_packet(
    *,
    heartbeat: float,
    original_error: BaseException,
) -> tuple[RegisterCommand, dict[str, float]]:
    """Build one finite manifest-bound layout-524 emergency packet."""

    try:
        safe_heartbeat = float(heartbeat)
    except (TypeError, ValueError):
        safe_heartbeat = 0.0
    if not math.isfinite(safe_heartbeat) or safe_heartbeat < 0.0:
        safe_heartbeat = 0.0
    command = RegisterCommand(
        heartbeat=safe_heartbeat,
        qdot=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        cmd_valid=False,
        path_time_s=0.0,
        force_error_n=0.0,
        orientation_error_rad=0.0,
        layout_code=STEP5D_STAGE25_JOINT_LAYOUT_CODE,
        stop_request=True,
        decision_reason=f"v30_bridge_exception:{type(original_error).__name__}",
    )
    packet = {name: 0.0 for name in INPUT_NAMES}
    input_name_by_register = {
        int(field.rsplit("_", 1)[1]): name
        for field, name in zip(INPUT_FIELDS, INPUT_NAMES)
    }
    register_values = command.as_register_values()
    missing = sorted(set(register_values) - set(input_name_by_register))
    if missing:
        raise RuntimeError(
            "v30 exception stop command is not bound to the active RTDE manifest: "
            + ",".join(str(value) for value in missing)
        )
    for register, value in register_values.items():
        packet[input_name_by_register[register]] = float(value)
    if (
        any(packet[name] != 0.0 for name in BRIDGE_INPUT_NAMES[:6])
        or packet["step4e_cmd_valid"] != 0.0
        or packet["step4e_controller_state"] != STEP5D_STAGE25_JOINT_LAYOUT_CODE
        or packet["stop_request"] != 1.0
    ):
        raise RuntimeError("v30 exception packet is not exact-zero layout-524 stop")
    return command, packet


def publish_step5d_v30_exception_stop(
    rtde: Any,
    *,
    recipe_id: int,
    type_names: list[str],
    heartbeat: float,
    original_error: BaseException,
) -> tuple[RegisterCommand | None, dict[str, float] | None, dict[str, Any]]:
    """Attempt one stop packet and return a durable, non-silent outcome row."""

    event: dict[str, Any] = {
        "event": "v30_control_exception_fail_closed_publish",
        "at_monotonic_s": time.monotonic(),
        "original_error": rtde_error_name(original_error),
        "stop_publish_succeeded": False,
        "stop_publish_error": "",
    }
    try:
        command, packet = build_step5d_v30_exception_stop_packet(
            heartbeat=heartbeat,
            original_error=original_error,
        )
    except Exception as publish_error:
        event["stop_publish_error"] = rtde_error_name(publish_error)
        return None, None, event
    event["stop_command"] = {
        "heartbeat": command.heartbeat,
        "qdot": list(command.qdot),
        "cmd_valid": command.cmd_valid,
        "layout_code": command.layout_code,
        "stop_request": command.stop_request,
        "decision_reason": command.decision_reason,
    }
    try:
        if rtde is None:
            raise RuntimeError("RTDE transport unavailable for v30 exception stop")
        rtde.send_input_sample(
            recipe_id,
            type_names,
            [packet[name] for name in INPUT_NAMES],
        )
    except Exception as publish_error:
        event["stop_publish_error"] = rtde_error_name(publish_error)
        return command, packet, event
    event["stop_publish_succeeded"] = True
    return command, packet, event


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
        self.step5d_solver_lifecycle_key = "inactive"
        self.step5d_pending_solver_warm_start = False
        self.step5d_outer_state = Step5dOuterLoopState()
        self.step5d_v30_sequence = 0
        self.step5d_v30_deferred_diagnostics: DeferredV30Diagnostics | None = None
        self.step5d_v30_safety_envelope = SafetyEnvelope()
        self.step5d_v30_policy: StrictRnnControlPolicy | None = None
        self.step5d_settle_filtered_normal_load_n: float | None = None
        self.step5d_line_guard_loss_s = 0.0
        self.step5d_last_qdot: np.ndarray | None = None
        self.step5d_contact_hold_s = 0.0
        self.step5d_hard_low_load_s = 0.0
        self.step5d_contact_high_window_s = 0.0
        self.step5d_actual_speed_violation_s = 0.0
        self.step5d_actual_speed_violation_count = 0
        self.step5d_contact_hold_path_time_s: float | None = None
        self.step5d_active_stage25_s = 0.0
        self.step5d_last_stage25_row_time_s: float | None = None
        self.step5d_hold_event_count = 0
        self.step5d_consecutive_hold_s = 0.0
        self.step5d_total_hold_s = 0.0
        self.step5d_repeated_hold_count = 0
        self.step5d_last_hold_reason = ""
        self.step5d_hold_actual_tcp_speed_m_s: float | None = None
        self.step5d_active_reacquire_s = 0.0
        self.step5d_no_contact_s = 0.0
        self.step5d_normal_direction_guard_dwell_s = 0.0
        self.step5d_tracking_guard_dwell_s = 0.0
        self.step5d_normal_direction_prev_load_n: float | None = None
        self.step5d_stage25_normal_relatched = False
        self.step5d_stage25_entry_relatch_angle_rad: float | None = None
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
        self.step5d_v30_sequence = 0
        self.step5d_v30_deferred_diagnostics = None
        self.step5d_v30_safety_envelope = SafetyEnvelope()
        self.step5d_v30_policy = None
        reset_step5d_solver_state_for_boundary(self, "inactive")
        self.step5d_settle_filtered_normal_load_n = None
        self.step5d_line_guard_loss_s = 0.0
        self.step5d_last_qdot = None
        self.step5d_contact_hold_s = 0.0
        self.step5d_hard_low_load_s = 0.0
        self.step5d_contact_high_window_s = 0.0
        self.step5d_actual_speed_violation_s = 0.0
        self.step5d_actual_speed_violation_count = 0
        self.step5d_contact_hold_path_time_s = None
        self.step5d_active_stage25_s = 0.0
        self.step5d_last_stage25_row_time_s = None
        self.step5d_hold_event_count = 0
        self.step5d_consecutive_hold_s = 0.0
        self.step5d_total_hold_s = 0.0
        self.step5d_repeated_hold_count = 0
        self.step5d_last_hold_reason = ""
        self.step5d_hold_actual_tcp_speed_m_s = None
        self.step5d_normal_direction_guard_dwell_s = 0.0
        self.step5d_tracking_guard_dwell_s = 0.0
        self.step5d_normal_direction_prev_load_n = None
        self.step5d_active_reacquire_s = 0.0
        self.step5d_no_contact_s = 0.0
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
    step5b_profile = args.bridge_profile in STEP5B_BRIDGE_PROFILES
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
    step5d_liveprep_v16_profile = args.bridge_profile == STEP5D_LIVEPREP_V16_STAGE_ID
    step5d_liveprep_v17_profile = args.bridge_profile == STEP5D_LIVEPREP_V17_STAGE_ID
    step5d_liveprep_v18_profile = args.bridge_profile == STEP5D_LIVEPREP_V18_STAGE_ID
    step5d_liveprep_v19_profile = args.bridge_profile == STEP5D_LIVEPREP_V19_STAGE_ID
    step5d_liveprep_v20_profile = args.bridge_profile == STEP5D_LIVEPREP_V20_STAGE_ID
    step5d_liveprep_v21_profile = args.bridge_profile == STEP5D_LIVEPREP_V21_STAGE_ID
    step5d_liveprep_v22_profile = args.bridge_profile == STEP5D_LIVEPREP_V22_STAGE_ID
    step5d_liveprep_v23_profile = args.bridge_profile == STEP5D_LIVEPREP_V23_STAGE_ID
    step5d_liveprep_v24_profile = args.bridge_profile == STEP5D_LIVEPREP_V24_STAGE_ID
    step5d_liveprep_v25_profile = args.bridge_profile == STEP5D_ABLATION_V25_STAGE_ID
    step5d_liveprep_v26_profile = args.bridge_profile == STEP5D_ABLATION_V26_STAGE_ID
    step5d_liveprep_v27_profile = args.bridge_profile == STEP5D_ABLATION_V27_STAGE_ID
    step5d_liveprep_v28_profile = args.bridge_profile == STEP5D_ABLATION_V28_STAGE_ID
    step5d_liveprep_v29_profile = args.bridge_profile == STEP5D_ABLATION_V29_STAGE_ID
    step5d_liveprep_v30_profile = args.bridge_profile == STEP5D_ABLATION_V30_STAGE_ID
    step5d_no_contact_p0_profile = is_no_contact_p0_stage(args.bridge_profile)
    step5d_no_contact_p0_v8_profile = (
        args.bridge_profile == STEP5D_NO_CONTACT_P0_V8_STAGE_ID
    )
    step5d_v30_contract_profile = uses_v30_control_contract(args.bridge_profile)
    step5d_step5b_speedl_live_profile = (
        step5d_liveprep_v27_profile
        or step5d_liveprep_v28_profile
        or step5d_liveprep_v29_profile
        or step5d_liveprep_v30_profile
    )
    step5d_ablation_profile = (
        step5d_liveprep_v25_profile
        or step5d_liveprep_v26_profile
        or step5d_step5b_speedl_live_profile
        or step5d_no_contact_p0_profile
    )
    step5d_liveprep_v16_or_v17_profile = step5d_liveprep_v16_profile or step5d_liveprep_v17_profile
    step5d_liveprep_v18_or_v19_profile = step5d_liveprep_v18_profile or step5d_liveprep_v19_profile
    step5d_liveprep_v18_or_newer_profile = (
        step5d_liveprep_v18_or_v19_profile
        or step5d_liveprep_v20_profile
        or step5d_liveprep_v21_profile
        or step5d_liveprep_v22_profile
        or step5d_liveprep_v23_profile
        or step5d_liveprep_v24_profile
        or step5d_liveprep_v25_profile
        or step5d_liveprep_v26_profile
        or step5d_step5b_speedl_live_profile
        or step5d_no_contact_p0_profile
    )
    step5d_liveprep_v17_or_newer_profile = step5d_liveprep_v17_profile or step5d_liveprep_v18_or_newer_profile
    if (
        step5d_liveprep_v21_profile
        or step5d_liveprep_v22_profile
        or step5d_liveprep_v23_profile
        or step5d_liveprep_v24_profile
        or step5d_liveprep_v25_profile
        or step5d_liveprep_v26_profile
        or step5d_step5b_speedl_live_profile
    ):
        step5d_entry_raw_sanity_min_n = float(args.step5d_preload_raw_min_n)
        step5d_entry_raw_sanity_max_n = float(args.step5d_preload_raw_max_n)
    else:
        step5d_entry_raw_sanity_min_n = STEP5D_V17_ENTRY_RAW_NORMAL_LOAD_MIN_N
        step5d_entry_raw_sanity_max_n = (
            STEP5D_V19_ENTRY_RAW_NORMAL_LOAD_MAX_N
            if (step5d_liveprep_v19_profile or step5d_liveprep_v20_profile)
            else STEP5D_V17_ENTRY_RAW_NORMAL_LOAD_MAX_N
        )
    step5d_liveprep_online_cage_profile = args.bridge_profile in STEP5D_TCP_CAGE_PROFILES
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
        or step5d_liveprep_v16_profile
        or step5d_liveprep_v17_profile
        or step5d_liveprep_v18_profile
        or step5d_liveprep_v19_profile
        or step5d_liveprep_v20_profile
        or step5d_liveprep_v21_profile
        or step5d_liveprep_v22_profile
        or step5d_liveprep_v23_profile
        or step5d_liveprep_v24_profile
        or step5d_liveprep_v25_profile
        or step5d_liveprep_v26_profile
        or step5d_step5b_speedl_live_profile
        or step5d_no_contact_p0_profile
    )
    if step5d_liveprep_profile:
        missing_runtime = step5d_liveprep_runtime_missing(state, args)
        if missing_runtime:
            values["_step5d_solver_error"] = (
                "prewarm: Step5d liveprep runtime missing before 500Hz control loop: "
                + ",".join(missing_runtime)
            )
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
    qdot_clear_stage_active = (
        args.bridge_mode == "line"
        and (step5d_liveprep_v22_profile or step5d_liveprep_v23_profile or step5d_liveprep_v24_profile or step5d_ablation_profile)
        and abs(robot_stage - STEP5D_QDOT_CLEAR_STAGE) < 0.05
    )
    line_entry_gate_active = (
        v29_profile or v30_profile or v31_profile or step5b_profile or step5c_contact_profile or step5d_liveprep_profile or step6b_profile
    ) and acquire_stage_active
    line_stage_active = args.bridge_mode == "line" and abs(robot_stage - 25.0) < 0.05
    step5d_joint_line_profile = (step5d_liveprep_profile or step5d_ablation_profile) and line_stage_active
    step5d_stage25_control_mode = (
        str(getattr(args, "step5d_stage25_control_mode", "speedl_cartesian_oracle"))
        if step5d_ablation_profile
        else "speedj_rnn_live"
    )
    if not step5d_joint_line_profile:
        state.step5d_line_guard_loss_s = 0.0
        state.step5d_last_qdot = None
        state.step5d_contact_hold_s = 0.0
        state.step5d_hard_low_load_s = 0.0
        state.step5d_contact_high_window_s = 0.0
        state.step5d_actual_speed_violation_s = 0.0
        state.step5d_actual_speed_violation_count = 0
        state.step5d_contact_hold_path_time_s = None
        state.step5d_active_stage25_s = 0.0
        state.step5d_last_stage25_row_time_s = None
        state.step5d_hold_event_count = 0
        state.step5d_consecutive_hold_s = 0.0
        state.step5d_total_hold_s = 0.0
        state.step5d_repeated_hold_count = 0
        state.step5d_last_hold_reason = ""
        state.step5d_hold_actual_tcp_speed_m_s = None
        state.step5d_active_reacquire_s = 0.0
        state.step5d_no_contact_s = 0.0
        state.step5d_normal_direction_guard_dwell_s = 0.0
        state.step5d_tracking_guard_dwell_s = 0.0
        state.step5d_normal_direction_prev_load_n = None
        reset_step5d_solver_state_for_boundary(state, "inactive")
    control_stage_active = (
        latch_stage_active
        or detach_stage_active
        or orient_stage_active
        or acquire_stage_active
        or qdot_clear_stage_active
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
        stage25_now_s = time.monotonic()
        values["_step5d_stage25_row_gap_s"] = (
            0.0 if state.step5d_last_stage25_row_time_s is None else stage25_now_s - state.step5d_last_stage25_row_time_s
        )
        state.step5d_last_stage25_row_time_s = stage25_now_s
        echo_command = []
        for idx in range(36, 42):
            try:
                echo_command.append(float(latest_output.get(f"output_double_register_{idx}", 0.0)))
            except (TypeError, ValueError):
                echo_command.append(0.0)
        try:
            values["_step5d_stage25_echo_cmd_valid"] = float(latest_output.get("output_double_register_42", 0.0))
        except (TypeError, ValueError):
            values["_step5d_stage25_echo_cmd_valid"] = 0.0
        try:
            values["_step5d_stage25_echo_layout_tag"] = float(latest_output.get("output_double_register_46", 0.0))
        except (TypeError, ValueError):
            values["_step5d_stage25_echo_layout_tag"] = 0.0
        try:
            values["_step5d_stage25_echo_consumed"] = float(latest_output.get("output_double_register_47", 0.0))
        except (TypeError, ValueError):
            values["_step5d_stage25_echo_consumed"] = 0.0
        values["_step5d_stage25_echo_command_norm"] = math.sqrt(sum(value * value for value in echo_command))
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
        if (
            step5d_step5b_speedl_live_profile
            and not state.step5d_stage25_normal_relatched
            and live_candidate_force_n >= args.bridge_normal_min_force_n
            and dot3(state.latched_normal_b, live_candidate_b) > 0.0
        ):
            # Stage25 entry re-latch: the pre-contact latch can sit several
            # degrees off the true reaction direction (0.117 rad on the v28
            # 60s run) while the 0.01 rad/s follow filter needs the whole run
            # to walk it back; any orientation loop chasing that stale latch
            # saturates. Re-anchor latch and filter state to the live
            # force-direction candidate on the first loaded Stage25 tick.
            state.step5d_stage25_entry_relatch_angle_rad = angle_between_unit(
                state.latched_normal_b, live_candidate_b
            )
            state.latched_normal_b = live_candidate_b
            state.filtered_normal_b = live_candidate_b
            filtered_current = live_candidate_b
            state.step5d_stage25_normal_relatched = True
        live_candidate_angle_rad = angle_between_unit(filtered_current, live_candidate_b)
        live_candidate_angle_from_latch_rad = angle_between_unit(state.latched_normal_b, live_candidate_b)
        if step5d_liveprep_v18_or_newer_profile and state.line_stage_s <= STEP5D_V18_NORMAL_FOLLOW_SETTLE_S:
            state.filtered_normal_b = state.latched_normal_b
            n_control_b = state.latched_normal_b
            normal_filter_source = "v18_v20_locked_normal_settle"
        elif v31_profile or step4f_profile or step4g_profile or step5b_profile or step5c_contact_profile or (step5d_liveprep_profile and not step5d_liveprep_v18_or_newer_profile) or step6b_profile:
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
    step5d_search_pose_contract_active = (
        step5d_liveprep_profile
        and math.isfinite(robot_stage)
        and (
            abs(robot_stage - 22.0) < 0.05
            or abs(robot_stage - 24.0) < 0.05
            or abs(robot_stage - 24.2) < 0.05
        )
    )
    step5d_search_pose_contract_axis_error_rad = (
        angle_between_unit(tcp_z_axis_b, STEP5D_SEARCH_POSE_TARGET_AXIS_B)
        if step5d_search_pose_contract_active
        else math.nan
    )
    step5d_search_pose_contract_ok = (
        step5d_search_pose_contract_active
        and step5d_search_pose_contract_axis_error_rad <= STEP5D_SEARCH_POSE_RUNTIME_TOLERANCE_RAD
    )
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
    step5d_reacquire_speed_cap_active = False
    step5d_reacquire_speed_cap_original_m_s = math.nan
    step5d_tcp_cage = {
        "distance_m": math.nan,
        "braking_margin_m": math.nan,
        "signed_distance_m": math.nan,
        "cell_index": -1.0,
        "reason": "not_active",
    }
    step5d_contact_safety_profile = (
        step5d_liveprep_v13_profile
        or step5d_liveprep_v14_profile
        or step5d_liveprep_v15_profile
        or step5d_liveprep_online_cage_profile
    )
    if step5d_contact_safety_profile and step5d_joint_line_profile:
        step5d_contact_safety_fn = (
            step5d_v18_guard
            if step5d_liveprep_v18_or_newer_profile
            else step5d_v15a_guard
            if step5d_liveprep_online_cage_profile
            else step5d_v15_permissive_recovery_guard
            if step5d_liveprep_v15_profile
            else step5d_v13_contact_safety_guard
        )
        if step5d_liveprep_online_cage_profile:
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
        if step5d_ablation_profile and step5d_stage25_control_mode == "speedl_cartesian_oracle":
            step5d_contact_safety = step5d_v25_speedl_cartesian_guard(
                normal_load_n=normal_load_n,
                force_norm_n=force_abs,
                actual_tcp_speed_m_s=step5d_line_tcp_speed_m_s,
                braking_margin_m=(
                    float(step5d_tcp_cage["braking_margin_m"])
                    if step5d_liveprep_online_cage_profile
                    else None
                ),
                prior_soft_low_load_s=state.step5d_contact_hold_s,
                prior_hard_low_load_s=state.step5d_hard_low_load_s,
                prior_actual_speed_violation_s=state.step5d_actual_speed_violation_s,
                prior_actual_speed_violation_count=state.step5d_actual_speed_violation_count,
                dt_s=dt_s,
            )
        else:
            step5d_contact_safety = step5d_contact_safety_fn(
                normal_load_n=normal_load_n,
                force_norm_n=force_abs,
                actual_tcp_speed_m_s=step5d_line_tcp_speed_m_s,
                predicted_tcp_speed_m_s=None,
                braking_margin_m=(
                    float(step5d_tcp_cage["braking_margin_m"])
                    if step5d_liveprep_online_cage_profile
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
                soft_low_load_n=STEP5D_V18_SOFT_LOW_LOAD_N if step5d_liveprep_v18_or_newer_profile else STEP5D_V16_SOFT_LOW_LOAD_N if step5d_liveprep_v16_or_v17_profile else STEP5D_V13_SOFT_LOW_LOAD_N,
                valid_contact_min_n=STEP5D_V16_VALID_CONTACT_MIN_N if (step5d_liveprep_v16_or_v17_profile or step5d_liveprep_v18_or_newer_profile) else STEP5D_V13_VALID_CONTACT_MIN_N,
                valid_contact_max_n=STEP5D_V18_VALID_CONTACT_MAX_N if step5d_liveprep_v18_or_newer_profile else STEP5D_V16_VALID_CONTACT_MAX_N if step5d_liveprep_v16_or_v17_profile else STEP5D_V13_VALID_CONTACT_MAX_N,
                low_load_speed_load_n=STEP5D_V16_LOW_LOAD_SPEED_LOAD_N if (step5d_liveprep_v16_or_v17_profile or step5d_liveprep_v18_or_newer_profile) else STEP5D_V13_LOW_LOAD_SPEED_LOAD_N,
                hold_timeout_s=STEP5D_V24_LOW_LOAD_HOLD_TIMEOUT_S if step5d_liveprep_v24_profile else STEP5D_V16_LOW_LOAD_HOLD_TIMEOUT_S if step5d_liveprep_v16_or_v17_profile else STEP5D_V13_LOW_LOAD_HOLD_TIMEOUT_S,
                high_window_dwell_stop_s=STEP5D_V16_HIGH_WINDOW_DWELL_STOP_S if (step5d_liveprep_v16_or_v17_profile or step5d_liveprep_v18_or_newer_profile) else STEP5D_V13_HIGH_WINDOW_DWELL_STOP_S,
                allow_high_contact_below_hard_force=step5d_liveprep_v18_or_newer_profile or not step5d_liveprep_v16_or_v17_profile,
                force_norm_hard_stop_n=STEP5D_V27_SENSOR_FORCE_HARD_STOP_N if step5d_step5b_speedl_live_profile else STEP5D_V24_SENSOR_FORCE_HARD_STOP_N if (step5d_liveprep_v24_profile or step5d_ablation_profile) else STEP5D_V18_SENSOR_FORCE_HARD_STOP_N if step5d_liveprep_v18_or_newer_profile else 60.0,
                cage_primary_low_load_reacquire=step5d_liveprep_v18_or_newer_profile and not (step5d_liveprep_v24_profile or step5d_ablation_profile),
                defer_low_load_hold_timeout=not (step5d_liveprep_v24_profile or step5d_ablation_profile),
            )
        state.step5d_contact_hold_s = float(step5d_contact_safety["hold_s"])
        state.step5d_hard_low_load_s = float(step5d_contact_safety.get("hard_low_load_s", 0.0))
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
        elif step5d_contact_safety["action"] == "active_reacquire_solver":
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
            or (step5d_no_contact_p0_profile and line_stage_active)
        )
    if step5d_no_contact_p0_profile and args.bridge_mode == "line" and line_stage_active:
        control_allowed = sensor_ok > 0.5
    if args.bridge_integrate_stage25_only and args.bridge_mode == "line" and not control_stage_active:
        control_allowed = False
    step5d_v8_pid_recovery_ok = True
    step5b_15n_trial_stop_reason = None
    if control_allowed:
        if (
            args.bridge_mode == "line"
            and not step5c_dryrun_profile
            and not (step5d_no_contact_p0_profile and line_stage_active)
            and not state.normal_acquired
        ):
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
            if step5d_liveprep_v10_profile or step5d_liveprep_v11_profile or step5d_liveprep_v12_profile or step5d_liveprep_v13_profile or step5d_liveprep_v14_profile or step5d_liveprep_v15_profile or step5d_liveprep_online_cage_profile:
                recovery_window_ok = step5d_v9_recovery_window_ok(normal_load_n=normal_load_n, force_norm_n=force_abs)
                if recovery_window_ok:
                    if step5d_liveprep_v11_profile or step5d_liveprep_v12_profile or step5d_liveprep_v13_profile or step5d_liveprep_v14_profile or step5d_liveprep_v15_profile or step5d_liveprep_online_cage_profile:
                        acquire_min_load_n, acquire_max_load_n, _acquire_force_norm_n = step5d_liveprep_contact_window_limits(args.bridge_profile) if step5d_liveprep_v17_or_newer_profile else (
                            STEP5D_V11_ENTRY_NORMAL_LOAD_MIN_N,
                            STEP5D_V11_ENTRY_NORMAL_LOAD_MAX_N,
                            STEP5D_V8_FORCE_NORM_MAX_N,
                        )
                        cmd, state.step5d_settle_filtered_normal_load_n, state.normal_velocity_m_s = (
                            step5d_v11_deadband_acquire_velocity(
                                normal_load_n=normal_load_n,
                                filtered_normal_load_n=state.step5d_settle_filtered_normal_load_n,
                                settle_velocity_m_s=state.normal_velocity_m_s,
                                dt_s=dt_s,
                                reaction_normal_b=tuple(float(value) for value in n_control_b),  # type: ignore[arg-type]
                                min_normal_load_n=acquire_min_load_n,
                                max_normal_load_n=acquire_max_load_n,
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
        step5d_outer_xdot_joint_feasible: np.ndarray | None = None
        step5d_outer_xdot_limiter_active = False
        step5d_xdot_feasibility_diagnostics: dict[str, Any] = {
            "qdot_cap_rad_s": float(args.step5d_qdot_limit_rad_s),
            "jinv_xdot_inf_rad_s": math.nan,
            "jinv_xdot_inf_over_qdot_cap": math.nan,
            "jinv_xdot_solve_status": "not_enabled",
            "xdot_feasibility_scale": 1.0,
            "xdot_norm_pre_feasibility_scale": math.nan,
            "xdot_norm_post_feasibility_scale": math.nan,
            "xdot_feasibility_scale_active": False,
        }
        step5d_qdot_slew_limiter_active = False
        step5d_raw_qdot_command: tuple[float, float, float, float, float, float] | None = None
        step5d_post_slew_qdot_command: tuple[float, float, float, float, float, float] | None = None
        step5d_stage25_command: tuple[float, float, float, float, float, float] | None = None
        step5d_rejected_rnn_diagnostics: dict[str, Any] | None = None
        step5d_rejected_solver_status = math.nan
        step5d_rejected_residual_norm = math.nan
        step5d_rejected_active_bounds_count = math.nan
        step5d_speedl_shadow_raw_linear_cmd: tuple[float, float, float] | None = None
        step5d_speedl_shadow_raw_angular_cmd: tuple[float, float, float] | None = None
        step5d_speedl_orientation_shadow_only = False
        step5d_live_control_source = ""
        step5d_stage25_layout_tag = STEP5D_STAGE25_JOINT_LAYOUT_CODE
        step5d_stage25_control_mode = (
            str(getattr(args, "step5d_stage25_control_mode", "speedl_cartesian_oracle"))
            if step5d_ablation_profile
            else "speedj_rnn_live"
        )
        step5d_p0_posture_policy = {
            "policy": "",
            "active": False,
            "orientation_gain_scale": 1.0,
            "effective_ko": math.nan,
        }
        step5d_p0_frame_diagnostics: dict[str, Any] = {
            "valid": False,
            "mode": "",
            "reason": "not_active",
            "limited_tcp": np.full(6, math.nan, dtype=float),
            "limited_base": np.full(6, math.nan, dtype=float),
            "tcp_press_speed_m_s": math.nan,
        }
        step5d_p0_rnn_accepted = math.nan
        step5d_p0_rnn_reject_reason = "not_active"
        step5d_p0_safe_hold_active = math.nan
        step5d_p0_v8_target: P0V8Target | None = None
        step5d_rnn_accepted = math.nan
        step5d_rnn_reject_reason = "not_active"
        step5d_safe_hold_active = math.nan
        step5d_v30_register_command: Any | None = None
        control_step_v30: Any | None = None
        observation_v30: Step5dObservation | None = None
        raw_candidate_v30: ControlCandidate | None = None
        candidate_v30: ControlCandidate | None = None
        dls_shadow_v30: Any | None = None
        decision_v30: Any | None = None
        step5d_cmd_valid_reason = "not_active"
        step5d_intervention_reasons: list[str] = []
        step5d_predicted_twist = np.full(6, math.nan, dtype=float)
        step5d_post_rnn_normal_guard = {
            "state": "inactive",
            "action": "inactive",
            "reason": "not_active",
            "predicted_twist": step5d_predicted_twist,
            "predicted_press_speed_m_s": math.nan,
            "actual_press_speed_m_s": math.nan,
            "normal_load_rate_n_s": math.nan,
            "dwell_s": state.step5d_normal_direction_guard_dwell_s,
            "zeroed_qdot": False,
            "directional_press": False,
        }
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
                require_step5d_liveprep_runtime_prewarmed(state, args)
                if step5d_contact_safety_profile:
                    lifecycle_key = (
                        "stage25_pass_solver"
                        if step5d_contact_safety["action"] == "pass_solver"
                        else f"stage25_{step5d_contact_safety['action']}:{step5d_contact_safety['reason']}"
                    )
                else:
                    lifecycle_key = "stage25_pass_solver"
                reset_step5d_solver_state_for_boundary(state, lifecycle_key)
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
                v19_freeze_low_force_reacquire = (
                    step5d_liveprep_v19_profile
                    and normal_filter_source == "freeze_low_force"
                    and normal_load_n <= STEP5D_V18_ACTIVE_REACQUIRE_LOAD_MAX_N
                )
                v20_low_load_active_reacquire = (
                    (
                        step5d_liveprep_v20_profile
                        or step5d_liveprep_v21_profile
                        or step5d_liveprep_v22_profile
                        or step5d_liveprep_v23_profile
                    )
                    and step5d_contact_safety["action"] == "active_reacquire_solver"
                    and normal_load_n <= STEP5D_V18_ACTIVE_REACQUIRE_LOAD_MAX_N
                )
                low_load_active_reacquire_reset = v19_freeze_low_force_reacquire or v20_low_load_active_reacquire
                step5d_outer_state_for_compute = (
                    Step5dOuterLoopState()
                    if low_load_active_reacquire_reset
                    else state.step5d_outer_state
                )
                base_step5d_ko = STEP5D_V28_SHADOW_KO if step5d_step5b_speedl_live_profile else 5.0
                if step5d_no_contact_p0_profile and not step5d_no_contact_p0_v8_profile:
                    step5d_p0_posture_policy = step5d_no_contact_p0_low_force_posture_policy(
                        normal_load_n=normal_load_n,
                        base_ko=base_step5d_ko,
                        low_ko=STEP5D_NO_CONTACT_P0_LOW_FORCE_POSTURE_KO,
                        policy=STEP5D_NO_CONTACT_P0_LOW_FORCE_POSTURE_POLICY,
                    )
                step5d_x_pd_base = (float(desired_x), float(desired_y), float(pose[2]))
                step5d_xdot_pd_base = (float(desired_velocity_xy[0]), float(desired_velocity_xy[1]), 0.0)
                if step5d_no_contact_p0_profile:
                    step5d_x_pd_base = (float(pose[0]), float(pose[1]), float(pose[2]))
                    step5d_xdot_pd_base = (0.0, 0.0, 0.0)
                if step5d_no_contact_p0_profile:
                    if step5d_no_contact_p0_v8_profile:
                        step5d_p0_v8_target = shared_build_p0_v8_target(
                            tcp_pose_base=tuple(float(value) for value in pose[:6]),
                            tcp_speed_base=tuple(float(value) for value in speed[:6]),
                            force_tcp_n=force_t,
                            reaction_normal_b=tuple(float(value) for value in n_control_b),
                            normal_load_n=normal_load_n,
                            force_error_n=force_error,
                            jacobian=jacobian,
                            dt_s=dt_s,
                            qdot_cap_rad_s=float(args.step5d_qdot_limit_rad_s),
                        )
                        step5d_p0_posture_policy = dict(step5d_p0_v8_target.posture_policy)
                        step5d_outer_output = SimpleNamespace(
                            xdot_c=np.asarray(step5d_p0_v8_target.raw_outer_twist, dtype=float),
                            cmd_valid=True,
                            next_state=Step5dOuterLoopState(),
                            diagnostics=dict(step5d_p0_v8_target.outer_diagnostics),
                        )
                    else:
                        step5d_outer_output = step5d_no_contact_p0_press_only_outer_output(
                            reaction_normal_b=n_control_b,
                            force_error_n=force_error,
                        )
                else:
                    step5d_outer_call_kwargs = (
                        {"include_diagnostics": "compact"}
                        if step5d_v30_contract_profile
                        else {}
                    )
                    step5d_outer_output = compute_step5d_outer_loop(
                        Step5dOuterLoopConfig(
                            kp=4.0,
                            ko=base_step5d_ko,
                            orientation_gain_scale=float(step5d_p0_posture_policy["orientation_gain_scale"]),
                            kf=1.0,
                            Md_scalar=STEP5D_V28_SHADOW_MD if step5d_step5b_speedl_live_profile else 12.0,
                            Bd_scalar=STEP5D_V28_SHADOW_BD if step5d_step5b_speedl_live_profile else 550.0,
                            force_target_n=float(args.target_force_n),
                            delay_T_s=dt_s,
                            force_sign_convention="step5_step6_positive_normal_load",
                        ),
                        step5d_outer_state_for_compute,
                        Step5dOuterLoopInputs(
                            tcp_pose_base=tuple(float(value) for value in pose[:6]),  # type: ignore[arg-type]
                            tcp_speed_base=tuple(float(value) for value in speed[:6]),  # type: ignore[arg-type]
                            force_tcp_n=force_t,
                            control_reaction_normal_base=tuple(float(value) for value in n_control_b),  # type: ignore[arg-type]
                            x_pd_base=step5d_x_pd_base,
                            xdot_pd_base=step5d_xdot_pd_base,
                            dt_s=dt_s,
                            cmd_valid=True,
                        ),
                        **step5d_outer_call_kwargs,
                    )
                outer_orientation_error_rad = float(step5d_outer_output.diagnostics["outer_orientation_angle_rad"])
                if not step5d_no_contact_p0_profile and not semantic_boundary_is_consistent(
                    contact_orientation_error_rad=orientation_error,
                    outer_orientation_error_rad=outer_orientation_error_rad,
                    tolerance_rad=STEP5D_SEMANTIC_ORIENTATION_TOLERANCE_RAD,
                ):
                    step5d_engage_gate_ok = False
                    raise ValueError(
                        "Step5d semantic gate blocked: contact orientation "
                        f"{orientation_error:.6f} rad vs outer orientation {outer_orientation_error_rad:.6f} rad"
                    )
                state.step5d_outer_state = (
                    Step5dOuterLoopState()
                    if low_load_active_reacquire_reset
                    else step5d_outer_output.next_state
                )
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
                if step5d_no_contact_p0_v8_profile:
                    if step5d_p0_v8_target is None:
                        raise RuntimeError("shared P0 v8 target was not built")
                    step5d_outer_xdot_limited = np.asarray(
                        step5d_p0_v8_target.limited_twist,
                        dtype=float,
                    )
                    step5d_outer_xdot_limiter_active = bool(
                        step5d_p0_v8_target.limiter_active
                    )
                    step5d_p0_frame_diagnostics = dict(
                        step5d_p0_v8_target.frame_diagnostics
                    )
                elif step5d_no_contact_p0_profile:
                    (
                        step5d_outer_xdot_limited,
                        step5d_outer_xdot_limiter_active,
                        step5d_p0_frame_diagnostics,
                    ) = limit_step5d_no_contact_p0_xdot_components(
                        step5d_outer_xdot_limited,
                        rotation_base_from_tcp=rotvec_to_matrix(float(pose[3]), float(pose[4]), float(pose[5])),
                        max_angular_rad_s=min(
                            float(args.bridge_angular_limit_rad_s),
                            STEP5D_NO_CONTACT_P0_ANGULAR_COMPONENT_LIMIT_RAD_S,
                        ),
                        return_diagnostics=True,
                    )
                elif step5d_liveprep_guarded_profile:
                    step5d_outer_xdot_limited, step5d_outer_xdot_limiter_active = limit_step5d_live_xdot(
                        step5d_outer_xdot_limited,
                        max_linear_m_s=float(args.bridge_total_linear_limit_m_s),
                        max_angular_rad_s=float(args.bridge_angular_limit_rad_s),
                    )
                step5d_outer_xdot_joint_feasible = step5d_outer_xdot_limited
                if step5d_no_contact_p0_v8_profile:
                    if step5d_p0_v8_target is None:
                        raise RuntimeError("shared P0 v8 target was not built")
                    step5d_outer_xdot_joint_feasible = np.asarray(
                        step5d_p0_v8_target.desired_twist,
                        dtype=float,
                    )
                    step5d_xdot_feasibility_diagnostics = dict(
                        step5d_p0_v8_target.feasibility_diagnostics
                    )
                elif (
                    step5d_liveprep_v26_profile
                    or step5d_step5b_speedl_live_profile
                    or step5d_no_contact_p0_profile
                ) and step5d_stage25_control_mode != "speedl_cartesian_oracle":
                    step5d_outer_xdot_joint_feasible, step5d_xdot_feasibility_diagnostics = (
                        scale_step5d_xdot_for_joint_feasibility(
                            step5d_outer_xdot_limited,
                            jacobian,
                            qdot_cap_rad_s=float(args.step5d_qdot_limit_rad_s),
                            safety=0.9,
                        )
                    )
                else:
                    step5d_xdot_feasibility_diagnostics.update(
                        {
                            "xdot_norm_pre_feasibility_scale": float(np.linalg.norm(step5d_outer_xdot_limited)),
                            "xdot_norm_post_feasibility_scale": float(np.linalg.norm(step5d_outer_xdot_limited)),
                        }
                    )
                target_state["xdot_c"] = step5d_outer_xdot_joint_feasible
                if step5d_v30_contract_profile:
                    reaction_v30 = tuple(float(value) for value in n_control_b)
                    observation_v30 = Step5dObservation(
                        sequence=state.step5d_v30_sequence,
                        timestamp_s=time.monotonic(),
                        q=tuple(float(value) for value in q),  # type: ignore[arg-type]
                        qd=tuple(float(value) for value in qd),  # type: ignore[arg-type]
                        tcp_pose=tuple(float(value) for value in pose[:6]),  # type: ignore[arg-type]
                        tcp_twist=tuple(float(value) for value in speed[:6]),  # type: ignore[arg-type]
                        wrench=tuple(float(value) for value in latest_zeroed[:6]),  # type: ignore[arg-type]
                        jacobian=tuple(  # type: ignore[arg-type]
                            tuple(float(value) for value in row) for row in jacobian
                        ),
                        desired_twist=tuple(  # type: ignore[arg-type]
                            float(value) for value in step5d_outer_xdot_joint_feasible
                        ),
                        reaction_normal=reaction_v30,  # type: ignore[arg-type]
                        approach_normal=tuple(  # type: ignore[arg-type]
                            -float(value) for value in n_control_b
                        ),
                        command_frame="base",
                        normal_frame="base",
                        path_time_s=float(progress),
                        force_error_n=float(step5d_outer_output.diagnostics["e_f"]),
                        orientation_error_rad=float(
                            step5d_outer_output.diagnostics["outer_orientation_angle_rad"]
                        ),
                        omega_minus=tuple(float(value) for value in omega_minus),  # type: ignore[arg-type]
                        omega_plus=tuple(float(value) for value in omega_plus),  # type: ignore[arg-type]
                        dt_s=float(dt_s),
                    )
                try:
                    if step5d_v30_contract_profile:
                        if state.step5d_v30_policy is None or observation_v30 is None:
                            raise RuntimeError("v30 ControlPolicy was not prewarmed")
                        if state.step5d_v30_deferred_diagnostics is None:
                            raise RuntimeError("v30 deferred diagnostics were not preallocated")

                        def prepare_v30_policy(governed: Step5dObservation) -> None:
                            if (
                                step5d_stage25_control_mode == "speedj_rnn_live"
                                and apply_step5d_solver_warm_start_if_pending(
                                    state,
                                    jacobian=np.asarray(governed.jacobian, dtype=float),
                                    xdot_c=np.asarray(governed.desired_twist, dtype=float),
                                    omega_minus=np.asarray(governed.omega_minus, dtype=float),
                                    omega_plus=np.asarray(governed.omega_plus, dtype=float),
                                )
                            ):
                                step5d_intervention_reasons.append("solver_warm_start")

                        control_step_v30 = step5d_v30_bridge_control_step(
                            observation_v30,
                            state.step5d_v30_policy,
                            previous_qdot=state.step5d_last_qdot,
                            safety_envelope=state.step5d_v30_safety_envelope,
                            deferred_diagnostics=state.step5d_v30_deferred_diagnostics,
                            prepare_policy=prepare_v30_policy,
                        )
                        raw_candidate_v30 = control_step_v30.raw_candidate
                        candidate_v30 = control_step_v30.candidate
                        dls_shadow_v30 = control_step_v30.dls_shadow
                        decision_v30 = control_step_v30.decision
                        step5d_v30_register_command = control_step_v30.register_command
                        raw_diagnostics = dict(raw_candidate_v30.diagnostics)
                        raw_diagnostics.setdefault("active_bounds_mask", (False,) * 6)
                        raw_diagnostics.setdefault("proj_input_form", "unavailable_fail_closed")
                        raw_diagnostics.setdefault("lambda_update_form", "unavailable_fail_closed")
                        try:
                            bridge_solver_status = float(raw_candidate_v30.solver_status)
                        except (TypeError, ValueError):
                            bridge_solver_status = STATUS_INVALID
                        step5d_result = SimpleNamespace(
                            qdot=raw_candidate_v30.qdot,
                            solver_status=bridge_solver_status,
                            residual_norm=raw_candidate_v30.residual_norm,
                            diagnostics=raw_diagnostics,
                        )
                    else:
                        if step5d_stage25_control_mode == "speedj_rnn_live" and apply_step5d_solver_warm_start_if_pending(
                            state,
                            jacobian=jacobian,
                            xdot_c=np.asarray(target_state["xdot_c"], dtype=float),
                            omega_minus=omega_minus,
                            omega_plus=omega_plus,
                        ):
                            step5d_intervention_reasons.append("solver_warm_start")
                        step5d_result = state.step5d_solver.solve(
                            actual_q=q,
                            actual_qd=qd,
                            target_state=target_state,
                        )
                except (ValueError, RuntimeError) as exc:
                    if step5d_ablation_profile and step5d_stage25_control_mode == "speedl_cartesian_oracle":
                        values["_step5d_solver_error"] = f"shadow: {exc}"
                        step5d_result = None
                    else:
                        raise
                if step5d_result is not None:
                    step5d_qdot_command = step5d_result.qdot
                    step5d_raw_qdot_command = tuple(float(value) for value in step5d_result.qdot)
                    step5d_post_slew_qdot_command = step5d_raw_qdot_command
                if (
                    not step5d_v30_contract_profile
                    and (
                        step5d_liveprep_v12_profile
                        or step5d_liveprep_v13_profile
                        or step5d_liveprep_v14_profile
                        or step5d_liveprep_v15_profile
                        or (step5d_liveprep_online_cage_profile and not step5d_ablation_profile)
                        or (
                            (step5d_liveprep_v26_profile or step5d_step5b_speedl_live_profile)
                            and step5d_stage25_control_mode != "speedl_cartesian_oracle"
                        )
                    )
                ):
                    if step5d_result is not None:
                        qdot_limited, step5d_qdot_slew_limiter_active = limit_step5d_qdot_slew(
                            step5d_result.qdot,
                            state.step5d_last_qdot,
                            dt_s=dt_s,
                            preserve_delta_direction=(
                                step5d_liveprep_v29_profile
                                or step5d_v30_contract_profile
                            ),
                        )
                        step5d_qdot_command = tuple(float(value) for value in qdot_limited.tolist())
                        step5d_post_slew_qdot_command = step5d_qdot_command
                        if step5d_qdot_slew_limiter_active:
                            step5d_intervention_reasons.append("qdot_slew_limited")
                        state.step5d_last_qdot = qdot_limited
                if (
                    step5d_contact_safety_profile
                    and step5d_qdot_command is not None
                    and not (
                        step5d_ablation_profile
                        and step5d_stage25_control_mode == "speedl_cartesian_oracle"
                    )
                ):
                    if (
                        v19_freeze_low_force_reacquire
                        or v20_low_load_active_reacquire
                    ):
                        qdot_capped, step5d_reacquire_speed_cap_original_m_s, step5d_reacquire_speed_cap_active = (
                            limit_step5d_predicted_tcp_speed(
                                step5d_qdot_command,
                                jacobian,
                                max_tcp_speed_m_s=STEP5D_V19_REACQUIRE_PREDICTED_TCP_SPEED_CAP_M_S,
                            )
                        )
                        if step5d_reacquire_speed_cap_active:
                            step5d_qdot_command = tuple(float(value) for value in qdot_capped.tolist())
                            step5d_intervention_reasons.append("active_reacquire_speed_cap")
                            state.step5d_last_qdot = qdot_capped
                    predicted_twist = jacobian @ np.asarray(step5d_qdot_command, dtype=float)
                    step5d_predicted_twist = predicted_twist
                    step5d_predicted_tcp_speed_m_s = float(np.linalg.norm(predicted_twist[:3]))
                    if step5d_liveprep_online_cage_profile:
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
                    if step5d_liveprep_v23_profile or step5d_liveprep_v24_profile:
                        step5d_post_rnn_normal_guard = step5d_post_rnn_normal_direction_guard(
                            qdot=step5d_qdot_command,
                            jacobian=jacobian,
                            reaction_normal_b=n_control_b,
                            actual_tcp_speed_b=speed[:3],
                            normal_load_n=normal_load_n,
                            force_norm_n=force_abs,
                            previous_normal_load_n=state.step5d_normal_direction_prev_load_n,
                            prior_dwell_s=state.step5d_normal_direction_guard_dwell_s,
                            dt_s=dt_s,
                        )
                        state.step5d_normal_direction_guard_dwell_s = float(step5d_post_rnn_normal_guard["dwell_s"])
                        state.step5d_normal_direction_prev_load_n = normal_load_n
                        if step5d_liveprep_v24_profile and step5d_post_rnn_normal_guard["action"] == "pass_solver":
                            step5d_post_rnn_normal_guard = step5d_post_rnn_tracking_guard(
                                qdot=step5d_qdot_command,
                                jacobian=jacobian,
                                outer_xdot_limited=step5d_outer_xdot_limited,
                                reaction_normal_b=n_control_b,
                                actual_tcp_speed_b=speed[:3],
                                prior_dwell_s=state.step5d_tracking_guard_dwell_s,
                                dt_s=dt_s,
                            )
                            state.step5d_tracking_guard_dwell_s = float(step5d_post_rnn_normal_guard["dwell_s"])
                        elif step5d_liveprep_v24_profile:
                            state.step5d_tracking_guard_dwell_s = 0.0
                    if (step5d_liveprep_v23_profile or step5d_liveprep_v24_profile) and step5d_post_rnn_normal_guard["action"] in {"hold_zero_qdot", "stop_zero_qdot"}:
                        step5d_contact_safety = {
                            "state": step5d_post_rnn_normal_guard["state"],
                            "action": step5d_post_rnn_normal_guard["action"],
                            "reason": step5d_post_rnn_normal_guard["reason"],
                            "hold_s": state.step5d_contact_hold_s + min(max(0.0, dt_s), STEP5D_V12_GUARD_DT_MAX_S),
                            "high_window_s": state.step5d_contact_high_window_s,
                            "actual_speed_violation_s": state.step5d_actual_speed_violation_s,
                            "actual_speed_violation_count": state.step5d_actual_speed_violation_count,
                            "consecutive_hold_s": state.step5d_consecutive_hold_s,
                            "total_hold_s": state.step5d_total_hold_s,
                            "hold_event_count": state.step5d_hold_event_count,
                            "repeated_hold_count": state.step5d_repeated_hold_count,
                            "last_hold_reason": step5d_post_rnn_normal_guard["reason"],
                            "hold_actual_tcp_speed_m_s": step5d_line_tcp_speed_m_s,
                        }
                    else:
                        step5d_contact_safety = step5d_contact_safety_fn(
                            normal_load_n=normal_load_n,
                            force_norm_n=force_abs,
                            actual_tcp_speed_m_s=step5d_line_tcp_speed_m_s,
                            predicted_tcp_speed_m_s=step5d_predicted_tcp_speed_m_s,
                            braking_margin_m=(
                                float(step5d_tcp_cage["braking_margin_m"])
                                if step5d_liveprep_online_cage_profile
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
                            soft_low_load_n=STEP5D_V18_SOFT_LOW_LOAD_N if step5d_liveprep_v18_or_newer_profile else STEP5D_V16_SOFT_LOW_LOAD_N if step5d_liveprep_v16_or_v17_profile else STEP5D_V13_SOFT_LOW_LOAD_N,
                            valid_contact_min_n=STEP5D_V16_VALID_CONTACT_MIN_N if (step5d_liveprep_v16_or_v17_profile or step5d_liveprep_v18_or_newer_profile) else STEP5D_V13_VALID_CONTACT_MIN_N,
                            valid_contact_max_n=STEP5D_V18_VALID_CONTACT_MAX_N if step5d_liveprep_v18_or_newer_profile else STEP5D_V16_VALID_CONTACT_MAX_N if step5d_liveprep_v16_or_v17_profile else STEP5D_V13_VALID_CONTACT_MAX_N,
                            low_load_speed_load_n=STEP5D_V16_LOW_LOAD_SPEED_LOAD_N if (step5d_liveprep_v16_or_v17_profile or step5d_liveprep_v18_or_newer_profile) else STEP5D_V13_LOW_LOAD_SPEED_LOAD_N,
                            hold_timeout_s=STEP5D_V24_LOW_LOAD_HOLD_TIMEOUT_S if step5d_liveprep_v24_profile else STEP5D_V16_LOW_LOAD_HOLD_TIMEOUT_S if step5d_liveprep_v16_or_v17_profile else STEP5D_V13_LOW_LOAD_HOLD_TIMEOUT_S,
                            high_window_dwell_stop_s=STEP5D_V16_HIGH_WINDOW_DWELL_STOP_S if (step5d_liveprep_v16_or_v17_profile or step5d_liveprep_v18_or_newer_profile) else STEP5D_V13_HIGH_WINDOW_DWELL_STOP_S,
                            allow_high_contact_below_hard_force=step5d_liveprep_v18_or_newer_profile or not step5d_liveprep_v16_or_v17_profile,
                            force_norm_hard_stop_n=STEP5D_V27_SENSOR_FORCE_HARD_STOP_N if step5d_step5b_speedl_live_profile else STEP5D_V24_SENSOR_FORCE_HARD_STOP_N if (step5d_liveprep_v24_profile or step5d_ablation_profile) else STEP5D_V18_SENSOR_FORCE_HARD_STOP_N if step5d_liveprep_v18_or_newer_profile else 60.0,
                            cage_primary_low_load_reacquire=step5d_liveprep_v18_or_newer_profile and not (step5d_liveprep_v24_profile or step5d_ablation_profile),
                            defer_low_load_hold_timeout=not (step5d_liveprep_v24_profile or step5d_ablation_profile),
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
                    if step5d_contact_safety["action"] == "active_reacquire_solver":
                        safe_dt_s = max(0.0, min(float(dt_s), STEP5D_V12_GUARD_DT_MAX_S))
                        state.step5d_active_reacquire_s += safe_dt_s
                        if normal_load_n <= STEP5D_V18_NO_CONTACT_LOAD_N:
                            state.step5d_no_contact_s += safe_dt_s
                        if state.step5d_contact_hold_path_time_s is None:
                            state.step5d_contact_hold_path_time_s = max(0.0, state.line_stage_s - max(0.0, dt_s))
                        state.line_stage_s = state.step5d_contact_hold_path_time_s
                    elif step5d_contact_safety["action"] in {"hold_zero_qdot", "stop_zero_qdot"}:
                        step5d_intervention_reasons.append(f"contact_safety:{step5d_contact_safety['reason']}")
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
                if (
                    step5d_no_contact_p0_profile
                    and not step5d_no_contact_p0_v8_profile
                    and step5d_stage25_control_mode == "speedj_rnn_live"
                    and step5d_qdot_command is not None
                    and step5d_outer_xdot_limited is not None
                    and step5d_result is not None
                ):
                    p0_qdot_limited, p0_qdot_limit_diagnostics = limit_step5d_no_contact_p0_qdot_command(
                        step5d_qdot_command,
                        jacobian,
                        qdot_cap_rad_s=float(args.step5d_qdot_limit_rad_s),
                        max_angular_rad_s=float(args.bridge_angular_limit_rad_s),
                    )
                    p0_candidate_qdot = tuple(float(value) for value in p0_qdot_limited.tolist())
                    step5d_qdot_command = p0_candidate_qdot
                    step5d_post_slew_qdot_command = p0_candidate_qdot
                    step5d_predicted_twist = p0_qdot_limit_diagnostics["limited_predicted_twist"]
                    step5d_predicted_tcp_speed_m_s = float(np.linalg.norm(step5d_predicted_twist[:3]))
                    if p0_qdot_limit_diagnostics["qdot_clip_active"]:
                        step5d_intervention_reasons.append("no_contact_p0_qdot_clipped")
                    if p0_qdot_limit_diagnostics["component_clamp_active"]:
                        step5d_intervention_reasons.append("no_contact_p0_component_velocity_clamped")
                    p0_qdot_gate = step5d_no_contact_p0_qdot_acceptance_gate(
                        qdot=step5d_qdot_command,
                        jacobian=jacobian,
                        outer_xdot_limited=step5d_outer_xdot_limited,
                        reaction_normal_b=n_control_b,
                        residual_norm=float(step5d_result.residual_norm),
                        active_bounds_count=sum(bool(value) for value in step5d_result.diagnostics["active_bounds_mask"]),
                        max_tcp_speed_m_s=float(args.bridge_total_linear_limit_m_s),
                        max_normal_tracking_error_m_s=5e-4,
                        max_residual_norm=1e-3,
                    )
                    if bool(p0_qdot_gate["accepted"]):
                        step5d_p0_rnn_accepted = 1.0
                        step5d_p0_rnn_reject_reason = "ok"
                        step5d_p0_safe_hold_active = 0.0
                        step5d_rnn_accepted = 1.0
                        step5d_rnn_reject_reason = "ok"
                        step5d_safe_hold_active = 0.0
                        step5d_cmd_valid_reason = "rnn_accepted"
                        step5d_qdot_command = tuple(float(value) for value in p0_qdot_gate["qdot"])
                    else:
                        p0_limited_base = np.asarray(
                            step5d_p0_frame_diagnostics.get("limited_base", np.full(6, math.nan, dtype=float)),
                            dtype=float,
                        )
                        p0_tcp_press_speed = float(
                            step5d_p0_frame_diagnostics.get("tcp_press_speed_m_s", math.nan)
                        )
                        p0_frame_escape = (
                            p0_qdot_gate["reason"]
                            in {"outer_approach_not_pressing", "base_upward_escape"}
                            or (math.isfinite(p0_tcp_press_speed) and p0_tcp_press_speed <= 1e-12)
                            or (
                                p0_limited_base.shape == (6,)
                                and math.isfinite(float(p0_limited_base[2]))
                                and float(p0_limited_base[2]) > 1e-6
                            )
                        )
                        if p0_frame_escape:
                            step5d_intervention_reasons.append("p0_frame_transform:tcp_unload_or_upward_escape")
                        step5d_intervention_reasons.append(f"no_contact_p0_evidence:{p0_qdot_gate['reason']}")
                        step5d_rejected_rnn_diagnostics = dict(step5d_result.diagnostics)
                        step5d_rejected_solver_status = float(step5d_result.solver_status)
                        step5d_rejected_residual_norm = float(step5d_result.residual_norm)
                        step5d_rejected_active_bounds_count = float(
                            sum(bool(value) for value in step5d_result.diagnostics["active_bounds_mask"])
                        )
                        step5d_p0_rnn_accepted = 0.0
                        step5d_p0_rnn_reject_reason = str(p0_qdot_gate["reason"])
                        step5d_rnn_accepted = 0.0
                        step5d_rnn_reject_reason = str(p0_qdot_gate["reason"])
                        step5d_qdot_command = tuple(float(value) for value in p0_qdot_gate["qdot"])
                        if p0_qdot_gate.get("motion_action") == "evidence_rejected_continue":
                            step5d_p0_safe_hold_active = 1.0
                            step5d_safe_hold_active = 1.0
                            step5d_cmd_valid_reason = "p0_evidence_rejected_continue"
                        else:
                            step5d_p0_safe_hold_active = 0.0
                            step5d_safe_hold_active = 0.0
                            step5d_cmd_valid_reason = "p0_invalid_reject"
                            step5d_qdot_command = None
                if (
                    step5d_v30_contract_profile
                    and step5d_stage25_control_mode == "speedj_rnn_live"
                    and step5d_qdot_command is not None
                    and step5d_outer_xdot_joint_feasible is not None
                    and step5d_outer_output is not None
                    and step5d_result is not None
                ):
                    if (
                        observation_v30 is None
                        or raw_candidate_v30 is None
                        or candidate_v30 is None
                        or decision_v30 is None
                        or step5d_v30_register_command is None
                        or control_step_v30 is None
                    ):
                        raise RuntimeError(
                            "v30 shared fail-closed control step was not executed"
                        )
                    step5d_qdot_slew_limiter_active = bool(
                        candidate_v30.diagnostics.get("slew_active", False)
                    )
                    state.step5d_v30_sequence += 1
                    step5d_qdot_command = step5d_v30_register_command.qdot
                    step5d_post_slew_qdot_command = step5d_qdot_command
                    step5d_predicted_twist = np.asarray(
                        candidate_v30.predicted_twist, dtype=float
                    )
                    step5d_predicted_tcp_speed_m_s = float(
                        np.linalg.norm(step5d_predicted_twist[:3])
                    )
                    if decision_v30.accepted:
                        state.step5d_last_qdot = np.asarray(
                            decision_v30.qdot,
                            dtype=float,
                        )
                        step5d_rnn_accepted = 1.0
                        step5d_rnn_reject_reason = "ok"
                        step5d_safe_hold_active = 0.0
                        step5d_cmd_valid_reason = "rnn_accepted"
                    else:
                        state.step5d_last_qdot = None
                        step5d_intervention_reasons.append(
                            f"v30_contract:{decision_v30.reason}"
                        )
                        step5d_rnn_accepted = 0.0
                        step5d_rnn_reject_reason = decision_v30.reason
                        step5d_safe_hold_active = (
                            1.0 if decision_v30.action == "safe_hold" else 0.0
                        )
                        step5d_cmd_valid_reason = (
                            "rnn_evidence_rejected_safe_hold"
                            if decision_v30.action == "safe_hold"
                            else "rnn_invalid_reject"
                        )
                        if decision_v30.action == "stop":
                            step5d_contact_safety_stop = True
                            step5d_engage_gate_ok = False
                    if step5d_no_contact_p0_v8_profile:
                        step5d_p0_rnn_accepted = step5d_rnn_accepted
                        step5d_p0_rnn_reject_reason = step5d_rnn_reject_reason
                        step5d_p0_safe_hold_active = step5d_safe_hold_active
                        if not decision_v30.accepted:
                            step5d_intervention_reasons.append(
                                f"p0_v8_contract:{decision_v30.reason}"
                            )
                if (
                    step5d_liveprep_v29_profile
                    and step5d_stage25_control_mode == "speedj_rnn_live"
                    and step5d_qdot_command is not None
                    and step5d_outer_xdot_limited is not None
                    and step5d_result is not None
                ):
                    v29_qdot_gate = step5d_v29_rnn_qdot_acceptance_gate(
                        qdot=step5d_qdot_command,
                        jacobian=jacobian,
                        outer_xdot_limited=step5d_outer_xdot_limited,
                        reaction_normal_b=n_control_b,
                        residual_norm=float(step5d_result.residual_norm),
                        active_bounds_count=sum(bool(value) for value in step5d_result.diagnostics["active_bounds_mask"]),
                    )
                    if bool(v29_qdot_gate["accepted"]):
                        step5d_rnn_accepted = 1.0
                        step5d_rnn_reject_reason = "ok"
                        step5d_safe_hold_active = 0.0
                        step5d_cmd_valid_reason = "rnn_accepted"
                        step5d_qdot_command = tuple(float(value) for value in v29_qdot_gate["qdot"])
                    else:
                        step5d_intervention_reasons.append(f"rnn_evidence:{v29_qdot_gate['reason']}")
                        step5d_rejected_rnn_diagnostics = dict(step5d_result.diagnostics)
                        step5d_rejected_solver_status = float(step5d_result.solver_status)
                        step5d_rejected_residual_norm = float(step5d_result.residual_norm)
                        step5d_rejected_active_bounds_count = float(
                            sum(bool(value) for value in step5d_result.diagnostics["active_bounds_mask"])
                        )
                        step5d_rnn_accepted = 0.0
                        step5d_rnn_reject_reason = str(v29_qdot_gate["reason"])
                        if v29_qdot_gate.get("motion_action") == "evidence_rejected_safe_hold":
                            step5d_safe_hold_active = 1.0
                            step5d_cmd_valid_reason = "rnn_evidence_rejected_safe_hold"
                            step5d_qdot_command = tuple(float(value) for value in v29_qdot_gate["qdot"])
                            step5d_post_slew_qdot_command = step5d_qdot_command
                            state.step5d_last_qdot = None
                        else:
                            step5d_safe_hold_active = 0.0
                            step5d_cmd_valid_reason = "rnn_invalid_reject"
                            step5d_qdot_command = None
                if step5d_ablation_profile:
                    if step5d_stage25_control_mode == "speedl_cartesian_oracle":
                        raw_stage25_command = tuple(float(value) for value in step5d_outer_xdot_limited.tolist())
                        step5d_stage25_command = raw_stage25_command
                        step5d_stage25_layout_tag = STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE
                        if (
                            step5d_step5b_speedl_live_profile
                            and STEP5D_ABLATION_SPEEDL_ORIENTATION_SHADOW_ONLY
                        ):
                            step5d_speedl_shadow_raw_linear_cmd = (
                                raw_stage25_command[0],
                                raw_stage25_command[1],
                                raw_stage25_command[2],
                            )
                            step5d_speedl_shadow_raw_angular_cmd = (
                                raw_stage25_command[3],
                                raw_stage25_command[4],
                                raw_stage25_command[5],
                            )
                            step5d_speedl_orientation_shadow_only = True
                            step5d_live_control_source = STEP5D_V27_SPEEDL_LIVE_CONTROL_SOURCE
                            if STEP5D_V28_LIVE_STEP5B_ORIENTATION:
                                live_angular = (
                                    orientation_cmd[0],
                                    orientation_cmd[1],
                                    orientation_cmd[2],
                                )
                            else:
                                live_angular = (0.0, 0.0, 0.0)
                            step5d_stage25_command = (
                                cmd[0],
                                cmd[1],
                                cmd[2],
                                live_angular[0],
                                live_angular[1],
                                live_angular[2],
                            )
                            step5d_intervention_reasons.append("stage25_step5b_speedl_live_step5d_shadow")
                    elif step5d_stage25_control_mode == "speedj_dls_oracle":
                        step5d_stage25_command = step5d_dls_qdot_oracle(
                            jacobian,
                            step5d_outer_xdot_joint_feasible,
                            omega_minus,
                            omega_plus,
                        )
                        step5d_qdot_command = step5d_stage25_command
                        step5d_stage25_layout_tag = STEP5D_STAGE25_JOINT_LAYOUT_CODE
                    elif step5d_stage25_control_mode == "speedj_rnn_live":
                        if step5d_qdot_command is None:
                            raise RuntimeError("Step5d ablation RNN live mode has no qdot command")
                        if math.isnan(float(step5d_rnn_accepted)):
                            step5d_rnn_accepted = 1.0
                            step5d_rnn_reject_reason = "ok"
                            step5d_safe_hold_active = 0.0
                            step5d_cmd_valid_reason = "rnn_accepted"
                        step5d_stage25_command = tuple(float(value) for value in step5d_qdot_command)
                        step5d_stage25_layout_tag = STEP5D_STAGE25_JOINT_LAYOUT_CODE
                    else:
                        raise ValueError(f"unsupported Step5d ablation Stage25 control mode: {step5d_stage25_control_mode}")
                    cmd = (step5d_stage25_command[0], step5d_stage25_command[1], step5d_stage25_command[2])
                    orientation_cmd = (
                        step5d_stage25_command[3],
                        step5d_stage25_command[4],
                        step5d_stage25_command[5],
                    )
                else:
                    if step5d_qdot_command is None:
                        raise RuntimeError("Step5d liveprep qdot command is unavailable")
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
            if step5d_no_contact_p0_profile and not step5d_no_contact_p0_v8_profile:
                values.update(
                    step5d_stage25_register_values(
                        step5d_stage25_command
                        if step5d_stage25_command is not None
                        else tuple(float(value) for value in (
                            step5d_qdot_command if step5d_qdot_command is not None else (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
                        )),
                        layout_tag=step5d_stage25_layout_tag,
                        cmd_valid=0.0
                        if args.bridge_mode == "preview"
                        or step5d_result is None
                        or step5d_outer_output is None
                        or step5d_stage25_command is None
                        else 1.0,
                        path_time_s=progress,
                        force_error_n=register_force_error,
                        pose_or_orientation_error=register_pose_error,
                    )
                )
            elif (
                step5d_v30_contract_profile
                and step5d_v30_register_command is not None
            ):
                values.update(
                    step5d_stage25_register_values(
                        step5d_v30_register_command.qdot,
                        layout_tag=step5d_v30_register_command.layout_code,
                        cmd_valid=(
                            0.0
                            if args.bridge_mode == "preview"
                            else 1.0
                            if step5d_v30_register_command.cmd_valid
                            else 0.0
                        ),
                        path_time_s=step5d_v30_register_command.path_time_s,
                        force_error_n=step5d_v30_register_command.force_error_n,
                        pose_or_orientation_error=(
                            step5d_v30_register_command.orientation_error_rad
                        ),
                    )
                )
                values["stop_request"] = (
                    1.0 if step5d_v30_register_command.stop_request else 0.0
                )
            elif step5d_ablation_profile:
                step5d_ablation_stage25_command_ready = step5d_stage25_command is not None
                step5d_ablation_safe_hold_valid = (
                    (
                        step5d_liveprep_v29_profile
                        or step5d_v30_contract_profile
                    )
                    and step5d_stage25_control_mode == "speedj_rnn_live"
                    and step5d_cmd_valid_reason == "rnn_evidence_rejected_safe_hold"
                    and step5d_qdot_command is not None
                    and step5d_outer_output is not None
                )
                values.update(
                    step5d_stage25_register_values(
                        step5d_stage25_command
                        if step5d_stage25_command is not None
                        else tuple(float(value) for value in step5d_qdot_command)
                        if step5d_ablation_safe_hold_valid
                        else (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                        layout_tag=step5d_stage25_layout_tag,
                        cmd_valid=0.0
                        if args.bridge_mode == "preview"
                        or step5d_outer_output is None
                        or not (step5d_ablation_stage25_command_ready or step5d_ablation_safe_hold_valid)
                        else 1.0,
                        path_time_s=progress,
                        force_error_n=register_force_error,
                        pose_or_orientation_error=register_pose_error,
                    )
                )
            else:
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
                (step5d_liveprep_v10_profile or step5d_liveprep_v11_profile or step5d_liveprep_v12_profile or step5d_liveprep_v13_profile or step5d_liveprep_v14_profile or step5d_liveprep_v15_profile or step5d_liveprep_online_cage_profile)
                and line_entry_gate_active
                and state.step5d_settle_filtered_normal_load_n is not None
            ):
                line_entry_register_load_n = state.step5d_settle_filtered_normal_load_n
                if step5d_liveprep_v17_or_newer_profile and not (
                    step5d_entry_raw_sanity_min_n <= normal_load_n <= step5d_entry_raw_sanity_max_n
                ):
                    line_entry_register_load_n = normal_load_n
                register_force_error = float(args.target_force_n) - line_entry_register_load_n
            preload_param_channel_active = (
                (
                    step5d_liveprep_v21_profile
                    or step5d_liveprep_v22_profile
                    or step5d_liveprep_v23_profile
                    or step5d_liveprep_v24_profile
                    or step5d_ablation_profile
                )
                and line_entry_gate_active
            )
            qdot_clear_packet_active = qdot_clear_stage_active
            values.update(
                {
                    "step4e_cmd_vx_m_s": 0.0
                    if qdot_clear_packet_active
                    else n_control_b[0] if (v21_profile and detach_stage_active) else cmd[0],
                    "step4e_cmd_vy_m_s": 0.0
                    if qdot_clear_packet_active
                    else n_control_b[1] if (v21_profile and detach_stage_active) else cmd[1],
                    "step4e_cmd_vz_m_s": 0.0
                    if qdot_clear_packet_active
                    else n_control_b[2] if (v21_profile and detach_stage_active) else cmd[2],
                    "step4e_cmd_wx_rad_s": 0.0
                    if qdot_clear_packet_active
                    else float(args.step5d_preload_filtered_min_n)
                    if preload_param_channel_active
                    else orientation_cmd[0],
                    "step4e_cmd_wy_rad_s": 0.0
                    if qdot_clear_packet_active
                    else float(args.step5d_preload_filtered_max_n)
                    if preload_param_channel_active
                    else orientation_cmd[1],
                    "step4e_cmd_wz_rad_s": 0.0
                    if qdot_clear_packet_active
                    else float(args.step5d_preload_force_norm_max_n)
                    if preload_param_channel_active
                    else orientation_cmd[2]
                    if (axis_iso_active or v21_profile or v22_profile or angular_speedl_profile)
                    else 0.0,
                    "step4e_cmd_valid": 0.0
                    if qdot_clear_packet_active
                    or args.bridge_mode == "preview"
                    or (
                        (step5d_liveprep_v8_profile or step5d_liveprep_v9_profile or step5d_liveprep_v10_profile or step5d_liveprep_v11_profile)
                        and line_entry_gate_active
                        and not step5d_v8_pid_recovery_ok
                    )
                    else 1.0,
                    "step4e_progress_m": 0.0
                    if qdot_clear_packet_active
                    else float(args.step5d_preload_hold_s)
                    if preload_param_channel_active
                    else progress,
                    "step4e_force_error_n": 0.0 if qdot_clear_packet_active else register_force_error,
                    "step4e_orientation_error_rad": 0.0
                    if qdot_clear_packet_active
                    else float(args.step5d_preload_timeout_s)
                    if preload_param_channel_active
                    else orientation_error,
                    "step4e_controller_state": (
                        STEP5D_QDOT_CLEAR_MODE_CODE
                        if qdot_clear_packet_active
                        else STEP5D_LINE_ENTRY_PARAM_VALID_CODE
                        if preload_param_channel_active
                        else
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
        if step5d_joint_line_profile:
            values["_step5d_stage25_control_mode"] = step5d_stage25_control_mode
            values["_step5d_live_control_source"] = step5d_live_control_source
            values["_step5d_stage25_entry_relatch_angle_rad"] = (
                state.step5d_stage25_entry_relatch_angle_rad
                if state.step5d_stage25_entry_relatch_angle_rad is not None
                else math.nan
            )
            values["_step5d_live_orientation_enabled"] = (
                1.0
                if step5d_speedl_orientation_shadow_only and STEP5D_V28_LIVE_STEP5B_ORIENTATION
                else 0.0
            )
            values["_step5d_speedl_orientation_shadow_only"] = 1.0 if step5d_speedl_orientation_shadow_only else 0.0
            values["_step5d_speedl_shadow_raw_vx_m_s"] = (
                step5d_speedl_shadow_raw_linear_cmd[0]
                if step5d_speedl_shadow_raw_linear_cmd is not None
                else math.nan
            )
            values["_step5d_speedl_shadow_raw_vy_m_s"] = (
                step5d_speedl_shadow_raw_linear_cmd[1]
                if step5d_speedl_shadow_raw_linear_cmd is not None
                else math.nan
            )
            values["_step5d_speedl_shadow_raw_vz_m_s"] = (
                step5d_speedl_shadow_raw_linear_cmd[2]
                if step5d_speedl_shadow_raw_linear_cmd is not None
                else math.nan
            )
            values["_step5d_speedl_shadow_raw_wx_rad_s"] = (
                step5d_speedl_shadow_raw_angular_cmd[0]
                if step5d_speedl_shadow_raw_angular_cmd is not None
                else math.nan
            )
            values["_step5d_speedl_shadow_raw_wy_rad_s"] = (
                step5d_speedl_shadow_raw_angular_cmd[1]
                if step5d_speedl_shadow_raw_angular_cmd is not None
                else math.nan
            )
            values["_step5d_speedl_shadow_raw_wz_rad_s"] = (
                step5d_speedl_shadow_raw_angular_cmd[2]
                if step5d_speedl_shadow_raw_angular_cmd is not None
                else math.nan
            )
        if step5d_joint_line_profile and step5d_result is not None and step5d_outer_output is not None:
            qdot_abs = [abs(float(value)) for value in (step5d_qdot_command or step5d_result.qdot)]
            raw_qdot_abs = [abs(float(value)) for value in (step5d_raw_qdot_command or step5d_result.qdot)]
            intervention_reason = "|".join(step5d_intervention_reasons) if step5d_intervention_reasons else "none"
            values.update(
                step5d_qdot_diagnostic_values(
                    jacobian=jacobian,
                    raw_qdot=step5d_raw_qdot_command,
                    post_slew_qdot=step5d_post_slew_qdot_command,
                    final_qdot=step5d_qdot_command or step5d_result.qdot,
                    outer_xdot_limited=step5d_outer_xdot_limited,
                    reaction_normal_b=n_control_b,
                    lambda_state=step5d_result.diagnostics.get("lambda_state"),
                    active_bounds_mask=step5d_result.diagnostics.get("active_bounds_mask"),
                    intervention_reason=intervention_reason,
                    feasibility_diagnostics=step5d_xdot_feasibility_diagnostics,
                )
            )
            values["_step5d_solver_status"] = step5d_result.solver_status
            values["_step5d_qdot_max_abs_rad_s"] = max(qdot_abs)
            values["_step5d_rnn_qdot_max_abs_raw_rad_s"] = max(raw_qdot_abs)
            values["_step5d_qdot_max_abs_after_guard_rad_s"] = max(qdot_abs)
            values["_step5d_constraint_residual_norm"] = step5d_result.residual_norm
            values["_step5d_outer_xdot_norm"] = float(np.linalg.norm(np.asarray(step5d_outer_output.xdot_c, dtype=float)))
            values["_step5d_outer_xdot_limited_norm"] = (
                float(np.linalg.norm(step5d_outer_xdot_limited)) if step5d_outer_xdot_limited is not None else values["_step5d_outer_xdot_norm"]
            )
            values["_step5d_outer_xdot_joint_feasible_norm"] = (
                float(np.linalg.norm(step5d_outer_xdot_joint_feasible))
                if step5d_outer_xdot_joint_feasible is not None
                else values["_step5d_outer_xdot_limited_norm"]
            )
            values["_step5d_outer_xdot_limiter_active"] = 1.0 if step5d_outer_xdot_limiter_active else 0.0
            values["_step5d_qdot_slew_limiter_active"] = 1.0 if step5d_qdot_slew_limiter_active else 0.0
            if candidate_v30 is not None:
                values["_step5d_constraint_residual_norm"] = float(
                    candidate_v30.residual_norm
                )
            values["_step5d_engage_gate_ok"] = 1.0 if step5d_engage_gate_ok else 0.0
            values["_step5d_line_guard_ok"] = 1.0 if step5d_line_guard_ok else 0.0
            values["_step5d_line_guard_loss_s"] = state.step5d_line_guard_loss_s
            values["_step5d_line_guard_reason"] = step5d_line_guard_reason
            values["_step5d_line_tcp_speed_m_s"] = step5d_line_tcp_speed_m_s
            values["_step5d_force_sign_convention"] = step5d_outer_output.diagnostics["force_sign_convention"]
            values["_step5d_proj_input_form"] = step5d_result.diagnostics["proj_input_form"]
            values["_step5d_lambda_update_form"] = step5d_result.diagnostics["lambda_update_form"]
            values["_step5d_rnn_inner_iterations"] = float(step5d_result.diagnostics.get("inner_iterations", math.nan))
            values["_step5d_rnn_backend"] = str(step5d_result.diagnostics.get("backend", ""))
            values["_step5d_rnn_solve_wall_ms"] = float(step5d_result.diagnostics.get("solve_wall_ms", math.nan))
            values["_step5d_rnn_epsilon"] = float(step5d_result.diagnostics.get("epsilon", math.nan))
            values["_step5d_rnn_sigr_exponent_r"] = float(step5d_result.diagnostics.get("sigr_exponent_r", math.nan))
            values["_step5d_active_bounds_count"] = float(sum(bool(value) for value in step5d_result.diagnostics["active_bounds_mask"]))
            if step5d_stage25_control_mode == "speedj_rnn_live":
                values["_step5d_rnn_accepted"] = step5d_rnn_accepted
                values["_step5d_rnn_reject_reason"] = step5d_rnn_reject_reason
                values["_step5d_safe_hold_active"] = step5d_safe_hold_active
                values["_step5d_cmd_valid_reason"] = step5d_cmd_valid_reason
            values["_step5d_p0_low_force_posture_policy"] = str(step5d_p0_posture_policy.get("policy", ""))
            values["_step5d_p0_low_force_posture_active"] = 1.0 if bool(step5d_p0_posture_policy.get("active", False)) else 0.0
            values["_step5d_p0_posture_gain_scale"] = float(step5d_p0_posture_policy.get("orientation_gain_scale", math.nan))
            values["_step5d_p0_effective_ko"] = float(step5d_p0_posture_policy.get("effective_ko", math.nan))
            if step5d_no_contact_p0_profile:
                values.update(step5d_p0_frame_diagnostic_values(step5d_p0_frame_diagnostics))
                values["_step5d_rnn_accepted"] = step5d_rnn_accepted
                values["_step5d_rnn_reject_reason"] = step5d_rnn_reject_reason
                values["_step5d_safe_hold_active"] = step5d_safe_hold_active
                values["_step5d_p0_rnn_accepted"] = step5d_p0_rnn_accepted
                values["_step5d_p0_rnn_reject_reason"] = step5d_p0_rnn_reject_reason
                values["_step5d_p0_safe_hold_active"] = step5d_p0_safe_hold_active
                values["_step5d_p0_v8_contract_active"] = (
                    1.0 if step5d_no_contact_p0_v8_profile else 0.0
                )
                values["_step5d_cmd_valid_reason"] = step5d_cmd_valid_reason
            if dls_shadow_v30 is not None:
                values["_step5d_dls_shadow_present"] = 1.0
                values["_step5d_dls_shadow_runtime_fallback_allowed"] = (
                    1.0 if dls_shadow_v30.runtime_fallback_allowed else 0.0
                )
                values["_step5d_dls_shadow_normal_sign_difference"] = (
                    1.0 if dls_shadow_v30.normal_sign_difference else 0.0
                )
                values["_step5d_dls_shadow_residual_norm"] = float(
                    dls_shadow_v30.residual_norm
                )
                values["_step5d_dls_shadow_saturation_count"] = float(
                    dls_shadow_v30.saturation_count
                )
                values["_step5d_dls_shadow_qdot_delta_norm"] = float(
                    dls_shadow_v30.qdot_delta_norm
                )
                values["_step5d_dls_shadow_twist_delta_norm"] = float(
                    dls_shadow_v30.twist_delta_norm
                )
            values["_step5d_predicted_tcp_vx_m_s"] = float(step5d_predicted_twist[0])
            values["_step5d_predicted_tcp_vy_m_s"] = float(step5d_predicted_twist[1])
            values["_step5d_predicted_tcp_vz_m_s"] = float(step5d_predicted_twist[2])
            values["_step5d_predicted_press_speed_m_s"] = step5d_post_rnn_normal_guard["predicted_press_speed_m_s"]
            values["_step5d_actual_press_speed_m_s"] = step5d_post_rnn_normal_guard["actual_press_speed_m_s"]
            values["_step5d_normal_load_rate_n_s"] = step5d_post_rnn_normal_guard["normal_load_rate_n_s"]
            values["_step5d_post_rnn_normal_guard_state"] = step5d_post_rnn_normal_guard["state"]
            values["_step5d_post_rnn_normal_guard_action"] = step5d_post_rnn_normal_guard["action"]
            values["_step5d_post_rnn_normal_guard_reason"] = step5d_post_rnn_normal_guard["reason"]
            values["_step5d_normal_direction_guard_dwell_s"] = step5d_post_rnn_normal_guard["dwell_s"]
            values["_step5d_normal_direction_guard_zeroed_qdot"] = 1.0 if step5d_post_rnn_normal_guard["zeroed_qdot"] else 0.0
            values["_step5d_contact_orientation_error_rad"] = orientation_error
            values["_step5d_outer_orientation_error_rad"] = float(step5d_outer_output.diagnostics["outer_orientation_angle_rad"])
            values["_step5d_R_d_z_dot_R_cur_z"] = float(step5d_outer_output.diagnostics["R_d_z_dot_R_cur_z"])
            values["_step5d_semantic_gate_ok"] = 1.0
        elif step5d_joint_line_profile:
            if step5d_intervention_reasons:
                values["_step5d_intervention_reason"] = "|".join(step5d_intervention_reasons)
            if step5d_no_contact_p0_profile:
                values.update(step5d_p0_frame_diagnostic_values(step5d_p0_frame_diagnostics))
                values["_step5d_rnn_accepted"] = step5d_rnn_accepted
                values["_step5d_rnn_reject_reason"] = step5d_rnn_reject_reason
                values["_step5d_safe_hold_active"] = step5d_safe_hold_active
                values["_step5d_p0_rnn_accepted"] = step5d_p0_rnn_accepted
                values["_step5d_p0_rnn_reject_reason"] = step5d_p0_rnn_reject_reason
                values["_step5d_p0_safe_hold_active"] = step5d_p0_safe_hold_active
                values["_step5d_cmd_valid_reason"] = step5d_cmd_valid_reason
            if step5d_rejected_rnn_diagnostics is not None:
                values["_step5d_solver_status"] = step5d_rejected_solver_status
                values["_step5d_constraint_residual_norm"] = step5d_rejected_residual_norm
                values["_step5d_rnn_inner_iterations"] = float(
                    step5d_rejected_rnn_diagnostics.get("inner_iterations", math.nan)
                )
                values["_step5d_rnn_backend"] = str(step5d_rejected_rnn_diagnostics.get("backend", ""))
                values["_step5d_rnn_solve_wall_ms"] = float(
                    step5d_rejected_rnn_diagnostics.get("solve_wall_ms", math.nan)
                )
                values["_step5d_rnn_epsilon"] = float(step5d_rejected_rnn_diagnostics.get("epsilon", math.nan))
                values["_step5d_rnn_sigr_exponent_r"] = float(
                    step5d_rejected_rnn_diagnostics.get("sigr_exponent_r", math.nan)
                )
                values["_step5d_active_bounds_count"] = step5d_rejected_active_bounds_count
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
            values["_step5d_reacquire_speed_cap_active"] = 1.0 if step5d_reacquire_speed_cap_active else 0.0
            values["_step5d_reacquire_speed_cap_m_s"] = (
                STEP5D_V19_REACQUIRE_PREDICTED_TCP_SPEED_CAP_M_S
                if (
                    step5d_liveprep_v19_profile
                    or step5d_liveprep_v20_profile
                    or step5d_liveprep_v21_profile
                    or step5d_liveprep_v22_profile
                    or step5d_liveprep_v23_profile
                    or step5d_liveprep_v24_profile
                )
                else float("nan")
            )
            values["_step5d_reacquire_speed_cap_original_m_s"] = step5d_reacquire_speed_cap_original_m_s
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
            values["_step5d_active_reacquire_s"] = state.step5d_active_reacquire_s
            values["_step5d_no_contact_s"] = state.step5d_no_contact_s
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
        values["_step5d_search_pose_contract_active"] = 1.0 if step5d_search_pose_contract_active else 0.0
        values["_step5d_search_pose_contract_ok"] = 1.0 if step5d_search_pose_contract_ok else 0.0
        values["_step5d_search_pose_contract_axis_error_rad"] = step5d_search_pose_contract_axis_error_rad
        values["_step5d_search_pose_contract_tcp_z_dot_down"] = dot3(tcp_z_axis_b, STEP5D_SEARCH_POSE_TARGET_AXIS_B)
        values["_step5d_force_settle_filtered_normal_load_n"] = (
            state.step5d_settle_filtered_normal_load_n
            if state.step5d_settle_filtered_normal_load_n is not None
            else float("nan")
        )
        values["_step5d_force_settle_velocity_m_s"] = state.normal_velocity_m_s
        if step5d_liveprep_v10_profile or step5d_liveprep_v11_profile or step5d_liveprep_v12_profile or step5d_liveprep_v17_or_newer_profile:
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
                    not step5d_liveprep_v17_or_newer_profile
                    or STEP5D_V17_ENTRY_RAW_NORMAL_LOAD_MIN_N <= normal_load_n <= step5d_entry_raw_sanity_max_n
                )
                and (
                    step5d_liveprep_v11_profile
                    or step5d_liveprep_v12_profile
                    or step5d_liveprep_v13_profile
                    or step5d_liveprep_v14_profile
                    or step5d_liveprep_v15_profile
                    or step5d_liveprep_online_cage_profile
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
            STEP5D_LIVEPREP_V16_STAGE_ID,
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
    if step5d_no_contact_p0_v8_profile:
        canary_phase_s = float(args.step5d_stop_register_canary_s)
        values["_step5d_p0_v8_contract_active"] = 1.0
        values["_step5d_p0_v8_canary_phase_s"] = canary_phase_s
        values["_step5d_p0_v8_canary_stop_active"] = 0.0
        if (
            canary_phase_s > 0.0
            and line_stage_active
            and state.step5d_active_stage25_s >= canary_phase_s
        ):
            values.update(
                step5d_stage25_register_values(
                    (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                    layout_tag=STEP5D_STAGE25_JOINT_LAYOUT_CODE,
                    cmd_valid=1.0,
                    path_time_s=state.step5d_active_stage25_s,
                    force_error_n=force_error,
                    pose_or_orientation_error=orientation_error,
                )
            )
            values["stop_request"] = 1.0
            values["_step5d_p0_v8_canary_stop_active"] = 1.0
            values["_step5d_contact_safety_reason"] = (
                f"p0_v8_canary_{canary_phase_s:g}s_complete"
            )
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


def open_rtde_bridge(
    args: argparse.Namespace,
    *,
    timeout_s: float | None = None,
) -> tuple[RTDEBridgeClient, int, list[str], int, list[str]]:
    rtde = RTDEBridgeClient(args.robot_host, timeout=args.connect_timeout_s if timeout_s is None else timeout_s)
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


def apply_v29_fail_stop(bridge_values: dict[str, float]) -> None:
    """Select TP reason 3 and clear every motion carrier for a v29 safety stop."""
    bridge_values["sensor_ok"] = 0.0
    bridge_values["stop_request"] = 0.0
    for name in BRIDGE_INPUT_NAMES[:6]:
        bridge_values[name] = 0.0
    bridge_values["step4e_cmd_valid"] = 0.0


def apply_step5d_deadline_overrun_hold(bridge_values: dict[str, float]) -> None:
    """Publish a same-heartbeat exact-zero packet after a missed host deadline.

    The matching v30/P0 TP program treats an unchanged heartbeat as a stale
    packet, reports command-consumed=0, and executes zero qdot.  Keeping the
    layout and cmd-valid carriers explicit prevents a late candidate from
    leaking through any alternate interpretation; this is not a timing-pass
    claim and does not relax the hard 2 ms gate.
    """

    for name in BRIDGE_INPUT_NAMES[:6]:
        bridge_values[name] = 0.0
    bridge_values["step4e_cmd_valid"] = 1.0
    bridge_values["step4e_controller_state"] = STEP5D_STAGE25_JOINT_LAYOUT_CODE


def finalize_step5d_publish_history(
    state: "BridgeState",
    *,
    v30_contract_profile: bool,
    deadline_overrun_hold_active: bool,
    rtde_send_succeeded: bool,
) -> bool:
    """Commit v30 command history only after a fresh RTDE packet is sent.

    ``compute_bridge_values`` necessarily computes the next candidate before
    the RTDE write and therefore updates the in-memory solver/qdot history
    optimistically.  A deadline hold publishes a repeated-heartbeat zero
    packet, while a failed or absent RTDE connection publishes nothing.  None
    of those outcomes may become the previous command for the next solve.

    Older profiles intentionally keep their historical bookkeeping semantics;
    this rollback contract is scoped to the inactive v30/P0 control contract.
    """

    if not v30_contract_profile:
        return not deadline_overrun_hold_active
    fresh_candidate_published = bool(
        rtde_send_succeeded and not deadline_overrun_hold_active
    )
    if fresh_candidate_published:
        return True
    if state.step5d_solver is not None:
        state.step5d_solver.reset_state()
    state.step5d_last_qdot = None
    state.step5d_pending_solver_warm_start = True
    return False


def request_v29_fail_stop_dashboard_stop(args: argparse.Namespace) -> dict[str, Any]:
    """Attempt the hard-coded secondary stop channel; never raise into the fail-stop loop."""
    result: dict[str, Any] = {
        "attempted": True,
        "delivered": False,
        "response": "",
        "error": "",
    }
    try:
        response = dashboard_exchange(
            args.robot_host,
            ["stop"],
            timeout=STEP5D_V29_FAIL_STOP_DASHBOARD_TIMEOUT_S,
        )
    except (OSError, RuntimeError, socket.timeout) as exc:
        result["error"] = rtde_error_name(exc)
        return result
    if not isinstance(response, Mapping):
        result["error"] = "invalid_dashboard_response"
        return result
    raw_response = str(response.get("stop") or "").strip()
    normalized = raw_response.lower()
    result["response"] = raw_response
    result["delivered"] = normalized == "stopped" or normalized.startswith("stopped ") or normalized == "stopping"
    return result


def v29_tp_reason3_ack_observed(
    output: Mapping[str, Any] | None,
    *,
    heartbeat_min: float | None,
    heartbeat_max: float | None,
) -> bool:
    if not isinstance(output, Mapping):
        return False
    if heartbeat_min is None or heartbeat_max is None:
        return False
    try:
        echoed_heartbeat = float(output.get("output_double_register_26"))
        echoed_sensor_ok = float(output.get("output_double_register_27"))
        echoed_stop_request = float(output.get("output_double_register_28"))
        stop_reason = float(output.get("output_double_register_30"))
    except (TypeError, ValueError):
        return False
    return (
        heartbeat_min - 1e-6 <= echoed_heartbeat <= heartbeat_max + 1e-6
        and abs(echoed_sensor_ok) <= 1e-6
        and abs(echoed_stop_request) <= 1e-6
        and abs(stop_reason - 3.0) <= 1e-6
    )


def select_v29_fail_stop_reason(
    latched_reason: str | None,
    *,
    hard_guard_reason: str | None,
    step4e_stop_request: bool,
    step4e_guard_reason: str,
) -> str | None:
    if latched_reason is not None:
        return latched_reason
    if hard_guard_reason is not None:
        return hard_guard_reason
    if step4e_stop_request:
        return step4e_guard_reason
    return None


def record_v29_fail_stop_rtde_packet(
    state: dict[str, Any],
    *,
    heartbeat: float,
    output_sequence: int,
) -> None:
    state["rtde_reason3_packets_sent"] += 1
    if state["first_rtde_packet_output_sequence"] is None:
        state["first_rtde_packet_output_sequence"] = output_sequence
        state["heartbeat_min_sent"] = heartbeat
    state["heartbeat_max_sent"] = heartbeat


def update_v29_fail_stop_tp_ack(
    state: dict[str, Any],
    *,
    output: Mapping[str, Any] | None,
    output_sequence: int,
) -> None:
    first_packet_sequence = state["first_rtde_packet_output_sequence"]
    if (
        first_packet_sequence is not None
        and output_sequence > first_packet_sequence
        and v29_tp_reason3_ack_observed(
            output,
            heartbeat_min=state["heartbeat_min_sent"],
            heartbeat_max=state["heartbeat_max_sent"],
        )
    ):
        state["tp_reason3_observed"] = True


def v29_fail_stop_termination_channel(state: Mapping[str, Any]) -> str | None:
    if state.get("tp_reason3_observed") is True:
        return "tp_reason3_echo"
    if state.get("dashboard_stop_delivered") is True:
        return "dashboard_stop_ack"
    return None


def step5b_15n_trial_enabled(args: argparse.Namespace) -> bool:
    return (
        getattr(args, "bridge_profile", "") in STEP5B_BRIDGE_PROFILES
        and getattr(args, "step5b_trial_profile", "none") == STEP5B_15N_TRIAL_PROFILE
    )


def step5b_ramp_trial_enabled(args: argparse.Namespace) -> bool:
    return (
        getattr(args, "bridge_profile", "") in STEP5B_BRIDGE_PROFILES
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
    if args.bridge_profile not in STEP5B_BRIDGE_PROFILES or args.bridge_mode != "line":
        raise SystemExit(f"--step5b-trial-profile {args.step5b_trial_profile} requires Step5b line mode")
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

    normal_stop_n = max(
        STEP5B_FORCE_GUARD_MIN_N,
        min(STEP5B_RAMP_PRELOAD_NORMAL_STOP_N, target + STEP5B_RAMP_NORMAL_STOP_MARGIN_N),
    )
    normal_dwell_n = max(
        STEP5B_FORCE_GUARD_MIN_N,
        min(STEP5B_RAMP_PRELOAD_NORMAL_STOP_N, target + STEP5B_RAMP_NORMAL_DWELL_MARGIN_N),
    )
    force_stop_n = max(
        STEP5B_FORCE_GUARD_MIN_N,
        min(STEP5B_RAMP_PRELOAD_FORCE_STOP_N, target + STEP5B_RAMP_FORCE_STOP_MARGIN_N),
    )
    force_dwell_n = max(
        STEP5B_FORCE_GUARD_MIN_N,
        min(STEP5B_RAMP_PRELOAD_FORCE_STOP_N, target + STEP5B_RAMP_FORCE_DWELL_MARGIN_N),
    )
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
    argv_list = list(argv) if argv is not None else sys.argv[1:]
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
    parser.add_argument("--bias-contact-normal-threshold-n", type=float, default=DEFAULT_BIAS_CONTACT_NORMAL_THRESHOLD_N)
    parser.add_argument("--bias-contact-force-norm-threshold-n", type=float, default=DEFAULT_BIAS_CONTACT_FORCE_NORM_THRESHOLD_N)
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
    parser.add_argument("--step5d-sigr-exponent-r", type=float, default=1.0)
    parser.add_argument("--step5d-rnn-inner-iterations", type=int, default=1)
    parser.add_argument("--step5d-rnn-backend", choices=("numpy", "cupy"), default="numpy")
    parser.add_argument(
        "--step5d-stage25-control-mode",
        default=os.environ.get("STEP5D_STAGE25_CONTROL_MODE", ""),
        help="Step5d v25 Stage25 control mode: speedl_cartesian_oracle, speedj_dls_oracle, or speedj_rnn_live",
    )
    parser.add_argument(
        "--step5d-preload-filtered-min-n",
        type=float,
        default=env_float_alias(
            "STEP5D_PRELOAD_FILTERED_MIN_N",
            "BRIDGE_STEP5D_PRELOAD_FILTERED_MIN_N",
            STEP5D_V21_ENTRY_FILTERED_NORMAL_LOAD_MIN_N,
        ),
    )
    parser.add_argument(
        "--step5d-preload-filtered-max-n",
        type=float,
        default=env_float_alias(
            "STEP5D_PRELOAD_FILTERED_MAX_N",
            "BRIDGE_STEP5D_PRELOAD_FILTERED_MAX_N",
            STEP5D_V21_ENTRY_FILTERED_NORMAL_LOAD_MAX_N,
        ),
    )
    parser.add_argument(
        "--step5d-preload-raw-min-n",
        type=float,
        default=env_float_alias(
            "STEP5D_PRELOAD_RAW_MIN_N",
            "BRIDGE_STEP5D_PRELOAD_RAW_MIN_N",
            STEP5D_V21_ENTRY_RAW_NORMAL_LOAD_MIN_N,
        ),
    )
    parser.add_argument(
        "--step5d-preload-raw-max-n",
        type=float,
        default=env_float_alias(
            "STEP5D_PRELOAD_RAW_MAX_N",
            "BRIDGE_STEP5D_PRELOAD_RAW_MAX_N",
            STEP5D_V21_ENTRY_RAW_NORMAL_LOAD_MAX_N,
        ),
    )
    parser.add_argument(
        "--step5d-preload-force-norm-max-n",
        type=float,
        default=env_float_alias(
            "STEP5D_PRELOAD_FORCE_NORM_MAX_N",
            "BRIDGE_STEP5D_PRELOAD_FORCE_NORM_MAX_N",
            STEP5D_V21_ENTRY_FORCE_NORM_MAX_N,
        ),
    )
    parser.add_argument(
        "--step5d-preload-hold-s",
        type=float,
        default=env_float_alias("STEP5D_PRELOAD_HOLD_S", "BRIDGE_STEP5D_PRELOAD_HOLD_S", STEP5D_V21_ENTRY_REQUIRED_S),
    )
    parser.add_argument(
        "--step5d-preload-timeout-s",
        type=float,
        default=env_float_alias(
            "STEP5D_PRELOAD_TIMEOUT_S",
            "BRIDGE_STEP5D_PRELOAD_TIMEOUT_S",
            STEP5D_V21_ENTRY_TIMEOUT_S,
        ),
    )
    parser.add_argument(
        "--disable-dashboard-program-watch",
        action="store_true",
        help="disable Step5d live-prep Dashboard watchdog that exits after TP stop/play timeout",
    )
    parser.add_argument("--dashboard-program-watch-timeout-s", type=float, default=45.0)
    parser.add_argument(
        "--step5d-stop-register-canary-s",
        type=float,
        default=env_float("STEP5D_STOP_REGISTER_CANARY_S", 0.0),
        help="disabled by default; P0 v8 permits only Stage25 phases 2, 10, or 60 seconds",
    )
    args = parser.parse_args(argv)
    for key, value in list(vars(args).items()):
        if key.startswith("step4e_"):
            setattr(args, f"bridge_{key.removeprefix('step4e_')}", value)
    args.bridge_profile = args.step4e_version
    if not args.step5d_stage25_control_mode:
        args.step5d_stage25_control_mode = (
            "speedj_rnn_live"
            if args.bridge_profile in {
                STEP5D_ABLATION_V29_STAGE_ID,
                STEP5D_ABLATION_V30_STAGE_ID,
                *STEP5D_NO_CONTACT_P0_STAGE_IDS,
            }
            else "speedl_cartesian_oracle"
            if args.bridge_profile in STEP5D_ABLATION_STAGE_IDS
            else "speedj_rnn_live"
        )
    if is_no_contact_p0_stage(args.bridge_profile):
        args.step5d_stage25_control_mode = "speedj_rnn_live"
    if args.step5d_stage25_control_mode not in STEP5D_STAGE25_CONTROL_MODES:
        raise SystemExit(
            "--step5d-stage25-control-mode must be one of "
            f"{', '.join(STEP5D_STAGE25_CONTROL_MODES)}"
        )
    if is_no_contact_p0_stage(args.bridge_profile):
        args.duration_s = STEP5D_NO_CONTACT_P0_DURATION_S
        args.baseline_s = STEP5D_NO_CONTACT_P0_BASELINE_S
        args.rezero_s = STEP5D_NO_CONTACT_P0_REZERO_S
        args.target_force_n = STEP5D_NO_CONTACT_P0_TARGET_FORCE_N
        args.rtde_hz = STEP5D_NO_CONTACT_P0_RTDE_HZ
        args.sensor_stale_s = STEP5D_NO_CONTACT_P0_SENSOR_STALE_S
        args.socket_timeout_s = STEP5D_NO_CONTACT_P0_SOCKET_TIMEOUT_S
        args.bridge_integrate_stage25_only = True
        args.step4e_integrate_stage25_only = True
        args.bridge_force_p_gain = STEP5D_NO_CONTACT_P0_FORCE_P_GAIN
        args.step4e_force_p_gain = STEP5D_NO_CONTACT_P0_FORCE_P_GAIN
        args.bridge_force_i_gain = STEP5D_NO_CONTACT_P0_FORCE_I_GAIN
        args.step4e_force_i_gain = STEP5D_NO_CONTACT_P0_FORCE_I_GAIN
        args.bridge_force_damping = STEP5D_NO_CONTACT_P0_FORCE_DAMPING
        args.step4e_force_damping = STEP5D_NO_CONTACT_P0_FORCE_DAMPING
        args.bridge_integral_limit_n_s = STEP5D_NO_CONTACT_P0_INTEGRAL_LIMIT_N_S
        args.step4e_integral_limit_n_s = STEP5D_NO_CONTACT_P0_INTEGRAL_LIMIT_N_S
        args.bridge_motion_limit_m_s = STEP5D_NO_CONTACT_P0_MOTION_LIMIT_M_S
        args.step4e_motion_limit_m_s = STEP5D_NO_CONTACT_P0_MOTION_LIMIT_M_S
        args.bridge_total_linear_limit_m_s = STEP5D_NO_CONTACT_P0_TOTAL_LINEAR_LIMIT_M_S
        args.step4e_total_linear_limit_m_s = STEP5D_NO_CONTACT_P0_TOTAL_LINEAR_LIMIT_M_S
        args.bridge_normal_velocity_limit_m_s = STEP5D_NO_CONTACT_P0_NORMAL_VELOCITY_LIMIT_M_S
        args.step4e_normal_velocity_limit_m_s = STEP5D_NO_CONTACT_P0_NORMAL_VELOCITY_LIMIT_M_S
        args.bridge_angular_limit_rad_s = STEP5D_NO_CONTACT_P0_ANGULAR_LIMIT_RAD_S
        args.step4e_angular_limit_rad_s = STEP5D_NO_CONTACT_P0_ANGULAR_LIMIT_RAD_S
        args.bridge_normal_filter_alpha = STEP5D_NO_CONTACT_P0_NORMAL_FILTER_ALPHA
        args.step4e_normal_filter_alpha = STEP5D_NO_CONTACT_P0_NORMAL_FILTER_ALPHA
        args.bridge_normal_follow_mode = "locked"
        args.step4e_normal_follow_mode = "locked"
        args.bridge_normal_min_force_n = STEP5D_NO_CONTACT_P0_NORMAL_MIN_FORCE_N
        args.step4e_normal_min_force_n = STEP5D_NO_CONTACT_P0_NORMAL_MIN_FORCE_N
        args.step5d_preload_filtered_min_n = 0.0
        args.step5d_preload_filtered_max_n = STEP5D_NO_CONTACT_P0_NORMAL_GUARD_N
        args.step5d_preload_raw_min_n = 0.0
        args.step5d_preload_raw_max_n = STEP5D_NO_CONTACT_P0_NORMAL_GUARD_N
        args.step5d_preload_force_norm_max_n = STEP5D_NO_CONTACT_P0_FORCE_GUARD_N
        args.step5d_preload_hold_s = 0.0
        args.step5d_preload_timeout_s = STEP5D_NO_CONTACT_P0_PRELOAD_TIMEOUT_S
        args.step5d_qdot_limit_rad_s = STEP5D_NO_CONTACT_P0_QDOT_CAP_RAD_S
        args.step5d_epsilon = STEP5D_NO_CONTACT_P0_EPSILON
        args.step5d_sigr_exponent_r = STEP5D_NO_CONTACT_P0_SIGR_EXPONENT_R
        args.step5d_rnn_inner_iterations = STEP5D_NO_CONTACT_P0_RNN_INNER_ITERATIONS
        args.step5d_rnn_backend = STEP5D_NO_CONTACT_P0_RNN_BACKEND
        args.max_normal_force_n = STEP5D_NO_CONTACT_P0_NORMAL_GUARD_N
        args.max_force_norm_n = STEP5D_NO_CONTACT_P0_FORCE_GUARD_N
        args.max_torque_norm_nm = STEP5D_NO_CONTACT_P0_TORQUE_GUARD_NM
        if args.bridge_profile == STEP5D_NO_CONTACT_P0_V8_STAGE_ID:
            args.step5d_qdot_limit_rad_s = 0.050
            args.step5d_rnn_inner_iterations = (
                STEP5D_NO_CONTACT_P0_V8_RNN_INNER_ITERATIONS
            )
    elif args.bridge_profile in STEP5D_ABLATION_STAGE_IDS:
        def preload_default_was_not_supplied(flag: str, *env_names: str) -> bool:
            return flag not in argv_list and all(os.environ.get(name, "") == "" for name in env_names)

        if args.bridge_profile in {
            STEP5D_ABLATION_V27_STAGE_ID,
            STEP5D_ABLATION_V28_STAGE_ID,
            STEP5D_ABLATION_V29_STAGE_ID,
            STEP5D_ABLATION_V30_STAGE_ID,
        }:
            default_filtered_min_n = STEP5D_V27_ENTRY_FILTERED_NORMAL_LOAD_MIN_N
            default_filtered_max_n = STEP5D_V27_ENTRY_FILTERED_NORMAL_LOAD_MAX_N
            default_raw_min_n = STEP5D_V27_ENTRY_RAW_NORMAL_LOAD_MIN_N
            default_raw_max_n = STEP5D_V27_ENTRY_RAW_NORMAL_LOAD_MAX_N
            default_force_norm_max_n = STEP5D_V27_ENTRY_FORCE_NORM_MAX_N
            default_hard_normal_n = STEP5D_V27_SENSOR_NORMAL_HARD_STOP_N
            default_hard_force_n = STEP5D_V27_SENSOR_FORCE_HARD_STOP_N
            default_hard_torque_nm = STEP5D_V27_SENSOR_TORQUE_HARD_STOP_NM
        elif args.bridge_profile == STEP5D_ABLATION_V26_STAGE_ID:
            default_filtered_min_n = STEP5D_V26_ENTRY_FILTERED_NORMAL_LOAD_MIN_N
            default_filtered_max_n = STEP5D_V26_ENTRY_FILTERED_NORMAL_LOAD_MAX_N
            default_raw_min_n = STEP5D_V26_ENTRY_RAW_NORMAL_LOAD_MIN_N
            default_raw_max_n = STEP5D_V26_ENTRY_RAW_NORMAL_LOAD_MAX_N
            default_force_norm_max_n = STEP5D_V21_ENTRY_FORCE_NORM_MAX_N
            default_hard_normal_n = STEP5D_V24_SENSOR_FORCE_HARD_STOP_N
            default_hard_force_n = STEP5D_V24_SENSOR_FORCE_HARD_STOP_N
            default_hard_torque_nm = STEP5D_V18_SENSOR_TORQUE_HARD_STOP_NM
        else:
            default_filtered_min_n = STEP5D_V25_ENTRY_FILTERED_NORMAL_LOAD_MIN_N
            default_filtered_max_n = STEP5D_V25_ENTRY_FILTERED_NORMAL_LOAD_MAX_N
            default_raw_min_n = STEP5D_V25_ENTRY_RAW_NORMAL_LOAD_MIN_N
            default_raw_max_n = STEP5D_V25_ENTRY_RAW_NORMAL_LOAD_MAX_N
            default_force_norm_max_n = STEP5D_V21_ENTRY_FORCE_NORM_MAX_N
            default_hard_normal_n = STEP5D_V24_SENSOR_FORCE_HARD_STOP_N
            default_hard_force_n = STEP5D_V24_SENSOR_FORCE_HARD_STOP_N
            default_hard_torque_nm = STEP5D_V18_SENSOR_TORQUE_HARD_STOP_NM
        if preload_default_was_not_supplied(
            "--step5d-preload-filtered-min-n",
            "STEP5D_PRELOAD_FILTERED_MIN_N",
            "BRIDGE_STEP5D_PRELOAD_FILTERED_MIN_N",
        ):
            args.step5d_preload_filtered_min_n = default_filtered_min_n
        if preload_default_was_not_supplied(
            "--step5d-preload-filtered-max-n",
            "STEP5D_PRELOAD_FILTERED_MAX_N",
            "BRIDGE_STEP5D_PRELOAD_FILTERED_MAX_N",
        ):
            args.step5d_preload_filtered_max_n = default_filtered_max_n
        if preload_default_was_not_supplied(
            "--step5d-preload-raw-min-n",
            "STEP5D_PRELOAD_RAW_MIN_N",
            "BRIDGE_STEP5D_PRELOAD_RAW_MIN_N",
        ):
            args.step5d_preload_raw_min_n = default_raw_min_n
        if preload_default_was_not_supplied(
            "--step5d-preload-raw-max-n",
            "STEP5D_PRELOAD_RAW_MAX_N",
            "BRIDGE_STEP5D_PRELOAD_RAW_MAX_N",
        ):
            args.step5d_preload_raw_max_n = default_raw_max_n
        if preload_default_was_not_supplied(
            "--step5d-preload-force-norm-max-n",
            "STEP5D_PRELOAD_FORCE_NORM_MAX_N",
            "BRIDGE_STEP5D_PRELOAD_FORCE_NORM_MAX_N",
        ):
            args.step5d_preload_force_norm_max_n = default_force_norm_max_n
        if preload_default_was_not_supplied(
            "--step5d-preload-hold-s",
            "STEP5D_PRELOAD_HOLD_S",
            "BRIDGE_STEP5D_PRELOAD_HOLD_S",
        ):
            args.step5d_preload_hold_s = STEP5D_V21_ENTRY_REQUIRED_S
        if (
            "--step4e-total-linear-limit-m-s" not in argv_list
            and "--bridge-total-linear-limit-m-s" not in argv_list
            and os.environ.get("STEP4E_TOTAL_LINEAR_LIMIT_M_S", "") == ""
            and os.environ.get("BRIDGE_TOTAL_LINEAR_LIMIT_M_S", "") == ""
        ):
            args.bridge_total_linear_limit_m_s = 0.004
            args.step4e_total_linear_limit_m_s = 0.004
        if "--max-normal-force-n" not in argv_list and os.environ.get("MAX_NORMAL_FORCE_N", "") == "":
            args.max_normal_force_n = default_hard_normal_n
        if "--max-force-norm-n" not in argv_list and os.environ.get("MAX_FORCE_NORM_N", "") == "":
            args.max_force_norm_n = default_hard_force_n
        if "--max-torque-norm-nm" not in argv_list and os.environ.get("MAX_TORQUE_NORM_NM", "") == "":
            args.max_torque_norm_nm = default_hard_torque_nm
        if (
            "--step4e-angular-limit-rad-s" not in argv_list
            and "--bridge-angular-limit-rad-s" not in argv_list
            and os.environ.get("STEP4E_ANGULAR_LIMIT_RAD_S", "") == ""
            and os.environ.get("BRIDGE_ANGULAR_LIMIT_RAD_S", "") == ""
            and args.bridge_profile == STEP5D_ABLATION_V25_STAGE_ID
        ):
            args.bridge_angular_limit_rad_s = 0.150
            args.step4e_angular_limit_rad_s = 0.150
        if args.bridge_profile in {
            STEP5D_ABLATION_V29_STAGE_ID,
            STEP5D_ABLATION_V30_STAGE_ID,
        }:
            if "--step5d-epsilon" not in argv_list:
                args.step5d_epsilon = STEP5D_NO_CONTACT_P0_EPSILON
            if "--step5d-sigr-exponent-r" not in argv_list:
                args.step5d_sigr_exponent_r = STEP5D_NO_CONTACT_P0_SIGR_EXPONENT_R
            if "--step5d-rnn-inner-iterations" not in argv_list:
                args.step5d_rnn_inner_iterations = (
                    STEP5D_V30_RNN_INNER_ITERATIONS
                    if args.bridge_profile == STEP5D_ABLATION_V30_STAGE_ID
                    else STEP5D_NO_CONTACT_P0_RNN_INNER_ITERATIONS
                )
            if "--step5d-rnn-backend" not in argv_list:
                args.step5d_rnn_backend = STEP5D_NO_CONTACT_P0_RNN_BACKEND
    if args.step5d_qdot_limit_rad_s is None:
        args.step5d_qdot_limit_rad_s = (
            STEP5D_V12_QDOT_LIMIT_RAD_S
            if args.bridge_profile
            in {
                STEP5D_LIVEPREP_V12_STAGE_ID,
                STEP5D_LIVEPREP_V13_STAGE_ID,
                STEP5D_LIVEPREP_STAGE_ID,
                STEP5D_LIVEPREP_V15_STAGE_ID,
                STEP5D_LIVEPREP_V15A_STAGE_ID,
                STEP5D_LIVEPREP_V16_STAGE_ID,
                STEP5D_LIVEPREP_V17_STAGE_ID,
                STEP5D_LIVEPREP_V18_STAGE_ID,
                STEP5D_LIVEPREP_V19_STAGE_ID,
                STEP5D_LIVEPREP_V20_STAGE_ID,
                STEP5D_LIVEPREP_V21_STAGE_ID,
                STEP5D_LIVEPREP_V22_STAGE_ID,
                STEP5D_LIVEPREP_V23_STAGE_ID,
                STEP5D_LIVEPREP_V24_STAGE_ID,
                STEP5D_ABLATION_V25_STAGE_ID,
                STEP5D_ABLATION_V26_STAGE_ID,
                STEP5D_ABLATION_V27_STAGE_ID,
                STEP5D_ABLATION_V28_STAGE_ID,
                STEP5D_ABLATION_V29_STAGE_ID,
                STEP5D_ABLATION_V30_STAGE_ID,
                *STEP5D_NO_CONTACT_P0_STAGE_IDS,
            }
            else 0.30
        )
    try:
        args.step5d_stop_register_canary_s = validate_p0_v8_canary_phase(
            args.bridge_profile,
            args.step5d_stop_register_canary_s,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return args


def step5d_runtime_prewarm_metadata(bridge_profile: str) -> dict[str, Any]:
    return {
        "enabled": bridge_profile in STEP5D_LIVEPREP_STAGE_IDS,
        "status": "not_required",
        "elapsed_s": 0.0,
        "before_socket_connect": True,
        "before_rtde_open": True,
    }


def step5d_dashboard_watch_metadata(
    bridge_profile: str,
    *,
    skip_dashboard_preflight: bool,
    disable_dashboard_program_watch: bool,
    timeout_s: float,
) -> dict[str, Any]:
    p0_profile = is_no_contact_p0_stage(bridge_profile)
    return {
        "enabled": (
            bridge_profile in STEP5D_LIVEPREP_STAGE_IDS
            and not p0_profile
            and not skip_dashboard_preflight
            and not disable_dashboard_program_watch
        ),
        "timeout_s": timeout_s,
        "scope": "Step5d live-prep bridge exits after TP program stop or Play timeout",
        "mode": "preflight_only_for_no_contact_p0" if p0_profile else "runtime_dashboard_watch",
    }


def require_p0_v8_canary_authorization(
    args: argparse.Namespace,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        return authorize_p0_v8_canary(args, current)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def require_v29_live_bridge_authorization(
    args: argparse.Namespace,
    *,
    root: Path = EXPERIMENT_ROOT,
) -> dict[str, Any] | None:
    """Apply the canonical v29 gate even when the raw bridge is invoked directly."""
    if args.bridge_profile == STEP5D_ABLATION_V30_STAGE_ID:
        raise SystemExit(
            "v30 raw bridge is an inactive offline candidate; live execution requires "
            "a separate promotion, delivered/read-back package, and new authorization"
        )
    try:
        current = json.loads((root / "config" / "current_stage.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"raw bridge cannot resolve current-stage identity: {exc}") from exc
    if not isinstance(current, Mapping):
        raise SystemExit("raw bridge cannot resolve current-stage identity: JSON root is not an object")
    if args.bridge_profile == STEP5D_NO_CONTACT_P0_V8_STAGE_ID:
        return require_p0_v8_canary_authorization(args, current)
    if args.bridge_profile == STEP5D_NO_CONTACT_P0_STAGE_ID:
        raise SystemExit("P0 v7 is frozen historical evidence; use the P0 v8 workflow")
    current_program = str(current.get("program") or "")
    current_stage_id = str(current.get("current_stage_id") or "")
    if not current_program or not current_stage_id or current_program != current_stage_id:
        raise SystemExit("raw bridge refuses inconsistent current-stage identity")
    current_is_v29 = current_program == STEP5D_ABLATION_V29_STAGE_ID
    requested_is_v29 = args.bridge_profile == STEP5D_ABLATION_V29_STAGE_ID
    if not current_is_v29 and not requested_is_v29:
        return None
    if current_is_v29 and not requested_is_v29:
        raise SystemExit("v29 raw bridge refuses profile relabel against the current-stage binding")
    if getattr(args, "skip_dashboard_preflight", False):
        raise SystemExit("v29 raw bridge requires Dashboard program-identity preflight")
    if getattr(args, "disable_dashboard_program_watch", False):
        raise SystemExit("v29 raw bridge requires the Dashboard program runtime watchdog")
    if args.step5d_stage25_control_mode != "speedj_rnn_live":
        raise SystemExit("v29 raw bridge requires speedj_rnn_live; DLS is not a runtime fallback")
    if v29_pending_audit_override_authorized(args):
        if (
            args.step5d_rnn_backend != "cupy"
            or args.step5d_rnn_inner_iterations != 1024
            or not math.isclose(args.step5d_epsilon, 0.010, abs_tol=1e-12)
            or not math.isclose(args.step5d_sigr_exponent_r, 0.8, abs_tol=1e-12)
            or not math.isclose(args.step5d_qdot_limit_rad_s, 0.050, abs_tol=1e-12)
        ):
            raise SystemExit("v29 pending-audit override requires exact cupy/1024/epsilon=0.010/r=0.8/qdot=0.050 profile")
        return verify_step5d_binding(root, STEP5D_ABLATION_V29_STAGE_ID)
    try:
        return verify_step5d_live_bridge_authorization(
            root,
            STEP5D_ABLATION_V29_STAGE_ID,
            args.step5d_stage25_control_mode,
            rnn_backend=args.step5d_rnn_backend,
            rnn_inner_iterations=args.step5d_rnn_inner_iterations,
            epsilon=args.step5d_epsilon,
            sigr_exponent_r=args.step5d_sigr_exponent_r,
            qdot_cap_rad_s=args.step5d_qdot_limit_rad_s,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


def v29_pending_audit_override_authorized(args: argparse.Namespace) -> bool:
    """Bind the narrow offline-audit exception to exact v29 live consent."""
    return (
        args.bridge_profile == STEP5D_ABLATION_V29_STAGE_ID
        and os.getenv("STEP5D_ALLOW_PENDING_OFFLINE_AUDIT", "0") == "1"
        and os.getenv("STEP5D_CONFIRM", "") == STEP5D_V29_LIVE_CONFIRMATION
    )


def requires_step5d_realtime_scheduler(bridge_profile: str) -> bool:
    return bridge_profile in {
        STEP5D_ABLATION_V29_STAGE_ID,
        STEP5D_ABLATION_V30_STAGE_ID,
        *STEP5D_NO_CONTACT_P0_STAGE_IDS,
    }


def require_step5d_realtime_scheduler(args: argparse.Namespace) -> None:
    """Fail closed unless v29/v30/P0 executes under production FIFO/20."""
    if not requires_step5d_realtime_scheduler(args.bridge_profile):
        return
    scheduler = os.sched_getscheduler(0)
    priority = os.sched_getparam(0).sched_priority
    if scheduler != os.SCHED_FIFO or priority != STEP5D_RT_PRIORITY:
        raise SystemExit(
            "v29/v30/P0 raw bridge requires effective SCHED_FIFO priority exactly 20 "
            f"(scheduler={scheduler}, priority={priority})"
        )


def require_v29_realtime_scheduler(args: argparse.Namespace) -> None:
    """Backward-compatible name for the shared v29/v30/P0 scheduler gate."""

    require_step5d_realtime_scheduler(args)


def require_v29_runtime_guard_policy(args: argparse.Namespace) -> None:
    """Reject v29 raw-bridge arguments that effectively disable mandatory guards."""
    if args.bridge_profile != STEP5D_ABLATION_V29_STAGE_ID:
        return
    finite_fields = {
        "max_normal_force_n": args.max_normal_force_n,
        "max_force_norm_n": args.max_force_norm_n,
        "max_torque_norm_nm": args.max_torque_norm_nm,
        "sensor_stale_s": args.sensor_stale_s,
        "dashboard_program_watch_timeout_s": args.dashboard_program_watch_timeout_s,
    }
    nonfinite = [name for name, value in finite_fields.items() if not math.isfinite(float(value))]
    if nonfinite:
        raise SystemExit(f"v29 raw bridge requires finite mandatory guard values: {', '.join(sorted(nonfinite))}")
    bounds = {
        "max_normal_force_n": (args.max_normal_force_n, STEP5D_V27_SENSOR_NORMAL_HARD_STOP_N),
        "max_force_norm_n": (args.max_force_norm_n, STEP5D_V27_SENSOR_FORCE_HARD_STOP_N),
        "max_torque_norm_nm": (args.max_torque_norm_nm, STEP5D_V27_SENSOR_TORQUE_HARD_STOP_NM),
        "sensor_stale_s": (args.sensor_stale_s, 0.10),
        "dashboard_program_watch_timeout_s": (args.dashboard_program_watch_timeout_s, 45.0),
    }
    over_limit = [
        f"{name}={value:g}>{limit:g}"
        for name, (value, limit) in bounds.items()
        if float(value) > float(limit)
    ]
    if over_limit:
        raise SystemExit(
            "v29 raw bridge refuses guard values above policy bounds: " + ", ".join(sorted(over_limit))
        )


def runtime_scheduler_metadata() -> dict[str, Any]:
    scheduler = os.sched_getscheduler(0)
    names = {
        getattr(os, "SCHED_OTHER", -1): "SCHED_OTHER",
        getattr(os, "SCHED_FIFO", -2): "SCHED_FIFO",
        getattr(os, "SCHED_RR", -3): "SCHED_RR",
    }
    return {
        "policy": names.get(scheduler, f"UNKNOWN_{scheduler}"),
        "policy_value": scheduler,
        "priority": os.sched_getparam(0).sched_priority,
    }


def advance_periodic_deadline(deadline: float, now: float, period: float) -> tuple[float, int, float]:
    """Advance to the first future slot without burst catch-up."""
    if period <= 0.0:
        raise ValueError("period must be positive")
    lateness = max(0.0, now - deadline)
    missed_slots = max(0, int(math.floor(lateness / period)))
    return deadline + (missed_slots + 1) * period, missed_slots, lateness


def dashboard_state_value(value: Any) -> str:
    text = str(value or "").strip()
    return text.rsplit(":", 1)[-1].strip().upper()


def dashboard_loaded_program_basenames(value: Any) -> set[str]:
    return {
        Path(token).name.lower()
        for token in re.findall(r"([^<>\s]+\.urp)(?=$|[>\s])", str(value or ""), flags=re.IGNORECASE)
    }


def v29_dashboard_program_identity_matches(value: Any) -> bool:
    expected = f"{STEP5D_ABLATION_V29_STAGE_ID}.urp".lower()
    return dashboard_loaded_program_basenames(value) == {expected}


def require_v29_dashboard_program_binding(
    args: argparse.Namespace,
    dashboard: Mapping[str, Any] | None,
) -> None:
    if args.bridge_profile != STEP5D_ABLATION_V29_STAGE_ID:
        return
    if not isinstance(dashboard, Mapping):
        raise SystemExit("v29 Dashboard preflight is missing")
    remote_state = dashboard_state_value(dashboard.get("is in remote control"))
    safety_state = dashboard_state_value(dashboard.get("safetymode"))
    robot_state = dashboard_state_value(dashboard.get("robotmode"))
    if not v29_dashboard_program_identity_matches(dashboard.get("programState")):
        raise SystemExit("v29 Dashboard program identity does not match the current package")
    allow_tp_local = v29_pending_audit_override_authorized(args)
    if remote_state != "TRUE" and not (allow_tp_local and remote_state == "FALSE"):
        raise SystemExit("v29 Dashboard remote-control state is not true")
    if safety_state != "NORMAL":
        raise SystemExit("v29 Dashboard safety state is not NORMAL")
    if robot_state != "RUNNING":
        raise SystemExit("v29 Dashboard robot mode is not RUNNING")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.duration_s <= 0 or args.baseline_s < 0 or args.rtde_hz <= 0:
        raise SystemExit("duration, baseline, and RTDE rate must be positive")
    if args.bias_contact_normal_threshold_n < 0.0 or args.bias_contact_force_norm_threshold_n < 0.0:
        raise SystemExit("bias contact thresholds must be non-negative")
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
        *STEP5B_BRIDGE_PROFILES,
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
    if args.step5d_rnn_inner_iterations < 1:
        raise SystemExit("--step5d-rnn-inner-iterations must be a positive integer")
    if args.step5d_rnn_backend not in {"numpy", "cupy"}:
        raise SystemExit("--step5d-rnn-backend must be numpy or cupy")
    if args.step5d_preload_filtered_min_n < 0.0 or args.step5d_preload_filtered_max_n < args.step5d_preload_filtered_min_n:
        raise SystemExit("--step5d-preload-filtered-* must define a nonnegative min<=max window")
    if args.step5d_preload_raw_min_n < 0.0 or args.step5d_preload_raw_max_n < args.step5d_preload_raw_min_n:
        raise SystemExit("--step5d-preload-raw-* must define a nonnegative min<=max window")
    if args.step5d_preload_force_norm_max_n <= 0.0:
        raise SystemExit("--step5d-preload-force-norm-max-n must be positive")
    if args.step5d_preload_hold_s < 0.0 or args.step5d_preload_timeout_s <= args.step5d_preload_hold_s:
        raise SystemExit("--step5d-preload-timeout-s must be greater than --step5d-preload-hold-s")
    if args.dashboard_program_watch_timeout_s <= 0.0:
        raise SystemExit("--dashboard-program-watch-timeout-s must be positive")
    validate_step5b_15n_trial_args(args)
    require_v29_runtime_guard_policy(args)
    require_v29_live_bridge_authorization(args)
    require_step5d_realtime_scheduler(args)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    sensor_csv_path = args.output_dir / "kunwei_sensor_1khz.csv"
    rtde_hz_label = f"{args.rtde_hz:g}".replace(".", "p")
    bridge_csv_path = args.output_dir / f"bridge_rtde_{rtde_hz_label}hz.csv"
    raw_path = args.output_dir / "raw_frames.bin"
    metadata_path = args.output_dir / "metadata.json"
    summary_path = args.output_dir / "summary.json"
    write_bridge_run_manifest(args, argv=list(argv if argv is not None else sys.argv[1:]))
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
        require_v29_dashboard_program_binding(args, dashboard)

    metadata = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "args": {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()},
        "dashboard_preflight": dashboard,
        "runtime_scheduler": runtime_scheduler_metadata(),
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
            "v29_bridge_safety_fail_stop": {
                "primary": "RTDE sensor_ok=0, stop_request=0, zero motion carriers; TP selects non-auto-home reason 3",
                "secondary": "hard-coded Dashboard stop command",
                "break_condition": "TP reason 3 observed or Dashboard stop acknowledged",
                "dashboard_timeout_s": STEP5D_V29_FAIL_STOP_DASHBOARD_TIMEOUT_S,
                "rtde_reconnect_timeout_s": STEP5D_V29_FAIL_STOP_RTDE_RECONNECT_TIMEOUT_S,
                "retry_s": STEP5D_V29_FAIL_STOP_DASHBOARD_RETRY_S,
            },
        },
        "bias_estimator_logging_contract": {
            "source": "STARS-2024-001-inspired logging only; no online Kalman bias estimator is run here",
            "zero_event_id": "alias of software baseline epoch, emitted per sensor and bridge row",
            "bias_est_fields": BIAS_ESTIMATE_FIELDS,
            "bias_rate_est_fields": BIAS_RATE_ESTIMATE_FIELDS,
            "bias_rate_estimator": "held finite difference between completed software-zero baseline estimates",
            "contact_mask": "bias-estimation contact mask; use it to gate/freeze bias estimation",
            "control_contact_window": "control-state contact window, logged separately from contact_mask",
            "normal_threshold_n": args.bias_contact_normal_threshold_n,
            "force_norm_threshold_n": args.bias_contact_force_norm_threshold_n,
            "derived_kinematics_fields": KINEMATIC_DERIVED_FIELDS,
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
        "dashboard_program_watch": step5d_dashboard_watch_metadata(
            args.bridge_profile,
            skip_dashboard_preflight=args.skip_dashboard_preflight,
            disable_dashboard_program_watch=args.disable_dashboard_program_watch,
            timeout_s=args.dashboard_program_watch_timeout_s,
        ),
        "step5d_preload_gate": {
            "profile": args.bridge_profile if args.bridge_profile in STEP5D_LIVEPREP_STAGE_IDS else None,
            "filtered_min_n": args.step5d_preload_filtered_min_n,
            "filtered_max_n": args.step5d_preload_filtered_max_n,
            "raw_min_n": args.step5d_preload_raw_min_n,
            "raw_max_n": args.step5d_preload_raw_max_n,
            "force_norm_max_n": args.step5d_preload_force_norm_max_n,
            "hold_s": args.step5d_preload_hold_s,
            "timeout_s": args.step5d_preload_timeout_s,
            "param_valid_code": STEP5D_LINE_ENTRY_PARAM_VALID_CODE,
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
            "step5d_v21_plus_liveprep_25.3": "input_double_register_37..39 are Cartesian deadband-acquire vx/vy/vz; 40/41 are preload filtered min/max, 42 is preload force_norm max, 44 is hold_s, 46 is timeout_s, and 47 must equal the Step5d preload param-valid code.",
            "step5d_v22_liveprep_25.95": "input_double_register_37..42 must be bridge-cleared qdot-safe zeros, 43 must be 0, and 47 must not equal the preload param-valid code before TP enters Stage 25.0.",
            "step5d_v23_liveprep_25.95": "TP requires qdot registers 37..42 to be near-zero before Stage 25.0; bridge also applies a post-RNN normal-direction command guard while preserving RNN as the object under test.",
            "step5d_v24_liveprep_25.95": "TP keeps the v23 near-zero qdot-clear barrier; bridge stops low/no-contact instead of executing active_reacquire_solver qdot and adds post-RNN tracking reversal detection.",
            "step5d_strict_rnn_ablation_v27_v28_25.0": "In speedl_cartesian_oracle, bridge writes layout tag 523 and Step5b-live linear vx/vy/vz, forces wx/wy/wz to 0 for all Stage25.0, and records limited raw Step5d angular/linear commands in _step5d_speedl_shadow_raw_* fields.",
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
            "step5b_contact_cycloid_baseline_v2": "Step5b v2 TP contact scaffold skips the lift/25.2 cycle when first-contact orientation error is already <=4deg; stage 25.0 keeps the Step5 table contact cycloid reference and filtered-live normal policy.",
            "step5b_contact_cycloid_baseline_v3": "Step5b v3 TP contact scaffold removes the lift/25.2 attitude cycle and second contact search after first-contact latch; stage 25.0 keeps the Step5 table contact cycloid reference and filtered-live normal policy.",
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
            "step5d_strict_rnn_liveprep_v15a": "Retained v15a live-prep evidence: online broad AABB TCP cage and bounded zero-qdot hold/reacquire reached Stage25, then stopped by hold_duty_limit.",
            "step5d_strict_rnn_liveprep_v16": "Retained v16 TP/script live-prep evidence: Step5b v3 no-lift/no-25.2/no-second-search scaffold, 12N target, 5-20N entry window, 0.05 rad/s strict RNN speedj, and v15a online cage bounded hold/reacquire safety.",
            "step5d_strict_rnn_liveprep_v17": "Retained v17 TP/script live-prep evidence: Step5b v3 no-lift/no-25.2/no-second-search scaffold, 12N target, 8-18N filtered preload release with 7.5-19N raw sanity, 0.05 rad/s strict RNN speedj, and v15a online cage bounded hold/reacquire safety stopped by hold_duty_limit.",
            "step5d_strict_rnn_liveprep_v18": "Retained v18 cage-primary diagnostic TP/script live-prep evidence: stopped on predicted TCP speed during low-load/no-contact active reacquire after v17/v18 8-18N preload allowed an over-target handoff.",
            "step5d_strict_rnn_liveprep_v19": "Retained v19 cage-primary diagnostic TP/script live-prep evidence: kept 12N target, 8-13N filtered preload with 7.5-14N raw sanity, 1.5x Stage22/24 speedups, and a freeze_low_force-only active-reacquire speed cap; live v19 still stopped on cage_primary_tcp_speed_hard_stop because the cap did not cover the locked-normal settle source.",
            "step5d_strict_rnn_liveprep_v20": "Current v20 cage-primary diagnostic TP/script live-prep candidate: keeps v19 numeric baselines, forces Stage22/24 pre-contact search posture to gravity-down [pi,0,0], logs pose-contract/reacquire-cap diagnostics, and applies low-load active-reacquire outer-state reset plus 0.035 m/s predicted TCP speed cap based on action/load semantics rather than normal_filter_source.",
            "step5d_strict_rnn_liveprep_v21": "Retained v21 failure evidence: bridge-time preload override registers could persist into Stage25.0 and be interpreted as qdot, causing stop_reason=13 at the 25.3->25.0 boundary.",
            "step5d_strict_rnn_liveprep_v22": "Retained v22 cage-primary TP/script live-prep evidence: Stage25.95 qdot clear barrier existed, but live v22 still reached normal_force_guard during strict RNN Stage25.",
            "step5d_strict_rnn_liveprep_v23": "Retained v23 cage-primary TP/script live-prep evidence: live run lost contact, continued active_reacquire_solver qdot, and exhausted TCP cage margin while RNN/J(q) approach tracking opposed the outer-loop press command.",
            "step5d_strict_rnn_liveprep_v24": "Current v24 cage-primary TP/script live-prep candidate: keeps v23 preload/qdot-clear scaffold, uses 25N raw/force hard guards, stops low/no-contact instead of executing active_reacquire_solver qdot, and adds post-RNN tracking reversal detection.",
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
                if args.bridge_path_shape == "cycloid" and args.bridge_profile not in STEP5B_BRIDGE_PROFILES
                else None
            ),
            "step5_stage_id": (
                STEP5_CONTACT_CYCLOID_STAGE_ID
                if args.bridge_profile in STEP5B_BRIDGE_PROFILES
                else args.bridge_profile
                if args.bridge_profile in STEP5D_LIVEPREP_STAGE_IDS
                else None
            ),
            "step5c_stage_id": args.bridge_profile
            if args.bridge_profile in {STEP5C_DRYRUN_STAGE_ID, STEP5C_CONTACT_STAGE_ID}
            else None,
            "step5c_register_contract": "Step5c/Step5d Stage 25.0: 37..42=qd0..qd5 rad/s, 43=cmd_valid, 44=path_time, 45=force_error, 46=pose/orientation_error, 47=solver_status. Step5d v8/v9 Stage 25.3: 37..39=Cartesian force-PID settle vx/vy/vz only. Step5d v10 Stage 25.3: 37..39=Cartesian admittance settle vx/vy/vz only and 45 carries filtered force_error. Step5d v11/v12 Stage 25.3: 37..39=Cartesian deadband-acquire vx/vy/vz only and 45 carries filtered force_error. Step5d v21+ Stage 25.3 additionally uses 40/41/42/44/46/47 as the preload parameter channel, with 47 holding the param-valid code. Step5d v22+ inserts Stage 25.95 as a qdot-clear barrier before Stage 25.0; v23 requires near-zero qdot and adds a post-RNN normal-direction guard; v24 stops low/no-contact before executing active_reacquire_solver qdot and adds tracking reversal detection. Step5d v12+ Stage 25.0 additionally gates loss-of-contact and TCP speed before cmd_valid. Step4e field names are carrier names only in joint mode."
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
    bias_rate_estimate = [0.0] * 6
    bias_estimate_initialized = baseline_ready
    bias_estimate_update_mono: float | None = start_mono if baseline_ready else None
    latest_zeroed = [0.0] * 6
    latest_frame_time: float | None = None
    latest_output: dict[str, Any] | None = None
    previous_kinematics_output: dict[str, Any] | None = None
    previous_kinematics_time: float | None = None
    latest_derived_kinematics = {field: math.nan for field in KINEMATIC_DERIVED_FIELDS}
    last_zero_request: float | None = None
    zero_request_epsilon = 1e-6
    heartbeat = (
        float((time.time_ns() // 1_000_000) % 1_000_000_000)
        if args.bridge_profile == STEP5D_ABLATION_V29_STAGE_ID
        else 0.0
    )
    last_published_heartbeat = heartbeat
    deadline_overrun_hold_total = 0
    deadline_overrun_consecutive = 0
    stop_request = 0.0
    stop_reason = "duration"
    guard_reason: str | None = None
    p0_v8_canary_stop_sent_at: float | None = None
    p0_v8_canary_tp_acknowledged = False
    parse_errors = 0
    dropped_sync_bytes = 0
    samples = 0
    bridge_writes = 0
    bridge_write_times: list[float] = []
    rtde_output_times: list[float] = []
    rtde_output_sequence = 0
    echo_transition_times: list[float] = []
    rtde_reconnect_events: list[dict[str, Any]] = []
    next_rtde_reconnect_mono = start_mono
    last_echo_heartbeat: float | None = None
    normals: list[float] = []
    force_norms: list[float] = []
    torque_norms: list[float] = []
    trusted_normals: list[float] = []
    trusted_force_norms: list[float] = []
    trusted_torque_norms: list[float] = []
    startup_untrusted_spikes: list[dict[str, Any]] = []
    zero_events: list[dict[str, Any]] = []
    v29_safety_fail_stop: dict[str, Any] = {
        "enabled": args.bridge_profile == STEP5D_ABLATION_V29_STAGE_ID,
        "latched_reason": None,
        "rtde_reason3_packets_sent": 0,
        "first_rtde_packet_output_sequence": None,
        "heartbeat_min_sent": None,
        "heartbeat_max_sent": None,
        "tp_reason3_observed": False,
        "dashboard_stop_delivered": False,
        "termination_channel": None,
        "dashboard_stop_attempts": 0,
        "dashboard_stop_response": "",
        "dashboard_stop_error": "",
        "next_dashboard_stop_attempt_mono": start_mono,
    }

    def attempt_v29_fail_stop_dashboard(now_mono: float) -> None:
        if (
            v29_safety_fail_stop["latched_reason"] is None
            or v29_safety_fail_stop["dashboard_stop_delivered"]
            or now_mono < v29_safety_fail_stop["next_dashboard_stop_attempt_mono"]
        ):
            return
        dashboard_stop = request_v29_fail_stop_dashboard_stop(args)
        v29_safety_fail_stop["dashboard_stop_attempts"] += 1
        v29_safety_fail_stop["dashboard_stop_response"] = dashboard_stop["response"]
        v29_safety_fail_stop["dashboard_stop_error"] = dashboard_stop["error"]
        v29_safety_fail_stop["dashboard_stop_delivered"] = dashboard_stop["delivered"]
        v29_safety_fail_stop["next_dashboard_stop_attempt_mono"] = (
            time.monotonic() + STEP5D_V29_FAIL_STOP_DASHBOARD_RETRY_S
        )

    buffer = bytearray()
    step4e_state = BridgeState()
    step5d_runtime_prewarm = step5d_runtime_prewarm_metadata(args.bridge_profile)
    if step5d_runtime_prewarm["enabled"]:
        prewarm_start = time.perf_counter()
        try:
            ensure_step5d_liveprep_runtime(step4e_state, args)
        except Exception as exc:
            step5d_runtime_prewarm["status"] = "failed"
            step5d_runtime_prewarm["error"] = rtde_error_name(exc)
            step5d_runtime_prewarm["elapsed_s"] = time.perf_counter() - prewarm_start
            metadata["step5d_liveprep_runtime_prewarm"] = step5d_runtime_prewarm
            write_json(metadata_path, metadata)
            raise
        step5d_runtime_prewarm["elapsed_s"] = time.perf_counter() - prewarm_start
        step5d_runtime_prewarm["status"] = (
            "ok"
            if step4e_state.step5d_model_bundle is not None
            and step4e_state.step5d_tcp_offset_tool0 is not None
            and step4e_state.step5d_solver is not None
            else "incomplete"
        )
        if step5d_runtime_prewarm["status"] != "ok":
            metadata["step5d_liveprep_runtime_prewarm"] = step5d_runtime_prewarm
            write_json(metadata_path, metadata)
            raise RuntimeError("Step5d liveprep runtime prewarm did not initialize model, TCP offset, and solver")

    next_write = start_mono
    write_period = 1.0 / args.rtde_hz
    write_deadline_missed_slots = 0
    write_deadline_overrun_events = 0
    write_deadline_max_lateness_s = 0.0
    dashboard_watch = step5d_dashboard_watch_metadata(
        args.bridge_profile,
        skip_dashboard_preflight=args.skip_dashboard_preflight,
        disable_dashboard_program_watch=args.disable_dashboard_program_watch,
        timeout_s=args.dashboard_program_watch_timeout_s,
    )
    dashboard_watch_enabled = bool(dashboard_watch["enabled"])
    dashboard_watch_saw_running = False
    next_dashboard_watch = start_mono
    metadata["step5d_liveprep_runtime_prewarm"] = step5d_runtime_prewarm
    metadata["dashboard_program_watch"].update(dashboard_watch)
    write_json(metadata_path, metadata)

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
            *BIAS_LOG_FIELDS,
            "frame_hex",
        ]
        bridge_fields = [
            "write_index",
            "t_wall_ns",
            "t_monotonic_s",
            "sensor_age_s",
            *INPUT_NAMES,
            "guard_reason",
            "v29_fail_stop_latched",
            "v29_fail_stop_rtde_packets",
            "v29_fail_stop_tp_reason3_observed",
            "v29_fail_stop_dashboard_ack",
            "v29_fail_stop_dashboard_attempts",
            "v29_fail_stop_terminate",
            "baseline_ready",
            "baseline_epoch",
            *BIAS_BRIDGE_LOG_FIELDS,
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
        bridge_output_fields += KINEMATIC_DERIVED_FIELDS
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
            last_csv_write_s = 0.0
            next_write = time.monotonic()
            ready_path = args.output_dir / "bridge_ready.json"
            write_json(
                ready_path,
                {
                    "ready_schema": "v29_bridge_ready_v1",
                    "ok": True,
                    "pid": os.getpid(),
                    "launch_nonce": os.getenv("STEP5D_BRIDGE_LAUNCH_NONCE", ""),
                    "bridge_profile": args.bridge_profile,
                    "rtde_hz": args.rtde_hz,
                    "runtime_scheduler": metadata["runtime_scheduler"],
                    "prewarm_status": step5d_runtime_prewarm["status"],
                    "rtde_connected": rtde is not None,
                    "output_dir": str(args.output_dir),
                },
            )

            while True:
                loop_sensor_recv_s = 0.0
                loop_rtde_recv_s = 0.0
                loop_compute_s = 0.0
                loop_rtde_send_s = 0.0
                now = time.monotonic()
                fail_stop_latched = v29_safety_fail_stop["latched_reason"] is not None
                if stop_signal["name"] is not None and not fail_stop_latched:
                    stop_reason = f"signal_{stop_signal['name'].lower()}"
                    break
                if now - start_mono >= args.duration_s and not fail_stop_latched:
                    stop_reason = "duration"
                    break
                if dashboard_watch_enabled and not fail_stop_latched and now >= next_dashboard_watch:
                    try:
                        dash = dashboard_exchange(
                            args.robot_host,
                            ["running", "programState", "safetymode"],
                            timeout=(
                                STEP5D_V29_RUNTIME_DASHBOARD_WATCH_TIMEOUT_S
                                if args.bridge_profile == STEP5D_ABLATION_V29_STAGE_ID
                                else 3.0
                            ),
                        )
                    except (OSError, RuntimeError, socket.timeout):
                        if args.bridge_profile != STEP5D_ABLATION_V29_STAGE_ID:
                            raise
                        v29_safety_fail_stop["latched_reason"] = "dashboard_program_watch_unavailable"
                        v29_safety_fail_stop["next_dashboard_stop_attempt_mono"] = (
                            time.monotonic() + STEP5D_V29_FAIL_STOP_DASHBOARD_RETRY_S
                        )
                        fail_stop_latched = True
                        dash = None
                    if dash is not None and dashboard_state_value(dash.get("safetymode")) != "NORMAL":
                        stop_reason = "dashboard_safety_not_normal"
                        break
                    if (
                        dash is not None
                        and args.bridge_profile == STEP5D_ABLATION_V29_STAGE_ID
                        and not v29_dashboard_program_identity_matches(dash.get("programState"))
                    ):
                        v29_safety_fail_stop["latched_reason"] = "dashboard_program_identity_drift"
                        fail_stop_latched = True
                    if dash is not None and not fail_stop_latched:
                        running = dashboard_state_value(dash.get("running")) == "TRUE"
                        stopped = dashboard_state_value(dash.get("programState")).startswith("STOPPED")
                        if running:
                            dashboard_watch_saw_running = True
                        elif dashboard_watch_saw_running and stopped:
                            stop_reason = "dashboard_program_stopped"
                            break
                        elif (
                            not dashboard_watch_saw_running
                            and stopped
                            and now - start_mono >= args.dashboard_program_watch_timeout_s
                        ):
                            stop_reason = "dashboard_play_timeout"
                            break
                    next_dashboard_watch = now + 0.25

                if fail_stop_latched and rtde is None:
                    attempt_v29_fail_stop_dashboard(now)

                sensor_recv_start = time.perf_counter()
                if fail_stop_latched:
                    chunk = b""
                else:
                    try:
                        chunk = sock.recv(8192)
                    except (BlockingIOError, socket.timeout):
                        chunk = b""
                loop_sensor_recv_s = time.perf_counter() - sensor_recv_start
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
                                new_baseline = [statistics.fmean(axis) for axis in zip(*baseline_raw_si)]
                                if bias_estimate_initialized and bias_estimate_update_mono is not None:
                                    bias_rate_estimate = finite_vector_derivative(
                                        new_baseline,
                                        baseline,
                                        latest_frame_time - bias_estimate_update_mono,
                                    )
                                else:
                                    bias_rate_estimate = [0.0] * 6
                                baseline = new_baseline
                                bias_estimate_initialized = True
                                bias_estimate_update_mono = latest_frame_time
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
                        sensor_contact_mask, sensor_bias_reason = bias_contact_mask(
                            baseline_ready=baseline_ready,
                            normal_load_n=normal,
                            force_norm_n=force_norm,
                            normal_threshold_n=args.bias_contact_normal_threshold_n,
                            force_norm_threshold_n=args.bias_contact_force_norm_threshold_n,
                        )
                        trusted_sensor_sample = baseline_ready and sensor_bias_reason != "baseline_not_ready"
                        if trusted_sensor_sample:
                            trusted_normals.append(normal)
                            trusted_force_norms.append(force_norm)
                            trusted_torque_norms.append(torque_norm)
                        elif (
                            args.bridge_profile == STEP5D_LIVEPREP_V24_STAGE_ID
                            and len(startup_untrusted_spikes) < 16
                            and (abs(normal) >= STEP5D_V24_SENSOR_FORCE_HARD_STOP_N or force_norm >= STEP5D_V24_SENSOR_FORCE_HARD_STOP_N)
                        ):
                            startup_untrusted_spikes.append(
                                {
                                    "sample_index": samples,
                                    "zero_event_id": baseline_epoch,
                                    "bias_contact_reason": sensor_bias_reason,
                                    "normal_force_n": normal,
                                    "force_norm_n": force_norm,
                                    "torque_norm_nm": torque_norm,
                                }
                            )
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
                                "zero_event_id": baseline_epoch,
                                "contact_mask": sensor_contact_mask,
                                "bias_estimation_contact_mask": sensor_contact_mask,
                                "bias_contact_reason": sensor_bias_reason,
                                **{
                                    key: csv_value(value)
                                    for key, value in bias_estimate_row(baseline, bias_rate_estimate).items()
                                },
                                "frame_hex": frame.hex(),
                            }
                        )
                elif sock.fileno() < 0 and not fail_stop_latched:
                    stop_reason = "socket_closed"
                    break

                if (
                    args.write_rtde_inputs
                    and rtde is None
                    and now >= next_rtde_reconnect_mono
                    and not v29_safety_fail_stop["dashboard_stop_delivered"]
                ):
                    try:
                        rtde, rtde_input_recipe, rtde_input_types, rtde_output_recipe, rtde_output_types = open_rtde_bridge(
                            args,
                            timeout_s=(
                                STEP5D_V29_FAIL_STOP_RTDE_RECONNECT_TIMEOUT_S
                                if args.bridge_profile == STEP5D_ABLATION_V29_STAGE_ID
                                else None
                            ),
                        )
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
                    rtde_recv_start = time.perf_counter()
                    try:
                        sample = rtde.recv_available_sample(rtde_output_recipe, rtde_output_types)
                    except (OSError, RuntimeError, socket.timeout) as exc:
                        loop_rtde_recv_s = time.perf_counter() - rtde_recv_start
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
                    else:
                        loop_rtde_recv_s = time.perf_counter() - rtde_recv_start
                    if sample is not None:
                        rtde_output_sequence += 1
                        rtde_output_time = time.monotonic()
                        kinematics_dt_s = (
                            None
                            if previous_kinematics_time is None
                            else rtde_output_time - previous_kinematics_time
                        )
                        latest_derived_kinematics = derived_kinematics_row(
                            sample,
                            previous_kinematics_output,
                            kinematics_dt_s,
                        )
                        previous_kinematics_output = sample
                        previous_kinematics_time = rtde_output_time
                        latest_output = sample
                        if p0_v8_canary_stop_sent_at is not None:
                            try:
                                p0_stop_echo = float(
                                    sample.get("output_double_register_28", 0.0)
                                )
                                p0_stage_echo = float(
                                    sample.get("output_double_register_35", 0.0)
                                )
                            except (TypeError, ValueError):
                                p0_stop_echo = 0.0
                                p0_stage_echo = 0.0
                            p0_v8_canary_tp_acknowledged = (
                                p0_stop_echo > 0.5 or abs(p0_stage_echo - 26.0) < 0.05
                            )
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
                    next_write, missed_slots, deadline_lateness_s = advance_periodic_deadline(
                        next_write,
                        now,
                        write_period,
                    )
                    write_deadline_missed_slots += missed_slots
                    if missed_slots > 0:
                        write_deadline_overrun_events += 1
                    write_deadline_max_lateness_s = max(write_deadline_max_lateness_s, deadline_lateness_s)
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
                    compute_start = time.perf_counter()
                    if fail_stop_latched:
                        step4e_values = {name: 0.0 for name in BRIDGE_INPUT_NAMES}
                        step4e_values["stop_request"] = 0.0
                    else:
                        try:
                            step4e_values = compute_bridge_values(
                                args,
                                latest_zeroed,
                                latest_output,
                                sensor_ok,
                                step4e_state,
                                write_period,
                            )
                        except Exception as control_error:
                            if not uses_v30_control_contract(args.bridge_profile):
                                raise
                            _stop_command, _stop_packet, stop_publish_event = (
                                publish_step5d_v30_exception_stop(
                                    rtde,
                                    recipe_id=rtde_input_recipe,
                                    type_names=rtde_input_types,
                                    heartbeat=heartbeat,
                                    original_error=control_error,
                                )
                            )
                            stop_publish_event["count"] = len(rtde_reconnect_events) + 1
                            rtde_reconnect_events.append(stop_publish_event)
                            if stop_publish_event["stop_publish_succeeded"]:
                                stop_reason = "v30_control_exception_stop_published"
                            else:
                                stop_reason = (
                                    "v30_control_exception_stop_publish_failed:"
                                    + str(stop_publish_event["stop_publish_error"])
                                )
                            break
                    loop_compute_s = time.perf_counter() - compute_start
                    step4e_values["_bridge_loop_gap_s"] = 0.0 if not bridge_write_times else now - bridge_write_times[-1]
                    step4e_values["_bridge_loop_deadline_lateness_s"] = deadline_lateness_s
                    step4e_values["_bridge_loop_missed_slots"] = missed_slots
                    step4e_values["_bridge_loop_deadline_miss_total"] = write_deadline_missed_slots
                    step4e_values["_bridge_loop_sensor_recv_s"] = loop_sensor_recv_s
                    step4e_values["_bridge_loop_rtde_recv_s"] = loop_rtde_recv_s
                    step4e_values["_bridge_loop_compute_s"] = loop_compute_s
                    step4e_values["_bridge_loop_rtde_send_s"] = loop_rtde_send_s
                    step4e_values["_bridge_loop_csv_write_s"] = last_csv_write_s
                    for name in BRIDGE_INPUT_NAMES:
                        bridge_values[name] = float(step4e_values.get(name, 0.0))
                    control_contact_window = control_contact_window_from_bridge_values(step4e_values)
                    bridge_contact_mask, bridge_bias_reason = bias_contact_mask(
                        baseline_ready=baseline_ready,
                        normal_load_n=bridge_values["normal_force_n"],
                        force_norm_n=bridge_values["force_norm_n"],
                        control_contact_window=control_contact_window,
                        normal_threshold_n=args.bias_contact_normal_threshold_n,
                        force_norm_threshold_n=args.bias_contact_force_norm_threshold_n,
                    )
                    guard_reason = None
                    terminate_after_write = False
                    step4e_stop_request = float(step4e_values.get("stop_request", 0.0)) > 0.5
                    step5b_trial_reason = str(step4e_values.get("_step5b_15n_trial_stop_reason", ""))
                    step5b_ramp_reason = str(step4e_values.get("_step5b_ramp_stop_reason", ""))
                    if step5b_trial_reason.startswith("step5b_15n_trial:"):
                        step4e_guard_reason = step5b_trial_reason
                    elif step5b_ramp_reason.startswith("step5b_ramp_5_to_15:"):
                        step4e_guard_reason = step5b_ramp_reason
                    else:
                        step4e_guard_reason = "step5d_contact_safety:" + str(
                            step4e_values.get("_step5d_contact_safety_reason", "stop_request")
                        )
                    hard_guard_reason = guard_stop_reason(args, bridge_values) if sensor_ok else None
                    if v29_safety_fail_stop["enabled"]:
                        v29_safety_fail_stop["latched_reason"] = select_v29_fail_stop_reason(
                            v29_safety_fail_stop["latched_reason"],
                            hard_guard_reason=hard_guard_reason,
                            step4e_stop_request=step4e_stop_request,
                            step4e_guard_reason=step4e_guard_reason,
                        )
                        if v29_safety_fail_stop["latched_reason"] is not None:
                            guard_reason = str(v29_safety_fail_stop["latched_reason"])
                            stop_reason = guard_reason
                            stop_request = 0.0
                            apply_v29_fail_stop(bridge_values)
                    else:
                        if step4e_stop_request:
                            bridge_values["stop_request"] = 1.0
                            stop_request = 1.0
                            guard_reason = step4e_guard_reason
                            stop_reason = guard_reason
                        if hard_guard_reason is not None:
                            bridge_values["stop_request"] = 1.0
                            stop_request = 1.0
                            guard_reason = hard_guard_reason
                            stop_reason = hard_guard_reason
                    deadline_overrun_hold_active = bool(
                        uses_v30_control_contract(args.bridge_profile)
                        and time.monotonic() >= next_write
                    )
                    if deadline_overrun_hold_active:
                        deadline_overrun_hold_total += 1
                        deadline_overrun_consecutive += 1
                        bridge_values["heartbeat"] = last_published_heartbeat
                        apply_step5d_deadline_overrun_hold(bridge_values)
                    else:
                        deadline_overrun_consecutive = 0
                    step4e_values["_bridge_loop_deadline_overrun_hold"] = (
                        1.0 if deadline_overrun_hold_active else 0.0
                    )
                    step4e_values["_bridge_loop_deadline_overrun_hold_total"] = float(
                        deadline_overrun_hold_total
                    )
                    step4e_values["_bridge_loop_deadline_overrun_consecutive"] = float(
                        deadline_overrun_consecutive
                    )
                    rtde_connected = rtde is not None
                    rtde_send_succeeded = False
                    if rtde is not None:
                        rtde_send_start = time.perf_counter()
                        try:
                            rtde.send_input_sample(
                                rtde_input_recipe,
                                rtde_input_types,
                                [bridge_values[name] for name in INPUT_NAMES],
                            )
                        except (OSError, RuntimeError, socket.timeout) as exc:
                            loop_rtde_send_s = time.perf_counter() - rtde_send_start
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
                        else:
                            loop_rtde_send_s = time.perf_counter() - rtde_send_start
                            rtde_send_succeeded = True
                    if v29_safety_fail_stop["latched_reason"] is not None:
                        if rtde_send_succeeded:
                            record_v29_fail_stop_rtde_packet(
                                v29_safety_fail_stop,
                                heartbeat=float(bridge_values["heartbeat"]),
                                output_sequence=rtde_output_sequence,
                            )
                        update_v29_fail_stop_tp_ack(
                            v29_safety_fail_stop,
                            output=latest_output,
                            output_sequence=rtde_output_sequence,
                        )
                        attempt_v29_fail_stop_dashboard(time.monotonic())
                        v29_safety_fail_stop["termination_channel"] = v29_fail_stop_termination_channel(
                            v29_safety_fail_stop
                        )
                        terminate_after_write = v29_safety_fail_stop["termination_channel"] is not None
                    step4e_values["_bridge_loop_rtde_send_s"] = loop_rtde_send_s
                    row = {
                        "write_index": bridge_writes + 1,
                        "t_wall_ns": time.time_ns(),
                        "t_monotonic_s": f"{now:.9f}",
                        "sensor_age_s": sensor_age if math.isfinite(sensor_age) else "",
                        **{key: csv_value(value) for key, value in bridge_values.items()},
                        **{key: csv_value(step4e_values.get(key, "")) for key in step4e_diag_fields},
                        "guard_reason": guard_reason or "",
                        "v29_fail_stop_latched": int(v29_safety_fail_stop["latched_reason"] is not None),
                        "v29_fail_stop_rtde_packets": v29_safety_fail_stop["rtde_reason3_packets_sent"],
                        "v29_fail_stop_tp_reason3_observed": int(v29_safety_fail_stop["tp_reason3_observed"]),
                        "v29_fail_stop_dashboard_ack": int(v29_safety_fail_stop["dashboard_stop_delivered"]),
                        "v29_fail_stop_dashboard_attempts": v29_safety_fail_stop["dashboard_stop_attempts"],
                        "v29_fail_stop_terminate": int(terminate_after_write),
                        "baseline_ready": int(baseline_ready),
                        "baseline_epoch": baseline_epoch,
                        "zero_event_id": baseline_epoch,
                        "contact_mask": bridge_contact_mask,
                        "bias_estimation_contact_mask": bridge_contact_mask,
                        "bias_contact_reason": bridge_bias_reason,
                        **{
                            key: csv_value(value)
                            for key, value in bias_estimate_row(baseline, bias_rate_estimate).items()
                        },
                        "control_contact_window": int(control_contact_window),
                        "last_zero_request": "" if last_zero_request is None else last_zero_request,
                        "rtde_connected": int(rtde_connected),
                        "rtde_reconnects": len(rtde_reconnect_events),
                    }
                    row.update(flatten_output(latest_output))
                    row.update({key: csv_value(latest_derived_kinematics.get(key, "")) for key in KINEMATIC_DERIVED_FIELDS})
                    row.update({key: csv_value(step4e_values.get(key, "")) for key in STEP5D_DIAG_FIELDS if key.startswith("_bridge_loop_")})
                    csv_write_start = time.perf_counter()
                    bridge_writer.writerow(row)
                    last_csv_write_s = time.perf_counter() - csv_write_start
                    bridge_writes += 1
                    bridge_write_times.append(now)
                    fresh_candidate_published = finalize_step5d_publish_history(
                        step4e_state,
                        v30_contract_profile=uses_v30_control_contract(
                            args.bridge_profile
                        ),
                        deadline_overrun_hold_active=deadline_overrun_hold_active,
                        rtde_send_succeeded=rtde_send_succeeded,
                    )
                    if fresh_candidate_published:
                        last_published_heartbeat = heartbeat
                        heartbeat += 1.0
                    p0_v8_canary_guard = bool(
                        args.bridge_profile == STEP5D_NO_CONTACT_P0_V8_STAGE_ID
                        and guard_reason is not None
                        and "p0_v8_canary_" in guard_reason
                    )
                    if p0_v8_canary_guard:
                        if p0_v8_canary_stop_sent_at is None:
                            p0_v8_canary_stop_sent_at = now
                        if p0_v8_canary_tp_acknowledged:
                            stop_reason = "p0_v8_canary_tp_stop_acknowledged"
                            break
                        if now - p0_v8_canary_stop_sent_at >= 1.0:
                            stop_reason = "p0_v8_canary_tp_stop_ack_timeout"
                            break
                    elif (
                        not v29_safety_fail_stop["enabled"]
                        and guard_reason is not None
                    ) or terminate_after_write:
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

    if args.bridge_profile == STEP5D_LIVEPREP_V24_STAGE_ID and trusted_force_norms:
        summary_normals = trusted_normals
        summary_force_norms = trusted_force_norms
        summary_torque_norms = trusted_torque_norms
    else:
        summary_normals = normals
        summary_force_norms = force_norms
        summary_torque_norms = torque_norms
    summary = {
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "stop_reason": stop_reason,
        "samples": samples,
        "bridge_writes": bridge_writes,
        "bridge_write_timing": interval_stats(bridge_write_times),
        "bridge_write_deadline": {
            "policy": "skip_missed_slots_no_burst_catchup",
            "period_s": write_period,
            "missed_slots": write_deadline_missed_slots,
            "overrun_events": write_deadline_overrun_events,
            "max_lateness_s": write_deadline_max_lateness_s,
        },
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
        "v29_safety_fail_stop": {
            key: value
            for key, value in v29_safety_fail_stop.items()
            if key != "next_dashboard_stop_attempt_mono"
        },
        "baseline_si_offsets": dict(zip(BIAS_VECTOR_NAMES, baseline)),
        "bias_rate_estimate_si_per_s": dict(zip(BIAS_VECTOR_NAMES, bias_rate_estimate)),
        "bias_estimator_logging_contract": metadata["bias_estimator_logging_contract"],
        "normal_force_stats_n": stats(summary_normals),
        "force_norm_stats_n": stats(summary_force_norms),
        "torque_norm_stats_nm": stats(summary_torque_norms),
        "raw_all_normal_force_stats_n": stats(normals),
        "raw_all_force_norm_stats_n": stats(force_norms),
        "raw_all_torque_norm_stats_nm": stats(torque_norms),
        "trusted_normal_force_stats_n": stats(trusted_normals),
        "trusted_force_norm_stats_n": stats(trusted_force_norms),
        "trusted_torque_norm_stats_nm": stats(trusted_torque_norms),
        "startup_untrusted_spikes": startup_untrusted_spikes,
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
    if args.bridge_profile == STEP5D_NO_CONTACT_P0_V8_STAGE_ID:
        summary["p0_v8_canary"] = {
            "phase_s": float(args.step5d_stop_register_canary_s),
            "stop_request_sent": p0_v8_canary_stop_sent_at is not None,
            "tp_stop_acknowledged": p0_v8_canary_tp_acknowledged,
            "stop_request_first_sent_monotonic_s": p0_v8_canary_stop_sent_at,
        }
    write_json(summary_path, summary)
    if args.bridge_profile == STEP5D_NO_CONTACT_P0_V8_STAGE_ID:
        manifest_path = args.output_dir / "bridge_run_manifest.json"
        run_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        canary_manifest = run_manifest.get("p0_v8_canary") or {}
        canary_manifest["terminal"] = {
            "stop_request_sent": p0_v8_canary_stop_sent_at is not None,
            "tp_stop_acknowledged": p0_v8_canary_tp_acknowledged,
            "stop_reason": stop_reason,
        }
        canary_manifest["summary_sha256"] = file_sha256(summary_path)
        run_manifest["p0_v8_canary"] = canary_manifest
        run_manifest["finished_at"] = summary["finished_at"]
        write_json(manifest_path, run_manifest)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if samples > 0 and parse_errors == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
