#!/usr/bin/env python3
"""Generate Step4e line outer-loop TP packages."""

from __future__ import annotations

import argparse
import gzip
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
PROGRAM_DIR = EXPERIMENT_ROOT / "programs"
CONFIG_PATH = EXPERIMENT_ROOT / "config" / "straight_line_reference.json"
TEMPLATE_URP = PROGRAM_DIR / "step4d_circle_detsearch_attitude_v1.urp"
CONTROLLER_DIR = "/programs/andyl/kunwei/step4"

def program_specs(version: str) -> dict[str, dict[str, str]]:
    specs = {
        "hold": {
            "name": f"step4e_contact_hold_line_{version}",
            "suffix": f"CONTACT_HOLD_LINE_{version.upper()}",
            "description": "deterministic search, contact latch, force/orientation hold",
        },
        "line": {
            "name": f"step4e_line_outerloop_{version}",
            "suffix": f"LINE_OUTERLOOP_{version.upper()}",
            "description": "deterministic search, contact latch, Step4e line outer-loop",
        },
    }
    if version in {"v1", "v2"}:
        return {
            "preview": {
                "name": f"step4e_preview_line_{version}",
                "suffix": f"PREVIEW_LINE_{version.upper()}",
                "description": "no-motion RTDE/command preview",
            },
            **specs,
        }
    return specs


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fmt(value: float) -> str:
    return f"{value:.9f}".rstrip("0").rstrip(".")


def source_stamp(suffix: str, now: datetime) -> str:
    return now.strftime(f"%Y-%m-%dT%H%MHKT_STEP4E_{suffix}")


def generated_at(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def line_cfg(config: dict) -> dict:
    cfg = config["step4e_line"]
    sx, sy, _sz, srx, sry, srz = [float(v) for v in cfg["start_tcp_pose_m_rad"]]
    ex, ey, _ez, _erx, _ery, _erz = [float(v) for v in cfg["end_tcp_pose_m_rad"]]
    return {
        "start_x": sx,
        "start_y": sy,
        "end_x": ex,
        "end_y": ey,
        "ref_rx": srx,
        "ref_ry": sry,
        "ref_rz": srz,
        "ux": float(cfg["xy_unit_vector"][0]),
        "uy": float(cfg["xy_unit_vector"][1]),
        "length": float(cfg["xy_length_m"]),
    }


COMMON_FUNCTIONS = r"""
def codex_abs(x):
  if x < 0.0:
    return -x
  end
  return x
end

def codex_clamp(x, lo, hi):
  if x < lo:
    return lo
  elif x > hi:
    return hi
  end
  return x
end

def codex_wait_for_fresh_heartbeat(timeout_s):
  local dt = 0.002
  local t = 0.0
  local initial_heartbeat = read_input_float_register(26)
  local current_heartbeat = initial_heartbeat
  while t < timeout_s:
    current_heartbeat = read_input_float_register(26)
    write_output_float_register(26, current_heartbeat)
    if read_input_float_register(27) > 0.5 and current_heartbeat != initial_heartbeat:
      return True
    end
    sync()
    t = t + dt
  end
  return False
end

def codex_wait_for_rezero_complete(timeout_s):
  local dt = 0.002
  local t = 0.0
  local initial_heartbeat = read_input_float_register(26)
  local current_heartbeat = initial_heartbeat
  local saw_sensor_not_ready = False
  while t < timeout_s:
    current_heartbeat = read_input_float_register(26)
    write_output_float_register(26, current_heartbeat)
    if read_input_float_register(27) < 0.5:
      saw_sensor_not_ready = True
    elif saw_sensor_not_ready and read_input_float_register(27) > 0.5 and current_heartbeat != initial_heartbeat:
      return True
    end
    sync()
    t = t + dt
  end
  return False
end

def codex_step4e_guard_stop_reason():
  local normal_force = read_input_float_register(24)
  local force_norm = read_input_float_register(25)
  local sensor_ok = read_input_float_register(27)
  local stop_request = read_input_float_register(28)
  local torque_norm = read_input_float_register(30)
  if sensor_ok < 0.5:
    return 3.0
  elif stop_request > 0.5:
    return 4.0
  elif codex_abs(normal_force) > 20.0:
    return 5.0
  elif force_norm > 50.0:
    return 6.0
  elif torque_norm > 0.6:
    return 7.0
  end
  return 0.0
end

def codex_should_auto_home(stop_reason):
  if stop_reason == 1.0:
    return True
  elif stop_reason == 2.0:
    return True
  elif stop_reason == 4.0:
    return True
  elif stop_reason == 8.0:
    return True
  elif stop_reason == 9.0:
    return True
  elif stop_reason == 10.0:
    return True
  elif stop_reason == 12.0:
    return True
  elif stop_reason == 13.0:
    return True
  elif stop_reason == 14.0:
    return True
  end
  return False
end

def codex_echo_basic(stop_reason):
  write_output_float_register(24, read_input_float_register(24))
  write_output_float_register(25, read_input_float_register(25))
  write_output_float_register(26, read_input_float_register(26))
  write_output_float_register(27, read_input_float_register(27))
  write_output_float_register(28, read_input_float_register(28))
  write_output_float_register(29, read_input_float_register(29))
  write_output_float_register(30, stop_reason)
end

def codex_echo_step4e(stop_reason):
  codex_echo_basic(stop_reason)
  write_output_float_register(31, read_input_float_register(44))
  write_output_float_register(32, read_input_float_register(45))
  write_output_float_register(33, read_input_float_register(39))
  write_output_float_register(36, read_input_float_register(37))
  write_output_float_register(37, read_input_float_register(38))
  write_output_float_register(38, read_input_float_register(39))
  write_output_float_register(39, read_input_float_register(40))
  write_output_float_register(40, read_input_float_register(41))
  write_output_float_register(41, read_input_float_register(42))
  write_output_float_register(42, read_input_float_register(43))
  write_output_float_register(43, read_input_float_register(44))
  write_output_float_register(44, read_input_float_register(45))
  write_output_float_register(45, read_input_float_register(46))
  write_output_float_register(46, read_input_float_register(47))
end
"""


def common_functions(normal_guard_n: str = "20.0") -> str:
    return COMMON_FUNCTIONS.replace(
        "codex_abs(normal_force) > 20.0",
        f"codex_abs(normal_force) > {normal_guard_n}",
    )


def preview_script(stamp: str, gen_at: str, version: str) -> str:
    return f"""# Step4e line preview {version}: no-motion RTDE/command preview.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: {gen_at}
# BEHAVIOR: wait for Kunwei bridge, echo Step4e command registers for review,
# and stop without speedl/movel contact motion. No UR zero_ftsensor, no Kunwei
# tare/config write, no TCP/payload write.
{common_functions()}

def codex_step4e_preview_line():
  local stop_reason = 0.0
  local t = 0.0
  local hold_s = 0.002
  textmsg("codex step4e version {stamp} start preview_line_{version}")
  write_output_float_register(34, 0.0)
  write_output_float_register(35, 20.0)
  if not codex_wait_for_fresh_heartbeat(30.0):
    stop_reason = 3.0
  end
  while stop_reason == 0.0 and t < 20.0:
    write_output_float_register(35, 25.0)
    codex_echo_step4e(stop_reason)
    stop_reason = codex_step4e_guard_stop_reason()
    sync()
    t = t + hold_s
  end
  write_output_float_register(30, stop_reason)
  write_output_float_register(35, 29.0)
  textmsg("codex step4e version {stamp} stop reason:", stop_reason)
end

codex_step4e_preview_line()
"""


def search_profile(version: str) -> dict[str, str]:
    if version in {"v6", "v7", "v8"}:
        return {
            "comment": "two-stage deterministic search: far 15 mm/s, then near 3 mm/s with 12 mm slow-search margin; no force admittance before contact latch.",
            "accel": "0.300",
            "hold": "0.002",
            "single_speed": "-0.003",
            "far_speed": "-0.015",
            "near_speed": "-0.003",
            "near_start": "0.080",
            "max_depth": "0.092",
            "runtime": "25.0",
            "two_stage": "True",
        }
    if version == "v5":
        return {
            "comment": "two-stage deterministic search: far 15 mm/s, then near 3 mm/s with 12 mm slow-search margin; no force admittance before contact latch.",
            "accel": "0.300",
            "hold": "0.002",
            "single_speed": "-0.003",
            "far_speed": "-0.015",
            "near_speed": "-0.003",
            "near_start": "0.080",
            "max_depth": "0.092",
            "runtime": "25.0",
            "two_stage": "True",
        }
    if version == "v4":
        return {
            "comment": "two-stage deterministic search: far 10 mm/s, then near 3 mm/s for the last 10 mm; no force admittance before contact latch.",
            "accel": "0.300",
            "hold": "0.002",
            "single_speed": "-0.003",
            "far_speed": "-0.010",
            "near_speed": "-0.003",
            "near_start": "0.080",
            "max_depth": "0.090",
            "runtime": "25.0",
            "two_stage": "True",
        }
    if version == "v3":
        return {
            "comment": "two-stage deterministic search: far 10 mm/s, then near 3 mm/s; no force admittance before contact latch.",
            "accel": "0.300",
            "hold": "0.002",
            "single_speed": "-0.003",
            "far_speed": "-0.010",
            "near_speed": "-0.003",
            "near_start": "0.045",
            "max_depth": "0.070",
            "runtime": "25.0",
            "two_stage": "True",
        }
    return {
        "comment": "deterministic downward speedl at 3 mm/s; no force admittance before contact latch.",
        "accel": "0.300",
        "hold": "0.002",
        "single_speed": "-0.003",
        "far_speed": "-0.003",
        "near_speed": "-0.003",
        "near_start": "0.060",
        "max_depth": "0.060",
        "runtime": "25.0",
        "two_stage": "False",
    }


def contact_script(mode: str, stamp: str, gen_at: str, geom: dict, version: str) -> str:
    is_line = mode == "line"
    enable_entry_rezero = version in {"v2", "v3", "v4", "v5", "v6", "v7", "v8"}
    search = search_profile(version)
    normal_guard_n = "30.0" if version in {"v6", "v7", "v8"} else "20.0"
    stop_decel = "0.1" if version in {"v7", "v8"} else "0.5"
    runtime_limit = 75.0 if is_line else 12.0
    end_check = f"""elif progress_m >= {fmt(geom['length'])}:
          stop_reason = 1.0""" if is_line else """elif t2 >= line_runtime_limit_s:
          stop_reason = 1.0"""
    timeout_check = """elif t2 >= line_runtime_limit_s:
          stop_reason = 10.0""" if is_line else "# hold mode reaches success at line_runtime_limit_s"
    program_label = "outerloop" if is_line else mode
    entry_rezero_block = """    write_output_float_register(35, 23.0)
    codex_echo_step4e(stop_reason)
    write_output_float_register(34, 1.0)
    if not codex_wait_for_rezero_complete(5.0):
      stop_reason = 14.0
    end
""" if enable_entry_rezero else ""
    rezero_comment = (
        "ENTRY_REZERO: request bridge re-baseline at entry pose before contact search."
        if enable_entry_rezero
        else "ENTRY_REZERO: not enabled in this version."
    )
    return f"""# Step4e {program_label} line {version}: deterministic contact search, Step4e bridge outer-loop command consumption.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: {gen_at}
# PATH: XY line from TP screenshot P0 [{fmt(geom['start_x'])}, {fmt(geom['start_y'])}]
# to P1 [{fmt(geom['end_x'])}, {fmt(geom['end_y'])}], length {fmt(geom['length'])} m.
# CONTROL: Ubuntu bridge computes paper-style outer-loop command in registers 37..47.
# URScript consumes speedl([37..42]) only after contact latch; yaw command is expected 0.
# SEARCH: {search['comment']}
# {rezero_comment}
# SAFETY: raw guards use registers 24..30; recoverable stops retract 10 mm then return home.
{common_functions(normal_guard_n)}

def codex_step4e_{program_label}_line():
  local entry_x = {fmt(geom['start_x'])}
  local entry_y = {fmt(geom['start_y'])}
  local ref_rx = {fmt(geom['ref_rx'])}
  local ref_ry = {fmt(geom['ref_ry'])}
  local ref_rz = {fmt(geom['ref_rz'])}
  local approach_speed_m_s = 0.050
  local search_accel_m_s2 = {search['accel']}
  local search_hold_s = {search['hold']}
  local search_down_m_s = {search['single_speed']}
  local search_far_down_m_s = {search['far_speed']}
  local search_near_down_m_s = {search['near_speed']}
  local search_near_start_depth_m = {search['near_start']}
  local max_search_down_m = {search['max_depth']}
  local use_two_stage_search = {search['two_stage']}
  local stale_limit_s = 0.100
  local search_runtime_limit_s = {search['runtime']}
  local line_accel_m_s2 = 0.300
  local line_hold_s = 0.002
  local line_runtime_limit_s = {fmt(runtime_limit)}
  local short_retract_z_m = 0.010
  local short_retract_speed_m_s = 0.020
  local home_return_speed_m_s = 0.050
  local stop_reason = 0.0
  local contact_triggered = 0
  local max_observed_stale_s = 0.0
  local final_progress_m = 0.0
  local home_pose = get_actual_tcp_pose()

  textmsg("codex step4e version {stamp} start {program_label}_line_{version}")
  write_output_float_register(34, 0.0)
  write_output_float_register(35, 20.0)
  codex_echo_step4e(0.0)

  if not codex_wait_for_fresh_heartbeat(30.0):
    stop_reason = 3.0
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 21.0)
    local p0 = get_actual_tcp_pose()
    local reference_orientation_pose = p[p0[0], p0[1], p0[2], ref_rx, ref_ry, ref_rz]
    movel(reference_orientation_pose, a=0.030, v=approach_speed_m_s, r=0.0)
    stopl({stop_decel})
    write_output_float_register(35, 22.0)
    local p1 = get_actual_tcp_pose()
    local entry_xy_pose = p[entry_x, entry_y, p1[2], ref_rx, ref_ry, ref_rz]
    movel(entry_xy_pose, a=0.030, v=approach_speed_m_s, r=0.0)
    stopl({stop_decel})
    sleep(0.20)
{entry_rezero_block}    if stop_reason == 0.0:
      sleep(0.20)
      stop_reason = codex_step4e_guard_stop_reason()
    end
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 24.0)
    local search_start = get_actual_tcp_pose()
    local last_heartbeat = read_input_float_register(26)
    local stale_s = 0.0
    local t = 0.0
    while stop_reason == 0.0:
      local heartbeat = read_input_float_register(26)
      local normal_force = read_input_float_register(24)
      local force_norm = read_input_float_register(25)
      local pose_now = get_actual_tcp_pose()
      local search_depth_m = search_start[2] - pose_now[2]
      if heartbeat == last_heartbeat:
        stale_s = stale_s + search_hold_s
        if stale_s > max_observed_stale_s:
          max_observed_stale_s = stale_s
        end
      else:
        stale_s = 0.0
        last_heartbeat = heartbeat
      end
      if normal_force <= -1.0 or force_norm > 1.5:
        contact_triggered = 1
        stop_reason = 11.0
      elif stale_s > stale_limit_s:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if search_depth_m >= max_search_down_m:
          stop_reason = 8.0
        elif t >= search_runtime_limit_s:
          stop_reason = 10.0
        else:
          if use_two_stage_search and search_depth_m < search_near_start_depth_m:
            write_output_float_register(35, 24.0)
            codex_echo_step4e(stop_reason)
            speedl([0.0, 0.0, search_far_down_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
          elif use_two_stage_search:
            write_output_float_register(35, 24.2)
            codex_echo_step4e(stop_reason)
            speedl([0.0, 0.0, search_near_down_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
          else:
            codex_echo_step4e(stop_reason)
            speedl([0.0, 0.0, search_down_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
          end
          t = t + search_hold_s
        end
      end
    end
    stopl({stop_decel})
  end

  if contact_triggered == 1:
    stop_reason = 0.0
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 25.0)
    local last_heartbeat2 = read_input_float_register(26)
    local stale_s2 = 0.0
    local t2 = 0.0
    while stop_reason == 0.0:
      local heartbeat2 = read_input_float_register(26)
      local cmd_valid = read_input_float_register(43)
      local progress_m = read_input_float_register(44)
      local cmd_vx = read_input_float_register(37)
      local cmd_vy = read_input_float_register(38)
      local cmd_vz = read_input_float_register(39)
      local cmd_wx = read_input_float_register(40)
      local cmd_wy = read_input_float_register(41)
      local cmd_wz = read_input_float_register(42)
      final_progress_m = progress_m
      if heartbeat2 == last_heartbeat2:
        stale_s2 = stale_s2 + line_hold_s
      else:
        stale_s2 = 0.0
        last_heartbeat2 = heartbeat2
      end
      codex_echo_step4e(stop_reason)
      if stale_s2 > stale_limit_s:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if cmd_valid < 0.5:
          stop_reason = 12.0
        elif codex_abs(cmd_vx) > 0.010 or codex_abs(cmd_vy) > 0.010 or codex_abs(cmd_vz) > 0.010:
          stop_reason = 13.0
        elif codex_abs(cmd_wx) > 0.030 or codex_abs(cmd_wy) > 0.030 or codex_abs(cmd_wz) > 0.005:
          stop_reason = 13.0
        {end_check}
        {timeout_check}
        else:
          speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy, 0.0], line_accel_m_s2, line_hold_s)
          t2 = t2 + line_hold_s
        end
      end
    end
    stopl({stop_decel})
  end

  if codex_should_auto_home(stop_reason):
    write_output_float_register(35, 26.0)
    local short_retract_start = get_actual_tcp_pose()
    local short_retract_pose = p[short_retract_start[0], short_retract_start[1], short_retract_start[2] + short_retract_z_m, short_retract_start[3], short_retract_start[4], short_retract_start[5]]
    codex_echo_step4e(stop_reason)
    movel(short_retract_pose, a=0.030, v=short_retract_speed_m_s, r=0.0)
    stopl({stop_decel})
    write_output_float_register(35, 27.0)
    codex_echo_step4e(stop_reason)
    movel(home_pose, a=0.030, v=home_return_speed_m_s, r=0.0)
    stopl({stop_decel})
  end

  write_output_float_register(30, stop_reason)
  write_output_float_register(31, final_progress_m)
  write_output_float_register(35, 29.0)
  textmsg("codex step4e version {stamp} stop reason:", stop_reason)
end

codex_step4e_{program_label}_line()
"""


def build_txt(name: str, stamp: str, description: str, geom: dict) -> str:
    return f"""Step4e line outer-loop TP package

Program:
  {name}

Version:
  {stamp}

Purpose:
  {description}

Path:
  start XY: {geom['start_x']:.9f}, {geom['start_y']:.9f}
  end XY: {geom['end_x']:.9f}, {geom['end_y']:.9f}
  XY length: {geom['length'] * 1000.0:.3f} mm

Control boundary:
  Ubuntu bridge computes Step4e outer-loop command registers 37..47.
  URScript consumes Cartesian speedl twist only after contact latch.
  This is not a finite-time RNN/joint-torque inner-loop reproduction.
"""


def build_urp(script: str, name: str) -> bytes:
    controller_script = f"{CONTROLLER_DIR}/{name}.script"
    xml = gzip.decompress(TEMPLATE_URP.read_bytes()).decode("utf-8")
    xml = re.sub(r'<URProgram name="[^"]+"', f'<URProgram name="{name}"', xml, count=1)
    xml = re.sub(r'directory="[^"]+"', f'directory="{CONTROLLER_DIR}"', xml, count=1)
    xml = re.sub(
        r'<cachedContents>.*?</cachedContents>',
        f"<cachedContents>{html.escape(script)}</cachedContents>",
        xml,
        count=1,
        flags=re.S,
    )
    xml = re.sub(
        r'<file resolves-to="file">.*?</file>',
        f'<file resolves-to="file">{controller_script}</file>',
        xml,
        count=1,
        flags=re.S,
    )
    return gzip.compress(xml.encode("utf-8"))


def validate(name: str, script: str, txt: str, urp: bytes, stamp: str) -> None:
    xml = gzip.decompress(urp).decode("utf-8")
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": f'URProgram name="{name}"' in xml,
        "installation path": 'installationRelativePath="../../../default"' in xml,
        "script file": f"{CONTROLLER_DIR}/{name}.script" in xml,
        "cached stamp": stamp in xml,
        "step4e registers": "read_input_float_register(37)" in xml,
    }
    if name.endswith(("_v2", "_v3", "_v4", "_v5", "_v6", "_v7", "_v8")) and "preview" not in name:
        checks.update(
            {
                "entry rezero request": "write_output_float_register(34, 1.0)" in xml,
                "entry rezero wait": "codex_wait_for_rezero_complete(5.0)" in xml,
                "rezero stop reason": "stop_reason = 14.0" in xml,
            }
        )
    if name.endswith("_v3") and "preview" not in name:
        checks.update(
            {
                "far search speed": "local search_far_down_m_s = -0.010" in xml,
                "near search speed": "local search_near_down_m_s = -0.003" in xml,
                "near search stage": "write_output_float_register(35, 24.2)" in xml,
                "near start depth": "local search_near_start_depth_m = 0.045" in xml,
                "max search depth": "local max_search_down_m = 0.070" in xml,
            }
        )
    if name.endswith("_v4") and "preview" not in name:
        checks.update(
            {
                "far search speed": "local search_far_down_m_s = -0.010" in xml,
                "near search speed": "local search_near_down_m_s = -0.003" in xml,
                "near search stage": "write_output_float_register(35, 24.2)" in xml,
                "near start depth": "local search_near_start_depth_m = 0.080" in xml,
                "max search depth": "local max_search_down_m = 0.090" in xml,
            }
        )
    if name.endswith(("_v5", "_v6", "_v7", "_v8")) and "preview" not in name:
        checks.update(
            {
                "far search speed": "local search_far_down_m_s = -0.015" in xml,
                "near search speed": "local search_near_down_m_s = -0.003" in xml,
                "near search stage": "write_output_float_register(35, 24.2)" in xml,
                "near start depth": "local search_near_start_depth_m = 0.080" in xml,
                "max search depth": "local max_search_down_m = 0.092" in xml,
            }
        )
    if name.endswith(("_v6", "_v7", "_v8")) and "preview" not in name:
        checks.update(
            {
                "normal guard": "codex_abs(normal_force) > 30.0" in script
                and "codex_abs(normal_force) &gt; 30.0" in xml,
            }
        )
    if name.endswith(("_v7", "_v8")) and "preview" not in name:
        checks.update(
            {
                "fast stop decel": "stopl(0.1)" in script and "stopl(0.1)" in xml,
                "old stop decel removed": "stopl(0.5)" not in script and "stopl(0.5)" not in xml,
            }
        )
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"{name} validation failed: {failed}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", choices=("v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8"), default="v2")
    parser.add_argument("--stamp-prefix", default=None)
    args = parser.parse_args()
    now = datetime.now(timezone(timedelta(hours=8)))
    config = load_json(CONFIG_PATH)
    geom = line_cfg(config)
    generated = {}
    for mode, spec in program_specs(args.version).items():
        stamp = args.stamp_prefix or source_stamp(spec["suffix"], now)
        if args.stamp_prefix:
            stamp = f"{args.stamp_prefix}_{spec['suffix']}"
        name = spec["name"]
        script = (
            preview_script(stamp, generated_at(now), args.version)
            if mode == "preview"
            else contact_script(mode, stamp, generated_at(now), geom, args.version)
        )
        txt = build_txt(name, stamp, spec["description"], geom)
        urp = build_urp(script, name)
        validate(name, script, txt, urp, stamp)
        script_path = PROGRAM_DIR / f"{name}.script"
        txt_path = PROGRAM_DIR / f"{name}.txt"
        urp_path = PROGRAM_DIR / f"{name}.urp"
        script_path.write_text(script, encoding="utf-8")
        txt_path.write_text(txt, encoding="utf-8")
        urp_path.write_bytes(urp)
        generated[mode] = {
            "script": str(script_path),
            "txt": str(txt_path),
            "urp": str(urp_path),
            "controller_script": f"{CONTROLLER_DIR}/{name}.script",
            "controller_urp": f"{CONTROLLER_DIR}/{name}.urp",
            "stamp": stamp,
        }
    print(json.dumps({"generated": generated, "line": geom}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
