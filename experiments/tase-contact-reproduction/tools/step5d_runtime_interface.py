#!/usr/bin/env python3
"""Step5d live-prep runtime interface defaults and read-only status helpers."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from tase_protocol_table import ProtocolTableError, resolve_experiment_profile


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
CURRENT_STAGE_PATH = EXPERIMENT_ROOT / "config" / "current_stage.json"
RUN_ROOT = EXPERIMENT_ROOT / "runs"
DEFAULT_LONG_CHECK_TTL_S = 7200.0
DEFAULT_ROBOT_HOST = "192.168.1.18"
DEFAULT_LONG_CHECK_CACHE = RUN_ROOT / ".bridge_long_checks_cache.json"

STEP5D_INTERFACE_CLASS = "tp_speedj_strict_rnn_liveprep_v1"
STEP5D_TUNING_BUNDLE = "v24_startup_quarantine_rnn_tracking_guard"
STEP5D_LIVEPREP_V20_STAGE_ID = "step5d_strict_rnn_liveprep_v20"
STEP5D_LIVEPREP_V21_STAGE_ID = "step5d_strict_rnn_liveprep_v21"
STEP5D_LIVEPREP_V22_STAGE_ID = "step5d_strict_rnn_liveprep_v22"
STEP5D_LIVEPREP_V23_STAGE_ID = "step5d_strict_rnn_liveprep_v23"
STEP5D_LIVEPREP_V24_STAGE_ID = "step5d_strict_rnn_liveprep_v24"
STEP5D_ABLATION_V25_STAGE_ID = "step5d_strict_rnn_ablation_v25"
STEP5D_ABLATION_V26_STAGE_ID = "step5d_strict_rnn_ablation_v26"
STEP5D_ABLATION_V27_STAGE_ID = "step5d_strict_rnn_ablation_v27"
STEP5D_ABLATION_V28_STAGE_ID = "step5d_strict_rnn_ablation_v28"
STEP5D_ABLATION_V29_STAGE_ID = "step5d_strict_rnn_ablation_v29"
STEP5D_ABLATION_V30_STAGE_ID = "step5d_strict_rnn_ablation_v30"
STEP5D_NO_CONTACT_P0_V7_STAGE_ID = "step5d_strict_rnn_no_contact_p0_v7"
STEP5D_NO_CONTACT_P0_V8_STAGE_ID = "step5d_strict_rnn_no_contact_p0_v8"
# Compatibility name for the immutable v7 evidence path.  New work must use
# STEP5D_NO_CONTACT_P0_V8_STAGE_ID explicitly so historical v7 evidence is not
# silently reinterpreted under the v30 control contract.
STEP5D_NO_CONTACT_P0_STAGE_ID = STEP5D_NO_CONTACT_P0_V7_STAGE_ID
STEP5D_NO_CONTACT_P0_STAGE_IDS = (
    STEP5D_NO_CONTACT_P0_V7_STAGE_ID,
    STEP5D_NO_CONTACT_P0_V8_STAGE_ID,
)
STEP5D_V30_CONTROL_CONTRACT_STAGE_IDS = (
    STEP5D_ABLATION_V30_STAGE_ID,
    STEP5D_NO_CONTACT_P0_V8_STAGE_ID,
)
STEP5D_ABLATION_STAGE_IDS = (
    STEP5D_ABLATION_V25_STAGE_ID,
    STEP5D_ABLATION_V26_STAGE_ID,
    STEP5D_ABLATION_V27_STAGE_ID,
    STEP5D_ABLATION_V28_STAGE_ID,
    STEP5D_ABLATION_V29_STAGE_ID,
    STEP5D_ABLATION_V30_STAGE_ID,
    *STEP5D_NO_CONTACT_P0_STAGE_IDS,
)
STEP5D_STAGE25_CONTROL_MODES = ("speedl_cartesian_oracle", "speedj_dls_oracle", "speedj_rnn_live")
STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE = 523.0
STEP5D_STAGE25_JOINT_LAYOUT_CODE = 524.0
STEP5D_LINE_ENTRY_PARAM_VALID_CODE = 521.0
STEP5D_QDOT_CLEAR_STAGE = 25.95
STEP5D_QDOT_CLEAR_ACK_CYCLES = 3
STEP5D_QDOT_CLEAR_ZERO_TOL_RAD_S = 0.0005
STEP5D_V27_STEP5B_ENVELOPE_NORMAL_GUARD_N = 50.0
STEP5D_V27_STEP5B_ENVELOPE_FORCE_GUARD_N = 60.0
STEP5D_V27_STEP5B_ENVELOPE_TORQUE_GUARD_NM = 3.0
STEP5D_V27_SPEEDL_LIVE_CONTROL_SOURCE = "step5b_speedl_live_step5d_shadow"
STEP5D_STAGE25_V27_FIX_VALIDATION_TARGET_S = 10.0
STEP5D_STAGE25_V28_FULL_RUN_TARGET_S = 60.0
STEP5D_STAGE25_V27_RUNTIME_LIMIT_S = 15.0
STEP5D_STAGE25_V28_RUNTIME_LIMIT_S = 65.0


class StageEnvError(RuntimeError):
    pass


def _stage_finite_float(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise StageEnvError(f"{label} must be a finite float: {value!r}") from exc
    if not math.isfinite(parsed):
        raise StageEnvError(f"{label} must be finite: {value!r}")
    return parsed


def _load_step5_stage_table(root: Path = EXPERIMENT_ROOT) -> dict[str, Any]:
    return json.loads((root / "config" / "step5_stage_table.json").read_text(encoding="utf-8"))


def _stage_row(stage_id: str, root: Path = EXPERIMENT_ROOT) -> dict[str, Any]:
    table = _load_step5_stage_table(root)
    row = next((row for row in table.get("stages", []) if row.get("id") == stage_id), None)
    if row is None:
        raise StageEnvError(f"missing stage table row: {stage_id}")
    return row


def _stage_field(row: dict[str, Any], dotted: str) -> Any:
    value: Any = row
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            raise StageEnvError(f"missing stage table field: {dotted}")
        value = value[part]
    if value is None:
        raise StageEnvError(f"stage table field is null: {dotted}")
    return value


def is_no_contact_p0_stage(program: str) -> bool:
    return program in STEP5D_NO_CONTACT_P0_STAGE_IDS


def uses_v30_control_contract(program: str) -> bool:
    return program in STEP5D_V30_CONTROL_CONTRACT_STAGE_IDS


def _fmt_int(value: Any) -> str:
    parsed = _stage_finite_float(value, "stage env integer")
    if not parsed.is_integer():
        raise StageEnvError(f"expected integer-valued stage env value: {value!r}")
    return str(int(parsed))


def _fmt_float(value: Any, places: int | None = None) -> str:
    parsed = _stage_finite_float(value, "stage env float")
    if places is None:
        return str(parsed)
    return f"{parsed:.{places}f}"


def _fmt_text(value: Any) -> str:
    text = str(value)
    if not text or any(ch.isspace() for ch in text) or any(ch in "'\"`$\\;" for ch in text):
        raise StageEnvError(f"unsafe shell env value: {text!r}")
    return text


_STAGE_ENV_MAP: tuple[tuple[str, str, object], ...] = (
    ("BRIDGE_PROFILE", "id", _fmt_text),
    ("BRIDGE_DURATION_S", "bridge_runtime.duration_s", _fmt_int),
    ("BRIDGE_BASELINE_S", "operator_lifecycle.baseline_s", _fmt_int),
    ("BRIDGE_REZERO_S", "operator_lifecycle.rezero_s", lambda value: _fmt_float(value, 2)),
    ("BRIDGE_RTDE_HZ", "bridge_runtime.rtde_hz", _fmt_int),
    ("BRIDGE_SENSOR_STALE_S", "bridge_runtime.sensor_stale_s", lambda value: _fmt_float(value, 2)),
    ("BRIDGE_SOCKET_TIMEOUT_S", "bridge_runtime.socket_timeout_s", lambda value: _fmt_float(value, 1)),
    ("BRIDGE_TARGET_FORCE_N", "guard.target_force_n", lambda value: _fmt_float(value, 1)),
    ("BRIDGE_FORCE_P_GAIN", "bridge_runtime.force_p_gain", lambda value: _fmt_float(value, 3)),
    ("BRIDGE_FORCE_I_GAIN", "bridge_runtime.force_i_gain", lambda value: _fmt_float(value, 5)),
    ("BRIDGE_FORCE_DAMPING", "bridge_runtime.force_damping", lambda value: _fmt_float(value, 1)),
    ("BRIDGE_INTEGRAL_LIMIT_N_S", "bridge_runtime.integral_limit_n_s", lambda value: _fmt_float(value, 1)),
    ("MAX_NORMAL_FORCE_N", "guard.raw_normal_guard_n", _fmt_int),
    ("MAX_FORCE_NORM_N", "guard.force_norm_guard_n", _fmt_int),
    ("MAX_TORQUE_NORM_NM", "guard.torque_norm_guard_nm", lambda value: _fmt_float(value, 1)),
    ("BRIDGE_NORMAL_FOLLOW_MODE", "bridge_runtime.normal_follow_mode", _fmt_text),
    ("BRIDGE_NORMAL_FILTER_ALPHA", "guard.normal_load_filter_alpha", lambda value: _fmt_float(value, 2)),
    ("BRIDGE_NORMAL_MIN_FORCE_N", "bridge_runtime.normal_min_force_n", lambda value: _fmt_float(value, 3)),
    ("BRIDGE_MOTION_LIMIT_M_S", "guard.path_cap_m_s", lambda value: _fmt_float(value, 3)),
    ("BRIDGE_TOTAL_LINEAR_LIMIT_M_S", "guard.total_linear_cap_m_s", lambda value: _fmt_float(value, 3)),
    ("BRIDGE_NORMAL_VELOCITY_LIMIT_M_S", "guard.normal_velocity_cap_m_s", lambda value: _fmt_float(value, 3)),
    ("BRIDGE_ANGULAR_LIMIT_RAD_S", "guard.attitude_cap_rad_s", lambda value: _fmt_float(value, 3)),
    ("STEP5D_STAGE25_CONTROL_MODE", "guard.stage25_default_control_mode", _fmt_text),
    ("STEP5D_QDOT_LIMIT_RAD_S", "guard.qdot_cap_rad_s", lambda value: _fmt_float(value, 3)),
    ("STEP5D_QDOT_SLEW_RAD_S2", "guard.qdot_slew_rad_s2", lambda value: _fmt_float(value, 3)),
    ("STEP5D_EPSILON", "guard.step5d_epsilon", lambda value: _fmt_float(value, 3)),
    ("STEP5D_SIGR_EXPONENT_R", "guard.step5d_sigr_exponent_r", lambda value: _fmt_float(value, 3)),
    ("STEP5D_RNN_INNER_ITERATIONS", "guard.step5d_rnn_inner_iterations", _fmt_int),
    ("STEP5D_RNN_BACKEND", "guard.step5d_rnn_backend", _fmt_text),
    ("STEP5D_PRELOAD_FILTERED_MIN_N", "guard.line_entry_normal_load_min_n", lambda value: _fmt_float(value, 1)),
    ("STEP5D_PRELOAD_FILTERED_MAX_N", "guard.line_entry_normal_load_max_n", lambda value: _fmt_float(value, 1)),
    ("STEP5D_PRELOAD_RAW_MIN_N", "guard.line_entry_normal_load_min_n", lambda value: _fmt_float(value, 1)),
    ("STEP5D_PRELOAD_RAW_MAX_N", "guard.line_entry_normal_load_max_n", lambda value: _fmt_float(value, 1)),
    ("STEP5D_PRELOAD_FORCE_NORM_MAX_N", "guard.force_norm_guard_n", lambda value: _fmt_float(value, 1)),
    ("STEP5D_PRELOAD_HOLD_S", "guard.line_entry_required_s", lambda value: _fmt_float(value, 1)),
    ("STEP5D_PRELOAD_TIMEOUT_S", "guard.line_entry_timeout_s", lambda value: _fmt_float(value, 1)),
)


def build_stage_env(stage_id: str, root: Path = EXPERIMENT_ROOT) -> dict[str, str]:
    row = _stage_row(stage_id, root)
    env: dict[str, str] = {}
    for env_name, dotted, formatter in _STAGE_ENV_MAP:
        env[env_name] = formatter(_stage_field(row, dotted))  # type: ignore[operator]
    return env


_P0_ENV = build_stage_env(STEP5D_NO_CONTACT_P0_STAGE_ID)
STEP5D_NO_CONTACT_P0_DURATION_S = float(_P0_ENV["BRIDGE_DURATION_S"])
STEP5D_NO_CONTACT_P0_TARGET_FORCE_N = float(_P0_ENV["BRIDGE_TARGET_FORCE_N"])
STEP5D_NO_CONTACT_P0_BASELINE_S = float(_P0_ENV["BRIDGE_BASELINE_S"])
STEP5D_NO_CONTACT_P0_REZERO_S = float(_P0_ENV["BRIDGE_REZERO_S"])
STEP5D_NO_CONTACT_P0_FORCE_P_GAIN = float(_P0_ENV["BRIDGE_FORCE_P_GAIN"])
STEP5D_NO_CONTACT_P0_FORCE_I_GAIN = float(_P0_ENV["BRIDGE_FORCE_I_GAIN"])
STEP5D_NO_CONTACT_P0_FORCE_DAMPING = float(_P0_ENV["BRIDGE_FORCE_DAMPING"])
STEP5D_NO_CONTACT_P0_INTEGRAL_LIMIT_N_S = float(_P0_ENV["BRIDGE_INTEGRAL_LIMIT_N_S"])
STEP5D_NO_CONTACT_P0_NORMAL_GUARD_N = float(_P0_ENV["MAX_NORMAL_FORCE_N"])
STEP5D_NO_CONTACT_P0_FORCE_GUARD_N = float(_P0_ENV["MAX_FORCE_NORM_N"])
STEP5D_NO_CONTACT_P0_TORQUE_GUARD_NM = float(_P0_ENV["MAX_TORQUE_NORM_NM"])
STEP5D_NO_CONTACT_P0_MOTION_LIMIT_M_S = float(_P0_ENV["BRIDGE_MOTION_LIMIT_M_S"])
STEP5D_NO_CONTACT_P0_TOTAL_LINEAR_LIMIT_M_S = float(_P0_ENV["BRIDGE_TOTAL_LINEAR_LIMIT_M_S"])
STEP5D_NO_CONTACT_P0_NORMAL_VELOCITY_LIMIT_M_S = float(_P0_ENV["BRIDGE_NORMAL_VELOCITY_LIMIT_M_S"])
STEP5D_NO_CONTACT_P0_NORMAL_FILTER_ALPHA = float(_P0_ENV["BRIDGE_NORMAL_FILTER_ALPHA"])
STEP5D_NO_CONTACT_P0_ANGULAR_LIMIT_RAD_S = float(_P0_ENV["BRIDGE_ANGULAR_LIMIT_RAD_S"])
STEP5D_NO_CONTACT_P0_QDOT_CAP_RAD_S = float(_P0_ENV["STEP5D_QDOT_LIMIT_RAD_S"])
STEP5D_NO_CONTACT_P0_QDOT_SLEW_RAD_S2 = float(_P0_ENV["STEP5D_QDOT_SLEW_RAD_S2"])
STEP5D_NO_CONTACT_P0_EPSILON = float(_P0_ENV["STEP5D_EPSILON"])
STEP5D_NO_CONTACT_P0_SIGR_EXPONENT_R = float(_P0_ENV["STEP5D_SIGR_EXPONENT_R"])
STEP5D_NO_CONTACT_P0_RNN_INNER_ITERATIONS = int(_P0_ENV["STEP5D_RNN_INNER_ITERATIONS"])
STEP5D_NO_CONTACT_P0_RNN_BACKEND = _P0_ENV["STEP5D_RNN_BACKEND"]
STEP5D_NO_CONTACT_P0_NORMAL_MIN_FORCE_N = float(_P0_ENV["BRIDGE_NORMAL_MIN_FORCE_N"])
STEP5D_NO_CONTACT_P0_PRELOAD_TIMEOUT_S = float(_P0_ENV["STEP5D_PRELOAD_TIMEOUT_S"])
STEP5D_NO_CONTACT_P0_RTDE_HZ = float(_P0_ENV["BRIDGE_RTDE_HZ"])
STEP5D_NO_CONTACT_P0_SENSOR_STALE_S = float(_P0_ENV["BRIDGE_SENSOR_STALE_S"])
STEP5D_NO_CONTACT_P0_SOCKET_TIMEOUT_S = float(_P0_ENV["BRIDGE_SOCKET_TIMEOUT_S"])

STEP5D_PROTOCOL_FALLBACK_PROFILE = {
    "parameters": {
        "bridge_duration_s": 180.0,
        "target_force_n": 12.0,
        "force_p_gain": 0.001,
        "force_i_gain": 0.00001,
        "force_damping": 7.0,
        "integral_limit_n_s": 1.0,
        "normal_filter_alpha": 0.55,
        "normal_filter_min_force_n": 2.0,
        "zero_hold_s": 0.25,
    },
    "safety_limits": {
        "normal_velocity_limit_m_s": 0.01,
        "total_linear_limit_m_s": 0.004,
        "angular_limit_rad_s": 0.015,
    },
}


@dataclass(frozen=True)
class Step5dPreloadGate:
    filtered_min_n: float
    filtered_max_n: float
    raw_min_n: float
    raw_max_n: float
    force_norm_max_n: float
    hold_s: float
    timeout_s: float = 10.0
    cmd_limit_m_s: float = 0.003
    recovery_normal_load_min_n: float = 0.0
    recovery_normal_load_max_n: float = 40.0
    force_norm_stop_n: float = 100.0


@dataclass(frozen=True)
class Step5dBridgeDefaults:
    duration_s: float
    target_force_n: float
    force_p_gain: float
    force_i_gain: float
    force_damping: float
    integral_limit_n_s: float
    normal_velocity_limit_m_s: float
    normal_filter_alpha: float
    normal_min_force_n: float
    total_linear_limit_m_s: float
    angular_limit_rad_s: float
    max_normal_force_n: float
    max_force_norm_n: float
    max_torque_norm_nm: float
    baseline_s: float
    rezero_s: float
    rtde_hz: float
    sensor_stale_s: float
    socket_timeout_s: float


@dataclass(frozen=True)
class Step5dRuntimeInterface:
    interface_class: str
    tuning_bundle: str
    program: str
    controller_target: str
    controller_dir: str
    preload_gate: Step5dPreloadGate
    bridge_defaults: Step5dBridgeDefaults
    stage25_control_mode: str
    line_entry_param_valid_code: float
    register_contract: dict[str, str]
    hard_contract: dict[str, Any]


def _finite_float(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite float: {value!r}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite: {value!r}")
    return parsed


def env_float(
    env: Mapping[str, str],
    primary: str,
    default: float,
    *,
    legacy: str | None = None,
) -> float:
    if primary in env and env[primary] != "":
        return _finite_float(env[primary], primary)
    if legacy and legacy in env and env[legacy] != "":
        return _finite_float(env[legacy], legacy)
    return float(default)


def _current_stage(path: Path = CURRENT_STAGE_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def current_step5d_program(path: Path = CURRENT_STAGE_PATH) -> str:
    current = _current_stage(path)
    program = str(current.get("program") or current.get("current_stage_id") or "")
    if program.startswith(("step5d_strict_rnn_liveprep_", "step5d_strict_rnn_ablation_", "step5d_strict_rnn_no_contact_p0_")):
        return program
    return STEP5D_LIVEPREP_V20_STAGE_ID


def controller_target_for(
    program: str,
    current: dict[str, Any] | None = None,
    root: Path = EXPERIMENT_ROOT,
) -> str:
    payload = current if current is not None else _current_stage()
    if payload.get("program") == program and payload.get("controller_target"):
        return str(payload["controller_target"])
    try:
        row_target = (_stage_row(program, root).get("package_delivery") or {}).get(
            "controller_target"
        )
    except StageEnvError:
        row_target = None
    if isinstance(row_target, str) and row_target:
        return row_target
    if program in {STEP5D_ABLATION_V30_STAGE_ID, STEP5D_NO_CONTACT_P0_V8_STAGE_ID}:
        return "LOCAL_ONLY_NOT_DELIVERED"
    if is_no_contact_p0_stage(program):
        return f"/programs/andyl/kunwei/step5/{program}.urp"
    return f"/programs/andyl/kunwei/step5/{program}.urp"


def uses_step5b_speedl_live_source(program: str) -> bool:
    return program in {
        STEP5D_ABLATION_V27_STAGE_ID,
        STEP5D_ABLATION_V28_STAGE_ID,
        STEP5D_ABLATION_V29_STAGE_ID,
        STEP5D_ABLATION_V30_STAGE_ID,
    }


def speedl_orientation_policy(program: str) -> str | None:
    if program == STEP5D_ABLATION_V28_STAGE_ID:
        return "step5b_orientation_follow_live_step5d_shadow"
    if program == STEP5D_ABLATION_V27_STAGE_ID:
        return "shadow_only_full_stage25"
    return None


def stage25_0_register_contract(program: str) -> str:
    if program == STEP5D_NO_CONTACT_P0_V8_STAGE_ID:
        return (
            "no-contact P0 v8: Stage25.95 first requires bridge-cleared 37..47, then "
            f"Stage25.0 accepts only 47={STEP5D_STAGE25_JOINT_LAYOUT_CODE:g} joint qd0..qd5 for TP speedj; "
            "strict-RNN is the only command source and DLS is shadow-only"
        )
    if program == STEP5D_NO_CONTACT_P0_V7_STAGE_ID:
        return (
            "no-contact P0: Stage25.95 first requires bridge-cleared 37..47, then "
            f"Stage25.0 accepts 47={STEP5D_STAGE25_JOINT_LAYOUT_CODE:g} joint qd0..qd5 for TP speedj "
            f"or 47={STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE:g} Cartesian speedl for diagnostics; "
            "43 cmd_valid; 44 path_time; 45 force_error; 46 pose/orientation_error."
        )
    prefix = (
        "v25/v26/v27/v28/v29/v30: 37..42 cartesian vx/vy/vz/wx/wy/wz when "
        f"47={STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE:g}; "
    )
    suffix = (
        "37..42 joint qd0..qd5 rad/s when "
        f"47={STEP5D_STAGE25_JOINT_LAYOUT_CODE:g}; "
        "43 cmd_valid; 44 path_time; 45 force_error; 46 pose/orientation_error. "
        "v24 and older: 37..42 qd0..qd5 rad/s; 47 solver_status."
    )
    if program == STEP5D_ABLATION_V28_STAGE_ID:
        return (
            prefix
            + "v28 speedl_cartesian_oracle bridge runtime uses "
            + f"{STEP5D_V27_SPEEDL_LIVE_CONTROL_SOURCE}: Step5b speedl live vx/vy/vz plus "
            + "Step5b/step4e orientation follow wx/wy/wz; Step5d paper/RNN outputs are logged as shadow diagnostics; "
            + suffix
        )
    if program in {STEP5D_ABLATION_V29_STAGE_ID, STEP5D_ABLATION_V30_STAGE_ID}:
        route = "strict RNN live speedj" if program == STEP5D_ABLATION_V29_STAGE_ID else "strict RNN offline-candidate speedj"
        alternatives = (
            "speedl_cartesian_oracle and speedj_dls_oracle are offline shadow/diagnostic only and forbidden as runtime fallback; "
            if program == STEP5D_ABLATION_V30_STAGE_ID
            else "speedl_cartesian_oracle and speedj_dls_oracle remain explicit debug/fallback modes; "
        )
        return (
            prefix
            + f"{program.rsplit('_', 1)[-1]} defaults to {route} on layout 524 after the v28 contact envelope; "
            + alternatives
            + suffix
        )
    if program == STEP5D_ABLATION_V27_STAGE_ID:
        return (
            prefix
            + "v27 speedl_cartesian_oracle bridge runtime uses "
            + f"{STEP5D_V27_SPEEDL_LIVE_CONTROL_SOURCE}: Step5b speedl live vx/vy/vz, "
            + "wx/wy/wz forced to 0 for all Stage25.0, and Step5d paper/RNN outputs logged as shadow diagnostics; "
            + suffix
        )
    return prefix + suffix


def stage25_success_target_s(program: str) -> float | None:
    if is_no_contact_p0_stage(program):
        return STEP5D_STAGE25_V28_FULL_RUN_TARGET_S
    if program in {STEP5D_ABLATION_V28_STAGE_ID, STEP5D_ABLATION_V29_STAGE_ID, STEP5D_ABLATION_V30_STAGE_ID}:
        return STEP5D_STAGE25_V28_FULL_RUN_TARGET_S
    if program == STEP5D_ABLATION_V27_STAGE_ID:
        return STEP5D_STAGE25_V27_FIX_VALIDATION_TARGET_S
    return None


def stage25_runtime_limit_s(program: str) -> float | None:
    if is_no_contact_p0_stage(program):
        return STEP5D_STAGE25_V28_RUNTIME_LIMIT_S
    if program in {STEP5D_ABLATION_V28_STAGE_ID, STEP5D_ABLATION_V29_STAGE_ID, STEP5D_ABLATION_V30_STAGE_ID}:
        return STEP5D_STAGE25_V28_RUNTIME_LIMIT_S
    if program == STEP5D_ABLATION_V27_STAGE_ID:
        return STEP5D_STAGE25_V27_RUNTIME_LIMIT_S
    return None


def default_preload_gate(program: str) -> Step5dPreloadGate:
    if is_no_contact_p0_stage(program):
        return Step5dPreloadGate(
            filtered_min_n=0.0,
            filtered_max_n=2.0,
            raw_min_n=0.0,
            raw_max_n=2.0,
            force_norm_max_n=5.0,
            hold_s=0.0,
            timeout_s=1.0,
            recovery_normal_load_min_n=0.0,
            recovery_normal_load_max_n=2.0,
            force_norm_stop_n=5.0,
        )
    if uses_step5b_speedl_live_source(program):
        return Step5dPreloadGate(
            filtered_min_n=5.0,
            filtered_max_n=22.0,
            raw_min_n=3.0,
            raw_max_n=25.0,
            force_norm_max_n=35.0,
            hold_s=0.100,
            recovery_normal_load_min_n=0.0,
            recovery_normal_load_max_n=35.0,
            force_norm_stop_n=35.0,
        )
    if program == STEP5D_ABLATION_V26_STAGE_ID:
        return Step5dPreloadGate(
            filtered_min_n=7.0,
            filtered_max_n=18.0,
            raw_min_n=5.0,
            raw_max_n=20.0,
            force_norm_max_n=25.0,
            hold_s=0.100,
            recovery_normal_load_min_n=0.0,
            recovery_normal_load_max_n=24.0,
            force_norm_stop_n=25.0,
        )
    if program == STEP5D_ABLATION_V25_STAGE_ID:
        return Step5dPreloadGate(
            filtered_min_n=10.5,
            filtered_max_n=12.8,
            raw_min_n=9.5,
            raw_max_n=13.5,
            force_norm_max_n=25.0,
            hold_s=0.100,
            recovery_normal_load_min_n=0.0,
            recovery_normal_load_max_n=20.0,
            force_norm_stop_n=25.0,
        )
    if program == STEP5D_LIVEPREP_V24_STAGE_ID:
        return Step5dPreloadGate(
            filtered_min_n=7.5,
            filtered_max_n=14.0,
            raw_min_n=7.0,
            raw_max_n=15.0,
            force_norm_max_n=25.0,
            hold_s=0.100,
            recovery_normal_load_min_n=0.0,
            recovery_normal_load_max_n=20.0,
            force_norm_stop_n=25.0,
        )
    if program in {STEP5D_LIVEPREP_V21_STAGE_ID, STEP5D_LIVEPREP_V22_STAGE_ID, STEP5D_LIVEPREP_V23_STAGE_ID}:
        return Step5dPreloadGate(
            filtered_min_n=7.5,
            filtered_max_n=14.0,
            raw_min_n=7.0,
            raw_max_n=15.0,
            force_norm_max_n=25.0,
            hold_s=0.100,
        )
    return Step5dPreloadGate(
        filtered_min_n=8.0,
        filtered_max_n=13.0,
        raw_min_n=7.5,
        raw_max_n=14.0,
        force_norm_max_n=25.0,
        hold_s=0.100,
    )


def default_bridge_rezero_s(program: str, root: Path = EXPERIMENT_ROOT) -> float:
    if uses_step5b_speedl_live_source(program):
        profile = runtime_protocol_profile(root)
        return float(profile["parameters"]["zero_hold_s"])
    return 1.0


def runtime_protocol_profile(root: Path = EXPERIMENT_ROOT) -> dict[str, Any]:
    try:
        return resolve_experiment_profile("Step5.step5d_rnn", root)
    except ProtocolTableError:
        if (root / "config" / "tase_protocol_table.json").exists():
            raise
        return STEP5D_PROTOCOL_FALLBACK_PROFILE


def resolve_runtime_interface(
    *,
    program: str | None = None,
    root: Path = EXPERIMENT_ROOT,
    env: Mapping[str, str] | None = None,
) -> Step5dRuntimeInterface:
    env_map = env if env is not None else os.environ
    current_path = root / "config" / "current_stage.json"
    current = _current_stage(current_path)
    selected = program or current_step5d_program(current_path)
    try:
        selected_row = _stage_row(selected, root)
    except StageEnvError:
        selected_row = {}
    selected_acceptance = selected_row.get("acceptance") if isinstance(selected_row, dict) else {}
    selected_delivery = selected_row.get("local_delivery_evidence") if isinstance(selected_row, dict) else {}
    selected_package_delivery = selected_row.get("package_delivery") if isinstance(selected_row, dict) else {}
    controller_readback_verified = bool(
        (isinstance(selected_acceptance, dict) and selected_acceptance.get("controller_readback_verified") is True)
        or (isinstance(selected_delivery, dict) and selected_delivery.get("controller_readback_verified") is True)
        or (
            isinstance(selected_package_delivery, dict)
            and selected_package_delivery.get("controller_readback_verified") is True
        )
    )
    controller_readback_required = bool(
        (isinstance(selected_acceptance, dict) and selected_acceptance.get("controller_readback_required") is True)
        or not controller_readback_verified
    )
    target = controller_target_for(selected, current, root)
    default_gate = default_preload_gate(selected)
    protocol_profile = runtime_protocol_profile(root)
    protocol_params = protocol_profile["parameters"]
    protocol_limits = protocol_profile["safety_limits"]
    if is_no_contact_p0_stage(selected):
        trusted_normal_default_n = 2.0
        trusted_force_default_n = 5.0
        trusted_torque_default_nm = 3.0
    elif uses_step5b_speedl_live_source(selected):
        trusted_normal_default_n = STEP5D_V27_STEP5B_ENVELOPE_NORMAL_GUARD_N
        trusted_force_default_n = STEP5D_V27_STEP5B_ENVELOPE_FORCE_GUARD_N
        trusted_torque_default_nm = STEP5D_V27_STEP5B_ENVELOPE_TORQUE_GUARD_NM
    elif selected in {STEP5D_LIVEPREP_V24_STAGE_ID, STEP5D_ABLATION_V25_STAGE_ID, STEP5D_ABLATION_V26_STAGE_ID}:
        trusted_normal_default_n = 25.0
        trusted_force_default_n = 25.0
        trusted_torque_default_nm = 4.0
    else:
        trusted_normal_default_n = 100.0
        trusted_force_default_n = 100.0
        trusted_torque_default_nm = 4.0
    stage25_control_mode = (
        "speedj_rnn_live"
        if is_no_contact_p0_stage(selected)
        else str(
            env_map.get(
                "STEP5D_STAGE25_CONTROL_MODE",
                "speedj_rnn_live"
                if selected in {STEP5D_ABLATION_V29_STAGE_ID, STEP5D_ABLATION_V30_STAGE_ID}
                else "speedl_cartesian_oracle"
                if selected in STEP5D_ABLATION_STAGE_IDS
                else "speedj_rnn_live",
            )
        )
    )
    if stage25_control_mode not in STEP5D_STAGE25_CONTROL_MODES:
        raise ValueError(
            "STEP5D_STAGE25_CONTROL_MODE must be one of "
            f"{', '.join(STEP5D_STAGE25_CONTROL_MODES)}: {stage25_control_mode!r}"
        )
    if is_no_contact_p0_stage(selected):
        gate = default_gate
    else:
        gate = Step5dPreloadGate(
            filtered_min_n=env_float(env_map, "STEP5D_PRELOAD_FILTERED_MIN_N", default_gate.filtered_min_n),
            filtered_max_n=env_float(env_map, "STEP5D_PRELOAD_FILTERED_MAX_N", default_gate.filtered_max_n),
            raw_min_n=env_float(env_map, "STEP5D_PRELOAD_RAW_MIN_N", default_gate.raw_min_n),
            raw_max_n=env_float(env_map, "STEP5D_PRELOAD_RAW_MAX_N", default_gate.raw_max_n),
            force_norm_max_n=env_float(env_map, "STEP5D_PRELOAD_FORCE_NORM_MAX_N", default_gate.force_norm_max_n),
            hold_s=env_float(env_map, "STEP5D_PRELOAD_HOLD_S", default_gate.hold_s),
            timeout_s=env_float(env_map, "STEP5D_PRELOAD_TIMEOUT_S", default_gate.timeout_s),
            cmd_limit_m_s=env_float(env_map, "STEP5D_PRELOAD_CMD_LIMIT_M_S", default_gate.cmd_limit_m_s),
            recovery_normal_load_min_n=env_float(
                env_map,
                "STEP5D_PRELOAD_RECOVERY_NORMAL_MIN_N",
                default_gate.recovery_normal_load_min_n,
            ),
            recovery_normal_load_max_n=env_float(
                env_map,
                "STEP5D_PRELOAD_RECOVERY_NORMAL_MAX_N",
                default_gate.recovery_normal_load_max_n,
            ),
            force_norm_stop_n=env_float(env_map, "STEP5D_PRELOAD_FORCE_NORM_STOP_N", default_gate.force_norm_stop_n),
        )
    if is_no_contact_p0_stage(selected):
        p0_env = build_stage_env(selected, root)
        bridge_defaults = Step5dBridgeDefaults(
            duration_s=float(p0_env["BRIDGE_DURATION_S"]),
            target_force_n=float(p0_env["BRIDGE_TARGET_FORCE_N"]),
            force_p_gain=float(p0_env["BRIDGE_FORCE_P_GAIN"]),
            force_i_gain=float(p0_env["BRIDGE_FORCE_I_GAIN"]),
            force_damping=float(p0_env["BRIDGE_FORCE_DAMPING"]),
            integral_limit_n_s=float(p0_env["BRIDGE_INTEGRAL_LIMIT_N_S"]),
            normal_velocity_limit_m_s=float(p0_env["BRIDGE_NORMAL_VELOCITY_LIMIT_M_S"]),
            normal_filter_alpha=float(p0_env["BRIDGE_NORMAL_FILTER_ALPHA"]),
            normal_min_force_n=float(p0_env["BRIDGE_NORMAL_MIN_FORCE_N"]),
            total_linear_limit_m_s=float(p0_env["BRIDGE_TOTAL_LINEAR_LIMIT_M_S"]),
            angular_limit_rad_s=float(p0_env["BRIDGE_ANGULAR_LIMIT_RAD_S"]),
            max_normal_force_n=float(p0_env["MAX_NORMAL_FORCE_N"]),
            max_force_norm_n=float(p0_env["MAX_FORCE_NORM_N"]),
            max_torque_norm_nm=float(p0_env["MAX_TORQUE_NORM_NM"]),
            baseline_s=float(p0_env["BRIDGE_BASELINE_S"]),
            rezero_s=float(p0_env["BRIDGE_REZERO_S"]),
            rtde_hz=float(p0_env["BRIDGE_RTDE_HZ"]),
            sensor_stale_s=float(p0_env["BRIDGE_SENSOR_STALE_S"]),
            socket_timeout_s=float(p0_env["BRIDGE_SOCKET_TIMEOUT_S"]),
        )
    else:
        bridge_defaults = Step5dBridgeDefaults(
            duration_s=env_float(env_map, "STEP5D_DURATION_S", float(protocol_params["bridge_duration_s"]), legacy="BRIDGE_DURATION_S"),
            target_force_n=env_float(env_map, "STEP5D_TARGET_FORCE_N", float(protocol_params["target_force_n"]), legacy="BRIDGE_TARGET_FORCE_N"),
            force_p_gain=env_float(env_map, "STEP5D_FORCE_P_GAIN", float(protocol_params["force_p_gain"]), legacy="BRIDGE_FORCE_P_GAIN"),
            force_i_gain=env_float(env_map, "STEP5D_FORCE_I_GAIN", float(protocol_params["force_i_gain"]), legacy="BRIDGE_FORCE_I_GAIN"),
            force_damping=env_float(env_map, "STEP5D_FORCE_DAMPING", float(protocol_params["force_damping"]), legacy="BRIDGE_FORCE_DAMPING"),
            integral_limit_n_s=env_float(env_map, "STEP5D_INTEGRAL_LIMIT_N_S", float(protocol_params["integral_limit_n_s"]), legacy="BRIDGE_INTEGRAL_LIMIT_N_S"),
            normal_velocity_limit_m_s=env_float(env_map, "STEP5D_NORMAL_VELOCITY_LIMIT_M_S", float(protocol_limits["normal_velocity_limit_m_s"]), legacy="BRIDGE_NORMAL_VELOCITY_LIMIT_M_S"),
            normal_filter_alpha=env_float(env_map, "STEP5D_NORMAL_FILTER_ALPHA", float(protocol_params["normal_filter_alpha"]), legacy="BRIDGE_NORMAL_FILTER_ALPHA"),
            normal_min_force_n=env_float(env_map, "STEP5D_NORMAL_MIN_FORCE_N", float(protocol_params["normal_filter_min_force_n"]), legacy="BRIDGE_NORMAL_MIN_FORCE_N"),
            total_linear_limit_m_s=env_float(env_map, "STEP5D_TOTAL_LINEAR_LIMIT_M_S", float(protocol_limits["total_linear_limit_m_s"]), legacy="BRIDGE_TOTAL_LINEAR_LIMIT_M_S"),
            angular_limit_rad_s=env_float(env_map, "STEP5D_ANGULAR_LIMIT_RAD_S", 0.150 if selected == STEP5D_ABLATION_V25_STAGE_ID else float(protocol_limits["angular_limit_rad_s"]), legacy="BRIDGE_ANGULAR_LIMIT_RAD_S"),
            max_normal_force_n=env_float(env_map, "STEP5D_MAX_NORMAL_FORCE_N", trusted_normal_default_n, legacy="MAX_NORMAL_FORCE_N"),
            max_force_norm_n=env_float(env_map, "STEP5D_MAX_FORCE_NORM_N", trusted_force_default_n, legacy="MAX_FORCE_NORM_N"),
            max_torque_norm_nm=env_float(env_map, "STEP5D_MAX_TORQUE_NORM_NM", trusted_torque_default_nm, legacy="MAX_TORQUE_NORM_NM"),
            baseline_s=env_float(env_map, "STEP5D_BASELINE_S", 5.0, legacy="BRIDGE_BASELINE_S"),
            rezero_s=env_float(env_map, "STEP5D_REZERO_S", default_bridge_rezero_s(selected, root), legacy="BRIDGE_REZERO_S"),
            rtde_hz=env_float(env_map, "STEP5D_RTDE_HZ", 500.0, legacy="BRIDGE_RTDE_HZ"),
            sensor_stale_s=env_float(env_map, "STEP5D_SENSOR_STALE_S", 0.10, legacy="BRIDGE_SENSOR_STALE_S"),
            socket_timeout_s=env_float(env_map, "STEP5D_SOCKET_TIMEOUT_S", 0.0, legacy="BRIDGE_SOCKET_TIMEOUT_S"),
        )
    validate_interface_values(gate, bridge_defaults)
    return Step5dRuntimeInterface(
        interface_class=STEP5D_INTERFACE_CLASS,
        tuning_bundle=STEP5D_TUNING_BUNDLE,
        program=selected,
        controller_target=target,
        controller_dir=str(PurePosixPath(target).parent),
        preload_gate=gate,
        bridge_defaults=bridge_defaults,
        stage25_control_mode=stage25_control_mode,
        line_entry_param_valid_code=STEP5D_LINE_ENTRY_PARAM_VALID_CODE,
        register_contract={
            "stage25_3": "37..39 Cartesian vx/vy/vz; v21+ 40..42/44/46/47 preload param channel",
            "stage25_95": (
                "37..47 bridge-cleared command barrier; 37..42 near-zero; 43 cmd_valid=0; "
                f"47 must not equal preload param code {STEP5D_LINE_ENTRY_PARAM_VALID_CODE:g}; "
                f"ack cycles={STEP5D_QDOT_CLEAR_ACK_CYCLES}; "
                f"zero tol={STEP5D_QDOT_CLEAR_ZERO_TOL_RAD_S:g}"
            ),
            "stage25_0": stage25_0_register_contract(selected),
        },
        hard_contract={
            "force_frame": "reaction normal for load; approach normal for posture/press direction",
            "stage25_cadence_max_gap_s": 0.020 if uses_step5b_speedl_live_source(selected) else None,
            "stage25_speedl_orientation_policy": speedl_orientation_policy(selected),
            "stage25_live_control_source": (
                "strict_rnn_live_speedj"
                if selected in {
                    STEP5D_ABLATION_V29_STAGE_ID,
                    *STEP5D_V30_CONTROL_CONTRACT_STAGE_IDS,
                }
                else STEP5D_V27_SPEEDL_LIVE_CONTROL_SOURCE
                if uses_step5b_speedl_live_source(selected)
                else None
            ),
            "stage25_success_target_s": stage25_success_target_s(selected),
            "stage25_runtime_limit_s": stage25_runtime_limit_s(selected),
            "controller_readback_required": controller_readback_required,
            "controller_readback_verified": controller_readback_verified,
            "runtime_profile": (
                {
                    "backend": "cupy",
                    "inner_iterations": 1024,
                    "epsilon": 0.010,
                    "sigr_exponent_r": 0.8,
                    "qdot_cap_rad_s": 0.05,
                    "control_mode": "speedj_rnn_live",
                    "joint_layout_code": 524.0,
                }
                if selected == STEP5D_ABLATION_V29_STAGE_ID
                else {
                    "backend": "cupy",
                    "inner_iterations": 128,
                    "epsilon": 0.010,
                    "sigr_exponent_r": 0.8,
                    "qdot_cap_rad_s": 0.05,
                    "control_mode": "speedj_rnn_live",
                    "joint_layout_code": 524.0,
                }
                if selected in STEP5D_V30_CONTROL_CONTRACT_STAGE_IDS
                else None
            ),
            "no_contact_p0_capture": is_no_contact_p0_stage(selected),
            "v30_control_contract": uses_v30_control_contract(selected),
            "no_ubuntu_motion": True,
            "no_zero_ftsensor": True,
            "no_kunwei_tare_or_config": True,
            "no_tcp_payload_write": True,
            "offline_candidate": selected
            in {STEP5D_ABLATION_V30_STAGE_ID, STEP5D_NO_CONTACT_P0_V8_STAGE_ID},
        },
    )


def validate_interface_values(gate: Step5dPreloadGate, bridge: Step5dBridgeDefaults) -> None:
    if gate.filtered_min_n < 0.0 or gate.filtered_max_n < gate.filtered_min_n:
        raise ValueError("STEP5D_PRELOAD_FILTERED range is invalid")
    if gate.raw_min_n < 0.0 or gate.raw_max_n < gate.raw_min_n:
        raise ValueError("STEP5D_PRELOAD_RAW range is invalid")
    if gate.force_norm_max_n <= 0.0 or gate.force_norm_stop_n < gate.force_norm_max_n:
        raise ValueError("STEP5D_PRELOAD force norm limits are invalid")
    if gate.hold_s < 0.0 or gate.timeout_s <= 0.0 or gate.hold_s > gate.timeout_s:
        raise ValueError("STEP5D_PRELOAD dwell/timeout is invalid")
    if gate.cmd_limit_m_s <= 0.0:
        raise ValueError("STEP5D_PRELOAD_CMD_LIMIT_M_S must be positive")
    if not 0.0 <= bridge.normal_filter_alpha <= 1.0:
        raise ValueError("STEP5D_NORMAL_FILTER_ALPHA must be in [0, 1]")
    for label, value in asdict(bridge).items():
        if value <= 0.0 and label not in {"socket_timeout_s"}:
            raise ValueError(f"{label} must be positive")
        if label == "socket_timeout_s" and value < 0.0:
            raise ValueError(f"{label} must be non-negative")


def _run_json(args: list[str]) -> Any:
    completed = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []


def _route_get(host: str) -> str:
    completed = subprocess.run(
        ["ip", "route", "get", host],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def current_long_check_fingerprint(gate: Mapping[str, Any]) -> dict[str, Any]:
    device = str(gate.get("device", "enp3s0"))
    kunwei = gate.get("kunwei") or {}
    kunwei_host = str(kunwei.get("sensor_host", ""))
    return {
        "boot_id": _boot_id(),
        "device": device,
        "ipv4_addresses": _run_json(["ip", "-j", "-4", "addr", "show", "dev", device]),
        "default_routes": _run_json(["ip", "-j", "route", "show", "default"]),
        "kunwei_route_get": _route_get(kunwei_host) if kunwei_host else "",
    }


def long_check_cache_status(
    cache_path: Path = DEFAULT_LONG_CHECK_CACHE,
    *,
    robot_host: str = DEFAULT_ROBOT_HOST,
    ttl_s: float = DEFAULT_LONG_CHECK_TTL_S,
) -> dict[str, Any]:
    status = {
        "ok": False,
        "state": "MISS",
        "age_s": None,
        "ttl_s": ttl_s,
        "fingerprint_ok": False,
        "path": str(cache_path),
    }
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        status["reason"] = "missing"
        return status
    except json.JSONDecodeError as exc:
        status["reason"] = f"invalid_json:{exc}"
        return status

    gate = payload.get("gate") or {}
    kunwei = gate.get("kunwei") or {}
    age_s = time.time() - float(payload.get("checked_at_epoch", 0.0))
    fingerprint_ok = payload.get("fingerprint") == current_long_check_fingerprint(gate)
    cache_ok = (
        payload.get("ok") is True
        and payload.get("robot_host") == robot_host
        and gate.get("ok") is True
        and not gate.get("issues")
        and gate.get("robot_host") == robot_host
        and gate.get("same_subnet") is True
        and gate.get("device") == "enp3s0"
        and kunwei.get("route_ok") is True
        and (kunwei.get("tcp_connect") or {}).get("ok") is True
        and fingerprint_ok
        and 0.0 <= age_s <= ttl_s
    )
    status.update(
        {
            "ok": bool(cache_ok),
            "state": "HIT" if cache_ok else "MISS",
            "age_s": age_s,
            "fingerprint_ok": bool(fingerprint_ok),
            "reason": "ok" if cache_ok else "stale_or_mismatch",
        }
    )
    return status


def live_ready_lines(
    interface: Step5dRuntimeInterface,
    cache: Mapping[str, Any],
    *,
    readiness: Mapping[str, Any] | None = None,
) -> list[str]:
    age = cache.get("age_s")
    age_text = "n/a" if age is None else f"{float(age) / 60.0:.1f}m"
    ttl_text = f"{float(cache.get('ttl_s', DEFAULT_LONG_CHECK_TTL_S)) / 3600.0:.1f}h"
    fp = "ok" if cache.get("fingerprint_ok") else "mismatch"
    gate = interface.preload_gate
    bridge = interface.bridge_defaults
    linear_cap_label = "legacy_total_linear_debug" if is_no_contact_p0_stage(interface.program) else "total_linear"
    if interface.program == STEP5D_ABLATION_V30_STAGE_ID:
        return [
            "[step5d][phase=v30-offline-candidate][rebuild=no][upload=no]",
            "[touches=offline-evidence-only]",
            f"[cache] long-check={cache.get('state', 'MISS')} age={age_text} ttl={ttl_text} fingerprint={fp}",
            "[next] complete v29 replay, 500Hz timing, package hash, and milestone review; current pointer remains v29",
            "[authorization] controller upload/readback, bridge start, TP Play, and live motion are not authorized",
        ]
    if (
        interface.hard_contract.get("controller_readback_required") is True
        and interface.hard_contract.get("controller_readback_verified") is not True
    ):
        return [
            "[step5d][phase=readback-blocked][rebuild=no][upload=required]",
            "[touches=controller-files-only]",
            f"[cache] long-check={cache.get('state', 'MISS')} age={age_text} ttl={ttl_text} fingerprint={fp}",
            f"[next] controller read-back required before TP handoff: {interface.controller_target}",
            (
                "[tuning] stage25 "
                f"mode={interface.stage25_control_mode} "
                f"cartesian_tag={STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE:g} "
                f"joint_tag={STEP5D_STAGE25_JOINT_LAYOUT_CODE:g}"
            ),
        ]
    if interface.program == STEP5D_ABLATION_V29_STAGE_ID:
        offline_ready = bool(
            isinstance(readiness, Mapping)
            and readiness.get("schema_version") == "step5d_liveprep_readiness_v1"
            and readiness.get("program") == interface.program
            and readiness.get("workflow_state") == "awaiting_live_authorization"
            and readiness.get("ready_for_explicit_live_authorization") is True
            and not readiness.get("blockers")
        )
        phase = "awaiting-live-authorization" if offline_ready else "liveprep-blocked"
        blockers = [] if readiness is None else list(readiness.get("blockers") or [])
        next_line = (
            "[next] offline live-prep accepted; separate explicit live/contact authorization is still required"
            if offline_ready
            else "[next] complete offline timing, DLS shadow, package binding, and milestone review"
        )
        if blockers:
            next_line += "; blockers=" + ",".join(str(item) for item in blockers)
        return [
            f"[step5d][phase={phase}][rebuild=no][upload=no]",
            "[touches=offline-evidence-only]",
            f"[cache] long-check={cache.get('state', 'MISS')} age={age_text} ttl={ttl_text} fingerprint={fp}",
            next_line,
            (
                "[tuning] stage25 "
                f"mode={interface.stage25_control_mode} "
                f"backend={interface.hard_contract['runtime_profile']['backend']} "
                f"inner_iterations={interface.hard_contract['runtime_profile']['inner_iterations']} "
                f"joint_tag={STEP5D_STAGE25_JOINT_LAYOUT_CODE:g}"
            ),
        ]
    return [
        "[step5d][phase=live-bridge][rebuild=no][upload=no]",
        "[touches=kunwei+rtde]",
        f"[cache] long-check={cache.get('state', 'MISS')} age={age_text} ttl={ttl_text} fingerprint={fp}",
        f"[next] short checks ETA=1-3s, TP Play wait<=20s, baseline+rezero={bridge.baseline_s + bridge.rezero_s:g}s",
        (
            "[tuning] preload "
            f"filtered={gate.filtered_min_n:g}..{gate.filtered_max_n:g}N "
            f"raw={gate.raw_min_n:g}..{gate.raw_max_n:g}N "
            f"force_norm<={gate.force_norm_max_n:g}N hold={gate.hold_s:g}s"
        ),
        (
            "[tuning] force "
            f"Kp={bridge.force_p_gain:g} Ki={bridge.force_i_gain:g} "
            f"damping={bridge.force_damping:g} filter_alpha={bridge.normal_filter_alpha:g}"
        ),
        (
            "[tuning] stage25 "
            f"mode={interface.stage25_control_mode} "
            f"cartesian_tag={STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE:g} "
            f"joint_tag={STEP5D_STAGE25_JOINT_LAYOUT_CODE:g}"
        ),
        (
            "[caps] "
            f"{linear_cap_label}={bridge.total_linear_limit_m_s:g}m/s "
            f"angular={bridge.angular_limit_rad_s:g}rad/s "
            f"hard_force={bridge.max_normal_force_n:g}/{bridge.max_force_norm_n:g}N "
            f"torque={bridge.max_torque_norm_nm:g}Nm"
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=EXPERIMENT_ROOT)
    parser.add_argument("--program", default=None)
    parser.add_argument("--robot-host", default=DEFAULT_ROBOT_HOST)
    parser.add_argument("--long-check-cache", type=Path, default=DEFAULT_LONG_CHECK_CACHE)
    parser.add_argument("--long-check-ttl-s", type=float, default=DEFAULT_LONG_CHECK_TTL_S)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("command", choices=("live-ready", "interface-json"))
    args = parser.parse_args(argv)

    interface = resolve_runtime_interface(program=args.program, root=args.root)
    cache = long_check_cache_status(args.long_check_cache, robot_host=args.robot_host, ttl_s=args.long_check_ttl_s)
    readiness: dict[str, Any] | None = None
    if interface.program == STEP5D_ABLATION_V29_STAGE_ID:
        current = _current_stage(args.root / "config" / "current_stage.json")
        artifact = (current.get("liveprep_status") or {}).get("readiness_artifact")
        if artifact:
            try:
                readiness = json.loads((args.root / str(artifact)).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                readiness = None
    if args.json or args.command == "interface-json":
        print(
            json.dumps(
                {
                    "interface": asdict(interface),
                    "long_check_cache": cache,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print("\n".join(live_ready_lines(interface, cache, readiness=readiness)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
