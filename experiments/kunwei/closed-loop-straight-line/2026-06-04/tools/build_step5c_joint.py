#!/usr/bin/env python3
"""Generate Step5c joint-space speedj TP packages."""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import build_step5b_contact as step5b
from build_step4e_line_programs import CONFIG_PATH, PROGRAM_DIR, generated_at, line_cfg, load_json
from build_step4e_p0p1_programs import build_urp, step4e_current_common_functions
from step5_table import load_stage_frame, step5_stage
from step5c_joint_rnn import CONTACT_STAGE_ID, DRYRUN_STAGE_ID, numeric_sanity


LOCAL_PROGRAM_DIR = PROGRAM_DIR / "step5"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
DRYRUN_PROGRAM = "step5c_speedj_dryrun_v1"
CONTACT_PROGRAM = "step5c_joint_rnn_cycloid_v1"
DRYRUN_BRIDGE_VERSION = DRYRUN_STAGE_ID
CONTACT_BRIDGE_VERSION = CONTACT_STAGE_ID


def source_stamp(now: datetime, program: str) -> str:
    suffix = program.upper()
    return now.strftime(f"%Y-%m-%dT%H%MHKT_{suffix}")


def load_step5c_frame(stage_id: str) -> dict:
    return load_stage_frame(step5_stage(stage_id))


def dryrun_script(stamp: str, gen_at: str, geom: dict[str, float], frame: dict) -> str:
    basis = frame["basis"]
    guard = frame["guard"]
    entry_x, entry_y = [float(v) for v in basis["origin_xy_m"]]
    stage = step5_stage(DRYRUN_STAGE_ID)
    duration_s = float(stage["duration_s"])
    qdot_cap = float(stage["guard"]["qdot_cap_rad_s"])
    runtime_limit_s = float(stage["guard"]["runtime_limit_s"])
    omega = float(stage["phase_law"]["omega_rad_s"])
    amplitude = float(stage["amplitude_m"])
    return f"""# Step5c speedj dry-run v1.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: {gen_at}
# FLOW_TABLE: STEP5_FLOW.md
# STEP5_STAGE_ID: {DRYRUN_STAGE_ID}
# STEP5_TABLE_SOURCE: config/step5_stage_table.json
# SAFE_FRAME_SOURCE: config/step5_safe_frame.json from confirmed drag-teach start/mid/end.
# TP_ROLE: joint_executor_and_guard_only; Step5c trajectory and IK are computed by the bridge.
# REGISTER_CONTRACT: 37..42=qd0..qd5 rad/s, 43=cmd_valid, 44=path_time, 45=force_error, 46=pose_error, 47=solver_status.
# STEP5_PATH_FORMULA: local-basis x={amplitude:.3f} * ({omega:.1f}t - sin({omega:.1f}t)), y={amplitude:.3f} * (1 - cos({omega:.1f}t)), duration={duration_s:.0f}s dry-run subset.
# ENTRY_XY_M: [{entry_x:.9f}, {entry_y:.9f}]
# X_GUARD: guard_line={float(guard['guard_line_x_m']):.9f} m, path_max_x={float(guard['path_max_x_m']):.9f} m.
# CONTROL: bridge step4e-version={DRYRUN_BRIDGE_VERSION} --step4e-path-shape cycloid; no force term.
# SAFETY: no contact search, no UR zero_ftsensor(), no Kunwei tare/config, no TCP/payload write.
{step4e_current_common_functions("12.0")}

def codex_step5c_speedj_dryrun_v1():
  local stop_reason = 0.0
  local home_pose = get_actual_tcp_pose()
  local line_hold_s = 0.002
  local joint_accel_rad_s2 = 0.300
  local qdot_cap_rad_s = {qdot_cap:.3f}
  local line_runtime_limit_s = {runtime_limit_s:.3f}
  local line_success_progress_s = {duration_s:.9f}
  local end_hold_required_s = 0.100
  local end_hold_s = 0.0
  local cmd_valid_grace_s = 0.250
  local cmd_valid_loss_limit_s = 0.100
  local cmd_invalid_s = 0.0
  local saw_cmd_valid = 0
  local final_progress_s = 0.0

  textmsg("codex step5c version {stamp} start speedj dryrun")
  write_output_float_register(34, 0.0)
  write_output_float_register(35, 20.0)
  codex_echo_step4e(0.0)
  if not codex_wait_for_fresh_heartbeat(30.0):
    stop_reason = 3.0
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 22.0)
    local p_current = get_actual_tcp_pose()
    local entry_pose = p[{entry_x:.9f}, {entry_y:.9f}, p_current[2], p_current[3], p_current[4], p_current[5]]
    codex_echo_step4e(stop_reason)
    movel(entry_pose, a=0.030, v=0.020, r=0.0)
    stopl(0.1)
  end

  if stop_reason == 0.0:
    write_output_float_register(35, 25.0)
    local last_heartbeat = read_input_float_register(26)
    local stale_s = 0.0
    local t = 0.0
    while stop_reason == 0.0:
      local heartbeat = read_input_float_register(26)
      local cmd_valid = read_input_float_register(43)
      local progress_s = read_input_float_register(44)
      local cmd_qd0 = read_input_float_register(37)
      local cmd_qd1 = read_input_float_register(38)
      local cmd_qd2 = read_input_float_register(39)
      local cmd_qd3 = read_input_float_register(40)
      local cmd_qd4 = read_input_float_register(41)
      local cmd_qd5 = read_input_float_register(42)
      local loop_dt = get_steptime()
      final_progress_s = progress_s
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
      if progress_s >= line_success_progress_s:
        end_hold_s = end_hold_s + loop_dt
      else:
        end_hold_s = 0.0
      end
      codex_echo_step4e(stop_reason)
      if stale_s > 0.100:
        stop_reason = 2.0
      else:
        stop_reason = codex_step4e_guard_stop_reason()
      end
      if stop_reason == 0.0:
        if cmd_valid < 0.5:
          if saw_cmd_valid == 0 and t < cmd_valid_grace_s:
            speedj([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], joint_accel_rad_s2, line_hold_s)
          elif saw_cmd_valid == 1 and cmd_invalid_s <= cmd_valid_loss_limit_s:
            speedj([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], joint_accel_rad_s2, line_hold_s)
          else:
            stop_reason = 12.0
          end
        elif codex_abs(cmd_qd0) > qdot_cap_rad_s or codex_abs(cmd_qd1) > qdot_cap_rad_s or codex_abs(cmd_qd2) > qdot_cap_rad_s or codex_abs(cmd_qd3) > qdot_cap_rad_s or codex_abs(cmd_qd4) > qdot_cap_rad_s or codex_abs(cmd_qd5) > qdot_cap_rad_s:
          stop_reason = 13.0
        elif end_hold_s >= end_hold_required_s:
          stop_reason = 1.0
        elif t >= line_runtime_limit_s:
          stop_reason = 10.0
        else:
          speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5], joint_accel_rad_s2, line_hold_s)
        end
      end
    end
    stopj(0.3)
  end

  if codex_should_auto_home(stop_reason):
    write_output_float_register(35, 27.0)
    codex_echo_step4e(stop_reason)
    movel(home_pose, a=0.030, v=0.030, r=0.0)
    stopl(0.1)
  end

  write_output_float_register(30, stop_reason)
  write_output_float_register(31, final_progress_s)
  write_output_float_register(35, 29.0)
  textmsg("codex step5c version {stamp} stop reason:", stop_reason)
end

codex_step5c_speedj_dryrun_v1()
"""


def contact_script(stamp: str, gen_at: str, geom: dict[str, float], frame: dict) -> str:
    script = step5b.build_script(stamp, gen_at, geom, frame)
    stage = step5_stage(CONTACT_STAGE_ID)
    qdot_cap = float(stage["guard"]["qdot_cap_rad_s"])
    replacements = {
        "Step5b contact cycloid baseline v1": "Step5c joint-RNN cycloid v1",
        "step5b_contact_cycloid_baseline_v1": CONTACT_PROGRAM,
        "codex_step5b_down_search": "codex_step5c_down_search",
        "step4e-version=step5b_v1": f"step4e-version={CONTACT_BRIDGE_VERSION}",
        "step4e-version=step5c_joint_rnn_cycloid_v1 --step4e-path-shape cycloid step4e-normal-follow-mode": "step4e-version=step5c_joint_rnn_cycloid_v1 --step4e-path-shape cycloid --step5c-qdot-limit-rad-s 0.15 step4e-normal-follow-mode",
        "STEP5_STAGE_ID: step5_contact_cycloid_baseline_v1": f"STEP5_STAGE_ID: {CONTACT_STAGE_ID}",
        "Step5 table stage step5_contact_cycloid_baseline_v1": f"Step5 table stage {CONTACT_STAGE_ID}",
        "TP_ROLE: executor_and_guard_only; Step5 trajectory reference is computed by the bridge.": "TP_ROLE: joint_executor_and_guard_only; Step5c trajectory, force feedback, and IK are computed by the bridge.",
        "Step5 table-driven contact cycloid reference for 60 s": "Step5c joint-space contact cycloid reference for 60 s",
        "local max_cmd_angular_xy_rad_s = 0.120": f"local max_cmd_qd_rad_s = {qdot_cap:.3f}",
        "local line_accel_m_s2 = 0.300": "local line_accel_m_s2 = 0.300\n  local joint_accel_rad_s2 = 0.300",
    }
    for old, new in replacements.items():
        if old not in script:
            raise RuntimeError(f"{CONTACT_PROGRAM} scaffold replacement failed: {old}")
        script = script.replace(old, new)

    for old, new in {
        "cmd_vx": "cmd_qd0",
        "cmd_vy": "cmd_qd1",
        "cmd_vz": "cmd_qd2",
        "cmd_wx": "cmd_qd3",
        "cmd_wy": "cmd_qd4",
        "cmd_wz": "cmd_qd5",
        "progress_m": "progress_s",
        "final_progress_m": "final_progress_s",
        "line_success_progress_m": "line_success_progress_s",
    }.items():
        script = script.replace(old, new)

    script = script.replace(
        "speedl([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], line_accel_m_s2, line_hold_s)",
        "speedj([0.0, 0.0, 0.0, 0.0, 0.0, 0.0], joint_accel_rad_s2, line_hold_s)",
    )
    script = script.replace(
        "speedl([0.0, 0.0, 0.0, cmd_qd3, cmd_qd4, cmd_qd5], line_accel_m_s2, line_hold_s)",
        "speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5], joint_accel_rad_s2, line_hold_s)",
    )
    script = script.replace(
        "speedl([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, 0.0], line_accel_m_s2, line_hold_s)",
        "speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5], joint_accel_rad_s2, line_hold_s)",
    )
    script = script.replace(
        "codex_abs(cmd_qd0) > 0.010 or codex_abs(cmd_qd1) > 0.010 or codex_abs(cmd_qd2) > 0.010",
        "codex_abs(cmd_qd0) > max_cmd_qd_rad_s or codex_abs(cmd_qd1) > max_cmd_qd_rad_s or codex_abs(cmd_qd2) > max_cmd_qd_rad_s",
    )
    script = script.replace(
        "codex_abs(cmd_qd0) > 0.001 or codex_abs(cmd_qd1) > 0.001 or codex_abs(cmd_qd2) > 0.001",
        "codex_abs(cmd_qd0) > max_cmd_qd_rad_s or codex_abs(cmd_qd1) > max_cmd_qd_rad_s or codex_abs(cmd_qd2) > max_cmd_qd_rad_s",
    )
    script = script.replace(
        "codex_abs(cmd_qd3) > max_cmd_angular_xy_rad_s or codex_abs(cmd_qd4) > max_cmd_angular_xy_rad_s or codex_abs(cmd_qd5) > 0.005",
        "codex_abs(cmd_qd3) > max_cmd_qd_rad_s or codex_abs(cmd_qd4) > max_cmd_qd_rad_s or codex_abs(cmd_qd5) > max_cmd_qd_rad_s",
    )
    script = script.replace("max_cmd_angular_xy_rad_s", "max_cmd_qd_rad_s")
    script = script.replace("stopl(0.1)", "stopj(0.3)")
    script = script.replace("  stopj(0.3)\n  return stop_reason\nend\n\n\ndef codex_wait_for_cmd_valid", "  stopl(0.1)\n  return stop_reason\nend\n\n\ndef codex_wait_for_cmd_valid")
    script = script.replace("  stopj(0.3)\n  return stop_reason\nend\n\ndef codex_step5c_joint_rnn_cycloid_v1", "  stopl(0.1)\n  return stop_reason\nend\n\ndef codex_step5c_joint_rnn_cycloid_v1")
    script = script.replace("movel(entry_xy_pose, a=0.030, v=0.020, r=0.0)\n    stopj(0.3)", "movel(entry_xy_pose, a=0.030, v=0.020, r=0.0)\n    stopl(0.1)")
    script = script.replace("movel(lift_pose, a=0.030, v=0.020, r=0.0)\n    stopj(0.3)", "movel(lift_pose, a=0.030, v=0.020, r=0.0)\n    stopl(0.1)")
    script = script.replace("stopj(0.3)\n    write_output_float_register(35, 27.0)", "stopl(0.1)\n    write_output_float_register(35, 27.0)")
    script = script.replace("movel(home_pose, a=0.030, v=home_return_speed_m_s, r=0.0)\n    stopj(0.3)", "movel(home_pose, a=0.030, v=home_return_speed_m_s, r=0.0)\n    stopl(0.1)")
    header_insert = (
        "# REGISTER_CONTRACT: 37..42=qd0..qd5 rad/s, 43=cmd_valid, 44=path_time, "
        "45=force_error, 46=pose_or_orientation_error, 47=solver_status.\n"
        "# JOINT_SOLVER: tools/step5c_joint_rnn.py MuJoCo nominal UR10e site Jacobian bounded least-squares.\n"
    )
    script = script.replace("# TP_ROLE: joint_executor_and_guard_only;", header_insert + "# TP_ROLE: joint_executor_and_guard_only;", 1)
    return script


def dryrun_txt(stamp: str) -> str:
    return f"""Step5c speedj dry-run TP package

Open on Teach Pendant:
  {CONTROLLER_DIR}/{DRYRUN_PROGRAM}.urp

Version:
  {stamp}

Motion boundary:
  No-contact speedj dry-run.
  Stage25 consumes registers 37..42 as qd0..qd5 rad/s.
  Bridge profile: --step4e-version {DRYRUN_BRIDGE_VERSION} --step4e-path-shape cycloid --step5c-qdot-limit-rad-s 0.10.
  No contact search, no force control, no UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.

Reference:
  STEP5_FLOW.md
  config/step5_stage_table.json stage {DRYRUN_STAGE_ID}
"""


def contact_txt(stamp: str) -> str:
    return f"""Step5c joint-RNN contact cycloid TP package

Open on Teach Pendant:
  {CONTROLLER_DIR}/{CONTACT_PROGRAM}.urp

Version:
  {stamp}

Motion boundary:
  Contact motion after the separate dry-run is accepted.
  Reuses the current Step5b/v31 contact search, first-contact normal latch, 20 mm lift,
  25.2 attitude correction, second contact, and 25.3 line-entry gate.
  Stage25 joint-control windows consume registers 37..42 as qd0..qd5 rad/s.
  Bridge profile: --step4e-version {CONTACT_BRIDGE_VERSION} --step4e-path-shape cycloid --step5c-qdot-limit-rad-s 0.15.
  Force target: --target-force-n 5.0.
  Raw normal guard: 50 N. Force norm guard: 60 N. Torque guard: 3.0 Nm.
  No UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.

Reference:
  STEP5_FLOW.md
  config/step5_stage_table.json stage {CONTACT_STAGE_ID}
"""


def validate_package(program: str, script: str, txt: str, urp: bytes, stamp: str) -> None:
    xml = gzip.decompress(urp).decode("utf-8")
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": f'URProgram name="{program}"' in xml,
        "controller directory": f'directory="{CONTROLLER_DIR}"' in xml,
        "script file": f"{CONTROLLER_DIR}/{program}.script" in xml,
        "cached stamp": stamp in xml,
        "function name": f"def codex_{program}()" in script,
        "step5 flow": "STEP5_FLOW.md" in script and "STEP5_FLOW.md" in txt,
        "joint register contract": "37..42=qd0..qd5 rad/s" in script and "37..42 as qd0..qd5" in txt,
        "speedj active command": "speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]" in script,
        "no cartesian active command": "speedl([cmd_vx" not in script and "speedl([cmd_qd0" not in script,
        "no stale step5b program": "step5b_contact_cycloid_baseline_v1" not in script + txt,
    }
    if program == DRYRUN_PROGRAM:
        checks.update(
            {
                "dry stage id": DRYRUN_STAGE_ID in script and DRYRUN_STAGE_ID in txt,
                "dry bridge contract": f"step4e-version={DRYRUN_BRIDGE_VERSION}" in script
                and f"--step4e-version {DRYRUN_BRIDGE_VERSION}" in txt,
                "dry qdot cap": "local qdot_cap_rad_s = 0.100" in script,
                "dry no contact search": "down_search" not in script and "contact search" in txt.lower(),
            }
        )
    else:
        checks.update(
            {
                "contact stage id": CONTACT_STAGE_ID in script and CONTACT_STAGE_ID in txt,
                "contact bridge contract": f"step4e-version={CONTACT_BRIDGE_VERSION}" in script
                and f"--step4e-version {CONTACT_BRIDGE_VERSION}" in txt,
                "contact qdot cap": "local max_cmd_qd_rad_s = 0.150" in script,
                "contact scaffold retained": "first-contact normal latch" in script
                and "25.2 attitude correction" in script
                and "25.3 line-entry gate" in script,
                "raw contact guards": "codex_abs(normal_force) > 50.0" in script
                and "force_norm > 60.0" in script
                and "torque_norm > 3.0" in script,
            }
        )
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"{program} validation failed: {failed}")


def write_triplet(program: str, script: str, txt: str, stamp: str) -> dict[str, str]:
    urp = build_urp(script, program, CONTROLLER_DIR)
    validate_package(program, script, txt, urp, stamp)
    LOCAL_PROGRAM_DIR.mkdir(parents=True, exist_ok=True)
    script_path = LOCAL_PROGRAM_DIR / f"{program}.script"
    txt_path = LOCAL_PROGRAM_DIR / f"{program}.txt"
    urp_path = LOCAL_PROGRAM_DIR / f"{program}.urp"
    script_path.write_text(script, encoding="utf-8")
    txt_path.write_text(txt, encoding="utf-8")
    urp_path.write_bytes(urp)
    return {
        "script": str(script_path),
        "txt": str(txt_path),
        "urp": str(urp_path),
        "controller_urp": f"{CONTROLLER_DIR}/{program}.urp",
        "stamp": stamp,
    }


def write_outputs(stamp_prefix: str | None = None, *, run_sanity: bool = True) -> dict[str, object]:
    if run_sanity:
        sanity = numeric_sanity()
        if not sanity["overall_pass"]:
            raise RuntimeError(f"Step5c numeric sanity failed: {sanity['artifact_dir']}")
    else:
        sanity = {"overall_pass": None, "artifact_dir": None}
    now = datetime.now(timezone(timedelta(hours=8)))
    gen_at = generated_at(now)
    geom = line_cfg(load_json(CONFIG_PATH))
    dry_frame = load_step5c_frame(DRYRUN_STAGE_ID)
    contact_frame = load_step5c_frame(CONTACT_STAGE_ID)
    dry_stamp = stamp_prefix or source_stamp(now, DRYRUN_PROGRAM)
    contact_stamp = stamp_prefix or source_stamp(now, CONTACT_PROGRAM)
    dry = write_triplet(DRYRUN_PROGRAM, dryrun_script(dry_stamp, gen_at, geom, dry_frame), dryrun_txt(dry_stamp), dry_stamp)
    contact = write_triplet(
        CONTACT_PROGRAM,
        contact_script(contact_stamp, gen_at, geom, contact_frame),
        contact_txt(contact_stamp),
        contact_stamp,
    )
    return {"sanity": sanity, "generated": {DRYRUN_PROGRAM: dry, CONTACT_PROGRAM: contact}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp-prefix", default=None)
    parser.add_argument("--skip-sanity", action="store_true")
    args = parser.parse_args()
    result = write_outputs(args.stamp_prefix, run_sanity=not args.skip_sanity)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
