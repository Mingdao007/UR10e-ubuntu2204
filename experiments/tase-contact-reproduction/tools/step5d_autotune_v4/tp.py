"""URScript renderer for the independent V4 r002 guarded command consumer."""

from __future__ import annotations

from step5d_eoat_profiles import load_new_eoat_profile, render_urscript_apply

from .contracts import PROGRAM, TARGET_FORCE_N, load_contract
from .wire import LAYOUT_CODE


CONTROLLER_DIR = "/programs/andyl/kunwei/step5"


def render_script() -> str:
    contract = load_contract()
    eoat_block = render_urscript_apply(load_new_eoat_profile())
    return f"""# ROLE: independent new-EOAT 5 N Autotune V4 executor
# TP_PROGRAM_ID: {PROGRAM}
# V4_CONTRACT_SHA256: {contract.sha256}
# V4_CAMPAIGN_FINGERPRINT: {contract.campaign_fingerprint}
# EOAT_CONTRACT_SHA256: {contract.eoat_sha256}
# TARGET_FORCE_N: {TARGET_FORCE_N:.1f} immutable, not transported as a tunable
# BOUNDARY: Kunwei-only wrench; no UR built-in force; no auto-Home

global codex_v4_entry_monitor_active = False
global codex_v4_entry_fault = 0.0
global codex_v4_entry_linear_cap = 0.0
global codex_v4_entry_angular_cap = 0.0
global codex_v4_last_heartbeat = 0.0
global codex_v4_heartbeat_gap_s = 0.0

def codex_abs(value):
  if value < 0.0:
    return -value
  end
  return value
end

def codex_finite(value, limit):
  return value == value and value <= limit and value >= -limit
end

def codex_v4_echo(stage, reason, internal_setpoint, elapsed_s):
  write_output_float_register(24, read_input_float_register(24))
  write_output_float_register(25, read_input_float_register(25))
  write_output_float_register(26, read_input_float_register(26))
  write_output_float_register(27, read_input_float_register(27))
  write_output_float_register(28, read_input_float_register(28))
  write_output_float_register(29, read_input_float_register(29))
  write_output_float_register(30, reason)
  write_output_float_register(31, read_input_float_register(30))
  write_output_float_register(32, read_input_float_register(45))
  write_output_float_register(33, read_input_float_register(46))
  write_output_float_register(34, internal_setpoint)
  write_output_float_register(35, stage)
  write_output_float_register(36, elapsed_s)
  write_output_float_register(37, read_input_float_register(37))
  write_output_float_register(38, read_input_float_register(38))
  write_output_float_register(39, read_input_float_register(39))
  write_output_float_register(40, read_input_float_register(40))
  write_output_float_register(41, read_input_float_register(41))
  write_output_float_register(42, read_input_float_register(42))
  write_output_float_register(43, read_input_float_register(43))
  write_output_float_register(44, read_input_float_register(44))
  write_output_float_register(45, read_input_float_register(45))
  write_output_float_register(46, read_input_float_register(46))
  write_output_float_register(47, read_input_float_register(47))
end

def codex_v4_update_heartbeat():
  local heartbeat = read_input_float_register(26)
  if not codex_finite(heartbeat, 1000000000000.0):
    codex_v4_heartbeat_gap_s = 1.0
  elif heartbeat == codex_v4_last_heartbeat:
    codex_v4_heartbeat_gap_s = codex_v4_heartbeat_gap_s + get_steptime()
  else:
    codex_v4_last_heartbeat = heartbeat
    codex_v4_heartbeat_gap_s = 0.0
  end
end

def codex_v4_common_guard(abs_normal_limit, force_norm_limit, torque_limit):
  local normal_load = read_input_float_register(24)
  local force_norm = read_input_float_register(25)
  local sensor_fresh = read_input_float_register(27)
  local stop_request = read_input_float_register(28)
  local eoat_ack = read_input_float_register(29)
  local torque_norm = read_input_float_register(30)
  if not codex_finite(normal_load, 1000.0) or not codex_finite(force_norm, 1000.0) or not codex_finite(sensor_fresh, 10.0) or not codex_finite(stop_request, 10.0) or not codex_finite(eoat_ack, 10.0) or not codex_finite(torque_norm, 100.0):
    return 3.0
  elif codex_v4_heartbeat_gap_s >= 0.080000000:
    return 2.0
  elif sensor_fresh < 0.5 or eoat_ack < 0.5:
    return 3.0
  elif stop_request > 0.5:
    return 4.0
  elif codex_abs(normal_load) >= abs_normal_limit:
    return 5.0
  elif force_norm >= force_norm_limit:
    return 6.0
  elif torque_norm >= torque_limit:
    return 7.0
  end
  return 0.0
end

def codex_v4_stationary_dwell(required_s, abs_normal_limit, force_norm_limit, torque_limit):
  local dwell_s = 0.0
  while dwell_s < required_s:
    codex_v4_update_heartbeat()
    local guard = codex_v4_common_guard(abs_normal_limit, force_norm_limit, torque_limit)
    local speed = get_actual_tcp_speed()
    local linear = sqrt(speed[0] * speed[0] + speed[1] * speed[1] + speed[2] * speed[2])
    local angular = sqrt(speed[3] * speed[3] + speed[4] * speed[4] + speed[5] * speed[5])
    if guard != 0.0 or codex_v4_entry_fault != 0.0:
      return False
    elif linear <= 0.000500000 and angular <= 0.005000000:
      dwell_s = dwell_s + get_steptime()
    else:
      dwell_s = 0.0
    end
    sync()
  end
  return True
end

thread codex_v4_entry_monitor():
  local stop_sent = False
  while codex_v4_entry_monitor_active:
    codex_v4_update_heartbeat()
    local guard = codex_v4_common_guard(3.0, 3.0, 0.2)
    local speed = get_actual_tcp_speed()
    local linear = sqrt(speed[0] * speed[0] + speed[1] * speed[1] + speed[2] * speed[2])
    local angular = sqrt(speed[3] * speed[3] + speed[4] * speed[4] + speed[5] * speed[5])
    if guard != 0.0:
      codex_v4_entry_fault = guard
    elif linear > codex_v4_entry_linear_cap + 0.000500000:
      codex_v4_entry_fault = 21.0
    elif codex_v4_entry_angular_cap > 0.0 and angular > codex_v4_entry_angular_cap + 0.005000000:
      codex_v4_entry_fault = 22.0
    end
    if codex_v4_entry_fault != 0.0 and not stop_sent:
      stopl(0.250000000)
      stop_sent = True
    end
    sync()
  end
end

def codex_v4_qdot_finite_and_bounded(qdot):
  local index = 0
  while index < 6:
    if not codex_finite(qdot[index], 0.150000000):
      return False
    end
    index = index + 1
  end
  return True
end

def codex_v4_read_qdot():
  return [read_input_float_register(37), read_input_float_register(38), read_input_float_register(39), read_input_float_register(40), read_input_float_register(41), read_input_float_register(42)]
end

def codex_v4_command_guard(last_sequence, expected_mode):
  local valid = read_input_float_register(43)
  local setpoint = read_input_float_register(44)
  local sequence = read_input_float_register(46)
  local layout = read_input_float_register(47)
  local command_mode = read_input_integer_register(25)
  local qdot = codex_v4_read_qdot()
  if not codex_finite(valid, 10.0) or not codex_finite(setpoint, 100.0) or not codex_finite(sequence, 1000000000000.0) or not codex_finite(layout, 10000.0):
    return 41.0
  elif valid < 0.5 or layout != {LAYOUT_CODE:.1f}:
    return 42.0
  elif sequence <= last_sequence:
    return 43.0
  elif command_mode != expected_mode:
    return 44.0
  elif setpoint < 1.000000000 or setpoint > 5.000000000:
    return 49.0
  elif not codex_v4_qdot_finite_and_bounded(qdot):
    return 45.0
  end
  return 0.0
end

def {PROGRAM}():
{eoat_block}
  local startup_last_heartbeat = read_input_float_register(26)
  local startup_first_increment_s = -1.0
  local startup_elapsed_s = 0.0
  local startup_increments = 0
  while startup_increments < 2 and startup_elapsed_s < 10.0:
    local heartbeat = read_input_float_register(26)
    local sensor_fresh = read_input_float_register(27)
    local eoat_ack = read_input_float_register(29)
    codex_v4_echo(10.0, 0.0, 1.0, startup_elapsed_s)
    if codex_finite(heartbeat, 1000000000000.0) and sensor_fresh > 0.5 and eoat_ack > 0.5 and heartbeat != startup_last_heartbeat:
      if startup_first_increment_s < 0.0:
        startup_first_increment_s = startup_elapsed_s
        startup_increments = 1
      elif startup_elapsed_s - startup_first_increment_s <= 0.250000000:
        startup_increments = startup_increments + 1
      else:
        startup_first_increment_s = startup_elapsed_s
        startup_increments = 1
      end
      startup_last_heartbeat = heartbeat
    end
    sync()
    startup_elapsed_s = startup_elapsed_s + get_steptime()
  end
  if startup_increments < 2:
    codex_v4_echo(90.0, 2.0, 1.0, startup_elapsed_s)
    halt
  end

  codex_v4_last_heartbeat = read_input_float_register(26)
  codex_v4_heartbeat_gap_s = 0.0
  codex_v4_entry_fault = 0.0
  codex_v4_entry_monitor_active = True
  local entry_monitor_handle = run codex_v4_entry_monitor()
  local current_pose = get_actual_tcp_pose()
  local transfer_z = current_pose[2]
  if transfer_z < 0.062863519:
    transfer_z = 0.062863519
  end
  codex_v4_entry_linear_cap = 0.040000000
  codex_v4_entry_angular_cap = 0.0
  movel(p[current_pose[0], current_pose[1], transfer_z, current_pose[3], current_pose[4], current_pose[5]], a=0.250000000, v=0.040000000, r=0.0)
  if codex_v4_entry_fault != 0.0 or not codex_v4_stationary_dwell(0.250000000, 3.0, 3.0, 0.2):
    stopl(0.250000000)
    codex_v4_entry_monitor_active = False
    kill entry_monitor_handle
    codex_v4_echo(90.0, 23.0, 1.0, 0.0)
    halt
  end
  codex_v4_entry_linear_cap = 0.010000000
  codex_v4_entry_angular_cap = 0.100000000
  movel(p[0.487834547, 0.129337053, transfer_z, 3.120752062, 0.0, 0.068626833], a=0.100000000, v=0.010000000, r=0.0)
  if codex_v4_entry_fault != 0.0 or not codex_v4_stationary_dwell(0.250000000, 3.0, 3.0, 0.2):
    stopl(0.250000000)
    codex_v4_entry_monitor_active = False
    kill entry_monitor_handle
    codex_v4_echo(90.0, 24.0, 1.0, 0.0)
    halt
  end
  codex_v4_entry_linear_cap = 0.005000000
  codex_v4_entry_angular_cap = 0.0
  movel(p[0.487834547, 0.129337053, 0.022863519, 3.120752062, 0.0, 0.068626833], a=0.050000000, v=0.005000000, r=0.0)
  if codex_v4_entry_fault != 0.0 or not codex_v4_stationary_dwell(0.250000000, 3.0, 3.0, 0.2):
    stopl(0.250000000)
    codex_v4_entry_monitor_active = False
    kill entry_monitor_handle
    codex_v4_echo(90.0, 25.0, 1.0, 0.0)
    halt
  end
  codex_v4_entry_monitor_active = False
  kill entry_monitor_handle

  local search_start_pose = get_actual_tcp_pose()
  local search_elapsed_s = 0.0
  local search_reason = 0.0
  while search_reason == 0.0:
    codex_v4_update_heartbeat()
    search_reason = codex_v4_common_guard(3.0, 3.0, 0.2)
    local normal_load = read_input_float_register(24)
    local force_norm = read_input_float_register(25)
    local search_pose = get_actual_tcp_pose()
    local search_travel = search_start_pose[2] - search_pose[2]
    if search_reason == 0.0 and (normal_load >= 0.800000000 or force_norm >= 1.000000000):
      search_reason = 11.0
    elif search_reason == 0.0 and search_travel >= 0.025000000:
      search_reason = 8.0
    elif search_reason == 0.0 and search_elapsed_s >= 90.000000000:
      search_reason = 10.0
    end
    codex_v4_echo(20.0, search_reason, 1.0, search_elapsed_s)
    if search_reason == 0.0:
      speedl([0.0, 0.0, -0.000500000, 0.0, 0.0, 0.0], 0.010000000, 0.008000000)
      search_elapsed_s = search_elapsed_s + 0.008000000
    end
  end
  stopl(0.010000000)
  if search_reason != 11.0 or not codex_v4_stationary_dwell(0.250000000, 3.0, 3.0, 0.2):
    codex_v4_echo(90.0, search_reason, 1.0, search_elapsed_s)
    halt
  end

  local baseline_elapsed_s = 0.0
  local baseline_reason = 0.0
  local last_sequence = read_input_float_register(46) - 1.0
  local baseline_transition_mode = 1
  local prior_internal_setpoint = 1.0
  while baseline_transition_mode == 1 and baseline_reason == 0.0:
    local actual_dt = get_steptime()
    if actual_dt <= 0.0 or actual_dt >= 0.080000000:
      baseline_reason = 46.0
    end
    codex_v4_update_heartbeat()
    if baseline_reason == 0.0:
      baseline_reason = codex_v4_common_guard(15.0, 20.0, 1.0)
    end
    baseline_elapsed_s = baseline_elapsed_s + actual_dt
    local internal_setpoint = read_input_float_register(44)
    baseline_transition_mode = read_input_integer_register(25)
    if baseline_elapsed_s > 20.0:
      baseline_reason = 47.0
    end
    if baseline_reason == 0.0 and baseline_transition_mode == 1:
      baseline_reason = codex_v4_command_guard(last_sequence, 1)
      if internal_setpoint - prior_internal_setpoint > 0.5 * actual_dt + 0.010000000 or internal_setpoint < prior_internal_setpoint - 0.010000000:
        baseline_reason = 49.0
      end
    end
    codex_v4_echo(21.0, baseline_reason, internal_setpoint, baseline_elapsed_s)
    if baseline_reason == 0.0 and baseline_transition_mode == 1:
      local qdot = codex_v4_read_qdot()
      speedj(qdot, 2.500000000, actual_dt)
      last_sequence = read_input_float_register(46)
      prior_internal_setpoint = internal_setpoint
    end
  end
  stopj(2.500000000)
  if baseline_reason != 0.0:
    codex_v4_echo(90.0, baseline_reason, 5.0, baseline_elapsed_s)
    halt
  end
  if not codex_v4_stationary_dwell(0.250000000, 15.0, 20.0, 1.0):
    codex_v4_echo(90.0, 48.0, 5.0, baseline_elapsed_s)
    halt
  end

  local baseline_successes = read_input_integer_register(24)
  if baseline_transition_mode == 3 and baseline_successes < 3:
    local baseline_retract_start_pose = get_actual_tcp_pose()
    local baseline_retract_start_z = baseline_retract_start_pose[2]
    local baseline_retract_m = 0.0
    while baseline_retract_m < 0.005000000:
      codex_v4_update_heartbeat()
      local retract_guard = codex_v4_common_guard(15.0, 20.0, 1.0)
      if retract_guard != 0.0:
        stopl(0.010000000)
        codex_v4_echo(90.0, retract_guard, 5.0, baseline_elapsed_s)
        halt
      end
      speedl([0.0, 0.0, 0.000500000, 0.0, 0.0, 0.0], 0.010000000, 0.008000000)
      local baseline_retract_pose = get_actual_tcp_pose()
      baseline_retract_m = baseline_retract_pose[2] - baseline_retract_start_z
    end
    stopl(0.010000000)
    codex_v4_echo(80.0, 31.0, 5.0, baseline_elapsed_s)
    halt
  elif baseline_transition_mode != 2 or baseline_successes < 3:
    codex_v4_echo(90.0, 50.0, 5.0, baseline_elapsed_s)
    halt
  end

  local path_elapsed_s = 0.0
  local path_reason = 0.0
  while path_elapsed_s < 60.000000000 and path_reason == 0.0:
    local actual_dt = get_steptime()
    if actual_dt <= 0.0 or actual_dt >= 0.080000000:
      path_reason = 46.0
    end
    codex_v4_update_heartbeat()
    if path_reason == 0.0:
      path_reason = codex_v4_common_guard(15.0, 20.0, 1.0)
    end
    if path_reason == 0.0:
      path_reason = codex_v4_command_guard(last_sequence, 2)
    end
    codex_v4_echo(25.0, path_reason, 5.0, path_elapsed_s)
    if path_reason == 0.0:
      local qdot = codex_v4_read_qdot()
      speedj(qdot, 2.500000000, actual_dt)
      last_sequence = read_input_float_register(46)
      path_elapsed_s = path_elapsed_s + actual_dt
    end
  end
  stopj(2.500000000)
  if path_reason != 0.0 or not codex_v4_stationary_dwell(0.250000000, 15.0, 20.0, 1.0):
    codex_v4_echo(90.0, path_reason, 5.0, path_elapsed_s)
    halt
  end

  local retract_start_pose = get_actual_tcp_pose()
  local retract_start_z = retract_start_pose[2]
  local retract_m = 0.0
  while retract_m < 0.005000000:
    codex_v4_update_heartbeat()
    local retract_guard = codex_v4_common_guard(15.0, 20.0, 1.0)
    if retract_guard != 0.0:
      stopl(0.010000000)
      codex_v4_echo(90.0, retract_guard, 5.0, path_elapsed_s)
      halt
    end
    speedl([0.0, 0.0, 0.000500000, 0.0, 0.0, 0.0], 0.010000000, 0.008000000)
    local retract_pose = get_actual_tcp_pose()
    retract_m = retract_pose[2] - retract_start_z
  end
  stopl(0.010000000)
  codex_v4_echo(80.0, 32.0, 5.0, path_elapsed_s)
  halt
end

{PROGRAM}()
"""


__all__ = ["CONTROLLER_DIR", "render_script"]
