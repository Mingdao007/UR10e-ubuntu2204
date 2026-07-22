#!/usr/bin/env python3
"""Fresh read-only preflight for the isolated manual-hold NO_ARM bridge."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Mapping

import preflight_step5d_autotune_v3 as r009_preflight
import run_step5d_autotune_v3_bridge as r009_bridge
from step5d_autotune_v3 import preflight_support as support
from step5d_autotune_v3.dashboard import dashboard_exchange
from step5d_autotune_v3.rtde_client import RTDEClient
from step5d_autotune_v3.launcher import build_bridge_argv
from step5d_autotune_v3.runtime_calibration import dependency_observation
from step5d_autotune_v3.runtime_gate import loaded_program_matches
from step5d_autotune_v3.runtime_profile import DEFAULT_OVERLAY
from step5d_autotune_v3.state import atomic_json
from step5d_manual_bridge import (
    CONTROL_PROFILE, PREFLIGHT_SCHEMA, PROGRAM, RELEASE_STAGE, ROOT,
    ManualBridgeError, load_context, sha256_path,
)
from step5d_manual_profile import DEFAULT_LAUNCH_PROFILE, load_manual_launch_profile
from promote_step5d_manual_release import TARGET_DIR, load_manual_release
import upload_ur_tp_package as tp_upload


def _manual_artifacts(
    root: Path,
    release_manifest_sha256: str,
) -> tuple[dict[str, Path], dict[str, str]]:
    release = load_manual_release(root)
    if release.get("manifest_sha256") != release_manifest_sha256:
        raise ManualBridgeError("Manual release identity changed before controller read-back")
    manifest_path = root / str(release["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(tp_upload.EXTENSIONS):
        raise ManualBridgeError("Manual release triplet fields differ")
    files: dict[str, Path] = {}
    expected: dict[str, str] = {}
    for extension in tp_upload.EXTENSIONS:
        reference = artifacts.get(extension)
        if not isinstance(reference, Mapping):
            raise ManualBridgeError(f"Manual {extension} release reference differs")
        path = root / str(reference.get("path", ""))
        digest = str(reference.get("sha256", ""))
        if (
            path.is_symlink()
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise ManualBridgeError(f"Manual {extension} local artifact drifted")
        files[extension] = path
        expected[extension] = digest
    return files, expected


def observe_manual_controller_triplet(
    root: Path,
    release_manifest_sha256: str,
    *,
    qualification_endpoints: Path | None = None,
) -> dict[str, Any]:
    files, expected = _manual_artifacts(root, release_manifest_sha256)
    if qualification_endpoints is not None:
        endpoint_path = qualification_endpoints.expanduser().resolve(strict=True)
        endpoint = json.loads(endpoint_path.read_text(encoding="utf-8"))
        addresses = endpoint.get("addresses") if isinstance(endpoint, Mapping) else None
        if (
            endpoint.get("schema")
            != "step5d.autotune-v3/qualification-endpoint-config-v1"
            or endpoint.get("motion_capable") is not False
            or not isinstance(addresses, Mapping)
            or any(
                not isinstance(addresses.get(role), Mapping)
                or addresses[role].get("host") != "127.0.0.1"
                for role in ("dashboard", "secondary", "rtde", "kunwei")
            )
        ):
            raise ManualBridgeError("Manual qualification endpoint identity differs")
        return {
            "schema": "step5d.manual-v2/controller-triplet-observation-v1",
            "ok": True,
            "mode": "qualification_endpoint_substitution",
            "observed_at_unix_ns": time.time_ns(),
            "expected_sha256": expected,
            "observed_sha256": dict(expected),
            "endpoint": {
                "path": str(endpoint_path),
                "sha256": hashlib.sha256(endpoint_path.read_bytes()).hexdigest(),
                "content_sha256": endpoint.get("content_sha256"),
                "controller_contacted": False,
            },
            "owner_helper": None,
        }
    try:
        helper, helper_sha256 = tp_upload.resolve_live_controller_helper(None, None)
        observed = tp_upload.readback_controller_sha256(
            helper,
            files,
            PROGRAM,
            TARGET_DIR,
            helper_sha256=helper_sha256,
            local_sha=expected,
        )
    except SystemExit as exc:
        raise ManualBridgeError(
            "Manual controller fresh read-back helper failed"
        ) from exc
    if observed != expected:
        raise ManualBridgeError("Manual controller fresh read-back triplet differs")
    return {
        "schema": "step5d.manual-v2/controller-triplet-observation-v1",
        "ok": True,
        "mode": "fresh_controller_get",
        "observed_at_unix_ns": time.time_ns(),
        "expected_sha256": expected,
        "observed_sha256": observed,
        "endpoint": None,
        "owner_helper": {"path": str(helper), "sha256": helper_sha256},
    }


def _read_rtde_once(
    host: str,
    fields: list[str],
    *,
    frequency_hz: float,
    timeout: float,
) -> dict[str, Any]:
    with RTDEClient(host, timeout=timeout) as client:
        client.negotiate()
        recipe_id, type_names = client.setup_outputs(frequency_hz, fields)
        client.start()
        values = client.recv_recipe_sample(recipe_id, type_names)
    return {
        **dict(zip(fields, values, strict=True)),
        "_recipe_id": recipe_id,
        "_recipe_types": type_names,
    }


def _program_safe(dashboard: Mapping[str, Any], rtde: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(dashboard.get("programState", dashboard.get("program_state", "")))
    state = raw.split(maxsplit=1)[0].upper() if raw else ""
    expected = f"/programs/andyl/kunwei/step5/{PROGRAM}.urp"
    loaded = str(
        dashboard.get("get loaded program", dashboard.get("loaded_program", raw))
    )
    exact_program = loaded_program_matches(loaded, expected)
    checks = {
        "exact_program": exact_program,
        "stopped": state == "STOPPED",
    }
    return {
        "ok": all(checks.values()),
        "mode": "loaded_stopped" if checks["stopped"] else "not_stopped",
        "checks": checks,
        "raw": raw,
    }


def run_preflight(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    context = load_context(ROOT, args.bridge_start_context)
    launch = load_manual_launch_profile(args.launch_profile)
    governed_argv = build_bridge_argv(
        args.mailbox.parent,
        launch_profile=launch,
        trial_overlay=DEFAULT_OVERLAY,
    )
    local = r009_preflight._parallel({
        "route_robot": lambda: support.run_command(["ip", "route", "get", args.robot_host]),
        "route_kunwei": lambda: support.run_command(["ip", "route", "get", args.sensor_ip]),
        "writer": support.no_existing_writer,
        "realtime": support.realtime_capability,
        "runtime_calibration": dependency_observation,
        "production_startup_prewarm": lambda: r009_bridge.check_v3_runtime_prewarm(governed_argv[2:]),
    })
    local_ok = all(r009_preflight._value(item).get("ok") is True for item in local.values())
    if not local_ok:
        remote: dict[str, Any] = {"skipped": "local_stage_failed"}
        dashboard: Mapping[str, Any] = {}
        rtde: Mapping[str, Any] = {}
    else:
        remote = r009_preflight._parallel({
            "dashboard": lambda: dashboard_exchange(
                args.robot_host,
                [
                    "PolyscopeVersion", "is in remote control", "safetymode",
                    "robotmode", "programState", "get loaded program",
                ],
                timeout=args.timeout_s,
            ),
            "rtde": lambda: _read_rtde_once(
                args.robot_host,
                [
                    *support.RTDE_FIELDS,
                    "actual_qd",
                    *[f"output_int_register_{index}" for index in range(24, 35)],
                ],
                frequency_hz=10.0,
                timeout=args.timeout_s,
            ),
            "secondary_port": lambda: support.probe_port(args.robot_host, 30002, timeout=args.timeout_s),
            "kunwei": lambda: support.tcp_connect_only(args.sensor_ip, args.sensor_port, args.timeout_s),
        })
        dashboard = r009_preflight._value(remote["dashboard"])
        rtde = r009_preflight._value(remote["rtde"])
    controller_identity, controller_sha = r009_preflight._controller_identity(
        dashboard, rtde, robot_host=args.robot_host
    )
    controller_triplet = observe_manual_controller_triplet(
        ROOT,
        context["manual_release_manifest_sha256"],
        qualification_endpoints=args.qualification_endpoints,
    )
    predicates = {
        "safety_normal": r009_preflight._safety_normal(dashboard),
        "program_safe_for_bridge": _program_safe(dashboard, rtde),
        "robot_stationary": r009_preflight._stationary(rtde),
        "prealign_start_clearance": r009_preflight._prealign_start_clearance(rtde),
        "no_existing_writer": {
            "ok": r009_preflight._value(local.get("writer", {})).get("ok") is True,
            "observation": r009_preflight._value(local.get("writer", {})),
        },
        "mailbox_initial_zero": r009_preflight._mailbox_initial_zero(args.mailbox),
        "runtime_dependencies": {
            "ok": all(
                r009_preflight._value(local.get(name, {})).get("ok") is True
                for name in ("runtime_calibration", "production_startup_prewarm")
            ),
            "calibration": r009_preflight._value(local.get("runtime_calibration", {})),
            "production_startup_prewarm": r009_preflight._value(local.get("production_startup_prewarm", {})),
        },
        "controller_artifact_identity": {
            "ok": controller_triplet["ok"] is True,
            "observation": controller_triplet,
        },
    }
    transport_ok = (
        local_ok
        and "dashboard" in remote
        and bool(dashboard)
        and r009_preflight._value(remote["dashboard"]).get("ok", True) is True
        and "rtde" in remote
        and support.rtde_predicate(dict(rtde)).get("ok") is True
        and r009_preflight._connect_observation_ok(remote.get("secondary_port", {}))
        and r009_preflight._connect_observation_ok(remote.get("kunwei", {}))
    )
    payload = {
        "schema": PREFLIGHT_SCHEMA,
        "ok": transport_ok and all(item["ok"] is True for item in predicates.values()),
        "fresh": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": time.monotonic() - started,
        "candidate_stage_id": RELEASE_STAGE,
        "control_profile_id": CONTROL_PROFILE,
        "tp_program_id": PROGRAM,
        "manual_release_manifest_sha256": context["manual_release_manifest_sha256"],
        "bridge_start_context_sha256": sha256_path(args.bridge_start_context),
        "launch_profile_fingerprint": launch.fingerprint,
        "controller_identity": controller_identity,
        "controller_identity_sha256": controller_sha,
        "predicates": predicates,
        "observations": {"local": local, "remote": remote},
        "safety_boundary": [
            "Dashboard read-only commands only",
            "RTDE output recipe only",
            "Kunwei TCP connect-only; no stream, zero, or tare command",
            "no bridge, RTDE input, Load, Play, ARM, contact, or motion",
        ],
    }
    atomic_json(args.output, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--robot-host", default="192.168.1.18")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--timeout-s", type=float, default=2.0)
    parser.add_argument("--mailbox", type=Path, required=True)
    parser.add_argument("--bridge-start-context", type=Path, required=True)
    parser.add_argument("--launch-profile", type=Path, default=DEFAULT_LAUNCH_PROFILE)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qualification-endpoints", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = run_preflight(args)
    except Exception as exc:
        payload = {"schema": PREFLIGHT_SCHEMA, "ok": False, "fresh": False, "blocker": str(exc)}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
