#!/usr/bin/env python3
"""Build the one-shot, no-contact Figure-eight geometry canary package."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any

try:
    from build_step6a_eight_no_contact import build_urp
    from step5d_autotune_v4_r013.path_context import (
        FIGURE8_ALONG_AMPLITUDE_M,
        FIGURE8_ALONG_OMEGA_RAD_S,
        FIGURE8_BIN_WIDTH_S,
        FIGURE8_DURATION_S,
        FIGURE8_FULL_BIN_COUNT,
        FIGURE8_LATERAL_AMPLITUDE_M,
        FIGURE8_LATERAL_OMEGA_RAD_S,
        FIGURE8_MAX_SPEED_M_S,
    )
except ModuleNotFoundError:  # pragma: no cover - repository-root import
    from tools.build_step6a_eight_no_contact import build_urp
    from tools.step5d_autotune_v4_r013.path_context import (
        FIGURE8_ALONG_AMPLITUDE_M,
        FIGURE8_ALONG_OMEGA_RAD_S,
        FIGURE8_BIN_WIDTH_S,
        FIGURE8_DURATION_S,
        FIGURE8_FULL_BIN_COUNT,
        FIGURE8_LATERAL_AMPLITUDE_M,
        FIGURE8_LATERAL_OMEGA_RAD_S,
        FIGURE8_MAX_SPEED_M_S,
    )


ROOT = Path(__file__).resolve().parents[1]
PROGRAM_NAME = "step6_figure8_no_contact_canary_v1"
CONTROLLER_DIR = "/programs/andyl/kunwei/step6"
LOCAL_DIR = ROOT / "programs" / "step6"
HOME_POSE = (
    0.4620551816,
    0.1778825964,
    0.0345,
    3.120752062,
    0.0,
    0.068626833,
)
ALONG = (0.0049336434, 0.9999878295)
LATERAL = (-0.9999878295, 0.0049336434)
DURATION_S = FIGURE8_DURATION_S
ANALYTIC_PEAK_SPEED_M_S = FIGURE8_MAX_SPEED_M_S
COMMAND_SPEED_CAP_M_S = 0.010
SOFT_ELLIPSE_M = (0.022, 0.012)
HARD_ELLIPSE_M = (0.025, 0.015)
X_GUARD_M = 0.4888784335
WORKSPACE = {
    "x_m": (0.425, X_GUARD_M),
    "y_m": (0.110, 0.245),
    "z_m": (0.028, 0.038),
}


class FigureEightNoContactPackageError(RuntimeError):
    """The no-contact canary triplet is not closed under its contract."""


def source_stamp(now: datetime | None = None) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    return now.astimezone(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%dT%H%MHKT_STEP6_FIGURE8_NO_CONTACT_CANARY_V1"
    )


def build_input_sha256(*, home_calibration_receipt: Path | None = None) -> str:
    digest = hashlib.sha256()
    for path in (
        Path(__file__),
        ROOT / "tools" / "build_step6a_eight_no_contact.py",
        ROOT / "tools" / "step6_figure8_autotune_v1" / "live_composition.py",
    ):
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(path.read_bytes())
    if home_calibration_receipt is not None:
        receipt = Path(home_calibration_receipt).resolve()
        digest.update(b"home_calibration_receipt")
        digest.update(receipt.read_bytes())
    return digest.hexdigest()


def _materialized_home(
    home_calibration_receipt: Path | None,
) -> tuple[tuple[float, ...], str | None]:
    if home_calibration_receipt is None:
        return HOME_POSE, None
    try:
        receipt = json.loads(Path(home_calibration_receipt).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FigureEightNoContactPackageError(
            "Figure-eight Home calibration receipt is unreadable"
        ) from exc
    if not isinstance(receipt, dict) or receipt.get(
        "schema"
    ) != "step6.autotune/figure8-home-calibration-receipt-v1":
        raise FigureEightNoContactPackageError(
            "Figure-eight Home calibration receipt schema differs"
        )
    pose = tuple(float(value) for value in receipt.get("final_home_pose", ()))
    receipt_sha = receipt.get("receipt_sha256")
    if (
        len(pose) != 6
        or not all(math.isfinite(value) for value in pose)
        or not isinstance(receipt_sha, str)
        or len(receipt_sha) != 64
        or any(character not in "0123456789abcdef" for character in receipt_sha)
    ):
        raise FigureEightNoContactPackageError(
            "Figure-eight Home calibration receipt fields are invalid"
        )
    return pose, receipt_sha


def numeric_sanity(
    *,
    home_pose: tuple[float, ...] = HOME_POSE,
    home_calibration_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, float]] = []
    sample_count_inclusive = int(round(DURATION_S / FIGURE8_BIN_WIDTH_S)) + 1
    for index in range(sample_count_inclusive):
        t = index * FIGURE8_BIN_WIDTH_S
        phase = FIGURE8_ALONG_OMEGA_RAD_S * t
        along = FIGURE8_ALONG_AMPLITUDE_M * math.sin(phase)
        lateral = FIGURE8_LATERAL_AMPLITUDE_M * math.sin(FIGURE8_LATERAL_OMEGA_RAD_S * t)
        vx_local = FIGURE8_ALONG_AMPLITUDE_M * FIGURE8_ALONG_OMEGA_RAD_S * math.cos(phase)
        vy_local = FIGURE8_LATERAL_AMPLITUDE_M * FIGURE8_LATERAL_OMEGA_RAD_S * math.cos(FIGURE8_LATERAL_OMEGA_RAD_S * t)
        rows.append(
            {
                "t_s": t,
                "x_m": home_pose[0] + along * ALONG[0] + lateral * LATERAL[0],
                "y_m": home_pose[1] + along * ALONG[1] + lateral * LATERAL[1],
                "speed_m_s": math.hypot(vx_local, vy_local),
            }
        )
    x_values = [row["x_m"] for row in rows]
    y_values = [row["y_m"] for row in rows]
    speed_values = [row["speed_m_s"] for row in rows]
    payload = {
        "schema": "step6.figure8/no-contact-canary-numeric-sanity-v1",
        "program": PROGRAM_NAME,
        "path": {
            "formula": "along=0.04*sin(0.1t), lateral=0.01*sin(0.2t)",
            "duration_s": DURATION_S,
            "sample_dt_s": FIGURE8_BIN_WIDTH_S,
            "sample_count": len(rows),
            "full_bin_count": FIGURE8_FULL_BIN_COUNT,
            "analytic_peak_speed_m_s": ANALYTIC_PEAK_SPEED_M_S,
            "sampled_peak_speed_m_s": max(speed_values),
            "command_speed_cap_m_s": COMMAND_SPEED_CAP_M_S,
        },
        "home_frame": {
            "pose": list(home_pose),
            "along_xy": list(ALONG),
            "lateral_xy": list(LATERAL),
        },
        "reference_envelope": {
            "x_m": [min(x_values), max(x_values)],
            "y_m": [min(y_values), max(y_values)],
            "z_m": [home_pose[2], home_pose[2]],
        },
        "workspace_guard": {key: list(value) for key, value in WORKSPACE.items()},
        "x_guard_margin_m": X_GUARD_M - max(x_values),
        "tracking_guard": {
            "soft_ellipse_m": list(SOFT_ELLIPSE_M),
            "hard_ellipse_m": list(HARD_ELLIPSE_M),
            "hard_guard_evaluated_before_motion_command": True,
        },
        "one_shot": True,
        "contact_search": False,
        "force_control": False,
        "kunwei_bridge": False,
        "live_acceptance": False,
        "home_status": (
            "contact_derived_final_home"
            if home_calibration_receipt_sha256 is not None
            else "calibration_only_pending_contact_confirmation"
        ),
        "home_calibration_receipt_sha256": home_calibration_receipt_sha256,
    }
    if payload["x_guard_margin_m"] <= 0.0:
        raise FigureEightNoContactPackageError("reference path crosses X guard")
    if max(speed_values) > COMMAND_SPEED_CAP_M_S:
        raise FigureEightNoContactPackageError("reference path exceeds command cap")
    return payload


def build_script(
    stamp: str,
    *,
    input_sha256: str | None = None,
    home_pose: tuple[float, ...] = HOME_POSE,
    home_calibration_receipt_sha256: str | None = None,
) -> str:
    home = ", ".join(f"{value:.10f}" for value in home_pose)
    return f"""# One-shot Step6 Figure-eight no-contact geometry canary v1.
# VERSION: {stamp}
# BUILD_INPUTS_SHA256: {input_sha256 or build_input_sha256()}
# CONTROLLER_TARGET: {CONTROLLER_DIR}/{PROGRAM_NAME}.urp
# MOTION_CLASS: no-contact motion only; no force control/search, no Kunwei, no host writer.
# PATH: along=0.04*sin(0.1t), lateral=0.01*sin(0.2t), duration=60s.
# HOME_FRAME: p[{home}]
# HOME_STATUS: {"contact-derived final Home" if home_calibration_receipt_sha256 else "calibration-only Home; campaign launch forbidden"}
# HOME_CALIBRATION_RECEIPT_SHA256: {home_calibration_receipt_sha256 or "pending"}
# GUARD_ORDER: current transfer envelope -> force/no-contact -> hard tracking ellipse -> speedl.
# TRACE_REGISTERS: int40=state int41=reason double40=t double41/42=desired_xy double43/44=hard/soft_rho double45=cmd_speed double46=z_error double47=orientation_error.

def codex_canary_abs(value):
  if value < 0.0:
    return -value
  end
  return value
end

def codex_canary_clamp(value, lower, upper):
  if value < lower:
    return lower
  elif value > upper:
    return upper
  end
  return value
end

def codex_canary_pose_position_error(actual, expected):
  return sqrt((actual[0] - expected[0]) * (actual[0] - expected[0]) + (actual[1] - expected[1]) * (actual[1] - expected[1]) + (actual[2] - expected[2]) * (actual[2] - expected[2]))
end

def codex_canary_pose_orientation_error(actual, expected):
  return sqrt((actual[3] - expected[3]) * (actual[3] - expected[3]) + (actual[4] - expected[4]) * (actual[4] - expected[4]) + (actual[5] - expected[5]) * (actual[5] - expected[5]))
end

def codex_canary_transfer_pose_ok(actual, expected):
  local orientation_error = codex_canary_pose_orientation_error(actual, expected)
  return actual[0] >= {WORKSPACE['x_m'][0]:.9f} and actual[0] <= {WORKSPACE['x_m'][1]:.9f} and actual[1] >= {WORKSPACE['y_m'][0]:.9f} and actual[1] <= {WORKSPACE['y_m'][1]:.9f} and actual[2] >= {WORKSPACE['z_m'][0]:.9f} and actual[2] <= {WORKSPACE['z_m'][1]:.9f} and orientation_error <= 0.020000000
end

def codex_step6_figure8_no_contact_canary_v1():
  local home_pose = p[{home}]
  local origin_x = {home_pose[0]:.10f}
  local origin_y = {home_pose[1]:.10f}
  local fixed_z = {home_pose[2]:.9f}
  local along_x = {ALONG[0]:.10f}
  local along_y = {ALONG[1]:.10f}
  local lateral_x = {LATERAL[0]:.10f}
  local lateral_y = {LATERAL[1]:.10f}
  local stop_reason = 0
  local state = 1
  local path_t = 0.0
  write_output_integer_register(40, state)
  write_output_integer_register(41, stop_reason)
  write_output_float_register(40, path_t)
  write_output_float_register(43, 0.0)
  write_output_float_register(44, 0.0)

  local transfer_pose = get_actual_tcp_pose()
  if not codex_canary_transfer_pose_ok(transfer_pose, home_pose):
    stop_reason = 1
  end
  if stop_reason == 0:
    movel(home_pose, a=0.030000000, v=0.020000000, r=0.0)
    stopl(0.500000000)
    local entry_pose = get_actual_tcp_pose()
    if codex_canary_pose_position_error(entry_pose, home_pose) > 0.001000000:
      stop_reason = 2
    elif codex_canary_pose_orientation_error(entry_pose, home_pose) > 0.010000000:
      stop_reason = 3
    end
  end

  if stop_reason == 0:
    state = 2
    write_output_integer_register(40, state)
  end
  while stop_reason == 0 and path_t < {DURATION_S:.9f}:
    local pose_now = get_actual_tcp_pose()
    local speed_now = get_actual_tcp_speed()
    local force_now = get_tcp_force()
    local phase = 0.100000000 * path_t
    local local_along = 0.040000000 * sin(phase)
    local local_lateral = 0.010000000 * sin(2.0 * phase)
    local local_v_along = 0.004000000 * cos(phase)
    local local_v_lateral = 0.002000000 * cos(2.0 * phase)
    local desired_x = origin_x + local_along * along_x + local_lateral * lateral_x
    local desired_y = origin_y + local_along * along_y + local_lateral * lateral_y
    local desired_vx = local_v_along * along_x + local_v_lateral * lateral_x
    local desired_vy = local_v_along * along_y + local_v_lateral * lateral_y
    local error_x = pose_now[0] - desired_x
    local error_y = pose_now[1] - desired_y
    local error_along = error_x * along_x + error_y * along_y
    local error_lateral = error_x * lateral_x + error_y * lateral_y
    local hard_rho = (error_along / {HARD_ELLIPSE_M[0]:.9f}) * (error_along / {HARD_ELLIPSE_M[0]:.9f}) + (error_lateral / {HARD_ELLIPSE_M[1]:.9f}) * (error_lateral / {HARD_ELLIPSE_M[1]:.9f})
    local soft_rho = (error_along / {SOFT_ELLIPSE_M[0]:.9f}) * (error_along / {SOFT_ELLIPSE_M[0]:.9f}) + (error_lateral / {SOFT_ELLIPSE_M[1]:.9f}) * (error_lateral / {SOFT_ELLIPSE_M[1]:.9f})
    local orientation_error = codex_canary_pose_orientation_error(pose_now, home_pose)
    local z_error = pose_now[2] - fixed_z
    local force_norm = sqrt(force_now[0] * force_now[0] + force_now[1] * force_now[1] + force_now[2] * force_now[2])
    local speed_norm = sqrt(speed_now[0] * speed_now[0] + speed_now[1] * speed_now[1] + speed_now[2] * speed_now[2])
    local correction_vx = codex_canary_clamp(2.0 * (desired_x - pose_now[0]), -0.003000000, 0.003000000)
    local correction_vy = codex_canary_clamp(2.0 * (desired_y - pose_now[1]), -0.003000000, 0.003000000)
    local correction_vz = codex_canary_clamp(2.0 * (fixed_z - pose_now[2]), -0.003000000, 0.003000000)
    local cmd_vx = desired_vx + correction_vx
    local cmd_vy = desired_vy + correction_vy
    local cmd_vz = correction_vz
    local cmd_speed = sqrt(cmd_vx * cmd_vx + cmd_vy * cmd_vy + cmd_vz * cmd_vz)
    if cmd_speed > {COMMAND_SPEED_CAP_M_S:.9f}:
      local cmd_scale = {COMMAND_SPEED_CAP_M_S:.9f} / cmd_speed
      cmd_vx = cmd_vx * cmd_scale
      cmd_vy = cmd_vy * cmd_scale
      cmd_vz = cmd_vz * cmd_scale
      cmd_speed = {COMMAND_SPEED_CAP_M_S:.9f}
    end
    write_output_float_register(40, path_t)
    write_output_float_register(41, desired_x)
    write_output_float_register(42, desired_y)
    write_output_float_register(43, hard_rho)
    write_output_float_register(44, soft_rho)
    write_output_float_register(45, cmd_speed)
    write_output_float_register(46, z_error)
    write_output_float_register(47, orientation_error)

    if force_norm > 20.000000000 or codex_canary_abs(force_now[2]) > 15.000000000:
      stop_reason = 10
    elif pose_now[0] > {X_GUARD_M:.10f}:
      stop_reason = 11
    elif pose_now[0] < {WORKSPACE['x_m'][0]:.9f} or pose_now[1] < {WORKSPACE['y_m'][0]:.9f} or pose_now[1] > {WORKSPACE['y_m'][1]:.9f} or pose_now[2] < {WORKSPACE['z_m'][0]:.9f} or pose_now[2] > {WORKSPACE['z_m'][1]:.9f}:
      stop_reason = 12
    elif hard_rho > 1.000000000:
      stop_reason = 13
    elif orientation_error > 0.030000000:
      stop_reason = 14
    elif speed_norm > 0.015000000:
      stop_reason = 15
    else:
      speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0], 0.300000000, 0.002000000)
      path_t = path_t + get_steptime()
    end
  end
  stopl(0.500000000)
  write_output_integer_register(41, stop_reason)

  local terminal_pose = get_actual_tcp_pose()
  local safe_return_allowed = stop_reason < 10 and codex_canary_transfer_pose_ok(terminal_pose, home_pose)
  if stop_reason == 0 or safe_return_allowed:
    state = 3
    write_output_integer_register(40, state)
    movel(home_pose, a=0.030000000, v=0.020000000, r=0.0)
    stopl(0.500000000)
    local final_pose = get_actual_tcp_pose()
    if codex_canary_pose_position_error(final_pose, home_pose) <= 0.001000000 and codex_canary_pose_orientation_error(final_pose, home_pose) <= 0.010000000:
      state = 4
    else:
      state = 90
      if stop_reason == 0:
        stop_reason = 20
      end
    end
  else:
    state = 90
  end
  write_output_integer_register(40, state)
  write_output_integer_register(41, stop_reason)
  textmsg("Step6 Figure-eight no-contact canary terminal state/reason:", state, stop_reason)
end

codex_step6_figure8_no_contact_canary_v1()
"""


def build_txt(
    stamp: str,
    *,
    input_sha256: str | None = None,
    home_pose: tuple[float, ...] = HOME_POSE,
    home_calibration_receipt_sha256: str | None = None,
) -> str:
    return f"""Step6 Figure-eight no-contact geometry canary v1

Open on Teach Pendant:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version: {stamp}
Build inputs SHA-256: {input_sha256 or build_input_sha256()}

Motion boundary:
  One-shot no-contact motion only. No force control, no contact search, no Kunwei bridge.
  Home={home_pose}
  Home status={"contact-derived final" if home_calibration_receipt_sha256 else "calibration-only; campaign launch forbidden"}
  Home calibration receipt SHA-256={home_calibration_receipt_sha256 or "pending"}
  path=along 0.04*sin(0.1t), lateral 0.01*sin(0.2t), duration=60 s.
  analytic peak speed={ANALYTIC_PEAK_SPEED_M_S:.12f} m/s; command cap={COMMAND_SPEED_CAP_M_S:.3f} m/s.
  hard tracking ellipse={HARD_ELLIPSE_M}; soft observation ellipse={SOFT_ELLIPSE_M}.
  Any force/workspace/hard/speed guard ends the one-shot; there is no automatic retry.
"""


def _urp_metadata(urp: bytes) -> tuple[str, str, str, str]:
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached = ""
    script_path = ""
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "cachedContents":
            cached = html.unescape(node.text or "")
        elif tag == "file" and node.attrib.get("resolves-to") == "file":
            script_path = (node.text or "").strip()
    return str(root.attrib.get("name", "")), str(root.attrib.get("directory", "")), cached, script_path


def validate_package(
    script: str,
    txt: str,
    urp: bytes,
    stamp: str,
    *,
    home_pose: tuple[float, ...] = HOME_POSE,
    home_calibration_receipt_sha256: str | None = None,
    input_sha256: str | None = None,
) -> dict[str, bool]:
    name, directory, cached, script_path = _urp_metadata(urp)
    path_block = script.split("while stop_reason == 0 and path_t <", 1)[1].split("stopl(0.500000000)", 1)[0]
    guard_offset = path_block.find("elif hard_rho > 1.000000000")
    motion_offset = path_block.find("speedl([cmd_vx")
    checks = {
        "urp_gzip": urp[:2] == b"\x1f\x8b",
        "program_name": name == PROGRAM_NAME,
        "controller_directory": directory == CONTROLLER_DIR,
        "script_path": script_path == f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script",
        "cached_contents_exact": cached == script,
        "source_stamp": stamp in script and stamp in txt and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{4}HKT_STEP6_FIGURE8_NO_CONTACT_CANARY_V1", stamp)),
        "build_inputs_hash": f"BUILD_INPUTS_SHA256: {input_sha256 or build_input_sha256()}" in script,
        "exact_home": f"p[{', '.join(f'{value:.10f}' for value in home_pose)}]" in script,
        "home_authority": (
            (
                home_calibration_receipt_sha256 is None
                and "HOME_CALIBRATION_RECEIPT_SHA256: pending" in script
                and "calibration-only Home; campaign launch forbidden" in script
            )
            or (
                home_calibration_receipt_sha256 is not None
                and f"HOME_CALIBRATION_RECEIPT_SHA256: {home_calibration_receipt_sha256}" in script
                and "contact-derived final Home" in script
            )
        ),
        "exact_duration": f"path_t < {DURATION_S:.9f}" in script,
        "paper_figure8_formula": (
            "along=0.04*sin(0.1t)" in script
            and "lateral=0.01*sin(0.2t)" in script
            and "along=0.04*sin(0.2t)" not in script
            and "lateral=0.01*sin(0.4t)" not in script
        ),
        "hard_guard_before_command": guard_offset >= 0 and motion_offset > guard_offset,
        "return_home": script.count("movel(home_pose") == 2 and "state = 4" in script,
        "one_shot": script.count("codex_step6_figure8_no_contact_canary_v1()") == 2,
        "trace_registers": all(f"write_output_float_register({index}" in script for index in range(40, 48)) and all(f"write_output_integer_register({index}" in script for index in (40, 41)),
        "no_contact_control": "force_mode(" not in script and "zero_ftsensor(" not in script,
        "no_tcp_payload_write": "set_tcp(" not in script and "set_payload(" not in script,
        "no_kunwei": "read_input_float_register" not in script and "socket_" not in script,
        "no_automatic_retry": script.count("while stop_reason == 0") == 1,
    }
    failed = [key for key, value in checks.items() if not value]
    if failed:
        raise FigureEightNoContactPackageError(f"no-contact package validation failed: {failed}")
    return checks


def write_outputs(
    *,
    stamp: str | None = None,
    output_dir: Path = LOCAL_DIR,
    home_calibration_receipt: Path | None = None,
) -> dict[str, Any]:
    selected_stamp = stamp or source_stamp()
    home_pose, home_receipt_sha = _materialized_home(home_calibration_receipt)
    input_sha = build_input_sha256(
        home_calibration_receipt=home_calibration_receipt
    )
    sanity = numeric_sanity(
        home_pose=home_pose,
        home_calibration_receipt_sha256=home_receipt_sha,
    )
    script = build_script(
        selected_stamp,
        input_sha256=input_sha,
        home_pose=home_pose,
        home_calibration_receipt_sha256=home_receipt_sha,
    )
    txt = build_txt(
        selected_stamp,
        input_sha256=input_sha,
        home_pose=home_pose,
        home_calibration_receipt_sha256=home_receipt_sha,
    )
    urp = build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    validation = validate_package(
        script,
        txt,
        urp,
        selected_stamp,
        home_pose=home_pose,
        home_calibration_receipt_sha256=home_receipt_sha,
        input_sha256=input_sha,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "script": output_dir / f"{PROGRAM_NAME}.script",
        "txt": output_dir / f"{PROGRAM_NAME}.txt",
        "urp": output_dir / f"{PROGRAM_NAME}.urp",
    }
    paths["script"].write_text(script, encoding="utf-8")
    paths["txt"].write_text(txt, encoding="utf-8")
    paths["urp"].write_bytes(urp)
    return {
        "program": PROGRAM_NAME,
        "stamp": selected_stamp,
        "build_inputs_sha256": input_sha,
        "paths": {key: str(path) for key, path in paths.items()},
        "sanity": sanity,
        "validation": validation,
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "home_pose": list(home_pose),
        "home_status": (
            "contact_derived_final_home"
            if home_receipt_sha is not None
            else "calibration_only_pending_contact_confirmation"
        ),
        "home_calibration_receipt_sha256": home_receipt_sha,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp")
    parser.add_argument("--output-dir", type=Path, default=LOCAL_DIR)
    parser.add_argument("--home-calibration-receipt", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            write_outputs(
                stamp=args.stamp,
                output_dir=args.output_dir,
                home_calibration_receipt=args.home_calibration_receipt,
            ),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
