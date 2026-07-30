"""Strict bridge start-context loader for the force-search canary."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SOURCE = ROOT.parents[1] / "src/ur10e_experiment_runtime"
if str(RUNTIME_SOURCE) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SOURCE))

import kunwei_rtde_bridge as bridge  # noqa: E402


DEFAULT_CONTRACT = ROOT / "config/step5d/force_search_bridge_v1.json"
SCHEMA = "step5d.force-search-bridge/v1"
ARTIFACT_ID = "new-eoat-force-search-observer-20260730"


class ForceSearchBridgeContractError(RuntimeError):
    """The frozen force-search bridge context is invalid."""


@dataclass(frozen=True)
class ForceSearchBridgeContract:
    artifact_id: str
    artifact_sha256: str
    argv: tuple[str, ...]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_bridge_contract(
    path: Path = DEFAULT_CONTRACT,
) -> ForceSearchBridgeContract:
    if path.is_symlink() or not path.is_file():
        raise ForceSearchBridgeContractError(
            f"bridge contract must be a regular file: {path}"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ForceSearchBridgeContractError(
            f"bridge contract is unreadable: {exc}"
        ) from exc
    if document.get("schema") != SCHEMA or document.get("artifact_id") != ARTIFACT_ID:
        raise ForceSearchBridgeContractError("bridge contract identity differs")
    if document.get("bridge") != "tools/kunwei_rtde_bridge.py":
        raise ForceSearchBridgeContractError("bridge implementation binding differs")
    argv = document.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or any(not isinstance(value, str) or not value for value in argv)
    ):
        raise ForceSearchBridgeContractError("bridge argv is invalid")
    parsed = bridge.parse_args(argv)
    required = {
        "robot_host": "192.168.1.18",
        "sensor_ip": "192.168.50.25",
        "sensor_port": 5152,
        "allow_kunwei_stream_command": True,
        "write_rtde_inputs": True,
        "baseline_s": 5.0,
        "rezero_s": 1.0,
        "duration_s": 180.0,
        "rtde_hz": 125.0,
        "target_force_n": 1.0,
        "normal_axis": "fz",
        "normal_sign": -1.0,
        "sensor_stale_s": 0.08,
        "max_normal_force_n": 3.0,
        "max_force_norm_n": 3.0,
        "max_torque_norm_nm": 0.2,
        "bridge_mode": "off",
        "bridge_profile": "",
        "step5b_trial_profile": "none",
    }
    mismatches = {
        name: {"expected": expected, "actual": getattr(parsed, name, None)}
        for name, expected in required.items()
        if getattr(parsed, name, None) != expected
    }
    if mismatches:
        raise ForceSearchBridgeContractError(
            f"bridge parsed invariants differ: {mismatches}"
        )
    if getattr(parsed, "step5d_autotune_command_mailbox", None) is not None:
        raise ForceSearchBridgeContractError("autotune mailbox must be disabled")
    state = bridge.BridgeState()
    command_values = bridge.compute_bridge_values(
        parsed,
        [0.0] * 6,
        {
            "actual_TCP_pose": [0.0] * 6,
            "actual_TCP_speed": [0.0] * 6,
            "output_double_register_35": 11.0,
        },
        1.0,
        state,
        1.0 / parsed.rtde_hz,
    )
    nonzero = {
        name: command_values.get(name)
        for name in bridge.BRIDGE_INPUT_NAMES
        if command_values.get(name, 0.0) != 0.0
    }
    if nonzero:
        raise ForceSearchBridgeContractError(
            f"observer-only bridge produced command-register values: {nonzero}"
        )
    return ForceSearchBridgeContract(
        artifact_id=document["artifact_id"],
        artifact_sha256=_sha256(path),
        argv=tuple(argv),
    )


__all__ = [
    "ARTIFACT_ID",
    "DEFAULT_CONTRACT",
    "ForceSearchBridgeContract",
    "ForceSearchBridgeContractError",
    "load_bridge_contract",
]
