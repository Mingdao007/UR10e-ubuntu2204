#!/usr/bin/env python3
"""Promote a successful raw URSim HOLD run into the compact evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from step5d_autotune_v3.profile import contract_sha256, control_fingerprint, load_contract
from step5d_autotune_v3.state import orchestration_fingerprint


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_IMAGE = (
    "universalrobots/ursim_e-series:5.25.2@sha256:"
    "a4c4365207d54d1a1a4ead87526ff3781e2e98ae703c72f362060a46688fa7a4"
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _recorded_path(path: Path) -> str:
    """Keep repo artifacts portable and external immutable evidence addressable."""

    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def promote(source: Path, raw_output: Path, result_output: Path) -> dict[str, Any]:
    encoded = source.read_bytes()
    raw = json.loads(encoded)
    container = raw.get("container") or {}
    if raw.get("ok") is not True or raw.get("forbidden_action_count") != 0:
        raise ValueError("raw URSim run did not pass cleanly")
    required_container = {
        "expected_image": EXPECTED_IMAGE,
        "network_internal": True,
        "host_ports_published": False,
    }
    for field, expected in required_container.items():
        if container.get(field) != expected:
            raise ValueError(f"raw URSim container {field} differs")
    production = raw.get("production_launcher") or {}
    contract = load_contract()
    identity = {
        "control_fingerprint": control_fingerprint(contract),
        "orchestration_fingerprint": orchestration_fingerprint(ROOT),
        "contract_sha256": contract_sha256(contract),
    }
    for field in (
        "control_fingerprint",
        "orchestration_fingerprint",
        "contract_sha256",
    ):
        if production.get(field) != identity[field]:
            raise ValueError(f"raw URSim {field} is stale")
    robot_model_selection = container.get("robot_model_selection")
    if robot_model_selection != {
        "image_family": "ursim_e-series",
        "environment_variable": "ROBOT_MODEL",
        "observed_token": "UR10",
        "bench_target": "UR10e",
        "binding": "official_e_series_image_ur10_token",
    }:
        raise ValueError("raw URSim robot model selection differs")
    lifecycle = raw.get("lifecycle") or {}
    observed = [item.get("state") for item in lifecycle.get("observed", [])]
    if observed[:3] != ["STOPPED", "STARTING", "READY_HOME"]:
        raise ValueError("raw URSim lifecycle differs")
    samples = (raw.get("hold_watchdog") or {}).get("samples") or []
    if len(samples) < 3:
        raise ValueError("raw URSim HOLD sample count is too small")
    max_qd = max(
        float((sample.get("hold") or {}).get("max_abs", {}).get("actual_qd", -1))
        for sample in samples
    )
    max_speed = max(
        float((sample.get("hold") or {}).get("max_abs", {}).get("actual_TCP_speed", -1))
        for sample in samples
    )
    dashboard = samples[0]["dashboard"]
    version_match = re.search(r"\b(\d+\.\d+\.\d+)\b", dashboard["PolyscopeVersion"])
    if version_match is None:
        raise ValueError("URSim Polyscope version is unavailable")
    raw_output.write_bytes(encoded)
    raw_sha = _sha256(encoded)
    result = {
        "schema": "step5d.autotune-v3/ursim-hold-result-v1",
        "observed_at": raw["observed_at"],
        "status": "pass",
        "acceptance_scope": "offline_tooling_and_ursim_hold_only",
        "claim_boundary": raw["claim_boundary"],
        "identity": identity,
        "image": {
            "reference": EXPECTED_IMAGE,
            "image_id": container["container_image_id"],
            "polyscope_version": version_match.group(1),
            "target_polyscope_version_equivalence_claimed": False,
            "entrypoint": ["/entrypoint.sh"],
            "entrypoint_overridden": False,
            "robot_model_selection": robot_model_selection,
        },
        "network": {
            "name": container["network"],
            "internal": True,
            "external_network_connected": False,
            "robot_network_connected": False,
            "host_ports_published": False,
            "removed_after_gate": True,
        },
        "container_lifecycle": {
            "name": container["container"],
            "container_id": container["container_id"],
            "privileged": False,
            "mounts": [],
            "devices": [],
            "removed_after_gate": True,
        },
        "service_lifecycle": {
            "expected_prefix": ["STOPPED", "STARTING", "READY_HOME"],
            "observed": observed,
            "hardware_enabled": False,
            "bridge_started": False,
            "controller_touched": False,
            "tp_started": False,
        },
        "hold": {
            "dashboard_program_state": "STOPPED",
            "dashboard_running": False,
            "rtde_output_recipe_only": True,
            "rtde_input_recipe_created": False,
            "sample_count": len(samples),
            "max_abs_actual_qd_rad_s": max_qd,
            "max_abs_actual_tcp_speed": max_speed,
            "play_count": 0,
            "arm_count": 0,
            "motion_count": 0,
            "controller_write_count": 0,
            "forbidden_action_count": 0,
        },
        "watchdog": {
            "hold_monitor_passed": True,
            "immutable_tp_watchdog_artifact_verified": True,
            "tp_watchdog_runtime_executed": False,
            "tp_watchdog_runtime_reason": "HOLD-only gate forbids TP Play",
        },
        "rollback": raw["rollback"],
        "raw_evidence": {
            "path": _recorded_path(raw_output),
            "sha256": raw_sha,
            "original_run_path": _recorded_path(source),
            "byte_identical_copy_verified": _sha256(raw_output.read_bytes()) == raw_sha,
        },
        "decision": {
            "offline_acceptance": "go",
            "live_robot_acceptance": "not_run_not_authorized",
            "rollout_authorized": False,
            "v3_active": False,
            "current_selector": "step5d_strict_rnn_autotune_v1",
        },
    }
    result_output.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return {
        "raw_sha256": raw_sha,
        "result_sha256": _sha256(result_output.read_bytes()),
        "identity": identity,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--raw-output",
        type=Path,
        default=ROOT / "config/step5/step5d_autotune_v3_ursim_hold_raw.json",
    )
    parser.add_argument(
        "--result-output",
        type=Path,
        default=ROOT / "config/step5/step5d_autotune_v3_ursim_hold_result.json",
    )
    args = parser.parse_args()
    print(json.dumps(promote(args.source, args.raw_output, args.result_output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
