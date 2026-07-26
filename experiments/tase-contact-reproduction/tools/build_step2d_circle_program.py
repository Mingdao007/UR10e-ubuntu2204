#!/usr/bin/env python3
"""Generate the Step2D circular contact-path TP package from Step2C v8."""

from __future__ import annotations

import argparse
import gzip
import html
import json
import math
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
PROGRAM_DIR = EXPERIMENT_ROOT / "programs"
CONFIG_PATH = EXPERIMENT_ROOT / "config" / "straight_line_reference.json"

BASE_NAME = "step2c_admittance_search30_guard20_search2ms_line1ms_alpha70_v8_v4copy"
BASE_SCRIPT = PROGRAM_DIR / f"{BASE_NAME}.script"
BASE_TXT = PROGRAM_DIR / f"{BASE_NAME}.txt"
BASE_URP = PROGRAM_DIR / f"{BASE_NAME}.urp"

OUT_NAME = "step2d_circle_full_paper_attitude_v1"
OUT_SCRIPT = PROGRAM_DIR / f"{OUT_NAME}.script"
OUT_TXT = PROGRAM_DIR / f"{OUT_NAME}.txt"
OUT_URP = PROGRAM_DIR / f"{OUT_NAME}.urp"
CONTROLLER_SCRIPT = f"/programs/andyl/kunwei/step2/{OUT_NAME}.script"
CONTROLLER_URP = f"/programs/andyl/kunwei/step2/{OUT_NAME}.urp"

DRY_NAME = "step2d_circle_no_contact_v1"
DRY_SCRIPT = PROGRAM_DIR / f"{DRY_NAME}.script"
DRY_TXT = PROGRAM_DIR / f"{DRY_NAME}.txt"
DRY_URP = PROGRAM_DIR / f"{DRY_NAME}.urp"
DRY_CONTROLLER_SCRIPT = f"/programs/andyl/kunwei/step2/{DRY_NAME}.script"
DRY_CONTROLLER_URP = f"/programs/andyl/kunwei/step2/{DRY_NAME}.urp"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def fmt(value: float) -> str:
    return f"{value:.9f}".rstrip("0").rstrip(".")


def derive_geometry(config: dict) -> dict[str, float]:
    line = config["reference_line"]
    circle_cfg = config.get("step2d_circle", {})
    start_fraction = float(circle_cfg.get("diameter_start_fraction", 0.25))
    end_fraction = float(circle_cfg.get("diameter_end_fraction", 0.75))
    center_fraction = 0.5 * (start_fraction + end_fraction)
    direction = float(circle_cfg.get("circle_direction", 1.0))
    ux, uy = [float(v) for v in line["xy_unit_vector"]]
    length = float(line["xy_length_m"])
    contact_x, contact_y, contact_z = [float(v) for v in line["contact_start_xyz_m"]]
    radius = 0.5 * abs(end_fraction - start_fraction) * length
    start_x = contact_x + start_fraction * length * ux
    start_y = contact_y + start_fraction * length * uy
    center_x = contact_x + center_fraction * length * ux
    center_y = contact_y + center_fraction * length * uy
    opposite_x = contact_x + end_fraction * length * ux
    opposite_y = contact_y + end_fraction * length * uy
    return {
        "ux": ux,
        "uy": uy,
        "contact_z": contact_z,
        "start_x": start_x,
        "start_y": start_y,
        "center_x": center_x,
        "center_y": center_y,
        "opposite_x": opposite_x,
        "opposite_y": opposite_y,
        "radius": radius,
        "arc_length": 2.0 * math.pi * radius,
        "direction": 1.0 if direction >= 0 else -1.0,
    }


def replace_once(text: str, old: str, new: str) -> str:
    if old not in text:
        raise RuntimeError(f"source block not found: {old[:80]!r}")
    return text.replace(old, new, 1)


def build_line_stage_block() -> str:
    return """  if stop_reason == 0.0:
    write_output_float_register(35, 25.0)
    local circle_start_pose = get_actual_tcp_pose()
    local last_heartbeat2 = read_input_float_register(26)
    local stale_s2 = 0.0
    local t2 = 0.0
    local theta = 0.0
    local filtered_normal_force = read_input_float_register(24)
    local filtered_fx = read_input_float_register(31)
    local filtered_fy = read_input_float_register(32)
    local filtered_mx = read_input_float_register(34)
    local filtered_my = read_input_float_register(35)
    local integral_error = 0.0
    while stop_reason == 0.0:
      if t2 >= line_fast_after_s:
        line_hold_s = line_hold_fast_s
      end
      local raw_normal_force = read_input_float_register(24)
      local heartbeat2 = read_input_float_register(26)
      local target_force_abs = read_input_float_register(29)
      local pose_line = get_actual_tcp_pose()
      local progress_m = theta * circle_radius_m
      local correction_m = pose_line[2] - circle_start_pose[2]

      if target_force_abs < 0.1:
        target_force_abs = target_force_abs_default_n
      end

      filtered_normal_force = normal_filter_alpha * raw_normal_force + (1.0 - normal_filter_alpha) * filtered_normal_force
      local force_error = target_force_abs + filtered_normal_force
      integral_error = codex_clamp(integral_error + force_error * line_hold_s, -integral_error_limit_n_s, integral_error_limit_n_s)
      local normal_velocity = 0.0 - codex_clamp(normal_velocity_p_gain * force_error + normal_velocity_i_gain * integral_error, -normal_velocity_limit_m_s, normal_velocity_limit_m_s)

      local fx_n = read_input_float_register(31)
      local fy_n = read_input_float_register(32)
      local mx_nm = read_input_float_register(34)
      local my_nm = read_input_float_register(35)
      filtered_fx = attitude_filter_alpha * fx_n + (1.0 - attitude_filter_alpha) * filtered_fx
      filtered_fy = attitude_filter_alpha * fy_n + (1.0 - attitude_filter_alpha) * filtered_fy
      filtered_mx = attitude_filter_alpha * mx_nm + (1.0 - attitude_filter_alpha) * filtered_mx
      filtered_my = attitude_filter_alpha * my_nm + (1.0 - attitude_filter_alpha) * filtered_my
      local force_tilt_x = codex_clamp(filtered_fy / target_force_abs, -max_attitude_error_proxy_rad, max_attitude_error_proxy_rad)
      local force_tilt_y = codex_clamp(0.0 - filtered_fx / target_force_abs, -max_attitude_error_proxy_rad, max_attitude_error_proxy_rad)
      local attitude_error_proxy_sq = force_tilt_x * force_tilt_x + force_tilt_y * force_tilt_y
      local angular_x = codex_clamp(attitude_force_gain_rad_s * force_tilt_x + attitude_torque_gain_rad_s_per_nm * filtered_mx, -angular_velocity_limit_rad_s, angular_velocity_limit_rad_s)
      local angular_y = codex_clamp(attitude_force_gain_rad_s * force_tilt_y + attitude_torque_gain_rad_s_per_nm * filtered_my, -angular_velocity_limit_rad_s, angular_velocity_limit_rad_s)

      local sin_theta = sin(theta)
      local cos_theta = cos(theta)
      local e1x = circle_direction * circle_perp_x
      local e1y = circle_direction * circle_perp_y
      local desired_x = circle_start_pose[0] + circle_radius_m * (1.0 - cos_theta) * ux + circle_radius_m * sin_theta * e1x
      local desired_y = circle_start_pose[1] + circle_radius_m * (1.0 - cos_theta) * uy + circle_radius_m * sin_theta * e1y
      local tangent_vx = tangent_speed_m_s * (sin_theta * ux + cos_theta * e1x)
      local tangent_vy = tangent_speed_m_s * (sin_theta * uy + cos_theta * e1y)
      local correction_vx = codex_clamp(circle_xy_p_gain_m_s_per_m * (desired_x - pose_line[0]), -circle_xy_velocity_limit_m_s, circle_xy_velocity_limit_m_s)
      local correction_vy = codex_clamp(circle_xy_p_gain_m_s_per_m * (desired_y - pose_line[1]), -circle_xy_velocity_limit_m_s, circle_xy_velocity_limit_m_s)
      local command_vx = tangent_vx + correction_vx
      local command_vy = tangent_vy + correction_vy

      if heartbeat2 == last_heartbeat2:
        stale_s2 = stale_s2 + line_hold_s
      else:
        stale_s2 = 0.0
        last_heartbeat2 = heartbeat2
      end

      codex_write_echo(stop_reason, progress_m, correction_m, normal_velocity)

      if stale_s2 > stale_limit_s:
        stop_reason = 2.0
      else:
        stop_reason = codex_step2_guard_stop_reason()
      end

      if stop_reason == 0.0:
        if codex_abs(correction_m) > 0.008:
          stop_reason = 8.0
        elif progress_m < -0.003 or progress_m > path_length_m + 0.003:
          stop_reason = 9.0
        elif attitude_error_proxy_sq > max_attitude_error_proxy_sq:
          stop_reason = 12.0
        elif t2 >= line_runtime_limit_s:
          stop_reason = 10.0
        elif theta >= two_pi:
          stop_reason = 1.0
        else:
          speedl([command_vx, command_vy, normal_velocity, angular_x, angular_y, 0.0], line_accel_m_s2, line_hold_s)
          t2 = t2 + line_hold_s
          theta = theta + tangent_speed_m_s * line_hold_s / circle_radius_m
        end
      end
    end
    stopl(0.5)
  end
"""


def build_script(config: dict, stamp: str, generated_at: str) -> str:
    geometry = derive_geometry(config)
    script = BASE_SCRIPT.read_text(encoding="utf-8")
    script = re.sub(
        r"^# Step2C.*?\n# VERSION:.*?\n# GENERATED_AT_LOCAL:.*?\n",
        (
            "# Step2D full-circle contact path v1: Step2C v8 scaffold with circular tangent twist, "
            "Z force admittance, and bounded paper-first attitude compliance.\n"
            f"# VERSION: {stamp}\n"
            f"# GENERATED_AT_LOCAL: {generated_at}\n"
        ),
        script,
        count=1,
        flags=re.S,
    )
    script = replace_once(
        script,
        "# BEHAVIOR: wait for Kunwei bridge; align to old reference orientation; move to\n# old contact-start XY; do bounded Z prep instead of blindly returning to\n# contact Z + 100 mm.",
        "# BEHAVIOR: wait for Kunwei bridge; align to old reference orientation; move to\n# Step2D circle-start XY derived from the middle half of the old contact path;\n# do bounded Z prep instead of blindly returning to contact Z + 100 mm.",
    )
    script = replace_once(
        script,
        "# first 0.10 s of line control uses 10 ms speedl holds, then line control\n# switches to 1 ms holds.",
        "# first 0.10 s of circle control uses 10 ms speedl holds, then circle control\n# switches to 1 ms holds. The circle stage commands Cartesian twist, so UR still owns IK.",
    )
    script = replace_once(
        script,
        "# Aggressive normal Z correction targets signed Fz ~= -target_force_abs, where the bridge\n# normally writes target_force_abs=5 N into input register 29.",
        "# Normal Z correction targets signed Fz ~= -target_force_abs, where the bridge\n# normally writes target_force_abs=5 N into input register 29. Bounded attitude\n# compliance uses lateral force and Mx/My as a paper-first force-shortest-arc proxy.",
    )
    script = replace_once(script, "def codex_step2c_full():", "def codex_step2d_full():")
    script = replace_once(script, "  local entry_x = 0.446585764", f"  local entry_x = {fmt(geometry['start_x'])}")
    script = replace_once(script, "  local entry_y = 0.226717001", f"  local entry_y = {fmt(geometry['start_y'])}")
    script = replace_once(script, "  local tangent_speed_m_s = 0.010", "  local tangent_speed_m_s = 0.005")
    script = replace_once(script, "  local path_length_m = 0.06358", f"  local path_length_m = {fmt(geometry['arc_length'])}")
    script = replace_once(
        script,
        "  local line_accel_m_s2 = 0.500",
        (
            "  local line_accel_m_s2 = 0.300\n"
            f"  local circle_radius_m = {fmt(geometry['radius'])}\n"
            "  local two_pi = 6.283185307\n"
            f"  local circle_direction = {fmt(geometry['direction'])}\n"
            "  local circle_perp_x = 0.733430768\n"
            "  local circle_perp_y = 0.679764156\n"
            "  local circle_xy_p_gain_m_s_per_m = 3.0\n"
            "  local circle_xy_velocity_limit_m_s = 0.003\n"
            "  local angular_velocity_limit_rad_s = 0.030\n"
            "  local attitude_force_gain_rad_s = 0.040\n"
            "  local attitude_torque_gain_rad_s_per_nm = 0.050\n"
            "  local attitude_filter_alpha = 0.20\n"
            "  local max_attitude_error_proxy_rad = 0.35\n"
            "  local max_attitude_error_proxy_sq = 0.1225"
        ),
    )
    script = re.sub(
        r'textmsg\("codex step2c version [^"]+ start [^"]+"\)',
        f'textmsg("codex step2d version {stamp} start full_circle_paper_attitude_v1")',
        script,
        count=1,
    )
    script = re.sub(
        r'textmsg\("codex step2c version [^"]+ stop reason:", stop_reason\)',
        f'textmsg("codex step2d version {stamp} stop reason:", stop_reason)',
        script,
        count=1,
    )
    start = script.index("  if stop_reason == 0.0:\n    write_output_float_register(35, 25.0)")
    end = script.index("\n  if stop_reason == 1.0:", start)
    script = script[:start] + build_line_stage_block() + script[end + 1 :]
    script = script.replace("codex_write_echo(stop_reason, path_length_m, short_retract_z_m, 0.0)", "codex_write_echo(stop_reason, path_length_m, short_retract_z_m, 0.0)")
    script = replace_once(script, "codex_step2c_full()", "codex_step2d_full()")
    return script


def build_txt(stamp: str, geometry: dict[str, float]) -> str:
    return f"""Step2D full-circle contact path v1 TP package

Open on Teach Pendant:
  {CONTROLLER_URP}

Script source:
  {CONTROLLER_SCRIPT}

Version stamp:
  {stamp}

Geometry:
  source: middle half of the existing Step2C contact path
  circle start XY: {geometry['start_x']:.9f}, {geometry['start_y']:.9f}
  circle center XY: {geometry['center_x']:.9f}, {geometry['center_y']:.9f}
  opposite diameter XY: {geometry['opposite_x']:.9f}, {geometry['opposite_y']:.9f}
  radius: {geometry['radius'] * 1000.0:.3f} mm
  full-circle arc length: {geometry['arc_length'] * 1000.0:.3f} mm

Motion summary:
  Step2C v8 scaffold: bounded high approach, contact search, soft acquisition, short retract, home return
  circle: full circle, Cartesian speedl twist, UR handles IK
  tangent speed: 5 mm/s in v1
  Z control: signed Fz velocity admittance
  attitude: bounded force/torque-based paper-first compliance proxy, wx/wy only

Raw guards:
  normal force guard: 20 N
  force norm guard: 50 N
  torque norm guard: 0.6 Nm
  sensor stale: 100 ms
  Z correction guard: 8 mm
  attitude proxy guard: 0.35 rad
"""


def build_dry_script(config: dict, stamp: str, generated_at: str) -> str:
    geometry = derive_geometry(config)
    return f"""# Step2D no-contact full-circle v1: fixed-Z dry run for circle geometry and UR-owned IK.
# VERSION: {stamp}_NO_CONTACT
# GENERATED_AT_LOCAL: {generated_at}
# BEHAVIOR: align to Step2C reference orientation, move to Step2D circle start at safe Z,
# trace one full circle with Cartesian speedl XY twist, stop, and return to TP-start home.
# No Kunwei stream, no RTDE input dependency, no contact search, no force control,
# no attitude compliance, no UR zero_ftsensor, no TCP/payload write.

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

def codex_step2d_circle_no_contact():
  local ux = {fmt(geometry['ux'])}
  local uy = {fmt(geometry['uy'])}
  local entry_x = {fmt(geometry['start_x'])}
  local entry_y = {fmt(geometry['start_y'])}
  local old_contact_z = {fmt(geometry['contact_z'])}
  local safe_z = old_contact_z + 0.100
  local ref_rx = 3.141572714
  local ref_ry = -0.000026465
  local ref_rz = -0.000024158
  local circle_radius_m = {fmt(geometry['radius'])}
  local two_pi = 6.283185307
  local circle_direction = {fmt(geometry['direction'])}
  local circle_perp_x = 0.733430768
  local circle_perp_y = 0.679764156
  local tangent_speed_m_s = 0.005
  local circle_xy_p_gain_m_s_per_m = 3.0
  local circle_xy_velocity_limit_m_s = 0.003
  local hold_s = 0.008
  local accel_m_s2 = 0.300

  local home_pose = get_actual_tcp_pose()
  textmsg("codex step2d no-contact version {stamp}_NO_CONTACT start")

  local p0 = get_actual_tcp_pose()
  movel(p[p0[0], p0[1], p0[2], ref_rx, ref_ry, ref_rz], a=0.030, v=0.050, r=0.0)
  stopl(0.5)
  movel(p[entry_x, entry_y, safe_z, ref_rx, ref_ry, ref_rz], a=0.030, v=0.050, r=0.0)
  stopl(0.5)

  local circle_start_pose = get_actual_tcp_pose()
  local theta = 0.0
  while theta < two_pi:
    local pose_now = get_actual_tcp_pose()
    local sin_theta = sin(theta)
    local cos_theta = cos(theta)
    local e1x = circle_direction * circle_perp_x
    local e1y = circle_direction * circle_perp_y
    local desired_x = circle_start_pose[0] + circle_radius_m * (1.0 - cos_theta) * ux + circle_radius_m * sin_theta * e1x
    local desired_y = circle_start_pose[1] + circle_radius_m * (1.0 - cos_theta) * uy + circle_radius_m * sin_theta * e1y
    local tangent_vx = tangent_speed_m_s * (sin_theta * ux + cos_theta * e1x)
    local tangent_vy = tangent_speed_m_s * (sin_theta * uy + cos_theta * e1y)
    local correction_vx = codex_clamp(circle_xy_p_gain_m_s_per_m * (desired_x - pose_now[0]), -circle_xy_velocity_limit_m_s, circle_xy_velocity_limit_m_s)
    local correction_vy = codex_clamp(circle_xy_p_gain_m_s_per_m * (desired_y - pose_now[1]), -circle_xy_velocity_limit_m_s, circle_xy_velocity_limit_m_s)
    speedl([tangent_vx + correction_vx, tangent_vy + correction_vy, 0.0, 0.0, 0.0, 0.0], accel_m_s2, hold_s)
    theta = theta + tangent_speed_m_s * hold_s / circle_radius_m
  end
  stopl(0.5)
  movel(home_pose, a=0.030, v=0.050, r=0.0)
  stopl(0.5)
  textmsg("codex step2d no-contact version {stamp}_NO_CONTACT complete")
end

codex_step2d_circle_no_contact()
"""


def build_dry_txt(stamp: str, geometry: dict[str, float]) -> str:
    return f"""Step2D no-contact full-circle v1 TP package

Open on Teach Pendant:
  {DRY_CONTROLLER_URP}

Script source:
  {DRY_CONTROLLER_SCRIPT}

Version stamp:
  {stamp}_NO_CONTACT

Purpose:
  First dry run before contact. It verifies the Step2D circle geometry and UR-owned IK at safe Z.

Geometry:
  circle start XY: {geometry['start_x']:.9f}, {geometry['start_y']:.9f}
  circle center XY: {geometry['center_x']:.9f}, {geometry['center_y']:.9f}
  radius: {geometry['radius'] * 1000.0:.3f} mm
  full-circle arc length: {geometry['arc_length'] * 1000.0:.3f} mm

Motion summary:
  no Kunwei stream
  no RTDE input dependency
  no contact search
  no force control
  no attitude compliance
  fixed safe Z = old contact Z + 100 mm
  tangent speed = 5 mm/s
"""


def build_urp_for(script: str, name: str, controller_script: str) -> bytes:
    xml = gzip.decompress(BASE_URP.read_bytes()).decode("utf-8")
    xml = re.sub(r'<URProgram name="[^"]+"', f'<URProgram name="{name}"', xml, count=1)
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


def build_urp(script: str) -> bytes:
    return build_urp_for(script, OUT_NAME, CONTROLLER_SCRIPT)


def validate(script: str, urp_bytes: bytes, stamp: str) -> None:
    if stamp not in script:
        raise RuntimeError("stamp missing from generated script")
    xml = gzip.decompress(urp_bytes).decode("utf-8")
    checks = {
        "program name": f'URProgram name="{OUT_NAME}"',
        "installation path": 'installationRelativePath="../../../default"',
        "controller script": CONTROLLER_SCRIPT,
        "stamp": stamp,
        "circle speedl": "speedl([command_vx, command_vy, normal_velocity, angular_x, angular_y, 0.0]",
    }
    for label, needle in checks.items():
        if needle not in xml and needle not in script:
            raise RuntimeError(f"{label} check failed: {needle}")


def validate_dry(script: str, urp_bytes: bytes, stamp: str) -> None:
    xml = gzip.decompress(urp_bytes).decode("utf-8")
    checks = {
        "program name": f'URProgram name="{DRY_NAME}"',
        "installation path": 'installationRelativePath="../../../default"',
        "controller script": DRY_CONTROLLER_SCRIPT,
        "stamp": f"{stamp}_NO_CONTACT",
        "dry speedl": "speedl([tangent_vx + correction_vx, tangent_vy + correction_vy, 0.0, 0.0, 0.0, 0.0]",
    }
    for label, needle in checks.items():
        if needle not in xml and needle not in script:
            raise RuntimeError(f"dry {label} check failed: {needle}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp", default=None)
    args = parser.parse_args()
    now = datetime.now(timezone(timedelta(hours=8)))
    stamp = args.stamp or now.strftime("%Y-%m-%dT%H%MHKT_STEP2D_CIRCLE_FULL_PAPER_ATTITUDE_V1")
    generated_at = now.isoformat(timespec="seconds")
    config = load_json(CONFIG_PATH)
    geometry = derive_geometry(config)
    script = build_script(config, stamp, generated_at)
    txt = build_txt(stamp, geometry)
    urp = build_urp(script)
    dry_script = build_dry_script(config, stamp, generated_at)
    dry_txt = build_dry_txt(stamp, geometry)
    dry_urp = build_urp_for(dry_script, DRY_NAME, DRY_CONTROLLER_SCRIPT)
    validate(script, urp, stamp)
    validate_dry(dry_script, dry_urp, stamp)
    OUT_SCRIPT.write_text(script, encoding="utf-8")
    OUT_TXT.write_text(txt, encoding="utf-8")
    OUT_URP.write_bytes(urp)
    DRY_SCRIPT.write_text(dry_script, encoding="utf-8")
    DRY_TXT.write_text(dry_txt, encoding="utf-8")
    DRY_URP.write_bytes(dry_urp)
    print(json.dumps({
        "script": str(OUT_SCRIPT),
        "txt": str(OUT_TXT),
        "urp": str(OUT_URP),
        "dry_script": str(DRY_SCRIPT),
        "dry_txt": str(DRY_TXT),
        "dry_urp": str(DRY_URP),
        "stamp": stamp,
        "geometry": geometry,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
