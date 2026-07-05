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
STEP5D_ABLATION_STAGE_IDS = (
    STEP5D_ABLATION_V25_STAGE_ID,
    STEP5D_ABLATION_V26_STAGE_ID,
    STEP5D_ABLATION_V27_STAGE_ID,
)
STEP5D_STAGE25_CONTROL_MODES = ("speedl_cartesian_oracle", "speedj_dls_oracle", "speedj_rnn_live")
STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE = 523.0
STEP5D_STAGE25_JOINT_LAYOUT_CODE = 524.0
STEP5D_LINE_ENTRY_PARAM_VALID_CODE = 521.0
STEP5D_QDOT_CLEAR_STAGE = 25.95
STEP5D_QDOT_CLEAR_ACK_CYCLES = 3
STEP5D_QDOT_CLEAR_ZERO_TOL_RAD_S = 0.0005


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
    if program.startswith(("step5d_strict_rnn_liveprep_", "step5d_strict_rnn_ablation_")):
        return program
    return STEP5D_LIVEPREP_V20_STAGE_ID


def controller_target_for(program: str, current: dict[str, Any] | None = None) -> str:
    payload = current if current is not None else _current_stage()
    if payload.get("program") == program and payload.get("controller_target"):
        return str(payload["controller_target"])
    return f"/programs/andyl/kunwei/step5/{program}.urp"


def default_preload_gate(program: str) -> Step5dPreloadGate:
    if program == STEP5D_ABLATION_V27_STAGE_ID:
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


def default_bridge_rezero_s(program: str) -> float:
    if program == STEP5D_ABLATION_V27_STAGE_ID:
        return 0.25
    return 1.0


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
    target = controller_target_for(selected, current)
    default_gate = default_preload_gate(selected)
    if selected == STEP5D_ABLATION_V27_STAGE_ID:
        trusted_force_default_n = 35.0
    elif selected in {STEP5D_LIVEPREP_V24_STAGE_ID, STEP5D_ABLATION_V25_STAGE_ID, STEP5D_ABLATION_V26_STAGE_ID}:
        trusted_force_default_n = 25.0
    else:
        trusted_force_default_n = 100.0
    stage25_control_mode = str(
        env_map.get(
            "STEP5D_STAGE25_CONTROL_MODE",
            "speedl_cartesian_oracle"
            if selected in STEP5D_ABLATION_STAGE_IDS
            else "speedj_rnn_live",
        )
    )
    if stage25_control_mode not in STEP5D_STAGE25_CONTROL_MODES:
        raise ValueError(
            "STEP5D_STAGE25_CONTROL_MODE must be one of "
            f"{', '.join(STEP5D_STAGE25_CONTROL_MODES)}: {stage25_control_mode!r}"
        )
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
    bridge_defaults = Step5dBridgeDefaults(
        duration_s=env_float(env_map, "STEP5D_DURATION_S", 180.0, legacy="BRIDGE_DURATION_S"),
        target_force_n=env_float(env_map, "STEP5D_TARGET_FORCE_N", 12.0, legacy="BRIDGE_TARGET_FORCE_N"),
        force_p_gain=env_float(env_map, "STEP5D_FORCE_P_GAIN", 0.0010, legacy="BRIDGE_FORCE_P_GAIN"),
        force_i_gain=env_float(env_map, "STEP5D_FORCE_I_GAIN", 0.00001, legacy="BRIDGE_FORCE_I_GAIN"),
        force_damping=env_float(env_map, "STEP5D_FORCE_DAMPING", 7.0, legacy="BRIDGE_FORCE_DAMPING"),
        integral_limit_n_s=env_float(
            env_map,
            "STEP5D_INTEGRAL_LIMIT_N_S",
            1.0,
            legacy="BRIDGE_INTEGRAL_LIMIT_N_S",
        ),
        normal_velocity_limit_m_s=env_float(
            env_map,
            "STEP5D_NORMAL_VELOCITY_LIMIT_M_S",
            0.0100,
            legacy="BRIDGE_NORMAL_VELOCITY_LIMIT_M_S",
        ),
        normal_filter_alpha=env_float(
            env_map,
            "STEP5D_NORMAL_FILTER_ALPHA",
            0.55,
            legacy="BRIDGE_NORMAL_FILTER_ALPHA",
        ),
        normal_min_force_n=env_float(env_map, "STEP5D_NORMAL_MIN_FORCE_N", 2.0, legacy="BRIDGE_NORMAL_MIN_FORCE_N"),
        total_linear_limit_m_s=env_float(
            env_map,
            "STEP5D_TOTAL_LINEAR_LIMIT_M_S",
            0.0040,
            legacy="BRIDGE_TOTAL_LINEAR_LIMIT_M_S",
        ),
        angular_limit_rad_s=env_float(
            env_map,
            "STEP5D_ANGULAR_LIMIT_RAD_S",
            0.150 if selected == STEP5D_ABLATION_V25_STAGE_ID else 0.015,
            legacy="BRIDGE_ANGULAR_LIMIT_RAD_S",
        ),
        max_normal_force_n=env_float(env_map, "STEP5D_MAX_NORMAL_FORCE_N", trusted_force_default_n, legacy="MAX_NORMAL_FORCE_N"),
        max_force_norm_n=env_float(env_map, "STEP5D_MAX_FORCE_NORM_N", trusted_force_default_n, legacy="MAX_FORCE_NORM_N"),
        max_torque_norm_nm=env_float(env_map, "STEP5D_MAX_TORQUE_NORM_NM", 4.0, legacy="MAX_TORQUE_NORM_NM"),
        baseline_s=env_float(env_map, "STEP5D_BASELINE_S", 5.0, legacy="BRIDGE_BASELINE_S"),
        rezero_s=env_float(env_map, "STEP5D_REZERO_S", default_bridge_rezero_s(selected), legacy="BRIDGE_REZERO_S"),
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
            "stage25_0": (
                "v25/v26/v27: 37..42 cartesian vx/vy/vz/wx/wy/wz when "
                f"47={STEP5D_STAGE25_CARTESIAN_LAYOUT_CODE:g}; "
                "37..42 joint qd0..qd5 rad/s when "
                f"47={STEP5D_STAGE25_JOINT_LAYOUT_CODE:g}; "
                "43 cmd_valid; 44 path_time; 45 force_error; 46 pose/orientation_error. "
                "v24 and older: 37..42 qd0..qd5 rad/s; 47 solver_status."
            ),
        },
        hard_contract={
            "force_frame": "reaction normal for load; approach normal for posture/press direction",
            "stage25_cadence_max_gap_s": 0.020 if selected == STEP5D_ABLATION_V27_STAGE_ID else None,
            "no_ubuntu_motion": True,
            "no_zero_ftsensor": True,
            "no_kunwei_tare_or_config": True,
            "no_tcp_payload_write": True,
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


def live_ready_lines(interface: Step5dRuntimeInterface, cache: Mapping[str, Any]) -> list[str]:
    age = cache.get("age_s")
    age_text = "n/a" if age is None else f"{float(age) / 60.0:.1f}m"
    ttl_text = f"{float(cache.get('ttl_s', DEFAULT_LONG_CHECK_TTL_S)) / 3600.0:.1f}h"
    fp = "ok" if cache.get("fingerprint_ok") else "mismatch"
    gate = interface.preload_gate
    bridge = interface.bridge_defaults
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
            f"total_linear={bridge.total_linear_limit_m_s:g}m/s "
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
        print("\n".join(live_ready_lines(interface, cache)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
