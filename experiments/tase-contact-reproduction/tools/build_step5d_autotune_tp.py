#!/usr/bin/env python3
"""Build the Step5d-native continuous TP package from frozen v35.

This builder writes the complete local ``.script/.txt/.urp`` triplet.  The
package-delivery owner uploads and reads it back automatically in the same
delivery transaction; this builder itself never starts a bridge or program.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from build_step4e_p0p1_programs import build_urp
from step5d_profile_semantic_contract import (
    assert_network_profile_semantics,
    assert_triplet_network_profile_semantics,
)


ROOT = Path(__file__).resolve().parents[1]
BASE_RELATIVE = Path(
    "programs/step5/step5d/step5d_strict_rnn_ablation_v35.script"
)
BASE_SHA256 = "50894de5cdf74dd17309c829b904251a3da26613a4d73e93653f7c165f5fbbd0"
PROGRAM_NAME = "step5d_strict_rnn_autotune_v1"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
LOCAL_PROGRAM_DIR = ROOT / "programs/step5/step5d"

BASE_FUNCTION = "codex_step5d_strict_rnn_ablation_v35"
TRIAL_FUNCTION = "codex_step5d_autotune_trial_v1"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _replace_once(source: str, old: str, new: str, *, label: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"frozen v35 {label} marker count changed")
    return source.replace(old, new, 1)


def load_frozen_v35(path: Path | None = None) -> str:
    source_path = (path or (ROOT / BASE_RELATIVE)).resolve()
    payload = source_path.read_bytes()
    actual = sha256_bytes(payload)
    if actual != BASE_SHA256:
        raise ValueError(
            f"frozen v35 source hash drift: expected {BASE_SHA256}, got {actual}"
        )
    return payload.decode("utf-8")


CONTINUOUS_WRAPPER = r'''

# STEP5D_AUTOTUNE_CONTINUOUS_TP: one campaign home, fresh integer handshake,
# immutable-bundle ACK barrier, and no automatic retry limit for infra reasons.
# HOST_TO_TP_INT: epoch=24 trial=25 command=26 token=27 profile=28 sequence=29
# TP_TO_HOST_INT: epoch=24 trial=25 state=26 token=27 reason=28 profile=29 consumed_sequence=30
# PROFILE_ID: hundreds=normal-rate level; tens=host-slew level; ones=TP accel level
# NETWORK_PROFILE_LEVELS: normal=1..3,5,6; host/TP=1..3 (.1/.2/.5)
# PROFILE_NORMAL_LEVELS: 1=.010, 2=.015, 3=.020, 5=.050, 6=.100 rad/s;
# level 4 (.030 rad/s) is offline_only and must never reach this TP
# TP_ACCEL_LEVEL: 1=.1 rad/s^2, 2=.2 rad/s^2, 3=.5 rad/s^2
# STOP_CONTRACT: integer STOP is polled only at READY_HOME/WAIT_ACK; during RUN
# the frozen trial consumes the legacy float stop_request safety carrier.

def codex_autotune_write_state(campaign_epoch, trial_id, state, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq):
  write_output_integer_register(24, campaign_epoch)
  write_output_integer_register(25, trial_id)
  write_output_integer_register(26, state)
  write_output_integer_register(27, candidate_token)
  write_output_integer_register(28, terminal_reason)
  write_output_integer_register(29, execution_profile_id)
  write_output_integer_register(30, consumed_command_seq)
end

def codex_autotune_speedj_accel(execution_profile_id):
  local accel_level = execution_profile_id - 10 * floor(execution_profile_id / 10.0)
  if accel_level == 1:
    return 0.100
  elif accel_level == 2:
    return 0.200
  elif accel_level == 3:
    return 0.500
  end
  return -1.000
end

def codex_autotune_network_profile_valid(execution_profile_id):
  local normal_level = floor(execution_profile_id / 100.0)
  local remainder = execution_profile_id - 100 * normal_level
  local host_slew_level = floor(remainder / 10.0)
  local tp_accel_level = remainder - 10 * host_slew_level
  return (normal_level >= 1 and normal_level <= 3 or normal_level == 5 or normal_level == 6) and host_slew_level >= 1 and host_slew_level <= 3 and tp_accel_level >= 1 and tp_accel_level <= 3
end

def codex_autotune_post_ack_state(stop_reason):
  # Host journals reason-4 host_cause separately.  TP reason 4 always returns
  # READY_HOME; host policy may remain WAIT_INFRA_READY for infra_stop.
  if stop_reason == 8 or stop_reason == 10 or stop_reason == 12 or stop_reason == 14:
    return 75
  elif stop_reason == 1 or stop_reason == 4:
    return 10
  end
  return 90
end

def codex_autotune_max_joint_error(home_q, actual_q):
  local maximum = 0.0
  local index = 0
  while index < 6:
    local error = codex_abs(home_q[index] - actual_q[index])
    if error > maximum:
      maximum = error
    end
    index = index + 1
  end
  return maximum
end

def codex_autotune_home_verified(campaign_home_pose, campaign_home_q):
  local actual_pose = get_actual_tcp_pose()
  local delta_pose = pose_trans(pose_inv(campaign_home_pose), actual_pose)
  local position_error_m = sqrt(delta_pose[0] * delta_pose[0] + delta_pose[1] * delta_pose[1] + delta_pose[2] * delta_pose[2])
  local orientation_error_rad = sqrt(delta_pose[3] * delta_pose[3] + delta_pose[4] * delta_pose[4] + delta_pose[5] * delta_pose[5])
  local joint_error_rad = codex_autotune_max_joint_error(campaign_home_q, get_actual_joint_positions())
  write_output_float_register(36, position_error_m)
  write_output_float_register(37, orientation_error_rad)
  write_output_float_register(38, joint_error_rad)
  return position_error_m <= 0.003 and orientation_error_rad <= 0.050 and joint_error_rad <= 0.010
end

def codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq):
  while True:
    codex_autotune_write_state(campaign_epoch, trial_id, 90, candidate_token, terminal_reason, execution_profile_id, consumed_command_seq)
    sync()
  end
end

def codex_step5d_strict_rnn_autotune_v1():
  # Campaign home is captured exactly once for the lifetime of this TP loop.
  local campaign_home_pose = get_actual_tcp_pose()
  local campaign_home_q = get_actual_joint_positions()
  local last_consumed_command_seq = 0
  codex_autotune_write_state(0, 0, 10, 0, 0, 0, 0)

  while True:
    local command = read_input_integer_register(26)
    local command_seq = read_input_integer_register(29)
    if command == 3 and command_seq > last_consumed_command_seq:
      last_consumed_command_seq = command_seq
      codex_autotune_fault_forever(0, 0, 0, 4, 0, last_consumed_command_seq)
    elif command == 1 and command_seq > last_consumed_command_seq:
      local campaign_epoch = read_input_integer_register(24)
      local trial_id = read_input_integer_register(25)
      local candidate_token = read_input_integer_register(27)
      local execution_profile_id = read_input_integer_register(28)
      local tp_speedj_accel_rad_s2 = codex_autotune_speedj_accel(execution_profile_id)
      last_consumed_command_seq = command_seq

      if campaign_epoch <= 0 or trial_id <= 0 or candidate_token <= 0 or execution_profile_id <= 0 or not codex_autotune_network_profile_valid(execution_profile_id) or tp_speedj_accel_rad_s2 < 0.0:
        codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, 13, execution_profile_id, last_consumed_command_seq)
      end

      codex_autotune_write_state(campaign_epoch, trial_id, 11, candidate_token, 0, execution_profile_id, last_consumed_command_seq)
      codex_autotune_write_state(campaign_epoch, trial_id, 20, candidate_token, 0, execution_profile_id, last_consumed_command_seq)
      local stop_reason = codex_step5d_autotune_trial_v1(campaign_home_pose, tp_speedj_accel_rad_s2)
      codex_autotune_write_state(campaign_epoch, trial_id, 30, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)

      if stop_reason == 2 or stop_reason == 3 or stop_reason == 17:
        # Transport/sensor/unsafe-entry stops have no automatic return proof.
        codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
      elif codex_should_auto_home(stop_reason):
        codex_autotune_write_state(campaign_epoch, trial_id, 40, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        local retract_start = get_actual_tcp_pose()
        local retract_pose = p[retract_start[0], retract_start[1], retract_start[2] + 0.010, retract_start[3], retract_start[4], retract_start[5]]
        movel(retract_pose, a=0.060, v=0.040, r=0.0)
        stopl(0.1)
        codex_autotune_write_state(campaign_epoch, trial_id, 50, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        movel(campaign_home_pose, a=0.030, v=0.050, r=0.0)
        stopl(0.1)
        codex_autotune_write_state(campaign_epoch, trial_id, 60, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        if not codex_autotune_home_verified(campaign_home_pose, campaign_home_q):
          codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        end

        # Host may ACK only after immutable bundle + host 0.5 s safe closure.
        codex_autotune_write_state(campaign_epoch, trial_id, 70, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        local waiting_for_ack = True
        while waiting_for_ack:
          local ack_command = read_input_integer_register(26)
          local ack_sequence = read_input_integer_register(29)
          if ack_command == 2 and ack_sequence > last_consumed_command_seq and read_input_integer_register(24) == campaign_epoch and read_input_integer_register(25) == trial_id and read_input_integer_register(27) == candidate_token and read_input_integer_register(28) == execution_profile_id:
            last_consumed_command_seq = ack_sequence
            waiting_for_ack = False
          elif ack_command == 3 and ack_sequence > last_consumed_command_seq:
            last_consumed_command_seq = ack_sequence
            codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, 4, execution_profile_id, last_consumed_command_seq)
          else:
            sync()
          end
        end

        local post_ack_state = codex_autotune_post_ack_state(stop_reason)
        if post_ack_state == 75:
          codex_autotune_write_state(campaign_epoch, trial_id, 75, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        elif post_ack_state == 10:
          codex_autotune_write_state(0, 0, 10, 0, 0, 0, last_consumed_command_seq)
        else:
          codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
        end
      else:
        codex_autotune_fault_forever(campaign_epoch, trial_id, candidate_token, stop_reason, execution_profile_id, last_consumed_command_seq)
      end
    else:
      sync()
    end
  end
end

codex_step5d_strict_rnn_autotune_v1()
'''


def render_script(base_source: str | None = None) -> str:
    source = load_frozen_v35() if base_source is None else base_source
    if sha256_bytes(source.encode("utf-8")) != BASE_SHA256:
        raise ValueError("rendering requires the exact frozen v35 source bytes")
    source = _replace_once(
        source,
        "# STEP5_STAGE_ID: step5d_strict_rnn_ablation_v35",
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v1\n"
        f"# PARENT_V35_SHA256: {BASE_SHA256}",
        label="stage identity",
    )
    control_lines = [line for line in source.splitlines() if line.startswith("# CONTROL:")]
    if len(control_lines) != 1:
        raise ValueError("frozen v35 CONTROL marker count changed")
    source = source.replace(
        control_lines[0],
        "# AUTOTUNE_CONTROL: fixed tau=.35s, fixed dt=.002s, profile rate .010/.015/.020; "
        "normal_filter_alpha is not an active candidate dimension.",
        1,
    )
    source = _replace_once(
        source,
        f"def {BASE_FUNCTION}():",
        f"def {TRIAL_FUNCTION}(campaign_home_pose, tp_speedj_accel_rad_s2):",
        label="trial function",
    )
    source = _replace_once(
        source,
        "  local home_pose = get_actual_tcp_pose()",
        "  local home_pose = campaign_home_pose",
        label="home capture",
    )
    source = _replace_once(
        source,
        "    local joint_accel_rad_s2 = 0.100",
        "    local joint_accel_rad_s2 = tp_speedj_accel_rad_s2",
        label="speedj acceleration",
    )
    source = _replace_once(
        source,
        "    movel(entry_xy_pose, a=0.090, v=0.060, r=0.0)",
        "    movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)",
        label="1.5x pre-contact entry movel",
    )
    source = _replace_once(
        source,
        "40.000, -0.0225, -0.0025)",
        "40.000, -0.03375, -0.0025)",
        label="1.5x far-search speed",
    )
    auto_home_start = "  if codex_should_auto_home(stop_reason):\n"
    final_evidence = "  write_output_float_register(30, stop_reason)\n"
    if source.count(auto_home_start) != 1:
        raise ValueError("frozen v35 terminal lifecycle markers changed")
    before, rest = source.split(auto_home_start, 1)
    if rest.count(final_evidence) != 1:
        raise ValueError("frozen v35 final evidence marker changed")
    _, after = rest.split(final_evidence, 1)
    source = before + final_evidence + after
    source = _replace_once(
        source,
        f"end\n\n{BASE_FUNCTION}()\n",
        "  return stop_reason\nend\n",
        label="one-shot terminal call",
    )
    rendered = source.rstrip() + CONTINUOUS_WRAPPER
    validate_rendered_script(rendered)
    return rendered


def validate_rendered_script(script: str) -> None:
    required = (
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v1",
        f"# PARENT_V35_SHA256: {BASE_SHA256}",
        f"def {TRIAL_FUNCTION}(campaign_home_pose, tp_speedj_accel_rad_s2):",
        "local qdot_cap_rad_s = 0.500",
        "local joint_accel_rad_s2 = tp_speedj_accel_rad_s2",
        "movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)",
        "40.000, -0.03375, -0.0025)",
        "elif saw_sensor_not_ready and read_input_float_register(27) > 0.5 and current_heartbeat != initial_heartbeat:",
        "local campaign_home_pose = get_actual_tcp_pose()",
        "local campaign_home_q = get_actual_joint_positions()",
        "read_input_integer_register(24)",
        "read_input_integer_register(29)",
        "write_output_integer_register(30, consumed_command_seq)",
        "codex_autotune_write_state(campaign_epoch, trial_id, 70",
        "position_error_m <= 0.003",
        "orientation_error_rad <= 0.050",
        "joint_error_rad <= 0.010",
        "return 0.100",
        "return 0.200",
        "return 0.500",
        "def codex_autotune_network_profile_valid(execution_profile_id):",
        "normal_level == 5",
        "normal_level == 6",
        "# the frozen trial consumes the legacy float stop_request safety carrier.",
    )
    missing = [marker for marker in required if marker not in script]
    if missing:
        raise ValueError(f"rendered autotune TP is missing markers: {missing}")
    if f"\n{BASE_FUNCTION}()\n" in script:
        raise ValueError("rendered autotune TP still contains the v35 one-shot entry call")
    if script.count("local campaign_home_pose = get_actual_tcp_pose()") != 1:
        raise ValueError("campaign home must be captured exactly once")
    lifecycle = (
        "trial_id, 11,",
        "trial_id, 20,",
        "trial_id, 30,",
        "trial_id, 40,",
        "trial_id, 50,",
        "trial_id, 60,",
        "trial_id, 70,",
    )
    indices = [script.index(marker) for marker in lifecycle]
    if indices != sorted(indices):
        raise ValueError("rendered autotune TP lifecycle is out of order")
    assert_network_profile_semantics(script)


def source_stamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone(timedelta(hours=8)))
    return value.strftime("%Y-%m-%dT%H%MHKT_STEP5D_STRICT_RNN_AUTOTUNE_V1")


def build_package_script(stamp: str) -> str:
    if not stamp or "\n" in stamp:
        raise ValueError("source stamp must be one non-empty line")
    return f"# VERSION: {stamp}\n" + render_script()


def build_txt(stamp: str) -> str:
    return f"""Step5d-native continuous autotune TP package

Open on Teach Pendant only after controller read-back is verified:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Motion class:
  Contact motion package. Upload/read-back does not load or run it.
  Program load/Play, bridge start, sensor zero/tare, contact, and motion remain live-gated.

Contract:
  Continuous campaign home is captured once.
  Host/TP integer handshake uses input 24..29 and output 24..30.
  qdot cap is fixed at 0.500 rad/s.
  Pre-contact entry movel is a=0.135 m/s^2, v=0.090 m/s.
  Far search is -0.03375 m/s; near search remains -0.0025 m/s.
  speedj acceleration profiles are 0.100, 0.200, and 0.500 rad/s^2.
  normal-rate profile 0.030 rad/s is offline-only and rejected by this TP.
"""


def validate_triplet(script: str, txt: str, urp: bytes, stamp: str) -> None:
    xml = gzip.decompress(urp).decode("utf-8")
    root = ET.fromstring(xml)
    cached = ""
    script_file = ""
    for node in root.iter():
        if node.tag == "cachedContents":
            cached = html.unescape(node.text or "")
        elif node.tag == "file" and node.attrib.get("resolves-to") == "file":
            script_file = node.text or ""
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": root.attrib.get("name") == PROGRAM_NAME,
        "controller directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "script file": script_file == f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script",
        "cached script": cached == script,
        "continuous entrypoint": script.rstrip().endswith(
            "codex_step5d_strict_rnn_autotune_v1()"
        ),
    }
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        raise ValueError(f"autotune TP package validation failed: {failed}")
    assert_triplet_network_profile_semantics(script, urp)


def write_triplet(output_dir: Path, stamp: str) -> dict[str, object]:
    script = build_package_script(stamp)
    txt = build_txt(stamp)
    urp = build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    validate_triplet(script, txt, urp, stamp)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        ".script": output_dir / f"{PROGRAM_NAME}.script",
        ".txt": output_dir / f"{PROGRAM_NAME}.txt",
        ".urp": output_dir / f"{PROGRAM_NAME}.urp",
    }
    paths[".script"].write_text(script, encoding="utf-8")
    paths[".txt"].write_text(txt, encoding="utf-8")
    paths[".urp"].write_bytes(urp)
    return {
        "program": PROGRAM_NAME,
        "controller_dir": CONTROLLER_DIR,
        "stamp": stamp,
        "paths": {ext: str(path) for ext, path in paths.items()},
        "sha256": {
            ext: sha256_bytes(path.read_bytes()) for ext, path in paths.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=LOCAL_PROGRAM_DIR)
    parser.add_argument("--stamp", default=None)
    args = parser.parse_args(argv)
    result = write_triplet(args.output_dir, args.stamp or source_stamp())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
