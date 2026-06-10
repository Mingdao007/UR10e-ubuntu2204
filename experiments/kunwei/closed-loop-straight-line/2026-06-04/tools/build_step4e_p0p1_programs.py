#!/usr/bin/env python3
"""Generate Step4e P0/P1 attitude-isolation TP packages."""

from __future__ import annotations

import argparse
import gzip
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from build_step4e_line_programs import (
    CONFIG_PATH,
    CONTROLLER_BASE_DIR,
    PROGRAM_DIR,
    TEMPLATE_URP,
    common_functions,
    generated_at,
    installation_relative_path,
    line_cfg,
    load_json,
    source_stamp,
)


def build_urp(script: str, name: str, controller_dir: str) -> bytes:
    controller_script = f"{controller_dir}/{name}.script"
    install_rel = installation_relative_path(controller_dir)
    xml = gzip.decompress(TEMPLATE_URP.read_bytes()).decode("utf-8")
    xml = re.sub(r'<URProgram name="[^"]+"', f'<URProgram name="{name}"', xml, count=1)
    xml = re.sub(r'directory="[^"]+"', f'directory="{controller_dir}"', xml, count=1)
    xml = re.sub(r'installationRelativePath="[^"]+"', f'installationRelativePath="{install_rel}"', xml, count=1)
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


def build_txt(name: str, stamp: str, description: str) -> str:
    return f"""Step4e P0/P1 attitude package

Program:
  {name}

Version:
  {stamp}

Purpose:
  {description}

Control boundary:
  Ubuntu bridge writes RTDE input registers 24..47.
  No UR zero_ftsensor, no Kunwei tare/config write, no TCP/payload write.
"""


def validate_package(name: str, script: str, txt: str, urp: bytes, stamp: str, controller_dir: str) -> None:
    xml = gzip.decompress(urp).decode("utf-8")
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": f'URProgram name="{name}"' in xml,
        "controller directory": f'directory="{controller_dir}"' in xml,
        "installation path": f'installationRelativePath="{installation_relative_path(controller_dir)}"' in xml,
        "script file": f"{controller_dir}/{name}.script" in xml,
        "cached stamp": stamp in xml,
        "reads rtde command registers": "read_input_float_register(37)" in script,
        "guard function": "codex_step4e_guard_stop_reason()" in script,
        "fast stop": "stopl(0.1)" in script,
    }
    if name == "step4e_attitude_axis_iso_v1":
        checks.update(
            {
                "four quadrant stages": all(f"25.{suffix}" in script for suffix in ("21", "22", "23", "24")),
                "full angular speedl": "speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz]" in script,
                "linear command reject": "codex_abs(cmd_vx) > 0.001" in script,
            }
        )
    if name == "step4e_detached_movel_minrot_v21":
        checks.update(
            {
                "latch stage": "write_output_float_register(35, 25.05)" in script,
                "detach stage": "write_output_float_register(35, 25.1)" in script,
                "movel stage": "write_output_float_register(35, 25.2)" in script,
                "no force acquire": "write_output_float_register(35, 25.3)" not in script,
                "no line stage": "write_output_float_register(35, 25.0)" not in script,
                "target rotvec movel": "movel(target_pose, a=0.030, v=0.010, r=0.0)" in script,
            }
        )
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"{name} validation failed: {failed}")


def axis_iso_script(stamp: str, gen_at: str) -> str:
    return f"""# Step4e P0-b attitude axis isolation v1.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: {gen_at}
# PURPOSE: no-contact four-quadrant orientation-axis sign test.
# CONTROL: bridge step4e-mode=axis_iso writes 40..42 as angular wx/wy/wz; 37..39 must stay zero.
# SAFETY: raw normal guard 20 N, force norm guard 50 N, torque guard 3.0 Nm.
{common_functions("20.0", "3.0")}

def codex_axis_iso_stage(stage_code):
  local stop_reason = 0.0
  local last_heartbeat = read_input_float_register(26)
  local stale_s = 0.0
  local t = 0.0
  local stable_s = 0.0
  local saw_cmd_valid = 0
  local cmd_invalid_s = 0.0
  local hold_s = 0.002
  while stop_reason == 0.0:
    write_output_float_register(35, stage_code)
    local heartbeat = read_input_float_register(26)
    local cmd_valid = read_input_float_register(43)
    local orientation_error = read_input_float_register(46)
    local cmd_vx = read_input_float_register(37)
    local cmd_vy = read_input_float_register(38)
    local cmd_vz = read_input_float_register(39)
    local cmd_wx = read_input_float_register(40)
    local cmd_wy = read_input_float_register(41)
    local cmd_wz = read_input_float_register(42)
    local loop_dt = get_steptime()
    if cmd_valid >= 0.5:
      saw_cmd_valid = 1
      cmd_invalid_s = 0.0
    else:
      cmd_invalid_s = cmd_invalid_s + loop_dt
    end
    if heartbeat == last_heartbeat:
      stale_s = stale_s + loop_dt
    else:
      stale_s = 0.0
      last_heartbeat = heartbeat
    end
    t = t + loop_dt
    if t >= 0.250 and orientation_error <= 0.050:
      stable_s = stable_s + loop_dt
    else:
      stable_s = 0.0
    end
    codex_echo_step4e(stop_reason)
    if stale_s > 0.100:
      stop_reason = 2.0
    else:
      stop_reason = codex_step4e_guard_stop_reason()
    end
    if stop_reason == 0.0:
      if stable_s >= 0.250:
        return 0.0
      elif cmd_valid < 0.5:
        if saw_cmd_valid == 0 and t < 0.250:
          speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], 0.300, hold_s)
        elif saw_cmd_valid == 1 and cmd_invalid_s <= 0.100:
          speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], 0.300, hold_s)
        else:
          stop_reason = 12.0
        end
      elif codex_abs(cmd_vx) > 0.001 or codex_abs(cmd_vy) > 0.001 or codex_abs(cmd_vz) > 0.001:
        stop_reason = 13.0
      elif codex_abs(cmd_wx) > 0.120 or codex_abs(cmd_wy) > 0.120 or codex_abs(cmd_wz) > 0.120:
        stop_reason = 13.0
      elif t >= 8.000:
        stop_reason = 10.0
      else:
        speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz], 0.300, hold_s)
      end
    end
  end
  return stop_reason
end

def codex_step4e_axis_iso_v1():
  local stop_reason = 0.0
  local home_pose = get_actual_tcp_pose()
  textmsg("codex step4e version {stamp} start axis_iso_v1")
  write_output_float_register(34, 0.0)
  write_output_float_register(35, 20.0)
  codex_echo_step4e(0.0)
  if not codex_wait_for_fresh_heartbeat(30.0):
    stop_reason = 3.0
  end
  if stop_reason == 0.0:
    write_output_float_register(35, 22.0)
    local p0 = get_actual_tcp_pose()
    local vertical_pose = p[p0[0], p0[1], p0[2], 3.141592654, 0.0, 0.0]
    movel(vertical_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
  end
  if stop_reason == 0.0:
    stop_reason = codex_axis_iso_stage(25.21)
  end
  if stop_reason == 0.0:
    stop_reason = codex_axis_iso_stage(25.22)
  end
  if stop_reason == 0.0:
    stop_reason = codex_axis_iso_stage(25.23)
  end
  if stop_reason == 0.0:
    stop_reason = codex_axis_iso_stage(25.24)
  end
  stopl(0.1)
  if stop_reason == 15.0 or codex_should_auto_home(stop_reason):
    write_output_float_register(35, 27.0)
    codex_echo_step4e(stop_reason)
    movel(home_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
  end
  write_output_float_register(30, stop_reason)
  write_output_float_register(35, 29.0)
  textmsg("codex step4e version {stamp} stop reason:", stop_reason)
end

codex_step4e_axis_iso_v1()
"""


def v21_detached_movel_script(stamp: str, gen_at: str, geom: dict[str, float]) -> str:
    return f"""# Step4e P1-a detached movel minimal-rotation v21.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: {gen_at}
# PURPOSE: latch first contact normal, detach, then adjust orientation with one minimal-rotation movel.
# CONTROL: bridge step4e-version=v21 writes 37..39 as detach direction in 25.1 and 40..42 as target rotvec in 25.2.
# SAFETY: raw normal guard 20 N, force norm guard 50 N, torque guard 3.0 Nm.
{common_functions("20.0", "3.0")}

def codex_wait_for_cmd_valid(stage_code, timeout_s):
  local t = 0.0
  local last_heartbeat = read_input_float_register(26)
  local stale_s = 0.0
  while t < timeout_s:
    write_output_float_register(35, stage_code)
    local heartbeat = read_input_float_register(26)
    if heartbeat == last_heartbeat:
      stale_s = stale_s + get_steptime()
    else:
      stale_s = 0.0
      last_heartbeat = heartbeat
    end
    codex_echo_step4e(0.0)
    if stale_s > 0.100:
      return 2.0
    end
    local guard_reason = codex_step4e_guard_stop_reason()
    if guard_reason != 0.0:
      return guard_reason
    end
    if read_input_float_register(43) >= 0.5:
      return 0.0
    end
    sync()
    t = t + get_steptime()
  end
  return 12.0
end

def codex_step4e_detached_movel_v21():
  local stop_reason = 0.0
  local home_pose = get_actual_tcp_pose()
  local search_accel_m_s2 = 0.300
  local search_hold_s = 0.002
  local stale_limit_s = 0.100
  local search_runtime_limit_s = 40.0
  local search_near_start_depth_m = 0.130
  local max_search_down_m = 0.150
  local contact_triggered = 0
  textmsg("codex step4e version {stamp} start detached_movel_v21")
  write_output_float_register(34, 0.0)
  write_output_float_register(35, 20.0)
  codex_echo_step4e(0.0)
  if not codex_wait_for_fresh_heartbeat(30.0):
    stop_reason = 3.0
  end
  if stop_reason == 0.0:
    write_output_float_register(35, 22.0)
    local p1 = get_actual_tcp_pose()
    local entry_pose = p[{geom['start_x']:.9f}, {geom['start_y']:.9f}, p1[2], 3.141592654, 0.0, 0.0]
    movel(entry_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
    write_output_float_register(35, 23.0)
    codex_echo_step4e(stop_reason)
    write_output_float_register(34, 1.0)
    if not codex_wait_for_rezero_complete(5.0):
      stop_reason = 14.0
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
        stale_s = stale_s + get_steptime()
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
          if search_depth_m < search_near_start_depth_m:
            codex_echo_step4e(stop_reason)
            speedl([0.0, 0.0, -0.015, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
          else:
            write_output_float_register(35, 24.2)
            codex_echo_step4e(stop_reason)
            speedl([0.0, 0.0, -0.003, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
          end
          t = t + get_steptime()
        end
      end
    end
    stopl(0.1)
  end
  if contact_triggered == 1:
    stop_reason = 0.0
  end
  if stop_reason == 0.0:
    write_output_float_register(35, 25.05)
    stop_reason = codex_wait_for_cmd_valid(25.05, 1.000)
  end
  if stop_reason == 0.0:
    write_output_float_register(35, 25.1)
    stop_reason = codex_wait_for_cmd_valid(25.1, 1.000)
    local detach_total_m = 0.0
    while stop_reason == 0.0 and detach_total_m < 0.005 and read_input_float_register(25) > 2.0:
      local dir_x = read_input_float_register(37)
      local dir_y = read_input_float_register(38)
      local dir_z = read_input_float_register(39)
      local step_m = 0.001
      if detach_total_m < 0.0005:
        step_m = 0.002
      end
      local p_detach = get_actual_tcp_pose()
      local detach_pose = p[p_detach[0] + step_m * dir_x, p_detach[1] + step_m * dir_y, p_detach[2] + step_m * dir_z, p_detach[3], p_detach[4], p_detach[5]]
      codex_echo_step4e(stop_reason)
      movel(detach_pose, a=0.030, v=0.010, r=0.0)
      stopl(0.1)
      detach_total_m = detach_total_m + step_m
      stop_reason = codex_step4e_guard_stop_reason()
    end
  end
  if stop_reason == 0.0:
    write_output_float_register(35, 25.2)
    stop_reason = codex_wait_for_cmd_valid(25.2, 1.000)
    if stop_reason == 0.0:
      local p_orient = get_actual_tcp_pose()
      local target_pose = p[p_orient[0], p_orient[1], p_orient[2], read_input_float_register(40), read_input_float_register(41), read_input_float_register(42)]
      codex_echo_step4e(stop_reason)
      movel(target_pose, a=0.030, v=0.010, r=0.0)
      stopl(0.1)
      sleep(0.250)
      if read_input_float_register(46) > 0.030:
        stop_reason = 15.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
    end
  end
  if stop_reason == 15.0 or codex_should_auto_home(stop_reason):
    write_output_float_register(35, 27.0)
    codex_echo_step4e(stop_reason)
    movel(home_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
  end
  write_output_float_register(30, stop_reason)
  write_output_float_register(35, 29.0)
  textmsg("codex step4e version {stamp} stop reason:", stop_reason)
end

codex_step4e_detached_movel_v21()
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp-prefix", default=None)
    args = parser.parse_args()
    now = datetime.now(timezone(timedelta(hours=8)))
    geom = line_cfg(load_json(CONFIG_PATH))
    specs = [
        (
            "step4e_attitude_axis_iso_v1",
            "AXIS_ISO_V1",
            "P0-b no-contact four-quadrant orientation-axis isolation",
            axis_iso_script,
        ),
        (
            "step4e_detached_movel_minrot_v21",
            "DETACHED_MOVEL_MINROT_V21",
            "P1-a latch, detach, minimal-rotation movel attitude check",
            v21_detached_movel_script,
        ),
    ]
    generated = {}
    for name, suffix, description, script_fn in specs:
        stamp = args.stamp_prefix or source_stamp(suffix, now)
        if name == "step4e_detached_movel_minrot_v21":
            script = script_fn(stamp, generated_at(now), geom)
        else:
            script = script_fn(stamp, generated_at(now))
        txt = build_txt(name, stamp, description)
        urp = build_urp(script, name, CONTROLLER_BASE_DIR)
        validate_package(name, script, txt, urp, stamp, CONTROLLER_BASE_DIR)
        script_path = PROGRAM_DIR / f"{name}.script"
        txt_path = PROGRAM_DIR / f"{name}.txt"
        urp_path = PROGRAM_DIR / f"{name}.urp"
        script_path.write_text(script, encoding="utf-8")
        txt_path.write_text(txt, encoding="utf-8")
        urp_path.write_bytes(urp)
        generated[name] = {
            "script": str(script_path),
            "txt": str(txt_path),
            "urp": str(urp_path),
            "controller_script": f"{CONTROLLER_BASE_DIR}/{name}.script",
            "controller_urp": f"{CONTROLLER_BASE_DIR}/{name}.urp",
            "stamp": stamp,
        }
    print(json.dumps({"generated": generated}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
