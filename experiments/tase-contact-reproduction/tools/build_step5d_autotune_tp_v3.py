#!/usr/bin/env python3
"""Build the explicit V3 TP triplet from the frozen V1 control script."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import build_step5d_autotune_tp as v1


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR

PROGRAM_NAME = "step5d_strict_rnn_autotune_v3_r008"
CONTROL_PROFILE_ID = "step5d_strict_rnn_autotune_v1"
PRECONTACT_POSE_PRIOR_ID = STEP5D_V3_PHYSICAL_PRIOR.prior_id
PRECONTACT_POSE_PRIOR_SHA256 = STEP5D_V3_PHYSICAL_PRIOR.fingerprint
PRECONTACT_XYZ_M = STEP5D_V3_PHYSICAL_PRIOR.precontact_xyz_m
PRECONTACT_ROTVEC_RAD = STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad
PRECONTACT_CLEARANCE_M = 0.005
MINIMUM_START_ABOVE_ENTRY_M = 0.01
# Preserve the frozen V1 Stage25 transport watchdog.  The V3 host already
# freezes the last accepted command and heartbeat on a late publication; a
# shorter TP timeout would turn ordinary host scheduling jitter into a false
# transport-loss stop and would no longer be V1 control-kernel parity.
STAGE25_STALE_COMMAND_HOLD_S = 1.000
READY_ARM_TIMEOUT_S = 30.000
CONTROLLER_DIR = v1.CONTROLLER_DIR
LOCAL_PROGRAM_DIR = v1.LOCAL_PROGRAM_DIR


def _replace_once(source: str, old: str, new: str, *, role: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"V1 rendered script {role} marker count differs")
    return source.replace(old, new, 1)


def _batch_lifecycle_replacements() -> tuple[tuple[str, str, str], ...]:
    globals_and_guard = r'''global codex_autotune_batch_row_echo = 0
global codex_autotune_return_kind_echo = 0
global codex_autotune_return_guard_mask = 0
global codex_autotune_return_observer_active = False
global codex_autotune_return_segment_id = 0
global codex_autotune_return_current_angular_speed_rad_s = 0.0
global codex_autotune_return_current_angular_accel_rad_s2 = 0.0
global codex_autotune_return_max_angular_speed_rad_s = 0.0
global codex_autotune_return_max_angular_accel_rad_s2 = 0.0
global codex_autotune_return_max_sample_gap_s = 0.0
global codex_autotune_logical_batch_sequence_echo = 0

def codex_autotune_norm3(x, y, z):
  return sqrt(x * x + y * y + z * z)
end

thread codex_autotune_return_telemetry_observer():
  local have_controller_time_sample = False
  local have_angular_sample = False
  local last_wx = 0.0
  local last_wy = 0.0
  local last_wz = 0.0
  local controller_clock = time()
  local last_controller_time_s = controller_clock.sec + controller_clock.nanosec / 1000000000.0
  while codex_autotune_return_observer_active:
    controller_clock = time()
    local controller_time_s = controller_clock.sec + controller_clock.nanosec / 1000000000.0
    local loop_dt = controller_time_s - last_controller_time_s
    last_controller_time_s = controller_time_s
    local tcp_speed = get_actual_tcp_speed()
    local angular_speed_rad_s = codex_autotune_norm3(tcp_speed[3], tcp_speed[4], tcp_speed[5])
    local angular_accel_rad_s2 = 0.0
    if have_controller_time_sample and have_angular_sample and loop_dt > 0.0:
      local angular_filter_alpha = loop_dt / (0.020 + loop_dt)
      local filtered_wx = last_wx + angular_filter_alpha * (tcp_speed[3] - last_wx)
      local filtered_wy = last_wy + angular_filter_alpha * (tcp_speed[4] - last_wy)
      local filtered_wz = last_wz + angular_filter_alpha * (tcp_speed[5] - last_wz)
      angular_accel_rad_s2 = codex_autotune_norm3(filtered_wx - last_wx, filtered_wy - last_wy, filtered_wz - last_wz) / loop_dt
      last_wx = filtered_wx
      last_wy = filtered_wy
      last_wz = filtered_wz
    else:
      last_wx = tcp_speed[3]
      last_wy = tcp_speed[4]
      last_wz = tcp_speed[5]
    end
    have_angular_sample = True
    codex_autotune_return_current_angular_speed_rad_s = angular_speed_rad_s
    codex_autotune_return_current_angular_accel_rad_s2 = angular_accel_rad_s2
    if angular_speed_rad_s > codex_autotune_return_max_angular_speed_rad_s:
      codex_autotune_return_max_angular_speed_rad_s = angular_speed_rad_s
    end
    if angular_accel_rad_s2 > codex_autotune_return_max_angular_accel_rad_s2:
      codex_autotune_return_max_angular_accel_rad_s2 = angular_accel_rad_s2
    end
    if have_controller_time_sample and loop_dt > codex_autotune_return_max_sample_gap_s:
      codex_autotune_return_max_sample_gap_s = loop_dt
    end
    have_controller_time_sample = True
    sync()
  end
end

def codex_autotune_latch_return_telemetry():
  write_output_float_register(35, 40.3)
  write_output_float_register(39, 3.0)
  write_output_float_register(40, codex_autotune_return_current_angular_speed_rad_s)
  write_output_float_register(41, codex_autotune_return_current_angular_accel_rad_s2)
  write_output_float_register(42, codex_autotune_return_max_angular_speed_rad_s)
  write_output_float_register(43, codex_autotune_return_max_angular_accel_rad_s2)
  write_output_float_register(44, codex_autotune_return_max_sample_gap_s)
end

def codex_autotune_typed_target_verified(target_pose, campaign_home_q, require_home_q):
  local actual_pose = get_actual_tcp_pose()
  local delta_pose = pose_trans(pose_inv(target_pose), actual_pose)
  local position_error_m = sqrt(delta_pose[0] * delta_pose[0] + delta_pose[1] * delta_pose[1] + delta_pose[2] * delta_pose[2])
  local orientation_error_rad = sqrt(delta_pose[3] * delta_pose[3] + delta_pose[4] * delta_pose[4] + delta_pose[5] * delta_pose[5])
  local tcp_speed = get_actual_tcp_speed()
  local linear_speed_m_s = sqrt(tcp_speed[0] * tcp_speed[0] + tcp_speed[1] * tcp_speed[1] + tcp_speed[2] * tcp_speed[2])
  local angular_speed_rad_s = sqrt(tcp_speed[3] * tcp_speed[3] + tcp_speed[4] * tcp_speed[4] + tcp_speed[5] * tcp_speed[5])
  local actual_qd = get_actual_joint_speeds()
  local qd_max_rad_s = 0.0
  local qd_index = 0
  while qd_index < 6:
    if codex_abs(actual_qd[qd_index]) > qd_max_rad_s:
      qd_max_rad_s = codex_abs(actual_qd[qd_index])
    end
    qd_index = qd_index + 1
  end
  local home_q_ok = True
  if require_home_q:
    home_q_ok = codex_autotune_max_joint_error(campaign_home_q, get_actual_joint_positions()) <= 0.010
  end
  write_output_float_register(36, position_error_m)
  write_output_float_register(37, orientation_error_rad)
  write_output_float_register(38, qd_max_rad_s)
  return position_error_m <= 0.003 and orientation_error_rad <= 0.050 and linear_speed_m_s <= 0.001 and angular_speed_rad_s <= 0.010 and qd_max_rad_s <= 0.010 and home_q_ok
end

def codex_autotune_single_owner_return(batch_row_index, campaign_home_pose, campaign_home_q):
  local current_pose = get_actual_tcp_pose()
  local safe_z = 0.033
  local near_pose = p[0.487834547, 0.129337053, 0.022863519, 3.120752062, 0.000000000, 0.068626833]
  local target_pose = campaign_home_pose
  local require_home_q = True
  local rise_pose = p[current_pose[0], current_pose[1], safe_z, current_pose[3], current_pose[4], current_pose[5]]
  local transfer_pose = p[target_pose[0], target_pose[1], safe_z, target_pose[3], target_pose[4], target_pose[5]]
  codex_autotune_return_guard_mask = 0
  codex_autotune_return_segment_id = 0
  codex_autotune_return_current_angular_speed_rad_s = 0.0
  codex_autotune_return_current_angular_accel_rad_s2 = 0.0
  codex_autotune_return_max_angular_speed_rad_s = 0.0
  codex_autotune_return_max_angular_accel_rad_s2 = 0.0
  codex_autotune_return_max_sample_gap_s = 0.0
  codex_autotune_return_observer_active = True
  local return_observer_handle = run codex_autotune_return_telemetry_observer()
  codex_autotune_return_segment_id = 1
  movel(rise_pose, a=0.060, v=0.040, r=0.0)
  stopl(0.1)
  codex_autotune_return_segment_id = 2
  movel(transfer_pose, a=0.135, v=0.090, r=0.0)
  stopl(0.1)
  codex_autotune_return_segment_id = 3
  movel(target_pose, a=0.060, v=0.040, r=0.0)
  stopl(0.1)
  sleep(0.20)
  codex_autotune_return_observer_active = False
  sync()
  kill return_observer_handle
  if not codex_autotune_typed_target_verified(target_pose, campaign_home_q, require_home_q):
    return False
  end
  # The main TP thread is the only motion owner.  Host/controller closure
  # validates the seven typed safety signals after the sequential movel route.
  codex_autotune_return_guard_mask = 127
  codex_autotune_latch_return_telemetry()
  return True
end

'''
    old_return = '''        local retract_start = get_actual_tcp_pose()
        local retract_pose = p[retract_start[0], retract_start[1], retract_start[2] + 0.010, retract_start[3], retract_start[4], retract_start[5]]
        movel(retract_pose, a=0.060, v=0.040, r=0.0)
        stopl(0.1)
        codex_autotune_write_state(campaign_epoch, trial_id, 50, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        movel(campaign_home_pose, a=0.030, v=0.050, r=0.0)
        stopl(0.1)
        codex_autotune_write_state(campaign_epoch, trial_id, 60, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        if not codex_autotune_home_verified(campaign_home_pose, campaign_home_q):
          codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        end'''
    new_return = '''        if not codex_autotune_single_owner_return(batch_row_index, campaign_home_pose, campaign_home_q):
          codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, 17, execution_profile_id, last_consumed_command_seq)
        end
        codex_autotune_write_state(campaign_epoch, trial_id, 50, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        codex_autotune_write_state(campaign_epoch, trial_id, 60, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)'''
    old_stage22 = '''    local p_current = get_actual_tcp_pose()
    if p_current[2] < precontact_z + minimum_start_above_entry_m:
      return 17.0
    else:
      write_output_float_register(35, 22.0)
      local entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]
      local entry_precontact_pose = p[entry_x, entry_y, precontact_z, target_rx, target_ry, target_rz]
      codex_echo_step4e(stop_reason)
      movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)
      stopl(0.1)
      movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)
      stopl(0.1)
      sleep(0.20)
    end'''
    new_stage22 = '''    local p_current = get_actual_tcp_pose()
    local expected_home_orientation = p[p_current[0], p_current[1], p_current[2], target_rx, target_ry, target_rz]
    local home_orientation_delta = pose_trans(pose_inv(expected_home_orientation), p_current)
    local home_orientation_error_rad = sqrt(home_orientation_delta[3] * home_orientation_delta[3] + home_orientation_delta[4] * home_orientation_delta[4] + home_orientation_delta[5] * home_orientation_delta[5])
    if p_current[2] < precontact_z + minimum_start_above_entry_m or home_orientation_error_rad > 0.035:
      return 17.0
    else:
      write_output_float_register(35, 22.0)
      local entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]
      local entry_precontact_pose = p[entry_x, entry_y, precontact_z, target_rx, target_ry, target_rz]
      codex_echo_step4e(stop_reason)
      movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)
      stopl(0.1)
      movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)
      stopl(0.1)
      sleep(0.20)
    end'''
    return (
        (
            "# HOST_TO_TP_INT: epoch=24 trial=25 command=26 token=27 profile=28 sequence=29",
            "# HOST_TO_TP_INT: epoch=24 trial=25 command=26 token=27 profile=28 sequence=29 batch_row=30 logical_batch=31",
            "batch-row host register",
        ),
        (
            "# TP_TO_HOST_INT: epoch=24 trial=25 state=26 token=27 reason=28 profile=29 consumed_sequence=30",
            "# TP_TO_HOST_INT: epoch=24 trial=25 state=26 token=27 reason=28 profile=29 consumed_sequence=30 batch_row=31 return_kind=32 guard_mask=33 logical_batch=34",
            "typed-return output registers",
        ),
        (
            "def codex_autotune_write_state(campaign_epoch, trial_id, state, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq):",
            globals_and_guard + "def codex_autotune_write_state(campaign_epoch, trial_id, state, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq):",
            "typed return helpers",
        ),
        (
            "  write_output_integer_register(30, consumed_command_seq)\nend",
            "  write_output_integer_register(30, consumed_command_seq)\n  write_output_integer_register(31, codex_autotune_batch_row_echo)\n  write_output_integer_register(32, codex_autotune_return_kind_echo)\n  write_output_integer_register(33, codex_autotune_return_guard_mask)\n  write_output_integer_register(34, codex_autotune_logical_batch_sequence_echo)\nend",
            "typed return echoes",
        ),
        (
            "def codex_step5d_autotune_trial_v1(campaign_home_pose, tp_speedj_accel_rad_s2):",
            "def codex_step5d_autotune_trial_v1(campaign_home_pose, tp_speedj_accel_rad_s2, batch_row_index):",
            "batch-row trial entry",
        ),
        (old_stage22, new_stage22, "first-only Stage22 and typed near-ready entry"),
        (
            "      local execution_profile_id = read_input_integer_register(28)\n      local tp_speedj_accel_rad_s2",
            "      local execution_profile_id = read_input_integer_register(28)\n      local batch_row_index = read_input_integer_register(30)\n      local logical_batch_sequence = read_input_integer_register(31)\n      local tp_speedj_accel_rad_s2",
            "batch-row ARM read",
        ),
        (
            "if campaign_epoch <= 0 or trial_id <= 0 or candidate_token <= 0 or execution_profile_id <= 0 or not codex_autotune_network_profile_valid(execution_profile_id)",
            "if campaign_epoch <= 0 or trial_id <= 0 or candidate_token <= 0 or execution_profile_id <= 0 or batch_row_index < 1 or batch_row_index > 5 or logical_batch_sequence <= 0 or not codex_autotune_network_profile_valid(execution_profile_id)",
            "batch-row ARM validation",
        ),
        (
            "      codex_autotune_write_state(campaign_epoch, trial_id, 11, candidate_token, 0, execution_profile_id, last_consumed_command_seq)",
            "      codex_autotune_batch_row_echo = batch_row_index\n      codex_autotune_logical_batch_sequence_echo = logical_batch_sequence\n      codex_autotune_return_kind_echo = 2\n      codex_autotune_return_guard_mask = 0\n      codex_autotune_write_state(campaign_epoch, trial_id, 11, candidate_token, 0, execution_profile_id, last_consumed_command_seq)",
            "typed return ARM identity",
        ),
        (
            "local stop_reason = codex_step5d_autotune_trial_v1(campaign_home_pose, tp_speedj_accel_rad_s2)",
            "local stop_reason = codex_step5d_autotune_trial_v1(campaign_home_pose, tp_speedj_accel_rad_s2, batch_row_index)",
            "batch-row trial call",
        ),
        (old_return, new_return, "exact guarded three-segment return"),
        (
            "read_input_integer_register(28) == execution_profile_id:",
            "read_input_integer_register(28) == execution_profile_id and read_input_integer_register(30) == batch_row_index and read_input_integer_register(31) == logical_batch_sequence:",
            "exact ACK batch-row binding",
        ),
        (
            "        local post_ack_state = codex_autotune_post_ack_state(stop_reason)\n        if post_ack_state == 75:",
            "        local post_ack_state = codex_autotune_post_ack_state(stop_reason)\n        if stop_reason == 1 and batch_row_index == 10:\n          codex_autotune_write_state(campaign_epoch, trial_id, 77, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)\n        elif stop_reason == 1:\n          codex_autotune_write_state(campaign_epoch, trial_id, 76, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)\n        elif post_ack_state == 75:",
            "typed post-ACK return state",
        ),
    )


def _apply_batch_lifecycle(source: str) -> str:
    result = source
    for old, new, role in _batch_lifecycle_replacements():
        result = _replace_once(result, old, new, role=role)
    return result


def _direct_arm_replacements() -> tuple[tuple[str, str, str], ...]:
    legacy_header = '''# STEP5D_AUTOTUNE_CONTINUOUS_TP: one campaign home, fresh integer handshake,
# immutable-bundle ACK barrier, and no automatic retry limit for infra reasons.'''
    direct_header = '''# STEP5D_AUTOTUNE_CONTINUOUS_TP: every trial returns to the captured campaign home.
# Immutable bundle cold-read precedes each rolling next ARM; no ACK command is active.'''
    legacy_stop_contract = '''# STOP_CONTRACT: integer STOP is polled only at READY_HOME/WAIT_ACK; during RUN
# the frozen trial consumes the legacy float stop_request safety carrier.'''
    direct_stop_contract = '''# STOP_CONTRACT: integer STOP is polled only in stationary ready states; during RUN
# the frozen trial consumes the legacy float stop_request safety carrier.'''
    legacy_post_ack_state = '''def codex_autotune_post_ack_state(stop_reason):
  # Host journals reason-4 host_cause separately.  TP reason 4 always returns
  # READY_HOME; host policy may remain WAIT_INFRA_READY for infra_stop.
  if stop_reason == 8 or stop_reason == 10 or stop_reason == 12 or stop_reason == 14:
    return 75
  elif stop_reason == 1 or stop_reason == 4:
    return 10
  end
  return 90
end'''
    direct_terminal_state = '''def codex_autotune_direct_terminal_state(stop_reason, batch_row_index):
  if stop_reason == 1:
    return 78
  elif stop_reason == 4 or stop_reason == 8 or stop_reason == 10 or stop_reason == 12 or stop_reason == 14:
    return 75
  end
  return 90
end'''
    legacy_fault = '''def codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq):
  while True:
    codex_autotune_write_state(campaign_epoch, trial_id, 90, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq)
    sync()
  end
end'''
    bounded_fault = f'''def codex_autotune_publish_state_and_halt(campaign_epoch, trial_id, state, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq):
  local publish_s = 0.0
  while publish_s < 0.100:
    codex_autotune_write_state(campaign_epoch, trial_id, state, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq)
    sync()
    publish_s = publish_s + get_steptime()
  end
  halt
end

def codex_autotune_publish_fault_and_halt(campaign_epoch, trial_id, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq):
  codex_autotune_publish_state_and_halt(campaign_epoch, trial_id, 90, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq)
end

def codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq):
  codex_autotune_publish_fault_and_halt(campaign_epoch, trial_id, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq)
end

def codex_autotune_wait_for_arm(campaign_epoch, trial_id, state, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq):
  local waiting_s = 0.0
  while waiting_s < {READY_ARM_TIMEOUT_S:.3f}:
    codex_autotune_write_state(campaign_epoch, trial_id, state, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq)
    local next_command = read_input_integer_register(26)
    local next_sequence = read_input_integer_register(29)
    if next_command == 1 and next_sequence > consumed_command_seq:
      return True
    elif state == 78 and next_command == 4 and next_sequence > consumed_command_seq and read_input_integer_register(24) == campaign_epoch and read_input_integer_register(25) == trial_id and read_input_integer_register(27) == candidate_token and read_input_integer_register(28) == execution_profile_id and read_input_integer_register(30) == codex_autotune_batch_row_echo and read_input_integer_register(31) == codex_autotune_logical_batch_sequence_echo:
      codex_autotune_publish_state_and_halt(campaign_epoch, trial_id, 77, candidate_token, terminal_reason, execution_profile_id, next_sequence)
    elif next_command == 3 and next_sequence > consumed_command_seq:
      codex_autotune_publish_fault_and_halt(campaign_epoch, trial_id, candidate_token, 4, execution_profile_id, next_sequence)
    elif trial_id > 0 and next_command == 2 and next_sequence > consumed_command_seq:
      codex_autotune_publish_fault_and_halt(campaign_epoch, trial_id, candidate_token, 13, execution_profile_id, consumed_command_seq)
    end
    sync()
    waiting_s = waiting_s + get_steptime()
  end
  codex_autotune_publish_fault_and_halt(campaign_epoch, trial_id, candidate_token, 19, execution_profile_id, consumed_command_seq)
  return False
end'''
    legacy_ack = '''        # Host may ACK only after immutable bundle + host 0.5 s safe closure.
        codex_autotune_write_state(campaign_epoch, trial_id, 70, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        local waiting_for_ack = True
        while waiting_for_ack:
          local ack_command = read_input_integer_register(26)
          local ack_sequence = read_input_integer_register(29)
          if ack_command == 2 and ack_sequence > last_consumed_command_seq and read_input_integer_register(24) == campaign_epoch and read_input_integer_register(25) == trial_id and read_input_integer_register(27) == candidate_token and read_input_integer_register(28) == execution_profile_id and read_input_integer_register(30) == batch_row_index and read_input_integer_register(31) == logical_batch_sequence:
            last_consumed_command_seq = ack_sequence
            waiting_for_ack = False
          elif ack_command == 3 and ack_sequence > last_consumed_command_seq:
            last_consumed_command_seq = ack_sequence
            codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, 4, execution_profile_id, last_consumed_command_seq)
          else:
            sync()
          end
        end

        local post_ack_state = codex_autotune_post_ack_state(stop_reason)
        if stop_reason == 1 and batch_row_index == 10:
          codex_autotune_write_state(campaign_epoch, trial_id, 77, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        elif stop_reason == 1:
          codex_autotune_write_state(campaign_epoch, trial_id, 76, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        elif post_ack_state == 75:
          codex_autotune_write_state(campaign_epoch, trial_id, 75, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        elif post_ack_state == 10:
          codex_autotune_write_state(0, 0, 10, 0, 0, 0, last_consumed_command_seq)
        else:
          codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        end'''
    direct_ready = '''        # r008 full-home rolling protocol: every sealed return waits at campaign home.
        if stop_reason == 1:
          codex_autotune_wait_for_arm(campaign_epoch, trial_id, 78, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        elif stop_reason == 4 or stop_reason == 8 or stop_reason == 10 or stop_reason == 12 or stop_reason == 14:
          codex_autotune_publish_state_and_halt(campaign_epoch, trial_id, 75, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        else:
          codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        end'''
    initial_ready = '''  codex_autotune_write_state(0, 0, 10, 0, 0, 0, 0)

  while True:'''
    initial_wait = '''  codex_autotune_wait_for_arm(0, 0, 10, 0, 0, 0, 0)

  while True:'''
    return (
        (legacy_header, direct_header, "direct ARM protocol header"),
        (legacy_stop_contract, direct_stop_contract, "stationary STOP contract"),
        (legacy_post_ack_state, direct_terminal_state, "direct terminal state map"),
        (legacy_fault, bounded_fault, "bounded terminal halt"),
        (legacy_ack, direct_ready, "direct ARM ready lifecycle"),
        (initial_ready, initial_wait, "bounded initial ARM wait"),
    )


def _apply_direct_arm_protocol(source: str) -> str:
    result = source
    for old, new, role in _direct_arm_replacements():
        result = _replace_once(result, old, new, role=role)
    return result


def _remove_direct_arm_protocol(source: str) -> str:
    result = source
    for old, new, role in reversed(_direct_arm_replacements()):
        result = _replace_once(result, new, old, role=f"normalized {role}")
    return result


def _remove_batch_lifecycle(source: str) -> str:
    result = source
    for old, new, role in reversed(_batch_lifecycle_replacements()):
        result = _replace_once(result, new, old, role=f"normalized {role}")
    return result


def render_script() -> str:
    parent = v1.render_script()
    parent_sha = hashlib.sha256(parent.encode("utf-8")).hexdigest()
    rendered = _replace_once(
        parent,
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v1",
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v3",
        role="stage identity",
    )
    rendered = _replace_once(
        rendered,
        "def codex_step5d_strict_rnn_autotune_v1():",
        "def codex_step5d_strict_rnn_autotune_v3():",
        role="main definition",
    )
    rendered = _replace_once(
        rendered,
        "codex_step5d_strict_rnn_autotune_v1()",
        "codex_step5d_strict_rnn_autotune_v3()",
        role="main call",
    )
    rendered = _replace_once(
        rendered,
        "      if stale_s2 > 1.000:",
        f"      if stale_s2 > {STAGE25_STALE_COMMAND_HOLD_S:.3f}:",
        role="Stage25 stale-command watchdog",
    )
    rendered = _replace_once(
        rendered,
        "      if stop_reason == 2 or stop_reason == 3 or stop_reason == 17:",
        "      if stop_reason == 2 or stop_reason == 3 or stop_reason == 14 or stop_reason == 17:",
        role="bridge-loss no-return policy",
    )
    rendered = _replace_once(
        rendered,
        "  local entry_x = 0.487795411\n"
        "  local entry_y = 0.129326793",
        f"  local entry_x = {PRECONTACT_XYZ_M[0]:.9f}\n"
        f"  local entry_y = {PRECONTACT_XYZ_M[1]:.9f}\n"
        f"  local precontact_z = {PRECONTACT_XYZ_M[2]:.9f}\n"
        f"  local minimum_start_above_entry_m = {MINIMUM_START_ABOVE_ENTRY_M:.9f}",
        role="contact-plus-0.1s prealign position",
    )
    rendered = _replace_once(
        rendered,
        "  # PRECONTACT_POSE_CONTRACT: pre_contact_search_gravity_down_v1; Stage22/24 TCP +Z targets base -Z.\n"
        "  local target_rx = 3.141592654\n"
        "  local target_ry = 0.000000000\n"
        "  local target_rz = 0.000000000",
        f"  # PRECONTACT_POSE_PRIOR_ID: {PRECONTACT_POSE_PRIOR_ID}; completed contact-plus-0.1s robust pose plus clearance before guarded search.\n"
        f"  local target_rx = {PRECONTACT_ROTVEC_RAD[0]:.9f}\n"
        f"  local target_ry = {PRECONTACT_ROTVEC_RAD[1]:.9f}\n"
        f"  local target_rz = {PRECONTACT_ROTVEC_RAD[2]:.9f}",
        role="precontact pose prior",
    )
    rendered = _replace_once(
        rendered,
        "    write_output_float_register(35, 22.0)\n"
        "    local p_current = get_actual_tcp_pose()\n"
        "    local entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]\n"
        "    codex_echo_step4e(stop_reason)\n"
        "    movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)\n"
        "    stopl(0.1)\n"
        "    sleep(0.20)\n"
        "    write_output_float_register(35, 23.0)",
        "    local p_current = get_actual_tcp_pose()\n"
        "    if p_current[2] < precontact_z + minimum_start_above_entry_m:\n"
        "      return 17.0\n"
        "    else:\n"
        "      write_output_float_register(35, 22.0)\n"
        "      local entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]\n"
        "      local entry_precontact_pose = p[entry_x, entry_y, precontact_z, target_rx, target_ry, target_rz]\n"
        "      codex_echo_step4e(stop_reason)\n"
        "      movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)\n"
        "      stopl(0.1)\n"
        "      movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)\n"
        "      stopl(0.1)\n"
        "      sleep(0.20)\n"
        "    end\n"
        "    write_output_float_register(35, 23.0)",
        role="two-step safe prealign",
    )
    rendered = _apply_batch_lifecycle(rendered)
    rendered = _apply_direct_arm_protocol(rendered)
    identity = (
        f"# RELEASE_STAGE_ID: {PROGRAM_NAME}\n"
        f"# CONTROL_PROFILE_ID: {CONTROL_PROFILE_ID}\n"
        f"# TP_PROGRAM_ID: {PROGRAM_NAME}\n"
        f"# PHYSICAL_PRIOR_SHA256: {PRECONTACT_POSE_PRIOR_SHA256}\n"
        f"# PARENT_AUTOTUNE_V1_RENDERED_SHA256: {parent_sha}\n"
    )
    rendered = identity + rendered
    validate_rendered_script(rendered, parent=parent)
    return rendered


def validate_rendered_script(script: str, *, parent: str | None = None) -> None:
    original = v1.render_script() if parent is None else parent
    required = (
        f"# RELEASE_STAGE_ID: {PROGRAM_NAME}",
        f"# CONTROL_PROFILE_ID: {CONTROL_PROFILE_ID}",
        f"# TP_PROGRAM_ID: {PROGRAM_NAME}",
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v3",
        "def codex_step5d_strict_rnn_autotune_v3():",
        "codex_step5d_autotune_trial_v1(campaign_home_pose, tp_speedj_accel_rad_s2, batch_row_index)",
        "codex_step5d_strict_rnn_autotune_v3()",
        f"# PRECONTACT_POSE_PRIOR_ID: {PRECONTACT_POSE_PRIOR_ID}",
        f"local entry_x = {PRECONTACT_XYZ_M[0]:.9f}",
        f"local entry_y = {PRECONTACT_XYZ_M[1]:.9f}",
        f"local precontact_z = {PRECONTACT_XYZ_M[2]:.9f}",
        f"local target_rx = {PRECONTACT_ROTVEC_RAD[0]:.9f}",
        f"local target_ry = {PRECONTACT_ROTVEC_RAD[1]:.9f}",
        f"local target_rz = {PRECONTACT_ROTVEC_RAD[2]:.9f}",
        "local entry_precontact_pose = p[entry_x, entry_y, precontact_z",
        "if p_current[2] < precontact_z + minimum_start_above_entry_m or home_orientation_error_rad > 0.035:",
        "movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)",
        "local qdot_cap_rad_s = 0.500",
        f"if stale_s2 > {STAGE25_STALE_COMMAND_HOLD_S:.3f}:",
        "read_input_integer_register(26)",
        "read_input_integer_register(30)",
        "write_output_integer_register(33, codex_autotune_return_guard_mask)",
        "thread codex_autotune_return_telemetry_observer():",
        "local controller_clock = time()",
        "local tcp_speed = get_actual_tcp_speed()",
        "write_output_float_register(35, 40.3)",
        "write_output_float_register(39, 3.0)",
        "codex_autotune_latch_return_telemetry()",
        "codex_autotune_single_owner_return(batch_row_index, campaign_home_pose, campaign_home_q)",
        "if stop_reason == 2 or stop_reason == 3 or stop_reason == 14 or stop_reason == 17:",
        "codex_autotune_wait_for_arm(campaign_epoch, trial_id, 78",
        "next_command == 4",
        "codex_autotune_publish_state_and_halt(campaign_epoch, trial_id, 77",
        "read_input_integer_register(31)",
        "write_output_integer_register(34, codex_autotune_logical_batch_sequence_echo)",
        f"while waiting_s < {READY_ARM_TIMEOUT_S:.3f}",
        "codex_autotune_wait_for_arm(0, 0, 10, 0, 0, 0, 0)",
        "codex_autotune_publish_fault_and_halt",
    )
    missing = [marker for marker in required if marker not in script]
    if missing:
        raise ValueError(f"V3 TP script lacks required markers: {missing}")
    motion_call = re.compile(
        r"\b(?:movel|movej|speedl|speedj|servoj|stopl|stopj)\s*\("
    )
    in_thread = False
    for line in script.splitlines():
        if line.startswith("thread "):
            in_thread = True
            continue
        if in_thread and line == "end":
            in_thread = False
            continue
        if in_thread and motion_call.search(line):
            raise ValueError("V3 TP helper thread contains a motion command")
    prefix_lines = 5
    normalized = "".join(script.splitlines(keepends=True)[prefix_lines:])
    normalized = _replace_once(
        normalized,
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v3",
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v1",
        role="normalized stage identity",
    )
    normalized = _replace_once(
        normalized,
        "def codex_step5d_strict_rnn_autotune_v3():",
        "def codex_step5d_strict_rnn_autotune_v1():",
        role="normalized main definition",
    )
    normalized = _replace_once(
        normalized,
        "codex_step5d_strict_rnn_autotune_v3()",
        "codex_step5d_strict_rnn_autotune_v1()",
        role="normalized main call",
    )
    normalized = _remove_direct_arm_protocol(normalized)
    normalized = _remove_batch_lifecycle(normalized)
    normalized = _replace_once(
        normalized,
        f"      if stale_s2 > {STAGE25_STALE_COMMAND_HOLD_S:.3f}:",
        "      if stale_s2 > 1.000:",
        role="normalized Stage25 stale-command watchdog",
    )
    normalized = _replace_once(
        normalized,
        "      if stop_reason == 2 or stop_reason == 3 or stop_reason == 14 or stop_reason == 17:",
        "      if stop_reason == 2 or stop_reason == 3 or stop_reason == 17:",
        role="normalized bridge-loss no-return policy",
    )
    normalized = _replace_once(
        normalized,
        f"  local entry_x = {PRECONTACT_XYZ_M[0]:.9f}\n"
        f"  local entry_y = {PRECONTACT_XYZ_M[1]:.9f}\n"
        f"  local precontact_z = {PRECONTACT_XYZ_M[2]:.9f}\n"
        f"  local minimum_start_above_entry_m = {MINIMUM_START_ABOVE_ENTRY_M:.9f}",
        "  local entry_x = 0.487795411\n"
        "  local entry_y = 0.129326793",
        role="normalized precontact position",
    )
    normalized = _replace_once(
        normalized,
        f"  # PRECONTACT_POSE_PRIOR_ID: {PRECONTACT_POSE_PRIOR_ID}; completed contact-plus-0.1s robust pose plus clearance before guarded search.\n"
        f"  local target_rx = {PRECONTACT_ROTVEC_RAD[0]:.9f}\n"
        f"  local target_ry = {PRECONTACT_ROTVEC_RAD[1]:.9f}\n"
        f"  local target_rz = {PRECONTACT_ROTVEC_RAD[2]:.9f}",
        "  # PRECONTACT_POSE_CONTRACT: pre_contact_search_gravity_down_v1; Stage22/24 TCP +Z targets base -Z.\n"
        "  local target_rx = 3.141592654\n"
        "  local target_ry = 0.000000000\n"
        "  local target_rz = 0.000000000",
        role="normalized precontact pose prior",
    )
    normalized = _replace_once(
        normalized,
        "    local p_current = get_actual_tcp_pose()\n"
        "    if p_current[2] < precontact_z + minimum_start_above_entry_m:\n"
        "      return 17.0\n"
        "    else:\n"
        "      write_output_float_register(35, 22.0)\n"
        "      local entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]\n"
        "      local entry_precontact_pose = p[entry_x, entry_y, precontact_z, target_rx, target_ry, target_rz]\n"
        "      codex_echo_step4e(stop_reason)\n"
        "      movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)\n"
        "      stopl(0.1)\n"
        "      movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)\n"
        "      stopl(0.1)\n"
        "      sleep(0.20)\n"
        "    end\n"
        "    write_output_float_register(35, 23.0)",
        "    write_output_float_register(35, 22.0)\n"
        "    local p_current = get_actual_tcp_pose()\n"
        "    local entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]\n"
        "    codex_echo_step4e(stop_reason)\n"
        "    movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)\n"
        "    stopl(0.1)\n"
        "    sleep(0.20)\n"
        "    write_output_float_register(35, 23.0)",
        role="normalized two-step prealign",
    )
    if normalized != original:
        raise ValueError("V3 TP differs from frozen V1 outside identity/precontact pose")


def source_stamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone(timedelta(hours=8)))
    return value.strftime("%Y-%m-%dT%H%MHKT_STEP5D_STRICT_RNN_AUTOTUNE_V3_R008")


def build_package_script(stamp: str) -> str:
    if not stamp or "\n" in stamp:
        raise ValueError("source stamp must be one non-empty line")
    return f"# VERSION: {stamp}\n" + render_script()


def build_txt(stamp: str) -> str:
    return f"""Step5d Autotune V3 TP package

Controller target:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Identity:
  release_stage_id={PROGRAM_NAME}
  control_profile_id={CONTROL_PROFILE_ID}
  tp_program_id={PROGRAM_NAME}

Motion class:
  Contact motion package. Upload/read-back does not Load or Play it.
  One Play enters the live campaign; there is no HIL HOLD or second user authorization.
  Before guarded search, Stage22 first moves at the existing safe Z, then moves
  vertically to {PRECONTACT_XYZ_M} with a {PRECONTACT_CLEARANCE_M:.3f} m clearance
  above the completed contact-plus-0.1 s robust surface pose. FAR/NEAR speeds and
  force thresholds remain frozen.

Frozen control contract:
  qdot cap 0.500 rad/s; target 12 N; input integer registers 24..31;
  output integer registers 24..34; heartbeat watchdog fail-closed.
  Every trial returns to the campaign home captured once when Play begins.
  Five-trial logical batches roll without imposing a physical stop at row 5 or 10.
  ACK_BUNDLE and WAIT_ACK are not active. A fresh next ARM is accepted only after
  the host has durably committed and cold-read the previous trial bundle.
  READY_HOME_NEXT waits at most {READY_ARM_TIMEOUT_S:.0f} s without motion.
"""


def simulate_return_telemetry(
    samples: Sequence[tuple[float, float, float, float]],
) -> dict[str, float]:
    """Mirror the generated read-only observer for production-chain tests."""

    if len(samples) < 2:
        raise ValueError("return telemetry simulation requires at least two samples")
    last_time_s, last_wx, last_wy, last_wz = map(float, samples[0])
    current_speed = (last_wx**2 + last_wy**2 + last_wz**2) ** 0.5
    current_accel = 0.0
    max_speed = current_speed
    max_accel = 0.0
    max_gap = 0.0
    for raw_sample in samples[1:]:
        controller_time_s, wx, wy, wz = map(float, raw_sample)
        loop_dt = controller_time_s - last_time_s
        if not math.isfinite(loop_dt) or loop_dt <= 0.0:
            raise ValueError("return telemetry controller time must increase")
        alpha = loop_dt / (0.020 + loop_dt)
        filtered_wx = last_wx + alpha * (wx - last_wx)
        filtered_wy = last_wy + alpha * (wy - last_wy)
        filtered_wz = last_wz + alpha * (wz - last_wz)
        current_speed = (wx**2 + wy**2 + wz**2) ** 0.5
        current_accel = (
            (filtered_wx - last_wx) ** 2
            + (filtered_wy - last_wy) ** 2
            + (filtered_wz - last_wz) ** 2
        ) ** 0.5 / loop_dt
        max_speed = max(max_speed, current_speed)
        max_accel = max(max_accel, current_accel)
        max_gap = max(max_gap, loop_dt)
        last_time_s = controller_time_s
        last_wx, last_wy, last_wz = filtered_wx, filtered_wy, filtered_wz
    return {
        "ur_output_double_register_35": 40.3,
        "ur_output_double_register_39": 3.0,
        "ur_output_double_register_40": current_speed,
        "ur_output_double_register_41": current_accel,
        "ur_output_double_register_42": max_speed,
        "ur_output_double_register_43": max_accel,
        "ur_output_double_register_44": max_gap,
    }


def numeric_sanity(script: str) -> dict[str, Any]:
    validate_rendered_script(script.split("\n", 1)[1] if script.startswith("# VERSION:") else script)
    return {
        "schema": "step5d.autotune-v3/tp-numeric-sanity-v1",
        "program": PROGRAM_NAME,
        "control_profile_id": CONTROL_PROFILE_ID,
        "delta_class": "identity_precontact_prior_exact_batch_lifecycle_single_owner_return_read_only_telemetry_v5",
        "precontact_pose_prior_id": PRECONTACT_POSE_PRIOR_ID,
        "physical_prior_sha256": PRECONTACT_POSE_PRIOR_SHA256,
        "reaction_normal_b": list(STEP5D_V3_PHYSICAL_PRIOR.reaction_normal_b),
        "approach_axis_b": list(STEP5D_V3_PHYSICAL_PRIOR.approach_axis_b),
        "precontact_xyz_m": list(PRECONTACT_XYZ_M),
        "precontact_rotvec_rad": list(PRECONTACT_ROTVEC_RAD),
        "precontact_clearance_m": PRECONTACT_CLEARANCE_M,
        "minimum_start_above_entry_m": MINIMUM_START_ABOVE_ENTRY_M,
        "precontact_z_policy": "contact_plus_0p1s_robust_z_plus_0p005m_clearance",
        "qdot_cap_rad_s": 0.5,
        "stage25_stale_command_hold_s": STAGE25_STALE_COMMAND_HOLD_S,
        "ready_arm_timeout_s": READY_ARM_TIMEOUT_S,
        "precontact_entry_accel_m_s2": 0.135,
        "precontact_entry_speed_m_s": 0.09,
        "far_search_speed_m_s": 0.03375,
        "speedj_acceleration_profiles_rad_s2": [0.1, 0.2, 0.5],
        "input_integer_registers": [24, 25, 26, 27, 28, 29, 30],
        "output_integer_registers": [24, 25, 26, 27, 28, 29, 30, 31, 32, 33],
        "safe_transfer_z_m": 0.033,
        "return_segment_count": 3,
        "batch_row_policy": "five_row_logical_batches_every_row_campaign_home",
        "host_protocol": "v3_full_home_rolling_arm_v1",
    }


def validate_triplet(script: str, txt: str, urp: bytes, stamp: str) -> dict[str, Any]:
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached = ""
    script_file = ""
    installation = ""
    for node in root.iter():
        if node.tag == "cachedContents":
            cached = html.unescape(node.text or "")
        elif node.tag == "file" and node.attrib.get("resolves-to") == "file":
            script_file = node.text or ""
        elif node.tag == "URProgram":
            installation = node.attrib.get("installationRelativePath", "")
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": root.attrib.get("name") == PROGRAM_NAME,
        "controller directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "script node": script_file == f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script",
        "cached script": cached == script,
        "installation path": bool(installation),
        "main entrypoint": script.rstrip().endswith(
            "codex_step5d_strict_rnn_autotune_v3()"
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"V3 TP triplet validation failed: {failed}")
    validate_rendered_script(script.split("\n", 1)[1])
    return checks


def write_triplet(output_dir: Path, stamp: str) -> dict[str, Any]:
    script = build_package_script(stamp)
    txt = build_txt(stamp)
    urp = v1.build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    checks = validate_triplet(script, txt, urp, stamp)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        ".script": output_dir / f"{PROGRAM_NAME}.script",
        ".txt": output_dir / f"{PROGRAM_NAME}.txt",
        ".urp": output_dir / f"{PROGRAM_NAME}.urp",
    }
    manifest_path = output_dir / f"{PROGRAM_NAME}.deploy-manifest.json"
    sanity_path = output_dir / f"{PROGRAM_NAME}.numeric-sanity.json"
    revision_outputs = (*paths.values(), manifest_path, sanity_path)
    collisions = [str(path) for path in revision_outputs if path.exists()]
    if collisions:
        raise FileExistsError(
            "TP revision is immutable; increment rNNN instead of overwriting: "
            + ", ".join(collisions)
        )
    with paths[".script"].open("x", encoding="utf-8") as handle:
        handle.write(script)
    with paths[".txt"].open("x", encoding="utf-8") as handle:
        handle.write(txt)
    with paths[".urp"].open("xb") as handle:
        handle.write(urp)
    digests = {
        suffix: hashlib.sha256(path.read_bytes()).hexdigest()
        for suffix, path in paths.items()
    }
    manifest = {
        "schema_version": 1,
        "basename": PROGRAM_NAME,
        "controller_directory": CONTROLLER_DIR,
        "artifacts": [
            {
                "filename": path.name,
                "source": path.name,
                "sha256": digests[suffix],
            }
            for suffix, path in paths.items()
        ],
    }
    with manifest_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    sanity = numeric_sanity(script)
    with sanity_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(sanity, indent=2, sort_keys=True) + "\n")
    return {
        "program": PROGRAM_NAME,
        "control_profile_id": CONTROL_PROFILE_ID,
        "controller_dir": CONTROLLER_DIR,
        "stamp": stamp,
        "paths": {suffix: str(path) for suffix, path in paths.items()},
        "sha256": digests,
        "deploy_manifest": str(manifest_path),
        "numeric_sanity": str(sanity_path),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=LOCAL_PROGRAM_DIR)
    parser.add_argument("--stamp", default=None)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            write_triplet(args.output_dir, args.stamp or source_stamp()),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
