#!/usr/bin/env python3
"""Generate Step5d strict RNN ablation TP package."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from build_step4e_line_programs import CONFIG_PATH, PROGRAM_DIR, generated_at, line_cfg, load_json
from build_step4e_p0p1_programs import build_urp
from build_step5b_contact import build_script as build_step5b_script
from step_pose_contract import PRE_CONTACT_GRAVITY_DOWN_CONTRACT_ID, contract_target_rotvec_rad, validate_contract_axis
from step5_table import load_stage_frame, step5_stage
from step5d_runtime_interface import (
    STEP5D_ABLATION_V25_STAGE_ID,
    STEP5D_ABLATION_V26_STAGE_ID,
    STEP5D_ABLATION_V27_STAGE_ID,
    STEP5D_ABLATION_V28_STAGE_ID,
    STEP5D_INTERFACE_CLASS,
    STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE,
    STEP5D_STAGE25_JOINT_LAYOUT_CODE,
    STEP5D_LINE_ENTRY_PARAM_VALID_CODE,
    STEP5D_STAGE25_V27_FIX_VALIDATION_TARGET_S,
    STEP5D_STAGE25_V27_RUNTIME_LIMIT_S,
    STEP5D_STAGE25_V28_FULL_RUN_TARGET_S,
    STEP5D_STAGE25_V28_RUNTIME_LIMIT_S,
    STEP5D_TUNING_BUNDLE,
)
from tase_protocol_table import resolve_experiment_profile


@dataclass(frozen=True)
class Step5dAblationSpec:
    program_name: str
    version_label: str
    stamp_token: str
    cartesian_angular_cap_rad_s: float
    default_stage25_control_mode: str
    stage25_success_target_s: float = STEP5D_STAGE25_V27_FIX_VALIDATION_TARGET_S
    stage25_runtime_limit_s: float = STEP5D_STAGE25_V27_RUNTIME_LIMIT_S
    source_stage_id: str = "step5d_strict_rnn_liveprep_v24"

    @property
    def stage_id(self) -> str:
        return self.program_name

    @property
    def bridge_version(self) -> str:
        return self.program_name


@dataclass(frozen=True)
class LineEntryConfig:
    normal_load_min_n: float
    normal_load_max_n: float
    force_norm_max_n: float
    required_s: float
    cmd_limit_m_s: float
    timeout_s: float
    raw_sanity_min_n: float
    raw_sanity_max_n: float
    recovery_normal_load_min_n: float
    recovery_normal_load_max_n: float
    force_norm_stop_n: float


ABLATION_SPECS = {
    STEP5D_ABLATION_V25_STAGE_ID: Step5dAblationSpec(
        program_name=STEP5D_ABLATION_V25_STAGE_ID,
        version_label="v25",
        stamp_token="STEP5D_STRICT_RNN_ABLATION_V25",
        cartesian_angular_cap_rad_s=0.150,
        default_stage25_control_mode="speedl_cartesian_oracle",
    ),
    STEP5D_ABLATION_V26_STAGE_ID: Step5dAblationSpec(
        program_name=STEP5D_ABLATION_V26_STAGE_ID,
        version_label="v26",
        stamp_token="STEP5D_STRICT_RNN_ABLATION_V26",
        cartesian_angular_cap_rad_s=0.015,
        default_stage25_control_mode="speedl_cartesian_oracle",
    ),
    STEP5D_ABLATION_V27_STAGE_ID: Step5dAblationSpec(
        program_name=STEP5D_ABLATION_V27_STAGE_ID,
        version_label="v27",
        stamp_token="STEP5D_STRICT_RNN_ABLATION_V27",
        cartesian_angular_cap_rad_s=0.015,
        default_stage25_control_mode="speedl_cartesian_oracle",
    ),
    STEP5D_ABLATION_V28_STAGE_ID: Step5dAblationSpec(
        program_name=STEP5D_ABLATION_V28_STAGE_ID,
        version_label="v28",
        stamp_token="STEP5D_STRICT_RNN_ABLATION_V28",
        cartesian_angular_cap_rad_s=0.015,
        default_stage25_control_mode="speedl_cartesian_oracle",
        stage25_success_target_s=STEP5D_STAGE25_V28_FULL_RUN_TARGET_S,
        stage25_runtime_limit_s=STEP5D_STAGE25_V28_RUNTIME_LIMIT_S,
    ),
}
DEFAULT_SPEC = ABLATION_SPECS[STEP5D_ABLATION_V27_STAGE_ID]
PROGRAM_NAME = DEFAULT_SPEC.program_name
STEP5_STAGE_ID = DEFAULT_SPEC.stage_id
SOURCE_STAGE_ID = DEFAULT_SPEC.source_stage_id
BRIDGE_VERSION = DEFAULT_SPEC.bridge_version
LOCAL_PROGRAM_DIR = PROGRAM_DIR / "step5"
LOCAL_CANDIDATE_ROOT = PROGRAM_DIR.parent / "runs" / "local_tp_packages"
LOCAL_CANDIDATE_MARKER = ".local_tp_candidate.json"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
POSE_CONTRACT_ID = PRE_CONTACT_GRAVITY_DOWN_CONTRACT_ID
SEARCH_GRAVITY_DOWN_ROTVEC = contract_target_rotvec_rad(POSE_CONTRACT_ID)
_STEP5D_PROTOCOL = resolve_experiment_profile("Step5.step5d_rnn")
_STEP5D_PARAMS = _STEP5D_PROTOCOL["parameters"]
_STEP5D_LIMITS = _STEP5D_PROTOCOL["safety_limits"]
TARGET_FORCE_N = float(_STEP5D_PARAMS["target_force_n"])
BRIDGE_NORMAL_FILTER_ALPHA = float(_STEP5D_PARAMS["normal_filter_alpha"])
QDOT_CAP_RAD_S = 0.050
QDOT_CLEAR_ZERO_TOL_RAD_S = 0.0005
JOINT_ACCEL_RAD_S2 = 0.050
CARTESIAN_LINEAR_CAP_M_S = float(_STEP5D_LIMITS["speedl_linear_cap_m_s"])
CARTESIAN_ANGULAR_CAP_RAD_S = float(_STEP5D_LIMITS["speedl_angular_cap_rad_s"])
LINE_RUNTIME_LIMIT_S = float(_STEP5D_PARAMS["line_runtime_limit_s"])
DIAGNOSTIC_WINDOW_S = float(_STEP5D_PARAMS["diagnostic_window_s"])
LINE_ACCEL_M_S2 = 0.050
ORIENTATION_SKIP_ERROR_RAD = 0.069813
RAW_NORMAL_GUARD_N = 25.0
FORCE_NORM_GUARD_N = 25.0
TORQUE_NORM_GUARD_NM = 4.0
V25_LINE_ENTRY_NORMAL_LOAD_MIN_N = 10.5
V25_LINE_ENTRY_NORMAL_LOAD_MAX_N = 12.8
V25_LINE_ENTRY_RAW_SANITY_MIN_N = 9.5
V25_LINE_ENTRY_RAW_SANITY_MAX_N = 13.5
V25_LINE_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N = 20.0
V26_LINE_ENTRY_NORMAL_LOAD_MIN_N = 7.0
V26_LINE_ENTRY_NORMAL_LOAD_MAX_N = 18.0
V26_LINE_ENTRY_RAW_SANITY_MIN_N = 5.0
V26_LINE_ENTRY_RAW_SANITY_MAX_N = 20.0
V26_LINE_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N = 24.0
V27_LINE_ENTRY_NORMAL_LOAD_MIN_N = 5.0
V27_LINE_ENTRY_NORMAL_LOAD_MAX_N = 22.0
V27_LINE_ENTRY_RAW_SANITY_MIN_N = 3.0
V27_LINE_ENTRY_RAW_SANITY_MAX_N = 25.0
V27_LINE_ENTRY_FORCE_NORM_MAX_N = 35.0
V27_LINE_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N = 35.0
V27_LINE_ENTRY_FORCE_NORM_STOP_N = 35.0
LINE_ENTRY_NORMAL_LOAD_MIN_N = 10.5
LINE_ENTRY_NORMAL_LOAD_MAX_N = 12.8
LINE_ENTRY_FORCE_NORM_MAX_N = 25.0
LINE_ENTRY_REQUIRED_S = 0.100
LINE_ENTRY_CMD_LIMIT_M_S = 0.003
LINE_ENTRY_TIMEOUT_S = 10.000
BRIDGE_START_WAIT_TIMEOUT_S = 60.000
LINE_ENTRY_RAW_SANITY_MIN_N = 9.5
LINE_ENTRY_RAW_SANITY_MAX_N = 13.5
LINE_ENTRY_RECOVERY_NORMAL_LOAD_MIN_N = 0.0
LINE_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N = 20.0
LINE_ENTRY_FORCE_NORM_STOP_N = 25.0
SECOND_SEARCH_MAX_DOWN_M = 0.035
SECOND_SEARCH_NEAR_START_DEPTH_M = 0.000
SECOND_SEARCH_RUNTIME_LIMIT_S = 45.000
SECOND_SEARCH_FAR_SPEED_M_S = -0.0025
SECOND_SEARCH_NEAR_SPEED_M_S = -0.0025
ENTRY_MOVEL_ACCEL_M_S2 = 0.090
ENTRY_MOVEL_SPEED_M_S = 0.060
FIRST_SEARCH_FAR_SPEED_M_S = float(_STEP5D_PARAMS["first_search_far_speed_m_s"])
FIRST_SEARCH_NEAR_SPEED_M_S = float(_STEP5D_PARAMS["first_search_near_speed_m_s"])


def spec_for(program: str | None = None) -> Step5dAblationSpec:
    selected = program or DEFAULT_SPEC.program_name
    try:
        return ABLATION_SPECS[selected]
    except KeyError as exc:
        raise ValueError(f"unsupported Step5d ablation program: {selected}") from exc


def source_stamp(now: datetime, spec: Step5dAblationSpec = DEFAULT_SPEC) -> str:
    return now.strftime(f"%Y-%m-%dT%H%MHKT_{spec.stamp_token}")


def existing_metadata(program_dir: Path = LOCAL_PROGRAM_DIR, spec: Step5dAblationSpec = DEFAULT_SPEC) -> tuple[str, str] | None:
    script_path = program_dir / f"{spec.program_name}.script"
    if not script_path.is_file():
        return None
    text = script_path.read_text(encoding="utf-8")
    stamp_match = re.search(r"^# VERSION:\s*(\S+)\s*$", text, flags=re.M)
    generated_match = re.search(r"^# GENERATED_AT_LOCAL:\s*(\S+)\s*$", text, flags=re.M)
    if not stamp_match or not generated_match:
        return None
    return stamp_match.group(1), generated_match.group(1)


def load_safe_frame(spec: Step5dAblationSpec = DEFAULT_SPEC) -> dict:
    try:
        stage = step5_stage(spec.stage_id)
    except KeyError:
        stage = step5_stage(spec.source_stage_id)
    return load_stage_frame(stage)


def guard_value(spec: Step5dAblationSpec, key: str, default: float) -> float:
    try:
        stage = step5_stage(spec.stage_id)
    except KeyError:
        return default
    guard = stage.get("guard", {})
    if not isinstance(guard, dict):
        return default
    value = guard.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def default_line_entry_config(spec: Step5dAblationSpec) -> LineEntryConfig:
    if spec.version_label in {"v27", "v28"}:
        return LineEntryConfig(
            normal_load_min_n=V27_LINE_ENTRY_NORMAL_LOAD_MIN_N,
            normal_load_max_n=V27_LINE_ENTRY_NORMAL_LOAD_MAX_N,
            force_norm_max_n=V27_LINE_ENTRY_FORCE_NORM_MAX_N,
            required_s=LINE_ENTRY_REQUIRED_S,
            cmd_limit_m_s=LINE_ENTRY_CMD_LIMIT_M_S,
            timeout_s=LINE_ENTRY_TIMEOUT_S,
            raw_sanity_min_n=V27_LINE_ENTRY_RAW_SANITY_MIN_N,
            raw_sanity_max_n=V27_LINE_ENTRY_RAW_SANITY_MAX_N,
            recovery_normal_load_min_n=LINE_ENTRY_RECOVERY_NORMAL_LOAD_MIN_N,
            recovery_normal_load_max_n=V27_LINE_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N,
            force_norm_stop_n=V27_LINE_ENTRY_FORCE_NORM_STOP_N,
        )
    if spec.version_label == "v26":
        return LineEntryConfig(
            normal_load_min_n=V26_LINE_ENTRY_NORMAL_LOAD_MIN_N,
            normal_load_max_n=V26_LINE_ENTRY_NORMAL_LOAD_MAX_N,
            force_norm_max_n=LINE_ENTRY_FORCE_NORM_MAX_N,
            required_s=LINE_ENTRY_REQUIRED_S,
            cmd_limit_m_s=LINE_ENTRY_CMD_LIMIT_M_S,
            timeout_s=LINE_ENTRY_TIMEOUT_S,
            raw_sanity_min_n=V26_LINE_ENTRY_RAW_SANITY_MIN_N,
            raw_sanity_max_n=V26_LINE_ENTRY_RAW_SANITY_MAX_N,
            recovery_normal_load_min_n=LINE_ENTRY_RECOVERY_NORMAL_LOAD_MIN_N,
            recovery_normal_load_max_n=V26_LINE_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N,
            force_norm_stop_n=LINE_ENTRY_FORCE_NORM_STOP_N,
        )
    return LineEntryConfig(
        normal_load_min_n=V25_LINE_ENTRY_NORMAL_LOAD_MIN_N,
        normal_load_max_n=V25_LINE_ENTRY_NORMAL_LOAD_MAX_N,
        force_norm_max_n=LINE_ENTRY_FORCE_NORM_MAX_N,
        required_s=LINE_ENTRY_REQUIRED_S,
        cmd_limit_m_s=LINE_ENTRY_CMD_LIMIT_M_S,
        timeout_s=LINE_ENTRY_TIMEOUT_S,
        raw_sanity_min_n=V25_LINE_ENTRY_RAW_SANITY_MIN_N,
        raw_sanity_max_n=V25_LINE_ENTRY_RAW_SANITY_MAX_N,
        recovery_normal_load_min_n=LINE_ENTRY_RECOVERY_NORMAL_LOAD_MIN_N,
        recovery_normal_load_max_n=V25_LINE_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N,
        force_norm_stop_n=LINE_ENTRY_FORCE_NORM_STOP_N,
    )


def line_entry_config(spec: Step5dAblationSpec = DEFAULT_SPEC) -> LineEntryConfig:
    default = default_line_entry_config(spec)
    return LineEntryConfig(
        normal_load_min_n=guard_value(spec, "line_entry_normal_load_min_n", default.normal_load_min_n),
        normal_load_max_n=guard_value(spec, "line_entry_normal_load_max_n", default.normal_load_max_n),
        force_norm_max_n=guard_value(spec, "line_entry_force_norm_max_n", default.force_norm_max_n),
        required_s=guard_value(spec, "line_entry_required_s", default.required_s),
        cmd_limit_m_s=guard_value(spec, "line_entry_cmd_limit_m_s", default.cmd_limit_m_s),
        timeout_s=guard_value(spec, "line_entry_timeout_s", default.timeout_s),
        raw_sanity_min_n=guard_value(spec, "line_entry_raw_sanity_min_n", default.raw_sanity_min_n),
        raw_sanity_max_n=guard_value(spec, "line_entry_raw_sanity_max_n", default.raw_sanity_max_n),
        recovery_normal_load_min_n=guard_value(
            spec,
            "line_entry_recovery_normal_load_min_n",
            default.recovery_normal_load_min_n,
        ),
        recovery_normal_load_max_n=guard_value(
            spec,
            "line_entry_recovery_normal_load_max_n",
            default.recovery_normal_load_max_n,
        ),
        force_norm_stop_n=guard_value(spec, "line_entry_force_norm_stop_n", default.force_norm_stop_n),
    )


def bridge_start_wait_timeout_s(spec: Step5dAblationSpec = DEFAULT_SPEC) -> float:
    return guard_value(spec, "bridge_start_wait_timeout_s", BRIDGE_START_WAIT_TIMEOUT_S)


def raw_normal_guard_n(spec: Step5dAblationSpec = DEFAULT_SPEC) -> float:
    if spec.version_label == "v28":
        return 50.0
    return 35.0 if spec.version_label == "v27" else RAW_NORMAL_GUARD_N


def force_norm_guard_n(spec: Step5dAblationSpec = DEFAULT_SPEC) -> float:
    if spec.version_label == "v28":
        return 60.0
    return 35.0 if spec.version_label == "v27" else FORCE_NORM_GUARD_N


def torque_norm_guard_nm(spec: Step5dAblationSpec = DEFAULT_SPEC) -> float:
    return 3.0 if spec.version_label == "v28" else TORQUE_NORM_GUARD_NM


def _replace_exact(script: str, old: str, new: str) -> str:
    if old not in script:
        raise RuntimeError(f"Step5d ablation scaffold replacement failed: {old}")
    return script.replace(old, new, 1)


def _replace_first_present(script: str, old_candidates: tuple[str, ...], new: str, label: str) -> str:
    for old in old_candidates:
        if old in script:
            return script.replace(old, new, 1)
    raise RuntimeError(f"Step5d ablation scaffold replacement failed: {label}")


def _replace_line_stage_with_stage25_multimode(script: str, spec: Step5dAblationSpec = DEFAULT_SPEC) -> str:
    start = script.index("  if stop_reason == 0.0:\n    write_output_float_register(35, 25.0)")
    end = script.index("\n\n  if codex_should_auto_home(stop_reason):", start)
    block = f"""  if stop_reason == 0.0:
    write_output_float_register(35, 25.0)
    local last_heartbeat2 = read_input_float_register(26)
    local stale_s2 = 0.0
    local t2 = 0.0
    local qdot_cap_rad_s = {QDOT_CAP_RAD_S:.3f}
    local cartesian_linear_cap_m_s = {CARTESIAN_LINEAR_CAP_M_S:.3f}
    local cartesian_angular_cap_rad_s = {spec.cartesian_angular_cap_rad_s:.3f}
    local cartesian_accel_m_s2 = {LINE_ACCEL_M_S2:.3f}
    local joint_accel_rad_s2 = {JOINT_ACCEL_RAD_S2:.3f}
    local cartesian_layout_code = {STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE:.3f}
    local joint_layout_code = {STEP5D_STAGE25_JOINT_LAYOUT_CODE:.3f}
    # STAGE25_CADENCE_CONSUMPTION: output register 47 is 1 only when this
    # TP loop accepts a current Stage25 command packet and reaches speedl/speedj.
    while stop_reason == 0.0:
      local heartbeat2 = read_input_float_register(26)
      local cmd_valid = read_input_float_register(43)
      local progress_s = read_input_float_register(44)
      local stage25_layout_tag = read_input_float_register(47)
      local stage25_command_consumed = 0
      local cartesian_layout_ok = codex_abs(stage25_layout_tag - cartesian_layout_code) < 0.001
      local joint_layout_ok = codex_abs(stage25_layout_tag - joint_layout_code) < 0.001
      local cmd_qd0 = read_input_float_register(37)
      local cmd_qd1 = read_input_float_register(38)
      local cmd_qd2 = read_input_float_register(39)
      local cmd_qd3 = read_input_float_register(40)
      local cmd_qd4 = read_input_float_register(41)
      local cmd_qd5 = read_input_float_register(42)
      local cmd_vx = cmd_qd0
      local cmd_vy = cmd_qd1
      local cmd_vz = cmd_qd2
      local cmd_wx = cmd_qd3
      local cmd_wy = cmd_qd4
      local cmd_wz = cmd_qd5
      local loop_dt = get_steptime()
      final_progress_m = progress_s
      if cmd_valid >= 0.5 and (cartesian_layout_ok or joint_layout_ok):
        saw_cmd_valid = 1
        cmd_invalid_s = 0.0
      else:
        cmd_invalid_s = cmd_invalid_s + loop_dt
      end
      if heartbeat2 == last_heartbeat2:
        stale_s2 = stale_s2 + loop_dt
      else:
        stale_s2 = 0.0
        last_heartbeat2 = heartbeat2
      end
      t2 = t2 + loop_dt
      if progress_s >= line_success_progress_m:
        end_hold_s = end_hold_s + loop_dt
      else:
        end_hold_s = 0.0
      end
      codex_echo_step4e(stop_reason)
      if stale_s2 > 0.100:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if cmd_valid < 0.5 or not (cartesian_layout_ok or joint_layout_ok):
          write_output_float_register(47, stage25_command_consumed)
          if saw_cmd_valid == 0 and t2 < cmd_valid_grace_s:
            sync()
          elif saw_cmd_valid == 1 and cmd_invalid_s <= cmd_valid_loss_limit_s:
            sync()
          else:
            stop_reason = 12.0
          end
        elif cartesian_layout_ok and (codex_abs(cmd_vx) > cartesian_linear_cap_m_s or codex_abs(cmd_vy) > cartesian_linear_cap_m_s or codex_abs(cmd_vz) > cartesian_linear_cap_m_s or codex_abs(cmd_wx) > cartesian_angular_cap_rad_s or codex_abs(cmd_wy) > cartesian_angular_cap_rad_s or codex_abs(cmd_wz) > cartesian_angular_cap_rad_s):
          write_output_float_register(47, stage25_command_consumed)
          stop_reason = 13.0
        elif joint_layout_ok and (codex_abs(cmd_qd0) > qdot_cap_rad_s or codex_abs(cmd_qd1) > qdot_cap_rad_s or codex_abs(cmd_qd2) > qdot_cap_rad_s or codex_abs(cmd_qd3) > qdot_cap_rad_s or codex_abs(cmd_qd4) > qdot_cap_rad_s or codex_abs(cmd_qd5) > qdot_cap_rad_s):
          write_output_float_register(47, stage25_command_consumed)
          stop_reason = 13.0
        elif end_hold_s >= end_hold_required_s:
          write_output_float_register(47, stage25_command_consumed)
          stop_reason = 1.0
        elif t2 >= line_runtime_limit_s:
          write_output_float_register(47, stage25_command_consumed)
          stop_reason = 10.0
        elif cartesian_layout_ok:
          stage25_command_consumed = 1
          write_output_float_register(47, stage25_command_consumed)
          speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy, cmd_wz], cartesian_accel_m_s2, line_hold_s)
        else:
          stage25_command_consumed = 1
          write_output_float_register(47, stage25_command_consumed)
          speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5], joint_accel_rad_s2, line_hold_s)
        end
      end
    end
    stopj(0.3)
    stopl(0.1)
  end"""
    return script[:start] + block + script[end:]


def _add_orientation_skip_gate(script: str) -> str:
    if (
        "local skip_lift_attitude = 0" in script
        and f"local orientation_skip_error_rad = {ORIENTATION_SKIP_ERROR_RAD:.6f}" in script
        and "if stop_reason == 0.0 and skip_lift_attitude == 0:" in script
    ):
        return script
    start_marker = """  if stop_reason == 0.0:
    write_output_float_register(35, 25.1)
    local p_lift = get_actual_tcp_pose()
    local lift_pose = p[p_lift[0], p_lift[1], p_lift[2] + 0.020, p_lift[3], p_lift[4], p_lift[5]]
    codex_echo_step4e(stop_reason)
    movel(lift_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 25.2)"""
    if start_marker not in script:
        raise RuntimeError("Step5d v2 orientation gate insertion point not found")
    replacement = f"""  local skip_lift_attitude = 0
  if stop_reason == 0.0:
    write_output_float_register(35, 25.15)
    local orientation_skip_error_rad = {ORIENTATION_SKIP_ERROR_RAD:.6f}
    local t_skip = 0.0
    local skip_timeout_s = 1.000
    local last_heartbeat_skip = read_input_float_register(26)
    local stale_s_skip = 0.0
    while stop_reason == 0.0 and skip_lift_attitude == 0 and t_skip < skip_timeout_s:
      local heartbeat_skip = read_input_float_register(26)
      local cmd_valid = read_input_float_register(43)
      local orientation_error = read_input_float_register(46)
      local loop_dt = get_steptime()
      if heartbeat_skip == last_heartbeat_skip:
        stale_s_skip = stale_s_skip + loop_dt
      else:
        stale_s_skip = 0.0
        last_heartbeat_skip = heartbeat_skip
      end
      t_skip = t_skip + loop_dt
      codex_echo_step4e(stop_reason)
      if stale_s_skip > 0.100:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if cmd_valid >= 0.5 and orientation_error <= orientation_skip_error_rad:
          skip_lift_attitude = 1
        elif cmd_valid >= 0.5:
          t_skip = skip_timeout_s
        else:
          sync()
        end
      end
    end
  end

  if stop_reason == 0.0 and skip_lift_attitude == 0:
    write_output_float_register(35, 25.1)
    local p_lift = get_actual_tcp_pose()
    local lift_pose = p[p_lift[0], p_lift[1], p_lift[2] + 0.020, p_lift[3], p_lift[4], p_lift[5]]
    codex_echo_step4e(stop_reason)
    movel(lift_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
  end

  if stop_reason == 0.0 and skip_lift_attitude == 0:
    write_output_float_register(35, 25.2)"""
    return script.replace(start_marker, replacement, 1)


def _replace_line_entry_with_force_settle(script: str, spec: Step5dAblationSpec = DEFAULT_SPEC) -> str:
    line_entry = line_entry_config(spec)
    start = script.index("  if stop_reason == 0.0:\n    write_output_float_register(35, 25.3)")
    end = script.index("\n\n  if stop_reason == 0.0:\n    write_output_float_register(35, 25.0)", start)
    block = f"""  if stop_reason == 0.0:
    write_output_float_register(35, 25.3)
    local last_heartbeat_entry = read_input_float_register(26)
    local stale_s_entry = 0.0
    local t_entry = 0.0
    local line_entry_s = 0.0
    local line_entry_default_required_s = {line_entry.required_s:.3f}
    local line_entry_default_timeout_s = {line_entry.timeout_s:.3f}
    local line_entry_default_normal_load_min_n = {line_entry.normal_load_min_n:.3f}
    local line_entry_default_normal_load_max_n = {line_entry.normal_load_max_n:.3f}
    local line_entry_default_force_norm_max_n = {line_entry.force_norm_max_n:.3f}
    local line_entry_recovery_normal_load_min_n = {line_entry.recovery_normal_load_min_n:.3f}
    local line_entry_recovery_normal_load_max_n = {line_entry.recovery_normal_load_max_n:.3f}
    local line_entry_force_norm_stop_n = {line_entry.force_norm_stop_n:.3f}
    local line_entry_cmd_limit_m_s = {line_entry.cmd_limit_m_s:.3f}
    local line_entry_param_valid_code = {STEP5D_LINE_ENTRY_PARAM_VALID_CODE:.3f}
    saw_cmd_valid = 0
    cmd_invalid_s = 0.0
    while stop_reason == 0.0:
      local heartbeat_entry = read_input_float_register(26)
      local cmd_valid = read_input_float_register(43)
      local cmd_vx = read_input_float_register(37)
      local cmd_vy = read_input_float_register(38)
      local cmd_vz = read_input_float_register(39)
      local target_force = read_input_float_register(29)
      local force_error = read_input_float_register(45)
      local normal_load = target_force - force_error
      local force_norm = read_input_float_register(25)
      local loop_dt = get_steptime()
      local line_entry_required_s = line_entry_default_required_s
      local line_entry_timeout_s = line_entry_default_timeout_s
      local line_entry_normal_load_min_n = line_entry_default_normal_load_min_n
      local line_entry_normal_load_max_n = line_entry_default_normal_load_max_n
      local line_entry_force_norm_max_n = line_entry_default_force_norm_max_n
      local line_entry_param_valid = read_input_float_register(47)
      if codex_abs(line_entry_param_valid - line_entry_param_valid_code) < 0.001:
        local candidate_min_n = read_input_float_register(40)
        local candidate_max_n = read_input_float_register(41)
        local candidate_force_norm_max_n = read_input_float_register(42)
        local candidate_required_s = read_input_float_register(44)
        local candidate_timeout_s = read_input_float_register(46)
        if candidate_min_n >= 0.0 and candidate_max_n >= candidate_min_n and candidate_force_norm_max_n > 0.0 and candidate_force_norm_max_n <= line_entry_force_norm_stop_n and candidate_required_s >= 0.0 and candidate_timeout_s > candidate_required_s:
          line_entry_normal_load_min_n = candidate_min_n
          line_entry_normal_load_max_n = candidate_max_n
          line_entry_force_norm_max_n = candidate_force_norm_max_n
          line_entry_required_s = candidate_required_s
          line_entry_timeout_s = candidate_timeout_s
        end
      end
      if cmd_valid >= 0.5:
        saw_cmd_valid = 1
        cmd_invalid_s = 0.0
      else:
        cmd_invalid_s = cmd_invalid_s + loop_dt
      end
      if heartbeat_entry == last_heartbeat_entry:
        stale_s_entry = stale_s_entry + loop_dt
      else:
        stale_s_entry = 0.0
        last_heartbeat_entry = heartbeat_entry
      end
      t_entry = t_entry + loop_dt
      if cmd_valid >= 0.5 and normal_load >= line_entry_normal_load_min_n and normal_load <= line_entry_normal_load_max_n and force_norm <= line_entry_force_norm_max_n:
        line_entry_s = line_entry_s + loop_dt
      else:
        line_entry_s = 0.0
      end
      codex_echo_step4e(stop_reason)
      if stale_s_entry > 0.100:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if line_entry_s >= line_entry_required_s:
          stop_reason = 16.0
        elif force_norm > line_entry_force_norm_stop_n or normal_load > line_entry_recovery_normal_load_max_n:
          stop_reason = 17.0
        elif cmd_valid < 0.5:
          if saw_cmd_valid == 0 and t_entry < cmd_valid_grace_s:
            sync()
          elif saw_cmd_valid == 1 and cmd_invalid_s <= cmd_valid_loss_limit_s:
            sync()
          else:
            stop_reason = 12.0
          end
        elif codex_abs(cmd_vx) > line_entry_cmd_limit_m_s or codex_abs(cmd_vy) > line_entry_cmd_limit_m_s or codex_abs(cmd_vz) > line_entry_cmd_limit_m_s:
          stop_reason = 13.0
        elif t_entry >= line_entry_timeout_s:
          stop_reason = 12.0
        else:
          speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
        end
      end
    end
    stopl(0.1)
    if stop_reason == 16.0:
      stop_reason = 0.0
      saw_cmd_valid = 0
      cmd_invalid_s = 0.0
      end_hold_s = 0.0
      write_output_float_register(35, 25.95)
      local register_clear_required_s = 0.006
      local register_clear_timeout_s = 1.000
      local register_clear_s = 0.0
      local register_clear_t = 0.0
      local register_clear_zero_tol = {QDOT_CLEAR_ZERO_TOL_RAD_S:.6f}
      local last_heartbeat_clear = read_input_float_register(26)
      local stale_s_clear = 0.0
      while stop_reason == 0.0 and register_clear_s < register_clear_required_s and register_clear_t < register_clear_timeout_s:
        local heartbeat_clear = read_input_float_register(26)
        local clear_cmd_valid = read_input_float_register(43)
        local clear_reg0 = read_input_float_register(37)
        local clear_reg1 = read_input_float_register(38)
        local clear_reg2 = read_input_float_register(39)
        local clear_reg3 = read_input_float_register(40)
        local clear_reg4 = read_input_float_register(41)
        local clear_reg5 = read_input_float_register(42)
        local clear_layout_tag = read_input_float_register(47)
        local loop_dt = get_steptime()
        if heartbeat_clear == last_heartbeat_clear:
          stale_s_clear = stale_s_clear + loop_dt
        else:
          stale_s_clear = 0.0
          last_heartbeat_clear = heartbeat_clear
        end
        register_clear_t = register_clear_t + loop_dt
        codex_echo_step4e(stop_reason)
        if stale_s_clear > 0.100:
          stop_reason = 2.0
        else:
          stop_reason = codex_step4e_guard_stop_reason()
        end
        if stop_reason == 0.0:
          if clear_cmd_valid < 0.5 and codex_abs(clear_layout_tag - line_entry_param_valid_code) >= 0.001 and codex_abs(clear_layout_tag - {STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE:.3f}) >= 0.001 and codex_abs(clear_layout_tag - {STEP5D_STAGE25_JOINT_LAYOUT_CODE:.3f}) >= 0.001 and codex_abs(clear_reg0) <= register_clear_zero_tol and codex_abs(clear_reg1) <= register_clear_zero_tol and codex_abs(clear_reg2) <= register_clear_zero_tol and codex_abs(clear_reg3) <= register_clear_zero_tol and codex_abs(clear_reg4) <= register_clear_zero_tol and codex_abs(clear_reg5) <= register_clear_zero_tol:
            register_clear_s = register_clear_s + loop_dt
          else:
            register_clear_s = 0.0
          end
          sync()
        end
      end
      if stop_reason == 0.0 and register_clear_s < register_clear_required_s:
        stop_reason = 12.0
      end
    end
  end"""
    return script[:start] + block + script[end:]


def _replace_second_contact_search(script: str) -> str:
    old = "stop_reason = codex_step5d_down_search(24.3, 24.4, 0.070, 0.040, 45.000, -0.005, -0.003)"
    new = (
        "stop_reason = codex_step5d_down_search("
        f"24.3, 24.4, {SECOND_SEARCH_MAX_DOWN_M:.3f}, {SECOND_SEARCH_NEAR_START_DEPTH_M:.3f}, "
        f"{SECOND_SEARCH_RUNTIME_LIMIT_S:.3f}, {SECOND_SEARCH_FAR_SPEED_M_S:.4f}, {SECOND_SEARCH_NEAR_SPEED_M_S:.4f})"
    )
    return _replace_exact(script, old, new)


def _speed_up_entry_and_first_search(script: str) -> str:
    script = _replace_exact(
        script,
        "movel(entry_xy_pose, a=0.060, v=0.040, r=0.0)",
        f"movel(entry_xy_pose, a={ENTRY_MOVEL_ACCEL_M_S2:.3f}, v={ENTRY_MOVEL_SPEED_M_S:.3f}, r=0.0)",
    )
    return _replace_exact(
        script,
        "stop_reason = codex_step5d_down_search(24.0, 24.2, first_search_max_down_m, first_search_near_start_depth_m, 40.000, -0.015, -0.0025)",
        "stop_reason = codex_step5d_down_search("
        "24.0, 24.2, first_search_max_down_m, first_search_near_start_depth_m, "
        f"40.000, {FIRST_SEARCH_FAR_SPEED_M_S:.4f}, {FIRST_SEARCH_NEAR_SPEED_M_S:.4f})",
    )


def _force_gravity_down_search_pose(script: str) -> str:
    validate_contract_axis(POSE_CONTRACT_ID)
    rx, ry, rz = SEARCH_GRAVITY_DOWN_ROTVEC
    pattern = (
        r"  local target_rx = [-0-9.]+\n"
        r"  local target_ry = [-0-9.]+\n"
        r"  local target_rz = [-0-9.]+"
    )
    replacement = (
        f"  # PRECONTACT_POSE_CONTRACT: {POSE_CONTRACT_ID}; Stage22/24 TCP +Z targets base -Z.\n"
        f"  local target_rx = {rx:.9f}\n"
        f"  local target_ry = {ry:.9f}\n"
        f"  local target_rz = {rz:.9f}"
    )
    script, count = re.subn(pattern, replacement, script, count=1)
    if count != 1:
        raise RuntimeError("Step5d ablation gravity-down pose contract replacement failed")
    return script


def _add_force_envelope_auto_home(script: str) -> str:
    old = """  elif stop_reason == 14.0:
    return True
  end"""
    new = """  elif stop_reason == 14.0:
    return True
  elif stop_reason == 17.0:
    return True
  end"""
    return _replace_exact(script, old, new)


def _add_down_search_force_trigger_echo(script: str) -> str:
    old = """    if normal_force <= -1.0 or force_norm > 1.5:
      stop_reason = 11.0"""
    new = """    if normal_force <= -1.0 or force_norm > 1.5:
      stop_reason = 11.0
      if search_depth_m < search_near_start_depth_m:
        write_output_float_register(35, search_stage)
      else:
        write_output_float_register(35, near_stage)
      end
      codex_echo_step4e(stop_reason)"""
    return _replace_exact(script, old, new)


def build_script(
    stamp: str,
    gen_at: str,
    geom: dict[str, float],
    frame: dict,
    spec: Step5dAblationSpec = DEFAULT_SPEC,
) -> str:
    bridge_wait_timeout_s = bridge_start_wait_timeout_s(spec)
    line_entry = line_entry_config(spec)
    script = build_step5b_script(stamp, gen_at, geom, frame, variant="v3")
    scaffold_mutated_stamp = stamp.replace(spec.version_label.upper(), "V31").replace(spec.version_label, "v31")
    script = script.replace(scaffold_mutated_stamp, stamp)
    script = script.replace("step5b_contact_cycloid_baseline_v1", spec.program_name)
    script = script.replace("step5b_contact_cycloid_baseline_v2", spec.program_name)
    script = script.replace("step5b_contact_cycloid_baseline_v3", spec.program_name)
    script = script.replace("Step5b contact cycloid baseline v1", f"Step5d strict RNN ablation {spec.version_label}")
    script = script.replace("Step5b contact cycloid baseline v2", f"Step5d strict RNN ablation {spec.version_label}")
    script = script.replace("Step5b contact cycloid baseline v3", f"Step5d strict RNN ablation {spec.version_label}")
    script = script.replace("STEP5B_CONTACT_CYCLOID_BASELINE_V1", spec.stamp_token)
    script = script.replace("STEP5B_CONTACT_CYCLOID_BASELINE_V2", spec.stamp_token)
    script = script.replace("STEP5B_CONTACT_CYCLOID_BASELINE_V3", spec.stamp_token)
    script = script.replace("STEP5D_STRICT_RNN_LIVEPREP_V31", spec.stamp_token)
    script = script.replace("codex_step5b_down_search", "codex_step5d_down_search")
    script = script.replace("step4e-version=step5b_v1", f"step4e-version={spec.bridge_version}")
    script = script.replace("step4e-version=step5b_v2", f"step4e-version={spec.bridge_version}")
    script = script.replace("step4e-version=step5b_v3", f"step4e-version={spec.bridge_version}")
    script = script.replace("step4e-normal-filter-alpha=0.55", f"step4e-normal-filter-alpha={BRIDGE_NORMAL_FILTER_ALPHA:.2f}")
    raw_guard = raw_normal_guard_n(spec)
    force_guard = force_norm_guard_n(spec)
    torque_guard = torque_norm_guard_nm(spec)
    script = script.replace("codex_abs(normal_force) > 50.0", f"codex_abs(normal_force) > {raw_guard:.1f}")
    script = script.replace("force_norm > 60.0", f"force_norm > {force_guard:.1f}")
    script = script.replace("torque_norm > 3.0", f"torque_norm > {torque_guard:.1f}")
    script = script.replace(
        "# SAFETY: raw normal guard 50 N, force norm guard 60 N, torque guard 3.0 Nm.",
        f"# SAFETY: raw normal guard {raw_guard:.0f} N, force norm guard {force_guard:.0f} N, torque guard {torque_guard:.1f} Nm.",
    )
    script = script.replace(
        "local line_runtime_limit_s = 65.000",
        f"local line_runtime_limit_s = {spec.stage25_runtime_limit_s:.3f}",
    )
    script = script.replace(
        "local line_success_progress_m = 60.000000000",
        f"local line_success_progress_m = {spec.stage25_success_target_s:.9f}",
    )
    script = script.replace(
        "codex_wait_for_fresh_heartbeat(30.0)",
        f"codex_wait_for_fresh_heartbeat({bridge_wait_timeout_s:.1f})",
    )
    script = _replace_first_present(
        script,
        (
            "PURPOSE: v31 contact search, first-contact normal latch, lift, 25.2 attitude correction, 25.3 line-entry gate, then Step5 table-driven contact cycloid reference for 60 s.",
            "PURPOSE: v31 contact search, first-contact normal latch, optional 4deg skip-lift/25.2 gate, otherwise lift and 25.2 attitude correction, 25.3 line-entry gate, then Step5 table-driven contact cycloid reference for 60 s.",
            "PURPOSE: v31 contact search, first-contact normal latch, no lift/25.2 attitude cycle and no second contact search, 25.3 line-entry gate, then Step5 table-driven contact cycloid reference for 60 s.",
        ),
        f"PURPOSE: v31 contact search with Stage22/24 gravity-down pre-contact posture, first-contact normal latch, no lift/25.2 attitude cycle and no second contact search, 25.3 bridge deadband acquire into the {line_entry.normal_load_min_n:.1f}-{line_entry.normal_load_max_n:.1f}N filtered preload window with {line_entry.raw_sanity_min_n:.1f}-{line_entry.raw_sanity_max_n:.1f}N raw sanity and bridge-time preload parameter channel, 25.95 register clear barrier, then {spec.version_label} Step5d ablation Stage25.0 multi-layout speedl/speedj {('full-run' if spec.version_label == 'v28' else 'diagnostic')} for {spec.stage25_success_target_s:g} s.",
        "purpose",
    )
    script = _force_gravity_down_search_pose(script)
    script = _speed_up_entry_and_first_search(script)
    script = script.replace(
        "25.0 uses desired_velocity + path_p_gain*(desired-actual) before normal projection and force-loop composition.",
        "25.0 uses layout-tagged registers 37..42: Cartesian speedl oracle, DLS speedj oracle, or strict RNN speedj live; bridge owns calibrated Pinocchio/J(q), paper outer-loop computation, and RNN shadow diagnostics.",
    )
    script = script.replace(
        "TP_ROLE: executor_and_guard_only; Step5 trajectory reference is computed by the bridge.",
        (
            f"TP_ROLE: multimode_executor_and_guard_only; Step5d {spec.version_label} command layout is computed by the bridge.\n"
            f"# REGISTER_CONTRACT: Stage 25.3 consumes 37..39 as Cartesian deadband-acquire vx/vy/vz, plus {spec.version_label} preload overrides in 40/41/42/44/46/47; Stage 25.95 requires bridge-cleared registers 37..47 before Stage 25.0. Stage 25.0 reads register 47 as layout tag: {STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE:.1f}=Cartesian speedl vx/vy/vz/wx/wy/wz in 37..42, {STEP5D_STAGE25_JOINT_LAYOUT_CODE:.1f}=joint speedj qd0..qd5 in 37..42; 43 cmd_valid, 44 path_time_s.\n"
            "# PRECONTACT_POSE_CONTRACT: config/step_pose_contract_table.json pre_contact_search_gravity_down_v1; Stage22/24 TCP +Z targets base -Z using [pi,0,0].\n"
            f"# STAGE25_CONTACT_SAFETY: {spec.version_label} bridge keeps {raw_guard:.0f}N normal/{force_guard:.0f}N force guards and {torque_guard:.1f}Nm torque guard; speedl_cartesian_oracle may re-press within bounded low-load timers while RNN is shadow-only, while joint modes keep stricter low-load stopping and RNN/J(q) diagnostics.\n"
            "# FORCE_FRAME_CONTRACT: UR_FORCE_FRAME_CONTRACT.md; reaction normal for load, approach normal for posture."
        ),
    )
    if spec.version_label == "v27":
        script = script.replace(
            "# FORCE_FRAME_CONTRACT: UR_FORCE_FRAME_CONTRACT.md; reaction normal for load, approach normal for posture.",
            (
                "# FORCE_FRAME_CONTRACT: UR_FORCE_FRAME_CONTRACT.md; reaction normal for load, approach normal for posture.\n"
                "# STAGE25_V27_SCAFFOLD: step5b_v3_scaffold_min_delta; only Stage25 command source becomes Cartesian speedl oracle."
            ),
            1,
        )
    if spec.version_label == "v28":
        script = script.replace(
            "# FORCE_FRAME_CONTRACT: UR_FORCE_FRAME_CONTRACT.md; reaction normal for load, approach normal for posture.",
            (
                "# FORCE_FRAME_CONTRACT: UR_FORCE_FRAME_CONTRACT.md; reaction normal for load, approach normal for posture.\n"
                "# STAGE25_V28_SCAFFOLD: v27_step5b_speedl_live_shadow_boundary_60s_full_run"
            ),
            1,
        )
    script = _replace_exact(script, "STEP5_STAGE_ID: step5_contact_cycloid_baseline_v1", f"STEP5_STAGE_ID: {spec.stage_id}")
    script = _add_down_search_force_trigger_echo(script)
    script = _add_force_envelope_auto_home(script)
    script = _replace_line_entry_with_force_settle(script, spec)
    script = _replace_line_stage_with_stage25_multimode(script, spec)
    if f"def codex_{spec.program_name}()" not in script:
        raise RuntimeError("Step5d ablation function rename failed")
    return script


def build_txt(stamp: str, spec: Step5dAblationSpec = DEFAULT_SPEC) -> str:
    bridge_wait_timeout_s = bridge_start_wait_timeout_s(spec)
    line_entry = line_entry_config(spec)
    speedl_mode_description = (
        "STEP5D_STAGE25_CONTROL_MODE=speedl_cartesian_oracle keeps the proven Step5b speedl live force/linear source, forces live wx/wy/wz to zero, and records Step5d paper/RNN linear and angular outputs as shadow-only diagnostics; RNN is shadow-only in this mode."
        if spec.version_label in {"v27", "v28"}
        else "STEP5D_STAGE25_CONTROL_MODE=speedl_cartesian_oracle sends the paper outer-loop xdot_c directly to speedl; RNN is shadow-only and strict RNN/J(q) qdot is logged only as diagnostics."
    )
    if spec.version_label == "v25":
        first_run = (
            "First live run must use speedl_cartesian_oracle. Acceptance target is Stage25.0\n"
            "  continuous runtime >=5 s, load mostly 8..14 N, trusted force max <20 N, and no\n"
            "  cage/hard-guard stop. If speedl succeeds but RNN shadow direction or magnitude\n"
            "  is abnormal, the defect is isolated to the RNN/Jacobian/qdot layer."
        )
    elif spec.version_label == "v27":
        first_run = (
            "v27 starts from the proven Step5b v3 scaffold with a minimal delta: Stage25.0\n"
            "  command source is the Cartesian speedl oracle, strict RNN/J(q) stays shadow-only,\n"
            "  and Stage25.0 cadence/command-consumption instrumentation is logged for diagnosis."
        )
    elif spec.version_label == "v28":
        first_run = (
            "v28 extends the successful v27 Step5b speedl live / Step5d shadow boundary to 60s:\n"
            "  Stage25.0 live vx/vy/vz remain the Step5b speedl force loop, live wx/wy/wz remain zero,\n"
            "  and Step5d paper/RNN linear/angular outputs remain shadow-only diagnostics."
        )
    else:
        first_run = (
            "v26 first live mode defaults to speedl_cartesian_oracle. The bridge sends\n"
            "  paper outer-loop xdot_c directly to TP speedl while strict RNN/J(q) qdot stays\n"
            "  shadow-only. speedj_rnn_live remains an explicit follow-up mode with\n"
            "  joint-feasibility-scaled xdot_c after speedl passes."
        )
    raw_guard = raw_normal_guard_n(spec)
    force_guard = force_norm_guard_n(spec)
    return f"""Step5d strict RNN ablation {spec.version_label} 12N diagnostic TP package

Open on Teach Pendant after controller read-back is verified:
  {CONTROLLER_DIR}/{spec.program_name}.urp

Version:
  {stamp}

Boundary:
  Contact-capable {spec.version_label} ablation package; not a completed reproduction claim.
  Reuses the Step5b v3 contact-search/latch/25.3 scaffold.
  {spec.version_label} scaffold delta: Step5b v3 timing/search/scaffold is retained, and only
  Stage25.0 command consumption changes to the layout-tagged Step5d ablation source.
  If TP Play is pressed before the Ubuntu bridge is started, Stage 20 waits up
  to {bridge_wait_timeout_s:.1f} s for a fresh bridge heartbeat before timing out.
  Stage 22 entry and Stage 24 far/near search use the shared pre-contact pose
  contract {POSE_CONTRACT_ID}: TCP +Z targets base -Z with rotvec
  [{SEARCH_GRAVITY_DOWN_ROTVEC[0]:.9f}, {SEARCH_GRAVITY_DOWN_ROTVEC[1]:.9f}, {SEARCH_GRAVITY_DOWN_ROTVEC[2]:.9f}].
  After first-contact latch, it does not run the 20 mm lift, 25.2 attitude
  correction, or 24.3/24.4 second contact search.
  no lift and no second contact search remain deliberate requirements.
  Stage 25.3 is deadband contact acquire: bridge filters normal_load and
  commands only locked-normal Cartesian vx/vy/vz in registers 37..39. It may
  actively recover while raw normal_load is between {line_entry.recovery_normal_load_min_n:.1f} N and {line_entry.recovery_normal_load_max_n:.1f} N,
  with force_norm <= {line_entry.force_norm_stop_n:.1f} N.
  {spec.version_label} Stage 25.3 accepts a bridge-time preload parameter channel when register
  47 equals {STEP5D_LINE_ENTRY_PARAM_VALID_CODE:.1f}: registers 40/41 are filtered
  min/max, 42 is force_norm max, 44 is hold time, and 46 is timeout.
  TP enters 25.0 only after the bridge-side preload register reports
  filtered normal_load between {line_entry.normal_load_min_n:.1f} N and {line_entry.normal_load_max_n:.1f} N,
  raw normal_load is sanity-checked between {line_entry.raw_sanity_min_n:.1f} N and {line_entry.raw_sanity_max_n:.1f} N,
  with force_norm <= {line_entry.force_norm_max_n:.1f} N,
  and bridge cmd_valid is true for {line_entry.required_s:.3f} s.
  Before Stage 25.0, Stage 25.95 requires the bridge to clear registers 37..47
  with cmd_valid=0 and registers 37..42 near zero (<= {QDOT_CLEAR_ZERO_TOL_RAD_S:.6f}) so stale 25.3 preload parameters or old Stage25 commands cannot be interpreted as current command.
  Stage 25.0 supports Cartesian speedl layout and joint speedj layout:
  register 47={STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE:.1f} means 37..42 are vx/vy/vz/wx/wy/wz for TP speedl,
  while register 47={STEP5D_STAGE25_JOINT_LAYOUT_CODE:.1f} means 37..42 are qd0..qd5 for TP speedj.
  Stage25.0 cadence/command-consumption instrumentation: output register 47
  is 1 only when the TP loop accepts a current Stage25 command packet and
  reaches speedl/speedj; bridge CSV records echo tag, cmd_valid, command norm,
  row gap, and loop recv/compute/send/csv timing.
  Bridge control modes:
    {speedl_mode_description}
    STEP5D_STAGE25_CONTROL_MODE=speedj_dls_oracle sends a DLS/Jacobian qdot oracle to speedj and keeps strict RNN shadow diagnostics.
    STEP5D_STAGE25_CONTROL_MODE=speedj_rnn_live sends strict RNN qdot to speedj.
  {first_run}
  stop_request remains hard for operational over-load, cage margin exhaustion,
  semantic failure, hard force/torque/joint/sensor gates, heartbeat/cmd_valid
  failure, Dashboard mismatch, or timeout.
  Stage 25.0 diagnostic progress target is {spec.stage25_success_target_s:g} s.
  Stage 22 entry movel is 0.060 m/s at 0.090 m/s^2; Stage 24 far search is
  {abs(FIRST_SEARCH_FAR_SPEED_M_S):.4f} m/s down, near search remains {abs(FIRST_SEARCH_NEAR_SPEED_M_S):.4f} m/s.

Bridge profile:
  --step4e-version {spec.bridge_version} --step4e-path-shape cycloid
  --target-force-n {TARGET_FORCE_N:.1f}
  --step4e-normal-follow-mode filtered_live
  --step4e-normal-filter-alpha {BRIDGE_NORMAL_FILTER_ALPHA:.2f}
  --step4e-normal-min-force-n 2.0

Safety:
  speedl Cartesian linear cap: {CARTESIAN_LINEAR_CAP_M_S:.3f} m/s
  speedl Cartesian angular cap: {spec.cartesian_angular_cap_rad_s:.3f} rad/s
  qdot cap: {QDOT_CAP_RAD_S:.3f} rad/s
  speedj acceleration: {JOINT_ACCEL_RAD_S2:.3f} rad/s^2
  Raw normal guard: {raw_guard:.0f} N. Force norm guard: {force_guard:.0f} N. Torque guard: {torque_norm_guard_nm(spec):.1f} Nm.
  {raw_guard:.0f} N raw-normal/{force_guard:.0f} N force-norm and {torque_norm_guard_nm(spec):.1f} Nm torque are sensor hard guards only;
  human safety still depends on the external cage/operator/E-stop boundary.
  No UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.
  This package is not a bridge-start or TP-Play authorization.

Reference:
  STEP5_FLOW.md
  config/step5_stage_table.json stage {spec.stage_id}
  config/step_pose_contract_table.json contract {POSE_CONTRACT_ID}
  UR_FORCE_FRAME_CONTRACT.md
  config/step5d_liveprep_solver_gate.json
"""


def validate_package(script: str, txt: str, urp: bytes, stamp: str, spec: Step5dAblationSpec = DEFAULT_SPEC) -> None:
    bridge_wait_timeout_s = bridge_start_wait_timeout_s(spec)
    line_entry = line_entry_config(spec)
    raw_guard = raw_normal_guard_n(spec)
    force_guard = force_norm_guard_n(spec)
    torque_guard = torque_norm_guard_nm(spec)
    xml = gzip.decompress(urp).decode("utf-8")
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": f'URProgram name="{spec.program_name}"' in xml,
        "controller directory": f'directory="{CONTROLLER_DIR}"' in xml,
        "script file": f"{CONTROLLER_DIR}/{spec.program_name}.script" in xml,
        "cached stamp": stamp in xml,
        "function name": f"def codex_{spec.program_name}()" in script,
        "step5 flow": "STEP5_FLOW.md" in script and "STEP5_FLOW.md" in txt,
        "step5d stage": spec.stage_id in script and spec.stage_id in txt,
        "bridge start wait timeout": f"codex_wait_for_fresh_heartbeat({bridge_wait_timeout_s:.1f})" in script
        and f"to {bridge_wait_timeout_s:.1f} s for a fresh bridge heartbeat" in txt,
        "bridge profile": f"step4e-version={spec.bridge_version}" in script
        and f"--step4e-version {spec.bridge_version}" in txt,
        "multimode executor role": "multimode_executor_and_guard_only" in script,
        "layout tag read": "local stage25_layout_tag = read_input_float_register(47)" in script,
        "cartesian layout code": f"local cartesian_layout_code = {STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE:.3f}" in script
        and f"register 47={STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE:.1f}" in txt,
        "joint layout code": f"local joint_layout_code = {STEP5D_STAGE25_JOINT_LAYOUT_CODE:.3f}" in script
        and f"register 47={STEP5D_STAGE25_JOINT_LAYOUT_CODE:.1f}" in txt,
        "qdot register reads": all(f"local cmd_qd{idx} = read_input_float_register({37 + idx})" in script for idx in range(6)),
        "cartesian aliases": all(name in script for name in ("local cmd_vx = cmd_qd0", "local cmd_wz = cmd_qd5")),
        "speedl line control": "speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy, cmd_wz]" in script,
        "speedj line control": "speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]" in script,
        "cartesian caps": f"local cartesian_linear_cap_m_s = {CARTESIAN_LINEAR_CAP_M_S:.3f}" in script
        and f"local cartesian_angular_cap_rad_s = {spec.cartesian_angular_cap_rad_s:.3f}" in script,
        "qdot cap": f"local qdot_cap_rad_s = {QDOT_CAP_RAD_S:.3f}" in script,
        "Stage25 ablation note": "STAGE25_CONTACT_SAFETY" in script
        and "speedl_cartesian_oracle" in txt
        and "speedj_dls_oracle" in txt
        and "speedj_rnn_live" in txt
        and "RNN is shadow-only" in txt
        and "cage margin exhaustion" in script + txt
        and "stop_request" in script + txt,
        "Stage25 consumption instrumentation": (
            spec.version_label not in {"v27", "v28"}
            or (
                "STAGE25_CADENCE_CONSUMPTION" in script
                and "local stage25_command_consumed = 0" in script
                and "write_output_float_register(47, stage25_command_consumed)" in script
                and "Stage25.0 cadence/command-consumption instrumentation" in txt
            )
        ),
        "gravity-down pose contract": f"PRECONTACT_POSE_CONTRACT: {POSE_CONTRACT_ID}" in script
        and "config/step_pose_contract_table.json" in script + txt
        and f"local target_rx = {SEARCH_GRAVITY_DOWN_ROTVEC[0]:.9f}" in script
        and f"local target_ry = {SEARCH_GRAVITY_DOWN_ROTVEC[1]:.9f}" in script
        and f"local target_rz = {SEARCH_GRAVITY_DOWN_ROTVEC[2]:.9f}" in script
        and "TCP +Z targets base -Z" in script + txt,
        "no lift attitude cycle": "local skip_lift_attitude = 0" not in script
        and "write_output_float_register(35, 25.1)" not in script
        and "write_output_float_register(35, 25.2)" not in script,
        "force-settle entry gate": f"local line_entry_default_normal_load_min_n = {line_entry.normal_load_min_n:.3f}" in script
        and f"local line_entry_default_normal_load_max_n = {line_entry.normal_load_max_n:.3f}" in script
        and f"local line_entry_default_force_norm_max_n = {line_entry.force_norm_max_n:.3f}" in script
        and f"normal_load between {line_entry.normal_load_min_n:.1f} N and {line_entry.normal_load_max_n:.1f} N" in txt
        and f"raw normal_load is sanity-checked between {line_entry.raw_sanity_min_n:.1f} N and {line_entry.raw_sanity_max_n:.1f} N" in txt
        and f"local line_entry_default_required_s = {line_entry.required_s:.3f}" in script
        and f"local line_entry_default_timeout_s = {line_entry.timeout_s:.3f}" in script
        and f"local line_entry_recovery_normal_load_min_n = {line_entry.recovery_normal_load_min_n:.3f}" in script
        and f"local line_entry_recovery_normal_load_max_n = {line_entry.recovery_normal_load_max_n:.3f}" in script
        and f"local line_entry_force_norm_stop_n = {line_entry.force_norm_stop_n:.3f}" in script
        and f"local line_entry_param_valid_code = {STEP5D_LINE_ENTRY_PARAM_VALID_CODE:.3f}" in script
        and "local normal_load = target_force - force_error" in script
        and "local candidate_min_n = read_input_float_register(40)" in script
        and "local candidate_required_s = read_input_float_register(44)" in script
        and "local candidate_timeout_s = read_input_float_register(46)" in script
        and "stop_reason = 17.0" in script
        and "speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0]" in script,
        "register clear barrier": "write_output_float_register(35, 25.95)" in script
        and "local register_clear_required_s = 0.006" in script
        and "clear_cmd_valid < 0.5" in script
        and f"codex_abs(clear_layout_tag - line_entry_param_valid_code) >= 0.001" in script
        and f"codex_abs(clear_layout_tag - {STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE:.3f}) >= 0.001" in script
        and f"codex_abs(clear_layout_tag - {STEP5D_STAGE25_JOINT_LAYOUT_CODE:.3f}) >= 0.001" in script
        and f"local register_clear_zero_tol = {QDOT_CLEAR_ZERO_TOL_RAD_S:.6f}" in script
        and "qdot_clear_cap_rad_s" not in script
        and "qdot_clear_required_s" not in script
        and f"<= register_clear_zero_tol" in script
        and "if cmd_valid < 0.5 or not (cartesian_layout_ok or joint_layout_ok)" in script
        and "Stage 25.95 requires the bridge to clear registers 37..47" in txt
        and "near zero" in txt,
        "no second contact search": "codex_step5d_down_search(24.3, 24.4" not in script,
        "force envelope auto-home": "elif stop_reason == 17.0:\n    return True" in script,
        "v31 scaffold retained": "first-contact normal latch" in script
        and "no lift/25.2 attitude cycle and no second contact search" in script
        and f"25.3 bridge deadband acquire into the {line_entry.normal_load_min_n:.1f}-{line_entry.normal_load_max_n:.1f}N filtered preload window with {line_entry.raw_sanity_min_n:.1f}-{line_entry.raw_sanity_max_n:.1f}N raw sanity" in script,
        "entry and far-search speedup": f"movel(entry_xy_pose, a={ENTRY_MOVEL_ACCEL_M_S2:.3f}, v={ENTRY_MOVEL_SPEED_M_S:.3f}, r=0.0)" in script
        and f"40.000, {FIRST_SEARCH_FAR_SPEED_M_S:.4f}, {FIRST_SEARCH_NEAR_SPEED_M_S:.4f})" in script
        and "Stage 22 entry movel is 0.060 m/s" in txt,
        "Stage25 target window": f"local line_runtime_limit_s = {spec.stage25_runtime_limit_s:.3f}" in script
        and f"local line_success_progress_m = {spec.stage25_success_target_s:.9f}" in script
        and f"Stage 25.0 diagnostic progress target is {spec.stage25_success_target_s:g} s" in txt,
        "raw contact guards": f"codex_abs(normal_force) > {raw_guard:.1f}" in script
        and f"force_norm > {force_guard:.1f}" in script
        and f"torque_norm > {torque_guard:.1f}" in script,
        "not quarantine": "stop_only_quarantine" not in script + txt,
        "no stale package": "step5b_contact_cycloid_baseline_v1" not in script + txt,
        "no stale step5b v2 package": "step5b_contact_cycloid_baseline_v2" not in script + txt,
        "no stale step5b v3 package": "step5b_contact_cycloid_baseline_v3" not in script + txt,
        "no stale v24 identity": "STEP5D_STRICT_RNN_LIVEPREP_V24" not in script + txt
        and "step5d_strict_rnn_liveprep_v24" not in script + txt
        and "liveprep v24" not in script + txt,
        "no stale v9/v10/v11 identity": "STEP5D_STRICT_RNN_LIVEPREP_V9" not in script + txt
        and "step5d_strict_rnn_liveprep_v9" not in script + txt
        and "STEP5D_STRICT_RNN_LIVEPREP_V10" not in script + txt
        and "step5d_strict_rnn_liveprep_v10" not in script + txt,
        "no stale v11/v12 identity": "STEP5D_STRICT_RNN_LIVEPREP_V11" not in script + txt
        and "step5d_strict_rnn_liveprep_v11" not in script + txt,
        "no stale v12/v13 identity": "STEP5D_STRICT_RNN_LIVEPREP_V12" not in script + txt
        and "step5d_strict_rnn_liveprep_v12" not in script + txt
        and "STEP5D_STRICT_RNN_LIVEPREP_V13" not in script + txt
        and "step5d_strict_rnn_liveprep_v13" not in script + txt,
        "no stale v14 identity": "STEP5D_STRICT_RNN_LIVEPREP_V14" not in script + txt
        and "step5d_strict_rnn_liveprep_v14" not in script + txt
        and "liveprep v14" not in script + txt,
        "no stale v15 identity": "STEP5D_STRICT_RNN_LIVEPREP_V15" not in script + txt
        and "step5d_strict_rnn_liveprep_v15" not in script + txt
        and "liveprep v15" not in script + txt,
        "no stale v15a identity": "STEP5D_STRICT_RNN_LIVEPREP_V15A" not in script + txt
        and "step5d_strict_rnn_liveprep_v15a" not in script + txt
        and "liveprep v15a" not in script + txt,
        "no stale v16 identity": "STEP5D_STRICT_RNN_LIVEPREP_V16" not in script + txt
        and "step5d_strict_rnn_liveprep_v16" not in script + txt
        and "liveprep v16" not in script + txt,
        "no stale v17 identity": "STEP5D_STRICT_RNN_LIVEPREP_V17" not in script + txt
        and "step5d_strict_rnn_liveprep_v17" not in script + txt
        and "liveprep v17" not in script + txt,
        "no stale v18 identity": "STEP5D_STRICT_RNN_LIVEPREP_V18" not in script + txt
        and "step5d_strict_rnn_liveprep_v18" not in script + txt
        and "liveprep v18" not in script + txt,
        "no stale v19 identity": "STEP5D_STRICT_RNN_LIVEPREP_V19" not in script + txt
        and "step5d_strict_rnn_liveprep_v19" not in script + txt
        and "liveprep v19" not in script + txt,
        "no stale v20/v21/v22 identity": "STEP5D_STRICT_RNN_LIVEPREP_V20" not in script + txt
        and "step5d_strict_rnn_liveprep_v20" not in script + txt
        and "liveprep v20" not in script + txt
        and "STEP5D_STRICT_RNN_LIVEPREP_V21" not in script + txt
        and "step5d_strict_rnn_liveprep_v21" not in script + txt
        and "liveprep v21" not in script + txt
        and "STEP5D_STRICT_RNN_LIVEPREP_V22" not in script + txt
        and "step5d_strict_rnn_liveprep_v22" not in script + txt
        and "liveprep v22" not in script + txt,
        "no stale v23 identity": "STEP5D_STRICT_RNN_LIVEPREP_V23" not in script + txt
        and "step5d_strict_rnn_liveprep_v23" not in script + txt
        and "liveprep v23" not in script + txt,
        "no stale ablation identity": (
            spec.program_name == STEP5D_ABLATION_V25_STAGE_ID
            or (
                STEP5D_ABLATION_V25_STAGE_ID not in script + txt + xml
                and "STEP5D_STRICT_RNN_ABLATION_V25" not in script + txt + xml
                and "ablation v25" not in script + txt
            )
        ),
        "no stale v26 ablation identity": (
            spec.program_name == STEP5D_ABLATION_V26_STAGE_ID
            or (
                STEP5D_ABLATION_V26_STAGE_ID not in script + txt + xml
                and "STEP5D_STRICT_RNN_ABLATION_V26" not in script + txt + xml
                and "ablation v26" not in script + txt
            )
        ),
        "no stale v27 ablation identity": (
            spec.program_name == STEP5D_ABLATION_V27_STAGE_ID
            or (
                STEP5D_ABLATION_V27_STAGE_ID not in script + txt + xml
                and "STEP5D_STRICT_RNN_ABLATION_V27" not in script + txt + xml
            )
        ),
        "low-load recovery does not stop": "or normal_load < line_entry_recovery_normal_load_min_n" not in script,
    }
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"{spec.program_name} validation failed: {failed}")


def write_text_if_changed(path: Path, text: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def write_bytes_if_changed(path: Path, data: bytes) -> bool:
    if path.is_file() and path.read_bytes() == data:
        return False
    path.write_bytes(data)
    return True


def semantic_fingerprint_payload(spec: Step5dAblationSpec = DEFAULT_SPEC) -> dict[str, object]:
    line_entry = line_entry_config(spec)
    raw_guard = raw_normal_guard_n(spec)
    force_guard = force_norm_guard_n(spec)
    torque_guard = torque_norm_guard_nm(spec)
    return {
        "schema": f"step5d_ablation_semantic_fingerprint_{spec.version_label}",
        "interface_class": STEP5D_INTERFACE_CLASS,
        "tuning_bundle": STEP5D_TUNING_BUNDLE,
        "program_family": "step5d_strict_rnn_ablation",
        "program": spec.program_name,
        "source_stage_id": spec.source_stage_id,
        "pose_contract_id": POSE_CONTRACT_ID,
        "target_force_n": TARGET_FORCE_N,
        "qdot_cap_rad_s": QDOT_CAP_RAD_S,
        "cartesian_linear_cap_m_s": CARTESIAN_LINEAR_CAP_M_S,
        "cartesian_angular_cap_rad_s": spec.cartesian_angular_cap_rad_s,
        "default_stage25_control_mode": spec.default_stage25_control_mode,
        "register_clear_zero_tol": QDOT_CLEAR_ZERO_TOL_RAD_S,
        "joint_accel_rad_s2": JOINT_ACCEL_RAD_S2,
        "cartesian_accel_m_s2": LINE_ACCEL_M_S2,
        "raw_normal_guard_n": raw_guard,
        "force_norm_guard_n": force_guard,
        "torque_norm_guard_nm": torque_guard,
        "stage25_success_target_s": spec.stage25_success_target_s,
        "stage25_runtime_limit_s": spec.stage25_runtime_limit_s,
        "scaffold_delta": (
            "v27_step5b_speedl_live_shadow_boundary_60s_full_run"
            if spec.version_label == "v28"
            else "step5b_v3_scaffold_min_delta_stage25_cartesian_speedl_oracle"
            if spec.version_label == "v27"
            else "step5b_v3_ablation_scaffold"
        ),
        "stage25_consumption_instrumentation": spec.version_label in {"v27", "v28"},
        "stage25_control_modes": [
            "speedl_cartesian_oracle",
            "speedj_dls_oracle",
            "speedj_rnn_live",
        ],
        "line_entry": {
            "filtered_min_n": line_entry.normal_load_min_n,
            "filtered_max_n": line_entry.normal_load_max_n,
            "raw_min_n": line_entry.raw_sanity_min_n,
            "raw_max_n": line_entry.raw_sanity_max_n,
            "force_norm_max_n": line_entry.force_norm_max_n,
            "hold_s": line_entry.required_s,
            "timeout_s": line_entry.timeout_s,
            "bridge_start_wait_timeout_s": bridge_start_wait_timeout_s(spec),
            "cmd_limit_m_s": line_entry.cmd_limit_m_s,
            "recovery_normal_load_min_n": line_entry.recovery_normal_load_min_n,
            "recovery_normal_load_max_n": line_entry.recovery_normal_load_max_n,
            "force_norm_stop_n": line_entry.force_norm_stop_n,
            "param_valid_code": STEP5D_LINE_ENTRY_PARAM_VALID_CODE,
        },
        "search": {
            "second_max_down_m": SECOND_SEARCH_MAX_DOWN_M,
            "second_near_start_depth_m": SECOND_SEARCH_NEAR_START_DEPTH_M,
            "second_runtime_limit_s": SECOND_SEARCH_RUNTIME_LIMIT_S,
            "second_far_speed_m_s": SECOND_SEARCH_FAR_SPEED_M_S,
            "second_near_speed_m_s": SECOND_SEARCH_NEAR_SPEED_M_S,
            "entry_movel_accel_m_s2": ENTRY_MOVEL_ACCEL_M_S2,
            "entry_movel_speed_m_s": ENTRY_MOVEL_SPEED_M_S,
            "first_far_speed_m_s": FIRST_SEARCH_FAR_SPEED_M_S,
            "first_near_speed_m_s": FIRST_SEARCH_NEAR_SPEED_M_S,
        },
        "register_contract": {
            "stage25_3": "37..39 Cartesian acquire, 40/41/42/44/46/47 preload parameter channel",
            "stage25_95": (
                f"37..47 bridge-cleared register barrier, 37..42 near-zero <= {QDOT_CLEAR_ZERO_TOL_RAD_S:.6f}, "
                "43 cmd_valid=0, 47 != preload/cartesian/joint layout code"
            ),
            "stage25_0": {
                "cartesian_layout_code": STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE,
                "cartesian_registers": "37..42 vx/vy/vz/wx/wy/wz, TP speedl",
                "joint_layout_code": STEP5D_STAGE25_JOINT_LAYOUT_CODE,
                "joint_registers": "37..42 qd0..qd5, TP speedj",
                "shared": "43 cmd_valid, 44 path_time, 45 force_error, 46 orientation_error, 47 layout_tag",
            },
        },
    }


def semantic_fingerprint(script: str, txt: str, urp: bytes, spec: Step5dAblationSpec = DEFAULT_SPEC) -> str:
    # Validate caller supplied a real rendered package, but hash only behavior.
    # Byte identity is tracked separately by the delivery/read-back manifests.
    gzip.decompress(urp)
    payload = semantic_fingerprint_payload(spec)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def default_local_candidate_dir(now: datetime, spec: Step5dAblationSpec = DEFAULT_SPEC) -> Path:
    stamp = now.strftime("%Y%m%d_%H%M%S")
    return LOCAL_CANDIDATE_ROOT / f"{spec.program_name}_{stamp}"


def write_local_candidate_marker(
    output_dir: Path,
    *,
    script_path: Path,
    txt_path: Path,
    urp_path: Path,
    stamp: str,
    gen_at: str,
    fingerprint: str,
    spec: Step5dAblationSpec = DEFAULT_SPEC,
) -> Path:
    marker = {
        "schema": "ur_tp_local_candidate_v1",
        "status": "local package verified",
        "local_only": True,
        "not_delivered": True,
        "program": spec.program_name,
        "target_dir": CONTROLLER_DIR,
        "controller_urp": f"{CONTROLLER_DIR}/{spec.program_name}.urp",
        "stamp": stamp,
        "generated_at": gen_at,
        "semantic_fingerprint": fingerprint,
        "sha256": {
            ".script": file_sha256(script_path),
            ".txt": file_sha256(txt_path),
            ".urp": file_sha256(urp_path),
        },
        "safety_boundary": [
            "local candidate only",
            "no controller upload",
            "no controller read-back",
            "not current_stage",
            "do not open on Teach Pendant",
            "no live bridge",
        ],
    }
    marker_path = output_dir / LOCAL_CANDIDATE_MARKER
    marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return marker_path


def write_outputs(
    stamp_prefix: str | None = None,
    generated_at_override: str | None = None,
    *,
    reuse_existing_metadata: bool = False,
    output_dir: Path | None = None,
    local_only: bool = False,
    program: str | None = None,
) -> dict[str, object]:
    spec = spec_for(program)
    now = datetime.now(timezone(timedelta(hours=8)))
    target_dir = output_dir or (default_local_candidate_dir(now, spec) if local_only else LOCAL_PROGRAM_DIR)
    reused = (
        existing_metadata(target_dir, spec)
        if reuse_existing_metadata and stamp_prefix is None and generated_at_override is None
        else None
    )
    if reused is not None:
        stamp, gen_at = reused
    else:
        stamp = stamp_prefix or source_stamp(now, spec)
        gen_at = generated_at_override or generated_at(now)
    frame = load_safe_frame(spec)
    geom = line_cfg(load_json(CONFIG_PATH))
    script = build_script(stamp, gen_at, geom, frame, spec)
    txt = build_txt(stamp, spec)
    urp = build_urp(script, spec.program_name, CONTROLLER_DIR)
    validate_package(script, txt, urp, stamp, spec)
    fingerprint = semantic_fingerprint(script, txt, urp, spec)

    target_dir.mkdir(parents=True, exist_ok=True)
    script_path = target_dir / f"{spec.program_name}.script"
    txt_path = target_dir / f"{spec.program_name}.txt"
    urp_path = target_dir / f"{spec.program_name}.urp"
    changed = {
        "script": write_text_if_changed(script_path, script),
        "txt": write_text_if_changed(txt_path, txt),
        "urp": write_bytes_if_changed(urp_path, urp),
    }
    marker_path = None
    if local_only:
        marker_path = write_local_candidate_marker(
            target_dir,
            script_path=script_path,
            txt_path=txt_path,
            urp_path=urp_path,
            stamp=stamp,
            gen_at=gen_at,
            fingerprint=fingerprint,
            spec=spec,
        )
    return {
        "script": str(script_path),
        "txt": str(txt_path),
        "urp": str(urp_path),
        "controller_urp": f"{CONTROLLER_DIR}/{spec.program_name}.urp",
        "stamp": stamp,
        "generated_at": gen_at,
        "semantic_fingerprint": fingerprint,
        "local_only": local_only,
        "local_candidate_marker": str(marker_path) if marker_path is not None else None,
        "reused_existing_metadata": reused is not None,
        "changed": changed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--program", choices=sorted(ABLATION_SPECS), default=DEFAULT_SPEC.program_name)
    parser.add_argument("--stamp-prefix", default=None)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--reuse-existing-metadata", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="write a local-only candidate marker; no controller delivery is implied",
    )
    args = parser.parse_args()
    local_only = args.local_only or args.output_dir is not None
    result = write_outputs(
        args.stamp_prefix,
        args.generated_at,
        reuse_existing_metadata=args.reuse_existing_metadata,
        output_dir=args.output_dir,
        local_only=local_only,
        program=args.program,
    )
    print(json.dumps({"generated": {args.program: result}}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
