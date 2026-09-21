"""Build the one-shot vertical-first migration to the canonical Figure-eight Home."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from build_step4e_p0p1_programs import build_urp
from figure8_home_config import CANONICAL_FIGURE8_HOME_POSE
from step5d_autotune_v4_r012.controller_triplet import validate_urscript_block_balance


ROOT = Path(__file__).resolve().parents[1]
PROGRAM_NAME = "step6_figure8_home_migration_v1"
CONTROLLER_DIR = "/programs/andyl/kunwei/step6"
SAFE_TRANSFER_Z_M = 0.040
VERTICAL_ACCEL_M_S2 = 0.030
VERTICAL_SPEED_M_S = 0.005
TRANSFER_ACCEL_M_S2 = 0.030
TRANSFER_SPEED_M_S = 0.020
POSITION_TOLERANCE_M = 0.001
ORIENTATION_TOLERANCE_RAD = 0.005
LINEAR_SPEED_TOLERANCE_M_S = 0.0005
ANGULAR_SPEED_TOLERANCE_RAD_S = 0.005
JOINT_SPEED_TOLERANCE_RAD_S = 0.001


def _format_pose(pose: tuple[float, ...]) -> str:
    return ", ".join(f"{value:.12f}" for value in pose)


def _stamp(value: str) -> str:
    if not re.fullmatch(r"\d{8}T\d{6}Z_FIGURE8_HOME_MIGRATION_V1", value):
        raise ValueError("stamp must be YYYYMMDDTHHMMSSZ_FIGURE8_HOME_MIGRATION_V1")
    return value


def build_script(stamp: str) -> str:
    _stamp(stamp)
    pose = _format_pose(CANONICAL_FIGURE8_HOME_POSE)
    return f'''# VERSION: {stamp}
# ROLE: one-shot Figure-eight Home migration; no contact, force, or path
# TARGET_POSE: p[{pose}]
# SAFE_TRANSFER_Z_M: {SAFE_TRANSFER_Z_M:.9f}
# MIGRATION_STATUS: explicit canonical Home transition; not a fault recovery

def codex_figure8_migration_finite_pose(value):
  local index = 0
  while index < 6:
    if value[index] != value[index] or value[index] > 100.0 or value[index] < -100.0:
      return False
    end
    index = index + 1
  end
  return True
end

def codex_figure8_migration_stationary(target_pose):
  local actual_pose = get_actual_tcp_pose()
  local actual_speed = get_actual_tcp_speed()
  local actual_qd = get_actual_joint_speeds()
  if not codex_figure8_migration_finite_pose(actual_pose):
    return False
  end
  local delta = pose_trans(pose_inv(target_pose), actual_pose)
  local position_error_m = sqrt(delta[0]*delta[0] + delta[1]*delta[1] + delta[2]*delta[2])
  local orientation_error_rad = sqrt(delta[3]*delta[3] + delta[4]*delta[4] + delta[5]*delta[5])
  local linear_speed_m_s = sqrt(actual_speed[0]*actual_speed[0] + actual_speed[1]*actual_speed[1] + actual_speed[2]*actual_speed[2])
  local angular_speed_rad_s = sqrt(actual_speed[3]*actual_speed[3] + actual_speed[4]*actual_speed[4] + actual_speed[5]*actual_speed[5])
  local joint_speed_max_rad_s = 0.0
  local index = 0
  while index < 6:
    local value = actual_qd[index]
    if value < 0.0:
      value = -value
    end
    if value > joint_speed_max_rad_s:
      joint_speed_max_rad_s = value
    end
    index = index + 1
  end
  return position_error_m <= {POSITION_TOLERANCE_M:.9f} and orientation_error_rad <= {ORIENTATION_TOLERANCE_RAD:.9f} and linear_speed_m_s <= {LINEAR_SPEED_TOLERANCE_M_S:.9f} and angular_speed_rad_s <= {ANGULAR_SPEED_TOLERANCE_RAD_S:.9f} and joint_speed_max_rad_s <= {JOINT_SPEED_TOLERANCE_RAD_S:.9f}
end

def {PROGRAM_NAME}():
  local current_pose = get_actual_tcp_pose()
  local target_pose = p[{pose}]
  if not codex_figure8_migration_finite_pose(current_pose):
    textmsg("figure8 migration: nonfinite initial pose; halted")
    halt
  end
  if abs(current_pose[0] - target_pose[0]) > 0.010 or abs(current_pose[1] - target_pose[1]) > 0.010 or current_pose[2] < 0.030 or current_pose[2] > 0.080:
    textmsg("figure8 migration: initial XY/Z envelope rejected; halted")
    halt
  end
  if not codex_figure8_migration_stationary(current_pose):
    textmsg("figure8 migration: initial state is moving; halted")
    halt
  end
  local safe_transfer_z = {SAFE_TRANSFER_Z_M:.9f}
  if current_pose[2] > safe_transfer_z:
    safe_transfer_z = current_pose[2]
  end
  local rise_pose = p[current_pose[0], current_pose[1], safe_transfer_z, current_pose[3], current_pose[4], current_pose[5]]
  local transfer_pose = p[target_pose[0], target_pose[1], safe_transfer_z, target_pose[3], target_pose[4], target_pose[5]]
  movel(rise_pose, a={VERTICAL_ACCEL_M_S2:.3f}, v={VERTICAL_SPEED_M_S:.3f}, r=0.0)
  stopl(0.1)
  movel(transfer_pose, a={TRANSFER_ACCEL_M_S2:.3f}, v={TRANSFER_SPEED_M_S:.3f}, r=0.0)
  stopl(0.1)
  if safe_transfer_z > target_pose[2] + 0.000001:
    local descent_pose = p[target_pose[0], target_pose[1], target_pose[2], target_pose[3], target_pose[4], target_pose[5]]
    movel(descent_pose, a={VERTICAL_ACCEL_M_S2:.3f}, v={VERTICAL_SPEED_M_S:.3f}, r=0.0)
    stopl(0.1)
  end
  sleep(0.20)
  if not codex_figure8_migration_stationary(target_pose):
    textmsg("figure8 migration: final Home verification failed; halted")
    halt
  end
  textmsg("figure8 migration: canonical Home stationary and verified")
  halt
end

{PROGRAM_NAME}()
'''


def validate_script(script: str, stamp: str) -> None:
    if not script.startswith(f"# VERSION: {stamp}\n"):
        raise ValueError("script stamp is not first")
    validate_urscript_block_balance(script)
    lower = script.lower()
    forbidden = ("speedj", "speedl", "servoj", "force_mode", "zero_ftsensor", "read_input", "write_input", "write_output", "bridge", "register", "optimizer", "campaign")
    if any(token in lower for token in forbidden):
        raise ValueError("migration script contains a forbidden external side effect")
    if script.count("movel(") != 3 or script.count("stopl(0.1)") != 3:
        raise ValueError("migration must contain exactly three stop-separated movel segments")
    if script.count(f"{PROGRAM_NAME}()") != 2:
        raise ValueError("migration call site is not unique")


def validate_urp(urp: bytes, script: str) -> dict[str, bool]:
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached = ""
    script_path = ""
    for node in root.iter():
        if node.tag.endswith("cachedContents"):
            cached = html.unescape(node.text or "")
        if node.tag.endswith("file") and node.attrib.get("resolves-to") == "file":
            script_path = (node.text or "").strip()
    checks = {
        "program_name": root.attrib.get("name") == PROGRAM_NAME,
        "controller_directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "script_path": script_path == f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script",
        "cached_script_exact": cached == script,
    }
    if not all(checks.values()):
        raise ValueError(f"migration URP validation failed: {checks}")
    return checks


def write_triplet(output_dir: Path, stamp: str) -> dict[str, object]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = [output_dir / f"{PROGRAM_NAME}{suffix}" for suffix in (".script", ".txt", ".urp")]
    if any(path.exists() for path in paths):
        raise FileExistsError("migration package output already exists")
    script = build_script(stamp)
    validate_script(script, stamp)
    txt = f"Figure-eight canonical Home migration\nPROGRAM={PROGRAM_NAME}\nVERSION={stamp}\nTARGET_POSE={_format_pose(CANONICAL_FIGURE8_HOME_POSE)}\n"
    urp = build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    checks = validate_urp(urp, script)
    paths[0].write_text(script, encoding="utf-8")
    paths[1].write_text(txt, encoding="utf-8")
    paths[2].write_bytes(urp)
    digests = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    manifest = {
        "schema": "tase.figure8/home-migration-package-v1",
        "program": PROGRAM_NAME,
        "controller_directory": CONTROLLER_DIR,
        "stamp": stamp,
        "target_pose_m_rad": list(CANONICAL_FIGURE8_HOME_POSE),
        "safe_transfer_z_m": SAFE_TRANSFER_Z_M,
        "motion": {
            "vertical_first": True,
            "no_contact": True,
            "no_force_control": True,
            "no_sensor_io": True,
            "no_path": True,
            "segment_1": {"acceleration_m_s2": VERTICAL_ACCEL_M_S2, "speed_m_s": VERTICAL_SPEED_M_S},
            "segment_2": {"acceleration_m_s2": TRANSFER_ACCEL_M_S2, "speed_m_s": TRANSFER_SPEED_M_S},
        },
        "delivery": {"uploaded": False, "readback_verified": False, "played": False},
        "artifacts": digests,
        "urp_checks": checks,
    }
    (output_dir / f"{PROGRAM_NAME}.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--stamp", required=True)
    args = parser.parse_args()
    print(json.dumps(write_triplet(args.output_dir, args.stamp), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
