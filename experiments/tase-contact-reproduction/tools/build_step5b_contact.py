#!/usr/bin/env python3
"""Generate Step5b contact cycloid baseline TP package."""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from build_step4e_line_programs import CONFIG_PATH, PROGRAM_DIR, generated_at, line_cfg, load_json
from build_step4e_p0p1_programs import build_urp, step4fg_seed_normal_loop_script
from step5_table import load_stage_frame, step5_stage


PROGRAM_NAME = "step5b_contact_cycloid_baseline_v2"
PROGRAM_NAME_V3 = "step5b_contact_cycloid_baseline_v3"
STEP5_STAGE_ID = "step5_contact_cycloid_baseline_v1"
BRIDGE_VERSION = "step5b_v2"
BRIDGE_VERSION_V3 = "step5b_v3"
LOCAL_PROGRAM_DIR = PROGRAM_DIR / "step5"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
SAFE_FRAME_PATH = CONFIG_PATH.with_name("step5_safe_frame.json")
FAST_NONCONTACT_MOVEL_ACCEL_M_S2 = 0.060
FAST_NONCONTACT_MOVEL_SPEED_M_S = 0.040
HOME_RETURN_SPEED_M_S = 0.050
TARGET_FORCE_N = 15.0
TARGET_FORCE_N_V3 = 12.0
BRIDGE_NORMAL_FILTER_ALPHA = 0.70
BRIDGE_NORMAL_FILTER_ALPHA_V3 = 0.55
ORIENTATION_SKIP_ERROR_RAD = 0.069813


def variant_program_name(variant: str) -> str:
    if variant == "v2":
        return PROGRAM_NAME
    if variant == "v3":
        return PROGRAM_NAME_V3
    raise ValueError(f"unsupported Step5b variant: {variant!r}")


def variant_bridge_version(variant: str) -> str:
    if variant == "v2":
        return BRIDGE_VERSION
    if variant == "v3":
        return BRIDGE_VERSION_V3
    raise ValueError(f"unsupported Step5b variant: {variant!r}")


def variant_title(variant: str) -> str:
    if variant == "v2":
        return "Step5b contact cycloid baseline v2"
    if variant == "v3":
        return "Step5b contact cycloid baseline v3"
    raise ValueError(f"unsupported Step5b variant: {variant!r}")


def variant_stamp_token(variant: str) -> str:
    if variant == "v2":
        return "STEP5B_CONTACT_CYCLOID_BASELINE_V2"
    if variant == "v3":
        return "STEP5B_CONTACT_CYCLOID_BASELINE_V3"
    raise ValueError(f"unsupported Step5b variant: {variant!r}")


def variant_target_force_n(variant: str) -> float:
    if variant == "v2":
        return TARGET_FORCE_N
    if variant == "v3":
        return TARGET_FORCE_N_V3
    raise ValueError(f"unsupported Step5b variant: {variant!r}")


def variant_filter_alpha(variant: str) -> float:
    if variant == "v2":
        return BRIDGE_NORMAL_FILTER_ALPHA
    if variant == "v3":
        return BRIDGE_NORMAL_FILTER_ALPHA_V3
    raise ValueError(f"unsupported Step5b variant: {variant!r}")


def source_stamp(now: datetime, variant: str = "v2") -> str:
    return now.strftime(f"%Y-%m-%dT%H%MHKT_{variant_stamp_token(variant)}")


def load_safe_frame() -> dict:
    return load_stage_frame(step5_stage(STEP5_STAGE_ID))


def add_orientation_skip_gate(script: str, variant: str = "v2") -> str:
    marker = """  if stop_reason == 0.0:
    write_output_float_register(35, 25.1)
    local p_lift = get_actual_tcp_pose()
    local lift_pose = p[p_lift[0], p_lift[1], p_lift[2] + 0.020, p_lift[3], p_lift[4], p_lift[5]]
    codex_echo_step4e(stop_reason)
    movel(lift_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 25.2)"""
    program_name = variant_program_name(variant)
    if marker not in script:
        raise RuntimeError(f"{program_name} orientation skip insertion point not found")
    if variant == "v3":
        end_marker = """  if stop_reason == 0.0:
    stop_reason = codex_step5b_down_search(24.3, 24.4, 0.070, 0.040, 45.000, -0.005, -0.003)
    if stop_reason == 11.0:
      stop_reason = 0.0
    end
  end

"""
        start = script.index(marker)
        end = script.index(end_marker, start) + len(end_marker)
        replacement = """  if stop_reason == 0.0:
    write_output_float_register(35, 25.15)
    codex_echo_step4e(stop_reason)
    sync()
  end

"""
        return script[:start] + replacement + script[end:]
    replacement = f"""  local skip_lift_attitude = 0
  if stop_reason == 0.0:
    write_output_float_register(35, 25.15)
    local orientation_skip_error_rad = {ORIENTATION_SKIP_ERROR_RAD:.6f}
    local t_skip = 0.0
    local skip_timeout_s = 1.000
    local last_heartbeat_skip = read_input_float_register(26)
    local stale_s_skip = 0.0
    while stop_reason == 0.0 and skip_lift_attitude == 0 and t_skip < skip_timeout_s:
      local heartbeat_skip = read_input_float_register(26)
      local cmd_valid = read_input_float_register(43)
      local orientation_error = read_input_float_register(46)
      local loop_dt = get_steptime()
      if heartbeat_skip == last_heartbeat_skip:
        stale_s_skip = stale_s_skip + loop_dt
      else:
        stale_s_skip = 0.0
        last_heartbeat_skip = heartbeat_skip
      end
      t_skip = t_skip + loop_dt
      codex_echo_step4e(stop_reason)
      if stale_s_skip > 0.100:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if cmd_valid >= 0.5 and orientation_error <= orientation_skip_error_rad:
          skip_lift_attitude = 1
        elif cmd_valid >= 0.5:
          t_skip = skip_timeout_s
        else:
          sync()
        end
      end
    end
  end

  if stop_reason == 0.0 and skip_lift_attitude == 0:
    write_output_float_register(35, 25.1)
    local p_lift = get_actual_tcp_pose()
    local lift_pose = p[p_lift[0], p_lift[1], p_lift[2] + 0.020, p_lift[3], p_lift[4], p_lift[5]]
    codex_echo_step4e(stop_reason)
    movel(lift_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
  end

  if stop_reason == 0.0 and skip_lift_attitude == 0:
    write_output_float_register(35, 25.2)"""
    return script.replace(marker, replacement, 1)


def build_script(stamp: str, gen_at: str, geom: dict[str, float], frame: dict, variant: str = "v2") -> str:
    basis = frame["basis"]
    guard = frame["guard"]
    entry_x, entry_y = [float(v) for v in basis["origin_xy_m"]]
    stage = step5_stage(STEP5_STAGE_ID)
    duration_s = float(stage["duration_s"])
    omega = float(stage["phase_law"]["omega_rad_s"])
    amplitude = float(stage["amplitude_m"])
    program_name = variant_program_name(variant)
    bridge_version = variant_bridge_version(variant)
    title = variant_title(variant)
    filter_alpha = variant_filter_alpha(variant)
    script = step4fg_seed_normal_loop_script(
        stamp=stamp,
        gen_at=gen_at,
        geom=geom,
        name=program_name,
        title=title,
        version_token=bridge_version,
        path_shape="cycloid",
        path_formula=(
            f"Step5 table stage {STEP5_STAGE_ID}: local-basis x={amplitude:.3f} * "
            f"({omega:.1f}t - sin({omega:.1f}t)), y={amplitude:.3f} * "
            f"(1 - cos({omega:.1f}t)), duration={duration_s:.0f}s"
        ),
        down_search_fn="codex_step5b_down_search",
    )
    replacements = {
        "# FLOW_TABLE: STEP4E_FLOW.md": (
            "# FLOW_TABLE: STEP5_FLOW.md\n"
            f"# STEP5_STAGE_ID: {STEP5_STAGE_ID}\n"
            "# STEP5_TABLE_SOURCE: config/step5_stage_table.json\n"
            "# TP_ROLE: executor_and_guard_only; Step5 trajectory reference is computed by the bridge."
        ),
        "# PAPER_PATH_SOURCE: TASE paper Section VI-A; formula is used directly, not digitized from screenshots.\n": "",
        f"# ENTRY_XY_M: [{geom['start_x']:.9f}, {geom['start_y']:.9f}]": (
            f"# ENTRY_XY_M: [{entry_x:.9f}, {entry_y:.9f}]\n"
            "# SAFE_FRAME_SOURCE: config/step5_safe_frame.json from confirmed drag-teach start/mid/end.\n"
            "# NO_SCALE_POLICY: Step5 table frame uses rotation+translation only.\n"
            f"# X_GUARD: guard_line={float(guard['guard_line_x_m']):.9f} m, "
            f"path_max_x={float(guard['path_max_x_m']):.9f} m."
        ),
        f"local entry_x = {geom['start_x']:.9f}": f"local entry_x = {entry_x:.9f}",
        f"local entry_y = {geom['start_y']:.9f}": f"local entry_y = {entry_y:.9f}",
    }
    for old, new in replacements.items():
        if old not in script:
            raise RuntimeError(f"{program_name} scaffold replacement failed: {old}")
        script = script.replace(old, new, 1)
    script = script.replace(
        "paper-derived cycloid XY reference for 60 s",
        "Step5 table-driven contact cycloid reference for 60 s",
    )
    if variant == "v3":
        script = script.replace(
            "first-contact normal latch, lift, 25.2 attitude correction,",
            "first-contact normal latch, no lift/25.2 attitude cycle and no second contact search,",
            1,
        )
    else:
        script = script.replace(
            "first-contact normal latch, lift, 25.2 attitude correction,",
            "first-contact normal latch, optional 4deg skip-lift/25.2 gate, otherwise lift and 25.2 attitude correction,",
            1,
        )
    script = script.replace(
        "PAPER_PATH_FORMULA:",
        "STEP5_PATH_FORMULA:",
    )
    script = script.replace(
        "step4e-normal-filter-alpha=0.35",
        f"step4e-normal-filter-alpha={filter_alpha:.2f}",
        1,
    )
    script = add_orientation_skip_gate(script, variant=variant)
    script = script.replace("local short_retract_speed_m_s = 0.020", f"local short_retract_speed_m_s = {FAST_NONCONTACT_MOVEL_SPEED_M_S:.3f}")
    script = script.replace("local home_return_speed_m_s = 0.050", f"local home_return_speed_m_s = {HOME_RETURN_SPEED_M_S:.3f}")
    script = script.replace(
        "movel(entry_xy_pose, a=0.030, v=0.020, r=0.0)",
        f"movel(entry_xy_pose, a={FAST_NONCONTACT_MOVEL_ACCEL_M_S2:.3f}, v={FAST_NONCONTACT_MOVEL_SPEED_M_S:.3f}, r=0.0)",
    )
    script = script.replace(
        "movel(lift_pose, a=0.030, v=0.020, r=0.0)",
        f"movel(lift_pose, a={FAST_NONCONTACT_MOVEL_ACCEL_M_S2:.3f}, v={FAST_NONCONTACT_MOVEL_SPEED_M_S:.3f}, r=0.0)",
    )
    script = script.replace(
        "movel(short_retract_pose, a=0.030, v=short_retract_speed_m_s, r=0.0)",
        f"movel(short_retract_pose, a={FAST_NONCONTACT_MOVEL_ACCEL_M_S2:.3f}, v=short_retract_speed_m_s, r=0.0)",
    )
    return script


def build_txt(stamp: str, variant: str = "v2") -> str:
    program_name = variant_program_name(variant)
    bridge_version = variant_bridge_version(variant)
    filter_alpha = variant_filter_alpha(variant)
    target_force_n = variant_target_force_n(variant)
    if variant == "v3":
        motion_detail = (
            "  Reuses the current v31 contact search and first-contact normal latch.\n"
            "  Stage 25.15 is a no-lift/no-attitude marker only; the program does not run\n"
            "  the 20 mm lift, 25.2 attitude correction, or 24.3/24.4 second contact search.\n"
            "  Stage 25.3 line-entry gate then releases Stage 25.0."
        )
        non_contact_speed = "entry/retract"
    else:
        motion_detail = (
            "  Reuses the current v31 contact search and first-contact normal latch.\n"
            f"  Stage 25.15 skips the 20 mm lift and 25.2 attitude correction when bridge\n"
            f"  orientation_error <= {ORIENTATION_SKIP_ERROR_RAD:.6f} rad; otherwise it runs the old lift,\n"
            "  25.2 attitude correction, second contact, and 25.3 line-entry gate."
        )
        non_contact_speed = "entry/lift/retract"
    return f"""Step5b contact cycloid baseline {variant} TP package

Open on Teach Pendant:
  {CONTROLLER_DIR}/{program_name}.urp

Version:
  {stamp}

Motion boundary:
  Contact motion.
{motion_detail}
  Stage 25.0 consumes bridge command registers 37..44 only.
  Bridge profile: --step4e-version {bridge_version} --step4e-path-shape cycloid.
  Bridge normal filter alpha: --step4e-normal-filter-alpha {filter_alpha:.2f}.
  Non-contact movel speed: {non_contact_speed} {FAST_NONCONTACT_MOVEL_SPEED_M_S:.3f} m/s,
  accel {FAST_NONCONTACT_MOVEL_ACCEL_M_S2:.3f} m/s^2; contact search/acquire/Stage 25.0 unchanged.
  Force target: --target-force-n {target_force_n:.1f}.
  Raw normal guard: 50 N. Force norm guard: 60 N. Torque guard: 3.0 Nm.
  No UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.

Reference:
  STEP5_FLOW.md
  config/step5_stage_table.json stage {STEP5_STAGE_ID}
"""


def validate_package(script: str, txt: str, urp: bytes, stamp: str, variant: str = "v2") -> None:
    xml = gzip.decompress(urp).decode("utf-8")
    program_name = variant_program_name(variant)
    bridge_version = variant_bridge_version(variant)
    filter_alpha = variant_filter_alpha(variant)
    target_force_n = variant_target_force_n(variant)
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": f'URProgram name="{program_name}"' in xml,
        "controller directory": f'directory="{CONTROLLER_DIR}"' in xml,
        "script file": f"{CONTROLLER_DIR}/{program_name}.script" in xml,
        "cached stamp": stamp in xml,
        "function name": f"def codex_{program_name}()" in script,
        "step5 flow": "STEP5_FLOW.md" in script and "STEP5_FLOW.md" in txt,
        "step5 stage": STEP5_STAGE_ID in script and STEP5_STAGE_ID in txt,
        "bridge profile": f"step4e-version={bridge_version}" in script
        and f"--step4e-version {bridge_version}" in txt,
        "bridge filter alpha": f"step4e-normal-filter-alpha={filter_alpha:.2f}" in script
        and f"--step4e-normal-filter-alpha {filter_alpha:.2f}" in txt,
        "path source": "STEP5_TABLE_SOURCE: config/step5_stage_table.json" in script,
        "executor only": "TP_ROLE: executor_and_guard_only" in script,
        "line runtime": "local line_runtime_limit_s = 65.000" in script,
        "line success": "local line_success_progress_m = 60.000000000" in script,
        "v31 scaffold": "first-contact normal latch" in script and "25.3 line-entry gate" in script,
        "fast non-contact movel": "movel(entry_xy_pose, a=0.060, v=0.040, r=0.0)" in script
        and "local short_retract_speed_m_s = 0.040" in script
        and "movel(short_retract_pose, a=0.060, v=short_retract_speed_m_s, r=0.0)" in script
        and (
            "Non-contact movel speed: entry/lift/retract 0.040 m/s" in txt
            if variant == "v2"
            else "Non-contact movel speed: entry/retract 0.040 m/s" in txt
        ),
        "command registers": "read_input_float_register(37)" in script
        and "read_input_float_register(44)" in script,
        "raw guards": "codex_abs(normal_force) > 50.0" in script
        and "force_norm > 60.0" in script
        and "torque_norm > 3.0" in script,
        "force target": f"Force target: --target-force-n {target_force_n:.1f}." in txt,
        "no stale step4 package names": "step4f_cycloid_seed_normal_v1" not in script
        and "step4g_eight_seed_normal_v1" not in script,
    }
    if variant == "v2":
        checks.update(
            {
                "v2 lift scaffold": "25.2 attitude correction" in script
                and "movel(lift_pose, a=0.060, v=0.040, r=0.0)" in script,
                "orientation skip gate": "local skip_lift_attitude = 0" in script
                and "write_output_float_register(35, 25.15)" in script
                and f"local orientation_skip_error_rad = {ORIENTATION_SKIP_ERROR_RAD:.6f}" in script
                and "if stop_reason == 0.0 and skip_lift_attitude == 0:" in script,
            }
        )
    elif variant == "v3":
        checks.update(
            {
                "v3 no lift": "write_output_float_register(35, 25.1)" not in script
                and "p_lift[2] + 0.020" not in script
                and "movel(lift_pose" not in script,
                "v3 no attitude cycle": "write_output_float_register(35, 25.2)" not in script
                and "local orientation_runtime_limit_s = 8.000" not in script
                and "speedl([0.0, 0.0, 0.0, cmd_wx, cmd_wy, cmd_wz]" not in script,
                "v3 no second search": "codex_step5b_down_search(24.3, 24.4" not in script,
                "v3 marker": "write_output_float_register(35, 25.15)" in script,
                "v3 no skip variable": "local skip_lift_attitude = 0" not in script,
            }
        )
    else:
        raise ValueError(f"unsupported Step5b variant: {variant!r}")
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"{program_name} validation failed: {failed}")


def write_outputs(stamp: str, gen_at: str, variant: str = "v2") -> dict[str, str]:
    frame = load_safe_frame()
    geom = line_cfg(load_json(CONFIG_PATH))
    program_name = variant_program_name(variant)
    script = build_script(stamp, gen_at, geom, frame, variant=variant)
    txt = build_txt(stamp, variant=variant)
    urp = build_urp(script, program_name, CONTROLLER_DIR)
    validate_package(script, txt, urp, stamp, variant=variant)

    LOCAL_PROGRAM_DIR.mkdir(parents=True, exist_ok=True)
    script_path = LOCAL_PROGRAM_DIR / f"{program_name}.script"
    txt_path = LOCAL_PROGRAM_DIR / f"{program_name}.txt"
    urp_path = LOCAL_PROGRAM_DIR / f"{program_name}.urp"

    script_path.write_text(script, encoding="utf-8")
    txt_path.write_text(txt, encoding="utf-8")
    urp_path.write_bytes(urp)
    return {
        "script": str(script_path),
        "txt": str(txt_path),
        "urp": str(urp_path),
        "controller_urp": f"{CONTROLLER_DIR}/{program_name}.urp",
        "stamp": stamp,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp-prefix", default=None)
    parser.add_argument("--variant", choices=("v2", "v3"), default="v2")
    args = parser.parse_args()
    now = datetime.now(timezone(timedelta(hours=8)))
    stamp = args.stamp_prefix or source_stamp(now, variant=args.variant)
    result = write_outputs(stamp, generated_at(now), variant=args.variant)
    print(json.dumps({"generated": {variant_program_name(args.variant): result}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
