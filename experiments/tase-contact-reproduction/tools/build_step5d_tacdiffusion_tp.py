#!/usr/bin/env python3
"""Build the single-Play Step5d Direct Torque fixture-shadow TP triplet.

The frozen V3 program supplies approach, contact search, preload, trajectory
entry, and guarded return.  This deterministic rewrite replaces only Stage25
and the outer campaign wrapper.  It never uploads, loads, or runs a program.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from build_step4e_p0p1_programs import build_urp


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "programs/step5/step5d/step5d_strict_rnn_autotune_v3.script"
SOURCE_SHA256 = "4601cce71b3270cf4bceb84a16378060acf23019ca58202cec44729db991d339"
DIRECT_TEMPLATE = ROOT.parent / "ur10e-variable-impedance/programs/direct_torque_vic_offline_template.script"
DIRECT_TEMPLATE_SHA256 = "297691225720dfb887ec1311c7fb8046c18e9f2aa191876e61422667de242d80"
PROGRAM_NAME = "step5d_tacdiffusion_direct_torque_fixture_shadow_v1"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
OUTPUT_DIR = ROOT / "programs/step5/step5d_tacdiffusion"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_bound(path: Path, expected: str, role: str) -> str:
    payload = path.read_bytes()
    actual = sha256_bytes(payload)
    if actual != expected:
        raise ValueError(f"{role} hash drift: expected {expected}, got {actual}")
    return payload.decode("utf-8")


def replace_once(source: str, old: str, new: str, role: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"{role} marker count changed: {source.count(old)}")
    return source.replace(old, new, 1)


def direct_stage_source() -> str:
    template = read_bound(DIRECT_TEMPLATE, DIRECT_TEMPLATE_SHA256, "Direct Torque template")
    helper_marker = "def ur10e_vic_direct_torque_offline_template():"
    if template.count(helper_marker) != 1:
        raise ValueError("Direct Torque entry marker changed")
    helpers, main = template.split(helper_marker, 1)
    helpers = helpers.replace(
        "# OFFLINE TEMPLATE ONLY. DO NOT UPLOAD OR EXECUTE WITHOUT A FRESH LIVE GATE.\n",
        "# HASH-BOUND DIRECT TORQUE HELPERS: live use still requires fresh owner gates.\n",
        1,
    )
    if helpers.count("local xyz_max = [0.55, 0.24, 0.12]") != 1:
        raise ValueError("source-exact Step5d y cage marker changed")
    fixed_gate = r'''
def vic_fixed_impedance_exact(stiffness, damping):
  local fixed_k = [600.0, 600.0, 600.0, 30.0, 30.0, 30.0]
  local fixed_d = [69.2820323, 69.2820323, 69.2820323, 4.898979486, 4.898979486, 4.898979486]
  local axis = 0
  while axis < 6:
    if vic_abs(stiffness[axis] - fixed_k[axis]) > 0.000001 or vic_abs(damping[axis] - fixed_d[axis]) > 0.000001:
      return False
    end
    axis = axis + 1
  end
  return True
end

def vic_contact_release_ready(eq):
  local actual_pose = get_actual_tcp_pose()
  local target_pose = p[eq[0], eq[1], eq[2], eq[3], eq[4], eq[5]]
  local error = pose_sub(target_pose, actual_pose)
  local qd = get_actual_joint_speeds()
  local qd_max = 0.0
  local joint = 0
  while joint < 6:
    if vic_abs(qd[joint]) > qd_max:
      qd_max = vic_abs(qd[joint])
    end
    joint = joint + 1
  end
  return vic_norm3(error, 0) <= 0.002 and vic_norm3(error, 3) <= 0.010 and qd_max <= 0.010
end

'''
    helpers = helpers.rstrip() + "\n\n" + fixed_gate

    main = "def codex_step5d_direct_torque_stage25():" + main
    main = replace_once(
        main,
        "  local MODE_ARMED = 1\n",
        "  local MODE_ARMED = 1\n  local MODE_COMPLETE = 2\n",
        "normal completion mode",
    )
    main = replace_once(
        main,
        "      elif not vic_feedforward_valid(raw_feedforward):",
        "      elif not vic_feedforward_zero(raw_feedforward):",
        "fixture shadow zero feedforward",
    )
    main = replace_once(
        main,
        "    local input_ok = vic_input_valid(eq, stiffness, damping)",
        "    local input_ok = vic_input_valid(eq, stiffness, damping) and vic_fixed_impedance_exact(stiffness, damping)",
        "fixed impedance gate",
    )
    main = replace_once(
        main,
        "      release_ok = vic_release_ready(eq)",
        "      release_ok = vic_contact_release_ready(eq)",
        "contact release gate",
    )
    completion_anchor = "    local model_fault = MODEL_FAULT_NONE\n"
    completion = r'''    if mode == MODE_COMPLETE and torque_active and coherent_packet and sequence_ok and lease_ok:
      local normal_exit_count = 0
      while normal_exit_count < safe_exit_ticks:
        if not vic_safe_exit_tick(vic_zero_six()):
          stopj(10.0)
          return 13.0
        end
        normal_exit_count = normal_exit_count + 1
      end
      stopj(10.0)
      return 1.0
    end

'''
    main = replace_once(main, completion_anchor, completion + completion_anchor, "normal completion path")
    fault_anchor = "    if not armed or not input_ok:\n"
    terminal_reason = r'''    if not armed or not input_ok:
      local terminal_reason = 13.0
      if not heartbeat_ok:
        terminal_reason = 2.0
      elif not coherent_packet:
        terminal_reason = 4.0
      elif not sequence_ok:
        terminal_reason = 13.0
      elif not lease_ok:
        terminal_reason = 13.0
      elif not model_packet_ok:
        terminal_reason = 13.0
      elif not release_ok:
        terminal_reason = 17.0
      elif not runtime_ok:
        terminal_reason = 17.0
      elif not stiffness_ok or not eq_ok:
        terminal_reason = 13.0
      end
'''
    main = replace_once(main, fault_anchor, terminal_reason, "terminal fault classification")
    main = replace_once(
        main,
        "        if not vic_safe_exit_tick(last_applied_feedforward):\n          stopj(10.0)\n          return False\n        end",
        "        if not vic_safe_exit_tick(last_applied_feedforward):\n          stopj(10.0)\n          return terminal_reason\n        end",
        "fault safe-exit failure",
    )
    main = replace_once(
        main,
        "        if safe_exit_count >= safe_exit_ticks:\n          stopj(10.0)\n          return False\n        end",
        "        if safe_exit_count >= safe_exit_ticks:\n          stopj(10.0)\n          return terminal_reason\n        end",
        "fault safe-exit completion",
    )
    main = replace_once(
        main,
        "        if not vic_safe_exit_tick(vic_zero_six()):\n          stopj(10.0)\n          return False\n        end",
        "        if not vic_safe_exit_tick(vic_zero_six()):\n          stopj(10.0)\n          return 13.0\n        end",
        "zero-startup safe-exit failure",
    )
    main = replace_once(
        main,
        "        sync()\n      end\n    end\n  end\nend\n\n# Intentionally no invocation. A reviewed TP/URSim wrapper must call this function.\n",
        "        if mode == MODE_ARMED:\n          return terminal_reason\n        end\n        sync()\n      end\n    end\n  end\nend\n",
        "pre-torque invalid packet latch",
    )
    return helpers + main


SINGLE_PLAY_WRAPPER = r'''
def codex_tacdiffusion_stationary_for_return():
  local stable_s = 0.0
  while stable_s < 0.500:
    local qd = get_actual_joint_speeds()
    local tcp_speed = get_actual_tcp_speed()
    local qd_max = 0.0
    local joint = 0
    while joint < 6:
      if codex_abs(qd[joint]) > qd_max:
        qd_max = codex_abs(qd[joint])
      end
      joint = joint + 1
    end
    local linear_speed = sqrt(tcp_speed[0] * tcp_speed[0] + tcp_speed[1] * tcp_speed[1] + tcp_speed[2] * tcp_speed[2])
    local angular_speed = sqrt(tcp_speed[3] * tcp_speed[3] + tcp_speed[4] * tcp_speed[4] + tcp_speed[5] * tcp_speed[5])
    if qd_max <= 0.010 and linear_speed <= 0.001 and angular_speed <= 0.010:
      stable_s = stable_s + get_steptime()
    else:
      stable_s = 0.0
    end
    sync()
  end
  return True
end

def codex_step5d_tacdiffusion_fixture_shadow_v1():
  local campaign_home_pose = get_actual_tcp_pose()
  local campaign_home_q = get_actual_joint_positions()
  # The frozen V3 trial keeps the exact approach/search/preload path. Stage25
  # is Direct Torque and waits for coherent bridge packets after Play.
  local stop_reason = codex_step5d_autotune_trial_v1(campaign_home_pose, 0.100, 10)
  if stop_reason == 1.0:
    if not codex_tacdiffusion_stationary_for_return():
      codex_autotune_fault_forever(1, 1, 1, 17, 1, 1)
    end
    if not codex_autotune_guarded_return(10, campaign_home_pose, campaign_home_q):
      local return_reason = codex_autotune_return_guard_reason
      if return_reason == 0.0:
        return_reason = 17.0
      end
      codex_autotune_fault_forever(1, 1, 1, return_reason, 1, 1)
    end
    write_output_float_register(35, 60.0)
    textmsg("Step5d TacDiffusion fixture-shadow completed and returned home")
  else:
    # Runtime, packet, sensor, and safety faults stop and latch in place.
    # They never enter the automatic retract/home path.
    codex_autotune_fault_forever(1, 1, 1, stop_reason, 1, 1)
  end
end

codex_step5d_tacdiffusion_fixture_shadow_v1()
'''


def render_script(stamp: str) -> str:
    source = read_bound(SOURCE, SOURCE_SHA256, "frozen V3 source")
    source = replace_once(
        source,
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v3",
        "# STEP5_STAGE_ID: step5d_tacdiffusion_direct_torque_fixture_shadow_v1\n"
        f"# PARENT_V3_SHA256: {SOURCE_SHA256}\n"
        f"# DIRECT_TEMPLATE_SHA256: {DIRECT_TEMPLATE_SHA256}\n"
        "# MODEL_AUTHORITY: fixture_shadow diagnostic_only command_invariant model_active=false",
        "stage identity",
    )
    source = replace_once(
        source,
        "elif codex_abs(normal_force) > 60.0:",
        "elif codex_abs(normal_force) > 50.0:",
        "raw normal force guard",
    )
    source = replace_once(
        source,
        "elif force_norm > 100.0:",
        "elif force_norm > 50.0:",
        "force norm guard",
    )
    trial_marker = "def codex_step5d_autotune_trial_v1(campaign_home_pose, tp_speedj_accel_rad_s2, batch_row_index):"
    source = replace_once(
        source,
        trial_marker,
        direct_stage_source().rstrip() + "\n\n" + trial_marker,
        "Direct Torque helper injection",
    )
    stage_start = "  if stop_reason == 0.0:\n    write_output_float_register(35, 25.0)\n"
    stage_end = (
        "\n  write_output_float_register(30, stop_reason)\n"
        "  write_output_float_register(31, final_progress_m)\n"
    )
    if source.count(stage_start) != 1 or source.count(stage_end) != 1:
        raise ValueError("frozen V3 Stage25 boundary changed")
    before, rest = source.split(stage_start, 1)
    _, after = rest.split(stage_end, 1)
    direct_call = (
        "  if stop_reason == 0.0:\n"
        "    write_output_float_register(35, 25.0)\n"
        "    stop_reason = codex_step5d_direct_torque_stage25()\n"
        "  end\n"
    )
    source = before + direct_call + stage_end + after
    wrapper_marker = "def codex_step5d_strict_rnn_autotune_v3():"
    if source.count(wrapper_marker) != 1:
        raise ValueError("frozen V3 wrapper marker changed")
    source = source.split(wrapper_marker, 1)[0]
    rendered = f"# VERSION: {stamp}\n" + source.rstrip() + "\n\n" + SINGLE_PLAY_WRAPPER.lstrip()
    validate_script(rendered)
    return rendered


def validate_script(script: str) -> None:
    required = (
        f"# PARENT_V3_SHA256: {SOURCE_SHA256}",
        f"# DIRECT_TEMPLATE_SHA256: {DIRECT_TEMPLATE_SHA256}",
        "model_active=false",
        "def codex_step5d_direct_torque_stage25():",
        "direct_torque(tau)",
        "get_coriolis_and_centrifugal_torques(q, qd)",
        "get_jacobian(q)",
        "local fixed_k = [600.0, 600.0, 600.0, 30.0, 30.0, 30.0]",
        "local fixed_d = [69.2820323, 69.2820323, 69.2820323, 4.898979486, 4.898979486, 4.898979486]",
        "local joint_damping = [1.5, 1.5, 1.2, 0.3, 0.3, 0.2]",
        "elif codex_abs(normal_force) > 50.0:",
        "elif force_norm > 50.0:",
        "elif torque_norm > 3.0:",
        "local tau_limit = [20.0, 20.0, 20.0, 8.0, 8.0, 8.0]",
        "local xyz_max = [0.55, 0.24, 0.12]",
        "local MODE_COMPLETE = 2",
        "stop_reason = codex_step5d_direct_torque_stage25()",
        "if stop_reason == 1.0:",
        "codex_autotune_guarded_return(10, campaign_home_pose, campaign_home_q)",
        "codex_autotune_fault_forever(1, 1, 1, stop_reason, 1, 1)",
        "codex_step5d_tacdiffusion_fixture_shadow_v1()",
    )
    missing = [marker for marker in required if marker not in script]
    if missing:
        raise ValueError(f"rendered TacDiffusion TP missing markers: {missing}")
    forbidden = (
        "codex_step5d_strict_rnn_autotune_v3()",
        "speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]",
        "model_active_allowed = True",
        "zero_ftsensor",
    )
    present = [marker for marker in forbidden if marker in script]
    if present:
        raise ValueError(f"rendered TacDiffusion TP contains forbidden markers: {present}")


def build_txt(stamp: str) -> str:
    return f"""Step5d TacDiffusion Direct Torque fixture-shadow v1

Open on Teach Pendant only after controller read-back is verified:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

One-Play contract:
  Start the separately gated bridge, then the operator presses Play once.
  The frozen V3 approach/search/preload runs before Direct Torque Stage25.
  Only normal 60 s completion may use the guarded retract/home route.
  Packet, sensor, runtime, or safety faults stop and latch without auto-home.

TacDiffusion authority:
  fixture_shadow at 50 Hz; diagnostic_only; command_invariant; model_active=false.
  No hardware tare and no zero_ftsensor; the bridge uses 1000 software samples.
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
        "cached script exact": cached == script,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        raise ValueError(f"TacDiffusion TP triplet validation failed: {failed}")


def write_triplet(output_dir: Path, stamp: str) -> dict[str, object]:
    script = render_script(stamp)
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
        "paths": {suffix: str(path) for suffix, path in paths.items()},
        "sha256": {suffix: sha256_bytes(path.read_bytes()) for suffix, path in paths.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--stamp", default="2026-07-20T1500HKT_STEP5D_TACDIFFUSION_DIRECT_TORQUE_FIXTURE_SHADOW_V1")
    args = parser.parse_args(argv)
    print(json.dumps(write_triplet(args.output_dir, args.stamp), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
