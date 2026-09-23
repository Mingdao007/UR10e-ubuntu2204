#!/usr/bin/env python3
"""Build the isolated contact-ramp diagnostic TP triplet; never opens a device."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import shutil

from build_step4e_p0p1_programs import build_urp
from figure8_home_config import (
    CANONICAL_FIGURE8_HOME_POSE,
    CANONICAL_FIGURE8_HOME_Q,
    load_canonical_figure8_home,
    load_canonical_figure8_home_q,
)
from step5d_autotune_v4_r012.controller_triplet import validate_urscript_block_balance
from step5d_eoat_profiles import load_new_eoat_profile


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "programs/step5/step5d/contact-six-qp/step5d_contact_six_qp_v1.script"
HOME_SOURCE_DIR = SOURCE.parent
HOME_SOURCE = HOME_SOURCE_DIR / "step5d_contact_home_v1.script"
HOME_CONFIG = ROOT / "config/figure8_home_v1.json"
DEFAULT_OUTPUT = ROOT / "programs/step5/step5d/contact-ramp-probe-r002"
PROGRAM = "contact_ramp_probe_v1"
HOME_PROGRAM = "step5d_contact_home_v1"
CONTROLLER_DIR = "/programs/andyl/kunwei/step5"
CONTROLLER_TARGET = f"{CONTROLLER_DIR}/{PROGRAM}.urp"
RUNTIME_PROTOCOL = 606006
RUNTIME_REVISION = 26
RUNTIME_EXTENSION = 618002
RAMP_DURATIONS_S = (8, 4, 3, 2, 1)
FORCE_NORM_PROBE_STOP_N = 10.0
RAW_NORMAL_GUARD_N = 20.0
RAW_TORQUE_GUARD_NM = 2.0
TCP_SPEED_GUARD_M_S = 0.005
ACTUAL_JOINT_SPEED_GUARD_RAD_S = 0.06
QDOT_CAP_RAD_S = 0.05


def _replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise ValueError(f"{label} source marker occurs {count} times")
    return source.replace(old, new, 1)


def _parse_pose_literal(source: str) -> tuple[float, ...]:
    match = re.search(r"local fixed_home_pose = p\[([^\]]+)\]", source)
    if match is None:
        raise ValueError("source TP fixed Home pose is missing")
    values = tuple(float(item.strip()) for item in match.group(1).split(","))
    if len(values) != 6 or not all(math.isfinite(value) for value in values):
        raise ValueError("source TP fixed Home pose is not finite six-dimensional")
    return values


def _validate_eoat_binding() -> dict:
    profile = load_new_eoat_profile()
    if (
        profile.profile_id != "new-3d-printed-eoat-v4"
        or profile.payload_kg != 0.413
        or profile.cog_m != (0.0011, 0.0031, 0.0163)
        or profile.controller_tcp_m_rad != (0.0, 0.0, 0.0874, 0.0, 0.0, 0.0)
        or profile.program_z_delta_m != 0.0
    ):
        raise ValueError("probe EOAT profile differs from the established V4 identity")
    return {
        "profile_id": profile.profile_id,
        "profile_sha256": profile.profile_sha256,
        "payload_kg": profile.payload_kg,
        "cog_m": list(profile.cog_m),
        "controller_tcp_m_rad": list(profile.controller_tcp_m_rad),
        "program_z_delta_m": profile.program_z_delta_m,
    }


def _validate_home_input(home_config: Path) -> tuple[tuple[float, ...], tuple[float, ...]]:
    pose = load_canonical_figure8_home(home_config)
    joints = load_canonical_figure8_home_q(home_config)
    if pose != CANONICAL_FIGURE8_HOME_POSE or joints != CANONICAL_FIGURE8_HOME_Q:
        raise ValueError("probe Home input differs from canonical Figure-eight Home")
    if len(pose) != 6 or len(joints) != 6:
        raise ValueError("canonical Home must bind six TCP and six joint values")
    return pose, joints


def transform_probe_script(
    source: str,
    *,
    pose: tuple[float, ...],
    home_q: tuple[float, ...],
    stamp: str,
) -> str:
    if tuple(pose) != CANONICAL_FIGURE8_HOME_POSE or tuple(home_q) != CANONICAL_FIGURE8_HOME_Q:
        raise ValueError("refusing noncanonical probe Home")
    source_pose = _parse_pose_literal(source)
    if any(abs(actual - expected) > 5e-12 for actual, expected in zip(source_pose, pose, strict=True)):
        raise ValueError("source TP TCP Home differs from canonical Figure-eight Home")
    body = source
    program_identity_count = body.count("step5d_contact_six_qp_v1")
    if program_identity_count < 1:
        raise ValueError("source program identity is missing")
    body = body.replace("step5d_contact_six_qp_v1", PROGRAM)
    body = body.replace("618001", str(RUNTIME_EXTENSION))
    body = re.sub(r"local runtime_revision = [0-9]+", f"local runtime_revision = {RUNTIME_REVISION}", body)
    body = body.replace("write_output_integer_register(33, 25)", f"write_output_integer_register(33, {RUNTIME_REVISION})")
    body = re.sub(r"^# VERSION: .*?$", f"# VERSION: {stamp}", body, flags=re.M)
    pose_text = "p[" + ", ".join(f"{value:.12f}" for value in pose) + "]"
    q_text = "[" + ", ".join(f"{value:.16f}" for value in home_q) + "]"
    body = re.sub(r"local fixed_home_pose = p\[[^\]]+\]", f"local fixed_home_pose = {pose_text}", body, count=1)
    body = re.sub(r"^# FIXED_HOME_POSE: .*?$", f"# FIXED_HOME_POSE: {pose_text}", body, flags=re.M)
    body = _replace_once(
        body,
        "  local fixed_home_pose = " + pose_text + "\n",
        "  local fixed_home_pose = " + pose_text + "\n"
        f"  local fixed_home_q = {q_text}\n",
        "canonical fixed Home q declaration",
    )
    body = _replace_once(
        body,
        "  local locked_home_q = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]\n  local locked_home_q_valid = False",
        "  local locked_home_q = fixed_home_q\n  local locked_home_q_valid = True",
        "canonical joint Home lock",
    )
    body = _replace_once(
        body,
        "          local arm_home_q = get_actual_joint_positions()",
        "          local arm_home_q = fixed_home_q",
        "canonical Home attempt q",
    )

    # Input integer 34 is a probe-only duration binding. It is included in the
    # TP's atomic packet snapshot and may not change during an attempt.
    body = _replace_once(
        body,
        "global codex_r006_cache_i32 = 0\n",
        "global codex_r006_cache_i32 = 0\nglobal codex_r006_cache_i34 = 0\n",
        "probe duration cache declaration",
    )
    body = _replace_once(
        body,
        "  if codex_r006_cache_i32 != read_input_integer_register(32):\n    same = False\n  end\n  return same",
        "  if codex_r006_cache_i32 != read_input_integer_register(32):\n    same = False\n  end\n"
        "  if codex_r006_cache_i34 != read_input_integer_register(34):\n    same = False\n  end\n"
        "  return same",
        "probe duration cache comparison",
    )
    body = _replace_once(
        body,
        "  codex_r006_cache_i32 = read_input_integer_register(32)\n",
        "  codex_r006_cache_i32 = read_input_integer_register(32)\n"
        "  codex_r006_cache_i34 = read_input_integer_register(34)\n",
        "probe duration cache copy",
    )
    body = _replace_once(
        body,
        "def codex_r006_execute_attempt(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension):\n",
        "def codex_r006_execute_attempt(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension):\n"
        "  local probe_ramp_duration_s = read_input_integer_register(34)\n"
        "  if kind != 1 or (probe_ramp_duration_s != 8 and probe_ramp_duration_s != 4 and probe_ramp_duration_s != 3 and probe_ramp_duration_s != 2 and probe_ramp_duration_s != 1):\n"
        "    return codex_r006_fault(epoch, ordinal, token, kind, consumed, 79, runtime_revision, runtime_extension)\n"
        "  end\n",
        "probe-only attempt and rung validation",
    )
    body = _replace_once(
        body,
        "    if guard != 0:\n      return codex_r006_fault(epoch, ordinal, token, kind, consumed, guard, runtime_revision, runtime_extension)\n    elif integer_reason != 0:",
        "    if guard != 0:\n      return codex_r006_fault(epoch, ordinal, token, kind, consumed, guard, runtime_revision, runtime_extension)\n"
        "    elif read_input_integer_register(34) != probe_ramp_duration_s:\n"
        "      return codex_r006_fault(epoch, ordinal, token, kind, consumed, 79, runtime_revision, runtime_extension)\n"
        "    elif integer_reason != 0:",
        "probe rung immutability check",
    )
    body = _replace_once(
        body,
        "setpoint - prior_setpoint > 0.5 * 0.080000000 + 0.010000000",
        "setpoint - prior_setpoint > (4.0 / probe_ramp_duration_s) * 0.080000000 + 0.010000000",
        "probe-specific TP ramp slope guard",
    )
    body = body.replace(
        "# Packet freshness bounds host dt below 80 ms; validate the 0.5 N/s\n"
        "      # ramp against that same worst-case interval.",
        "# Fresh packet age remains capped at 80 ms; validate this rung's\n"
        "      # exact 4/duration N/s ramp against the same interval.",
    )

    # Keep the existing 20 N raw-normal and 2 Nm torque limits while adding an
    # independent 10 N Euclidean force-norm stop in normal and Home recovery.
    body, normal_guard_count = re.subn(
        r"codex_r006_packet_guard\(packet_reason, 20\.0, 20\.0, 2\.0\)",
        "codex_r006_packet_guard(packet_reason, 20.0, 10.0, 2.0)",
        body,
    )
    body, recovery_guard_count = re.subn(
        r"codex_r006_recovery_packet_guard\(packet_reason, 20\.0, 20\.0, 2\.0\)",
        "codex_r006_recovery_packet_guard(packet_reason, 20.0, 10.0, 2.0)",
        body,
    )
    if normal_guard_count < 1 or recovery_guard_count < 1:
        raise ValueError("probe 10 N stop was not applied to all TP guard paths")
    if "packet_guard(packet_reason, 20.0, 20.0, 2.0)" in body or "recovery_packet_guard(packet_reason, 20.0, 20.0, 2.0)" in body:
        raise ValueError("a TP normal or recovery force-norm guard remains at 20 N")

    body = body.replace(
        "# ROLE: six-law shared-QP preparation; requires matching host owner 618002",
        "# ROLE: independent contact-ramp probe; qualification-only, no PATH/BO",
    )
    body = body.replace(
        "# CONTACT_CAPS: qdot<=0.05rad/s; speedj_accel=5rad/s2; force_norm<20N; torque_norm<2Nm",
        "# CONTACT_CAPS: qdot<=0.05rad/s; speedj_accel=5rad/s2; force_norm<10N; torque_norm<2Nm",
    )
    body = body.replace("# FIXED_HOME_Q_RAD:", "# FIXED_HOME_Q_RAD:")
    # Keep package identity reviewable next to the fixed Home literals.
    body = _replace_once(
        body,
        f"# FIXED_HOME_POSE: {pose_text}\n",
        f"# FIXED_HOME_POSE: {pose_text}\n# FIXED_HOME_Q_RAD: {q_text}\n",
        "canonical fixed Home q metadata",
    )
    validate_urscript_block_balance(body)
    if body.count("kind != 1 or (probe_ramp_duration_s") != 1:
        raise ValueError("probe package does not fail closed to qualification-only attempts")
    if body.count("codex_r006_packet_guard(packet_reason, 20.0, 10.0, 2.0)") != normal_guard_count:
        raise ValueError("normal force-norm probe guards differ after transform")
    return body


def numeric_sanity() -> dict:
    rungs = []
    for duration_s in RAMP_DURATIONS_S:
        slope = 4.0 / duration_s
        delta_500hz = slope / 500.0
        tp_delta_limit = slope * 0.080 + 0.010
        if delta_500hz > tp_delta_limit + 1e-12:
            raise ValueError(f"{duration_s}s rung exceeds the probe TP slew bound")
        rungs.append({
            "duration_s": duration_s,
            "setpoint_start_n": 1.0,
            "setpoint_target_n": 5.0,
            "slope_n_s": slope,
            "nominal_delta_n_at_500_hz": delta_500hz,
            "tp_max_delta_at_80ms_n": tp_delta_limit,
        })
    return {
        "schema": "contact-ramp-probe-numeric-sanity-v1",
        "claim": "offline arithmetic only; no TP read-back or live qualification",
        "program": PROGRAM,
        "controller_target": CONTROLLER_TARGET,
        "readable_runtime_identity": [RUNTIME_REVISION, RUNTIME_EXTENSION],
        "ramp_durations_s": list(RAMP_DURATIONS_S),
        "rungs": rungs,
        "probe_stop": {"force_norm_n": FORCE_NORM_PROBE_STOP_N},
        "retained_guards": {
            "raw_normal_n": RAW_NORMAL_GUARD_N,
            "raw_torque_norm_nm": RAW_TORQUE_GUARD_NM,
            "qdot_rad_s": QDOT_CAP_RAD_S,
            "actual_joint_speed_rad_s": ACTUAL_JOINT_SPEED_GUARD_RAD_S,
            "speedj_acceleration_rad_s2": 5.0,
        },
        "release": {
            "path_commanded": False,
            "existing_gate_hold_s": 0.5,
            "post_5n_observation_max_s": 2.0,
            "filtered_normal_window_n": [4.0, 5.5],
            "raw_normal_window_n": [3.0, 7.0],
        },
        "figure8_commanded": False,
        "bo_observation": False,
        "return": "existing qualification RETRACT and canonical joint Home verification",
    }


def build(home_config: Path, output: Path) -> dict:
    if not SOURCE.is_file() or not HOME_SOURCE.is_file() or not home_config.is_file():
        raise FileNotFoundError("probe source, Home helper or canonical Home config is missing")
    pose, home_q = _validate_home_input(home_config)
    source = SOURCE.read_text(encoding="utf-8")
    source_pose = _parse_pose_literal(source)
    if any(abs(actual - expected) > 5e-12 for actual, expected in zip(source_pose, pose, strict=True)):
        raise ValueError("source TP TCP Home differs from canonical Figure-eight Home")
    home_source = HOME_SOURCE.read_text(encoding="utf-8")
    target_match = re.search(r"local target_pose = p\[([^\]]+)\]", home_source)
    if target_match is None:
        raise ValueError("existing joint Home helper target is missing")
    helper_pose = tuple(float(item.strip()) for item in target_match.group(1).split(","))
    if len(helper_pose) != 6 or any(
        abs(actual - expected) > 5e-12
        for actual, expected in zip(helper_pose, pose, strict=True)
    ):
        raise ValueError("existing Home helper TCP target differs from canonical Figure-eight Home")
    eoat = _validate_eoat_binding()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    artifacts = [output / f"{PROGRAM}.{ext}" for ext in ("script", "txt", "urp", "binding.json", "numeric-sanity.json")]
    artifacts.extend(output / f"{HOME_PROGRAM}.{ext}" for ext in ("script", "txt", "urp", "binding.json"))
    if any(path.exists() or path.is_symlink() for path in artifacts):
        raise FileExistsError(f"refusing to replace retained probe package: {output}")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%MZ_CONTACT_RAMP_PROBE_V1_618002")
    script = transform_probe_script(source, pose=pose, home_q=home_q, stamp=stamp)
    sanity = numeric_sanity()
    (output / f"{PROGRAM}.script").write_text(script, encoding="utf-8")
    (output / f"{PROGRAM}.txt").write_text(
        "Contact-ramp diagnostic probe\n"
        f"PROGRAM={PROGRAM}\nVERSION={stamp}\nRUNTIME_PROTOCOL={RUNTIME_PROTOCOL}\n"
        f"READABLE_RUNTIME_IDENTITY={RUNTIME_REVISION},{RUNTIME_EXTENSION}\n"
        f"RAMP_DURATIONS_S={','.join(map(str, RAMP_DURATIONS_S))}\n"
        f"FORCE_NORM_PROBE_STOP_N={FORCE_NORM_PROBE_STOP_N:g}\n"
        "HOME=canonical-figure8-joint-home\nFIGURE8=false\nPATH=false\nBO_OBSERVATION=false\nLIVE_QUALIFIED=false\n",
        encoding="ascii",
    )
    (output / f"{PROGRAM}.urp").write_bytes(build_urp(script, PROGRAM, CONTROLLER_DIR))
    for extension in ("script", "txt", "urp", "binding.json"):
        shutil.copyfile(HOME_SOURCE_DIR / f"{HOME_PROGRAM}.{extension}", output / f"{HOME_PROGRAM}.{extension}")
    probe_triplet = {
        extension: hashlib.sha256((output / f"{PROGRAM}.{extension}").read_bytes()).hexdigest()
        for extension in ("script", "txt", "urp")
    }
    home_triplet = {
        extension: hashlib.sha256((output / f"{HOME_PROGRAM}.{extension}").read_bytes()).hexdigest()
        for extension in ("script", "txt", "urp")
    }
    binding = {
        "schema": "contact-ramp-probe-package-binding-v1",
        "program": PROGRAM,
        "home_program": HOME_PROGRAM,
        "version": stamp,
        "stamp": stamp,
        "controller_directory": CONTROLLER_DIR,
        "controller_target": CONTROLLER_TARGET,
        "runtime_protocol": RUNTIME_PROTOCOL,
        "runtime_revision": RUNTIME_REVISION,
        "runtime_extension_protocol": RUNTIME_EXTENSION,
        "readable_runtime_identity": [RUNTIME_REVISION, RUNTIME_EXTENSION],
        "home_config": str(home_config.resolve().relative_to(ROOT)),
        "home_config_sha256": hashlib.sha256(home_config.read_bytes()).hexdigest(),
        "home_pose_m_rad": list(pose),
        "home_q_rad": list(home_q),
        "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "eoat": eoat,
        "triplet_sha256": probe_triplet,
        "home_triplet_sha256": home_triplet,
        "ramp_durations_s": list(RAMP_DURATIONS_S),
        "ramp_slope_n_s": {str(duration): 4.0 / duration for duration in RAMP_DURATIONS_S},
        "force_norm_probe_stop_n": FORCE_NORM_PROBE_STOP_N,
        "raw_normal_guard_n": RAW_NORMAL_GUARD_N,
        "raw_torque_guard_nm": RAW_TORQUE_GUARD_NM,
        "path_commanded": False,
        "figure8_commanded": False,
        "bo_observation": False,
        "live_qualified": False,
        "numeric_sanity": sanity,
    }
    (output / f"{PROGRAM}.binding.json").write_text(json.dumps(binding, indent=2) + "\n", encoding="ascii")
    (output / f"{PROGRAM}.numeric-sanity.json").write_text(json.dumps(sanity, indent=2) + "\n", encoding="ascii")
    return binding


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home-config", type=Path, default=HOME_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    options = parser.parse_args()
    print(json.dumps(build(options.home_config, options.output), indent=2))
