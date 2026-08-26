#!/usr/bin/env python3
"""Build the dedicated one-shot autotuner XY no-contact TP package."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from build_step4e_p0p1_programs import build_urp


PROGRAM = "step5d_autotuner_xy_no_contact_diagnostic_v1"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
STAMP = "20260815_STEP5D_AUTOTUNER_XY_NO_CONTACT_DIAGNOSTIC_V1"
LAYOUT_TAG = 725.0
ORIGIN_X = 0.487795411149049
ORIGIN_Y = 0.12932679270060748


def script_source() -> str:
    return f'''# VERSION: {STAMP}
# PURPOSE: one-shot 60 s autotuner absolute-XY, startup fixed-Z/orientation, no contact/search/force control.
# WIRE: input float 24 force norm, 25 torque norm, 26 heartbeat, 27 sensor fresh, 28 abort, 29 phase, 37..42 qdot, 43 valid, 44 path time, 47 layout={LAYOUT_TAG:.0f}.
# LIFECYCLE: guarded preposition -> one PATH -> guarded path-origin -> startup Home -> stationary. Faults zero/abort without recovery motion.

def codex_abs(x):
  if x < 0:
    return -x
  end
  return x
end

def codex_guard_reason(last_heartbeat, stale_s):
  local heartbeat = read_input_float_register(26)
  local sensor_ok = read_input_float_register(27)
  local abort_reason = read_input_float_register(28)
  local force_norm = read_input_float_register(24)
  local torque_norm = read_input_float_register(25)
  if sensor_ok < 0.5:
    return [2.0, heartbeat, stale_s]
  elif abort_reason > 0.5:
    return [abort_reason, heartbeat, stale_s]
  elif force_norm > 6.0:
    return [6.0, heartbeat, stale_s]
  elif torque_norm > 0.5:
    return [7.0, heartbeat, stale_s]
  end
  if heartbeat == last_heartbeat:
    stale_s = stale_s + get_steptime()
  else:
    stale_s = 0.0
  end
  if stale_s > 1.0:
    return [2.0, heartbeat, stale_s]
  end
  return [0.0, heartbeat, stale_s]
end

def codex_guarded_xy_move(target_pose, stage_code, last_heartbeat, stale_s):
  write_output_float_register(35, stage_code)
  local reason = 0.0
  local elapsed = 0.0
  while reason == 0.0 and elapsed < 75.0:
    local guard = codex_guard_reason(last_heartbeat, stale_s)
    reason = guard[0]
    last_heartbeat = guard[1]
    stale_s = guard[2]
    local current = get_actual_tcp_pose()
    local ex = target_pose[0] - current[0]
    local ey = target_pose[1] - current[1]
    local distance = sqrt(ex*ex + ey*ey)
    write_output_float_register(36, distance)
    if reason == 0.0:
      if distance <= 0.00020:
        stopl(0.1)
        return [0.0, last_heartbeat, stale_s]
      end
      local speed = 2.0*distance
      if speed > 0.003:
        speed = 0.003
      end
      local vx = speed*ex/distance
      local vy = speed*ey/distance
      speedl([vx, vy, 0.0, 0.0, 0.0, 0.0], 0.05, 0.002)
    end
    elapsed = elapsed + get_steptime()
  end
  stopl(0.1)
  if reason == 0.0:
    reason = 15.0
  end
  return [reason, last_heartbeat, stale_s]
end

def codex_{PROGRAM}():
  write_output_float_register(28, 0.0)
  write_output_float_register(35, 20.0)
  write_output_float_register(47, 0.0)
  local reason = 0.0
  local last_heartbeat = read_input_float_register(26)
  local stale_s = 0.0
  local wait_s = 0.0
  local fresh_count = 0.0
  while reason == 0.0 and fresh_count < 2.0 and wait_s < 30.0:
    local guard0 = codex_guard_reason(last_heartbeat, stale_s)
    reason = guard0[0]
    if guard0[1] != last_heartbeat and read_input_float_register(27) >= 0.5:
      fresh_count = fresh_count + 1.0
    end
    last_heartbeat = guard0[1]
    stale_s = guard0[2]
    wait_s = wait_s + get_steptime()
    sync()
  end
  if reason == 0.0 and fresh_count < 2.0:
    reason = 2.0
  end

  local session_home = get_actual_tcp_pose()
  local path_origin = p[{ORIGIN_X:.15f}, {ORIGIN_Y:.15f}, session_home[2], session_home[3], session_home[4], session_home[5]]
  if reason == 0.0:
    local moved0 = codex_guarded_xy_move(path_origin, 30.0, last_heartbeat, stale_s)
    reason = moved0[0]
    last_heartbeat = moved0[1]
    stale_s = moved0[2]
  end

  if reason == 0.0:
    write_output_float_register(35, 25.0)
    local path_s = 0.0
    local accepted = False
    while reason == 0.0 and read_input_float_register(29) < 1.5 and path_s < 65.0:
      local guard1 = codex_guard_reason(last_heartbeat, stale_s)
      reason = guard1[0]
      last_heartbeat = guard1[1]
      stale_s = guard1[2]
      local valid = read_input_float_register(43)
      local layout = read_input_float_register(47)
      local q0 = read_input_float_register(37)
      local q1 = read_input_float_register(38)
      local q2 = read_input_float_register(39)
      local q3 = read_input_float_register(40)
      local q4 = read_input_float_register(41)
      local q5 = read_input_float_register(42)
      write_output_float_register(36, q0)
      write_output_float_register(37, q1)
      write_output_float_register(38, q2)
      write_output_float_register(39, q3)
      write_output_float_register(40, q4)
      write_output_float_register(41, q5)
      write_output_float_register(42, valid)
      write_output_float_register(43, read_input_float_register(44))
      write_output_float_register(46, layout)
      if reason == 0.0:
        if valid >= 0.5 and codex_abs(layout-{LAYOUT_TAG:.3f}) < 0.001 and codex_abs(q0) <= 0.15 and codex_abs(q1) <= 0.15 and codex_abs(q2) <= 0.15 and codex_abs(q3) <= 0.15 and codex_abs(q4) <= 0.15 and codex_abs(q5) <= 0.15:
          accepted = True
          write_output_float_register(47, 1.0)
          speedj([q0,q1,q2,q3,q4,q5], 0.05, 0.002)
        elif path_s < 1.0 and not accepted:
          sync()
        else:
          reason = 12.0
        end
      end
      path_s = path_s + get_steptime()
    end
    stopj(0.3)
    stopl(0.1)
    if reason == 0.0 and read_input_float_register(29) < 1.5:
      reason = 16.0
    end
  end

  if reason == 0.0:
    local moved1 = codex_guarded_xy_move(path_origin, 40.0, last_heartbeat, stale_s)
    reason = moved1[0]
    last_heartbeat = moved1[1]
    stale_s = moved1[2]
  end
  if reason == 0.0:
    local moved2 = codex_guarded_xy_move(session_home, 41.0, last_heartbeat, stale_s)
    reason = moved2[0]
  end
  stopj(0.3)
  stopl(0.1)
  write_output_float_register(28, reason)
  if reason == 0.0:
    sleep(0.5)
    write_output_float_register(35, 78.0)
  else:
    write_output_float_register(35, 90.0)
  end
end

codex_{PROGRAM}()
'''


def build(output_dir: Path) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    script = script_source()
    txt = f"""Autotuner XY no-contact diagnostic package

Program: {PROGRAM}
Version: {STAMP}
Claim: single_autotuner_xy_no_contact_diagnostic
Contact/search/force-control/training/qualification/promotion/tell_exact: inactive
Normal completion: guarded absolute-origin preposition, one PATH, path origin, startup Home, stationary
Fault completion: zero/abort, no automatic recovery motion
"""
    urp = build_urp(script, PROGRAM, CONTROLLER_DIR)
    payloads = {"script": script.encode(), "txt": txt.encode(), "urp": urp}
    for suffix, payload in payloads.items():
        (output_dir / f"{PROGRAM}.{suffix}").write_bytes(payload)
    manifest = {
        "schema": "step5d.autotuner-xy-no-contact-package/v1",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "program": PROGRAM,
        "controller_dir": CONTROLLER_DIR,
        "stamp": STAMP,
        "layout_tag": LAYOUT_TAG,
        "sha256": {key: hashlib.sha256(value).hexdigest() for key, value in payloads.items()},
    }
    (output_dir / "package_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.output_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
