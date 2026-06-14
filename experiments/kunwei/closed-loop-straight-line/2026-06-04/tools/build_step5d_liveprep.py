#!/usr/bin/env python3
"""Generate Step5d strict RNN live-prep TP package."""

from __future__ import annotations

import argparse
import gzip
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from build_step4e_line_programs import CONFIG_PATH, PROGRAM_DIR, generated_at, line_cfg, load_json
from build_step4e_p0p1_programs import build_urp
from build_step5b_contact import build_script as build_step5b_script
from step5_table import load_stage_frame, step5_stage


PROGRAM_NAME = "step5d_strict_rnn_liveprep_v1"
STEP5_STAGE_ID = "step5d_strict_rnn_liveprep_v1"
BRIDGE_VERSION = STEP5_STAGE_ID
LOCAL_PROGRAM_DIR = PROGRAM_DIR / "step5"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
QDOT_CAP_RAD_S = 0.300
JOINT_ACCEL_RAD_S2 = 0.300


def source_stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H%MHKT_STEP5D_STRICT_RNN_LIVEPREP_V1")


def load_safe_frame() -> dict:
    return load_stage_frame(step5_stage(STEP5_STAGE_ID))


def _replace_exact(script: str, old: str, new: str) -> str:
    if old not in script:
        raise RuntimeError(f"{PROGRAM_NAME} scaffold replacement failed: {old}")
    return script.replace(old, new, 1)


def _replace_line_stage_with_speedj(script: str) -> str:
    start = script.index("  if stop_reason == 0.0:\n    write_output_float_register(35, 25.0)")
    end = script.index("\n\n  if codex_should_auto_home(stop_reason):", start)
    block = f"""  if stop_reason == 0.0:
    write_output_float_register(35, 25.0)
    local last_heartbeat2 = read_input_float_register(26)
    local stale_s2 = 0.0
    local t2 = 0.0
    local qdot_cap_rad_s = {QDOT_CAP_RAD_S:.3f}
    local joint_accel_rad_s2 = {JOINT_ACCEL_RAD_S2:.3f}
    while stop_reason == 0.0:
      local heartbeat2 = read_input_float_register(26)
      local cmd_valid = read_input_float_register(43)
      local progress_s = read_input_float_register(44)
      local cmd_qd0 = read_input_float_register(37)
      local cmd_qd1 = read_input_float_register(38)
      local cmd_qd2 = read_input_float_register(39)
      local cmd_qd3 = read_input_float_register(40)
      local cmd_qd4 = read_input_float_register(41)
      local cmd_qd5 = read_input_float_register(42)
      local loop_dt = get_steptime()
      final_progress_m = progress_s
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
      if progress_s >= line_success_progress_m:
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
        elif t2 >= line_runtime_limit_s:
          stop_reason = 10.0
        else:
          speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5], joint_accel_rad_s2, line_hold_s)
        end
      end
    end
    stopj(0.3)
  end"""
    return script[:start] + block + script[end:]


def build_script(stamp: str, gen_at: str, geom: dict[str, float], frame: dict) -> str:
    script = build_step5b_script(stamp, gen_at, geom, frame)
    script = script.replace("step5b_contact_cycloid_baseline_v1", PROGRAM_NAME)
    script = script.replace("Step5b contact cycloid baseline v1", "Step5d strict RNN liveprep v1")
    script = script.replace("STEP5B_CONTACT_CYCLOID_BASELINE_V1", "STEP5D_STRICT_RNN_LIVEPREP_V1")
    script = script.replace("codex_step5b_down_search", "codex_step5d_down_search")
    script = script.replace("step4e-version=step5b_v1", f"step4e-version={BRIDGE_VERSION}")
    script = script.replace(
        "25.0 uses desired_velocity + path_p_gain*(desired-actual) before normal projection and force-loop composition.",
        "25.0 uses strict RNN qdot registers 37..42 and TP speedj execution; bridge owns calibrated Pinocchio/J(q) and paper outer-loop computation.",
    )
    script = script.replace(
        "TP_ROLE: executor_and_guard_only; Step5 trajectory reference is computed by the bridge.",
        (
            "TP_ROLE: joint_executor_and_guard_only; Step5d strict RNN qdot is computed by the bridge.\n"
            "# REGISTER_CONTRACT: Stage 25.0 consumes 37..42 as qd0..qd5 rad/s, 43 cmd_valid, 44 path_time_s."
        ),
    )
    script = _replace_exact(script, "STEP5_STAGE_ID: step5_contact_cycloid_baseline_v1", f"STEP5_STAGE_ID: {STEP5_STAGE_ID}")
    script = _replace_line_stage_with_speedj(script)
    if "speedl([cmd_vx, cmd_vy, cmd_vz" in script:
        raise RuntimeError("line-control speedl command survived Step5d liveprep rewrite")
    if "def codex_step5d_strict_rnn_liveprep_v1()" not in script:
        raise RuntimeError("Step5d liveprep function rename failed")
    return script


def build_txt(stamp: str) -> str:
    return f"""Step5d strict RNN live-prep TP package

Open on Teach Pendant after controller read-back is verified:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Boundary:
  Contact-capable live-prep package; not a completed reproduction claim.
  Reuses the Step5b contact-search/latch/lift/25.2/25.3 scaffold.
  Stage 25.0 is different from Step5b: it consumes 37..42 as qd0..qd5 rad/s
  and executes speedj, not Cartesian speedl.

Bridge profile:
  --step4e-version {BRIDGE_VERSION} --step4e-path-shape cycloid
  --target-force-n 5.0
  --step4e-normal-follow-mode filtered_live
  --step4e-normal-filter-alpha 0.35
  --step4e-normal-min-force-n 2.0

Safety:
  qdot cap: {QDOT_CAP_RAD_S:.3f} rad/s
  speedj acceleration: {JOINT_ACCEL_RAD_S2:.3f} rad/s^2
  Raw normal guard: 50 N. Force norm guard: 60 N. Torque guard: 3.0 Nm.
  No UR zero_ftsensor(), no Kunwei tare/zero/config, no TCP/payload write.
  This package is not a bridge-start or TP-Play authorization.

Reference:
  STEP5_FLOW.md
  config/step5_stage_table.json stage {STEP5_STAGE_ID}
  config/step5d_liveprep_solver_gate.json
"""


def validate_package(script: str, txt: str, urp: bytes, stamp: str) -> None:
    xml = gzip.decompress(urp).decode("utf-8")
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": f'URProgram name="{PROGRAM_NAME}"' in xml,
        "controller directory": f'directory="{CONTROLLER_DIR}"' in xml,
        "script file": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script" in xml,
        "cached stamp": stamp in xml,
        "function name": f"def codex_{PROGRAM_NAME}()" in script,
        "step5 flow": "STEP5_FLOW.md" in script and "STEP5_FLOW.md" in txt,
        "step5d stage": STEP5_STAGE_ID in script and STEP5_STAGE_ID in txt,
        "bridge profile": f"step4e-version={BRIDGE_VERSION}" in script
        and f"--step4e-version {BRIDGE_VERSION}" in txt,
        "joint executor role": "joint_executor_and_guard_only" in script,
        "qdot register reads": all(f"local cmd_qd{idx} = read_input_float_register({37 + idx})" in script for idx in range(6)),
        "speedj line control": "speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]" in script,
        "line no cartesian speedl": "speedl([cmd_vx, cmd_vy, cmd_vz" not in script,
        "qdot cap": f"local qdot_cap_rad_s = {QDOT_CAP_RAD_S:.3f}" in script,
        "v31 scaffold retained": "first-contact normal latch" in script
        and "25.2 attitude correction" in script
        and "25.3 line-entry gate" in script,
        "raw contact guards": "codex_abs(normal_force) > 50.0" in script
        and "force_norm > 60.0" in script
        and "torque_norm > 3.0" in script,
        "not quarantine": "stop_only_quarantine" not in script + txt,
        "no stale package": "step5b_contact_cycloid_baseline_v1" not in script + txt,
    }
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise RuntimeError(f"{PROGRAM_NAME} validation failed: {failed}")


def write_outputs(stamp_prefix: str | None = None) -> dict[str, str]:
    now = datetime.now(timezone(timedelta(hours=8)))
    stamp = stamp_prefix or source_stamp(now)
    frame = load_safe_frame()
    geom = line_cfg(load_json(CONFIG_PATH))
    script = build_script(stamp, generated_at(now), geom, frame)
    txt = build_txt(stamp)
    urp = build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    validate_package(script, txt, urp, stamp)

    LOCAL_PROGRAM_DIR.mkdir(parents=True, exist_ok=True)
    script_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.script"
    txt_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.txt"
    urp_path = LOCAL_PROGRAM_DIR / f"{PROGRAM_NAME}.urp"
    script_path.write_text(script, encoding="utf-8")
    txt_path.write_text(txt, encoding="utf-8")
    urp_path.write_bytes(urp)
    return {
        "script": str(script_path),
        "txt": str(txt_path),
        "urp": str(urp_path),
        "controller_urp": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "stamp": stamp,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp-prefix", default=None)
    args = parser.parse_args()
    result = write_outputs(args.stamp_prefix)
    print(json.dumps({"generated": {PROGRAM_NAME: result}}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
