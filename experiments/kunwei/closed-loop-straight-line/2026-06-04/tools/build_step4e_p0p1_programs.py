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

V13_FIRST_CONTACT_Z_M = 0.008044839
V13_FIRST_CONTACT_SOURCE = "bridge_step4e_line_outerloop_v13_autowatch_20260609_141027"
V16_FIRST_CONTACT_Z_M = 0.007996174
V27_FIRST_CONTACT_Z_M = V13_FIRST_CONTACT_Z_M
V27_FIRST_CONTACT_BELOW_MARGIN_M = 0.004
V28_FIRST_CONTACT_Z_M = V13_FIRST_CONTACT_Z_M
V28_FIRST_CONTACT_BELOW_MARGIN_M = 0.004
V28_FIRST_CONTACT_NEAR_MARGIN_M = 0.025
V28_FIRST_SEARCH_NEAR_SPEED_M_S = -0.0025
V28_RAW_NORMAL_GUARD_N = 50.0
V29_FIRST_CONTACT_Z_M = V28_FIRST_CONTACT_Z_M
V29_FIRST_CONTACT_BELOW_MARGIN_M = V28_FIRST_CONTACT_BELOW_MARGIN_M
V29_FIRST_CONTACT_NEAR_MARGIN_M = 0.020
V29_FIRST_SEARCH_NEAR_SPEED_M_S = V28_FIRST_SEARCH_NEAR_SPEED_M_S
V29_RAW_NORMAL_GUARD_N = V28_RAW_NORMAL_GUARD_N
V30_FIRST_CONTACT_Z_M = V29_FIRST_CONTACT_Z_M
V30_FIRST_CONTACT_BELOW_MARGIN_M = V29_FIRST_CONTACT_BELOW_MARGIN_M
V30_FIRST_CONTACT_NEAR_MARGIN_M = V29_FIRST_CONTACT_NEAR_MARGIN_M
V30_FIRST_SEARCH_NEAR_SPEED_M_S = V29_FIRST_SEARCH_NEAR_SPEED_M_S
V30_RAW_NORMAL_GUARD_N = V29_RAW_NORMAL_GUARD_N


def default_pose_pair_path() -> Path:
    tmp_hint = Path("/tmp/base_y_drag_pose_pair_dir.txt")
    if tmp_hint.exists():
        hinted = Path(tmp_hint.read_text(encoding="utf-8").strip()) / "pose_pair_delta.json"
        if hinted.exists():
            return hinted
    runs_dir = PROGRAM_DIR.parent / "runs"
    candidates = sorted(runs_dir.glob("base_y_drag_pose_pair_*/pose_pair_delta.json"))
    if not candidates:
        raise FileNotFoundError("cannot find base_y_drag_pose_pair_*/pose_pair_delta.json")
    return candidates[-1]


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

Canonical flow:
  STEP4E_FLOW.md
"""


def program_default_subdir(name: str) -> str:
    """Return the default Step4 placement subdir for current vs archived packages."""
    if name in {
        "step4e_detached_movel_minrot_v21",
        "step4e_seed_normal_loop_v22",
        "step4e_seed_normal_loop_v23",
        "step4e_seed_normal_loop_v24",
        "step4e_seed_normal_loop_v25",
        "step4e_seed_normal_loop_v26",
        "step4e_seed_normal_loop_v27",
    }:
        return "step4e"
    return ""


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
        "guard function": "codex_step4e_guard_stop_reason()" in script,
        "fast stop": "stopl(0.1)" in script,
    }
    if name not in {"step4e_ball_first_contact_p0_v1", "step4e_ball_vs_cyl_contact_p0_v1"}:
        checks["reads rtde command registers"] = "read_input_float_register(37)" in script
    if name == "step4e_attitude_axis_iso_v1":
        checks.update(
            {
                "raw normal guard 35n": "codex_abs(normal_force) > 35.0" in script,
                "four quadrant stages": all(f"25.{suffix}" in script for suffix in ("21", "22", "23", "24")),
                "full angular speedl": "speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz]" in script,
                "linear command reject": "codex_abs(cmd_vx) > 0.001" in script,
            }
        )
    if name == "step4e_ball_first_contact_p0_v1":
        checks.update(
            {
                "raw normal guard 35n": "codex_abs(normal_force) > 35.0" in script,
                "two-stage search depths": "local search_near_start_depth_m = 0.080" in script
                and "local max_search_down_m = 0.092" in script,
                "far search 15mm/s": "local search_far_speed_m_s = -0.015" in script,
                "near search 3mm/s": "local search_near_speed_m_s = -0.003" in script,
                "touch witness stage": "write_output_float_register(35, 25.05)" in script,
                "visual dwell": "local visual_dwell_s = 8.000" in script,
                "base z retract": "write_output_float_register(35, 25.06)" in script,
                "no attitude command": "cmd_wx" not in script
                and "cmd_wy" not in script
                and "cmd_wz" not in script
                and "speedl([0.0, 0.0, 0.0, read_input_float_register(40)" not in script,
                "no force acquire": "write_output_float_register(35, 25.3)" not in script,
                "no line stage": "write_output_float_register(35, 25.0)" not in script,
            }
        )
    if name == "step4e_ball_vs_cyl_contact_p0_v1":
        checks.update(
            {
                "raw normal guard 35n": "codex_abs(normal_force) > 35.0" in script,
                "ball stage": "witness ball" in script,
                "cyl stage": "witness cyl" in script,
                "visual dwell": "local visual_dwell_s = 8.000" in script,
                "near search 3mm/s": "local search_near_speed_m_s = -0.003" in script,
                "ball far search": "local search_far_speed_m_s = -0.015" in script,
                "no attitude command": "cmd_wx" not in script and "cmd_wy" not in script and "cmd_wz" not in script,
                "no force acquire": "write_output_float_register(35, 25.3)" not in script,
                "no line stage": "write_output_float_register(35, 25.0)" not in script,
            }
        )
    if name == "step4e_detached_movel_minrot_v21":
        checks.update(
            {
                "latch stage": "write_output_float_register(35, 25.05)" in script,
                "detach stage": "write_output_float_register(35, 25.1)" in script,
                "movel stage": "write_output_float_register(35, 25.2)" in script,
                "raw normal guard 35n": "codex_abs(normal_force) > 35.0" in script,
                "two-stage search depths": "local search_near_start_depth_m = 0.080" in script
                and "local max_search_down_m = 0.092" in script,
                "far search 15mm/s": "local search_far_speed_m_s = -0.015" in script,
                "near search 3mm/s": "local search_near_speed_m_s = -0.003" in script,
                "near search stage": "write_output_float_register(35, 24.2)" in script,
                "no force acquire": "write_output_float_register(35, 25.3)" not in script,
                "no line stage": "write_output_float_register(35, 25.0)" not in script,
                "locked-normal detach semantics": "+locked-normal detach direction" in script,
                "two millimeter detach cap": "detach_total_m < 0.002" in script,
                "target rotvec preview": "25.2 is target-preview only; no contact-posture movel" in script,
                "no contact-posture movel": "movel(target_pose, a=0.030, v=0.010, r=0.0)" not in script,
                "negative normal target": "z_tcp_B ~= -locked_normal_B" in script,
            }
        )
    if name == "step4e_seed_normal_loop_v22":
        checks.update(
            {
                "seed pose": "SEED_TCP_POSE_M_RAD: [0.433010000, 0.108020000, -0.391790000" in script,
                "raw normal guard 35n": "codex_abs(normal_force) > 35.0" in script,
                "first search": "codex_v22_down_search(24.0, 24.2, 0.030, 0.010, 25.000)" in script,
                "lift 50mm": "p_lift[2] + 0.050" in script,
                "orientation threshold": "orientation_error > 0.174533" in script,
                "orientation hard stop": "orientation_error > 0.523599" in script,
                "target rotvec movel": "read_input_float_register(40), read_input_float_register(41), read_input_float_register(42)" in script
                and "movel(target_pose, a=0.030, v=0.010, r=0.0)" in script,
                "second search": "codex_v22_down_search(24.3, 24.4, 0.070, 0.040, 45.000)" in script,
                "acquire stage": "write_output_float_register(35, 25.3)" in script,
                "line stage": "write_output_float_register(35, 25.0)" in script,
                "fast stop": "stopl(0.1)" in script,
            }
        )
    if name == "step4e_seed_normal_loop_v23":
        checks.update(
            {
                "safe entry stage": "write_output_float_register(35, 21.0)" in script
                and "reference_orientation_pose = p[p0[0], p0[1], p0[2]" in script,
                "entry xy at current z": "entry_xy_pose = p[" in script and ", p1[2]," in script,
                "fixed search start": "local fixed_search_start_z_m = 0.09835" in script
                and "write_output_float_register(35, 22.5)" in script,
                "no low z seed movel": "local seed_pose = p[" not in script,
                "first search v13 envelope": "codex_v23_down_search(24.0, 24.2, 0.092, 0.080, 40.000, -0.015, -0.003)" in script,
                "lift 50mm": "p_lift[2] + 0.050" in script,
                "orientation ignore 3deg": "local orientation_ignore_error_rad = 0.052360" in script,
                "orientation hard stop 30deg": "local orientation_hard_stop_error_rad = 0.523599" in script,
                "angular speedl orientation": "speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz]" in script,
                "no target pose movel": "movel(target_pose" not in script,
                "second search": "codex_v23_down_search(24.3, 24.4, 0.070, 0.040, 45.000, -0.005, -0.003)" in script,
                "acquire stage": "write_output_float_register(35, 25.3)" in script,
                "line stage": "write_output_float_register(35, 25.0)" in script,
            }
        )
    if name == "step4e_seed_normal_loop_v24":
        checks.update(
            {
                "one-step entry": "entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]" in script,
                "no split orientation movel": "reference_orientation_pose" not in script
                and "write_output_float_register(35, 21.0)" not in script,
                "no fixed search start": "fixed_search_start_z_m" not in script
                and "fixed_search_start_pose" not in script
                and "write_output_float_register(35, 22.5)" not in script,
                "no low z seed movel": "local seed_pose = p[" not in script,
                "first search": "codex_v24_down_search(24.0, 24.2, 0.092, 0.080, 40.000, -0.015, -0.003)" in script,
                "lift 30mm": "p_lift[2] + 0.030" in script,
                "angular speedl orientation": "speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz]" in script,
                "linear command reject": "codex_abs(cmd_vx) > 0.001" in script,
                "second search": "codex_v24_down_search(24.3, 24.4, 0.070, 0.040, 45.000, -0.005, -0.003)" in script,
                "acquire stage": "write_output_float_register(35, 25.3)" in script,
                "line stage": "write_output_float_register(35, 25.0)" in script,
            }
        )
    if name == "step4e_seed_normal_loop_v25":
        checks.update(
            {
                "one-step entry": "entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]" in script,
                "dynamic near threshold": "local first_near_start_z_m = target_initial_z_m + 0.030" in script
                and "local first_search_near_start_depth_m = first_search_start_pose[2] - first_near_start_z_m" in script,
                "dynamic max depth": "local first_max_end_z_m = target_initial_z_m - 0.012" in script
                and "local first_search_max_down_m = first_search_start_pose[2] - first_max_end_z_m" in script,
                "no fixed search start": "fixed_search_start_z_m" not in script
                and "fixed_search_start_pose" not in script
                and "write_output_float_register(35, 22.5)" not in script,
                "first search": "codex_v25_down_search(24.0, 24.2, first_search_max_down_m, first_search_near_start_depth_m, 40.000, -0.015, -0.003)" in script,
                "lift 30mm": "p_lift[2] + 0.030" in script,
                "angular speedl orientation": "speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz]" in script,
                "linear command reject": "codex_abs(cmd_vx) > 0.001" in script,
                "second search": "codex_v25_down_search(24.3, 24.4, 0.070, 0.040, 45.000, -0.005, -0.003)" in script,
                "acquire stage": "write_output_float_register(35, 25.3)" in script,
                "line stage": "write_output_float_register(35, 25.0)" in script,
            }
        )
    if name == "step4e_seed_normal_loop_v26":
        checks.update(
            {
                "target initial z": f"local target_initial_z_m = {line_cfg(load_json(CONFIG_PATH))['target_initial_z']:.9f}" in script,
                "one-step entry": "entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]" in script,
                "dynamic near threshold": "local first_near_start_z_m = target_initial_z_m + 0.030" in script
                and "local first_search_near_start_depth_m = first_search_start_pose[2] - first_near_start_z_m" in script,
                "dynamic max depth": "local first_max_end_z_m = target_initial_z_m - 0.012" in script
                and "local first_search_max_down_m = first_search_start_pose[2] - first_max_end_z_m" in script,
                "first search": "codex_v26_down_search(24.0, 24.2, first_search_max_down_m, first_search_near_start_depth_m, 40.000, -0.015, -0.003)" in script,
                "lift 30mm": "p_lift[2] + 0.030" in script,
                "angular speedl orientation": "speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz]" in script,
                "linear command reject": "codex_abs(cmd_vx) > 0.001" in script,
                "second search": "codex_v26_down_search(24.3, 24.4, 0.070, 0.040, 45.000, -0.005, -0.003)" in script,
                "acquire stage": "write_output_float_register(35, 25.3)" in script,
                "line stage": "write_output_float_register(35, 25.0)" in script,
            }
        )
    if name == "step4e_seed_normal_loop_v27":
        checks.update(
            {
                "v13 first contact z": f"local first_contact_z_m = {V27_FIRST_CONTACT_Z_M:.9f}" in script,
                "v13 evidence source": V13_FIRST_CONTACT_SOURCE in script,
                "one-step entry": "entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]" in script,
                "near threshold from first contact": "local first_near_start_z_m = first_contact_z_m + 0.030" in script
                and "local first_search_near_start_depth_m = first_search_start_pose[2] - first_near_start_z_m" in script,
                "max depth from first contact": f"local first_max_end_z_m = first_contact_z_m - {V27_FIRST_CONTACT_BELOW_MARGIN_M:.3f}" in script
                and "local first_search_max_down_m = first_search_start_pose[2] - first_max_end_z_m" in script,
                "first search": "codex_v27_down_search(24.0, 24.2, first_search_max_down_m, first_search_near_start_depth_m, 40.000, -0.015, -0.003)" in script,
                "lift 30mm": "p_lift[2] + 0.030" in script,
                "angular speedl orientation": "speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz]" in script,
                "linear command reject": "codex_abs(cmd_vx) > 0.001" in script,
                "second search": "codex_v27_down_search(24.3, 24.4, 0.070, 0.040, 45.000, -0.005, -0.003)" in script,
                "acquire stage": "write_output_float_register(35, 25.3)" in script,
                "line stage": "write_output_float_register(35, 25.0)" in script,
            }
        )
    if name == "step4e_seed_normal_loop_v28":
        checks.update(
            {
                "v13 first contact z": f"local first_contact_z_m = {V28_FIRST_CONTACT_Z_M:.9f}" in script,
                "v13 evidence source": V13_FIRST_CONTACT_SOURCE in script,
                "raw normal guard 50n": f"codex_abs(normal_force) > {V28_RAW_NORMAL_GUARD_N:.1f}" in script,
                "one-step entry": "entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]" in script,
                "near threshold from first contact": f"local first_near_start_z_m = first_contact_z_m + {V28_FIRST_CONTACT_NEAR_MARGIN_M:.3f}" in script
                and "local first_search_near_start_depth_m = first_search_start_pose[2] - first_near_start_z_m" in script,
                "max depth from first contact": f"local first_max_end_z_m = first_contact_z_m - {V28_FIRST_CONTACT_BELOW_MARGIN_M:.3f}" in script
                and "local first_search_max_down_m = first_search_start_pose[2] - first_max_end_z_m" in script,
                "first search": f"codex_v28_down_search(24.0, 24.2, first_search_max_down_m, first_search_near_start_depth_m, 40.000, -0.015, {V28_FIRST_SEARCH_NEAR_SPEED_M_S:.4f})" in script,
                "lift 30mm": "p_lift[2] + 0.030" in script,
                "angular speedl orientation": "speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz]" in script,
                "linear command reject": "codex_abs(cmd_vx) > 0.001" in script,
                "second search": "codex_v28_down_search(24.3, 24.4, 0.070, 0.040, 45.000, -0.005, -0.003)" in script,
                "acquire stage": "write_output_float_register(35, 25.3)" in script,
                "line stage": "write_output_float_register(35, 25.0)" in script,
            }
        )
    if name == "step4e_seed_normal_loop_v29":
        checks.update(
            {
                "v13 first contact z": f"local first_contact_z_m = {V29_FIRST_CONTACT_Z_M:.9f}" in script,
                "v13 evidence source": V13_FIRST_CONTACT_SOURCE in script,
                "bridge profile fix wording": "bridge-profile-fix" in script,
                "line-entry bypass wording": "line-entry-gate release" in script,
                "v29 function names": "codex_v29_down_search" in script
                and "codex_step4e_seed_normal_loop_v29" in script,
                "raw normal guard 50n": f"codex_abs(normal_force) > {V29_RAW_NORMAL_GUARD_N:.1f}" in script,
                "one-step entry": "entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]" in script,
                "near threshold from first contact": f"local first_near_start_z_m = first_contact_z_m + {V29_FIRST_CONTACT_NEAR_MARGIN_M:.3f}" in script
                and "local first_search_near_start_depth_m = first_search_start_pose[2] - first_near_start_z_m" in script,
                "stage25.2 linear zero settle": "codex_wait_for_stage_linear_zero(25.2, 1.000)" in script
                and "def codex_wait_for_stage_linear_zero(stage_code, timeout_s):" in script,
                "max depth from first contact": f"local first_max_end_z_m = first_contact_z_m - {V29_FIRST_CONTACT_BELOW_MARGIN_M:.3f}" in script
                and "local first_search_max_down_m = first_search_start_pose[2] - first_max_end_z_m" in script,
                "first search": f"codex_v29_down_search(24.0, 24.2, first_search_max_down_m, first_search_near_start_depth_m, 40.000, -0.015, {V29_FIRST_SEARCH_NEAR_SPEED_M_S:.4f})" in script,
                "lift 20mm": "p_lift[2] + 0.020" in script,
                "angular speedl orientation": "speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz]" in script,
                "linear command reject": "codex_abs(cmd_vx) > 0.001" in script,
                "second search": "codex_v29_down_search(24.3, 24.4, 0.070, 0.040, 45.000, -0.005, -0.003)" in script,
                "line-entry gate stage": "write_output_float_register(35, 25.3)" in script
                and "local line_entry_required_s = 0.100" in script
                and "local line_entry_timeout_s = 1.000" in script
                and "speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)" not in script,
                "line stage": "write_output_float_register(35, 25.0)" in script,
            }
        )
    if name == "step4e_seed_normal_loop_v30":
        checks.update(
            {
                "v13 first contact z": f"local first_contact_z_m = {V30_FIRST_CONTACT_Z_M:.9f}" in script,
                "v13 evidence source": V13_FIRST_CONTACT_SOURCE in script,
                "filtered live normal wording": "filtered-live-normal" in script
                and "step4e-normal-follow-mode=filtered_live" in script,
                "line-entry bypass wording": "line-entry-gate release" in script,
                "v30 function names": "codex_v30_down_search" in script
                and "codex_step4e_seed_normal_loop_v30" in script,
                "raw normal guard 50n": f"codex_abs(normal_force) > {V30_RAW_NORMAL_GUARD_N:.1f}" in script,
                "one-step entry": "entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]" in script,
                "near threshold from first contact": f"local first_near_start_z_m = first_contact_z_m + {V30_FIRST_CONTACT_NEAR_MARGIN_M:.3f}" in script
                and "local first_search_near_start_depth_m = first_search_start_pose[2] - first_near_start_z_m" in script,
                "stage25.2 linear zero settle": "codex_wait_for_stage_linear_zero(25.2, 1.000)" in script
                and "def codex_wait_for_stage_linear_zero(stage_code, timeout_s):" in script,
                "max depth from first contact": f"local first_max_end_z_m = first_contact_z_m - {V30_FIRST_CONTACT_BELOW_MARGIN_M:.3f}" in script
                and "local first_search_max_down_m = first_search_start_pose[2] - first_max_end_z_m" in script,
                "first search": f"codex_v30_down_search(24.0, 24.2, first_search_max_down_m, first_search_near_start_depth_m, 40.000, -0.015, {V30_FIRST_SEARCH_NEAR_SPEED_M_S:.4f})" in script,
                "lift 20mm": "p_lift[2] + 0.020" in script,
                "angular speedl orientation": "speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz]" in script,
                "linear command reject": "codex_abs(cmd_vx) > 0.001" in script,
                "second search": "codex_v30_down_search(24.3, 24.4, 0.070, 0.040, 45.000, -0.005, -0.003)" in script,
                "line-entry gate stage": "write_output_float_register(35, 25.3)" in script
                and "local line_entry_required_s = 0.100" in script
                and "local line_entry_timeout_s = 1.000" in script
                and "speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)" not in script,
                "line stage": "write_output_float_register(35, 25.0)" in script,
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
# SAFETY: raw normal guard 35 N, force norm guard 50 N, torque guard 3.0 Nm.
{common_functions("35.0", "3.0")}

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


def ball_first_contact_script(stamp: str, gen_at: str, geom: dict[str, float]) -> str:
    return f"""# Step4e P0-geo ball-first contact witness v1.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: {gen_at}
# PURPOSE: verify whether first contact is the KSM-8N ball or surrounding housing/cylindrical face.
# CONTROL: bridge writes only base force/heartbeat registers; no attitude, no 5N acquire, no line.
# SAFETY: raw normal guard 35 N, force norm guard 50 N, torque guard 3.0 Nm.
{common_functions("35.0", "3.0")}

def codex_step4e_ball_first_contact_p0_v1():
  local stop_reason = 0.0
  local home_pose = get_actual_tcp_pose()
  local search_accel_m_s2 = 0.300
  local search_hold_s = 0.002
  local stale_limit_s = 0.100
  local search_runtime_limit_s = 40.0
  local search_near_start_depth_m = 0.080
  local max_search_down_m = 0.092
  local search_far_speed_m_s = -0.015
  local search_near_speed_m_s = -0.003
  local visual_dwell_s = 8.000
  local contact_triggered = 0
  textmsg("codex step4e version {stamp} start ball_first_contact_p0_v1")
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
            speedl([0.0, 0.0, search_far_speed_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
          else:
            write_output_float_register(35, 24.2)
            codex_echo_step4e(stop_reason)
            speedl([0.0, 0.0, search_near_speed_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
          end
          t = t + get_steptime()
        end
      end
    end
    stopl(0.1)
  end
  if contact_triggered == 1:
    stop_reason = 0.0
    write_output_float_register(35, 25.05)
    local dwell_t = 0.0
    local last_heartbeat2 = read_input_float_register(26)
    local stale_s2 = 0.0
    while stop_reason == 0.0 and dwell_t < visual_dwell_s:
      local heartbeat2 = read_input_float_register(26)
      if heartbeat2 == last_heartbeat2:
        stale_s2 = stale_s2 + get_steptime()
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
      sync()
      dwell_t = dwell_t + get_steptime()
    end
    if stop_reason == 0.0:
      write_output_float_register(35, 25.06)
      local p_retract = get_actual_tcp_pose()
      local retract_pose = p[p_retract[0], p_retract[1], p_retract[2] + 0.002, p_retract[3], p_retract[4], p_retract[5]]
      codex_echo_step4e(stop_reason)
      movel(retract_pose, a=0.030, v=0.010, r=0.0)
      stopl(0.1)
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

codex_step4e_ball_first_contact_p0_v1()
"""


def format_pose(values: list[float]) -> str:
    return ", ".join(f"{value:.9f}" for value in values)


def ball_vs_cyl_contact_script(stamp: str, gen_at: str, pair: dict) -> str:
    p0_pose = [float(value) for value in pair["p0_pose"]]
    p1_pose = [float(value) for value in pair["p1_pose"]]
    p1_start = p1_pose.copy()
    p1_start[2] = p1_start[2] + 0.020
    source = pair.get("pair_dir", "unknown")
    return f"""# Step4e P0 witness pair: ball-first vs KSM-8N housing/cylindrical-face contact.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: {gen_at}
# POSE_PAIR_SOURCE: {source}
# P0_BALL_POSE_M_RAD: [{format_pose(p0_pose)}]
# P1_CYL_POSE_M_RAD: [{format_pose(p1_pose)}]
# PURPOSE: compare low-threshold wrench/torque/contact-offset signatures for ball contact vs housing/cylindrical-face contact.
# CONTROL: bridge writes only base force/heartbeat registers; no attitude, no 5N acquire, no line.
# SAFETY: raw normal guard 35 N, force norm guard 50 N, torque guard 3.0 Nm.
{common_functions("35.0", "3.0")}

def codex_wait_and_rezero(stage_code):
  write_output_float_register(35, stage_code)
  codex_echo_step4e(0.0)
  write_output_float_register(34, 1.0)
  if not codex_wait_for_rezero_complete(5.0):
    return 14.0
  end
  return 0.0
end

def codex_contact_witness(move_stage, rezero_stage, search_stage, near_stage, dwell_stage, retract_stage, start_pose, max_search_down_m, search_near_start_depth_m):
  local stop_reason = 0.0
  local search_accel_m_s2 = 0.300
  local search_hold_s = 0.002
  local stale_limit_s = 0.100
  local search_runtime_limit_s = 45.0
  local search_far_speed_m_s = -0.015
  local search_near_speed_m_s = -0.003
  local visual_dwell_s = 8.000
  local contact_triggered = 0

  write_output_float_register(35, move_stage)
  codex_echo_step4e(0.0)
  movel(start_pose, a=0.030, v=0.020, r=0.0)
  stopl(0.1)

  stop_reason = codex_wait_and_rezero(rezero_stage)
  if stop_reason != 0.0:
    return stop_reason
  end

  write_output_float_register(35, search_stage)
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
          speedl([0.0, 0.0, search_far_speed_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
        else:
          write_output_float_register(35, near_stage)
          codex_echo_step4e(stop_reason)
          speedl([0.0, 0.0, search_near_speed_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
        end
        t = t + get_steptime()
      end
    end
  end
  stopl(0.1)

  if contact_triggered == 1:
    stop_reason = 0.0
    write_output_float_register(35, dwell_stage)
    local dwell_t = 0.0
    local last_heartbeat2 = read_input_float_register(26)
    local stale_s2 = 0.0
    while stop_reason == 0.0 and dwell_t < visual_dwell_s:
      local heartbeat2 = read_input_float_register(26)
      if heartbeat2 == last_heartbeat2:
        stale_s2 = stale_s2 + get_steptime()
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
      sync()
      dwell_t = dwell_t + get_steptime()
    end
    if stop_reason == 0.0:
      write_output_float_register(35, retract_stage)
      local p_retract = get_actual_tcp_pose()
      local retract_pose = p[p_retract[0], p_retract[1], p_retract[2] + 0.020, p_retract[3], p_retract[4], p_retract[5]]
      codex_echo_step4e(stop_reason)
      movel(retract_pose, a=0.030, v=0.010, r=0.0)
      stopl(0.1)
    end
  end
  return stop_reason
end

def codex_step4e_ball_vs_cyl_contact_p0_v1():
  local stop_reason = 0.0
  local home_pose = get_actual_tcp_pose()
  local ball_pose = p[{format_pose(p0_pose)}]
  local cyl_pose = p[{format_pose(p1_start)}]
  textmsg("codex step4e version {stamp} start ball_vs_cyl_contact_p0_v1")
  write_output_float_register(34, 0.0)
  write_output_float_register(35, 20.0)
  codex_echo_step4e(0.0)
  if not codex_wait_for_fresh_heartbeat(30.0):
    stop_reason = 3.0
  end
  if stop_reason == 0.0:
    textmsg("codex step4e version {stamp} witness ball")
    stop_reason = codex_contact_witness(22.11, 23.11, 24.11, 24.12, 25.11, 25.16, ball_pose, 0.092, 0.080)
  end
  if stop_reason == 0.0:
    textmsg("codex step4e version {stamp} witness cyl")
    stop_reason = codex_contact_witness(22.21, 23.21, 24.21, 24.22, 25.21, 25.26, cyl_pose, 0.030, 0.000)
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

codex_step4e_ball_vs_cyl_contact_p0_v1()
"""


def v21_detached_movel_script(stamp: str, gen_at: str, geom: dict[str, float]) -> str:
    return f"""# Step4e P1-a detached movel minimal-rotation v21.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: {gen_at}
# PURPOSE: latch first contact normal, detach along the locked normal, then preview the minimal-rotation target without contact-posture motion.
# CONTROL: bridge step4e-version=v21 writes 37..39 as +locked-normal detach direction in 25.1 and 40..42 as target rotvec for z_tcp_B ~= -locked_normal_B in 25.2.
# SAFETY: raw normal guard 35 N, force norm guard 50 N, torque guard 3.0 Nm.
{common_functions("35.0", "3.0")}

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
  local search_near_start_depth_m = 0.080
  local max_search_down_m = 0.092
  local search_far_speed_m_s = -0.015
  local search_near_speed_m_s = -0.003
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
            speedl([0.0, 0.0, search_far_speed_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
          else:
            write_output_float_register(35, 24.2)
            codex_echo_step4e(stop_reason)
            speedl([0.0, 0.0, search_near_speed_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
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
    while stop_reason == 0.0 and detach_total_m < 0.002 and read_input_float_register(25) > 2.0:
      local dir_x = read_input_float_register(37)
      local dir_y = read_input_float_register(38)
      local dir_z = read_input_float_register(39)
      local step_m = 0.001
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
      # 25.2 is target-preview only; no contact-posture movel.
      codex_echo_step4e(stop_reason)
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


def v22_seed_normal_loop_script(stamp: str, gen_at: str, geom: dict[str, float]) -> str:
    seed_pose = [
        geom["start_x"],
        geom["start_y"],
        geom["start_z"],
        -3.044172198,
        -0.130573165,
        -0.202188631,
    ]
    return f"""# Step4e v22 seed-normal TASE minimal reproduction loop.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: {gen_at}
# SEED_TCP_POSE_M_RAD: [{format_pose(seed_pose)}]
# PURPOSE: use the marked path-start XYZ with the manually marked near-normal rotvec, touch once, lift 50 mm, optionally align TCP z to the measured contact normal, touch again, then run normal FT line control.
# CONTROL: bridge step4e-version=v22 latches the first contact normal, writes target rotvec in 25.2, reacquires 5 N in 25.3, and runs line control in 25.0.
# SAFETY: raw normal guard 35 N, force norm guard 50 N, torque guard 3.0 Nm.
{common_functions("35.0", "3.0")}

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

def codex_v22_down_search(search_stage, near_stage, max_search_down_m, search_near_start_depth_m, search_runtime_limit_s):
  local stop_reason = 0.0
  local search_accel_m_s2 = 0.300
  local search_hold_s = 0.002
  local stale_limit_s = 0.100
  local search_far_speed_m_s = -0.005
  local search_near_speed_m_s = -0.003
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
          write_output_float_register(35, search_stage)
          codex_echo_step4e(stop_reason)
          speedl([0.0, 0.0, search_far_speed_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
        else:
          write_output_float_register(35, near_stage)
          codex_echo_step4e(stop_reason)
          speedl([0.0, 0.0, search_near_speed_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
        end
        t = t + get_steptime()
      end
    end
  end
  stopl(0.1)
  return stop_reason
end

def codex_step4e_seed_normal_loop_v22():
  local stop_reason = 0.0
  local home_pose = get_actual_tcp_pose()
  local line_accel_m_s2 = 0.300
  local line_hold_s = 0.002
  local line_runtime_limit_s = 75.000
  local line_success_progress_m = {max(0.0, geom['length'] - 0.0005):.9f}
  local end_hold_required_s = 0.100
  local end_hold_s = 0.0
  local max_cmd_angular_xy_rad_s = 0.120
  local cmd_valid_grace_s = 0.250
  local cmd_valid_loss_limit_s = 0.100
  local cmd_invalid_s = 0.0
  local saw_cmd_valid = 0
  local short_retract_z_m = 0.010
  local short_retract_speed_m_s = 0.020
  local home_return_speed_m_s = 0.050
  local final_progress_m = 0.0

  textmsg("codex step4e version {stamp} start seed_normal_loop_v22")
  write_output_float_register(34, 0.0)
  write_output_float_register(35, 20.0)
  codex_echo_step4e(0.0)
  if not codex_wait_for_fresh_heartbeat(30.0):
    stop_reason = 3.0
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 22.0)
    local seed_pose = p[{format_pose(seed_pose)}]
    codex_echo_step4e(stop_reason)
    movel(seed_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
    write_output_float_register(35, 23.0)
    codex_echo_step4e(stop_reason)
    write_output_float_register(34, 1.0)
    if not codex_wait_for_rezero_complete(5.0):
      stop_reason = 14.0
    end
  end

  if stop_reason == 0.0:
    stop_reason = codex_v22_down_search(24.0, 24.2, 0.030, 0.010, 25.000)
    if stop_reason == 11.0:
      stop_reason = 0.0
    end
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 25.05)
    stop_reason = codex_wait_for_cmd_valid(25.05, 1.000)
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 25.1)
    local p_lift = get_actual_tcp_pose()
    local lift_pose = p[p_lift[0], p_lift[1], p_lift[2] + 0.050, p_lift[3], p_lift[4], p_lift[5]]
    codex_echo_step4e(stop_reason)
    movel(lift_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 25.2)
    stop_reason = codex_wait_for_cmd_valid(25.2, 1.000)
    if stop_reason == 0.0:
      local orientation_error = read_input_float_register(46)
      codex_echo_step4e(stop_reason)
      if orientation_error > 0.523599:
        stop_reason = 15.0
      elif orientation_error > 0.174533:
        local p_orient = get_actual_tcp_pose()
        local target_pose = p[p_orient[0], p_orient[1], p_orient[2], read_input_float_register(40), read_input_float_register(41), read_input_float_register(42)]
        movel(target_pose, a=0.030, v=0.010, r=0.0)
        stopl(0.1)
      else:
        sleep(0.250)
      end
    end
  end

  if stop_reason == 0.0:
    stop_reason = codex_v22_down_search(24.3, 24.4, 0.070, 0.040, 45.000)
    if stop_reason == 11.0:
      stop_reason = 0.0
    end
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 25.3)
    local last_heartbeat_acquire = read_input_float_register(26)
    local stale_s_acquire = 0.0
    local t_acquire = 0.0
    local acquire_stable_s = 0.0
    local acquire_min_s = 0.200
    local acquire_runtime_limit_s = 8.000
    local acquire_force_error_limit_n = 0.750
    local acquire_stable_required_s = 0.250
    saw_cmd_valid = 0
    cmd_invalid_s = 0.0
    while stop_reason == 0.0:
      local heartbeat_acquire = read_input_float_register(26)
      local cmd_valid = read_input_float_register(43)
      local force_error = read_input_float_register(45)
      local cmd_vx = read_input_float_register(37)
      local cmd_vy = read_input_float_register(38)
      local cmd_vz = read_input_float_register(39)
      local loop_dt = get_steptime()
      if cmd_valid >= 0.5:
        saw_cmd_valid = 1
        cmd_invalid_s = 0.0
      else:
        cmd_invalid_s = cmd_invalid_s + loop_dt
      end
      if heartbeat_acquire == last_heartbeat_acquire:
        stale_s_acquire = stale_s_acquire + loop_dt
      else:
        stale_s_acquire = 0.0
        last_heartbeat_acquire = heartbeat_acquire
      end
      t_acquire = t_acquire + loop_dt
      if t_acquire >= acquire_min_s and codex_abs(force_error) <= acquire_force_error_limit_n:
        acquire_stable_s = acquire_stable_s + loop_dt
      else:
        acquire_stable_s = 0.0
      end
      codex_echo_step4e(stop_reason)
      if stale_s_acquire > 0.100:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if acquire_stable_s >= acquire_stable_required_s:
          stop_reason = 16.0
        elif cmd_valid < 0.5:
          if saw_cmd_valid == 0 and t_acquire < cmd_valid_grace_s:
            speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
          elif saw_cmd_valid == 1 and cmd_invalid_s <= cmd_valid_loss_limit_s:
            speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
          else:
            stop_reason = 12.0
          end
        elif codex_abs(cmd_vx) > 0.010 or codex_abs(cmd_vy) > 0.010 or codex_abs(cmd_vz) > 0.010:
          stop_reason = 13.0
        elif t_acquire >= acquire_runtime_limit_s:
          stop_reason = 10.0
        else:
          speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
        end
      end
    end
    stopl(0.1)
    if stop_reason == 16.0:
      stop_reason = 0.0
      saw_cmd_valid = 0
      cmd_invalid_s = 0.0
      end_hold_s = 0.0
    end
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
      local loop_dt = get_steptime()
      final_progress_m = progress_m
      if cmd_valid >= 0.5:
        saw_cmd_valid = 1
        cmd_invalid_s = 0.0
      else:
        cmd_invalid_s = cmd_invalid_s + loop_dt
      end
      if heartbeat2 == last_heartbeat2:
        stale_s2 = stale_s2 + loop_dt
      else:
        stale_s2 = 0.0
        last_heartbeat2 = heartbeat2
      end
      t2 = t2 + loop_dt
      if progress_m >= line_success_progress_m:
        end_hold_s = end_hold_s + loop_dt
      else:
        end_hold_s = 0.0
      end
      codex_echo_step4e(stop_reason)
      if stale_s2 > 0.100:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if cmd_valid < 0.5:
          if saw_cmd_valid == 0 and t2 < cmd_valid_grace_s:
            speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
          elif saw_cmd_valid == 1 and cmd_invalid_s <= cmd_valid_loss_limit_s:
            speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
          else:
            stop_reason = 12.0
          end
        elif codex_abs(cmd_vx) > 0.010 or codex_abs(cmd_vy) > 0.010 or codex_abs(cmd_vz) > 0.010:
          stop_reason = 13.0
        elif codex_abs(cmd_wx) > max_cmd_angular_xy_rad_s or codex_abs(cmd_wy) > max_cmd_angular_xy_rad_s or codex_abs(cmd_wz) > 0.005:
          stop_reason = 13.0
        elif end_hold_s >= end_hold_required_s:
          stop_reason = 1.0
        elif t2 >= line_runtime_limit_s:
          stop_reason = 10.0
        else:
          speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy, 0.0], line_accel_m_s2, line_hold_s)
        end
      end
    end
    stopl(0.1)
  end

  if codex_should_auto_home(stop_reason):
    write_output_float_register(35, 26.0)
    local short_retract_start = get_actual_tcp_pose()
    local short_retract_pose = p[short_retract_start[0], short_retract_start[1], short_retract_start[2] + short_retract_z_m, short_retract_start[3], short_retract_start[4], short_retract_start[5]]
    codex_echo_step4e(stop_reason)
    movel(short_retract_pose, a=0.030, v=short_retract_speed_m_s, r=0.0)
    stopl(0.1)
    write_output_float_register(35, 27.0)
    codex_echo_step4e(stop_reason)
    movel(home_pose, a=0.030, v=home_return_speed_m_s, r=0.0)
    stopl(0.1)
  end

  write_output_float_register(30, stop_reason)
  write_output_float_register(31, final_progress_m)
  write_output_float_register(35, 29.0)
  textmsg("codex step4e version {stamp} stop reason:", stop_reason)
end

codex_step4e_seed_normal_loop_v22()
"""


def v23_seed_normal_loop_script(stamp: str, gen_at: str, geom: dict[str, float]) -> str:
    near_normal_rotvec = [-3.044172198, -0.130573165, -0.202188631]
    return f"""# Step4e v23 v13-safe seed-normal TASE minimal reproduction loop.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: {gen_at}
# ENTRY_XY_M: [{geom['start_x']:.9f}, {geom['start_y']:.9f}]
# NEAR_NORMAL_ROTVEC_RAD: [{format_pose(near_normal_rotvec)}]
# PURPOSE: keep v13 safe entry/search flow, then add first-contact lift, lifted angular speedl posture adjustment, second touch, 5N acquire and line.
# CONTROL: bridge step4e-version=v23 latches the first contact normal, writes angular speedl commands in 25.2, reacquires 5 N in 25.3, and runs line control in 25.0.
# SAFETY: raw normal guard 35 N, force norm guard 50 N, torque guard 3.0 Nm.
{common_functions("35.0", "3.0")}

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

def codex_v23_down_search(search_stage, near_stage, max_search_down_m, search_near_start_depth_m, search_runtime_limit_s, search_far_speed_m_s, search_near_speed_m_s):
  local stop_reason = 0.0
  local search_accel_m_s2 = 0.300
  local search_hold_s = 0.002
  local stale_limit_s = 0.100
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
          write_output_float_register(35, search_stage)
          codex_echo_step4e(stop_reason)
          speedl([0.0, 0.0, search_far_speed_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
        else:
          write_output_float_register(35, near_stage)
          codex_echo_step4e(stop_reason)
          speedl([0.0, 0.0, search_near_speed_m_s, 0.0, 0.0, 0.0], search_accel_m_s2, search_hold_s)
        end
        t = t + get_steptime()
      end
    end
  end
  stopl(0.1)
  return stop_reason
end

def codex_step4e_seed_normal_loop_v23():
  local stop_reason = 0.0
  local home_pose = get_actual_tcp_pose()
  local entry_x = {geom['start_x']:.9f}
  local entry_y = {geom['start_y']:.9f}
  local ref_rx = {near_normal_rotvec[0]:.9f}
  local ref_ry = {near_normal_rotvec[1]:.9f}
  local ref_rz = {near_normal_rotvec[2]:.9f}
  local fixed_search_start_z_m = {geom['validated_search_start_z']:.5f}
  local line_accel_m_s2 = 0.300
  local line_hold_s = 0.002
  local line_runtime_limit_s = 75.000
  local line_success_progress_m = {max(0.0, geom['length'] - 0.0005):.9f}
  local end_hold_required_s = 0.100
  local end_hold_s = 0.0
  local max_cmd_angular_xy_rad_s = 0.120
  local cmd_valid_grace_s = 0.250
  local cmd_valid_loss_limit_s = 0.100
  local cmd_invalid_s = 0.0
  local saw_cmd_valid = 0
  local short_retract_z_m = 0.010
  local short_retract_speed_m_s = 0.020
  local home_return_speed_m_s = 0.050
  local final_progress_m = 0.0

  textmsg("codex step4e version {stamp} start seed_normal_loop_v23")
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
    codex_echo_step4e(stop_reason)
    movel(reference_orientation_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
    write_output_float_register(35, 22.0)
    local p1 = get_actual_tcp_pose()
    local entry_xy_pose = p[entry_x, entry_y, p1[2], ref_rx, ref_ry, ref_rz]
    codex_echo_step4e(stop_reason)
    movel(entry_xy_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
    write_output_float_register(35, 22.5)
    local p2 = get_actual_tcp_pose()
    local fixed_search_start_pose = p[p2[0], p2[1], fixed_search_start_z_m, ref_rx, ref_ry, ref_rz]
    codex_echo_step4e(stop_reason)
    movel(fixed_search_start_pose, a=0.030, v=home_return_speed_m_s, r=0.0)
    stopl(0.1)
    sleep(0.20)
    write_output_float_register(35, 23.0)
    codex_echo_step4e(stop_reason)
    write_output_float_register(34, 1.0)
    if not codex_wait_for_rezero_complete(5.0):
      stop_reason = 14.0
    end
    if stop_reason == 0.0:
      sleep(0.20)
      stop_reason = codex_step4e_guard_stop_reason()
    end
  end

  if stop_reason == 0.0:
    stop_reason = codex_v23_down_search(24.0, 24.2, 0.092, 0.080, 40.000, -0.015, -0.003)
    if stop_reason == 11.0:
      stop_reason = 0.0
    end
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 25.05)
    stop_reason = codex_wait_for_cmd_valid(25.05, 1.000)
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 25.1)
    local p_lift = get_actual_tcp_pose()
    local lift_pose = p[p_lift[0], p_lift[1], p_lift[2] + 0.050, p_lift[3], p_lift[4], p_lift[5]]
    codex_echo_step4e(stop_reason)
    movel(lift_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 25.2)
    stop_reason = codex_wait_for_cmd_valid(25.2, 1.000)
    if stop_reason == 0.0:
      local orientation_ignore_error_rad = 0.052360
      local orientation_hard_stop_error_rad = 0.523599
      local orientation_runtime_limit_s = 8.000
      local t_orient = 0.0
      local stale_s_orient = 0.0
      local last_heartbeat_orient = read_input_float_register(26)
      saw_cmd_valid = 0
      cmd_invalid_s = 0.0
      while stop_reason == 0.0:
        local heartbeat_orient = read_input_float_register(26)
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
        if heartbeat_orient == last_heartbeat_orient:
          stale_s_orient = stale_s_orient + loop_dt
        else:
          stale_s_orient = 0.0
          last_heartbeat_orient = heartbeat_orient
        end
        t_orient = t_orient + loop_dt
        codex_echo_step4e(stop_reason)
        if stale_s_orient > 0.100:
          stop_reason = 2.0
        else:
          stop_reason = codex_step4e_guard_stop_reason()
        end
        if stop_reason == 0.0:
          if orientation_error > orientation_hard_stop_error_rad:
            stop_reason = 15.0
          elif orientation_error <= orientation_ignore_error_rad:
            stop_reason = 17.0
          elif cmd_valid < 0.5:
            if saw_cmd_valid == 0 and t_orient < cmd_valid_grace_s:
              speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
            elif saw_cmd_valid == 1 and cmd_invalid_s <= cmd_valid_loss_limit_s:
              speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
            else:
              stop_reason = 12.0
            end
          elif codex_abs(cmd_vx) > 0.001 or codex_abs(cmd_vy) > 0.001 or codex_abs(cmd_vz) > 0.001:
            stop_reason = 13.0
          elif codex_abs(cmd_wx) > max_cmd_angular_xy_rad_s or codex_abs(cmd_wy) > max_cmd_angular_xy_rad_s or codex_abs(cmd_wz) > max_cmd_angular_xy_rad_s:
            stop_reason = 13.0
          elif t_orient >= orientation_runtime_limit_s:
            stop_reason = 10.0
          else:
            speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz], line_accel_m_s2, line_hold_s)
          end
        end
      end
      stopl(0.1)
      if stop_reason == 17.0:
        stop_reason = 0.0
      end
    end
  end

  if stop_reason == 0.0:
    stop_reason = codex_v23_down_search(24.3, 24.4, 0.070, 0.040, 45.000, -0.005, -0.003)
    if stop_reason == 11.0:
      stop_reason = 0.0
    end
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 25.3)
    local last_heartbeat_acquire = read_input_float_register(26)
    local stale_s_acquire = 0.0
    local t_acquire = 0.0
    local acquire_stable_s = 0.0
    local acquire_min_s = 0.200
    local acquire_runtime_limit_s = 8.000
    local acquire_force_error_limit_n = 0.750
    local acquire_stable_required_s = 0.250
    saw_cmd_valid = 0
    cmd_invalid_s = 0.0
    while stop_reason == 0.0:
      local heartbeat_acquire = read_input_float_register(26)
      local cmd_valid = read_input_float_register(43)
      local force_error = read_input_float_register(45)
      local cmd_vx = read_input_float_register(37)
      local cmd_vy = read_input_float_register(38)
      local cmd_vz = read_input_float_register(39)
      local loop_dt = get_steptime()
      if cmd_valid >= 0.5:
        saw_cmd_valid = 1
        cmd_invalid_s = 0.0
      else:
        cmd_invalid_s = cmd_invalid_s + loop_dt
      end
      if heartbeat_acquire == last_heartbeat_acquire:
        stale_s_acquire = stale_s_acquire + loop_dt
      else:
        stale_s_acquire = 0.0
        last_heartbeat_acquire = heartbeat_acquire
      end
      t_acquire = t_acquire + loop_dt
      if t_acquire >= acquire_min_s and codex_abs(force_error) <= acquire_force_error_limit_n:
        acquire_stable_s = acquire_stable_s + loop_dt
      else:
        acquire_stable_s = 0.0
      end
      codex_echo_step4e(stop_reason)
      if stale_s_acquire > 0.100:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if acquire_stable_s >= acquire_stable_required_s:
          stop_reason = 16.0
        elif cmd_valid < 0.5:
          if saw_cmd_valid == 0 and t_acquire < cmd_valid_grace_s:
            speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
          elif saw_cmd_valid == 1 and cmd_invalid_s <= cmd_valid_loss_limit_s:
            speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
          else:
            stop_reason = 12.0
          end
        elif codex_abs(cmd_vx) > 0.010 or codex_abs(cmd_vy) > 0.010 or codex_abs(cmd_vz) > 0.010:
          stop_reason = 13.0
        elif t_acquire >= acquire_runtime_limit_s:
          stop_reason = 10.0
        else:
          speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
        end
      end
    end
    stopl(0.1)
    if stop_reason == 16.0:
      stop_reason = 0.0
      saw_cmd_valid = 0
      cmd_invalid_s = 0.0
      end_hold_s = 0.0
    end
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
      local loop_dt = get_steptime()
      final_progress_m = progress_m
      if cmd_valid >= 0.5:
        saw_cmd_valid = 1
        cmd_invalid_s = 0.0
      else:
        cmd_invalid_s = cmd_invalid_s + loop_dt
      end
      if heartbeat2 == last_heartbeat2:
        stale_s2 = stale_s2 + loop_dt
      else:
        stale_s2 = 0.0
        last_heartbeat2 = heartbeat2
      end
      t2 = t2 + loop_dt
      if progress_m >= line_success_progress_m:
        end_hold_s = end_hold_s + loop_dt
      else:
        end_hold_s = 0.0
      end
      codex_echo_step4e(stop_reason)
      if stale_s2 > 0.100:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if cmd_valid < 0.5:
          if saw_cmd_valid == 0 and t2 < cmd_valid_grace_s:
            speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
          elif saw_cmd_valid == 1 and cmd_invalid_s <= cmd_valid_loss_limit_s:
            speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
          else:
            stop_reason = 12.0
          end
        elif codex_abs(cmd_vx) > 0.010 or codex_abs(cmd_vy) > 0.010 or codex_abs(cmd_vz) > 0.010:
          stop_reason = 13.0
        elif codex_abs(cmd_wx) > max_cmd_angular_xy_rad_s or codex_abs(cmd_wy) > max_cmd_angular_xy_rad_s or codex_abs(cmd_wz) > 0.005:
          stop_reason = 13.0
        elif end_hold_s >= end_hold_required_s:
          stop_reason = 1.0
        elif t2 >= line_runtime_limit_s:
          stop_reason = 10.0
        else:
          speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy, 0.0], line_accel_m_s2, line_hold_s)
        end
      end
    end
    stopl(0.1)
  end

  if codex_should_auto_home(stop_reason):
    write_output_float_register(35, 26.0)
    local short_retract_start = get_actual_tcp_pose()
    local short_retract_pose = p[short_retract_start[0], short_retract_start[1], short_retract_start[2] + short_retract_z_m, short_retract_start[3], short_retract_start[4], short_retract_start[5]]
    codex_echo_step4e(stop_reason)
    movel(short_retract_pose, a=0.030, v=short_retract_speed_m_s, r=0.0)
    stopl(0.1)
    write_output_float_register(35, 27.0)
    codex_echo_step4e(stop_reason)
    movel(home_pose, a=0.030, v=home_return_speed_m_s, r=0.0)
    stopl(0.1)
  end

  write_output_float_register(30, stop_reason)
  write_output_float_register(31, final_progress_m)
  write_output_float_register(35, 29.0)
  textmsg("codex step4e version {stamp} stop reason:", stop_reason)
end

codex_step4e_seed_normal_loop_v23()
"""


def v24_seed_normal_loop_script(stamp: str, gen_at: str, geom: dict[str, float]) -> str:
    script = v23_seed_normal_loop_script(stamp, gen_at, geom)
    script = script.replace("v23", "v24").replace("V23", "V24")
    script = script.replace(
        "# Step4e v24 v13-safe seed-normal TASE minimal reproduction loop.",
        "# Step4e v24 one-step-entry seed-normal TASE minimal reproduction loop.",
    )
    script = script.replace(
        "# PURPOSE: keep v13 safe entry/search flow, then add first-contact lift, lifted angular speedl posture adjustment, second touch, 5N acquire and line.",
        "# PURPOSE: move once to entry XY with target attitude at current Z, then run far/near search, first-contact lift, lifted angular speedl posture adjustment, second touch, 5N acquire and line.",
    )
    script = script.replace("p_lift[2] + 0.050", "p_lift[2] + 0.030")
    script = script.replace(
        "# CONTROL: bridge step4e-version=v24",
        "# FLOW_TABLE: STEP4E_FLOW.md\n# CONTROL: bridge step4e-version=v24",
    )
    script = script.replace(
        "# CONTROL: bridge step4e-version=v24 latches the first contact normal, writes angular speedl commands in 25.2, reacquires 5 N in 25.3, and runs line control in 25.0.",
        "# CONTROL: bridge step4e-version=v24 latches the first contact normal, forces 37..39 zero during 25.2, writes angular speedl commands in 40..42, reacquires 5 N in 25.3, and runs line control in 25.0.",
    )
    script = script.replace("NEAR_NORMAL_ROTVEC_RAD", "TARGET_ROTVEC_RAD")
    script = script.replace("local ref_rx", "local target_rx")
    script = script.replace("local ref_ry", "local target_ry")
    script = script.replace("local ref_rz", "local target_rz")
    script = script.replace(", ref_rx, ref_ry, ref_rz", ", target_rx, target_ry, target_rz")
    script = script.replace(f"  local fixed_search_start_z_m = {geom['validated_search_start_z']:.5f}\n", "")
    old_entry = """  if stop_reason == 0.0:
    write_output_float_register(35, 21.0)
    local p0 = get_actual_tcp_pose()
    local reference_orientation_pose = p[p0[0], p0[1], p0[2], target_rx, target_ry, target_rz]
    codex_echo_step4e(stop_reason)
    movel(reference_orientation_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
    write_output_float_register(35, 22.0)
    local p1 = get_actual_tcp_pose()
    local entry_xy_pose = p[entry_x, entry_y, p1[2], target_rx, target_ry, target_rz]
    codex_echo_step4e(stop_reason)
    movel(entry_xy_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
    write_output_float_register(35, 22.5)
    local p2 = get_actual_tcp_pose()
    local fixed_search_start_pose = p[p2[0], p2[1], fixed_search_start_z_m, target_rx, target_ry, target_rz]
    codex_echo_step4e(stop_reason)
    movel(fixed_search_start_pose, a=0.030, v=home_return_speed_m_s, r=0.0)
    stopl(0.1)
    sleep(0.20)
    write_output_float_register(35, 23.0)
    codex_echo_step4e(stop_reason)
    write_output_float_register(34, 1.0)
    if not codex_wait_for_rezero_complete(5.0):
      stop_reason = 14.0
    end
    if stop_reason == 0.0:
      sleep(0.20)
      stop_reason = codex_step4e_guard_stop_reason()
    end
  end
"""
    new_entry = """  if stop_reason == 0.0:
    write_output_float_register(35, 22.0)
    local p_current = get_actual_tcp_pose()
    local entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]
    codex_echo_step4e(stop_reason)
    movel(entry_xy_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
    sleep(0.20)
    write_output_float_register(35, 23.0)
    codex_echo_step4e(stop_reason)
    write_output_float_register(34, 1.0)
    if not codex_wait_for_rezero_complete(5.0):
      stop_reason = 14.0
    end
    if stop_reason == 0.0:
      sleep(0.20)
      stop_reason = codex_step4e_guard_stop_reason()
    end
  end
"""
    if old_entry not in script:
        raise RuntimeError("v24 entry scaffold replacement failed")
    return script.replace(old_entry, new_entry)


def v25_seed_normal_loop_script(stamp: str, gen_at: str, geom: dict[str, float]) -> str:
    script = v24_seed_normal_loop_script(stamp, gen_at, geom)
    script = script.replace("v24", "v25").replace("V24", "V25")
    script = script.replace(
        "# Step4e v25 one-step-entry seed-normal TASE minimal reproduction loop.",
        "# Step4e v25 one-step-entry dynamic-search seed-normal TASE minimal reproduction loop.",
    )
    script = script.replace(
        "# PURPOSE: move once to entry XY with target attitude at current Z, then run far/near search, first-contact lift, lifted angular speedl posture adjustment, second touch, 5N acquire and line.",
        "# PURPOSE: move once to entry XY with target attitude at current Z, then compute first far/near search from current Z so near search starts about 30 mm above the target initial point.",
    )
    old_call = """  if stop_reason == 0.0:
    stop_reason = codex_v25_down_search(24.0, 24.2, 0.092, 0.080, 40.000, -0.015, -0.003)
    if stop_reason == 11.0:
      stop_reason = 0.0
    end
  end
"""
    new_call = f"""  if stop_reason == 0.0:
    local target_initial_z_m = {geom['validated_search_start_z']:.5f}
    local first_near_start_z_m = target_initial_z_m + 0.030
    local first_max_end_z_m = target_initial_z_m - 0.012
    local first_search_start_pose = get_actual_tcp_pose()
    local first_search_near_start_depth_m = first_search_start_pose[2] - first_near_start_z_m
    local first_search_max_down_m = first_search_start_pose[2] - first_max_end_z_m
    if first_search_near_start_depth_m < 0.0:
      first_search_near_start_depth_m = 0.0
    end
    if first_search_max_down_m < 0.020:
      first_search_max_down_m = 0.020
    end
    if first_search_max_down_m < first_search_near_start_depth_m + 0.010:
      first_search_max_down_m = first_search_near_start_depth_m + 0.010
    end
    stop_reason = codex_v25_down_search(24.0, 24.2, first_search_max_down_m, first_search_near_start_depth_m, 40.000, -0.015, -0.003)
    if stop_reason == 11.0:
      stop_reason = 0.0
    end
  end
"""
    if old_call not in script:
        raise RuntimeError("v25 first search replacement failed")
    return script.replace(old_call, new_call)


def v26_seed_normal_loop_script(stamp: str, gen_at: str, geom: dict[str, float]) -> str:
    script = v25_seed_normal_loop_script(stamp, gen_at, geom)
    script = script.replace("v25", "v26").replace("V25", "V26")
    script = script.replace(
        "# Step4e v26 one-step-entry dynamic-search seed-normal TASE minimal reproduction loop.",
        "# Step4e v26 one-step-entry reference-z-search seed-normal TASE minimal reproduction loop.",
    )
    script = script.replace(
        "# PURPOSE: move once to entry XY with target attitude at current Z, then compute first far/near search from current Z so near search starts about 30 mm above the target initial point.",
        "# PURPOSE: move once to entry XY with target attitude at current Z, then compute first far/near search so near search starts about 30 mm above the reference target initial point.",
    )
    old_z = f"    local target_initial_z_m = {geom['validated_search_start_z']:.5f}\n"
    new_z = f"    local target_initial_z_m = {geom['target_initial_z']:.9f}\n"
    if old_z not in script:
        raise RuntimeError("v26 target initial Z replacement failed")
    return script.replace(old_z, new_z)


def v27_seed_normal_loop_script(stamp: str, gen_at: str, geom: dict[str, float]) -> str:
    script = v26_seed_normal_loop_script(stamp, gen_at, geom)
    script = script.replace("v26", "v27").replace("V26", "V27")
    script = script.replace(
        "# Step4e v27 one-step-entry reference-z-search seed-normal TASE minimal reproduction loop.",
        "# Step4e v27 one-step-entry force-jump-z-search seed-normal TASE minimal reproduction loop.",
    )
    script = script.replace(
        "# PURPOSE: move once to entry XY with target attitude at current Z, then compute first far/near search so near search starts about 30 mm above the reference target initial point.",
        "# PURPOSE: move once to entry XY with target attitude at current Z, then compute first far/near search so near search starts about 30 mm above the v13 force-jump first-contact point.",
    )
    script = script.replace(
        "# FLOW_TABLE: STEP4E_FLOW.md",
        (
            "# FLOW_TABLE: STEP4E_FLOW.md\n"
            f"# FIRST_CONTACT_Z_EVIDENCE: {V13_FIRST_CONTACT_SOURCE} force jump at z={V13_FIRST_CONTACT_Z_M:.9f} m; "
            f"v16 cross-check z={V16_FIRST_CONTACT_Z_M:.9f} m."
        ),
    )
    script = script.replace(
        "    local target_initial_z_m = {0:.9f}\n"
        "    local first_near_start_z_m = target_initial_z_m + 0.030\n"
        "    local first_max_end_z_m = target_initial_z_m - 0.012\n".format(geom["target_initial_z"]),
        (
            f"    local first_contact_z_m = {V27_FIRST_CONTACT_Z_M:.9f}\n"
            "    local first_near_start_z_m = first_contact_z_m + 0.030\n"
            f"    local first_max_end_z_m = first_contact_z_m - {V27_FIRST_CONTACT_BELOW_MARGIN_M:.3f}\n"
        ),
    )
    if "local target_initial_z_m =" in script:
        raise RuntimeError("v27 first-contact Z replacement failed")
    return script


def v28_seed_normal_loop_script(stamp: str, gen_at: str, geom: dict[str, float]) -> str:
    script = v27_seed_normal_loop_script(stamp, gen_at, geom)
    script = script.replace("v27", "v28").replace("V27", "V28")
    script = script.replace(
        "# Step4e v28 one-step-entry force-jump-z-search seed-normal TASE minimal reproduction loop.",
        "# Step4e v28 50N-guard 25mm-near-search seed-normal TASE minimal reproduction loop.",
    )
    script = script.replace(
        "# PURPOSE: move once to entry XY with target attitude at current Z, then compute first far/near search so near search starts about 30 mm above the v13 force-jump first-contact point.",
        "# PURPOSE: move once to entry XY with target attitude at current Z, then compute first far/near search so near search starts about 25 mm above the v13 force-jump first-contact point at 2.5 mm/s.",
    )
    script = script.replace(
        "# SAFETY: raw normal guard 35 N, force norm guard 50 N, torque guard 3.0 Nm.",
        "# SAFETY: raw normal guard 50 N, force norm guard 50 N, torque guard 3.0 Nm.",
    )
    script = script.replace(
        "elif codex_abs(normal_force) > 35.0:",
        f"elif codex_abs(normal_force) > {V28_RAW_NORMAL_GUARD_N:.1f}:",
    )
    script = script.replace(
        "    local first_near_start_z_m = first_contact_z_m + 0.030\n",
        f"    local first_near_start_z_m = first_contact_z_m + {V28_FIRST_CONTACT_NEAR_MARGIN_M:.3f}\n",
    )
    script = script.replace(
        "    stop_reason = codex_v28_down_search(24.0, 24.2, first_search_max_down_m, first_search_near_start_depth_m, 40.000, -0.015, -0.003)\n",
        f"    stop_reason = codex_v28_down_search(24.0, 24.2, first_search_max_down_m, first_search_near_start_depth_m, 40.000, -0.015, {V28_FIRST_SEARCH_NEAR_SPEED_M_S:.4f})\n",
    )
    return script


def v29_seed_normal_loop_script(stamp: str, gen_at: str, geom: dict[str, float]) -> str:
    script = v28_seed_normal_loop_script(stamp, gen_at, geom)
    script = script.replace("v28", "v29").replace("V28", "V29")
    script = script.replace(
        "# Step4e v29 50N-guard 25mm-near-search seed-normal TASE minimal reproduction loop.",
        "# Step4e v29 bridge-profile-fix 50N-guard 20mm-near-search seed-normal TASE minimal reproduction loop.",
    )
    script = script.replace(
        "# PURPOSE: move once to entry XY with target attitude at current Z, then compute first far/near search so near search starts about 25 mm above the v13 force-jump first-contact point at 2.5 mm/s.",
        "# PURPOSE: bridge-profile-fix + line-entry-gate release; after posture-adjusted second contact, bypass 5N stable acquire and go directly to line control.",
    )
    script = script.replace(
        "# CONTROL: bridge step4e-version=v29 latches the first contact normal, forces 37..39 zero during 25.2, writes angular speedl commands in 40..42, reacquires 5 N in 25.3, and runs line control in 25.0.",
        "# CONTROL: bridge step4e-version=v29 latches the first contact normal, forces 37..39 zero during 25.2, writes angular speedl commands in 40..42, treats 25.3 as a zero-linear line-entry gate, and runs line control in 25.0.",
    )
    script = script.replace("p_lift[2] + 0.030", "p_lift[2] + 0.020")
    script = script.replace(
        f"    local first_near_start_z_m = first_contact_z_m + {V28_FIRST_CONTACT_NEAR_MARGIN_M:.3f}\n",
        f"    local first_near_start_z_m = first_contact_z_m + {V29_FIRST_CONTACT_NEAR_MARGIN_M:.3f}\n",
    )
    linear_zero_helper = """
def codex_wait_for_stage_linear_zero(stage_code, timeout_s):
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
    if read_input_float_register(43) >= 0.5 and codex_abs(read_input_float_register(37)) <= 0.000001 and codex_abs(read_input_float_register(38)) <= 0.000001 and codex_abs(read_input_float_register(39)) <= 0.000001:
      return 0.0
    end
    sync()
    t = t + get_steptime()
  end
  return 12.0
end

"""
    script = script.replace("\ndef codex_v29_down_search(", "\n" + linear_zero_helper + "def codex_v29_down_search(")
    script = script.replace(
        "    stop_reason = codex_wait_for_cmd_valid(25.2, 1.000)\n",
        "    stop_reason = codex_wait_for_stage_linear_zero(25.2, 1.000)\n",
    )
    old_acquire = """  if stop_reason == 0.0:
    write_output_float_register(35, 25.3)
    local last_heartbeat_acquire = read_input_float_register(26)
    local stale_s_acquire = 0.0
    local t_acquire = 0.0
    local acquire_stable_s = 0.0
    local acquire_min_s = 0.200
    local acquire_runtime_limit_s = 8.000
    local acquire_force_error_limit_n = 0.750
    local acquire_stable_required_s = 0.250
    saw_cmd_valid = 0
    cmd_invalid_s = 0.0
    while stop_reason == 0.0:
      local heartbeat_acquire = read_input_float_register(26)
      local cmd_valid = read_input_float_register(43)
      local force_error = read_input_float_register(45)
      local cmd_vx = read_input_float_register(37)
      local cmd_vy = read_input_float_register(38)
      local cmd_vz = read_input_float_register(39)
      local loop_dt = get_steptime()
      if cmd_valid >= 0.5:
        saw_cmd_valid = 1
        cmd_invalid_s = 0.0
      else:
        cmd_invalid_s = cmd_invalid_s + loop_dt
      end
      if heartbeat_acquire == last_heartbeat_acquire:
        stale_s_acquire = stale_s_acquire + loop_dt
      else:
        stale_s_acquire = 0.0
        last_heartbeat_acquire = heartbeat_acquire
      end
      t_acquire = t_acquire + loop_dt
      if t_acquire >= acquire_min_s and codex_abs(force_error) <= acquire_force_error_limit_n:
        acquire_stable_s = acquire_stable_s + loop_dt
      else:
        acquire_stable_s = 0.0
      end
      codex_echo_step4e(stop_reason)
      if stale_s_acquire > 0.100:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if acquire_stable_s >= acquire_stable_required_s:
          stop_reason = 16.0
        elif cmd_valid < 0.5:
          if saw_cmd_valid == 0 and t_acquire < cmd_valid_grace_s:
            speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
          elif saw_cmd_valid == 1 and cmd_invalid_s <= cmd_valid_loss_limit_s:
            speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
          else:
            stop_reason = 12.0
          end
        elif codex_abs(cmd_vx) > 0.010 or codex_abs(cmd_vy) > 0.010 or codex_abs(cmd_vz) > 0.010:
          stop_reason = 13.0
        elif t_acquire >= acquire_runtime_limit_s:
          stop_reason = 10.0
        else:
          speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)
        end
      end
    end
    stopl(0.1)
    if stop_reason == 16.0:
      stop_reason = 0.0
      saw_cmd_valid = 0
      cmd_invalid_s = 0.0
      end_hold_s = 0.0
    end
  end
"""
    new_line_entry = """  if stop_reason == 0.0:
    write_output_float_register(35, 25.3)
    local last_heartbeat_entry = read_input_float_register(26)
    local stale_s_entry = 0.0
    local t_entry = 0.0
    local line_entry_s = 0.0
    local line_entry_required_s = 0.100
    local line_entry_timeout_s = 1.000
    saw_cmd_valid = 0
    cmd_invalid_s = 0.0
    while stop_reason == 0.0:
      local heartbeat_entry = read_input_float_register(26)
      local cmd_valid = read_input_float_register(43)
      local cmd_vx = read_input_float_register(37)
      local cmd_vy = read_input_float_register(38)
      local cmd_vz = read_input_float_register(39)
      local loop_dt = get_steptime()
      if cmd_valid >= 0.5:
        saw_cmd_valid = 1
        cmd_invalid_s = 0.0
      else:
        cmd_invalid_s = cmd_invalid_s + loop_dt
      end
      if heartbeat_entry == last_heartbeat_entry:
        stale_s_entry = stale_s_entry + loop_dt
      else:
        stale_s_entry = 0.0
        last_heartbeat_entry = heartbeat_entry
      end
      t_entry = t_entry + loop_dt
      if cmd_valid >= 0.5 and codex_abs(cmd_vx) <= 0.000001 and codex_abs(cmd_vy) <= 0.000001 and codex_abs(cmd_vz) <= 0.000001:
        line_entry_s = line_entry_s + loop_dt
      else:
        line_entry_s = 0.0
      end
      codex_echo_step4e(stop_reason)
      if stale_s_entry > 0.100:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if line_entry_s >= line_entry_required_s:
          stop_reason = 16.0
        elif cmd_valid < 0.5:
          if saw_cmd_valid == 0 and t_entry < cmd_valid_grace_s:
            sync()
          elif saw_cmd_valid == 1 and cmd_invalid_s <= cmd_valid_loss_limit_s:
            sync()
          else:
            stop_reason = 12.0
          end
        elif codex_abs(cmd_vx) > 0.001 or codex_abs(cmd_vy) > 0.001 or codex_abs(cmd_vz) > 0.001:
          stop_reason = 13.0
        elif t_entry >= line_entry_timeout_s:
          stop_reason = 12.0
        else:
          sync()
        end
      end
    end
    stopl(0.1)
    if stop_reason == 16.0:
      stop_reason = 0.0
      saw_cmd_valid = 0
      cmd_invalid_s = 0.0
      end_hold_s = 0.0
    end
  end
"""
    if old_acquire not in script:
        raise RuntimeError("v29 line-entry gate replacement failed")
    script = script.replace(old_acquire, new_line_entry)
    return script


def v30_seed_normal_loop_script(stamp: str, gen_at: str, geom: dict[str, float]) -> str:
    script = v29_seed_normal_loop_script(stamp, gen_at, geom)
    script = script.replace("v29", "v30").replace("V29", "V30")
    script = script.replace(
        "# Step4e v30 bridge-profile-fix 50N-guard 20mm-near-search seed-normal TASE minimal reproduction loop.",
        "# Step4e v30 filtered-live-normal 50N-guard 20mm-near-search seed-normal TASE minimal reproduction loop.",
    )
    script = script.replace(
        "# PURPOSE: bridge-profile-fix + line-entry-gate release; after posture-adjusted second contact, bypass 5N stable acquire and go directly to line control.",
        "# PURPOSE: filtered-live-normal attitude reference during line control + line-entry-gate release; keep v29 entry/search/line-entry-gate flow and force/admittance parameters unchanged.",
    )
    script = script.replace(
        "# CONTROL: bridge step4e-version=v30 latches the first contact normal, forces 37..39 zero during 25.2, writes angular speedl commands in 40..42, treats 25.3 as a zero-linear line-entry gate, and runs line control in 25.0.",
        "# CONTROL: bridge step4e-version=v30 step4e-normal-follow-mode=filtered_live latches the first contact normal, keeps 25.2 and 25.3 on locked-normal behavior, then uses a gated filtered live normal only during 25.0 line control.",
    )
    return script


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp-prefix", default=None)
    parser.add_argument(
        "--program",
        choices=(
            "all",
            "step4e_ball_first_contact_p0_v1",
            "step4e_ball_vs_cyl_contact_p0_v1",
            "step4e_attitude_axis_iso_v1",
            "step4e_detached_movel_minrot_v21",
            "step4e_seed_normal_loop_v22",
            "step4e_seed_normal_loop_v23",
            "step4e_seed_normal_loop_v24",
            "step4e_seed_normal_loop_v25",
            "step4e_seed_normal_loop_v26",
            "step4e_seed_normal_loop_v27",
            "step4e_seed_normal_loop_v28",
            "step4e_seed_normal_loop_v29",
            "step4e_seed_normal_loop_v30",
        ),
        default="all",
    )
    parser.add_argument(
        "--program-subdir",
        default=None,
        help=(
            "Optional local/controller subdirectory under the Step4 program directory. "
            "When omitted, current packages are generated in the Step4 root and archived "
            "v21/v22 packages are generated under step4e."
        ),
    )
    parser.add_argument("--pose-pair", type=Path, default=None)
    args = parser.parse_args()
    now = datetime.now(timezone(timedelta(hours=8)))
    geom = line_cfg(load_json(CONFIG_PATH))
    pose_pair = None
    specs = [
        (
            "step4e_ball_first_contact_p0_v1",
            "BALL_FIRST_CONTACT_P0_V1",
            "P0-geo ball-first contact witness; no attitude, no 5N acquire, no line",
            ball_first_contact_script,
        ),
        (
            "step4e_ball_vs_cyl_contact_p0_v1",
            "BALL_VS_CYL_CONTACT_P0_V1",
            "P0 witness pair: ball contact then housing/cylindrical-face contact",
            ball_vs_cyl_contact_script,
        ),
        (
            "step4e_attitude_axis_iso_v1",
            "AXIS_ISO_V1",
            "P0-b no-contact four-quadrant orientation-axis isolation",
            axis_iso_script,
        ),
        (
            "step4e_detached_movel_minrot_v21",
            "DETACHED_MOVEL_MINROT_V21",
            "P1-a latch, detach, minimal-rotation target preview; no contact-posture motion",
            v21_detached_movel_script,
        ),
        (
            "step4e_seed_normal_loop_v22",
            "SEED_NORMAL_LOOP_V22",
            "marked path-start XYZ with manually marked near-normal rotvec, first touch, 50 mm lift, optional normal alignment, second touch, 5N acquire and line",
            v22_seed_normal_loop_script,
        ),
        (
            "step4e_seed_normal_loop_v23",
            "SEED_NORMAL_LOOP_V23",
            "v13-safe entry/search with near-normal rotvec, first touch, 50 mm lift, angular speedl alignment, second touch, 5N acquire and line",
            v23_seed_normal_loop_script,
        ),
        (
            "step4e_seed_normal_loop_v24",
            "SEED_NORMAL_LOOP_V24",
            "one-step entry XY plus target attitude at current Z, far/near search, first touch, 30 mm lift, angular speedl alignment, second touch, 5N acquire and line",
            v24_seed_normal_loop_script,
        ),
        (
            "step4e_seed_normal_loop_v25",
            "SEED_NORMAL_LOOP_V25",
            "one-step entry XY plus target attitude at current Z, dynamic first far/near search, first touch, 30 mm lift, angular speedl alignment, second touch, 5N acquire and line",
            v25_seed_normal_loop_script,
        ),
        (
            "step4e_seed_normal_loop_v26",
            "SEED_NORMAL_LOOP_V26",
            "one-step entry XY plus target attitude at current Z, reference-Z first far/near search, first touch, 30 mm lift, angular speedl alignment, second touch, 5N acquire and line",
            v26_seed_normal_loop_script,
        ),
        (
            "step4e_seed_normal_loop_v27",
            "SEED_NORMAL_LOOP_V27",
            "one-step entry XY plus target attitude at current Z, v13 force-jump first-contact-Z search, first touch, 30 mm lift, angular speedl alignment, second touch, 5N acquire and line",
            v27_seed_normal_loop_script,
        ),
        (
            "step4e_seed_normal_loop_v28",
            "SEED_NORMAL_LOOP_V28",
            "one-step entry XY plus target attitude at current Z, v13 force-jump first-contact-Z search with 25 mm near window, 2.5 mm/s near descent, 50 N raw-normal guard, first touch, 30 mm lift, angular speedl alignment, second touch, 5N acquire and line",
            v28_seed_normal_loop_script,
        ),
        (
            "step4e_seed_normal_loop_v29",
            "SEED_NORMAL_LOOP_V29",
            "bridge-profile-fix + line-entry-gate release: one-step entry XY plus target attitude at current Z, v13 force-jump first-contact-Z search with 20 mm near window, 2.5 mm/s near descent, 50 N raw-normal guard, first touch, 20 mm lift, angular speedl alignment, second touch, zero-linear 25.3 gate, then line",
            v29_seed_normal_loop_script,
        ),
        (
            "step4e_seed_normal_loop_v30",
            "SEED_NORMAL_LOOP_V30",
            "filtered-live-normal line-control profile: v29 entry/search/25.2/25.3 flow unchanged, then bridge follows a gated filtered live normal during 25.0 only",
            v30_seed_normal_loop_script,
        ),
    ]
    if args.program != "all":
        specs = [spec for spec in specs if spec[0] == args.program]
    generated = {}
    for name, suffix, description, script_fn in specs:
        program_subdir = args.program_subdir if args.program_subdir is not None else program_default_subdir(name)
        local_program_dir = PROGRAM_DIR / program_subdir if program_subdir else PROGRAM_DIR
        controller_dir = f"{CONTROLLER_BASE_DIR}/{program_subdir}" if program_subdir else CONTROLLER_BASE_DIR
        local_program_dir.mkdir(parents=True, exist_ok=True)
        stamp = args.stamp_prefix or source_stamp(suffix, now)
        if name == "step4e_ball_vs_cyl_contact_p0_v1":
            if pose_pair is None:
                pose_pair = load_json(args.pose_pair or default_pose_pair_path())
            script = script_fn(stamp, generated_at(now), pose_pair)
        elif name in {
            "step4e_ball_first_contact_p0_v1",
            "step4e_detached_movel_minrot_v21",
            "step4e_seed_normal_loop_v22",
            "step4e_seed_normal_loop_v23",
            "step4e_seed_normal_loop_v24",
            "step4e_seed_normal_loop_v25",
            "step4e_seed_normal_loop_v26",
            "step4e_seed_normal_loop_v27",
            "step4e_seed_normal_loop_v28",
            "step4e_seed_normal_loop_v29",
            "step4e_seed_normal_loop_v30",
        }:
            script = script_fn(stamp, generated_at(now), geom)
        else:
            script = script_fn(stamp, generated_at(now))
        txt = build_txt(name, stamp, description)
        urp = build_urp(script, name, controller_dir)
        validate_package(name, script, txt, urp, stamp, controller_dir)
        script_path = local_program_dir / f"{name}.script"
        txt_path = local_program_dir / f"{name}.txt"
        urp_path = local_program_dir / f"{name}.urp"
        script_path.write_text(script, encoding="utf-8")
        txt_path.write_text(txt, encoding="utf-8")
        urp_path.write_bytes(urp)
        generated[name] = {
            "script": str(script_path),
            "txt": str(txt_path),
            "urp": str(urp_path),
            "controller_script": f"{controller_dir}/{name}.script",
            "controller_urp": f"{controller_dir}/{name}.urp",
            "stamp": stamp,
        }
    print(json.dumps({"generated": generated}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
