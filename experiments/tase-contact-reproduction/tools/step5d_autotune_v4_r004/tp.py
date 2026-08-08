"""URScript renderer for the isolated V4 r004 resident ARM loop.

The host-side primitives and this TP renderer intentionally share the same
register contract, but neither side is a generic plugin framework.  The TP
owns the bounded motion state machine; the host owns fresh Kunwei packets,
session receipts, and durable campaign rows.
"""

from __future__ import annotations

from typing import Any

from step5d_eoat_profiles import load_new_eoat_profile

from .contracts import (
    CONTROLLER_READBACK_MAX_AGE_S,
    PACKET_STALE_S,
    POST_LATCH_TIMEOUT_S,
    PRE_LATCH_TIMEOUT_S,
    PROGRAM,
    SCRIPT1_PROGRAM,
    TARGET_FORCE_N,
    load_contract,
    runtime_identity_limbs,
)
from .home_profile import (
    ATTEMPT_ENTRY_NOT_CAPTURED_HOME,
    RETURN_RISE_ACCEL_M_S2,
    RETURN_RISE_SPEED_M_S,
    RETURN_TRANSFER_ACCEL_M_S2,
    RETURN_TRANSFER_SPEED_M_S,
    FixedHomeProfile,
    load_fixed_home_profile,
)

from .wire import (
    LAYOUT_TAG,
    OUTPUT_INTEGER_FIELDS,
    INPUT_INTEGER_REGISTERS,
    DOUBLE_REGISTERS,
)


CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
RUNTIME_PROTOCOL = 606004


def _runtime_limbs(
    program: str, contract_sha256: str, campaign_fingerprint: str
) -> tuple[int, int]:
    """Map the bound runtime identity material to two URScript int limbs."""

    return runtime_identity_limbs(program, contract_sha256, campaign_fingerprint)


def _compare_payload_lines() -> str:
    lines = ["  local same = True"]
    for register in DOUBLE_REGISTERS:
        lines.extend(
            (
                f"  if codex_r004_cache_d{register} != read_input_float_register({register}):",
                "    same = False",
                "  end",
            )
        )
    for register in INPUT_INTEGER_REGISTERS:
        lines.extend(
            (
                f"  if codex_r004_cache_i{register} != read_input_integer_register({register}):",
                "    same = False",
                "  end",
            )
        )
    lines.append("  return same")
    return "\n".join(lines)


def _copy_payload_lines() -> str:
    lines: list[str] = []
    for register in DOUBLE_REGISTERS:
        lines.append(
            f"  codex_r004_cache_d{register} = read_input_float_register({register})"
        )
    for register in INPUT_INTEGER_REGISTERS:
        lines.append(
            f"  codex_r004_cache_i{register} = read_input_integer_register({register})"
        )
    return "\n".join(lines)


def _finite_payload_lines() -> str:
    lines = [
        "  if not codex_r004_finite(read_input_float_register(24), 1000000.0):",
        "    return 41",
        "  end",
    ]
    for register in range(25, 48):
        lines.extend(
            (
                f"  if not codex_r004_finite(read_input_float_register({register}), 1000000000000.0):",
                "    return 41",
                "  end",
            )
        )
    return "\n".join(lines)


def _script_body(contract: Any, home_profile: FixedHomeProfile) -> str:
    raw = contract.raw
    contact = raw["contact_acquisition"]
    runtime = raw["runtime"]
    motion_runtime = contract.motion_runtime
    baseline = raw["baseline"]
    return_home = raw["return_home"]
    runtime_hi, runtime_lo = _runtime_limbs(
        PROGRAM, contract.sha256, contract.campaign_fingerprint
    )
    anchor_damping = float(raw["fingerprint"]["damping"])
    path_runtime = float(runtime["path_runtime_s"])
    qdot_limit = motion_runtime.qdot_abs_rad_s
    tp_acceleration = motion_runtime.tp_speedj_acceleration_rad_s2
    retract_min = float(return_home["retract_min_m"])
    home_pos_tol = home_profile.position_tolerance_m
    home_rot_tol = home_profile.orientation_tolerance_rad
    home_q_tol = home_profile.joint_tolerance_rad
    fixed_home_pose = ", ".join(f"{value:.9f}" for value in home_profile.pose)
    return_rise_accel = f"{RETURN_RISE_ACCEL_M_S2:.3f}"
    return_rise_speed = f"{RETURN_RISE_SPEED_M_S:.3f}"
    return_transfer_accel = f"{RETURN_TRANSFER_ACCEL_M_S2:.3f}"
    return_transfer_speed = f"{RETURN_TRANSFER_SPEED_M_S:.3f}"
    tp_acceleration_literal = f"{tp_acceleration:.9f}"

    copy_payload = _copy_payload_lines()
    compare_payload = _compare_payload_lines()
    finite_payload = _finite_payload_lines()

    return f"""# ROLE: isolated V4 r004 resident rolling ARM loop
# TP_PROGRAM_ID: {PROGRAM}
# EXPECTED_PROGRAM: {PROGRAM}
# WIRE_LAYOUT: layout-606
# V4_CONTRACT_SHA256: {contract.sha256}
# V4_CAMPAIGN_FINGERPRINT: {contract.campaign_fingerprint}
# EOAT_PROFILE_SHA256: {contract.eoat_sha256}
# SCRIPT1_PROGRAM: {SCRIPT1_PROGRAM}
# SCRIPT1_POLICY: initial and after STOP/problem/restart only; never per attempt
# TARGET_FORCE_N: {TARGET_FORCE_N:.1f} immutable; D_ANCHOR: {anchor_damping:.1f}
# WRENCH_AUTHORITY: Kunwei only; no UR built-in force; no sensor zero/tare/config writes
# LIFECYCLE: fixed EOAT-bound Home; every ARM verifies pose and q branch before motion
# STATE: READY_HOME_NEXT=78, COMPLETE=80, STOPPED=90; no auto-resume after fault
# SESSION_COMMAND: SessionCommand HOLD=0 ARM=1 COMPLETE=2 STOP=3
# FRESHNESS: newer sequence updates immutable cache; equal sequence needs exact payload
# FRESHNESS_FAILURE: equal changed payload, regression, or held age >= {PACKET_STALE_S:.3f}s => reason 43
# LATCH_TIMING: pre-latch <= {PRE_LATCH_TIMEOUT_S:.1f}s, separate post-latch <= {POST_LATCH_TIMEOUT_S:.1f}s
# FIXED_HOME_PROFILE: {home_profile.home_profile_id}
# FIXED_HOME_POSE: p[{fixed_home_pose}]
# HOME_ENTRY: position <= {home_pos_tol:.3f}m, orientation <= {home_rot_tol:.3f}rad, q <= {home_q_tol:.3f}rad
# HOME_ENTRY_FAILURE: {ATTEMPT_ENTRY_NOT_CAPTURED_HOME} ATTEMPT_ENTRY_NOT_CAPTURED_HOME; stop without Script2 auto-home
# RETURN: V3 rise/descent a={return_rise_accel} v={return_rise_speed}; XY transfer a={return_transfer_accel} v={return_transfer_speed}; fixed Home
# CONTACT_SEARCH: downward V4 speed={float(contact["speed_m_s"]):.7f}m/s, accel={float(contact["acceleration_m_s2"]):.3f}m/s^2, max={float(contact["max_travel_m"]):.3f}m, timeout={float(contact["timeout_s"]):.1f}s
# OUTPUT_INT_24_34: epoch, ordinal, state, token, reason, consumed_session_seq, kind, return_guard, runtime_protocol, digest_hi, digest_lo
# OUTPUT_DOUBLE_24: latest packet_sequence actually consumed by the TP loop

global codex_r004_cache_valid = False
global codex_r004_cache_sequence = -1.0
global codex_r004_cache_age_s = 0.0
global codex_r004_attempt_success = False
global codex_r004_attempt_reason = 0
global codex_r004_attempt_guard = 0

""" + "\n".join(
        f"global codex_r004_cache_d{register} = 0.0" for register in DOUBLE_REGISTERS
    ) + "\n" + "\n".join(
        f"global codex_r004_cache_i{register} = 0" for register in INPUT_INTEGER_REGISTERS
    ) + f"""

def codex_r004_finite(value, limit):
  return value == value and value <= limit and value >= -limit
end

def codex_r004_abs(value):
  if value < 0.0:
    return -value
  end
  return value
end

def codex_r004_packet_payload_equal():
{compare_payload}
end

def codex_r004_copy_packet():
{copy_payload}
  codex_r004_cache_sequence = read_input_float_register(46)
  codex_r004_cache_age_s = 0.0
  codex_r004_cache_valid = True
end

def codex_r004_packet_observe():
{finite_payload}
  local sequence = read_input_float_register(46)
  local layout = read_input_float_register(47)
  local valid = read_input_float_register(43)
  if sequence < 0.0 or sequence != floor(sequence):
    return 41
  elif layout != {LAYOUT_TAG:.1f} or valid < 0.5:
    return 42
  elif not codex_r004_cache_valid:
    codex_r004_copy_packet()
    return 0
  elif sequence > codex_r004_cache_sequence:
    codex_r004_copy_packet()
    return 0
  elif sequence < codex_r004_cache_sequence:
    return 43
  elif not codex_r004_packet_payload_equal():
    return 43
  end
  codex_r004_cache_age_s = codex_r004_cache_age_s + get_steptime()
  if codex_r004_cache_age_s >= {PACKET_STALE_S:.9f}:
    return 43
  end
  return 0
end

def codex_r004_integer_wire_guard():
  local baseline_successes = read_input_integer_register(24)
  local command_mode = read_input_integer_register(25)
  local latch = read_input_integer_register(26)
  local session_command = read_input_integer_register(27)
  local session_sequence = read_input_integer_register(28)
  local epoch = read_input_integer_register(29)
  local ordinal = read_input_integer_register(30)
  local kind = read_input_integer_register(31)
  local token = read_input_integer_register(32)
  if baseline_successes < 0 or baseline_successes > 3:
    return 53
  elif command_mode < 0 or command_mode > 4:
    return 54
  elif latch < 0 or latch > 1:
    return 52
  elif session_command < 0 or session_command > 3:
    return 66
  elif session_sequence < 0 or epoch < 0 or ordinal < 0 or ordinal > 16 or kind < 0 or kind > 4 or token < 0:
    return 67
  end
  return 0
end

def codex_r004_qdot_ok():
  local index = 0
  while index < 6:
    local qdot = read_input_float_register(37 + index)
    if not codex_r004_finite(qdot, {qdot_limit:.9f}):
      return False
    end
    index = index + 1
  end
  return True
end

def codex_r004_packet_guard(packet_reason, abs_normal_limit, force_norm_limit, torque_limit):
  if packet_reason != 0:
    return packet_reason
  elif read_input_float_register(27) < 0.5 or read_input_float_register(29) < 0.5:
    return 3
  elif read_input_float_register(28) > 0.5:
    return 4
  elif read_input_integer_register(27) == 3:
    return 4
  elif read_input_integer_register(27) != 0 and read_input_integer_register(27) != 1:
    return 66
  elif codex_r004_abs(read_input_float_register(24)) >= abs_normal_limit:
    return 5
  elif read_input_float_register(25) >= force_norm_limit:
    return 6
  elif read_input_float_register(30) >= torque_limit:
    return 7
  elif read_input_float_register(44) < 1.0 or read_input_float_register(44) > {TARGET_FORCE_N:.1f}:
    return 49
  elif not codex_r004_qdot_ok():
    return 45
  end
  return 0
end

def codex_r004_stationary(required_s):
  local dwell_s = 0.0
  local elapsed_s = 0.0
  # A blocking move may expose one deceleration sample after it returns.
  # Keep the existing speed thresholds and require the full dwell, but allow
  # at most four dwell-windows for that residual motion to settle.  With the
  # fixed 250 ms call sites this is a 1.0 s settle plus 250 ms full dwell.
  while dwell_s < required_s and elapsed_s < required_s * 5.0:
    local packet_reason = codex_r004_packet_observe()
    local guard = codex_r004_packet_guard(packet_reason, 60.0, 100.0, 3.0)
    local speed = get_actual_tcp_speed()
    local linear = sqrt(speed[0] * speed[0] + speed[1] * speed[1] + speed[2] * speed[2])
    local angular = sqrt(speed[3] * speed[3] + speed[4] * speed[4] + speed[5] * speed[5])
    if guard != 0:
      return False
    elif linear > {float(runtime["stationary_linear_m_s"]):.9f} or angular > {float(runtime["stationary_angular_rad_s"]):.9f}:
      dwell_s = 0.0
    else:
      dwell_s = dwell_s + get_steptime()
    end
    elapsed_s = elapsed_s + get_steptime()
    sync()
  end
  return dwell_s >= required_s
end

def codex_r004_pose_close(actual, expected):
  local position_error = sqrt((actual[0] - expected[0]) * (actual[0] - expected[0]) + (actual[1] - expected[1]) * (actual[1] - expected[1]) + (actual[2] - expected[2]) * (actual[2] - expected[2]))
  local rotation_error = sqrt((actual[3] - expected[3]) * (actual[3] - expected[3]) + (actual[4] - expected[4]) * (actual[4] - expected[4]) + (actual[5] - expected[5]) * (actual[5] - expected[5]))
  return position_error <= {home_pos_tol:.9f} and rotation_error <= {home_rot_tol:.9f}
end

def codex_r004_q_close(actual, expected):
  local index = 0
  while index < 6:
    if not codex_r004_finite(actual[index], 1000000.0) or not codex_r004_finite(expected[index], 1000000.0):
      return False
    elif codex_r004_abs(actual[index] - expected[index]) > {home_q_tol:.9f}:
      return False
    end
    index = index + 1
  end
  return True
end

def codex_r004_home_close(expected_pose, expected_q):
  local actual_pose = get_actual_tcp_pose()
  if not codex_r004_pose_close(actual_pose, expected_pose):
    return False
  end
  local actual_q = get_actual_joint_positions()
  if not codex_r004_q_close(actual_q, expected_q):
    return False
  end
  return True
end

def codex_r004_entry_home_verified(expected_pose, locked_q, locked_q_valid):
  # The Cartesian check is authoritative.  Only after it passes may the
  # current q establish or be compared with the resident IK branch.
  local actual_pose = get_actual_tcp_pose()
  if not codex_r004_pose_close(actual_pose, expected_pose):
    return False
  end
  local actual_q = get_actual_joint_positions()
  if not codex_r004_q_close(actual_q, actual_q):
    return False
  elif locked_q_valid and not codex_r004_q_close(actual_q, locked_q):
    return False
  end
  return True
end

def codex_r004_echo(epoch, ordinal, state, token, reason, consumed, kind, return_guard, runtime_hi, runtime_lo):
  write_output_float_register(24, codex_r004_cache_sequence)
  write_output_integer_register(24, epoch)
  write_output_integer_register(25, ordinal)
  write_output_integer_register(26, state)
  write_output_integer_register(27, token)
  write_output_integer_register(28, reason)
  write_output_integer_register(29, consumed)
  write_output_integer_register(30, kind)
  write_output_integer_register(31, return_guard)
  write_output_integer_register(32, {RUNTIME_PROTOCOL})
  write_output_integer_register(33, runtime_hi)
  write_output_integer_register(34, runtime_lo)
end

def codex_r004_fault(epoch, ordinal, token, kind, consumed, reason, runtime_hi, runtime_lo):
  stopl(0.250000000)
  codex_r004_attempt_reason = reason
  codex_r004_attempt_guard = 0
  codex_r004_attempt_success = False
  codex_r004_echo(epoch, ordinal, 90, token, reason, consumed, kind, 0, runtime_hi, runtime_lo)
  return False
end

def codex_r004_return_fault(epoch, ordinal, token, kind, consumed, reason, runtime_hi, runtime_lo, return_guard):
  stopl(0.250000000)
  codex_r004_attempt_reason = reason
  codex_r004_attempt_guard = return_guard
  codex_r004_attempt_success = False
  codex_r004_echo(epoch, ordinal, 90, token, reason, consumed, kind, return_guard, runtime_hi, runtime_lo)
  return False
end

def codex_r004_return_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_hi, runtime_lo, current_guard):
  local return_guard = current_guard
  local packet_reason = codex_r004_packet_observe()
  local guard = codex_r004_packet_guard(packet_reason, 60.0, 100.0, 3.0)
  if guard != 0 or not codex_r004_stationary(0.250000000):
    return codex_r004_return_fault(epoch, ordinal, token, kind, consumed, 58, runtime_hi, runtime_lo, return_guard)
  end
  # Keep the V3 sequential route: rise clear of the fixed Home, transfer XY
  # at the clear height, then descend to the fixed Home.  There is no legacy
  # legacy upward transfer floor in this V4 route.
  local current_pose = get_actual_tcp_pose()
  local safe_z = home_pose[2] + {retract_min:.9f}
  if current_pose[2] + {retract_min:.9f} > safe_z:
    safe_z = current_pose[2] + {retract_min:.9f}
  end
  local rise_pose = p[current_pose[0], current_pose[1], safe_z, current_pose[3], current_pose[4], current_pose[5]]
  local transfer_pose = p[home_pose[0], home_pose[1], safe_z, home_pose[3], home_pose[4], home_pose[5]]
  movel(rise_pose, a={return_rise_accel}, v={return_rise_speed}, r=0.0)
  stopl(0.1)
  if not codex_r004_stationary(0.250000000):
    return codex_r004_return_fault(epoch, ordinal, token, kind, consumed, 59, runtime_hi, runtime_lo, return_guard)
  end
  return_guard = return_guard + 1 + 2 + 8
  codex_r004_echo(epoch, ordinal, 40, token, 0, consumed, kind, return_guard, runtime_hi, runtime_lo)
  movel(transfer_pose, a={return_transfer_accel}, v={return_transfer_speed}, r=0.0)
  stopl(0.1)
  if not codex_r004_stationary(0.250000000):
    return codex_r004_return_fault(epoch, ordinal, token, kind, consumed, 59, runtime_hi, runtime_lo, return_guard)
  end
  return_guard = return_guard + 16
  codex_r004_echo(epoch, ordinal, 40, token, 0, consumed, kind, return_guard, runtime_hi, runtime_lo)
  movel(home_pose, a={return_rise_accel}, v={return_rise_speed}, r=0.0)
  stopl(0.1)
  if not codex_r004_stationary(0.250000000):
    return codex_r004_return_fault(epoch, ordinal, token, kind, consumed, 60, runtime_hi, runtime_lo, return_guard)
  end
  codex_r004_echo(epoch, ordinal, 40, token, 0, consumed, kind, return_guard, runtime_hi, runtime_lo)
  if not codex_r004_home_close(home_pose, home_q):
    return codex_r004_return_fault(epoch, ordinal, token, kind, consumed, 60, runtime_hi, runtime_lo, return_guard)
  end
  return_guard = return_guard + 32 + 64
  codex_r004_attempt_guard = return_guard
  codex_r004_attempt_reason = 0
  codex_r004_attempt_success = True
  codex_r004_echo(epoch, ordinal, 78, token, 0, consumed, kind, return_guard, runtime_hi, runtime_lo)
  return True
end

def codex_r004_execute_attempt(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_hi, runtime_lo):
  codex_r004_attempt_success = False
  codex_r004_attempt_reason = 0
  codex_r004_attempt_guard = 0
  local packet_reason = codex_r004_packet_observe()
  local guard = codex_r004_packet_guard(packet_reason, 60.0, 100.0, 3.0)
  if guard != 0:
    return codex_r004_fault(epoch, ordinal, token, kind, consumed, guard, runtime_hi, runtime_lo)
  end
  if not codex_r004_stationary(0.250000000):
    return codex_r004_fault(epoch, ordinal, token, kind, consumed, 23, runtime_hi, runtime_lo)
  end
  # ARM entry has already verified the fixed Home.  Start the existing V4
  # bounded negative-Z search directly from that stationary pose.
  local contact_start = get_actual_tcp_pose()
  local contact_elapsed_s = 0.0
  local contact_confirm_s = 0.0
  local contact_done = False
  while not contact_done:
    packet_reason = codex_r004_packet_observe()
    guard = codex_r004_packet_guard(packet_reason, 60.0, 100.0, 3.0)
    local contact_pose = get_actual_tcp_pose()
    local travel = contact_start[2] - contact_pose[2]
    if guard != 0:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, guard, runtime_hi, runtime_lo)
    elif travel >= {float(contact["max_travel_m"]):.9f}:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, 8, runtime_hi, runtime_lo)
    elif contact_elapsed_s >= {float(contact["timeout_s"]):.9f}:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, 10, runtime_hi, runtime_lo)
    else:
      local contact_dt = get_steptime()
      if read_input_float_register(24) >= 0.500000000 or read_input_float_register(25) >= 0.700000000:
        contact_confirm_s = contact_confirm_s + contact_dt
      else:
        contact_confirm_s = 0.0
      end
      if contact_confirm_s >= {PACKET_STALE_S:.9f}:
        contact_done = True
      else:
        speedl([0.0, 0.0, -{float(contact["speed_m_s"]):.9f}, 0.0, 0.0, 0.0], {float(contact["acceleration_m_s2"]):.9f}, contact_dt)
        contact_elapsed_s = contact_elapsed_s + contact_dt
        codex_r004_echo(epoch, ordinal, 20, token, 0, consumed, kind, 0, runtime_hi, runtime_lo)
      end
    end
  end
  stopl(0.010000000)
  if not codex_r004_stationary(0.250000000):
    return codex_r004_fault(epoch, ordinal, token, kind, consumed, 11, runtime_hi, runtime_lo)
  end

  local pre_latch_elapsed_s = 0.0
  local post_latch_elapsed_s = 0.0
  local latch_seen = False
  local prior_setpoint = 1.000000000
  local prior_baseline_successes = read_input_integer_register(24)
  local baseline_done = False
  local baseline_mode = 0
  # Publish the baseline state before requiring its host command.  The host
  # deliberately keeps HOLD through contact acquisition and can only switch
  # to BASELINE after observing state 21.  Bound that transition by the same
  # 80 ms packet-age invariant; the main loop retains reason 44 on timeout.
  local baseline_entry_elapsed_s = 0.0
  while read_input_integer_register(25) == 0 and baseline_entry_elapsed_s < {PACKET_STALE_S:.9f}:
    codex_r004_echo(epoch, ordinal, 21, token, 0, consumed, kind, 0, runtime_hi, runtime_lo)
    baseline_entry_elapsed_s = baseline_entry_elapsed_s + get_steptime()
    sync()
  end
  while not baseline_done:
    local actual_dt = get_steptime()
    if actual_dt <= 0.0 or actual_dt >= {PACKET_STALE_S:.9f}:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, 46, runtime_hi, runtime_lo)
    end
    packet_reason = codex_r004_packet_observe()
    guard = codex_r004_packet_guard(packet_reason, 60.0, 100.0, 3.0)
    local integer_reason = codex_r004_integer_wire_guard()
    local latch = read_input_integer_register(26)
    local baseline_successes = read_input_integer_register(24)
    baseline_mode = read_input_integer_register(25)
    local setpoint = read_input_float_register(44)
    if guard != 0:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, guard, runtime_hi, runtime_lo)
    elif integer_reason != 0:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, integer_reason, runtime_hi, runtime_lo)
    elif baseline_successes < prior_baseline_successes:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, 53, runtime_hi, runtime_lo)
    elif baseline_mode != 1 and baseline_mode != 2 and baseline_mode != 3:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, 44, runtime_hi, runtime_lo)
    elif not latch_seen and latch == 0 and pre_latch_elapsed_s + actual_dt > {float(baseline["pre_latch_timeout_s"]):.9f}:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, 47, runtime_hi, runtime_lo)
    elif latch_seen and latch == 0:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, 52, runtime_hi, runtime_lo)
    elif latch == 1 and not latch_seen:
      latch_seen = True
      post_latch_elapsed_s = 0.0
    end
    local baseline_qdot = [read_input_float_register(37), read_input_float_register(38), read_input_float_register(39), read_input_float_register(40), read_input_float_register(41), read_input_float_register(42)]
    if not latch_seen:
      pre_latch_elapsed_s = pre_latch_elapsed_s + actual_dt
      # Canonical V4 WAIT_ONE_NEWTON publishes a guarded approach command.
      # Execute it so contact can progress from the lower acquisition
      # threshold to the sticky 1 N latch.
      speedj(baseline_qdot, {tp_acceleration_literal}, actual_dt)
      codex_r004_echo(epoch, ordinal, 21, token, 0, consumed, kind, 0, runtime_hi, runtime_lo)
      sync()
    else:
      post_latch_elapsed_s = post_latch_elapsed_s + actual_dt
      if post_latch_elapsed_s > {float(baseline["post_latch_timeout_s"]):.9f}:
        return codex_r004_fault(epoch, ordinal, token, kind, consumed, 48, runtime_hi, runtime_lo)
      # Packet freshness bounds host dt below 80 ms; validate the 0.5 N/s
      # ramp against that same worst-case interval.
      elif setpoint < 1.0:
        return codex_r004_fault(epoch, ordinal, token, kind, consumed, 71, runtime_hi, runtime_lo)
      elif setpoint > {TARGET_FORCE_N:.1f}:
        return codex_r004_fault(epoch, ordinal, token, kind, consumed, 72, runtime_hi, runtime_lo)
      elif setpoint - prior_setpoint > 0.5 * {PACKET_STALE_S:.9f} + 0.010000000:
        return codex_r004_fault(epoch, ordinal, token, kind, consumed, 73, runtime_hi, runtime_lo)
      elif setpoint < prior_setpoint - 0.010000000:
        return codex_r004_fault(epoch, ordinal, token, kind, consumed, 74, runtime_hi, runtime_lo)
      elif baseline_mode == 3:
        baseline_done = True
      elif baseline_mode == 2:
        if baseline_successes < 3:
          return codex_r004_fault(epoch, ordinal, token, kind, consumed, 50, runtime_hi, runtime_lo)
        end
        baseline_done = True
      else:
        # Joint components are not Cartesian axes.  The host's canonical V4
        # calibrated Jacobian gate proves pure-normal baseline motion before
        # the single writer publishes this bounded six-joint command.
        speedj(baseline_qdot, {tp_acceleration_literal}, actual_dt)
        prior_setpoint = setpoint
        prior_baseline_successes = baseline_successes
        codex_r004_echo(epoch, ordinal, 21, token, 0, consumed, kind, 0, runtime_hi, runtime_lo)
      end
    end
    if baseline_done:
      break
    end
  end
  stopj({tp_acceleration_literal})
  if not codex_r004_stationary(0.250000000):
    return codex_r004_fault(epoch, ordinal, token, kind, consumed, 58, runtime_hi, runtime_lo)
  end
  if kind == 1 or baseline_mode == 3:
    return codex_r004_return_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_hi, runtime_lo, 0)
  end
  if read_input_integer_register(24) < 3 or read_input_integer_register(25) != 2:
    return codex_r004_fault(epoch, ordinal, token, kind, consumed, 50, runtime_hi, runtime_lo)
  end

  local path_elapsed_s = 0.0
  while path_elapsed_s < {path_runtime:.9f}:
    local actual_path_dt = get_steptime()
    if actual_path_dt <= 0.0 or actual_path_dt >= {PACKET_STALE_S:.9f}:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, 46, runtime_hi, runtime_lo)
    end
    packet_reason = codex_r004_packet_observe()
    guard = codex_r004_packet_guard(packet_reason, 60.0, 100.0, 3.0)
    if guard != 0:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, guard, runtime_hi, runtime_lo)
    elif read_input_integer_register(25) != 2 or read_input_integer_register(24) < 3:
      return codex_r004_fault(epoch, ordinal, token, kind, consumed, 50, runtime_hi, runtime_lo)
    end
    local path_qdot = [read_input_float_register(37), read_input_float_register(38), read_input_float_register(39), read_input_float_register(40), read_input_float_register(41), read_input_float_register(42)]
    speedj(path_qdot, {tp_acceleration_literal}, actual_path_dt)
    path_elapsed_s = path_elapsed_s + actual_path_dt
    codex_r004_echo(epoch, ordinal, 25, token, 0, consumed, kind, 0, runtime_hi, runtime_lo)
  end
  stopj({tp_acceleration_literal})
  if not codex_r004_stationary(0.250000000):
    return codex_r004_fault(epoch, ordinal, token, kind, consumed, 58, runtime_hi, runtime_lo)
  end
  return codex_r004_return_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_hi, runtime_lo, 0)
end

def {PROGRAM}():
  # Script1 owns the verified V4 EOAT setup.  This resident Script2 performs no EOAT writes.
  # Cartesian Home is fixed by the EOAT-bound profile; it is never captured at Play.
  local fixed_home_pose = p[{fixed_home_pose}]
  local locked_home_q = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local locked_home_q_valid = False
  local runtime_hi = {runtime_hi}
  local runtime_lo = {runtime_lo}
  local active_epoch = 0
  local last_failed_epoch = 0
  # Fence any ARM image left in RTDE input registers across a TP restart.  The
  # host must publish a strictly newer sequence after this Play.
  local consumed_session_sequence = read_input_integer_register(28)
  local current_ordinal = 0
  local current_token = 0
  local current_kind = 0
  local state = 78
  local reason = 0
  local return_guard = 0
  local session_active = False
  local completed = False
  while True:
    local packet_reason = codex_r004_packet_observe()
    local integer_reason = codex_r004_integer_wire_guard()
    local session_command = read_input_integer_register(27)
    local session_sequence = read_input_integer_register(28)
    local input_epoch = read_input_integer_register(29)
    local input_ordinal = read_input_integer_register(30)
    local input_kind = read_input_integer_register(31)
    local input_token = read_input_integer_register(32)
    if session_command == 3 and session_sequence > consumed_session_sequence:
      stopl(0.250000000)
      session_active = False
      completed = False
      last_failed_epoch = input_epoch
      active_epoch = input_epoch
      state = 90
      reason = 4
      return_guard = 0
    elif integer_reason != 0 and session_command != 0:
      stopl(0.250000000)
      session_active = False
      last_failed_epoch = input_epoch
      state = 90
      reason = integer_reason
      return_guard = 0
    elif not session_active and not completed and session_command == 1 and session_sequence > consumed_session_sequence and packet_reason == 0:
      if input_epoch <= last_failed_epoch or input_epoch <= 0 or input_ordinal < 1 or input_ordinal > 16 or input_kind < 1 or input_kind > 4 or input_token <= 0:
        state = 90
        reason = 61
        last_failed_epoch = input_epoch
      elif (input_ordinal <= 3 and input_kind != 1) or (input_ordinal >= 4 and input_ordinal <= 8 and input_kind != 2) or (input_ordinal >= 9 and input_ordinal <= 13 and input_kind != 3) or (input_ordinal >= 14 and input_kind != 4):
        state = 90
        reason = 68
        last_failed_epoch = input_epoch
      else:
        active_epoch = input_epoch
        current_ordinal = input_ordinal
        current_kind = input_kind
        current_token = input_token
        consumed_session_sequence = session_sequence
        # No Script2 auto-home is permitted.  A bad fixed-Home entry is a
        # terminal typed failure before ARM state or any contact motion.
        if not codex_r004_entry_home_verified(fixed_home_pose, locked_home_q, locked_home_q_valid):
          stopl(0.250000000)
          session_active = False
          state = 90
          reason = {ATTEMPT_ENTRY_NOT_CAPTURED_HOME}
          return_guard = 0
          last_failed_epoch = input_epoch
        else:
          # The fixed Cartesian pose gates this q read; it may establish the
          # current IK branch for this and subsequent rolling ARM attempts.
          local arm_home_q = get_actual_joint_positions()
          locked_home_q = arm_home_q
          locked_home_q_valid = True
          session_active = True
          state = 11
          reason = 0
          return_guard = 0
          codex_r004_echo(active_epoch, current_ordinal, state, current_token, reason, consumed_session_sequence, current_kind, return_guard, runtime_hi, runtime_lo)
          if not codex_r004_execute_attempt(fixed_home_pose, arm_home_q, active_epoch, current_ordinal, current_token, current_kind, consumed_session_sequence, runtime_hi, runtime_lo):
            session_active = False
            state = 90
            reason = codex_r004_attempt_reason
            return_guard = codex_r004_attempt_guard
            last_failed_epoch = active_epoch
          else:
            session_active = False
            state = 78
            reason = 0
            return_guard = codex_r004_attempt_guard
          end
        end
      end
    elif session_command == 2 and state == 78 and session_sequence > consumed_session_sequence and input_epoch == active_epoch and input_ordinal == 16:
      consumed_session_sequence = session_sequence
      completed = True
      state = 80
      reason = 0
      return_guard = 123
    elif session_command == 1 and (session_sequence <= consumed_session_sequence or input_epoch != active_epoch) and state == 78:
      reason = 62
    end
    codex_r004_echo(active_epoch, current_ordinal, state, current_token, reason, consumed_session_sequence, current_kind, return_guard, runtime_hi, runtime_lo)
    sync()
  end
end

{PROGRAM}()
"""


def render_script(contract_path=None) -> str:
    contract = load_contract() if contract_path is None else load_contract(contract_path)
    # Loading the local EOAT profile is a source-closure check; Script1 remains
    # the only program that applies it before an offline candidate is used.
    profile = load_new_eoat_profile()
    if profile.profile_sha256 != contract.eoat_sha256:
        raise ValueError("r004 TP EOAT binding differs from the contract")
    home_profile = load_fixed_home_profile(profile)
    return _script_body(contract, home_profile)


__all__ = ["CONTROLLER_DIR", "RUNTIME_PROTOCOL", "render_script"]
