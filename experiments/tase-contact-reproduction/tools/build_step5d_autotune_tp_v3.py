#!/usr/bin/env python3
"""Build the explicit V3 TP triplet from the frozen V1 control script."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import build_step5d_autotune_tp as v1


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR

PROGRAM_NAME = "step5d_strict_rnn_autotune_v3"
CONTROL_PROFILE_ID = "step5d_strict_rnn_autotune_v1"
PRECONTACT_POSE_PRIOR_ID = STEP5D_V3_PHYSICAL_PRIOR.prior_id
PRECONTACT_POSE_PRIOR_SHA256 = STEP5D_V3_PHYSICAL_PRIOR.fingerprint
PRECONTACT_XYZ_M = STEP5D_V3_PHYSICAL_PRIOR.precontact_xyz_m
PRECONTACT_ROTVEC_RAD = STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad
PRECONTACT_CLEARANCE_M = 0.005
MINIMUM_START_ABOVE_ENTRY_M = 0.01
CONTROLLER_DIR = v1.CONTROLLER_DIR
LOCAL_PROGRAM_DIR = v1.LOCAL_PROGRAM_DIR


def _replace_once(source: str, old: str, new: str, *, role: str) -> str:
    if source.count(old) != 1:
        raise ValueError(f"V1 rendered script {role} marker count differs")
    return source.replace(old, new, 1)


def render_script() -> str:
    parent = v1.render_script()
    parent_sha = hashlib.sha256(parent.encode("utf-8")).hexdigest()
    rendered = _replace_once(
        parent,
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v1",
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v3",
        role="stage identity",
    )
    rendered = _replace_once(
        rendered,
        "def codex_step5d_strict_rnn_autotune_v1():",
        "def codex_step5d_strict_rnn_autotune_v3():",
        role="main definition",
    )
    rendered = _replace_once(
        rendered,
        "codex_step5d_strict_rnn_autotune_v1()",
        "codex_step5d_strict_rnn_autotune_v3()",
        role="main call",
    )
    rendered = _replace_once(
        rendered,
        "  local entry_x = 0.487795411\n"
        "  local entry_y = 0.129326793",
        f"  local entry_x = {PRECONTACT_XYZ_M[0]:.9f}\n"
        f"  local entry_y = {PRECONTACT_XYZ_M[1]:.9f}\n"
        f"  local precontact_z = {PRECONTACT_XYZ_M[2]:.9f}\n"
        f"  local minimum_start_above_entry_m = {MINIMUM_START_ABOVE_ENTRY_M:.9f}",
        role="contact-plus-0.1s prealign position",
    )
    rendered = _replace_once(
        rendered,
        "  # PRECONTACT_POSE_CONTRACT: pre_contact_search_gravity_down_v1; Stage22/24 TCP +Z targets base -Z.\n"
        "  local target_rx = 3.141592654\n"
        "  local target_ry = 0.000000000\n"
        "  local target_rz = 0.000000000",
        f"  # PRECONTACT_POSE_PRIOR_ID: {PRECONTACT_POSE_PRIOR_ID}; completed contact-plus-0.1s robust pose plus clearance before guarded search.\n"
        f"  local target_rx = {PRECONTACT_ROTVEC_RAD[0]:.9f}\n"
        f"  local target_ry = {PRECONTACT_ROTVEC_RAD[1]:.9f}\n"
        f"  local target_rz = {PRECONTACT_ROTVEC_RAD[2]:.9f}",
        role="precontact pose prior",
    )
    rendered = _replace_once(
        rendered,
        "    write_output_float_register(35, 22.0)\n"
        "    local p_current = get_actual_tcp_pose()\n"
        "    local entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]\n"
        "    codex_echo_step4e(stop_reason)\n"
        "    movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)\n"
        "    stopl(0.1)\n"
        "    sleep(0.20)\n"
        "    write_output_float_register(35, 23.0)",
        "    local p_current = get_actual_tcp_pose()\n"
        "    if p_current[2] < precontact_z + minimum_start_above_entry_m:\n"
        "      return 17.0\n"
        "    else:\n"
        "      write_output_float_register(35, 22.0)\n"
        "      local entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]\n"
        "      local entry_precontact_pose = p[entry_x, entry_y, precontact_z, target_rx, target_ry, target_rz]\n"
        "      codex_echo_step4e(stop_reason)\n"
        "      movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)\n"
        "      stopl(0.1)\n"
        "      movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)\n"
        "      stopl(0.1)\n"
        "      sleep(0.20)\n"
        "    end\n"
        "    write_output_float_register(35, 23.0)",
        role="two-step safe prealign",
    )
    identity = (
        f"# RELEASE_STAGE_ID: {PROGRAM_NAME}\n"
        f"# CONTROL_PROFILE_ID: {CONTROL_PROFILE_ID}\n"
        f"# TP_PROGRAM_ID: {PROGRAM_NAME}\n"
        f"# PHYSICAL_PRIOR_SHA256: {PRECONTACT_POSE_PRIOR_SHA256}\n"
        f"# PARENT_AUTOTUNE_V1_RENDERED_SHA256: {parent_sha}\n"
    )
    rendered = identity + rendered
    validate_rendered_script(rendered, parent=parent)
    return rendered


def validate_rendered_script(script: str, *, parent: str | None = None) -> None:
    original = v1.render_script() if parent is None else parent
    required = (
        f"# RELEASE_STAGE_ID: {PROGRAM_NAME}",
        f"# CONTROL_PROFILE_ID: {CONTROL_PROFILE_ID}",
        f"# TP_PROGRAM_ID: {PROGRAM_NAME}",
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v3",
        "def codex_step5d_strict_rnn_autotune_v3():",
        "codex_step5d_autotune_trial_v1(campaign_home_pose, tp_speedj_accel_rad_s2)",
        "codex_step5d_strict_rnn_autotune_v3()",
        f"# PRECONTACT_POSE_PRIOR_ID: {PRECONTACT_POSE_PRIOR_ID}",
        f"local entry_x = {PRECONTACT_XYZ_M[0]:.9f}",
        f"local entry_y = {PRECONTACT_XYZ_M[1]:.9f}",
        f"local precontact_z = {PRECONTACT_XYZ_M[2]:.9f}",
        f"local target_rx = {PRECONTACT_ROTVEC_RAD[0]:.9f}",
        f"local target_ry = {PRECONTACT_ROTVEC_RAD[1]:.9f}",
        f"local target_rz = {PRECONTACT_ROTVEC_RAD[2]:.9f}",
        "local entry_precontact_pose = p[entry_x, entry_y, precontact_z",
        "if p_current[2] < precontact_z + minimum_start_above_entry_m:",
        "movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)",
        "local qdot_cap_rad_s = 0.500",
        "read_input_integer_register(26)",
        "codex_autotune_write_state(0, 0, 10, 0, 0, 0, 0)",
    )
    missing = [marker for marker in required if marker not in script]
    if missing:
        raise ValueError(f"V3 TP script lacks required markers: {missing}")
    prefix_lines = 5
    normalized = "".join(script.splitlines(keepends=True)[prefix_lines:])
    normalized = _replace_once(
        normalized,
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v3",
        "# STEP5_STAGE_ID: step5d_strict_rnn_autotune_v1",
        role="normalized stage identity",
    )
    normalized = _replace_once(
        normalized,
        "def codex_step5d_strict_rnn_autotune_v3():",
        "def codex_step5d_strict_rnn_autotune_v1():",
        role="normalized main definition",
    )
    normalized = _replace_once(
        normalized,
        "codex_step5d_strict_rnn_autotune_v3()",
        "codex_step5d_strict_rnn_autotune_v1()",
        role="normalized main call",
    )
    normalized = _replace_once(
        normalized,
        f"  local entry_x = {PRECONTACT_XYZ_M[0]:.9f}\n"
        f"  local entry_y = {PRECONTACT_XYZ_M[1]:.9f}\n"
        f"  local precontact_z = {PRECONTACT_XYZ_M[2]:.9f}\n"
        f"  local minimum_start_above_entry_m = {MINIMUM_START_ABOVE_ENTRY_M:.9f}",
        "  local entry_x = 0.487795411\n"
        "  local entry_y = 0.129326793",
        role="normalized precontact position",
    )
    normalized = _replace_once(
        normalized,
        f"  # PRECONTACT_POSE_PRIOR_ID: {PRECONTACT_POSE_PRIOR_ID}; completed contact-plus-0.1s robust pose plus clearance before guarded search.\n"
        f"  local target_rx = {PRECONTACT_ROTVEC_RAD[0]:.9f}\n"
        f"  local target_ry = {PRECONTACT_ROTVEC_RAD[1]:.9f}\n"
        f"  local target_rz = {PRECONTACT_ROTVEC_RAD[2]:.9f}",
        "  # PRECONTACT_POSE_CONTRACT: pre_contact_search_gravity_down_v1; Stage22/24 TCP +Z targets base -Z.\n"
        "  local target_rx = 3.141592654\n"
        "  local target_ry = 0.000000000\n"
        "  local target_rz = 0.000000000",
        role="normalized precontact pose prior",
    )
    normalized = _replace_once(
        normalized,
        "    local p_current = get_actual_tcp_pose()\n"
        "    if p_current[2] < precontact_z + minimum_start_above_entry_m:\n"
        "      return 17.0\n"
        "    else:\n"
        "      write_output_float_register(35, 22.0)\n"
        "      local entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]\n"
        "      local entry_precontact_pose = p[entry_x, entry_y, precontact_z, target_rx, target_ry, target_rz]\n"
        "      codex_echo_step4e(stop_reason)\n"
        "      movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)\n"
        "      stopl(0.1)\n"
        "      movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)\n"
        "      stopl(0.1)\n"
        "      sleep(0.20)\n"
        "    end\n"
        "    write_output_float_register(35, 23.0)",
        "    write_output_float_register(35, 22.0)\n"
        "    local p_current = get_actual_tcp_pose()\n"
        "    local entry_xy_pose = p[entry_x, entry_y, p_current[2], target_rx, target_ry, target_rz]\n"
        "    codex_echo_step4e(stop_reason)\n"
        "    movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)\n"
        "    stopl(0.1)\n"
        "    sleep(0.20)\n"
        "    write_output_float_register(35, 23.0)",
        role="normalized two-step prealign",
    )
    if normalized != original:
        raise ValueError("V3 TP differs from frozen V1 outside identity/precontact pose")


def source_stamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone(timedelta(hours=8)))
    return value.strftime("%Y-%m-%dT%H%MHKT_STEP5D_STRICT_RNN_AUTOTUNE_V3")


def build_package_script(stamp: str) -> str:
    if not stamp or "\n" in stamp:
        raise ValueError("source stamp must be one non-empty line")
    return f"# VERSION: {stamp}\n" + render_script()


def build_txt(stamp: str) -> str:
    return f"""Step5d Autotune V3 TP package

Controller target:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Identity:
  release_stage_id={PROGRAM_NAME}
  control_profile_id={CONTROL_PROFILE_ID}
  tp_program_id={PROGRAM_NAME}

Motion class:
  Contact motion package. Upload/read-back does not Load or Play it.
  One Play enters the live campaign; there is no HIL HOLD or second user authorization.
  Before guarded search, Stage22 first moves at the existing safe Z, then moves
  vertically to {PRECONTACT_XYZ_M} with a {PRECONTACT_CLEARANCE_M:.3f} m clearance
  above the completed contact-plus-0.1 s robust surface pose. FAR/NEAR speeds and
  force thresholds remain frozen.

Frozen control contract:
  qdot cap 0.500 rad/s; target 12 N; input integer registers 24..29;
  output integer registers 24..30; heartbeat watchdog fail-closed.
"""


def numeric_sanity(script: str) -> dict[str, Any]:
    validate_rendered_script(script.split("\n", 1)[1] if script.startswith("# VERSION:") else script)
    return {
        "schema": "step5d.autotune-v3/tp-numeric-sanity-v1",
        "program": PROGRAM_NAME,
        "control_profile_id": CONTROL_PROFILE_ID,
        "delta_class": "identity_plus_precontact_pose_and_clearance",
        "precontact_pose_prior_id": PRECONTACT_POSE_PRIOR_ID,
        "physical_prior_sha256": PRECONTACT_POSE_PRIOR_SHA256,
        "reaction_normal_b": list(STEP5D_V3_PHYSICAL_PRIOR.reaction_normal_b),
        "approach_axis_b": list(STEP5D_V3_PHYSICAL_PRIOR.approach_axis_b),
        "precontact_xyz_m": list(PRECONTACT_XYZ_M),
        "precontact_rotvec_rad": list(PRECONTACT_ROTVEC_RAD),
        "precontact_clearance_m": PRECONTACT_CLEARANCE_M,
        "minimum_start_above_entry_m": MINIMUM_START_ABOVE_ENTRY_M,
        "precontact_z_policy": "contact_plus_0p1s_robust_z_plus_0p005m_clearance",
        "qdot_cap_rad_s": 0.5,
        "precontact_entry_accel_m_s2": 0.135,
        "precontact_entry_speed_m_s": 0.09,
        "far_search_speed_m_s": 0.03375,
        "speedj_acceleration_profiles_rad_s2": [0.1, 0.2, 0.5],
        "input_integer_registers": [24, 25, 26, 27, 28, 29],
        "output_integer_registers": [24, 25, 26, 27, 28, 29, 30],
    }


def validate_triplet(script: str, txt: str, urp: bytes, stamp: str) -> dict[str, Any]:
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached = ""
    script_file = ""
    installation = ""
    for node in root.iter():
        if node.tag == "cachedContents":
            cached = html.unescape(node.text or "")
        elif node.tag == "file" and node.attrib.get("resolves-to") == "file":
            script_file = node.text or ""
        elif node.tag == "URProgram":
            installation = node.attrib.get("installationRelativePath", "")
    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "program name": root.attrib.get("name") == PROGRAM_NAME,
        "controller directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "script node": script_file == f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script",
        "cached script": cached == script,
        "installation path": bool(installation),
        "main entrypoint": script.rstrip().endswith(
            "codex_step5d_strict_rnn_autotune_v3()"
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"V3 TP triplet validation failed: {failed}")
    validate_rendered_script(script.split("\n", 1)[1])
    return checks


def write_triplet(output_dir: Path, stamp: str) -> dict[str, Any]:
    script = build_package_script(stamp)
    txt = build_txt(stamp)
    urp = v1.build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    checks = validate_triplet(script, txt, urp, stamp)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        ".script": output_dir / f"{PROGRAM_NAME}.script",
        ".txt": output_dir / f"{PROGRAM_NAME}.txt",
        ".urp": output_dir / f"{PROGRAM_NAME}.urp",
    }
    paths[".script"].write_text(script, encoding="utf-8")
    paths[".txt"].write_text(txt, encoding="utf-8")
    paths[".urp"].write_bytes(urp)
    digests = {
        suffix: hashlib.sha256(path.read_bytes()).hexdigest()
        for suffix, path in paths.items()
    }
    manifest = {
        "schema_version": 1,
        "basename": PROGRAM_NAME,
        "controller_directory": CONTROLLER_DIR,
        "artifacts": [
            {
                "filename": path.name,
                "source": path.name,
                "sha256": digests[suffix],
            }
            for suffix, path in paths.items()
        ],
    }
    manifest_path = output_dir / f"{PROGRAM_NAME}.deploy-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    sanity = numeric_sanity(script)
    sanity_path = output_dir / f"{PROGRAM_NAME}.numeric-sanity.json"
    sanity_path.write_text(
        json.dumps(sanity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "program": PROGRAM_NAME,
        "control_profile_id": CONTROL_PROFILE_ID,
        "controller_dir": CONTROLLER_DIR,
        "stamp": stamp,
        "paths": {suffix: str(path) for suffix, path in paths.items()},
        "sha256": digests,
        "deploy_manifest": str(manifest_path),
        "numeric_sanity": str(sanity_path),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=LOCAL_PROGRAM_DIR)
    parser.add_argument("--stamp", default=None)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            write_triplet(args.output_dir, args.stamp or source_stamp()),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
