#!/usr/bin/env python3
"""Fast, read-only preflight for the one-Play V3 live campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import build_step5d_autotune_tp_v3 as tp_v3
import run_step5d_autotune_v3_bridge as bridge_wrapper
from step5d_autotune_v3 import preflight_support as support
from step5d_autotune_v3.launcher import build_bridge_argv
from step5d_autotune_v3.profile import load_contract
from step5d_autotune_v3.dashboard import dashboard_exchange
from step5d_autotune_v3.delivery_observation import load_delivery_observation
from step5d_autotune_v3.release_identity import (
    LAUNCH_PROFILE_PATH,
    SAFETY_ENVELOPE_PATH,
    load_runtime_release,
    release_payload_path,
)
from step5d_autotune_v3.runtime_gate import (
    loaded_program_matches,
    release_runtime_contract,
    validate_tp_runtime_identity,
)
from step5d_autotune_v3.runtime_identity import validate_rtde_output_recipe
from step5d_autotune_v3.rtde_client import RTDEClient
from step5d_autotune_v3.runtime_profile import DEFAULT_OVERLAY
from step5d_autotune_v3.runtime_profile import load_launch_profile
from step5d_autotune_v3.runtime_calibration import dependency_observation
from step5d_autotune_v3.runtime_installation import require_runtime_profile
from step5d_autotune_v3.state import atomic_json


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "step5d.autotune-v3/live-preflight-snapshot-v3"
PREDICATE_NAMES = frozenset(
    {
        "safety_normal",
        "program_safe_for_bridge",
        "robot_stationary",
        "prealign_start_clearance",
        "no_existing_writer",
        "mailbox_initial_zero",
        "runtime_dependencies",
        "controller_delivery",
    }
)


class PreflightError(RuntimeError):
    pass


def _observed(function: Callable[[], Any]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        value = function()
    except Exception as exc:
        value = {"ok": False, "error": f"{type(exc).__name__}:{exc}"}
    if not isinstance(value, dict):
        value = {"ok": False, "error": "observation was not an object"}
    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": time.monotonic() - started,
        "value": value,
    }


def _parallel(checks: Mapping[str, Callable[[], Any]]) -> dict[str, dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=len(checks)) as executor:
        futures = {name: executor.submit(_observed, function) for name, function in checks.items()}
        return {name: futures[name].result() for name in checks}


def _read_rtde_with_recipe_proof(
    host: str,
    fields: list[str],
    *,
    frequency_hz: float,
    timeout: float,
) -> dict[str, Any]:
    with RTDEClient(host, timeout=timeout) as client:
        client.negotiate()
        recipe_id, type_names = client.setup_outputs(frequency_hz, fields)
        identity_types = validate_rtde_output_recipe(fields, type_names)
        client.start()
        values = client.recv_recipe_sample(recipe_id, type_names)
    return {
        **dict(zip(fields, values, strict=True)),
        "_recipe_id": recipe_id,
        "_runtime_identity_recipe_types": identity_types,
    }


def _value(observation: Mapping[str, Any]) -> Mapping[str, Any]:
    value = observation.get("value")
    return value if isinstance(value, Mapping) else {}


def _program_safe_for_bridge(
    dashboard: Mapping[str, Any],
    rtde: Mapping[str, Any],
    *,
    expected_controller_program: str,
    runtime_identity: Mapping[str, Any],
) -> dict[str, Any]:
    raw_state = str(
        dashboard.get("programState", dashboard.get("program_state", ""))
    )
    raw_loaded = str(
        dashboard.get("get loaded program", dashboard.get("loaded_program", ""))
    )
    state = raw_state.split(maxsplit=1)[0].upper() if raw_state else ""
    exact_program = loaded_program_matches(raw_loaded, expected_controller_program)
    if state == "STOPPED":
        checks = {"exact_program": exact_program, "stopped": True}
        return {
            "ok": all(checks.values()),
            "mode": "loaded_stopped",
            "checks": checks,
            "program_state": raw_state,
            "loaded_program": raw_loaded,
            "expected_loaded_program": expected_controller_program,
        }
    identity_fields = [24, 25, 27, 28, 29, 30, 31, 32, 33, 34]
    ready_home = rtde.get("output_int_register_26") == 10
    zero_identity = all(
        rtde.get(f"output_int_register_{index}") == 0 for index in identity_fields
    )
    try:
        validate_tp_runtime_identity(rtde, runtime_identity)
        runtime_identity_ok = True
        runtime_identity_error = None
    except Exception as exc:
        runtime_identity_ok = False
        runtime_identity_error = f"{type(exc).__name__}:{exc}"
    checks = {
        "exact_program": exact_program,
        "playing": state == "PLAYING",
        "ready_home": ready_home,
        "zero_identity": zero_identity,
        "tp_runtime_identity": runtime_identity_ok,
    }
    return {
        "ok": all(checks.values()),
        "mode": "playing_ready_home_zero_identity",
        "checks": checks,
        "program_state": raw_state,
        "loaded_program": raw_loaded,
        "expected_loaded_program": expected_controller_program,
        "tp_runtime_identity_error": runtime_identity_error,
    }


def _stationary(rtde: Mapping[str, Any]) -> dict[str, Any]:
    speed = rtde.get("actual_TCP_speed")
    qd = rtde.get("actual_qd", [0.0] * 6)
    if not (
        isinstance(speed, list)
        and len(speed) == 6
        and isinstance(qd, list)
        and len(qd) == 6
    ):
        return {"ok": False, "error": "RTDE stationary vectors are incomplete"}
    try:
        linear = math.sqrt(sum(float(value) ** 2 for value in speed[:3]))
        joint = max(abs(float(value)) for value in qd)
    except (TypeError, ValueError):
        return {"ok": False, "error": "RTDE stationary vectors are nonnumeric"}
    return {
        "ok": math.isfinite(linear)
        and math.isfinite(joint)
        and linear <= 0.001
        and joint <= 0.005,
        "tcp_linear_speed_m_s": linear,
        "joint_speed_max_rad_s": joint,
        "limits": {"tcp_linear_speed_m_s": 0.001, "joint_speed_max_rad_s": 0.005},
    }


def _prealign_start_clearance(rtde: Mapping[str, Any]) -> dict[str, Any]:
    pose = rtde.get("actual_TCP_pose")
    required_z_m = tp_v3.PRECONTACT_XYZ_M[2] + tp_v3.MINIMUM_START_ABOVE_ENTRY_M
    if not isinstance(pose, list) or len(pose) != 6:
        return {"ok": False, "error": "RTDE actual_TCP_pose is incomplete"}
    try:
        observed_z_m = float(pose[2])
    except (TypeError, ValueError):
        return {"ok": False, "error": "RTDE actual_TCP_pose z is nonnumeric"}
    return {
        "ok": math.isfinite(observed_z_m) and observed_z_m >= required_z_m,
        "observed_start_z_m": observed_z_m,
        "precontact_entry_z_m": tp_v3.PRECONTACT_XYZ_M[2],
        "minimum_start_above_entry_m": tp_v3.MINIMUM_START_ABOVE_ENTRY_M,
        "required_start_z_min_m": required_z_m,
        "policy": "reject_before Play when two-step prealign lacks vertical clearance",
    }


def _safety_normal(dashboard: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(dashboard.get("safetymode", dashboard.get("safety_mode", "")))
    value = raw.rsplit(":", 1)[-1].strip().upper()
    return {"ok": value == "NORMAL", "observed": value}


def _mailbox_initial_zero(path: Path) -> dict[str, Any]:
    absent = not path.exists() and not path.is_symlink()
    return {"ok": absent, "path": str(path), "policy": "absent_before_live_bridge"}


def _connect_observation_ok(observation: Mapping[str, Any]) -> bool:
    value = _value(observation)
    return value.get("ok", True) is True and value.get("open") is True


def _controller_identity(
    dashboard: Mapping[str, Any], rtde: Mapping[str, Any], *, robot_host: str
) -> tuple[dict[str, Any], str]:
    material = {
        "schema": "step5d.autotune-v3/controller-identity-snapshot-v1",
        "robot_host": robot_host,
        "dashboard": {
            key: dashboard.get(key)
            for key in (
                "PolyscopeVersion",
                "robotmode",
                "safetymode",
                "programState",
                "get loaded program",
                "is in remote control",
                "remote_control",
            )
            if key in dashboard
        },
        "rtde": {
            key: rtde.get(key)
            for key in (
                "robot_mode",
                "safety_mode",
                "runtime_state",
                "output_int_register_24",
                "output_int_register_25",
                "output_int_register_26",
                "output_int_register_27",
                "output_int_register_28",
                "output_int_register_29",
                "output_int_register_30",
                "output_int_register_31",
                "output_int_register_32",
                "output_int_register_33",
                "output_int_register_34",
                "output_int_register_35",
                "output_int_register_36",
                "output_int_register_37",
                "_runtime_identity_recipe_types",
            )
        },
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return material, hashlib.sha256(encoded).hexdigest()


def run_preflight(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    release = load_runtime_release(ROOT)
    delivery = load_delivery_observation(
        ROOT, args.delivery_observation, release=release
    )
    runtime_contract = release_runtime_contract(ROOT, release)
    contract_path = release_payload_path(ROOT, release, SAFETY_ENVELOPE_PATH)
    launch_path = release_payload_path(ROOT, release, LAUNCH_PROFILE_PATH)
    contract = load_contract(contract_path)
    launch = load_launch_profile(launch_path, contract=contract)
    governed_argv = build_bridge_argv(
        args.mailbox.parent,
        contract=contract,
        launch_profile=launch,
        trial_overlay=DEFAULT_OVERLAY,
    )
    local = _parallel(
        {
            "route_robot": lambda: support.run_command(
                ["ip", "route", "get", args.robot_host]
            ),
            "route_kunwei": lambda: support.run_command(
                ["ip", "route", "get", args.sensor_ip]
            ),
            "writer": support.no_existing_writer,
            "realtime": support.realtime_capability,
            "runtime_calibration": dependency_observation,
            "production_startup_prewarm": lambda: bridge_wrapper.check_v3_runtime_prewarm(
                governed_argv[2:]
            ),
        }
    )
    local_ok = all(_value(item).get("ok") is True for item in local.values())
    if not local_ok:
        remote: dict[str, Any] = {"skipped": "local_stage_failed"}
        dashboard: Mapping[str, Any] = {}
        rtde: Mapping[str, Any] = {}
    else:
        remote = _parallel(
            {
                "dashboard": lambda: dashboard_exchange(
                    args.robot_host,
                    [
                        "PolyscopeVersion",
                        "is in remote control",
                        "safetymode",
                        "robotmode",
                        "programState",
                        "get loaded program",
                    ],
                    timeout=args.timeout_s,
                ),
                "rtde": lambda: _read_rtde_with_recipe_proof(
                    args.robot_host,
                    [
                        *support.RTDE_FIELDS,
                        "actual_qd",
                        *[f"output_int_register_{index}" for index in range(24, 38)],
                    ],
                    frequency_hz=10.0,
                    timeout=args.timeout_s,
                ),
                "secondary_port": lambda: support.probe_port(
                    args.robot_host, 30002, timeout=args.timeout_s
                ),
                "kunwei": lambda: support.tcp_connect_only(
                    args.sensor_ip, args.sensor_port, args.timeout_s
                ),
            }
        )
        dashboard = _value(remote["dashboard"])
        rtde = _value(remote["rtde"])
    controller_identity, controller_sha = _controller_identity(
        dashboard, rtde, robot_host=args.robot_host
    )
    predicates = {
        "safety_normal": _safety_normal(dashboard),
        "program_safe_for_bridge": _program_safe_for_bridge(
            dashboard,
            rtde,
            expected_controller_program=runtime_contract["expected_loaded_program"],
            runtime_identity=runtime_contract["tp_runtime_identity"],
        ),
        "robot_stationary": _stationary(rtde),
        "prealign_start_clearance": _prealign_start_clearance(rtde),
        "no_existing_writer": {
            "ok": _value(local.get("writer", {})).get("ok") is True,
            "observation": _value(local.get("writer", {})),
        },
        "mailbox_initial_zero": _mailbox_initial_zero(args.mailbox),
        "runtime_dependencies": {
            "ok": all(
                _value(local.get(name, {})).get("ok") is True
                for name in ("runtime_calibration", "production_startup_prewarm")
            ),
            "calibration": _value(local.get("runtime_calibration", {})),
            "production_startup_prewarm": _value(
                local.get("production_startup_prewarm", {})
            ),
        },
        "controller_delivery": {"ok": True, "observation": delivery},
    }
    if set(predicates) != PREDICATE_NAMES:
        raise PreflightError("internal live-preflight predicate schema drift")
    transport_ok = (
        local_ok
        and "dashboard" in remote
        and bool(dashboard)
        and _value(remote["dashboard"]).get("ok", True) is True
        and "rtde" in remote
        and support.rtde_predicate(dict(rtde)).get("ok") is True
        and _connect_observation_ok(remote.get("secondary_port", {}))
        and _connect_observation_ok(remote.get("kunwei", {}))
    )
    elapsed = time.monotonic() - started
    payload = {
        "schema": SCHEMA,
        "ok": transport_ok and all(item["ok"] is True for item in predicates.values()),
        "fresh": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": elapsed,
        "healthy_target_s": 1.25,
        "candidate_stage_id": release.release_stage_id,
        "control_profile_id": release.control_profile_id,
        "tp_program_id": release.program_id,
        "release_manifest_sha256": release.manifest_sha256,
        "expected_loaded_program": runtime_contract["expected_loaded_program"],
        "tp_runtime_identity": runtime_contract["tp_runtime_identity"],
        "launch_profile_fingerprint": launch.fingerprint,
        "launch_profile": {
            "path": LAUNCH_PROFILE_PATH,
            "sha256": release.generated_files[LAUNCH_PROFILE_PATH],
        },
        "controller_identity": controller_identity,
        "controller_identity_sha256": controller_sha,
        "predicates": predicates,
        "observations": {"local": local, "remote": remote},
        "probe_reuse": {
            "dashboard_port_29999": "dashboard observation",
            "rtde_port_30004": "RTDE observation",
            "secondary_port_30002": "one connect-only probe",
            "kunwei": "one connect-only probe",
            "ping": "omitted_from_fast_gate",
            "bench_network": "omitted_from_fast_gate",
        },
        "safety_boundary": [
            "Dashboard read-only commands only",
            "RTDE output recipe only",
            "Kunwei TCP connect-only; no stream/zero/tare command",
            "local runtime calibration artifact and installed model sources are hash-checked",
            "production startup prewarm runs locally before any device writer is started",
            "the current TCP start Z is checked against the evidence-bound prealign Z before Play",
            "no bridge, RTDE input, Load, ARM, contact, or motion",
            "an operator-started exact release is accepted only at READY_HOME with zero trial identity and matching TP runtime identity",
        ],
    }
    atomic_json(args.output, payload)
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument("--delivery-observation", type=Path, required=True)
    parser.add_argument(
        "--launch-profile",
        type=Path,
        default=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        require_runtime_profile("control")
        payload = run_preflight(args)
    except Exception as exc:
        payload = {"schema": SCHEMA, "ok": False, "fresh": False, "blocker": str(exc)}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
