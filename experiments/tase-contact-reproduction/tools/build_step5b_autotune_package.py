#!/usr/bin/env python3
"""Generate the isolated Step5b TP Local infinite-loop package."""

from __future__ import annotations

import argparse
import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from build_step4e_line_programs import generated_at
from build_step4e_p0p1_programs import build_urp
from build_step5b_contact import build_script as build_step5b_script
from build_step5b_contact import load_safe_frame
from build_step4e_line_programs import CONFIG_PATH, line_cfg, load_json
from step5b_autotune_contract import CONTROLLER_DIRECTORY, PROGRAM_BASENAME


LOCAL_PROGRAM_DIR = Path(__file__).resolve().parents[1] / "programs" / "step5" / "autotune"


def source_stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H%MHKT_STEP5B_CONTACT_CYCLOID_BAYES_LOOP_V2")


def loop_helpers() -> str:
    return r'''
def codex_autotune_set_state(session_epoch, trial_id, state_code, terminal_reason, candidate_token):
  write_output_integer_register(24, session_epoch)
  write_output_integer_register(25, trial_id)
  write_output_integer_register(26, state_code)
  write_output_integer_register(27, terminal_reason)
  write_output_integer_register(28, candidate_token)
end

def codex_autotune_home_verified(home_pose, home_q):
  local current_pose = get_actual_tcp_pose()
  local home_delta = pose_trans(pose_inv(home_pose), current_pose)
  local position_error_m = sqrt(home_delta[0] * home_delta[0] + home_delta[1] * home_delta[1] + home_delta[2] * home_delta[2])
  local orientation_error_rad = sqrt(home_delta[3] * home_delta[3] + home_delta[4] * home_delta[4] + home_delta[5] * home_delta[5])
  local current_q = get_actual_joint_positions()
  local joint_error_rad = 0.0
  local joint_index = 0
  while joint_index < 6:
    local this_error = codex_abs(current_q[joint_index] - home_q[joint_index])
    if this_error > joint_error_rad:
      joint_error_rad = this_error
    end
    joint_index = joint_index + 1
  end
  return position_error_m <= 0.003 and orientation_error_rad <= 0.050 and joint_error_rad <= 0.010
end

def codex_step5b_contact_cycloid_bayes_loop_v2():
  local home_pose = get_actual_tcp_pose()
  local home_q = get_actual_joint_positions()
  local last_trial_id = -1
  local session_epoch = 0
  local trial_id = -1
  local terminal_reason = 0
  local active_token = 0
  codex_autotune_set_state(0, -1, 0, 0, 0)
  sync()
  while True:
    local requested_epoch = read_input_integer_register(24)
    local requested_trial = read_input_integer_register(25)
    local requested_command = read_input_integer_register(26)
    local candidate_token = read_input_integer_register(27)
    if requested_command == 2:
      if codex_autotune_home_verified(home_pose, home_q):
        codex_autotune_set_state(session_epoch, trial_id, 50, terminal_reason, active_token)
      else:
        codex_autotune_set_state(session_epoch, trial_id, 90, 91, active_token)
      end
      return
    elif requested_command == 1 and requested_epoch > 0 and requested_trial > last_trial_id and candidate_token > 0:
      session_epoch = requested_epoch
      trial_id = requested_trial
      active_token = candidate_token
      terminal_reason = 0
      codex_autotune_set_state(session_epoch, trial_id, 20, 0, active_token)
      terminal_reason = codex_step5b_autotune_trial(home_pose)
      if terminal_reason == 3 or terminal_reason == 15:
        codex_autotune_set_state(session_epoch, trial_id, 90, terminal_reason, active_token)
        return
      elif codex_should_auto_home(terminal_reason):
        if codex_autotune_home_verified(home_pose, home_q):
          codex_autotune_set_state(session_epoch, trial_id, 50, terminal_reason, active_token)
          local supervisor_released_home = False
          while not supervisor_released_home:
            local release_epoch = read_input_integer_register(24)
            local release_trial = read_input_integer_register(25)
            local release_command = read_input_integer_register(26)
            local release_token = read_input_integer_register(27)
            if release_epoch == session_epoch and release_trial == trial_id and release_command == 0 and release_token == active_token:
              supervisor_released_home = True
            elif release_command == 2:
              return
            else:
              codex_autotune_set_state(session_epoch, trial_id, 50, terminal_reason, active_token)
              sync()
            end
          end
          last_trial_id = trial_id
          codex_autotune_set_state(session_epoch, trial_id, 10, terminal_reason, active_token)
        else:
          codex_autotune_set_state(session_epoch, trial_id, 90, 91, active_token)
          return
        end
      else:
        codex_autotune_set_state(session_epoch, trial_id, 90, terminal_reason, active_token)
        return
      end
    else:
      codex_autotune_set_state(session_epoch, trial_id, 10, terminal_reason, active_token)
      sync()
    end
  end
end

codex_step5b_contact_cycloid_bayes_loop_v2()
'''


def build_script(stamp: str, gen_at: str) -> str:
    geom = line_cfg(load_json(CONFIG_PATH))
    base = build_step5b_script(stamp, gen_at, geom, load_safe_frame(), variant="v2")
    call = "\ncodex_step5b_contact_cycloid_baseline_v2()\n"
    if not base.endswith(call):
        raise RuntimeError("Step5b v2 executable-spec call marker changed")
    base = base[: -len(call)]
    replacements = {
        "# Step5b contact cycloid baseline v2.": "# Step5b contact cycloid Bayesian infinite loop v2.",
        "def codex_step5b_contact_cycloid_baseline_v2():": "def codex_step5b_autotune_trial(home_pose):",
        "  local home_pose = get_actual_tcp_pose()\n": "",
        "start step5b_contact_cycloid_baseline_v2": "start step5b_contact_cycloid_bayes_loop_v2 trial",
    }
    for old, new in replacements.items():
        if old not in base:
            raise RuntimeError(f"Step5b v2 transformation marker missing: {old!r}")
        base = base.replace(old, new, 1)

    base = base.replace(
        "    write_output_float_register(35, 26.0)\n",
        "    write_output_integer_register(26, 30)\n    write_output_float_register(35, 26.0)\n",
        1,
    )
    base = base.replace(
        "    write_output_float_register(35, 27.0)\n",
        "    write_output_integer_register(26, 40)\n    write_output_float_register(35, 27.0)\n",
        1,
    )
    old_tail = """  write_output_float_register(30, stop_reason)
  write_output_float_register(31, final_progress_m)
  write_output_float_register(35, 29.0)
  textmsg("codex step4e version {stamp} stop reason:", stop_reason)
end
""".format(stamp=stamp)
    if not base.endswith(old_tail):
        raise RuntimeError("Step5b v2 transformed tail marker changed")
    new_tail = old_tail.replace(
        f"codex step4e version {stamp} stop reason:",
        f"codex step4e version {stamp} step5b_contact_cycloid_bayes_loop_v2 trial stop reason:",
    ).replace("end\n", "  return stop_reason\nend\n", 1)
    base = base[: -len(old_tail)] + new_tail
    header_insert = (
        "# AUTOTUNE_FLOW: STEP5B_AUTOTUNE_FLOW.md\n"
        "# AUTOTUNE_CONTRACT: config/step5b_autotune_loop_v2.json\n"
        "# AUTOTUNE_HANDSHAKE: RTDE integer inputs 24..27 and outputs 24..28.\n"
        "# HOME_GATE: TCP position <=0.003 m, orientation <=0.050 rad, joints <=0.010 rad.\n"
        "# SESSION_POLICY: wait at home; one bridge child per armed trial; fatal faults end the program.\n"
    )
    version_line = f"# VERSION: {stamp}\n"
    base = base.replace(version_line, version_line + header_insert, 1)
    return base + loop_helpers()


def build_txt(stamp: str) -> str:
    return f"""Step5b TP Local Bayesian autotune loop v2

Open on Teach Pendant:
  {CONTROLLER_DIRECTORY}/{PROGRAM_BASENAME}.urp

Version:
  {stamp}

Motion boundary:
  Contact motion. Open and press Play only after the separate live gate.
  The program captures its home anchor once, then waits for RTDE integer-register ARM packets.
  Each trial preserves the Step5b v2 full flow and 60 s cycloid.
  A recoverable trial returns home and waits for a new trial_id.
  A sensor/fatal/home-verification fault ends the TP program.
  Home gate: TCP <=3 mm, orientation <=0.05 rad, every joint <=0.01 rad.
  Force guards: raw normal 50 N, force norm 60 N, torque norm 3 Nm.
  No zero_ftsensor(), Kunwei tare/config, payload write, or TCP write.

Companion:
  step5b-autotune.sh
  STEP5B_AUTOTUNE_FLOW.md
  config/step5b_autotune_loop_v2.json
"""


def validate(script: str, txt: str, urp: bytes, stamp: str) -> None:
    xml = gzip.decompress(urp).decode("utf-8")
    required = {
        "stamp": stamp in script and stamp in txt and stamp in xml,
        "program name": f'URProgram name="{PROGRAM_BASENAME}"' in xml,
        "controller directory": f'directory="{CONTROLLER_DIRECTORY}"' in xml,
        "script path": f"{CONTROLLER_DIRECTORY}/{PROGRAM_BASENAME}.script" in xml,
        "v2 flow": "local skip_lift_attitude = 0" in script and "write_output_float_register(35, 25.2)" in script,
        "token-bound handshake": "read_input_integer_register(24)" in script and "write_output_integer_register(28, candidate_token)" in script,
        "home hold release": "release_command == 0 and release_token == active_token" in script,
        "home gate": "position_error_m <= 0.003" in script and "joint_error_rad <= 0.010" in script,
        "infinite wait": "while True:" in script,
        "fatal fault": "codex_autotune_set_state(session_epoch, trial_id, 90" in script,
        "force guards": "codex_abs(normal_force) > 50.0" in script and "force_norm > 60.0" in script and "torque_norm > 3.0" in script,
        "no hardware zero": "zero_ftsensor" not in script and "tare" not in script.lower(),
        "single invocation": script.count("\ncodex_step5b_contact_cycloid_bayes_loop_v2()\n") == 1,
    }
    failed = [name for name, passed in required.items() if not passed]
    if failed:
        raise RuntimeError(f"autotune package validation failed: {failed}")


def write_outputs(stamp: str, gen_at: str) -> dict[str, str]:
    script = build_script(stamp, gen_at)
    txt = build_txt(stamp)
    urp = build_urp(script, PROGRAM_BASENAME, CONTROLLER_DIRECTORY)
    validate(script, txt, urp, stamp)
    LOCAL_PROGRAM_DIR.mkdir(parents=True, exist_ok=True)
    paths = {
        "script": LOCAL_PROGRAM_DIR / f"{PROGRAM_BASENAME}.script",
        "txt": LOCAL_PROGRAM_DIR / f"{PROGRAM_BASENAME}.txt",
        "urp": LOCAL_PROGRAM_DIR / f"{PROGRAM_BASENAME}.urp",
    }
    paths["script"].write_text(script, encoding="utf-8")
    paths["txt"].write_text(txt, encoding="utf-8")
    paths["urp"].write_bytes(urp)
    return {
        **{name: str(path) for name, path in paths.items()},
        "controller_urp": f"{CONTROLLER_DIRECTORY}/{PROGRAM_BASENAME}.urp",
        "stamp": stamp,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp-prefix")
    args = parser.parse_args()
    now = datetime.now(timezone(timedelta(hours=8)))
    stamp = args.stamp_prefix or source_stamp(now)
    result = write_outputs(stamp, generated_at(now))
    print(json.dumps({"generated": {PROGRAM_BASENAME: result}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
