#!/usr/bin/env python3
"""Generate Step5a no-contact cycloid TP package and XY overview."""

from __future__ import annotations

import argparse
import gzip
import html
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from build_step4e_line_programs import CONFIG_PATH, PROGRAM_DIR, TEMPLATE_URP, installation_relative_path, line_cfg, load_json
from step5_table import active_no_contact_stage, load_stage_frame


STEP5_STAGE = active_no_contact_stage()
PROGRAM_NAME = STEP5_STAGE["id"]
LOCAL_PROGRAM_DIR = PROGRAM_DIR / "step5"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
REPORT_DIR = Path("/home/andy/ur10e_ros2_ws/report/assets/step5a-cycloid-no-contact")
SAFE_FRAME_PATH = CONFIG_PATH.with_name("step5_safe_frame.json")

PATH_DURATION_S = float(STEP5_STAGE["duration_s"])
AMPLITUDE_M = float(STEP5_STAGE["amplitude_m"])
OMEGA_RAD_S = float(STEP5_STAGE["phase_law"]["omega_rad_s"])
FINAL_PHASE_RAD = float(STEP5_STAGE["phase_law"]["final_phase_rad"])
WARMUP_HOLD_S = float(STEP5_STAGE["cadence"]["warmup_hold_s"])
FAST_HOLD_S = float(STEP5_STAGE["cadence"]["fast_hold_s"])
FAST_AFTER_S = float(STEP5_STAGE["cadence"]["fast_after_s"])
HIGHEST_TAUGHT_Z_M = 0.019423891
CLEARANCE_ABOVE_PLATFORM_M = 0.010
FIXED_BASE_Z_M = float(STEP5_STAGE["fixed_base_z_m"])
SAMPLE_DT_S = 0.1


def fmt(value: float) -> str:
    return f"{value:.9f}".rstrip("0").rstrip(".")


def generated_at(now: datetime) -> str:
    return now.isoformat(timespec="seconds")


def source_stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H%MHKT_STEP5A_CYCLOID_NO_CONTACT_V3")


def load_safe_frame() -> dict:
    return load_stage_frame(STEP5_STAGE)


def cycloid_local(t_s: float) -> tuple[float, float, float, float]:
    phase = OMEGA_RAD_S * t_s
    x = AMPLITUDE_M * (phase - math.sin(phase))
    y = AMPLITUDE_M * (1.0 - math.cos(phase))
    vx = AMPLITUDE_M * OMEGA_RAD_S * (1.0 - math.cos(phase))
    vy = AMPLITUDE_M * OMEGA_RAD_S * math.sin(phase)
    return x, y, vx, vy


def shifted_taught_targets(frame: dict) -> dict[str, dict[str, float]]:
    points = frame["points"]
    safe_origin_x, safe_origin_y = [float(v) for v in frame["basis"]["origin_xy_m"]]
    taught_start_x, taught_start_y = [float(v) for v in points["start"]["taught_base_xyz_m"][:2]]
    offset_x = safe_origin_x - taught_start_x
    offset_y = safe_origin_y - taught_start_y
    targets = {}
    for name in ("start", "mid", "end"):
        local_x, local_y = [float(v) for v in points[name]["paper_local_xy_m"]]
        taught_x, taught_y = [float(v) for v in points[name]["taught_base_xyz_m"][:2]]
        targets[name] = {
            "local_x_m": local_x,
            "local_y_m": local_y,
            "taught_base_x_m": taught_x,
            "taught_base_y_m": taught_y,
            "target_base_x_m": taught_x + offset_x,
            "target_base_y_m": taught_y + offset_y,
        }
    targets["offset"] = {
        "base_x_m": offset_x,
        "base_y_m": offset_y,
    }
    return targets


def affine_path_map(frame: dict) -> dict[str, float | dict]:
    targets = shifted_taught_targets(frame)
    p0 = targets["start"]
    p1 = targets["mid"]
    p2 = targets["end"]
    dx1 = p1["local_x_m"] - p0["local_x_m"]
    dy1 = p1["local_y_m"] - p0["local_y_m"]
    dx2 = p2["local_x_m"] - p0["local_x_m"]
    dy2 = p2["local_y_m"] - p0["local_y_m"]
    det = dx1 * dy2 - dx2 * dy1
    if abs(det) < 1e-12:
        raise RuntimeError("Step5a local taught points are degenerate")

    bx1 = p1["target_base_x_m"] - p0["target_base_x_m"]
    bx2 = p2["target_base_x_m"] - p0["target_base_x_m"]
    by1 = p1["target_base_y_m"] - p0["target_base_y_m"]
    by2 = p2["target_base_y_m"] - p0["target_base_y_m"]
    m_xx = (bx1 * dy2 - bx2 * dy1) / det
    m_xy = (dx1 * bx2 - dx2 * bx1) / det
    m_yx = (by1 * dy2 - by2 * dy1) / det
    m_yy = (dx1 * by2 - dx2 * by1) / det
    origin_x = p0["target_base_x_m"] - m_xx * p0["local_x_m"] - m_xy * p0["local_y_m"]
    origin_y = p0["target_base_y_m"] - m_yx * p0["local_x_m"] - m_yy * p0["local_y_m"]
    return {
        "origin_x_m": origin_x,
        "origin_y_m": origin_y,
        "m_xx": m_xx,
        "m_xy": m_xy,
        "m_yx": m_yx,
        "m_yy": m_yy,
        "targets": targets,
    }


def transform_xy(path_map: dict, local_x: float, local_y: float) -> tuple[float, float]:
    return (
        float(path_map["origin_x_m"]) + local_x * float(path_map["m_xx"]) + local_y * float(path_map["m_xy"]),
        float(path_map["origin_y_m"]) + local_x * float(path_map["m_yx"]) + local_y * float(path_map["m_yy"]),
    )


def transform_vxy(path_map: dict, local_vx: float, local_vy: float) -> tuple[float, float]:
    return (
        local_vx * float(path_map["m_xx"]) + local_vy * float(path_map["m_xy"]),
        local_vx * float(path_map["m_yx"]) + local_vy * float(path_map["m_yy"]),
    )


def samples(frame: dict) -> list[dict[str, float]]:
    rows = []
    path_map = affine_path_map(frame)
    sample_count = int(round(PATH_DURATION_S / SAMPLE_DT_S))
    for idx in range(sample_count + 1):
        t_s = round(idx * SAMPLE_DT_S, 10)
        local_x, local_y, local_vx, local_vy = cycloid_local(t_s)
        base_x, base_y = transform_xy(path_map, local_x, local_y)
        base_vx, base_vy = transform_vxy(path_map, local_vx, local_vy)
        rows.append(
            {
                "t_s": t_s,
                "local_x_m": local_x,
                "local_y_m": local_y,
                "local_vx_m_s": local_vx,
                "local_vy_m_s": local_vy,
                "base_vx_m_s": base_vx,
                "base_vy_m_s": base_vy,
                "base_x_m": base_x,
                "base_y_m": base_y,
                "base_z_m": FIXED_BASE_Z_M,
            }
        )
    return rows


def build_metrics(frame: dict) -> dict:
    rows = samples(frame)
    guard = frame["guard"]
    path_map = affine_path_map(frame)
    target_residuals = {}
    for name in ("start", "mid", "end"):
        target = path_map["targets"][name]
        pred_x, pred_y = transform_xy(path_map, target["local_x_m"], target["local_y_m"])
        err_x = target["target_base_x_m"] - pred_x
        err_y = target["target_base_y_m"] - pred_y
        target_residuals[name] = {
            "base_x_mm": err_x * 1000.0,
            "base_y_mm": err_y * 1000.0,
            "base_xy_norm_mm": math.hypot(err_x, err_y) * 1000.0,
        }
    points = {
        "start": rows[0],
        "mid": rows[len(rows) // 2],
        "end": rows[-1],
    }
    return {
        "program": PROGRAM_NAME,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "policy": {
            "stage": "Step5a",
            "contact": "no-contact",
            "force_control": False,
            "contact_search": False,
            "kunwei_bridge_required": False,
            "zero_ftsensor": False,
            "tcp_payload_write": False,
            "paper_local_axes_drawn_in_overview": False,
        },
        "path": {
            "formula": f"x=0.015({OMEGA_RAD_S:.6f}t-sin({OMEGA_RAD_S:.6f}t)), y=0.015(1-cos({OMEGA_RAD_S:.6f}t))",
            "duration_s": PATH_DURATION_S,
            "amplitude_m": AMPLITUDE_M,
            "omega_rad_s": OMEGA_RAD_S,
            "final_phase_rad": OMEGA_RAD_S * PATH_DURATION_S,
            "geometry_map": "affine_map_exact_to_shifted_drag_teach_start_mid_end",
            "max_reference_speed_m_s": max(math.hypot(row["base_vx_m_s"], row["base_vy_m_s"]) for row in rows),
        },
        "cadence": STEP5_STAGE["cadence"],
        "fixed_z": {
            "highest_taught_z_m": HIGHEST_TAUGHT_Z_M,
            "clearance_above_platform_m": CLEARANCE_ABOVE_PLATFORM_M,
            "fixed_base_z_m": FIXED_BASE_Z_M,
        },
        "basis": frame["basis"],
        "physical_path_gate": {
            "target": "drag-teach start/mid/end with the same safe-start offset used by Step5a v2",
            "target_offset_from_taught_m": path_map["targets"]["offset"],
            "affine_map": {k: path_map[k] for k in ("origin_x_m", "origin_y_m", "m_xx", "m_xy", "m_yx", "m_yy")},
            "target_points": {k: path_map["targets"][k] for k in ("start", "mid", "end")},
            "target_residuals_mm": target_residuals,
            "max_target_residual_mm": max(v["base_xy_norm_mm"] for v in target_residuals.values()),
        },
        "guard": {
            "guard_line_x_m": float(guard["guard_line_x_m"]),
            "path_max_x_m": max(row["base_x_m"] for row in rows),
            "source_path_max_x_m": float(guard["path_max_x_m"]),
            "passed": max(row["base_x_m"] for row in rows) <= float(guard["guard_line_x_m"]) + 1e-12,
        },
        "envelope": {
            "base_x_min_m": min(row["base_x_m"] for row in rows),
            "base_x_max_m": max(row["base_x_m"] for row in rows),
            "base_y_min_m": min(row["base_y_m"] for row in rows),
            "base_y_max_m": max(row["base_y_m"] for row in rows),
            "local_x_max_m": max(row["local_x_m"] for row in rows),
            "local_y_max_m": max(row["local_y_m"] for row in rows),
        },
        "points": points,
        "samples": rows,
    }


def build_script(stamp: str, gen_at: str, geom: dict[str, float], metrics: dict) -> str:
    guard = metrics["guard"]
    path_map = metrics["physical_path_gate"]["affine_map"]
    origin_x = float(path_map["origin_x_m"])
    origin_y = float(path_map["origin_y_m"])
    m_xx = float(path_map["m_xx"])
    m_xy = float(path_map["m_xy"])
    m_yx = float(path_map["m_yx"])
    m_yy = float(path_map["m_yy"])
    return f"""# Step5a cycloid no-contact fixed-Z rehearsal v3.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: {gen_at}
# CONTROLLER_TARGET: {CONTROLLER_DIR}/{PROGRAM_NAME}.urp
# ENTRY_XY_M: [{origin_x:.9f}, {origin_y:.9f}]
# FIXED_BASE_Z_M: {FIXED_BASE_Z_M:.9f}
# CLEARANCE_POLICY: highest taught Z {HIGHEST_TAUGHT_Z_M:.9f} m + 0.010000000 m; no contact search.
# SAFE_FRAME_SOURCE: config/step5_safe_frame.json from confirmed drag-teach start/mid/end.
# PHYSICAL_PATH_GATE: affine map exactly matches shifted drag-teach start/mid/end; max target residual {metrics['physical_path_gate']['max_target_residual_mm']:.6f} mm.
# X_GUARD: max_base_x <= 0.4888784335 m; generated path max_base_x={metrics['envelope']['base_x_max_m']:.10f} m.
# PAPER_PATH_FORMULA: x=0.015({OMEGA_RAD_S:.6f}t-sin({OMEGA_RAD_S:.6f}t)), y=0.015(1-cos({OMEGA_RAD_S:.6f}t)), duration={PATH_DURATION_S:.1f} s, final phase 6 rad.
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

def codex_step5a_cycloid_no_contact_v3():
  local origin_x = {origin_x:.9f}
  local origin_y = {origin_y:.9f}
  local map_xx = {m_xx:.9f}
  local map_xy = {m_xy:.9f}
  local map_yx = {m_yx:.9f}
  local map_yy = {m_yy:.9f}
  local fixed_base_z_m = {FIXED_BASE_Z_M:.9f}
  local ref_rx = {geom['ref_rx']:.9f}
  local ref_ry = {geom['ref_ry']:.9f}
  local ref_rz = {geom['ref_rz']:.9f}
  local x_guard_m = {float(guard['guard_line_x_m']):.10f}
  local path_duration_s = {PATH_DURATION_S:.3f}
  local amplitude_m = {AMPLITUDE_M:.9f}
  local omega_rad_s = {OMEGA_RAD_S:.9f}
  local warmup_hold_s = {WARMUP_HOLD_S:.3f}
  local fast_hold_s = {FAST_HOLD_S:.3f}
  local fast_after_s = {FAST_AFTER_S:.3f}
  local accel_m_s2 = 0.300
  local xy_p_gain_m_s_per_m = 2.0
  local z_p_gain_m_s_per_m = 2.0
  local correction_limit_m_s = 0.003
  local z_velocity_limit_m_s = 0.003
  local velocity_cap_m_s = {float(STEP5_STAGE['guard']['velocity_cap_m_s']):.3f}
  local stop_reason = 0.0
  local t = 0.0

  textmsg("codex Step5a no-contact cycloid start {stamp}")
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
    local s = sin(phase)
    local c = cos(phase)
    local local_x = amplitude_m * (phase - s)
    local local_y = amplitude_m * (1.0 - c)
    local local_vx = amplitude_m * omega_rad_s * (1.0 - c)
    local local_vy = amplitude_m * omega_rad_s * s
    local desired_x = origin_x + local_x * map_xx + local_y * map_xy
    local desired_y = origin_y + local_x * map_yx + local_y * map_yy
    local desired_vx = local_vx * map_xx + local_vy * map_xy
    local desired_vy = local_vx * map_yx + local_vy * map_yy
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
      t = t + hold_s
    end
  end
  speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], accel_m_s2, 0.010)
  stopl(0.5)
  textmsg("codex Step5a no-contact cycloid stop reason:", stop_reason)
end

codex_step5a_cycloid_no_contact_v3()
"""


def build_txt(stamp: str, metrics: dict) -> str:
    return f"""Step5a no-contact cycloid TP package

Open on Teach Pendant:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Motion boundary:
  No-contact motion only.
  Fixed base Z: {FIXED_BASE_Z_M:.9f} m.
  Duration: {PATH_DURATION_S:.1f} s.
  Velocity cap: {float(STEP5_STAGE['guard']['velocity_cap_m_s']):.3f} m/s.
  No force control, no contact search, no Kunwei bridge, no zero_ftsensor(), no TCP/payload write.

Path:
  x=0.015({OMEGA_RAD_S:.6f}t-sin({OMEGA_RAD_S:.6f}t)), y=0.015(1-cos({OMEGA_RAD_S:.6f}t))
  physical path gate: shifted drag-teach start/mid/end max residual {metrics['physical_path_gate']['max_target_residual_mm']:.6f} mm
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


def build_html(metrics: dict, stamp: str) -> str:
    rows = metrics["samples"]
    points = metrics["points"]
    payload = {
        "samples": [{"x": row["base_x_m"], "y": row["base_y_m"], "t": row["t_s"]} for row in rows],
        "points": {
            name: {"x": row["base_x_m"], "y": row["base_y_m"], "t": row["t_s"]}
            for name, row in points.items()
        },
        "fixedZ": FIXED_BASE_Z_M,
        "xGuard": metrics["guard"]["guard_line_x_m"],
        "program": PROGRAM_NAME,
        "stamp": stamp,
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Step5a XY Overview</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #18212f;
      --muted: #5b6675;
      --grid: #d9e0e8;
      --path: #0b6bcb;
      --accent: #b42318;
      --axis: #1f7a4d;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: #f7f9fb;
      color: var(--ink);
    }}
    main {{
      width: min(1120px, 100%);
      margin: 0 auto;
      padding: 24px;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 28px;
      line-height: 1.15;
      letter-spacing: 0;
    }}
    .subtitle {{
      margin: 0 0 18px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.45;
    }}
    .layout {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 280px;
      gap: 18px;
      align-items: start;
    }}
    .panel {{
      background: #fff;
      border: 1px solid #dce3ea;
      border-radius: 8px;
      overflow: hidden;
    }}
    svg {{
      display: block;
      width: 100%;
      height: auto;
      aspect-ratio: 1.2;
      background: #fbfcfe;
    }}
    .facts {{
      padding: 16px;
      display: grid;
      gap: 12px;
    }}
    .fact-label {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.2;
    }}
    .fact-value {{
      margin-top: 3px;
      font-size: 15px;
      line-height: 1.35;
      word-break: break-word;
    }}
    @media (max-width: 760px) {{
      main {{ padding: 14px; }}
      h1 {{ font-size: 22px; }}
      .layout {{ grid-template-columns: 1fr; }}
      .facts {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 460px) {{
      .facts {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Step5a XY Overview</h1>
    <p class="subtitle">UR base frame top view. The cycloid is shown in base XY only; paper-local axes are intentionally omitted.</p>
    <div class="layout">
      <section class="panel">
        <svg id="overview" viewBox="0 0 960 800" role="img" aria-label="UR base frame XY overview for Step5a cycloid"></svg>
      </section>
      <aside class="panel facts">
        <div><div class="fact-label">Program</div><div class="fact-value">{PROGRAM_NAME}</div></div>
        <div><div class="fact-label">Fixed base Z</div><div class="fact-value">{FIXED_BASE_Z_M:.9f} m</div></div>
        <div><div class="fact-label">X guard</div><div class="fact-value">{metrics['guard']['guard_line_x_m']:.10f} m</div></div>
        <div><div class="fact-label">Max base X</div><div class="fact-value">{metrics['envelope']['base_x_max_m']:.10f} m</div></div>
        <div><div class="fact-label">Duration</div><div class="fact-value">{PATH_DURATION_S:.1f} s</div></div>
        <div><div class="fact-label">Target residual</div><div class="fact-value">{metrics['physical_path_gate']['max_target_residual_mm']:.6f} mm</div></div>
      </aside>
    </div>
  </main>
  <script>
    const data = {payload_json};
    const svg = document.getElementById("overview");
    const ns = "http://www.w3.org/2000/svg";
    const W = 960, H = 800;
    const pad = 86;
    const xs = data.samples.map(p => p.x).concat([0, data.xGuard]);
    const ys = data.samples.map(p => p.y).concat([0]);
    const minX = Math.min(...xs) - 0.035;
    const maxX = Math.max(...xs) + 0.025;
    const minY = Math.min(...ys) - 0.035;
    const maxY = Math.max(...ys) + 0.035;
    const sx = x => pad + (x - minX) / (maxX - minX) * (W - 2 * pad);
    const sy = y => H - pad - (y - minY) / (maxY - minY) * (H - 2 * pad);
    function el(tag, attrs, text) {{
      const node = document.createElementNS(ns, tag);
      for (const [k, v] of Object.entries(attrs || {{}})) node.setAttribute(k, v);
      if (text !== undefined) node.textContent = text;
      svg.appendChild(node);
      return node;
    }}
    el("defs", {{}}).innerHTML = `
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#1f7a4d"></path>
      </marker>
      <marker id="arrow-red" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#b42318"></path>
      </marker>`;
    for (let x = Math.ceil(minX * 20) / 20; x <= maxX; x += 0.05) {{
      el("line", {{x1: sx(x), y1: sy(minY), x2: sx(x), y2: sy(maxY), stroke: "#d9e0e8", "stroke-width": 1}});
      el("text", {{x: sx(x) + 3, y: H - 26, fill: "#687487", "font-size": 12}}, x.toFixed(2));
    }}
    for (let y = Math.ceil(minY * 20) / 20; y <= maxY; y += 0.05) {{
      el("line", {{x1: sx(minX), y1: sy(y), x2: sx(maxX), y2: sy(y), stroke: "#d9e0e8", "stroke-width": 1}});
      el("text", {{x: 14, y: sy(y) - 4, fill: "#687487", "font-size": 12}}, y.toFixed(2));
    }}
    el("line", {{x1: sx(0), y1: sy(0), x2: sx(0.085), y2: sy(0), stroke: "#1f7a4d", "stroke-width": 4, "marker-end": "url(#arrow)"}});
    el("line", {{x1: sx(0), y1: sy(0), x2: sx(0), y2: sy(0.085), stroke: "#1f7a4d", "stroke-width": 4, "marker-end": "url(#arrow)"}});
    el("circle", {{cx: sx(0), cy: sy(0), r: 6, fill: "#18212f"}});
    el("text", {{x: sx(0) + 10, y: sy(0) + 18, fill: "#18212f", "font-size": 15, "font-weight": 700}}, "UR base origin (0,0)");
    el("text", {{x: sx(0.087), y: sy(0) - 10, fill: "#1f7a4d", "font-size": 18, "font-weight": 700}}, "+X");
    el("text", {{x: sx(0) + 10, y: sy(0.087), fill: "#1f7a4d", "font-size": 18, "font-weight": 700}}, "+Y");
    el("line", {{x1: sx(data.xGuard), y1: sy(minY), x2: sx(data.xGuard), y2: sy(maxY), stroke: "#b42318", "stroke-width": 2, "stroke-dasharray": "8 6"}});
    el("text", {{x: sx(data.xGuard) - 132, y: sy(maxY) + 22, fill: "#b42318", "font-size": 14, "font-weight": 700}}, "X guard " + data.xGuard.toFixed(10) + " m");
    const pathD = data.samples.map((p, i) => (i ? "L" : "M") + sx(p.x).toFixed(2) + "," + sy(p.y).toFixed(2)).join(" ");
    el("path", {{d: pathD, fill: "none", stroke: "#0b6bcb", "stroke-width": 5, "stroke-linecap": "round", "stroke-linejoin": "round"}});
    for (const [name, p] of Object.entries(data.points)) {{
      const color = name === "end" ? "#b42318" : name === "mid" ? "#7a4cc2" : "#0b6bcb";
      el("circle", {{cx: sx(p.x), cy: sy(p.y), r: 7, fill: color, stroke: "#fff", "stroke-width": 2}});
      el("text", {{x: sx(p.x) + 10, y: sy(p.y) - 10, fill: color, "font-size": 14, "font-weight": 700}}, `${{name}} t=${{p.t.toFixed(0)}}s`);
    }}
    const labelPoint = data.samples[Math.floor(data.samples.length * 0.7)];
    el("text", {{x: sx(labelPoint.x) + 18, y: sy(labelPoint.y) + 30, fill: "#0b6bcb", "font-size": 16, "font-weight": 700}}, "Step5a cycloid in base XY");
    el("text", {{x: W - 310, y: H - 36, fill: "#18212f", "font-size": 14}}, "fixed Z = " + data.fixedZ.toFixed(9) + " m");
  </script>
</body>
</html>
"""


def validate_package(script: str, txt: str, urp: bytes, stamp: str, metrics: dict) -> None:
    xml = gzip.decompress(urp).decode("utf-8")
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": f'URProgram name="{PROGRAM_NAME}"' in xml,
        "controller directory": f'directory="{CONTROLLER_DIR}"' in xml,
        "installation path": f'installationRelativePath="{installation_relative_path(CONTROLLER_DIR)}"' in xml,
        "script file": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script" in xml,
        "cached stamp": stamp in xml,
        "fixed z": f"local fixed_base_z_m = {FIXED_BASE_Z_M:.9f}" in script,
        "path duration": f"local path_duration_s = {PATH_DURATION_S:.3f}" in script,
        "final phase": abs(metrics["path"]["final_phase_rad"] - FINAL_PHASE_RAD) < 1e-12,
        "warmup hold": f"local warmup_hold_s = {WARMUP_HOLD_S:.3f}" in script,
        "fast hold": f"local fast_hold_s = {FAST_HOLD_S:.3f}" in script,
        "cadence switch": f"local fast_after_s = {FAST_AFTER_S:.3f}" in script and "if t >= fast_after_s:" in script,
        "fast cycloid formula": f"x=0.015({OMEGA_RAD_S:.6f}t-sin({OMEGA_RAD_S:.6f}t))" in script and f"y=0.015(1-cos({OMEGA_RAD_S:.6f}t))" in script,
        "physical path gate": "PHYSICAL_PATH_GATE" in script and metrics["physical_path_gate"]["max_target_residual_mm"] <= 1e-6,
        "no contact": "no force control" in script and "no contact search" in script,
        "no bridge": "no Kunwei/bridge requirement" in script,
        "no rtde input dependency": "read_input_float_register" not in script,
        "no zero": "zero_ftsensor" not in script.replace("no zero_ftsensor()", ""),
        "velocity cap": "local velocity_cap_m_s = 0.009" in script,
        "x guard": metrics["guard"]["passed"] and metrics["envelope"]["base_x_max_m"] <= 0.4888784335,
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
    metrics_path = REPORT_DIR / "step5a_cycloid_no_contact_metrics.json"
    html_path = REPORT_DIR / "step5a_xy_overview.html"

    script_path.write_text(script, encoding="utf-8")
    txt_path.write_text(txt, encoding="utf-8")
    urp_path.write_bytes(urp)
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    html_path.write_text(build_html(metrics, stamp), encoding="utf-8")
    return {
        "script": str(script_path),
        "txt": str(txt_path),
        "urp": str(urp_path),
        "metrics": str(metrics_path),
        "html": str(html_path),
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
    raise SystemExit(main())
