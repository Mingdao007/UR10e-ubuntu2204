"""Read-only admission for the exact TP-local Step5d release."""

from __future__ import annotations

import hashlib
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from .dashboard import dashboard_exchange
from .delivery_observation import resolve_delivery_observation
from .profile import ContractViolation, load_contract
from .release_identity import (
    SAFETY_ENVELOPE_PATH,
    ReleaseIdentity,
    load_runtime_release,
    release_payload_path,
)
from .runtime_gate import loaded_program_matches, release_runtime_contract


SCHEMA = "step5d.autotune-v3/bridge-admission-v1"


class BridgeAdmissionError(RuntimeError):
    """The read-only admission observation could not be completed safely."""


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise BridgeAdmissionError("delivery observation is missing or unsafe")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def release_robot_host(root: Path, release: ReleaseIdentity) -> str:
    contract_path = release_payload_path(root, release, SAFETY_ENVELOPE_PATH)
    contract = load_contract(contract_path)
    try:
        robot_host = contract["effective_fields"]["runtime_identity"]["robot_host"]
    except (KeyError, TypeError) as exc:
        raise ContractViolation(
            "immutable release contract lacks robot_host"
        ) from exc
    if not isinstance(robot_host, str) or not robot_host or any(
        character in robot_host for character in ("\x00", "\r", "\n")
    ):
        raise ContractViolation("immutable release robot_host is unsafe")
    return robot_host


def compute_program_admission(
    dashboard: Mapping[str, Any],
    *,
    expected_program: str,
) -> dict[str, Any]:
    raw_state = str(
        dashboard.get("programState", dashboard.get("program_state", ""))
    )
    raw_loaded = str(
        dashboard.get("get loaded program", dashboard.get("loaded_program", ""))
    )
    state = raw_state.split(maxsplit=1)[0].upper() if raw_state else ""
    checks = {
        "exact_program_loaded": loaded_program_matches(
            raw_loaded,
            expected_program,
        ),
        "program_stopped": state == "STOPPED",
    }
    ready = all(checks.values())
    return {
        "state": "BENCH_READY" if ready else "ACTION_REQUIRED",
        "ok": ready,
        "reason_code": (
            "PROGRAM_LOADED_STOPPED"
            if ready
            else "EXTERNAL_ACTION_REQUIRED"
        ),
        "checks": checks,
        "program_state": raw_state,
        "loaded_program": raw_loaded,
        "expected_loaded_program": expected_program,
        "operator_action": (
            None
            if ready
            else "LOAD_EXACT_PROGRAM_ON_TP_AND_LEAVE_STOPPED"
        ),
    }


def observe_bridge_admission(
    root: Path,
    *,
    compatibility_delivery_observation: Path | None = None,
    dashboard_reader: Callable[..., Mapping[str, Any]] = dashboard_exchange,
    robot_host: str | None = None,
    timeout_s: float = 3.0,
) -> dict[str, Any]:
    experiment = root.resolve(strict=True)
    release = load_runtime_release(experiment)
    runtime_contract = release_runtime_contract(experiment, release)
    delivery_path, delivery = resolve_delivery_observation(
        experiment,
        release=release,
        compatibility_path=compatibility_delivery_observation,
    )
    host = robot_host or release_robot_host(experiment, release)
    try:
        dashboard = dashboard_reader(
            host,
            ["programState", "get loaded program"],
            timeout=timeout_s,
        )
    except Exception as exc:
        raise BridgeAdmissionError(
            f"Dashboard read-only admission failed: {type(exc).__name__}:{exc}"
        ) from exc
    if not isinstance(dashboard, Mapping):
        raise BridgeAdmissionError("Dashboard admission response is not an object")
    computed = compute_program_admission(
        dashboard,
        expected_program=str(runtime_contract["expected_loaded_program"]),
    )
    return {
        "schema": SCHEMA,
        "observed_at_unix_ns": time.time_ns(),
        **computed,
        "release": {
            "manifest_sha256": release.manifest_sha256,
            "program_id": release.program_id,
        },
        "delivery_observation": {
            "path": delivery_path.relative_to(experiment).as_posix(),
            "sha256": _sha256(delivery_path),
            "transaction_id": delivery["transaction_id"],
        },
        "dashboard": {
            "host": host,
            "commands": ["programState", "get loaded program"],
        },
        "authority_acquired": False,
        "attempt_created": False,
    }


__all__ = [
    "BridgeAdmissionError",
    "SCHEMA",
    "compute_program_admission",
    "observe_bridge_admission",
    "release_robot_host",
]
