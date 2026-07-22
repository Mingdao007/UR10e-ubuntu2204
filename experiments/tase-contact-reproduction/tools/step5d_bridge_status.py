#!/usr/bin/env python3
"""Reduce the latest canonical Step5d attempt into one route-neutral status."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping

from step5d_autotune_v3.governance import (
    EXTERNAL_REASON_CODES,
    LAUNCH_ATTEMPT_SCHEMA,
    LaunchAttemptAttestationError,
    LaunchAttemptPointerError,
    PHYSICAL_REASON_CODES,
    load_current_launch_attempt,
    read_proc_starttime_ticks,
    resolve_governed_status,
)
from step5d_autotune_v3.profile import canonical_json_bytes
from step5d_manual_status import read_run_status
from step5d_bridge_authority import (
    BridgeAuthorityError,
    load_current as load_owner_authority,
)


STATUS_SCHEMA = "step5d.bridge/governed-status-v2"
CLAIM_SCHEMA = "step5d.bridge/readiness-claim-v1"
AUTHORITY_RELATIVE = Path("runs/step5d_bridge_authority")
CLAIM_TTL_NS = 5_000_000_000


def _launch_view(attempt: Mapping[str, Any] | None) -> dict[str, Any]:
    if attempt is None:
        return {
            "present": False,
            "schema": None,
            "attempt_id": None,
            "state": None,
            "phase": None,
            "route": None,
            "observed_at_unix_ns": None,
            "bindings": None,
        }
    return {
        "present": True,
        "schema": attempt["schema"],
        "attempt_id": attempt["attempt_id"],
        "state": attempt["state"],
        "phase": attempt["phase"],
        "route": attempt.get("route"),
        "observed_at_unix_ns": attempt["observed_at_unix_ns"],
        "bindings": attempt.get("bindings"),
    }


def _base_status(reason: str, *, attempt: Mapping[str, Any] | None) -> dict[str, Any]:
    route = None if attempt is None else attempt.get("route")
    blocker_class = (
        "BLOCKED_EXTERNAL"
        if reason in EXTERNAL_REASON_CODES
        else "PHYSICAL"
        if reason in PHYSICAL_REASON_CODES
        else "INTERNAL"
    )
    return {
        "schema": STATUS_SCHEMA,
        "route": route,
        "state": None,
        "predicates": {
            "offline_proven": False,
            "production_path_qualified": False,
            "bridge_process_alive": False,
            "bridge_heartbeat_fresh": False,
            "authorization_scope_valid": False,
            "play_prompt_ready": False,
        },
        "blocker": {"class": blocker_class, "reason_codes": [reason], "evidence": []},
        "next_action": {
            "NO_CANONICAL_LAUNCH_ATTEMPT": "start_canonical_bridge",
            "LAUNCH_ATTEMPT_FAILED": "inspect_launch_attempt_evidence",
            "LAUNCH_ATTEMPT_CANCELLED": "start_new_canonical_attempt",
            "ROUTE_NOT_RESOLVED": "collect_fresh_route_snapshot",
            "ROUTE_RUNTIME_NOT_OBSERVED": "wait_for_route_runtime_observation",
            "LAUNCH_ATTEMPT_BINDING_INVALID": "repair_launch_attempt_binding",
            "CURRENT_RELEASE_INVALID": "repair_current_release_before_retry",
            "LOADED_PROGRAM_UNSUPPORTED": "load_exact_supported_program_before_retry",
        }.get(reason, "repair_internal_governance_state"),
        "launch_attempt": _launch_view(attempt),
    }


def _load_attempt(root: Path) -> tuple[dict[str, Any] | None, str | None]:
    authority = root / AUTHORITY_RELATIVE
    try:
        attempt, _ = load_current_launch_attempt(authority)
        return attempt, None
    except LaunchAttemptAttestationError:
        return None, "LAUNCH_ATTEMPT_ATTESTATION_INVALID"
    except LaunchAttemptPointerError:
        if (authority / "governance/current-launch.json").exists():
            return None, "LAUNCH_ATTEMPT_POINTER_INVALID"
        return None, None


def _binding_valid(root: Path, attempt: Mapping[str, Any]) -> bool:
    if attempt.get("schema") != LAUNCH_ATTEMPT_SCHEMA:
        return False
    bindings = attempt.get("bindings")
    if not isinstance(bindings, Mapping):
        return False
    owner = bindings.get("resource_owner")
    if not isinstance(owner, Mapping):
        return False
    pid = owner.get("pid")
    starttime = owner.get("starttime_ticks")
    authority_epoch = owner.get("authority_epoch")
    if (
        not isinstance(pid, int)
        or not isinstance(starttime, int)
        or not isinstance(authority_epoch, int)
    ):
        return False
    completed = attempt.get("state") == "COMPLETED"
    current_starttime = read_proc_starttime_ticks(pid)
    if not completed and current_starttime != starttime:
        return False
    try:
        authority = load_owner_authority(root / AUTHORITY_RELATIVE)
    except (OSError, ValueError, BridgeAuthorityError):
        return False
    expected_authority_state = "REVOKED" if completed else "ACTIVE"
    expected_sequence = authority_epoch + 1 if completed else authority_epoch
    if (
        authority is None
        or authority.get("state") != expected_authority_state
        or authority.get("sequence") != expected_sequence
        or authority.get("attempt_id") != attempt.get("attempt_id")
        or authority.get("owner") != {"pid": pid, "starttime_ticks": starttime}
        or (completed and authority.get("reason") != "completed")
    ):
        return False
    snapshot = bindings.get("route_snapshot")
    if snapshot is None:
        return attempt.get("route") == "UNKNOWN"
    if not isinstance(snapshot, Mapping):
        return False
    path = Path(str(snapshot.get("path", "")))
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        return False
    if hashlib.sha256(path.read_bytes()).hexdigest() != snapshot.get("sha256"):
        return False
    try:
        route_snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(route_snapshot, Mapping)
        and route_snapshot.get("schema") == "step5d.bridge-route/v2"
        and route_snapshot.get("route") == attempt.get("route")
        and route_snapshot.get("read_only") is True
    )


def _scope_valid(capabilities: Any) -> bool:
    return bool(
        isinstance(capabilities, Mapping)
        and all(capabilities.get(name) is True for name in ("bridge", "play", "arm", "motion"))
        and capabilities.get("zero") is False
        and capabilities.get("tare") is False
    )


def _apply_attempt_gate(
    status: dict[str, Any], attempt: Mapping[str, Any]
) -> dict[str, Any]:
    status["schema"] = STATUS_SCHEMA
    status["route"] = attempt.get("route")
    status["launch_attempt"] = _launch_view(attempt)
    status.setdefault("predicates", {})
    status["predicates"]["production_path_qualified"] = bool(
        status["predicates"].get("offline_proven") is True
    )
    bindings = attempt.get("bindings") or {}
    scope = bool(
        status.get("predicates", {}).get("authorization_scope_valid") is True
        or (
            attempt.get("route") == "autotune_v3"
            and status.get("predicates", {}).get("lease_valid") is True
        )
        or _scope_valid(bindings.get("capabilities"))
    )
    status["predicates"]["authorization_scope_valid"] = scope
    terminal = attempt["state"] in {"FAILED", "CANCELLED"}
    if terminal:
        reason = (
            "LAUNCH_ATTEMPT_CANCELLED"
            if attempt["state"] == "CANCELLED"
            else str(attempt.get("reason_code") or "LAUNCH_ATTEMPT_FAILED")
        )
        blocked = _base_status(reason, attempt=attempt)
        blocked["blocker"]["evidence"] = [
            {
                "role": "launch_attempt",
                "detail": attempt.get("detail"),
                "phase": attempt.get("phase"),
                "exit_code": attempt.get("exit_code"),
            }
        ]
        return blocked
    if attempt["state"] == "COMPLETED":
        status["predicates"]["authorization_scope_valid"] = False
        status["predicates"]["play_prompt_ready"] = False
        status["next_action"] = "inspect_outcome_or_start_new_canonical_attempt"
        return status
    if not scope:
        status["predicates"]["play_prompt_ready"] = False
        if status.get("state") in {"WAITING_FOR_IDENTITY_PLAY", "WAITING_FOR_PLAY"}:
            status["state"] = "BRIDGE_ALIVE_NO_ARM"
        status["next_action"] = "await_explicit_play_arm_motion_authorization_or_stop"
    return status


def resolve_status(experiment_root: Path) -> dict[str, Any]:
    root = experiment_root.expanduser().resolve(strict=True)
    attempt, attempt_error = _load_attempt(root)
    if attempt_error is not None:
        return _base_status(attempt_error, attempt=None)
    if attempt is None:
        return _base_status("NO_CANONICAL_LAUNCH_ATTEMPT", attempt=None)
    if attempt["state"] in {"FAILED", "CANCELLED"}:
        return _apply_attempt_gate(
            _base_status(
                "LAUNCH_ATTEMPT_CANCELLED"
                if attempt["state"] == "CANCELLED"
                else "LAUNCH_ATTEMPT_FAILED",
                attempt=attempt,
            ),
            attempt,
        )
    if not _binding_valid(root, attempt):
        return _base_status("LAUNCH_ATTEMPT_BINDING_INVALID", attempt=attempt)
    route = attempt.get("route")
    if route == "UNKNOWN":
        return _base_status("ROUTE_NOT_RESOLVED", attempt=attempt)
    campaign_root = Path(attempt["bindings"]["campaign_root"])
    if route == "manual_v2":
        try:
            manual = read_run_status(campaign_root)
        except (OSError, ValueError, json.JSONDecodeError):
            return _base_status("ROUTE_RUNTIME_NOT_OBSERVED", attempt=attempt)
        if manual.get("launch_attempt_id") != attempt.get("attempt_id"):
            return _base_status("LAUNCH_ATTEMPT_BINDING_INVALID", attempt=attempt)
        status = {
            "schema": STATUS_SCHEMA,
            "route": "manual_v2",
            "state": manual.get("state"),
            "predicates": {
                "offline_proven": manual.get("offline_proven") is True,
                "production_path_qualified": manual.get("offline_proven") is True,
                "bridge_process_alive": manual.get("bridge_heartbeat") is True,
                "bridge_heartbeat_fresh": manual.get("bridge_heartbeat") is True,
                "controller_identity_fresh": manual.get("controller_identity_fresh")
                is True,
                "authorization_scope_valid": _scope_valid(manual.get("capabilities")),
                "play_prompt_ready": manual.get("play_prompt_ready") is True,
            },
            "blocker": {
                "class": None if manual.get("blocker") is None else "INTERNAL",
                "reason_codes": [] if manual.get("blocker") is None else [manual["blocker"]],
                "evidence": [],
            },
            "next_action": manual.get("next_action"),
            "manual": manual,
        }
        return _apply_attempt_gate(status, attempt)
    status = resolve_governed_status(root, campaign_root)
    return _apply_attempt_gate(status, attempt)


def readiness_claim(status: Mapping[str, Any], required_state: str) -> dict[str, Any]:
    if required_state not in {"WAITING_FOR_IDENTITY_PLAY", "WAITING_FOR_PLAY"}:
        raise ValueError("only pre-Play readiness states can be asserted")
    predicates = status.get("predicates")
    if (
        status.get("state") != required_state
        or not isinstance(predicates, Mapping)
        or predicates.get("play_prompt_ready") is not True
        or predicates.get("authorization_scope_valid") is not True
        or predicates.get("offline_proven") is not True
        or predicates.get("production_path_qualified") is not True
        or (
            status.get("route") == "manual_v2"
            and predicates.get("controller_identity_fresh") is not True
        )
    ):
        raise ValueError("machine status does not authorize the requested readiness claim")
    issued = time.time_ns()
    status_sha = hashlib.sha256(canonical_json_bytes(dict(status))).hexdigest()
    return {
        "schema": CLAIM_SCHEMA,
        "state": required_state,
        "attempt_id": status["launch_attempt"]["attempt_id"],
        "status_sha256": status_sha,
        "issued_at_unix_ns": issued,
        "expires_at_unix_ns": issued + CLAIM_TTL_NS,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, required=True)
    parser.add_argument(
        "--assert-state",
        choices=("WAITING_FOR_IDENTITY_PLAY", "WAITING_FOR_PLAY"),
    )
    args = parser.parse_args(argv)
    try:
        status = resolve_status(args.experiment_root)
        payload = status if args.assert_state is None else readiness_claim(status, args.assert_state)
    except Exception as exc:
        print(f"Step5d governed status unavailable: {type(exc).__name__}:{exc}", file=sys.stderr)
        return 3
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
