#!/usr/bin/env python3
"""Build the immutable Step6 Figure-eight calibration-only Home helper."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    from build_step4e_p0p1_programs import build_urp
except ModuleNotFoundError:  # pragma: no cover - repository-root import
    from tools.build_step4e_p0p1_programs import build_urp


ROOT = Path(__file__).resolve().parents[1]
PROGRAM_NAME = "step6_figure8_start_home_v1"
CONTROLLER_DIR = "/programs/andyl/kunwei/step6"
LOCAL_PROGRAM_DIR = ROOT / "programs" / "step6"
DEFAULT_STAMP = "2026-08-16T0000HKT_STEP6_FIGURE8_START_HOME_V1"

TARGET_POSE = (
    0.4620551816,
    0.1778825964,
    0.034500000,
    3.120752062,
    0.0,
    0.068626833,
)
SAFE_TRANSFER_Z_M = 0.034500000
SEGMENT_1_ACCEL_M_S2 = 0.060
SEGMENT_1_SPEED_M_S = 0.040
SEGMENT_2_ACCEL_M_S2 = 0.135
SEGMENT_2_SPEED_M_S = 0.090
BLEND_RADIUS_M = 0.0
STOPL_DECEL_M_S2 = 0.1

TARGET_X_RANGE_M = (0.300, 0.700)
TARGET_Y_RANGE_M = (-0.200, 0.400)
TARGET_Z_RANGE_M = (0.025, 0.200)
TARGET_ROTATION_NORM_MAX_RAD = 3.200
MOTION_SPEED_MAX_M_S = 0.100
MOTION_ACCEL_MAX_M_S2 = 0.150
FINAL_POSITION_ERROR_MAX_M = 0.003
FINAL_ORIENTATION_ERROR_MAX_RAD = 0.050
FINAL_LINEAR_SPEED_MAX_M_S = 0.001
FINAL_ANGULAR_SPEED_MAX_RAD_S = 0.010
FINAL_JOINT_SPEED_MAX_RAD_S = 0.010

# These are inherited from the mature one-shot positioning helper.  The
# absence check also covers the requested no-path/no-contact/no-runtime scope.
FORBIDDEN_SCRIPT_TOKENS = (
    "speedl",
    "speedj",
    "servoj",
    "force_mode",
    "force",
    "contact",
    "zero_ftsensor",
    "tare",
    "read_input",
    "write_input",
    "write_output",
    "dashboard",
    "rtde",
    "bridge",
    "register",
    "optimizer",
    "campaign",
    "path",
    "arm",
)
EXPECTED_INSTALLATION_RELATIVE_PATH = "../../../default"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _format_pose(values: Sequence[float]) -> str:
    return ", ".join(f"{value:.10f}" for value in values)


def _finite_vector(values: Sequence[float], *, size: int, label: str) -> tuple[float, ...]:
    if len(values) != size:
        raise ValueError(f"{label} must contain exactly {size} values")
    result = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{label} must contain only finite values")
    return result


def initial_pose_is_finite(pose: Sequence[float]) -> bool:
    try:
        values = _finite_vector(pose, size=6, label="initial pose")
    except (TypeError, ValueError):
        return False
    return all(abs(value) <= 100.0 for value in values)


def target_pose_is_within_limits(pose: Sequence[float]) -> bool:
    try:
        values = _finite_vector(pose, size=6, label="target pose")
    except (TypeError, ValueError):
        return False
    x, y, z, rx, ry, rz = values
    rotation_norm = math.sqrt(rx * rx + ry * ry + rz * rz)
    return (
        TARGET_X_RANGE_M[0] <= x <= TARGET_X_RANGE_M[1]
        and TARGET_Y_RANGE_M[0] <= y <= TARGET_Y_RANGE_M[1]
        and TARGET_Z_RANGE_M[0] <= z <= TARGET_Z_RANGE_M[1]
        and rotation_norm <= TARGET_ROTATION_NORM_MAX_RAD
        and SEGMENT_1_ACCEL_M_S2 <= MOTION_ACCEL_MAX_M_S2
        and SEGMENT_2_ACCEL_M_S2 <= MOTION_ACCEL_MAX_M_S2
        and SEGMENT_1_SPEED_M_S <= MOTION_SPEED_MAX_M_S
        and SEGMENT_2_SPEED_M_S <= MOTION_SPEED_MAX_M_S
    )


def _profile_duration(distance_m: float, acceleration_m_s2: float, speed_m_s: float) -> float:
    if distance_m <= 0.0:
        return 0.0
    if acceleration_m_s2 <= 0.0 or speed_m_s <= 0.0:
        raise ValueError("motion profile limits must be positive")
    switching_distance = speed_m_s * speed_m_s / acceleration_m_s2
    if distance_m <= switching_distance:
        return 2.0 * math.sqrt(distance_m / acceleration_m_s2)
    return 2.0 * speed_m_s / acceleration_m_s2 + (
        distance_m - switching_distance
    ) / speed_m_s


def route_geometry(current_pose: Sequence[float]) -> dict[str, Any]:
    """Describe the mature vertical-first route for an arbitrary stationary pose."""

    current = _finite_vector(current_pose, size=6, label="current pose")
    if not initial_pose_is_finite(current):
        raise ValueError("initial pose is outside the finite guard")
    if not target_pose_is_within_limits(TARGET_POSE):
        raise ValueError("fixed calibration Home is outside conservative limits")

    safe_transfer_z = max(current[2], TARGET_POSE[2])
    segment_1_span = safe_transfer_z - current[2]
    segment_2_span = math.hypot(TARGET_POSE[0] - current[0], TARGET_POSE[1] - current[1])
    segment_2_orientation_delta = math.sqrt(
        (TARGET_POSE[3] - current[3]) ** 2
        + (TARGET_POSE[4] - current[4]) ** 2
        + (TARGET_POSE[5] - current[5]) ** 2
    )
    segment_3_span = safe_transfer_z - TARGET_POSE[2]
    return {
        "current_pose": list(current),
        "target_pose": list(TARGET_POSE),
        "safe_transfer_z_m": safe_transfer_z,
        "segments": [
            {
                "id": 1,
                "kind": "vertical_current_xy_current_orientation",
                "span_m": segment_1_span,
                "acceleration_m_s2": SEGMENT_1_ACCEL_M_S2,
                "speed_m_s": SEGMENT_1_SPEED_M_S,
                "minimum_duration_s": _profile_duration(
                    segment_1_span, SEGMENT_1_ACCEL_M_S2, SEGMENT_1_SPEED_M_S
                ),
            },
            {
                "id": 2,
                "kind": "safe_z_target_xy_target_orientation",
                "span_m": segment_2_span,
                "orientation_delta_norm_rad": segment_2_orientation_delta,
                "acceleration_m_s2": SEGMENT_2_ACCEL_M_S2,
                "speed_m_s": SEGMENT_2_SPEED_M_S,
                "minimum_duration_s": _profile_duration(
                    segment_2_span, SEGMENT_2_ACCEL_M_S2, SEGMENT_2_SPEED_M_S
                ),
            },
            {
                "id": 3,
                "kind": "conditional_vertical_target_xy_target_orientation",
                "span_m": segment_3_span,
                "included": segment_3_span > 1.0e-6,
                "acceleration_m_s2": SEGMENT_1_ACCEL_M_S2,
                "speed_m_s": SEGMENT_1_SPEED_M_S,
                "minimum_duration_s": _profile_duration(
                    segment_3_span, SEGMENT_1_ACCEL_M_S2, SEGMENT_1_SPEED_M_S
                ),
            },
        ],
    }


SCRIPT_BODY = f"""# ROLE: standalone immutable one-shot Step6 Figure-eight calibration-only Home helper
# TP_PROGRAM_ID: {PROGRAM_NAME}
# HOME_STATUS: calibration-only Home; live qualification is outside this package
# TARGET_POSE: p[{_format_pose(TARGET_POSE)}]
# SAFE_TRANSFER_Z_POLICY: max(current_z, 0.034500000)
# MOTION_SEGMENT_1: vertical current xy/current orientation, a=0.060 m/s^2, v=0.040 m/s
# MOTION_SEGMENT_2: safe transfer z/target xy/target orientation, a=0.135 m/s^2, v=0.090 m/s
# MOTION_SEGMENT_3: conditional vertical descent to z=0.034500000, a=0.060 m/s^2, v=0.040 m/s
# MOTION_SCOPE: one-shot positioning only; no repeated trajectory or external side effects

def codex_step6_start_home_finite_value(value, absolute_limit):
  if value != value:
    return False
  end
  if value > absolute_limit or value < -absolute_limit:
    return False
  end
  return True
end

def codex_step6_start_home_finite_pose(pose_value):
  local index = 0
  while index < 6:
    if not codex_step6_start_home_finite_value(pose_value[index], 100.0):
      return False
    end
    index = index + 1
  end
  return True
end

def codex_step6_start_home_motion_limits_ok():
  local segment_1_accel_m_s2 = {SEGMENT_1_ACCEL_M_S2:.3f}
  local segment_1_speed_m_s = {SEGMENT_1_SPEED_M_S:.3f}
  local segment_2_accel_m_s2 = {SEGMENT_2_ACCEL_M_S2:.3f}
  local segment_2_speed_m_s = {SEGMENT_2_SPEED_M_S:.3f}
  if segment_1_accel_m_s2 <= 0.0 or segment_1_accel_m_s2 > {MOTION_ACCEL_MAX_M_S2:.9f}:
    return False
  end
  if segment_2_accel_m_s2 <= 0.0 or segment_2_accel_m_s2 > {MOTION_ACCEL_MAX_M_S2:.9f}:
    return False
  end
  if segment_1_speed_m_s <= 0.0 or segment_1_speed_m_s > {MOTION_SPEED_MAX_M_S:.9f}:
    return False
  end
  if segment_2_speed_m_s <= 0.0 or segment_2_speed_m_s > {MOTION_SPEED_MAX_M_S:.9f}:
    return False
  end
  return True
end

def codex_step6_start_home_target_within_limits(target_pose):
  if not codex_step6_start_home_finite_pose(target_pose):
    return False
  end
  if target_pose[0] < {TARGET_X_RANGE_M[0]:.9f} or target_pose[0] > {TARGET_X_RANGE_M[1]:.9f}:
    return False
  end
  if target_pose[1] < {TARGET_Y_RANGE_M[0]:.9f} or target_pose[1] > {TARGET_Y_RANGE_M[1]:.9f}:
    return False
  end
  if target_pose[2] < {TARGET_Z_RANGE_M[0]:.9f} or target_pose[2] > {TARGET_Z_RANGE_M[1]:.9f}:
    return False
  end
  local rotation_norm_rad = sqrt(target_pose[3] * target_pose[3] + target_pose[4] * target_pose[4] + target_pose[5] * target_pose[5])
  if rotation_norm_rad > {TARGET_ROTATION_NORM_MAX_RAD:.9f}:
    return False
  end
  return codex_step6_start_home_motion_limits_ok()
end

def codex_step6_start_home_final_stationary_verified(target_pose):
  local actual_pose = get_actual_tcp_pose()
  if not codex_step6_start_home_finite_pose(actual_pose):
    return False
  end
  local actual_speed = get_actual_tcp_speed()
  local actual_joint_speed = get_actual_joint_speeds()
  local speed_index = 0
  local joint_speed_max_rad_s = 0.0
  while speed_index < 6:
    if not codex_step6_start_home_finite_value(actual_speed[speed_index], 100.0):
      return False
    end
    if not codex_step6_start_home_finite_value(actual_joint_speed[speed_index], 100.0):
      return False
    end
    local joint_speed_abs = actual_joint_speed[speed_index]
    if joint_speed_abs < 0.0:
      joint_speed_abs = -joint_speed_abs
    end
    if joint_speed_abs > joint_speed_max_rad_s:
      joint_speed_max_rad_s = joint_speed_abs
    end
    speed_index = speed_index + 1
  end
  local delta_pose = pose_trans(pose_inv(target_pose), actual_pose)
  local position_error_m = sqrt(delta_pose[0] * delta_pose[0] + delta_pose[1] * delta_pose[1] + delta_pose[2] * delta_pose[2])
  local orientation_error_rad = sqrt(delta_pose[3] * delta_pose[3] + delta_pose[4] * delta_pose[4] + delta_pose[5] * delta_pose[5])
  local linear_speed_m_s = sqrt(actual_speed[0] * actual_speed[0] + actual_speed[1] * actual_speed[1] + actual_speed[2] * actual_speed[2])
  local angular_speed_rad_s = sqrt(actual_speed[3] * actual_speed[3] + actual_speed[4] * actual_speed[4] + actual_speed[5] * actual_speed[5])
  if position_error_m != position_error_m or orientation_error_rad != orientation_error_rad:
    return False
  end
  if linear_speed_m_s != linear_speed_m_s or angular_speed_rad_s != angular_speed_rad_s:
    return False
  end
  return position_error_m <= {FINAL_POSITION_ERROR_MAX_M:.9f} and orientation_error_rad <= {FINAL_ORIENTATION_ERROR_MAX_RAD:.9f} and linear_speed_m_s <= {FINAL_LINEAR_SPEED_MAX_M_S:.9f} and angular_speed_rad_s <= {FINAL_ANGULAR_SPEED_MAX_RAD_S:.9f} and joint_speed_max_rad_s <= {FINAL_JOINT_SPEED_MAX_RAD_S:.9f}
end

def {PROGRAM_NAME}():
  local current_pose = get_actual_tcp_pose()
  local target_pose = p[{_format_pose(TARGET_POSE)}]
  if not codex_step6_start_home_finite_pose(current_pose):
    textmsg("step6_start_home_v1: nonfinite initial pose; halted")
    halt
  end
  if not codex_step6_start_home_target_within_limits(target_pose):
    textmsg("step6_start_home_v1: target or motion limits rejected; halted")
    halt
  end
  local safe_transfer_z = {SAFE_TRANSFER_Z_M:.9f}
  if current_pose[2] > safe_transfer_z:
    safe_transfer_z = current_pose[2]
  end
  local rise_pose = p[current_pose[0], current_pose[1], safe_transfer_z, current_pose[3], current_pose[4], current_pose[5]]
  local transfer_pose = p[target_pose[0], target_pose[1], safe_transfer_z, target_pose[3], target_pose[4], target_pose[5]]
  movel(rise_pose, a={SEGMENT_1_ACCEL_M_S2:.3f}, v={SEGMENT_1_SPEED_M_S:.3f}, r={BLEND_RADIUS_M:.1f})
  stopl({STOPL_DECEL_M_S2:.1f})
  movel(transfer_pose, a={SEGMENT_2_ACCEL_M_S2:.3f}, v={SEGMENT_2_SPEED_M_S:.3f}, r={BLEND_RADIUS_M:.1f})
  stopl({STOPL_DECEL_M_S2:.1f})
  if safe_transfer_z > target_pose[2] + 0.000001:
    local descent_pose = p[target_pose[0], target_pose[1], {SAFE_TRANSFER_Z_M:.9f}, target_pose[3], target_pose[4], target_pose[5]]
    movel(descent_pose, a={SEGMENT_1_ACCEL_M_S2:.3f}, v={SEGMENT_1_SPEED_M_S:.3f}, r={BLEND_RADIUS_M:.1f})
    stopl({STOPL_DECEL_M_S2:.1f})
  end
  sleep(0.20)
  if not codex_step6_start_home_final_stationary_verified(target_pose):
    textmsg("step6_start_home_v1: final target or stationary verification failed; halted")
    halt
  end
  textmsg("step6_start_home_v1: calibration-only Home target stationary and verified")
  halt
end

{PROGRAM_NAME}()
"""


def _validate_urscript_block_balance(script: str) -> None:
    starters = re.compile(r"^(def|thread|if|while|for|sec)\b.*:$")
    stack: list[tuple[str, int]] = []
    for line_number, line in enumerate(script.splitlines(), start=1):
        stripped = line.strip()
        match = starters.match(stripped)
        if match:
            stack.append((match.group(1), line_number))
        elif stripped == "end":
            if not stack:
                raise ValueError(f"unmatched end at line {line_number}")
            stack.pop()
    if stack:
        kind, line_number = stack[-1]
        raise ValueError(f"unclosed {kind} block from line {line_number}")


def _validate_stamp(stamp: str) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{4}HKT_[A-Z0-9_]+", stamp):
        raise ValueError("stamp must include date, hour, minute, and HKT")


def validate_rendered_script(script: str, *, stamp: str) -> None:
    if not script.startswith(f"# VERSION: {stamp}\n"):
        raise ValueError("package script stamp must be the first line")
    _validate_urscript_block_balance(script)
    lower = script.lower()
    forbidden = [token for token in FORBIDDEN_SCRIPT_TOKENS if token in lower]
    if forbidden:
        raise ValueError(f"forbidden runtime/path token in calibration-only helper: {forbidden}")
    required = (
        f"# TP_PROGRAM_ID: {PROGRAM_NAME}",
        "# HOME_STATUS: calibration-only Home",
        f"local target_pose = p[{_format_pose(TARGET_POSE)}]",
        "local current_pose = get_actual_tcp_pose()",
        "local safe_transfer_z = 0.034500000",
        "movel(rise_pose, a=0.060, v=0.040, r=0.0)",
        "movel(transfer_pose, a=0.135, v=0.090, r=0.0)",
        "movel(descent_pose, a=0.060, v=0.040, r=0.0)",
        "get_actual_tcp_speed()",
        "get_actual_joint_speeds()",
        "codex_step6_start_home_final_stationary_verified(target_pose)",
    )
    missing = [marker for marker in required if marker not in script]
    if missing:
        raise ValueError(f"calibration-only helper is missing markers: {missing}")
    if script.count("movel(") != 3 or script.count("stopl(0.1)") != 3:
        raise ValueError("calibration-only route must contain exactly three movel/stopl calls")
    main_start = script.index(f"def {PROGRAM_NAME}():")
    if script[main_start:].splitlines()[1].strip() != "local current_pose = get_actual_tcp_pose()":
        raise ValueError("calibration-only program must begin from get_actual_tcp_pose()")
    route_start = script.index("  local rise_pose", main_start)
    rise = script.index("  movel(rise_pose", route_start)
    transfer = script.index("  movel(transfer_pose", rise)
    descent = script.index("  movel(descent_pose", transfer)
    verification = script.index(
        "codex_step6_start_home_final_stationary_verified(target_pose)", descent
    )
    if not (rise < transfer < descent < verification):
        raise ValueError("calibration-only route is not vertical-first")
    if script.count(f"{PROGRAM_NAME}()") != 2:
        raise ValueError("calibration-only function must have exactly one definition call site")


def source_stamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone(timedelta(hours=8)))
    return value.strftime("%Y-%m-%dT%H%MHKT_STEP6_FIGURE8_START_HOME_V1")


def build_package_script(stamp: str = DEFAULT_STAMP) -> str:
    _validate_stamp(stamp)
    script = f"# VERSION: {stamp}\n" + SCRIPT_BODY
    validate_rendered_script(script, stamp=stamp)
    return script


def build_txt(stamp: str = DEFAULT_STAMP) -> str:
    _validate_stamp(stamp)
    return f"""Step6 Figure-eight calibration-only Home one-shot TP package

Open on the Teach Pendant only after the separate live gates are satisfied:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Identity and scope:
  Standalone one-shot positioning helper for the calibration-only Figure-eight Home.
  Home status: calibration-only; contact-derived Home evidence is intentionally absent.
  This package does not traverse the Figure-eight or claim a contact-derived final Home.

Motion geometry:
  Final target: p[{_format_pose(TARGET_POSE)}]
  Segment 1: current x/y/orientation to max(current z, 0.034500000), a=0.060 m/s^2, v=0.040 m/s.
  Segment 2: constant safe-transfer Z to target x/y/orientation, a=0.135 m/s^2, v=0.090 m/s.
  Segment 3: only when needed, vertical descent to z=0.034500000 with a=0.060 m/s^2, v=0.040 m/s.
  Every segment uses r=0.0 and is separated by stopl(0.1); final target and stationary checks are required.

Excluded behavior:
  No path traversal, contact search, force mode, zero/tare, bridge, register ARM, optimizer, or campaign side effect.
  The triplet is deterministic for a fixed version stamp and local-only; no upload, read-back, or release-pointer change is performed.
"""


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def validate_triplet(script: str, txt: str, urp: bytes, stamp: str) -> dict[str, bool]:
    validate_rendered_script(script, stamp=stamp)
    _validate_stamp(stamp)
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached_contents = ""
    script_path = ""
    for node in root.iter():
        if _xml_local_name(node.tag) == "cachedContents":
            cached_contents = html.unescape(node.text or "")
        elif _xml_local_name(node.tag) == "file" and node.attrib.get("resolves-to") == "file":
            script_path = (node.text or "").strip()
    checks = {
        "script_stamp": stamp in script,
        "txt_stamp": stamp in txt,
        "program_name": root.attrib.get("name") == PROGRAM_NAME,
        "controller_directory": root.attrib.get("directory") == CONTROLLER_DIR,
        "installation_relative_path": root.attrib.get("installationRelativePath")
        == EXPECTED_INSTALLATION_RELATIVE_PATH,
        "script_node_path": script_path == f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script",
        "cached_contents_exact": cached_contents == script,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError(f"calibration-only TP package validation failed: {failed}")
    return checks


def _relative_or_absolute(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path.resolve())


def _manifest(stamp: str, output_dir: Path, digests: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "schema": "step6.figure8-start-home/local-candidate-v1",
        "basename": PROGRAM_NAME,
        "controller_directory": CONTROLLER_DIR,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "source_stamp": stamp,
        "status": "local_candidate_not_uploaded",
        "identity": {
            "step": "Step6",
            "geometry": "Figure-eight calibration-only Home",
            "home_status": "calibration_only_pending_contact_confirmation",
            "contact_derived_final_home_claimed": False,
        },
        "motion_contract": {
            "vertical_first": True,
            "safe_transfer_z_policy": "max(current_z, 0.034500000 m)",
            "one_shot": True,
            "figure8_traversal": False,
            "contact_search": False,
            "force_mode": False,
            "zero_or_tare": False,
            "bridge": False,
            "register_arm": False,
            "optimizer": False,
            "campaign_side_effects": False,
        },
        "immutable": True,
        "builder": "tools/build_step6_figure8_start_home_v1.py",
        "local_package_dir": _relative_or_absolute(output_dir),
        "artifacts": [
            {
                "filename": f"{PROGRAM_NAME}{suffix}",
                "source": f"{PROGRAM_NAME}{suffix}",
                "sha256": digests[suffix],
            }
            for suffix in (".script", ".txt", ".urp")
        ],
        "delivery": {
            "local_urp_internal_content_gate": True,
            "controller_upload": False,
            "controller_readback": False,
            "current_release_pointer_changed": False,
        },
        "live_acceptance": False,
    }


def write_triplet(output_dir: Path = LOCAL_PROGRAM_DIR, stamp: str = DEFAULT_STAMP) -> dict[str, Any]:
    script = build_package_script(stamp)
    txt = build_txt(stamp)
    urp = build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    checks = validate_triplet(script, txt, urp, stamp)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        ".script": output_dir / f"{PROGRAM_NAME}.script",
        ".txt": output_dir / f"{PROGRAM_NAME}.txt",
        ".urp": output_dir / f"{PROGRAM_NAME}.urp",
    }
    manifest_path = output_dir / f"{PROGRAM_NAME}.deploy-manifest.json"
    outputs = (*paths.values(), manifest_path)
    collisions = [str(path) for path in outputs if path.exists()]
    if collisions:
        raise FileExistsError(
            "TP candidate is immutable; existing output must not be overwritten: "
            + ", ".join(collisions)
        )
    paths[".script"].write_text(script, encoding="utf-8")
    paths[".txt"].write_text(txt, encoding="utf-8")
    paths[".urp"].write_bytes(urp)
    digests = {suffix: sha256_bytes(path.read_bytes()) for suffix, path in paths.items()}
    manifest_path.write_text(
        json.dumps(_manifest(stamp, output_dir, digests), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "program": PROGRAM_NAME,
        "controller_dir": CONTROLLER_DIR,
        "stamp": stamp,
        "paths": {suffix: str(path) for suffix, path in paths.items()},
        "sha256": digests,
        "deploy_manifest": str(manifest_path),
        "checks": checks,
    }


def check_triplet(output_dir: Path = LOCAL_PROGRAM_DIR, stamp: str = DEFAULT_STAMP) -> dict[str, Any]:
    output_dir = Path(output_dir)
    expected = {
        ".script": build_package_script(stamp).encode("utf-8"),
        ".txt": build_txt(stamp).encode("utf-8"),
        ".urp": build_urp(build_package_script(stamp), PROGRAM_NAME, CONTROLLER_DIR),
    }
    actual = {
        suffix: (output_dir / f"{PROGRAM_NAME}{suffix}").read_bytes()
        for suffix in expected
    }
    if actual != expected:
        mismatched = [suffix for suffix in expected if actual[suffix] != expected[suffix]]
        raise ValueError(f"immutable calibration-only package differs from canonical render: {mismatched}")
    manifest = json.loads(
        (output_dir / f"{PROGRAM_NAME}.deploy-manifest.json").read_text(encoding="utf-8")
    )
    digests = {suffix: sha256_bytes(value) for suffix, value in actual.items()}
    manifest_digests = {item["source"]: item["sha256"] for item in manifest["artifacts"]}
    expected_manifest_digests = {
        f"{PROGRAM_NAME}{suffix}": digest for suffix, digest in digests.items()
    }
    if manifest_digests != expected_manifest_digests:
        raise ValueError("deploy manifest does not bind the immutable triplet")
    if manifest.get("identity", {}).get("home_status") != "calibration_only_pending_contact_confirmation":
        raise ValueError("deploy manifest does not retain calibration-only Home identity")
    if manifest.get("live_acceptance") is not False:
        raise ValueError("calibration-only package cannot claim live acceptance")
    return {"pass": True, "sha256": digests, "manifest": manifest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=LOCAL_PROGRAM_DIR)
    parser.add_argument("--stamp", default=DEFAULT_STAMP)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    result = (
        check_triplet(args.output_dir, args.stamp)
        if args.check
        else write_triplet(args.output_dir, args.stamp)
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
