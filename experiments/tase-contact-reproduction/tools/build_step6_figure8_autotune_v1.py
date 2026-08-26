#!/usr/bin/env python3
"""Build the isolated Step6 Figure-eight Autotuner package triplet offline."""

from __future__ import annotations

import argparse
import gzip
import html
import json
import math
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any

try:
    from build_step6a_eight_no_contact import build_urp
    from step6_figure8_autotune_v1.v5_composition_contract import (
        AUTOTUNER_V5_COMPOSITION_SCHEMA,
        AUTOTUNER_V5_COMPOSITION_VERSION,
    )
    from step5d_autotune_v4_r013.path_context import (
        FIGURE8_ALONG_AMPLITUDE_M,
        FIGURE8_ALONG_OMEGA_RAD_S,
        FIGURE8_BIN_WIDTH_S,
        FIGURE8_DURATION_S,
        FIGURE8_FORMAL_BIN_COUNT,
        FIGURE8_FORMAL_START_S,
        FIGURE8_FULL_BIN_COUNT,
        FIGURE8_LATERAL_AMPLITUDE_M,
        FIGURE8_LATERAL_OMEGA_RAD_S,
        FIGURE8_MAX_SPEED_M_S,
        FigureEightPathProviderV1,
        PlanarBasisReceiptV1,
    )
except ModuleNotFoundError:  # pragma: no cover - import from repository root
    from tools.build_step6a_eight_no_contact import build_urp
    from tools.step6_figure8_autotune_v1.v5_composition_contract import (
        AUTOTUNER_V5_COMPOSITION_SCHEMA,
        AUTOTUNER_V5_COMPOSITION_VERSION,
    )
    from tools.step5d_autotune_v4_r013.path_context import (
        FIGURE8_ALONG_AMPLITUDE_M,
        FIGURE8_ALONG_OMEGA_RAD_S,
        FIGURE8_BIN_WIDTH_S,
        FIGURE8_DURATION_S,
        FIGURE8_FORMAL_BIN_COUNT,
        FIGURE8_FORMAL_START_S,
        FIGURE8_FULL_BIN_COUNT,
        FIGURE8_LATERAL_AMPLITUDE_M,
        FIGURE8_LATERAL_OMEGA_RAD_S,
        FIGURE8_MAX_SPEED_M_S,
        FigureEightPathProviderV1,
        PlanarBasisReceiptV1,
    )


ROOT = Path(__file__).resolve().parents[1]
PROGRAM_NAME = "step6_figure8_autotune_v1"
CONTROLLER_DIR = "/programs/andyl/kunwei/step6"
LOCAL_DIR = ROOT / "programs" / "step6"
MATURE_SOURCE = (
    ROOT
    / "programs"
    / "step5"
    / "step5d"
    / "step5d_strict_rnn_autotune_v4_r013.script"
)
NUMERIC_SANITY_PATH = ROOT / "config" / "step6" / f"{PROGRAM_NAME}_numeric_sanity.json"
CALIBRATION_HOME_POSE = (
    0.4620551816,
    0.1778825964,
    0.0345,
    3.120752062,
    0.0,
    0.068626833,
)
HOME_POSE = CALIBRATION_HOME_POSE
ALONG = (0.0049336434, 0.9999878295)
LATERAL = (-0.9999878295, 0.0049336434)
GUARD_LINE_X_M = 0.4888784335
HARD_ELLIPSE_M = (0.025, 0.015)
ACTIVE_CBF_ELLIPSE_M = (0.022, 0.012)
MAX_SPEED_M_S = FIGURE8_MAX_SPEED_M_S
NORMAL_RESERVE_M_S = 0.003
MAX_TOTAL_SPEED_M_S = math.hypot(MAX_SPEED_M_S, NORMAL_RESERVE_M_S)
# Figure-eight differs from V4 only in its path/metric/frame.  Contact search
# remains byte-equivalent to the mature V4 function.
FIGURE8_CONTACT_SEARCH_BOUNDARY_M = 0.011029311
FIGURE8_CONTACT_SEARCH_FAR_SPEED_M_S = 0.005
FIGURE8_CONTACT_SEARCH_NEAR_SPEED_M_S = 0.0002
FIGURE8_CONTACT_SEARCH_FAR_ACCEL_M_S2 = 0.01
FIGURE8_CONTACT_SEARCH_NEAR_ACCEL_M_S2 = 0.005
FIGURE8_CONTACT_SEARCH_FUSE_N = 50.0
FIGURE8_CONTACT_SEARCH_MAX_TRAVEL_M = 0.025
FIGURE8_CONTACT_SEARCH_TIMEOUT_S = 90.0
FIXED_STAMP: str | None = None  # compatibility name; build identity is generated per invocation
V5_LAYOUT_TAG = 607.0
V5_RUNTIME_REVISION = 5
V5_RUNTIME_EXTENSION = 520607
V5_MAX_COMMITTED_ROLLOVERS = 4
V5_INPUT_DOUBLE_REGISTERS = tuple(range(24, 48))
V5_INPUT_INTEGER_REGISTERS = tuple(range(24, 40))
V5_OUTPUT_INTEGER_REGISTERS = tuple(range(24, 35))
V5_METRIC_NAME = "figure8-force-mae-v2"


class Step6FigureEightPackageError(RuntimeError):
    """The local Step6 package is not closed under its offline contract."""


def source_stamp(now: datetime | None = None) -> str:
    """Return the current minute-resolution HKT source stamp."""

    if now is None:
        now = datetime.now(timezone.utc)
    return now.astimezone(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%dT%H%MHKT_STEP6_FIGURE8_AUTOTUNE_V1"
    )


def build_input_sha256(*, home_calibration_receipt: Path | None = None) -> str:
    """Hash the actual source/template/config inputs used by this build."""

    inputs = (
        Path(__file__),
        ROOT / "tools" / "build_step6a_eight_no_contact.py",
        MATURE_SOURCE,
        ROOT / "config" / "step6" / "r013_figure8_direct_campaign_v1.json",
        ROOT / "config" / "step6" / "autotuner_v5_campaign_v2.json",
        ROOT / "config" / "step6" / "autotuner_v5_composition_contract_v2.json",
        ROOT / "tools" / "step6_figure8_autotune_v1" / "v5_composition_contract.py",
        ROOT / "tools" / "step6_figure8_autotune_v1" / "v5_rollover.py",
        ROOT / "tools" / "step6_figure8_autotune_v1" / "v5_resident_protocol.py",
        ROOT / "tools" / "step5d_autotune_v4_r013" / "path_context.py",
    )
    digest = __import__("hashlib").sha256()
    for path in inputs:
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(Path(path).read_bytes())
    if home_calibration_receipt is not None:
        receipt = Path(home_calibration_receipt).resolve()
        digest.update(b"home_calibration_receipt")
        digest.update(receipt.read_bytes())
    return digest.hexdigest()


def _materialized_home(
    home_calibration_receipt: Path | None,
) -> tuple[tuple[float, ...], str | None]:
    if home_calibration_receipt is None:
        return CALIBRATION_HOME_POSE, None
    # Package generation is an offline, dependency-light operation.  The
    # receipt itself was already cold-derived by the live composition owner;
    # re-importing that module here would pull Pinocchio/RTDE into the host
    # builder and turn a valid package check into a misleading host blocker.
    try:
        receipt = json.loads(Path(home_calibration_receipt).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Step6FigureEightPackageError(
            "Figure-eight Home calibration receipt is unreadable"
        ) from exc
    if not isinstance(receipt, dict) or receipt.get(
        "schema"
    ) != "step6.autotune/figure8-home-calibration-receipt-v1":
        raise Step6FigureEightPackageError(
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
        raise Step6FigureEightPackageError(
            "Figure-eight Home calibration receipt fields are invalid"
        )
    return pose, receipt_sha


def _urscript_home_pose(home_pose: tuple[float, ...]) -> str:
    if len(home_pose) != 6:
        raise Step6FigureEightPackageError("Figure-eight Home must contain six values")
    return (
        f"p[{home_pose[0]:.10f}, {home_pose[1]:.10f}, {home_pose[2]:.9f}, "
        f"{home_pose[3]:.9f}, {home_pose[4]:.9f}, {home_pose[5]:.9f}]"
    )


def numeric_sanity(
    *, home_pose: tuple[float, ...] = HOME_POSE,
    home_calibration_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    provider = FigureEightPathProviderV1(
        anchor_pose_base=home_pose,
        basis_receipt=PlanarBasisReceiptV1(
            along_base=(*ALONG, 0.0),
            lateral_base=(*LATERAL, 0.0),
        ),
    )
    sample_count_inclusive = int(round(FIGURE8_DURATION_S / FIGURE8_BIN_WIDTH_S)) + 1
    samples = [
        provider.sample(round(index * FIGURE8_BIN_WIDTH_S, 10))
        for index in range(sample_count_inclusive)
    ]
    xy = [sample.desired_pose_base[:2] for sample in samples]
    acceleration = max(math.hypot(sample.desired_acceleration_base[0], sample.desired_acceleration_base[1]) for sample in samples)
    max_curvature = max(abs(sample.signed_planar_curvature_m_inv) for sample in samples if not sample.degenerate_speed)
    return {
        "schema": "step6.autotune/figure8-numeric-sanity-v1",
        "version": 1,
        "deterministic": True,
        "path": {
            "provider": "FigureEightPathProviderV1",
            "path_id": provider.path_id,
            "path_identity_sha256": provider.identity_receipt.sha256,
            "anchor_pose": list(home_pose),
            "formula": "along=0.04*sin(0.1t), lateral=0.01*sin(0.2t)",
            "duration_s": FIGURE8_DURATION_S,
            "sample_dt_s": FIGURE8_BIN_WIDTH_S,
            "sample_count_inclusive": len(samples),
            "formal_window_s": [FIGURE8_FORMAL_START_S, FIGURE8_DURATION_S],
            "formal_bin_count": FIGURE8_FORMAL_BIN_COUNT,
            "full_bin_count": FIGURE8_FULL_BIN_COUNT,
        },
        "spans_envelope": {
            "local_along_m": [-0.04, 0.04],
            "local_lateral_m": [-0.01, 0.01],
            "base_x_m": [min(value[0] for value in xy), max(value[0] for value in xy)],
            "base_y_m": [min(value[1] for value in xy), max(value[1] for value in xy)],
            "base_x_guard_line_m": GUARD_LINE_X_M,
            "base_x_guard_margin_m": GUARD_LINE_X_M - max(value[0] for value in xy),
        },
        "kinematics": {
            "max_speed_m_s": MAX_SPEED_M_S,
            "max_speed_expected_m_s": FIGURE8_MAX_SPEED_M_S,
            "max_acceleration_m_s2": acceleration,
            "max_abs_signed_curvature_m_inv_sampled": max_curvature,
            "motion_limit_m_s": 0.015,
            "normal_reserve_m_s": NORMAL_RESERVE_M_S,
            "required_total_linear_m_s": MAX_TOTAL_SPEED_M_S,
            "total_linear_limit_m_s": 0.015,
            "acceleration_feasible": acceleration < 0.3,
            "linear_speed_feasible": MAX_TOTAL_SPEED_M_S < 0.015,
        },
        "force_metric_semantics": {
            "target_force_n": 5.0,
            "filtered_normal_is_metric_input": True,
            "metric_name": V5_METRIC_NAME,
            "metric_statistic": "mean_abs(mean(filtered_normal_n_in_bin)-5N)",
            "metric_evidence_window_s": [0.0, FIGURE8_DURATION_S],
            "metric_evidence_bin_count": 600,
            "formal_metric_window_s": [FIGURE8_FORMAL_START_S, FIGURE8_DURATION_S],
            "formal_metric_bin_count": FIGURE8_FORMAL_BIN_COUNT,
            "closure_tail_window_s": [FIGURE8_DURATION_S, 20.0 * math.pi],
            "closure_tail_authority": "old_candidate",
            "closure_tail_excluded_from": ["mae", "censor", "gp", "tell_exact", "exact_novel_budget"],
            "entry_target_ramp": "5.0 N from the stable-contact trial-start tick; contact acquisition latch is separate",
            "primary_metric": "evidence_0_60",
            "secondary_metric": "formal_5_60",
            "correction_enabled_from_s": 5.0,
            "correction_changes_internal_target_only": True,
            "positive_load_semantics": "filtered_normal_n",
        },
        "contact_search": {
            "schema": "step6.autotune/figure8-contact-search-v4-equivalent-v1",
            "boundary_m": FIGURE8_CONTACT_SEARCH_BOUNDARY_M,
            "far_speed_m_s": FIGURE8_CONTACT_SEARCH_FAR_SPEED_M_S,
            "near_speed_m_s": FIGURE8_CONTACT_SEARCH_NEAR_SPEED_M_S,
            "far_acceleration_m_s2": FIGURE8_CONTACT_SEARCH_FAR_ACCEL_M_S2,
            "near_acceleration_m_s2": FIGURE8_CONTACT_SEARCH_NEAR_ACCEL_M_S2,
            "fuse_n": FIGURE8_CONTACT_SEARCH_FUSE_N,
            "max_travel_m": FIGURE8_CONTACT_SEARCH_MAX_TRAVEL_M,
            "timeout_s": FIGURE8_CONTACT_SEARCH_TIMEOUT_S,
            "contract_basis": "v4_equivalent_restored_not_failed_16mm_experiment",
        },
        "guards": {
            "hard_tracking_error_ellipse_m": list(HARD_ELLIPSE_M),
            "hard_tracking_error_evaluated_first": True,
            "active_path_cbf_qp_ellipse_m": list(ACTIVE_CBF_ELLIPSE_M),
            "raw_normal_force_limit_n": 50.0,
            "raw_force_norm_limit_n": 60.0,
            "raw_torque_norm_limit_nm": 3.0,
            "raw_tcp_speed_limit_m_s": 0.015,
            "stale_or_invalid_state": "terminal_stop",
            "attempt_count": 1,
            "safe_return": "recoverable_home",
        },
        "home_frame": {
            "origin_xy_m": list(home_pose[:2]),
            "along_xy": list(ALONG),
            "lateral_xy": list(LATERAL),
            "no_contact_z_m": home_pose[2],
            "orientation_rotvec": list(home_pose[3:]),
            "home_status": (
                "calibration_only_pending_contact_confirmation"
                if home_calibration_receipt_sha256 is None
                else "contact_derived_final_home"
            ),
            "home_calibration_receipt_sha256": home_calibration_receipt_sha256,
        },
        "live_acceptance": False,
    }


def _balance(script: str) -> None:
    starters = re.compile(r"^(def|if|while|for|sec)\b.*:$")
    stack: list[int] = []
    for line_number, line in enumerate(script.splitlines(), 1):
        stripped = line.strip()
        if starters.match(stripped):
            stack.append(line_number)
        elif stripped == "end":
            if not stack:
                raise Step6FigureEightPackageError(f"unmatched URScript end at {line_number}")
            stack.pop()
    if stack:
        line = script.splitlines()[stack[-1] - 1]
        raise Step6FigureEightPackageError(f"unclosed URScript block at {stack[-1]}: {line}")


def _build_direct_speedl_scaffold(
    stamp: str,
    sanity: Mapping[str, Any],
    *,
    input_sha256: str | None = None,
    home_pose: tuple[float, ...] = HOME_POSE,
) -> str:
    origin_x, origin_y = home_pose[:2]
    return f"""# Step6 Figure-eight Autotuner direct/provisional transfer v1.
# VERSION: {stamp}
# GENERATED_AT_LOCAL: frozen minute-resolution HKT source stamp.
# BUILD_INPUTS_SHA256: {input_sha256 or build_input_sha256()}
# CONTROLLER_TARGET: {CONTROLLER_DIR}/{PROGRAM_NAME}.urp
# FLOW_TABLE: STEP6_FLOW.md
# STEP6_STAGE_ID: {PROGRAM_NAME}
# STEP6_TABLE_SOURCE: config/step6_stage_table.json
# STEP6_DIRECT_CAMPAIGN_CONFIG: config/step6/r013_figure8_direct_campaign_v1.json
# STEP6_PATH_PROVIDER: FigureEightPathProviderV1; exact 60 s; filtered_normal_n primary metric [0,60) 600 bins; secondary [5,60) 550 bins.
# STEP6_PRIMARY_METRIC: evidence=[0,60) 600 bins.
# STEP6_SECONDARY_METRIC: formal=[5,60) 550 bins.
# STEP6_PATH_FORMULA: along=0.04*sin(0.1t), lateral=0.01*sin(0.2t), duration=60s.
# HOME_FRAME: origin=[{home_pose[0]:.10f},{home_pose[1]:.10f}], along=[0.0049336434,0.9999878295], lateral=[-0.9999878295,0.0049336434], z={home_pose[2]:.9f}, orientation=[3.120752062,0,0.068626833].
# HARD_GUARD_ORDER: hard tracking-error ellipse 0.025 x 0.015 m evaluated first.
# ACTIVE_PATH_GUARD: active PATH CBF-QP ellipse 0.022 x 0.012 m.
# RAW_LIMITS: normal=50 N, force_norm=60 N, torque_norm=3 Nm, tcp_speed=0.015 m/s.
# STALE_INVALID_STATE: terminal stop; ATTEMPTS: one; RETURN: recoverable safe Home.
# TRANSACTION: Home -> ARM/apply -> 60 s PATH -> seal -> strict admission -> Home.
# OFFLINE_ONLY: no upload/read-back, no Load/Play, no bridge, no motion/contact, no zero/tare, no TCP/payload write.

def codex_abs(x):
  if x < 0.0:
    return -x
  end
  return x
end

def codex_clamp(x, lo, hi):
  if x < lo:
    return lo
  elif x > hi:
    return hi
  end
  return x
end

def codex_step6_figure8_hard_ellipse_first(error_x, error_y):
  local normalized = (error_x / 0.025000000) * (error_x / 0.025000000) + (error_y / 0.015000000) * (error_y / 0.015000000)
  return normalized <= 1.0
end

def codex_step6_figure8_path_cbf_qp(error_x, error_y):
  local normalized = (error_x / 0.022000000) * (error_x / 0.022000000) + (error_y / 0.012000000) * (error_y / 0.012000000)
  return normalized <= 1.0
end

def codex_step6_figure8_safe_home(home_pose):
  stopl(0.250000000)
  movel(home_pose, a=0.060000000, v=0.040000000, r=0.0)
  stopl(0.250000000)
  return True
end

def codex_step6_figure8_autotune_v1():
  local home_pose = p[{origin_x:.10f}, {origin_y:.10f}, {home_pose[2]:.9f}, 3.120752062, 0.000000000, 0.068626833]
  local attempt_count = 0
  local stop_reason = 0.0
  local path_elapsed_s = 0.0
  local stale_s = 0.0
  local last_heartbeat = read_input_float_register(26)
  local along_amplitude_m = 0.040000000
  local lateral_amplitude_m = 0.010000000
  local omega_rad_s = 0.100000000
  local path_hold_s = 0.002000000
  local path_speed_limit_m_s = 0.015000000

  if attempt_count >= 1:
    stop_reason = 91.0
  else:
    attempt_count = attempt_count + 1
    codex_step6_figure8_safe_home(home_pose)
    textmsg("Step6 Figure-eight ARM/apply; host owns strict admission")
    while path_elapsed_s < 60.000000000 and stop_reason == 0.0:
      local heartbeat = read_input_float_register(26)
      local sensor_ok = read_input_float_register(27)
      local stop_request = read_input_float_register(28)
      local normal_force = read_input_float_register(24)
      local force_norm = read_input_float_register(25)
      local torque_norm = read_input_float_register(30)
      local tcp_speed = norm(get_actual_tcp_speed())
      if heartbeat == last_heartbeat:
        stale_s = stale_s + get_steptime()
      else:
        stale_s = 0.0
        last_heartbeat = heartbeat
      end
      local pose_now = get_actual_tcp_pose()
      local phase = omega_rad_s * path_elapsed_s
      local local_x = along_amplitude_m * sin(phase)
      local local_y = lateral_amplitude_m * sin(2.0 * phase)
      local local_vx = along_amplitude_m * omega_rad_s * cos(phase)
      local local_vy = lateral_amplitude_m * 2.0 * omega_rad_s * cos(2.0 * phase)
      local desired_x = {origin_x:.10f} + local_x * {ALONG[0]:.10f} + local_y * {LATERAL[0]:.10f}
      local desired_y = {origin_y:.10f} + local_x * {ALONG[1]:.10f} + local_y * {LATERAL[1]:.10f}
      local error_x = desired_x - pose_now[0]
      local error_y = desired_y - pose_now[1]
      # The outer hard ellipse is evaluated before the active CBF-QP ellipse.
      if not codex_step6_figure8_hard_ellipse_first(error_x, error_y):
        stop_reason = 61.0
      elif not codex_step6_figure8_path_cbf_qp(error_x, error_y):
        stop_reason = 62.0
      elif sensor_ok < 0.5 or stale_s > 0.080000000 or stop_request > 0.5:
        stop_reason = 43.0
      elif codex_abs(normal_force) > 50.000000000 or force_norm > 60.000000000 or torque_norm > 3.000000000 or tcp_speed > 0.015000000:
        stop_reason = 60.0
      else:
        local cmd_vx = local_vx + codex_clamp(2.0 * error_x, -0.003, 0.003)
        local cmd_vy = local_vy + codex_clamp(2.0 * error_y, -0.003, 0.003)
        local cmd_norm = sqrt(cmd_vx * cmd_vx + cmd_vy * cmd_vy)
        if cmd_norm > path_speed_limit_m_s:
          local scale = path_speed_limit_m_s / cmd_norm
          cmd_vx = cmd_vx * scale
          cmd_vy = cmd_vy * scale
        end
        speedl([cmd_vx, cmd_vy, 0.0, 0.0, 0.0, 0.0], 0.300000000, path_hold_s)
        path_elapsed_s = path_elapsed_s + get_steptime()
      end
    end
    if stop_reason == 0.0 and path_elapsed_s >= 60.000000000:
      textmsg("Step6 Figure-eight PATH complete; host seals filtered_normal_n evidence")
    end
    codex_step6_figure8_safe_home(home_pose)
    textmsg("Step6 Figure-eight stop reason:", stop_reason)
  end
end

codex_step6_figure8_autotune_v1()
"""


def _build_scaffold_txt(stamp: str, sanity: Mapping[str, Any], *, input_sha256: str | None = None) -> str:
    return f"""Step6 Figure-eight Autotuner direct/provisional transfer v1

Open on Teach Pendant:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version:
  {stamp}

Build inputs SHA-256:
  {input_sha256 or build_input_sha256()}

Lineage:
  source_mode=provisional_cycloid_n3
  cycloid_empirical_floor_available=false
  correction_seed=zero
  historical_observations_imported=false

Transaction:
  Home -> ARM/apply -> 60 s PATH -> seal -> strict admission -> Home
  One attempt; gaps are preserved; rejected observations never call tell_exact.

Path:
  FigureEightPathProviderV1
  along=0.04*sin(0.1t), lateral=0.01*sin(0.2t), duration=60s
  metric=figure8-force-mae-v2, target=5.0 N, filtered_normal_n, formal=[5,60), bins=550 at 0.1 s

Physical invariants:
  Hard tracking-error ellipse 0.025 x 0.015 m is evaluated first.
  Active PATH CBF-QP ellipse 0.022 x 0.012 m.
  Raw limits: normal 50 N, force norm 60 N, torque norm 3 Nm, TCP speed 0.015 m/s.
  Stale/invalid state is terminal stop; recoverable safe return Home; readable stop reason.
  No TCP/payload write, no zero/tare, no controller upload/read-back in this offline package.
"""


def build_script(
    stamp: str,
    sanity: Mapping[str, Any],
    *,
    input_sha256: str | None = None,
    home_pose: tuple[float, ...] = HOME_POSE,
    home_calibration_receipt_sha256: str | None = None,
) -> str:
    """Build the V5 resident from mature R013 safety/contact/Home seams."""

    del sanity
    source = MATURE_SOURCE.read_text(encoding="utf-8")
    old_program = "step5d_strict_rnn_autotune_v4_r013"
    old_home = (
        "p[0.487834547, 0.129337053, 0.033000000, "
        "3.120752062, 0.000000000, 0.068626833]"
    )
    new_home = _urscript_home_pose(home_pose)
    if source.count(old_program) < 3:
        raise Step6FigureEightPackageError("mature R013 program identity seam differs")
    if source.count(old_home) != 2:
        raise Step6FigureEightPackageError("mature R013 fixed-Home seam differs")
    v5_chain = _v5_execute_chain(source)
    source = source.replace(old_program, PROGRAM_NAME)
    source = source.replace(old_home, new_home)
    body_start = source.index("global codex_r006_cache_valid")
    body = source[body_start:]

    globals_start = body.index("global codex_r006_cache_valid")
    globals_end = body.index("\ndef codex_r006_finite", globals_start)
    body = body[:globals_start] + _v5_cache_globals() + body[globals_end + 1 :]

    packet_start = body.index("def codex_r006_packet_payload_equal")
    packet_end = body.index("\ndef codex_r006_qdot_ok", packet_start)
    body = body[:packet_start] + _v5_packet_functions() + body[packet_end + 1 :]

    guard_start = body.index("def codex_r006_packet_guard")
    guard_end = body.index("\ndef codex_r006_stationary", guard_start)
    packet_guard = body[guard_start:guard_end]
    packet_guard = packet_guard.replace(
        "elif read_input_integer_register(27) != 0 and read_input_integer_register(27) != 1:",
        "elif read_input_integer_register(27) != 0 and read_input_integer_register(27) != 1 and read_input_integer_register(27) != 2 and read_input_integer_register(27) != 3:",
    )
    body = body[:guard_start] + packet_guard + body[guard_end + 1 :]

    echo_start = body.index("def codex_r006_echo")
    fault_start = body.index("def codex_r006_fault", echo_start)
    body = body[:echo_start] + _v5_wire_helpers() + body[fault_start:]

    execute_start = body.index("def codex_r006_execute_attempt")
    body = body[:execute_start] + v5_chain + _v5_main(home_pose)
    provenance = "\n".join(
        (
            f"# VERSION: {stamp}",
            f"# BUILD_INPUTS_SHA256: {input_sha256 or build_input_sha256()}",
            f"# MATURE_PARENT_SCRIPT_SHA256: {__import__('hashlib').sha256(MATURE_SOURCE.read_bytes()).hexdigest()}",
            f"# TP_PROGRAM_ID: {PROGRAM_NAME}",
            f"# EXPECTED_PROGRAM: {PROGRAM_NAME}",
            f"# V5_COMPOSITION_CONTRACT: {AUTOTUNER_V5_COMPOSITION_SCHEMA} v{AUTOTUNER_V5_COMPOSITION_VERSION}.",
            "# WIRE_LAYOUT: layout-607; input_double[24..47]; input_integer[24..39]; output_integer[24..34].",
            "# V5_SESSION_IDENTITY: input_integer[27]=command, input_integer[28]=sequence, input_integer[29]=epoch, input_integer[30]=ordinal, input_integer[31]=kind10..15, input_integer[32]=token.",
            "# V5_ROLLOVER_INPUT: input_integer[33]=NONE/PREPARE/COMMIT/CANCEL, [34]=generation, [35]=early_end, [36]=next_ordinal, [37]=next_kind10..15, [38]=next_token, [39]=qdot_generation.",
            "# V5_OUTPUT_IDENTITY: output_integer[24..28]=epoch/ordinal/state/token/reason; [29..31]=state-dependent overlay; [32..34]=fixed V5 runtime identity.",
            "# V5_SETUP_RATE_HZ: 500; one live writer; no second connection or reader.",
            "# STEP6_LINEAGE: r013 mature contact/safety/fixed-Home/return-Home/qdot seams with V5 resident replacement.",
            "# STEP6_PATH_PROVIDER: FigureEightPathProviderV1; host-owned desired pose/twist/acceleration; exact analytic closure.",
            "# STEP6_PATH: along=0.04sin(0.1t), lateral=0.01sin(0.2t), PATH=[0,60), CLOSURE_TAIL=[60,20*pi).",
            f"# STEP6_METRIC: {V5_METRIC_NAME}; evidence=[0,60) 600 bins; formal=[5,60) 550 bins; tail excluded from MAE/censor/GP/tell_exact/exact-novel budget.",
            "# STEP6_METRIC_INPUT: filtered_normal_n; raw signed normal/force/torque are hard-safety/evidence channels only; no measured readiness window.",
            "# STEP6_ENTRY: contact acquisition uses a 1N latch; same-tick PATH clock=0 starts at fixed 5N; correction/formal metric from 5s.",
            "# V5_ROLLOVER: typed two-slot PREPARE/CANCEL/COMMIT; monotonic generation fence; exactly four committed rollovers; fifth seam Home.",
            "# V5_STATE_OVERLAYS: PATH=25 base; CLOSURE_TAIL=26, ROLLOVER_PREPARED=27, ROLLOVER_COMMITTED=28, ROLLOVER_REJECTED=29 rollover overlay.",
            "# V5_LIFECYCLE: READY_HOME_NEXT=78; COMPLETE=80; STOPPED=90; one live writer; no automatic retry.",
            f"# STEP6_HOME_FRAME: {new_home}.",
            f"# FIXED_HOME_POSE: {new_home}",
            "# STEP6_HOME_STATUS: "
            + (
                "contact-derived final Home"
                if home_calibration_receipt_sha256 is not None
                else "calibration-only Home; owner admission required"
            ),
            f"# STEP6_HOME_CALIBRATION_RECEIPT_SHA256: {home_calibration_receipt_sha256 or 'pending'}",
            "# STEP6_FORCE_CORRECTION: host context policy only; no performance/readiness window is a switch gate.",
            "# STEP6_MATURE_SEAMS: contact search + host speedj + fixed Home + verified return Home + READY_HOME_NEXT.",
            f"# STEP6_CONTACT_SEARCH: V4-equivalent boundary={FIGURE8_CONTACT_SEARCH_BOUNDARY_M:.9f}m; far={FIGURE8_CONTACT_SEARCH_FAR_SPEED_M_S:.6f}m/s; near={FIGURE8_CONTACT_SEARCH_NEAR_SPEED_M_S:.6f}m/s; far_accel={FIGURE8_CONTACT_SEARCH_FAR_ACCEL_M_S2:.6f}m/s2; near_accel={FIGURE8_CONTACT_SEARCH_NEAR_ACCEL_M_S2:.6f}m/s2; fuse={FIGURE8_CONTACT_SEARCH_FUSE_N:.1f}N; travel={FIGURE8_CONTACT_SEARCH_MAX_TRAVEL_M:.3f}m; timeout={FIGURE8_CONTACT_SEARCH_TIMEOUT_S:.1f}s.",
        )
    )
    script = provenance + "\n" + body + "\n"
    if "layout-606" in script or "WIRE_LAYOUT: layout-606" in script:
        raise Step6FigureEightPackageError("Step6 package retained layout-606")
    if "local baseline_" in script or "qualification" in script.lower():
        raise Step6FigureEightPackageError("Step6 package retained a V4 baseline/qualification seam")
    if re.search(r"write_output_integer_register\(\s*(3[5-9]|[4-9][0-9])\s*,", script):
        raise Step6FigureEightPackageError("Step6 package writes an output register outside 24..34")
    if script.count("local d_near_start_travel_m = 0.011029311") != 1:
        raise Step6FigureEightPackageError("Step6 V4 contact-search boundary differs")
    return script


def build_txt(
    stamp: str,
    sanity: Mapping[str, Any],
    *,
    input_sha256: str | None = None,
    home_pose: tuple[float, ...] = HOME_POSE,
    home_calibration_receipt_sha256: str | None = None,
) -> str:
    del sanity
    return f"""Step6 Figure-eight Autotuner V5 layout-607 resident package

Open on Teach Pendant only after offline package inspection and owner admission:
  {CONTROLLER_DIR}/{PROGRAM_NAME}.urp

Version: {stamp}
Build inputs SHA-256: {input_sha256 or build_input_sha256()}
Composition contract: {AUTOTUNER_V5_COMPOSITION_SCHEMA} v{AUTOTUNER_V5_COMPOSITION_VERSION}
Wire: input doubles 24..47; input integers 24..39; output double24 and output integers 24..34 only.
Session identity: int27 command, int28 sequence, int29 epoch, int30 ordinal, int31 V5 kind10..15, int32 token.
Stable latch: input int26; int24/int25 are reserved and are not phase controls.

Mature resident seams retained:
  fixed Home entry check; R008 FAR/NEAR contact search; one chain-start contact acquisition;
  host-owned 500 Hz qdot via speedj; packet/timing/freshness/hard-safety guards;
  verified return Home; READY_HOME_NEXT=78; COMPLETE=80; no EOAT/TCP/payload/tare writes.

V5 resident protocol:
  ONE_NEWTON_ENTRY=21; latch tick enters PATH=25 with path clock exactly 0.
  Contact acquisition uses a 1 N latch outside the trial; PATH starts on the stable-contact tick at fixed 5 N; correction/formal metric from 5 s.
  PATH=[0,60); analytic CLOSURE_TAIL=[60,20*pi); no boundary stopj/stationary/Home/contact.
  Closure tail is old-candidate authority and excluded from MAE/censor/GP/tell_exact/exact-novel budget.
  Rollover inputs: int33..39; PREPARE/CANCEL/COMMIT typed; monotonic generation fence; four committed cap.
  State overlays: base PATH; rollover states 26/27/28/29; commit state 28 resets logical/segment state.
  Matching input35 during PATH is trial-local typed Home; nonmatching input35 is ignored.
  Hard fault/session STOP is dominant STOPPED=90 with no automatic Home/retry.

Figure-eight metric metadata:
  FigureEightPathProviderV1; along=0.04*sin(0.1t); lateral=0.01*sin(0.2t).
  metric={V5_METRIC_NAME}; evidence=[0,60) 600 bins; formal=[5,60) 550 bins.
  tail excluded from MAE/censor/GP/tell_exact/exact-novel budget; closure tail=[60,20*pi).
  filtered_normal_n is the metric input; raw signed normal/force/torque are hard-safety/evidence channels only; no performance/readiness force window is a gate.

Home:
  pose={home_pose}
  status={"contact-derived final" if home_calibration_receipt_sha256 else "calibration-only; owner admission required"}
  calibration receipt SHA-256={home_calibration_receipt_sha256 or "pending"}

Contact search (mature R013/R008 seam):
  boundary={FIGURE8_CONTACT_SEARCH_BOUNDARY_M * 1000.0:.6f} mm;
  far={FIGURE8_CONTACT_SEARCH_FAR_SPEED_M_S * 1000.0:.3f} mm/s;
  near={FIGURE8_CONTACT_SEARCH_NEAR_SPEED_M_S * 1000.0:.3f} mm/s;
  far_accel={FIGURE8_CONTACT_SEARCH_FAR_ACCEL_M_S2 * 1000.0:.3f} mm/s^2;
  near_accel={FIGURE8_CONTACT_SEARCH_NEAR_ACCEL_M_S2 * 1000.0:.3f} mm/s^2;
  fuse={FIGURE8_CONTACT_SEARCH_FUSE_N:.1f} N; travel={FIGURE8_CONTACT_SEARCH_MAX_TRAVEL_M * 1000.0:.1f} mm;
  timeout={FIGURE8_CONTACT_SEARCH_TIMEOUT_S:.1f} s.
"""


def _v5_cache_globals() -> str:
    lines = [
        "global codex_r006_cache_valid = False",
        "global codex_r006_cache_sequence = -1.0",
        "global codex_r006_cache_age_s = 0.0",
        "global codex_r006_attempt_success = False",
        "global codex_r006_attempt_reason = 0",
        "global codex_r006_attempt_guard = 0",
        "global codex_v5_active_epoch = 0",
        "global codex_v5_active_ordinal = 0",
        "global codex_v5_active_kind = 0",
        "global codex_v5_active_token = 0",
        "global codex_v5_active_qdot_generation = 0",
    ]
    lines.extend(f"global codex_r006_cache_d{register} = 0.0" for register in V5_INPUT_DOUBLE_REGISTERS)
    lines.extend(f"global codex_r006_cache_i{register} = 0" for register in V5_INPUT_INTEGER_REGISTERS)
    return "\n".join(lines) + "\n"


def _v5_packet_functions() -> str:
    equal_lines = [
        "def codex_r006_packet_payload_equal():",
        "  local same = True",
    ]
    for register in V5_INPUT_DOUBLE_REGISTERS:
        equal_lines.extend(
            (
                f"  if codex_r006_cache_d{register} != read_input_float_register({register}):",
                "    same = False",
                "  end",
            )
        )
    for register in V5_INPUT_INTEGER_REGISTERS:
        equal_lines.extend(
            (
                f"  if codex_r006_cache_i{register} != read_input_integer_register({register}):",
                "    same = False",
                "  end",
            )
        )
    equal_lines.extend(("  return same", "end", ""))

    copy_lines = ["def codex_r006_copy_packet():"]
    for register in V5_INPUT_DOUBLE_REGISTERS:
        copy_lines.append(
            f"  codex_r006_cache_d{register} = read_input_float_register({register})"
        )
    for register in V5_INPUT_INTEGER_REGISTERS:
        copy_lines.append(
            f"  codex_r006_cache_i{register} = read_input_integer_register({register})"
        )
    copy_lines.extend(
        (
            "  codex_r006_cache_sequence = read_input_float_register(46)",
            "  codex_r006_cache_age_s = 0.0",
            "  codex_r006_cache_valid = True",
            "end",
            "",
        )
    )

    observe_lines = [
        "def codex_r006_packet_observe():",
    ]
    for register in V5_INPUT_DOUBLE_REGISTERS:
        observe_lines.extend(
            (
                f"  if not codex_r006_finite(read_input_float_register({register}), 1000000000000.0):",
                "    return 41",
                "  end",
            )
        )
    observe_lines.extend(
        (
            "  local sequence = read_input_float_register(46)",
            "  local layout = read_input_float_register(47)",
            "  local valid = read_input_float_register(43)",
            "  if sequence < 0.0 or sequence != floor(sequence):",
            "    return 41",
            "  elif layout != 607.0:",
            "    return 42",
            "  elif valid < 0.5 and read_input_float_register(28) < 0.5:",
            "    return 42",
            "  elif not codex_r006_cache_valid:",
            "    codex_r006_copy_packet()",
            "    return 0",
            "  elif sequence > codex_r006_cache_sequence:",
            "    codex_r006_copy_packet()",
            "    return 0",
            "  elif sequence < codex_r006_cache_sequence:",
            "    return 43",
            "  elif not codex_r006_packet_payload_equal():",
            "    return 43",
            "  end",
            "  codex_r006_cache_age_s = codex_r006_cache_age_s + get_steptime()",
            "  if codex_r006_cache_age_s >= 0.080000000:",
            "    return 43",
            "  end",
            "  return 0",
            "end",
            "",
        )
    )

    wire_guard = """def codex_r006_integer_wire_guard():
  # Integer24/25 are reserved in V5 and are not phase controls.
  local reserved_i24 = read_input_integer_register(24)
  local reserved_i25 = read_input_integer_register(25)
  local latch = read_input_integer_register(26)
  local session_command = read_input_integer_register(27)
  local session_sequence = read_input_integer_register(28)
  local epoch = read_input_integer_register(29)
  local ordinal = read_input_integer_register(30)
  local kind = read_input_integer_register(31)
  local token = read_input_integer_register(32)
  local rollover_command = read_input_integer_register(33)
  local generation = read_input_integer_register(34)
  local early_end = read_input_integer_register(35)
  local next_ordinal = read_input_integer_register(36)
  local next_kind = read_input_integer_register(37)
  local next_token = read_input_integer_register(38)
  local qdot_generation = read_input_integer_register(39)
  if reserved_i24 < 0 or reserved_i25 < 0:
    return 67
  elif latch < 0 or latch > 1:
    return 52
  elif session_command < 0 or session_command > 3:
    return 66
  elif session_sequence < 0 or epoch < 0 or ordinal < 0 or token < 0:
    return 67
  elif session_command != 0 and (kind < 10 or kind > 15):
    return 67
  elif rollover_command < 0 or rollover_command > 3:
    return 67
  elif generation < 0 or early_end < 0 or next_ordinal < 0 or next_kind < 0 or next_token < 0 or qdot_generation < 0:
    return 67
  elif next_kind != 0 and (next_kind < 10 or next_kind > 15):
    return 67
  elif rollover_command == 1 and (generation <= 0 or next_ordinal <= 0 or next_kind == 0 or next_token <= 0 or qdot_generation != 0):
    return 67
  elif rollover_command == 2 and (generation <= 0 or next_ordinal <= 0 or next_kind == 0 or next_token <= 0 or qdot_generation != generation):
    return 67
  elif rollover_command == 3 and (generation <= 0 or next_ordinal != 0 or next_kind != 0 or next_token != 0 or qdot_generation != 0):
    return 67
  end
  return 0
end

"""
    return "\n".join(equal_lines + copy_lines + observe_lines) + wire_guard


def _v5_wire_helpers() -> str:
    return """def codex_v5_stop_requested():
  return read_input_integer_register(27) == 3 or read_input_float_register(28) > 0.5
end

def codex_v5_switch_gate_families(hard_safety_gate, timing_freshness_gate, tube_cbf_gate, identity_metric_closure_gate, command_envelope_gate):
  # Every family is evaluated by the resident tick before this pure combiner.
  return hard_safety_gate and timing_freshness_gate and tube_cbf_gate and identity_metric_closure_gate and command_envelope_gate
end

def codex_r006_echo(epoch, ordinal, state, token, reason, consumed, kind, return_guard, runtime_revision, runtime_extension):
  # V5 writes only output integer24..34; the overlay interpretation is state-dependent.
  write_output_float_register(24, codex_r006_cache_sequence)
  write_output_integer_register(24, epoch)
  write_output_integer_register(25, ordinal)
  write_output_integer_register(26, state)
  write_output_integer_register(27, token)
  write_output_integer_register(28, reason)
  write_output_integer_register(29, consumed)
  write_output_integer_register(30, kind)
  write_output_integer_register(31, return_guard)
  write_output_integer_register(32, 607007)
  write_output_integer_register(33, 5)
  write_output_integer_register(34, 520607)
end

"""


def _v5_execute_chain(source: str) -> str:
    start = source.index("def codex_r006_execute_attempt")
    end = source.index("\ndef step5d_strict_rnn_autotune_v4_r013", start)
    mature_attempt = source[start:end]
    contact_marker = (
        "  stopl(0.010000000)\n"
        "  if not codex_r006_stationary(0.250000000):\n"
        "    return codex_r006_fault(epoch, ordinal, token, kind, consumed, 11, runtime_revision, runtime_extension)\n"
        "  end\n"
    )
    contact_end = mature_attempt.index(contact_marker) + len(contact_marker)
    contact_prefix = mature_attempt[:contact_end].replace(
        "def codex_r006_execute_attempt",
        "def codex_r006_execute_chain",
        1,
    )
    return contact_prefix + _v5_reject_helper() + _v5_resident_template()


def _v5_reject_helper() -> str:
    return """def codex_v5_reject_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, reason, ack_generation, active_qdot_generation, prepared_candidate_token):
  codex_r006_echo(epoch, ordinal, 29, token, reason, ack_generation, active_qdot_generation, prepared_candidate_token, runtime_revision, runtime_extension)
  return codex_r006_return_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, 0)
end

"""




def _v5_resident_template() -> str:
    return r'''  # One contact acquisition feeds the complete resident chain.
  codex_v5_active_epoch = epoch
  codex_v5_active_ordinal = ordinal
  codex_v5_active_kind = kind
  codex_v5_active_token = token
  codex_v5_active_qdot_generation = 0
  local active_path_qdot = [read_input_float_register(37), read_input_float_register(38), read_input_float_register(39), read_input_float_register(40), read_input_float_register(41), read_input_float_register(42)]
  local committed_rollovers = 0
  local rollover_generation_fence = 0
  local prepared_valid = False
  local prepared_generation = 0
  local prepared_ordinal = 0
  local prepared_kind = 0
  local prepared_token = 0
  local prepared_path_qdot = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local commit_armed = False
  local armed_qdot_generation = 0
  local qdot_generation = 0
  local metric_sample_count = 0
  local censor_sample_count = 0
  local formal_metric_sample_count = 0
  local correction_generation = 0
  local force_integral_n_s = 0.0
  local path_elapsed_s = 0.0
  local tail_end_s = 20.0 * 3.141592653589793
  local latch_seen = False
  local last_rollover_command = 0
  local last_rollover_generation = 0
  local last_rollover_ordinal = 0
  local last_rollover_kind = 0
  local last_rollover_token = 0
  local last_rollover_qdot_generation = 0

  # ONE_NEWTON_ENTRY: only the existing bounded host qdot approach runs until
  # the typed stable latch.  Rollover registers are held untouched here.
  while not latch_seen:
    local actual_dt = get_steptime()
    local packet_reason = codex_r006_packet_observe()
    local entry_guard = codex_r006_packet_guard(packet_reason, 60.0, 100.0, 3.0)
    local integer_reason = codex_r006_integer_wire_guard()
    local latch = read_input_integer_register(26)
    if actual_dt <= 0.0 or actual_dt >= 0.080000000:
      return codex_r006_fault(epoch, ordinal, token, kind, consumed, 46, runtime_revision, runtime_extension)
    elif codex_v5_stop_requested():
      return codex_r006_fault(epoch, ordinal, token, kind, consumed, 4, runtime_revision, runtime_extension)
    elif entry_guard != 0:
      return codex_r006_fault(epoch, ordinal, token, kind, consumed, entry_guard, runtime_revision, runtime_extension)
    elif integer_reason != 0:
      return codex_r006_fault(epoch, ordinal, token, kind, consumed, integer_reason, runtime_revision, runtime_extension)
    elif latch == 1:
      latch_seen = True
      active_path_qdot = [read_input_float_register(37), read_input_float_register(38), read_input_float_register(39), read_input_float_register(40), read_input_float_register(41), read_input_float_register(42)]
      path_elapsed_s = 0.0
      codex_r006_echo(epoch, ordinal, 25, token, 0, consumed, kind, 0, runtime_revision, runtime_extension)
    else:
      local entry_qdot = [read_input_float_register(37), read_input_float_register(38), read_input_float_register(39), read_input_float_register(40), read_input_float_register(41), read_input_float_register(42)]
      speedj(entry_qdot, 40.000000000, actual_dt)
      codex_r006_echo(epoch, ordinal, 21, token, 0, consumed, kind, 0, runtime_revision, runtime_extension)
    end
    sync()
  end

  while True:
    local actual_path_dt = get_steptime()
    local packet_reason = codex_r006_packet_observe()
    local motion_guard = codex_r006_packet_guard(packet_reason, 60.0, 100.0, 3.0)
    local integer_reason = codex_r006_integer_wire_guard()
    local rollover_command = read_input_integer_register(33)
    local rollover_generation = read_input_integer_register(34)
    local early_end_request = read_input_integer_register(35)
    local next_ordinal = read_input_integer_register(36)
    local next_kind = read_input_integer_register(37)
    local next_token = read_input_integer_register(38)
    local next_qdot_generation = read_input_integer_register(39)
    local live_candidate_qdot = [read_input_float_register(37), read_input_float_register(38), read_input_float_register(39), read_input_float_register(40), read_input_float_register(41), read_input_float_register(42)]
    local resident_state = 25
    local tail_crossing = path_elapsed_s + actual_path_dt >= tail_end_s
    local bounded_motion_dt = actual_path_dt
    if tail_crossing:
      bounded_motion_dt = tail_end_s - path_elapsed_s
    end
    local duplicate_prepare = rollover_command == 1 and last_rollover_command == 1 and rollover_generation == last_rollover_generation and next_ordinal == last_rollover_ordinal and next_kind == last_rollover_kind and next_token == last_rollover_token and next_qdot_generation == last_rollover_qdot_generation
    local duplicate_cancel = rollover_command == 3 and last_rollover_command == 3 and rollover_generation == last_rollover_generation and next_ordinal == last_rollover_ordinal and next_kind == last_rollover_kind and next_token == last_rollover_token and next_qdot_generation == last_rollover_qdot_generation
    local duplicate_commit = rollover_command == 2 and last_rollover_command == 2 and rollover_generation == last_rollover_generation and next_ordinal == last_rollover_ordinal and next_kind == last_rollover_kind and next_token == last_rollover_token and next_qdot_generation == last_rollover_qdot_generation
    local duplicate_commit_ack = duplicate_commit
    if actual_path_dt <= 0.0 or actual_path_dt >= 0.080000000:
      return codex_r006_fault(epoch, ordinal, token, kind, consumed, 46, runtime_revision, runtime_extension)
    elif codex_v5_stop_requested():
      return codex_r006_fault(epoch, ordinal, token, kind, consumed, 4, runtime_revision, runtime_extension)
    elif motion_guard != 0:
      return codex_r006_fault(epoch, ordinal, token, kind, consumed, motion_guard, runtime_revision, runtime_extension)
    elif integer_reason != 0:
      if rollover_command != 0:
        return codex_v5_reject_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, integer_reason, rollover_generation_fence, qdot_generation, prepared_token)
      else:
        return codex_r006_fault(epoch, ordinal, token, kind, consumed, integer_reason, runtime_revision, runtime_extension)
      end
    else:
      if path_elapsed_s < 60.000000000 and early_end_request == ordinal and early_end_request > 0:
        return codex_v5_reject_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, 35, rollover_generation_fence, qdot_generation, prepared_token)
      elif rollover_command == 0:
        # NONE clears only the contiguous duplicate discriminator.
        last_rollover_command = 0
        last_rollover_generation = 0
        last_rollover_ordinal = 0
        last_rollover_kind = 0
        last_rollover_token = 0
        last_rollover_qdot_generation = 0
      elif rollover_command == 1:
        if duplicate_prepare and prepared_valid:
          resident_state = 27
        elif committed_rollovers >= 4 or prepared_valid or rollover_generation <= rollover_generation_fence or next_ordinal <= ordinal or next_kind < 10 or next_kind > 15 or next_token <= 0:
          return codex_v5_reject_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, 80, rollover_generation_fence, qdot_generation, prepared_token)
        else:
          prepared_valid = True
          prepared_generation = rollover_generation
          prepared_ordinal = next_ordinal
          prepared_kind = next_kind
          prepared_token = next_token
          commit_armed = False
          armed_qdot_generation = 0
          rollover_generation_fence = rollover_generation
          last_rollover_command = rollover_command
          last_rollover_generation = rollover_generation
          last_rollover_ordinal = next_ordinal
          last_rollover_kind = next_kind
          last_rollover_token = next_token
          last_rollover_qdot_generation = next_qdot_generation
          resident_state = 27
        end
      elif rollover_command == 3:
        if duplicate_cancel:
          resident_state = 26
        elif not prepared_valid or rollover_generation != prepared_generation:
          return codex_v5_reject_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, 88, rollover_generation_fence, qdot_generation, prepared_token)
        else:
          prepared_valid = False
          prepared_generation = 0
          prepared_ordinal = 0
          prepared_kind = 0
          prepared_token = 0
          prepared_path_qdot = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
          commit_armed = False
          armed_qdot_generation = 0
          last_rollover_command = rollover_command
          last_rollover_generation = rollover_generation
          last_rollover_ordinal = next_ordinal
          last_rollover_kind = next_kind
          last_rollover_token = next_token
          last_rollover_qdot_generation = next_qdot_generation
          resident_state = 26
        end
      elif rollover_command == 2:
        if duplicate_commit_ack and not commit_armed:
          resident_state = 28
        elif duplicate_commit_ack:
          # A repeated in-flight COMMIT is the expected idempotent hold until
          # the exact periodic seam; it does not revoke old-candidate authority.
          resident_state = 27
        elif not prepared_valid or rollover_generation != prepared_generation or next_ordinal != prepared_ordinal or next_kind != prepared_kind or next_token != prepared_token:
          return codex_v5_reject_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, 82, rollover_generation_fence, qdot_generation, prepared_token)
        elif next_qdot_generation != prepared_generation or next_qdot_generation <= qdot_generation:
          return codex_v5_reject_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, 84, rollover_generation_fence, qdot_generation, prepared_token)
        else:
          # COMMIT is validated and latched before the periodic seam.  The old
          # candidate remains the sole qdot authority until the seam tick.
          commit_armed = True
          armed_qdot_generation = next_qdot_generation
          prepared_path_qdot = live_candidate_qdot
          last_rollover_command = rollover_command
          last_rollover_generation = rollover_generation
          last_rollover_ordinal = next_ordinal
          last_rollover_kind = next_kind
          last_rollover_token = next_token
          last_rollover_qdot_generation = next_qdot_generation
          resident_state = 27
        end
      end

      local base_target_n = 5.000000000
      local correction_enabled = path_elapsed_s >= 5.000000000 and path_elapsed_s < tail_end_s
      local formal_metric_enabled = path_elapsed_s >= 5.000000000 and path_elapsed_s < 60.000000000
      local tail_old_candidate_authority = path_elapsed_s >= 60.000000000 and path_elapsed_s < tail_end_s
      if path_elapsed_s < 60.000000000:
        metric_sample_count = metric_sample_count + 1
        if formal_metric_enabled:
          formal_metric_sample_count = formal_metric_sample_count + 1
        end
        if early_end_request > 0 and early_end_request != ordinal:
          censor_sample_count = censor_sample_count
        end
        if correction_enabled:
          correction_generation = correction_generation
        end
      end
      # The current candidate owns every motion tick through the exact 20*pi
      # endpoint, including the bounded interval while COMMIT is in flight.
      active_path_qdot = live_candidate_qdot
      if commit_armed:
        prepared_path_qdot = live_candidate_qdot
      end
      if bounded_motion_dt > 0.0:
        speedj(active_path_qdot, 40.000000000, bounded_motion_dt)
      end
      path_elapsed_s = path_elapsed_s + bounded_motion_dt
      if tail_crossing:
        local hard_safety_gate = motion_guard == 0
        local timing_freshness_gate = packet_reason == 0 and actual_path_dt > 0.0 and actual_path_dt < 0.080000000
        local tube_cbf_gate = codex_r006_qdot_ok()
        local identity_metric_closure_gate = path_elapsed_s == tail_end_s and prepared_valid and commit_armed and prepared_generation == rollover_generation_fence
        local command_envelope_gate = integer_reason == 0 and armed_qdot_generation == prepared_generation and armed_qdot_generation > qdot_generation
        if not prepared_valid and not commit_armed and rollover_command == 0:
          # No prepared successor is the typed, successful end of this chain.
          # The current candidate owns the exact periodic endpoint, then the
          # unchanged mature return-Home primitive closes the artifact.
          return codex_r006_return_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, 0)
        elif committed_rollovers >= 4:
          return codex_v5_reject_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, 87, rollover_generation_fence, qdot_generation, prepared_token)
        elif not identity_metric_closure_gate:
          return codex_v5_reject_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, 82, rollover_generation_fence, qdot_generation, prepared_token)
        elif not command_envelope_gate:
          return codex_v5_reject_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, 84, rollover_generation_fence, qdot_generation, prepared_token)
        elif not codex_v5_switch_gate_families(hard_safety_gate, timing_freshness_gate, tube_cbf_gate, identity_metric_closure_gate, command_envelope_gate):
          return codex_v5_reject_home(home_pose, home_q, epoch, ordinal, token, kind, consumed, runtime_revision, runtime_extension, 85, rollover_generation_fence, qdot_generation, prepared_token)
        else:
          ordinal = prepared_ordinal
          kind = prepared_kind
          token = prepared_token
          qdot_generation = armed_qdot_generation
          codex_v5_active_epoch = epoch
          codex_v5_active_ordinal = ordinal
          codex_v5_active_kind = kind
          codex_v5_active_token = token
          codex_v5_active_qdot_generation = qdot_generation
          active_path_qdot = prepared_path_qdot
          committed_rollovers = committed_rollovers + 1
          prepared_valid = False
          prepared_generation = 0
          prepared_ordinal = 0
          prepared_kind = 0
          prepared_token = 0
          prepared_path_qdot = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
          commit_armed = False
          armed_qdot_generation = 0
          last_rollover_command = rollover_command
          last_rollover_generation = rollover_generation
          last_rollover_ordinal = next_ordinal
          last_rollover_kind = next_kind
          last_rollover_token = next_token
          last_rollover_qdot_generation = next_qdot_generation
          path_elapsed_s = 0.0
          metric_sample_count = 0
          censor_sample_count = 0
          formal_metric_sample_count = 0
          correction_generation = 0
          force_integral_n_s = 0.0
          codex_r006_echo(epoch, ordinal, 28, token, 0, rollover_generation, qdot_generation, 0, runtime_revision, runtime_extension)
        end
      elif resident_state == 28:
        codex_r006_echo(epoch, ordinal, 28, token, 0, last_rollover_generation, qdot_generation, 0, runtime_revision, runtime_extension)
      elif resident_state == 27:
        codex_r006_echo(epoch, ordinal, 27, token, 0, rollover_generation_fence, qdot_generation, prepared_token, runtime_revision, runtime_extension)
      elif resident_state == 26:
        codex_r006_echo(epoch, ordinal, 26, token, 0, rollover_generation_fence, qdot_generation, 0, runtime_revision, runtime_extension)
      elif path_elapsed_s >= 60.000000000:
        codex_r006_echo(epoch, ordinal, 26, token, 0, rollover_generation_fence, qdot_generation, 0, runtime_revision, runtime_extension)
      else:
        codex_r006_echo(epoch, ordinal, 25, token, 0, consumed, kind, 0, runtime_revision, runtime_extension)
      end
    end
    # The timed speed command is the one blocking controller-cycle primitive
    # for this PATH loop.  A second synchronization barrier would halve packet
    # consumption while the host/RTDE path clock continued at 500 Hz.
  end
end
'''


def _v5_main(home_pose: tuple[float, ...]) -> str:
    return f"""def {PROGRAM_NAME}():
  # Script1 owns EOAT setup.  This resident V5 writer performs no EOAT writes.
  local fixed_home_pose = {_urscript_home_pose(home_pose)}
  local locked_home_q = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
  local locked_home_q_valid = False
  local runtime_revision = {V5_RUNTIME_REVISION}
  local runtime_extension = {V5_RUNTIME_EXTENSION}
  local active_epoch = 0
  local last_failed_epoch = 0
  local consumed_session_sequence = read_input_integer_register(28)
  local current_ordinal = 0
  local current_token = 0
  local current_kind = 0
  local state = 78
  local reason = 0
  local return_guard = 0
  local session_active = False
  local completed = False
  while True:
    local packet_reason = codex_r006_packet_observe()
    local integer_reason = codex_r006_integer_wire_guard()
    local session_command = read_input_integer_register(27)
    local session_sequence = read_input_integer_register(28)
    local input_epoch = read_input_integer_register(29)
    local input_ordinal = read_input_integer_register(30)
    local input_kind = read_input_integer_register(31)
    local input_token = read_input_integer_register(32)
    if codex_v5_stop_requested():
      stopl(0.250000000)
      session_active = False
      completed = False
      active_epoch = input_epoch
      state = 90
      reason = 4
      return_guard = 0
    elif integer_reason != 0 and session_command != 0:
      stopl(0.250000000)
      session_active = False
      state = 90
      reason = integer_reason
      return_guard = 0
    elif not session_active and not completed and session_command == 1 and session_sequence > consumed_session_sequence and packet_reason == 0:
      if input_epoch <= last_failed_epoch or input_epoch <= 0 or input_ordinal < 1 or input_ordinal <= current_ordinal or (active_epoch > 0 and input_epoch != active_epoch) or input_kind < 10 or input_kind > 15 or input_token <= 0:
        state = 90
        reason = 61
        last_failed_epoch = input_epoch
      else:
        active_epoch = input_epoch
        current_ordinal = input_ordinal
        current_kind = input_kind
        current_token = input_token
        consumed_session_sequence = session_sequence
        if not codex_r006_entry_home_verified(fixed_home_pose, locked_home_q, locked_home_q_valid):
          stopl(0.250000000)
          session_active = False
          state = 90
          reason = 69
          return_guard = 0
          last_failed_epoch = input_epoch
        else:
          locked_home_q = get_actual_joint_positions()
          locked_home_q_valid = True
          session_active = True
          state = 11
          reason = 0
          return_guard = 0
          codex_r006_echo(active_epoch, current_ordinal, state, current_token, reason, consumed_session_sequence, current_kind, return_guard, runtime_revision, runtime_extension)
          codex_v5_active_epoch = active_epoch
          codex_v5_active_ordinal = current_ordinal
          codex_v5_active_kind = current_kind
          codex_v5_active_token = current_token
          codex_v5_active_qdot_generation = 0
          local chain_ok = codex_r006_execute_chain(fixed_home_pose, locked_home_q, active_epoch, current_ordinal, current_token, current_kind, consumed_session_sequence, runtime_revision, runtime_extension)
          active_epoch = codex_v5_active_epoch
          current_ordinal = codex_v5_active_ordinal
          current_kind = codex_v5_active_kind
          current_token = codex_v5_active_token
          if chain_ok:
            session_active = False
            state = 78
            reason = 0
            return_guard = codex_r006_attempt_guard
          else:
            session_active = False
            state = 90
            reason = codex_r006_attempt_reason
            return_guard = codex_r006_attempt_guard
          end
        end
      end
    elif session_command == 2 and state == 78 and session_sequence > consumed_session_sequence:
      if input_epoch == active_epoch and input_ordinal == current_ordinal and input_token == current_token and input_kind == current_kind:
        consumed_session_sequence = session_sequence
        completed = True
        state = 80
        reason = 0
        return_guard = 123
      else:
        reason = 63
      end
    elif session_command == 1 and (session_sequence <= consumed_session_sequence or input_epoch != active_epoch) and state == 78:
      reason = 62
    end
    codex_r006_echo(active_epoch, current_ordinal, state, current_token, reason, consumed_session_sequence, current_kind, return_guard, runtime_revision, runtime_extension)
    sync()
  end
end

{PROGRAM_NAME}()
"""


def _urp_metadata(urp: bytes) -> tuple[str, str, str]:
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached = ""
    script_path = ""
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "cachedContents":
            cached = html.unescape(node.text or "")
        elif tag == "file" and node.attrib.get("resolves-to") == "file":
            script_path = (node.text or "").strip()
    return str(root.attrib.get("name", "")), cached, script_path


def validate_package(
    script: str,
    txt: str,
    urp: bytes,
    stamp: str | None = None,
    *,
    home_pose: tuple[float, ...] = HOME_POSE,
    home_calibration_receipt_sha256: str | None = None,
    input_sha256: str | None = None,
) -> dict[str, Any]:
    name, cached, script_path = _urp_metadata(urp)
    mature_sha = __import__("hashlib").sha256(MATURE_SOURCE.read_bytes()).hexdigest()
    try:
        resident_block = script.split("def codex_r006_execute_chain", 1)[1].split(
            f"def {PROGRAM_NAME}()", 1
        )[0]
    except IndexError as exc:
        raise Step6FigureEightPackageError("V5 resident execute block is missing") from exc
    try:
        resident_block = resident_block.split(
            "  # One contact acquisition feeds the complete resident chain.",
            1,
        )[1]
    except IndexError as exc:
        raise Step6FigureEightPackageError("V5 resident state block is missing") from exc
    output_writes = [
        int(value)
        for value in re.findall(r"write_output_integer_register\(\s*(\d+)\s*,", script)
    ]
    forbidden_words = ("baseline", "qualification", "3-success", "three-success")
    no_forbidden_words = not any(word in script.lower() for word in forbidden_words)
    cache_equal_complete = all(
        f"codex_r006_cache_i{register}" in script
        for register in V5_INPUT_INTEGER_REGISTERS
    )
    cache_copy_complete = all(
        f"codex_r006_cache_i{register} = read_input_integer_register({register})" in script
        for register in V5_INPUT_INTEGER_REGISTERS
    )
    entry_motion_block = resident_block.split("while not latch_seen", 1)[1].split(
        "while True", 1
    )[0]
    path_motion_block = resident_block.split("while True", 1)[1]

    def _guarded_speedj(block: str) -> bool:
        speed_positions = [match.start() for match in re.finditer(r"speedj\(", block)]
        guard_text = "codex_r006_packet_guard(packet_reason, 60.0, 100.0, 3.0)"
        guard_positions = [
            match.start() for match in re.finditer(re.escape(guard_text), block)
        ]
        if not speed_positions or len(guard_positions) != len(speed_positions):
            return False
        for speed_position, guard_position in zip(speed_positions, guard_positions):
            if guard_position >= speed_position:
                return False
            guard_match = re.search(
                r"local ([A-Za-z0-9_]+_guard) = " + re.escape(guard_text),
                block[guard_position - 40 : speed_position],
            )
            if guard_match is None:
                return False
            guard_name = guard_match.group(1)
            failure_position = block.find(
                f"elif {guard_name} != 0:", guard_position, speed_position
            )
            fault_position = block.find("codex_r006_fault", failure_position, speed_position)
            if failure_position < 0 or fault_position < 0:
                return False
        return True

    switch_gate_source = script.split("def codex_v5_switch_gate_families", 1)[1].split(
        "\ndef codex_r006_echo", 1
    )[0]
    active_speed_position = resident_block.find(
        "speedj(active_path_qdot, 40.000000000, bounded_motion_dt)"
    )
    live_qdot_assignments = [
        match.start()
        for match in re.finditer(r"active_path_qdot = live_candidate_qdot", resident_block)
    ]
    commit_identity_position = resident_block.find("ordinal = prepared_ordinal")
    commit_qdot_position = resident_block.find(
        "active_path_qdot = prepared_path_qdot", commit_identity_position
    )
    command_motion_position = resident_block.find("speedj(active_path_qdot")
    path_clock_position = resident_block.find(
        "path_elapsed_s = path_elapsed_s + bounded_motion_dt"
    )
    checks = {
        "urp_gzip": urp[:2] == b"\x1f\x8b",
        "program_name": name == PROGRAM_NAME,
        "controller_directory": script_path == f"{CONTROLLER_DIR}/{PROGRAM_NAME}.script",
        "cached_contents_exact": cached == script,
        "source_stamp": bool(re.search(r"VERSION:\s+\d{4}-\d{2}-\d{2}T\d{4}HKT", script)) and bool(re.search(r"Version:\s+\d{4}-\d{2}-\d{2}T\d{4}HKT", txt)),
        "minute_hkt_stamp": bool(re.search(r"\d{4}-\d{2}-\d{2}T\d{4}HKT", script)),
        "build_inputs_hash": f"BUILD_INPUTS_SHA256: {input_sha256 or build_input_sha256()}" in script,
        "mature_parent_hash": f"MATURE_PARENT_SCRIPT_SHA256: {mature_sha}" in script,
        "independent_program_identity": (
            f"TP_PROGRAM_ID: {PROGRAM_NAME}" in script
            and f"EXPECTED_PROGRAM: {PROGRAM_NAME}" in script
            and "step5d_strict_rnn_autotune_v4_r013" not in script
        ),
        "layout_607_and_recipe": (
            "WIRE_LAYOUT: layout-607" in script
            and "input_double[24..47]" in script
            and "input_integer[24..39]" in script
            and "output_integer[24..34]" in script
            and "read_input_float_register(47)" in script
            and "layout != 607.0" in script
        ),
        "output_register_range": (
            set(output_writes).issubset(set(V5_OUTPUT_INTEGER_REGISTERS))
            and set(output_writes) == set(V5_OUTPUT_INTEGER_REGISTERS)
            and not re.search(
                r"write_output_integer_register\(\s*(3[5-9]|[4-9][0-9])\s*,",
                script,
            )
        ),
        "no_layout_606": "layout-606" not in script and "606006" not in script,
        "no_v4_baseline_or_qualification": no_forbidden_words,
        "v5_session_identity": all(
            marker in script
            for marker in (
                "input_integer[27]=command",
                "input_integer[28]=sequence",
                "input_integer[29]=epoch",
                "input_integer[30]=ordinal",
                "input_integer[31]=kind10..15",
                "input_integer[32]=token",
            )
        ),
        "v5_integer_packet_cache_equal_copy": (
            cache_equal_complete
            and cache_copy_complete
            and "def codex_r006_packet_payload_equal" in script
            and "def codex_r006_copy_packet" in script
            and "def codex_r006_packet_observe" in script
        ),
        "v5_integer_wire_contract": all(
            marker in script
            for marker in (
                "local rollover_command = read_input_integer_register(33)",
                "local generation = read_input_integer_register(34)",
                "local early_end = read_input_integer_register(35)",
                "local next_ordinal = read_input_integer_register(36)",
                "local next_kind = read_input_integer_register(37)",
                "local next_token = read_input_integer_register(38)",
                "local qdot_generation = read_input_integer_register(39)",
                "next_kind < 10 or next_kind > 15",
            )
        ),
        "path_60s_and_tail": (
            "along=0.04sin(0.1t)" in script
            and "lateral=0.01sin(0.2t)" in script
            and "path_elapsed_s < 60.000000000" in resident_block
                and "tail_crossing = path_elapsed_s + actual_path_dt >= tail_end_s" in resident_block
                and "bounded_motion_dt = tail_end_s - path_elapsed_s" in resident_block
                and "path_elapsed_s = path_elapsed_s + bounded_motion_dt" in resident_block
            and "local tail_end_s = 20.0 * 3.141592653589793" in resident_block
            and "tail_old_candidate_authority" in resident_block
        ),
        "same_tick_latch_path_zero": (
            "latch_seen = True" in resident_block
            and "path_elapsed_s = 0.0" in resident_block
            and "codex_r006_echo(epoch, ordinal, 25" in resident_block
            and "while not latch_seen" in resident_block
        ),
        "entry_target_and_metric_flags": (
            "local base_target_n = 5.000000000" in resident_block
            and "1.000000000 + 2.000000000 * (1.000000000 - cos" not in resident_block
            and "correction_enabled = path_elapsed_s >= 5.000000000" in resident_block
            and "formal_metric_enabled = path_elapsed_s >= 5.000000000" in resident_block
        ),
        "no_60s_boundary_stop_or_home_and_typed_chain_home": (
            "stopj(" not in resident_block
            and "codex_r006_stationary" not in resident_block
            and resident_block.count("return codex_r006_return_home") == 1
            and "if not prepared_valid and not commit_armed and rollover_command == 0:" in resident_block
            and resident_block.find("return codex_r006_return_home")
            > resident_block.find("if tail_crossing:")
        ),
        "figure8_home_exact": (
            script.count(_urscript_home_pose(home_pose)) == 3
            and f"local fixed_home_pose = {_urscript_home_pose(home_pose)}" in script
        ),
        "home_authority": (
            (
                home_calibration_receipt_sha256 is None
                and "STEP6_HOME_CALIBRATION_RECEIPT_SHA256: pending" in script
                and "calibration-only Home; owner admission required" in script
            )
            or (
                home_calibration_receipt_sha256 is not None
                and f"STEP6_HOME_CALIBRATION_RECEIPT_SHA256: {home_calibration_receipt_sha256}" in script
                and "contact-derived final Home" in script
            )
        ),
        "mature_contact_search": (
            "R008 B3 two-stage contact search" in script
            and "speedl([0.0, 0.0, -v_far_m_s" in script
            and "speedl([0.0, 0.0, -v_near_m_s" in script
        ),
        "figure8_contact_v4_equivalent": (
            script.count("local d_near_start_travel_m = 0.011029311") == 1
            and "local force_fuse_n = 50.000000000" in script
            and "local v_far_m_s = 0.005000000" in script
            and "local v_near_m_s = 0.000200000" in script
            and "local far_accel_m_s2 = 0.010000000" in script
            and "local near_accel_m_s2 = 0.005000000" in script
            and "travel >= 0.025000000" in script
            and "contact_elapsed_s >= 90.000000000" in script
            and "STEP6_CONTACT_SEARCH: V4-equivalent" in script
            and "prebrake" not in script.lower()
        ),
        "mature_host_speedj_path": (
                "speedj(active_path_qdot, 40.000000000, bounded_motion_dt)" in resident_block
            and "speedl(" not in resident_block
        ),
        "active_tail_qdot_authority": (
            active_speed_position >= 0
            and "local active_path_qdot = [read_input_float_register(37)" in resident_block
            and "local live_candidate_qdot = [read_input_float_register(37)" in resident_block
            and len(live_qdot_assignments) == 1
            and commit_identity_position >= 0
            and commit_qdot_position > commit_identity_position
            and active_speed_position < commit_qdot_position
            and "active_path_qdot = live_candidate_qdot\n      if commit_armed:\n        prepared_path_qdot = live_candidate_qdot" in resident_block
            and "if not tail_crossing and not commit_armed:" not in resident_block
            and "local path_qdot = [read_input_float_register" not in resident_block
        ),
        "common_motion_after_rollover_ack": (
            command_motion_position >= 0
            and path_clock_position > command_motion_position
            and resident_block.find("if duplicate_prepare and prepared_valid") < command_motion_position
            and resident_block.find("if duplicate_cancel") < command_motion_position
            and resident_block.find("if duplicate_commit_ack") < command_motion_position
            and resident_block.find("elif rollover_command == 0:") < command_motion_position
            and "elif rollover_command == 2:\n        if duplicate_commit_ack and not commit_armed:" in resident_block
            and "commit_armed = True" in resident_block
            and "elif path_elapsed_s >= tail_end_s:" not in resident_block
        ),
        "single_controller_cycle_path_clock": (
            "speedj(active_path_qdot, 40.000000000, bounded_motion_dt)"
            in path_motion_block
            and "sync()" not in path_motion_block
        ),
        "contiguous_rollover_idempotence": (
            "elif rollover_command == 0:" in resident_block
            and "last_rollover_command = 0" in resident_block
            and "last_rollover_generation = 0" in resident_block
            and "rollover_generation_fence" in resident_block
        ),
        "mature_packet_force_guards": (
                "codex_r006_packet_observe()" in resident_block
                and "codex_r006_packet_guard(packet_reason, 60.0, 100.0, 3.0)" in script
                and "codex_r006_qdot_ok" in script
                and _guarded_speedj(entry_motion_block)
                and _guarded_speedj(path_motion_block)
        ),
        "resident_home_loop": (
            "READY_HOME_NEXT=78" in script
            and "state = 78" in script
            and "codex_r006_return_home" in script
        ),
        "persistent_ready_home_return_guard": (
            "if chain_ok:\n"
            "            session_active = False\n"
            "            state = 78\n"
            "            reason = 0\n"
            "            return_guard = codex_r006_attempt_guard\n"
            "          else:" in script
        ),
        "host_figure8_contract": (
            "FigureEightPathProviderV1" in script
            and V5_METRIC_NAME in script
                and "filtered_normal_n" in script
                and "raw signed normal/force/torque are hard-safety/evidence channels only" in script
                and "raw measured force" not in script
                and "raw_measured_force_is_metric_input" not in script
                and "mean(raw_measured_normal_force" not in script
            and "evidence=[0,60) 600 bins" in txt
            and "formal=[5,60) 550 bins" in txt
            and "tail excluded from MAE/censor/GP/tell_exact/exact-novel budget" in txt
        ),
        "v5_switch_gate_inputs": (
            "codex_v5_switch_gate_families(hard_safety_gate, timing_freshness_gate, tube_cbf_gate, identity_metric_closure_gate, command_envelope_gate)" in resident_block
            and "= True" not in switch_gate_source
            and "local hard_safety_gate = motion_guard == 0" in resident_block
            and "local timing_freshness_gate = packet_reason == 0" in resident_block
            and "local tube_cbf_gate = codex_r006_qdot_ok()" in resident_block
            and "local identity_metric_closure_gate = path_elapsed_s == tail_end_s" in resident_block
            and "local command_envelope_gate = integer_reason == 0" in resident_block
        ),
        "v5_active_identity_propagation": all(
            marker in script
            for marker in (
                "global codex_v5_active_ordinal = 0",
                "codex_v5_active_ordinal = ordinal",
                    "ordinal = prepared_ordinal",
                    "codex_v5_active_ordinal = ordinal",
                "local chain_ok = codex_r006_execute_chain",
                "current_ordinal = codex_v5_active_ordinal",
                "current_kind = codex_v5_active_kind",
                "current_token = codex_v5_active_token",
            )
        ),
        "main_pre_call_identity_initialization": (
            script.find("codex_v5_active_epoch = active_epoch") >= 0
            and script.find("codex_v5_active_epoch = active_epoch")
            < script.find("local chain_ok = codex_r006_execute_chain")
            and script.find("codex_v5_active_token = current_token")
            < script.find("local chain_ok = codex_r006_execute_chain")
        ),
        "v5_persistent_rollover_ack": all(
            marker in resident_block
            for marker in (
                "last_rollover_command",
                "duplicate_prepare",
                "duplicate_cancel",
                "duplicate_commit",
                "duplicate_commit_ack",
                "last_rollover_qdot_generation",
                "codex_r006_echo(epoch, ordinal, 28, token, 0, last_rollover_generation",
                "codex_r006_echo(epoch, ordinal, 26, token, 0, rollover_generation_fence, qdot_generation, 0",
            )
        ),
        "v5_rollover_state_machine": all(
            marker in resident_block
            for marker in (
                "prepared_valid",
                "rollover_generation_fence",
                "rollover_command == 1",
                "rollover_command == 3",
                "rollover_command == 2",
                "commit_armed = True",
                "prepared_path_qdot = live_candidate_qdot",
                "active_path_qdot = prepared_path_qdot",
                "committed_rollovers >= 4",
                "codex_v5_switch_gate_families",
                "codex_r006_echo(epoch, ordinal, 27",
                "codex_r006_echo(epoch, ordinal, 28",
                "codex_v5_reject_home",
            )
        ),
        "v5_rejected_overlay": "codex_r006_echo(epoch, ordinal, 29" in script,
        "early_censor_and_stop_dominance": (
            "early_end_request == ordinal" in resident_block
            and "early_end_request != ordinal" in resident_block
            and "codex_v5_stop_requested()" in resident_block
            and "codex_r006_fault(epoch, ordinal, token, kind, consumed, 4" in resident_block
        ),
        "mature_fixed_home_and_return": (
            "codex_r006_entry_home_verified" in script
            and "codex_r006_return_home" in script
            and "READY_HOME_NEXT=78" in script
            and "state = 78" in script
        ),
        "no_write_surfaces": "zero_ftsensor(" not in script and "set_tcp(" not in script and "set_payload(" not in script,
        "resident_chain_identity": (
            "V5_ROLLOVER: typed two-slot PREPARE/CANCEL/COMMIT" in script
            and "exactly four committed rollovers" in script
        ),
    }
    if stamp is not None:
        checks["requested_stamp_matches"] = stamp in script and stamp in txt
    _balance(script)
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise Step6FigureEightPackageError(f"Step6 Figure-eight package validation failed: {failed}")
    checks["deployable"] = True
    checks["package_status"] = "offline_validated_mature_seam_not_live_accepted"
    checks["live_acceptance"] = False
    return checks


def write_outputs(
    *,
    stamp: str | None = None,
    output_dir: Path = LOCAL_DIR,
    home_calibration_receipt: Path | None = None,
) -> dict[str, Any]:
    home_pose, home_receipt_sha = _materialized_home(home_calibration_receipt)
    sanity = numeric_sanity(
        home_pose=home_pose,
        home_calibration_receipt_sha256=home_receipt_sha,
    )
    selected_stamp = stamp or source_stamp()
    input_sha = build_input_sha256(
        home_calibration_receipt=home_calibration_receipt
    )
    script = build_script(
        selected_stamp,
        sanity,
        input_sha256=input_sha,
        home_pose=home_pose,
        home_calibration_receipt_sha256=home_receipt_sha,
    )
    txt = build_txt(
        selected_stamp,
        sanity,
        input_sha256=input_sha,
        home_pose=home_pose,
        home_calibration_receipt_sha256=home_receipt_sha,
    )
    urp = build_urp(script, PROGRAM_NAME, CONTROLLER_DIR)
    checks = validate_package(
        script,
        txt,
        urp,
        selected_stamp,
        home_pose=home_pose,
        home_calibration_receipt_sha256=home_receipt_sha,
        input_sha256=input_sha,
    )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "script": output_dir / f"{PROGRAM_NAME}.script",
        "txt": output_dir / f"{PROGRAM_NAME}.txt",
        "urp": output_dir / f"{PROGRAM_NAME}.urp",
    }
    paths["script"].write_text(script, encoding="utf-8")
    paths["txt"].write_text(txt, encoding="utf-8")
    paths["urp"].write_bytes(urp)
    manifest_path = output_dir / f"{PROGRAM_NAME}.deploy-manifest.json"
    manifest = {
        "schema_version": 1,
        "basename": PROGRAM_NAME,
        "controller_directory": CONTROLLER_DIR,
        "artifacts": [
            {
                "filename": f"{PROGRAM_NAME}.{suffix}",
                "source": f"{PROGRAM_NAME}.{suffix}",
                "sha256": __import__("hashlib").sha256(
                    paths[suffix].read_bytes()
                ).hexdigest(),
            }
            for suffix in ("urp", "txt", "script")
        ],
    }
    manifest_part = manifest_path.with_name(manifest_path.name + ".part")
    manifest_part.write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    manifest_part.replace(manifest_path)
    paths["deploy_manifest"] = manifest_path
    NUMERIC_SANITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    NUMERIC_SANITY_PATH.write_text(json.dumps(sanity, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "program": PROGRAM_NAME,
        "stamp": selected_stamp,
        "build_inputs_sha256": input_sha,
        "paths": {key: str(value) for key, value in paths.items()},
        "numeric_sanity": str(NUMERIC_SANITY_PATH),
        "controller_target": f"{CONTROLLER_DIR}/{PROGRAM_NAME}.urp",
        "home_pose": list(home_pose),
        "home_status": (
            "contact_derived_final_home"
            if home_receipt_sha is not None
            else "calibration_only_pending_contact_confirmation"
        ),
        "home_calibration_receipt_sha256": home_receipt_sha,
        "validation": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stamp", default=None)
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
