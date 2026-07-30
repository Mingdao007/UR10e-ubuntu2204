"""Independent Kunwei-only 1 N force-search canary r006 primitive."""

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
DEFAULT_CONTRACT = ROOT / "config/step5d/force_search_canary_r006.json"
SCHEMA = "step5d.force-search-canary/contract-v2"
ARTIFACT_ID = "new-eoat-kunwei-1n-canary-r006"
PROGRAM_NAME = "step5d_force_search_canary_r006"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"


class CanaryR006Error(RuntimeError):
    """The r006 contract is invalid."""


@dataclass(frozen=True)
class CanaryR006Contract:
    sha256: str
    eoat_sha256: str
    search_speed_m_s: float
    search_acceleration_m_s2: float
    cycle_s: float
    max_travel_m: float
    runtime_limit_s: float
    search_stopl_acceleration_m_s2: float
    retract_distance_m: float
    retract_speed_m_s: float
    retract_acceleration_m_s2: float
    startup_increment_count: int
    heartbeat_gap_s: float
    startup_timeout_s: float
    contact_positive_normal_n: float
    contact_force_norm_n: float
    hard_abs_normal_n: float
    hard_force_norm_n: float
    hard_torque_norm_nm: float
    stationary_linear_m_s: float
    stationary_angular_rad_s: float
    stationary_dwell_s: float


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanaryR006Error(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise CanaryR006Error(f"{role} must be finite")
    return result


def _digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CanaryR006Error(f"{role} must be a lowercase SHA-256")
    return value


def load_contract(path: Path = DEFAULT_CONTRACT) -> CanaryR006Contract:
    if path.is_symlink() or not path.is_file():
        raise CanaryR006Error(f"r006 contract must be a regular file: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CanaryR006Error(f"r006 contract is unreadable: {exc}") from exc
    if document.get("schema") != SCHEMA or document.get("artifact_id") != ARTIFACT_ID:
        raise CanaryR006Error("r006 contract identity differs")
    eoat_binding = document.get("eoat_contract")
    motion = document.get("motion")
    startup = document.get("startup")
    contact = document.get("contact")
    stationary = document.get("stationary")
    if not all(
        isinstance(value, dict)
        for value in (eoat_binding, motion, startup, contact, stationary)
    ):
        raise CanaryR006Error("r006 contract sections are missing")
    eoat = load_new_eoat()
    eoat_sha = _digest(eoat_binding.get("sha256"), "EOAT binding")
    if (
        eoat_binding.get("path") != "config/step5d/new_eoat_calibration.json"
        or eoat_sha != eoat.artifact_sha256
    ):
        raise CanaryR006Error("r006 EOAT binding differs")
    if motion.get("frame") != "base" or motion.get(
        "axis"
    ) != "negative_z_search_positive_z_retract":
        raise CanaryR006Error("r006 must use base-Z only")
    increment_count = startup.get("heartbeat_increment_count")
    if not isinstance(increment_count, int) or isinstance(increment_count, bool):
        raise CanaryR006Error("startup increment count must be an integer")
    if startup.get("eoat_controller_get_ack_register") != 29:
        raise CanaryR006Error("EOAT readback acknowledgement register differs")
    result = CanaryR006Contract(
        sha256=_sha256(path),
        eoat_sha256=eoat_sha,
        search_speed_m_s=_finite(motion.get("search_speed_m_s"), "search speed"),
        search_acceleration_m_s2=_finite(
            motion.get("search_acceleration_m_s2"), "search acceleration"
        ),
        cycle_s=_finite(motion.get("cycle_s"), "cycle"),
        max_travel_m=_finite(motion.get("max_travel_m"), "maximum travel"),
        runtime_limit_s=_finite(motion.get("runtime_limit_s"), "runtime limit"),
        search_stopl_acceleration_m_s2=_finite(
            motion.get("search_stopl_acceleration_m_s2"), "search stopl acceleration"
        ),
        retract_distance_m=_finite(
            motion.get("retract_distance_m"), "retract distance"
        ),
        retract_speed_m_s=_finite(motion.get("retract_speed_m_s"), "retract speed"),
        retract_acceleration_m_s2=_finite(
            motion.get("retract_acceleration_m_s2"), "retract acceleration"
        ),
        startup_increment_count=increment_count,
        heartbeat_gap_s=_finite(
            startup.get("each_increment_max_gap_s"), "heartbeat maximum gap"
        ),
        startup_timeout_s=_finite(startup.get("startup_timeout_s"), "startup timeout"),
        contact_positive_normal_n=_finite(
            contact.get("positive_normal_load_n"), "contact normal load"
        ),
        contact_force_norm_n=_finite(
            contact.get("force_norm_n"), "contact force norm"
        ),
        hard_abs_normal_n=_finite(
            contact.get("hard_abs_normal_load_n"), "hard absolute normal"
        ),
        hard_force_norm_n=_finite(
            contact.get("hard_force_norm_n"), "hard force norm"
        ),
        hard_torque_norm_nm=_finite(
            contact.get("hard_torque_norm_nm"), "hard torque norm"
        ),
        stationary_linear_m_s=_finite(
            stationary.get("linear_speed_max_m_s"), "stationary linear speed"
        ),
        stationary_angular_rad_s=_finite(
            stationary.get("angular_speed_max_rad_s"), "stationary angular speed"
        ),
        stationary_dwell_s=_finite(
            stationary.get("continuous_dwell_s"), "stationary dwell"
        ),
    )
    exact = {
        "search_speed_m_s": 0.0005,
        "search_acceleration_m_s2": 0.01,
        "cycle_s": 0.008,
        "max_travel_m": 0.025,
        "runtime_limit_s": 90.0,
        "search_stopl_acceleration_m_s2": 0.01,
        "startup_increment_count": 3,
        "heartbeat_gap_s": 0.08,
        "contact_positive_normal_n": 0.8,
        "contact_force_norm_n": 1.0,
        "hard_abs_normal_n": 3.0,
        "hard_force_norm_n": 3.0,
        "hard_torque_norm_nm": 0.2,
        "stationary_dwell_s": 0.25,
    }
    mismatches = {
        name: {"expected": expected, "actual": getattr(result, name)}
        for name, expected in exact.items()
        if getattr(result, name) != expected
    }
    if mismatches:
        raise CanaryR006Error(f"r006 fixed invariants differ: {mismatches}")
    if result.runtime_limit_s * result.search_speed_m_s < result.max_travel_m:
        raise CanaryR006Error("runtime cannot cover maximum travel")
    if not (
        0.0 < result.contact_positive_normal_n < result.hard_abs_normal_n
        and 0.0 < result.contact_force_norm_n < result.hard_force_norm_n
    ):
        raise CanaryR006Error("contact and hard guards are not nested")
    return result


def stop_reason(
    contract: CanaryR006Contract,
    *,
    normal_load_n: float,
    force_norm_n: float,
    torque_norm_nm: float,
    sensor_fresh: bool,
    stop_requested: bool,
    heartbeat_gap_s: float,
    travel_m: float,
    elapsed_s: float,
) -> int:
    """r006 specialization of the shared force-search state machine."""
    engine = ForceSearchEngine(
        ForceSearchProfile(
            profile_id="r006_positive_normal",
            heartbeat_limit_s=contract.heartbeat_gap_s,
            heartbeat_limit_inclusive=True,
            max_travel_m=contract.max_travel_m,
            runtime_limit_s=contract.runtime_limit_s,
            contact_normal_n=contract.contact_positive_normal_n,
            contact_force_norm_n=contract.contact_force_norm_n,
            hard_abs_normal_n=contract.hard_abs_normal_n,
            hard_force_norm_n=contract.hard_force_norm_n,
            hard_torque_norm_nm=contract.hard_torque_norm_nm,
            contact_normal_mode=ContactNormalMode.POSITIVE_RAW,
            hard_guard_includes_delta=False,
        )
    )
    _, decision = engine.step(
        ForceSearchState(),
        ForceSearchObservation(
            normal_n=normal_load_n,
            force_norm_n=force_norm_n,
            torque_norm_nm=torque_norm_nm,
            sensor_fresh=sensor_fresh,
            stop_requested=stop_requested,
            heartbeat_gap_s=heartbeat_gap_s,
            travel_m=travel_m,
            elapsed_s=elapsed_s,
        ),
    )
    return decision.reason


def _f(value: float) -> str:
    return f"{value:.9f}"


def render_script(contract: CanaryR006Contract | None = None) -> str:
    selected = contract or load_contract()
    eoat_block = urscript_initialization_block()
    return f"""# ROLE: independent Kunwei register-live-writer 1 N canary
# TP_PROGRAM_ID: {PROGRAM_NAME}
# FORCE_SEARCH_CONTRACT_SHA256: {selected.sha256}
# EOAT_CONTRACT_SHA256: {selected.eoat_sha256}
# BOUNDARY: base-Z only; no Home/XY/angular/force_mode/UR built-in wrench

global codex_canary_monitor_active = False
global codex_canary_monitor_fault = 0.0
global codex_canary_last_heartbeat = 0.0
global codex_canary_heartbeat_gap_s = 0.0

def codex_abs(value):
  if value < 0.0:
    return -value
  end
  return value
end

def codex_finite(value, limit):
  return value == value and value <= limit and value >= -limit
end

def codex_echo(stage, reason, travel_m):
  write_output_float_register(24, read_input_float_register(24))
  write_output_float_register(25, read_input_float_register(25))
  write_output_float_register(26, read_input_float_register(26))
  write_output_float_register(27, read_input_float_register(27))
  write_output_float_register(28, read_input_float_register(28))
  write_output_float_register(29, read_input_float_register(29))
  write_output_float_register(30, reason)
  write_output_float_register(32, travel_m)
  write_output_float_register(35, stage)
end

def codex_monitor_reason():
  local normal_load = read_input_float_register(24)
  local force_norm = read_input_float_register(25)
  local heartbeat = read_input_float_register(26)
  local sensor_fresh = read_input_float_register(27)
  local stop_request = read_input_float_register(28)
  local torque_norm = read_input_float_register(30)
  if not codex_finite(normal_load, 1000.0) or not codex_finite(force_norm, 1000.0) or not codex_finite(heartbeat, 1000000000000.0) or not codex_finite(sensor_fresh, 10.0) or not codex_finite(stop_request, 10.0) or not codex_finite(torque_norm, 100.0):
    return 3.0
  elif codex_canary_heartbeat_gap_s >= {_f(selected.heartbeat_gap_s)}:
    return 2.0
  elif sensor_fresh < 0.5:
    return 3.0
  elif stop_request > 0.5:
    return 4.0
  elif codex_abs(normal_load) >= {_f(selected.hard_abs_normal_n)}:
    return 5.0
  elif force_norm >= {_f(selected.hard_force_norm_n)}:
    return 6.0
  elif torque_norm >= {_f(selected.hard_torque_norm_nm)}:
    return 7.0
  end
  return 0.0
end

thread codex_deceleration_and_retract_monitor():
  while codex_canary_monitor_active:
    local heartbeat = read_input_float_register(26)
    if not codex_finite(heartbeat, 1000000000000.0):
      codex_canary_monitor_fault = 3.0
    elif heartbeat == codex_canary_last_heartbeat:
      codex_canary_heartbeat_gap_s = codex_canary_heartbeat_gap_s + get_steptime()
    else:
      codex_canary_last_heartbeat = heartbeat
      codex_canary_heartbeat_gap_s = 0.0
    end
    local monitor_reason = codex_monitor_reason()
    if monitor_reason != 0.0:
      codex_canary_monitor_fault = monitor_reason
    end
    sync()
  end
end

def {PROGRAM_NAME}():
{eoat_block}
  local last_startup_heartbeat = read_input_float_register(26)
  local startup_increment_count = 0
  local startup_gap_s = 0.0
  local startup_elapsed_s = 0.0
  while startup_increment_count < {selected.startup_increment_count} and startup_elapsed_s < {_f(selected.startup_timeout_s)}:
    local heartbeat = read_input_float_register(26)
    local sensor_fresh = read_input_float_register(27)
    local eoat_get_ack = read_input_float_register(29)
    codex_echo(10.0, 0.0, 0.0)
    if not codex_finite(heartbeat, 1000000000000.0) or not codex_finite(sensor_fresh, 10.0) or not codex_finite(eoat_get_ack, 10.0):
      startup_increment_count = 0
      startup_gap_s = 0.0
    elif sensor_fresh < 0.5 or eoat_get_ack < 0.5:
      startup_increment_count = 0
      startup_gap_s = 0.0
    elif heartbeat != last_startup_heartbeat:
      if startup_gap_s < {_f(selected.heartbeat_gap_s)}:
        startup_increment_count = startup_increment_count + 1
      else:
        startup_increment_count = 1
      end
      last_startup_heartbeat = heartbeat
      startup_gap_s = 0.0
    else:
      startup_gap_s = startup_gap_s + get_steptime()
      if startup_gap_s >= {_f(selected.heartbeat_gap_s)}:
        startup_increment_count = 0
      end
    end
    sync()
    startup_elapsed_s = startup_elapsed_s + get_steptime()
  end
  if startup_increment_count < {selected.startup_increment_count}:
    codex_echo(90.0, 2.0, 0.0)
    halt
  end

  local start_pose = get_actual_tcp_pose()
  local start_speed = get_actual_tcp_speed()
  if not codex_finite(start_pose[2], 10.0) or not codex_finite(start_speed[0], 10.0) or not codex_finite(start_speed[1], 10.0) or not codex_finite(start_speed[2], 10.0) or not codex_finite(start_speed[3], 10.0) or not codex_finite(start_speed[4], 10.0) or not codex_finite(start_speed[5], 10.0):
    codex_echo(90.0, 3.0, 0.0)
    halt
  end
  local entry_linear_speed = sqrt(start_speed[0] * start_speed[0] + start_speed[1] * start_speed[1] + start_speed[2] * start_speed[2])
  local entry_angular_speed = sqrt(start_speed[3] * start_speed[3] + start_speed[4] * start_speed[4] + start_speed[5] * start_speed[5])
  if entry_linear_speed > {_f(selected.stationary_linear_m_s)} or entry_angular_speed > {_f(selected.stationary_angular_rad_s)}:
    codex_echo(90.0, 9.0, 0.0)
    halt
  end

  codex_canary_last_heartbeat = read_input_float_register(26)
  codex_canary_heartbeat_gap_s = 0.0
  codex_canary_monitor_fault = 0.0
  codex_canary_monitor_active = True
  local monitor_handle = run codex_deceleration_and_retract_monitor()
  local reason = 0.0
  local travel_m = 0.0
  local elapsed_s = 0.0
  while reason == 0.0:
    local normal_load = read_input_float_register(24)
    local force_norm = read_input_float_register(25)
    local torque_norm = read_input_float_register(30)
    local pose_now = get_actual_tcp_pose()
    travel_m = start_pose[2] - pose_now[2]
    reason = codex_monitor_reason()
    if codex_canary_monitor_fault != 0.0:
      reason = codex_canary_monitor_fault
    elif reason == 0.0 and normal_load >= {_f(selected.contact_positive_normal_n)}:
      reason = 11.0
    elif reason == 0.0 and force_norm >= {_f(selected.contact_force_norm_n)}:
      reason = 11.0
    elif reason == 0.0 and travel_m >= {_f(selected.max_travel_m)}:
      reason = 8.0
    elif reason == 0.0 and elapsed_s >= {_f(selected.runtime_limit_s)}:
      reason = 10.0
    end
    codex_echo(11.0, reason, travel_m)
    if reason == 0.0:
      speedl([0.0, 0.0, -{_f(selected.search_speed_m_s)}, 0.0, 0.0, 0.0], {_f(selected.search_acceleration_m_s2)}, {_f(selected.cycle_s)})
      elapsed_s = elapsed_s + {_f(selected.cycle_s)}
    end
  end

  stopl({_f(selected.search_stopl_acceleration_m_s2)})
  codex_echo(12.0, reason, travel_m)
  if codex_canary_monitor_fault != 0.0:
    reason = codex_canary_monitor_fault
  end

  local stationary_dwell_s = 0.0
  while reason == 11.0 and stationary_dwell_s < {_f(selected.stationary_dwell_s)}:
    local speed_now = get_actual_tcp_speed()
    local linear_now = sqrt(speed_now[0] * speed_now[0] + speed_now[1] * speed_now[1] + speed_now[2] * speed_now[2])
    local angular_now = sqrt(speed_now[3] * speed_now[3] + speed_now[4] * speed_now[4] + speed_now[5] * speed_now[5])
    if codex_canary_monitor_fault != 0.0:
      reason = codex_canary_monitor_fault
    elif linear_now <= {_f(selected.stationary_linear_m_s)} and angular_now <= {_f(selected.stationary_angular_rad_s)}:
      stationary_dwell_s = stationary_dwell_s + get_steptime()
    else:
      stationary_dwell_s = 0.0
    end
    codex_echo(13.0, reason, travel_m)
    sync()
  end

  if reason == 11.0:
    local retract_start_z = get_actual_tcp_pose()[2]
    local retract_travel_m = 0.0
    while reason == 11.0 and retract_travel_m < {_f(selected.retract_distance_m)}:
      if codex_canary_monitor_fault != 0.0:
        reason = codex_canary_monitor_fault
      else:
        speedl([0.0, 0.0, {_f(selected.retract_speed_m_s)}, 0.0, 0.0, 0.0], {_f(selected.retract_acceleration_m_s2)}, {_f(selected.cycle_s)})
        retract_travel_m = get_actual_tcp_pose()[2] - retract_start_z
      end
      codex_echo(14.0, reason, travel_m)
    end
    stopl({_f(selected.search_stopl_acceleration_m_s2)})
  end

  codex_canary_monitor_active = False
  kill monitor_handle
  if reason == 11.0:
    codex_echo(15.0, reason, travel_m)
  else:
    codex_echo(90.0, reason, travel_m)
  end
  textmsg("{PROGRAM_NAME} terminal reason:", reason)
  halt
end

{PROGRAM_NAME}()
"""


__all__ = [
    "ARTIFACT_ID",
    "CONTROLLER_DIR",
    "DEFAULT_CONTRACT",
    "PROGRAM_NAME",
    "CanaryR006Contract",
    "CanaryR006Error",
    "load_contract",
    "render_script",
    "stop_reason",
]
