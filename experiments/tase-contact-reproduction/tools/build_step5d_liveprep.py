#!/usr/bin/env python3
"""Generate Step5d strict RNN live-prep TP package."""

from __future__ import annotations

import argparse
import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from build_step4e_line_programs import CONFIG_PATH, PROGRAM_DIR, generated_at, line_cfg, load_json
from build_step4e_p0p1_programs import build_urp
from build_step5b_contact import build_script as build_step5b_script
from step5_table import load_stage_frame, step5_stage


PROGRAM_NAME = "step5d_strict_rnn_liveprep_v19"
STEP5_STAGE_ID = "step5d_strict_rnn_liveprep_v19"
BRIDGE_VERSION = STEP5_STAGE_ID
LOCAL_PROGRAM_DIR = PROGRAM_DIR / "step5"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
TARGET_FORCE_N = 12.0
BRIDGE_NORMAL_FILTER_ALPHA = 0.55
QDOT_CAP_RAD_S = 0.050
JOINT_ACCEL_RAD_S2 = 0.050
ORIENTATION_SKIP_ERROR_RAD = 0.069813
RAW_NORMAL_GUARD_N = 100.0
FORCE_NORM_GUARD_N = 100.0
TORQUE_NORM_GUARD_NM = 4.0
LINE_ENTRY_NORMAL_LOAD_MIN_N = 8.0
LINE_ENTRY_NORMAL_LOAD_MAX_N = 13.0
LINE_ENTRY_FORCE_NORM_MAX_N = 25.0
LINE_ENTRY_REQUIRED_S = 0.100
LINE_ENTRY_CMD_LIMIT_M_S = 0.003
LINE_ENTRY_TIMEOUT_S = 10.000
LINE_ENTRY_RAW_SANITY_MIN_N = 7.5
LINE_ENTRY_RAW_SANITY_MAX_N = 14.0
LINE_ENTRY_RECOVERY_NORMAL_LOAD_MIN_N = 0.0
LINE_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N = 40.0
LINE_ENTRY_FORCE_NORM_STOP_N = 100.0
SECOND_SEARCH_MAX_DOWN_M = 0.035
SECOND_SEARCH_NEAR_START_DEPTH_M = 0.000
SECOND_SEARCH_RUNTIME_LIMIT_S = 45.000
SECOND_SEARCH_FAR_SPEED_M_S = -0.0025
SECOND_SEARCH_NEAR_SPEED_M_S = -0.0025
ENTRY_MOVEL_ACCEL_M_S2 = 0.090
ENTRY_MOVEL_SPEED_M_S = 0.060
FIRST_SEARCH_FAR_SPEED_M_S = -0.0225
FIRST_SEARCH_NEAR_SPEED_M_S = -0.0025


def source_stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H%MHKT_STEP5D_STRICT_RNN_LIVEPREP_V19")


def existing_metadata() -> tuple[str, str] | None:
    script_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.script"
    if not script_path.is_file():
        return None
    text = script_path.read_text(encoding="utf-8")
    stamp_match = re.search(r"^# VERSION:\s*(\S+)\s*$", text, flags=re.M)
    generated_match = re.search(r"^# GENERATED_AT_LOCAL:\s*(\S+)\s*$", text, flags=re.M)
    if not stamp_match or not generated_match:
        return None
    return stamp_match.group(1), generated_match.group(1)


def load_safe_frame() -> dict:
    return load_stage_frame(step5_stage(STEP5_STAGE_ID))


def _replace_exact(script: str, old: str, new: str) -> str:
    if old not in script:
        raise RuntimeError(f"{PROGRAM_NAME} scaffold replacement failed: {old}")
    return script.replace(old, new, 1)


def _replace_first_present(script: str, old_candidates: tuple[str, ...], new: str, label: str) -> str:
    for old in old_candidates:
        if old in script:
            return script.replace(old, new, 1)
    raise RuntimeError(f"{PROGRAM_NAME} scaffold replacement failed: {label}")


def _replace_line_stage_with_speedj(script: str) -> str:
    start = script.index("  if stop_reason == 0.0:\n    write_output_float_register(35, 25.0)")
    end = script.index("\n\n  if codex_should_auto_home(stop_reason):", start)
    block = f"""  if stop_reason == 0.0:
    write_output_float_register(35, 25.0)
    local last_heartbeat2 = read_input_float_register(26)
    local stale_s2 = 0.0
    local t2 = 0.0
    local qdot_cap_rad_s = {QDOT_CAP_RAD_S:.3f}
    local joint_accel_rad_s2 = {JOINT_ACCEL_RAD_S2:.3f}
    while stop_reason == 0.0:
      local heartbeat2 = read_input_float_register(26)
      local cmd_valid = read_input_float_register(43)
      local progress_s = read_input_float_register(44)
      local cmd_qd0 = read_input_float_register(37)
      local cmd_qd1 = read_input_float_register(38)
      local cmd_qd2 = read_input_float_register(39)
      local cmd_qd3 = read_input_float_register(40)
      local cmd_qd4 = read_input_float_register(41)
      local cmd_qd5 = read_input_float_register(42)
      local loop_dt = get_steptime()
      final_progress_m = progress_s
      if cmd_valid >= 0.5:
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
        if cmd_valid < 0.5:
          if saw_cmd_valid == 0 and t2 < cmd_valid_grace_s:
            speedj([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], joint_accel_rad_s2, line_hold_s)
          elif saw_cmd_valid == 1 and cmd_invalid_s <= cmd_valid_loss_limit_s:
            speedj([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], joint_accel_rad_s2, line_hold_s)
          else:
            stop_reason = 12.0
          end
        elif codex_abs(cmd_qd0) > qdot_cap_rad_s or codex_abs(cmd_qd1) > qdot_cap_rad_s or codex_abs(cmd_qd2) > qdot_cap_rad_s or codex_abs(cmd_qd3) > qdot_cap_rad_s or codex_abs(cmd_qd4) > qdot_cap_rad_s or codex_abs(cmd_qd5) > qdot_cap_rad_s:
          stop_reason = 13.0
        elif end_hold_s >= end_hold_required_s:
          stop_reason = 1.0
        elif t2 >= line_runtime_limit_s:
          stop_reason = 10.0
        else:
          speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5], joint_accel_rad_s2, line_hold_s)
        end
      end
    end
    stopj(0.3)
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


def _replace_line_entry_with_force_settle(script: str) -> str:
    start = script.index("  if stop_reason == 0.0:\n    write_output_float_register(35, 25.3)")
    end = script.index("\n\n  if stop_reason == 0.0:\n    write_output_float_register(35, 25.0)", start)
    block = f"""  if stop_reason == 0.0:
    write_output_float_register(35, 25.3)
    local last_heartbeat_entry = read_input_float_register(26)
    local stale_s_entry = 0.0
    local t_entry = 0.0
    local line_entry_s = 0.0
    local line_entry_required_s = {LINE_ENTRY_REQUIRED_S:.3f}
    local line_entry_timeout_s = {LINE_ENTRY_TIMEOUT_S:.3f}
    local line_entry_normal_load_min_n = {LINE_ENTRY_NORMAL_LOAD_MIN_N:.3f}
    local line_entry_normal_load_max_n = {LINE_ENTRY_NORMAL_LOAD_MAX_N:.3f}
    local line_entry_force_norm_max_n = {LINE_ENTRY_FORCE_NORM_MAX_N:.3f}
    local line_entry_recovery_normal_load_min_n = {LINE_ENTRY_RECOVERY_NORMAL_LOAD_MIN_N:.3f}
    local line_entry_recovery_normal_load_max_n = {LINE_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N:.3f}
    local line_entry_force_norm_stop_n = {LINE_ENTRY_FORCE_NORM_STOP_N:.3f}
    local line_entry_cmd_limit_m_s = {LINE_ENTRY_CMD_LIMIT_M_S:.3f}
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


def build_script(stamp: str, gen_at: str, geom: dict[str, float], frame: dict) -> str:
    script = build_step5b_script(stamp, gen_at, geom, frame, variant="v3")
    script = script.replace("step5b_contact_cycloid_baseline_v1", PROGRAM_NAME)
    script = script.replace("step5b_contact_cycloid_baseline_v2", PROGRAM_NAME)
    script = script.replace("step5b_contact_cycloid_baseline_v3", PROGRAM_NAME)
    script = script.replace("Step5b contact cycloid baseline v1", "Step5d strict RNN liveprep v19")
    script = script.replace("Step5b contact cycloid baseline v2", "Step5d strict RNN liveprep v19")
    script = script.replace("Step5b contact cycloid baseline v3", "Step5d strict RNN liveprep v19")
    script = script.replace("STEP5B_CONTACT_CYCLOID_BASELINE_V1", "STEP5D_STRICT_RNN_LIVEPREP_V19")
    script = script.replace("STEP5B_CONTACT_CYCLOID_BASELINE_V2", "STEP5D_STRICT_RNN_LIVEPREP_V19")
    script = script.replace("STEP5B_CONTACT_CYCLOID_BASELINE_V3", "STEP5D_STRICT_RNN_LIVEPREP_V19")
    script = script.replace("codex_step5b_down_search", "codex_step5d_down_search")
    script = script.replace("step4e-version=step5b_v1", f"step4e-version={BRIDGE_VERSION}")
    script = script.replace("step4e-version=step5b_v2", f"step4e-version={BRIDGE_VERSION}")
    script = script.replace("step4e-version=step5b_v3", f"step4e-version={BRIDGE_VERSION}")
    script = script.replace("step4e-normal-filter-alpha=0.55", f"step4e-normal-filter-alpha={BRIDGE_NORMAL_FILTER_ALPHA:.2f}")
    script = script.replace("codex_abs(normal_force) > 50.0", f"codex_abs(normal_force) > {RAW_NORMAL_GUARD_N:.1f}")
    script = script.replace("force_norm > 60.0", f"force_norm > {FORCE_NORM_GUARD_N:.1f}")
    script = script.replace("torque_norm > 3.0", f"torque_norm > {TORQUE_NORM_GUARD_NM:.1f}")
    script = script.replace("local line_runtime_limit_s = 65.000", "local line_runtime_limit_s = 15.000")
    script = script.replace("local line_success_progress_m = 60.000000000", "local line_success_progress_m = 10.000000000")
    script = _replace_first_present(
        script,
        (
            "PURPOSE: v31 contact search, first-contact normal latch, lift, 25.2 attitude correction, 25.3 line-entry gate, then Step5 table-driven contact cycloid reference for 60 s.",
            "PURPOSE: v31 contact search, first-contact normal latch, optional 4deg skip-lift/25.2 gate, otherwise lift and 25.2 attitude correction, 25.3 line-entry gate, then Step5 table-driven contact cycloid reference for 60 s.",
            "PURPOSE: v31 contact search, first-contact normal latch, no lift/25.2 attitude cycle and no second contact search, 25.3 line-entry gate, then Step5 table-driven contact cycloid reference for 60 s.",
        ),
        "PURPOSE: v31 contact search, first-contact normal latch, no lift/25.2 attitude cycle and no second contact search, 25.3 bridge deadband acquire into the 8-13N filtered preload window with 7.5-14N raw sanity, then v19 cage-primary Step5d strict RNN qdot diagnostic for 10 s.",
        "purpose",
    )
    script = _speed_up_entry_and_first_search(script)
    script = script.replace(
        "25.0 uses desired_velocity + path_p_gain*(desired-actual) before normal projection and force-loop composition.",
        "25.0 uses strict RNN qdot registers 37..42 and TP speedj execution; bridge owns calibrated Pinocchio/J(q) and paper outer-loop computation.",
    )
    script = script.replace(
        "TP_ROLE: executor_and_guard_only; Step5 trajectory reference is computed by the bridge.",
        (
            "TP_ROLE: joint_executor_and_guard_only; Step5d strict RNN qdot is computed by the bridge.\n"
            "# REGISTER_CONTRACT: Stage 25.3 consumes 37..39 as Cartesian deadband-acquire vx/vy/vz; Stage 25.0 consumes 37..42 as qd0..qd5 rad/s, 43 cmd_valid, 44 path_time_s.\n"
            "# STAGE25_CONTACT_SAFETY: v19 bridge computes online broad AABB TCP cage distance/braking margin, routes low-load/no-contact to active_reacquire_solver with frozen path_time_s and a reacquire predicted-speed cap instead of hold-duty stop, logs active reacquire/no-contact diagnostics, and preserves stop_request for cage margin exhaustion, semantic failure, hard force/torque/joint/sensor gates, heartbeat/cmd_valid, Dashboard mismatch, or timeout.\n"
            "# FORCE_FRAME_CONTRACT: UR_FORCE_FRAME_CONTRACT.md; reaction normal for load, approach normal for posture."
        ),
    )
    script = _replace_exact(script, "STEP5_STAGE_ID: step5_contact_cycloid_baseline_v1", f"STEP5_STAGE_ID: {STEP5_STAGE_ID}")
    script = _add_down_search_force_trigger_echo(script)
    script = _add_force_envelope_auto_home(script)
    script = _replace_line_entry_with_force_settle(script)
    script = _replace_line_stage_with_speedj(script)
    if "speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy" in script:
        raise RuntimeError("line-control speedl command survived Step5d liveprep rewrite")
    if f"def codex_{PROGRAM_NAME}()" not in script:
        raise RuntimeError("Step5d liveprep function rename failed")
    return script


def build_txt(stamp: str) -> str:
    return f"""Step5d strict RNN v19 cage-primary 12N diagnostic TP package

Open on Teach Pendant after controller read-back is verified:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Boundary:
  Contact-capable v19 cage-primary diagnostic package; not a completed reproduction claim.
  Reuses the Step5b v3 contact-search/latch/25.3 scaffold.
  After first-contact latch, it does not run the 20 mm lift, 25.2 attitude
  correction, or 24.3/24.4 second contact search.
  no lift and no second contact search remain deliberate requirements.
  Stage 25.3 is deadband contact acquire: bridge filters normal_load and
  commands only locked-normal Cartesian vx/vy/vz in registers 37..39. It may
  actively recover while raw normal_load is between {LINE_ENTRY_RECOVERY_NORMAL_LOAD_MIN_N:.1f} N and {LINE_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N:.1f} N,
  with force_norm <= {LINE_ENTRY_FORCE_NORM_STOP_N:.1f} N.
  TP enters 25.0 only after the bridge-side preload register reports filtered
  normal_load between {LINE_ENTRY_NORMAL_LOAD_MIN_N:.1f} N and {LINE_ENTRY_NORMAL_LOAD_MAX_N:.1f} N,
  raw normal_load is sanity-checked between {LINE_ENTRY_RAW_SANITY_MIN_N:.1f} N and {LINE_ENTRY_RAW_SANITY_MAX_N:.1f} N,
  with force_norm <= {LINE_ENTRY_FORCE_NORM_MAX_N:.1f} N,
  and bridge cmd_valid is true for {LINE_ENTRY_REQUIRED_S:.3f} s.
  Stage 25.0 is different from Step5b: it consumes 37..42 as qd0..qd5 rad/s
  and executes speedj, not Cartesian speedl. v19 bridge must compute online
  broad AABB TCP cage distance/braking margin from Step5b/Step6b success traces,
  use cage-primary active reacquire, and log _step5d_tcp_cage_*,
  _step5d_active_reacquire_s, and _step5d_no_contact_s fields. Low-load or
  no-contact inside the cage freezes path_time_s, holds the force integrator,
  applies a reacquire predicted-speed cap, and stays in the solver path;
  hold duty is diagnostic only for legacy hold actions. stop_request remains
  hard for cage margin exhaustion, semantic failure, hard force/torque/joint/
  sensor gates, heartbeat/cmd_valid failure, Dashboard mismatch, or timeout.
  Stage 25.0 diagnostic progress target is 10 s.
  Stage 22 entry movel is 0.060 m/s at 0.090 m/s^2; Stage 24 far search is
  {abs(FIRST_SEARCH_FAR_SPEED_M_S):.4f} m/s down, near search remains {abs(FIRST_SEARCH_NEAR_SPEED_M_S):.4f} m/s.

Bridge profile:
  --step4e-version {BRIDGE_VERSION} --step4e-path-shape cycloid
  --target-force-n {TARGET_FORCE_N:.1f}
  --step4e-normal-follow-mode filtered_live
  --step4e-normal-filter-alpha {BRIDGE_NORMAL_FILTER_ALPHA:.2f}
  --step4e-normal-min-force-n 2.0

Safety:
  qdot cap: {QDOT_CAP_RAD_S:.3f} rad/s
  speedj acceleration: {JOINT_ACCEL_RAD_S2:.3f} rad/s^2
  Raw normal guard: {RAW_NORMAL_GUARD_N:.0f} N. Force norm guard: {FORCE_NORM_GUARD_N:.0f} N. Torque guard: {TORQUE_NORM_GUARD_NM:.1f} Nm.
  100 N raw-normal/force-norm and 4.0 Nm torque are sensor hard guards only;
  human safety still depends on the external cage/operator/E-stop boundary.
  No UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.
  This package is not a bridge-start or TP-Play authorization.

Reference:
  STEP5_FLOW.md
  config/step5_stage_table.json stage {STEP5_STAGE_ID}
  UR_FORCE_FRAME_CONTRACT.md
  config/step5d_liveprep_solver_gate.json
"""


def validate_package(script: str, txt: str, urp: bytes, stamp: str) -> None:
    xml = gzip.decompress(urp).decode("utf-8")
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": f'URProgram name="{PROGRAM_NAME}"' in xml,
        "controller directory": f'directory="{CONTROLLER_DIR}"' in xml,
        "script file": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script" in xml,
        "cached stamp": stamp in xml,
        "function name": f"def codex_{PROGRAM_NAME}()" in script,
        "step5 flow": "STEP5_FLOW.md" in script and "STEP5_FLOW.md" in txt,
        "step5d stage": STEP5_STAGE_ID in script and STEP5_STAGE_ID in txt,
        "bridge profile": f"step4e-version={BRIDGE_VERSION}" in script
        and f"--step4e-version {BRIDGE_VERSION}" in txt,
        "joint executor role": "joint_executor_and_guard_only" in script,
        "qdot register reads": all(f"local cmd_qd{idx} = read_input_float_register({37 + idx})" in script for idx in range(6)),
        "speedj line control": "speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]" in script,
        "line no cartesian speedl": "speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy" not in script,
        "qdot cap": f"local qdot_cap_rad_s = {QDOT_CAP_RAD_S:.3f}" in script,
        "v19 Stage25 cage-primary note": "STAGE25_CONTACT_SAFETY" in script
        and "active_reacquire_solver" in script
        and "low-load/no-contact" in script
        and "reacquire predicted-speed cap" in script
        and "online broad AABB TCP cage" in script
        and "cage margin exhaustion" in script
        and "active reacquire/no-contact diagnostics" in script
        and "_step5d_active_reacquire_s" in txt
        and "_step5d_no_contact_s" in txt
        and "stop_request" in script,
        "no lift attitude cycle": "local skip_lift_attitude = 0" not in script
        and "write_output_float_register(35, 25.1)" not in script
        and "write_output_float_register(35, 25.2)" not in script,
        "force-settle entry gate": f"local line_entry_normal_load_min_n = {LINE_ENTRY_NORMAL_LOAD_MIN_N:.3f}" in script
        and f"local line_entry_normal_load_max_n = {LINE_ENTRY_NORMAL_LOAD_MAX_N:.3f}" in script
        and f"local line_entry_force_norm_max_n = {LINE_ENTRY_FORCE_NORM_MAX_N:.3f}" in script
        and f"local line_entry_required_s = {LINE_ENTRY_REQUIRED_S:.3f}" in script
        and f"local line_entry_timeout_s = {LINE_ENTRY_TIMEOUT_S:.3f}" in script
        and f"local line_entry_recovery_normal_load_min_n = {LINE_ENTRY_RECOVERY_NORMAL_LOAD_MIN_N:.3f}" in script
        and f"local line_entry_recovery_normal_load_max_n = {LINE_ENTRY_RECOVERY_NORMAL_LOAD_MAX_N:.3f}" in script
        and f"local line_entry_force_norm_stop_n = {LINE_ENTRY_FORCE_NORM_STOP_N:.3f}" in script
        and "local normal_load = target_force - force_error" in script
        and "stop_reason = 17.0" in script
        and "speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0]" in script,
        "no second contact search": "codex_step5d_down_search(24.3, 24.4" not in script,
        "force envelope auto-home": "elif stop_reason == 17.0:\n    return True" in script,
        "v31 scaffold retained": "first-contact normal latch" in script
        and "no lift/25.2 attitude cycle and no second contact search" in script
        and "25.3 bridge deadband acquire into the 8-13N filtered preload window with 7.5-14N raw sanity" in script,
        "entry and far-search speedup": f"movel(entry_xy_pose, a={ENTRY_MOVEL_ACCEL_M_S2:.3f}, v={ENTRY_MOVEL_SPEED_M_S:.3f}, r=0.0)" in script
        and f"40.000, {FIRST_SEARCH_FAR_SPEED_M_S:.4f}, {FIRST_SEARCH_NEAR_SPEED_M_S:.4f})" in script
        and "Stage 22 entry movel is 0.060 m/s" in txt,
        "10s diagnostic window": "local line_runtime_limit_s = 15.000" in script
        and "local line_success_progress_m = 10.000000000" in script
        and "Stage 25.0 diagnostic progress target is 10 s" in txt,
        "raw contact guards": f"codex_abs(normal_force) > {RAW_NORMAL_GUARD_N:.1f}" in script
        and f"force_norm > {FORCE_NORM_GUARD_N:.1f}" in script
        and f"torque_norm > {TORQUE_NORM_GUARD_NM:.1f}" in script,
        "not quarantine": "stop_only_quarantine" not in script + txt,
        "no stale package": "step5b_contact_cycloid_baseline_v1" not in script + txt,
        "no stale step5b v2 package": "step5b_contact_cycloid_baseline_v2" not in script + txt,
        "no stale step5b v3 package": "step5b_contact_cycloid_baseline_v3" not in script + txt,
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
        "low-load recovery does not stop": "or normal_load < line_entry_recovery_normal_load_min_n" not in script,
    }
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"{PROGRAM_NAME} validation failed: {failed}")


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


def write_outputs(
    stamp_prefix: str | None = None,
    generated_at_override: str | None = None,
    *,
    reuse_existing_metadata: bool = False,
) -> dict[str, object]:
    now = datetime.now(timezone(timedelta(hours=8)))
    reused = existing_metadata() if reuse_existing_metadata and stamp_prefix is None and generated_at_override is None else None
    if reused is not None:
        stamp, gen_at = reused
    else:
        stamp = stamp_prefix or source_stamp(now)
        gen_at = generated_at_override or generated_at(now)
    frame = load_safe_frame()
    geom = line_cfg(load_json(CONFIG_PATH))
    script = build_script(stamp, gen_at, geom, frame)
    txt = build_txt(stamp)
    urp = build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    validate_package(script, txt, urp, stamp)

    LOCAL_PROGRAM_DIR.mkdir(parents=True, exist_ok=True)
    script_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.script"
    txt_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.txt"
    urp_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.urp"
    changed = {
        "script": write_text_if_changed(script_path, script),
        "txt": write_text_if_changed(txt_path, txt),
        "urp": write_bytes_if_changed(urp_path, urp),
    }
    return {
        "script": str(script_path),
        "txt": str(txt_path),
        "urp": str(urp_path),
        "controller_urp": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "stamp": stamp,
        "generated_at": gen_at,
        "reused_existing_metadata": reused is not None,
        "changed": changed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp-prefix", default=None)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--reuse-existing-metadata", action="store_true")
    args = parser.parse_args()
    result = write_outputs(
        args.stamp_prefix,
        args.generated_at,
        reuse_existing_metadata=args.reuse_existing_metadata,
    )
    print(json.dumps({"generated": {PROGRAM_NAME: result}}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
