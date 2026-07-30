"""Bounded new-EOAT force-search primitive and URScript renderer."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from step5d_force_search_core import (
    ContactNormalMode,
    ForceSearchEngine,
    ForceSearchObservation,
    ForceSearchProfile,
    ForceSearchState,
)
from step5d_new_eoat import load_new_eoat, urscript_initialization_block


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACT = ROOT / "config/step5d/force_search_canary_v1.json"
SCHEMA = "step5d.force-search-primitive/v1"
ARTIFACT_ID = "new-eoat-force-search-canary-20260730"
PROGRAM_NAME = "step5d_force_search_canary_r005"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"


class ForceSearchContractError(RuntimeError):
    """The force-search contract is invalid."""


@dataclass(frozen=True)
class ForceSearchContract:
    artifact_id: str
    artifact_sha256: str
    search_speed_m_s: float
    search_acceleration_m_s2: float
    cycle_s: float
    max_travel_m: float
    runtime_limit_s: float
    retract_distance_m: float
    retract_speed_m_s: float
    retract_acceleration_m_s2: float
    tool_z_down_cos_min: float
    initial_linear_speed_max_m_s: float
    initial_angular_speed_max_rad_s: float
    initial_joint_speed_max_rad_s: float
    initial_abs_normal_force_max_n: float
    initial_force_norm_max_n: float
    initial_torque_norm_max_nm: float
    sensor_fresh_timeout_s: float
    heartbeat_stale_s: float
    contact_abs_normal_force_n: float
    contact_force_norm_n: float
    hard_abs_normal_force_n: float
    hard_force_norm_n: float
    hard_torque_norm_nm: float


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ForceSearchContractError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ForceSearchContractError(f"{name} must be finite")
    return result


def load_contract(path: Path = DEFAULT_CONTRACT) -> ForceSearchContract:
    if path.is_symlink() or not path.is_file():
        raise ForceSearchContractError(f"contract must be a regular file: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ForceSearchContractError(f"contract is unreadable: {exc}") from exc
    if document.get("schema") != SCHEMA or document.get("artifact_id") != ARTIFACT_ID:
        raise ForceSearchContractError("contract identity differs")
    if document.get("eoat_contract") != "config/step5d/new_eoat_calibration.json":
        raise ForceSearchContractError("EOAT contract binding differs")
    motion = document.get("motion")
    entry = document.get("entry_gates")
    force = document.get("force_stop")
    if not isinstance(motion, dict) or not isinstance(entry, dict) or not isinstance(force, dict):
        raise ForceSearchContractError("contract sections are missing")
    if motion.get("frame") != "base" or motion.get("axis") != "z":
        raise ForceSearchContractError("the canary must use base-Z only")

    result = ForceSearchContract(
        artifact_id=document["artifact_id"],
        artifact_sha256=_sha256(path),
        search_speed_m_s=_finite(motion.get("search_speed_m_s"), "search speed"),
        search_acceleration_m_s2=_finite(
            motion.get("search_acceleration_m_s2"), "search acceleration"
        ),
        cycle_s=_finite(motion.get("cycle_s"), "cycle"),
        max_travel_m=_finite(motion.get("max_travel_m"), "maximum travel"),
        runtime_limit_s=_finite(motion.get("runtime_limit_s"), "runtime limit"),
        retract_distance_m=_finite(
            motion.get("retract_distance_m"), "retract distance"
        ),
        retract_speed_m_s=_finite(motion.get("retract_speed_m_s"), "retract speed"),
        retract_acceleration_m_s2=_finite(
            motion.get("retract_acceleration_m_s2"), "retract acceleration"
        ),
        tool_z_down_cos_min=_finite(
            entry.get("tool_z_down_cos_min"), "tool-Z down cosine"
        ),
        initial_linear_speed_max_m_s=_finite(
            entry.get("initial_linear_speed_max_m_s"), "initial linear speed"
        ),
        initial_angular_speed_max_rad_s=_finite(
            entry.get("initial_angular_speed_max_rad_s"), "initial angular speed"
        ),
        initial_joint_speed_max_rad_s=_finite(
            entry.get("initial_joint_speed_max_rad_s"), "initial joint speed"
        ),
        initial_abs_normal_force_max_n=_finite(
            entry.get("initial_abs_normal_force_max_n"), "initial normal force"
        ),
        initial_force_norm_max_n=_finite(
            entry.get("initial_force_norm_max_n"), "initial force norm"
        ),
        initial_torque_norm_max_nm=_finite(
            entry.get("initial_torque_norm_max_nm"), "initial torque norm"
        ),
        sensor_fresh_timeout_s=_finite(
            entry.get("sensor_fresh_timeout_s"), "sensor-fresh timeout"
        ),
        heartbeat_stale_s=_finite(
            entry.get("heartbeat_stale_s"), "heartbeat stale limit"
        ),
        contact_abs_normal_force_n=_finite(
            force.get("contact_abs_normal_force_n"), "contact normal force"
        ),
        contact_force_norm_n=_finite(
            force.get("contact_force_norm_n"), "contact force norm"
        ),
        hard_abs_normal_force_n=_finite(
            force.get("hard_abs_normal_force_n"), "hard normal force"
        ),
        hard_force_norm_n=_finite(
            force.get("hard_force_norm_n"), "hard force norm"
        ),
        hard_torque_norm_nm=_finite(
            force.get("hard_torque_norm_nm"), "hard torque norm"
        ),
    )
    if not -0.001 <= result.search_speed_m_s < 0.0:
        raise ForceSearchContractError("search speed must be downward and at most 1 mm/s")
    if not 0.0 < result.search_acceleration_m_s2 <= 0.02:
        raise ForceSearchContractError("search acceleration is outside the canary bound")
    if not 0.004 <= result.cycle_s <= 0.02:
        raise ForceSearchContractError("cycle is outside the bounded bridge cadence")
    if not 0.0 < result.max_travel_m <= 0.025:
        raise ForceSearchContractError("maximum travel is outside the canary bound")
    if not 0.0 < result.retract_distance_m <= 0.005:
        raise ForceSearchContractError("retract distance is outside the canary bound")
    if not 0.0 < result.retract_speed_m_s <= 0.001:
        raise ForceSearchContractError("retract speed is outside the canary bound")
    if not 0.98 <= result.tool_z_down_cos_min <= 1.0:
        raise ForceSearchContractError("verticality gate is outside the canary bound")
    if not (
        0.0 < result.contact_abs_normal_force_n < result.hard_abs_normal_force_n <= 3.0
        and 0.0 < result.contact_force_norm_n < result.hard_force_norm_n <= 3.0
        and 0.0 < result.hard_torque_norm_nm <= 0.2
    ):
        raise ForceSearchContractError("force thresholds are not strictly nested")
    if result.runtime_limit_s * abs(result.search_speed_m_s) < result.max_travel_m:
        raise ForceSearchContractError("runtime cannot cover the bounded search travel")
    if result.heartbeat_stale_s < 2.0 * result.cycle_s:
        raise ForceSearchContractError("heartbeat stale limit is too short")
    load_new_eoat()
    return result


def stop_reason(
    contract: ForceSearchContract,
    *,
    normal_force_n: float,
    force_norm_n: float,
    torque_norm_nm: float,
    delta_normal_force_n: float,
    delta_force_norm_n: float,
    delta_torque_norm_nm: float,
    sensor_ok: bool,
    stop_requested: bool,
    heartbeat_stale_s: float,
    travel_m: float,
    elapsed_s: float,
) -> int:
    """r005 specialization of the shared force-search state machine."""
    engine = ForceSearchEngine(
        ForceSearchProfile(
            profile_id="r005_entry_relative",
            heartbeat_limit_s=contract.heartbeat_stale_s,
            heartbeat_limit_inclusive=False,
            max_travel_m=contract.max_travel_m,
            runtime_limit_s=contract.runtime_limit_s,
            contact_normal_n=contract.contact_abs_normal_force_n,
            contact_force_norm_n=contract.contact_force_norm_n,
            hard_abs_normal_n=contract.hard_abs_normal_force_n,
            hard_force_norm_n=contract.hard_force_norm_n,
            hard_torque_norm_nm=contract.hard_torque_norm_nm,
            contact_normal_mode=ContactNormalMode.ABS_DELTA,
            hard_guard_includes_delta=True,
        )
    )
    _, decision = engine.step(
        ForceSearchState(),
        ForceSearchObservation(
            normal_n=normal_force_n,
            force_norm_n=force_norm_n,
            torque_norm_nm=torque_norm_nm,
            delta_normal_n=delta_normal_force_n,
            delta_force_norm_n=delta_force_norm_n,
            delta_torque_norm_nm=delta_torque_norm_nm,
            sensor_fresh=sensor_ok,
            stop_requested=stop_requested,
            heartbeat_gap_s=heartbeat_stale_s,
            travel_m=travel_m,
            elapsed_s=elapsed_s,
        ),
    )
    return decision.reason


def _f(value: float) -> str:
    return f"{value:.9f}"


def render_script(contract: ForceSearchContract | None = None) -> str:
    selected = contract or load_contract()
    eoat_block = urscript_initialization_block()
    return f"""# ROLE: standalone low-speed force-search primitive
# TP_PROGRAM_ID: {PROGRAM_NAME}
# FORCE_SEARCH_CONTRACT_ID: {selected.artifact_id}
# FORCE_SEARCH_CONTRACT_SHA256: {selected.artifact_sha256}
# BOUNDARY: no absolute XY/Home, no lateral/angular command, no force_mode, no optimizer

def codex_abs(value):
  if value < 0.0:
    return -value
  end
  return value
end

def codex_finite_value(value, absolute_limit):
  if value != value:
    return False
  end
  if value > absolute_limit or value < -absolute_limit:
    return False
  end
  return True
end

def codex_finite_vector(value, size, absolute_limit):
  local index = 0
  while index < size:
    if not codex_finite_value(value[index], absolute_limit):
      return False
    end
    index = index + 1
  end
  return True
end

def codex_echo_force_search(stage, stop_reason, travel_m, search_speed_m_s, force_delta_norm_n):
  write_output_float_register(24, read_input_float_register(24))
  write_output_float_register(25, read_input_float_register(25))
  write_output_float_register(26, read_input_float_register(26))
  write_output_float_register(27, read_input_float_register(27))
  write_output_float_register(28, read_input_float_register(28))
  write_output_float_register(29, force_delta_norm_n)
  write_output_float_register(30, stop_reason)
  write_output_float_register(31, read_input_float_register(30))
  write_output_float_register(32, travel_m)
  write_output_float_register(33, search_speed_m_s)
  write_output_float_register(35, stage)
end

def {PROGRAM_NAME}():
{eoat_block}
  local search_speed_m_s = {_f(selected.search_speed_m_s)}
  local search_accel_m_s2 = {_f(selected.search_acceleration_m_s2)}
  local cycle_s = {_f(selected.cycle_s)}
  local max_travel_m = {_f(selected.max_travel_m)}
  local runtime_limit_s = {_f(selected.runtime_limit_s)}
  local stale_limit_s = {_f(selected.heartbeat_stale_s)}
  local stop_reason = 0.0
  local initial_heartbeat = read_input_float_register(26)
  local last_wait_heartbeat = initial_heartbeat
  local fresh_heartbeat_count = 0
  local fresh_sensor = False
  local wait_s = 0.0

  while wait_s < {_f(selected.sensor_fresh_timeout_s)} and not fresh_sensor:
    local heartbeat_wait = read_input_float_register(26)
    local sensor_ok_wait = read_input_float_register(27)
    codex_echo_force_search(10.0, 0.0, 0.0, 0.0, 0.0)
    if codex_finite_value(heartbeat_wait, 1000000000000.0) and codex_finite_value(sensor_ok_wait, 10.0) and sensor_ok_wait > 0.5:
      if heartbeat_wait != last_wait_heartbeat:
        fresh_heartbeat_count = fresh_heartbeat_count + 1
        last_wait_heartbeat = heartbeat_wait
      end
      if fresh_heartbeat_count >= 3:
        fresh_sensor = True
      end
    else:
      fresh_heartbeat_count = 0
    end
    sync()
    wait_s = wait_s + get_steptime()
  end
  if not fresh_sensor:
    textmsg("force_search_canary_r005: fresh zeroed sensor unavailable")
    halt
  end

  local start_pose = get_actual_tcp_pose()
  local start_speed = get_actual_tcp_speed()
  local start_joint_speed = get_actual_joint_speeds()
  if not codex_finite_vector(start_pose, 6, 100.0) or not codex_finite_vector(start_speed, 6, 100.0) or not codex_finite_vector(start_joint_speed, 6, 100.0):
    textmsg("force_search_canary_r005: nonfinite robot entry state")
    halt
  end
  local linear_speed = sqrt(start_speed[0] * start_speed[0] + start_speed[1] * start_speed[1] + start_speed[2] * start_speed[2])
  local angular_speed = sqrt(start_speed[3] * start_speed[3] + start_speed[4] * start_speed[4] + start_speed[5] * start_speed[5])
  local max_joint_speed = 0.0
  local joint_index = 0
  while joint_index < 6:
    local joint_speed_abs = codex_abs(start_joint_speed[joint_index])
    if joint_speed_abs > max_joint_speed:
      max_joint_speed = joint_speed_abs
    end
    joint_index = joint_index + 1
  end
  if linear_speed > {_f(selected.initial_linear_speed_max_m_s)} or angular_speed > {_f(selected.initial_angular_speed_max_rad_s)} or max_joint_speed > {_f(selected.initial_joint_speed_max_rad_s)}:
    textmsg("force_search_canary_r005: nonstationary entry")
    halt
  end

  local tool_z_point = pose_trans(start_pose, p[0, 0, 1, 0, 0, 0])
  local tool_z_dx = tool_z_point[0] - start_pose[0]
  local tool_z_dy = tool_z_point[1] - start_pose[1]
  local tool_z_dz = tool_z_point[2] - start_pose[2]
  local tool_z_norm = sqrt(tool_z_dx * tool_z_dx + tool_z_dy * tool_z_dy + tool_z_dz * tool_z_dz)
  if not codex_finite_value(tool_z_norm, 2.0) or tool_z_norm < 0.999 or tool_z_norm > 1.001:
    textmsg("force_search_canary_r005: invalid tool-Z axis")
    halt
  end
  local tool_z_down_cos = -tool_z_dz / tool_z_norm
  if not codex_finite_value(tool_z_down_cos, 1.0) or tool_z_down_cos < {_f(selected.tool_z_down_cos_min)}:
    textmsg("force_search_canary_r005: tool Z is not vertical-down")
    halt
  end

  local initial_normal = read_input_float_register(24)
  local initial_force_norm = read_input_float_register(25)
  local initial_torque_norm = read_input_float_register(30)
  local initial_fx = read_input_float_register(31)
  local initial_fy = read_input_float_register(32)
  local initial_fz = read_input_float_register(33)
  local initial_mx = read_input_float_register(34)
  local initial_my = read_input_float_register(35)
  local initial_mz = read_input_float_register(36)
  if not codex_finite_value(initial_normal, 1000.0) or not codex_finite_value(initial_force_norm, 1000.0) or not codex_finite_value(initial_torque_norm, 100.0) or not codex_finite_value(initial_fx, 1000.0) or not codex_finite_value(initial_fy, 1000.0) or not codex_finite_value(initial_fz, 1000.0) or not codex_finite_value(initial_mx, 100.0) or not codex_finite_value(initial_my, 100.0) or not codex_finite_value(initial_mz, 100.0):
    textmsg("force_search_canary_r005: nonfinite Kunwei entry wrench")
    halt
  end
  if codex_abs(initial_normal) > {_f(selected.initial_abs_normal_force_max_n)} or initial_force_norm > {_f(selected.initial_force_norm_max_n)} or initial_torque_norm > {_f(selected.initial_torque_norm_max_nm)}:
    textmsg("force_search_canary_r005: Kunwei entry wrench is not zeroed")
    halt
  end

  local last_heartbeat = read_input_float_register(26)
  local heartbeat_stale_s = 0.0
  local elapsed_s = 0.0
  local travel_m = 0.0
  codex_echo_force_search(11.0, 0.0, 0.0, search_speed_m_s, 0.0)
  while stop_reason == 0.0:
    local normal_force = read_input_float_register(24)
    local force_norm = read_input_float_register(25)
    local heartbeat = read_input_float_register(26)
    local sensor_ok = read_input_float_register(27)
    local stop_request = read_input_float_register(28)
    local torque_norm = read_input_float_register(30)
    local fx = read_input_float_register(31)
    local fy = read_input_float_register(32)
    local fz = read_input_float_register(33)
    local mx = read_input_float_register(34)
    local my = read_input_float_register(35)
    local mz = read_input_float_register(36)
    local delta_normal = normal_force - initial_normal
    local delta_fx = fx - initial_fx
    local delta_fy = fy - initial_fy
    local delta_fz = fz - initial_fz
    local delta_mx = mx - initial_mx
    local delta_my = my - initial_my
    local delta_mz = mz - initial_mz
    local delta_force_norm = sqrt(delta_fx * delta_fx + delta_fy * delta_fy + delta_fz * delta_fz)
    local delta_torque_norm = sqrt(delta_mx * delta_mx + delta_my * delta_my + delta_mz * delta_mz)
    local pose_now = get_actual_tcp_pose()
    travel_m = start_pose[2] - pose_now[2]
    if codex_finite_value(heartbeat, 1000000000000.0) and heartbeat == last_heartbeat:
      heartbeat_stale_s = heartbeat_stale_s + cycle_s
    elif codex_finite_value(heartbeat, 1000000000000.0):
      heartbeat_stale_s = 0.0
      last_heartbeat = heartbeat
    end
    codex_echo_force_search(11.0, stop_reason, travel_m, search_speed_m_s, delta_force_norm)
    if not codex_finite_value(normal_force, 1000.0) or not codex_finite_value(force_norm, 1000.0) or not codex_finite_value(heartbeat, 1000000000000.0) or not codex_finite_value(sensor_ok, 10.0) or not codex_finite_value(stop_request, 10.0) or not codex_finite_value(torque_norm, 100.0) or not codex_finite_value(delta_normal, 1000.0) or not codex_finite_value(delta_force_norm, 1000.0) or not codex_finite_value(delta_torque_norm, 100.0) or not codex_finite_vector(pose_now, 6, 100.0) or not codex_finite_value(travel_m, 100.0):
      stop_reason = 3.0
    elif heartbeat_stale_s > stale_limit_s:
      stop_reason = 2.0
    elif sensor_ok < 0.5:
      stop_reason = 3.0
    elif stop_request > 0.5:
      stop_reason = 4.0
    elif codex_abs(normal_force) >= {_f(selected.hard_abs_normal_force_n)} or codex_abs(delta_normal) >= {_f(selected.hard_abs_normal_force_n)}:
      stop_reason = 5.0
    elif force_norm >= {_f(selected.hard_force_norm_n)} or delta_force_norm >= {_f(selected.hard_force_norm_n)}:
      stop_reason = 6.0
    elif torque_norm >= {_f(selected.hard_torque_norm_nm)} or delta_torque_norm >= {_f(selected.hard_torque_norm_nm)}:
      stop_reason = 7.0
    elif codex_abs(delta_normal) >= {_f(selected.contact_abs_normal_force_n)} or delta_force_norm >= {_f(selected.contact_force_norm_n)}:
      stop_reason = 11.0
    elif travel_m >= max_travel_m:
      stop_reason = 8.0
    elif elapsed_s >= runtime_limit_s:
      stop_reason = 10.0
    else:
      speedl([0.0, 0.0, search_speed_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, cycle_s)
      elapsed_s = elapsed_s + cycle_s
    end
  end
  stopl(0.5)
  codex_echo_force_search(12.0, stop_reason, travel_m, 0.0, 0.0)

  if stop_reason == 11.0:
    local contact_pose = get_actual_tcp_pose()
    local retract_pose = p[contact_pose[0], contact_pose[1], contact_pose[2] + {_f(selected.retract_distance_m)}, contact_pose[3], contact_pose[4], contact_pose[5]]
    movel(retract_pose, a={_f(selected.retract_acceleration_m_s2)}, v={_f(selected.retract_speed_m_s)}, r=0.0)
    stopl(0.5)
    codex_echo_force_search(13.0, stop_reason, travel_m, 0.0, 0.0)
  end
  textmsg("force_search_canary_r005 stop reason:", stop_reason)
  halt
end

{PROGRAM_NAME}()
"""


__all__ = [
    "ARTIFACT_ID",
    "CONTROLLER_DIR",
    "DEFAULT_CONTRACT",
    "ForceSearchContract",
    "ForceSearchContractError",
    "PROGRAM_NAME",
    "load_contract",
    "render_script",
    "stop_reason",
]
