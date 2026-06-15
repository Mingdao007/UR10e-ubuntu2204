#!/usr/bin/env python3
"""Generate Step6a no-contact 8-shaped TP package."""

from __future__ import annotations

import argparse
import gzip
import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from build_step4e_line_programs import CONFIG_PATH, TEMPLATE_URP, generated_at, installation_relative_path, line_cfg, load_json
from step6_eight import (
    ALONG_AMPLITUDE_M,
    CONTROLLER_DIR,
    LATERAL_AMPLITUDE_M,
    LOCAL_PROGRAM_DIR,
    OMEGA_RAD_S,
    PATH_DURATION_S,
    PROGRAM_NAME,
    REPORT_DIR,
    STEP6_SAFE_FRAME_PATH,
    STEP6_TABLE_PATH,
    eight_local,
    fixed_base_z,
    load_safe_frame,
    reference_samples,
    step6_stage,
)


WARMUP_HOLD_S = float(step6_stage()["cadence"]["warmup_hold_s"])
FAST_HOLD_S = float(step6_stage()["cadence"]["fast_hold_s"])
FAST_AFTER_S = float(step6_stage()["cadence"]["fast_after_s"])
VELOCITY_CAP_M_S = float(step6_stage()["guard"]["velocity_cap_m_s"])
CLEARANCE_ABOVE_PLATFORM_M = 0.010


def source_stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H%MHKT_STEP6A_EIGHT_NO_CONTACT_V1")


def build_metrics(frame: dict) -> dict:
    rows = reference_samples(frame)
    guard = frame["guard"]
    fixed_z_m = fixed_base_z(frame)
    points = {
        "start": rows[0],
        "mid": rows[len(rows) // 2],
        "end": rows[-1],
    }
    return {
        "program": PROGRAM_NAME,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "policy": {
            "stage": "Step6a",
            "contact": "no-contact",
            "force_control": False,
            "contact_search": False,
            "kunwei_bridge_required": False,
            "zero_ftsensor": False,
            "tcp_payload_write": False,
        },
        "path": {
            "formula": "along=0.04*sin(0.2t), lateral=0.01*sin(0.4t)",
            "duration_s": PATH_DURATION_S,
            "along_amplitude_m": ALONG_AMPLITUDE_M,
            "lateral_amplitude_m": LATERAL_AMPLITUDE_M,
            "omega_rad_s": OMEGA_RAD_S,
            "max_reference_speed_m_s": max((row["base_vx_m_s"] ** 2 + row["base_vy_m_s"] ** 2) ** 0.5 for row in rows),
        },
        "cadence": step6_stage()["cadence"],
        "fixed_z": {
            "highest_taught_z_m": float(frame.get("fixed_z", {}).get("highest_waypoint_z_m", fixed_z_m - CLEARANCE_ABOVE_PLATFORM_M)),
            "clearance_above_platform_m": CLEARANCE_ABOVE_PLATFORM_M,
            "fixed_base_z_m": fixed_z_m,
        },
        "basis": frame["basis"],
        "safe_frame": {
            "source": str(STEP6_SAFE_FRAME_PATH.relative_to(STEP6_SAFE_FRAME_PATH.parents[1])),
            "source_points_json": frame["source_points_json"],
            "max_residual_mm": frame["max_residual_mm"],
            "residual_gate_passed": frame["residual_gate_passed"],
        },
        "guard": {
            "guard_line_x_m": float(guard["guard_line_x_m"]),
            "path_max_x_m": max(row["base_x_m"] for row in rows),
            "guard_margin_after_m": float(guard["guard_line_x_m"]) - max(row["base_x_m"] for row in rows),
            "passed": max(row["base_x_m"] for row in rows) <= float(guard["guard_line_x_m"]) + 1e-12,
        },
        "envelope": {
            "base_x_min_m": min(row["base_x_m"] for row in rows),
            "base_x_max_m": max(row["base_x_m"] for row in rows),
            "base_y_min_m": min(row["base_y_m"] for row in rows),
            "base_y_max_m": max(row["base_y_m"] for row in rows),
            "local_x_min_m": min(row["local_x_m"] for row in rows),
            "local_x_max_m": max(row["local_x_m"] for row in rows),
            "local_y_min_m": min(row["local_y_m"] for row in rows),
            "local_y_max_m": max(row["local_y_m"] for row in rows),
        },
        "points": points,
        "samples": rows,
    }


def build_script(stamp: str, gen_at: str, geom: dict[str, float], metrics: dict) -> str:
    basis = metrics["basis"]
    origin_x, origin_y = [float(v) for v in basis["origin_xy_m"]]
    u_along_x, u_along_y = [float(v) for v in basis["u_along_xy"]]
    p_lateral_x, p_lateral_y = [float(v) for v in basis["p_lateral_xy"]]
    guard = metrics["guard"]
    fixed_z_m = float(metrics["fixed_z"]["fixed_base_z_m"])
    return f"""# Step6a 8-shaped no-contact fixed-Z rehearsal v1.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: {gen_at}
# CONTROLLER_TARGET: {CONTROLLER_DIR}/{PROGRAM_NAME}.urp
# FLOW_TABLE: STEP6_FLOW.md
# STEP6_STAGE_ID: {PROGRAM_NAME}
# STEP6_TABLE_SOURCE: config/step6_stage_table.json
# STEP6_SAFE_FRAME_SOURCE: config/step6_eight_safe_frame.json
# STEP6_WAYPOINTS: five-point read-only RTDE calibration; max residual {metrics['safe_frame']['max_residual_mm']:.6f} mm.
# ENTRY_XY_M: [{origin_x:.9f}, {origin_y:.9f}]
# FIXED_BASE_Z_M: {fixed_z_m:.9f}
# X_GUARD: guard_line={float(guard['guard_line_x_m']):.9f} m, path_max_x={float(guard['path_max_x_m']):.9f} m.
# STEP6_PATH_FORMULA: along=0.04*sin(0.2t), lateral=0.01*sin(0.4t), duration=30s.
# STEP6_TIMING: speedl hold_s=0.001 requests near-500Hz cadence; path clock uses get_steptime().
# NO_CONTACT_POLICY: fixed base Z, no force control, no contact search, no Kunwei/bridge requirement, no zero_ftsensor(), no TCP/payload write.

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

def codex_step6a_eight_no_contact_v1():
  local origin_x = {origin_x:.9f}
  local origin_y = {origin_y:.9f}
  local u_along_x = {u_along_x:.9f}
  local u_along_y = {u_along_y:.9f}
  local p_lateral_x = {p_lateral_x:.9f}
  local p_lateral_y = {p_lateral_y:.9f}
  local fixed_base_z_m = {fixed_z_m:.9f}
  local ref_rx = {geom['ref_rx']:.9f}
  local ref_ry = {geom['ref_ry']:.9f}
  local ref_rz = {geom['ref_rz']:.9f}
  local x_guard_m = {float(guard['guard_line_x_m']):.9f}
  local path_duration_s = {PATH_DURATION_S:.3f}
  local along_amplitude_m = {ALONG_AMPLITUDE_M:.9f}
  local lateral_amplitude_m = {LATERAL_AMPLITUDE_M:.9f}
  local omega_rad_s = {OMEGA_RAD_S:.9f}
  local warmup_hold_s = {WARMUP_HOLD_S:.3f}
  local fast_hold_s = {FAST_HOLD_S:.3f}
  local fast_after_s = {FAST_AFTER_S:.3f}
  local accel_m_s2 = 0.300
  local xy_p_gain_m_s_per_m = 2.0
  local z_p_gain_m_s_per_m = 2.0
  local correction_limit_m_s = 0.003
  local z_velocity_limit_m_s = 0.003
  local velocity_cap_m_s = {VELOCITY_CAP_M_S:.3f}
  local stop_reason = 0.0
  local t = 0.0

  textmsg("codex Step6a 8-shaped no-contact start {stamp}")
  local entry_pose = p[origin_x, origin_y, fixed_base_z_m, ref_rx, ref_ry, ref_rz]
  movel(entry_pose, a=0.030, v=0.020, r=0.0)
  stopl(0.5)

  while stop_reason == 0.0 and t < path_duration_s:
    local hold_s = warmup_hold_s
    if t >= fast_after_s:
      hold_s = fast_hold_s
    end
    local pose_now = get_actual_tcp_pose()
    local phase = omega_rad_s * t
    local local_x = along_amplitude_m * sin(phase)
    local local_y = lateral_amplitude_m * sin(2.0 * phase)
    local local_vx = along_amplitude_m * omega_rad_s * cos(phase)
    local local_vy = lateral_amplitude_m * 2.0 * omega_rad_s * cos(2.0 * phase)
    local desired_x = origin_x + local_x * u_along_x + local_y * p_lateral_x
    local desired_y = origin_y + local_x * u_along_y + local_y * p_lateral_y
    local desired_vx = local_vx * u_along_x + local_vy * p_lateral_x
    local desired_vy = local_vx * u_along_y + local_vy * p_lateral_y
    local correction_vx = codex_clamp(xy_p_gain_m_s_per_m * (desired_x - pose_now[0]), -correction_limit_m_s, correction_limit_m_s)
    local correction_vy = codex_clamp(xy_p_gain_m_s_per_m * (desired_y - pose_now[1]), -correction_limit_m_s, correction_limit_m_s)
    local correction_vz = codex_clamp(z_p_gain_m_s_per_m * (fixed_base_z_m - pose_now[2]), -z_velocity_limit_m_s, z_velocity_limit_m_s)
    local cmd_vx = desired_vx + correction_vx
    local cmd_vy = desired_vy + correction_vy
    local cmd_vz = correction_vz
    local cmd_norm = sqrt(cmd_vx * cmd_vx + cmd_vy * cmd_vy + cmd_vz * cmd_vz)
    if cmd_norm > velocity_cap_m_s:
      local cmd_scale = velocity_cap_m_s / cmd_norm
      cmd_vx = cmd_vx * cmd_scale
      cmd_vy = cmd_vy * cmd_scale
      cmd_vz = cmd_vz * cmd_scale
    end
    if desired_x > x_guard_m:
      stop_reason = 8.0
    elif pose_now[0] > x_guard_m:
      stop_reason = 9.0
    else:
      speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0], accel_m_s2, hold_s)
      t = t + get_steptime()
    end
  end
  speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], accel_m_s2, 0.010)
  stopl(0.5)
  textmsg("codex Step6a 8-shaped no-contact stop reason:", stop_reason)
end

codex_step6a_eight_no_contact_v1()
"""


def build_txt(stamp: str, metrics: dict) -> str:
    fixed_z_m = float(metrics["fixed_z"]["fixed_base_z_m"])
    return f"""Step6a 8-shaped no-contact TP package

Open on Teach Pendant:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Motion boundary:
  No-contact motion only.
  Fixed base Z: {fixed_z_m:.9f} m.
  Duration: {PATH_DURATION_S:.1f} s.
  Timing: speedl hold_s=0.001 requests near-500Hz cadence; path time advances with get_steptime().
  Velocity cap: {VELOCITY_CAP_M_S:.3f} m/s.
  No force control, no contact search, no Kunwei bridge, no zero_ftsensor(), no TCP/payload write.

Reference:
  STEP6_FLOW.md
  config/step6_stage_table.json
  config/step6_eight_safe_frame.json

Path:
  along=0.04*sin(0.2t), lateral=0.01*sin(0.4t)
  five-waypoint max residual: {metrics['safe_frame']['max_residual_mm']:.6f} mm
  max base X: {metrics['envelope']['base_x_max_m']:.10f} m
  X guard: {metrics['guard']['guard_line_x_m']:.10f} m
"""


def build_urp(script: str, name: str, controller_dir: str) -> bytes:
    controller_script = f"{controller_dir}/{name}.script"
    install_rel = installation_relative_path(controller_dir)
    xml = gzip.decompress(TEMPLATE_URP.read_bytes()).decode("utf-8")
    xml = re.sub(r'<URProgram name="[^"]+"', f'<URProgram name="{name}"', xml, count=1)
    xml = re.sub(r'directory="[^"]+"', f'directory="{controller_dir}"', xml, count=1)
    xml = re.sub(r'installationRelativePath="[^"]+"', f'installationRelativePath="{install_rel}"', xml, count=1)
    xml = re.sub(r'<cachedContents>.*?</cachedContents>', f"<cachedContents>{html.escape(script)}</cachedContents>", xml, count=1, flags=re.S)
    xml = re.sub(r'<file resolves-to="file">.*?</file>', f'<file resolves-to="file">{controller_script}</file>', xml, count=1, flags=re.S)
    return gzip.compress(xml.encode("utf-8"))


def validate_package(script: str, txt: str, urp: bytes, stamp: str, metrics: dict) -> None:
    xml = gzip.decompress(urp).decode("utf-8")
    fixed_z_m = float(metrics["fixed_z"]["fixed_base_z_m"])
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": f'URProgram name="{PROGRAM_NAME}"' in xml,
        "controller directory": f'directory="{CONTROLLER_DIR}"' in xml,
        "installation path": f'installationRelativePath="{installation_relative_path(CONTROLLER_DIR)}"' in xml,
        "script file": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script" in xml,
        "cached stamp": stamp in xml,
        "step6 flow": "STEP6_FLOW.md" in script and "STEP6_FLOW.md" in txt,
        "step6 safe frame": "STEP6_SAFE_FRAME_SOURCE: config/step6_eight_safe_frame.json" in script,
        "step6 waypoints": "STEP6_WAYPOINTS" in script,
        "function name": "def codex_step6a_eight_no_contact_v1()" in script,
        "fixed z": f"local fixed_base_z_m = {fixed_z_m:.9f}" in script,
        "path duration": f"local path_duration_s = {PATH_DURATION_S:.3f}" in script,
        "path clock": "STEP6_TIMING" in script and "t = t + get_steptime()" in script,
        "formula": "along=0.04*sin(0.2t)" in script and "lateral=0.01*sin(0.4t)" in script,
        "no contact": "no force control" in script and "no contact search" in script,
        "no bridge": "no Kunwei/bridge requirement" in script,
        "no rtde input dependency": "read_input_float_register" not in script,
        "no zero": "zero_ftsensor" not in script.replace("no zero_ftsensor()", ""),
        "no tcp payload": "set_tcp" not in script and "set_payload" not in script,
        "velocity cap": f"local velocity_cap_m_s = {VELOCITY_CAP_M_S:.3f}" in script,
        "guard passed": metrics["guard"]["passed"],
        "safe-frame residual passed": metrics["safe_frame"]["residual_gate_passed"],
        "no stale step4": "step4g_eight_seed_normal_v1" not in script and "/step4/" not in script,
    }
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"{PROGRAM_NAME} validation failed: {failed}")


def write_outputs(stamp: str, gen_at: str) -> dict[str, str]:
    frame = load_safe_frame()
    metrics = build_metrics(frame)
    geom = line_cfg(load_json(CONFIG_PATH))
    script = build_script(stamp, gen_at, geom, metrics)
    txt = build_txt(stamp, metrics)
    urp = build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    validate_package(script, txt, urp, stamp, metrics)

    LOCAL_PROGRAM_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    script_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.script"
    txt_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.txt"
    urp_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.urp"
    metrics_path = REPORT_DIR / "step6a_eight_no_contact_metrics.json"

    script_path.write_text(script, encoding="utf-8")
    txt_path.write_text(txt, encoding="utf-8")
    urp_path.write_bytes(urp)
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "script": str(script_path),
        "txt": str(txt_path),
        "urp": str(urp_path),
        "metrics": str(metrics_path),
        "controller_urp": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "stamp": stamp,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp-prefix", default=None)
    args = parser.parse_args()
    now = datetime.now(timezone(timedelta(hours=8)))
    stamp = args.stamp_prefix or source_stamp(now)
    result = write_outputs(stamp, generated_at(now))
    print(json.dumps({"generated": {PROGRAM_NAME: result}}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Step6a package build blocked: {exc}")
        raise SystemExit(2)
